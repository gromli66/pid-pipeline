"""
Async-safe Celery dispatch for API endpoints.

Analog of worker.utils.db_helpers.safe_dispatch, but for async context:
- accepts AsyncSession (not sync Session)
- wraps send_task in asyncio.to_thread (does not block event loop)
- supports queue parameter
- on failure: logs warning, returns None

⚠ `None` — это НЕ «ничего не произошло, оператор повторит». Прежняя редакция
этой докстроки так и обещала («does NOT change diagram status, so user can
retry via UI»), и обещание было ложным: статус к моменту вызова УЖЕ закоммичен
вперёд самим эндпоинтом, а вернуть его назад отсюда некому. Замер ноги 1.16
(MEASUREMENTS §99): при мёртвом брокере четыре эндпоинта `app/api/validation.py`
отдавали 200 с `task_id: null` — тот же ответ, что при идемпотентном «цепочка
ушла вперёд», — и диаграмму, из которой задача уже не выйдет.

Поэтому решение об исходе принимает ВЫЗЫВАЮЩИЙ, и оно обязано быть явным.
Обе действующие политики:

- `app/api/validation.py` — вернуть состояние, каким оно было ДО вызова
  (все три поля), и ответить 503 (`_restore_and_fail`; правило записано
  в `docs/STATUS_MACHINE.md §5`). Исключение — параллельный OCR
  в `junctions/complete`: там граф уже поставлен, возврат осиротил бы
  бегущую задачу, поэтому отказ проговаривается в ответе и в логе;
- `app/services/layout_dispatch.py` — стадия `LAYOUT` становится `FAILED`,
  иначе гейт «Ручной правки» ждал бы задачу, которой нет.

Ловится `Exception`, а не узкий класс, и это замер, а не перестраховка: на бою
брокер и result-бэкенд сидят на одном Redis, и `send_task` бросает `RuntimeError`
retry-цикла БЭКЕНДА (≈64 с), а не `OperationalError` брокера (≈4 с) —
`docs/STATUS_MACHINE.md §5`, MEASUREMENTS §87д/§91.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def async_safe_dispatch(
    task_name: str,
    args: list,
    queue: Optional[str] = None,
) -> Optional[str]:
    """
    Send a Celery task from an async API endpoint.

    On success returns task_id.
    On failure logs warning and returns None — исход решает вызывающий,
    см. предупреждение в шапке модуля.
    """
    from worker.celery_app import celery_app

    kwargs = {"args": args}
    if queue:
        kwargs["queue"] = queue

    try:
        result = await asyncio.to_thread(
            celery_app.send_task, task_name, **kwargs
        )
        logger.info("Dispatched %s -> %s", task_name, result.id)
        return result.id
    except Exception as exc:
        logger.warning(
            "Failed to dispatch %s: %s (user can retry via UI)",
            task_name,
            exc,
        )
        return None

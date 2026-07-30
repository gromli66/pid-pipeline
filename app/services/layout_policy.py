# -*- coding: utf-8 -*-
"""layout_policy.py — решение «ставить ли раскладку», без БД и без Celery.

Правило вынесено из `layout_dispatch.py` отдельно ровно затем, чтобы его можно
было проверить тестом: сам диспетчер тянет psycopg2, Celery и файловую
систему, и в обычном `pytest` не импортируется вовсе.

Правило (§3.2 плана docs/planning/AUTO_LAYOUT_INTEGRATION.md):

  * холст уже посчитан на эту истину -> ничего не делаем;
  * бежит задача на ТУ ЖЕ истину -> ничего не делаем (оба contours-эндпоинта
    вызываются повторно из любого статуса, а вкладка контуров открыта всегда);
  * бежит задача на ДРУГУЮ истину -> её надо отозвать и поставить новую:
    иначе при worker_concurrency=2 старая финиширует позже и перезапишет
    результат новой.
"""
from __future__ import annotations

ALREADY_FRESH = "already_fresh"
ALREADY_RUNNING = "already_running"
DISPATCH = "dispatch"


def plan_dispatch(source_sha, canvas=None, running=(), force=False):
    """Что делать с раскладкой. -> (действие, [что отозвать]).

    source_sha: sha-проекция текущего `graph_validated` — «истина».
    canvas: None или {"layout_applied": bool, "stale": bool} по холсту.
    running: [(ключ_стадии, sha_истины_стадии), ...] — незавершённые стадии.
    force: ВОЗВРАТ (откат по бусине). Холст пересчитывается всегда, даже если
        истина не менялась — решение заказчика §3.2 «повторное закрытие этапа
        перезапускает раскладку». Готовый холст тут нельзя переиспользовать не
        ради экономии, а по существу: он несёт правки оператора, сделанные ДО
        возврата, и пережить возврат они не должны.

    Ключи возвращаются в том же виде, в каком пришли: вызывающий сам знает,
    что с ними делать (revoke по celery_task_id, закрыть строку стадии).
    """
    if not force and canvas and canvas.get("layout_applied") \
            and not canvas.get("stale"):
        return ALREADY_FRESH, []

    same = [key for key, sha in running if sha == source_sha]
    if same:
        return ALREADY_RUNNING, []

    stale_tasks = [key for key, sha in running if sha != source_sha]
    return DISPATCH, stale_tasks

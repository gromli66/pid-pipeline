"""
Segmentation API — запуск цепочки: сегментация → скелетизация.

POST /{uid}/segment — основная точка входа.
Запускает полную цепочку обработки. При повторном вызове на ERROR
определяет error_stage и перезапускает с нужного шага.

Отправку держит `dispatch_segmentation` — ЕДИНСТВЕННАЯ точка постановки
сегментации. Её зовут отсюда (кнопка «Выделение труб») и `app/api/cvat.py`
(автозапуск после подтверждения разметки, Б8 плана точечных болей): конвейер
после CVAT двигает сервер, а не десктоп, иначе закрытая вкладка останавливает
схему навсегда.
"""

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import obs
from app.core.logging import get_logger
from app.db import get_async_db
from app.models import Diagram, DiagramStatus

router = APIRouter()

logger = get_logger(__name__)

# error_stage → (celery task name, target status)
_STAGE_DISPATCH = {
    "direction_classification": ("worker.tasks.segmentation.task_segment_pipes", DiagramStatus.SEGMENTING),
    "segmenting": ("worker.tasks.segmentation.task_segment_pipes", DiagramStatus.SEGMENTING),
    "skeletonizing": ("worker.tasks.skeleton.task_skeletonize", DiagramStatus.SKELETONIZING),
    "detecting_junctions": ("worker.tasks.junction.task_detect_junctions", DiagramStatus.DETECTING_JUNCTIONS),
}

# Имена задач цепочки «направление → сегментация».
_DIRECTION_TASK = "worker.tasks.direction.task_classify_direction"
_SEGMENT_TASK = "worker.tasks.segmentation.task_segment_pipes"

# Статусы, допускающие запуск сегментации
_ALLOWED_STATUSES = {
    DiagramStatus.VALIDATED_BBOX,
    DiagramStatus.ERROR,
}


def _resolve_restart(diagram: Diagram):
    """С какого шага перезапускать: (restart_from, целевой статус, задача).

    Полный запуск (и retry с шага сегментации/направления) начинается с
    классификации направления, затем сегментация: направление пишет `direction`
    в `coco_validated.json` ДО генерации `node_mask` — единый источник правды
    для маски и графа. На частичных retry (skeleton/junction) цепочка не нужна,
    диспетчится один таск.
    """
    if diagram.status == DiagramStatus.ERROR and diagram.error_stage:
        stage = diagram.error_stage
        if stage in _STAGE_DISPATCH:
            task_name, target_status = _STAGE_DISPATCH[stage]
            return stage, target_status, task_name
    return "segmenting", DiagramStatus.SEGMENTING, _SEGMENT_TASK


async def dispatch_segmentation(db: AsyncSession, diagram: Diagram) -> dict:
    """Перевести диаграмму в целевой статус и поставить сегментацию.

    Единственная точка отправки сегментации: сюда сведены и кнопка
    «Выделение труб», и автозапуск после подтверждения разметки CVAT (Б8).
    Новых сырых `send_task` правка не заводит — прежний вызов `/segment`
    переехал внутрь.

    Отправка идёт через `asyncio.to_thread`: `send_task`/`apply_async`
    синхронные, а на бою мёртвый result-бэкенд держит их до ~64 с
    (`app/services/dispatch.py`), то есть весь event loop сервера.

    Возвращает `{"task_id", "error", "restart_from", "target_status"}`.
    `task_id is None` означает, что отправка не удалась И состояние ВЕРНУТО
    к пред-вызовному (все три поля). Что ответить оператору, решает
    вызывающий: `/segment` отдаёт 503, `cvat.py` — 200 с warning, потому что
    подтверждение разметки уже состоялось и валить его брокером нельзя.
    """
    restart_from, target_status, task_name = _resolve_restart(diagram)

    # Обновляем статус ПЕРЕД запуском task (короткая транзакция).
    # Состояние до перехода держим целиком: если отправка упадёт, вернуть надо
    # всё, что переход записал, а не один статус — иначе диаграмма останется
    # в ERROR с пустым error_stage, и клиент погасит ВСЕ кнопки
    # (ui/widgets/diagram_workspace.py: _error_key).
    previous_state = (diagram.status, diagram.error_stage, diagram.error_message)
    diagram.status = target_status
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()

    from worker.celery_app import celery_app
    from celery import chain

    uid = str(diagram.uid)
    project_code = diagram.project_code

    def _send():
        # Обе ветки отправки — под одной защитой: цепочка уходит в брокер тем же
        # одним сообщением, что и одиночная задача, и падает так же.
        if restart_from in ("segmenting", "direction_classification"):
            return chain(
                celery_app.signature(_DIRECTION_TASK, args=[uid], immutable=True),
                celery_app.signature(_SEGMENT_TASK, args=[uid, project_code],
                                     immutable=True),
            ).apply_async()
        return celery_app.send_task(task_name, args=[uid, project_code])

    try:
        async_result = await asyncio.to_thread(_send)
    except Exception as exc:
        # Брокер недоступен — возвращаем состояние, каким оно было до вызова:
        # работа не начиналась, откатывать некуда, кроме исходной точки.
        # След — ДО коммита возврата: на бою БД падает вместе с брокером, и тогда
        # исключение коммита унесло бы наружу единственную запись об отказе ОТПРАВКИ.
        logger.exception(
            "Отправка сегментации не удалась (%s) — возвращаю состояние в '%s'",
            exc, previous_state[0].value, extra={"event": "dispatch_failed"},
        )
        diagram.status, diagram.error_stage, diagram.error_message = previous_state
        await db.commit()
        return {"task_id": None, "error": exc,
                "restart_from": restart_from, "target_status": target_status}

    return {"task_id": async_result.id, "error": None,
            "restart_from": restart_from, "target_status": target_status}


@router.post("/{uid}/segment")
async def start_segmentation(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Запустить цепочку: сегментация → скелетизация.

    Preconditions:
    - status == validated_bbox: полный запуск с начала
    - status == error: smart retry — определяет error_stage, перезапускает
      с нужного шага (segmenting / skeletonizing / detecting_junctions)

    Returns:
        task_id, status, restart_from (если retry)
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    # ---- Идемпотентный выход: цепочка уже идёт (образец — app/api/graph.py:61-66)
    # Сегментацию теперь ставит СЕРВЕР сразу после подтверждения разметки (Б8),
    # поэтому необновлённый десктоп зовёт `/segment` из `segmenting` на КАЖДОМ
    # счастливом пути. До этой ветки он получал 400 и показывал оператору окно
    # ошибки (`ui/widgets/diagram_workspace.py: _start_segmentation`) там, где
    # всё в порядке. Заодно закрывается и настоящий дубль: гард самой задачи
    # (`worker/tasks/segmentation.py:282`) обе копии пропускает.
    if diagram.status == DiagramStatus.SEGMENTING:
        return {
            "status": "segmenting",
            "message": "Segmentation already in progress",
            "task_id": None,
            "diagram_uid": str(uid),
            "restart_from": None,
        }

    if diagram.status not in _ALLOWED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot start segmentation: status is '{diagram.status.value}'. "
                f"Expected: validated_bbox or error"
            ),
        )

    obs.bind(uid=str(uid), phase="segmentation")
    sent = await dispatch_segmentation(db, diagram)

    if sent["task_id"] is None:
        raise HTTPException(
            status_code=503,
            detail=f"Worker unavailable: {sent['error']}",
        )

    return {
        "status": sent["target_status"].value,
        "task_id": sent["task_id"],
        "diagram_uid": str(uid),
        "restart_from": sent["restart_from"],
    }

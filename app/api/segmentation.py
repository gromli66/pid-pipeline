"""
Segmentation API — запуск цепочки: сегментация → скелетизация.

POST /{uid}/segment — основная точка входа.
Запускает полную цепочку обработки. При повторном вызове на ERROR
определяет error_stage и перезапускает с нужного шага.
"""

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

    if diagram.status not in _ALLOWED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot start segmentation: status is '{diagram.status.value}'. "
                f"Expected: validated_bbox or error"
            ),
        )

    # Определяем, с какого шага запускать
    restart_from = "segmenting"
    target_status = DiagramStatus.SEGMENTING
    task_name = "worker.tasks.segmentation.task_segment_pipes"

    if diagram.status == DiagramStatus.ERROR and diagram.error_stage:
        stage = diagram.error_stage
        if stage in _STAGE_DISPATCH:
            task_name, target_status = _STAGE_DISPATCH[stage]
            restart_from = stage

    # Обновляем статус
    # Состояние до перехода держим целиком: если отправка упадёт, вернуть надо
    # всё, что переход записал, а не один статус — иначе диаграмма останется
    # в ERROR с пустым error_stage, и клиент погасит ВСЕ кнопки
    # (ui/widgets/diagram_workspace.py: _error_key).
    obs.bind(uid=str(uid), phase="segmentation")
    previous_state = (diagram.status, diagram.error_stage, diagram.error_message)
    diagram.status = target_status
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()

    # Dispatch task через send_task (без импорта worker модулей)
    from worker.celery_app import celery_app
    from celery import chain

    # Полный запуск (и retry с шага сегментации/направления) начинается с
    # классификации направления, затем сегментация. Направление пишет
    # direction в coco_validated.json ДО генерации node_mask — единый источник
    # правды для node_mask и графа. На частичных retry (skeleton/junction)
    # цепочка не нужна — диспетчим один таск как раньше.
    # Обе ветки отправки — под одной защитой: цепочка уходит в брокер тем же
    # одним сообщением, что и одиночная задача, и падает так же.
    try:
        if restart_from in ("segmenting", "direction_classification"):
            async_result = chain(
                celery_app.signature(_DIRECTION_TASK, args=[str(uid)], immutable=True),
                celery_app.signature(_SEGMENT_TASK, args=[str(uid), diagram.project_code], immutable=True),
            ).apply_async()
        else:
            async_result = celery_app.send_task(
                task_name,
                args=[str(uid), diagram.project_code],
            )
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
        raise HTTPException(
            status_code=503,
            detail=f"Worker unavailable: {exc}",
        )

    return {
        "status": target_status.value,
        "task_id": async_result.id,
        "diagram_uid": str(uid),
        "restart_from": restart_from,
    }

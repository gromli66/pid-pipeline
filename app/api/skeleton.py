"""
Skeleton API — ручной перезапуск скелетизации.

POST /{uid}/skeletonize — запускает skeleton + junction CNN цепочку.
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


@router.post("/{uid}/skeletonize")
async def start_skeletonization(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Запустить скелетизацию + junction CNN (ручной retry).

    Preconditions:
    - status == segmented: сегментация завершена
    - status == error + error_stage == 'skeletonizing': перезапуск

    Returns:
        task_id, status
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    allowed = diagram.status in (
        DiagramStatus.SEGMENTING,
        DiagramStatus.VALIDATED_MASKS,
    ) or (
        diagram.status == DiagramStatus.ERROR
        and diagram.error_stage == "skeletonizing"
    )

    if not allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot start skeletonization: status is '{diagram.status.value}'"
                + (f", error_stage='{diagram.error_stage}'" if diagram.error_stage else "")
            ),
        )

    # Обновляем статус
    # Состояние до перехода держим целиком: если отправка упадёт, вернуть надо
    # всё, что переход записал, а не один статус — иначе диаграмма останется
    # в ERROR с пустым error_stage, и клиент погасит ВСЕ кнопки
    # (ui/widgets/diagram_workspace.py: _error_key).
    obs.bind(uid=str(uid), phase="skeleton")
    previous_state = (diagram.status, diagram.error_stage, diagram.error_message)
    diagram.status = DiagramStatus.SKELETONIZING
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()

    # Dispatch через send_task
    from worker.celery_app import celery_app

    try:
        async_result = celery_app.send_task(
            "worker.tasks.skeleton.task_skeletonize",
            args=[str(uid), diagram.project_code],
        )
    except Exception as exc:
        # Брокер недоступен — возвращаем состояние, каким оно было до вызова.
        # След — ДО коммита возврата: на бою БД падает вместе с брокером, и тогда
        # исключение коммита унесло бы наружу единственную запись об отказе ОТПРАВКИ.
        logger.exception(
            "Отправка скелетизации не удалась (%s) — возвращаю состояние в '%s'",
            exc, previous_state[0].value, extra={"event": "dispatch_failed"},
        )
        diagram.status, diagram.error_stage, diagram.error_message = previous_state
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail=f"Worker unavailable: {exc}",
        )

    return {
        "status": "skeletonizing",
        "task_id": async_result.id,
        "diagram_uid": str(uid),
    }

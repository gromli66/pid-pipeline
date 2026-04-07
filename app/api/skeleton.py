"""
Skeleton API — ручной перезапуск скелетизации.

POST /{uid}/skeletonize — запускает skeleton + junction CNN цепочку.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_async_db
from app.models import Diagram, DiagramStatus

router = APIRouter()


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
    diagram.status = DiagramStatus.SKELETONIZING
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()

    # Dispatch через send_task
    from worker.celery_app import celery_app

    async_result = celery_app.send_task(
        "worker.tasks.skeleton.task_skeletonize",
        args=[str(uid), diagram.project_code],
    )

    return {
        "status": "skeletonizing",
        "task_id": async_result.id,
        "diagram_uid": str(uid),
    }

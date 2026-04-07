"""
Junction API — ручной запуск/перезапуск детекции перекрёстков.

POST /{uid}/detect-junctions — запускает junction segmentation (CenterNet).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_async_db
from app.models import Diagram, DiagramStatus

router = APIRouter()


@router.post("/{uid}/detect-junctions")
async def start_junction_detection(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Запустить детекцию перекрёстков и мостов (ручной запуск / retry).

    Preconditions:
    - status == skeletonized_final (нормальный путь)
    - status == error + error_stage == 'detecting_junctions' (retry)

    Returns:
        task_id, status
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    allowed = diagram.status == DiagramStatus.SKELETONIZED_FINAL or (
        diagram.status == DiagramStatus.ERROR
        and diagram.error_stage == "detecting_junctions"
    )

    if not allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot start junction detection: status is '{diagram.status.value}'"
                + (f", error_stage='{diagram.error_stage}'" if diagram.error_stage else "")
            ),
        )

    # Обновляем статус
    diagram.status = DiagramStatus.DETECTING_JUNCTIONS
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()

    # Dispatch через send_task
    from worker.celery_app import celery_app

    async_result = celery_app.send_task(
        "worker.tasks.junction.task_detect_junctions",
        args=[str(uid), diagram.project_code],
        queue="gpu",
    )

    return {
        "status": "detecting_junctions",
        "task_id": async_result.id,
        "diagram_uid": str(uid),
    }

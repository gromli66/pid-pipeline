"""
Detection API - YOLO детекция.
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_async_db
from app.models import Diagram, DiagramStatus

router = APIRouter()


@router.post("/{uid}/detect")
async def start_detection(
    uid: UUID,
    model_id: Optional[str] = Query(
        None,
        description="ID модели детекции (None → default из конфига проекта)",
    ),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Запустить YOLO детекцию.
    
    1. Проверяет status == uploaded
    2. Обновляет status = detecting
    3. Запускает Celery task
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")
    
    if diagram.status != DiagramStatus.UPLOADED:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot start detection: status is '{diagram.status.value}', expected 'uploaded'"
        )
    
    # Валидация model_id (если указан) — проверяем что модель существует в конфиге
    if model_id:
        from app.services.project_loader import get_project_loader
        loader = get_project_loader()
        project_config = loader.load(diagram.project_code or "thermohydraulics")
        if project_config and model_id not in project_config.detection.models:
            available = list(project_config.detection.models.keys())
            raise HTTPException(
                status_code=400,
                detail=f"Detection model '{model_id}' not found. Available: {available}"
            )
    
    # Обновляем статус ПЕРЕД запуском task (короткая транзакция)
    # ⚠️ НЕ оборачивать Celery send_task в ту же транзакцию!
    diagram.status = DiagramStatus.DETECTING
    await db.commit()
    
    # Запускаем Celery task ВНЕ транзакции
    from worker.celery_app import celery_app
    task = celery_app.send_task(
        "worker.tasks.detection.task_detect_yolo",
        args=[str(uid)],
        kwargs={
            "project_code": diagram.project_code or "thermohydraulics",
            "model_id": model_id,
        },
    )
    
    return {
        "status": "detecting",
        "task_id": task.id,
        "uid": str(uid),
        "model_id": model_id,
    }


@router.post("/{uid}/retry")
async def retry_detection(
    uid: UUID,
    model_id: Optional[str] = Query(
        None,
        description="ID модели (None → использовать ту же что была, или default)",
    ),
    db: AsyncSession = Depends(get_async_db),
):
    """Повторить детекцию после ошибки."""
    
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")
    
    if diagram.status != DiagramStatus.ERROR:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry: status is '{diagram.status.value}', expected 'error'"
        )
    
    if diagram.error_stage != "detecting":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry detection: error_stage is '{diagram.error_stage}'"
        )
    
    # Использовать предыдущую модель если model_id не указан
    effective_model_id = model_id or diagram.detection_model
    
    # Сбрасываем ошибку
    diagram.status = DiagramStatus.DETECTING
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()
    
    # Запускаем Celery task
    from worker.celery_app import celery_app
    task = celery_app.send_task(
        "worker.tasks.detection.task_detect_yolo",
        args=[str(uid)],
        kwargs={
            "project_code": diagram.project_code or "thermohydraulics",
            "model_id": effective_model_id,
        },
    )
    
    return {
        "status": "detecting",
        "task_id": task.id,
        "uid": str(uid),
        "model_id": effective_model_id,
    }
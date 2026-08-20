"""
Detection API - YOLO детекция.
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import obs
from app.core.logging import get_logger
from app.db import get_async_db
from app.models import Diagram, DiagramStatus

router = APIRouter()

logger = get_logger(__name__)


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
    Запустить YOLO детекцию — первый запуск и повтор после падения.

    Preconditions:
    - status == frame_cleaned (нормальный путь)
    - status == error + error_stage == 'detecting' (повтор: сюда ведёт красная
      кнопка «Поиск элементов» в клиенте — retry-эндпоинт ниже из UI не зовётся)
    
    1. Проверяет гейт статуса
    2. Обновляет status = detecting и снимает ошибку
    3. Запускает Celery task
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")
    
    allowed = diagram.status == DiagramStatus.FRAME_CLEANED or (
        diagram.status == DiagramStatus.ERROR
        and diagram.error_stage == "detecting"
    )

    if not allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot start detection: status is '{diagram.status.value}'"
                + (f", error_stage='{diagram.error_stage}'" if diagram.error_stage else "")
                + ". Expected: frame_cleaned, or error with error_stage='detecting'"
            ),
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
    
    obs.bind(uid=str(uid), phase="detection")
    if diagram.status == DiagramStatus.ERROR:
        logger.info(
            "Повторный запуск детекции после ошибки (error_stage=%s)",
            diagram.error_stage, extra={"event": "retry"},
        )

    # Обновляем статус ПЕРЕД запуском task (короткая транзакция)
    # ⚠️ НЕ оборачивать Celery send_task в ту же транзакцию!
    # Состояние до перехода держим целиком: если отправка упадёт, вернуть надо
    # всё, что переход записал, а не один статус — иначе диаграмма останется
    # в ERROR с пустым error_stage, и клиент погасит ВСЕ кнопки
    # (ui/widgets/diagram_workspace.py: _error_key).
    previous_state = (diagram.status, diagram.error_stage, diagram.error_message)
    diagram.status = DiagramStatus.DETECTING
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()
    
    # Запускаем Celery task ВНЕ транзакции
    from worker.celery_app import celery_app
    try:
        task = celery_app.send_task(
            "worker.tasks.detection.task_detect_yolo",
            args=[str(uid)],
            kwargs={
                "project_code": diagram.project_code or "thermohydraulics",
                "model_id": model_id,
            },
        )
    except Exception as exc:
        # Брокер недоступен — возвращаем состояние, каким оно было до вызова.
        # Без этого диаграмма оставалась в DETECTING навсегда: задачи нет,
        # значит некому ни упасть в error, ни дойти до конца, а кнопка
        # «Поиск элементов» при *ING-статусе даже не нажимается.
        # След — ДО коммита возврата: на бою БД падает вместе с брокером, и тогда
        # исключение коммита унесло бы наружу единственную запись об отказе ОТПРАВКИ.
        logger.exception(
            "Отправка детекции не удалась (%s) — возвращаю состояние в '%s'",
            exc, previous_state[0].value, extra={"event": "dispatch_failed"},
        )
        diagram.status, diagram.error_stage, diagram.error_message = previous_state
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail=f"Worker unavailable: {exc}",
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
    
    # Сбрасываем ошибку. Пред-вызовное состояние — на случай отказа отправки
    # (тот же шов, что в /detect выше).
    obs.bind(uid=str(uid), phase="detection")
    previous_state = (diagram.status, diagram.error_stage, diagram.error_message)
    diagram.status = DiagramStatus.DETECTING
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()
    
    # Запускаем Celery task
    from worker.celery_app import celery_app
    try:
        task = celery_app.send_task(
            "worker.tasks.detection.task_detect_yolo",
            args=[str(uid)],
            kwargs={
                "project_code": diagram.project_code or "thermohydraulics",
                "model_id": effective_model_id,
            },
        )
    except Exception as exc:
        # След — ДО коммита возврата: на бою БД падает вместе с брокером, и тогда
        # исключение коммита унесло бы наружу единственную запись об отказе ОТПРАВКИ.
        logger.exception(
            "Отправка повтора детекции не удалась (%s) — возвращаю состояние в '%s'",
            exc, previous_state[0].value, extra={"event": "dispatch_failed"},
        )
        diagram.status, diagram.error_stage, diagram.error_message = previous_state
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail=f"Worker unavailable: {exc}",
        )
    
    return {
        "status": "detecting",
        "task_id": task.id,
        "uid": str(uid),
        "model_id": effective_model_id,
    }
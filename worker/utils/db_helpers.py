"""
Worker DB Helpers — общие утилиты для всех Celery tasks.

Решает:
- 4.3: _set_error дублирована в 5 tasks → одна реализация
- 1.1: upsert_artifact вместо голого db.add
- 1.5: safe_dispatch с откатом статуса при ошибке
- 3.3: проверка is_deleted перед обработкой
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def set_diagram_error(
    db,
    diagram_uid: str,
    message: str,
    stage: str,
    max_message_len: int = 500,
) -> None:
    """
    Пометить диаграмму как ERROR.
    Единая реализация для всех worker tasks (4.3).
    """
    try:
        from app.models import Diagram, DiagramStatus

        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if diagram:
            diagram.status = DiagramStatus.ERROR
            diagram.error_message = message[:max_message_len]
            diagram.error_stage = stage
            db.commit()
    except Exception as exc:
        logger.error("Failed to set error status for %s: %s", diagram_uid, exc)


def check_deleted(db, diagram_uid: str) -> bool:
    """
    Проверить, помечена ли диаграмма как удалённая (3.3).
    Вызывать в начале каждого task.

    Returns:
        True если диаграмма удалена (task должен прерваться).
    """
    from app.models import Diagram

    diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
    if diagram is None:
        logger.warning("Diagram %s not found (may have been deleted)", diagram_uid)
        return True
    if diagram.is_deleted:
        logger.info("Diagram %s is marked as deleted, aborting task", diagram_uid)
        return True
    return False


def upsert_artifact(
    db,
    diagram_uid: str,
    artifact_type,
    file_path: str,
    storage_base: Path,
    mime_type: str = None,
):
    """
    Создать или обновить артефакт (1.1).
    Безопасен при retry — не создаёт дубли.
    """
    from app.models import Artifact

    existing = (
        db.query(Artifact)
        .filter(
            Artifact.diagram_uid == diagram_uid,
            Artifact.artifact_type == artifact_type,
        )
        .first()
    )

    full_path = Path(file_path) if Path(file_path).is_absolute() else storage_base / file_path
    file_size = full_path.stat().st_size if full_path.exists() else None

    # Относительный путь для хранения в БД
    try:
        rel_path = str(full_path.relative_to(storage_base))
    except ValueError:
        rel_path = file_path

    if existing:
        existing.file_path = rel_path
        existing.file_size = file_size
        existing.mime_type = mime_type
        return existing

    artifact = Artifact(
        diagram_uid=diagram_uid,
        artifact_type=artifact_type,
        file_path=rel_path,
        file_size=file_size,
        mime_type=mime_type,
    )
    db.add(artifact)
    return artifact


def safe_dispatch(
    db,
    diagram,
    task_name: str,
    args: list,
    fallback_status=None,
) -> Optional[str]:
    """
    Безопасная отправка Celery task (1.5).
    Если .send_task() упадёт — откатывает статус и логирует.

    Args:
        db: SQLAlchemy session
        diagram: Diagram instance (уже с новым статусом, commit сделан)
        task_name: полное имя celery task
        args: аргументы task
        fallback_status: статус при ошибке dispatch (None → ERROR)

    Returns:
        task_id или None при ошибке
    """
    from worker.celery_app import celery_app
    from app.models import DiagramStatus

    try:
        result = celery_app.send_task(task_name, args=args)
        logger.info("Dispatched %s → %s", task_name, result.id)
        return result.id
    except Exception as exc:
        logger.error("Failed to dispatch %s: %s", task_name, exc)
        if fallback_status:
            diagram.status = fallback_status
        else:
            diagram.status = DiagramStatus.ERROR
            diagram.error_message = f"Failed to dispatch next task: {exc}"
            diagram.error_stage = task_name.split(".")[-1]
        db.commit()
        return None


def start_stage(db, diagram_uid: str, stage_type, celery_task_id: str = None):
    """
    Create a ProcessingStage record and mark it RUNNING.

    Args:
        db: SQLAlchemy session
        diagram_uid: UUID of the diagram
        stage_type: StageType enum value
        celery_task_id: optional Celery task ID

    Returns:
        ProcessingStage instance (already added to session, not yet committed)
    """
    from app.models.stage import ProcessingStage, StageStatus

    # Count previous attempts for this diagram + stage_type
    attempt = (
        db.query(ProcessingStage)
        .filter(
            ProcessingStage.diagram_uid == diagram_uid,
            ProcessingStage.stage_type == stage_type,
        )
        .count()
        + 1
    )

    stage = ProcessingStage(
        diagram_uid=diagram_uid,
        stage_type=stage_type,
        status=StageStatus.PENDING,
        attempt=attempt,
        celery_task_id=celery_task_id,
    )
    stage.start()
    db.add(stage)
    db.flush()
    return stage


def complete_stage(stage, metrics: dict = None) -> None:
    """Mark a ProcessingStage as COMPLETED with optional metrics."""
    if stage is not None:
        stage.complete(metrics)


def fail_stage(stage, error: str, tb: str = None) -> None:
    """Mark a ProcessingStage as FAILED."""
    if stage is not None:
        stage.fail(error[:2000], tb[:10000] if tb else None)

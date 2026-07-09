"""
Frame API — удаление рамки/штампа с исходного изображения (ручной этап, без worker).

Этап 0 pipeline. Канонический подход к хранению:
  * original/image.png      — АКТИВНОЕ изображение pipeline. До очистки = сырое
    (от загрузки/PDF-рендера), после очистки = очищенное. Его читают ВСЕ этапы
    (сегментация, джанкшены, скелет, OCR, контуры, граф, CVAT) — поэтому очистку
    «видят» все, без правок в воркерах.
  * original/image_raw.png  — сырой бэкап (создаётся при первом сохранении очистки),
    нужен для отката/повторного редактирования. Неразрушающесть сохранена.
  * original/source.pdf     — исходный PDF (если загружали PDF).

Observability (Волна 3 — upload/frame, RUNBOOK §8.5-канон): ручная/await-стадия
(budget=None в progress_model.py, наравне с cvat_validation/mask_validation/
graph_validation) — НЕ worker, async-хелперы `start_frame_stage`/`fail_frame_stage`
(app/api/frame_stage_helpers.py) зеркалят `_start_cvat_stage`/`_fail_cvat_stage`
(app/api/cvat.py); вынесены в отдельный файл, чтобы тесты грузили их без
транзитивного `aiofiles` (StorageService).
RUNNING-строка открывается в /start (на реальном переходе UPLOADED→CLEANING_FRAME)
и закрывается в /complete или /skip; если RUNNING-строки нет (клиент вызвал
/complete или /skip напрямую — _FRAME_EDITABLE это допускает) — заводим и сразу
закрываем новую (самовосстановление).

Endpoints:
- POST /{uid}/start    — UPLOADED → CLEANING_FRAME
- POST /{uid}/save     — загрузить очищенный PNG: бэкап raw → image_raw.png,
                         очищенное → image.png; маркер-артефакт ORIGINAL_CLEANED
- POST /{uid}/complete — → FRAME_CLEANED (требует сохранённую очистку)
- POST /{uid}/skip     — «рамки нет»: восстановить raw в image.png → FRAME_CLEANED
"""

import asyncio
import shutil
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_async_db
from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
from app.services.storage import StorageService
from app.core import obs
from app.core.errors import PipelineError
from app.core.logging import get_logger
from app.api.frame_stage_helpers import (
    start_frame_stage,
    get_running_frame_stage,
    fail_frame_stage,
)

logger = get_logger(__name__)

router = APIRouter()

# Статусы, из которых допустимы операции с рамкой (идемпотентно).
_FRAME_EDITABLE = (
    DiagramStatus.UPLOADED,
    DiagramStatus.CLEANING_FRAME,
    DiagramStatus.FRAME_CLEANED,
)

_STAGE = "original"
_CANON = "image.png"        # активное изображение pipeline
_RAW_BACKUP = "image_raw.png"  # сырой бэкап (создаётся при первой очистке)


async def _get_diagram(uid: UUID, db: AsyncSession) -> Diagram:
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")
    return diagram


def _original_dir(uid: UUID):
    return StorageService().base_path / str(uid) / _STAGE


async def _get_cleaned_artifact(uid: UUID, db: AsyncSession):
    res = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.ORIGINAL_CLEANED,
        )
    )
    return res.scalar_one_or_none()


@router.post("/{uid}/start")
async def start_frame_removal(uid: UUID, db: AsyncSession = Depends(get_async_db)):
    """Начать очистку рамки: UPLOADED → CLEANING_FRAME."""
    diagram = await _get_diagram(uid, db)

    if diagram.status not in _FRAME_EDITABLE:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot start frame removal: status is '{diagram.status.value}', "
                   f"expected 'uploaded'",
        )

    if diagram.status == DiagramStatus.UPLOADED:
        obs.bind(uid=str(uid), phase="frame_removal")
        logger.info("Starting frame removal for %s", uid)
        await start_frame_stage(db, uid)
        diagram.status = DiagramStatus.CLEANING_FRAME
        diagram.error_message = None
        diagram.error_stage = None
        await db.commit()

    return {"status": "cleaning_frame", "message": "Frame removal started", "uid": str(uid)}


@router.post("/{uid}/save")
async def save_cleaned_image(
    uid: UUID,
    file: UploadFile = File(..., description="Очищенный PNG (рендерится в UI)"),
    db: AsyncSession = Depends(get_async_db),
):
    """Сохранить очищенное: бэкап raw → image_raw.png, очищенное → image.png."""
    diagram = await _get_diagram(uid, db)

    if diagram.status not in _FRAME_EDITABLE:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot save cleaned image: status is '{diagram.status.value}'",
        )

    if file.content_type not in {"image/png", "application/octet-stream"}:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type '{file.content_type}'. Expected: image/png",
        )

    obs.bind(uid=str(uid), phase="frame_removal")

    if diagram.status == DiagramStatus.UPLOADED:
        diagram.status = DiagramStatus.CLEANING_FRAME

    orig_dir = _original_dir(uid)
    canon = orig_dir / _CANON
    raw_backup = orig_dir / _RAW_BACKUP

    try:
        with obs.step("persist_artifacts", logger):
            # Бэкап сырого изображения один раз (перед первой перезаписью канона)
            if canon.exists() and not raw_backup.exists():
                await asyncio.to_thread(shutil.copy2, str(canon), str(raw_backup))

            # Очищенное → канонический image.png (его читают все этапы)
            storage = StorageService()
            content = await file.read()
            file_path, file_size = await storage.save_file(uid, _STAGE, _CANON, content)

            # Маркер «очистка сохранена» (указывает на тот же image.png)
            old = await _get_cleaned_artifact(uid, db)
            if old:
                await db.delete(old)
                await db.flush()
            db.add(Artifact(
                diagram_uid=uid,
                artifact_type=ArtifactType.ORIGINAL_CLEANED,
                file_path=file_path,
                file_size=file_size,
                mime_type="image/png",
            ))
            await db.commit()
    except PipelineError as exc:
        # Стадия НЕ падает — /save повторяем, оператор ещё работает во вкладке
        # (в отличие от /skip, это не терминальная операция стадии).
        raise HTTPException(status_code=500, detail=f"Failed to save cleaned image: {exc}") from exc

    return {"status": "saved", "file_size": file_size, "uid": str(uid)}


@router.post("/{uid}/complete")
async def complete_frame_removal(uid: UUID, db: AsyncSession = Depends(get_async_db)):
    """Завершить очистку: → FRAME_CLEANED. Требует сохранённую очистку."""
    diagram = await _get_diagram(uid, db)

    if diagram.status not in _FRAME_EDITABLE:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot complete frame removal: status is '{diagram.status.value}'",
        )

    if diagram.status == DiagramStatus.FRAME_CLEANED:
        return {"status": "frame_cleaned", "message": "Already completed", "uid": str(uid)}

    if not await _get_cleaned_artifact(uid, db):
        raise HTTPException(
            status_code=400,
            detail="No cleaned image saved. Save the cleaned image first, or use /skip.",
        )

    obs.bind(uid=str(uid), phase="frame_removal")
    # Самовосстановление: если /start не звали (_FRAME_EDITABLE это допускает),
    # открытой RUNNING-строки нет — заводим и сразу закрываем.
    stage = await get_running_frame_stage(db, uid) or await start_frame_stage(db, uid)

    diagram.status = DiagramStatus.FRAME_CLEANED
    diagram.error_message = None
    diagram.error_stage = None
    stage.complete()
    await db.commit()

    logger.info("Frame removal completed for %s", uid)
    return {"status": "frame_cleaned", "message": "Frame removal completed", "uid": str(uid)}


@router.post("/{uid}/skip")
async def skip_frame_removal(uid: UUID, db: AsyncSession = Depends(get_async_db)):
    """«Рамки нет»: вернуть сырое изображение в image.png → FRAME_CLEANED."""
    diagram = await _get_diagram(uid, db)

    if diagram.status not in _FRAME_EDITABLE:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot skip frame removal: status is '{diagram.status.value}'",
        )

    obs.bind(uid=str(uid), phase="frame_removal")
    stage = await get_running_frame_stage(db, uid) or await start_frame_stage(db, uid)

    # Если очистка ранее перезаписала канон — восстановить сырое из бэкапа.
    orig_dir = _original_dir(uid)
    canon = orig_dir / _CANON
    raw_backup = orig_dir / _RAW_BACKUP

    try:
        with obs.step("persist_artifacts", logger):
            if raw_backup.exists():
                await asyncio.to_thread(shutil.copy2, str(raw_backup), str(canon))
                raw_backup.unlink()

            old = await _get_cleaned_artifact(uid, db)
            if old:
                await db.delete(old)
                await db.flush()

            diagram.status = DiagramStatus.FRAME_CLEANED
            diagram.error_message = None
            diagram.error_stage = None
            stage.complete()
            await db.commit()
    except PipelineError as exc:
        fail_frame_stage(stage, exc, default_step="skip")
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to skip frame removal: {exc}") from exc

    logger.info("Frame removal skipped for %s", uid)
    return {"status": "frame_cleaned", "message": "Frame removal skipped", "uid": str(uid)}

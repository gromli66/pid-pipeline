"""
Diagrams API - CRUD операции с диаграммами.
"""

from uuid import UUID
from pathlib import Path
from typing import Optional
import re

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Form
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_async_db
from app.models import Diagram, DiagramStatus, Artifact, ArtifactType, Project
from app.models.stage import ProcessingStage
from app.schemas.diagram import (
    DiagramResponse,
    DiagramListResponse,
    DiagramStatusResponse,
    DiagramUploadResponse,
    ProcessingStageResponse,
    StagesResponse,
)
from app.services.storage import StorageService
from app.services.project_loader import get_project_loader, ProjectLoader
from app.config import settings
from app.core import obs
from app.core.errors import InvalidUploadError, PipelineError
from app.core.logging import get_logger

router = APIRouter()

logger = get_logger(__name__)

MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB


def sanitize_filename(filename: str) -> str:
    """Очистить имя файла от опасных символов (path traversal, спецсимволы)."""
    # Убрать path traversal
    filename = filename.replace("\\", "/").split("/")[-1]
    # Убрать спецсимволы, оставить буквы, цифры, пробелы, -, _, .
    filename = re.sub(r'[^\w\s\-\.]', '', filename)
    # Убрать множественные точки (защита от ..)
    filename = re.sub(r'\.{2,}', '.', filename)
    return filename.strip() or "unnamed"


@router.post("/upload", response_model=DiagramUploadResponse)
async def upload_diagram(
    file: UploadFile = File(...),
    project_code: str = Form(..., description="Код проекта"),
    page: int = Form(1, description="Страница PDF (1-based); игнорируется для изображений"),
    db: AsyncSession = Depends(get_async_db),
    loader: ProjectLoader = Depends(get_project_loader),
):
    """
    Загрузить новую диаграмму.

    Требует указания project_code.
    """
    # Проверка проекта
    config = loader.load(project_code)
    if not config:
        raise HTTPException(status_code=400, detail=f"Unknown project: {project_code}")

    # Проверяем/создаём проект в БД
    result = await db.execute(select(Project).where(Project.code == project_code))
    project = result.scalar_one_or_none()

    if not project:
        project = Project(
            code=config.code,
            name=config.name,
            cvat_project_name=config.cvat_project_name,
            config_path=config.config_path,
        )
        db.add(project)
        await db.flush()

    # Проверка типа файла по РАСШИРЕНИЮ (UI-клиент шлёт content_type=image/png
    # для всех файлов, поэтому ориентируемся на имя файла).
    ext = (Path(file.filename).suffix.lower() if file.filename else "")
    ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif"}
    is_pdf = ext == ".pdf"
    if not is_pdf and ext not in ALLOWED_IMAGE_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type '{ext}'. Allowed: {sorted(ALLOWED_IMAGE_EXTS)} or .pdf",
        )

    # Проверка размера файла (чанками, без загрузки всего в RAM)
    size = 0
    while chunk := await file.read(1024 * 1024):  # 1MB чанки
        size += len(chunk)
        if size > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum: {MAX_FILE_SIZE // (1024 * 1024)}MB"
            )
    await file.seek(0)  # Сбрасываем позицию для дальнейшего чтения

    # Следующий номер (advisory lock per-project предотвращает race condition)
    lock_key = hash(project_code) % (2**31)
    await db.execute(text(f"SELECT pg_advisory_xact_lock({lock_key})"))

    # Проверка дубликата по имени файла в проекте
    sanitized_name = sanitize_filename(file.filename or "unnamed")
    dup_result = await db.execute(
        select(Diagram).where(
            Diagram.project_code == project_code,
            Diagram.original_filename == sanitized_name,
            Diagram.is_deleted == False,  # noqa: E712
        )
    )
    existing = dup_result.scalars().first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Диаграмма с именем '{sanitized_name}' уже существует в проекте (#{existing.number})",
        )

    result = await db.execute(
        select(func.max(Diagram.number)).where(Diagram.project_code == project_code)
    )
    max_number = result.scalar() or 0

    # Создаём диаграмму
    diagram = Diagram(
        number=max_number + 1,
        project_code=project_code,
        original_filename=sanitized_name,
        status=DiagramStatus.UPLOADED,
    )
    db.add(diagram)
    await db.flush()

    obs.bind(uid=str(diagram.uid), phase="upload")
    logger.info(
        "Starting upload for %s (project=%s, pdf=%s, filename=%s)",
        diagram.uid, project_code, is_pdf, sanitized_name,
    )

    # Сохраняем файл. PDF → рендерим выбранную страницу в PNG @300 DPI
    # (модели обучались на 300 DPI сканах); изображение сохраняем как есть.
    storage = StorageService()
    if is_pdf:
        # Ленивый импорт: отсутствие PyMuPDF не должно ронять весь API на старте —
        # ошибка проявится только при загрузке PDF.
        try:
            from app.services.pdf_render import render_pdf_page_to_png
        except ImportError:
            raise HTTPException(
                status_code=500,
                detail="PDF upload requires PyMuPDF on the server (pip install PyMuPDF).",
            )

    try:
        with obs.step("compute", logger):
            if is_pdf:
                pdf_bytes = await file.read()
                try:
                    png_bytes, dimensions, _n_pages = render_pdf_page_to_png(
                        pdf_bytes,
                        page=page,
                        dpi=settings.PDF_RENDER_DPI,
                        max_side=settings.PDF_MAX_SIDE,
                    )
                except ValueError as exc:
                    raise InvalidUploadError(
                        f"PDF render failed: {exc}", stage="upload", diagram_uid=str(diagram.uid),
                    ) from exc
            else:
                # Нормализуем любое изображение в канонический original/image.png,
                # чтобы этап рамки и все downstream-этапы работали с одним именем файла
                # (раньше JPG/TIFF сохранялись как image.jpg и часть читателей их не находила).
                import io as _io
                from PIL import Image as _Image
                img_bytes = await file.read()
                try:
                    im = _Image.open(_io.BytesIO(img_bytes))
                    dimensions = im.size
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")
                    buf = _io.BytesIO()
                    im.save(buf, format="PNG")
                    png_bytes = buf.getvalue()
                except Exception as exc:
                    raise InvalidUploadError(
                        f"Invalid image: {exc}", stage="upload", diagram_uid=str(diagram.uid),
                    ) from exc

        with obs.step("persist_artifacts", logger):
            if is_pdf:
                # PNG — основное изображение pipeline (downstream читает original/image.png)
                file_path, file_size = await storage.save_file(
                    diagram.uid, "original", "image.png", png_bytes
                )
                # Исходный PDF сохраняем рядом (на случай перерендера в другом DPI)
                await storage.save_file(diagram.uid, "original", "source.pdf", pdf_bytes)
                mime_type = "image/png"
            else:
                file_path, file_size = await storage.save_file(
                    diagram.uid, "original", "image.png", png_bytes
                )
                mime_type = "image/png"

            diagram.image_width = dimensions[0] if dimensions else None
            diagram.image_height = dimensions[1] if dimensions else None

            # Артефакт (для PDF указывает на отрендеренный PNG)
            artifact = Artifact(
                diagram_uid=diagram.uid,
                artifact_type=ArtifactType.ORIGINAL_IMAGE,
                file_path=file_path,
                file_size=file_size,
                mime_type=mime_type,
            )
            db.add(artifact)

            await db.commit()
            await db.refresh(diagram)

    except InvalidUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PipelineError as exc:
        raise HTTPException(status_code=500, detail=f"Upload processing failed: {exc}") from exc

    return DiagramUploadResponse(
        uid=diagram.uid,
        number=diagram.number,
        project_code=diagram.project_code,
        status=diagram.status,
        filename=diagram.original_filename,
    )


@router.get("/", response_model=DiagramListResponse)
async def list_diagrams(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    project_code: Optional[str] = Query(None, description="Фильтр по проекту"),
    status: Optional[DiagramStatus] = None,
    db: AsyncSession = Depends(get_async_db),
):
    """Получить список диаграмм."""
    query = select(Diagram).where(
        Diagram.is_deleted == False  # noqa: E712
    ).order_by(Diagram.number.desc())

    if project_code:
        query = query.where(Diagram.project_code == project_code)
    if status:
        query = query.where(Diagram.status == status)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar()

    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    diagrams = result.scalars().all()

    return DiagramListResponse(
        items=[DiagramResponse.model_validate(d) for d in diagrams],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/{uid}", response_model=DiagramResponse)
async def get_diagram(uid: UUID, db: AsyncSession = Depends(get_async_db)):
    """Получить диаграмму по UID."""
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    return DiagramResponse.model_validate(diagram)


@router.get("/{uid}/status", response_model=DiagramStatusResponse)
async def get_diagram_status(uid: UUID, db: AsyncSession = Depends(get_async_db)):
    """Статус диаграммы для polling."""
    result = await db.execute(
        select(
            Diagram.status,
            Diagram.error_message,
            Diagram.error_stage,
            Diagram.cvat_task_id,
            Diagram.cvat_job_id,
            Diagram.detection_count,
            Diagram.updated_at,
        ).where(Diagram.uid == uid)
    )
    row = result.one_or_none()

    if not row:
        raise HTTPException(status_code=404, detail="Diagram not found")

    return DiagramStatusResponse(
        status=row.status,
        error_message=row.error_message,
        error_stage=row.error_stage,
        cvat_task_id=row.cvat_task_id,
        cvat_job_id=row.cvat_job_id,
        detection_count=row.detection_count,
        updated_at=row.updated_at,
    )


@router.get("/{uid}/stages", response_model=StagesResponse)
async def get_diagram_stages(uid: UUID, db: AsyncSession = Depends(get_async_db)):
    """Пер-этапные данные обработки диаграммы (для UI)."""
    result = await db.execute(
        select(ProcessingStage)
        .where(ProcessingStage.diagram_uid == uid)
        .order_by(ProcessingStage.id)
    )
    stages = result.scalars().all()

    return StagesResponse(
        stages=[
            ProcessingStageResponse(
                id=stage.id,
                stage_type=stage.stage_type.value,
                status=stage.status.value,
                attempt=stage.attempt,
                celery_task_id=stage.celery_task_id,
                error_message=stage.error_message,
                error_traceback=stage.error_traceback,
                error_code=stage.error_code,
                failed_step=stage.failed_step,
                current_step=stage.current_step,
                started_at=stage.started_at,
                completed_at=stage.completed_at,
                duration_seconds=stage.duration_seconds,
            )
            for stage in stages
        ]
    )


@router.get("/{uid}/download/{artifact_type}")
async def download_artifact(
    uid: UUID,
    artifact_type: str,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Скачать артефакт диаграммы.

    artifact_type: original_image, yolo_predicted, yolo_validated, coco_validated,
                   node_mask, pipe_mask, skeleton, skeleton_mask,
                   junction_mask, bridge_mask
    """
    from fastapi.responses import FileResponse
    from app.config import settings

    # Проверяем диаграмму
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    # Валидация типа артефакта
    try:
        art_type = ArtifactType(artifact_type)
    except ValueError:
        valid_types = [t.value for t in ArtifactType]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact type. Valid: {valid_types}"
        )

    # Ищем артефакт
    result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == art_type,
        )
    )
    artifact = result.scalar_one_or_none()
    if not artifact:
        raise HTTPException(
            status_code=404,
            detail=f"Artifact '{artifact_type}' not found for diagram {uid}"
        )

    # Собираем полный путь
    storage_path = Path(settings.STORAGE_PATH)
    file_path = storage_path / artifact.file_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    # Имя файла для скачивания
    suffix = file_path.suffix
    download_name = f"{diagram.original_filename}_{artifact_type}{suffix}"

    return FileResponse(
        path=file_path,
        filename=download_name,
        media_type="application/octet-stream",
    )


@router.delete("/{uid}")
async def delete_diagram(uid: UUID, db: AsyncSession = Depends(get_async_db)):
    """Удалить диаграмму."""
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    storage = StorageService()
    await storage.delete_diagram_folder(uid)

    diagram.is_deleted = True
    await db.commit()

    return {"status": "deleted", "uid": str(uid)}


@router.post("/{uid}/retry")
async def retry_operation(
        uid: UUID,
        db: AsyncSession = Depends(get_async_db),
):
    """
    Повторить операцию после ошибки.
    Сбрасывает статус на предыдущий шаг в зависимости от error_stage.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if diagram.status != DiagramStatus.ERROR:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry: status is '{diagram.status.value}', expected 'error'"
        )

    # Маппинг error_stage → предыдущий статус для retry
    stage_to_status = {
        # Phase 1: Detection
        "detecting": DiagramStatus.FRAME_CLEANED,
        "creating_cvat_task": DiagramStatus.DETECTED,
        "fetching_annotations": DiagramStatus.VALIDATING_BBOX,
        # Phase 2: Segmentation + skeleton #1
        "segmenting": DiagramStatus.VALIDATED_BBOX,
        "skeletonizing": DiagramStatus.SEGMENTING,
        # Phase 3: Mask Validation
        "validating_masks": DiagramStatus.SKELETONIZED,
        # Phase 4: Final skeleton + junction detection
        "skeletonizing_simple": DiagramStatus.VALIDATED_MASKS,
        "skeletonizing_final": DiagramStatus.VALIDATED_MASKS,
        "detecting_junctions": DiagramStatus.SKELETONIZED_FINAL,
        # Phase 7: Graph
        "building_graph": DiagramStatus.VALIDATED_JUNCTIONS,
    }

    new_status = stage_to_status.get(diagram.error_stage, DiagramStatus.UPLOADED)

    diagram.status = new_status
    diagram.error_message = None
    diagram.error_stage = None

    await db.commit()

    return {
        "status": new_status.value,
        "message": f"Status reset to {new_status.value}",
    }


@router.post("/{uid}/reupload-original")
async def reupload_original(
        uid: UUID,
        file: UploadFile = File(...),
        db: AsyncSession = Depends(get_async_db),
):
    """Перезагрузить оригинальное изображение."""
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    # Проверка типа файла по РАСШИРЕНИЮ (UI-клиент шлёт content_type=image/png
    # для всех файлов, поэтому ориентируемся на имя файла).
    ext = (Path(file.filename).suffix.lower() if file.filename else "")
    ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif"}
    is_pdf = ext == ".pdf"
    if not is_pdf and ext not in ALLOWED_IMAGE_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type '{ext}'. Allowed: {sorted(ALLOWED_IMAGE_EXTS)} or .pdf",
        )

    # Проверка размера файла (чанками, без загрузки всего в RAM)
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum: {MAX_FILE_SIZE // (1024 * 1024)}MB"
            )
    await file.seek(0)

    # Сохраняем файл
    storage = StorageService()
    file_path, file_size, dimensions = await storage.save_upload(diagram.uid, file, "original")

    diagram.image_width = dimensions[0] if dimensions else None
    diagram.image_height = dimensions[1] if dimensions else None

    # Сбрасываем статус на UPLOADED
    diagram.status = DiagramStatus.UPLOADED
    diagram.error_message = None
    diagram.error_stage = None

    await db.commit()

    return {
        "status": "uploaded",
        "message": "Original image reuploaded successfully",
    }

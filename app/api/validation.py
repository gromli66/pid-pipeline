"""
Validation API — валидация масок и графа.

Mask validation endpoints:
- POST /{uid}/masks/start — начать валидацию масок (SKELETONIZED → VALIDATING_MASKS)
- POST /{uid}/masks/upload — загрузить валидированную маску
- POST /{uid}/masks/complete — завершить валидацию → VALIDATED_MASKS → auto-dispatch skeletonize_simple
"""

import asyncio
import json
import shutil
from uuid import UUID

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_async_db
from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
from app.services.dispatch import async_safe_dispatch
from app.services.storage import StorageService

router = APIRouter()

# Допустимые типы масок для загрузки
VALID_MASK_TYPES = {
    "junction_mask_validated": ArtifactType.JUNCTION_MASK_VALIDATED,
    "bridge_mask_validated": ArtifactType.BRIDGE_MASK_VALIDATED,
    "pipe_mask_validated": ArtifactType.PIPE_MASK_VALIDATED,
}

# Маппинг mask_type → (stage_folder, filename)
MASK_STORAGE_MAP = {
    "junction_mask_validated": ("junction", "junction_mask_validated.png"),
    "bridge_mask_validated": ("junction", "bridge_mask_validated.png"),
    "pipe_mask_validated": ("segmentation", "pipe_mask_validated.png"),
}


@router.post("/{uid}/masks/start")
async def start_mask_validation(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Начать валидацию масок.

    Переводит диаграмму из SKELETONIZED → VALIDATING_MASKS.
    После этого UI может загружать артефакты и редактировать маски.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if diagram.status != DiagramStatus.SKELETONIZED:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot start mask validation: status is '{diagram.status.value}', expected 'skeletonized'",
        )

    diagram.status = DiagramStatus.VALIDATING_MASKS
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()

    return {
        "status": "validating_masks",
        "message": "Mask validation started",
        "uid": str(uid),
    }


@router.post("/{uid}/masks/upload")
async def upload_validated_mask(
    uid: UUID,
    mask_type: str = Form(
        ...,
        description="Тип маски: junction_mask_validated, bridge_mask_validated, pipe_mask_validated",
    ),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Загрузить валидированную маску.

    Можно вызывать несколько раз для разных типов масок.
    Перезаписывает предыдущую версию.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if diagram.status not in (
        DiagramStatus.SKELETONIZED,
        DiagramStatus.VALIDATING_MASKS,
        DiagramStatus.DETECTED_JUNCTIONS,
        DiagramStatus.VALIDATING_JUNCTIONS,
        DiagramStatus.BUILT,
        DiagramStatus.VALIDATED_GRAPH,
        DiagramStatus.OCR_COMPLETED,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot upload mask: status is '{diagram.status.value}'",
        )

    # Валидация mask_type
    if mask_type not in VALID_MASK_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mask_type '{mask_type}'. Valid: {list(VALID_MASK_TYPES.keys())}",
        )

    # Проверка типа файла
    if file.content_type not in {"image/png", "application/octet-stream"}:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type '{file.content_type}'. Expected: image/png",
        )

    # Автоматически переводим в VALIDATING_MASKS при первой загрузке
    if diagram.status == DiagramStatus.SKELETONIZED:
        diagram.status = DiagramStatus.VALIDATING_MASKS

    # Сохраняем файл
    art_type = VALID_MASK_TYPES[mask_type]
    stage, filename = MASK_STORAGE_MAP[mask_type]

    storage = StorageService()
    content = await file.read()
    file_path, file_size = await storage.save_file(
        uid, stage, filename, content
    )

    # Удаляем старый артефакт если есть
    old_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == art_type,
        )
    )
    old_artifact = old_result.scalar_one_or_none()
    if old_artifact:
        await db.delete(old_artifact)
        await db.flush()

    # Создаём новый артефакт
    artifact = Artifact(
        diagram_uid=uid,
        artifact_type=art_type,
        file_path=file_path,
        file_size=file_size,
        mime_type="image/png",
    )
    db.add(artifact)

    await db.commit()

    return {
        "status": "uploaded",
        "mask_type": mask_type,
        "file_size": file_size,
        "uid": str(uid),
    }


# ---------------------------------------------------------------------------
# Node mask regeneration helper
# ---------------------------------------------------------------------------

def _generate_node_mask_from_coco(coco_data: dict, height: int, width: int) -> np.ndarray:
    """Generate node_mask from COCO dict (same logic as segmentation.generate_node_mask)."""
    from PIL import Image, ImageDraw

    categories = {cat["id"]: cat["name"] for cat in coco_data.get("categories", [])}

    # Исключаем из node_mask: truba, annotation, napravlenie.
    # napravlenie — стрелка направления НА трубе: труба должна проходить сквозь
    # её bbox, поэтому bbox не вырезаем из pipe_mask (направление обрабатывается
    # как атрибут спец-узла в графе). ВАЖНО держать список синхронным с
    # worker/tasks/segmentation.py::generate_node_mask, иначе правка теряется
    # при ревалидации детекции. strelka НЕ исключаем.
    excluded_names = {"truba", "annotation", "napravlenie"}
    excluded_ids = {
        cat_id for cat_id, cat_name in categories.items()
        if cat_name.lower() in excluded_names
    }

    mask = np.zeros((height, width), dtype=np.uint8)

    for ann in coco_data.get("annotations", []):
        cat_id = ann.get("category_id")
        if cat_id in excluded_ids:
            continue

        ann_mask = np.zeros((height, width), dtype=np.uint8)

        seg = ann.get("segmentation")
        if seg and isinstance(seg, list):
            pil_mask = Image.new("L", (width, height), 0)
            draw = ImageDraw.Draw(pil_mask)
            for polygon in seg:
                if len(polygon) >= 6:
                    coords = [(polygon[i], polygon[i + 1]) for i in range(0, len(polygon), 2)]
                    draw.polygon(coords, outline=1, fill=1)
            ann_mask = np.array(pil_mask, dtype=np.uint8)

        if ann_mask.max() == 0 and "bbox" in ann and ann["bbox"]:
            x, y, w, h = [int(v) for v in ann["bbox"]]
            x2 = min(x + w, width)
            y2 = min(y + h, height)
            x = max(0, x)
            y = max(0, y)
            ann_mask[y:y2, x:x2] = 1

        mask = np.maximum(mask, ann_mask)

    return mask * 255


@router.post("/{uid}/nodes/update")
async def update_nodes(
    uid: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Update coco_validated.json and regenerate node_mask.png.

    Used by pipe_tab when user adds new equipment nodes during mask validation.
    Accepts updated COCO JSON, saves as coco_validated artifact,
    regenerates node_mask from it.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if diagram.status not in (
        DiagramStatus.SKELETONIZED,
        DiagramStatus.VALIDATING_MASKS,
        DiagramStatus.VALIDATED_MASKS,
        DiagramStatus.SKELETONIZED_FINAL,
        DiagramStatus.DETECTED_JUNCTIONS,
        DiagramStatus.VALIDATING_JUNCTIONS,
        DiagramStatus.BUILT,
        DiagramStatus.VALIDATED_GRAPH,
        DiagramStatus.OCR_COMPLETED,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot update nodes: status is '{diagram.status.value}'",
        )

    # Read and validate JSON
    content = await file.read()
    try:
        coco_data = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if "annotations" not in coco_data or "categories" not in coco_data:
        raise HTTPException(status_code=400, detail="Invalid COCO format: missing annotations or categories")

    storage = StorageService()

    # 1. Save updated coco_validated.json
    coco_path, coco_size = await storage.save_file(
        uid, "detection", "coco_validated.json", content
    )

    # Update or create COCO_VALIDATED artifact
    old = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.COCO_VALIDATED,
        )
    )
    old_art = old.scalar_one_or_none()
    if old_art:
        old_art.file_path = coco_path
        old_art.file_size = coco_size
    else:
        db.add(Artifact(
            diagram_uid=uid,
            artifact_type=ArtifactType.COCO_VALIDATED,
            file_path=coco_path,
            file_size=coco_size,
            mime_type="application/json",
        ))

    # 2. Regenerate node_mask from updated COCO
    # Get image dimensions from coco_data
    images = coco_data.get("images", [])
    if images:
        img_w = images[0].get("width", 0)
        img_h = images[0].get("height", 0)
    else:
        img_w, img_h = 0, 0

    # Fallback: read from original image
    if img_w == 0 or img_h == 0:
        orig_result = await db.execute(
            select(Artifact).where(
                Artifact.diagram_uid == uid,
                Artifact.artifact_type == ArtifactType.ORIGINAL_IMAGE,
            )
        )
        orig_art = orig_result.scalar_one_or_none()
        if orig_art:
            orig_full = storage.base_path / orig_art.file_path
            img = await asyncio.to_thread(cv2.imread, str(orig_full))
            if img is not None:
                img_h, img_w = img.shape[:2]

    if img_w == 0 or img_h == 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot determine image dimensions for node_mask generation",
        )

    node_mask = _generate_node_mask_from_coco(coco_data, img_h, img_w)

    # Encode and save
    _, mask_buf = await asyncio.to_thread(cv2.imencode, ".png", node_mask)
    mask_bytes = mask_buf.tobytes()
    mask_path, mask_size = await storage.save_file(
        uid, "segmentation", "node_mask.png", mask_bytes
    )

    # Update or create NODE_MASK artifact
    old_mask = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.NODE_MASK,
        )
    )
    old_mask_art = old_mask.scalar_one_or_none()
    if old_mask_art:
        old_mask_art.file_path = mask_path
        old_mask_art.file_size = mask_size
    else:
        db.add(Artifact(
            diagram_uid=uid,
            artifact_type=ArtifactType.NODE_MASK,
            file_path=mask_path,
            file_size=mask_size,
            mime_type="image/png",
        ))

    await db.commit()

    return {
        "status": "updated",
        "uid": str(uid),
        "annotations_count": len(coco_data.get("annotations", [])),
        "node_mask_size": mask_size,
    }


@router.post("/{uid}/masks/complete")
async def complete_mask_validation(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Завершить валидацию масок.

    Проверяет наличие всех трёх валидированных масок.
    Переводит VALIDATING_MASKS → VALIDATED_MASKS.
    Автоматически запускает task_skeletonize_simple для перестройки скелета.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if diagram.status not in (
        DiagramStatus.VALIDATING_MASKS,
        DiagramStatus.SKELETONIZED,
        DiagramStatus.VALIDATED_MASKS,
        # Idempotent: chain already moved past mask validation
        DiagramStatus.SKELETONIZING_FINAL,
        DiagramStatus.SKELETONIZED_FINAL,
        DiagramStatus.DETECTING_JUNCTIONS,
        DiagramStatus.DETECTED_JUNCTIONS,
        DiagramStatus.BUILT,
        DiagramStatus.VALIDATED_GRAPH,
        DiagramStatus.OCR_COMPLETED,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot complete validation: status is '{diagram.status.value}'",
        )

    # Проверяем наличие валидированной pipe mask.
    # Если маска не загружена — копируем оригинальную как validated.
    # Junction/bridge маски теперь на ОТДЕЛЬНОМ шаге (после junction detection).
    ORIGINAL_TO_VALIDATED = {
        ArtifactType.PIPE_MASK_VALIDATED: (ArtifactType.PIPE_MASK, "pipe_mask_validated.png"),
    }

    storage = StorageService()

    for validated_type, (original_type, validated_filename) in ORIGINAL_TO_VALIDATED.items():
        result = await db.execute(
            select(Artifact).where(
                Artifact.diagram_uid == uid,
                Artifact.artifact_type == validated_type,
            )
        )
        if result.scalar_one_or_none():
            continue  # уже загружена

        # Найти оригинальную маску в DB
        result = await db.execute(
            select(Artifact).where(
                Artifact.diagram_uid == uid,
                Artifact.artifact_type == original_type,
            )
        )
        original_artifact = result.scalar_one_or_none()
        if not original_artifact:
            raise HTTPException(
                status_code=400,
                detail=f"Original mask '{original_type.value}' not found in DB. "
                       f"Cannot auto-approve.",
            )

        # Копируем файл
        original_path = storage.base_path / original_artifact.file_path
        validated_path = original_path.parent / validated_filename

        if not original_path.exists():
            raise HTTPException(
                status_code=400,
                detail=f"Original mask file not found: {original_artifact.file_path}",
            )

        await asyncio.to_thread(shutil.copy2, str(original_path), str(validated_path))

        # Создаём артефакт
        artifact = Artifact(
            diagram_uid=uid,
            artifact_type=validated_type,
            file_path=str(validated_path.relative_to(storage.base_path)),
            file_size=validated_path.stat().st_size,
            mime_type="image/png",
        )
        db.add(artifact)
        await db.flush()

    # Обновляем статус — только если ещё не ушли вперёд
    already_past = diagram.status in (
        DiagramStatus.SKELETONIZING_FINAL,
        DiagramStatus.SKELETONIZED_FINAL,
        DiagramStatus.DETECTING_JUNCTIONS,
        DiagramStatus.DETECTED_JUNCTIONS,
        DiagramStatus.VALIDATED_JUNCTIONS,
        DiagramStatus.BUILDING_GRAPH,
        DiagramStatus.BUILT,
        DiagramStatus.VALIDATED_GRAPH,
        DiagramStatus.OCR_COMPLETED,
    )

    task_id = None
    if not already_past:
        diagram.status = DiagramStatus.VALIDATED_MASKS
        diagram.error_message = None
        diagram.error_stage = None
        await db.commit()

        task_id = await async_safe_dispatch(
            "worker.tasks.skeleton.task_skeletonize_simple",
            args=[str(uid)],
        )

    return {
        "status": "validated_masks",
        "message": "Mask validation completed",
        "task_id": task_id,
        "uid": str(uid),
    }


# ==================== Phase 6 (Junction Validation) ====================


@router.post("/{uid}/junctions/start")
async def start_junction_validation(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Начать валидацию перекрёстков/мостов.

    Переводит DETECTED_JUNCTIONS → VALIDATING_JUNCTIONS.
    После этого UI может загружать junction_mask/bridge_mask и редактировать.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if diagram.status not in (
        DiagramStatus.DETECTED_JUNCTIONS,
        DiagramStatus.VALIDATING_JUNCTIONS,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot start junction validation: status is '{diagram.status.value}', "
                   f"expected 'detected_junctions'",
        )

    if diagram.status == DiagramStatus.DETECTED_JUNCTIONS:
        diagram.status = DiagramStatus.VALIDATING_JUNCTIONS
        diagram.error_message = None
        diagram.error_stage = None
        await db.commit()

    return {
        "status": "validating_junctions",
        "message": "Junction validation started",
        "uid": str(uid),
    }


@router.post("/{uid}/junctions/complete")
async def complete_junction_validation(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Завершить валидацию перекрёстков/мостов.

    Если validated маски не загружены — копирует оригинальные как validated.
    Переводит → VALIDATED_JUNCTIONS.
    Автоматически запускает task_build_graph.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if diagram.status not in (
        DiagramStatus.DETECTED_JUNCTIONS,
        DiagramStatus.VALIDATING_JUNCTIONS,
        # Idempotent: chain already moved past junction validation
        DiagramStatus.VALIDATED_JUNCTIONS,
        DiagramStatus.BUILDING_GRAPH,
        DiagramStatus.BUILT,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot complete junction validation: status is '{diagram.status.value}'",
        )

    # Если validated маски не загружены — копируем оригинальные
    ORIGINAL_TO_VALIDATED = {
        ArtifactType.JUNCTION_MASK_VALIDATED: (ArtifactType.JUNCTION_MASK, "junction_mask_validated.png"),
        ArtifactType.BRIDGE_MASK_VALIDATED: (ArtifactType.BRIDGE_MASK, "bridge_mask_validated.png"),
    }

    storage = StorageService()

    for validated_type, (original_type, validated_filename) in ORIGINAL_TO_VALIDATED.items():
        result = await db.execute(
            select(Artifact).where(
                Artifact.diagram_uid == uid,
                Artifact.artifact_type == validated_type,
            )
        )
        if result.scalar_one_or_none():
            continue  # уже загружена

        # Найти оригинальную маску
        result = await db.execute(
            select(Artifact).where(
                Artifact.diagram_uid == uid,
                Artifact.artifact_type == original_type,
            )
        )
        original_artifact = result.scalar_one_or_none()
        if not original_artifact:
            raise HTTPException(
                status_code=400,
                detail=f"Original mask '{original_type.value}' not found. "
                       f"Junction detection may not have completed.",
            )

        # Копируем файл
        original_path = storage.base_path / original_artifact.file_path
        validated_path = original_path.parent / validated_filename

        if not original_path.exists():
            raise HTTPException(
                status_code=400,
                detail=f"Original mask file not found: {original_artifact.file_path}",
            )

        await asyncio.to_thread(shutil.copy2, str(original_path), str(validated_path))

        artifact = Artifact(
            diagram_uid=uid,
            artifact_type=validated_type,
            file_path=str(validated_path.relative_to(storage.base_path)),
            file_size=validated_path.stat().st_size,
            mime_type="image/png",
        )
        db.add(artifact)
        await db.flush()

    # Обновляем статус — только если ещё не ушли вперёд
    already_past = diagram.status in (
        DiagramStatus.VALIDATED_JUNCTIONS,
        DiagramStatus.BUILDING_GRAPH,
        DiagramStatus.BUILT,
    )

    task_id = None
    contour_task_id = None
    ocr_task_id = None
    if not already_past:
        diagram.status = DiagramStatus.VALIDATED_JUNCTIONS
        diagram.error_message = None
        diagram.error_stage = None
        await db.commit()

        # Auto-dispatch graph build + SAM2 contours + OCR (all parallel)
        task_id = await async_safe_dispatch(
            "worker.tasks.graph.task_build_graph",
            args=[str(uid)],
            queue="gpu",
        )

        # SAM2 contour extraction (parallel, independent)
        contour_task_id = await async_safe_dispatch(
            "worker.tasks.contours.task_extract_contours",
            args=[str(uid)],
            queue="sam2",
        )

        # OCR -- parallel with graph and SAM2 (separate worker, queue "ocr")
        ocr_task_id = await async_safe_dispatch(
            "worker.tasks.ocr.task_run_ocr",
            args=[str(uid)],
            queue="ocr",
        )

    return {
        "status": "validated_junctions",
        "message": "Junction validation completed, graph build + contours + OCR started",
        "task_id": task_id,
        "contour_task_id": contour_task_id,
        "ocr_task_id": ocr_task_id,
        "uid": str(uid),
    }


@router.post("/{uid}/graph/complete-simple")
async def complete_simple_graph_validation(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Завершить Simple-валидацию графа → запустить OCR.

    Отличие от complete: НЕ запускает FXML, а запускает task_run_ocr.
    Переводит VALIDATING_GRAPH → VALIDATED_GRAPH → auto-start OCR.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if diagram.status not in (
        DiagramStatus.VALIDATING_GRAPH,
        DiagramStatus.BUILT,
        DiagramStatus.VALIDATED_GRAPH,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot complete simple validation: status is '{diagram.status.value}'",
        )

    # Проверить/скопировать GRAPH_VALIDATED
    validated_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.GRAPH_VALIDATED,
        )
    )
    if not validated_result.scalar_one_or_none():
        original_result = await db.execute(
            select(Artifact).where(
                Artifact.diagram_uid == uid,
                Artifact.artifact_type == ArtifactType.GRAPH_JSON,
            )
        )
        original_artifact = original_result.scalar_one_or_none()
        if not original_artifact:
            raise HTTPException(
                status_code=400,
                detail="No graph found. Save the graph before completing.",
            )

        storage = StorageService()
        original_path = storage.base_path / original_artifact.file_path
        if not original_path.exists():
            raise HTTPException(
                status_code=400,
                detail=f"Original graph file not found: {original_artifact.file_path}",
            )

        validated_path = original_path.parent / "graph_validated.json"
        await asyncio.to_thread(shutil.copy2, str(original_path), str(validated_path))

        artifact = Artifact(
            diagram_uid=uid,
            artifact_type=ArtifactType.GRAPH_VALIDATED,
            file_path=str(validated_path.relative_to(storage.base_path)),
            file_size=validated_path.stat().st_size,
            mime_type="application/json",
        )
        db.add(artifact)
        await db.flush()

    # Статус → VALIDATED_GRAPH
    diagram.status = DiagramStatus.VALIDATED_GRAPH
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()

    # Auto-dispatch OCR (NOT FXML!)
    task_id = await async_safe_dispatch(
        "worker.tasks.ocr.task_run_ocr",
        args=[str(uid)],
        queue="ocr",
    )

    return {
        "status": "validated_graph",
        "message": "Simple validation completed, OCR started",
        "task_id": task_id,
        "uid": str(uid),
    }


# ==================== Phase 5 (Graph) ====================


@router.post("/{uid}/graph/start")
async def start_graph_validation(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Начать валидацию графа.

    Переводит диаграмму из BUILT → VALIDATING_GRAPH.
    После этого UI может загружать graph_json и редактировать граф.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if diagram.status not in (DiagramStatus.BUILT, DiagramStatus.VALIDATING_GRAPH):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot start graph validation: status is '{diagram.status.value}', expected 'built'",
        )

    if diagram.status == DiagramStatus.BUILT:
        diagram.status = DiagramStatus.VALIDATING_GRAPH
        diagram.error_message = None
        diagram.error_stage = None
        await db.commit()

    return {
        "status": "validating_graph",
        "message": "Graph validation started",
        "uid": str(uid),
    }


@router.post("/{uid}/graph/save")
async def save_validated_graph(
    uid: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Сохранить валидированный граф.

    Принимает JSON файл графа, сохраняет как GRAPH_VALIDATED артефакт.
    Автоматически переводит в VALIDATING_GRAPH если ещё BUILT.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if diagram.status not in (
        DiagramStatus.BUILT,
        DiagramStatus.VALIDATING_GRAPH,
        DiagramStatus.VALIDATED_GRAPH,
        DiagramStatus.CONTOURS_VALIDATED,
        DiagramStatus.OCR_COMPLETED,
        DiagramStatus.OCR_BOUND,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot save graph: status is '{diagram.status.value}', "
            f"expected 'built' or 'validating_graph'",
        )

    # Автоматически переводим в VALIDATING_GRAPH
    if diagram.status == DiagramStatus.BUILT:
        diagram.status = DiagramStatus.VALIDATING_GRAPH

    # Сохраняем файл
    storage = StorageService()
    content = await file.read()
    file_path, file_size = await storage.save_file(
        uid, "graph", "graph_validated.json", content
    )

    # Удаляем старый артефакт если есть
    old_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.GRAPH_VALIDATED,
        )
    )
    old_artifact = old_result.scalar_one_or_none()
    if old_artifact:
        await db.delete(old_artifact)
        await db.flush()

    # Создаём новый артефакт
    artifact = Artifact(
        diagram_uid=uid,
        artifact_type=ArtifactType.GRAPH_VALIDATED,
        file_path=file_path,
        file_size=file_size,
        mime_type="application/json",
    )
    db.add(artifact)

    await db.commit()

    return {
        "status": "saved",
        "file_size": file_size,
        "uid": str(uid),
    }


@router.post("/{uid}/graph/complete")
async def complete_graph_validation(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Завершить валидацию графа.

    Проверяет наличие GRAPH_VALIDATED артефакта.
    Переводит VALIDATING_GRAPH → VALIDATED_GRAPH.
    Автоматически запускает генерацию FXML.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if diagram.status not in (
        DiagramStatus.VALIDATING_GRAPH,
        DiagramStatus.BUILT,
        DiagramStatus.VALIDATED_GRAPH,
        DiagramStatus.OCR_COMPLETED,
        DiagramStatus.OCR_BOUND,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot complete graph validation: status is '{diagram.status.value}'",
        )

    # Проверяем наличие GRAPH_VALIDATED
    # Если нет — копируем GRAPH_JSON как validated
    validated_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.GRAPH_VALIDATED,
        )
    )
    if not validated_result.scalar_one_or_none():
        # Ищем оригинальный GRAPH_JSON
        original_result = await db.execute(
            select(Artifact).where(
                Artifact.diagram_uid == uid,
                Artifact.artifact_type == ArtifactType.GRAPH_JSON,
            )
        )
        original_artifact = original_result.scalar_one_or_none()
        if not original_artifact:
            raise HTTPException(
                status_code=400,
                detail="No graph_json or graph_validated artifact found. "
                       "Save the graph before completing validation.",
            )

        # Копируем как validated
        storage = StorageService()
        original_path = storage.base_path / original_artifact.file_path
        if not original_path.exists():
            raise HTTPException(
                status_code=400,
                detail=f"Original graph file not found: {original_artifact.file_path}",
            )

        validated_filename = "graph_validated.json"
        validated_path = original_path.parent / validated_filename

        await asyncio.to_thread(shutil.copy2, str(original_path), str(validated_path))

        artifact = Artifact(
            diagram_uid=uid,
            artifact_type=ArtifactType.GRAPH_VALIDATED,
            file_path=str(validated_path.relative_to(storage.base_path)),
            file_size=validated_path.stat().st_size,
            mime_type="application/json",
        )
        db.add(artifact)
        await db.flush()

    # Обновляем статус
    # Если пришли из OCR_BOUND (после привязки + редактор) → сразу GENERATING_FXML
    # Если из более ранних статусов → VALIDATED_GRAPH (Simple flow → OCR)
    if diagram.status in (DiagramStatus.OCR_BOUND, DiagramStatus.OCR_COMPLETED):
        diagram.status = DiagramStatus.GENERATING_FXML
    else:
        diagram.status = DiagramStatus.VALIDATED_GRAPH
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()

    # Auto-dispatch FXML generation
    task_id = await async_safe_dispatch(
        "worker.tasks.graph.task_generate_fxml",
        args=[str(uid)],
    )

    return {
        "status": "validated_graph",
        "message": "Graph validation completed",
        "task_id": task_id,
        "uid": str(uid),
    }

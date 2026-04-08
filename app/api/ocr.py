"""
OCR API — распознавание текста и привязка к графу.

Endpoints:
  POST /api/ocr/{uid}/start          — ручной запуск/retry OCR
  GET  /api/ocr/{uid}/status         — статус OCR
  GET  /api/ocr/{uid}/result         — скачать ocr_final.json
  POST /api/ocr/{uid}/binding/save   — сохранить привязки
  GET  /api/ocr/{uid}/binding        — скачать привязки
  POST /api/ocr/{uid}/binding/apply  — применить привязки к graph_validated
"""

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_async_db
from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
from app.services.storage import StorageService

router = APIRouter()


@router.post("/{uid}/start")
async def start_ocr(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Запустить OCR pipeline (ручной запуск или retry).

    Допустимые статусы: VALIDATED_GRAPH, OCR_COMPLETED, ERROR.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if diagram.status not in (
        DiagramStatus.VALIDATED_JUNCTIONS,
        DiagramStatus.BUILDING_GRAPH,
        DiagramStatus.BUILT,
        DiagramStatus.VALIDATING_GRAPH,
        DiagramStatus.VALIDATED_GRAPH,
        DiagramStatus.EXTRACTING_CONTOURS,
        DiagramStatus.CONTOURS_EXTRACTED,
        DiagramStatus.CONTOURS_VALIDATED,
        DiagramStatus.OCR_COMPLETED,
        DiagramStatus.OCR_BOUND,
        DiagramStatus.ERROR,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot start OCR: status is '{diagram.status.value}', "
            f"expected 'validated_junctions' or later",
        )

    # Удалить существующий OCR результат (retry case),
    # чтобы worker не пропустил из-за idempotency check
    old_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.OCR_RESULT,
        )
    )
    old_artifact = old_result.scalar_one_or_none()
    if old_artifact:
        await db.delete(old_artifact)
        await db.commit()

    # Dispatch task
    task_id = None
    try:
        from worker.celery_app import celery_app

        result = celery_app.send_task(
            "worker.tasks.ocr.task_run_ocr",
            args=[str(uid)],
            queue="ocr",
        )
        task_id = result.id
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to dispatch OCR task: {e}",
        )

    return {
        "status": "dispatched",
        "task_id": task_id,
        "uid": str(uid),
    }


@router.get("/{uid}/status")
async def get_ocr_status(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """Получить статус OCR для диаграммы."""
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    # Проверить наличие OCR артефактов
    ocr_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.OCR_RESULT,
        )
    )
    has_result = ocr_result.scalar_one_or_none() is not None

    binding_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.OCR_BINDING,
        )
    )
    has_binding = binding_result.scalar_one_or_none() is not None

    return {
        "uid": str(uid),
        "status": diagram.status.value,
        "has_ocr_result": has_result,
        "has_binding": has_binding,
        "error_message": diagram.error_message,
        "error_stage": diagram.error_stage,
    }


@router.get("/{uid}/result")
async def get_ocr_result(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """Скачать OCR результат (ocr_final.json)."""
    result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.OCR_RESULT,
        )
    )
    artifact = result.scalar_one_or_none()

    if not artifact:
        raise HTTPException(status_code=404, detail="OCR result not found")

    storage = StorageService()
    file_path = storage.base_path / artifact.file_path

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="OCR result file not found on disk")

    return FileResponse(
        path=str(file_path),
        filename="ocr_result.json",
        media_type="application/json",
    )


@router.put("/{uid}/result")
async def update_ocr_result(
    uid: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Обновить OCR результат (ocr_final.json).

    Используется после слияния блоков в UI.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    storage = StorageService()
    content = await file.read()

    # Validate JSON before saving
    try:
        json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    file_path, file_size = await storage.save_file(
        uid, "ocr", "ocr_result.json", content
    )

    # Upsert artifact
    old_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.OCR_RESULT,
        )
    )
    old = old_result.scalar_one_or_none()
    if old:
        old.file_path = file_path
        old.file_size = file_size
    else:
        artifact = Artifact(
            diagram_uid=uid,
            artifact_type=ArtifactType.OCR_RESULT,
            file_path=file_path,
            file_size=file_size,
            mime_type="application/json",
        )
        db.add(artifact)

    await db.commit()

    return {"status": "updated", "file_size": file_size, "uid": str(uid)}


@router.post("/{uid}/binding/save")
async def save_ocr_binding(
    uid: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Сохранить привязки OCR → граф.

    Принимает JSON файл с привязками.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    # Binding требует: 1) граф провалидирован, 2) OCR результат есть
    if diagram.status not in (
        DiagramStatus.VALIDATED_GRAPH,
        DiagramStatus.CONTOURS_VALIDATED,
        DiagramStatus.OCR_COMPLETED,
        DiagramStatus.OCR_BOUND,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot save binding: status is '{diagram.status.value}', "
            f"expected 'validated_graph' or later",
        )

    # Проверить что OCR результат существует (OCR мог ещё не закончиться)
    ocr_check = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.OCR_RESULT,
        )
    )
    if not ocr_check.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail="OCR result not available yet, cannot save binding",
        )

    storage = StorageService()
    content = await file.read()
    file_path, file_size = await storage.save_file(
        uid, "ocr_binding", "ocr_binding.json", content
    )

    # Upsert artifact
    old_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.OCR_BINDING,
        )
    )
    old = old_result.scalar_one_or_none()
    if old:
        old.file_path = file_path
        old.file_size = file_size
    else:
        artifact = Artifact(
            diagram_uid=uid,
            artifact_type=ArtifactType.OCR_BINDING,
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


@router.get("/{uid}/binding")
async def get_ocr_binding(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """Скачать привязки OCR."""
    result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.OCR_BINDING,
        )
    )
    artifact = result.scalar_one_or_none()

    if not artifact:
        raise HTTPException(status_code=404, detail="OCR binding not found")

    storage = StorageService()
    file_path = storage.base_path / artifact.file_path

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="OCR binding file not found on disk")

    return FileResponse(
        path=str(file_path),
        filename="ocr_binding.json",
        media_type="application/json",
    )


@router.post("/{uid}/validation/save")
async def save_ocr_validation(
    uid: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_async_db),
):
    """Сохранить результаты валидации OCR-блоков."""
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    storage = StorageService()
    content = await file.read()
    file_path, file_size = await storage.save_file(
        uid, "ocr_validation", "ocr_validation.json", content
    )

    # Upsert artifact
    old_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.OCR_VALIDATION,
        )
    )
    old = old_result.scalar_one_or_none()
    if old:
        old.file_path = file_path
        old.file_size = file_size
    else:
        artifact = Artifact(
            diagram_uid=uid,
            artifact_type=ArtifactType.OCR_VALIDATION,
            file_path=file_path,
            file_size=file_size,
            mime_type="application/json",
        )
        db.add(artifact)

    await db.commit()
    return {"status": "saved", "file_size": file_size, "uid": str(uid)}


@router.get("/{uid}/validation")
async def get_ocr_validation(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """Скачать результаты валидации OCR-блоков."""
    result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.OCR_VALIDATION,
        )
    )
    artifact = result.scalar_one_or_none()
    if not artifact:
        raise HTTPException(status_code=404, detail="OCR validation not found")

    storage = StorageService()
    file_path = storage.base_path / artifact.file_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="OCR validation file not found on disk")

    return FileResponse(
        path=str(file_path),
        filename="ocr_validation.json",
        media_type="application/json",
    )


@router.post("/{uid}/binding/apply")
async def apply_ocr_binding(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Применить привязки к graph_validated.json.

    Читает ocr_binding.json, обновляет node labels в graph_validated.json.
    """
    storage = StorageService()

    # Загрузить binding
    binding_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.OCR_BINDING,
        )
    )
    binding_art = binding_result.scalar_one_or_none()
    if not binding_art:
        raise HTTPException(status_code=404, detail="OCR binding not found")

    # Загрузить graph_validated
    graph_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.GRAPH_VALIDATED,
        )
    )
    graph_art = graph_result.scalar_one_or_none()
    if not graph_art:
        raise HTTPException(status_code=404, detail="Graph validated not found")

    binding_path = storage.base_path / binding_art.file_path
    graph_path = storage.base_path / graph_art.file_path

    if not binding_path.exists() or not graph_path.exists():
        raise HTTPException(status_code=404, detail="Files not found on disk")

    # Применить привязки
    binding_text = await asyncio.to_thread(binding_path.read_text, "utf-8")
    binding_raw = json.loads(binding_text)
    # Поддержка формата v2 {bindings, edited_blocks} и legacy (list)
    if isinstance(binding_raw, dict) and "bindings" in binding_raw:
        bindings = binding_raw["bindings"]
    elif isinstance(binding_raw, list):
        bindings = binding_raw
    else:
        bindings = []
    graph_text = await asyncio.to_thread(graph_path.read_text, "utf-8")
    graph = json.loads(graph_text)

    # Обновить node labels
    binding_map = {}
    for b in bindings:
        node_id = b.get("node_id")
        text = b.get("text", "")
        if node_id and text:
            binding_map[str(node_id)] = text

    updated = 0
    for node in graph.get("nodes", []):
        nid = str(node.get("id", ""))
        if nid in binding_map:
            node["kks_full"] = binding_map[nid]
            updated += 1

    # Сохранить обновлённый граф (atomic write)
    tmp_path = graph_path.with_suffix(".tmp")
    graph_json = json.dumps(graph, ensure_ascii=False, indent=2)
    await asyncio.to_thread(tmp_path.write_text, graph_json, "utf-8")
    await asyncio.to_thread(tmp_path.rename, graph_path)

    # Перевести статус → OCR_BOUND
    diagram_result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = diagram_result.scalar_one_or_none()
    if diagram and diagram.status in (
        DiagramStatus.OCR_COMPLETED,
        DiagramStatus.VALIDATED_GRAPH,
        DiagramStatus.CONTOURS_VALIDATED,
    ):
        diagram.status = DiagramStatus.OCR_BOUND
        await db.commit()

    return {
        "status": "applied",
        "updated_nodes": updated,
        "total_bindings": len(binding_map),
        "uid": str(uid),
    }

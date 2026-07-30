"""
Contours API -- SAM2 contour management.

Endpoints:
  GET  /api/contours/{uid}/status       -- contour artifact status + stats
  GET  /api/contours/{uid}/auto         -- download contours_auto.json
  GET  /api/contours/{uid}/validated    -- download contours_validated.json
  PUT  /api/contours/{uid}/validated    -- upload contours_validated.json
  PUT  /api/contours/{uid}/training     -- upload contours_training.json (SAM2 fine-tuning)
  POST /api/contours/{uid}/auto-accept  -- copy polygon_auto -> polygon_validated for all
  POST /api/contours/{uid}/complete     -- complete contour validation -> CONTOURS_VALIDATED
"""

import json
import shutil
from copy import deepcopy
from pathlib import Path
from uuid import UUID

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Body
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_async_db
from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
from app.services.layout_dispatch import dispatch_layout
from app.services.storage import StorageService

router = APIRouter()


def _ocr_enabled(project_code: str) -> bool:
    """True if OCR is enabled for the project (defaults to True / fail-open)."""
    try:
        from app.services.project_loader import get_project_loader
        pc = get_project_loader().load(project_code)
        return bool(pc and getattr(pc.ocr, "enabled", True))
    except Exception:
        return True


@router.post("/{uid}/extract")
async def extract_contours(
    uid: UUID,
    ann_ids: Optional[List[int]] = Body(default=None, embed=True),
    db: AsyncSession = Depends(get_async_db),
):
    """Trigger SAM2 contour extraction on demand.

    If ann_ids is given, only those annotation ids are processed (selective
    recognition); otherwise all eligible nodes are processed. Does NOT change
    diagram.status -- readiness is polled via GET /{uid}/status (CONTOURS_AUTO).
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    # Invalidate previous auto result so GET /status reflects re-processing.
    old = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.CONTOURS_AUTO,
        )
    )
    old_art = old.scalar_one_or_none()
    if old_art:
        await db.delete(old_art)
        await db.commit()

    from worker.celery_app import celery_app
    async_result = celery_app.send_task(
        "worker.tasks.contours.task_extract_contours",
        args=[str(uid)],
        kwargs={"ann_ids": ann_ids},
        queue="sam2",
    )
    return {
        "status": "started",
        "task_id": async_result.id,
        "selected": len(ann_ids) if ann_ids else None,
    }


@router.get("/{uid}/status")
async def get_contours_status(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """Check contour artifact availability and stats."""
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    # Check CONTOURS_AUTO
    auto_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.CONTOURS_AUTO,
        )
    )
    auto_artifact = auto_result.scalar_one_or_none()

    # Check CONTOURS_VALIDATED
    val_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.CONTOURS_VALIDATED,
        )
    )
    val_artifact = val_result.scalar_one_or_none()

    stats = None
    if auto_artifact:
        storage = StorageService()
        auto_path = storage.base_path / auto_artifact.file_path
        if auto_path.exists():
            try:
                with open(auto_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                stats = data.get("stats")
            except Exception:
                pass

    return {
        "has_auto": auto_artifact is not None,
        "has_validated": val_artifact is not None,
        "stats": stats,
    }


@router.get("/{uid}/auto")
async def download_contours_auto(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """Download contours_auto.json."""
    result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.CONTOURS_AUTO,
        )
    )
    artifact = result.scalar_one_or_none()
    if not artifact:
        raise HTTPException(status_code=404, detail="Contours auto not found")

    storage = StorageService()
    file_path = storage.base_path / artifact.file_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Contours auto file not found on disk")

    return FileResponse(
        path=str(file_path),
        media_type="application/json",
        filename="contours_auto.json",
    )


@router.get("/{uid}/validated")
async def download_contours_validated(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """Download contours_validated.json."""
    result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.CONTOURS_VALIDATED,
        )
    )
    artifact = result.scalar_one_or_none()
    if not artifact:
        raise HTTPException(status_code=404, detail="Contours validated not found")

    storage = StorageService()
    file_path = storage.base_path / artifact.file_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Contours validated file not found on disk")

    return FileResponse(
        path=str(file_path),
        media_type="application/json",
        filename="contours_validated.json",
    )


@router.put("/{uid}/validated")
async def upload_contours_validated(
    uid: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_async_db),
):
    """Upload contours_validated.json (after operator edits)."""
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    storage = StorageService()
    diagram_dir = storage.get_diagram_path(uid)
    contours_dir = diagram_dir / "contours"
    contours_dir.mkdir(parents=True, exist_ok=True)

    # Save file
    output_path = contours_dir / "contours_validated.json"
    content = await file.read()

    # Basic JSON validation
    try:
        data = json.loads(content)
        if "nodes" not in data:
            raise ValueError("Missing 'nodes' key")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    with open(output_path, "wb") as f:
        f.write(content)

    # Upsert artifact
    rel_path = str(output_path.relative_to(storage.base_path))
    existing = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.CONTOURS_VALIDATED,
        )
    )
    artifact = existing.scalar_one_or_none()
    if artifact:
        artifact.file_path = rel_path
        artifact.file_size = output_path.stat().st_size
    else:
        artifact = Artifact(
            diagram_uid=uid,
            artifact_type=ArtifactType.CONTOURS_VALIDATED,
            file_path=rel_path,
            file_size=output_path.stat().st_size,
            mime_type="application/json",
        )
        db.add(artifact)

    await db.commit()

    return {"status": "ok", "file_size": output_path.stat().st_size}


@router.post("/{uid}/auto-accept")
async def auto_accept_contours(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """Auto-accept: copy polygon_auto -> polygon_validated for all nodes.

    Creates contours_validated.json and sets status to CONTOURS_VALIDATED.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    # Load contours_auto
    auto_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.CONTOURS_AUTO,
        )
    )
    auto_artifact = auto_result.scalar_one_or_none()

    storage = StorageService()
    diagram_dir = storage.get_diagram_path(uid)
    contours_dir = diagram_dir / "contours"
    contours_dir.mkdir(parents=True, exist_ok=True)

    if auto_artifact:
        auto_path = storage.base_path / auto_artifact.file_path
        if not auto_path.exists():
            raise HTTPException(status_code=404, detail="contours_auto.json not found on disk")

        with open(auto_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Copy polygon_auto -> polygon_validated
        accepted = 0
        for node in data.get("nodes", []):
            poly = node.get("polygon_auto")
            if poly:
                node["polygon_validated"] = poly
                node["status"] = "approved"
                accepted += 1
            else:
                node["polygon_validated"] = None
                node["status"] = "skipped"
    else:
        # No contours_auto — create empty validated
        data = {"version": "1.0", "diagram_uid": str(uid), "nodes": [], "stats": {}}
        accepted = 0

    # Save contours_validated.json
    output_path = contours_dir / "contours_validated.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # Upsert artifact
    rel_path = str(output_path.relative_to(storage.base_path))
    existing = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.CONTOURS_VALIDATED,
        )
    )
    artifact = existing.scalar_one_or_none()
    if artifact:
        artifact.file_path = rel_path
        artifact.file_size = output_path.stat().st_size
    else:
        artifact = Artifact(
            diagram_uid=uid,
            artifact_type=ArtifactType.CONTOURS_VALIDATED,
            file_path=rel_path,
            file_size=output_path.stat().st_size,
            mime_type="application/json",
        )
        db.add(artifact)

    # Status -> CONTOURS_VALIDATED (auto-skip OCR if disabled)
    diagram.status = DiagramStatus.CONTOURS_VALIDATED
    if not _ocr_enabled(diagram.project_code):
        # OCR off: jump to OCR_BOUND so OCR + binding beads show completed
        # and edit_graph / export unlock without running OCR.
        diagram.status = DiagramStatus.OCR_BOUND
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()

    # Контуры закрыты — геометрия финальная, можно раскладывать. Диспетчер
    # идемпотентен: повторное подтверждение на ту же истину задачу не плодит.
    layout = await dispatch_layout(uid, db)

    return {"status": "ok", "nodes_accepted": accepted, "layout": layout}


@router.post("/{uid}/complete")
async def complete_contour_validation(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """Complete contour validation.

    If contours_validated.json does not exist, auto-accept all.
    Sets status to CONTOURS_VALIDATED.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    # Check if validated already exists
    val_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.CONTOURS_VALIDATED,
        )
    )
    if not val_result.scalar_one_or_none():
        # Auto-accept if no validated contours
        return await auto_accept_contours(uid, db)

    # Status -> CONTOURS_VALIDATED (auto-skip OCR if disabled)
    diagram.status = DiagramStatus.CONTOURS_VALIDATED
    if not _ocr_enabled(diagram.project_code):
        # OCR off: jump to OCR_BOUND so OCR + binding beads show completed
        # and edit_graph / export unlock without running OCR.
        diagram.status = DiagramStatus.OCR_BOUND
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()

    layout = await dispatch_layout(uid, db)

    return {"status": "ok", "message": "Contour validation completed",
            "layout": layout}


@router.put("/{uid}/training")
async def upload_contours_training(
    uid: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_async_db),
):
    """Upload contours_training.json (approved polygons for SAM2 fine-tuning).

    Saved alongside contours_validated.json in the contours directory.
    No separate DB artifact — training scripts find it by path convention.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    storage = StorageService()
    diagram_dir = storage.get_diagram_path(uid)
    contours_dir = diagram_dir / "contours"
    contours_dir.mkdir(parents=True, exist_ok=True)

    output_path = contours_dir / "contours_training.json"
    content = await file.read()

    try:
        data = json.loads(content)
        if "samples" not in data:
            raise ValueError("Missing 'samples' key")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    with open(output_path, "wb") as f:
        f.write(content)

    return {
        "status": "ok",
        "file_size": output_path.stat().st_size,
        "samples": len(data.get("samples", [])),
    }

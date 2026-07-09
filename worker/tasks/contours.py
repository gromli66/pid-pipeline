"""
Contour Tasks -- SAM2 contour extraction for P&ID nodes.

task_extract_contours:
    SAM2 batch inference for eligible nodes (those without a skin/SVG).
    Inputs: original_image + coco_validated + pipe_mask_refined
    Outputs: contours/contours_auto.json

    PARALLEL: runs alongside task_build_graph and task_run_ocr.
    DOES NOT change DiagramStatus -- readiness is determined by the
    CONTOURS_AUTO artifact presence.
"""

import gc
import json
import logging
import os
import traceback
from pathlib import Path

import numpy as np
from celery.exceptions import SoftTimeLimitExceeded

from worker.celery_app import celery_app
from worker.utils.db_helpers import (
    set_diagram_error,
    check_deleted,
    start_stage,
    complete_stage,
    fail_stage,
)
from app.core import obs
from app.core.errors import ArtifactMissingError, ConfigError, PipelineError
from app.core.logging import get_logger

logger = get_logger(__name__)


class _NumpyEncoder(json.JSONEncoder):
    """JSON encoder that handles numpy types."""

    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


@celery_app.task(
    bind=True,
    name="worker.tasks.contours.task_extract_contours",
    max_retries=1,
    default_retry_delay=60,
    time_limit=600,        # 10 min max
    soft_time_limit=540,   # 9 min warning
    acks_late=True,
)
def task_extract_contours(self, diagram_uid: str, ann_ids=None):
    """
    SAM2 batch inference for eligible P&ID nodes.

    Runs in parallel with task_build_graph and task_run_ocr after
    complete_junction_validation. Does NOT change diagram.status --
    the main pipeline flow (graph) controls the status.

    Readiness is determined by artifact CONTOURS_AUTO.
    """
    from app.db.session import SessionLocal
    from app.models import Diagram, Artifact, ArtifactType
    from app.models.stage import StageType
    from app.services.project_loader import get_project_loader

    storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
    diagram_dir = storage_path / str(diagram_uid)
    contours_dir = diagram_dir / "contours"

    db = SessionLocal()
    stage = None

    # Корреляционный контекст фазы (Волна 3): uid/phase/task_id/attempt → в каждую
    # строку лога через ContextFilter (Волна 0).
    obs.bind(
        uid=str(diagram_uid),
        phase="contour_extraction",
        task_id=self.request.id,
        attempt=self.request.retries,
    )

    try:
        # ===== 1. Load diagram, basic checks =====
        diagram = db.query(Diagram).filter(
            Diagram.uid == diagram_uid
        ).first()

        if not diagram:
            logger.error("Diagram %s not found", diagram_uid)
            return {"status": "not_found", "diagram_uid": diagram_uid}

        if check_deleted(db, diagram_uid):
            logger.info("Diagram %s is deleted, aborting contour extraction", diagram_uid)
            return {"status": "deleted", "diagram_uid": diagram_uid}

        # ===== 2. Idempotency: check if result already exists =====
        existing = db.query(Artifact).filter(
            Artifact.diagram_uid == diagram_uid,
            Artifact.artifact_type == ArtifactType.CONTOURS_AUTO,
        ).first()
        if existing and not ann_ids:
            logger.info(
                "Contours already extracted for %s, skipping", diagram_uid
            )
            return {"status": "already_completed", "diagram_uid": diagram_uid}

        # ===== 3. Load project config, check enabled =====
        loader = get_project_loader()
        project_config = loader.load(diagram.project_code)
        if not project_config:
            raise ConfigError(
                f"Project config not found for '{diagram.project_code}'",
                stage="contour_extraction",
            )

        ce_cfg = project_config.contour_extraction
        if not ce_cfg.enabled:
            logger.info(
                "Contour extraction disabled for project '%s', skipping",
                diagram.project_code,
            )
            return {"status": "disabled", "diagram_uid": diagram_uid}

        # ===== 4. Start processing stage =====
        stage = start_stage(
            db, diagram_uid, StageType.CONTOUR_EXTRACTION,
            celery_task_id=self.request.id,
        )

        logger.info("Contour extraction started for %s", diagram_uid)
        contours_dir.mkdir(parents=True, exist_ok=True)

        # ===== 5. Load input files =====
        # Original image
        with obs.step("load_inputs", logger):
            original_image_path = diagram_dir / "original" / "image.png"
            if not original_image_path.exists():
                for ext in (".jpg", ".jpeg", ".tiff", ".tif"):
                    alt = original_image_path.with_suffix(ext)
                    if alt.exists():
                        original_image_path = alt
                        break
            if not original_image_path.exists():
                raise ArtifactMissingError(
                    f"Original image not found: {diagram_dir / 'original'}",
                    stage="contour_extraction",
                )

            # COCO validated annotations
            coco_path = diagram_dir / "detection" / "coco_validated.json"
            if not coco_path.exists():
                raise ArtifactMissingError(
                    f"COCO validated not found: {coco_path}",
                    stage="contour_extraction",
                )

            # Pipe mask (refined > validated > raw)
            pipe_mask_path = diagram_dir / "segmentation" / "pipe_mask_refined.png"
            if not pipe_mask_path.exists():
                pipe_mask_path = diagram_dir / "segmentation" / "pipe_mask_validated.png"
            if not pipe_mask_path.exists():
                pipe_mask_path = diagram_dir / "segmentation" / "pipe_mask.png"
            if not pipe_mask_path.exists():
                raise ArtifactMissingError(
                    f"No pipe mask found in {diagram_dir / 'segmentation'}",
                    stage="contour_extraction",
                )

            logger.info(
                "[%s] Inputs: image=%s, coco=%s, pipe_mask=%s",
                diagram_uid,
                original_image_path.name,
                coco_path.name,
                pipe_mask_path.name,
            )

            # ===== 6. Load data =====
            import cv2

            image = cv2.imread(str(original_image_path))
            if image is None:
                raise ArtifactMissingError(f"Failed to read image: {original_image_path}", stage="contour_extraction")

            pipe_mask = cv2.imread(str(pipe_mask_path), cv2.IMREAD_GRAYSCALE)
            if pipe_mask is None:
                raise ArtifactMissingError(f"Failed to read pipe mask: {pipe_mask_path}", stage="contour_extraction")

            with open(coco_path, "r", encoding="utf-8") as f:
                coco_data = json.load(f)

        # ===== 7. Filter eligible annotations =====
        categories = {
            cat["id"]: cat["name"]
            for cat in coco_data.get("categories", [])
        }
        skip_classes = set(ce_cfg.skip_classes)

        eligible_anns = []
        skipped_count = 0
        for ann in coco_data.get("annotations", []):
            cat_name = categories.get(ann.get("category_id"), "")
            if cat_name in skip_classes:
                skipped_count += 1
                continue
            eligible_anns.append(ann)

        if ann_ids:
            _sel = set(ann_ids)
            eligible_anns = [a for a in eligible_anns if a.get("id") in _sel]
            logger.info("[%s] Selective extraction: %d of selected requested",
                        diagram_uid, len(eligible_anns))

        logger.info(
            "[%s] Annotations: %d total, %d eligible, %d skipped",
            diagram_uid,
            len(coco_data.get("annotations", [])),
            len(eligible_anns),
            skipped_count,
        )

        if not eligible_anns:
            logger.warning(
                "[%s] No eligible annotations for contour extraction",
                diagram_uid,
            )
            # Save empty result
            result_data = _build_result(diagram_uid, [], categories, [])
            _save_and_register(
                db, diagram_uid, contours_dir, storage_path,
                result_data, stage,
            )
            return {"status": "empty", "diagram_uid": diagram_uid}

        # ===== 8. Run SAM2 inference (model is process-cached) =====
        from modules.sam2_contour import get_contour_extractor

        from worker.utils.device import resolve_device
        with obs.step("load_model", logger):
            extractor = get_contour_extractor(
                checkpoint=ce_cfg.checkpoint,
                checkpoint_v8=None,
                device=resolve_device(),
                target_size=1024,
                snap_dp_eps=ce_cfg.snap_dp_eps,
                snap_threshold=ce_cfg.snap_threshold,
                snap_min_edge=ce_cfg.snap_min_edge,
                confidence_threshold=ce_cfg.confidence_threshold,
            )

        with obs.step("compute", logger):
            raw_results = extractor.predict_batch(
                image=image,
                detections=eligible_anns,
                pipe_mask=pipe_mask,
            )

        logger.info(
            "[%s] SAM2 inference done: %d results", diagram_uid, len(raw_results)
        )

        # ===== 9. Release transient memory (keep cached model alive) =====
        # NOTE: do NOT delete the extractor -- it is process-cached and reused
        # across task runs. empty_cache() only frees unused allocator blocks.
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()

        # ===== 10. Build and save result =====
        result_data = _build_result(
            diagram_uid, raw_results, categories, eligible_anns,
        )

        with obs.step("persist_artifacts", logger):
            _save_and_register(
                db, diagram_uid, contours_dir, storage_path,
                result_data, stage,
            )

        logger.info(
            "[%s] Contour extraction completed: %d auto, %d review, %d skipped",
            diagram_uid,
            result_data["stats"]["auto"],
            result_data["stats"]["manual_review"],
            result_data["stats"]["skipped"],
        )

        return {
            "status": "success",
            "diagram_uid": diagram_uid,
            "stats": result_data["stats"],
        }

    except SoftTimeLimitExceeded:
        logger.error("[%s] Contour extraction timed out", diagram_uid, exc_info=True)
        fail_stage(stage, "Contour extraction timed out (9 min limit)", traceback.format_exc())
        set_diagram_error(
            db, diagram_uid,
            "Contour extraction timed out", "contour_extraction",
        )
        db.rollback()
        raise

    except Exception as exc:
        # exc_info=True + exc= в fail_stage → error_code/failed_step/traceback
        # доезжают до /stages (DoD §4).
        logger.error("[%s] Contour extraction failed: %s", diagram_uid, exc, exc_info=True)

        if self.request.retries < self.max_retries:
            fail_stage(stage, str(exc)[:500], traceback.format_exc(), exc=exc)
            db.rollback()
            raise self.retry(exc=exc)

        fail_stage(stage, str(exc)[:500], traceback.format_exc(), exc=exc)
        set_diagram_error(
            db, diagram_uid, str(exc)[:500], "contour_extraction",
        )
        db.rollback()
        raise

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_result(
    diagram_uid: str,
    results: list,
    categories: dict,
    annotations: list,
) -> dict:
    """Build contours_auto.json from sam2_contour.ContourExtractor results.

    Each result from predict_batch has:
        polygon_flat, confidence, status, n_points, K, ann_id, category_id, ...
    """
    # Build ann_id -> bbox lookup from original annotations
    bbox_by_ann = {ann["id"]: ann["bbox"] for ann in annotations if "id" in ann}

    nodes = []
    auto_count = 0
    review_count = 0

    for r in results:
        status = r.get("status", "manual_review")
        if status == "auto":
            auto_count += 1
        else:
            review_count += 1

        ann_id = r.get("ann_id")
        cat_id = r.get("category_id")

        nodes.append({
            "ann_id": ann_id,
            "category_id": cat_id,
            "class_name": categories.get(cat_id, ""),
            "bbox": bbox_by_ann.get(ann_id, []),
            "polygon_auto": r.get("polygon_flat", []),
            "polygon_validated": None,
            "confidence": r.get("confidence", 0.0),
            "status": status,
            "was_edited": False,
            "n_points": r.get("n_points", 0),
            "K": r.get("K", 0.0),
        })

    eligible = len(results)
    confidences = [r.get("confidence", 0.0) for r in results]
    mean_conf = round(sum(confidences) / len(confidences), 4) if confidences else 0.0

    return {
        "version": "1.0",
        "diagram_uid": str(diagram_uid),
        "nodes": nodes,
        "stats": {
            "total": eligible,
            "eligible": eligible,
            "auto": auto_count,
            "manual_review": review_count,
            "skipped": 0,
            "mean_confidence": mean_conf,
        },
    }


def _save_and_register(
    db,
    diagram_uid: str,
    contours_dir: Path,
    storage_path: Path,
    result_data: dict,
    stage,
):
    """Save contours_auto.json and register artifact in DB."""
    from app.models import Artifact, ArtifactType

    output_path = contours_dir / "contours_auto.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result_data, f, ensure_ascii=False, indent=2, cls=_NumpyEncoder)

    rel_path = str(output_path.relative_to(storage_path))

    # Remove old artifact if exists (retry case)
    old = db.query(Artifact).filter(
        Artifact.diagram_uid == diagram_uid,
        Artifact.artifact_type == ArtifactType.CONTOURS_AUTO,
    ).first()
    if old:
        db.delete(old)
        db.flush()

    artifact = Artifact(
        diagram_uid=diagram_uid,
        artifact_type=ArtifactType.CONTOURS_AUTO,
        file_path=rel_path,
        file_size=output_path.stat().st_size,
        mime_type="application/json",
    )
    db.add(artifact)
    complete_stage(stage, result_data.get("stats", {}))
    db.commit()

"""
Junction/Bridge Detection — CenterNet heatmap segmentation.

Input:  original image + validated pipe mask + skeleton_final
Output: junction_mask.png, bridge_mask.png, points.json, visualization.png
Status: SKELETONIZED_FINAL → DETECTING_JUNCTIONS → DETECTED_JUNCTIONS
"""

import logging
import os
import traceback
from pathlib import Path

import cv2
import numpy as np
from celery.exceptions import SoftTimeLimitExceeded

from worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="worker.tasks.junction.task_detect_junctions",
    max_retries=2,
    default_retry_delay=60,
    time_limit=1200,
    soft_time_limit=1140,
    acks_late=True,
)
def task_detect_junctions(
    self,
    diagram_uid: str,
    project_code: str = "thermohydraulics",
):
    """
    Junction/Bridge detection via CenterNet heatmap segmentation.

    Uses 5-channel input: RGB + pipe_mask + skeleton.
    Produces junction_mask.png, bridge_mask.png, points.json, visualization.png.

    Args:
        diagram_uid: UUID диаграммы
        project_code: Код проекта для загрузки конфигурации
    """
    from app.db.session import SessionLocal

    db = SessionLocal()

    try:
        logger.info("Starting junction detection for %s", diagram_uid)

        import torch
        from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
        from app.services.project_loader import get_project_loader

        from junction_segmentation.config import Config as JunctConfig
        from junction_segmentation.model import JunctionSegModel
        from junction_segmentation.inference import (
            run_inference,
            create_binary_mask,
            create_visualization,
            skeletonize_mask,
        )

        # ===== 1. Project config =====
        project_loader = get_project_loader()
        project_config = project_loader.load(project_code)
        if not project_config:
            raise ValueError(f"Project config '{project_code}' not found")

        jcfg = project_config.junction_seg

        # ===== 2. Diagram from DB =====
        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if not diagram:
            raise ValueError(f"Diagram {diagram_uid} not found")

        # Idempotency
        if diagram.status == DiagramStatus.DETECTED_JUNCTIONS:
            logger.info("Diagram %s already detected junctions, skipping", diagram_uid)
            return {"status": "already_completed", "diagram_uid": diagram_uid}

        if diagram.status not in (
            DiagramStatus.SKELETONIZED_FINAL,
            DiagramStatus.DETECTING_JUNCTIONS,
            DiagramStatus.ERROR,
        ):
            logger.warning(
                "Diagram %s status is %s, expected SKELETONIZED_FINAL or DETECTING_JUNCTIONS",
                diagram_uid,
                diagram.status.value,
            )
            return {"status": "skipped", "diagram_uid": diagram_uid}

        diagram.status = DiagramStatus.DETECTING_JUNCTIONS
        db.commit()

        # ===== 3. Paths =====
        storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
        diagram_dir = storage_path / str(diagram_uid)

        image_path = diagram_dir / "original" / "image.png"
        if not image_path.exists():
            for ext in (".jpg", ".jpeg", ".tiff", ".tif"):
                alt = image_path.with_suffix(ext)
                if alt.exists():
                    image_path = alt
                    break
            else:
                raise FileNotFoundError(f"Image not found: {image_path}")

        # Prefer refined > validated > original pipe mask
        pipe_mask_path = diagram_dir / "segmentation" / "pipe_mask_refined.png"
        if not pipe_mask_path.exists():
            pipe_mask_path = diagram_dir / "segmentation" / "pipe_mask_validated.png"
        if not pipe_mask_path.exists():
            pipe_mask_path = diagram_dir / "segmentation" / "pipe_mask.png"
        if not pipe_mask_path.exists():
            raise FileNotFoundError(f"Pipe mask not found in {diagram_dir / 'segmentation'}")

        skeleton_path = diagram_dir / "skeleton" / "skeleton_final.png"
        if not skeleton_path.exists():
            raise FileNotFoundError(f"skeleton_final.png not found: {skeleton_path}")

        junction_dir = diagram_dir / "junction"
        junction_dir.mkdir(parents=True, exist_ok=True)

        # ===== 4. Load inputs =====
        img_bgr = cv2.imread(str(image_path))
        if img_bgr is None:
            raise FileNotFoundError(f"Cannot read image: {image_path}")
        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        h, w = img_rgb.shape[:2]

        pipe_mask = cv2.imread(str(pipe_mask_path), cv2.IMREAD_GRAYSCALE)
        if pipe_mask is None:
            raise FileNotFoundError(f"Cannot read pipe mask: {pipe_mask_path}")

        skeleton = cv2.imread(str(skeleton_path), cv2.IMREAD_GRAYSCALE)
        if skeleton is None:
            raise FileNotFoundError(f"Cannot read skeleton: {skeleton_path}")

        # ===== 5. Load model =====
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        weights = Path(jcfg.weights)
        if not weights.is_absolute():
            weights = Path("/app") / weights
        if not weights.exists():
            raise FileNotFoundError(f"Junction seg weights not found: {weights}")

        logger.info("Loading junction segmentation model on %s ...", device)

        ckpt = torch.load(str(weights), map_location="cpu", weights_only=False)
        cfg = JunctConfig()
        for k, v in ckpt.get("config", {}).items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)

        model = JunctionSegModel(cfg).to(device)
        model.load_state_dict(ckpt["model_state_dict"])
        model.eval()

        logger.info(
            "Model loaded (epoch %d), running tiled inference (tile=%d, overlap=%d) ...",
            ckpt.get("epoch", -1),
            jcfg.tile_size,
            jcfg.overlap,
        )

        # ===== 6. Run inference =====
        result = run_inference(
            model, img_rgb, pipe_mask, skeleton, device,
            tile_size=jcfg.tile_size,
            overlap=jcfg.overlap,
            batch_size=jcfg.batch_size,
            junction_threshold=jcfg.junction_threshold,
            bridge_threshold=jcfg.bridge_threshold,
            nms_kernel=jcfg.nms_kernel,
            use_amp=True,
        )

        junctions = result["junction_points"]
        bridges = result["bridge_points"]
        logger.info(
            "Found %d junctions, %d bridges in %.1fs (%d tiles)",
            len(junctions), len(bridges), result["time_sec"], result["n_tiles"],
        )

        # ===== 7. Save outputs =====
        j_mask = create_binary_mask(h, w, junctions, jcfg.square_size)
        b_mask = create_binary_mask(h, w, bridges, jcfg.square_size)
        cv2.imwrite(str(junction_dir / "junction_mask.png"), j_mask)
        cv2.imwrite(str(junction_dir / "bridge_mask.png"), b_mask)

        import json
        with open(junction_dir / "points.json", "w") as f:
            json.dump({
                "junctions": junctions,
                "bridges": bridges,
                "junction_threshold": jcfg.junction_threshold,
                "bridge_threshold": jcfg.bridge_threshold,
                "n_tiles": result["n_tiles"],
                "time_sec": round(result["time_sec"], 2),
            }, f, indent=2)

        vis = create_visualization(img_rgb, skeleton, junctions, bridges, jcfg.square_size)
        cv2.imwrite(
            str(junction_dir / "visualization.png"),
            cv2.cvtColor(vis, cv2.COLOR_RGB2BGR),
        )

        # Free GPU memory
        del model
        if device.type == "cuda":
            torch.cuda.empty_cache()

        # ===== 8. Артефакты в БД =====
        # Remove old artifacts if re-running
        for art_type in (ArtifactType.JUNCTION_MASK, ArtifactType.BRIDGE_MASK):
            old = (
                db.query(Artifact)
                .filter(
                    Artifact.diagram_uid == diagram_uid,
                    Artifact.artifact_type == art_type,
                )
                .first()
            )
            if old:
                db.delete(old)
                db.flush()

        for art_type, art_path in [
            (ArtifactType.JUNCTION_MASK, junction_dir / "junction_mask.png"),
            (ArtifactType.BRIDGE_MASK, junction_dir / "bridge_mask.png"),
        ]:
            artifact = Artifact(
                diagram_uid=diagram_uid,
                artifact_type=art_type,
                file_path=str(art_path.relative_to(storage_path)),
                file_size=art_path.stat().st_size,
                mime_type="image/png",
            )
            db.add(artifact)

        # ===== 9. Обновление статуса =====
        diagram.junction_count = len(junctions)
        diagram.bridge_count = len(bridges)
        diagram.status = DiagramStatus.DETECTED_JUNCTIONS
        diagram.error_message = None
        diagram.error_stage = None
        db.commit()

        # НЕ чейним — ждём UI валидации перекрёстков

        logger.info(
            "Junction detection complete for %s: %d junctions, %d bridges",
            diagram_uid,
            len(junctions),
            len(bridges),
        )

        return {
            "status": "success",
            "diagram_uid": diagram_uid,
            "junction_count": len(junctions),
            "bridge_count": len(bridges),
            "n_tiles": result["n_tiles"],
        }

    except SoftTimeLimitExceeded:
        logger.error("Junction detection timed out for %s", diagram_uid)
        _set_error(
            db, diagram_uid,
            "Junction detection timed out (19 min limit)",
            "detecting_junctions",
        )
        raise

    except Exception as exc:
        logger.error("Junction detection failed for %s: %s", diagram_uid, exc)
        logger.debug(traceback.format_exc())

        if self.request.retries < self.max_retries:
            logger.info("Retrying (%d/%d) ...", self.request.retries + 1, self.max_retries)
            raise self.retry(exc=exc)

        _set_error(db, diagram_uid, str(exc)[:500], "detecting_junctions")
        raise

    finally:
        db.close()


def _set_error(db, diagram_uid: str, message: str, stage: str):
    """Утилита: пометить диаграмму как ERROR."""
    try:
        from app.models import Diagram, DiagramStatus

        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if diagram:
            diagram.status = DiagramStatus.ERROR
            diagram.error_message = message
            diagram.error_stage = stage
            db.commit()
    except Exception as db_exc:
        logger.error("Failed to set error status: %s", db_exc)

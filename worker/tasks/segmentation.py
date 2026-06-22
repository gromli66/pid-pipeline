"""
Segmentation Task — U2-Net++ сегментация труб.

Этапы:
1. Генерация node_mask из coco_validated.json
2. U2-Net++ tiled inference → pipe_mask
3. Сохранение артефактов: NODE_MASK, PIPE_MASK
4. Auto-chain → task_skeletonize
"""

import json
import logging
import os
import traceback
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from celery.exceptions import SoftTimeLimitExceeded

from worker.celery_app import celery_app
from worker.utils.db_helpers import set_diagram_error, check_deleted, upsert_artifact, start_stage, complete_stage, fail_stage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utility: генерация node_mask из COCO JSON
# ---------------------------------------------------------------------------

# Категории, исключаемые из node_mask (эталон: pipe_segmentation.config.defaults)
COCO_PIPE_CATEGORY = "truba"
COCO_ANNOTATION_CATEGORY = "annotation"
# napravlenie — стрелка направления НА трубе: труба проходит сквозь её bbox.
# НЕ должна попадать в node_mask, иначе bbox вырезается из pipe_mask и
# раздувается постобработкой → труба под боксом пропадает. Направление —
# атрибут спец-узла в графе (см. napravlenie_integration_plan #3/#4).
# ВАЖНО: strelka НЕ исключаем — для неё поведение не меняем.
COCO_DIRECTION_CATEGORY = "napravlenie"


def _polygon_to_mask(segmentation, height: int, width: int) -> np.ndarray:
    """Конвертирует полигон(ы) в бинарную маску (PIL — как в pipe module)."""
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)

    if isinstance(segmentation, list):
        for polygon in segmentation:
            if len(polygon) >= 6:  # минимум 3 точки
                coords = [
                    (polygon[i], polygon[i + 1])
                    for i in range(0, len(polygon), 2)
                ]
                draw.polygon(coords, outline=1, fill=1)

    return np.array(mask, dtype=np.uint8)


def _bbox_to_mask(bbox, height: int, width: int) -> np.ndarray:
    """Конвертирует COCO bbox [x,y,w,h] в маску (numpy slicing — как в pipe module)."""
    mask = np.zeros((height, width), dtype=np.uint8)
    x, y, w, h = [int(val) for val in bbox]
    # Exclusive end — ровно w×h пикселей (как в pipe module)
    x2 = min(x + w, width)
    y2 = min(y + h, height)
    x = max(0, x)
    y = max(0, y)
    mask[y:y2, x:x2] = 1
    return mask


def _rle_to_mask(segmentation, height: int, width: int) -> np.ndarray:
    """Конвертирует RLE в бинарную маску."""
    try:
        from pycocotools import mask as mask_utils

        if isinstance(segmentation, dict) and "counts" in segmentation:
            counts = segmentation["counts"]

            # Compressed RLE (строка)
            if isinstance(counts, str):
                return mask_utils.decode(segmentation)

            # Uncompressed RLE (список)
            elif isinstance(counts, list):
                rle_obj = {"counts": counts, "size": [height, width]}
                compressed_rle = mask_utils.frPyObjects(rle_obj, height, width)
                return mask_utils.decode(compressed_rle)

    except Exception:
        pass

    return np.zeros((height, width), dtype=np.uint8)


def _process_annotation(ann: dict, height: int, width: int) -> np.ndarray:
    """
    Обрабатывает одну аннотацию и возвращает маску.

    Поддерживает: polygon, RLE, bbox (с fallback).
    Логика полностью повторяет pipe_segmentation.data.coco_parser.process_annotation.
    """
    mask = np.zeros((height, width), dtype=np.uint8)

    try:
        has_segmentation = "segmentation" in ann and ann["segmentation"]

        if has_segmentation:
            seg = ann["segmentation"]

            # RLE формат
            if isinstance(seg, dict) and "counts" in seg:
                mask = _rle_to_mask(seg, height, width)

            # Polygon формат
            elif isinstance(seg, list):
                if len(seg) == 0:
                    # Пустой список — fallback на bbox
                    if "bbox" in ann:
                        mask = _bbox_to_mask(ann["bbox"], height, width)
                elif isinstance(seg[0], (list, tuple)):
                    mask = _polygon_to_mask(seg, height, width)
                elif isinstance(seg[0], (int, float)):
                    mask = _polygon_to_mask([seg], height, width)

        # Fallback на bbox если маска пустая
        if mask.sum() == 0 and "bbox" in ann:
            mask = _bbox_to_mask(ann["bbox"], height, width)

    except Exception:
        if "bbox" in ann:
            try:
                mask = _bbox_to_mask(ann["bbox"], height, width)
            except Exception:
                pass

    return mask


def generate_node_mask(
    coco_json_path: Path,
    image_height: int,
    image_width: int,
) -> np.ndarray:
    """
    Создать бинарную маску узлов из COCO JSON.

    Логика:
    - Исключаются 'truba', 'annotation' и 'napravlenie'
    - Все остальные категории (включая background) → node_mask
    - Поддерживает polygon, RLE, bbox (с fallback)

    napravlenie исключается, чтобы труба под её bbox не вырезалась из pipe_mask
    (направление обрабатывается отдельно как атрибут спец-узла в графе).

    Returns:
        node_mask [H, W] uint8, 0 | 255
    """
    with open(coco_json_path, "r", encoding="utf-8") as f:
        coco = json.load(f)

    categories = {cat["id"]: cat["name"] for cat in coco.get("categories", [])}

    # ID категорий, исключаемых из node_mask: truba, annotation, napravlenie.
    excluded_names = {
        COCO_PIPE_CATEGORY,
        COCO_ANNOTATION_CATEGORY,
        COCO_DIRECTION_CATEGORY,
    }
    excluded_ids = {
        cat_id for cat_id, cat_name in categories.items()
        if cat_name.lower() in excluded_names
    }

    mask = np.zeros((image_height, image_width), dtype=np.uint8)

    for ann in coco.get("annotations", []):
        cat_id = ann.get("category_id")

        if cat_id in excluded_ids:
            # truba → pipe_mask; annotation → текст; napravlenie → труба насквозь
            continue

        # Все остальные категории = узлы оборудования
        ann_mask = _process_annotation(ann, image_height, image_width)
        mask = np.maximum(mask, ann_mask)

    return mask * 255


# ---------------------------------------------------------------------------
# Celery Task
# ---------------------------------------------------------------------------
@celery_app.task(
    bind=True,
    name="worker.tasks.segmentation.task_segment_pipes",
    max_retries=2,
    default_retry_delay=60,
    time_limit=3600,
    soft_time_limit=3540,
    acks_late=True,
)
def task_segment_pipes(
    self,
    diagram_uid: str,
    project_code: str = "thermohydraulics",
):
    """
    U2-Net++ сегментация труб (tiled inference).

    Args:
        diagram_uid: UUID диаграммы
        project_code: Код проекта для загрузки конфигурации
    """
    from app.db.session import SessionLocal

    db = SessionLocal()
    stage = None

    try:
        logger.info("Starting pipe segmentation for %s", diagram_uid)

        # --- lazy imports (avoid circular / heavy at module level) ---
        import torch
        from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
        from app.services.project_loader import get_project_loader

        from pipe_segmentation.model.architecture import create_model, load_checkpoint
        from pipe_segmentation.inference.engine import TiledInference

        # ===== 1. Project config =====
        project_loader = get_project_loader()
        project_config = project_loader.load(project_code)
        if not project_config:
            raise ValueError(f"Project config '{project_code}' not found")

        seg_cfg = project_config.segmentation
        logger.info("Project: %s, weights: %s", project_config.name, seg_cfg.weights)

        # ===== 2. Diagram from DB =====
        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if not diagram:
            raise ValueError(f"Diagram {diagram_uid} not found")

        if check_deleted(db, diagram_uid):
            logger.info("Diagram %s is deleted, aborting", diagram_uid)
            return {"status": "deleted", "diagram_uid": diagram_uid}

        # Idempotency
        if diagram.status in (
            DiagramStatus.SKELETONIZING,
            DiagramStatus.SKELETONIZED,
            DiagramStatus.VALIDATING_MASKS,
            DiagramStatus.VALIDATED_MASKS,
        ):
            logger.info("Diagram %s already past segmentation, skipping", diagram_uid)
            return {"status": "already_completed", "diagram_uid": diagram_uid}

        if diagram.status not in (DiagramStatus.SEGMENTING, DiagramStatus.ERROR):
            logger.warning(
                "Diagram %s status is %s, expected SEGMENTING",
                diagram_uid,
                diagram.status.value,
            )
            return {"status": "skipped", "diagram_uid": diagram_uid}

        # ===== Processing Stage tracking =====
        from app.models.stage import StageType
        stage = start_stage(db, diagram_uid, StageType.SEGMENTATION, celery_task_id=self.request.id)

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

        seg_dir = diagram_dir / "segmentation"
        seg_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Image: %s (%dx%d)", image_path.name,
                     diagram.image_width or 0, diagram.image_height or 0)

        # ===== 4. Генерация node_mask =====
        coco_path = diagram_dir / "detection" / "coco_validated.json"
        if not coco_path.exists():
            # Fallback на predicted
            coco_path = diagram_dir / "detection" / "coco_predicted.json"
            if coco_path.exists():
                logger.warning("coco_validated.json not found, using coco_predicted.json")

        image_bgr = cv2.imread(str(image_path))
        if image_bgr is None:
            raise ValueError(f"Failed to read image: {image_path}")

        h, w = image_bgr.shape[:2]
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)

        if coco_path.exists():
            node_mask = generate_node_mask(coco_path, h, w)
            node_pixels = int(np.sum(node_mask > 0))
            logger.info("Node mask generated: %d non-zero pixels", node_pixels)
        else:
            logger.warning("No COCO annotations found, using empty node mask")
            node_mask = np.zeros((h, w), dtype=np.uint8)

        node_mask_path = seg_dir / "node_mask.png"
        cv2.imwrite(str(node_mask_path), node_mask)

        # ===== 5. Single-model inference (A_600, 3-канальная RGB) =====
        weights_a = Path(seg_cfg.weights)
        if not weights_a.is_absolute():
            weights_a = Path("/app") / weights_a
        if not weights_a.exists():
            raise FileNotFoundError(f"Checkpoint not found: {weights_a}")

        from worker.utils.device import resolve_device
        device = resolve_device()
        logger.info(
            "Loading model (%s, %s/%s, in_ch=%d) on %s ...",
            weights_a.name, seg_cfg.architecture, seg_cfg.encoder_name,
            seg_cfg.in_channels, device,
        )

        model = create_model(
            architecture=seg_cfg.architecture,
            encoder_name=seg_cfg.encoder_name,
            encoder_weights=None,
            in_channels=seg_cfg.in_channels,
            classes=seg_cfg.classes,
            decoder_attention_type=seg_cfg.decoder_attention_type,
            dual_head=seg_cfg.dual_head_a,
            verbose=False,
        )
        load_checkpoint(str(weights_a), model, device=device, verbose=False)

        engine = TiledInference(
            model=model,
            device=device,
            tile_size=seg_cfg.tile_size,
            overlap=seg_cfg.overlap,
            batch_size=seg_cfg.batch_size,
            threshold=seg_cfg.threshold,
            use_tta=seg_cfg.use_tta,
            binarize=seg_cfg.binarize,
            binarize_method=seg_cfg.binarize_method,
            in_channels=seg_cfg.in_channels,
        )

        logger.info(
            "Running tiled inference (tile=%d, overlap=%d) ...",
            seg_cfg.tile_size, seg_cfg.overlap,
        )
        result = engine.predict(
            image_rgb, node_mask,
            postprocess=seg_cfg.postprocess,
            postprocess_config=seg_cfg.postprocess_config or None,
        )

        # Освобождение GPU (у TiledInference нет free_memory())
        del engine, model
        if device == "cuda":
            torch.cuda.empty_cache()

        pipe_mask = result["mask"]  # [H, W] uint8 0-255
        logger.info(
            "Segmentation done: %d tiles, %.1fs, coverage %.1f%%",
            result["n_tiles"], result["time_sec"], result.get("coverage_pct", 0),
        )

        pipe_mask_path = seg_dir / "pipe_mask.png"
        cv2.imwrite(str(pipe_mask_path), pipe_mask)

        # ===== 5b. Overlay визуализация =====
        overlay_path = None
        if project_config.save_visualizations:
            overlay_path = seg_dir / "segmentation_overlay.png"
            try:
                overlay = image_bgr.copy()
                # Зелёный полупрозрачный overlay на pipe_mask
                green = np.zeros_like(overlay)
                green[:, :, 1] = 255  # зелёный канал
                mask_bool = pipe_mask > 127
                alpha = 0.4
                overlay[mask_bool] = cv2.addWeighted(
                    overlay[mask_bool], 1 - alpha,
                    green[mask_bool], alpha, 0,
                )
                # Красный контур node_mask
                node_bool = node_mask > 127
                node_contours, _ = cv2.findContours(
                    node_bool.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
                )
                cv2.drawContours(overlay, node_contours, -1, (0, 0, 255), 2)
                cv2.imwrite(str(overlay_path), overlay)
                logger.info("Segmentation overlay saved: %s", overlay_path.name)
            except Exception as viz_exc:
                logger.warning("Failed to create overlay: %s", viz_exc)
                overlay_path = None

        # ===== 6. Артефакты в БД =====
        artifacts_to_save = [
            (ArtifactType.NODE_MASK, node_mask_path),
            (ArtifactType.PIPE_MASK, pipe_mask_path),
        ]
        if overlay_path and overlay_path.exists():
            artifacts_to_save.append((ArtifactType.SEGMENTATION_OVERLAY, overlay_path))

        for art_type, art_path in artifacts_to_save:
            upsert_artifact(db, diagram_uid, art_type, str(art_path), storage_path, "image/png")

        # ===== 7. Обновление статуса =====
        diagram.segmentation_pixels = int(np.sum(pipe_mask > 0))
        diagram.status = DiagramStatus.SKELETONIZING
        diagram.error_message = None
        diagram.error_stage = None
        complete_stage(stage, {
            "n_tiles": result["n_tiles"],
            "time_sec": round(result["time_sec"], 2),
            "coverage_pct": round(result.get("coverage_pct", 0), 2),
        })
        db.commit()

        logger.info("Segmentation complete, dispatching skeletonization")

        # ===== 8. Auto-chain → skeleton =====
        from worker.tasks.skeleton import task_skeletonize

        task_skeletonize.delay(diagram_uid, project_code)

        return {
            "status": "success",
            "diagram_uid": diagram_uid,
            "n_tiles": result["n_tiles"],
            "time_sec": round(result["time_sec"], 2),
            "coverage_pct": round(result.get("coverage_pct", 0), 2),
        }

    except SoftTimeLimitExceeded:
        logger.error("Segmentation timed out for %s", diagram_uid)
        fail_stage(stage, "Segmentation timed out (59 min limit)")
        set_diagram_error(db, diagram_uid, "Segmentation timed out (59 min limit)", "segmenting")
        raise

    except Exception as exc:
        logger.error("Segmentation failed for %s: %s", diagram_uid, exc)
        logger.debug(traceback.format_exc())

        if self.request.retries < self.max_retries:
            fail_stage(stage, str(exc)[:500], traceback.format_exc())
            logger.info("Retrying (%d/%d) ...", self.request.retries + 1, self.max_retries)
            raise self.retry(exc=exc)

        fail_stage(stage, str(exc)[:500], traceback.format_exc())
        set_diagram_error(db, diagram_uid, str(exc)[:500], "segmenting")
        raise

    finally:
        db.close()

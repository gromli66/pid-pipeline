"""
Direction Classification Task — проставление направления (up/right/down/left) объектам.

task_classify_direction:
    Запускается ПОСЛЕ валидации детекции (есть coco_validated.json).
    Для каждого объекта целевых классов (по умолчанию napravlenie) классифицирует
    направление и пишет его прямо в coco_validated.json:

        annotation["attributes"]["direction"]            = "up|right|down|left"
        annotation["attributes"]["direction_confidence"] = float

    coco_validated.json — единый источник правды: его читают и сегментация
    (node_mask), и граф (направление как атрибут спец-узла napravlenie).

    Идемпотентность: повторный запуск перезаписывает direction; на выходе нет
    нового файла-артефакта, модифицируется coco_validated.json на месте, поэтому
    статус диаграммы НЕ меняется — шаг встраивается между CVAT_VALIDATION и
    SEGMENTATION без слома основного флоу.
"""

import json
import logging
import os
import traceback
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded

from worker.celery_app import celery_app
from worker.utils.db_helpers import (
    set_diagram_error,
    check_deleted,
    start_stage,
    complete_stage,
    fail_stage,
)

logger = logging.getLogger(__name__)


def _abs_weights(path_str: str) -> Path:
    """Относительный путь к весам — относительно /app (как в task_detect_yolo)."""
    p = Path(path_str)
    return p if p.is_absolute() else Path("/app") / p


@celery_app.task(
    bind=True,
    name="worker.tasks.direction.task_classify_direction",
    max_retries=1,
    default_retry_delay=60,
    time_limit=600,        # 10 min max
    soft_time_limit=540,   # 9 min warning
    acks_late=True,
)
def task_classify_direction(self, diagram_uid: str):
    """
    Классифицировать направление целевых объектов и записать в coco_validated.json.

    Args:
        diagram_uid: UUID диаграммы.

    Returns:
        dict со статусом и статистикой.
    """
    from app.db.session import SessionLocal
    from app.models import Diagram
    from app.models.stage import StageType
    from app.services.project_loader import get_project_loader

    storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
    diagram_dir = storage_path / str(diagram_uid)

    db = SessionLocal()
    stage = None
    try:
        # ===== 1. Диаграмма, базовые проверки =====
        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if not diagram:
            logger.error("Diagram %s not found", diagram_uid)
            return {"status": "not_found", "diagram_uid": diagram_uid}

        if check_deleted(db, diagram_uid):
            logger.info("Diagram %s deleted, aborting direction classification", diagram_uid)
            return {"status": "deleted", "diagram_uid": diagram_uid}

        # ===== 2. Конфиг проекта, проверка включённости =====
        loader = get_project_loader()
        project_config = loader.load(diagram.project_code)
        if not project_config:
            raise RuntimeError(f"Project config not found for '{diagram.project_code}'")

        dc_cfg = project_config.direction_classification
        if not dc_cfg.enabled:
            logger.info(
                "Direction classification disabled for project '%s', skipping",
                diagram.project_code,
            )
            return {"status": "disabled", "diagram_uid": diagram_uid}

        target_classes = set(dc_cfg.classes or [])
        if not target_classes:
            logger.info("No target classes configured for direction, skipping")
            return {"status": "no_classes", "diagram_uid": diagram_uid}

        # Мягкий скип, если веса ещё не подложены — не блокируем цепочку
        # сегментации (DX на период, пока best.pt не размещён).
        weights_path = _abs_weights(dc_cfg.weights)
        if not dc_cfg.weights or not weights_path.exists():
            logger.warning(
                "[%s] Direction weights not found (%s) — skipping, pipeline continues",
                diagram_uid, weights_path,
            )
            return {"status": "weights_missing", "diagram_uid": diagram_uid}

        # ===== 3. Старт стадии =====
        stage = start_stage(
            db, diagram_uid, StageType.DIRECTION_CLASSIFICATION,
            celery_task_id=self.request.id,
        )
        logger.info("Direction classification started for %s", diagram_uid)

        # ===== 4. Входные файлы =====
        coco_path = diagram_dir / "detection" / "coco_validated.json"
        if not coco_path.exists():
            raise FileNotFoundError(f"COCO validated not found: {coco_path}")

        image_path = diagram_dir / "original" / "image.png"
        if not image_path.exists():
            for ext in (".jpg", ".jpeg", ".tiff", ".tif"):
                alt = image_path.with_suffix(ext)
                if alt.exists():
                    image_path = alt
                    break
        if not image_path.exists():
            raise FileNotFoundError(f"Original image not found in {diagram_dir / 'original'}")

        with open(coco_path, "r", encoding="utf-8") as f:
            coco_data = json.load(f)

        categories = {c["id"]: c["name"] for c in coco_data.get("categories", [])}

        # ===== 5. Отбор целевых аннотаций =====
        target_anns = [
            ann for ann in coco_data.get("annotations", [])
            if categories.get(ann.get("category_id"), "") in target_classes
        ]
        logger.info(
            "[%s] Direction targets: %d (classes=%s), %d annotations total",
            diagram_uid, len(target_anns), sorted(target_classes),
            len(coco_data.get("annotations", [])),
        )

        if not target_anns:
            complete_stage(stage, {"targets": 0, "classified": 0})
            db.commit()
            return {"status": "empty", "diagram_uid": diagram_uid, "classified": 0}

        # ===== 6. Инференс =====
        import cv2
        from modules.direction_classifier import DirectionClassifier

        image = cv2.imread(str(image_path))
        if image is None:
            raise RuntimeError(f"Failed to read image: {image_path}")

        from worker.utils.device import resolve_device
        clf = DirectionClassifier(
            weights=weights_path,
            device=resolve_device(),
            img_size=dc_cfg.img_size,
            pad_frac=dc_cfg.pad_frac,
        )

        bboxes = [ann["bbox"] for ann in target_anns]
        preds = clf.predict_batch(image, bboxes)

        # ===== 7. Запись результатов в coco (на месте) =====
        classified = 0
        low_conf = 0
        failed = 0
        thr = dc_cfg.confidence_threshold

        for ann, pred in zip(target_anns, preds):
            if pred is None:
                failed += 1
                continue
            attrs = ann.setdefault("attributes", {})
            attrs["direction"] = pred["direction"]
            attrs["direction_confidence"] = round(float(pred["confidence"]), 4)
            attrs["direction_low_confidence"] = bool(pred["confidence"] < thr)
            classified += 1
            if pred["confidence"] < thr:
                low_conf += 1

        # ===== 8. Освобождение GPU =====
        del clf
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

        # ===== 9. Атомарное сохранение coco_validated.json =====
        tmp_path = coco_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(coco_data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(coco_path)

        stats = {
            "targets": len(target_anns),
            "classified": classified,
            "low_confidence": low_conf,
            "failed": failed,
        }
        complete_stage(stage, stats)
        db.commit()

        logger.info(
            "[%s] Direction classification done: %d/%d classified, %d low-conf, %d failed",
            diagram_uid, classified, len(target_anns), low_conf, failed,
        )
        return {"status": "success", "diagram_uid": diagram_uid, "stats": stats}

    except SoftTimeLimitExceeded:
        logger.error("[%s] Direction classification timed out", diagram_uid)
        fail_stage(stage, "Direction classification timed out (9 min limit)")
        set_diagram_error(db, diagram_uid, "Direction classification timed out", "direction_classification")
        db.rollback()
        raise

    except Exception as exc:
        logger.error(
            "[%s] Direction classification failed: %s\n%s",
            diagram_uid, exc, traceback.format_exc(),
        )
        if self.request.retries < self.max_retries:
            fail_stage(stage, str(exc)[:500], traceback.format_exc())
            db.rollback()
            raise self.retry(exc=exc)

        fail_stage(stage, str(exc)[:500], traceback.format_exc())
        set_diagram_error(db, diagram_uid, str(exc)[:500], "direction_classification")
        db.rollback()
        raise

    finally:
        db.close()

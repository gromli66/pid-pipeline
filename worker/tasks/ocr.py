"""
OCR Tasks — 3-проходный OCR pipeline для P&ID.

task_run_ocr:
    Полный OCR: clean → 3 итерации Surya → regroup → classify → postprocess.
    Входы: original_image + pipe_mask + node_mask + junction/bridge points
    Выходы: ocr/ocr_result.json

    ПАРАЛЛЕЛЬНЫЙ ЗАПУСК: может работать одновременно с task_build_graph.
    НЕ МЕНЯЕТ DiagramStatus — готовность определяется по артефакту OCR_RESULT.
"""

import gc
import json
import logging
import os
import traceback
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded

from worker.celery_app import celery_app
from worker.utils.db_helpers import set_diagram_error, check_deleted

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="worker.tasks.ocr.task_run_ocr",
    max_retries=1,
    default_retry_delay=60,
    time_limit=3600,       # 60 мин max
    soft_time_limit=3300,  # 55 мин warning
    acks_late=True,
)
def task_run_ocr(self, diagram_uid: str):
    """
    3-проходный OCR pipeline для одной диаграммы.

    Запускается параллельно с task_build_graph после complete_junction_validation,
    или отдельно через start_ocr endpoint.

    Не меняет DiagramStatus — готовность определяется по наличию
    артефакта OCR_RESULT.
    """
    import importlib

    from app.db.session import SessionLocal
    from app.models import Diagram, Artifact, ArtifactType
    from app.services.project_loader import get_project_loader

    storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
    diagram_dir = storage_path / str(diagram_uid)
    ocr_dir = diagram_dir / "ocr"

    db = SessionLocal()
    try:
        # ═══ Проверить существование диаграммы ═══
        diagram = db.query(Diagram).filter(
            Diagram.uid == diagram_uid
        ).first()

        if not diagram:
            logger.error("Diagram %s not found", diagram_uid)
            return

        if check_deleted(db, diagram_uid):
            logger.info("Diagram %s is deleted, aborting OCR", diagram_uid)
            return

        # ═══ Idempotency: проверить нет ли уже результата ═══
        existing = db.query(Artifact).filter(
            Artifact.diagram_uid == diagram_uid,
            Artifact.artifact_type == ArtifactType.OCR_RESULT,
        ).first()
        if existing:
            logger.info(
                "OCR result already exists for %s, skipping", diagram_uid
            )
            return

        logger.info("OCR started for %s", diagram_uid)
        ocr_dir.mkdir(parents=True, exist_ok=True)

        # ═══ Загрузка OCR конфига из project YAML ═══
        loader = get_project_loader()
        project_config = loader.load(diagram.project_code)
        if not project_config:
            raise RuntimeError(
                f"Project config not found for '{diagram.project_code}'"
            )
        ocr_cfg = project_config.ocr

        # ═══ Динамический импорт OCR профиля ═══
        # Приоритет: domain_profile_path (v2.0) → profile_path (v1.x) → profile_module (legacy)
        if ocr_cfg.domain_profile_path:
            from modules.ocr.domain_profile import ConfigDrivenProfile
            dp_path = Path(ocr_cfg.domain_profile_path)
            if not dp_path.is_absolute():
                dp_path = Path("/app") / dp_path
            if not dp_path.exists():
                raise FileNotFoundError(
                    f"domain_profile.yaml not found: {dp_path}"
                )
            profile = ConfigDrivenProfile(yaml_path=str(dp_path))
            logger.info(
                "Loaded ConfigDrivenProfile from: %s (name=%s)",
                dp_path, profile.name,
            )
        elif ocr_cfg.profile_path:
            import importlib.util
            profile_file = Path(ocr_cfg.profile_path)
            if not profile_file.is_absolute():
                profile_file = Path("/app") / profile_file
            if not profile_file.exists():
                raise FileNotFoundError(
                    f"OCR profile not found: {profile_file}"
                )
            spec = importlib.util.spec_from_file_location(
                "ocr_profile", str(profile_file)
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            logger.info("Loaded OCR profile from path: %s", profile_file)
        elif ocr_cfg.profile_module:
            mod = importlib.import_module(ocr_cfg.profile_module)
            logger.info("Loaded OCR profile from module: %s", ocr_cfg.profile_module)
        else:
            raise RuntimeError(
                "OCR profile not configured: set 'domain_profile_path', "
                "'profile_path' or 'profile_module' in project YAML ocr section"
            )

        # Legacy path: создаём профиль из Python-класса
        if not ocr_cfg.domain_profile_path:
            ProfileClass = getattr(mod, ocr_cfg.profile_class)
            profile = ProfileClass()
            logger.info(
                "OCR profile class: %s", ocr_cfg.profile_class,
            )

        # ═══ Входные данные ═══
        original_image = diagram_dir / "original" / "image.png"
        pipe_mask = diagram_dir / "segmentation" / "pipe_mask_refined.png"
        if not pipe_mask.exists():
            pipe_mask = diagram_dir / "segmentation" / "pipe_mask_validated.png"
        if not pipe_mask.exists():
            pipe_mask = diagram_dir / "segmentation" / "pipe_mask.png"
        node_mask = diagram_dir / "segmentation" / "node_mask.png"
        junction_points_path = diagram_dir / "junction" / "points.json"

        if not original_image.exists():
            raise FileNotFoundError(
                f"Original image not found: {original_image}"
            )

        # Junction/bridge points (могут отсутствовать — пустые списки)
        junctions = []
        bridges = []
        if junction_points_path.exists():
            with open(junction_points_path, encoding="utf-8") as f:
                pts = json.load(f)

            def _pt(p):
                """dict {"x","y"} или list [y,x] → (x, y)."""
                if isinstance(p, dict):
                    return (p["x"], p["y"])
                return (p[0], p[1])

            junctions = [_pt(p) for p in pts.get("junctions", [])]
            bridges = [_pt(p) for p in pts.get("bridges", [])]
        logger.info(
            "[%s] Inputs: junctions=%d, bridges=%d",
            diagram_uid, len(junctions), len(bridges),
        )

        # ═══ Запуск pipeline ═══
        from modules.ocr.pipeline import run_ocr_pipeline

        result = run_ocr_pipeline(
            image_path=original_image,
            pipe_mask_path=pipe_mask if pipe_mask.exists() else None,
            node_mask_path=node_mask if node_mask.exists() else None,
            junction_points=junctions,
            bridge_points=bridges,
            profile=profile,
            output_dir=ocr_dir,
            no_protection=ocr_cfg.no_protection,
            tile2_size=ocr_cfg.tile2_size,
            tile2_overlap=ocr_cfg.tile2_overlap,
            tile3_size=ocr_cfg.tile3_size,
            tile3_overlap=ocr_cfg.tile3_overlap,
        )

        # ═══ Освободить GPU ═══
        logger.info("[%s] Releasing GPU memory", diagram_uid)
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()

        # ═══ Сохранить результат ═══
        import numpy as np

        class _NumpyEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, (np.integer,)):
                    return int(obj)
                if isinstance(obj, (np.floating,)):
                    return float(obj)
                if isinstance(obj, np.ndarray):
                    return obj.tolist()
                return super().default(obj)

        ocr_result_path = ocr_dir / "ocr_result.json"
        with open(ocr_result_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, cls=_NumpyEncoder)
        logger.info(
            "[%s] OCR result saved: %d target, %d secondary",
            diagram_uid,
            len(result.get("target", [])),
            len(result.get("secondary", [])),
        )

        # ═══ Регистрация артефакта ═══
        rel_path = str(ocr_result_path.relative_to(storage_path))

        # Удалить старый артефакт если есть (retry case)
        old = db.query(Artifact).filter(
            Artifact.diagram_uid == diagram_uid,
            Artifact.artifact_type == ArtifactType.OCR_RESULT,
        ).first()
        if old:
            db.delete(old)
            db.flush()

        artifact = Artifact(
            diagram_uid=diagram_uid,
            artifact_type=ArtifactType.OCR_RESULT,
            file_path=rel_path,
            file_size=ocr_result_path.stat().st_size,
        )
        db.add(artifact)
        db.commit()

        logger.info("[%s] OCR completed successfully", diagram_uid)

    except SoftTimeLimitExceeded:
        logger.error("[%s] OCR timed out (soft limit)", diagram_uid)
        set_diagram_error(db, diagram_uid, "OCR timed out", "ocr")
        db.rollback()
        raise

    except Exception as exc:
        logger.error(
            "[%s] OCR failed: %s\n%s",
            diagram_uid, exc, traceback.format_exc(),
        )

        # BUG-10 fix: retry before giving up
        if self.request.retries < self.max_retries:
            db.rollback()
            raise self.retry(exc=exc)

        # BUG-11 fix: set ERROR status when all retries exhausted
        set_diagram_error(db, diagram_uid, str(exc)[:500], "ocr")
        db.rollback()
        raise

    finally:
        db.close()

"""
OCR Tasks — чистый OCR pipeline для P&ID (П2).

task_run_ocr:
    Доменная детекция текста YOLO (тайлинг + merge) -> Surya 0.17.1 (из коробки,
    расширение бокса/паддинг/опц. выбеление, вертикаль -> вправо) -> cleanup -> фильтр мусора.
    Входы: original_image. Выходы: ocr/ocr_result.json

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
from worker.utils.db_helpers import set_diagram_error, check_deleted, start_stage, complete_stage, fail_stage, persist_failed_attempt, make_step_reporter
from app.core import obs
from app.core.errors import ArtifactMissingError, OcrError
from app.core.logging import get_logger

logger = get_logger(__name__)


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
    Чистый OCR pipeline для одной диаграммы.

    Запускается параллельно с task_build_graph после complete_junction_validation,
    или отдельно через start_ocr endpoint. Не меняет DiagramStatus.
    """
    # Surya читает TORCH_DEVICE из окружения — проставляем по PID_DEVICE
    from worker.utils.device import apply_torch_device_env
    apply_torch_device_env()

    from app.db.session import SessionLocal
    from app.models import Diagram, Artifact, ArtifactType

    storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
    diagram_dir = storage_path / str(diagram_uid)
    ocr_dir = diagram_dir / "ocr"

    db = SessionLocal()
    stage = None

    # Корреляционный контекст фазы (Волна 3): uid/phase/task_id/attempt → в каждую
    # строку лога через ContextFilter (Волна 0).
    obs.bind(
        uid=str(diagram_uid),
        phase="ocr",
        task_id=self.request.id,
        attempt=self.request.retries,
    )

    try:
        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if not diagram:
            logger.error("Diagram %s not found", diagram_uid)
            return

        if check_deleted(db, diagram_uid):
            logger.info("Diagram %s is deleted, aborting OCR", diagram_uid)
            return

        # Idempotency: уже есть результат?
        existing = db.query(Artifact).filter(
            Artifact.diagram_uid == diagram_uid,
            Artifact.artifact_type == ArtifactType.OCR_RESULT,
        ).first()
        if existing:
            logger.info("OCR result already exists for %s, skipping", diagram_uid)
            return

        # OCR disabled в конфиге проекта -> skip (defense-in-depth)
        from app.services.project_loader import get_project_loader as _gpl
        _pc = _gpl().load(diagram.project_code)
        if _pc and not getattr(_pc.ocr, "enabled", True):
            logger.info("OCR disabled for project '%s', skipping", diagram.project_code)
            return {"status": "disabled", "diagram_uid": diagram_uid}

        from app.models.stage import StageType
        stage = start_stage(db, diagram_uid, StageType.OCR, celery_task_id=self.request.id)
        obs.bind_step_sink(make_step_reporter(stage.id))  # current_step → клиент (Волна B)

        logger.info("OCR started for %s", diagram_uid)
        ocr_dir.mkdir(parents=True, exist_ok=True)

        # === П2: ЧИСТЫЙ OCR — доменная детекция YOLO (тайлинг+merge) + Surya из коробки ===
        # Старый путь (профили/конфиг/reclustering/маски/3 итерации) отключён.
        # Логика: modules/ocr/pipeline_clean.py. Модель детекции — TEXT_YOLO_WEIGHTS.
        from worker.utils.device import resolve_device
        from modules.ocr.pipeline_clean import run_ocr_pipeline_clean

        with obs.step("load_inputs", logger):
            original_image = diagram_dir / "original" / "image.png"
            if not original_image.exists():
                raise ArtifactMissingError(
                    f"Original image not found: {original_image}", stage="ocr"
                )

        model_path = os.getenv("TEXT_YOLO_WEIGHTS", "/models/text_detect/best.pt")
        device = resolve_device()
        whiten = os.getenv("OCR_WHITEN", "0") == "1"
        expand_frac = float(os.getenv("OCR_EXPAND_FRAC", "0.07"))
        pad_frac = float(os.getenv("OCR_PAD_FRAC", "0.25"))
        logger.info(
            "[%s] Clean OCR: model=%s device=%s whiten=%s expand=%.2f pad=%.2f",
            diagram_uid, model_path, device, whiten, expand_frac, pad_frac,
        )

        with obs.step("compute", logger):
            result = run_ocr_pipeline_clean(
                image_path=original_image,
                output_dir=ocr_dir,
                model_path=model_path,
                device=device,
                expand_frac=expand_frac,
                pad_frac=pad_frac,
                whiten=whiten,
            )

        # === Освободить GPU ===
        logger.info("[%s] Releasing GPU memory", diagram_uid)
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()

        # === Сохранить результат ===
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

        with obs.step("persist_artifacts", logger):
            ocr_result_path = ocr_dir / "ocr_result.json"
            with open(ocr_result_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2, cls=_NumpyEncoder)
            logger.info(
                "[%s] OCR result saved: %d target, %d secondary",
                diagram_uid,
                len(result.get("target", [])),
                len(result.get("secondary", [])),
            )

            # === Регистрация артефакта ===
            rel_path = str(ocr_result_path.relative_to(storage_path))

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
            complete_stage(stage, {"result_path": rel_path})
            db.commit()

        # === Слияние OCR -> общий граф (чистая схема) ===
        # Граф уже создан (graph_validated при simple-валидации) — переносим
        # блоки в graph["text_blocks"]. Идемпотентно; сырой ocr_result остаётся.
        try:
            from app.services.ocr_graph_merge import merge_ocr_result_into_graph
            graph_art = (
                db.query(Artifact)
                .filter(
                    Artifact.diagram_uid == diagram_uid,
                    Artifact.artifact_type.in_((
                        ArtifactType.GRAPH_VALIDATED, ArtifactType.GRAPH_JSON,
                    )),
                )
                .order_by(Artifact.artifact_type == ArtifactType.GRAPH_VALIDATED)
                .first()
            )
            if graph_art:
                n_merged = merge_ocr_result_into_graph(
                    storage_path / graph_art.file_path, ocr_result_path,
                )
                if n_merged:
                    logger.info("[%s] merged %d OCR blocks into graph", diagram_uid, n_merged)
        except Exception as merge_exc:  # noqa: BLE001
            logger.warning(
                "[%s] OCR->graph merge skipped: %s", diagram_uid, merge_exc,
                exc_info=True,
            )

        logger.info("[%s] OCR completed successfully", diagram_uid)

    except SoftTimeLimitExceeded:
        # exc_info=True → traceback в лог; fail_stage без exc= (STL — не PipelineError,
        # error_code/failed_step остаются NULL, как в skeleton/segmentation).
        logger.error("[%s] OCR timed out (soft limit)", diagram_uid, exc_info=True)
        fail_stage(stage, "OCR timed out", traceback.format_exc())
        set_diagram_error(db, diagram_uid, "OCR timed out", "ocr")
        db.rollback()
        raise

    except Exception as exc:
        # exc_info=True + exc= в fail_stage → error_code/failed_step/traceback
        # доезжают до /stages (DoD §4).
        logger.error("[%s] OCR failed: %s", diagram_uid, exc, exc_info=True)
        if self.request.retries < self.max_retries:
            # rollback теперь ВНУТРИ (и ДО fail_stage): прежний порядок стирал сам фейл
            persist_failed_attempt(db, stage, str(exc)[:500], traceback.format_exc(), exc=exc)
            raise self.retry(exc=exc)
        fail_stage(stage, str(exc)[:500], traceback.format_exc(), exc=exc)
        set_diagram_error(db, diagram_uid, str(exc)[:500], "ocr")
        db.rollback()
        raise

    finally:
        db.close()


@celery_app.task(
    name="worker.tasks.ocr.task_recognize_boxes",
    time_limit=300,
    soft_time_limit=280,
)
def task_recognize_boxes(diagram_uid: str, boxes: list):
    """П3: распознать переданные ВРУЧНУЮ боксы (ручной режим валидации OCR), батчем.

    boxes: [[x0,y0,x1,y1], ...] или [{"bbox":[...]}, ...].
    Возврат: [{"bbox":[...], "text": "...", "confidence": ..., "junk": ...}].
    """
    from worker.utils.device import apply_torch_device_env, resolve_device
    apply_torch_device_env()

    from modules.ocr.pipeline_clean import recognize_given_boxes

    storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
    image = storage_path / str(diagram_uid) / "original" / "image.png"
    if not image.exists():
        raise ArtifactMissingError(f"Original image not found: {image}")

    bxs = [b.get("bbox") if isinstance(b, dict) else b for b in (boxes or [])]
    bxs = [b for b in bxs if b]
    if not bxs:
        return []

    device = resolve_device()
    whiten = os.getenv("OCR_WHITEN", "0") == "1"
    expand_frac = float(os.getenv("OCR_EXPAND_FRAC", "0.07"))
    pad_frac = float(os.getenv("OCR_PAD_FRAC", "0.25"))
    items = recognize_given_boxes(
        image, bxs, device=device,
        expand_frac=expand_frac, pad_frac=pad_frac, whiten=whiten,
    )
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()
    except Exception:
        logger.debug("GPU cleanup skipped", exc_info=True)
    return items

"""
Detection Tasks - YOLO детекция.
"""

import os
import tempfile
import traceback
from pathlib import Path

from worker.celery_app import celery_app
from celery.exceptions import SoftTimeLimitExceeded
from worker.utils.db_helpers import set_diagram_error, check_deleted, upsert_artifact, start_stage, complete_stage, fail_stage, persist_failed_attempt, make_step_reporter
from worker.utils.device import resolve_device
from app.core import obs
from app.core.errors import (
    ArtifactMissingError,
    ArtifactWriteError,
    ConfigError,
    CVATError,
    PipelineError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)


def detections_to_yolo_txt(detections: list) -> str:
    """
    Конвертация детекций в YOLO формат (для сохранения в файл).

    Args:
        detections: Список детекций от NodeDetector.detect()

    Returns:
        Строка в YOLO формате (class x_center y_center width height confidence)
    """
    lines = []
    for det in detections:
        line = "{} {:.6f} {:.6f} {:.6f} {:.6f} {:.4f}".format(
            det["class_id"],
            det["x_center"],
            det["y_center"],
            det["width"],
            det["height"],
            det.get("confidence", 1.0),
        )
        lines.append(line)
    return "\n".join(lines)


@celery_app.task(
    bind=True,
    name="worker.tasks.detection.task_detect_yolo",
    max_retries=2,
    default_retry_delay=60,
    time_limit=5400,
    soft_time_limit=5340,  # 89 min - 1 min for cleanup before hard kill
    acks_late=True,
)
def task_detect_yolo(self, diagram_uid: str, project_code: str = "thermohydraulics", model_id: str = None):
    """
    Ансамблевая YOLO детекция с SAHI (3 модели на разных tile_size + WBF).

    Этапы:
    1. Загрузить изображение из storage
    2. NodeDetector.detect()
    3. Сохранить yolo_predicted.txt
    4. Создать CVAT task + job
    5. Импортировать аннотации в CVAT
    6. Обновить статус -> detected

    Args:
        diagram_uid: UUID диаграммы
        project_code: Код проекта для загрузки конфигурации (default: thermohydraulics)
        model_id: ID модели детекции (None → default_model из конфига)
    """
    # Импорт SessionLocal (PYTHONPATH=/app настроен в Dockerfile.worker)
    from app.db.session import SessionLocal

    db = SessionLocal()
    stage = None

    # Корреляционный контекст фазы: uid/phase/task_id/attempt попадают в каждую
    # строку лога (в т.ч. под-под-шаги ансамбля) через ContextFilter (Волна 0).
    obs.bind(
        uid=str(diagram_uid),
        phase="detecting",
        task_id=self.request.id,
        attempt=self.request.retries,
    )

    try:
        logger.info("detection: task received", extra={"event": "task_start"})

        # Импорты внутри task (избегаем circular imports)
        from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
        from app.services.project_loader import get_project_loader
        from app.services.cvat_client import get_cvat_client, CVATLabel
        from app.services.cvat_export import (
            CVATExporter,
            Detection,
            detections_to_cvat_detections,
            create_exporter_from_config,
        )
        from modules.yolo_detector import EnsembleDetector

        # ===== 1. Загрузка конфигурации проекта =====
        project_loader = get_project_loader()
        project_config = project_loader.load(project_code)
        if not project_config:
            raise ConfigError(
                f"Project config '{project_code}' not found", stage="detecting"
            )

        logger.info("detection: project=%s", project_config.name, extra={"event": "config"})

        # ===== 2. Получаем диаграмму из БД =====
        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if not diagram:
            raise PipelineError(
                f"Diagram {diagram_uid} not found",
                stage="detecting", diagram_uid=str(diagram_uid),
            )

        if check_deleted(db, diagram_uid):
            logger.info("detection: diagram deleted, aborting", extra={"event": "skip"})
            return {"status": "deleted", "diagram_uid": diagram_uid}

        # Idempotency: если уже обработана - не перезапускаем
        if diagram.status == DiagramStatus.DETECTED:
            logger.info(
                "detection: already detected, skipping (idempotency)",
                extra={"event": "skip"},
            )
            return {"status": "already_completed", "diagram_uid": diagram_uid}

        if diagram.status not in [DiagramStatus.DETECTING, DiagramStatus.ERROR]:
            logger.info(
                "detection: status=%s, expected DETECTING, skipping",
                diagram.status.value, extra={"event": "skip"},
            )
            return {"status": "skipped", "diagram_uid": diagram_uid}

        # ===== Processing Stage tracking =====
        from app.models.stage import StageType
        stage = start_stage(db, diagram_uid, StageType.DETECTION, celery_task_id=self.request.id)
        obs.bind_step_sink(make_step_reporter(stage.id))  # current_step → клиент (Волна B)

        # ===== 3. LOAD_INPUTS: путь к изображению =====
        storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
        with obs.step("load_inputs", logger, artifact="image"):
            image_path = storage_path / str(diagram_uid) / "original" / "image.png"

            # Проверяем существование файла
            if not image_path.exists():
                for ext in [".jpg", ".jpeg", ".tiff", ".tif"]:
                    alt_path = image_path.with_suffix(ext)
                    if alt_path.exists():
                        image_path = alt_path
                        break
                else:
                    raise ArtifactMissingError(
                        f"Image not found: {image_path}", stage="detecting"
                    )

        # ===== 4. LOAD_MODEL: конфиг модели + построение ансамбля =====
        with obs.step("load_model", logger):
            model_cfg = project_config.detection.get_model(model_id)
            effective_model_id = model_id or project_config.detection.default_model

            if model_cfg.type != "ensemble" or not model_cfg.ensemble_models:
                raise ConfigError(
                    f"Detection model '{effective_model_id}' must have type 'ensemble' "
                    f"with non-empty 'ensemble_models' (got type='{model_cfg.type}')",
                    stage="detecting",
                )

            def _abs_weights(path_str: str) -> Path:
                """Относительный путь - относительно /app."""
                p = Path(path_str)
                return p if p.is_absolute() else Path("/app") / p

            # Per-class confidence: инференс с min(thresholds), потом фильтрация
            base_confidence = model_cfg.confidence
            if model_cfg.per_class_confidence:
                min_conf = min(
                    base_confidence,
                    min(model_cfg.per_class_confidence.values()),
                )
            else:
                min_conf = base_confidence

            device = resolve_device()
            detector = EnsembleDetector(
                models=[
                    {
                        "weights": _abs_weights(m.weights),
                        "tile_size": m.tile_size,
                        "weight": m.weight,
                        "sahi_overlap": m.sahi_overlap,
                        "confidence": min_conf,
                    }
                    for m in model_cfg.ensemble_models
                ],
                merge_strategy=model_cfg.merge_strategy,
                iou_threshold=model_cfg.iou_threshold,
                confidence_threshold=min_conf,
                skip_box_thr=model_cfg.skip_box_thr,
                device=device,
                per_class_weights=model_cfg.per_class_weights or None,
            )

        # ===== Баннер: входы / размеры / модель (DoD §4) =====
        _banner = {
            "event": "banner",
            "image": str(image_path),
            "model": effective_model_id,
            "model_name": model_cfg.name,
            "tiles": [m.tile_size for m in model_cfg.ensemble_models],
            "merge": model_cfg.merge_strategy,
            "device": device,
        }
        try:
            _banner["bytes"] = image_path.stat().st_size
            from PIL import Image
            with Image.open(image_path) as _im:
                _banner["width"], _banner["height"] = _im.size
        except Exception:
            pass  # баннер информативный — не роняем задачу из-за размеров
        logger.info(
            "detection: start image=%s bytes=%s wh=%sx%s model=%s(%s) tiles=%s merge=%s device=%s",
            _banner["image"], _banner.get("bytes"),
            _banner.get("width"), _banner.get("height"),
            effective_model_id, model_cfg.name, _banner["tiles"],
            _banner["merge"], device, extra=_banner,
        )
        if model_cfg.per_class_weights:
            logger.info(
                "detection: adaptive ensemble, per-class weights for %d classes",
                len(model_cfg.per_class_weights),
            )

        # ===== 5. COMPUTE: ансамблевая детекция (tiling/inference/fusion внутри) =====
        with obs.step("compute", logger, model=effective_model_id):
            detections = detector.detect(
                image=image_path,
                apply_grayscale=True,  # бинаризация как при обучении ансамбля
                apply_reverse_mapping=True,  # 34->35, 35->38
            )

        detection_count_raw = len(detections)
        logger.info(
            "detection: raw detections=%d", detection_count_raw,
            extra={"event": "compute_done"},
        )

        # ===== 6. POSTPROCESS: per-class confidence + разрешение перекрытий =====
        with obs.step("postprocess", logger):
            if model_cfg.per_class_confidence:
                before = len(detections)
                filtered = []
                for det in detections:
                    cls_name = det.get("class_name", "")
                    threshold = model_cfg.per_class_confidence.get(
                        cls_name, base_confidence
                    )
                    if det.get("confidence", 1.0) >= threshold:
                        filtered.append(det)
                detections = filtered
                dropped = before - len(detections)
                if dropped > 0:
                    logger.info(
                        "detection: per_class_confidence dropped=%d remaining=%d",
                        dropped, len(detections),
                    )
            else:
                # Фильтрация по глобальному порогу если min_conf был ниже base
                if min_conf < base_confidence:
                    detections = [
                        d for d in detections
                        if d.get("confidence", 1.0) >= base_confidence
                    ]

            from modules.yolo_detector import resolve_overlaps

            detections = resolve_overlaps(
                detections,
                mutual_overlap_threshold=0.7,
            )

            detection_count = len(detections)
            suppressed = detection_count_raw - detection_count
            if suppressed > 0:
                logger.info(
                    "detection: resolve_overlaps suppressed=%d remaining=%d",
                    suppressed, detection_count,
                )
            else:
                logger.info("detection: resolve_overlaps no overlaps found")

        # ===== 7. PERSIST_ARTIFACTS: сохраняем YOLO predictions =====
        with obs.step("persist_artifacts", logger, artifact="yolo_predicted"):
            detection_dir = storage_path / str(diagram_uid) / "detection"
            detection_dir.mkdir(parents=True, exist_ok=True)

            yolo_path = detection_dir / "yolo_predicted.txt"

            from modules.yolo_detector import detections_to_yolo
            yolo_txt = detections_to_yolo(detections, include_confidence=False)

            try:
                yolo_path.write_text(yolo_txt)
            except OSError as exc:
                raise ArtifactWriteError(
                    f"failed to write {yolo_path}", stage="detecting", cause=exc
                ) from exc

            logger.info("detection: saved predictions path=%s", yolo_path)

            # Создаём артефакт YOLO_PREDICTED
            upsert_artifact(db, diagram_uid, ArtifactType.YOLO_PREDICTED,
                            str(yolo_path), storage_path)

        # ===== 8. Создаём CVAT task (Волна 1: не фатально, но видимо) =====
        cvat_task_id = None
        cvat_job_id = None

        try:
            cvat_client = get_cvat_client()

            # Авторизация через CVAT_TOKEN (в settings)

            # Создаём labels из конфига проекта
            labels = [CVATLabel(name=cls.name) for cls in project_config.classes]

            # Получаем или создаём проект
            project_id = cvat_client.get_or_create_project(
                name=project_config.cvat_project_name,
                labels=labels,
            )
            logger.info("detection: cvat project id=%s", project_id)

            # Создаём task с именем из original_filename или uid
            task_name = f"Diagram #{diagram.number} - {diagram.original_filename}"
            cvat_task_id, cvat_job_id = cvat_client.create_task(
                project_id=project_id,
                name=task_name,
                image_path=image_path,
            )
            logger.info("detection: cvat task id=%s job id=%s", cvat_task_id, cvat_job_id)

            # ===== 9. Импортируем аннотации в CVAT =====
            if detections:
                # Конвертируем детекции в формат CVAT
                cvat_detections = detections_to_cvat_detections(detections)

                # Создаём exporter с class_mapping из конфига
                exporter = create_exporter_from_config(project_config)

                # Создаём временный ZIP для импорта
                with tempfile.TemporaryDirectory() as temp_dir:
                    zip_path = Path(temp_dir) / "annotations.zip"
                    exporter.export_yolo(
                        detections=cvat_detections,
                        image_filename=image_path.name,
                        output_path=zip_path,
                    )

                    # Импортируем в CVAT
                    cvat_client.import_annotations(
                        task_id=cvat_task_id,
                        annotations_path=zip_path,
                        format_name="YOLO 1.1",
                    )
                    logger.info(
                        "detection: imported %d annotations to cvat", len(detections)
                    )

            # Сохраняем URL для быстрого доступа
            cvat_url = cvat_client.get_task_url(cvat_task_id, cvat_job_id)
            logger.info("detection: cvat url=%s", cvat_url)

        except CVATError as cvat_exc:
            # CVAT-сбой не фатален для detection (сама детекция выполнена), но теперь
            # видимый: типизированный + warning-лог + код на стадии.
            logger.warning(
                "detection: CVAT step failed (non-fatal)",
                extra={"uid": str(diagram_uid), "phase": "detecting",
                       "step": "export_to_cvat", "event": "error", "code": cvat_exc.code},
                exc_info=True,
            )
            if stage is not None:
                stage.error_code = cvat_exc.code
                stage.error_message = str(cvat_exc)[:500]
        except Exception as cvat_exc:
            # Не-CVAT сбой CVAT-блока (парсинг ответа, конфиг экспортёра, tempfile/zip):
            # как в deploy — non-fatal (детекция выполнена, разметку можно догрузить).
            # Фатальность гоняла бы полный GPU-пересчёт ретраями и плодила дубликаты
            # CVAT-задач — create_task не идемпотентен (аудит 2026-07-09, R1/A6).
            logger.warning(
                "detection: CVAT step failed (non-fatal, non-CVAT error)",
                extra={"uid": str(diagram_uid), "phase": "detecting",
                       "step": "export_to_cvat", "event": "error",
                       "code": "cvat_export_failed"},
                exc_info=True,
            )
            if stage is not None:
                stage.error_code = "cvat_export_failed"
                stage.error_message = str(cvat_exc)[:500]

        # ===== 10. Обновляем диаграмму в БД =====
        diagram.status = DiagramStatus.DETECTED
        diagram.detection_count = detection_count
        diagram.detection_model = effective_model_id
        diagram.cvat_task_id = cvat_task_id
        diagram.cvat_job_id = cvat_job_id

        complete_stage(stage, {"detection_count": detection_count, "model": effective_model_id})
        db.commit()

        logger.info(
            "detection: completed count=%d", detection_count,
            extra={"event": "task_done"},
        )

        return {
            "status": "success",
            "diagram_uid": diagram_uid,
            "detection_count": detection_count,
            "detection_model": effective_model_id,
            "cvat_task_id": cvat_task_id,
            "cvat_job_id": cvat_job_id,
        }

    except SoftTimeLimitExceeded:
        logger.error(
            "detection: timed out (89 min limit)", exc_info=True,
            extra={"event": "timeout"},
        )
        fail_stage(stage, "Detection timed out (89 min limit)", traceback.format_exc())
        set_diagram_error(db, diagram_uid, "Detection timed out (89 min limit)", "detecting")
        raise

    except Exception as exc:
        # exc_info=True + exc= в fail_stage → error_code/failed_step/traceback
        # доезжают до /stages (DoD §4). Под-под-шаг сбоя (inference/fusion/…)
        # проставляется obs.step() и всплывает в failed_step.
        logger.error("detection: failed: %s", exc, exc_info=True, extra={"event": "error"})

        # Retry или fail -- НЕ ставим ERROR до исчерпания всех попыток
        if self.request.retries < self.max_retries:
            persist_failed_attempt(db, stage, str(exc)[:500], traceback.format_exc(), exc=exc)
            logger.warning(
                "detection: retrying (%d/%d)",
                self.request.retries + 1, self.max_retries,
            )
            raise self.retry(exc=exc)

        # Все попытки исчерпаны -- теперь ставим ERROR
        fail_stage(stage, str(exc)[:500], traceback.format_exc(), exc=exc)
        set_diagram_error(db, diagram_uid, str(exc)[:500], "detecting")
        raise

    finally:
        db.close()

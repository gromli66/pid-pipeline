"""
Detection Tasks - YOLO детекция.
"""

import os
import tempfile
import traceback
from pathlib import Path

from worker.celery_app import celery_app
from celery.exceptions import SoftTimeLimitExceeded
from worker.utils.db_helpers import set_diagram_error, check_deleted, upsert_artifact


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
    time_limit=1800,
    soft_time_limit=1740,  # 29 min - 1 min for cleanup before hard kill
    acks_late=True,
)
def task_detect_yolo(self, diagram_uid: str, project_code: str = "thermohydraulics", model_id: str = None):
    """
    YOLO детекция с SAHI.

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

    try:
        print(f"[DETECT] Starting YOLO detection for {diagram_uid}")

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
        from modules.yolo_detector import NodeDetector

        # ===== 1. Загрузка конфигурации проекта =====
        project_loader = get_project_loader()
        project_config = project_loader.load(project_code)
        if not project_config:
            raise ValueError(f"Project config '{project_code}' not found")

        print(f"[PROJECT] Project: {project_config.name}")

        # ===== 2. Получаем диаграмму из БД =====
        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if not diagram:
            raise ValueError(f"Diagram {diagram_uid} not found")

        if check_deleted(db, diagram_uid):
            print(f"[SKIP] Diagram {diagram_uid} is deleted, aborting")
            return {"status": "deleted", "diagram_uid": diagram_uid}

        # Idempotency: если уже обработана - не перезапускаем
        if diagram.status == DiagramStatus.DETECTED:
            print(f"[SKIP] Diagram {diagram_uid} already detected, skipping (idempotency)")
            return {"status": "already_completed", "diagram_uid": diagram_uid}

        if diagram.status not in [DiagramStatus.DETECTING, DiagramStatus.ERROR]:
            print(f"[SKIP] Diagram {diagram_uid} status is {diagram.status.value}, expected DETECTING")
            return {"status": "skipped", "diagram_uid": diagram_uid}

        # ===== 3. Получаем путь к изображению =====
        storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
        image_path = storage_path / str(diagram_uid) / "original" / "image.png"

        # Проверяем существование файла
        if not image_path.exists():
            for ext in [".jpg", ".jpeg", ".tiff", ".tif"]:
                alt_path = image_path.with_suffix(ext)
                if alt_path.exists():
                    image_path = alt_path
                    break
            else:
                raise FileNotFoundError(f"Image not found: {image_path}")

        print(f"[FILE] Image path: {image_path}")

        # ===== 4. YOLO детекция =====
        model_cfg = project_config.detection.get_model(model_id)
        effective_model_id = model_id or project_config.detection.default_model
        print(f"[MODEL] Using detection model: '{effective_model_id}' ({model_cfg.name})")

        weights_path = Path(model_cfg.weights)
        if not weights_path.is_absolute():
            # Относительный путь - относительно /app
            weights_path = Path("/app") / weights_path

        # Per-class confidence: инференс с min(thresholds), потом фильтрация
        base_confidence = model_cfg.confidence
        if model_cfg.per_class_confidence:
            min_conf = min(
                base_confidence,
                min(model_cfg.per_class_confidence.values()),
            )
        else:
            min_conf = base_confidence

        detector = NodeDetector(
            weights=weights_path,
            confidence=min_conf,
            device=os.getenv("YOLO_DEVICE", "cuda"),
            use_sahi=True,
            sahi_slice_size=model_cfg.sahi_slice_size,
            sahi_overlap_ratio=model_cfg.sahi_overlap_ratio,
            apply_preprocessing=False,
        )

        detections = detector.detect(
            image=image_path,
            return_absolute=False,
            apply_reverse_mapping=True,  # 34->35, 35->38
        )

        detection_count_raw = len(detections)
        print(f"[OK] Detected {detection_count_raw} objects (raw)")

        # ===== 4.0.1. Per-class confidence фильтрация =====
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
                print(f"[FILTER] per_class_confidence: {dropped} dropped, {len(detections)} remaining")
        else:
            # Фильтрация по глобальному порогу если min_conf был ниже base
            if min_conf < base_confidence:
                detections = [
                    d for d in detections
                    if d.get("confidence", 1.0) >= base_confidence
                ]

        # ===== 4.1. Постпроцессинг: разрешение перекрытий =====
        from modules.yolo_detector import resolve_overlaps

        detections = resolve_overlaps(
            detections,
            mutual_overlap_threshold=0.7,
        )

        detection_count = len(detections)
        suppressed = detection_count_raw - detection_count
        if suppressed > 0:
            print(f"[FILTER] resolve_overlaps: {suppressed} suppressed, {detection_count} remaining")
        else:
            print(f"[FILTER] resolve_overlaps: no overlaps found")

        # ===== 5. Сохраняем YOLO predictions =====
        detection_dir = storage_path / str(diagram_uid) / "detection"
        detection_dir.mkdir(parents=True, exist_ok=True)

        yolo_path = detection_dir / "yolo_predicted.txt"

        from modules.yolo_detector import detections_to_yolo
        yolo_txt = detections_to_yolo(detections, include_confidence=False)

        yolo_path.write_text(yolo_txt)

        print(f"[SAVE] Saved predictions to {yolo_path}")

        # Создаём артефакт YOLO_PREDICTED
        upsert_artifact(db, diagram_uid, ArtifactType.YOLO_PREDICTED,
                        str(yolo_path), storage_path)

        # ===== 6. Создаём CVAT task =====
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
            print(f"[CVAT] CVAT project ID: {project_id}")

            # Создаём task с именем из original_filename или uid
            task_name = f"Diagram #{diagram.number} - {diagram.original_filename}"
            cvat_task_id, cvat_job_id = cvat_client.create_task(
                project_id=project_id,
                name=task_name,
                image_path=image_path,
            )
            print(f"[TASK] CVAT task ID: {cvat_task_id}, job ID: {cvat_job_id}")

            # ===== 7. Импортируем аннотации в CVAT =====
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
                    print(f"[UPLOAD] Imported {len(detections)} annotations to CVAT")

            # Сохраняем URL для быстрого доступа
            cvat_url = cvat_client.get_task_url(cvat_task_id, cvat_job_id)
            print(f"[LINK] CVAT URL: {cvat_url}")

        except Exception as cvat_exc:
            # CVAT ошибки не фатальны - детекция выполнена
            print(f"[WARN] CVAT error (non-fatal): {cvat_exc}")
            print(traceback.format_exc())

        # ===== 8. Обновляем диаграмму в БД =====
        diagram.status = DiagramStatus.DETECTED
        diagram.detection_count = detection_count
        diagram.detection_model = effective_model_id
        diagram.cvat_task_id = cvat_task_id
        diagram.cvat_job_id = cvat_job_id

        db.commit()

        print(f"[OK] Detection completed for {diagram_uid}")

        return {
            "status": "success",
            "diagram_uid": diagram_uid,
            "detection_count": detection_count,
            "detection_model": effective_model_id,
            "cvat_task_id": cvat_task_id,
            "cvat_job_id": cvat_job_id,
        }

    except SoftTimeLimitExceeded:
        print(f"[TIMEOUT] Detection timed out for {diagram_uid}")
        set_diagram_error(db, diagram_uid, "Detection timed out (29 min limit)", "detecting")
        raise

    except Exception as exc:
        print(f"[ERROR] Detection failed: {exc}")
        print(traceback.format_exc())

        # Retry или fail -- НЕ ставим ERROR до исчерпания всех попыток
        if self.request.retries < self.max_retries:
            print(f"[RETRY] Retrying ({self.request.retries + 1}/{self.max_retries})...")
            raise self.retry(exc=exc)

        # Все попытки исчерпаны -- теперь ставим ERROR
        set_diagram_error(db, diagram_uid, str(exc)[:500], "detecting")
        raise

    finally:
        db.close()

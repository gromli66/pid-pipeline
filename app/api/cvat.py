"""
CVAT API - интеграция с CVAT.
"""

import asyncio
import json
import os
import tempfile
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Tuple
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_async_db
from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
from app.models.stage import ProcessingStage, StageStatus, StageType
from app.config import settings
from app.core import obs
from app.core.logging import get_logger
from app.core.errors import CVATLabelMismatchError, StageStateError

logger = get_logger(__name__)

router = APIRouter()


def _get_cvat_browser_url() -> str:
    """Получить URL CVAT для браузера пользователя."""
    return getattr(settings, 'CVAT_BROWSER_URL', None) or settings.CVAT_URL


async def _start_cvat_stage(db: AsyncSession, uid: UUID) -> ProcessingStage:
    """RUNNING-строка `cvat_validation` для интерактивных CVAT-эндпоинтов.

    Воркерные `start_stage`/`fail_stage` синхронные (Session) — их НЕ
    переиспользуем; здесь AsyncSession (RUNBOOK §8.4). Коммитим сразу: строка
    видна в `/stages`, пока идёт долгая CVAT-операция (как worker `start_stage`).
    Под-шаг (`upload_media`/`task_shell`/`confirm`) различаем полем `failed_step`.
    """
    attempt = (
        await db.execute(
            select(func.count())
            .select_from(ProcessingStage)
            .where(
                ProcessingStage.diagram_uid == uid,
                ProcessingStage.stage_type == StageType.CVAT_VALIDATION,
            )
        )
    ).scalar_one() + 1
    stage = ProcessingStage(
        diagram_uid=uid,
        stage_type=StageType.CVAT_VALIDATION,
        status=StageStatus.PENDING,
        attempt=attempt,
    )
    stage.start()
    db.add(stage)
    await db.commit()
    return stage


def _fail_cvat_stage(stage: ProcessingStage, exc: BaseException, *, default_step: str) -> None:
    """Проставить FAILED + `error_code`/`failed_step`/traceback (БЕЗ commit — коммитит вызывающий).

    Зеркалит воркерный `fail_stage`: `error_code` = `exc.code` (или имя типа),
    `failed_step` = `exc.step` (проставлен `_cvat_op`/`obs.step`), иначе — `default_step`.
    """
    # .code бывает чужим (у SQLAlchemyError свой .code = None/"e3q8"): берём
    # только непустую строку, иначе — имя типа (аудит 2026-07-09, R6).
    code = getattr(exc, "code", None)
    stage.fail(
        str(exc)[:2000],
        traceback.format_exc()[:10000],
        error_code=code if isinstance(code, str) and code else type(exc).__name__,
        failed_step=getattr(exc, "step", None) or default_step,
    )


def parse_coco_annotations(coco_json: dict) -> Tuple[List[Dict], Dict[int, str]]:
    """
    Парсинг COCO JSON в список аннотаций.
    
    Args:
        coco_json: COCO формат JSON
        
    Returns:
        Tuple (annotations, category_map)
        - annotations: список dict с class_id, x_center, y_center, width, height
        - category_map: dict category_id → name
    """
    # Получаем размеры изображения
    images = coco_json.get("images", [])
    if not images:
        return [], {}
    
    image_info = images[0]
    img_width = image_info.get("width", 1)
    img_height = image_info.get("height", 1)
    
    # Маппинг категорий
    categories = coco_json.get("categories", [])
    category_map = {cat["id"]: cat["name"] for cat in categories}
    
    # Парсим аннотации
    annotations = []
    for ann in coco_json.get("annotations", []):
        bbox = ann.get("bbox", [0, 0, 0, 0])  # [x, y, width, height]
        category_id = ann.get("category_id", 0)
        
        # COCO bbox → YOLO normalized
        x, y, w, h = bbox
        x_center = (x + w / 2) / img_width
        y_center = (y + h / 2) / img_height
        width = w / img_width
        height = h / img_height
        
        annotations.append({
            "class_id": category_id - 1,  # COCO 1-based → 0-based
            "class_name": category_map.get(category_id, f"class_{category_id}"),
            "x_center": x_center,
            "y_center": y_center,
            "width": width,
            "height": height,
        })
    
    return annotations, category_map


def denormalize_coco_labels(coco_json: dict, project_config) -> None:
    """Привести категории из выгрузки CVAT к каноническому виду (правка на месте).

    CVAT нумерует `category_id` позицией метки в проекте (`1 + индекс`, см.
    datumaro coco exporter), а не её id. Как только метки создаются в алфавитном
    порядке отображаемых названий, `category_id - 1` перестаёт быть `class_id`.
    Эта прослойка сопоставляет категории ПО ИМЕНИ и восстанавливает канонические
    id из `classes:` — дальше по конвейеру ничего менять не пришлось.

    Порядок разбора имени:
      а) уже каноническое английское имя — путь задач из старого CVAT-проекта;
      б) отображаемое название из `display_labels` — путь новых задач;
      в) иначе метка не опознана → `CVATLabelMismatchError`.

    Вариант «оставить как есть» для (в) отвергнут сознательно: имя ушло бы в
    артефакт, `class_id` посчитался бы из позиции, и объекты молча уехали бы в
    чужой класс (замер: до 135 из 209 на одной схеме), а фильтры по именам
    `truba`/`annotation`/`napravlenie` в сегментации и скелете перестали бы
    срабатывать. Лучше остановиться и показать оператору, какую метку чинить.
    """
    from app.services import class_display

    canonical = {cls.name: cls.id for cls in project_config.classes}
    by_display = class_display.to_internal(project_config)

    remap: Dict[int, int] = {}
    unknown: List[str] = []
    for category in coco_json.get("categories", []):
        name = category.get("name", "")
        if name in canonical:
            en_name = name
        else:
            en_name = by_display.get(class_display.sort_key(name))
        if en_name is None:
            unknown.append(name)
            continue
        remap[category.get("id")] = canonical[en_name]

    if unknown:
        affected = sum(
            1 for ann in coco_json.get("annotations", [])
            if ann.get("category_id") not in remap
        )
        logger.error(
            "cvat labels mismatch: unknown=%s affected_annotations=%s",
            unknown, affected,
            extra={"phase": "cvat_validation", "step": "confirm", "event": "error",
                   "code": CVATLabelMismatchError.code, "unknown_labels": unknown,
                   "affected_annotations": affected},
        )
        raise CVATLabelMismatchError(
            f"Метки CVAT не опознаны: {', '.join(repr(n) for n in unknown)}. "
            f"Затронуто аннотаций: {affected}. Аннотации не сохранены. "
            f"Верните меткам исходные названия в CVAT (или добавьте класс в "
            f"конфиг проекта) и повторите «Получить аннотации».",
            stage="cvat_validation",
            step="confirm",
        )

    coco_json["categories"] = [
        {"id": cls.id, "name": cls.name, "supercategory": ""}
        for cls in project_config.classes
    ]
    for ann in coco_json.get("annotations", []):
        ann["category_id"] = remap[ann["category_id"]]


def annotations_to_yolo_txt(annotations: List[Dict]) -> str:
    """Конвертация аннотаций в YOLO формат."""
    lines = []
    for ann in annotations:
        line = "{} {:.6f} {:.6f} {:.6f} {:.6f}".format(
            ann["class_id"],
            ann["x_center"],
            ann["y_center"],
            ann["width"],
            ann["height"],
        )
        lines.append(line)
    return "\n".join(lines)


def _fetch_cvat_annotations_sync(
    task_id: int,
    output_dir: Path,
    project_config,
) -> Tuple[Path, Path, int]:
    """
    Синхронная функция для получения аннотаций из CVAT.
    
    Returns:
        Tuple (coco_path, yolo_path, annotation_count)
    """
    from app.services.cvat_client import get_cvat_client
    
    cvat_client = get_cvat_client()
    
    # Авторизация через CVAT_TOKEN (настроено в settings)
    
    # Экспортируем аннотации в COCO формате
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = Path(temp_dir) / "annotations.zip"
        cvat_client.export_annotations(
            task_id=task_id,
            format_name="COCO 1.0",
            output_path=zip_path,
        )
        
        # Распаковываем ZIP
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(temp_dir)
        
        # Ищем JSON файл
        coco_json_path = None
        for root, dirs, files in os.walk(temp_dir):
            for f in files:
                if f.endswith('.json'):
                    coco_json_path = Path(root) / f
                    break
            if coco_json_path:
                break
        
        if not coco_json_path or not coco_json_path.exists():
            raise ValueError("COCO JSON not found in exported archive")
        
        # Парсим COCO
        with open(coco_json_path, 'r', encoding='utf-8') as f:
            coco_data = json.load(f)
        
        # Прослойка нормализации: приводим сегментации к правилам пайплайна
        # (RLE-маски от CVAT, например «эллипс», -> полигоны; нераспознанное -> bbox)
        # ДО записи на диск, чтобы источник истины был чистым и ниже ничего менять не пришлось.
        from app.services.coco_normalize import normalize_coco_segmentation
        normalize_coco_segmentation(coco_data)

        # Категории CVAT -> канонические имена и id проекта. Строго ДО разбора и
        # записи: ниже `class_id` считается как `category_id - 1`, а имена классов
        # уходят в артефакт, по которому потом фильтруют сегментация и скелет.
        denormalize_coco_labels(coco_data, project_config)

        annotations, _ = parse_coco_annotations(coco_data)
        annotation_count = len(annotations)
        
        # Создаём выходную директорию
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Сохраняем COCO JSON
        coco_output = output_dir / "coco_validated.json"
        with open(coco_output, 'w', encoding='utf-8') as f:
            json.dump(coco_data, f, ensure_ascii=False, indent=2)
        
        # Сохраняем YOLO txt
        yolo_output = output_dir / "yolo_validated.txt"
        yolo_txt = annotations_to_yolo_txt(annotations)
        yolo_output.write_text(yolo_txt)
        
        return coco_output, yolo_output, annotation_count


@router.post("/{uid}/open-validation")
async def open_cvat_validation(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Открыть валидацию в CVAT.
    Возвращает URL для открытия в браузере.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")
    
    if diagram.status != DiagramStatus.DETECTED:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot open CVAT: status is '{diagram.status.value}', expected 'detected'"
        )
    
    if not diagram.cvat_task_id or not diagram.cvat_job_id:
        raise HTTPException(
            status_code=400,
            detail="CVAT task not created yet"
        )
    
    # Обновляем статус
    diagram.status = DiagramStatus.VALIDATING_BBOX
    await db.commit()
    
    # Формируем URL для браузера
    browser_url = _get_cvat_browser_url()
    cvat_url = f"{browser_url}/tasks/{diagram.cvat_task_id}/jobs/{diagram.cvat_job_id}"
    
    return {
        "status": "validating_bbox",
        "cvat_url": cvat_url,
        "cvat_task_id": diagram.cvat_task_id,
        "cvat_job_id": diagram.cvat_job_id,
    }


@router.post("/{uid}/fetch-annotations")
async def fetch_cvat_annotations(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Получить аннотации из CVAT и сохранить как валидированные.
    
    Этапы:
    1. Экспорт аннотаций из CVAT (COCO формат)
    2. Сохранение coco_validated.json
    3. Конвертация в yolo_validated.txt
    4. Создание артефактов в БД
    5. Обновление статуса → validated_bbox
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")
    
    if diagram.status != DiagramStatus.VALIDATING_BBOX:
        logger.warning(
            f"confirm rejected: wrong state code={StageStateError.code} from_status={diagram.status.value}",
            extra={"uid": str(uid), "phase": "cvat_validation", "step": "confirm",
                   "event": "error", "code": StageStateError.code,
                   "from_status": diagram.status.value, "expected": "validating_bbox"},
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Нельзя подтвердить: диаграмма в статусе '{diagram.status.value}', "
                f"ожидается 'validating_bbox'. Если вернулись доразметить — "
                f"используйте «переоткрыть валидацию»."
            ),
        )
    
    if not diagram.cvat_task_id:
        raise HTTPException(
            status_code=400,
            detail="CVAT task not found"
        )
    
    from app.services.project_loader import get_project_loader

    project_config = get_project_loader().load(diagram.project_code)
    if not project_config:
        raise HTTPException(
            status_code=400,
            detail=f"Project config not found: {diagram.project_code}",
        )

    # Путь к директории detection
    storage_path = Path(settings.STORAGE_PATH)
    detection_dir = storage_path / str(diagram.uid) / "detection"
    
    stage = await _start_cvat_stage(db, uid)

    obs.bind(uid=str(uid), phase="cvat_validation")
    try:
        # Долгая операция ВНЕ транзакции (может занять минуты)
        # ⚠️ НЕ оборачивать в db.begin() — это заблокирует БД!
        with obs.step("confirm", logger, cvat_task_id=diagram.cvat_task_id):
            coco_path, yolo_path, annotation_count = await asyncio.to_thread(
                _fetch_cvat_annotations_sync,
                diagram.cvat_task_id,
                detection_dir,
                project_config,
            )
        
        # Быстрые DB writes после долгой операции (неявная транзакция)
        # Upsert артефакт COCO_VALIDATED (удаляем старый при retry)
        await db.execute(
            delete(Artifact).where(
                Artifact.diagram_uid == str(uid),
                Artifact.artifact_type == ArtifactType.COCO_VALIDATED,
            )
        )
        artifact_coco = Artifact(
            diagram_uid=str(uid),
            artifact_type=ArtifactType.COCO_VALIDATED,
            file_path=str(coco_path.relative_to(storage_path)),
            file_size=coco_path.stat().st_size,
        )
        db.add(artifact_coco)
        
        # Upsert артефакт YOLO_VALIDATED
        await db.execute(
            delete(Artifact).where(
                Artifact.diagram_uid == str(uid),
                Artifact.artifact_type == ArtifactType.YOLO_VALIDATED,
            )
        )
        artifact_yolo = Artifact(
            diagram_uid=str(uid),
            artifact_type=ArtifactType.YOLO_VALIDATED,
            file_path=str(yolo_path.relative_to(storage_path)),
            file_size=yolo_path.stat().st_size,
        )
        db.add(artifact_yolo)
        
        # Обновляем диаграмму
        diagram.status = DiagramStatus.VALIDATED_BBOX
        diagram.validated_detection_count = annotation_count
        stage.complete({"annotation_count": annotation_count})

        await db.commit()
        
        return {
            "status": "validated_bbox",
            "annotation_count": annotation_count,
            "coco_path": str(coco_path.relative_to(storage_path)),
            "yolo_path": str(yolo_path.relative_to(storage_path)),
        }
        
    except CVATLabelMismatchError as exc:
        # Разошлись метки CVAT и конфиг проекта. Диаграмму в ERROR НЕ уводим:
        # на диск ничего не записано, чинится в CVAT, после чего та же кнопка
        # «Получить аннотации» отработает из того же статуса validating_bbox.
        _fail_cvat_stage(stage, exc, default_step="confirm")
        await db.commit()

        raise HTTPException(status_code=400, detail=str(exc))

    except Exception as exc:
        _fail_cvat_stage(stage, exc, default_step="confirm")
        # Откатываем статус при ошибке
        diagram.status = DiagramStatus.ERROR
        diagram.error_message = str(exc)[:500]
        diagram.error_stage = "fetching_annotations"
        await db.commit()
        
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch annotations: {exc}"
        )


# Из этих статусов возвращаться на валидацию bbox нельзя (ещё до неё / уже там).
_NOT_REOPENABLE = {
    DiagramStatus.UPLOADED,
    DiagramStatus.FRAME_CLEANED,
    DiagramStatus.DETECTED,
    DiagramStatus.VALIDATING_BBOX,
}


@router.post("/{uid}/reopen-bbox-validation")
async def reopen_bbox_validation(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """Жёсткий возврат к валидации bbox (кнопка «Проверка элементов» с поздних этапов).

    Останавливает текущий этап (revoke бегущей Celery-задачи), сбрасывает артефакты
    после `detected`, ставит статус `validating_bbox` и переоткрывает ТУ ЖЕ CVAT-задачу —
    ручные правки в CVAT сохраняются (таск не пересоздаётся).
    """
    obs.bind(uid=str(uid), phase="cvat_validation")

    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if not diagram.cvat_task_id or not diagram.cvat_job_id:
        raise HTTPException(status_code=400, detail="CVAT task not created — nothing to reopen")

    from_status = diagram.status
    if from_status in _NOT_REOPENABLE:
        logger.warning(
            f"reopen rejected: wrong state code={StageStateError.code} from_status={from_status.value}",
            extra={"uid": str(uid), "phase": "cvat_validation", "step": "reopen_validation",
                   "event": "error", "code": StageStateError.code, "from_status": from_status.value},
        )
        raise HTTPException(
            status_code=409,
            detail=f"Нельзя переоткрыть валидацию из статуса '{from_status.value}'",
        )

    with obs.step("reopen_validation", logger,
                  from_status=from_status.value, cvat_task_id=diagram.cvat_task_id):
        # 1. Жёсткий стоп: revoke бегущих/ожидающих стадий этой диаграммы.
        from worker.celery_app import celery_app
        running = await db.execute(
            select(ProcessingStage).where(
                ProcessingStage.diagram_uid == uid,
                ProcessingStage.status.in_([StageStatus.RUNNING, StageStatus.PENDING]),
            )
        )
        revoked = 0
        for stage in running.scalars().all():
            if stage.celery_task_id:
                celery_app.control.revoke(stage.celery_task_id, terminate=True)
                revoked += 1
            stage.status = StageStatus.SKIPPED
            stage.completed_at = datetime.utcnow()
            stage.error_message = "stopped: reopened bbox validation"

        # 2. Сбросить артефакты после detected (seg/skeleton + старый coco_validated).
        from app.api.rollback import _artifacts_to_delete
        art_types = _artifacts_to_delete(DiagramStatus.DETECTED)
        deleted = 0
        if art_types:
            res = await db.execute(
                delete(Artifact).where(
                    Artifact.diagram_uid == uid,
                    Artifact.artifact_type.in_(art_types),
                )
            )
            deleted = res.rowcount

        # 3. Статус → validating_bbox, чистим ошибку.
        diagram.status = DiagramStatus.VALIDATING_BBOX
        diagram.error_message = None
        diagram.error_stage = None
        await db.commit()

    browser_url = _get_cvat_browser_url()
    cvat_url = f"{browser_url}/tasks/{diagram.cvat_task_id}/jobs/{diagram.cvat_job_id}"
    logger.info(
        "reopen_validation done",
        extra={"uid": str(uid), "phase": "cvat_validation", "step": "reopen_validation",
               "revoked_tasks": revoked, "deleted_artifacts": deleted,
               "from_status": from_status.value},
    )
    return {
        "status": "validating_bbox",
        "cvat_url": cvat_url,
        "cvat_task_id": diagram.cvat_task_id,
        "cvat_job_id": diagram.cvat_job_id,
        "revoked_tasks": revoked,
        "deleted_artifacts": deleted,
    }


@router.get("/{uid}/cvat-url")
async def get_cvat_url(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """Получить URL CVAT задачи."""
    
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")
    
    if not diagram.cvat_task_id:
        raise HTTPException(status_code=400, detail="CVAT task not created")
    
    browser_url = _get_cvat_browser_url()
    cvat_url = f"{browser_url}/tasks/{diagram.cvat_task_id}/jobs/{diagram.cvat_job_id}"
    
    return {"cvat_url": cvat_url}


@router.post("/{uid}/retry-fetch")
async def retry_fetch_annotations(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """Повторить получение аннотаций после ошибки."""
    
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")
    
    if diagram.status != DiagramStatus.ERROR:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry: status is '{diagram.status.value}', expected 'error'"
        )
    
    if diagram.error_stage != "fetching_annotations":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry fetch: error_stage is '{diagram.error_stage}'"
        )
    
    # Сбрасываем на validating_bbox для повторной попытки
    diagram.status = DiagramStatus.VALIDATING_BBOX
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()
    
    return {
        "status": "validating_bbox",
        "message": "Ready for retry. Call fetch-annotations again.",
    }


def _create_cvat_task_sync(diagram, project_config, image_path: Path, yolo_path: Path):
    """Синхронное создание CVAT task."""
    from app.services.cvat_client import get_cvat_client, create_labels_from_config
    from app.services.cvat_export import create_exporter_from_config, Detection

    cvat_client = get_cvat_client()

    # Labels из конфига — общий источник с воркером (worker/tasks/detection.py)
    labels = create_labels_from_config(project_config)

    # Получаем или создаём проект
    project_id = cvat_client.get_or_create_project(
        name=project_config.cvat_project_name,
        labels=labels,
    )

    # Создаём task
    task_name = f"Diagram #{diagram.number} - {diagram.original_filename}"
    cvat_task_id, cvat_job_id = cvat_client.create_task(
        project_id=project_id,
        name=task_name,
        image_path=image_path,
    )

    # Импортируем аннотации если есть
    if yolo_path.exists():
        yolo_content = yolo_path.read_text().strip()
        if yolo_content:
            detections = []
            for line in yolo_content.split("\n"):
                parts = line.strip().split()
                if len(parts) >= 5:
                    detections.append(Detection(
                        class_id=int(parts[0]),
                        x_center=float(parts[1]),
                        y_center=float(parts[2]),
                        width=float(parts[3]),
                        height=float(parts[4]),
                        confidence=float(parts[5]) if len(parts) > 5 else 1.0,
                    ))

            if detections:
                exporter = create_exporter_from_config(project_config)

                with tempfile.TemporaryDirectory() as temp_dir:
                    zip_path = Path(temp_dir) / "annotations.zip"
                    exporter.export_yolo(
                        detections=detections,
                        image_filename=image_path.name,
                        output_path=zip_path,
                    )

                    cvat_client.import_annotations(
                        task_id=cvat_task_id,
                        annotations_path=zip_path,
                        format_name="YOLO 1.1",
                    )

    return cvat_task_id, cvat_job_id


@router.post("/{uid}/create-task")
async def create_cvat_task_endpoint(
        uid: UUID,
        db: AsyncSession = Depends(get_async_db),
):
    """Создать CVAT task для диаграммы."""
    from app.services.project_loader import get_project_loader
    import asyncio

    # Тегируем логи запроса uid (в т.ч. cvat.error из create_task в рабочем потоке).
    obs.bind(uid=str(uid), phase="cvat_validation")

    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if diagram.cvat_task_id:
        # Уже есть
        browser_url = _get_cvat_browser_url()
        return {
            "status": "exists",
            "cvat_task_id": diagram.cvat_task_id,
            "cvat_job_id": diagram.cvat_job_id,
            "cvat_url": f"{browser_url}/tasks/{diagram.cvat_task_id}/jobs/{diagram.cvat_job_id}",
        }

    # Загружаем конфиг проекта
    project_loader = get_project_loader()
    project_config = project_loader.load(diagram.project_code)
    if not project_config:
        raise HTTPException(status_code=400, detail=f"Project config not found: {diagram.project_code}")

    storage_path = Path(settings.STORAGE_PATH)

    # Путь к изображению
    image_path = storage_path / str(diagram.uid) / "original" / "image.png"
    if not image_path.exists():
        for ext in [".jpg", ".jpeg", ".tiff", ".tif"]:
            alt_path = image_path.with_suffix(ext)
            if alt_path.exists():
                image_path = alt_path
                break
        else:
            raise HTTPException(status_code=400, detail="Original image not found")

    # Путь к YOLO predictions
    yolo_path = storage_path / str(diagram.uid) / "detection" / "yolo_predicted.txt"

    stage = await _start_cvat_stage(db, uid)

    try:
        cvat_task_id, cvat_job_id = await asyncio.to_thread(
            _create_cvat_task_sync,
            diagram,
            project_config,
            image_path,
            yolo_path,
        )

        diagram.cvat_task_id = cvat_task_id
        diagram.cvat_job_id = cvat_job_id
        stage.complete()

        await db.commit()

        browser_url = _get_cvat_browser_url()

        return {
            "status": "created",
            "cvat_task_id": cvat_task_id,
            "cvat_job_id": cvat_job_id,
            "cvat_url": f"{browser_url}/tasks/{cvat_task_id}/jobs/{cvat_job_id}",
        }

    except Exception as exc:
        _fail_cvat_stage(stage, exc, default_step="create_task")
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to create CVAT task: {exc}")
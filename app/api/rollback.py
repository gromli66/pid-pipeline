"""
Rollback API — откат диаграммы на предыдущий этап.

POST /api/diagrams/{uid}/rollback?target_status=detected
  → Удаляет артефакты всех этапов ПОСЛЕ target_status
  → Устанавливает статус = target_status
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

import logging

from app.db.session import get_async_db
from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
from app.services.layout_dispatch import dispatch_layout

logger = logging.getLogger(__name__)

router = APIRouter()

# Порядок этапов пайплайна
_STAGE_ORDER = [
    DiagramStatus.UPLOADED,
    DiagramStatus.FRAME_CLEANED,        # frame/stamp removal (UI)
    DiagramStatus.DETECTED,
    DiagramStatus.VALIDATED_BBOX,
    DiagramStatus.SKELETONIZED,           # segmentation + skeleton #1
    DiagramStatus.VALIDATED_MASKS,
    DiagramStatus.SKELETONIZED_FINAL,     # skeleton #2 (from validated mask)
    DiagramStatus.DETECTED_JUNCTIONS,     # junction/bridge segmentation
    DiagramStatus.VALIDATED_JUNCTIONS,    # junction/bridge validation (UI)
    DiagramStatus.BUILT,
    DiagramStatus.VALIDATED_GRAPH,
    DiagramStatus.CONTOURS_EXTRACTED,     # SAM2 (parallel, but UX after graph)
    DiagramStatus.CONTOURS_VALIDATED,     # contour review in editor
    DiagramStatus.OCR_COMPLETED,
    DiagramStatus.OCR_BOUND,
    DiagramStatus.COMPLETED,
]

# Какие артефакты принадлежат каждому этапу (создаются НА этом этапе)
_STAGE_ARTIFACTS = {
    DiagramStatus.FRAME_CLEANED: [
        ArtifactType.ORIGINAL_CLEANED,
    ],
    DiagramStatus.DETECTED: [
        ArtifactType.YOLO_PREDICTED,
        ArtifactType.COCO_PREDICTED,
        ArtifactType.DETECTION_OVERLAY,
    ],
    DiagramStatus.VALIDATED_BBOX: [
        ArtifactType.YOLO_VALIDATED,
        ArtifactType.COCO_VALIDATED,
    ],
    DiagramStatus.SKELETONIZED: [
        # Segmentation + Skeleton #1 (combined phase)
        ArtifactType.NODE_MASK,
        ArtifactType.PIPE_MASK,
        ArtifactType.SEGMENTATION_OVERLAY,
        ArtifactType.SKELETON,
        ArtifactType.SKELETON_MASK,
    ],
    DiagramStatus.VALIDATED_MASKS: [
        ArtifactType.PIPE_MASK_VALIDATED,
    ],
    DiagramStatus.SKELETONIZED_FINAL: [
        ArtifactType.SKELETON_FINAL,
        ArtifactType.PIPE_MASK_REFINED,
    ],
    DiagramStatus.DETECTED_JUNCTIONS: [
        ArtifactType.JUNCTION_MASK,
        ArtifactType.BRIDGE_MASK,
    ],
    DiagramStatus.VALIDATED_JUNCTIONS: [
        ArtifactType.JUNCTION_MASK_VALIDATED,
        ArtifactType.BRIDGE_MASK_VALIDATED,
    ],
    DiagramStatus.BUILT: [
        ArtifactType.GRAPH_JSON,
        ArtifactType.GRAPH_OVERLAY,
    ],
    DiagramStatus.VALIDATED_GRAPH: [
        ArtifactType.GRAPH_VALIDATED,
    ],
    DiagramStatus.CONTOURS_EXTRACTED: [
        ArtifactType.CONTOURS_AUTO,
    ],
    DiagramStatus.CONTOURS_VALIDATED: [
        ArtifactType.CONTOURS_VALIDATED,
    ],
    DiagramStatus.OCR_COMPLETED: [
        ArtifactType.OCR_CLEANED,
        ArtifactType.OCR_RESULT,
        ArtifactType.OCR_BINDING,
    ],
    DiagramStatus.OCR_BOUND: [
        ArtifactType.OCR_VALIDATION,
    ],
    DiagramStatus.COMPLETED: [
        ArtifactType.FXML,
        # «Ручная правка» своей стадии в _STAGE_ORDER не имеет (её done_status —
        # GENERATING_FXML), а живёт между OCR_BOUND и COMPLETED. Держим её артефакт
        # здесь: откат на любую более раннюю стадию сносит холст, и он пересобирается
        # из свежего graph_validated. Обратно холст не конвертируется — pretransform
        # необратим (фикс-размеры затирают детекционные, declust двигает символы).
        ArtifactType.GRAPH_CANVAS,
        # ЛЕГАСИ (подсветка очагов вырезана 2026-08-02): артефакт больше не
        # производится, но на старых установках лежит рядом с холстом и
        # обязан сноситься вместе с ним.
        ArtifactType.RESIDUAL_DEFECTS,
    ],
}


def _stages_after(target: DiagramStatus) -> list:
    """Этапы ПОСЛЕ target (не включая target)."""
    try:
        idx = _STAGE_ORDER.index(target)
    except ValueError:
        return []
    return _STAGE_ORDER[idx + 1:]


def _artifacts_to_delete(
    target: DiagramStatus,
    preserve_ocr: bool = False,
    preserve_contours: bool = False,
) -> list:
    """Типы артефактов, которые нужно удалить при откате до target.

    Args:
        target: target status to rollback to
        preserve_ocr: if True, keep OCR artifacts even if in stages after target.
            Use when rolling back graph without losing independent OCR results.
        preserve_contours: if True, keep contour artifacts (CONTOURS_AUTO,
            CONTOURS_VALIDATED). SAM2 depends on image + COCO + pipe_mask,
            not on graph — contours survive graph rollback.
    """
    _OCR_ARTIFACTS = {
        ArtifactType.OCR_CLEANED,
        ArtifactType.OCR_RESULT,
        ArtifactType.OCR_BINDING,
        ArtifactType.OCR_VALIDATION,
    }
    _CONTOUR_ARTIFACTS = {
        ArtifactType.CONTOURS_AUTO,
        ArtifactType.CONTOURS_VALIDATED,
    }
    types = []
    for stage in _stages_after(target):
        types.extend(_STAGE_ARTIFACTS.get(stage, []))
    if preserve_ocr:
        types = [t for t in types if t not in _OCR_ARTIFACTS]
    if preserve_contours:
        types = [t for t in types if t not in _CONTOUR_ARTIFACTS]
    return types


@router.post("/{uid}/rollback")
async def rollback_diagram(
    uid: UUID,
    target_status: str = Query(..., description="Target status to rollback to"),
    preserve_ocr: bool = Query(False, description="Keep OCR artifacts when rolling back graph"),
    preserve_contours: bool = Query(False, description="Keep contour artifacts when rolling back graph"),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Откатить диаграмму до указанного этапа.

    Удаляет артефакты всех последующих этапов и устанавливает статус.
    """
    # Валидация target_status
    try:
        target = DiagramStatus(target_status)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid target_status: '{target_status}'. "
                   f"Valid: {[s.value for s in _STAGE_ORDER]}",
        )

    if target not in _STAGE_ORDER:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot rollback to '{target_status}' — not a stable stage",
        )

    # Найти диаграмму
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    # Нельзя откатить вперёд
    try:
        current_idx = _STAGE_ORDER.index(diagram.status)
        target_idx = _STAGE_ORDER.index(target)
    except ValueError:
        current_idx, target_idx = -1, 0

    if target_idx >= current_idx and current_idx >= 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot rollback: current '{diagram.status.value}' "
                   f"is not ahead of target '{target_status}'",
        )

    # Собрать типы артефактов для удаления
    art_types = _artifacts_to_delete(target, preserve_ocr=preserve_ocr, preserve_contours=preserve_contours)

    # Удалить артефакты из БД
    deleted_count = 0
    if art_types:
        result = await db.execute(
            delete(Artifact).where(
                Artifact.diagram_uid == uid,
                Artifact.artifact_type.in_(art_types),
            )
        )
        deleted_count = result.rowcount

    # Файл холста сносится ВМЕСТЕ с записью в БД. Раньше удалялась только
    # строка Artifact, а `graph_canvas.json` оставался лежать — и вместе с ним
    # флаг `operator_saved`. Задача раскладки читает флаг из ФАЙЛА
    # (`worker/tasks/layout.py`) и при нём выбрасывает свой результат: откат
    # звал пересчёт с force=True, тот честно считал и молча ничего не писал.
    # Холст производный, восстанавливается из graph_validated — держать его
    # поверх отката нечем.
    if ArtifactType.GRAPH_CANVAS in art_types:
        from app.services.storage import StorageService
        graph_dir = StorageService().base_path / str(uid) / "graph"
        # residual_defects.json — ЛЕГАСИ (подсветка очагов вырезана
        # 2026-08-02): больше не производится, но на старых установках лежит
        # рядом с холстом и сносится вместе с ним.
        for fname in ("graph_canvas.json", "residual_defects.json"):
            f = graph_dir / fname
            try:
                f.unlink()
                logger.info("rollback %s: снят %s", uid, f.name)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("rollback %s: %s не удалён (%s)", uid, f.name, exc)

    # Откат за этап рамки: вернуть сырое изображение в канонический image.png из
    # бэкапа image_raw.png (при очистке мы перезаписали image.png очищенным).
    if ArtifactType.ORIGINAL_CLEANED in art_types:
        import shutil
        from app.services.storage import StorageService
        orig = StorageService().base_path / str(uid) / "original"
        raw = orig / "image_raw.png"
        canon = orig / "image.png"
        if raw.exists():
            shutil.copy2(str(raw), str(canon))
            raw.unlink()

    # Установить статус
    diagram.status = target
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()

    # Откат СНОСИТ артефакт холста (GRAPH_CANVAS числится за COMPLETED), но
    # проходит мимо точек запуска раскладки: вернуться можно на бусину
    # привязки, а она ПОСЛЕ контуров, и `complete_contour_validation` второй
    # раз не позовётся. Без этого оператор шёл вперёд и получал в «Ручной
    # правке» pretransform-холст БЕЗ раскладки — молча.
    #
    # force=True: на возврате холст пересчитывается ВСЕГДА, даже если истина не
    # менялась (§3.2). Переиспользовать прежний нельзя не из-за экономии — он
    # несёт правки оператора, сделанные до возврата, а они не сохраняются.
    # Индекс берём от САМОГО target, а не от `target_idx`: тот считается в
    # общем try с `current_idx` и обнуляется, когда текущий статус не из
    # _STAGE_ORDER. А это как раз частый случай возврата — после «Ручной
    # правки» статус GENERATING_FXML, и его в списке нет. `target` же
    # гарантированно в списке: проверено выше.
    layout = None
    if _STAGE_ORDER.index(target) >= _STAGE_ORDER.index(
            DiagramStatus.CONTOURS_VALIDATED):
        layout = await dispatch_layout(uid, db, force=True)
        logger.info("rollback %s -> %s: раскладка %s", uid, target.value,
                    (layout or {}).get("status"))

    return {
        "status": target.value,
        "deleted_artifacts": deleted_count,
        "uid": str(uid),
        "layout": layout,
    }

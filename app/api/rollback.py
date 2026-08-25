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
    ],
}

# ── артефакты «Ручной правки» ────────────────────────────────────────────
#
# Своей стадии в `_STAGE_ORDER` у холста нет (его `done_status` —
# GENERATING_FXML), и раньше он числился за COMPLETED вместе с FXML — то есть
# погибал при ЛЮБОЙ цели отката. Это и есть жалоба фронта 2: оператор вернулся
# на бусину «Привязка подписей», чтобы поправить одну подпись, и потерял часы
# ручной раскладки.
#
# Граница названа явно: возврат НА привязку холст переживает, всё, что глубже,
# — сносит. Основание не вкусовое: при целях раньше `OCR_BOUND` меняется сам
# `graph_validated`, из которого холст производен, а обратно он не
# конвертируется (pretransform необратим — фикс-размеры затирают детекционные,
# declust двигает символы).
_CANVAS_ARTIFACTS = [
    ArtifactType.GRAPH_CANVAS,
    # ЛЕГАСИ (подсветка очагов вырезана 2026-08-02): артефакт больше не
    # производится, но на старых установках лежит рядом с холстом и
    # обязан сноситься вместе с ним.
    ArtifactType.RESIDUAL_DEFECTS,
]

#: Файлы холста на диске. Строка БД и файл сносятся ВМЕСТЕ — см. `purge_artifacts`.
_CANVAS_FILES = ("graph_canvas.json", "residual_defects.json")

#: Цель отката, начиная с которой холст переживает возврат.
_CANVAS_SURVIVES_FROM = DiagramStatus.OCR_BOUND


def canvas_dies(target: DiagramStatus) -> bool:
    """Гибнет ли холст при откате до `target` — цель СТРОГО РАНЬШЕ привязки.

    Один предикат на два решения сразу: что удалять и звать ли раскладку
    с `force`. Разъедься они — Н1 аннулировал бы сам себя (см. `rollback_diagram`).
    """
    if target not in _STAGE_ORDER:
        return False
    return _STAGE_ORDER.index(target) < _STAGE_ORDER.index(_CANVAS_SURVIVES_FROM)


# Порядок конвейера для статусов, которых нет в `_STAGE_ORDER`. Промежуточные
# `*ING` — штатные состояния (в `generating_fxml` диаграмма живёт минутами), и
# без их позиции гейт цели пропускал ЛЮБУЮ цель, включая движение ВПЕРЁД.
# Порядок объявления `DiagramStatus` — тот же конвейерный, что и `_STAGE_ORDER`
# (сторож — `test_stage_order_is_a_subsequence_of_the_enum`).
# `ERROR` объявлен последним и позиции в конвейере НЕ означает: из него откат
# разрешён куда угодно, это штатный выход из тупика (`docs/STATUS_MACHINE.md §5`).
_PIPELINE_RANK = {
    status: idx for idx, status in enumerate(DiagramStatus)
    if status is not DiagramStatus.ERROR
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
    # ⛔ `OCR_BINDING` в этом множестве НЕТ, и это блок 5 (решение Максима
    # 2026-08-25). Флаг `preserve_ocr` носят только кнопки, чья цель отката
    # РАНЬШЕ сборки, — а между сборками не выживают `id` узлов: они порядковые
    # `node_N` от порядка компонент маски (`modules/graph/core/nodes.py`).
    # Привязка держит `binding_map[node_id]`, то есть после пересборки она
    # вешает KKS не на тот элемент. «Сохранить» её означало сохранить ложь.
    #
    # Что при этом ЖИВЁТ и почему:
    #   • OCR_RESULT / OCR_CLEANED — сырые пиксельные блоки, к графу не
    #     привязаны вовсе: распознавание считается параллельно сборке;
    #   • OCR_VALIDATION — правки текстов с ключами `block_N` от ТЕХ ЖЕ сырых
    #     блоков (решение №4 редтима): труд оператора переживает пересборку
    #     легитимно, потому что зависит от блоков, а не от узлов.
    _OCR_ARTIFACTS = {
        ArtifactType.OCR_CLEANED,
        ArtifactType.OCR_RESULT,
        ArtifactType.OCR_VALIDATION,
    }
    _CONTOUR_ARTIFACTS = {
        ArtifactType.CONTOURS_AUTO,
        ArtifactType.CONTOURS_VALIDATED,
    }
    types = []
    for stage in _stages_after(target):
        types.extend(_STAGE_ARTIFACTS.get(stage, []))
    if canvas_dies(target):
        types.extend(_CANVAS_ARTIFACTS)
    if preserve_ocr:
        types = [t for t in types if t not in _OCR_ARTIFACTS]
    if preserve_contours:
        types = [t for t in types if t not in _CONTOUR_ARTIFACTS]
    return types


def _unlink_canvas_files(uid: UUID) -> None:
    """Снести файлы холста с диска.

    Раньше удалялась только строка `Artifact`, а `graph_canvas.json` оставался
    лежать — и вместе с ним флаг `operator_saved`. Задача раскладки читает флаг
    из ФАЙЛА (`worker/tasks/layout.py`) и при нём выбрасывает свой результат:
    откат звал пересчёт с force=True, тот честно считал и молча ничего не писал.
    """
    from app.services.storage import StorageService
    graph_dir = StorageService().base_path / str(uid) / "graph"
    for fname in _CANVAS_FILES:
        f = graph_dir / fname
        try:
            f.unlink()
            logger.info("rollback %s: снят %s", uid, f.name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("rollback %s: %s не удалён (%s)", uid, f.name, exc)


async def purge_artifacts(uid: UUID, art_types: list, db) -> int:
    """Снять артефакты: строки БД и файлы холста ВМЕСТЕ. Вернуть число строк.

    Общая точка для отката по бусине и для переоткрытия валидации CVAT
    (`app/api/cvat.py`), который раньше сносил только строки. Осиротевший
    `graph_canvas.json` опаснее удалённого: раскладка читает холст С ДИСКА и
    МИМО строки (`app/services/layout_dispatch.py`), видит в нём чужой
    `operator_saved` и выбрасывает свой результат. Клиент при этом артефакт не
    покажет вовсе — он ходит только по БД (`app/api/diagrams.py`).
    """
    if not art_types:
        return 0
    result = await db.execute(
        delete(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type.in_(art_types),
        )
    )
    if any(t in art_types for t in _CANVAS_ARTIFACTS):
        _unlink_canvas_files(uid)
    return result.rowcount


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

    # Нельзя откатить вперёд.
    #
    # ⛔ Считать по `_STAGE_ORDER` нельзя: промежуточных `*ING` там нет, и у
    # штатного `generating_fxml` индекс получался -1 — проверка не срабатывала
    # вовсе, проходила ЛЮБАЯ цель, включая движение ВПЕРЁД («откат» из
    # `generating_fxml` в `completed` сносил артефакты и ставил статус готовой
    # схемы). Позицию даёт порядок объявления `DiagramStatus`, для стабильных
    # этапов он тот же, что в `_STAGE_ORDER`.
    current_rank = _PIPELINE_RANK.get(diagram.status)
    if current_rank is not None and _PIPELINE_RANK[target] >= current_rank:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot rollback: current '{diagram.status.value}' "
                   f"is not ahead of target '{target_status}'",
        )

    # Собрать типы артефактов для удаления и снести их вместе с файлами.
    art_types = _artifacts_to_delete(target, preserve_ocr=preserve_ocr, preserve_contours=preserve_contours)
    deleted_count = await purge_artifacts(uid, art_types, db)

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

    # Откат проходит мимо точек запуска раскладки: вернуться можно на бусину
    # привязки, а она ПОСЛЕ контуров, и `complete_contour_validation` второй
    # раз не позовётся. Без этого оператор шёл вперёд и получал в «Ручной
    # правке» pretransform-холст БЕЗ раскладки — молча.
    #
    # ⚠ `force` — ровно тот же предикат, что и гибель холста, и это не
    # совпадение: force снимает `operator_saved`
    # (`app/services/layout_dispatch.py`), после чего воркер через 1-2 минуты
    # перезаписывает сохранённый оператором холст (`worker/tasks/layout.py`).
    # Позови мы force при цели «привязка» — Н1 аннулировался бы собственным
    # пунктом: строку и файл сберегли, а правки всё равно затёрли. Когда холст
    # погиб, force осмыслен: пересчитывать надо всегда, даже если истина не
    # менялась (§3.2). Когда холст жив, диспетчер без force ответит
    # ALREADY_FRESH на свежем, а устаревший пересчитается штатной stale-проверкой.
    # Индекс берём от САМОГО target: он гарантированно в списке (проверено выше).
    layout = None
    if _STAGE_ORDER.index(target) >= _STAGE_ORDER.index(
            DiagramStatus.CONTOURS_VALIDATED):
        layout = await dispatch_layout(uid, db, force=canvas_dies(target))
        logger.info("rollback %s -> %s: раскладка %s", uid, target.value,
                    (layout or {}).get("status"))

    return {
        "status": target.value,
        "deleted_artifacts": deleted_count,
        "uid": str(uid),
        "layout": layout,
    }

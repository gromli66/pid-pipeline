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

from app.db.session import get_async_db
from app.models import Diagram, DiagramStatus, Artifact, ArtifactType

router = APIRouter()

# Порядок этапов пайплайна
_STAGE_ORDER = [
    DiagramStatus.UPLOADED,
    DiagramStatus.DETECTED,
    DiagramStatus.VALIDATED_BBOX,
    DiagramStatus.SKELETONIZED,           # segmentation + skeleton #1
    DiagramStatus.VALIDATED_MASKS,
    DiagramStatus.SKELETONIZED_FINAL,     # skeleton #2 (from validated mask)
    DiagramStatus.DETECTED_JUNCTIONS,     # junction/bridge segmentation
    DiagramStatus.VALIDATED_JUNCTIONS,    # junction/bridge validation (UI)
    DiagramStatus.BUILT,
    DiagramStatus.VALIDATED_GRAPH,
    DiagramStatus.OCR_COMPLETED,
    DiagramStatus.OCR_BOUND,
    DiagramStatus.COMPLETED,
]

# Какие артефакты принадлежат каждому этапу (создаются НА этом этапе)
_STAGE_ARTIFACTS = {
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
    DiagramStatus.OCR_COMPLETED: [
        ArtifactType.OCR_CLEANED,
        ArtifactType.OCR_RESULT,
        ArtifactType.OCR_BINDING,
    ],
    DiagramStatus.COMPLETED: [
        ArtifactType.FXML,
    ],
}


def _stages_after(target: DiagramStatus) -> list:
    """Этапы ПОСЛЕ target (не включая target)."""
    try:
        idx = _STAGE_ORDER.index(target)
    except ValueError:
        return []
    return _STAGE_ORDER[idx + 1:]


def _artifacts_to_delete(target: DiagramStatus) -> list:
    """Типы артефактов, которые нужно удалить при откате до target."""
    types = []
    for stage in _stages_after(target):
        types.extend(_STAGE_ARTIFACTS.get(stage, []))
    return types


@router.post("/{uid}/rollback")
async def rollback_diagram(
    uid: UUID,
    target_status: str = Query(..., description="Target status to rollback to"),
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
    art_types = _artifacts_to_delete(target)

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

    # Установить статус
    diagram.status = target
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()

    return {
        "status": target.value,
        "deleted_artifacts": deleted_count,
        "uid": str(uid),
    }

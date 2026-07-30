"""
Artifact Model - файлы, создаваемые в процессе обработки.
"""

import enum
from datetime import datetime
from typing import Optional, TYPE_CHECKING
import uuid

from sqlalchemy import String, Integer, DateTime, Enum, ForeignKey, BigInteger
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, Mapped, mapped_column

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.diagram import Diagram


class ArtifactType(str, enum.Enum):
    """Типы артефактов (файлов)."""

    # Original
    ORIGINAL_IMAGE = "original_image"
    ORIGINAL_CLEANED = "original_cleaned"

    # Detection (YOLO)
    YOLO_PREDICTED = "yolo_predicted"
    YOLO_VALIDATED = "yolo_validated"
    COCO_PREDICTED = "coco_predicted"
    COCO_VALIDATED = "coco_validated"

    # Segmentation (U2-Net++)
    NODE_MASK = "node_mask"
    PIPE_MASK = "pipe_mask"
    PIPE_MASK_VALIDATED = "pipe_mask_validated"

    # Refined pipe mask (after adaptive dilate correction)
    PIPE_MASK_REFINED = "pipe_mask_refined"

    # Skeleton
    SKELETON = "skeleton"
    SKELETON_MASK = "skeleton_mask"      # маска, построенная из скелета (для валидации)
    SKELETON_FINAL = "skeleton_final"

    # Junction (CNN)
    JUNCTION_MASK = "junction_mask"
    BRIDGE_MASK = "bridge_mask"
    JUNCTION_MASK_VALIDATED = "junction_mask_validated"
    BRIDGE_MASK_VALIDATED = "bridge_mask_validated"
    # Центры перекрёстков/мостов. JUNCTION_POINTS — модельный points.json
    # воркера; JUNCTION_POINTS_VALIDATED — правленые оператором центры.
    # Без них операция «изменить размер» ломает свой фундамент: экстрактор с
    # дефолтным окном 15 не найдёт ни одного окна в ужатых квадратах.
    JUNCTION_POINTS = "junction_points"
    JUNCTION_POINTS_VALIDATED = "junction_points_validated"

    # Graph
    GRAPH_JSON = "graph_json"
    GRAPH_VALIDATED = "graph_validated"
    # WYSIWYG: производная от GRAPH_VALIDATED в холсте 1920x1080.
    # Правится только в «Ручной правке», назад в GRAPH_VALIDATED не пишется.
    GRAPH_CANVAS = "graph_canvas"
    # Э12: остаточные очаги после авто-раскладки (невидимые трубы, боксы на
    # чужих трубах, нелегальные наложения) — подсветка в «Ручной правке».
    # Производен от GRAPH_CANVAS, живёт и сносится вместе с ним.
    RESIDUAL_DEFECTS = "residual_defects"

    # Contours (SAM2)
    CONTOURS_AUTO = "contours_auto"
    CONTOURS_VALIDATED = "contours_validated"

    # OCR
    OCR_CLEANED = "ocr_cleaned"
    OCR_RESULT = "ocr_result"
    OCR_BINDING = "ocr_binding"
    OCR_VALIDATION = "ocr_validation"

    # Output
    FXML = "fxml"

    # Debug/Visualization
    DETECTION_OVERLAY = "detection_overlay"
    SEGMENTATION_OVERLAY = "segmentation_overlay"
    GRAPH_OVERLAY = "graph_overlay"


class Artifact(Base):
    """Модель артефакта (файла)."""

    __tablename__ = "artifacts"

    # Primary Key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Foreign Key
    diagram_uid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("diagrams.uid", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # Artifact Info
    artifact_type: Mapped[ArtifactType] = mapped_column(
        Enum(ArtifactType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        index=True
    )

    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)


    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    # Relationships
    diagram: Mapped["Diagram"] = relationship("Diagram", back_populates="artifacts")

    @classmethod
    def training_types(cls) -> set:
        """Типы артефактов для дообучения."""
        return {
            ArtifactType.YOLO_VALIDATED,
            ArtifactType.COCO_VALIDATED,
            ArtifactType.PIPE_MASK_VALIDATED,
            ArtifactType.JUNCTION_MASK_VALIDATED,
            ArtifactType.BRIDGE_MASK_VALIDATED,
            ArtifactType.GRAPH_VALIDATED,
        }

    def __repr__(self) -> str:
        return f"<Artifact {self.artifact_type.value} for {self.diagram_uid}>"

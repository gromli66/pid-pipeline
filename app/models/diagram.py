"""
Diagram Model - основная сущность P&ID диаграммы.
"""

import uuid
import enum
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING

from sqlalchemy import (
    Column, String, Integer, DateTime, Enum, Text, ForeignKey, Boolean, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, Mapped, mapped_column

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.artifact import Artifact
    from app.models.stage import ProcessingStage
    from app.models.project import Project


class DiagramStatus(str, enum.Enum):
    """Статусы обработки диаграммы."""

    # Upload
    UPLOADED = "uploaded"

    # Phase 1: Detection (YOLO)
    DETECTING = "detecting"
    DETECTED = "detected"
    VALIDATING_BBOX = "validating_bbox"
    VALIDATED_BBOX = "validated_bbox"

    # Phase 2: Segmentation + initial skeleton
    SEGMENTING = "segmenting"
    SKELETONIZING = "skeletonizing"
    SKELETONIZED = "skeletonized"

    # Phase 3: Mask validation (UI)
    VALIDATING_MASKS = "validating_masks"
    VALIDATED_MASKS = "validated_masks"

    # Phase 4: Final skeletonization
    SKELETONIZING_FINAL = "skeletonizing_final"
    SKELETONIZED_FINAL = "skeletonized_final"

    # Phase 5: Junction/Bridge segmentation (CenterNet)
    DETECTING_JUNCTIONS = "detecting_junctions"
    DETECTED_JUNCTIONS = "detected_junctions"

    # Phase 6: Junction/Bridge validation (UI)
    VALIDATING_JUNCTIONS = "validating_junctions"
    VALIDATED_JUNCTIONS = "validated_junctions"

    # Phase 7: Graph
    BUILDING_GRAPH = "building_graph"
    BUILT = "built"
    VALIDATING_GRAPH = "validating_graph"
    VALIDATED_GRAPH = "validated_graph"

    # Phase 7b: Contour extraction (SAM2) — parallel with graph/OCR
    EXTRACTING_CONTOURS = "extracting_contours"
    CONTOURS_EXTRACTED = "contours_extracted"
    CONTOURS_VALIDATED = "contours_validated"

    # Phase 8: OCR
    OCR_PROCESSING = "ocr_processing"
    OCR_COMPLETED = "ocr_completed"
    OCR_BOUND = "ocr_bound"

    # Phase 9: FXML
    GENERATING_FXML = "generating_fxml"
    COMPLETED = "completed"

    # Error
    ERROR = "error"


class Diagram(Base):
    """Модель P&ID диаграммы."""

    __tablename__ = "diagrams"
    __table_args__ = (
        UniqueConstraint('project_code', 'number', name='uq_diagram_project_number'),
    )

    # Primary Key
    uid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Project Reference
    project_code: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("projects.code"),
        nullable=False,
        index=True,
    )

    # Basic Info
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)

    # Status
    status: Mapped[DiagramStatus] = mapped_column(
        Enum(DiagramStatus, values_callable=lambda x: [e.value for e in x]),
        default=DiagramStatus.UPLOADED,
        nullable=False,
        index=True,
    )

    # Error Info
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_stage: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # CVAT Integration
    cvat_task_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cvat_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Image Metadata
    image_width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    image_height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Processing Statistics
    detection_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    detection_model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    validated_detection_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    segmentation_pixels: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    junction_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bridge_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    node_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    edge_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Soft Delete
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default='false')

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    project: Mapped["Project"] = relationship("Project", back_populates="diagrams")
    artifacts: Mapped[List["Artifact"]] = relationship("Artifact", back_populates="diagram", cascade="all, delete-orphan")
    stages: Mapped[List["ProcessingStage"]] = relationship("ProcessingStage", back_populates="diagram", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Diagram #{self.number} [{self.project_code}] ({self.status.value})>"

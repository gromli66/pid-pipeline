"""
ProcessingStage Model - история этапов обработки.
"""

import enum
from datetime import datetime
from typing import Optional, TYPE_CHECKING
import uuid

from sqlalchemy import String, Integer, DateTime, Enum, ForeignKey, Text, Float
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, Mapped, mapped_column

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.diagram import Diagram


class StageType(str, enum.Enum):
    """Типы этапов обработки."""

    UPLOAD = "upload"
    FRAME_REMOVAL = "frame_removal"
    DETECTION = "detection"
    CVAT_VALIDATION = "cvat_validation"
    DIRECTION_CLASSIFICATION = "direction_classification"
    SEGMENTATION = "segmentation"
    SKELETONIZATION = "skeletonization"
    JUNCTION_CLASSIFICATION = "junction_classification"
    MASK_VALIDATION = "mask_validation"
    FINAL_SKELETONIZATION = "final_skeletonization"
    GRAPH_BUILDING = "graph_building"
    GRAPH_VALIDATION = "graph_validation"
    CONTOUR_EXTRACTION = "contour_extraction"
    OCR = "ocr"
    FXML_GENERATION = "fxml_generation"


class StageStatus(str, enum.Enum):
    """Статусы этапа."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProcessingStage(Base):
    """Модель этапа обработки."""

    __tablename__ = "processing_stages"

    # Primary Key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Foreign Key
    diagram_uid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("diagrams.uid", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # Stage Info
    stage_type: Mapped[StageType] = mapped_column(
        Enum(StageType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        index=True
    )

    status: Mapped[StageStatus] = mapped_column(
        Enum(StageStatus, values_callable=lambda x: [e.value for e in x]),
        default=StageStatus.PENDING,
        nullable=False
    )

    attempt: Mapped[int] = mapped_column(Integer, default=1)

    # Celery Task Info
    celery_task_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Timing
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Error Info
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_traceback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Metrics (JSON string)
    metrics_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    # Relationships
    diagram: Mapped["Diagram"] = relationship("Diagram", back_populates="stages")

    def start(self) -> None:
        """Отметить начало выполнения."""
        self.status = StageStatus.RUNNING
        self.started_at = datetime.utcnow()

    def complete(self, metrics: Optional[dict] = None) -> None:
        """Отметить успешное завершение."""
        import json
        self.status = StageStatus.COMPLETED
        self.completed_at = datetime.utcnow()
        if self.started_at:
            self.duration_seconds = (self.completed_at - self.started_at).total_seconds()
        if metrics:
            self.metrics_json = json.dumps(metrics)

    def fail(self, error: str, traceback: Optional[str] = None) -> None:
        """Отметить ошибку."""
        self.status = StageStatus.FAILED
        self.completed_at = datetime.utcnow()
        self.error_message = error
        self.error_traceback = traceback
        if self.started_at:
            self.duration_seconds = (self.completed_at - self.started_at).total_seconds()

    def __repr__(self) -> str:
        return f"<Stage {self.stage_type.value} ({self.status.value})>"

"""
Project model - проекты P&ID (термогидравлика, электрика и т.д.)
"""

from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime
from sqlalchemy.orm import relationship

from app.db.base import Base


class Project(Base):
    """
    Проект P&ID.
    
    Каждый проект имеет:
    - Свой набор классов оборудования
    - Свою YOLO модель
    - Свой CVAT Project
    
    Конфигурация хранится в YAML файле (configs/projects/{code}.yaml),
    динамические данные (cvat_project_id) — в БД.
    """
    __tablename__ = "projects"
    
    # Уникальный код проекта (thermohydraulics, electrical, ...)
    code = Column(String(50), primary_key=True)
    
    # Человекочитаемое имя
    name = Column(String(200), nullable=False)
    
    # CVAT интеграция
    cvat_project_id = Column(Integer, nullable=True)  # NULL если не создан
    cvat_project_name = Column(String(200), nullable=True)
    
    # Путь к YAML конфигу (относительный)
    config_path = Column(String(500), nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationships
    diagrams = relationship("Diagram", back_populates="project")
    
    def __repr__(self) -> str:
        return f"<Project {self.code}: {self.name}>"

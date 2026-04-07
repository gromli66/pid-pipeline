"""
Pydantic schemas для Project.
"""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


class ProjectBase(BaseModel):
    """Базовые поля проекта."""
    code: str = Field(..., example="thermohydraulics")
    name: str = Field(..., example="Термогидравлика")


class ProjectResponse(ProjectBase):
    """Ответ с информацией о проекте."""
    cvat_project_id: Optional[int] = None
    cvat_project_name: Optional[str] = None
    config_path: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class ProjectListResponse(BaseModel):
    """Список проектов."""
    items: List[ProjectResponse]
    total: int


class ProjectSummary(BaseModel):
    """Краткая информация для выбора в UI."""
    code: str
    name: str
    has_cvat_project: bool = False
    diagram_count: int = 0


class ProjectConfigInfo(BaseModel):
    """Информация из YAML конфига."""
    code: str
    name: str
    cvat_project_name: str
    num_classes: int
    yolo_num_classes: int
    yolo_weights: str
    detection_models_count: int = 1
    default_detection_model: str = "default"

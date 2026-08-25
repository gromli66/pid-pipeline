"""
Projects API - управление проектами P&ID.
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_async_db
from app.models import Project, Diagram
from app.schemas.project import (
    ProjectResponse,
    ProjectListResponse,
    ProjectSummary,
    ProjectConfigInfo,
)
from app.services.project_loader import get_project_loader, ProjectLoader
from app.services.class_display import display_name

router = APIRouter()


@router.get("/", response_model=ProjectListResponse)
async def list_projects(
    db: AsyncSession = Depends(get_async_db),
    loader: ProjectLoader = Depends(get_project_loader),
):
    """
    Получить список всех проектов.
    
    Синхронизирует YAML конфиги с БД.
    """
    configs = loader.load_all()
    
    result = await db.execute(select(Project))
    db_projects = {p.code: p for p in result.scalars().all()}
    
    items = []
    for config in configs:
        db_project = db_projects.get(config.code)
        
        if not db_project:
            # Создаём запись в БД
            db_project = Project(
                code=config.code,
                name=config.name,
                cvat_project_name=config.cvat_project_name,
                config_path=config.config_path,
            )
            db.add(db_project)
            await db.flush()
        
        items.append(ProjectResponse(
            code=db_project.code,
            name=db_project.name,
            cvat_project_id=db_project.cvat_project_id,
            cvat_project_name=db_project.cvat_project_name,
            config_path=db_project.config_path,
            created_at=db_project.created_at,
            updated_at=db_project.updated_at,
        ))
    
    await db.commit()
    
    return ProjectListResponse(items=items, total=len(items))


@router.get("/summary", response_model=List[ProjectSummary])
async def list_projects_summary(
    db: AsyncSession = Depends(get_async_db),
    loader: ProjectLoader = Depends(get_project_loader),
):
    """Краткий список проектов для UI."""
    configs = loader.load_all()
    
    result = await db.execute(select(Project))
    db_projects = {p.code: p for p in result.scalars().all()}
    
    # Подсчёт диаграмм
    counts_result = await db.execute(
        select(Diagram.project_code, func.count(Diagram.uid))
        .group_by(Diagram.project_code)
    )
    diagram_counts = dict(counts_result.all())
    
    items = []
    for config in configs:
        db_project = db_projects.get(config.code)
        items.append(ProjectSummary(
            code=config.code,
            name=config.name,
            has_cvat_project=db_project.cvat_project_id is not None if db_project else False,
            diagram_count=diagram_counts.get(config.code, 0),
        ))
    
    return items


@router.get("/{project_code}", response_model=ProjectResponse)
async def get_project(
    project_code: str,
    db: AsyncSession = Depends(get_async_db),
    loader: ProjectLoader = Depends(get_project_loader),
):
    """Получить проект по коду."""
    config = loader.load(project_code)
    if not config:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_code}")
    
    result = await db.execute(select(Project).where(Project.code == project_code))
    db_project = result.scalar_one_or_none()
    
    if not db_project:
        db_project = Project(
            code=config.code,
            name=config.name,
            cvat_project_name=config.cvat_project_name,
            config_path=config.config_path,
        )
        db.add(db_project)
        await db.commit()
        await db.refresh(db_project)
    
    return ProjectResponse(
        code=db_project.code,
        name=db_project.name,
        cvat_project_id=db_project.cvat_project_id,
        cvat_project_name=db_project.cvat_project_name,
        config_path=db_project.config_path,
        created_at=db_project.created_at,
        updated_at=db_project.updated_at,
    )


@router.get("/{project_code}/config", response_model=ProjectConfigInfo)
async def get_project_config(
    project_code: str,
    loader: ProjectLoader = Depends(get_project_loader),
):
    """Получить конфигурацию проекта из YAML."""
    config = loader.load(project_code)
    if not config:
        raise HTTPException(status_code=404, detail=f"Project config not found: {project_code}")
    
    return ProjectConfigInfo(
        code=config.code,
        name=config.name,
        cvat_project_name=config.cvat_project_name,
        num_classes=config.num_classes,
        yolo_num_classes=config.yolo.num_classes,
        yolo_weights=config.yolo.weights,
        detection_models_count=len(config.detection.models),
        default_detection_model=config.detection.default_model,
    )


@router.get("/{project_code}/classes")
async def get_project_classes(
    project_code: str,
    loader: ProjectLoader = Depends(get_project_loader),
):
    """Получить список классов проекта.

    `name` — внутреннее английское имя, его клиент кладёт в узел графа.
    `display_name` — что показать человеку (`display_labels` из YAML); при
    отсутствии перевода равно `name`, так что старые клиенты, читающие только
    `name`, продолжают работать без изменений.
    """
    config = loader.load(project_code)
    if not config:
        raise HTTPException(status_code=404, detail=f"Project config not found: {project_code}")

    return {
        "project_code": project_code,
        "num_classes": config.num_classes,
        "classes": [
            {"id": c.id, "name": c.name, "display_name": display_name(config, c.name)}
            for c in config.classes
        ],
    }


@router.get("/{project_code}/detection-models")
async def get_detection_models(
    project_code: str,
    loader: ProjectLoader = Depends(get_project_loader),
):
    """Получить список доступных моделей детекции для проекта."""
    config = loader.load(project_code)
    if not config:
        raise HTTPException(status_code=404, detail=f"Project config not found: {project_code}")

    return {
        "project_code": project_code,
        "default_model": config.detection.default_model,
        "models": [
            {
                "id": mid,
                "name": m.name or mid,
                "type": m.type,
                "description": m.description,
                "confidence": m.confidence,
                "has_per_class_confidence": bool(m.per_class_confidence),
            }
            for mid, m in config.detection.models.items()
        ],
    }

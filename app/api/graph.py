"""
Graph API — построение графа P&ID (Phase 5).

Endpoints:
- POST /{uid}/build        — запустить построение графа
                             (VALIDATED_JUNCTIONS | BUILT | ERROR → BUILDING_GRAPH)
- GET  /{uid}/result       — получить результат построения (node/edge count, artifacts)
"""

from uuid import UUID
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_async_db
from app.models import Diagram, DiagramStatus, Artifact, ArtifactType

router = APIRouter()


@router.post("/{uid}/build")
async def start_graph_building(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Запустить построение графа.

    Проверяет:
    - Диаграмма существует
    - Статус допускает запуск
    - Артефакт SKELETON_FINAL существует (скелетизация завершена)

    Переводит: → BUILDING_GRAPH
    Запускает: task_build_graph через Celery
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    # ---- Race condition fix: auto-chain уже запустил task ----
    # Если статус уже BUILDING_GRAPH — task уже в очереди,
    # просто возвращаем OK вместо ошибки.
    if diagram.status == DiagramStatus.BUILDING_GRAPH:
        return {
            "status": "building_graph",
            "message": "Graph building already in progress",
            "uid": str(uid),
        }

    # Разрешаем запуск из VALIDATED_JUNCTIONS (нормальный путь), повторный из BUILT/ERROR
    allowed_statuses = (
        DiagramStatus.VALIDATED_JUNCTIONS,
        DiagramStatus.BUILT,
        DiagramStatus.ERROR,
    )
    if diagram.status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot build graph: status is '{diagram.status.value}', "
                f"expected one of: {', '.join(s.value for s in allowed_statuses)}"
            ),
        )

    # Проверяем что скелетизация завершена (SKELETON_FINAL есть)
    skel_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.SKELETON_FINAL,
        )
    )
    skeleton_artifact = skel_result.scalar_one_or_none()

    if not skeleton_artifact:
        # Auto-dispatch: запускаем скелетонизацию и сообщаем клиенту
        try:
            from worker.celery_app import celery_app

            celery_app.send_task(
                "worker.tasks.skeleton.task_skeletonize_simple",
                args=[str(uid)],
            )
        except Exception:
            pass  # worker может быть недоступен

        return {
            "status": "skeletonizing",
            "message": (
                "Skeleton not ready — skeletonization auto-started. "
                "Retry graph build in a few seconds."
            ),
            "uid": str(uid),
        }

    # Переводим в BUILDING_GRAPH. Состояние до перехода держим целиком: если
    # отправка задачи упадёт, вернуть надо всё, что переход записал, а не один
    # статус — иначе диаграмма останется в ERROR с пустым error_stage, и клиент
    # погасит ВСЕ кнопки (ui/widgets/diagram_workspace.py: _error_key).
    previous_state = (diagram.status, diagram.error_stage, diagram.error_message)
    diagram.status = DiagramStatus.BUILDING_GRAPH
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()

    # Dispatch Celery task
    task_id = None
    try:
        from worker.celery_app import celery_app

        async_result = celery_app.send_task(
            "worker.tasks.graph.task_build_graph",
            args=[str(uid)],
        )
        task_id = async_result.id
    except Exception as exc:
        # Worker недоступен — возвращаем состояние, каким оно было до вызова.
        # Прежний откат ставил VALIDATED_MASKS, которого нет в allowed_statuses
        # этого же эндпоинта: повторная сборка отвечала 400 навсегда, а кнопка
        # «Сборка схемы» при таком статусе даже не нажимается (порог доступности —
        # VALIDATED_JUNCTIONS). Работа не начиналась, значит и откатывать некуда,
        # кроме исходной точки.
        diagram.status, diagram.error_stage, diagram.error_message = previous_state
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail=f"Worker unavailable: {exc}",
        )

    return {
        "status": "building_graph",
        "message": "Graph building started",
        "task_id": task_id,
        "uid": str(uid),
    }


@router.get("/{uid}/result")
async def get_graph_result(
    uid: UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Получить результат построения графа.

    Возвращает node_count, edge_count и список артефактов (graph_json, graph_overlay).
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    # Собираем артефакты графа
    artifacts_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type.in_([
                ArtifactType.GRAPH_JSON,
                ArtifactType.GRAPH_OVERLAY,
            ]),
        )
    )
    artifacts = artifacts_result.scalars().all()

    artifacts_info = {}
    for art in artifacts:
        artifacts_info[art.artifact_type.value] = {
            "file_path": art.file_path,
            "file_size": art.file_size,
        }

    return {
        "uid": str(uid),
        "status": diagram.status.value,
        "node_count": diagram.node_count,
        "edge_count": diagram.edge_count,
        "artifacts": artifacts_info,
    }


@router.post("/{uid}/generate-fxml")
async def generate_fxml(
    uid: UUID,
    page_size: str = Query(default=None, description="Page size: A0-A4 (landscape); '1920x1080' = screen sheet (standardized); None = original pixels."),
    bridge_gap: Optional[float] = Query(default=None, description="Bridge gap factor for FXML generation. None = use default."),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Запустить генерацию FXML из валидированного графа.

    Допустимые статусы: VALIDATED_GRAPH, COMPLETED (повторная генерация), ERROR.
    Переводит: → GENERATING_FXML.
    Запускает: task_generate_fxml через Celery.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    if diagram.status not in (
        DiagramStatus.VALIDATED_GRAPH,
        DiagramStatus.COMPLETED,
        DiagramStatus.GENERATING_FXML,
        DiagramStatus.ERROR,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot generate FXML: status is '{diagram.status.value}', "
            f"expected 'validated_graph' or 'completed'",
        )

    # Проверяем наличие графа (validated или обычного)
    graph_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type.in_([
                ArtifactType.GRAPH_VALIDATED,
                ArtifactType.GRAPH_JSON,
            ]),
        )
    )
    if not graph_result.scalars().first():
        raise HTTPException(
            status_code=400,
            detail="No graph artifact found. Build the graph first.",
        )

    # Переводим в GENERATING_FXML
    diagram.status = DiagramStatus.GENERATING_FXML
    diagram.error_message = None
    diagram.error_stage = None
    await db.commit()

    # Запускаем task
    task_id = None
    try:
        from worker.celery_app import celery_app

        task_kwargs = {"page_size": page_size}
        if bridge_gap is not None:
            task_kwargs["bridge_gap"] = bridge_gap
        async_result = celery_app.send_task(
            "worker.tasks.graph.task_generate_fxml",
            args=[str(uid)],
            kwargs=task_kwargs,
        )
        task_id = async_result.id
    except Exception as exc:
        # Worker недоступен — откатываем
        diagram.status = DiagramStatus.VALIDATED_GRAPH
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail=f"Worker unavailable: {exc}",
        )

    return {
        "status": "generating_fxml",
        "message": "FXML generation started",
        "task_id": task_id,
        "uid": str(uid),
    }

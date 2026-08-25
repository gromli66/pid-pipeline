"""
Graph API — построение графа P&ID (Phase 5).

Endpoints:
- POST /{uid}/build        — запустить построение графа
                             (VALIDATED_JUNCTIONS | BUILT | ERROR → BUILDING_GRAPH)
- GET  /{uid}/result       — получить результат построения (node/edge count, artifacts)
- POST /{uid}/prtx/build   — собрать .prtx на сервере (клиент отдаёт ключ лицензии)
- POST /{uid}/prtx/upload  — принять .prtx, собранный на машине с коробкой
"""

from datetime import datetime
from pathlib import Path
from uuid import UUID
from typing import Optional
from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import obs
from app.core.logging import get_logger
from app.db import get_async_db
from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
from app.models.stage import ProcessingStage, StageStatus, StageType
from app.services.storage import StorageService

router = APIRouter()

logger = get_logger(__name__)

# Б13: предел возраста живой строки сборки — `time_limit` самой задачи
# (`worker/tasks/graph.py`: 1800 с). Строка старше него — сирота от воркера,
# убитого hard limit'ом или OOM: `stage.fail()` в этом случае не исполняется,
# и RUNNING остаётся навсегда. Без предела ЛЮБАЯ такая строка блокировала бы
# пересборку вечно, а снять её нечем — откат стадий не трогает. Прецедент без
# предела — `app/services/layout_dispatch.py` (`_ACTIVE`), копировать слепо
# нельзя.
GRAPH_BUILD_TIME_LIMIT_SECONDS = 1800


async def running_graph_build(uid: UUID, db: AsyncSession) -> Optional[ProcessingStage]:
    """Живая строка сборки графа этой диаграммы, иначе None.

    «Живая» = RUNNING И моложе `GRAPH_BUILD_TIME_LIMIT_SECONDS` по `started_at`
    (сравнивается наивным UTC — ровно таким его пишет `ProcessingStage.start`).
    Берётся САМАЯ СВЕЖАЯ строка: `worker/utils/db_helpers.start_stage` заводит
    и стартует её одним действием, поэтому порядок по `id` совпадает с порядком
    по `started_at`, и если новейшая просрочена — просрочены и все прежние.

    Зачем это, если статус уже проверен. Статус и стадия расходятся: задача
    может БЕЖАТЬ, когда диаграмма уже `built` (первая досчитала), или откачена
    оператором назад. Замер 1-23: 7 пар ОДНОВРЕМЕННОГО `graph_building` на
    3 uid, 344.5 с CPU впустую; гард самой задачи обе копии пропускает.

    ⚠ Заслон ЧАСТИЧНЫЙ. PENDING-строк при диспатче никто не создаёт — строка
    появляется, когда воркер БЕРЁТ задачу, — поэтому всё окно очереди (на CPU
    это десятки минут; замеренные пары родились именно там) заслон не видит.
    Полное закрытие — PENDING-строка при диспатче плюс `stage_id` в задаче,
    паттерн `layout_dispatch` — за пунктом 6-9 дороги.
    """
    result = await db.execute(
        select(ProcessingStage)
        .where(
            ProcessingStage.diagram_uid == uid,
            ProcessingStage.stage_type == StageType.GRAPH_BUILDING,
            ProcessingStage.status == StageStatus.RUNNING,
        )
        .order_by(ProcessingStage.id.desc())
        .limit(1)
    )
    stage = result.scalar_one_or_none()
    if stage is None or stage.started_at is None:
        # Пустой `started_at` у RUNNING-строки судить нечем: по возрасту она
        # неотличима от вечной, а вечная блокировать не имеет права.
        return None

    age = (datetime.utcnow() - stage.started_at).total_seconds()
    if age >= GRAPH_BUILD_TIME_LIMIT_SECONDS:
        logger.info(
            "Строка сборки %s старше %s с (%.0f) — сиротой не блокирует",
            stage.id, GRAPH_BUILD_TIME_LIMIT_SECONDS, age,
            extra={"event": "stale_stage"},
        )
        return None
    return stage


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

    # Метка фазы поднята сюда из перехода ниже: ветка авто-скелетизации тоже
    # пишет в лог, и без привязки её строка ушла бы без `uid`.
    obs.bind(uid=str(uid), phase="graph_build")

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
        # Auto-dispatch: запускаем скелетонизацию и сообщаем клиенту.
        # Прежняя редакция глотала отказ брокера (`except Exception: pass`) и всё
        # равно отвечала «skeletonizing» — при НУЛЕ поставленных задач, во всех
        # трёх допустимых статусах (замер 1.16, §99). Клиент печатал оператору
        # «📊 Построение графа запущено» и уходил ждать того, чего не будет.
        # Статус здесь не менялся, поэтому возвращать нечего: чинится только
        # правда ответа.
        try:
            from worker.celery_app import celery_app

            celery_app.send_task(
                "worker.tasks.skeleton.task_skeletonize_simple",
                args=[str(uid)],
            )
        except Exception as exc:  # noqa: BLE001 — на бою это RuntimeError
            # (мёртвый result-бэкенд), а не OperationalError брокера:
            # docs/STATUS_MACHINE.md §5. Узкий класс промахнулся бы.
            logger.warning(
                "Авто-скелетизация не поставлена (%s) — статус остался '%s'",
                exc, diagram.status.value, extra={"event": "dispatch_failed"},
            )
            raise HTTPException(
                status_code=503,
                detail=f"Worker unavailable: скелетизация не поставлена ({exc})",
            )

        return {
            "status": "skeletonizing",
            "message": (
                "Skeleton not ready — skeletonization auto-started. "
                "Retry graph build in a few seconds."
            ),
            "uid": str(uid),
        }

    # Б13: сборка уже БЕЖИТ — второй задачи не ставим. Идемпотентный выход
    # выше судит по СТАТУСУ, а он с бегущей задачей расходится (первая копия
    # досчитала и поставила `built`; оператор откатился назад), — поэтому
    # заслон смотрит на строку стадии, а не на статус.
    running = await running_graph_build(uid, db)
    if running is not None:
        logger.info(
            "Сборка графа уже бежит (стадия %s) — вторую не ставлю",
            running.id, extra={"event": "dispatch_skipped"},
        )
        return {
            "status": diagram.status.value,
            "message": "Graph building already in progress",
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
        logger.warning(
            "Отправка сборки графа не удалась (%s) — состояние возвращено в '%s'",
            exc, previous_state[0].value, extra={"event": "dispatch_failed"},
        )
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


async def _store_prtx(db: AsyncSession, uid: UUID, content: bytes):
    """Положить .prtx рядом с FXML и перевыпустить артефакт PRTX."""
    storage = StorageService()
    file_path, file_size = await storage.save_file(uid, "fxml", "diagram.prtx", content)

    # Перевыпуск: старый артефакт снимаем, файл перезаписан по тому же пути
    old_result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.PRTX,
        )
    )
    old = old_result.scalar_one_or_none()
    if old:
        await db.delete(old)
        await db.flush()

    db.add(Artifact(
        diagram_uid=uid,
        artifact_type=ArtifactType.PRTX,
        file_path=file_path,
        file_size=file_size,
        mime_type="application/octet-stream",
    ))
    await db.commit()
    return file_path, file_size


async def _artifact_bytes(db: AsyncSession, uid: UUID, art_type: ArtifactType):
    """Прочитать файл артефакта с диска. None, если артефакта или файла нет."""
    from app.config import settings

    result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == art_type,
        )
    )
    artifact = result.scalar_one_or_none()
    if not artifact:
        return None
    path = Path(settings.STORAGE_PATH) / artifact.file_path
    return path.read_bytes() if path.is_file() else None


@router.get("/prtx/health")
async def prtx_health():
    """Готов ли конвертер расчётных схем.

    Нужен клиенту для проверки лицензии до начала работы: оператор жмёт
    «Лицензия САПФИР» и сразу видит, дойдёт ли дело до сборки, а не узнаёт об
    этом на экспорте, пройдя весь пайплайн.
    """
    import httpx

    from app.config import settings

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{settings.PRTX_SERVICE_URL}/health")
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Сервис конвертера недоступен: {exc}") from exc


#: Состояние фоновых сборок .prtx: uid -> {"state", "started_at", …}.
#: Живёт в памяти процесса — api запускается одним воркером uvicorn
#: (Dockerfile.api). После рестарта состояние теряется, и клиент это переживает:
#: `state: idle` он трактует как «сборки нет», а готовый файл всё равно лежит
#: артефактом.
_PRTX_JOBS: dict[str, dict] = {}


async def _prtx_job(uid: UUID, payload: dict):
    """Собрать схему и сохранить её. Выполняется ПОСЛЕ ответа клиенту.

    Сборка идёт минутами (замер 2026-08-22: 2 минуты на схеме со сканом
    4.6 МБ). Держать всё это время открытым HTTP-соединение с клиентом
    нельзя — промежуточные узлы рвут его по бездействию, и оператор видит
    «ничего не скачалось», хотя схема на сервере уже готова.
    """
    import base64

    import httpx

    from app.config import settings
    from app.db.session import AsyncSessionLocal

    key = str(uid)
    try:
        async with httpx.AsyncClient(timeout=settings.PRTX_TIMEOUT_SEC) as client:
            response = await client.post(
                f"{settings.PRTX_SERVICE_URL}/build", json=payload)
    except httpx.HTTPError as exc:
        logger.error("PRTX service unreachable for %s: %s", uid, exc)
        _PRTX_JOBS[key] = {"state": "error",
                           "error": f"Сервис конвертера недоступен: {exc}"}
        return
    finally:
        payload.clear()      # ключ не держим в памяти дольше нужного

    if response.status_code != 200:
        detail = response.json().get("error", response.text[:500])
        logger.error("PRTX service failed for %s (%d): %s",
                     uid, response.status_code, detail)
        _PRTX_JOBS[key] = {"state": "error", "error": detail}
        return

    try:
        content = base64.b64decode(response.json()["prtx"])
        async with AsyncSessionLocal() as db:
            file_path, file_size = await _store_prtx(db, uid, content)
    except Exception as exc:  # noqa: BLE001 — диск/БД; причина уходит клиенту
        logger.error("PRTX store failed for %s: %s", uid, exc, exc_info=True)
        _PRTX_JOBS[key] = {"state": "error", "error": f"Не удалось сохранить: {exc}"}
        return

    logger.info("PRTX built for %s: %s (%d bytes)", uid, file_path, file_size)
    _PRTX_JOBS[key] = {"state": "done", "file_path": file_path, "file_size": file_size}


@router.get("/{uid}/prtx/status")
async def prtx_status(uid: UUID, db: AsyncSession = Depends(get_async_db)):
    """Чем закончилась фоновая сборка. Клиент опрашивает это короткими запросами.

    `state` — ход ТЕКУЩЕЙ сборки; он живёт в памяти процесса и после рестарта
    api теряется. `artifact_ready` отвечает на другой вопрос — «лежит ли на
    сервере готовый .prtx». По нему диалог экспорта решает, есть ли что качать,
    и переживает рестарт api; ждущему сборку клиенту он не указ (свежесть файла
    он не доказывает — см. _wait_for_build).
    """
    from app.config import settings

    result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.PRTX,
        )
    )
    artifact = result.scalar_one_or_none()
    ready = artifact is not None and (
        Path(settings.STORAGE_PATH) / artifact.file_path).is_file()

    state = dict(_PRTX_JOBS.get(str(uid), {"state": "idle"}))
    state["artifact_ready"] = ready
    state["artifact_size"] = artifact.file_size if ready else None
    return state


@router.post("/{uid}/prtx/build", status_code=202)
async def build_prtx(
    uid: UUID,
    background_tasks: BackgroundTasks,
    license_key: UploadFile = File(
        ..., alias="license",
        description="Ключ лицензии САПФИР (.S$lk$.bin) с машины оператора"),
    text_mode: str = Query("all", pattern="^(all|bound|none)$"),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Запустить сборку .prtx на сервере и сразу вернуть управление.

    Движок САПФИР крутится в контейнере `prtx` (docker/prtx): в Linux он даёт
    дамп, совпадающий с windows-прогоном байт-в-байт (замер 2026-08-21).
    Единственное, чего у сервера нет, — ключ лицензии: он приезжает этим
    запросом, уходит в сервис и на сервере не хранится — ни на диске, ни в БД,
    ни в логах.

    Результат забирается опросом `/prtx/status` и скачиванием артефакта: ждать
    ответа в этом же запросе нельзя, сборка идёт минутами (см. `_prtx_job`).
    """
    import base64

    from app.config import settings                      # noqa: F401 — см. _prtx_job

    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()
    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    obs.bind(uid=str(uid), phase="generating_fxml")

    graph = await _artifact_bytes(db, uid, ArtifactType.GRAPH_VALIDATED)
    if graph is None:
        raise HTTPException(
            status_code=409,
            detail="Нет валидированного графа — нечего конвертировать")
    # Скан не обязателен: без него угол насосов берётся по трубам
    image = await _artifact_bytes(db, uid, ArtifactType.ORIGINAL_IMAGE)

    key = await license_key.read()
    if not key:
        raise HTTPException(status_code=400, detail="Пустой ключ лицензии")

    payload = {
        "graph": base64.b64encode(graph).decode("ascii"),
        "license": base64.b64encode(key).decode("ascii"),
        "text_mode": text_mode,
    }
    if image:
        payload["image"] = base64.b64encode(image).decode("ascii")

    # Повтор поверх бегущей сборки не запускает вторую: клиент повторяет
    # запрос при обрыве связи, и без этого сервер получил бы очередь одинаковых
    # заданий, каждое по паре минут.
    if _PRTX_JOBS.get(str(uid), {}).get("state") == "building":
        payload.clear()
        del key
        return {"status": "building", "message": "Сборка уже идёт", "uid": str(uid)}

    logger.info("PRTX build for %s: graph %d B, image %s",
                uid, len(graph), f"{len(image)} B" if image else "none")

    _PRTX_JOBS[str(uid)] = {"state": "building"}
    background_tasks.add_task(_prtx_job, uid, payload)
    del key

    return {"status": "building", "uid": str(uid)}


@router.post("/{uid}/prtx/upload")
async def upload_prtx(
    uid: UUID,
    file: UploadFile = File(..., description="Расчётная схема САПФИР (.prtx)"),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Принять .prtx, собранный клиентом, и положить рядом с FXML.

    Путь для машин, где конвертер стоит локально коробкой. Штатный путь —
    /{uid}/prtx/build: сборка на сервере, клиент отдаёт только ключ лицензии.
    """
    result = await db.execute(select(Diagram).where(Diagram.uid == uid))
    diagram = result.scalar_one_or_none()

    if not diagram:
        raise HTTPException(status_code=404, detail="Diagram not found")

    obs.bind(uid=str(uid), phase="generating_fxml")

    content = await file.read()
    file_path, file_size = await _store_prtx(db, uid, content)

    logger.info("PRTX uploaded for %s: %s (%d bytes)", uid, file_path, file_size)

    return {"status": "saved", "file_path": file_path, "file_size": file_size, "uid": str(uid)}

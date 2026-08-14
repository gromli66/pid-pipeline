"""Диспетчер авто-раскладки: поставить задачу ровно один раз на одну истину.

Зовётся из точек, где закрывается этап контуров (`app/api/contours.py`), то
есть до OCR: раскладке нужен граф с финальной геометрией, а контуры её и
меняют. Счёт прячется за вкладкой «Привязка подписей» — §3.2 плана
docs/planning/AUTO_LAYOUT_INTEGRATION.md.

Почему идемпотентность здесь, а не в задаче. Оба contours-эндпоинта не
проверяют текущий статус и вызываются повторно из любого состояния, а вкладка
контуров открыта всегда — каждое подтверждение шлёт complete. При
worker_concurrency=2 две задачи на разные истины побегут параллельно, и та,
что финишировала позже, перезапишет холст более свежей. Дедупликации задач в
проекте нет, поэтому правило простое:

  * бежит задача на ТУ ЖЕ истину -> ничего не делаем;
  * бежит задача на ДРУГУЮ истину -> revoke по celery_task_id, старую стадию
    закрываем SKIPPED, ставим новую;
  * истина сменилась -> снимаем operator_saved с холста: правки сделаны на
    устаревшей истине и не сохраняются (решение заказчика 2026-07-28).

Состояние — стадия ProcessingStage типа LAYOUT. По наличию артефакта его
определить нельзя: отсутствие холста неразличимо между «считает», «упала» и
«не стартовала» (а async_safe_dispatch при сбое брокера молча вернёт None).
"""

import asyncio
import json
import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.models import Artifact, ArtifactType
from app.models.stage import ProcessingStage, StageStatus, StageType
from app.services.layout_policy import (
    ALREADY_FRESH, ALREADY_RUNNING, plan_dispatch,
)
from app.services.storage import StorageService
from modules.graph.core import canvas_state
from modules.graph.core.contours_merge import (
    contours_are_stale, load_validated_contours,
)

logger = logging.getLogger(__name__)

TASK_NAME = "worker.tasks.layout.task_run_layout"
_ACTIVE = (StageStatus.PENDING, StageStatus.RUNNING)


async def _validated_graph(uid: UUID, db):
    """graph_validated с диска. None, если его ещё нет."""
    result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.GRAPH_VALIDATED,
        )
    )
    art = result.scalar_one_or_none()
    if not art:
        return None
    path = StorageService().base_path / art.file_path
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("layout: не читается %s: %s", path, exc)
        return None


def _stage_sha(stage) -> str:
    """Истина, под которую заводилась стадия."""
    try:
        return (json.loads(stage.metrics_json or "{}") or {}).get("source_sha")
    except ValueError:
        return None


def _revoke(task_id: str) -> None:
    """Отозвать задачу. Без terminate: убивать чужой процесс на полпути опаснее,
    чем дать ей досчитать — записать она всё равно не сможет, сверка sha перед
    записью не пустит (прецедент revoke в проекте — app/api/cvat.py)."""
    if not task_id:
        return
    try:
        from worker.celery_app import celery_app
        celery_app.control.revoke(task_id)
        logger.info("layout: отозвана задача %s", task_id)
    except Exception as exc:  # noqa: BLE001 — брокер может быть недоступен
        logger.warning("layout: не удалось отозвать %s: %s", task_id, exc)


def _validated_contours(uid: UUID) -> list:
    """Выбранные оператором контуры (polygon_validated) с диска, [] если нет."""
    path = (StorageService().base_path / str(uid)
            / "contours" / "contours_validated.json")
    return load_validated_contours(path)


async def _canvas_artifact(uid: UUID, db):
    result = await db.execute(
        select(Artifact).where(
            Artifact.diagram_uid == uid,
            Artifact.artifact_type == ArtifactType.GRAPH_CANVAS,
        )
    )
    return result.scalar_one_or_none()


async def _canvas(uid: UUID, db):
    """(граф холста, путь, артефакт-или-None). Всё None, если холста нет.

    Файл ищется и БЕЗ строки артефакта: откат сносит строку, а файл на диске
    оставляет. Для раскладки это существенно — её результат чистая функция от
    истины, и если истина не менялась, пересчитывать 2-5 минут нечего.
    Устаревший файл при этом не пройдёт: свежесть проверяется отдельно.
    """
    art = await _canvas_artifact(uid, db)
    base = StorageService().base_path
    path = (base / art.file_path) if art else (
        base / str(uid) / "graph" / "graph_canvas.json")
    if not path.exists():
        return None, None, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), path, art
    except (OSError, ValueError) as exc:
        logger.warning("layout: не читается холст %s: %s", path, exc)
        return None, None, None


async def _clear_operator_saved(uid: UUID, db) -> None:
    """Снять с холста метку «правился руками»: истина сменилась.

    Правки, сделанные на устаревшей истине, не сохраняются — холст всё равно
    будет пересобран. Флаг снимаем, чтобы задача не приняла их за причину
    выбросить свежий результат.
    """
    canvas, path, _art = await _canvas(uid, db)
    if canvas is None:
        return
    if not canvas_state.read_state(canvas)["operator_saved"]:
        return
    canvas.setdefault("graph", {}).setdefault(
        "canvas_transform", {})["operator_saved"] = False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(canvas, ensure_ascii=False), encoding="utf-8")
    import os
    os.replace(tmp, path)
    logger.info("layout: снят operator_saved — истина сменилась (%s)", uid)


async def dispatch_layout(uid: UUID, db, *, force: bool = False) -> dict:
    """Поставить раскладку, если её ещё нет на текущую истину.

    force=True — ВОЗВРАТ (откат по бусине): пересчитываем всегда, даже если
    истина не менялась. Так решено в §3.2: «повторное закрытие этапа
    перезапускает раскладку». Готовый холст тут не переиспользуется по
    существу, а не ради экономии — он несёт правки оператора, сделанные до
    возврата, а они на возврате не сохраняются.

    Возвращает {"status": ..., "task_id": ...}. Никогда не бросает: сбой
    постановки не должен ронять закрытие этапа контуров — оператор пойдёт
    дальше, а гейт «Ручной правки» увидит стадию FAILED и скажет почему.
    """
    try:
        validated = await _validated_graph(uid, db)
        if validated is None:
            logger.info("layout: %s — graph_validated нет, раскладка пропущена",
                        uid)
            return {"status": "no_input"}
        source_sha = canvas_state.graph_projection_sha(validated)

        # Уже посчитано на эту истину — считать нечего. Без этой проверки
        # диспетчер нельзя было бы звать из «возвратных» точек: каждый повтор
        # заводил бы лишнюю задачу на 2-5 минут боевого CPU.
        canvas, path, art = await _canvas(uid, db)
        canvas_info = None
        if canvas is not None:
            stale = canvas_state.is_stale(canvas, validated)[0]
            if not stale:
                # Контуры — единственный вход холста вне sha-проекции графа:
                # оператор мог переиграть их и снова закрыть этап без
                # возврата. Проверка ленивая (геометрия уже решила — контуры
                # не читаем) и вне event loop (json с полигонами немаленький).
                # Corner: если задача на ту же graph-истину уже бежит, новая
                # не ставится (ALREADY_RUNNING) — бегущая читает контуры с
                # диска сама, а разойдясь во времени, устареет и будет
                # переставлена следующим закрытием этапа.
                contour_nodes = await asyncio.to_thread(
                    _validated_contours, uid)
                stale, c_reason = contours_are_stale(canvas, contour_nodes)
                if stale:
                    logger.info("layout: %s — контуры холста устарели: %s",
                                uid, c_reason)
            canvas_info = {
                "layout_applied": canvas_state.has_layout(canvas),
                "stale": stale,
            }
            if art is None and not force and canvas_info["layout_applied"] \
                    and not canvas_info["stale"]:
                # Холст цел и актуален, а строку артефакта снесли (не откатом —
                # на возврате мы сюда не заходим, там force). Считать заново
                # нечего — возвращаем её на место.
                db.add(Artifact(
                    diagram_uid=uid,
                    artifact_type=ArtifactType.GRAPH_CANVAS,
                    file_path=str(path.relative_to(StorageService().base_path)),
                    file_size=path.stat().st_size,
                    mime_type="application/json",
                ))
                await db.commit()
                logger.info("layout: %s — холст цел, артефакт восстановлен", uid)

        result = await db.execute(
            select(ProcessingStage)
            .where(ProcessingStage.diagram_uid == uid,
                   ProcessingStage.stage_type == StageType.LAYOUT)
            .order_by(ProcessingStage.id.desc())
        )
        stages = result.scalars().all()
        running = [s for s in stages if s.status in _ACTIVE]

        action, to_revoke = plan_dispatch(
            source_sha, canvas_info, [(s, _stage_sha(s)) for s in running],
            force=force)

        if action == ALREADY_FRESH:
            logger.info("layout: %s — холст уже посчитан на эту истину", uid)
            return {"status": ALREADY_FRESH}
        if action == ALREADY_RUNNING:
            same = next(s for s in running if _stage_sha(s) == source_sha)
            logger.info("layout: %s — задача на ту же истину уже бежит "
                        "(stage %s)", uid, same.id)
            return {"status": ALREADY_RUNNING, "task_id": same.celery_task_id}

        for stage in to_revoke:
            # Истина сменилась: старая задача считает по устаревшему графу
            await asyncio.to_thread(_revoke, stage.celery_task_id)
            stage.status = StageStatus.SKIPPED
            stage.error_message = "истина изменилась, поставлена новая раскладка"

        await _clear_operator_saved(uid, db)

        stage = ProcessingStage(
            diagram_uid=uid,
            stage_type=StageType.LAYOUT,
            status=StageStatus.PENDING,
            attempt=len(stages) + 1,
            metrics_json=json.dumps({"source_sha": source_sha}),
        )
        db.add(stage)
        await db.commit()
        await db.refresh(stage)

        from app.services.dispatch import async_safe_dispatch
        task_id = await async_safe_dispatch(
            TASK_NAME, [str(uid), stage.id, source_sha])

        if task_id is None:
            # Брокер недоступен: стадия обязана стать FAILED, иначе гейт будет
            # ждать задачу, которой не существует.
            stage.status = StageStatus.FAILED
            stage.error_message = "не удалось поставить задачу раскладки"
            await db.commit()
            return {"status": "dispatch_failed"}

        stage.celery_task_id = task_id
        await db.commit()
        logger.info("layout: %s — поставлена задача %s (stage %s)",
                    uid, task_id, stage.id)
        return {"status": "dispatched", "task_id": task_id,
                "stage_id": stage.id}

    except Exception as exc:  # noqa: BLE001
        logger.exception("layout: диспетчеризация для %s не удалась: %s",
                         uid, exc)
        return {"status": "error", "error": str(exc)[:200]}

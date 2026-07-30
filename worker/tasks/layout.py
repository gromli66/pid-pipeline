"""Layout Task — авто-раскладка графа на холсте 1920x1080.

task_run_layout:
    Вход: graph_validated (координаты растра) -> холст без declust
    (`canvas_input.to_canvas`) -> осевая расстановка -> раздвигание.
    Выход: graph/graph_canvas.json (артефакт GRAPH_CANVAS).

    НЕ МЕНЯЕТ DiagramStatus: операция идёт внутри одного этапа. Состояние —
    стадия ProcessingStage типа LAYOUT, её читает клиент через GET /{uid}/stages.

Запускается диспетчером (`app/services/layout_dispatch.py`) в момент закрытия
контуров и считается, пока оператор работает во вкладке «Привязка подписей».
Полный контекст — docs/planning/AUTO_LAYOUT_INTEGRATION.md, §3.2/§3.3 и Э5.

ДВЕ ЗАЩИТЫ ПЕРЕД ЗАПИСЬЮ, без них задача портит работу оператора:

1. Сверка входа. `task_acks_late` + `task_reject_on_worker_lost` означают, что
   задача, потерянная вместе с воркером, штатно передоставляется брокером и
   допишет результат позже; а `time_limit`, равный дефолтному redis
   visibility_timeout, допускает дубль и вовсе без падения. Поэтому досчитав,
   задача перечитывает graph_validated и сверяет sha-проекцию со своим входом.
   Не сошлось — результат выбрасывается, писать его уже некуда.

2. Флаг operator_saved. Если оператор успел сохранить холст руками, «воскресшая»
   задача не имеет права его затереть. Флаг ставит сервер на POST canvas/save;
   диспетчер снимает его, когда истина сменилась и раскладка считается заново.

Запись атомарная (tmp + os.replace): клиентский автосейв ходит в тот же файл,
а StorageService пишет напрямую, без tmp.
"""

import json
import os
import traceback
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded

from app.core import obs
from app.core.errors import ArtifactMissingError
from app.core.logging import get_logger
from worker.celery_app import celery_app
from worker.utils.db_helpers import (
    check_deleted, complete_stage, fail_stage,
)

logger = get_logger(__name__)

# Бюджет времени. Замер на dev-CPU: 936 узлов — 72 с, корпус целиком меньше
# двух минут. План оценивает боевой CPU-only в 2-5 мин, но модели
# «время от размера» нет, а 936 узлов — максимум корпуса. Поэтому лимит взят с
# большим запасом, и он ОБЯЗАН быть перемерян на боевом железе и на синтетике
# крупнее корпуса (gen_synth.py, 2-5 тыс. узлов) — Э5 плана.
LAYOUT_TIME_LIMIT = 1800        # 30 мин: жёсткий предел
LAYOUT_SOFT_TIME_LIMIT = 1500   # 25 мин: мягкий, даёт записать понятный отказ


def _skip_stage(stage, reason: str) -> None:
    """Стадия завершена БЕЗ записи результата — это штатный исход, не отказ.

    Отдельно от complete_stage: гейт вкладки различает «раскладка готова» и
    «раскладка не применилась», и SKIPPED для него значит «жди не её».
    """
    import json as _json
    from datetime import datetime

    from app.models.stage import StageStatus

    if stage is None:
        return
    stage.status = StageStatus.SKIPPED
    stage.completed_at = datetime.utcnow()
    stage.current_step = None
    if stage.started_at:
        stage.duration_seconds = (
            stage.completed_at - stage.started_at).total_seconds()
    stage.metrics_json = _json.dumps({"skipped": reason}, ensure_ascii=False)


def _atomic_write_json(path: Path, payload: str) -> int:
    """Записать JSON атомарно: tmp рядом + os.replace.

    Прецедент в проекте — binding/apply. os.replace (а не Path.rename) потому,
    что rename на Windows падает при существующей цели; на бою Linux, но
    разработка идёт на Windows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = payload.encode("utf-8")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return len(data)


@celery_app.task(
    bind=True,
    name="worker.tasks.layout.task_run_layout",
    max_retries=0,          # повтор не нужен: диспетчер поставит задачу заново
    time_limit=LAYOUT_TIME_LIMIT,
    soft_time_limit=LAYOUT_SOFT_TIME_LIMIT,
    acks_late=True,
)
def task_run_layout(self, diagram_uid: str, stage_id: int = None,
                    dispatch_sha: str = None):
    """Посчитать раскладку и записать холст.

    stage_id: строка ProcessingStage, созданная диспетчером (он же владеет
        идемпотентностью и revoke). Без неё задача заведёт стадию сама.
    dispatch_sha: sha-проекция графа на момент постановки — только для лога:
        истину задача берёт из файла, который читает сама.
    """
    from app.db.session import SessionLocal
    from app.models import Artifact, ArtifactType, Diagram
    from app.models.stage import ProcessingStage, StageType
    from modules.graph.core import canvas_state
    from modules.graph.core.layout import LayoutParams, layout
    from modules.graph.core.layout.residual import collect_residual
    from modules.graph.core.canvas_input import to_canvas

    storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
    graph_dir = storage_path / str(diagram_uid) / "graph"
    validated_path = graph_dir / "graph_validated.json"
    canvas_path = graph_dir / "graph_canvas.json"

    db = SessionLocal()
    stage = None
    obs.bind(uid=str(diagram_uid), phase="layout", task_id=self.request.id,
             attempt=self.request.retries)

    try:
        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if not diagram:
            logger.error("Diagram %s not found", diagram_uid)
            return {"status": "not_found"}
        if check_deleted(db, diagram_uid):
            logger.info("Diagram %s is deleted, aborting layout", diagram_uid)
            return {"status": "deleted"}

        if stage_id is not None:
            stage = db.query(ProcessingStage).filter(
                ProcessingStage.id == stage_id).first()
        if stage is None:
            from worker.utils.db_helpers import start_stage
            stage = start_stage(db, diagram_uid, StageType.LAYOUT,
                                celery_task_id=self.request.id)
        else:
            stage.celery_task_id = self.request.id
            stage.start()
            db.commit()

        if not validated_path.exists():
            raise ArtifactMissingError(
                f"graph_validated.json not found: {validated_path}",
                stage="layout")

        with obs.step("load_input", logger):
            validated = json.loads(validated_path.read_text(encoding="utf-8"))
            source_sha = canvas_state.graph_projection_sha(validated)
            if dispatch_sha and dispatch_sha != source_sha:
                # Не отказ: истина сменилась между постановкой и стартом, и
                # считать надо по текущей. Сверка перед записью всё равно
                # защитит от гонки.
                logger.info("[%s] истина сменилась после постановки: %s -> %s",
                            diagram_uid, dispatch_sha, source_sha)
            graph, _transform = to_canvas(validated)

        with obs.step("compute", logger):
            # stages["orig"] — снимок входа раскладки; по нему residual (Э12)
            # считает легальность наложений (детекция ещё на своих местах).
            layout_stages = {}
            graph, stats = layout(graph, LayoutParams(), stages=layout_stages)
            logger.info("[%s] раскладка: дефектов %s -> %s, узлов %d",
                        diagram_uid, stats["defects_before"],
                        stats["defects_after"], len(graph.get("nodes", [])))

        # ─── защита 1: истина не изменилась, пока мы считали ───
        fresh = json.loads(validated_path.read_text(encoding="utf-8"))
        if canvas_state.graph_projection_sha(fresh) != source_sha:
            logger.warning(
                "[%s] graph_validated изменился за время счёта — результат "
                "выброшен без записи", diagram_uid)
            _skip_stage(stage, "источник изменился за время счёта")
            db.commit()
            return {"status": "stale_input", "diagram_uid": diagram_uid}

        # ─── защита 2: оператор правил холст руками ───
        if canvas_path.exists():
            try:
                existing = json.loads(canvas_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = {}
            if canvas_state.read_state(existing)["operator_saved"]:
                logger.warning(
                    "[%s] холст правился оператором — результат раскладки "
                    "выброшен без записи", diagram_uid)
                _skip_stage(stage, "холст правился оператором")
                db.commit()
                return {"status": "operator_saved", "diagram_uid": diagram_uid}

        # ─── запись ───
        canvas_state.stamp(graph, validated, layout_applied=True)
        size = _atomic_write_json(
            canvas_path, json.dumps(graph, ensure_ascii=False))

        rel_path = str(canvas_path.relative_to(storage_path))
        old = db.query(Artifact).filter(
            Artifact.diagram_uid == diagram_uid,
            Artifact.artifact_type == ArtifactType.GRAPH_CANVAS,
        ).first()
        if old:
            old.file_path = rel_path
            old.file_size = size
        else:
            db.add(Artifact(
                diagram_uid=diagram_uid,
                artifact_type=ArtifactType.GRAPH_CANVAS,
                file_path=rel_path,
                file_size=size,
                mime_type="application/json",
            ))

        # ─── Э12: остаток — оператору адресно ───
        # Строго ПОСЛЕ обеих защит и записи холста: файл-сирота при
        # выброшенном результате невозможен. Ошибка здесь раскладку не валит:
        # холст уже записан, подсветка — вспомогательный артефакт.
        try:
            residual = collect_residual(graph, layout_stages["orig"])
            # Метка холста, для которого посчитан остаток: вкладка сверяет её
            # с загруженным холстом и не подсвечивает устаревшее.
            residual["canvas_sha"] = canvas_state.graph_projection_sha(graph)
            residual_path = graph_dir / "residual_defects.json"
            rsize = _atomic_write_json(
                residual_path, json.dumps(residual, ensure_ascii=False))
            rrel = str(residual_path.relative_to(storage_path))
            rold = db.query(Artifact).filter(
                Artifact.diagram_uid == diagram_uid,
                Artifact.artifact_type == ArtifactType.RESIDUAL_DEFECTS,
            ).first()
            if rold:
                rold.file_path = rrel
                rold.file_size = rsize
            else:
                db.add(Artifact(
                    diagram_uid=diagram_uid,
                    artifact_type=ArtifactType.RESIDUAL_DEFECTS,
                    file_path=rrel,
                    file_size=rsize,
                    mime_type="application/json",
                ))
            logger.info("[%s] остаток раскладки: %d очагов",
                        diagram_uid, residual["total"])
        except Exception as exc:  # noqa: BLE001 — подсветка не валит раскладку
            logger.exception("[%s] остаток раскладки не посчитан: %s",
                             diagram_uid, exc)

        complete_stage(stage, {
            "defects_before": stats["defects_before"],
            "defects_after": stats["defects_after"],
            "nodes": len(graph.get("nodes", [])),
            "source_sha": source_sha,
        })
        db.commit()
        logger.info("[%s] холст записан (%d байт)", diagram_uid, size)
        return {"status": "ok", "diagram_uid": diagram_uid,
                "defects_after": stats["defects_after"]}

    except SoftTimeLimitExceeded:
        # Явный отказ, а не молчаливый таймаут: гейт вкладки читает стадию и
        # обязан показать оператору причину, а не висеть.
        logger.error("[%s] раскладка не уложилась в лимит времени",
                     diagram_uid, exc_info=True)
        db.rollback()
        fail_stage(stage, "Раскладка не уложилась в лимит времени",
                   traceback.format_exc())
        db.commit()
        raise

    except Exception as exc:
        logger.error("[%s] раскладка упала: %s", diagram_uid, exc, exc_info=True)
        db.rollback()
        fail_stage(stage, str(exc)[:500], traceback.format_exc(), exc=exc)
        db.commit()
        raise

    finally:
        db.close()

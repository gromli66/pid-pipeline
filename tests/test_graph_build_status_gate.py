# -*- coding: utf-8 -*-
"""Гейт статуса сборки графа — таблица переходов `app/api/graph.py` (пункт 1.14 дороги).

Зачем. Класс правки — [сма], стейт-машина: `PROTOCOL §Гейты` требует СНАЧАЛА
зафиксировать текущие переходы, и только потом править. Покрытия у эндпоинта нет
вовсе: грепом `start_graph_building`/`api/graph` по `tests/` и `tools/` попадают
только тесты воркерной задачи `worker/tasks/graph.py` — про сам эндпоинт ноль.
Без этой таблицы отличить рефакторинг от порчи было бы нечем.

Что проверяется. `POST /{uid}/build` прогоняется по ВСЕМУ множеству
`DiagramStatus`, а не по паре интересных значений, и по обеим веткам отправки:
брокер жив и брокер лёг. Судится настоящая корутина эндпоинта: `AsyncSession`
подделана (её поверхность здесь — `execute` + `commit`), `send_task` подменён,
живой брокер и БД не нужны.

Тупик, ради которого таблица снята (пункт 1.14). Отправка задачи упала — эндпоинт
откатывает статус в `validated_masks`, которого НЕТ в его же `allowed_statuses`
(`graph.py:54-58`). Дальше оператор заперт дважды: повторный `/build` отвечает 400,
а кнопка «Сборка схемы» при этом статусе даже не нажимается — её порог доступности
`validated_junctions` (индекс 15) против индекса 9 у отката. Оба замка проверяются
здесь, второй — на инварианте ДАННЫХ (`_buttons_for_status`), без живого виджета.

Числа абсолютные, ожидания — литералы. `ALLOWED` и `ROLLBACK` не вычисляются из
проверяемого кода: иначе набор остался бы зелёным при любом его значении
(`PROTOCOL §3`). Порог заперт с двух сторон — и «пропускает то, что должен», и
«не пропускает ничего сверх»: каждый отказ дополнительно утверждает, что статус
не сдвинут, транзакция не коммитилась и задача не отправлялась.
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException

from app.api.graph import start_graph_building
from app.models import Artifact, ArtifactType, Diagram, DiagramStatus

UID = uuid.UUID("d74eb9f1-aaaa-bbbb-cccc-ddddeeeeffff")
BUILD_TASK = "worker.tasks.graph.task_build_graph"
SKELETON_TASK = "worker.tasks.skeleton.task_skeletonize_simple"

# Размер машины. Абсолютное число: новый статус обязан пройти через эту таблицу,
# а не проскочить мимо неё молча.
STATUS_COUNT = 31

# Какие статусы гейт пропускает к отправке задачи.
# Литерал — снят чтением `app/api/graph.py:54-58`, не вычислен из него.
ALLOWED = frozenset({
    "validated_junctions",
    "built",
    "error",
})

# Короткое замыкание: задача уже в очереди — 200 без отправки и без коммита
# (`graph.py:44-50`). Это не отказ и не запуск, поэтому отдельным множеством.
SHORTCIRCUIT = frozenset({
    "building_graph",
})

# Значения `error_stage`, которые реально пишет конвейер
# (`worker/utils/db_helpers.set_diagram_error`).
ERROR_STAGES = [
    None,
    "building_graph",
    "detecting",
    "segmenting",
    "skeletonizing",
    "skeletonizing_simple",
    "detecting_junctions",
    "generating_fxml",
    "ocr",
]

# Откат при мёртвом брокере. Ключ — состояние ДО вызова, значение — состояние
# ПОСЛЕ отказа отправки: (status, error_stage, error_message). Перечислены все
# входы из `ALLOWED` — других путей до отката нет.
#
# Читается так: чем бы диаграмма ни была, отказ отправки сваливает её в один
# и тот же `validated_masks` и стирает поля ошибки.
ROLLBACK = {
    ("validated_junctions", None, None): ("validated_masks", None, None),
    ("built", None, None): ("validated_masks", None, None),
    ("error", "building_graph", "boom"): ("validated_masks", None, None),
}


def _pre_state(status):
    """Пред-состояние для перебора: у `error` поля ошибки заполнены.

    Правило фиксированное и от проверяемого кода не зависит — иначе перебор
    подстроился бы под гейт вместо того, чтобы его судить.
    """
    if status is DiagramStatus.ERROR:
        return (status.value, "building_graph", "boom")
    return (status.value, None, None)


# ── харнесс ──────────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class FakeDB:
    """Поверхность `AsyncSession`, которой пользуется эндпоинт: `execute` + `commit`.

    Ответ выбирается по СУЩНОСТИ запроса, а не по порядку вызовов: порядок двух
    `select` внутри эндпоинта — не контракт, и тест не должен за него держаться.
    """

    def __init__(self, diagram, skeleton=None):
        self.diagram = diagram
        self.skeleton = skeleton
        self.commits = 0

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Diagram:
            return _FakeResult(self.diagram)
        if entity is Artifact:
            return _FakeResult(self.skeleton)
        raise AssertionError(f"неожиданная сущность в запросе: {entity!r}")

    async def commit(self):
        self.commits += 1


def _diagram(status, error_stage=None, error_message=None):
    diagram = Diagram()
    diagram.uid = UID
    diagram.status = status
    diagram.error_stage = error_stage
    diagram.error_message = error_message
    diagram.project_code = "thermohydraulics"
    return diagram


def _skeleton():
    """Артефакт `SKELETON_FINAL` — предусловие сборки (`graph.py:68-77`)."""
    artifact = Artifact()
    artifact.diagram_uid = UID
    artifact.artifact_type = ArtifactType.SKELETON_FINAL
    return artifact


def _state(diagram):
    return (diagram.status.value, diagram.error_stage, diagram.error_message)


@pytest.fixture
def dispatched(monkeypatch):
    """Журнал отправленных задач вместо живого брокера."""
    from worker.celery_app import celery_app

    calls = []

    class _AsyncResult:
        id = "task-0001"

    def _send_task(name, args=None, kwargs=None, **rest):
        calls.append({"name": name, "args": args, "kwargs": kwargs or {}})
        return _AsyncResult()

    monkeypatch.setattr(celery_app, "send_task", _send_task)
    return calls


@pytest.fixture
def broker_down(monkeypatch):
    """Брокер лёг: `send_task` бросает — ровно то, что ловит ветка отката."""
    from worker.celery_app import celery_app

    calls = []

    def _send_task(name, args=None, kwargs=None, **rest):
        calls.append({"name": name, "args": args, "kwargs": kwargs or {}})
        raise OSError("[Errno 111] Connection refused")

    monkeypatch.setattr(celery_app, "send_task", _send_task)
    return calls


# ── сторожа самой таблицы ────────────────────────────────────────────────

def test_status_machine_size_is_locked():
    """Машина ровно того размера, на который написана таблица."""
    assert len(list(DiagramStatus)) == STATUS_COUNT


def test_table_keys_name_real_statuses():
    """Сторож набора: ключ таблицы — существующий статус, а не опечатка."""
    for value in ALLOWED | SHORTCIRCUIT:
        assert DiagramStatus(value).value == value
    for (before, _stage, _msg), (after, _s2, _m2) in ROLLBACK.items():
        assert DiagramStatus(before).value == before
        assert DiagramStatus(after).value == after


def test_rollback_table_covers_every_allowed_status():
    """До отката доходит ровно то, что пропустил гейт, — ни больше, ни меньше."""
    assert {before for before, _stage, _msg in ROLLBACK} == set(ALLOWED)


# ── POST /{uid}/build, брокер жив — полный перебор статусов ──────────────

@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_build_gate_over_every_status(status, dispatched):
    """Каждый статус машины: пропущен ровно тот, что в таблице."""
    value, stage, message = _pre_state(status)
    diagram = _diagram(status, error_stage=stage, error_message=message)
    db = FakeDB(diagram, skeleton=_skeleton())

    if value in SHORTCIRCUIT:
        result = asyncio.run(start_graph_building(UID, db=db))
        assert result["status"] == "building_graph"
        assert diagram.status is status, "короткое замыкание сдвинуло статус"
        assert db.commits == 0, "короткое замыкание закоммитило транзакцию"
        assert dispatched == [], "короткое замыкание отправило вторую задачу"
    elif value in ALLOWED:
        result = asyncio.run(start_graph_building(UID, db=db))
        assert result["status"] == "building_graph"
        assert result["task_id"] == "task-0001"
        assert diagram.status is DiagramStatus.BUILDING_GRAPH
        assert db.commits == 1
        assert [c["name"] for c in dispatched] == [BUILD_TASK]
        assert [c["args"] for c in dispatched] == [[str(UID)]]
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(start_graph_building(UID, db=db))
        assert exc.value.status_code == 400
        assert diagram.status is status, "статус сдвинут отказавшим эндпоинтом"
        assert db.commits == 0, "транзакция закоммичена при отказе"
        assert dispatched == [], "задача отправлена при отказе"


@pytest.mark.parametrize("stage", ERROR_STAGES)
def test_build_gate_on_error_ignores_stage(stage, dispatched):
    """Статус `error` пропускается при ЛЮБОЙ стадии отказа.

    Отличие от гейта детекции (`app/api/detection.py`), который решает по
    `error_stage`: здесь его не спрашивают вовсе. Разница намеренная и
    зафиксирована, чтобы правка одного гейта не подровняла второй молча.
    """
    diagram = _diagram(DiagramStatus.ERROR, error_stage=stage, error_message="boom")
    db = FakeDB(diagram, skeleton=_skeleton())

    asyncio.run(start_graph_building(UID, db=db))

    assert diagram.status is DiagramStatus.BUILDING_GRAPH
    assert diagram.error_stage is None
    assert diagram.error_message is None
    assert [c["name"] for c in dispatched] == [BUILD_TASK]


# ── POST /{uid}/build, брокер лёг — таблица отката ───────────────────────

@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_build_rollback_over_every_status(status, broker_down):
    """Отказ отправки: у пропущенных статусов — откат из таблицы, у прочих 400."""
    value, stage, message = _pre_state(status)
    diagram = _diagram(status, error_stage=stage, error_message=message)
    db = FakeDB(diagram, skeleton=_skeleton())

    if value in SHORTCIRCUIT:
        asyncio.run(start_graph_building(UID, db=db))
        assert _state(diagram) == (value, stage, message)
        assert broker_down == [], "короткое замыкание дошло до брокера"
    elif value in ALLOWED:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(start_graph_building(UID, db=db))
        assert exc.value.status_code == 503
        assert _state(diagram) == ROLLBACK[(value, stage, message)]
        assert db.commits == 2, "переход и откат — два коммита"
        assert [c["name"] for c in broker_down] == [BUILD_TASK]
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(start_graph_building(UID, db=db))
        assert exc.value.status_code == 400
        assert _state(diagram) == (value, stage, message)
        assert db.commits == 0
        assert broker_down == []


@pytest.mark.parametrize("entry", sorted(ROLLBACK), ids=lambda e: e[0])
def test_second_build_after_dead_broker(entry, broker_down):
    """Что остаётся оператору после отката: повторная «Сборка схемы».

    Тупик пункта 1.14 при исходной редакции: откат оставляет `validated_masks`,
    гейт его не знает — повтор отвечает 400, и так навсегда. Вперёд из этого
    статуса не ведёт ничто: `/junctions/start` и `/junctions/complete` его не
    принимают, а единственный принимающий эндпоинт `/api/skeleton/{uid}/skeletonize`
    из клиента не вызывается ни разу (`api_client.start_skeletonization` без
    единого вызывающего — родня находки 1.3 про `skip_frame_removal`).
    """
    value, stage, message = entry
    diagram = _diagram(DiagramStatus(value), error_stage=stage, error_message=message)
    db = FakeDB(diagram, skeleton=_skeleton())

    with pytest.raises(HTTPException) as first:
        asyncio.run(start_graph_building(UID, db=db))
    assert first.value.status_code == 503

    rolled_back, _stage, _msg = ROLLBACK[entry]
    assert diagram.status is DiagramStatus(rolled_back)

    with pytest.raises(HTTPException) as second:
        asyncio.run(start_graph_building(UID, db=db))
    assert second.value.status_code == 400, "повтор после отката больше не отказ"


@pytest.mark.parametrize("entry", sorted(ROLLBACK), ids=lambda e: e[0])
def test_rolled_back_status_leaves_graph_button_dead(entry):
    """Второй замок тупика — кнопка «Сборка схемы» после отката.

    Инвариант ДАННЫХ, без живого виджета: `_buttons_for_status` считает доступность
    по порогам `_BEAD_DEFS`. При исходной редакции откат уводит диаграмму ниже
    порога кнопки, и до 400 оператор даже не добирается — кнопка серая.
    """
    from ui.widgets.diagram_workspace import _buttons_for_status

    rolled_back, _stage, _msg = ROLLBACK[entry]
    available, completed, processing = _buttons_for_status(DiagramStatus(rolled_back))

    assert "graph" not in available
    assert "graph" not in completed
    assert "graph" not in processing


# ── прочие ветки того же эндпоинта ───────────────────────────────────────

def test_missing_skeleton_auto_dispatches_and_keeps_status(dispatched):
    """Нет `SKELETON_FINAL` — запускается скелетизация, статус не трогается."""
    diagram = _diagram(DiagramStatus.VALIDATED_JUNCTIONS)
    db = FakeDB(diagram, skeleton=None)

    result = asyncio.run(start_graph_building(UID, db=db))

    assert result["status"] == "skeletonizing"
    assert diagram.status is DiagramStatus.VALIDATED_JUNCTIONS
    assert db.commits == 0
    assert [c["name"] for c in dispatched] == [SKELETON_TASK]


def test_missing_skeleton_survives_dead_broker(broker_down):
    """Та же ветка при мёртвом брокере: 200, статус цел (исключение проглочено)."""
    diagram = _diagram(DiagramStatus.VALIDATED_JUNCTIONS)
    db = FakeDB(diagram, skeleton=None)

    result = asyncio.run(start_graph_building(UID, db=db))

    assert result["status"] == "skeletonizing"
    assert diagram.status is DiagramStatus.VALIDATED_JUNCTIONS
    assert db.commits == 0


def test_missing_diagram_is_404(dispatched):
    """Нет диаграммы — 404, а не 400 гейта."""
    db = FakeDB(None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(start_graph_building(UID, db=db))
    assert exc.value.status_code == 404
    assert dispatched == []

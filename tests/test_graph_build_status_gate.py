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
откатывал статус в `validated_masks`, которого НЕТ в его же `allowed_statuses`
(`graph.py:54-58`). Дальше оператор был заперт дважды: повторный `/build` отвечал 400,
а кнопка «Сборка схемы» при этом статусе даже не нажимается — её порог доступности
`validated_junctions` (индекс 15) против индекса 9 у отката. Оба замка проверяются
здесь, второй — на инварианте ДАННЫХ (`_buttons_for_status`), без живого виджета.

Пункт 1.14 переписывает в этой таблице ровно три клетки отката: состояние ДО
вызова возвращается целиком — статус и оба поля ошибки. Возврата одного статуса
мало: вход `error` вернулся бы с пустым `error_stage`, а на таком сочетании клиент
гасит ВСЕ кнопки (`_error_key`), то есть правка завела бы новый тупик класса 1.12.
Обе редакции таблицы лежат рядом (`ROLLBACK_BEFORE` и `ROLLBACK`), и их разницу
стережёт отдельный тест.

Числа абсолютные, ожидания — литералы. `ALLOWED` и `ROLLBACK` не вычисляются из
проверяемого кода: иначе набор остался бы зелёным при любом его значении
(`PROTOCOL §3`). Порог заперт с двух сторон — и «пропускает то, что должен», и
«не пропускает ничего сверх»: каждый отказ дополнительно утверждает, что статус
не сдвинут, транзакция не коммитилась и задача не отправлялась.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app.api.graph import GRAPH_BUILD_TIME_LIMIT_SECONDS, start_graph_building
from app.models import Artifact, ArtifactType, Diagram, DiagramStatus
from app.models.stage import ProcessingStage, StageStatus, StageType

UID = uuid.UUID("d74eb9f1-aaaa-bbbb-cccc-ddddeeeeffff")
BUILD_TASK = "worker.tasks.graph.task_build_graph"
SKELETON_TASK = "worker.tasks.skeleton.task_skeletonize_simple"

# Размер машины. Абсолютное число: новый статус обязан пройти через эту таблицу,
# а не проскочить мимо неё молча.
STATUS_COUNT = 31

# Б13 (блок 2 плана точечных болей 2026-08-25): предел возраста живой строки
# сборки. Абсолютные числа, снятые ЧТЕНИЕМ `worker/tasks/graph.py`
# (`time_limit=1800`), а не из проверяемой константы — вычисленный вход
# остался бы зелёным при любом её значении (`PROTOCOL §3`). Порог заперт
# с двух сторон: молодая строка блокирует, старая — нет.
BUILD_TIME_LIMIT = 1800
FRESH_AGE = 60      # строка живая: воркер считает
STALE_AGE = 5400    # строка-сирота: воркер убит hard limit'ом, `fail()` не исполнился

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
    "skeletonizing_final",
    "detecting_junctions",
    "generating_fxml",
    "ocr",
]

# Откат при мёртвом брокере. Ключ — состояние ДО вызова, значение — состояние
# ПОСЛЕ отказа отправки: (status, error_stage, error_message). Перечислены все
# входы из `ALLOWED` — других путей до отката нет.
#
# ДО пункта 1.14 (зафиксировано коммитом 4feb0d6, 83 теста зелёные на нетронутом
# коде). Читается так: чем бы диаграмма ни была, отказ отправки сваливает её
# в один и тот же `validated_masks` и стирает поля ошибки.
ROLLBACK_BEFORE = {
    ("validated_junctions", None, None): ("validated_masks", None, None),
    ("built", None, None): ("validated_masks", None, None),
    ("error", "building_graph", "boom"): ("validated_masks", None, None),
}

# ПОСЛЕ пункта 1.14: откат возвращает состояние, каким оно было до вызова —
# работа не начиналась, откатывать некуда, кроме исходной точки. Разница
# с прежней редакцией обязана быть ровно в этих трёх клетках — сторож ниже.
ROLLBACK = {
    ("validated_junctions", None, None): ("validated_junctions", None, None),
    ("built", None, None): ("built", None, None),
    ("error", "building_graph", "boom"): ("error", "building_graph", "boom"),
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

    Ответ выбирается по СУЩНОСТИ запроса, а не по порядку вызовов: порядок трёх
    `select` внутри эндпоинта — не контракт, и тест не должен за него держаться.
    """

    def __init__(self, diagram, skeleton=None, build_stage=None):
        self.diagram = diagram
        self.skeleton = skeleton
        self.build_stage = build_stage
        self.commits = 0

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Diagram:
            return _FakeResult(self.diagram)
        if entity is Artifact:
            return _FakeResult(self.skeleton)
        if entity is ProcessingStage:
            return _FakeResult(self.build_stage)
        raise AssertionError(f"неожиданная сущность в запросе: {entity!r}")

    async def commit(self):
        self.commits += 1


def _build_stage(age_seconds):
    """RUNNING-строка сборки, начатая `age_seconds` секунд назад.

    Возраст задаётся АБСОЛЮТНЫМ числом от `utcnow` — тем же наивным UTC, каким
    его пишет `ProcessingStage.start`. Тест не вычисляет его из порога кода:
    вычисленный вход остался бы зелёным при любом значении порога
    (`PROTOCOL §3`).
    """
    stage = ProcessingStage()
    stage.id = 4242
    stage.diagram_uid = UID
    stage.stage_type = StageType.GRAPH_BUILDING
    stage.status = StageStatus.RUNNING
    stage.started_at = datetime.utcnow() - timedelta(seconds=age_seconds)
    return stage


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
    for table in (ROLLBACK, ROLLBACK_BEFORE):
        for (before, _stage, _msg), (after, _s2, _m2) in table.items():
            assert DiagramStatus(before).value == before
            assert DiagramStatus(after).value == after


def test_rollback_table_covers_every_allowed_status():
    """До отката доходит ровно то, что пропустил гейт, — ни больше, ни меньше."""
    assert {before for before, _stage, _msg in ROLLBACK} == set(ALLOWED)


def test_rollback_changed_by_exactly_the_declared_cells():
    """Пункт 1.14 переписал ровно три клетки отката и не завёл ни одной новой.

    Обе редакции — независимые литералы, поэтому правка одной без другой краснит
    этот сторож: «переход вне зафиксированного набора» (`PROTOCOL §Гейты`)
    ловится здесь, а не глазами ревизора.
    """
    assert set(ROLLBACK) == set(ROLLBACK_BEFORE), "у отката появился новый вход"

    changed = {key for key in ROLLBACK if ROLLBACK[key] != ROLLBACK_BEFORE[key]}
    assert changed == set(ROLLBACK_BEFORE), "изменились не все три клетки"

    for key, value in ROLLBACK_BEFORE.items():
        assert value == ("validated_masks", None, None)
    for key, value in ROLLBACK.items():
        assert value == key, "откат обязан вернуть пред-вызовное состояние целиком"


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
def test_second_build_after_dead_broker(entry, broker_down, monkeypatch):
    """Выход из отката: брокер поднялся — «Сборка схемы» работает.

    Тупик пункта 1.14 при исходной редакции: откат оставлял `validated_masks`,
    гейт его не знает — повтор отвечал 400, и так навсегда. Вперёд из того статуса
    не вело ничто: `/junctions/start` и `/junctions/complete` его не принимают,
    а единственный принимающий эндпоинт `/api/skeleton/{uid}/skeletonize` из
    клиента не вызывается ни разу (`api_client.start_skeletonization` без единого
    вызывающего — родня находки 1.3 про `skip_frame_removal`).
    """
    value, stage, message = entry
    diagram = _diagram(DiagramStatus(value), error_stage=stage, error_message=message)
    db = FakeDB(diagram, skeleton=_skeleton())

    with pytest.raises(HTTPException) as first:
        asyncio.run(start_graph_building(UID, db=db))
    assert first.value.status_code == 503
    assert _state(diagram) == ROLLBACK[entry]

    # Брокер поднялся — оператор жмёт кнопку ещё раз.
    from worker.celery_app import celery_app

    sent = []

    class _AsyncResult:
        id = "task-0002"

    def _send_task(name, args=None, kwargs=None, **rest):
        sent.append(name)
        return _AsyncResult()

    monkeypatch.setattr(celery_app, "send_task", _send_task)

    result = asyncio.run(start_graph_building(UID, db=db))

    assert result["status"] == "building_graph"
    assert result["task_id"] == "task-0002"
    assert sent == [BUILD_TASK]
    assert diagram.status is DiagramStatus.BUILDING_GRAPH


@pytest.mark.parametrize("entry", sorted(ROLLBACK), ids=lambda e: e[0])
def test_rolled_back_status_keeps_graph_button_alive(entry):
    """Второй замок тупика — кнопка «Сборка схемы» после отката.

    Инвариант ДАННЫХ, без живого виджета. При исходной редакции откат уводил
    диаграмму ниже порога кнопки (`validated_masks` — индекс 9 против порога
    `validated_junctions` — 15), и до 400 оператор даже не добирался: кнопка серая.
    """
    from ui.widgets.diagram_workspace import _buttons_for_status

    rolled_back, stage, _msg = ROLLBACK[entry]
    available, completed, processing = _buttons_for_status(DiagramStatus(rolled_back))

    if rolled_back == "error":
        # У `error` порогов нет вовсе (его нет в `_STATUS_ORDER`) — кнопку красит
        # `error_stage` через `_error_key`. Поэтому здесь судится он: пустой
        # `error_stage` гасит В КЛИЕНТЕ все кнопки разом, и это был бы новый
        # тупик класса 1.12, заведённый самой правкой.
        assert stage == "building_graph"
        assert (available, completed, processing) == (set(), set(), set())
    else:
        assert "graph" in (available | completed), "кнопка «Сборка схемы» серая"


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


@pytest.mark.parametrize("status", [
    DiagramStatus.VALIDATED_JUNCTIONS, DiagramStatus.BUILT, DiagramStatus.ERROR,
], ids=lambda s: s.value)
def test_missing_skeleton_does_not_lie_about_a_dead_broker(status, broker_down):
    """Та же ветка при мёртвом брокере: 503, а не «skeletonizing» из ниоткуда.

    ⛔ Прежняя редакция этого теста запирала дефект как норму: «200, статус цел
    (исключение проглочено)». Замер ноги 1.16 (§99) показал, чем это было на
    самом деле — ответ «skeletonizing» при НУЛЕ поставленных задач во всех трёх
    допустимых статусах, а клиент (`diagram_workspace._start_graph_build`)
    печатал «📊 Построение графа запущено» и уходил ждать.

    Статус тут не меняется ни до, ни после правки — работа не начиналась, значит
    и возвращать нечего: чинится только правда ответа. Перебор по ВСЕМ трём
    статусам, а не по одному: ветка одна, но достижима из каждого.
    """
    diagram = _diagram(
        status,
        error_stage="building_graph" if status is DiagramStatus.ERROR else None,
        error_message="boom" if status is DiagramStatus.ERROR else None,
    )
    db = FakeDB(diagram, skeleton=None)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(start_graph_building(UID, db=db))

    assert exc.value.status_code == 503
    assert "Connection refused" in exc.value.detail, "причина отказа потеряна"
    assert _state(diagram) == _pre_state(status), "состояние тронуто, а работы не было"
    assert db.commits == 0


def test_missing_skeleton_dead_broker_leaves_a_trace(broker_down):
    """Д2 у той же ветки: след с `uid` есть и называет статус, на котором стоим.

    Метка фазы поднята в начало эндпоинта именно ради этой ветки — до правки
    `obs.bind` стоял ниже, и строка ушла бы без `uid`.
    """
    import logging

    from app.core.logging import ContextFilter

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.records = []
            self.addFilter(ContextFilter())

        def emit(self, record):
            self.records.append(record)

    handler = _Capture()
    api_logger = logging.getLogger("app.api.graph")
    previous_level = api_logger.level
    api_logger.addHandler(handler)
    api_logger.setLevel(logging.DEBUG)
    try:
        db = FakeDB(_diagram(DiagramStatus.VALIDATED_JUNCTIONS), skeleton=None)
        with pytest.raises(HTTPException):
            asyncio.run(start_graph_building(UID, db=db))
    finally:
        api_logger.removeHandler(handler)
        api_logger.setLevel(previous_level)

    trace = [r for r in handler.records if getattr(r, "event", None) == "dispatch_failed"]
    assert len(trace) == 1, "отказ авто-скелетизации не оставил следа"
    assert trace[0].uid == str(UID)
    assert trace[0].phase == "graph_build"
    assert "validated_junctions" in trace[0].getMessage()


def test_dead_broker_leaves_a_trace_with_uid(broker_down):
    """Д2: отказ отправки больше не молчит — строка в логе с `uid` и точкой возврата.

    До пункта 1.14 сервер не оставлял об этом ничего: 503 уходил клиенту, а
    диаграмма тихо оказывалась в статусе, из которого нет выхода. `uid` судится
    настоящим механизмом — `ContextFilter` тянет его из `obs.bind`, — и фильтр
    обязан отработать ВНУТРИ задачи: `asyncio.run` копирует контекст, и наружу
    его правки не возвращаются.
    """
    import logging

    from app.core.logging import ContextFilter

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.records = []
            self.addFilter(ContextFilter())

        def emit(self, record):
            self.records.append(record)

    handler = _Capture()
    api_logger = logging.getLogger("app.api.graph")
    previous_level = api_logger.level
    api_logger.addHandler(handler)
    api_logger.setLevel(logging.DEBUG)
    try:
        db = FakeDB(_diagram(DiagramStatus.VALIDATED_JUNCTIONS), skeleton=_skeleton())
        with pytest.raises(HTTPException):
            asyncio.run(start_graph_building(UID, db=db))
    finally:
        api_logger.removeHandler(handler)
        api_logger.setLevel(previous_level)

    trace = [r for r in handler.records if getattr(r, "event", None) == "dispatch_failed"]
    assert len(trace) == 1, "отказ отправки не оставил следа"
    assert trace[0].uid == str(UID)
    assert trace[0].phase == "graph_build"
    assert "validated_junctions" in trace[0].getMessage(), "точка возврата не названа"


def test_missing_diagram_is_404(dispatched):
    """Нет диаграммы — 404, а не 400 гейта."""
    db = FakeDB(None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(start_graph_building(UID, db=db))
    assert exc.value.status_code == 404
    assert dispatched == []


# ── Б13: заслон дубля сборки ─────────────────────────────────────────────

def test_declared_time_limit_matches_the_task():
    """Предел возраста — `time_limit` самой задачи, и это сверяется, а не верится.

    Литерал таблицы независим от проверяемой константы; здесь единственное
    место, где они сходятся, плюс сверка с декоратором задачи — иначе подъём
    `time_limit` у воркера молча оставил бы заслон с прежним порогом.
    """
    import re
    from pathlib import Path

    assert GRAPH_BUILD_TIME_LIMIT_SECONDS == BUILD_TIME_LIMIT
    assert FRESH_AGE < BUILD_TIME_LIMIT < STALE_AGE, "порог не заперт с двух сторон"

    src = (Path(__file__).resolve().parents[1] / "worker" / "tasks" / "graph.py"
           ).read_text(encoding="utf-8")
    head = src.split("def task_build_graph")[0]
    limits = re.findall(r"time_limit=(\d+)", head)
    assert str(BUILD_TIME_LIMIT) in limits, (
        f"time_limit задачи разошёлся с заслоном: {limits}")


@pytest.mark.parametrize("entry", sorted(ALLOWED))
def test_running_build_blocks_the_second_dispatch(entry, dispatched):
    """Сборка БЕЖИТ — второй задачи нет ни из одного пропускаемого статуса.

    Клетка не покрывается идемпотентным выходом по статусу: `built` и `error`
    он не знает вовсе, а именно из них оператор жмёт «Сборка схемы» повторно,
    пока первая копия ещё считает (замер 1-23 — 7 таких пар на 3 uid).
    """
    value, stage, message = _pre_state(DiagramStatus(entry))
    diagram = _diagram(DiagramStatus(entry), stage, message)
    db = FakeDB(diagram, _skeleton(), build_stage=_build_stage(FRESH_AGE))

    result = asyncio.run(start_graph_building(UID, db=db))

    assert result["message"] == "Graph building already in progress"
    assert result["status"] == value, "заслон соврал про статус диаграммы"
    assert _state(diagram) == (value, stage, message), "заслон сдвинул состояние"
    assert db.commits == 0, "заслон закоммитил транзакцию"
    assert dispatched == [], "заслон всё-таки отправил вторую сборку"


@pytest.mark.parametrize("entry", sorted(ALLOWED))
def test_stale_running_row_does_not_block_forever(entry, dispatched):
    """Строка старше `time_limit` — сирота, и пересборку она НЕ запирает.

    Прецедент `layout_dispatch` предела возраста не знает: любая из вечных
    RUNNING-строк (воркер убит hard limit'ом или OOM — `stage.fail()` не
    исполнился) блокировала бы навсегда, а снять её нечем — откат стадий
    не трогает.
    """
    diagram = _diagram(DiagramStatus(entry), *_pre_state(DiagramStatus(entry))[1:])
    db = FakeDB(diagram, _skeleton(), build_stage=_build_stage(STALE_AGE))

    result = asyncio.run(start_graph_building(UID, db=db))

    assert result["status"] == "building_graph"
    assert diagram.status is DiagramStatus.BUILDING_GRAPH
    assert db.commits == 1
    assert [c["name"] for c in dispatched] == [BUILD_TASK]


def test_running_row_without_started_at_does_not_block(dispatched):
    """RUNNING без `started_at` судить по возрасту нечем — и он не блокирует.

    От вечной такая строка неотличима, а вечная запирать пересборку не вправе.
    """
    diagram = _diagram(DiagramStatus.BUILT)
    stage = _build_stage(FRESH_AGE)
    stage.started_at = None
    db = FakeDB(diagram, _skeleton(), build_stage=stage)

    result = asyncio.run(start_graph_building(UID, db=db))

    assert result["status"] == "building_graph"
    assert [c["name"] for c in dispatched] == [BUILD_TASK]


def test_finished_build_does_not_block(dispatched):
    """Строка есть, но не RUNNING — запрос её не находит, сборка ставится.

    Судится САМ запрос: фильтр по статусу стоит в `where`, поэтому подделка
    отдаёт None ровно тогда, когда его отдал бы настоящий `AsyncSession`.
    """
    diagram = _diagram(DiagramStatus.BUILT)
    db = FakeDB(diagram, _skeleton(), build_stage=None)

    result = asyncio.run(start_graph_building(UID, db=db))

    assert result["status"] == "building_graph"
    assert [c["name"] for c in dispatched] == [BUILD_TASK]


def test_blocked_build_keeps_the_button_alive(dispatched):
    """Инвариант ДАННЫХ: после заслона кнопка «Сборка схемы» не гаснет.

    Состояние не сдвинуто, значит и кнопка осталась той же — иначе заслон
    завёл бы тупик класса 1.13 на пустом месте.
    """
    from ui.widgets.diagram_workspace import _buttons_for_status

    diagram = _diagram(DiagramStatus.BUILT)
    db = FakeDB(diagram, _skeleton(), build_stage=_build_stage(FRESH_AGE))
    asyncio.run(start_graph_building(UID, db=db))

    available, completed, processing = _buttons_for_status(diagram.status)
    assert "graph" not in processing
    assert "graph" in available or "graph" in completed

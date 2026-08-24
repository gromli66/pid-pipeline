# -*- coding: utf-8 -*-
"""Завершение валидации: таблица исходов отправки задачи (пункт 1-13, нога 1.16 дороги).

Зачем. Класс правки — [сма]: `PROTOCOL §Гейты` требует СНАЧАЛА зафиксировать текущие
переходы и только потом править. Серверного покрытия у этих четырёх эндпоинтов нет
вовсе: греп `complete_mask_validation` / `complete_junction_validation` /
`complete_simple_graph_validation` / `complete_graph_validation` по `tests/` даёт только
клиентские наборы (`tests/ui/test_masks_completion_once.py`,
`tests/ui/test_workspace_graph_flow.py`), которые поднимают СВОЙ поддельный сервер и
о настоящих корутинах не знают ничего.

Что проверяется. Четыре эндпоинта завершения валидации:

    POST /api/validation/{uid}/masks/complete          — complete_mask_validation
    POST /api/validation/{uid}/junctions/complete      — complete_junction_validation
    POST /api/validation/{uid}/graph/complete-simple   — complete_simple_graph_validation
    POST /api/validation/{uid}/graph/complete          — complete_graph_validation

Каждый — по ПОЛНОЙ решётке: 31 статус × 11 значений `error_stage` = 341 клетка, и в ДВУХ
ветках: брокер жив и брокер лёг. Судятся настоящие корутины: `AsyncSession` подделана
(её поверхность здесь — `execute` + `commit` + `add` + `flush`), `send_task` подменён,
живой брокер, БД и файлы не нужны.

Дефект, ради которого таблица снята (нога 1.16). Все пять точек зовут
`async_safe_dispatch`, который при отказе брокера ГЛОТАЕТ исключение и возвращает `None`,
и ни одна из пяти этот `None` не проверяет (шестая точка, `layout_dispatch.py:260`,
проверяет — и ставит стадию `FAILED`). Статус к этому моменту уже закоммичен ВПЕРЁД,
поэтому оператор получает **200 с `task_id: null`** — тот же ответ, что при идемпотентном
«цепочка уже ушла вперёд», — и диаграмму, из которой задача больше никогда не выйдет.
Клиент этот `null` читает буквально: `diagram_workspace.py:1592-1595` печатает
«✅ Валидация масок завершена (цепочка уже запущена)», а `:2378` после 200 сам красит
`GENERATING_FXML` и уходит в опрос. Второй замок — кнопки: ни `validated_masks`, ни
`generating_fxml` не лежат в `_MANUAL_INPROGRESS`, и `_buttons_for_status` не даёт при них
НИ ОДНОЙ доступной кнопки (см. `BUTTONS_AFTER_FAILURE_BEFORE`).

Числа абсолютные, ожидания — литералы: `ACCEPTS`, `ALREADY_PAST`, `TASKS`, `FAILURE_BEFORE`,
`BUTTONS_*` сняты ЧТЕНИЕМ кода и ИСПОЛНЕНИЕМ, а не вычислены из проверяемого
(`PROTOCOL §3` — вычисленное ожидание осталось бы зелёным при любом значении).

Словарь `error_stage` здесь тот же, что в `tests/test_stage_dispatch_failure_gate.py`
(нога 1.13); сторож «конвейер не пишет стадию мимо таблицы» живёт там и не дублируется.
"""
import asyncio
import inspect
import logging
import time
import uuid

import pytest
from fastapi import HTTPException
from kombu.exceptions import OperationalError

import app.api.validation as validation_api
from app.api.validation import (
    complete_graph_validation,
    complete_junction_validation,
    complete_mask_validation,
    complete_simple_graph_validation,
)
from app.core.logging import ContextFilter
from app.models import Artifact, ArtifactType, Diagram, DiagramStatus

UID = uuid.UUID("c1a55e77-1111-2222-3333-444455556666")

# Размер машины. Абсолютное число: новый статус обязан пройти через эту таблицу,
# а не проскочить мимо неё молча. Доки пишут 29 — по коду 31 (адрес починки — 0-3).
STATUS_COUNT = 31

# Полный словарь `error_stage`, который пишет конвейер (единственный производитель —
# `worker/utils/db_helpers.set_diagram_error`). Список дословно совпадает с ногой 1.13.
ERROR_STAGES = [
    None,
    "detecting",
    "direction_classification",
    "segmenting",
    "skeletonizing",
    "skeletonizing_simple",
    "detecting_junctions",
    "building_graph",
    "generating_fxml",
    "ocr",
    "contour_extraction",
]

SKELETON_TASK = "worker.tasks.skeleton.task_skeletonize_simple"
GRAPH_TASK = "worker.tasks.graph.task_build_graph"
OCR_TASK = "worker.tasks.ocr.task_run_ocr"
FXML_TASK = "worker.tasks.graph.task_generate_fxml"

ENDPOINTS = ("masks", "junctions", "simple", "complete")

# ── гейт статуса: какие статусы эндпоинт вообще пускает ──────────────────
#
# Всё, чего в множестве нет, — 400 без единого коммита и без отправки.
ACCEPTS = {
    "masks": {
        "validating_masks", "skeletonized", "validated_masks",
        "skeletonizing_final", "skeletonized_final", "detecting_junctions",
        "detected_junctions", "built", "validated_graph", "ocr_completed",
    },
    "junctions": {
        "detected_junctions", "validating_junctions", "validated_junctions",
        "building_graph", "built",
    },
    "simple": {"validating_graph", "built", "validated_graph"},
    "complete": {
        "validating_graph", "built", "validated_graph",
        "ocr_completed", "ocr_bound",
    },
}

# ── «ушли вперёд»: гейт пустил, но перехода и отправки НЕ будет ──────────
#
# Ответ 200 с `task_id: null` — ровно тот же, каким сегодня выглядит ОТКАЗ БРОКЕРА.
# В этом и дефект ноги: два разных исхода неразличимы для клиента.
ALREADY_PAST = {
    "masks": {
        "skeletonizing_final", "skeletonized_final", "detecting_junctions",
        "detected_junctions", "validated_junctions", "building_graph", "built",
        "validated_graph", "ocr_completed",
    },
    "junctions": {"validated_junctions", "building_graph", "built"},
    "simple": set(),
    "complete": set(),
}

# ── переход: целевой статус и порядок отправляемых задач ─────────────────
TARGET = {
    "masks": "validated_masks",
    "junctions": "validated_junctions",
    "simple": "validated_graph",
    "complete": "validated_graph",
}

# `graph/complete` — единственный, у кого цель зависит от входа: возврат в «Проверку
# схемы» ПОСЛЕ контуров ведёт сразу в генерацию (плюс зовётся `dispatch_layout`).
RETURNED_AFTER_CONTOURS = {"ocr_bound", "ocr_completed"}
TARGET_AFTER_CONTOURS = "generating_fxml"

TASKS = {
    "masks": [SKELETON_TASK],
    "junctions": [GRAPH_TASK, OCR_TASK],
    "simple": [OCR_TASK],
    "complete": [FXML_TASK],
}

# Что эндпоинт КЛАДЁТ в поле `status` ответа. Литералы, а не `diagram.status`: у двух
# из четырёх ответ уже сегодня расходится с диаграммой, и таблица обязана это помнить.
#   masks     — «validated_masks» даже на ветке «ушли вперёд» (диаграмма может быть `built`);
#   complete  — «validated_graph» даже когда диаграмма ушла в `generating_fxml`.
RESPONSE_STATUS = {
    "masks": "validated_masks",
    "junctions": "validated_junctions",
    "simple": "validated_graph",
    "complete": "validated_graph",
}

# ── таблица отказа отправки ──────────────────────────────────────────────
#
# Значения — правила, а не строки статусов: правило одно на весь эндпоинт,
# и меняет его именно эта нога. Резолвятся ниже по входу и целевому статусу.
OK = "200, ложный успех: `task_id: null` неотличим от «ушли вперёд»"
TARGET_RULE = "целевой статус перехода"
ENTRY_RULE = "состояние до вызова"
CLEARED = "поля ошибки стёрты"
KEPT = "поля ошибки сохранены"

# ДО пункта: `None` не проверяет никто. Переход закоммичен (`commits=1`), задачи нет,
# оператору 200. Попытки отправки — ВСЕ, что стоят в `TASKS`: у перекрёстков вслед
# за упавшей сборкой графа уходит ещё и OCR (замер: sent = [build_graph, run_ocr]).
FAILURE_BEFORE = {
    "masks": (OK, TARGET_RULE, CLEARED, 1, 1),
    "junctions": (OK, TARGET_RULE, CLEARED, 1, 2),
    "simple": (OK, TARGET_RULE, CLEARED, 1, 1),
    "complete": (OK, TARGET_RULE, CLEARED, 1, 1),
}

# ПОСЛЕ пункта: отказ отправки ПЕРВОЙ задачи шага возвращает состояние, каким оно
# было ДО вызова, и отвечает 503 вместо ложного 200. Коммитов два: переход и возврат.
# У перекрёстков попытка ОДНА, а не две: до OCR управление уже не доходит.
# Редакция независима от `FAILURE_BEFORE` — правка одной без другой краснит сторож.
FAILURE = {
    "masks": (503, ENTRY_RULE, KEPT, 2, 1),
    "junctions": (503, ENTRY_RULE, KEPT, 2, 1),
    "simple": (503, ENTRY_RULE, KEPT, 2, 1),
    "complete": (503, ENTRY_RULE, KEPT, 2, 1),
}

# ── второй замок: кнопки оператора после отказа отправки ─────────────────
#
# Инвариант ДАННЫХ (`_buttons_for_status`), без живого виджета. Значение — множество
# ДОСТУПНЫХ кнопок (`available`): пустое означает, что нажать нечего вовсе.
BUTTONS_AFTER_FAILURE_BEFORE = {
    "masks": set(),                 # validated_masks: `junction` синяя, доступных нет
    "junctions": {"graph"},         # validated_junctions: «Сборка схемы» доступна
    "simple": {"contours"},         # validated_graph: OCR синий, вперёд можно
    "complete": {"contours"},       # прямой путь → validated_graph
}
BUTTONS_AFTER_FAILURE_BEFORE_AC = set()   # complete после контуров: generating_fxml — пусто

# ПОСЛЕ пункта состояние возвращается в точку входа, значит и кнопки — её.
# Это `BUTTONS_AT_ENTRY`; отдельной таблицей не дублируется, но факт совпадения
# утверждается тестом `test_buttons_after_failed_dispatch`.

# Достижимая точка входа для сценариев: клетка, которой оператор реально пользуется.
SCENARIO_ENTRY = {
    "masks": "validating_masks",
    "junctions": "validating_junctions",
    "simple": "validating_graph",
    "complete": "validating_graph",
}
SCENARIO_ENTRY_AC = "ocr_bound"           # `complete` на пути «после контуров»

# Кнопки в точке возврата — то, что оператор получит обратно вместе с состоянием.
BUTTONS_AT_ENTRY = {
    "masks": {"pipe"},
    "junctions": set(),             # `_MANUAL_INPROGRESS` вернёт `junction` в клиенте
    "simple": {"val_graph"},
    "complete": {"val_graph"},
}


# ── харнесс ──────────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class FakeDB:
    """Поверхность `AsyncSession`, которой пользуются все четыре эндпоинта.

    Запрос различается по сущности (`Diagram` против `Artifact`) и по значению
    `artifact_type` в параметрах — иначе артефакт-заглушка приехала бы и туда,
    где эндпоинт ждёт его ОТСУТСТВИЯ (safety-net слияния OCR).
    """

    def __init__(self, diagram, artifacts=None):
        self.diagram = diagram
        self.artifacts = artifacts if artifacts is not None else ARTIFACTS
        self.commits = 0

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Diagram:
            return _FakeResult(self.diagram)
        params = stmt.compile().params
        return _FakeResult(self.artifacts.get(params.get("artifact_type_1")))

    async def commit(self):
        self.commits += 1

    def add(self, obj):
        pass

    async def flush(self):
        pass


def _artifact(path):
    art = Artifact()
    art.file_path = path
    art.file_size = 1
    art.mime_type = "image/png"
    return art


# Все validated-маски и graph_validated на месте: копирования файлов не будет,
# гейты артефактов пройдены. `OCR_RESULT` намеренно отсутствует — иначе
# `complete-simple` полез бы сливать OCR в граф с диска.
ARTIFACTS = {
    ArtifactType.PIPE_MASK_VALIDATED: _artifact("pipe_mask_validated.png"),
    ArtifactType.JUNCTION_MASK_VALIDATED: _artifact("junction_mask_validated.png"),
    ArtifactType.BRIDGE_MASK_VALIDATED: _artifact("bridge_mask_validated.png"),
    ArtifactType.GRAPH_VALIDATED: _artifact("graph_validated.json"),
    ArtifactType.OCR_RESULT: None,
}

CALL = {
    "masks": lambda db: complete_mask_validation(UID, db=db),
    "junctions": lambda db: complete_junction_validation(UID, db=db),
    "simple": lambda db: complete_simple_graph_validation(UID, db=db),
    "complete": lambda db: complete_graph_validation(UID, db=db),
}


def _diagram(status, error_stage=None, error_message=None):
    diagram = Diagram()
    diagram.uid = UID
    diagram.status = status
    diagram.error_stage = error_stage
    diagram.error_message = error_message
    diagram.project_code = "thermohydraulics"
    return diagram


def _state(diagram):
    return (diagram.status.value, diagram.error_stage, diagram.error_message)


def _entry_state(status, stage):
    """Состояние ДО вызова: поля ошибки заполнены только у `error`.

    Правило фиксированное и от проверяемого кода не зависит: единственный
    производитель `error_stage` — `set_diagram_error`, и он всегда пишет его
    ВМЕСТЕ со статусом `error`.
    """
    if status is DiagramStatus.ERROR:
        return (status.value, stage, "boom")
    return (status.value, stage, None)


def _target(endpoint, status_value):
    """Целевой статус перехода для клетки."""
    if endpoint == "complete" and status_value in RETURNED_AFTER_CONTOURS:
        return TARGET_AFTER_CONTOURS
    return TARGET[endpoint]


def _dispatches(endpoint, status_value):
    """Что эндпоинт делает на клетке: список задач или None — перехода нет."""
    if status_value not in ACCEPTS[endpoint]:
        return None
    if status_value in ALREADY_PAST[endpoint]:
        return []
    return list(TASKS[endpoint])


@pytest.fixture
def ocr_enabled(monkeypatch):
    """OCR включён — как на бою (`thermohydraulics.yaml: ocr.enabled: true`).

    Подмена здесь ОБЯЗАТЕЛЬНА, а не для удобства: `tests/conftest.py` уводит
    `PROJECTS_CONFIG_DIR` на `/tmp/test_configs`, поэтому под pytest боевой
    конфиг не читается ВООБЩЕ и `_pc_ocr` всегда `None` — ветка OCR оказалась бы
    непройденной, а решётка судила бы половину эндпоинта. Что боевой конфиг
    действительно говорит `enabled: true`, проверяет отдельный тест ниже —
    чтением файла репозитория мимо `settings`.
    """
    import app.services.project_loader as project_loader

    class _Ocr:
        enabled = True

    class _Config:
        ocr = _Ocr()

    class _Loader:
        def load(self, code):
            return _Config()

    monkeypatch.setattr(project_loader, "get_project_loader", lambda: _Loader())


@pytest.fixture
def no_layout(monkeypatch):
    """`dispatch_layout` — чужая подсистема (её исход судит `layout_dispatch`).

    Здесь она заглушена: пункт про то, что делает САМ эндпоинт с отказом
    отправки FXML, а не про идемпотентность раскладки.
    """
    calls = []

    async def _stub(uid, db):
        calls.append(str(uid))
        return {"status": "stub"}

    monkeypatch.setattr(validation_api, "dispatch_layout", _stub)
    return calls


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
    """Брокер лёг: `send_task` бросает — ровно то, что происходит на бою.

    Здесь берётся `OSError` — тот же класс исхода для вызывающего. Настоящие
    исключения проверяются отдельным тестом ниже, и их ДВА: какое прилетит,
    решает то, что именно мертво (замер §87д/§91), — см. его докстроку.
    """
    from worker.celery_app import celery_app

    calls = []

    def _send_task(name, args=None, kwargs=None, **rest):
        calls.append({"name": name, "args": args, "kwargs": kwargs or {}})
        raise OSError("[Errno 111] Connection refused")

    monkeypatch.setattr(celery_app, "send_task", _send_task)
    return calls


# ── сторожа самих таблиц ─────────────────────────────────────────────────

def test_status_machine_size_is_locked():
    """Машина ровно того размера, на который написана таблица."""
    assert len(list(DiagramStatus)) == STATUS_COUNT


def test_table_keys_name_real_statuses():
    """Ключ и значение таблицы — существующий статус, а не опечатка."""
    for endpoint in ENDPOINTS:
        for value in ACCEPTS[endpoint] | ALREADY_PAST[endpoint]:
            assert DiagramStatus(value).value == value, (endpoint, value)
        assert DiagramStatus(TARGET[endpoint]).value == TARGET[endpoint]
    for value in RETURNED_AFTER_CONTOURS:
        assert DiagramStatus(value).value == value
    assert DiagramStatus(TARGET_AFTER_CONTOURS).value == TARGET_AFTER_CONTOURS


def test_every_endpoint_has_a_rule():
    """Ни один эндпоинт таблицы не остался без объявленного исхода."""
    for table in (ACCEPTS, ALREADY_PAST, TARGET, TASKS, RESPONSE_STATUS,
                  FAILURE_BEFORE, BUTTONS_AFTER_FAILURE_BEFORE,
                  SCENARIO_ENTRY, BUTTONS_AT_ENTRY, CALL):
        assert set(table) == set(ENDPOINTS)


def test_already_past_is_narrower_than_the_gate():
    """«Ушли вперёд» без пропуска гейтом недостижимо — клетка мёртвая.

    У масок таких две (`validated_junctions`, `building_graph`): гейт их не пускает,
    поэтому до проверки «ушли вперёд» они не доходят. Тест называет это числом,
    чтобы решётка не выглядела противоречивой.
    """
    dead = {e: ALREADY_PAST[e] - ACCEPTS[e] for e in ENDPOINTS}
    assert dead["masks"] == {"validated_junctions", "building_graph"}
    assert dead["junctions"] == set()
    assert dead["simple"] == set()
    assert dead["complete"] == set()


def test_error_stage_vocabulary_size():
    """Словарь стадий — тот же, что у ноги 1.13 (сторож производителя живёт там)."""
    assert len(ERROR_STAGES) == 11
    assert ERROR_STAGES[0] is None


def test_dispatch_failure_changed_by_exactly_the_declared_cells():
    """Нога 1.16 переписала исход отказа у ВСЕХ ЧЕТЫРЁХ и ничего сверх того.

    Обе редакции — независимые литералы, поэтому правка одной без другой
    краснит этот сторож: «переход вне зафиксированного набора»
    (`PROTOCOL §Гейты`) ловится здесь, а не глазами ревизора.
    """
    changed = {key for key in FAILURE if FAILURE[key] != FAILURE_BEFORE[key]}
    assert changed == set(ENDPOINTS)

    for key, value in FAILURE_BEFORE.items():
        assert value[:4] == (OK, TARGET_RULE, CLEARED, 1), key
    for key, value in FAILURE.items():
        assert value == (503, ENTRY_RULE, KEPT, 2, 1), key

    # Число попыток отправки изменилось РОВНО у перекрёстков: до правки за упавшей
    # сборкой графа уходил ещё и OCR, теперь до него управление не доходит.
    assert {k for k in FAILURE if FAILURE[k][4] != FAILURE_BEFORE[k][4]} == {"junctions"}


def test_button_table_changed_at_both_dead_ends():
    """Кнопки вернулись там, где их не было: у масок и у пути «после контуров».

    У двух других эндпоинтов множество доступных кнопок меняется тоже (вперёд
    против точки входа), но тупиком они не были — это записано отдельно, чтобы
    «стало лучше» не перепутали с «было сломано».
    """
    dead_ends = {e for e in ENDPOINTS if not BUTTONS_AFTER_FAILURE_BEFORE[e]}
    assert dead_ends == {"masks"}
    assert BUTTONS_AFTER_FAILURE_BEFORE_AC == set()
    assert BUTTONS_AT_ENTRY["masks"] == {"pipe"}, "возврат не вернул кнопку"


# ── брокер жив: полная решётка 31 × 11 на каждый эндпоинт ────────────────

@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_completion_over_every_status(endpoint, status, dispatched,
                                      ocr_enabled, no_layout):
    """Каждая клетка решётки: пропущена ровно та, что в таблице.

    Порог заперт с двух сторон: отказ дополнительно утверждает, что статус
    не сдвинут, транзакция не коммитилась и задача не отправлялась.
    """
    for stage in ERROR_STAGES:
        dispatched.clear()
        entry = _entry_state(status, stage)
        diagram = _diagram(status, entry[1], entry[2])
        db = FakeDB(diagram)
        cell = (endpoint, status.value, stage)
        tasks = _dispatches(endpoint, status.value)

        if tasks is None:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(CALL[endpoint](db))
            assert exc.value.status_code == 400, cell
            assert _state(diagram) == entry, cell
            assert db.commits == 0, cell
            assert dispatched == [], cell
            continue

        result = asyncio.run(CALL[endpoint](db))
        assert result["status"] == RESPONSE_STATUS[endpoint], cell
        assert result["uid"] == str(UID), cell
        assert [c["name"] for c in dispatched] == tasks, cell

        if not tasks:                       # «ушли вперёд»: ни перехода, ни задачи
            assert _state(diagram) == entry, cell
            assert db.commits == 0, cell
            assert result["task_id"] is None, cell
            continue

        target = _target(endpoint, status.value)
        assert _state(diagram) == (target, None, None), cell
        assert db.commits == 1, cell
        assert result["task_id"] == "task-0001", cell


# ── брокер лёг: та же решётка, таблица отказа ────────────────────────────

@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_dispatch_failure_over_every_status(endpoint, status, broker_down,
                                            ocr_enabled, no_layout):
    """Отказ отправки: у пропущенных клеток — исход из таблицы, у прочих 400."""
    outcome, status_rule, fields_rule, commits, attempts = FAILURE[endpoint]

    for stage in ERROR_STAGES:
        broker_down.clear()
        entry = _entry_state(status, stage)
        diagram = _diagram(status, entry[1], entry[2])
        db = FakeDB(diagram)
        cell = (endpoint, status.value, stage)
        tasks = _dispatches(endpoint, status.value)

        if tasks is None:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(CALL[endpoint](db))
            assert exc.value.status_code == 400, cell
            assert _state(diagram) == entry, cell
            assert db.commits == 0, cell
            assert broker_down == [], cell
            continue

        if not tasks:                       # «ушли вперёд»: брокер не при делах
            result = asyncio.run(CALL[endpoint](db))
            assert result["task_id"] is None, cell
            assert _state(diagram) == entry, cell
            assert db.commits == 0, cell
            assert broker_down == [], cell
            continue

        target = _target(endpoint, status.value)
        expected_status = target if status_rule == TARGET_RULE else entry[0]
        expected_fields = (None, None) if fields_rule == CLEARED else (entry[1], entry[2])

        if outcome == OK:
            result = asyncio.run(CALL[endpoint](db))
            assert result["task_id"] is None, cell
            assert result["status"] == RESPONSE_STATUS[endpoint], cell
        else:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(CALL[endpoint](db))
            assert exc.value.status_code == outcome, cell

        assert _state(diagram) == (expected_status,) + expected_fields, cell
        assert db.commits == commits, cell
        assert len(broker_down) == attempts, cell
        assert broker_down[0]["name"] == TASKS[endpoint][0], cell


# ── второй замок: кнопки оператора после отказа отправки ─────────────────

@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_buttons_after_failed_dispatch(endpoint, broker_down, ocr_enabled, no_layout):
    """Инвариант ДАННЫХ: что оператор может нажать после отказа отправки.

    До правки у масок доступных кнопок не оставалось НИ ОДНОЙ: `validated_masks`
    не лежит в `_MANUAL_INPROGRESS`, а бусина перекрёстков при нём «в процессе» —
    синяя и не нажимается; выход был только правкой БД. После возврата состояния
    кнопки — те же, что в точке входа, то есть работа оператора продолжается.
    """
    from ui.widgets.diagram_workspace import _buttons_for_status

    entry = SCENARIO_ENTRY[endpoint]
    diagram = _diagram(DiagramStatus(entry))
    db = FakeDB(diagram)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(CALL[endpoint](db))
    assert exc.value.status_code == 503, endpoint

    assert diagram.status.value == entry, "состояние не вернулось в точку входа"
    available, _completed, _processing = _buttons_for_status(diagram.status)
    assert available == BUTTONS_AT_ENTRY[endpoint], endpoint


def test_buttons_after_failed_dispatch_after_contours(broker_down, ocr_enabled, no_layout):
    """Тот же замок на пути «возврат в проверку схемы ПОСЛЕ контуров».

    Клетка отдельная, потому что до правки у неё был другой целевой статус
    (`generating_fxml`, второй тупик пункта), а решётка выше идёт по общей
    точке входа.
    """
    from ui.widgets.diagram_workspace import _buttons_for_status

    diagram = _diagram(DiagramStatus(SCENARIO_ENTRY_AC))
    db = FakeDB(diagram)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(complete_graph_validation(UID, db=db))
    assert exc.value.status_code == 503

    assert diagram.status.value == SCENARIO_ENTRY_AC
    assert diagram.status.value != TARGET_AFTER_CONTOURS
    available, _completed, _processing = _buttons_for_status(diagram.status)
    assert available != BUTTONS_AFTER_FAILURE_BEFORE_AC, "тупик остался тупиком"
    assert available == {"edit_graph"}


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_buttons_at_the_entry_point(endpoint):
    """Кнопки в точке возврата — то, что вернулось бы вместе с состоянием.

    Числа сняты с `_buttons_for_status` напрямую: они не зависят от проверяемого
    кода и служат базой сравнения для правки.
    """
    from ui.widgets.diagram_workspace import _buttons_for_status

    available, _completed, _processing = _buttons_for_status(
        DiagramStatus(SCENARIO_ENTRY[endpoint]))
    assert available == BUTTONS_AT_ENTRY[endpoint], endpoint


# ── повторная попытка после отказа ───────────────────────────────────────

@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_second_attempt_after_dead_broker(endpoint, broker_down, monkeypatch,
                                          ocr_enabled, no_layout):
    """Брокер поднялся — оператор повторяет действие. Сценарий уровня дефекта.

    Тест судит по таблице, а не по знанию редакции: чем кончится повтор, решает
    состояние, в котором диаграмма осталась после отказа.
    """
    diagram = _diagram(DiagramStatus(SCENARIO_ENTRY[endpoint]))
    db = FakeDB(diagram)

    with pytest.raises(HTTPException):
        asyncio.run(CALL[endpoint](db))

    from worker.celery_app import celery_app

    sent = []

    class _AsyncResult:
        id = "task-0002"

    def _send_task(name, args=None, kwargs=None, **rest):
        sent.append(name)
        return _AsyncResult()

    monkeypatch.setattr(celery_app, "send_task", _send_task)

    stuck = diagram.status.value
    tasks = _dispatches(endpoint, stuck)

    if tasks is None:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(CALL[endpoint](db))
        assert exc.value.status_code == 400, endpoint
        assert sent == [], "отказавший гейт всё-таки отправил задачу"
    else:
        asyncio.run(CALL[endpoint](db))
        assert sent == tasks, endpoint


# ── настоящие исключения отказа отправки ─────────────────────────────────

# Оба класса перемерены четырьмя точками (§87д, сводка §91). Какое прилетит,
# решает то, ЧТО мертво:
#
#   брокер И result-бэкенд (на бою это ОДИН Redis)   RuntimeError      ≈ 64 с
#   только брокер, бэкенд жив                        OperationalError  ≈ 4 с
#
# 64 с набирает retry-цикл БЭКЕНДА внутри `send_task` — он отрабатывает ДО
# публикации в брокер. Первая строка и есть боевая: узкий `except OperationalError`
# промахнулся бы мимо неё. Здесь широту держит сам `async_safe_dispatch`
# (`except Exception`), и этот тест — единственное, что не даёт её сузить.
REAL_DISPATCH_FAILURES = [
    pytest.param(
        RuntimeError,
        "Retry limit exceeded while trying to reconnect to the Celery"
        " result store backend: Error 111 connecting to redis:6379.",
        id="dead-result-backend",
    ),
    pytest.param(
        OperationalError,
        "Error 111 connecting to redis:6379.",
        id="dead-broker-live-backend",
    ),
]


@pytest.mark.parametrize("exc_class, message", REAL_DISPATCH_FAILURES)
def test_real_broker_exception_class_is_not_special(exc_class, message, monkeypatch,
                                                    ocr_enabled, no_layout):
    """Оба настоящих исключения дают тот же исход, что `OSError` из фикстуры.

    Иначе таблица судила бы синтетический класс, а бой отдавал бы другой — и хуже
    того, боевой класс тут не тот, которого ждёшь: не `OperationalError` брокера,
    а `RuntimeError` мёртвого result-бэкенда. Широту держит сам
    `async_safe_dispatch` (`except Exception`), и сузить её без красного нельзя.
    """
    from worker.celery_app import celery_app

    def _send_task(name, args=None, kwargs=None, **rest):
        raise exc_class(message)

    monkeypatch.setattr(celery_app, "send_task", _send_task)

    diagram = _diagram(DiagramStatus.VALIDATING_MASKS)
    db = FakeDB(diagram)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(complete_mask_validation(UID, db=db))

    assert exc.value.status_code == 503
    assert _state(diagram) == ("validating_masks", None, None)
    assert db.commits == 2


# ── ложный успех: чем именно он лжёт ─────────────────────────────────────

def test_task_id_none_means_exactly_already_past(monkeypatch, ocr_enabled, no_layout):
    """`task_id: null` в ответе 200 означает РОВНО «цепочка ушла вперёд».

    До правки тот же ответ отдавал и мёртвый брокер, и клиент
    (`diagram_workspace.py:1592-1595`) печатал «цепочка уже запущена» в обоих
    случаях — ложный успех, ради которого пункт и делается. Теперь второй случай
    отвечает 503, и фраза клиента стала правдой.

    Брокер здесь переключается ВНУТРИ теста, а не двумя фикстурами: обе ставят
    один и тот же `celery_app.send_task`, и вторая молча отменяла бы первую
    (первая редакция этого теста так и упала — «мёртвый» вызов оказался живым).
    """
    from worker.celery_app import celery_app

    class _AsyncResult:
        id = "task-0001"

    monkeypatch.setattr(celery_app, "send_task",
                        lambda name, **rest: _AsyncResult())

    past = _diagram(DiagramStatus.BUILT)
    past_db = FakeDB(past)
    past_result = asyncio.run(complete_mask_validation(UID, db=past_db))

    assert past_result["task_id"] is None
    assert past_result["status"] == "validated_masks"
    assert past_db.commits == 0

    def _dead(name, **rest):
        raise OSError("[Errno 111] Connection refused")

    monkeypatch.setattr(celery_app, "send_task", _dead)

    dead = _diagram(DiagramStatus.VALIDATING_MASKS)
    dead_db = FakeDB(dead)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(complete_mask_validation(UID, db=dead_db))

    assert exc.value.status_code == 503, "отказ брокера снова выдаёт себя за успех"
    assert _state(dead) == ("validating_masks", None, None)
    assert dead_db.commits == 2


def test_junction_message_promises_only_what_it_did(dispatched, ocr_enabled):
    """Сообщение перекрёстков обещает ровно сделанное.

    Прежняя редакция обещала «graph build + contours + OCR started» ВСЕГДА —
    при том что `contour_task_id` захардкожен `None` (SAM2 давно ручной).
    Контуры из текста ушли, OCR называется по факту.
    """
    diagram = _diagram(DiagramStatus.VALIDATING_JUNCTIONS)
    db = FakeDB(diagram)
    result = asyncio.run(complete_junction_validation(UID, db=db))

    assert result["message"] == (
        "Junction validation completed, graph build started, OCR started")
    assert "contours" not in result["message"]
    assert result["task_id"] == "task-0001"
    assert result["contour_task_id"] is None
    assert result["ocr_task_id"] == "task-0001"


def test_junction_message_says_nothing_when_already_past(dispatched, ocr_enabled):
    """«Ушли вперёд» — ни одного обещания: ничего и не отправлялось."""
    diagram = _diagram(DiagramStatus.BUILT)
    db = FakeDB(diagram)
    result = asyncio.run(complete_junction_validation(UID, db=db))

    assert result["message"] == "Junction validation completed"
    assert dispatched == []


def test_ocr_failure_alone_is_named_and_traced(monkeypatch, ocr_enabled):
    """Граф ушёл, OCR не ушёл — ответ это ГОВОРИТ, и в логе есть след.

    Ветка отдельная и это заявленная граница: откатывать её нельзя — задача
    сборки графа уже в брокере, возврат осиротил бы её. Поэтому лечится она
    не возвратом состояния, а правдой в ответе и следом; штатное восстановление
    даёт тот же OCR из `graph/complete-simple` (idempotent safety net).
    """
    from worker.celery_app import celery_app

    sent = []

    class _AsyncResult:
        id = "task-0001"

    def _send_task(name, args=None, kwargs=None, **rest):
        sent.append(name)
        if name == OCR_TASK:
            raise OSError("[Errno 111] Connection refused")
        return _AsyncResult()

    monkeypatch.setattr(celery_app, "send_task", _send_task)

    diagram = _diagram(DiagramStatus.VALIDATING_JUNCTIONS)
    db = FakeDB(diagram)
    trace = _traces("junctions", db)

    assert sent == [GRAPH_TASK, OCR_TASK]
    assert _state(diagram) == ("validated_junctions", None, None)
    assert db.commits == 1, "состояние всё-таки вернули — граф остался сиротой"

    assert len(trace) == 1, "отказ OCR остался молчаливым"
    assert trace[0].uid == str(UID)
    assert "OCR" in trace[0].getMessage()
    assert "НЕ возвращаю" in trace[0].getMessage()


def test_ocr_disabled_is_not_a_failure(dispatched, monkeypatch):
    """OCR выключен конфигом — это не отказ: ни следа, ни жалобы в сообщении."""
    import app.services.project_loader as project_loader

    class _Ocr:
        enabled = False

    class _Config:
        ocr = _Ocr()

    class _Loader:
        def load(self, code):
            return _Config()

    monkeypatch.setattr(project_loader, "get_project_loader", lambda: _Loader())

    diagram = _diagram(DiagramStatus.VALIDATING_JUNCTIONS)
    db = FakeDB(diagram)
    trace = _traces("junctions", db)

    assert [c["name"] for c in dispatched] == [GRAPH_TASK], "OCR всё-таки ушёл"
    assert dispatched[0]["args"] == [str(UID)]
    assert trace == []


# ── Д2: следа отказа сегодня нет ─────────────────────────────────────────

class _Capture(logging.Handler):
    """Приёмник записей эндпоинта с настоящим `ContextFilter` на входе."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []
        self.addFilter(ContextFilter())

    def emit(self, record):
        self.records.append(record)


def _traces(endpoint, db, expected_exc=None):
    """Прогнать эндпоинт с ловушкой на его логгере и вернуть следы `dispatch_failed`.

    `uid` судится настоящим механизмом — `ContextFilter` тянет его из `obs.bind`, —
    и фильтр обязан отработать ВНУТРИ задачи: `asyncio.run` копирует контекст,
    и наружу его правки не возвращаются.
    """
    handler = _Capture()
    api_logger = logging.getLogger("app.api.validation")
    previous_level = api_logger.level
    api_logger.addHandler(handler)
    api_logger.setLevel(logging.DEBUG)
    try:
        if expected_exc is None:
            asyncio.run(CALL[endpoint](db))
        else:
            with pytest.raises(expected_exc):
                asyncio.run(CALL[endpoint](db))
    finally:
        api_logger.removeHandler(handler)
        api_logger.setLevel(previous_level)

    return [r for r in handler.records
            if getattr(r, "event", None) == "dispatch_failed"]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_dead_broker_leaves_a_trace_with_uid(endpoint, broker_down, ocr_enabled, no_layout):
    """Д2: отказ отправки больше не молчит — строка с `uid` и точкой возврата.

    До правки логгер эндпоинта об этом не говорил ничего: единственная строка
    уходила в чужой логгер (`app.services.dispatch`, `Failed to dispatch …`)
    и не несла ни `uid`, ни точки возврата.
    """
    entry = SCENARIO_ENTRY[endpoint]
    db = FakeDB(_diagram(DiagramStatus(entry)))

    trace = _traces(endpoint, db, HTTPException)

    assert len(trace) == 1, "отказ отправки не оставил следа"
    assert trace[0].uid == str(UID)
    assert trace[0].phase == "validation"
    assert entry in trace[0].getMessage(), "точка возврата не названа"


class _DBGone(RuntimeError):
    """БД недоступна: коммит ВОЗВРАТА состояния не проходит."""


class _DeadDB(FakeDB):
    """БД легла вместе с брокером — падает второй `commit`, тот, что возвращает."""

    async def commit(self):
        self.commits += 1
        if self.commits == 2:
            raise _DBGone("connection already closed")


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_dispatch_failed_trace_survives_a_dead_db(endpoint, broker_down,
                                                  ocr_enabled, no_layout):
    """Д2 в САМОЙ тяжёлой ветке: БД легла ВМЕСТЕ с брокером — след всё равно есть.

    Брокер и БД на бою падают вместе (одна машина, одна сеть, один рестарт).
    Тогда возврат состояния записать не удаётся: второй `commit` падает следом
    и уносит исключение наружу. След, стоящий ПОСЛЕ этого коммита, не ляжет
    никогда — в логе остался бы только traceback БД, по которому не видно,
    что отказала ОТПРАВКА.

    Состояние диаграммы здесь не судится намеренно: возврат не записан, она
    остаётся впереди ровно как до пункта — не хуже, чем было. Судится СЛЕД,
    потому что в этой ветке он единственное, что вообще остаётся.
    """
    entry = SCENARIO_ENTRY[endpoint]
    db = _DeadDB(_diagram(DiagramStatus(entry)))

    trace = _traces(endpoint, db, _DBGone)

    assert db.commits == 2, "возврат состояния даже не попытались записать"
    assert len(trace) == 1, "отказ отправки не оставил следа: БД унесла его с собой"
    assert trace[0].uid == str(UID)
    assert trace[0].phase == "validation"
    assert entry in trace[0].getMessage(), "точка возврата не названа"


# ── прочие ветки тех же эндпоинтов ───────────────────────────────────────

@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_missing_diagram_is_404(endpoint, dispatched, ocr_enabled, no_layout):
    """Нет диаграммы — 404, а не 400 гейта и не отправка."""
    db = FakeDB(None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(CALL[endpoint](db))
    assert exc.value.status_code == 404
    assert db.commits == 0
    assert dispatched == []


@pytest.mark.parametrize("endpoint, missing", [
    ("masks", ArtifactType.PIPE_MASK_VALIDATED),
    ("junctions", ArtifactType.JUNCTION_MASK_VALIDATED),
    ("simple", ArtifactType.GRAPH_VALIDATED),
    ("complete", ArtifactType.GRAPH_VALIDATED),
])
def test_missing_artifact_is_400_before_any_dispatch(endpoint, missing, dispatched,
                                                     ocr_enabled, no_layout):
    """Гейт артефакта стоит ДО перехода: ни коммита, ни задачи.

    У масок и перекрёстков «не найдена оригинальная маска» — тот же 400, что
    у графа «граф не сохранён»: одна ветка отказа на все четыре.
    """
    artifacts = dict(ARTIFACTS)
    artifacts[missing] = None
    db = FakeDB(_diagram(DiagramStatus(SCENARIO_ENTRY[endpoint])), artifacts)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(CALL[endpoint](db))
    assert exc.value.status_code == 400
    assert db.commits == 0
    assert dispatched == []


def test_boevoy_project_config_really_enables_ocr():
    """Решётка судит бой: у `thermohydraulics` OCR действительно включён.

    Иначе клетки OCR проверяли бы фикцию. Конфиг берётся из РЕПОЗИТОРИЯ явным
    путём, а не через `settings`: `tests/conftest.py` подменяет
    `PROJECTS_CONFIG_DIR` на `/tmp/test_configs`, и глобальный загрузчик под
    pytest вернул бы `None` — то есть «OCR выключен», чего на бою нет.
    """
    from pathlib import Path

    from app.services.project_loader import ProjectLoader

    configs_dir = Path(__file__).resolve().parents[1] / "configs" / "projects"
    config = ProjectLoader(configs_dir=configs_dir).load("thermohydraulics")
    assert config is not None, "боевой конфиг проекта не читается"
    assert config.ocr.enabled is True


def test_layout_is_dispatched_only_after_contours(broker_down, ocr_enabled, no_layout):
    """`dispatch_layout` зовётся ровно на возврате после контуров, и до FXML.

    Граница пункта: исход самой раскладки судит `layout_dispatch` (единственная
    из шести точек, которая `None` проверяет), здесь — только факт вызова.
    """
    direct = FakeDB(_diagram(DiagramStatus.VALIDATING_GRAPH))
    with pytest.raises(HTTPException):
        asyncio.run(complete_graph_validation(UID, db=direct))
    assert no_layout == []

    after = FakeDB(_diagram(DiagramStatus.OCR_BOUND))
    with pytest.raises(HTTPException):
        asyncio.run(complete_graph_validation(UID, db=after))
    assert no_layout == [str(UID)], "раскладка ставится ДО отправки FXML"


# ── БОЕВАЯ ГРАНИЦА: конкурентные обработчики одного клика ────────────────
#
# Найдена red-team, подтверждена ревизией связки (§104е) и переснята
# исполнением (§107.5). Возврат состояния здесь НЕ чинится — честная починка
# требует условного UPDATE («верни, только если статус всё ещё мой»), а это
# чужая зона (адрес 5-3, долг A1 last-writer-wins). Форма фиксации — та же,
# что у ветки «БД легла ВМЕСТЕ с брокером»: граница названа в
# `docs/STATUS_MACHINE.md §5` и заперта здесь, чтобы её починка сказала
# об этом вслух, а не прошла молча.
#
# ⚠ ГРАНИЦА СУЖЕНА пунктом 1-19 (§118): клиент больше не повторяет POST по
# таймауту, поэтому ОДИН клик оператора конкурентных обработчиков не
# собирает. Сама гонка на сервере жива как класс — два разных клиента (или
# два оператора) её соберут по-прежнему, и тесты ниже, зовущие корутины
# напрямую, остаются верными без единой правки. Изменился только порог
# достижимости, и он переписан ровно на то, что теперь правда.

def test_one_click_of_the_operator_no_longer_makes_a_second_handler():
    """Порог достижимости ПОСЛЕ 1-19: из одного клика конкурентов больше нет.

    Числа не изменились и остаются абсолютными: клиент сдаётся на **60**-й
    секунде (`ui/services/api_client.py`), боевой отказ `send_task` наступает
    на **64**-й (§87д, §91). Раньше этого хватало, чтобы один клик собрал до
    `max_retries + 1` обработчиков: `_request` ловил `httpx.RequestError`,
    а таймаут — его подкласс, и повторялся И POST.

    Теперь спрашивают не про класс перехвата, а про РЕШЕНИЕ клиента, и
    поэлементно — по обе стороны границы (`PROTOCOL §3`): «мог дойти» против
    «соединения не было». Откатят правку — покраснеет первый же ассерт;
    запретят POST совсем — покраснеет второй, и полезный повтор «API ещё не
    поднят» не уедет молча.

    ⛔ Что тест НЕ утверждает: что гонка исчезла. Она достижима двумя разными
    клиентами, и это ровно то, что проверяют два теста ниже прямым вызовом
    корутин. Снят один источник конкурентности — авторетрай, — а не класс.
    """
    import httpx

    from ui.services.api_client import APIClient, _may_retry

    signature = inspect.signature(APIClient.__init__)
    timeout = signature.parameters["timeout"].default
    retries = signature.parameters["max_retries"].default

    assert timeout == 60.0, "клиентский таймаут переехал — пересними границу"
    assert retries == 3, "число попыток переехало — пересними границу"
    assert timeout < 64, (
        "клиент теперь ждёт дольше боевого отказа (64 с) — профиль сменился, "
        "границу в STATUS_MACHINE §5 надо пересматривать целиком"
    )

    boom = httpx.ReadTimeout("60 с вышли")
    assert not _may_retry("POST", boom), (
        "POST снова повторяется по таймауту — один клик опять собирает "
        "конкурентные обработчики, см. STATUS_MACHINE §5 и MEASUREMENTS §118"
    )
    assert _may_retry("POST", httpx.ConnectError("API ещё не поднят")), (
        "повтор при неустановленном соединении обязан жить: сервер запроса "
        "не видел, дубль невозможен по построению"
    )
    assert _may_retry("GET", boom), "идемпотентный повтор сломан заодно"


def _concurrent_masks_complete(entry, hold_first, hold_second, gap):
    """Два обработчика `masks/complete` на ОДНОЙ диаграмме, как на бою.

    `send_task` держит поток и бросает — ровно то, что делает боевой
    result-бэкенд 64 секунды. `to_thread` отпускает цикл событий, поэтому
    второй обработчик входит, пока первый висит в отправке.
    """
    from worker.celery_app import celery_app

    db = FakeDB(_diagram(entry))
    outcome = {}
    saved = celery_app.send_task

    def _hang(hold):
        def _send(name, args=None, kwargs=None, **rest):
            time.sleep(hold)
            raise OSError("[Errno 111] Connection refused")
        return _send

    async def _call(tag, hold):
        celery_app.send_task = _hang(hold)
        try:
            outcome[tag] = ("200", await complete_mask_validation(UID, db=db))
        except HTTPException as exc:
            outcome[tag] = (exc.status_code, exc.detail)

    async def _second():
        await asyncio.sleep(gap)
        outcome["вход №2"] = db.diagram.status.value
        await _call("2", hold_second)

    async def _race():
        await asyncio.gather(_call("1", hold_first), _second())

    try:
        asyncio.run(_race())
    finally:
        celery_app.send_task = saved
    return db, outcome


def test_a_concurrent_handler_restores_a_stale_snapshot(ocr_enabled, no_layout):
    """⛔ ГРАНИЦА: `previous_state` — снимок, и при конкуренции он ЧУЖОЙ.

    Обработчик №2 входит, пока №1 висит в отправке, и снимает `previous_state`
    уже ПОСЛЕ коммита-вперёд №1 — то есть запоминает `validated_masks` как
    «состояние до вызова». Кончая последним, он этот снимок и записывает:
    ИСХОДНЫЙ ТУПИК ПУНКТА ВОССТАНОВЛЕН, а лог честен и абсурден —
    «возвращаю состояние в 'validated_masks'».

    Утверждается РАЗНИЦА двух порядков (`PROTOCOL §3`), а не совпадение
    с состоянием «до»: перевернёшь, кто кончает последним, — финал другой.
    Значит тест видит именно конкуренцию, а не общий результат отказа.

    Ни одна клетка при этом НЕ ХУЖЕ прежнего: до ноги 1.16 тупик был во всех
    четырёх исходах из четырёх. ⚠ Прежняя редакция говорила «теперь это
    лотерея чётности попыток клиента» — после 1-19 это неверно: попытка одна,
    и из одного клика конкурента не выходит. Сюда приводят два разных клиента,
    и тест поэтому зовёт корутины напрямую, а не через клиент.
    Чинится условным UPDATE — 5-3; здесь только фиксация.
    """
    late_second, outcome = _concurrent_masks_complete(
        DiagramStatus.VALIDATING_MASKS, hold_first=0.05, hold_second=0.30, gap=0.02,
    )
    late_first, _ = _concurrent_masks_complete(
        DiagramStatus.VALIDATING_MASKS, hold_first=0.30, hold_second=0.05, gap=0.02,
    )
    alone, _ = _concurrent_masks_complete(
        DiagramStatus.VALIDATING_MASKS, hold_first=0.05, hold_second=0.05, gap=1.0,
    )

    # Конкуренция состоялась: №2 вошёл уже на чужом коммите-вперёд.
    assert outcome["вход №2"] == "validated_masks"
    # Оба получают честный 503 — обещание ноги на КАЖДОМ обработчике выполнено.
    assert outcome["1"][0] == 503 and outcome["2"][0] == 503

    from ui.widgets.diagram_workspace import _buttons_for_status

    available_late_second, _c, _p = _buttons_for_status(late_second.diagram.status)
    available_late_first, _c, _p = _buttons_for_status(late_first.diagram.status)
    available_alone, _c, _p = _buttons_for_status(alone.diagram.status)

    assert late_second.diagram.status.value == "validated_masks"
    assert available_late_second == set(), (
        "тупик пункта: доступных кнопок нет — но так и записано в границе"
    )
    assert late_first.diagram.status.value == SCENARIO_ENTRY["masks"]
    assert available_late_first == BUTTONS_AT_ENTRY["masks"]
    assert alone.diagram.status.value == SCENARIO_ENTRY["masks"]
    assert available_alone == BUTTONS_AT_ENTRY["masks"]

    assert late_second.diagram.status != late_first.diagram.status, (
        "порядок завершения перестал решать исход — граница протухла, "
        "пересними её и проверь, не починили ли гонку (5-3)"
    )


def test_the_stale_snapshot_is_named_in_the_log(ocr_enabled, no_layout):
    """След не врёт даже в этой ветке: точка возврата названа как есть.

    Читать лог надо буквально: строка «возвращаю состояние в 'validated_masks'»
    означает, что снимок был снят после чужого коммита. Именно она — единственный
    способ увидеть гонку на бою, пока условного UPDATE нет.
    """
    handler = _Capture()
    api_logger = logging.getLogger("app.api.validation")
    previous_level = api_logger.level
    api_logger.addHandler(handler)
    api_logger.setLevel(logging.DEBUG)
    try:
        _concurrent_masks_complete(
            DiagramStatus.VALIDATING_MASKS, hold_first=0.05, hold_second=0.30, gap=0.02,
        )
    finally:
        api_logger.removeHandler(handler)
        api_logger.setLevel(previous_level)

    traces = [r.getMessage() for r in handler.records
              if getattr(r, "event", None) == "dispatch_failed"]

    assert len(traces) == 2, f"следов не два: {traces}"
    assert any("'validating_masks'" in m for m in traces), traces
    assert any("'validated_masks'" in m for m in traces), (
        "лог не назвал чужой снимок — гонку на бою будет не увидеть"
    )

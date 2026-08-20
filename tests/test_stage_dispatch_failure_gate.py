# -*- coding: utf-8 -*-
"""Запуск стадии: таблица исходов отправки задачи (пункт 1-13, нога 1.13 дороги).

Зачем. Класс правки — [сма]: `PROTOCOL §Гейты` требует СНАЧАЛА зафиксировать
текущие переходы и только потом править. У трёх эндпоинтов из пяти покрытия нет
вовсе (греп `start_segmentation` / `start_skeletonization` /
`start_junction_detection` по `tests/` и `tools/` — ноль попаданий); у детекции
покрыт гейт статуса (`tests/test_detection_status_gate.py`, нога 1.11), а исход
самой ОТПРАВКИ не покрыт нигде.

Что проверяется. Пять эндпоинтов запуска стадий:

    POST /api/detection/{uid}/detect          — start_detection
    POST /api/detection/{uid}/retry           — retry_detection
    POST /api/segmentation/{uid}/segment      — start_segmentation
    POST /api/skeleton/{uid}/skeletonize      — start_skeletonization
    POST /api/junction/{uid}/detect-junctions — start_junction_detection

Каждый — по ПОЛНОЙ решётке: 31 статус × 11 значений `error_stage` = 341 клетка,
и в ДВУХ ветках: брокер жив и брокер лёг. Судятся настоящие корутины эндпоинтов:
`AsyncSession` подделана (её поверхность здесь — `execute` + `commit`), `send_task`
подменён, живой брокер и БД не нужны.

Дефект, ради которого таблица снята (нога 1.13). Все пять коммитят статус ДО
отправки и отправляют БЕЗ ВСЯКОЙ ЗАЩИТЫ. Брокер лёг между двумя действиями —
исключение уходит наружу (оператору 500), а диаграмма остаётся в `*ING`-статусе,
которого уже никто не снимет: задачи нет, значит некому ни упасть в `error`,
ни дойти до конца. Второй замок: кнопка своей стадии при `*ING`-статусе уходит
в `processing` (`_buttons_for_status`), то есть до 400 оператор даже не добирается.
Выхода нет вовсе — `_self_heal_stuck_stage` снял пункт 1.3, а поправка
`_MANUAL_INPROGRESS` знает только четыре РУЧНЫХ `VALIDATING_*`, и ни одного
из этих пяти статусов в ней нет.

Числа абсолютные, ожидания — литералы: `LAUNCH_*`, `FAILURE`, `BUTTON_AFTER_FAILURE`
сняты ЧТЕНИЕМ кода, а не вычислены из него (`PROTOCOL §3` — вычисленное ожидание
осталось бы зелёным при любом значении проверяемого).
"""
import asyncio
import logging
import uuid

import pytest
from fastapi import HTTPException
from kombu.exceptions import OperationalError

from app.api.detection import retry_detection, start_detection
from app.api.junction import start_junction_detection
from app.api.segmentation import start_segmentation
from app.api.skeleton import start_skeletonization
from app.core.logging import ContextFilter
from app.models import Diagram, DiagramStatus

UID = uuid.UUID("d74eb9f1-5555-6666-7777-888899990000")

# Размер машины. Абсолютное число: новый статус обязан пройти через эту таблицу,
# а не проскочить мимо неё молча. Доки пишут 29 — по коду 31 (`ast`-разбор
# `app/models/diagram.py`, адрес починки доков — пункт 0-3).
STATUS_COUNT = 31

# Полный словарь `error_stage`, который реально пишет конвейер: единственный
# производитель — `worker/utils/db_helpers.set_diagram_error`, значения сняты
# по его вызовам в `worker/tasks/*.py`. Список ноги 1.11 был у́же на две стадии
# (`direction_classification`, `contour_extraction`), а первая из них — ключ
# `_STAGE_DISPATCH` сегментации, то есть клетка с поведением.
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

DETECT_TASK = "worker.tasks.detection.task_detect_yolo"
DIRECTION_TASK = "worker.tasks.direction.task_classify_direction"
SKELETON_TASK = "worker.tasks.skeleton.task_skeletonize"
JUNCTION_TASK = "worker.tasks.junction.task_detect_junctions"

ENDPOINTS = ("detect", "retry", "segment", "skeletonize", "junctions")

# ── таблица запуска: что эндпоинт делает на ПРОПУЩЕННОЙ клетке ───────────
#
# Ключ `LAUNCH_BY_STATUS` — статус, который гейт пускает независимо от
# `error_stage`; ключ `LAUNCH_BY_ERROR` — стадия, с которой пускается `error`.
# Значение — (целевой статус, имя отправленной задачи). Клетки, которых нет
# ни в одной таблице, — отказ 400.
#
# Отправленной считается ГОЛОВА цепочки: `chain(direction, segment).apply_async()`
# уходит в брокер одним сообщением, хвост едет в его опциях (замер §79.1).
LAUNCH_BY_STATUS = {
    ("detect", "frame_cleaned"): ("detecting", DETECT_TASK),
    ("segment", "validated_bbox"): ("segmenting", DIRECTION_TASK),
    ("skeletonize", "segmenting"): ("skeletonizing", SKELETON_TASK),
    ("skeletonize", "validated_masks"): ("skeletonizing", SKELETON_TASK),
    ("junctions", "skeletonized_final"): ("detecting_junctions", JUNCTION_TASK),
}

# `error` + стадия. Сегментация пускает `error` с ЛЮБОЙ стадией (её гейт судит
# только статус, `_ALLOWED_STATUSES`), а шаг перезапуска выбирает `_STAGE_DISPATCH`:
# три стадии ведут на свой шаг, остальные — на начало цепочки.
LAUNCH_BY_ERROR = {
    ("detect", "detecting"): ("detecting", DETECT_TASK),
    ("retry", "detecting"): ("detecting", DETECT_TASK),
    ("segment", None): ("segmenting", DIRECTION_TASK),
    ("segment", "detecting"): ("segmenting", DIRECTION_TASK),
    ("segment", "direction_classification"): ("segmenting", DIRECTION_TASK),
    ("segment", "segmenting"): ("segmenting", DIRECTION_TASK),
    ("segment", "skeletonizing"): ("skeletonizing", SKELETON_TASK),
    ("segment", "skeletonizing_simple"): ("segmenting", DIRECTION_TASK),
    ("segment", "detecting_junctions"): ("detecting_junctions", JUNCTION_TASK),
    ("segment", "building_graph"): ("segmenting", DIRECTION_TASK),
    ("segment", "generating_fxml"): ("segmenting", DIRECTION_TASK),
    ("segment", "ocr"): ("segmenting", DIRECTION_TASK),
    ("segment", "contour_extraction"): ("segmenting", DIRECTION_TASK),
    ("skeletonize", "skeletonizing"): ("skeletonizing", SKELETON_TASK),
    ("junctions", "detecting_junctions"): ("detecting_junctions", JUNCTION_TASK),
}

# ── таблица отказа отправки ──────────────────────────────────────────────
#
# Значения — правила, а не строки статусов: правило одно на весь эндпоинт,
# и меняет его именно эта нога. Резолвятся ниже по входу и целевому статусу.
RAW = "исключение уходит наружу (оператору 500)"
TARGET = "целевой статус запуска"
ENTRY = "состояние до вызова"
CLEARED = "поля ошибки стёрты"
KEPT = "поля ошибки сохранены"

# ДО пункта (зафиксировано коммитом b64035d, 331 тест зелёный на нетронутом коде):
# отправка не защищена ничем. Исключение брокера уходит наружу, диаграмма
# остаётся в целевом `*ING`-статусе, поля ошибки уже стёрты переходом, коммит
# ровно один — тот, что записал переход.
FAILURE_BEFORE = {
    "detect": (RAW, TARGET, CLEARED, 1),
    "retry": (RAW, TARGET, CLEARED, 1),
    "segment": (RAW, TARGET, CLEARED, 1),
    "skeletonize": (RAW, TARGET, CLEARED, 1),
    "junctions": (RAW, TARGET, CLEARED, 1),
}

# ПОСЛЕ пункта: отказ отправки возвращает состояние, каким оно было ДО вызова —
# работа не начиналась, откатывать некуда, кроме исходной точки, — и отвечает 503
# вместо 500. Коммитов два: переход и возврат. Разница с прежней редакцией обязана
# быть ровно во всех пяти эндпоинтах — сторож ниже.
FAILURE = {
    "detect": (503, ENTRY, KEPT, 2),
    "retry": (503, ENTRY, KEPT, 2),
    "segment": (503, ENTRY, KEPT, 2),
    "skeletonize": (503, ENTRY, KEPT, 2),
    "junctions": (503, ENTRY, KEPT, 2),
}

# Второй замок тупика — кнопка стадии в клиенте после отказа отправки.
# Инвариант ДАННЫХ (`_buttons_for_status`), без живого виджета.
# "processing" — кнопка синяя и НЕ нажимается; "free" — доступна или зелёная;
# "error" — статус `error`, порогов у него нет вовсе, кнопку красит `error_stage`
# через `_error_key`, поэтому судится сохранность стадии.
BUTTON_KEY = {
    "detect": "detect",
    "retry": "detect",
    "segment": "segment",
    "skeletonize": "segment",
    "junctions": "junction",
}

# ДО пункта: диаграмма садилась в `*ING`, и кнопка своей стадии гасла у всех пяти.
BUTTON_AFTER_FAILURE_BEFORE = {
    "detect": "processing",
    "retry": "processing",
    "segment": "processing",
    "skeletonize": "processing",
    "junctions": "processing",
}

# ПОСЛЕ пункта: вернулась исходная точка — вместе с ней вернулась и кнопка.
# У перекрёстков она НЕ меняется, и это не недоделка: `skeletonized_final` сам
# по себе «в процессе» для бусины перекрёстков, а эндпоинт из клиента не зовётся
# вовсе — у `/api/junction/{uid}/detect-junctions` нет даже метода в `api_client`.
BUTTON_AFTER_FAILURE = {
    "detect": "free",
    "retry": "error",
    "segment": "free",
    "skeletonize": "free",
    "junctions": "processing",
}

# Достижимая точка входа для сценариев. Взята из тех же таблиц, но названа
# отдельно: сценарий обязан идти по клетке, которой оператор реально
# пользуется, а не по любой пропущенной.
SCENARIO_ENTRY = {
    "detect": ("frame_cleaned", None, None),
    "retry": ("error", "detecting", "boom"),
    "segment": ("validated_bbox", None, None),
    "skeletonize": ("validated_masks", None, None),
    "junctions": ("skeletonized_final", None, None),
}


# ── харнесс ──────────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class FakeDB:
    """Поверхность `AsyncSession`, которой пользуются все пять эндпоинтов."""

    def __init__(self, diagram):
        self.diagram = diagram
        self.commits = 0

    async def execute(self, stmt):
        return _FakeResult(self.diagram)

    async def commit(self):
        self.commits += 1


CALL = {
    "detect": lambda db: start_detection(UID, model_id=None, db=db),
    "retry": lambda db: retry_detection(UID, model_id=None, db=db),
    "segment": lambda db: start_segmentation(UID, db=db),
    "skeletonize": lambda db: start_skeletonization(UID, db=db),
    "junctions": lambda db: start_junction_detection(UID, db=db),
}


def _diagram(status, error_stage=None, error_message=None):
    diagram = Diagram()
    diagram.uid = UID
    diagram.status = status
    diagram.error_stage = error_stage
    diagram.error_message = error_message
    diagram.project_code = "thermohydraulics"
    diagram.detection_model = None
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


def _launch(endpoint, status_value, stage):
    """Что эндпоинт делает на клетке: (целевой статус, задача) или None — отказ."""
    by_status = LAUNCH_BY_STATUS.get((endpoint, status_value))
    if by_status is not None:
        return by_status
    if status_value == "error":
        return LAUNCH_BY_ERROR.get((endpoint, stage))
    return None


def _expected_failure(endpoint, entry, target):
    """Разрешить правило отказа в конкретное ожидаемое состояние."""
    _outcome, status_rule, fields_rule, commits = FAILURE[endpoint]
    status = target if status_rule == TARGET else entry[0]
    stage, message = (None, None) if fields_rule == CLEARED else (entry[1], entry[2])
    return (status, stage, message), commits


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
    for endpoint, value in LAUNCH_BY_STATUS:
        assert endpoint in ENDPOINTS
        assert DiagramStatus(value).value == value
    for endpoint, stage in LAUNCH_BY_ERROR:
        assert endpoint in ENDPOINTS
        assert stage in ERROR_STAGES
    for target, _task in list(LAUNCH_BY_STATUS.values()) + list(LAUNCH_BY_ERROR.values()):
        assert DiagramStatus(target).value == target


def test_error_stage_vocabulary_is_complete():
    """Словарь стадий покрывает всё, что пишет конвейер.

    Единственный производитель — `set_diagram_error(db, uid, message, stage)`;
    новая стадия обязана пройти через эту таблицу, а не появиться молча.
    """
    import re
    from pathlib import Path

    def _calls(src):
        """Аргументы каждого вызова целиком — со счётом скобок.

        Наивный `.*?` обрывается о первую же `)` внутри аргументов
        (`str(exc)[:500]`, «(89 min limit)») и молча теряет половину вызовов.
        """
        marker = "set_diagram_error("
        pos = src.find(marker)
        while pos != -1:
            start = pos + len(marker)
            depth, i = 1, start
            while depth and i < len(src):
                depth += (src[i] == "(") - (src[i] == ")")
                i += 1
            yield src[start:i - 1]
            pos = src.find(marker, i)

    root = Path(__file__).resolve().parents[1] / "worker" / "tasks"
    found = set()
    for path in sorted(root.glob("*.py")):
        for call in _calls(path.read_text(encoding="utf-8")):
            literals = re.findall(r'"([a-z_]+)"', call)
            if literals:
                found.add(literals[-1])

    assert found == {
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
    }, found
    assert found <= set(ERROR_STAGES), "конвейер пишет стадию мимо таблицы"


def test_every_endpoint_has_a_failure_rule():
    """Ни один эндпоинт таблицы не остался без объявленного исхода отказа."""
    assert set(FAILURE) == set(ENDPOINTS)
    assert set(FAILURE_BEFORE) == set(ENDPOINTS)
    assert set(BUTTON_AFTER_FAILURE) == set(ENDPOINTS)
    assert set(BUTTON_AFTER_FAILURE_BEFORE) == set(ENDPOINTS)
    assert set(BUTTON_KEY) == set(ENDPOINTS)
    assert set(SCENARIO_ENTRY) == set(ENDPOINTS)
    assert set(CALL) == set(ENDPOINTS)


def test_dispatch_failure_changed_by_exactly_the_declared_cells():
    """Пункт 1.13 переписал исход отказа у ВСЕХ ПЯТИ и ничего сверх того.

    Обе редакции — независимые литералы, поэтому правка одной без другой
    краснит этот сторож: «переход вне зафиксированного набора»
    (`PROTOCOL §Гейты`) ловится здесь, а не глазами ревизора.
    """
    changed = {key for key in FAILURE if FAILURE[key] != FAILURE_BEFORE[key]}
    assert changed == set(ENDPOINTS)

    for key, value in FAILURE_BEFORE.items():
        assert value == (RAW, TARGET, CLEARED, 1), key
    for key, value in FAILURE.items():
        assert value == (503, ENTRY, KEPT, 2), key


def test_button_table_changed_only_where_the_button_exists():
    """Кнопка вернулась у четырёх эндпоинтов из пяти — и это замер, не недоделка.

    У перекрёстков исход не изменился: `skeletonized_final` сам по себе «в
    процессе» для своей бусины, а эндпоинт из клиента не зовётся вовсе. Правка
    там всё равно нужна — статус перестал застревать, — но кнопки, которая бы
    от этого ожила, у него нет.
    """
    changed = {
        key for key in BUTTON_AFTER_FAILURE
        if BUTTON_AFTER_FAILURE[key] != BUTTON_AFTER_FAILURE_BEFORE[key]
    }
    assert changed == {"detect", "retry", "segment", "skeletonize"}
    assert BUTTON_AFTER_FAILURE["junctions"] == "processing"
    assert set(BUTTON_AFTER_FAILURE_BEFORE.values()) == {"processing"}


# ── брокер жив: полная решётка 31 × 11 на каждый эндпоинт ────────────────

@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_launch_over_every_status(endpoint, status, dispatched):
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
        launch = _launch(*cell)

        if launch is None:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(CALL[endpoint](db))
            assert exc.value.status_code == 400, cell
            assert _state(diagram) == entry, cell
            assert db.commits == 0, cell
            assert dispatched == [], cell
        else:
            target, task = launch
            result = asyncio.run(CALL[endpoint](db))
            assert result["status"] == target, cell
            assert _state(diagram) == (target, None, None), cell
            assert db.commits == 1, cell
            assert [c["name"] for c in dispatched] == [task], cell


# ── брокер лёг: та же решётка, таблица отказа ────────────────────────────

@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_dispatch_failure_over_every_status(endpoint, status, broker_down):
    """Отказ отправки: у пропущенных клеток — исход из таблицы, у прочих 400."""
    outcome = FAILURE[endpoint][0]

    for stage in ERROR_STAGES:
        broker_down.clear()
        entry = _entry_state(status, stage)
        diagram = _diagram(status, entry[1], entry[2])
        db = FakeDB(diagram)
        cell = (endpoint, status.value, stage)
        launch = _launch(*cell)

        if launch is None:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(CALL[endpoint](db))
            assert exc.value.status_code == 400, cell
            assert _state(diagram) == entry, cell
            assert db.commits == 0, cell
            assert broker_down == [], cell
            continue

        target, task = launch
        expected_state, expected_commits = _expected_failure(endpoint, entry, target)

        if outcome == RAW:
            with pytest.raises(OSError) as exc:
                asyncio.run(CALL[endpoint](db))
            assert "Connection refused" in str(exc.value), cell
        else:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(CALL[endpoint](db))
            assert exc.value.status_code == outcome, cell

        assert _state(diagram) == expected_state, cell
        assert db.commits == expected_commits, cell
        assert [c["name"] for c in broker_down] == [task], cell


# Оба настоящих исключения отказа отправки, перемеренные четырьмя точками
# (§87д, перемер ревизии; сводка — §91). Какое прилетит, решает то, ЧТО мертво:
#
#   брокер И result-бэкенд (на бою это ОДИН Redis)   RuntimeError      ≈ 64 с
#   только брокер, бэкенд жив                        OperationalError  ≈ 4 с
#
# 64 с набирает retry-цикл БЭКЕНДА внутри `send_task` — он отрабатывает ДО
# публикации в брокер. Первая строка и есть боевая: узкий `except
# OperationalError` промахнулся бы мимо неё и оставил бы тупик ровно там,
# где его чинят. Числа — литералы замера, кодом не вычисляются.
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
def test_real_broker_exception_class_is_not_special(exc_class, message, monkeypatch):
    """Оба настоящих исключения ловятся тем же швом, что `OSError` из фикстуры.

    Иначе таблица судила бы синтетический класс, а бой отдавал бы другой —
    и хуже того, боевой класс тут не тот, которого ждёшь: не `OperationalError`
    брокера, а `RuntimeError` мёртвого result-бэкенда (таблица выше). Поэтому
    в коде ловится `Exception`, и этот тест — единственное, что держит широту
    шва осмысленной. До правки оба уходили наружу как есть: оператору 500
    и диаграмма в `detecting` навсегда.
    """
    from worker.celery_app import celery_app

    def _send_task(name, args=None, kwargs=None, **rest):
        raise exc_class(message)

    monkeypatch.setattr(celery_app, "send_task", _send_task)

    diagram = _diagram(DiagramStatus.FRAME_CLEANED)
    db = FakeDB(diagram)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(start_detection(UID, model_id=None, db=db))

    assert exc.value.status_code == 503
    assert "Error 111" in exc.value.detail, "причина отказа не доехала до оператора"
    assert _state(diagram) == ("frame_cleaned", None, None)
    assert db.commits == 2


# ── второй замок: кнопка стадии после отказа отправки ────────────────────

@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_button_after_failed_dispatch(endpoint, broker_down):
    """Инвариант ДАННЫХ: в каком состоянии кнопка стадии после отказа.

    До правки диаграмма садилась в `*ING`-статус, и кнопка своей стадии уходила
    в `processing` — синяя и не нажимается, то есть повторить запуск оператору
    было нечем. Ни `_MANUAL_INPROGRESS` (только четыре ручных `VALIDATING_*`),
    ни `_stage_stuck` (строки стадии нет — задача не стартовала) этот статус
    не отпускают: выход был только правкой БД.
    """
    from ui.widgets.diagram_workspace import _buttons_for_status

    entry = SCENARIO_ENTRY[endpoint]
    diagram = _diagram(DiagramStatus(entry[0]), entry[1], entry[2])
    db = FakeDB(diagram)

    with pytest.raises(Exception):
        asyncio.run(CALL[endpoint](db))

    available, completed, processing = _buttons_for_status(diagram.status)
    key = BUTTON_KEY[endpoint]
    expected = BUTTON_AFTER_FAILURE[endpoint]

    if expected == "processing":
        assert key in processing, "кнопка стадии доступна — таблица врёт"
        assert key not in (available | completed)
    elif expected == "free":
        assert key in (available | completed), "кнопка стадии серая"
        assert key not in processing
    else:
        # Статус `error`: порогов у него нет, кнопку красит `error_stage`.
        # Пустая стадия гасит В КЛИЕНТЕ все кнопки разом (`_error_key` → None),
        # то есть возврат одного статуса завёл бы новый тупик класса 1.12.
        assert diagram.status is DiagramStatus.ERROR
        assert diagram.error_stage == entry[1]
        assert diagram.error_message == entry[2]
        assert (available, completed, processing) == (set(), set(), set())


# ── повторный запуск после отказа ────────────────────────────────────────

@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_second_launch_after_dead_broker(endpoint, broker_down, monkeypatch):
    """Брокер поднялся — оператор жмёт кнопку ещё раз. Сценарий уровня дефекта.

    До правки второй запуск отвечал 400: диаграмма уже в `*ING`-статусе, а его
    же гейт такой статус не принимает — тупик без выхода, кроме правки БД.
    После правки вернулась исходная точка, и повтор проходит: тест судит
    по таблице, а не по знанию редакции.
    """
    entry = SCENARIO_ENTRY[endpoint]
    diagram = _diagram(DiagramStatus(entry[0]), entry[1], entry[2])
    db = FakeDB(diagram)

    with pytest.raises(Exception):
        asyncio.run(CALL[endpoint](db))

    from worker.celery_app import celery_app

    sent = []

    class _AsyncResult:
        id = "task-0002"

    def _send_task(name, args=None, kwargs=None, **rest):
        sent.append(name)
        return _AsyncResult()

    monkeypatch.setattr(celery_app, "send_task", _send_task)

    stuck = _state(diagram)
    launch = _launch(endpoint, stuck[0], stuck[1])

    if launch is None:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(CALL[endpoint](db))
        assert exc.value.status_code == 400
        assert sent == [], "отказавший гейт всё-таки отправил задачу"
    else:
        result = asyncio.run(CALL[endpoint](db))
        assert result["status"] == launch[0]
        assert sent == [launch[1]]


# ── Д2: след отказа в логе ───────────────────────────────────────────────

# Приёмник и фаза каждого эндпоинта: логгер модуля + метка `obs.bind`.
LOG = {
    "detect": ("app.api.detection", "detection"),
    "retry": ("app.api.detection", "detection"),
    "segment": ("app.api.segmentation", "segmentation"),
    "skeletonize": ("app.api.skeleton", "skeleton"),
    "junctions": ("app.api.junction", "junction"),
}


class _Capture(logging.Handler):
    """Приёмник записей эндпоинта с настоящим `ContextFilter` на входе."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []
        self.addFilter(ContextFilter())

    def emit(self, record):
        self.records.append(record)


def _dispatch_traces(endpoint, db, expected_exc):
    """Прогнать эндпоинт с ловушкой на его логгере и вернуть следы `dispatch_failed`.

    `uid` судится настоящим механизмом — `ContextFilter` тянет его из `obs.bind`, —
    и фильтр обязан отработать ВНУТРИ задачи: `asyncio.run` копирует контекст,
    и наружу его правки не возвращаются.
    """
    handler = _Capture()
    api_logger = logging.getLogger(LOG[endpoint][0])
    previous_level = api_logger.level
    api_logger.addHandler(handler)
    api_logger.setLevel(logging.DEBUG)
    try:
        with pytest.raises(expected_exc):
            asyncio.run(CALL[endpoint](db))
    finally:
        api_logger.removeHandler(handler)
        api_logger.setLevel(previous_level)

    return [r for r in handler.records if getattr(r, "event", None) == "dispatch_failed"]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_dead_broker_leaves_a_trace_with_uid(endpoint, broker_down):
    """Д2: отказ отправки больше не молчит — строка с `uid` и точкой возврата.

    До правки сервер не оставлял об этом ничего: наружу уходило исключение, а
    диаграмма тихо оказывалась в статусе, из которого нет выхода.
    """
    entry = SCENARIO_ENTRY[endpoint]
    db = FakeDB(_diagram(DiagramStatus(entry[0]), entry[1], entry[2]))

    trace = _dispatch_traces(endpoint, db, HTTPException)

    assert len(trace) == 1, "отказ отправки не оставил следа"
    assert trace[0].uid == str(UID)
    assert trace[0].phase == LOG[endpoint][1]
    assert entry[0] in trace[0].getMessage(), "точка возврата не названа"


class _DBGone(RuntimeError):
    """БД недоступна: коммит ВОЗВРАТА состояния не проходит."""


class _DeadDB(FakeDB):
    """БД легла вместе с брокером — падает второй `commit`, тот, что возвращает.

    Первый коммит (переход в `*ING`) уже прошёл, поэтому диаграмма в БД остаётся
    в рабочем статусе — ровно то состояние, в котором её застаёт оператор.
    """

    async def commit(self):
        self.commits += 1
        if self.commits == 2:
            raise _DBGone("connection already closed")


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_dispatch_failed_trace_survives_a_dead_db(endpoint, broker_down):
    """Д2 в САМОЙ тяжёлой ветке: БД легла ВМЕСТЕ с брокером — след всё равно есть.

    Брокер и БД на бою падают вместе (одна машина, одна сеть, один рестарт).
    Тогда `send_task` бросает, возврат состояния записать не удаётся: второй
    `commit` падает следом и уносит исключение из обработчика наружу. След,
    стоящий ПОСЛЕ этого коммита, не ляжет НИКОГДА — в логе останется только
    traceback БД от глобального хендлера, и разобрать, почему диаграмма
    застряла в `*ING`, будет нечем: об отказе ОТПРАВКИ не сказано ни слова.

    Состояние диаграммы здесь не судится намеренно: возврат не записан, она
    остаётся в `*ING` ровно как до пункта — не хуже, чем было. Судится СЛЕД,
    потому что в этой ветке он единственное, что вообще остаётся.
    """
    entry = SCENARIO_ENTRY[endpoint]
    db = _DeadDB(_diagram(DiagramStatus(entry[0]), entry[1], entry[2]))

    trace = _dispatch_traces(endpoint, db, _DBGone)

    assert db.commits == 2, "возврат состояния даже не попытались записать"
    assert len(trace) == 1, "отказ отправки не оставил следа: БД унесла его с собой"
    assert trace[0].uid == str(UID)
    assert trace[0].phase == LOG[endpoint][1]
    assert entry[0] in trace[0].getMessage(), "точка возврата не названа"


# ── прочие ветки тех же эндпоинтов ───────────────────────────────────────

@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_missing_diagram_is_404(endpoint, dispatched):
    """Нет диаграммы — 404, а не 400 гейта и не отправка."""
    db = FakeDB(None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(CALL[endpoint](db))
    assert exc.value.status_code == 404
    assert db.commits == 0
    assert dispatched == []


def test_segment_chain_head_carries_the_tail(dispatched):
    """Цепочка «направление → сегментация» уходит ОДНИМ сообщением.

    Хвост едет в опциях головы (`chain`), поэтому таблица называет одну задачу,
    а не две: иначе отказ отправки выглядел бы как «первая ушла, вторая нет».
    """
    db = FakeDB(_diagram(DiagramStatus.VALIDATED_BBOX))
    result = asyncio.run(start_segmentation(UID, db=db))

    assert result["restart_from"] == "segmenting"
    assert [c["name"] for c in dispatched] == [DIRECTION_TASK]

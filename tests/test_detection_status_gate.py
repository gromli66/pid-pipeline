# -*- coding: utf-8 -*-
"""Гейт статуса детекции — таблица переходов `app/api/detection.py` (пункт 1.11 дороги).

Зачем. Класс правки — [сма], стейт-машина: гейт `PROTOCOL §Гейты` требует
СНАЧАЛА зафиксировать текущие переходы, и только потом править. В `app/api/`
покрытия нет вовсе (грепом `start_detection`/`FRAME_CLEANED` по `tests/` и
`tools/` — ноль попаданий), поэтому без этой таблицы отличить рефакторинг от
порчи было бы нечем.

Что проверяется. Оба эндпоинта файла — `POST /{uid}/detect` и `POST /{uid}/retry` —
прогоняются по ВСЕМУ множеству `DiagramStatus`, а не по паре интересных значений.
Судится настоящая корутина эндпоинта: `AsyncSession` подделана (её поверхность
здесь — `execute` + `commit`), `send_task` подменён, живой брокер и БД не нужны.

Пункт 1.11 меняет в этой таблице РОВНО ОДНУ клетку — `error`+`detecting` на
`/detect`: туда ведёт красная кнопка «🔄 Поиск элементов», а гейт требовал
точного `frame_cleaned` и отвечал 400, из чего оператор не выходил без правки
БД. Обе редакции таблицы лежат рядом (`ALLOWED_BEFORE` и `ALLOWED`), и их
разницу стережёт отдельный тест.

Числа абсолютные, ожидания — литералы. `ALLOWED` не вычисляется из проверяемого
гейта: иначе набор остался бы зелёным при любом его значении (`PROTOCOL §3`).
Порог заперт с двух сторон — и «пропускает то, что должен», и «не пропускает
ничего сверх»: каждый отказ дополнительно утверждает, что статус не сдвинут,
транзакция не коммитилась и задача не отправлялась.
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException

from app.api.detection import retry_detection, start_detection
from app.models import Diagram, DiagramStatus

UID = uuid.UUID("d74eb9f1-1111-2222-3333-444455556666")
TASK_NAME = "worker.tasks.detection.task_detect_yolo"

# Размер машины. Абсолютное число: новый статус обязан пройти через эту таблицу,
# а не проскочить мимо неё молча.
STATUS_COUNT = 31

# `POST /{uid}/detect`: какие пары (status, error_stage) гейт пропускает.
# Литералы — сняты чтением `app/api/detection.py`, не вычислены из него.
# ДО пункта 1.11 (зафиксировано коммитом 56b7c35, 85 тестов зелёные на
# нетронутом коде):
ALLOWED_BEFORE = frozenset({
    ("frame_cleaned", None),
})

# ПОСЛЕ пункта 1.11. Разница обязана быть ровно в одной клетке — сторож ниже.
ALLOWED = frozenset({
    ("frame_cleaned", None),
    ("error", "detecting"),
})

# `POST /{uid}/retry`: та же таблица для второго эндпоинта файла.
RETRY_ALLOWED = frozenset({
    ("error", "detecting"),
})

# Значения `error_stage`, которые реально пишет конвейер (`db_helpers.set_diagram_error`).
ERROR_STAGES = [
    None,
    "detecting",
    "segmenting",
    "skeletonizing",
    "skeletonizing_simple",
    "detecting_junctions",
    "building_graph",
    "generating_fxml",
    "ocr",
]


# ── харнесс ──────────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class FakeDB:
    """Поверхность `AsyncSession`, которой пользуются оба эндпоинта."""

    def __init__(self, diagram):
        self.diagram = diagram
        self.commits = 0

    async def execute(self, stmt):
        return _FakeResult(self.diagram)

    async def commit(self):
        self.commits += 1


def _diagram(status, error_stage=None, error_message=None, detection_model=None):
    diagram = Diagram()
    diagram.uid = UID
    diagram.status = status
    diagram.error_stage = error_stage
    diagram.error_message = error_message
    diagram.project_code = "thermohydraulics"
    diagram.detection_model = detection_model
    return diagram


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


# ── сторожа самой таблицы ────────────────────────────────────────────────

def test_status_machine_size_is_locked():
    """Машина ровно того размера, на который написана таблица."""
    assert len(list(DiagramStatus)) == STATUS_COUNT


def test_gate_changed_by_exactly_one_cell():
    """Пункт 1.11 добавил ровно одну клетку и не отнял ни одной.

    Оба множества — независимые литералы, поэтому правка одного без другого
    краснит этот сторож: «переход вне зафиксированного набора» (`PROTOCOL §Гейты`)
    ловится здесь, а не глазами ревизора.
    """
    assert ALLOWED - ALLOWED_BEFORE == {("error", "detecting")}
    assert ALLOWED_BEFORE - ALLOWED == frozenset()


def test_table_keys_name_real_statuses():
    """Сторож набора: ключ таблицы — существующий статус, а не опечатка."""
    for value, _stage in ALLOWED | ALLOWED_BEFORE | RETRY_ALLOWED:
        assert DiagramStatus(value).value == value


# ── POST /{uid}/detect — полный перебор статусов ─────────────────────────

@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_detect_gate_over_every_status(status, dispatched):
    """Каждый статус машины: пропущен ровно тот, что в таблице."""
    diagram = _diagram(status)
    db = FakeDB(diagram)
    allowed = (status.value, None) in ALLOWED

    if allowed:
        result = asyncio.run(start_detection(UID, model_id=None, db=db))
        assert result["status"] == "detecting"
        assert diagram.status is DiagramStatus.DETECTING
        assert db.commits == 1
        assert [c["name"] for c in dispatched] == [TASK_NAME]
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(start_detection(UID, model_id=None, db=db))
        assert exc.value.status_code == 400
        assert diagram.status is status, "статус сдвинут отказавшим эндпоинтом"
        assert db.commits == 0, "транзакция закоммичена при отказе"
        assert dispatched == [], "задача отправлена при отказе"


@pytest.mark.parametrize("stage", ERROR_STAGES)
def test_detect_gate_on_error_by_stage(stage, dispatched):
    """Статус `error`: решает `error_stage`, а не сам факт ошибки."""
    diagram = _diagram(DiagramStatus.ERROR, error_stage=stage,
                       error_message="boom")
    db = FakeDB(diagram)

    if ("error", stage) in ALLOWED:
        asyncio.run(start_detection(UID, model_id=None, db=db))
        assert diagram.status is DiagramStatus.DETECTING
        assert [c["name"] for c in dispatched] == [TASK_NAME]
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(start_detection(UID, model_id=None, db=db))
        assert exc.value.status_code == 400
        assert diagram.status is DiagramStatus.ERROR
        assert db.commits == 0
        assert dispatched == []


def test_detect_retry_clears_error(dispatched):
    """Повтор снимает ошибку вместе со сменой статуса.

    Иначе диаграмма бежит в `detecting` с протухшим `error_stage='detecting'`,
    и на нём стоят гейты обоих retry-эндпоинтов (`detection.py`, `diagrams.py`).
    """
    diagram = _diagram(DiagramStatus.ERROR, error_stage="detecting",
                       error_message="Detection timed out (89 min limit)")
    db = FakeDB(diagram)

    asyncio.run(start_detection(UID, model_id=None, db=db))

    assert diagram.status is DiagramStatus.DETECTING
    assert diagram.error_stage is None
    assert diagram.error_message is None


def test_detect_missing_diagram_is_404(dispatched):
    """Нет диаграммы — 404, а не 400 гейта."""
    db = FakeDB(None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(start_detection(UID, model_id=None, db=db))
    assert exc.value.status_code == 404
    assert dispatched == []


def test_detect_passes_model_id_to_task(dispatched):
    """Выбранная оператором модель доезжает до задачи."""
    db = FakeDB(_diagram(DiagramStatus.FRAME_CLEANED))
    asyncio.run(start_detection(UID, model_id=None, db=db))
    assert dispatched[0]["kwargs"]["model_id"] is None
    assert dispatched[0]["kwargs"]["project_code"] == "thermohydraulics"
    assert dispatched[0]["args"] == [str(UID)]


# ── POST /{uid}/retry — второй эндпоинт того же файла ────────────────────

@pytest.mark.parametrize("stage", ERROR_STAGES)
def test_retry_gate_on_error_by_stage(stage, dispatched):
    """`/retry` пускает только свою стадию отказа."""
    diagram = _diagram(DiagramStatus.ERROR, error_stage=stage,
                       error_message="boom")
    db = FakeDB(diagram)

    if ("error", stage) in RETRY_ALLOWED:
        asyncio.run(retry_detection(UID, model_id=None, db=db))
        assert diagram.status is DiagramStatus.DETECTING
        assert diagram.error_stage is None
        assert diagram.error_message is None
        assert [c["name"] for c in dispatched] == [TASK_NAME]
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(retry_detection(UID, model_id=None, db=db))
        assert exc.value.status_code == 400
        assert diagram.status is DiagramStatus.ERROR
        assert db.commits == 0
        assert dispatched == []


@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_retry_gate_over_every_status(status, dispatched):
    """`/retry` вне `error` не пускает ни один статус машины."""
    diagram = _diagram(status, error_stage="detecting")
    db = FakeDB(diagram)

    if (status.value, "detecting") in RETRY_ALLOWED:
        asyncio.run(retry_detection(UID, model_id=None, db=db))
        assert diagram.status is DiagramStatus.DETECTING
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(retry_detection(UID, model_id=None, db=db))
        assert exc.value.status_code == 400
        assert diagram.status is status
        assert db.commits == 0
        assert dispatched == []


def test_retry_reuses_previous_model(dispatched):
    """Без явной модели `/retry` берёт ту, что была."""
    db = FakeDB(_diagram(DiagramStatus.ERROR, error_stage="detecting",
                         detection_model="ensemble_v2"))
    asyncio.run(retry_detection(UID, model_id=None, db=db))
    assert dispatched[0]["kwargs"]["model_id"] == "ensemble_v2"

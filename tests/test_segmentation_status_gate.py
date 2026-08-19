# -*- coding: utf-8 -*-
"""Гейт статуса сегментации — таблица переходов `app/api/segmentation.py` (пункт 1.12 дороги).

Зачем. Класс правки — [сма], стейт-машина: `PROTOCOL §Гейты` требует СНАЧАЛА
зафиксировать текущие переходы, и только потом править. Покрытия у файла не было
вовсе (грепом `start_segmentation`/`_STAGE_DISPATCH` по `tests/` и `tools/` — ноль
попаданий), поэтому без этой таблицы отличить рефакторинг от порчи было бы нечем.

Что проверяется. Единственный эндпоинт файла — `POST /{uid}/segment` — прогоняется
по ВСЕМУ множеству `DiagramStatus` (31 значение) и по всему словарю `error_stage`.
Судится настоящая корутина эндпоинта: `AsyncSession` подделана (её поверхность
здесь — `execute` + `commit`), `send_task` и `celery.chain` подменены, живой
брокер и БД не нужны.

⭐ Эта таблица — доказательство того, что тупик 1.12 живёт НЕ здесь. Пункт 1.12
её не меняет ни в одной клетке: гейт уже сегодня пускает `error` и уже сегодня
знает `error_stage="direction_classification"` — перезапускает с шага направления,
а не с сегментации. Слепым оказался клиент (`ui/widgets/diagram_workspace.py`),
его решётку держит `tests/ui/test_direction_retry_deadend.py`. Замер, решивший
форму правки, — `MEASUREMENTS §71б`.

Числа абсолютные, ожидания — литералы: сняты чтением `app/api/segmentation.py`,
а не вычислены из него (иначе набор остался бы зелёным при любом его значении,
`PROTOCOL §3`). Порог заперт с двух сторон — и «пропускает то, что должен»,
и «не пропускает ничего сверх»: каждый отказ дополнительно утверждает, что
статус не сдвинут, транзакция не коммитилась и задача не отправлялась.
"""
import asyncio
import uuid

import celery
import pytest
from fastapi import HTTPException

from app.api.segmentation import start_segmentation
from app.models import Diagram, DiagramStatus

UID = uuid.UUID("d74eb9f1-1111-2222-3333-444455556666")

DIRECTION_TASK = "worker.tasks.direction.task_classify_direction"
SEGMENT_TASK = "worker.tasks.segmentation.task_segment_pipes"
SKELETON_TASK = "worker.tasks.skeleton.task_skeletonize"
JUNCTION_TASK = "worker.tasks.junction.task_detect_junctions"

# Размер машины. Абсолютное число: новый статус обязан пройти через эту таблицу,
# а не проскочить мимо неё молча.
STATUS_COUNT = 31

# Какие статусы гейт пускает. Литерал снят чтением `_ALLOWED_STATUSES`.
ALLOWED_STATUSES = frozenset({
    "validated_bbox",
    "error",
})

# Значения `error_stage`, которые реально пишет конвейер (`set_diagram_error`),
# плюс те, что называет `_STAGE_DISPATCH`. `None` — ошибка без стадии.
ERROR_STAGES = [
    None,
    "detecting",
    "direction_classification",
    "segmenting",
    "skeletonizing",
    "skeletonizing_simple",
    "detecting_junctions",
    "building_graph",
    "validating_graph",
    "contour_extraction",
    "generating_fxml",
    "ocr",
]

# `error_stage` → (restart_from, целевой статус, отправленные задачи по порядку).
# Литерал снят чтением `_STAGE_DISPATCH` (`:21-26`) и развилки цепочки (`:96-105`):
# всё, чего нет в `_STAGE_DISPATCH`, сваливается в дефолт «с сегментации».
DISPATCH = {
    None:                       ("segmenting", "segmenting", (DIRECTION_TASK, SEGMENT_TASK)),
    "detecting":                ("segmenting", "segmenting", (DIRECTION_TASK, SEGMENT_TASK)),
    "direction_classification": ("direction_classification", "segmenting",
                                 (DIRECTION_TASK, SEGMENT_TASK)),
    "segmenting":               ("segmenting", "segmenting", (DIRECTION_TASK, SEGMENT_TASK)),
    "skeletonizing":            ("skeletonizing", "skeletonizing", (SKELETON_TASK,)),
    "skeletonizing_simple":     ("segmenting", "segmenting", (DIRECTION_TASK, SEGMENT_TASK)),
    "detecting_junctions":      ("detecting_junctions", "detecting_junctions", (JUNCTION_TASK,)),
    "building_graph":           ("segmenting", "segmenting", (DIRECTION_TASK, SEGMENT_TASK)),
    "validating_graph":         ("segmenting", "segmenting", (DIRECTION_TASK, SEGMENT_TASK)),
    "contour_extraction":       ("segmenting", "segmenting", (DIRECTION_TASK, SEGMENT_TASK)),
    "generating_fxml":          ("segmenting", "segmenting", (DIRECTION_TASK, SEGMENT_TASK)),
    "ocr":                      ("segmenting", "segmenting", (DIRECTION_TASK, SEGMENT_TASK)),
}


# ── харнесс ──────────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class FakeDB:
    """Поверхность `AsyncSession`, которой пользуется эндпоинт."""

    def __init__(self, diagram):
        self.diagram = diagram
        self.commits = 0

    async def execute(self, stmt):
        return _FakeResult(self.diagram)

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


class _AsyncResult:
    id = "task-0001"


@pytest.fixture
def dispatched(monkeypatch):
    """Журнал отправленных задач вместо живого брокера.

    Эндпоинт умеет ДВА способа отправки — `chain(...)` на полном запуске и
    голый `send_task` на частичном retry. Журнал один, порядок сохраняется:
    иначе «цепочка вместо одной задачи» проходила бы незамеченной.
    """
    from worker.celery_app import celery_app

    calls = []

    def _send_task(name, args=None, kwargs=None, **rest):
        calls.append({"name": name, "args": args})
        return _AsyncResult()

    class _FakeChain:
        def __init__(self, *signatures):
            self._signatures = signatures

        def apply_async(self, *a, **kw):
            for sig in self._signatures:
                calls.append({"name": sig["task"], "args": list(sig["args"])})
            return _AsyncResult()

    monkeypatch.setattr(celery_app, "send_task", _send_task)
    monkeypatch.setattr(celery, "chain", _FakeChain)
    return calls


def names(calls):
    """Имена отправленных задач по порядку."""
    return [c["name"] for c in calls]


# ── сторожа самой таблицы ────────────────────────────────────────────────

def test_status_machine_size_is_locked():
    """Машина ровно того размера, на который написана таблица."""
    assert len(list(DiagramStatus)) == STATUS_COUNT


def test_table_keys_name_real_statuses():
    """Сторож набора: ключ таблицы — существующий статус, а не опечатка."""
    for value in ALLOWED_STATUSES:
        assert DiagramStatus(value).value == value
    for _restart, target, _tasks in DISPATCH.values():
        assert DiagramStatus(target).value == target


def test_error_stage_vocabulary_covers_the_dispatch_table():
    """Перебор идёт по тому же словарю, который описывает таблица."""
    assert set(ERROR_STAGES) == set(DISPATCH)


# ── POST /{uid}/segment — полный перебор статусов ────────────────────────

@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_segment_gate_over_every_status(status, dispatched):
    """Каждый статус машины: пропущен ровно тот, что в таблице."""
    diagram = _diagram(status)
    db = FakeDB(diagram)

    if status.value in ALLOWED_STATUSES:
        result = asyncio.run(start_segmentation(UID, db=db))
        assert result["status"] == "segmenting"
        assert diagram.status is DiagramStatus.SEGMENTING
        assert db.commits == 1
        assert names(dispatched) == [DIRECTION_TASK, SEGMENT_TASK]
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(start_segmentation(UID, db=db))
        assert exc.value.status_code == 400
        assert diagram.status is status, "статус сдвинут отказавшим эндпоинтом"
        assert db.commits == 0, "транзакция закоммичена при отказе"
        assert dispatched == [], "задача отправлена при отказе"


@pytest.mark.parametrize("stage", ERROR_STAGES, ids=lambda s: str(s))
def test_segment_gate_on_error_by_stage(stage, dispatched):
    """Статус `error`: `error_stage` решает, С КАКОГО шага перезапускать."""
    diagram = _diagram(DiagramStatus.ERROR, error_stage=stage,
                       error_message="boom")
    db = FakeDB(diagram)

    restart_from, target, tasks = DISPATCH[stage]
    result = asyncio.run(start_segmentation(UID, db=db))

    assert result["restart_from"] == restart_from
    assert result["status"] == target
    assert diagram.status is DiagramStatus(target)
    assert db.commits == 1
    assert names(dispatched) == list(tasks)


# ── клетка пункта 1.12: направление ──────────────────────────────────────

def test_direction_failure_restarts_from_direction(dispatched):
    """⭐ Упавшее направление перезапускается С НАПРАВЛЕНИЯ, не с сегментации.

    Это и есть ответ на вопрос «где чинить 1.12»: выход из тупика на сервере
    УЖЕ ЕСТЬ и он правильный — цепочка идёт заново с классификации, потому что
    `coco_validated.json` без `direction` испортил бы и `node_mask`, и граф.
    """
    diagram = _diagram(DiagramStatus.ERROR,
                       error_stage="direction_classification",
                       error_message="Direction classification timed out")
    db = FakeDB(diagram)

    result = asyncio.run(start_segmentation(UID, db=db))

    assert result["restart_from"] == "direction_classification"
    assert names(dispatched) == [DIRECTION_TASK, SEGMENT_TASK]
    assert names(dispatched)[0] == DIRECTION_TASK, "цепочка стартует не с направления"


def test_partial_retry_does_not_replay_direction(dispatched):
    """Частичный retry (скелет/узлы) направление НЕ переигрывает.

    Порог с другой стороны: «пускает error» не означает «всегда гонит цепочку
    сначала» — иначе повтор скелетизации молча перезаписывал бы `direction`
    в `coco_validated.json`.
    """
    db = FakeDB(_diagram(DiagramStatus.ERROR, error_stage="skeletonizing"))
    asyncio.run(start_segmentation(UID, db=db))
    assert names(dispatched) == [SKELETON_TASK]

    db = FakeDB(_diagram(DiagramStatus.ERROR, error_stage="detecting_junctions"))
    dispatched.clear()
    asyncio.run(start_segmentation(UID, db=db))
    assert names(dispatched) == [JUNCTION_TASK]


# ── общие свойства перехода ──────────────────────────────────────────────

def test_restart_clears_error(dispatched):
    """Повтор снимает ошибку вместе со сменой статуса.

    Иначе диаграмма бежит в `segmenting` с протухшим `error_stage`, а на нём
    стоят гейты клиента (красная кнопка) и `POST /diagrams/{uid}/retry`.
    """
    diagram = _diagram(DiagramStatus.ERROR,
                       error_stage="direction_classification",
                       error_message="Direction classification timed out")
    db = FakeDB(diagram)

    asyncio.run(start_segmentation(UID, db=db))

    assert diagram.status is DiagramStatus.SEGMENTING
    assert diagram.error_stage is None
    assert diagram.error_message is None


def test_missing_diagram_is_404(dispatched):
    """Нет диаграммы — 404, а не 400 гейта."""
    db = FakeDB(None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(start_segmentation(UID, db=db))
    assert exc.value.status_code == 404
    assert dispatched == []


def test_refusal_names_the_expected_statuses(dispatched):
    """Отказ говорит оператору, чего ждали, — а не «нельзя»."""
    db = FakeDB(_diagram(DiagramStatus.SKELETONIZED))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(start_segmentation(UID, db=db))
    assert "skeletonized" in str(exc.value.detail)
    assert "validated_bbox or error" in str(exc.value.detail)


def test_task_args_carry_project_code(dispatched):
    """Аргументы задач: uid всегда, `project_code` — там, где воркер его ждёт.

    Цепочка направления зовётся `immutable=True`, поэтому её звенья несут
    собственные аргументы: направление берёт конфиг проекта по диаграмме,
    сегментация — параметром.
    """
    db = FakeDB(_diagram(DiagramStatus.ERROR, error_stage="skeletonizing"))
    asyncio.run(start_segmentation(UID, db=db))
    assert dispatched == [
        {"name": SKELETON_TASK, "args": [str(UID), "thermohydraulics"]},
    ]

    db = FakeDB(_diagram(DiagramStatus.VALIDATED_BBOX))
    dispatched.clear()
    asyncio.run(start_segmentation(UID, db=db))
    assert dispatched == [
        {"name": DIRECTION_TASK, "args": [str(UID)]},
        {"name": SEGMENT_TASK, "args": [str(UID), "thermohydraulics"]},
    ]

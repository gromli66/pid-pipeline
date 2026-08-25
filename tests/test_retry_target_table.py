# -*- coding: utf-8 -*-
"""Нога 1.15 пункта 1-13 — таблица переходов `POST /api/diagrams/{uid}/retry`.

Что стережёт. `retry_operation` (`app/api/diagrams.py:427`) — единственная дверь,
которой оператор выходит из тупика «ноль кнопок из 13»: клиент по ней не гадает,
куда откатывать, а спрашивает сервер (страховка `_offer_error_retry`, пункт 1-36).
Сервер отвечает картой `error_stage → предыдущий статус`, а на незнакомом ключе —
дефолтом `UPLOADED`, то есть САМЫМ НАЧАЛОМ конвейера: в `uploaded` у оператора
доступна ровно одна кнопка «Очистка рамки», и детекцию, CVAT-валидацию и валидацию
масок (ручные этапы) придётся проходить заново. Артефакты при этом не удаляются —
`retry_operation` трогает только статус.

Почему решётка, а не пара примеров. Класс [сма] по `PROTOCOL`: таблица переходов
фиксируется ОТДЕЛЬНЫМ коммитом ДО правки и перебирается по ПОЛНОМУ множеству —
31 статус (`ast`-разбор `app/models/diagram.py`; доки пишут 29, адрес починки 0-3)
× 13 значений `error_stage`. После правки обязана измениться ровно заявленная
часть решётки, и обе редакции лежат здесь независимыми литералами.

Множество `error_stage` снимается `ast`-разбором `app/**` и `worker/**` в самом
тесте, а не переписывается руками: новый писатель попадает в перебор сам, а не
когда о нём вспомнят (правило набора 1-36, `MEASUREMENTS §85б`). Писателем
считается присваивание `x.error_stage = "константа"` и четвёртый аргумент
`set_diagram_error(db, uid, msg, "константа")`; `= None` — это очистка, а не
значение, и в множество не идёт.

Судит НАСТОЯЩАЯ корутина эндпоинта с поддельной сессией БД — не моё
представление о том, что она пропускает (образец — `tests/ui/test_error_stage_contract.py`).
"""
import ast
import asyncio
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.diagrams import retry_operation
from app.models import Diagram, DiagramStatus

UID = uuid.UUID("d74eb9f1-1111-2222-3333-444455556666")
REPO_ROOT = Path(__file__).resolve().parent.parent
ERROR_TEXT = "worker упал: подробности в логе"

# ── множества, снятые с кода ─────────────────────────────────────────────


def _written_error_stages(package: str):
    """Константы, которые пакет кладёт в `Diagram.error_stage`.

    Возвращает (константы, число динамических писателей). Область разбора —
    только сам пакет; `_scratch/`, `.venv*`, `storage/` в неё не входят,
    потому что путь строится от корня репозитория, а не грепом по диску.
    """
    const, dynamic = {}, []
    for path in sorted((REPO_ROOT / package).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if not (isinstance(tgt, ast.Attribute) and tgt.attr == "error_stage"):
                        continue
                    val = node.value
                    if isinstance(val, ast.Constant) and isinstance(val.value, str):
                        const.setdefault(val.value, []).append(f"{rel}:{node.lineno}")
                    elif isinstance(val, ast.Constant) and val.value is None:
                        pass                      # очистка флага, а не значение
                    else:
                        dynamic.append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name == "set_diagram_error" and len(node.args) >= 4:
                    arg = node.args[3]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        const.setdefault(arg.value, []).append(f"{rel}:{node.lineno}")
                    else:
                        dynamic.append(f"{rel}:{node.lineno}")
    return const, dynamic


API_CONST, API_DYNAMIC = _written_error_stages("app")
WORKER_CONST, WORKER_DYNAMIC = _written_error_stages("worker")
WRITTEN = sorted(set(API_CONST) | set(WORKER_CONST))

# Полный перебор: всё, что пишут, плюс отсутствие значения, плюс заведомо чужая
# строка — писатель, которого сегодня нет (динамический `safe_dispatch` кладёт
# именно такое: `task_name.split('.')[-1]`).
CASES = WRITTEN + [None, "totally_unknown_stage"]
STATUSES = list(DiagramStatus)

# ── редакция таблицы ДО правки ───────────────────────────────────────────
# Снята исполнением настоящей корутины 2026-08-20 (MEASUREMENTS §98), записана
# сюда независимым литералом: тест не имеет права вычислять ожидание из той
# самой карты, которую проверяет.
TABLE_BEFORE = {
    "building_graph": "validated_junctions",
    "contour_extraction": "uploaded",          # ⛔ начало конвейера
    "detecting": "frame_cleaned",
    "detecting_junctions": "skeletonized_final",
    "direction_classification": "uploaded",    # ⛔ дефект ноги 1.15
    "fetching_annotations": "validating_bbox",
    "generating_fxml": "uploaded",             # ⛔ начало конвейера
    "ocr": "uploaded",                         # ⛔ начало конвейера
    "segmenting": "validated_bbox",
    "skeletonizing": "segmenting",
    "skeletonizing_simple": "validated_masks",
    None: "uploaded",
    "totally_unknown_stage": "uploaded",
}

# ── редакция таблицы ПОСЛЕ правки ────────────────────────────────────────
# Второй независимый литерал, а не выражение «TABLE_BEFORE плюс правки»:
# иначе редакции перестали бы быть независимыми, и сторож разницы ниже
# сравнивал бы литерал сам с собой.
# Клетка переименована вслед за писателем (pains-1, боль 1/Б16): цель у обоих
# ключей одна — `validated_masks` (`app/api/diagrams.py` знает и старый, и
# новый), поэтому решётка не изменилась, изменилось имя, под которым её
# спрашивают. Снимок ДО правки остаётся историческим и не переписывается.
RENAMED_BY_PAINS_1 = {"skeletonizing_simple": "skeletonizing_final"}

TABLE_AFTER = {
    "building_graph": "validated_junctions",
    "contour_extraction": "validated_graph",
    "detecting": "frame_cleaned",
    "detecting_junctions": "skeletonized_final",
    "direction_classification": "validated_bbox",
    "fetching_annotations": "validating_bbox",
    "generating_fxml": "ocr_bound",
    "ocr": "validated_graph",
    "segmenting": "validated_bbox",
    "skeletonizing": "segmenting",
    "skeletonizing_final": "validated_masks",
    None: "uploaded",
    "totally_unknown_stage": "uploaded",
}

# Действующая редакция.
TABLE = TABLE_AFTER


# ── поддельная сессия БД: гейт настоящий ─────────────────────────────────


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class FakeDB:
    def __init__(self, diagram):
        self.diagram = diagram
        self.commits = 0

    async def execute(self, stmt):
        return _FakeResult(self.diagram)

    async def commit(self):
        self.commits += 1


def make_diagram(status, error_stage):
    d = Diagram()
    d.uid = UID
    d.number = 7
    d.project_code = "thermohydraulics"
    d.original_filename = "shema.png"
    d.status = status
    d.error_stage = error_stage
    d.error_message = ERROR_TEXT
    return d


def call_retry(status, error_stage):
    """Настоящая корутина эндпоинта. Возвращает (ответ|код ошибки, БД)."""
    db = FakeDB(make_diagram(status, error_stage))
    try:
        return asyncio.run(retry_operation(UID, db=db)), db
    except HTTPException as exc:
        return exc.status_code, db


# ── часть 1: множества, по которым идёт перебор ──────────────────────────


def test_written_values_are_exactly_eleven():
    """Одиннадцать значений — один писатель в `app`, десять в `worker`.

    Число не круглое и не из головы: `app` ставит `ERROR` ровно в одном месте
    (`app/api/cvat.py`), остальное пишут задачи воркера. Разойдётся — решётка
    ниже станет неполной, и об этом обязан сказать этот тест, а не ревизор.
    """
    assert len(WRITTEN) == 11, WRITTEN
    assert sorted(API_CONST) == ["fetching_annotations"], API_CONST
    assert len(WORKER_CONST) == 10, sorted(WORKER_CONST)


def test_status_machine_carries_thirty_one_values():
    """31 статус, а не 29 из доков (`GLOSSARY.md:26`, `UI_GUIDE.md:559`)."""
    assert len(STATUSES) == 31


def test_dynamic_writers_are_two_and_both_in_worker():
    """Динамических писателей два, оба в `worker/utils/db_helpers.py`.

    Пока их столько, множество перечислимо и решётка полна. Появится третий —
    перебор перестанет быть полным, и это надо заметить здесь.
    """
    assert API_DYNAMIC == [], API_DYNAMIC
    assert len(WORKER_DYNAMIC) == 2, WORKER_DYNAMIC
    assert all(a.startswith("worker/utils/db_helpers.py:") for a in WORKER_DYNAMIC)


def test_grid_covers_the_full_set():
    """Решётка — 31 × 13 = 403 клетки, а не диагональ."""
    assert len(STATUSES) * len(CASES) == 403
    assert set(TABLE) == set(CASES)


# ── часть 2: решётка переходов ───────────────────────────────────────────


@pytest.mark.parametrize("stage", CASES, ids=lambda s: str(s))
@pytest.mark.parametrize("status", STATUSES, ids=lambda s: s.value)
def test_retry_transition(status, stage):
    """Полный перебор: куда эндпоинт уводит диаграмму из каждой клетки."""
    out, db = call_retry(status, stage)

    if status is not DiagramStatus.ERROR:
        # Дверь узкая: retry живёт ровно в `error`. Отказ ничего не мутирует —
        # ни статуса, ни полей ошибки, ни коммита.
        assert out == 400, f"{status.value} × {stage}: ожидался отказ, получено {out}"
        assert db.diagram.status is status
        assert db.diagram.error_stage == stage
        assert db.diagram.error_message == ERROR_TEXT
        assert db.commits == 0
        return

    assert out["status"] == TABLE[stage], (
        f"error × {stage}: ушли в '{out['status']}', таблица обещает '{TABLE[stage]}'"
    )
    assert db.diagram.status.value == TABLE[stage]
    # Флаги ошибки снимаются — иначе клиент гасит все кнопки на пустом
    # `error_stage` (`_error_key → None`, замер ноги 1.14).
    assert db.diagram.error_stage is None
    assert db.diagram.error_message is None
    assert db.commits == 1


def test_missing_diagram_is_404():
    """Порог с другой стороны: диаграммы нет — 404, а не откат."""
    db = FakeDB(None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(retry_operation(UID, db=db))
    assert exc.value.status_code == 404
    assert db.commits == 0


# ── часть 3: кто падает в дефолт ─────────────────────────────────────────


def test_no_written_value_falls_into_the_default():
    """Ни одно ЗАПИСЫВАЕМОЕ значение больше не уводит в начало конвейера.

    Это и есть предмет ноги 1.15. Число абсолютное и меняется той же правкой,
    что и карта: до правки в дефолт падали четыре значения, после — ноль.
    """
    fallen = sorted(s for s in WRITTEN if TABLE[s] == "uploaded")
    assert fallen == [], fallen


def test_the_edit_changed_exactly_four_cells():
    """Сторож разницы: правка сдвинула РОВНО заявленное, и ничего больше.

    Обе редакции лежат выше независимыми литералами; остальные 399 клеток
    решётки проверяются прогоном по действующей карте (`test_retry_transition`).
    """
    diff = {
        k: (TABLE_BEFORE[k], TABLE_AFTER[RENAMED_BY_PAINS_1.get(k, k)])
        for k in TABLE_BEFORE
        if TABLE_BEFORE[k] != TABLE_AFTER[RENAMED_BY_PAINS_1.get(k, k)]
    }
    assert diff == {
        "direction_classification": ("uploaded", "validated_bbox"),
        "contour_extraction": ("uploaded", "validated_graph"),
        "ocr": ("uploaded", "validated_graph"),
        "generating_fxml": ("uploaded", "ocr_bound"),
    }
    assert len(TABLE_BEFORE) == len(TABLE_AFTER) == len(CASES) == 13


def test_unknown_and_absent_values_still_land_at_the_start():
    """Граница пункта: дефолт НЕ чинится, и это заявлено, а не забыто.

    Незнакомое значение и его отсутствие по-прежнему уводят в `uploaded`.
    Честная починка требует считать точку отката из `ProcessingStage`, то есть
    серверного реестра «стадия → статус», которого в `app/` нет вовсе, — это
    волна 5. Тест держит границу видимой: снимут дефолт — придут сюда.
    """
    assert TABLE[None] == "uploaded"
    assert TABLE["totally_unknown_stage"] == "uploaded"

# -*- coding: utf-8 -*-
"""Откат по бусине: полная решётка `POST /api/diagrams/{uid}/rollback` (блок 3, Н1).

Зачем файл. Эндпоинтных тестов на `app/api/rollback.py` не было **ни одного**
(греп `rollback_diagram` по `tests/` до этой сессии давал только клиентские наборы
`tests/ui/test_no_silent_rollback.py`, `tests/ui/test_masks_completion_once.py`,
`tests/test_frame_status_gate.py` — все они подделывают откат целиком и о настоящей
корутине не знают ничего). Класс правки — [сма]: `PROTOCOL §3` требует СНАЧАЛА
зафиксировать текущие переходы и только потом их менять, поэтому первая редакция
файла снята с НЕТРОНУТОГО кода и зелена на нём.

Что судится. Настоящая `app.api.rollback.rollback_diagram`: `AsyncSession` подделана
(её поверхность здесь — `execute` для `select` и для `delete` + `commit`), хранилище
уведено в `tmp_path`, `dispatch_layout` замокан. Живой БД, брокера и Celery не нужно.

Три вопроса, на которые файл отвечает числами:

  1. **Гейт цели** — какая пара «текущий статус × цель» проходит. Решётка полная:
     31 статус × 16 целей = 496 клеток, ожидание — литеральная функция от порядка
     конвейера, а не копия проверяемого условия.
  2. **Что сносится** — множество типов артефактов и файлы на диске. Граница холста
     («Ручная правка») названа абсолютным списком, не вычислена из `_STAGE_ARTIFACTS`.
  3. **Раскладка** — зовётся ли `dispatch_layout` и с каким `force`. Флаг важен сам
     по себе: `force=True` снимает `operator_saved` (`layout_dispatch.py:243`), и
     воркер через 1-2 минуты перезаписывает сохранённый оператором холст
     (`worker/tasks/layout.py:216-219`).

Четвёртый путь удаления — `POST /api/cvat/{uid}/reopen-validation`: он зовёт ту же
`_artifacts_to_delete` и обязан сносить строку и файл ВМЕСТЕ с ней, иначе
`graph_canvas.json` остаётся сиротой со своим `operator_saved`.
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete as sa_delete

import app.api.rollback as rollback_api
import app.services.storage as storage_mod
from app.api.rollback import (
    _STAGE_ARTIFACTS,
    _STAGE_ORDER,
    _artifacts_to_delete,
    rollback_diagram,
)
from app.models import Artifact, ArtifactType, Diagram, DiagramStatus

UID = uuid.UUID("50110bac-1111-2222-3333-444455556666")

# Размер машины. Абсолютное число: новый статус обязан пройти через решётку,
# а не проскочить мимо неё молча (родня `test_status_machine_size_is_locked`
# из `tests/test_validation_dispatch_failure_gate.py`).
STATUS_COUNT = 31

# Стабильные этапы, до которых откат вообще разрешён. Список литеральный —
# копия `_STAGE_ORDER` здесь была бы «входом из проверяемой константы»
# (`PROTOCOL §3`) и осталась бы зелёной при любой его правке.
STABLE_STAGES = [
    "uploaded",
    "frame_cleaned",
    "detected",
    "validated_bbox",
    "skeletonized",
    "validated_masks",
    "skeletonized_final",
    "detected_junctions",
    "validated_junctions",
    "built",
    "validated_graph",
    "contours_extracted",
    "contours_validated",
    "ocr_completed",
    "ocr_bound",
    "completed",
]

# Артефакты «Ручной правки». Своей стадии в `_STAGE_ORDER` у холста нет
# (его `done_status` — `generating_fxml`), поэтому он числится за `COMPLETED`.
CANVAS_ARTIFACTS = {ArtifactType.GRAPH_CANVAS, ArtifactType.RESIDUAL_DEFECTS}
CANVAS_FILES = ("graph_canvas.json", "residual_defects.json")

# Граница Н1: цель СТРОГО РАНЬШЕ этого этапа сносит холст, сама она и всё,
# что позже, — оставляет.
CANVAS_SURVIVES_FROM = "ocr_bound"

# Статусы, для которых порядок не судится вовсе: `ERROR` объявлен последним
# в перечислении и позиции в конвейере не означает — из него откат разрешён
# куда угодно (выход из тупика, `docs/STATUS_MACHINE.md §5`).
RANKLESS = {"error"}


# ── харнесс ──────────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, obj=None, rowcount=0):
        self._obj = obj
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._obj


class FakeDB:
    """Поверхность `AsyncSession`, которой пользуется откат.

    Журнал `deleted` копит ИМЕННО те типы, которые эндпоинт попросил снести —
    это и есть наблюдаемое поведение, а не наше представление о нём.
    """

    def __init__(self, diagram, present=()):
        self.diagram = diagram
        self.present = set(present)
        self.deleted = []
        self.commits = 0

    async def execute(self, stmt):
        if isinstance(stmt, type(sa_delete(Artifact))):
            types = _requested_types(stmt)
            self.deleted.append(types)
            hit = [t for t in types if t in self.present]
            self.present -= set(hit)
            return _FakeResult(rowcount=len(hit))
        return _FakeResult(self.diagram)

    async def commit(self):
        self.commits += 1


def _requested_types(stmt):
    """Типы артефактов из `IN (...)` в DELETE — как их видит база.

    `in_()` компилируется в РАСКРЫВАЕМЫЙ bindparam, поэтому в `params` лежит
    не набор скаляров, а один список.
    """
    types = []
    for _key, value in sorted(stmt.compile().params.items()):
        if isinstance(value, ArtifactType):
            types.append(value)
        elif isinstance(value, (list, tuple)):
            types.extend(v for v in value if isinstance(v, ArtifactType))
    return types


def _diagram(status_value):
    diagram = Diagram()
    diagram.uid = UID
    diagram.status = DiagramStatus(status_value)
    diagram.error_stage = "generating_fxml"
    diagram.error_message = "boom"
    diagram.project_code = "thermohydraulics"
    return diagram


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Хранилище на время теста + готовая папка графа с обоими файлами."""
    monkeypatch.setattr(storage_mod.settings, "STORAGE_PATH", str(tmp_path))
    graph_dir = tmp_path / str(UID) / "graph"
    graph_dir.mkdir(parents=True)
    for name in CANVAS_FILES:
        (graph_dir / name).write_text('{"operator_saved": true}', encoding="utf-8")
    return graph_dir


@pytest.fixture
def layout(monkeypatch):
    """Журнал вызовов раскладки: (uid, force). Сама раскладка — чужая подсистема."""
    calls = []

    async def _stub(uid, db, force=False):
        calls.append((str(uid), force))
        return {"status": "stub"}

    monkeypatch.setattr(rollback_api, "dispatch_layout", _stub)
    return calls


def _rollback(diagram, target, db=None, preserve_ocr=False, preserve_contours=False):
    """Вызов настоящей корутины — ровно с теми аргументами, что даёт FastAPI.

    ⛔ Флаги передаются ВСЕГДА, даже когда они `False`. Корутина зовётся напрямую,
    мимо разрешения зависимостей, поэтому её собственные дефолты — это объекты
    `Query(False)`, а они ИСТИННЫ: пропущенный аргумент включил бы сохранение OCR
    и контуров, и решётка судила бы не тот путь. Сторож факта —
    `test_bench_passes_the_flags_fastapi_would_pass`.
    """
    db = db if db is not None else FakeDB(diagram)
    return asyncio.run(rollback_diagram(
        UID, target_status=target, db=db,
        preserve_ocr=preserve_ocr, preserve_contours=preserve_contours,
    )), db


# ── сторожа самих таблиц ─────────────────────────────────────────────────

def test_status_machine_size_is_locked():
    """Машина ровно того размера, на который написана решётка."""
    assert len(list(DiagramStatus)) == STATUS_COUNT


def test_stable_stages_match_the_endpoint():
    """Литеральный список целей — тот же, что знает эндпоинт."""
    assert [s.value for s in _STAGE_ORDER] == STABLE_STAGES


def test_stage_order_is_a_subsequence_of_the_enum():
    """Порядок стабильных этапов — подпоследовательность порядка объявления.

    На этом стоит вся арифметика «раньше/позже»: у промежуточных `*ING`-статусов
    позиции в `_STAGE_ORDER` нет, и единственный доступный порядок для них —
    порядок объявления `DiagramStatus`. Разъедься эти два порядка — гейт цели
    начал бы судить по чужой шкале, и тест обязан сказать об этом вслух.
    """
    declared = [s.value for s in DiagramStatus]
    positions = [declared.index(v) for v in STABLE_STAGES]
    assert positions == sorted(positions)


def test_bench_passes_the_flags_fastapi_would_pass(storage, layout):
    """Стенд не наследует дефолты корутины — они не `False`, а `Query(False)`.

    Проверяется НАШЕ решение стенда, а не свойство библиотеки: пропущенный
    аргумент даёт истинный `Query`, и весь набор молча судил бы путь
    «сохранить OCR и контуры». Клетка ловит это одним наблюдаемым различием.
    """
    _result, kept = _rollback(_diagram("completed"), "validated_graph",
                              preserve_ocr=True)
    _result, cleaned = _rollback(_diagram("completed"), "validated_graph")
    assert ArtifactType.OCR_RESULT not in set(kept.deleted[0])
    assert ArtifactType.OCR_RESULT in set(cleaned.deleted[0]), (
        "стенд забыл передать флаги — решётка судит не тот путь"
    )


def test_canvas_artifacts_are_owned_by_completed():
    """Холст числится за `COMPLETED` — там же, где FXML."""
    owned = set(_STAGE_ARTIFACTS[DiagramStatus.COMPLETED])
    assert CANVAS_ARTIFACTS <= owned
    assert ArtifactType.FXML in owned


# ── 1. гейт цели: полная решётка 31 × 16 ─────────────────────────────────

def _rank(value):
    """Позиция статуса в конвейере — по порядку объявления перечисления."""
    return [s.value for s in DiagramStatus].index(value)


def _gate_allows(current, target):
    """Пускает ли гейт цели сегодня (редакция ДО Н1).

    Условие эндпоинта смотрит только в `_STAGE_ORDER`: у статуса, которого там
    нет (любой `*ING`, включая штатный `generating_fxml`, и `error`),
    `current_idx = -1`, и проверка `target_idx >= current_idx and
    current_idx >= 0` ложна — проходит ЛЮБАЯ цель, в том числе движение ВПЕРЁД.
    """
    if current not in STABLE_STAGES:
        return True
    return STABLE_STAGES.index(target) < STABLE_STAGES.index(current)


@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_target_gate_over_every_status(status, storage, layout):
    """Каждая клетка решётки: пропущена ровно та, что в таблице."""
    for target in STABLE_STAGES:
        diagram = _diagram(status.value)
        allowed = _gate_allows(status.value, target)
        cell = (status.value, target)

        if not allowed:
            with pytest.raises(HTTPException) as exc:
                _rollback(diagram, target)
            assert exc.value.status_code == 400, cell
            assert diagram.status.value == status.value, cell
            continue

        result, db = _rollback(diagram, target)
        assert result["status"] == target, cell
        assert diagram.status.value == target, cell
        assert db.commits == 1, cell


def test_forward_rollback_from_generating_fxml_is_allowed_today(storage, layout):
    """Дыра гейта, названная адресно: из `generating_fxml` проходит ВПЕРЁД.

    Клетка выписана отдельно от решётки, потому что это дефект, а не свойство:
    штатный статус «идёт экспорт» в `_STAGE_ORDER` не значится, поэтому «откат»
    в `completed` принимается — со сносом артефактов и статусом готовой схемы.
    """
    diagram = _diagram("generating_fxml")
    result, _db = _rollback(diagram, "completed")
    assert result["status"] == "completed"
    assert diagram.status is DiagramStatus.COMPLETED


def test_rollback_to_the_same_stage_is_refused(storage, layout):
    """Порог заперт с другой стороны: цель == текущему стабильному этапу — 400."""
    diagram = _diagram("ocr_bound")
    with pytest.raises(HTTPException) as exc:
        _rollback(diagram, "ocr_bound")
    assert exc.value.status_code == 400


def test_unknown_and_unstable_targets_are_refused(storage, layout):
    """Цель — не статус вовсе или промежуточный `*ING`: 400 и разные тексты."""
    diagram = _diagram("completed")
    with pytest.raises(HTTPException) as exc:
        _rollback(diagram, "no_such_status")
    assert exc.value.status_code == 400
    assert "Invalid target_status" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        _rollback(_diagram("completed"), "generating_fxml")
    assert exc.value.status_code == 400
    assert "not a stable stage" in exc.value.detail


def test_missing_diagram_is_404(storage, layout):
    db = FakeDB(None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rollback_diagram(UID, target_status="built", db=db))
    assert exc.value.status_code == 404


# ── 2. что сносится: строки и файлы ──────────────────────────────────────

# Ожидание СНЯТО ЧТЕНИЕМ `_STAGE_ARTIFACTS`, а не вычислено из него: таблица
# ниже — независимый литерал, и правка кода без правки таблицы краснеет здесь.
DOOMED_BY_TARGET = {
    "ocr_bound": {ArtifactType.FXML,
                  ArtifactType.GRAPH_CANVAS, ArtifactType.RESIDUAL_DEFECTS},
    "ocr_completed": {ArtifactType.OCR_VALIDATION, ArtifactType.FXML,
                      ArtifactType.GRAPH_CANVAS, ArtifactType.RESIDUAL_DEFECTS},
    "contours_validated": {ArtifactType.OCR_CLEANED, ArtifactType.OCR_RESULT,
                           ArtifactType.OCR_BINDING, ArtifactType.OCR_VALIDATION,
                           ArtifactType.FXML, ArtifactType.GRAPH_CANVAS,
                           ArtifactType.RESIDUAL_DEFECTS},
    "validated_graph": {ArtifactType.CONTOURS_AUTO, ArtifactType.CONTOURS_VALIDATED,
                        ArtifactType.OCR_CLEANED, ArtifactType.OCR_RESULT,
                        ArtifactType.OCR_BINDING, ArtifactType.OCR_VALIDATION,
                        ArtifactType.FXML, ArtifactType.GRAPH_CANVAS,
                        ArtifactType.RESIDUAL_DEFECTS},
}


@pytest.mark.parametrize("target, doomed", sorted(DOOMED_BY_TARGET.items()))
def test_deleted_types_for_target(target, doomed, storage, layout):
    """Типы, которые эндпоинт просит снести, — ровно объявленные."""
    diagram = _diagram("completed")
    _result, db = _rollback(diagram, target)
    assert db.deleted, "DELETE не выполнялся вовсе"
    assert set(db.deleted[0]) == doomed, target


def test_canvas_dies_on_every_target_today(storage, layout):
    """ДО Н1 холст сносится при ЛЮБОЙ цели — включая возврат на привязку.

    Это и есть жалоба фронта 2: оператор вернулся на бусину «Привязка подписей»,
    чтобы поправить одну подпись, и потерял часы ручной раскладки.
    """
    for target in ("ocr_bound", "ocr_completed", "contours_validated",
                   "validated_graph", "built", "uploaded"):
        diagram = _diagram("completed")
        _result, db = _rollback(diagram, target)
        assert CANVAS_ARTIFACTS <= set(db.deleted[0]), target


@pytest.mark.parametrize("target", ["ocr_bound", "validated_graph"])
def test_canvas_files_are_unlinked(target, storage, layout):
    """Файлы холста сносятся с диска вместе со строками — обе цели, ДО Н1."""
    for name in CANVAS_FILES:
        assert (storage / name).exists(), "фикстура не разложила файлы"

    _result, _db = _rollback(_diagram("completed"), target)

    for name in CANVAS_FILES:
        assert not (storage / name).exists(), (target, name)


def test_deleted_count_is_the_number_of_rows_really_hit(storage, layout):
    """`deleted_artifacts` считает реально лежавшие строки, а не размер списка."""
    diagram = _diagram("completed")
    db = FakeDB(diagram, present={ArtifactType.FXML, ArtifactType.GRAPH_CANVAS})
    result, _db = _rollback(diagram, "ocr_bound", db=db)
    assert result["deleted_artifacts"] == 2


def test_preserve_flags_narrow_the_set(storage, layout):
    """Флаги сохранения снимают OCR и контуры — холст к ним не относится."""
    diagram = _diagram("completed")
    _result, db = _rollback(diagram, "validated_graph",
                            preserve_ocr=True, preserve_contours=True)
    doomed = set(db.deleted[0])
    assert ArtifactType.OCR_RESULT not in doomed
    assert ArtifactType.CONTOURS_VALIDATED not in doomed
    assert CANVAS_ARTIFACTS <= doomed, "preserve-флаги холста не касаются"


def test_error_fields_are_cleared(storage, layout):
    """Откат снимает ошибку — иначе диаграмма бежит с протухшим `error_stage`."""
    diagram = _diagram("completed")
    _rollback(diagram, "built")
    assert diagram.error_stage is None
    assert diagram.error_message is None


# ── 3. раскладка: зовётся ли и с каким force ─────────────────────────────

# Литеральная таблица: цель → (позвали ли раскладку, значение `force`).
# ДО Н1 условие эндпоинта — «цель >= contours_validated», флаг всегда `True`.
LAYOUT_BY_TARGET_BEFORE = {
    "ocr_bound": (True, True),
    "ocr_completed": (True, True),
    "contours_validated": (True, True),
    "contours_extracted": (False, None),
    "validated_graph": (False, None),
    "built": (False, None),
    "uploaded": (False, None),
}


@pytest.mark.parametrize("target, expected", sorted(LAYOUT_BY_TARGET_BEFORE.items()))
def test_layout_dispatch_per_target(target, expected, storage, layout):
    """Кого зовёт откат и с каким флагом — по таблице, а не по знанию редакции."""
    called, force = expected
    _rollback(_diagram("completed"), target)
    if not called:
        assert layout == [], target
    else:
        assert layout == [(str(UID), force)], target


def test_force_is_what_erases_operator_saved(storage, layout):
    """Почему флаг вообще важен: `force=True` снимает метку ручных правок.

    Утверждение о НАШЕМ решении, а не о свойствах чужого модуля: здесь
    проверяется, что откат передаёт `force` дальше, а что с ним делает
    раскладка, судит `tests/test_layout_dispatch_policy.py`.
    """
    from app.services.layout_policy import plan_dispatch, ALREADY_FRESH, DISPATCH

    fresh = {"layout_applied": True, "stale": False}
    assert plan_dispatch("sha", fresh, [], force=False)[0] == ALREADY_FRESH
    assert plan_dispatch("sha", fresh, [], force=True)[0] == DISPATCH

    _rollback(_diagram("completed"), "ocr_bound")
    assert layout == [(str(UID), True)], "откат зовёт пересчёт поверх свежего холста"


# ── 4. четвёртый путь удаления: переоткрытие валидации CVAT ──────────────

def test_cvat_reopen_asks_for_the_canvas_too():
    """`reopen-validation` зовёт ту же таблицу и просит снести холст.

    Файлы при этом остаются на диске: своей ветки удаления у этого пути нет
    (замер блока 3 — `app/api/cvat.py:562-572`). Осиротевший `graph_canvas.json`
    несёт `operator_saved`, который читают `worker/tasks/layout.py`
    и `app/services/layout_dispatch.py`.
    """
    doomed = set(_artifacts_to_delete(DiagramStatus.DETECTED))
    assert CANVAS_ARTIFACTS <= doomed

    import inspect

    import app.api.cvat as cvat_api

    source = inspect.getsource(cvat_api.reopen_bbox_validation)
    assert "_artifacts_to_delete" in source
    assert "unlink" not in source, "ветка удаления файлов появилась — обновить пункт"

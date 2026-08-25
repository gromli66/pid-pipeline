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
# (его `done_status` — `generating_fxml`); до Н1 он числился за `COMPLETED`
# вместе с FXML и потому погибал при ЛЮБОЙ цели отката.
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
        self.added = []

    async def execute(self, stmt):
        if isinstance(stmt, type(sa_delete(Artifact))):
            types = _requested_types(stmt)
            self.deleted.append(types)
            hit = [t for t in types if t in self.present]
            self.present -= set(hit)
            return _FakeResult(rowcount=len(hit))
        # SELECT: диаграмма или артефакт. Различать обязательно — иначе на
        # запрос артефакта приезжает диаграмма, и upsert идёт не той веткой.
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Diagram:
            return _FakeResult(self.diagram)
        return _FakeResult(None)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        pass


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


def test_canvas_artifacts_are_no_longer_owned_by_completed():
    """Холст выехал из `COMPLETED` в собственный список (Н1).

    Пока он лежал рядом с FXML, «удалить всё после цели» означало «удалить
    холст при любой цели» — своей стадии у него нет. Теперь у него своя
    граница, и `_STAGE_ARTIFACTS` про него не знает вовсе.
    """
    owned = set(_STAGE_ARTIFACTS[DiagramStatus.COMPLETED])
    assert owned == {ArtifactType.FXML}
    assert not (CANVAS_ARTIFACTS & owned)


def test_canvas_border_is_the_binding_stage():
    """Граница названа в коде тем же этапом, что и в решении Максима №5."""
    from app.api.rollback import _CANVAS_SURVIVES_FROM, canvas_dies

    assert _CANVAS_SURVIVES_FROM is DiagramStatus.OCR_BOUND
    assert canvas_dies(DiagramStatus.OCR_COMPLETED) is True
    assert canvas_dies(DiagramStatus.OCR_BOUND) is False
    assert canvas_dies(DiagramStatus.COMPLETED) is False


# ── 1. гейт цели: полная решётка 31 × 16 ─────────────────────────────────

def _rank(value):
    """Позиция статуса в конвейере — по порядку объявления перечисления."""
    return [s.value for s in DiagramStatus].index(value)


def _gate_allows(current, target):
    """Пускает ли гейт цели: цель СТРОГО РАНЬШЕ текущего статуса.

    Позиция берётся из порядка объявления `DiagramStatus` — у промежуточных
    `*ING` позиции в `_STAGE_ORDER` нет, и раньше проверка на них не
    срабатывала вовсе. `error` позиции в конвейере не означает: из него откат
    разрешён куда угодно, это штатный выход из тупика.
    """
    if current in RANKLESS:
        return True
    return _rank(target) < _rank(current)


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


def test_forward_rollback_from_generating_fxml_is_refused(storage, layout):
    """Закрытая дыра гейта: из `generating_fxml` вперёд больше не пройти.

    Клетка выписана отдельно от решётки, потому что это дефект, а не свойство:
    штатный статус «идёт экспорт» в `_STAGE_ORDER` не значится, поэтому «откат»
    в `completed` принимался — со сносом артефактов и статусом готовой схемы,
    пока задача экспорта ещё считала.
    """
    diagram = _diagram("generating_fxml")
    with pytest.raises(HTTPException) as exc:
        _rollback(diagram, "completed")
    assert exc.value.status_code == 400
    assert diagram.status is DiagramStatus.GENERATING_FXML

    # Порог заперт с другой стороны: НАЗАД из того же статуса — можно.
    back = _diagram("generating_fxml")
    _result, _db = _rollback(back, "ocr_bound")
    assert back.status is DiagramStatus.OCR_BOUND


def test_error_still_rolls_back_anywhere(storage, layout):
    """`error` остаётся без позиции: выход из тупика не заперт.

    Сужать его этот пункт не берётся — цена ошибки несимметрична: запертый
    `error` оставляет оператора без единой кнопки.
    """
    diagram = _diagram("error")
    _result, _db = _rollback(diagram, "completed")
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
    # Н1: возврат НА привязку холст не трогает — только FXML.
    "ocr_bound": {ArtifactType.FXML},
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


def test_canvas_survives_only_the_binding_target(storage, layout):
    """Оба берега границы Н1 в одном тесте — иначе она не граница, а слово.

    До правки холст сносился при ЛЮБОЙ цели: оператор возвращался на бусину
    «Привязка подписей», чтобы поправить одну подпись, и терял часы ручной
    раскладки (жалоба фронта 2).
    """
    survives = ("ocr_bound",)
    dies = ("ocr_completed", "contours_validated", "validated_graph",
            "built", "uploaded")

    for target in survives:
        _result, db = _rollback(_diagram("completed"), target)
        assert not (CANVAS_ARTIFACTS & set(db.deleted[0])), target
    for target in dies:
        _result, db = _rollback(_diagram("completed"), target)
        assert CANVAS_ARTIFACTS <= set(db.deleted[0]), target


def test_canvas_files_survive_the_binding_target(storage, layout):
    """Файл на диске идёт за строкой БД — и остаётся, и сносится ВМЕСТЕ с ней.

    Разъедься эти два решения — получился бы «файл без строки»: клиент холста
    не покажет (он ходит по БД), а раскладка его прочитает с диска и увидит
    чужой `operator_saved`.
    """
    for name in CANVAS_FILES:
        assert (storage / name).exists(), "фикстура не разложила файлы"

    _result, _db = _rollback(_diagram("completed"), "ocr_bound")
    for name in CANVAS_FILES:
        assert (storage / name).exists(), f"{name} снесён при возврате на привязку"

    _result, _db = _rollback(_diagram("completed"), "validated_graph")
    for name in CANVAS_FILES:
        assert not (storage / name).exists(), f"{name} пережил глубокий откат"


def test_operator_saved_survives_the_binding_target(storage, layout):
    """Главное наблюдение Н1: правки оператора переживают возврат на привязку.

    Утверждается РАЗНИЦА, а не совпадение: тот же откат на шаг глубже метку
    уносит вместе с файлом.
    """
    import json

    kept = _diagram("completed")
    _rollback(kept, "ocr_bound")
    saved = json.loads((storage / "graph_canvas.json").read_text(encoding="utf-8"))
    assert saved["operator_saved"] is True


def test_deleted_count_is_the_number_of_rows_really_hit(storage, layout):
    """`deleted_artifacts` считает реально лежавшие строки, а не размер списка."""
    diagram = _diagram("completed")
    db = FakeDB(diagram, present={ArtifactType.FXML, ArtifactType.GRAPH_CANVAS})
    result, _db = _rollback(diagram, "validated_graph", db=db)
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
# Условие ВЫЗОВА не менялось — «цель >= contours_validated». Менялся ФЛАГ:
# ДО Н1 он был `True` всегда, теперь совпадает с гибелью холста.
LAYOUT_BY_TARGET = {
    "ocr_bound": (True, False),
    "ocr_completed": (True, True),
    "contours_validated": (True, True),
    "contours_extracted": (False, None),
    "validated_graph": (False, None),
    "built": (False, None),
    "uploaded": (False, None),
}


@pytest.mark.parametrize("target, expected", sorted(LAYOUT_BY_TARGET.items()))
def test_layout_dispatch_per_target(target, expected, storage, layout):
    """Кого зовёт откат и с каким флагом — по таблице, а не по знанию редакции."""
    called, force = expected
    _rollback(_diagram("completed"), target)
    if not called:
        assert layout == [], target
    else:
        assert layout == [(str(UID), force)], target


def test_force_follows_the_canvas_and_nothing_else(storage, layout):
    """Почему флаг вообще важен: `force=True` снимает метку ручных правок.

    Если бы Н1 сберёг строку и файл, но оставил `force=True`, пункт
    аннулировал бы сам себя: раскладка сняла бы `operator_saved`, и воркер
    через 1-2 минуты переписал бы сохранённый холст. Поэтому предикат один
    на оба решения, и это утверждается прямо.

    Первая половина — про НАШЕ решение (что передаёт откат), вторая
    показывает, ЧЕМ отличаются два значения флага в политике раскладки;
    исход самой раскладки судит `tests/test_layout_dispatch_policy.py`.
    """
    from app.api.rollback import canvas_dies
    from app.services.layout_policy import plan_dispatch, ALREADY_FRESH, DISPATCH

    fresh = {"layout_applied": True, "stale": False}
    assert plan_dispatch("sha", fresh, [], force=False)[0] == ALREADY_FRESH
    assert plan_dispatch("sha", fresh, [], force=True)[0] == DISPATCH

    for target, (called, force) in LAYOUT_BY_TARGET.items():
        if not called:
            continue
        assert force == canvas_dies(DiagramStatus(target)), target


def test_dirty_tab_resurrects_the_canvas_after_a_deep_rollback(storage, layout,
                                                              monkeypatch):
    """Зафиксировано КАК ЕСТЬ: грязная вкладка возвращает холст после отката.

    Откат глубже привязки сносит холст и строку. Но открытая «Ручная правка»
    об этом не знает — канала «узнать о смене статуса» у вкладки нет — и её
    автосейв через 120 с шлёт `/graph/canvas/save`, гейт которого пускает
    `ocr_completed`. Холст возвращается вместе с меткой `operator_saved`.

    Пункт Н1 этот гейт НЕ трогает (граница блока): клетка названа тестом,
    чтобы правка отката не выглядела полнее, чем она есть. Настоящее лечение —
    заморозка буфера вкладки на 400 (кандидат блока 5).
    """
    import json

    from app.api.validation import save_canvas_graph

    class _Upload:
        async def read(self):
            return b'{"nodes": [{"id": "n1", "x": 1, "y": 2}], "edges": []}'

    diagram = _diagram("completed")
    _result, db = _rollback(diagram, "ocr_completed")
    assert diagram.status is DiagramStatus.OCR_COMPLETED
    assert not (storage / "graph_canvas.json").exists(), "откат холст не снёс"

    asyncio.run(save_canvas_graph(UID, file=_Upload(), db=db))

    revived = storage / "graph_canvas.json"
    assert revived.exists(), "поведение изменилось — пересверить границу пункта"
    state = json.loads(revived.read_text(encoding="utf-8"))
    assert state["graph"]["canvas_transform"]["operator_saved"] is True


# ── 4. четвёртый путь удаления: переоткрытие валидации CVAT ──────────────

def test_cvat_reopen_deletes_the_canvas_file_too(storage, monkeypatch):
    """`reopen-validation` сносит строку и файл ВМЕСТЕ — через общую функцию.

    Раньше у этого пути своей ветки удаления файлов не было: он звал ту же
    `_artifacts_to_delete` (а холст в её списке при цели `detected` есть) и
    удалял только строки. Осиротевший `graph_canvas.json` нёс `operator_saved`,
    который читают `worker/tasks/layout.py` и `app/services/layout_dispatch.py`.

    Тест судит НАСТОЯЩУЮ `purge_artifacts`, а не текст исходника: сторож по
    исходнику проверял бы форму, а не решение.
    """
    from app.api.rollback import purge_artifacts

    doomed = _artifacts_to_delete(DiagramStatus.DETECTED)
    assert CANVAS_ARTIFACTS <= set(doomed), "цель `detected` холст не сносит"
    for name in CANVAS_FILES:
        assert (storage / name).exists()

    db = FakeDB(_diagram("validating_bbox"),
                present={ArtifactType.GRAPH_CANVAS})
    deleted = asyncio.run(purge_artifacts(UID, doomed, db))

    assert deleted == 1
    for name in CANVAS_FILES:
        assert not (storage / name).exists(), f"{name} остался сиротой"


class _StagesResult:
    """`select(ProcessingStage)` → `.scalars().all()`; бегущих стадий нет."""

    def scalars(self):
        return self

    def all(self):
        return []


class CvatDB(FakeDB):
    """FakeDB, умеющая ещё и запрос стадий — его делает переоткрытие CVAT."""

    async def execute(self, stmt):
        if not isinstance(stmt, type(sa_delete(Artifact))):
            entity = stmt.column_descriptions[0]["entity"]
            if entity is not Diagram and entity is not Artifact:
                return _StagesResult()
        return await super().execute(stmt)


def test_cvat_reopen_endpoint_takes_the_canvas_file_with_it(storage, monkeypatch):
    """Настоящий эндпоинт переоткрытия — файл холста уходит вместе со строкой.

    Тест судит корутину `reopen_bbox_validation`, а не текст её исходника:
    сторож по исходнику проверял бы форму («вызывается ли функция с таким
    именем»), а не решение. Бегущих стадий нет — брокер не нужен.
    """
    import app.api.cvat as cvat_api

    diagram = _diagram("skeletonized")
    diagram.cvat_task_id = 42
    diagram.cvat_job_id = 43
    db = CvatDB(diagram, present={ArtifactType.GRAPH_CANVAS})

    for name in CANVAS_FILES:
        assert (storage / name).exists()

    asyncio.run(cvat_api.reopen_bbox_validation(UID, db=db))

    assert diagram.status is DiagramStatus.VALIDATING_BBOX
    assert CANVAS_ARTIFACTS <= set(db.deleted[0]), "холст не попал в удаление"
    for name in CANVAS_FILES:
        assert not (storage / name).exists(), f"{name} остался сиротой"

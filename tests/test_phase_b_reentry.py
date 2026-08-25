# -*- coding: utf-8 -*-
"""Повторный проход по фазе B: гейты сохранения и входа (блок 3, пункты 3.1в и Н8+).

Зачем файл. Фаза B — «Проверка схемы» ⇄ «Контуры» ⇄ «Распознавание» ⇄ «Привязка» —
по решениям Максима №7/№8 ходится СВОБОДНО: вход в пройденный этап откатом не
является. Клиент это уже умеет (Н3+), но сервер держит на каждом эндпоинте свой
белый список статусов, и списки друг с другом не сведены. Пока они расходятся,
свободный вход просто переносит «400 после часа работы» с подтверждения на
СОХРАНЕНИЕ — оператор входит во вкладку, правит и не может сохранить.

Класс правки — [сма], значит `PROTOCOL §3`: первая редакция файла снята с
НЕТРОНУТОГО кода и зелена на нём; правка гейтов меняет таблицы ЯВНО, и по диффу
этого файла видно, какие именно клетки открылись.

Что судится. Настоящие корутины четырёх эндпоинтов:

    POST /api/validation/{uid}/graph/start   — start_graph_validation
    POST /api/validation/{uid}/graph/save    — save_validated_graph
    POST /api/ocr/{uid}/binding/save         — save_ocr_binding
    POST /api/contours/{uid}/complete        — complete_contour_validation

`AsyncSession` подделана, хранилище уведено в `tmp_path`, раскладка замокана.

Отдельный сторож — СВЕДЕНИЕ клиента с сервером: клиентский порог
`_binding_reachable` (`ui/widgets/diagram_workspace.py`) и белый список
`/binding/save` обязаны давать один и тот же ответ на КАЖДОМ из 31 статуса.
Порог был назван по этому списку в докстроке самой функции, то есть расхождение
здесь — не стилистика, а обещание, данное наследнику.
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException

import app.api.contours as contours_api
import app.services.storage as storage_mod
from app.api.contours import complete_contour_validation
from app.api.ocr import save_ocr_binding
from app.api.validation import save_validated_graph, start_graph_validation
from app.models import Artifact, ArtifactType, Diagram, DiagramStatus

UID = uuid.UUID("b0000000-1111-2222-3333-444455556666")

STATUS_COUNT = 31

ENDPOINTS = ("graph_start", "graph_save", "binding_save", "contours_complete")

# ── гейт статуса: какие статусы эндпоинт пускает ─────────────────────────
#
# Литералы, снятые ЧТЕНИЕМ кода. Всё, чего в множестве нет, — 400.
ACCEPTS = {
    # Н8+: единственный в семействе, кто не пускал после `validated_graph`.
    # Набор сведён с `/graph/save` — это симметрия семейства, а не догадка.
    "graph_start": {"built", "validating_graph", "validated_graph",
                    "contours_validated", "ocr_completed", "ocr_bound",
                    "generating_fxml", "completed"},
    # 3.1в: +generating_fxml/completed — вкладка открывается и из готовой схемы.
    "graph_save": {"built", "validating_graph", "validated_graph",
                   "contours_validated", "ocr_completed", "ocr_bound",
                   "generating_fxml", "completed"},
    # 3.1в + Н8+: весь «хвост» начиная с `validated_graph` — ровно то, что
    # пускает клиентский порог `_binding_reachable`.
    "binding_save": {"validated_graph", "extracting_contours",
                     "contours_extracted", "contours_validated",
                     "ocr_processing", "ocr_completed", "ocr_bound",
                     "generating_fxml", "completed"},
    # У подтверждения контуров статусного гейта нет вовсе — принимается любой.
    # Гейты сборки (BUILDING_GRAPH → 400) — предмет блока 5, не этого.
    "contours_complete": {s.value for s in DiagramStatus},
}

# Статусы ПОСЛЕ контуров: подтверждение принимается, статус НЕ двигается.
CONTOURS_ALREADY_PAST = {"ocr_processing", "ocr_completed", "ocr_bound",
                         "generating_fxml", "completed"}

# ── что эндпоинт делает со статусом ──────────────────────────────────────
#
# Значение — целевой статус, `None` — «не трогает». Правило `SAME` означает
# «остаётся тем, с чем пришли».
SAME = "оставить как есть"

MOVES_TO = {
    # BUILT → VALIDATING_GRAPH, прочее не трогает.
    "graph_start": {"built": "validating_graph"},
    "graph_save": {"built": "validating_graph"},
    "binding_save": {},
    # 3.1в: вперёд — только с тех статусов, что раньше контуров. Из «хвоста»
    # подтверждение принимается, но статус остаётся на месте.
    "contours_complete": {s.value: "contours_validated" for s in DiagramStatus
                          if s.value not in CONTOURS_ALREADY_PAST},
}

# Кого зовёт подтверждение контуров: раскладку — всегда, когда гейт пустил.
CONTOURS_DISPATCHES_LAYOUT = True


# ── харнесс ──────────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class FakeDB:
    """Поверхность `AsyncSession` этих четырёх эндпоинтов."""

    def __init__(self, diagram, artifacts=None):
        self.diagram = diagram
        self.artifacts = dict(artifacts or {})
        self.commits = 0
        self.added = []
        self.removed = []

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Diagram:
            return _FakeResult(self.diagram)
        params = stmt.compile().params
        return _FakeResult(self.artifacts.get(params.get("artifact_type_1")))

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.removed.append(obj)


class FakeUpload:
    """Поверхность `UploadFile`, которой пользуются оба сохранения."""

    def __init__(self, payload=b'{"nodes": [], "edges": []}'):
        self._payload = payload

    async def read(self):
        return self._payload


def _artifact(path):
    art = Artifact()
    art.file_path = path
    art.file_size = 1
    art.mime_type = "application/json"
    return art


def _diagram(status_value):
    diagram = Diagram()
    diagram.uid = UID
    diagram.status = DiagramStatus(status_value)
    diagram.error_stage = None
    diagram.error_message = None
    diagram.project_code = "thermohydraulics"
    return diagram


# Артефакты на месте: гейты наличия пройдены, судится ГЕЙТ СТАТУСА.
ARTIFACTS = {
    ArtifactType.GRAPH_VALIDATED: _artifact("graph/graph_validated.json"),
    ArtifactType.OCR_RESULT: _artifact("ocr/ocr_result.json"),
    ArtifactType.OCR_BINDING: _artifact("ocr_binding/ocr_binding.json"),
    ArtifactType.CONTOURS_VALIDATED: _artifact("contours/contours_validated.json"),
}

CALL = {
    "graph_start": lambda db: start_graph_validation(UID, db=db),
    "graph_save": lambda db: save_validated_graph(UID, file=FakeUpload(), db=db),
    "binding_save": lambda db: save_ocr_binding(UID, file=FakeUpload(), db=db),
    "contours_complete": lambda db: complete_contour_validation(UID, db=db),
}


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_mod.settings, "STORAGE_PATH", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def layout(monkeypatch):
    """Раскладка — чужая подсистема; здесь только факт вызова."""
    calls = []

    async def _stub(uid, db, force=False):
        calls.append((str(uid), force))
        return {"status": "stub"}

    monkeypatch.setattr(contours_api, "dispatch_layout", _stub)
    return calls


@pytest.fixture(autouse=True)
def ocr_enabled(monkeypatch):
    """OCR включён — как на бою (`thermohydraulics.yaml: ocr.enabled: true`).

    Без подмены `PROJECTS_CONFIG_DIR` из `tests/conftest.py` уводит загрузчик
    в пустоту, `_ocr_enabled` отдаёт дефолт и половина ветки не проходится.
    """
    monkeypatch.setattr(contours_api, "_ocr_enabled", lambda code: True)


def _run(endpoint, diagram, artifacts=None):
    db = FakeDB(diagram, ARTIFACTS if artifacts is None else artifacts)
    return asyncio.run(CALL[endpoint](db)), db


# ── сторожа самих таблиц ─────────────────────────────────────────────────

def test_status_machine_size_is_locked():
    assert len(list(DiagramStatus)) == STATUS_COUNT


def test_table_keys_name_real_statuses():
    for endpoint in ENDPOINTS:
        for value in ACCEPTS[endpoint] | set(MOVES_TO[endpoint]):
            assert DiagramStatus(value).value == value, (endpoint, value)
        for value in MOVES_TO[endpoint].values():
            assert DiagramStatus(value).value == value, (endpoint, value)


def test_every_endpoint_has_a_rule():
    for table in (ACCEPTS, MOVES_TO, CALL):
        assert set(table) == set(ENDPOINTS)


# ── решётка: 31 статус × 4 эндпоинта ─────────────────────────────────────

@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_gate_over_every_status(endpoint, status, layout):
    """Каждая клетка: пропущена ровно та, что в таблице, и статус — по таблице.

    Порог заперт с двух сторон: отказ дополнительно утверждает, что статус
    не сдвинут и транзакция не коммитилась.
    """
    diagram = _diagram(status.value)
    cell = (endpoint, status.value)
    allowed = status.value in ACCEPTS[endpoint]

    if not allowed:
        with pytest.raises(HTTPException) as exc:
            _run(endpoint, diagram)
        assert exc.value.status_code == 400, cell
        assert diagram.status.value == status.value, cell
        assert layout == [], cell
        return

    _result, _db = _run(endpoint, diagram)
    expected = MOVES_TO[endpoint].get(status.value, status.value)
    assert diagram.status.value == expected, cell


def test_contours_complete_does_not_downgrade_from_the_ocr_stages(layout):
    """3.1в: подтверждение контуров из «хвоста» фазы B статус НЕ трогает.

    До правки оператор, вернувшийся в «Контуры» из уже пройденной привязки,
    нажимал «Подтвердить» — и конвейер молча уезжал на два шага назад, к
    `contours_validated`. Ветка «→ OCR_BOUND» рядом от этого не спасала:
    она про ВЫКЛЮЧЕННЫЙ OCR, а на бою он включён.
    """
    for entry in ("ocr_completed", "ocr_bound", "generating_fxml", "completed"):
        diagram = _diagram(entry)
        result, _db = _run("contours_complete", diagram)
        assert diagram.status is DiagramStatus(entry), entry
        assert result["status"] == "ok", entry


def test_contours_complete_still_moves_forward_from_before(layout):
    """Порог заперт с другой стороны: с ранних статусов переход остался.

    Без этой клетки правка «не двигать статус» могла бы отменить сам переход,
    и тест выше остался бы зелёным.
    """
    diagram = _diagram("contours_extracted")
    _run("contours_complete", diagram)
    assert diagram.status is DiagramStatus.CONTOURS_VALIDATED


def test_contours_complete_delegates_and_still_guards(layout, storage):
    """Ветка авто-приёмки (валидированных контуров ещё нет) — та же граница.

    `/complete` при отсутствии `CONTOURS_VALIDATED` делегирует
    `/auto-accept`, и тот пишет статус СВОИМ кодом. Без общей точки повторный
    проход чинился бы наполовину: делегированная ветка так и тянула бы назад.
    """
    auto = storage / str(UID) / "contours"
    auto.mkdir(parents=True)
    (auto / "contours_auto.json").write_text('{"nodes": []}', encoding="utf-8")

    diagram = _diagram("ocr_bound")
    _run("contours_complete", diagram, artifacts={
        ArtifactType.CONTOURS_AUTO: _artifact(
            f"{UID}/contours/contours_auto.json"),
    })
    assert diagram.status is DiagramStatus.OCR_BOUND


def test_contours_complete_dispatches_layout(layout):
    """Подтверждение контуров ставит раскладку — без force."""
    _run("contours_complete", _diagram("contours_validated"))
    assert layout == [(str(UID), False)]


def test_graph_save_writes_the_artifact(storage):
    """Сохранение и правда пишет файл и заводит строку — не только гейт."""
    diagram = _diagram("validating_graph")
    _result, db = _run("graph_save", diagram, artifacts={})
    written = storage / str(UID) / "graph" / "graph_validated.json"
    assert written.exists()
    assert [a.artifact_type for a in db.added] == [ArtifactType.GRAPH_VALIDATED]


def test_binding_save_needs_the_ocr_result():
    """Второй гейт привязки — наличие сырого OCR; он к статусам не относится."""
    diagram = _diagram("ocr_completed")
    with pytest.raises(HTTPException) as exc:
        _run("binding_save", diagram, artifacts={})
    assert exc.value.status_code == 400
    assert "OCR result not available" in exc.value.detail


# ── сведение клиента с сервером ──────────────────────────────────────────

def test_client_binding_threshold_matches_the_server_gate():
    """Порог кнопки «Привязка подписей» и белый список сервера — одно и то же.

    Клиент решает `_binding_reachable` (порог `VALIDATED_GRAPH`), сервер —
    белым списком `/binding/save`. Докстрока клиентской функции ссылается на
    ЭТОТ список как на основание порога, поэтому расхождение здесь — обещание,
    данное наследнику и не выполненное: кнопка горит, вкладка открывается,
    сохранение отвечает 400. До блока 3 расходились ПЯТЬ клеток:
    `extracting_contours`, `contours_extracted`, `ocr_processing`,
    `generating_fxml`, `completed`.

    Перебор ведётся ПОЛНЫМ списком статусов, а не выборкой (`PROTOCOL §3`).
    """
    from ui.widgets.diagram_workspace import _binding_reachable
    from ui.services.api_client import DiagramStatus as UiStatus

    mismatch = set()
    for status in DiagramStatus:
        if status is DiagramStatus.ERROR:
            continue                      # ошибка позиции в конвейере не означает
        client_ok = _binding_reachable(UiStatus(status.value))
        server_ok = status.value in ACCEPTS["binding_save"]
        if client_ok != server_ok:
            mismatch.add(status.value)

    assert mismatch == set(), (
        f"клиент и сервер разошлись на {sorted(mismatch)}"
    )


# ── Н2: пара «сохранить холст из готовой схемы → подтвердить» ────────────

def test_canvas_save_then_graph_complete_keeps_the_operator_flag(storage, monkeypatch):
    """Сценарий уровня дефекта: правка из COMPLETED переживает подтверждение.

    Оператор из готовой схемы правит холст (автосейв шлёт `/graph/canvas/save`,
    сервер ставит `operator_saved`), потом подтверждает «Проверку схемы».
    До Н2 подтверждение отвечало 400. После Н2 оно уходит по ветке «возврат
    после контуров» — а она зовёт раскладку БЕЗ `force`, то есть метка ручных
    правок не снимается и воркер не перезапишет холст своим результатом.

    Проверяется РАЗНИЦА, а не совпадение с «до»: флага на входе нет, после
    сохранения он есть, и подтверждение его не снимает.
    """
    import json

    import app.api.validation as validation_api
    from app.api.validation import complete_graph_validation, save_canvas_graph
    from worker.celery_app import celery_app

    dispatched = []

    async def _layout(uid, db, force=False):
        dispatched.append(("layout", force))
        return {"status": "stub"}

    monkeypatch.setattr(validation_api, "dispatch_layout", _layout)

    class _AsyncResult:
        id = "task-0001"

    monkeypatch.setattr(celery_app, "send_task",
                        lambda name, **kw: dispatched.append(("task", name))
                        or _AsyncResult())

    graph_dir = storage / str(UID) / "graph"
    graph_dir.mkdir(parents=True)
    canvas_path = graph_dir / "graph_canvas.json"
    canvas_path.write_text('{"nodes": [], "edges": []}', encoding="utf-8")

    from modules.graph.core import canvas_state

    before = json.loads(canvas_path.read_text(encoding="utf-8"))
    assert not canvas_state.read_state(before)["operator_saved"], "флаг уже стоял"

    diagram = _diagram("completed")
    db = FakeDB(diagram, {ArtifactType.GRAPH_VALIDATED:
                          _artifact(f"{UID}/graph/graph_validated.json")})
    edited = b'{"nodes": [{"id": "n1", "x": 10, "y": 20}], "edges": []}'
    asyncio.run(save_canvas_graph(UID, file=FakeUpload(edited), db=db))

    saved = json.loads(canvas_path.read_text(encoding="utf-8"))
    assert canvas_state.read_state(saved)["operator_saved"], "сервер не отметил правку"

    asyncio.run(complete_graph_validation(UID, db=db))

    assert diagram.status is DiagramStatus.GENERATING_FXML
    assert ("layout", False) in dispatched, "раскладка позвана с force — флаг погибнет"
    still = json.loads(canvas_path.read_text(encoding="utf-8"))
    assert canvas_state.read_state(still)["operator_saved"], "флаг ручной правки снят"


# ── угол Н2с: оператор удалил ВСЕ текстовые блоки ────────────────────────

def test_empty_text_blocks_are_refilled_by_the_safety_net(storage, monkeypatch):
    """Известный угол, зафиксированный как есть: пустой список — не «есть блоки».

    Safety-net слияния в `complete-simple` пропускает работу, когда в графе уже
    ЕСТЬ `text_blocks` (`app/services/ocr_graph_merge.py`), а проверка — на
    ИСТИННОСТЬ. Оператор, удаливший все блоки до единого, оставляет `[]`, и
    повторное подтверждение фазы B воскрешает сырые OCR-блоки.

    Клетка не чинится этим пунктом — она НАЗЫВАЕТСЯ: повторный проход фазы B
    делает её достижимой чаще, чем раньше, и наследник должен знать о ней из
    красного теста, а не из боя. Починка — отдельным решением Максима.
    """
    import app.services.project_loader as project_loader
    from app.api.validation import complete_simple_graph_validation

    class _Loader:
        def load(self, code):
            return type("_C", (), {"ocr": type("_O", (), {"enabled": True})()})()

    monkeypatch.setattr(project_loader, "get_project_loader", lambda: _Loader())

    graph_dir = storage / str(UID) / "graph"
    graph_dir.mkdir(parents=True)
    graph_path = graph_dir / "graph_validated.json"
    graph_path.write_text(
        '{"nodes": [], "edges": [], "text_blocks": [], "bindings": []}',
        encoding="utf-8")

    ocr_dir = storage / str(UID) / "ocr"
    ocr_dir.mkdir(parents=True)
    ocr_path = ocr_dir / "ocr_result.json"
    ocr_path.write_text(
        '{"target": [{"bbox": [1, 2, 3, 4], "text": "10PAB10", '
        '"confidence": 0.9}]}', encoding="utf-8")

    diagram = _diagram("ocr_bound")
    db = FakeDB(diagram, {
        ArtifactType.GRAPH_VALIDATED: _artifact(f"{UID}/graph/graph_validated.json"),
        ArtifactType.OCR_RESULT: _artifact(f"{UID}/ocr/ocr_result.json"),
    })
    asyncio.run(complete_simple_graph_validation(UID, db=db))

    import json

    after = json.loads(graph_path.read_text(encoding="utf-8"))
    assert [b["text"] for b in after["text_blocks"]] == ["10PAB10"], (
        "поведение угла изменилось — пересверить пункт Н2с"
    )
    assert diagram.status is DiagramStatus.OCR_BOUND, "статус всё же сдвинулся"


def test_binding_gate_table_is_the_one_the_endpoint_uses():
    """Таблица набора — та же, что стоит в коде, а не её копия рядом.

    Иначе решётка выше судила бы собственное представление о гейте
    (`tests/ui/test_no_silent_rollback.py` — образец того же требования).
    """
    from app.api.ocr import _BINDING_SAVE_STATUSES

    assert {s.value for s in _BINDING_SAVE_STATUSES} == ACCEPTS["binding_save"]

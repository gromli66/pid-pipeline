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
    "graph_start": {"built", "validating_graph"},
    "graph_save": {"built", "validating_graph", "validated_graph",
                   "contours_validated", "ocr_completed", "ocr_bound"},
    "binding_save": {"validated_graph", "contours_validated",
                     "ocr_completed", "ocr_bound"},
    # У подтверждения контуров статусного гейта нет вовсе — принимается любой.
    "contours_complete": {s.value for s in DiagramStatus},
}

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
    # ДО пункта 3.1в статус ставится БЕЗУСЛОВНО — из любого пропущенного.
    "contours_complete": {s.value: "contours_validated" for s in DiagramStatus},
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


def test_contours_complete_downgrades_from_the_ocr_stages(layout):
    """Названный адресно дефект 3.1в: подтверждение контуров тянет статус НАЗАД.

    Оператор вернулся в «Контуры» из уже пройденной привязки, нажал
    «Подтвердить» — и конвейер молча уехал на два шага назад, к
    `contours_validated`. Ветка «→ OCR_BOUND» рядом (`contours.py:375-378`)
    от этого не спасает: она про ВЫКЛЮЧЕННЫЙ OCR, а на бою он включён.
    """
    for entry in ("ocr_completed", "ocr_bound", "generating_fxml", "completed"):
        diagram = _diagram(entry)
        _run("contours_complete", diagram)
        assert diagram.status is DiagramStatus.CONTOURS_VALIDATED, entry


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
    сохранение отвечает 400.

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

    assert mismatch == {"extracting_contours", "contours_extracted",
                        "ocr_processing", "generating_fxml", "completed"}, (
        "состав расхождения изменился — пересверить обе стороны"
    )

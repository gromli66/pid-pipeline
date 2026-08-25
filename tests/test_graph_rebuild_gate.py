# -*- coding: utf-8 -*-
"""Пересборка графа против фазы B (блок 5 «точечных болей», 2026-08-25).

Зачем файл. Сборка графа заново («Сборка схемы» из уже готовой схемы, повтор
после ошибки) переписывает `graph.json`/`graph_validated.json` целиком, и `id`
узлов между сборками НЕ выживают — они порядковые `node_N` от порядка компонент
маски (`modules/graph/core/nodes.py`). Пока она идёт, любая работа фазы B
пишется в граф, которого через минуту не будет: подтверждение контуров
переставляет статус на `contours_validated` и ставит раскладку по
переписываемому графу, привязка вешает KKS на узлы ПРОШЛОГО поколения.

Решение Максима (2026-08-25): **на время сборки фаза B блокируется**. Форма
гейта — WHITELIST, а не «BUILDING и раньше»: чёрный список пропускает `ERROR`,
в который упавшая сборка и уходит (замечание редтима). `ERROR` разбирается
отдельно по `error_stage` (решение №3): упавшая сборка — 400, упавший OCR фазу B
не запирает.

Класс правки — [сма], значит `PROTOCOL §3`: **первая редакция этого файла снята
с НЕТРОНУТОГО кода и зелена на нём**. Таблица `ACCEPTS` ниже — литералы,
прочитанные в коде, а не вычисленные из него; правка гейтов меняет её ЯВНО, и по
диффу этого файла видно, какие именно клетки закрылись.

Что судится. Настоящие корутины десяти эндпоинтов — все, кто при пересборке
пишет или жжёт очередь:

    POST /api/contours/{uid}/extract        — extract_contours
    PUT  /api/contours/{uid}/validated      — upload_contours_validated
    POST /api/contours/{uid}/auto-accept    — auto_accept_contours
    POST /api/contours/{uid}/complete       — complete_contour_validation
    PUT  /api/contours/{uid}/training       — upload_contours_training
    PUT  /api/ocr/{uid}/result              — update_ocr_result
    POST /api/ocr/{uid}/validation/save     — save_ocr_validation
    POST /api/ocr/{uid}/recognize           — recognize_ocr_boxes
    POST /api/ocr/{uid}/binding/apply       — apply_ocr_binding
    POST /api/ocr/{uid}/start               — start_ocr

`AsyncSession` подделана, хранилище уведено в `tmp_path`, раскладка и брокер
замоканы. Гейты НАЛИЧИЯ (нет артефакта → 404) пройдены заранее: судится ровно
гейт СТАТУСА.
"""
import asyncio
import json
import uuid

import pytest
from fastapi import HTTPException

import app.api.contours as contours_api
import app.services.storage as storage_mod
from app.api.contours import (
    auto_accept_contours,
    complete_contour_validation,
    extract_contours,
    upload_contours_training,
    upload_contours_validated,
)
from app.api.ocr import (
    apply_ocr_binding,
    recognize_ocr_boxes,
    save_ocr_validation,
    start_ocr,
    update_ocr_result,
)
from app.models import Artifact, ArtifactType, Diagram, DiagramStatus

UID = uuid.UUID("c5000000-1111-2222-3333-444455556666")

STATUS_COUNT = 31

#: Этап, которым воркер подписывает упавшую сборку графа
#: (`worker/tasks/graph.py`, заперто `tests/test_graph_build_status_gate.py`).
GRAPH_BUILD_STAGE = "building_graph"

ENDPOINTS = (
    "contours_extract",
    "contours_validated_put",
    "contours_auto_accept",
    "contours_complete",
    "contours_training_put",
    "ocr_result_put",
    "ocr_validation_save",
    "ocr_recognize",
    "ocr_binding_apply",
    "ocr_start",
)

ALL_STATUSES = {s.value for s in DiagramStatus}

# ── гейт статуса: какие статусы эндпоинт пускает ─────────────────────────
#
# Литералы, снятые ЧТЕНИЕМ кода. Всё, чего в множестве нет, — 400.
ACCEPTS = {
    # У девяти из десяти статусного гейта НЕТ ВОВСЕ: пишут при любом статусе,
    # включая `building_graph` и `error` от упавшей сборки.
    "contours_extract": ALL_STATUSES,
    "contours_validated_put": ALL_STATUSES,
    "contours_auto_accept": ALL_STATUSES,
    "contours_complete": ALL_STATUSES,
    "contours_training_put": ALL_STATUSES,
    "ocr_result_put": ALL_STATUSES,
    "ocr_validation_save": ALL_STATUSES,
    "ocr_recognize": ALL_STATUSES,
    "ocr_binding_apply": ALL_STATUSES,
    # Единственный со своим списком — и `building_graph` в нём ЕСТЬ, хотя
    # первым же делом эндпоинт УДАЛЯЕТ `OCR_RESULT` (`app/api/ocr.py`).
    "ocr_start": {"validated_junctions", "building_graph", "built",
                  "validating_graph", "validated_graph", "extracting_contours",
                  "contours_extracted", "contours_validated", "ocr_completed",
                  "ocr_bound", "error"},
}

#: Пускает ли эндпоинт `ERROR` с `error_stage` упавшей СБОРКИ ГРАФА.
#: Решение №3 редтима: упавшая сборка — 400, упавший OCR фазу B не запирает.
ACCEPTS_FAILED_BUILD = {e: True for e in ENDPOINTS}


# ── харнесс ──────────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class FakeDB:
    """Поверхность `AsyncSession` этих десяти эндпоинтов."""

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
    """Поверхность `UploadFile` трёх загрузок."""

    def __init__(self, payload):
        self._payload = payload

    async def read(self):
        return self._payload


def _artifact(path):
    art = Artifact()
    art.file_path = path
    art.file_size = 1
    art.mime_type = "application/json"
    return art


def _diagram(status_value, error_stage=None):
    diagram = Diagram()
    diagram.uid = UID
    diagram.status = DiagramStatus(status_value)
    diagram.error_stage = error_stage
    diagram.error_message = "boom" if error_stage else None
    diagram.project_code = "thermohydraulics"
    return diagram


# Артефакты на месте: гейты наличия пройдены, судится ГЕЙТ СТАТУСА.
ARTIFACTS = {
    ArtifactType.GRAPH_VALIDATED: _artifact(
        f"{UID}/graph/graph_validated.json"),
    ArtifactType.OCR_RESULT: _artifact(f"{UID}/ocr/ocr_result.json"),
    ArtifactType.OCR_BINDING: _artifact(
        f"{UID}/ocr_binding/ocr_binding.json"),
    ArtifactType.CONTOURS_AUTO: _artifact(f"{UID}/contours/contours_auto.json"),
    ArtifactType.CONTOURS_VALIDATED: _artifact(
        f"{UID}/contours/contours_validated.json"),
}

CALL = {
    "contours_extract": lambda db: extract_contours(UID, ann_ids=None, db=db),
    "contours_validated_put": lambda db: upload_contours_validated(
        UID, file=FakeUpload(b'{"nodes": []}'), db=db),
    "contours_auto_accept": lambda db: auto_accept_contours(UID, db=db),
    "contours_complete": lambda db: complete_contour_validation(UID, db=db),
    "contours_training_put": lambda db: upload_contours_training(
        UID, file=FakeUpload(b'{"samples": []}'), db=db),
    "ocr_result_put": lambda db: update_ocr_result(
        UID, file=FakeUpload(b'{"target": []}'), db=db),
    "ocr_validation_save": lambda db: save_ocr_validation(
        UID, file=FakeUpload(b'{"classifications": []}'), db=db),
    "ocr_recognize": lambda db: recognize_ocr_boxes(
        UID, payload={"boxes": []}, db=db),
    "ocr_binding_apply": lambda db: apply_ocr_binding(UID, db=db),
    "ocr_start": lambda db: start_ocr(UID, db=db),
}


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    """Хранилище с уже лежащими файлами фазы B."""
    monkeypatch.setattr(storage_mod.settings, "STORAGE_PATH", str(tmp_path))
    for stage, name, payload in (
        ("graph", "graph_validated.json", {"nodes": [], "links": []}),
        ("ocr", "ocr_result.json", {"target": []}),
        ("ocr_binding", "ocr_binding.json", {"version": 2, "bindings": []}),
        ("contours", "contours_auto.json", {"nodes": []}),
        ("contours", "contours_validated.json", {"nodes": []}),
    ):
        d = tmp_path / str(UID) / stage
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(payload), encoding="utf-8")
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
def broker(monkeypatch):
    """Брокер — чужая подсистема; журнал имён отправленных задач."""
    sent = []

    class _AsyncResult:
        id = "task-stub"

    def _send_task(name, *args, **kwargs):
        sent.append(name)
        return _AsyncResult()

    from worker.celery_app import celery_app
    monkeypatch.setattr(celery_app, "send_task", _send_task)
    return sent


@pytest.fixture(autouse=True)
def ocr_enabled(monkeypatch):
    """OCR включён — как на бою (`thermohydraulics.yaml: ocr.enabled: true`)."""
    monkeypatch.setattr(contours_api, "_ocr_enabled", lambda code: True)


def _run(endpoint, diagram, artifacts=None):
    db = FakeDB(diagram, ARTIFACTS if artifacts is None else artifacts)
    return asyncio.run(CALL[endpoint](db)), db


# ── сторожа самих таблиц ─────────────────────────────────────────────────

def test_status_machine_size_is_locked():
    assert len(list(DiagramStatus)) == STATUS_COUNT


def test_table_keys_name_real_statuses():
    for endpoint in ENDPOINTS:
        for value in ACCEPTS[endpoint]:
            assert DiagramStatus(value).value == value, (endpoint, value)


def test_every_endpoint_has_a_rule():
    for table in (ACCEPTS, ACCEPTS_FAILED_BUILD, CALL):
        assert set(table) == set(ENDPOINTS)


# ── решётка: 31 статус × 10 эндпоинтов ───────────────────────────────────

@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_gate_over_every_status(endpoint, status, layout, broker):
    """Каждая клетка: пропущена ровно та, что в таблице.

    Порог заперт с двух сторон: отказ дополнительно утверждает, что статус
    не сдвинут, транзакция не коммитилась и в брокер ничего не ушло.
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
        assert broker == [], cell
        return

    _result, _db = _run(endpoint, diagram)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_gate_over_a_failed_graph_build(endpoint, layout, broker):
    """`ERROR` от УПАВШЕЙ СБОРКИ: решение №3 редтима.

    Отдельной клеткой, потому что `error` в решётке выше судится с пустым
    `error_stage` — а вся разница именно в нём.
    """
    diagram = _diagram("error", error_stage=GRAPH_BUILD_STAGE)

    if not ACCEPTS_FAILED_BUILD[endpoint]:
        with pytest.raises(HTTPException) as exc:
            _run(endpoint, diagram)
        assert exc.value.status_code == 400, endpoint
        assert diagram.status is DiagramStatus.ERROR, endpoint
        assert layout == [], endpoint
        assert broker == [], endpoint
        return

    _result, _db = _run(endpoint, diagram)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_failed_ocr_does_not_lock_phase_b(endpoint, layout, broker):
    """Второй берег решения №3: упавший OCR фазу B НЕ запирает.

    Без этой клетки гейт мог бы закрыть `ERROR` целиком, и тест выше остался
    бы зелёным — а оператор после падения распознавания терял бы и контуры.
    """
    diagram = _diagram("error", error_stage="ocr")
    _result, _db = _run(endpoint, diagram)


# ── что именно пишет пропущенный вызов ───────────────────────────────────

def test_contours_complete_still_moves_forward(layout):
    """Порог заперт с другой стороны: разрешённый статус работу ДЕЛАЕТ.

    Иначе гейт мог бы отменить сам переход, и решётка выше не заметила бы.
    """
    diagram = _diagram("contours_extracted")
    _run("contours_complete", diagram)
    assert diagram.status is DiagramStatus.CONTOURS_VALIDATED
    assert layout == [(str(UID), False)]


def test_binding_apply_writes_kks_when_allowed(storage, layout):
    """`/binding/apply` на разрешённом статусе и правда правит граф.

    ⚠ Эта клетка поймала ЧУЖОЙ дефект, без починки которого судить эндпоинт
    нечем: перезапись графа шла через `Path.rename`, а у него на Windows нет
    перезаписи — `os.rename` на существующий файл поднимает `FileExistsError`.
    Граф на этом пути существует ВСЕГДА (без него выше 404), то есть на Windows
    эндпоинт не работал ни разу. На POSIX (бой — Linux) `rename` перезаписывает,
    поэтому там клетка зелена по обе стороны правки: среда, в которой она
    доказуема, — Windows.
    """
    graph_path = storage / str(UID) / "graph" / "graph_validated.json"
    graph_path.write_text(
        json.dumps({"nodes": [{"id": "node_1"}], "links": []}),
        encoding="utf-8")
    binding_path = storage / str(UID) / "ocr_binding" / "ocr_binding.json"
    binding_path.write_text(
        json.dumps({"version": 2, "bindings": [
            {"node_id": "node_1", "text": "10LBA10AA001"}]}),
        encoding="utf-8")

    result, _db = _run("ocr_binding_apply", _diagram("ocr_completed"))

    assert result["updated_nodes"] == 1
    written = json.loads(graph_path.read_text(encoding="utf-8"))
    assert written["nodes"][0]["kks_full"] == "10LBA10AA001"
    assert not graph_path.with_suffix(".tmp").exists(), "временный файл остался"

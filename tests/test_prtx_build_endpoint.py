# -*- coding: utf-8 -*-
"""Серверная сборка .prtx — `POST /{uid}/prtx/build` (app/api/graph.py).

Зачем. Конвертация переехала с клиента на сервер: коробка САПФИР на 660 МБ
уходит с машины оператора, а вместо неё по сети едет ключ лицензии. Ключ —
единственное, что у сервера нельзя завести самому, и единственное, что нельзя
оставить у себя. Поэтому здесь заперты обе стороны: что запрос вообще не
уходит в сервис, когда конвертировать нечем, и что ключ не оседает в логах.

Судится настоящая корутина эндпоинта: `AsyncSession` подделана (её поверхность
здесь — `execute`/`add`/`delete`/`flush`/`commit`), `httpx.AsyncClient` и
`StorageService` подменены. Живой БД, сервиса и движка не нужно — прогон самого
движка проверяется не здесь, а сверкой дампа с windows-эталоном.
"""
import asyncio
import base64
import json
import logging
import uuid

import pytest
from fastapi import HTTPException

import app.api.graph as graph_api
from app.api.graph import build_prtx, prtx_status
from app.models import Artifact, ArtifactType, Diagram

UID = uuid.UUID("aa11bb22-cc33-dd44-ee55-ff6677889900")
KEY = b"\x00license-key\xff"
GRAPH = b'{"nodes": []}'
IMAGE = b"\x89PNG scan"
PRTX = b"PRTX-BODY"


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class FakeDB:
    def __init__(self, diagram, artifacts):
        self.diagram = diagram
        #: {ArtifactType: Artifact}
        self.artifacts = artifacts
        self.added = []
        self.deleted = []
        self.commits = 0

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Diagram:
            return _FakeResult(self.diagram)
        if entity is Artifact:
            # тип артефакта — во втором сравнении WHERE (diagram_uid, artifact_type)
            wanted = list(stmt.whereclause.clauses)[1].right.value
            return _FakeResult(self.artifacts.get(wanted))
        raise AssertionError(f"неожиданная сущность в запросе: {entity!r}")

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


class FakeTasks:
    """BackgroundTasks в объёме, который нужен эндпоинту."""

    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))

    async def run_all(self):
        for func, args, kwargs in self.tasks:
            await func(*args, **kwargs)


class FakeUpload:
    """UploadFile в объёме, который читает эндпоинт."""

    def __init__(self, data):
        self._data = data

    async def read(self):
        return self._data


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _artifact(art_type, rel_path):
    art = Artifact()
    art.diagram_uid = UID
    art.artifact_type = art_type
    art.file_path = rel_path
    return art


def _diagram():
    diagram = Diagram()
    diagram.uid = UID
    return diagram


@pytest.fixture(autouse=True)
def _clean_jobs():
    graph_api._PRTX_JOBS.clear()
    yield
    graph_api._PRTX_JOBS.clear()


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Артефакты на диске + перехват записи результата."""
    from app.config import settings

    monkeypatch.setattr(settings, "STORAGE_PATH", str(tmp_path))
    (tmp_path / "graph_validated.json").write_bytes(GRAPH)
    (tmp_path / "image.png").write_bytes(IMAGE)

    saved = {}

    class FakeStorage:
        async def save_file(self, uid, stage, name, content):
            saved["content"] = content
            saved["path"] = f"{uid}/{stage}/{name}"
            return saved["path"], len(content)

    monkeypatch.setattr("app.api.graph.StorageService", FakeStorage)
    return saved


#: сессию фоновая задача открывает сама — подсовываем ей ту же FakeDB
_CURRENT_DB = {}


@pytest.fixture(autouse=True)
def _session_factory(monkeypatch):
    class _Ctx:
        async def __aenter__(self):
            return _CURRENT_DB["db"]

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.db.session.AsyncSessionLocal", lambda: _Ctx())


@pytest.fixture
def service(monkeypatch):
    """Подменённый prtx-сервис: пишет, что получил, отвечает, что велено."""
    import httpx

    state = {"calls": [], "response": FakeResponse(
        200, {"prtx": base64.b64encode(PRTX).decode(), "size": len(PRTX)})}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            # копия: отправленный payload затирается сразу после запроса,
            # чтобы ключ не жил в памяти дольше нужного
            state["calls"].append({"url": url, "payload": dict(json or {}),
                                   "sent": json})
            if isinstance(state["response"], Exception):
                raise state["response"]
            return state["response"]

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return state


def _call(db, key=KEY, text_mode="all", run_job=True):
    """Дёрнуть эндпоинт и, если он поставил задачу, доиграть её как сервер."""
    tasks = FakeTasks()
    _CURRENT_DB["db"] = db

    async def _go():
        answer = await build_prtx(
            uid=UID, background_tasks=tasks, license_key=FakeUpload(key),
            text_mode=text_mode, db=db)
        if run_job:
            await tasks.run_all()
        return answer

    result = asyncio.run(_go())
    return result, tasks


def _db_full():
    return FakeDB(_diagram(), {
        ArtifactType.GRAPH_VALIDATED: _artifact(
            ArtifactType.GRAPH_VALIDATED, "graph_validated.json"),
        ArtifactType.ORIGINAL_IMAGE: _artifact(
            ArtifactType.ORIGINAL_IMAGE, "image.png"),
    })


def test_unknown_diagram_is_404(storage, service):
    db = FakeDB(None, {})
    with pytest.raises(HTTPException) as exc:
        _call(db)
    assert exc.value.status_code == 404
    assert service["calls"] == []


def test_without_validated_graph_service_is_not_called(storage, service):
    """Конвертировать нечего — не занимаем очередь сервиса."""
    db = FakeDB(_diagram(), {})
    with pytest.raises(HTTPException) as exc:
        _call(db)
    assert exc.value.status_code == 409
    assert service["calls"] == []
    assert db.commits == 0


def test_empty_key_is_refused_before_service(storage, service):
    db = _db_full()
    with pytest.raises(HTTPException) as exc:
        _call(db, key=b"")
    assert exc.value.status_code == 400
    assert service["calls"] == []


def test_success_sends_graph_key_and_scan(storage, service):
    db = _db_full()
    result, _ = _call(db)

    # эндпоинт отвечает сразу, не дожидаясь движка
    assert result["status"] == "building"
    assert len(service["calls"]) == 1
    payload = service["calls"][0]["payload"]
    assert base64.b64decode(payload["graph"]) == GRAPH
    assert base64.b64decode(payload["license"]) == KEY
    assert base64.b64decode(payload["image"]) == IMAGE
    assert payload["text_mode"] == "all"

    assert storage["content"] == PRTX
    assert graph_api._PRTX_JOBS[str(UID)] == {
        "state": "done", "file_path": storage["path"], "file_size": len(PRTX)}
    assert db.commits == 1
    assert [a.artifact_type for a in db.added] == [ArtifactType.PRTX]


def test_scan_is_optional(storage, service):
    """Без скана прогон идёт — угол насосов берётся по трубам."""
    db = FakeDB(_diagram(), {
        ArtifactType.GRAPH_VALIDATED: _artifact(
            ArtifactType.GRAPH_VALIDATED, "graph_validated.json"),
    })
    _call(db)
    assert "image" not in service["calls"][0]["payload"]


def test_reissue_drops_old_artifact(storage, service):
    """Пересборка поверх старой схемы: запись артефакта одна, не две."""
    old = _artifact(ArtifactType.PRTX, "fxml/diagram.prtx")
    db = _db_full()
    db.artifacts[ArtifactType.PRTX] = old

    _call(db)

    assert db.deleted == [old]
    assert len(db.added) == 1


def test_engine_refusing_key_lands_in_status(storage, service):
    """Движок не принял ключ — причина доходит до клиента через /prtx/status."""
    service["response"] = FakeResponse(
        403, {"error": "движок не принял ключ лицензии"})
    db = _db_full()

    _call(db)

    job = graph_api._PRTX_JOBS[str(UID)]
    assert job["state"] == "error"
    assert "ключ лицензии" in job["error"]
    assert db.commits == 0


def test_dead_service_lands_in_status(storage, service):
    import httpx

    service["response"] = httpx.ConnectError("connection refused")
    db = _db_full()

    _call(db)

    job = graph_api._PRTX_JOBS[str(UID)]
    assert job["state"] == "error"
    assert "недоступен" in job["error"]
    assert db.commits == 0


def test_key_never_reaches_the_log(storage, service, caplog):
    """Ключ едет через сервер — в журнале его быть не должно ни в каком виде."""
    db = _db_full()
    with caplog.at_level(logging.DEBUG):
        _call(db)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert KEY.decode("latin-1") not in text
    assert base64.b64encode(KEY).decode() not in text


def test_key_is_wiped_after_send(storage, service):
    """Ключ не должен оставаться в памяти процесса после отправки в сервис."""
    _call(_db_full())
    assert service["calls"][0]["sent"] == {}, "payload с ключом не затёрт"


def test_repeat_while_building_does_not_start_second_run(storage, service):
    """Клиент повторяет запрос при обрыве — вторая сборка стартовать не должна."""
    db = _db_full()
    graph_api._PRTX_JOBS[str(UID)] = {"state": "building"}

    result, tasks = _call(db, run_job=False)

    assert result["status"] == "building"
    assert tasks.tasks == [], "поставлено второе задание поверх бегущего"
    assert service["calls"] == []


def test_status_reports_idle_for_unknown_diagram():
    """Сервер перезапустили — задание потеряно, и это видно клиенту."""
    assert asyncio.run(prtx_status(uid=UID)) == {"state": "idle"}

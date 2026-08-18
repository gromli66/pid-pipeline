# -*- coding: utf-8 -*-
"""Стенд локального прогона конвейера (`tools/e2e_local.py`, пункт 0.12).

Сам прогон требует поднятого docker-стека и десятков минут, поэтому в CI его
нет. Здесь на поддельном транспорте (`httpx.MockTransport`) закрывается то,
что молча соврёт и без стека:

1. **Цепочка шагов.** Дырка или петля в `STEPS` даёт стенд, который «прошёл
   все этапы», пропустив половину. Проверяется, что от `uploaded` цепочка
   доходит до `completed`, каждый шаг встречается ровно раз и все имена
   артефактов существуют в `ArtifactType`.
2. **Остановки.** Статус `error` обязан ронять прогон с текстом этапа из БД,
   а не крутиться до таймаута; таймаут обязан ронять с указанием статуса.
3. **Вердикт `--check`.** Мёртвый API, отсутствующая очередь и чужой проект
   обязаны давать exit 1 — гейт, который зелен всегда, гейтом не является
   (тот же класс дефекта, что блокер 0.3).
"""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import e2e_local  # noqa: E402

UID = "11111111-2222-3333-4444-555555555555"


# --------------------------------------------------------------------------
# Поддельный стек: та же машина состояний, что у API
# --------------------------------------------------------------------------

class FakeStack:
    """Отвечает на те же запросы, что боевой API, по таблице переходов."""

    # POST-путь → статус, который он выставляет (None — статус не трогает)
    POSTS = {
        "/api/frame/{uid}/skip": "frame_cleaned",
        "/api/detection/{uid}/detect": "detecting",
        "/api/cvat/{uid}/create-task": None,
        "/api/cvat/{uid}/open-validation": "validating_bbox",
        "/api/cvat/{uid}/fetch-annotations": "validated_bbox",
        "/api/segmentation/{uid}/segment": "skeletonized",
        "/api/validation/{uid}/masks/start": "validating_masks",
        "/api/validation/{uid}/masks/complete": "detected_junctions",
        "/api/validation/{uid}/junctions/start": "validating_junctions",
        "/api/validation/{uid}/junctions/complete": "built",
        "/api/validation/{uid}/graph/start": "validating_graph",
        "/api/validation/{uid}/graph/save": None,
        "/api/validation/{uid}/graph/complete-simple": "validated_graph",
        "/api/contours/{uid}/extract": None,
        "/api/contours/{uid}/complete": "contours_validated",
        "/api/ocr/{uid}/binding/save": None,
        "/api/ocr/{uid}/binding/apply": "ocr_bound",
        "/api/validation/{uid}/graph/complete": "completed",
    }

    def __init__(self, *, artifacts=None, fail_at=None, healthy=True,
                 queues=("default", "gpu", "sam2", "ocr")):
        self.status = "uploaded"
        self.calls: list[str] = []
        self.artifacts = set(e2e_local.REQUIRED_ARTIFACTS if artifacts is None
                             else artifacts)
        self.fail_at = fail_at          # путь, после которого уходим в error
        self.healthy = healthy
        self.queues = set(queues)

    # --- транспорт --------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            return self._get(path)
        self.calls.append(path)
        key = path.replace(UID, "{uid}")
        if key not in self.POSTS:
            return httpx.Response(404, json={"detail": f"нет ручки {path}"})
        if self.fail_at and path.endswith(self.fail_at):
            self.status = "error"
            return httpx.Response(200, json={"status": "error"})
        new = self.POSTS[key]
        if new:
            # «detecting» — статус воркера: следующий опрос уже видит результат
            self.status = "detected" if new == "detecting" else new
        return httpx.Response(200, json={"status": self.status})

    def _get(self, path: str) -> httpx.Response:
        if path == "/health":
            state = "healthy" if self.healthy else "unhealthy"
            return httpx.Response(200, json={"status": state, "checks": {
                "api": "healthy", "database": state, "redis": "healthy"}})
        if path.startswith("/api/projects/"):
            code = path.rsplit("/", 1)[-1]
            if code != "thermohydraulics":
                return httpx.Response(404, json={"detail": "нет такого"})
            return httpx.Response(200, json={"code": code})
        if path.endswith("/status") and "/diagrams/" in path:
            body = {"status": self.status}
            if self.status == "error":
                body |= {"error_stage": "segmenting", "error_message": "упало"}
            return httpx.Response(200, json=body)
        if path.startswith("/api/contours/"):
            return httpx.Response(200, json={"has_auto": True})
        if path.startswith("/api/ocr/"):
            return httpx.Response(200, json={"has_ocr_result": True})
        if "/download/" in path:
            kind = path.rsplit("/", 1)[-1]
            if kind not in self.artifacts:
                return httpx.Response(404, json={"detail": "нет артефакта"})
            return httpx.Response(200, content=b'{"nodes": []}')
        return httpx.Response(404, json={"detail": f"нет ручки {path}"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler),
                            base_url="http://stub")


def _run(stack: FakeStack) -> e2e_local.RunReport:
    with stack.client() as client:
        runner = e2e_local.Runner(client, poll=0.0, log=lambda _m: None)
        return runner.drive(UID, e2e_local.RunReport())


@pytest.fixture
def fast_clock(monkeypatch):
    """Часы, прыгающие на 600 с за опрос: таймауты шагов срабатывают сразу.

    Без них тест на остановке по `error` в случае дефекта не падает, а
    крутится до боевого таймаута (5400 с) — красный тест обязан быть быстрым.
    """
    ticks = iter(range(0, 10_000_000, 600))
    monkeypatch.setattr(e2e_local.time, "monotonic", lambda: float(next(ticks)))


# --------------------------------------------------------------------------
# 1. Цепочка
# --------------------------------------------------------------------------

def test_chain_from_uploaded_reaches_completed():
    """Дырка в таблице = «прошёл все этапы», пропустив половину."""
    chain = e2e_local.chain_from("uploaded")
    assert [s.id for s in chain] == [s.id for s in e2e_local.STEPS]
    last = chain[-1]
    assert e2e_local.FINAL_STATUS in last.until


def test_every_step_reachable_and_unique():
    froms = [s.frm for s in e2e_local.STEPS]
    assert len(froms) == len(set(froms)), "два шага на одном статусе"
    reachable = {"uploaded"} | {u for s in e2e_local.STEPS for u in s.until}
    for step in e2e_local.STEPS:
        assert step.frm in reachable, f"шаг {step.id} недостижим"


def test_wait_steps_cover_transient_statuses():
    """`--resume` после таймаута попадает именно на промежуточный статус."""
    for status in ("detecting", "segmenting", "building_graph", "generating_fxml"):
        step = e2e_local.next_step(status)
        assert step is not None and step.kind == "wait", status


def test_required_artifacts_exist_in_enum():
    from app.models.artifact import ArtifactType

    known = {a.value for a in ArtifactType}
    assert set(e2e_local.REQUIRED_ARTIFACTS) <= known


# --------------------------------------------------------------------------
# 2. Прогон и остановки
# --------------------------------------------------------------------------

def test_full_run_walks_every_step():
    stack = FakeStack()
    report = _run(stack)
    assert report.final_status == "completed"
    assert [r["id"] for r in report.steps] == [s.id for s in e2e_local.STEPS]
    assert all(report.artifacts.values())
    # приёмка графа идёт явным сохранением: молчаливой копии в API больше нет
    assert f"/api/validation/{UID}/graph/save" in stack.calls


def test_missing_artifact_is_visible_in_report():
    stack = FakeStack(artifacts=set(e2e_local.REQUIRED_ARTIFACTS) - {"fxml"})
    report = _run(stack)
    assert report.artifacts["fxml"] is False
    assert not all(report.artifacts.values())


def test_error_status_stops_run_with_stage(fast_clock):
    stack = FakeStack(fail_at="/segment")
    with pytest.raises(e2e_local.E2EError) as exc:
        _run(stack)
    assert "segmenting" in str(exc.value) and "упало" in str(exc.value)


def test_timeout_names_the_status():
    stack = FakeStack()
    with stack.client() as client:
        runner = e2e_local.Runner(client, poll=0.0, log=lambda _m: None)
        with pytest.raises(e2e_local.E2EError) as exc:
            runner.wait_status(UID, {"completed"}, timeout=0, label="шаг")
    assert "таймаут" in str(exc.value) and "uploaded" in str(exc.value)


def test_unknown_status_is_a_dead_end_not_a_silent_pass():
    stack = FakeStack()
    stack.status = "cleaning_frame"       # оператор бросил очистку рамки
    with pytest.raises(e2e_local.E2EError) as exc:
        _run(stack)
    assert "cleaning_frame" in str(exc.value)


# --------------------------------------------------------------------------
# 3. Вердикт --check
# --------------------------------------------------------------------------

def _check(stack: FakeStack, **kw) -> tuple[int, list[str]]:
    with stack.client() as client:
        return e2e_local.check_stack(
            client, project=kw.get("project", "thermohydraulics"),
            broker="redis://stub", queues_probe=lambda _b: stack.queues,
            image=kw.get("image"))


def test_check_green_on_live_stack():
    code, lines = _check(FakeStack())
    assert code == 0, lines


@pytest.mark.parametrize("stack, kw", [
    (FakeStack(healthy=False), {}),
    (FakeStack(queues=("default", "gpu", "sam2")), {}),          # нет ocr
    (FakeStack(), {"project": "net_takogo"}),
])
def test_check_red_on_broken_stack(stack, kw):
    code, lines = _check(stack, **kw)
    assert code == 1, lines


def test_check_red_when_image_missing(tmp_path):
    code, _ = _check(FakeStack(), image=tmp_path / "нет.png")
    assert code == 1


def test_check_red_when_api_dead():
    def dead(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("стек не поднят")

    with httpx.Client(transport=httpx.MockTransport(dead),
                      base_url="http://stub") as client:
        code, lines = e2e_local.check_stack(
            client, project="thermohydraulics", broker="redis://stub",
            queues_probe=lambda _b: set())
    assert code == 1
    assert any("ConnectError" in line for line in lines)


def test_report_json_is_machine_readable():
    report = _run(FakeStack())
    payload = json.loads(json.dumps(report.as_dict(), ensure_ascii=False))
    assert payload["final_status"] == "completed"
    assert payload["total_seconds"] >= 0
    assert len(payload["steps"]) == len(e2e_local.STEPS)

# -*- coding: utf-8 -*-
"""Пункт 1-48 дороги: бусина OCR обязана показать отказ, а не молчать до страховки.

Дефект (red-team №9 ревизии связки 1.15+1.16, `MEASUREMENTS §104з`). Веер
`junctions/complete` умеет отправить сборку графа и НЕ отправить параллельный OCR:
эта ветка не откатывает состояние сознательно (граф уже в брокере), а про отказ
говорит `message`-прозой и `ocr_task_id: null`. Клиент из ответа читает ТОЛЬКО
`task_id` (`_on_junction_confirmed`), поэтому оператор об отказе не узнаёт.

⛔ Молчание — не худшая половина. Через один опрос статус уходит в `building_graph`,
а `_BEAD_DEFS` держит бусину OCR «в процессе» на ВОСЬМИ статусах подряд, включая
этот. То есть оператор видит не пустоту, а крутящуюся оранжевую бусину «идёт
распознавание» — там, где не отправлено ничего. Молчит оно до самой страховки
(`graph/complete-simple`, `API.md §7` — idempotent safety net).

Что судится здесь. Сервер — НАСТОЯЩАЯ корутина `complete_junction_validation`
(поддельны только `AsyncSession` и `send_task`), клиент — НАСТОЯЩИЙ
`DiagramWorkspace`. Между ними ходит настоящее тело ответа. Утверждения — о
РАЗНИЦЕ (`PROTOCOL §3`): один и тот же статус `building_graph` при ушедшем и
не ушедшем OCR обязан давать РАЗНЫЕ бусины, иначе тест зелен и без лечения.

⛔ Сценарий с предысторией (`PROTOCOL §3`) здесь не украшение, а главный:
`ProcessingStage` откатом НЕ удаляются (`app/api/diagrams.py` строк стадий не
трогает, `worker/utils/db_helpers.start_stage` заводит новую попытку рядом),
поэтому у оператора, вернувшегося с `ocr_completed` и переподтвердившего
перекрёстки, в `/stages` УЖЕ ЛЕЖИТ строка `ocr` прошлого прогона. Свидетельство
«стадия есть» сняло бы свежий отказ немедленно — и бусина замолчала бы снова,
ровно там, где дефект и живёт.

⚠ Модалки пути подменены (`PROTOCOL §5`): без подмены красный прогон не падает,
а виснет на `QMessageBox`, и на CI это выглядит как «долго».
⚠ Свои виджеты набор сносит сам, точечно (`sendPostedEvents(DeferredDelete)`),
а не общим `processEvents()` — замер §85/1-42: общая очередь оплачивает чужой мусор.
"""
import asyncio
import os
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                              # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QObject, Signal          # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox    # noqa: E402

from app.api.validation import complete_junction_validation  # noqa: E402
from app.models import Artifact, ArtifactType, Diagram      # noqa: E402
from app.models import DiagramStatus as SrvStatus           # noqa: E402

import ui.widgets.diagram_workspace as dw                   # noqa: E402
from ui.services.api_client import (                        # noqa: E402
    APIClient, DiagramStatus, DiagramStatusInfo,
)
from ui.widgets.progress_beads import BeadState             # noqa: E402

UID = "c1a55e77-1111-2222-3333-444455556666"
OCR_TASK = "worker.tasks.ocr.task_run_ocr"

# Подпись этапа глазами оператора — из `_BUTTON_DEFS` самого воркспейса.
OCR_LABEL = "Распознавание текста"


# ── поддельная БД ────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


def _artifact(path):
    art = Artifact()
    art.file_path = path
    art.file_size = 1
    art.mime_type = "image/png"
    return art


ARTIFACTS = {
    ArtifactType.JUNCTION_MASK_VALIDATED: _artifact("junction_mask_validated.png"),
    ArtifactType.BRIDGE_MASK_VALIDATED: _artifact("bridge_mask_validated.png"),
}


class _FakeDB:
    def __init__(self, diagram):
        self.diagram = diagram
        self.commits = 0

    async def execute(self, stmt):
        if stmt.column_descriptions[0]["entity"] is Diagram:
            return _FakeResult(self.diagram)
        params = stmt.compile().params
        return _FakeResult(ARTIFACTS.get(params.get("artifact_type_1")))

    async def commit(self):
        self.commits += 1

    def add(self, obj):
        pass

    async def flush(self):
        pass


class FakeServer:
    """Одна диаграмма. Ответ веера собирает НАСТОЯЩАЯ корутина."""

    def __init__(self, status=SrvStatus.VALIDATING_JUNCTIONS):
        self.diagram = Diagram()
        self.diagram.uid = uuid.UUID(UID)
        self.diagram.number = 7
        self.diagram.project_code = "thermohydraulics"
        self.diagram.original_filename = "shema.png"
        self.diagram.status = status
        self.diagram.error_stage = None
        self.diagram.error_message = None
        self.diagram.cvat_task_id = None
        self.diagram.cvat_job_id = None

    def payload(self):
        return {
            "uid": UID,
            "number": self.diagram.number,
            "project_code": self.diagram.project_code,
            "original_filename": self.diagram.original_filename,
            "status": self.diagram.status.value,
            "error_message": self.diagram.error_message,
            "error_stage": self.diagram.error_stage,
            "cvat_task_id": self.diagram.cvat_task_id,
            "cvat_job_id": self.diagram.cvat_job_id,
        }

    def complete_junctions(self):
        return asyncio.run(
            complete_junction_validation(uuid.UUID(UID), db=_FakeDB(self.diagram)))


def real_diagram_info(payload):
    """`DiagramInfo` собран НАСТОЯЩИМ клиентом из тела ответа сервера."""
    client = APIClient.__new__(APIClient)
    client._request = lambda *a, **kw: payload
    return APIClient.get_diagram(client, UID)


class FakeAPI:
    """Поверхность `APIClient`, которой воркспейс пользуется в сценарии."""

    def __init__(self, server):
        self.server = server
        self.stages = []
        self.has_ocr_result = False

    def get_diagram(self, uid):
        return real_diagram_info(self.server.payload())

    def get_stages(self, uid):
        return list(self.stages)

    def get_ocr_status(self, uid):
        return {"has_ocr_result": self.has_ocr_result}

    def get_stage_durations(self):
        return {}

    def download_artifact(self, uid, kind, path):
        raise OSError("холста нет")          # `_has_saved_canvas` → False, без модалки

    def complete_junction_validation(self, uid):
        return self.server.complete_junctions()


class FakeStatusProvider(QObject):
    status_updated = Signal(str, object)
    stages_updated = Signal(str, object)

    def watch(self, uid):
        pass

    def unwatch(self, uid):
        pass

    def is_watching(self, uid):
        return False


class FakeMsgBox:
    StandardButton = QMessageBox.StandardButton
    calls = []

    @classmethod
    def question(cls, *args, **kwargs):
        cls.calls.append(("question", args[2] if len(args) > 2 else ""))
        return cls.StandardButton.Yes

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append(("warning", args[2] if len(args) > 2 else ""))
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, *args, **kwargs):
        cls.calls.append(("information", args[2] if len(args) > 2 else ""))
        return cls.StandardButton.Ok


def stage_row(stage_type, status="running", row_id=1, attempt=1):
    """Строка `ProcessingStage`, как её отдаёт `/stages`."""
    started = "2026-08-20T10:00:00"
    return {
        "id": row_id,
        "stage_type": stage_type,
        "status": status,
        "attempt": attempt,
        "error_message": None,
        "started_at": started,
        "created_at": started,
    }


def _set_ocr_enabled(monkeypatch, on: bool):
    """Без подмены `_pc_ocr` всегда `None` (conftest уводит конфиги) и ветка OCR
    оказалась бы непройденной — решался бы не тот эндпоинт."""
    import app.services.project_loader as project_loader

    class _Ocr:
        enabled = on

    class _Config:
        ocr = _Ocr()

    class _Loader:
        def load(self, code):
            return _Config()

    monkeypatch.setattr(project_loader, "get_project_loader", lambda: _Loader())


def _set_broker(monkeypatch, dead_tasks=()):
    from worker.celery_app import celery_app

    class _AsyncResult:
        id = "task-0001"

    def _send_task(name, args=None, kwargs=None, **rest):
        if name in dead_tasks:
            raise OSError("[Errno 111] Connection refused")
        return _AsyncResult()

    monkeypatch.setattr(celery_app, "send_task", _send_task)


# ── харнесс ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class Bench:
    """Воркспейс + сервер + рычаги сценария."""

    def __init__(self, ws, api, server):
        self.ws = ws
        self.api = api
        self.server = server
        self.messages = []
        ws.status_message.connect(lambda text, ms: self.messages.append(text))

    def confirm_junctions(self):
        """Оператор нажал «подтвердить» во вкладке перекрёстков."""
        self.ws._on_junction_confirmed()

    def poll_status(self, status: SrvStatus):
        """Опрос принёс новый статус — тем же сигналом, что и в бою."""
        self.server.diagram.status = status
        self.ws.status_provider.status_updated.emit(
            UID, DiagramStatusInfo(status=DiagramStatus(status.value)))

    def poll_stages(self, rows):
        """Опрос принёс строки `/stages`."""
        self.api.stages = list(rows)
        self.ws.status_provider.stages_updated.emit(UID, list(rows))

    def ocr_bead(self):
        return self.ws.beads.get_state(dw.BEAD_OCR)


@pytest.fixture
def bench(qapp, monkeypatch):
    FakeMsgBox.calls = []
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)
    made = []

    def _make(ocr_enabled=True, ocr_broker_dead=False, stages=(),
              status=SrvStatus.VALIDATING_JUNCTIONS):
        _set_ocr_enabled(monkeypatch, ocr_enabled)
        _set_broker(monkeypatch, dead_tasks=(OCR_TASK,) if ocr_broker_dead else ())
        server = FakeServer(status)
        api = FakeAPI(server)
        api.stages = list(stages)
        ws = dw.DiagramWorkspace(api, FakeStatusProvider())
        ws.show()
        ws.load_diagram(UID, "схема оператора")
        made.append(ws)
        return Bench(ws, api, server)

    yield _make

    for ws in made:
        ws.cleanup()
        ws.hide()
        ws.setParent(None)
        ws.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# ── Д1: отказ виден сразу ────────────────────────────────────────────────

def test_bead_shows_refusal_right_after_the_fan_out(bench):
    """OCR не ушёл — бусина красная уже в `validated_junctions`."""
    b = bench(ocr_broker_dead=True)
    b.confirm_junctions()

    assert b.server.diagram.status is SrvStatus.VALIDATED_JUNCTIONS
    assert b.ocr_bead() is BeadState.ERROR


def test_operator_is_told_in_words_which_stage_did_not_start(bench):
    """Не только цвет: этап назван подписью, которую оператор видит на кнопке."""
    b = bench(ocr_broker_dead=True)
    b.confirm_junctions()

    assert any(OCR_LABEL in m for m in b.messages), b.messages


# ── Д1: главная клетка — та, где сегодня КРУТИТСЯ ложь ───────────────────

def test_refusal_survives_the_move_to_building_graph(bench):
    """Статус ушёл в `building_graph` — бусина обязана остаться красной.

    Здесь дефект и виден: `_BEAD_DEFS` красит OCR «в процессе» на этом статусе,
    и до правки оператор получает крутящуюся оранжевую бусину вместо отказа.
    """
    b = bench(ocr_broker_dead=True)
    b.confirm_junctions()
    b.poll_status(SrvStatus.BUILDING_GRAPH)

    assert b.ocr_bead() is BeadState.ERROR


def test_a_started_ocr_spins_at_the_very_same_status(bench):
    """РАЗНИЦА, а не совпадение: тот же статус, ушедший OCR — бусина «в процессе».

    Без этой половины утверждение выше было бы зелёным и у клиента, который
    красит бусину OCR красной всегда (`PROTOCOL §3`).
    """
    b = bench(ocr_broker_dead=False)
    b.confirm_junctions()
    b.poll_status(SrvStatus.BUILDING_GRAPH)

    assert b.ocr_bead() is BeadState.IN_PROGRESS


def test_ocr_disabled_by_config_is_not_a_refusal(bench):
    """Выключенный конфигом OCR — не отказ: красить нечего.

    Замок с той же стороны, что и выше: `ocr_task_id: null` у выключенного и
    у не ушедшего одинаков, и клиент, читающий только его, покрасил бы оба.
    """
    b = bench(ocr_enabled=False)
    b.confirm_junctions()
    b.poll_status(SrvStatus.BUILDING_GRAPH)

    assert b.ocr_bead() is BeadState.IN_PROGRESS


def test_already_past_repeat_is_not_a_refusal(bench):
    """Идемпотентный повтор (`already_past`): не отправлялось ничего — и не отказ."""
    b = bench(ocr_broker_dead=True, status=SrvStatus.BUILT)
    b.confirm_junctions()

    assert b.ocr_bead() is not BeadState.ERROR


# ── Д1: отказ переживает ЧУЖИЕ действия ──────────────────────────────────

def test_refusal_survives_a_foreign_stage_poll(bench):
    """Опрос принёс ЧУЖУЮ бегущую стадию — отказ OCR это не лечит.

    Сценарий идёт ПОСЛЕ чужого действия (`PROTOCOL §3`): пока оператор ждёт,
    конвейер живёт своей жизнью и шлёт `/stages` про сборку графа. Свидетельством
    о запуске OCR это не является.
    """
    b = bench(ocr_broker_dead=True)
    b.confirm_junctions()
    b.poll_stages([stage_row("graph_building", row_id=11)])
    b.poll_status(SrvStatus.BUILT)

    assert b.ocr_bead() is BeadState.ERROR


def test_refusal_survives_a_stale_ocr_row_from_a_previous_run(bench):
    """⛔ ПРЕДЫСТОРИЯ: строка `ocr` прошлого прогона отказ НЕ снимает.

    Оператор вернулся с `ocr_completed` назад и переподтвердил перекрёстки.
    Откат строки `ProcessingStage` не удаляет, поэтому в `/stages` лежит `ocr`
    прошлой попытки. Свидетельство «стадия есть» сняло бы свежий отказ и вернуло
    бы молчание — здесь оно обязано быть отвергнуто как СТАРШЕЕ отказа.
    """
    old = stage_row("ocr", status="completed", row_id=5, attempt=1)
    b = bench(ocr_broker_dead=True, stages=[old])
    b.confirm_junctions()
    b.poll_stages([old])
    b.poll_status(SrvStatus.BUILDING_GRAPH)

    assert b.ocr_bead() is BeadState.ERROR


# ── Д1: отказ снимается настоящим свидетельством ─────────────────────────

def test_safety_net_run_clears_the_refusal(bench):
    """Страховка отправила OCR — появилась НОВАЯ строка стадии, красное снято."""
    old = stage_row("ocr", status="completed", row_id=5, attempt=1)
    b = bench(ocr_broker_dead=True, stages=[old])
    b.confirm_junctions()
    b.poll_stages([old, stage_row("ocr", status="running", row_id=9, attempt=2)])
    b.poll_status(SrvStatus.BUILDING_GRAPH)

    assert b.ocr_bead() is BeadState.IN_PROGRESS
    assert b.ws._dispatch_refusals == {}


def test_ocr_artifact_clears_the_refusal(bench):
    """Артефакт OCR на месте — задача точно отработала, красное снято."""
    b = bench(ocr_broker_dead=True)
    b.confirm_junctions()
    b.api.has_ocr_result = True
    b.poll_status(SrvStatus.BUILDING_GRAPH)

    assert b.ocr_bead() is BeadState.COMPLETED


def test_finished_ocr_status_clears_the_refusal(bench):
    """Статус дошёл до `ocr_completed` — этап пройден, красить нечего."""
    b = bench(ocr_broker_dead=True)
    b.confirm_junctions()
    b.poll_status(SrvStatus.OCR_COMPLETED)

    assert b.ocr_bead() is BeadState.COMPLETED


def test_another_diagram_does_not_inherit_the_refusal(bench):
    """Отказ принадлежит диаграмме, а не окну: перезагрузка его снимает."""
    b = bench(ocr_broker_dead=True)
    b.confirm_junctions()
    assert b.ocr_bead() is BeadState.ERROR

    b.server.diagram.status = SrvStatus.BUILDING_GRAPH
    b.ws.load_diagram(UID, "другая схема")

    assert b.ws._dispatch_refusals == {}
    assert b.ocr_bead() is BeadState.IN_PROGRESS

# -*- coding: utf-8 -*-
"""Пункт 1.11 дороги (Т1 аудита) — из упавшей детекции есть выход кнопкой.

Дефект. Детекция упала → воркер ставит `status=ERROR`, `error_stage="detecting"`
(`worker/utils/db_helpers.py:36`). Клиент рисует красную кнопку «🔄 Поиск
элементов», клик уходит в `_start_detection` → `_run_detection` →
`api_client.start_detection` → `POST /api/detection/{uid}/detect`, а гейт
эндпоинта требовал ТОЧНОГО `frame_cleaned` и отвечал **400**. Выхода не было:
три retry-эндпоинта, которые умеют выйти (`detection.py:82`, `cvat.py:503`,
`diagrams.py:427`), из UI не зовутся ни разу — `retry_operation` объявлен в
`api_client.py` и не вызван ниоткуда. Оператор оставался в тупике до правки БД.

Почему тест сценарный, а не юнит. Дефект живёт на ШВЕ «кнопка клиента → гейт
сервера»: и кнопка права, и гейт сам по себе непротиворечив. Поэтому здесь
живой `DiagramWorkspace` с настоящими сигналами Qt, а поддельный сервер судит
**настоящей** `app.api.detection.start_detection` — не моим представлением о
том, что она пропускает (образец — `tests/ui/test_no_silent_rollback.py`,
пункт 1.3, тот же класс дефекта). Таблицу переходов самого гейта держит
`tests/test_detection_status_gate.py`.

Оба пути к красной кнопке проверяются отдельно, потому что они разные:
  • стадии доступны → `_apply_error_status` → `_stage_errors` → окно отчёта
    (`_show_stage_error_dialog`) → обработчик;
  • стадий нет → фолбэк `_update_buttons(ERROR, error_stage)` → `_error_key`
    → `_on_button_click` → обработчик напрямую.

⚠ Два модальных диалога в пути инъекции подменены (`PROTOCOL §5`): `QMessageBox`
из `_run_detection` и `ErrorReportDialog` из `_show_stage_error_dialog`. Без
подмены красный прогон не падает, а ВИСНЕТ, и на CI это выглядит как «долго».

Порог заперт с двух сторон: открыт выход при своей ошибке (`error_stage
== "detecting"`) и НЕ открыт при чужой — гейт не превращается в «любая ошибка
пускает куда угодно».
"""
import asyncio
import os
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                              # noqa: E402

pytest.importorskip("PySide6")

from fastapi import HTTPException                          # noqa: E402
from PySide6.QtCore import QObject, Signal                 # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox    # noqa: E402

import ui.widgets.error_report_dialog as erd               # noqa: E402
from app.api.detection import start_detection as srv_start_detection  # noqa: E402
from app.models import Diagram                             # noqa: E402
from app.models import DiagramStatus as SrvStatus          # noqa: E402

import ui.widgets.diagram_workspace as dw                  # noqa: E402
from ui.services.api_client import APIError, DiagramStatus  # noqa: E402

UID = "d74eb9f1-1111-2222-3333-444455556666"
TASK_NAME = "worker.tasks.detection.task_detect_yolo"


# ── поддельный сервер: гейт настоящий ────────────────────────────────────

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeDB:
    def __init__(self, diagram):
        self.diagram = diagram

    async def execute(self, stmt):
        return _FakeResult(self.diagram)

    async def commit(self):
        pass


class FakeServer:
    """Одна диаграмма + журнал отправленных задач.

    Пускать или нет, решает НАСТОЯЩАЯ корутина эндпоинта.
    """

    def __init__(self, status, error_stage=None, error_message=None):
        self.diagram = Diagram()
        self.diagram.uid = uuid.UUID(UID)
        self.diagram.status = status
        self.diagram.error_stage = error_stage
        self.diagram.error_message = error_message
        self.diagram.project_code = "thermohydraulics"
        self.diagram.detection_model = None
        self.dispatched = []

    def detect(self, model_id=None):
        """`POST /api/detection/{uid}/detect` — ровно так, как её зовёт APIClient."""
        try:
            return asyncio.run(srv_start_detection(
                uuid.UUID(UID), model_id=model_id, db=_FakeDB(self.diagram),
            ))
        except HTTPException as exc:
            # `APIClient._request` превращает ответ >= 400 ровно в это.
            raise APIError(str(exc.detail), exc.status_code) from None


class FakeDiagramInfo:
    def __init__(self, server):
        self.status = DiagramStatus(server.diagram.status.value)
        self.error_stage = server.diagram.error_stage
        self.project_code = server.diagram.project_code


class FakeAPI:
    """Поверхность APIClient, которой воркспейс пользуется в этом сценарии."""

    def __init__(self, server, stages):
        self.server = server
        self._stages = stages

    def get_diagram(self, uid):
        return FakeDiagramInfo(self.server)

    def get_stages(self, uid):
        return list(self._stages)

    def get_ocr_status(self, uid):
        return {"has_ocr_result": False}

    def get_detection_models(self, project_code):
        # Одна модель — меню выбора не открывается, идём сразу в _run_detection.
        return {"models": [{"id": "default", "name": "Ансамбль"}],
                "default_model": "default"}

    def start_detection(self, uid, model_id=None):
        return self.server.detect(model_id=model_id)


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
    """Подмена модалки: жалоба оператору штатна, но висеть на ней тест не должен."""

    StandardButton = QMessageBox.StandardButton
    calls = []

    @classmethod
    def question(cls, *args, **kwargs):
        cls.calls.append(("question", args[1] if len(args) > 1 else ""))
        return cls.StandardButton.Yes

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append(("warning", args[2] if len(args) > 2 else ""))
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, *args, **kwargs):
        cls.calls.append(("information", args[1] if len(args) > 1 else ""))
        return cls.StandardButton.Ok


class StubErrorDialog:
    """Окно отчёта об ошибке этапа: оператор нажал «🔄 Перезапустить»."""

    opened = []

    def __init__(self, stage, parent=None, phase_label=None, diagram_name=None):
        StubErrorDialog.opened.append(phase_label)

    def exec_retry(self):
        return True


def _failed_detection_stage():
    return [{
        "id": 1,
        "stage_type": "detection",
        "status": "failed",
        "attempt": 1,
        "error_message": "Detection timed out (89 min limit)",
    }]


# ── харнесс ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bench(qapp, monkeypatch):
    FakeMsgBox.calls = []
    StubErrorDialog.opened = []
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)
    monkeypatch.setattr(erd, "ErrorReportDialog", StubErrorDialog)

    from worker.celery_app import celery_app

    def _make(status, error_stage=None, stages=None):
        server = FakeServer(status, error_stage=error_stage,
                            error_message="Detection timed out (89 min limit)")

        class _AsyncResult:
            id = "task-0001"

        def _send_task(name, args=None, kwargs=None, **rest):
            server.dispatched.append(name)
            return _AsyncResult()

        monkeypatch.setattr(celery_app, "send_task", _send_task)

        ws = dw.DiagramWorkspace(FakeAPI(server, stages or []),
                                 FakeStatusProvider())
        ws.load_diagram(UID, "схема оператора")
        return ws, server

    return _make


# ── гейт пункта: выход из тупика есть ────────────────────────────────────

def test_red_button_restarts_detection_with_stages(bench):
    """Стадии доступны: клик по красной кнопке через окно отчёта запускает детекцию."""
    ws, server = bench(SrvStatus.ERROR, "detecting", _failed_detection_stage())

    btn = ws._action_buttons["detect"]
    assert btn.isEnabled(), "красная кнопка недоступна — тупик виден уже здесь"
    assert "detect" in ws._stage_errors, "упавший этап не опознан"

    btn.click()

    assert StubErrorDialog.opened == ["Поиск элементов"], "окно отчёта не открылось"
    assert server.diagram.status is SrvStatus.DETECTING, (
        f"детекция не перезапущена: на сервере {server.diagram.status.value}"
    )
    assert server.dispatched == [TASK_NAME], f"задача не ушла: {server.dispatched}"
    assert FakeMsgBox.calls == [], f"оператор получил отказ: {FakeMsgBox.calls}"


def test_red_button_restarts_detection_without_stages(bench):
    """Стадий нет: та же кнопка в фолбэк-ветке `_update_buttons` делает то же."""
    ws, server = bench(SrvStatus.ERROR, "detecting", stages=[])

    btn = ws._action_buttons["detect"]
    assert btn.isEnabled(), "красная кнопка недоступна в фолбэк-ветке"
    assert ws._stage_errors == {}, "фолбэк-ветка не должна знать стадий"

    btn.click()

    assert StubErrorDialog.opened == [], "в фолбэк-ветке окна отчёта нет"
    assert server.diagram.status is SrvStatus.DETECTING, (
        f"детекция не перезапущена: на сервере {server.diagram.status.value}"
    )
    assert server.dispatched == [TASK_NAME], f"задача не ушла: {server.dispatched}"
    assert FakeMsgBox.calls == [], f"оператор получил отказ: {FakeMsgBox.calls}"


def test_restart_clears_error_on_server(bench):
    """Перезапуск снимает ошибку — иначе диаграмма бежит с протухшим error_stage."""
    ws, server = bench(SrvStatus.ERROR, "detecting", _failed_detection_stage())

    ws._action_buttons["detect"].click()

    assert server.diagram.error_stage is None
    assert server.diagram.error_message is None


# ── порог с другой стороны: чужая ошибка выхода не открывает ─────────────

def test_foreign_error_stage_still_refused(bench):
    """Упала ДРУГАЯ стадия — детекция не стартует, оператор видит отказ.

    Гейт открыт для своей ошибки, а не для «любой ошибки»: без этой проверки
    правка `allowed = True` осталась бы зелёной.
    """
    ws, server = bench(SrvStatus.ERROR, "building_graph", _failed_detection_stage())

    ws._action_buttons["detect"].click()

    assert server.diagram.status is SrvStatus.ERROR, "чужая ошибка открыла детекцию"
    assert server.dispatched == [], f"задача ушла на чужой ошибке: {server.dispatched}"
    assert [c[0] for c in FakeMsgBox.calls] == ["warning"], (
        f"отказ не показан оператору: {FakeMsgBox.calls}"
    )


# ── сторож самого стенда ─────────────────────────────────────────────────

def test_bench_judges_by_real_endpoint():
    """Стенд судит настоящей корутиной эндпоинта, а не своей копией гейта."""
    server = FakeServer(SrvStatus.SKELETONIZED)
    with pytest.raises(APIError) as exc:
        server.detect()
    assert exc.value.status_code == 400
    assert "skeletonized" in exc.value.message

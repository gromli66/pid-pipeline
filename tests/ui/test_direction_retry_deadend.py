# -*- coding: utf-8 -*-
"""Пункт 1.12 дороги (Т2 аудита) — из упавшей классификации направления есть выход.

Дефект. Направление падает → воркер ставит `status=ERROR`,
`error_stage="direction_classification"` (`worker/tasks/direction.py:251,265`)
и `ProcessingStage(stage_type="direction_classification", status="failed")`.
Сервер к этому готов: `POST /api/segmentation/{uid}/segment` пускает `error`
и по `_STAGE_DISPATCH` перезапускает цепочку С НАПРАВЛЕНИЯ (таблицу держит
`tests/test_segmentation_status_gate.py`). Слеп КЛИЕНТ: стадии нет ни в
`_STAGE_TYPE_TO_KEY` (`diagram_workspace.py:288`), ни в `_STAGE_TO_KEY`
(`:1010`), поэтому `_error_key = None` — красной кнопки не появляется,
а в фолбэк-ветке (стадии недоступны) серыми становятся ВСЕ тринадцать.

Почему тест сценарный, а не юнит. Дефект живёт на ШВЕ «стадия сервера →
кнопка клиента»: и сервер прав, и клиент сам по себе непротиворечив. Поэтому
здесь живой `DiagramWorkspace` с настоящими сигналами Qt, а поддельный сервер
судит **настоящей** `app.api.segmentation.start_segmentation` — не моим
представлением о том, что она пропускает (образец — `tests/ui/
test_detection_retry_deadend.py`, нога 1.11 того же пункта).

Оба пути к красной кнопке проверяются отдельно, потому что они разные:
  • стадии доступны → `_apply_error_status` → `_stage_errors` → окно отчёта
    (`_show_stage_error_dialog`) → обработчик;
  • стадий нет → фолбэк `_update_buttons(ERROR, error_stage)` → `_error_key`
    → `_on_button_click` → обработчик напрямую.

Решётки — литералы, снятые ЧТЕНИЕМ обеих карт, а не вычисленные из них
(`PROTOCOL §3`): иначе набор остался бы зелёным при любом их содержимом.
Перебор идёт по ПОЛНОМУ множеству — 31 статус `DiagramStatus`, 16 значений
`StageType`, весь словарь `error_stage` конвейера.

⚠ Модальные диалоги пути инъекции подменены (`PROTOCOL §5`): `QMessageBox`
из `_start_segmentation` и `ErrorReportDialog` из `_show_stage_error_dialog`.
Без подмены красный прогон не падает, а ВИСНЕТ, и на CI это выглядит
как «долго».
"""
import asyncio
import os
import uuid
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                              # noqa: E402

pytest.importorskip("PySide6")

import celery                                              # noqa: E402
from fastapi import HTTPException                          # noqa: E402
from PySide6.QtCore import QObject, Signal                 # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox    # noqa: E402

import ui.widgets.error_report_dialog as erd               # noqa: E402
from app.api.segmentation import start_segmentation as srv_start_segmentation  # noqa: E402
from app.models import Diagram                             # noqa: E402
from app.models import DiagramStatus as SrvStatus          # noqa: E402
from app.models.stage import StageType                     # noqa: E402

import ui.widgets.diagram_workspace as dw                  # noqa: E402
from ui.services.api_client import APIError, DiagramStatus  # noqa: E402

UID = "d74eb9f1-1111-2222-3333-444455556666"
DIRECTION_TASK = "worker.tasks.direction.task_classify_direction"
SEGMENT_TASK = "worker.tasks.segmentation.task_segment_pipes"

# Размеры обоих множеств. Абсолютные числа: новое значение обязано пройти
# через эту решётку, а не проскочить мимо неё молча.
STATUS_COUNT = 31
STAGE_TYPE_COUNT = 16
BUTTON_COUNT = 13

# Значения `error_stage`, которые пишет конвейер (`set_diagram_error`).
# Словарь шире карты клиента намеренно: половина этих строк ни на какую
# кнопку не ложится, и решётка обязана это показывать.
RUNTIME_ERROR_STAGES = [
    "detecting",
    "direction_classification",
    "segmenting",
    "skeletonizing",
    "skeletonizing_simple",
    "detecting_junctions",
    "building_graph",
    "validating_graph",
    "contour_extraction",
    "generating_fxml",
    "ocr",
]
# Полный перебор: плюс все значения `StageType` (их пишет `/stages`).
ERROR_STAGES = sorted(set(RUNTIME_ERROR_STAGES) | {s.value for s in StageType})

# ── решётка 1: фолбэк-ветка `_update_buttons` (карта `_STAGE_TO_KEY`) ─────
# `error_stage` → ключ кнопки, которая станет красной. Чего нет в карте —
# нет и красной кнопки: в фолбэк-ветке это ноль доступных кнопок из 13.
RED_BY_ERROR_STAGE = {
    "detecting": "detect",
    "segmenting": "segment",
    "skeletonizing": "segment",
    "skeletonizing_simple": "pipe",
    "detecting_junctions": "junction",
    "building_graph": "graph",
    "validating_graph": "val_graph",
    "contour_extraction": "contours",
    "generating_fxml": "fxml",
    "ocr": "ocr",
}

# ── решётка 2: основной путь `_apply_error_status` (карта `_STAGE_TYPE_TO_KEY`) ─
# `stage_type` упавшей стадии → ключ, который попадёт в `_stage_errors`
# (красная бусина + красная кнопка + окно отчёта). `upload` кнопки не имеет
# по замыслу — этапа «Загрузка» в столбце нет.
FAILED_KEY_BY_STAGE_TYPE = {
    "frame_removal": "frame",
    "detection": "detect",
    "cvat_validation": "cvat",
    "segmentation": "segment",
    "skeletonization": "segment",
    "mask_validation": "pipe",
    "junction_classification": "junction",
    "final_skeletonization": "segment",
    "graph_building": "graph",
    "graph_validation": "val_graph",
    "contour_extraction": "contours",
    "ocr": "ocr",
    "layout": "edit_graph",
    "fxml_generation": "fxml",
}


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

    Пускать или нет и с какого шага — решает НАСТОЯЩАЯ корутина эндпоинта.
    """

    def __init__(self, status, error_stage=None, error_message=None):
        self.diagram = Diagram()
        self.diagram.uid = uuid.UUID(UID)
        self.diagram.status = status
        self.diagram.error_stage = error_stage
        self.diagram.error_message = error_message
        self.diagram.project_code = "thermohydraulics"
        self.dispatched = []
        self.restart_from = []

    def segment(self):
        """`POST /api/segmentation/{uid}/segment` — ровно так, как её зовёт APIClient."""
        try:
            result = asyncio.run(srv_start_segmentation(
                uuid.UUID(UID), db=_FakeDB(self.diagram),
            ))
        except HTTPException as exc:
            # `APIClient._request` превращает ответ >= 400 ровно в это.
            raise APIError(str(exc.detail), exc.status_code) from None
        self.restart_from.append(result.get("restart_from"))
        return result


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

    def get_stage_durations(self):
        return {}

    def start_segmentation(self, uid):
        return self.server.segment()


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


def stage_row(stage_type, status="failed", *, fresh=False):
    """Строка `ProcessingStage`, как её отдаёт `/stages`.

    `fresh=True` — стадия стартовала «сейчас»: иначе `_stage_stuck` снимает
    глушение кнопки по пределу ожидания (600 с) и «бежит» превращается
    в «не бежит».
    """
    started = (datetime.utcnow() if fresh
               else datetime(2026, 8, 19, 10, 0, 0)).isoformat()
    return {
        "id": 1,
        "stage_type": stage_type,
        "status": status,
        "attempt": 1,
        "error_message": "Direction classification timed out (9 min limit)",
        "started_at": started,
        "created_at": started,
    }


def _failed_direction_stages():
    """Боевая картина: валидация детекции прошла, направление упало."""
    return [stage_row("cvat_validation", "completed"),
            stage_row("direction_classification", "failed")]


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
                            error_message="Direction classification timed out (9 min limit)")

        class _AsyncResult:
            id = "task-0001"

        def _send_task(name, args=None, kwargs=None, **rest):
            server.dispatched.append(name)
            return _AsyncResult()

        class _FakeChain:
            def __init__(self, *signatures):
                self._signatures = signatures

            def apply_async(self, *a, **kw):
                for sig in self._signatures:
                    server.dispatched.append(sig["task"])
                return _AsyncResult()

        monkeypatch.setattr(celery_app, "send_task", _send_task)
        monkeypatch.setattr(celery, "chain", _FakeChain)

        ws = dw.DiagramWorkspace(FakeAPI(server, stages or []),
                                 FakeStatusProvider())
        ws.load_diagram(UID, "схема оператора")
        return ws, server

    return _make


def red_keys(ws):
    return sorted(k for k, b in ws._action_buttons.items()
                  if b.styleSheet() == dw._BTN_STYLE_RED)


def enabled_keys(ws):
    return sorted(k for k, b in ws._action_buttons.items() if b.isEnabled())


# ── сторожа самих решёток ────────────────────────────────────────────────

def test_machine_sizes_are_locked():
    """Обе решётки написаны на машину ровно такого размера."""
    assert len(list(SrvStatus)) == STATUS_COUNT
    assert len(list(StageType)) == STAGE_TYPE_COUNT


def test_grid_keys_name_real_buttons(bench):
    """Сторож набора: значение решётки — существующая кнопка, а не опечатка."""
    ws, _ = bench(SrvStatus.ERROR, "detecting", stages=[])
    keys = set(ws._action_buttons)
    assert len(keys) == BUTTON_COUNT
    assert set(RED_BY_ERROR_STAGE.values()) <= keys
    assert set(FAILED_KEY_BY_STAGE_TYPE.values()) <= keys


def test_grid_keys_name_real_stage_types():
    """Ключи решётки 2 — существующие `StageType`, а не выдуманные строки."""
    for value in FAILED_KEY_BY_STAGE_TYPE:
        assert StageType(value).value == value


# ── решётка 1: фолбэк-ветка, весь словарь `error_stage` ──────────────────

@pytest.mark.parametrize("stage", ERROR_STAGES)
def test_fallback_paints_exactly_the_grid(stage, bench):
    """Стадий нет: красной становится ровно та кнопка, что в решётке.

    Порог заперт с двух сторон. Есть в решётке — ровно ОДНА красная и она же
    единственная доступная. Нет в решётке — доступных кнопок НОЛЬ из 13,
    то есть тупик: оператору нечего нажать вовсе.
    """
    ws, _ = bench(SrvStatus.ERROR, stage, stages=[])
    expected = RED_BY_ERROR_STAGE.get(stage)

    if expected is not None:
        assert red_keys(ws) == [expected]
        assert enabled_keys(ws) == [expected]
    else:
        assert red_keys(ws) == []
        assert enabled_keys(ws) == [], (
            f"'{stage}' не в решётке, но кнопка доступна — решётка протухла"
        )


# ── решётка 2: основной путь, весь `StageType` ──────────────────────────

@pytest.mark.parametrize("stage_type", list(StageType), ids=lambda s: s.value)
def test_stage_overlay_paints_exactly_the_grid(stage_type, bench):
    """Упавшая стадия из `/stages`: красной становится ровно та, что в решётке.

    Стадия вне решётки не даёт ни красной бусины, ни окна отчёта: сбой
    не показан оператору вовсе.
    """
    ws, _ = bench(SrvStatus.ERROR, stage_type.value,
                  stages=[stage_row(stage_type.value, "failed")])
    expected = FAILED_KEY_BY_STAGE_TYPE.get(stage_type.value)

    if expected is not None:
        assert sorted(ws._stage_errors) == [expected]
        assert red_keys(ws) == [expected]
    else:
        assert sorted(ws._stage_errors) == []
        assert red_keys(ws) == [], (
            f"'{stage_type.value}' не в решётке, но кнопка покрасилась"
        )


# ── решётка 3: тупик на полном множестве статусов ────────────────────────

@pytest.mark.parametrize("status", list(SrvStatus), ids=lambda s: s.value)
def test_direction_error_over_every_status(status, bench):
    """31 статус машины с `error_stage='direction_classification'`.

    Утверждение абсолютное: тупик — РОВНО ОДНА клетка решётки. Ноль доступных
    кнопок бывает только в `error`; во всех тридцати остальных статусах
    `error_stage` вообще не участвует в расчёте (ветка `_error_key` заперта
    условием `status == ERROR`), и оператору всегда есть что нажать.
    """
    ws, _ = bench(status, "direction_classification", stages=[])

    if status is SrvStatus.ERROR:
        assert enabled_keys(ws) == [], (
            "тупик 1.12 больше не воспроизводится — пересними решётку"
        )
    else:
        assert enabled_keys(ws), f"статус {status.value} осиротел без ошибки"

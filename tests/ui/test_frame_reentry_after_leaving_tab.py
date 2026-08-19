# -*- coding: utf-8 -*-
"""Пункт 1.17 дороги — вернуться в «Очистку рамки» после выхода из вкладки.

Зачем. Пункт 1.3 снял молчаливый откат и вернул повторный вход поправкой
`_MANUAL_INPROGRESS` в `_update_buttons` (#3): у ручного `*ING`-этапа кнопка
остаётся кликабельной, «иначе после „открыл и вышел“ не зайти». Замер §75
показал, что поправка перекрывается — и жест из строки 1.17 («Очистка рамки →
💾 → ← Назад») ломается снова, только иначе.

Механизм. Стадию `frame_removal` открывает RUNNING-строкой сам сервер
(`app/api/frame.py:105`, канон §8.5 — строка видна в `/stages`, пока оператор
во вкладке), а закрыть её при выходе некому: «← Назад» на сервер не ходит,
`/complete` и `/skip` не звались. Цикл бегущих стадий (`_update_buttons`)
сметает в `processing` кнопку ЛЮБОЙ RUNNING-стадии и делает это ПОСЛЕ
поправки #3 — кнопка «Очистка рамки» становится синей и `enabled=False`
на `WAIT_LIMIT_S` = 600 с. Оператор не может вернуться к СВОЕЙ ЖЕ сохранённой
очистке; сама очистка при этом цела (её сохранность стережёт
`tests/test_frame_status_gate.py`).

Цикл заводился под РАСКЛАДКУ — у неё нет своего статуса, и без него её кнопка
оставалась доступной во время счёта. Поэтому порог здесь заперт С ДВУХ СТОРОН:
ручной этап своей строкой не глушится, а раскладка и любая авто-стадия — по-прежнему
глушатся. `frame_removal` — единственная стадия, чья строка живёт сквозь сеанс
оператора (замер §75.15: `mask_validation`/`graph_validation` строк не заводит
никто, CVAT открывает и закрывает свою внутри одного запроса), поэтому и правило
пишется про ручной этап ТЕКУЩЕГО статуса, а не про тип стадии.

Что проверяется — только наблюдаемое оператором: `isEnabled()` кнопки и её цвет.
Ни одного утверждения про внутренние множества `_update_buttons`. Числа
абсолютные: 60 с (строка свежая) и 700 с (строка старше предела), а сам предел
заперт отдельным утверждением — иначе тест вычислял бы свой вход из константы,
поведение которой проверяет (`PROTOCOL §3`).
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                            # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal               # noqa: E402
from PySide6.QtWidgets import (                          # noqa: E402
    QApplication, QHBoxLayout, QMessageBox, QVBoxLayout, QWidget,
)

import ui.widgets.diagram_workspace as dw                # noqa: E402
from ui.services.api_client import DiagramStatus         # noqa: E402
from ui.services.layout_gate import WAIT_LIMIT_S         # noqa: E402

UID = "d74eb9f1-1111-2222-3333-444455556666"

FRESH_S = 60      # строка стадии моложе предела ожидания
STALE_S = 700     # строка стадии старше предела ожидания

# Цвета из стилей `_update_buttons` — то, что видит оператор.
BLUE = "#2196F3"      # «в процессе», кнопка выключена
YELLOW = "#FF9800"    # «доступна»
GREEN = "#4CAF50"     # «пройдена, кликабельна для отката»


# ── харнесс ──────────────────────────────────────────────────────────────

class FakeDiagram:
    def __init__(self, status):
        self.status = status
        self.error_stage = None
        self.cvat_task_id = None
        self.project_code = "thermohydraulics"


class FakeAPI:
    """Поверхность APIClient, которой пользуется воркспейс в этом сценарии.

    `start_frame_removal` двигает статус ровно так, как настоящий
    `POST /api/frame/{uid}/start` (`app/api/frame.py:102-109`), и заводит
    RUNNING-строку стадии — без неё сценарий не воспроизводит бой.
    """

    def __init__(self, status):
        self.status = status
        self.calls = []
        self.stages = []

    def get_diagram(self, uid):
        return FakeDiagram(self.status)

    def get_ocr_status(self, uid):
        return {"has_ocr_result": False}

    def get_stages(self, uid):
        return list(self.stages)

    def get_stage_durations(self):
        return {}

    def start_frame_removal(self, uid):
        self.calls.append("start_frame_removal")
        self.status = DiagramStatus.CLEANING_FRAME
        self.stages.append(_running("frame_removal", FRESH_S))
        return {"status": "cleaning_frame"}


class FakeStatusProvider(QObject):
    status_updated = Signal(str, object)
    stages_updated = Signal(str, object)

    def watch(self, uid):
        pass

    def unwatch(self, uid):
        pass

    def is_watching(self, uid):
        return False


class StubTab(QWidget):
    """Вкладка-заглушка: тулбар первым элементом — воркспейс врежет «← Назад»."""

    confirmed = Signal()
    status_message = Signal(str)

    def __init__(self, diagram_uid=None, diagram_name=None, api_client=None,
                 parent=None):
        super().__init__(parent)
        self._confirmed = False
        root = QVBoxLayout(self)
        root.addLayout(QHBoxLayout())

    def set_project_code(self, code):
        pass


class FakeMsgBox:
    """Подмена модального диалога: в пути закрытия он штатен, и красный прогон
    обязан падать, а не виснуть (`PROTOCOL §5`, четвёртый исход)."""

    StandardButton = QMessageBox.StandardButton
    calls = []

    @classmethod
    def question(cls, *args, **kwargs):
        cls.calls.append("question")
        return cls.StandardButton.Yes

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append("warning")
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, *args, **kwargs):
        cls.calls.append("information")
        return cls.StandardButton.Ok


def _running(stage_type, age_s):
    """RUNNING-строка `/stages` возрастом `age_s` секунд."""
    started = (datetime.utcnow() - timedelta(seconds=age_s)).isoformat()
    return {"stage_type": stage_type, "status": "running",
            "started_at": started, "created_at": started}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bench(qapp, monkeypatch):
    """Воркспейс на поддельном сервере; вкладки — заглушки."""
    FakeMsgBox.calls = []
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)
    monkeypatch.setattr("ui.tabs.frame_tab.FrameTab", StubTab)

    def _make(status):
        api = FakeAPI(status)
        ws = dw.DiagramWorkspace(api, FakeStatusProvider())
        return ws, api

    return _make


def _btn(ws, key):
    """Что видит оператор: (кнопка нажимаема, цвет)."""
    button = ws._action_buttons[key]
    sheet = button.styleSheet()
    colour = next((c for c in (BLUE, YELLOW, GREEN) if c in sheet), "серая")
    return button.isEnabled(), colour


# ── замок предела: числа теста осмысленны только при этом значении ───────

def test_wait_limit_is_ten_minutes():
    """`FRESH_S`/`STALE_S` выбраны по этому пределу — он заперт явно."""
    assert WAIT_LIMIT_S == 600.0
    assert FRESH_S < WAIT_LIMIT_S < STALE_S


# ── сценарий строки пункта: «Очистка рамки → 💾 → ← Назад» ───────────────

def test_operator_can_reenter_frame_tab_after_leaving(bench):
    """Жест целиком: открыл вкладку, сохранил, вышел — и может войти снова.

    Сервер в этот момент держит `frame_removal` RUNNING (её открыл `/start`,
    закрыть некому). Кнопка обязана остаться жёлтой и нажимаемой: очистка
    сохранена и лежит на сервере, вернуться к ней — единственный ход вперёд
    (`cleaning_frame` иначе выхода не имеет, замер §51.23).
    """
    ws, api = bench(DiagramStatus.UPLOADED)
    ws.load_diagram(UID, "схема оператора")
    assert _btn(ws, "frame") == (True, YELLOW), "вход недоступен до открытия"

    ws._action_buttons["frame"].click()
    assert ws._active_tab is not None, "вкладка не открылась"
    assert api.calls == ["start_frame_removal"], "клиент не открыл стадию"

    ws._btn_back_injected.click()           # ← Назад
    assert ws._active_tab is None, "вкладка не закрылась"

    # Следующий тик опроса приносит стадии — вот здесь дефект и срабатывал.
    ws._on_stages_updated(UID, api.get_stages(UID))
    ws._apply_status(DiagramStatus.CLEANING_FRAME)

    assert _btn(ws, "frame") == (True, YELLOW), (
        "после выхода из вкладки вернуться в «Очистку рамки» нечем: "
        "RUNNING-строка стадии перекрыла поправку повторного входа"
    )
    ws.cleanup()


@pytest.mark.parametrize("status,key,stage_type", [
    (DiagramStatus.CLEANING_FRAME, "frame", "frame_removal"),
], ids=lambda v: getattr(v, "value", v))
def test_manual_stage_button_survives_its_own_running_row(
        bench, status, key, stage_type):
    """Ручной `*ING`-этап своей же строкой стадии не глушится."""
    ws, api = bench(status)
    ws.load_diagram(UID, "схема оператора")
    ws._on_stages_updated(UID, [_running(stage_type, FRESH_S)])
    ws._apply_status(status)

    assert _btn(ws, key) == (True, YELLOW)
    ws.cleanup()


def test_running_row_of_another_stage_still_disables_frame(bench):
    """Замок с другой стороны: чужая бегущая стадия кнопку глушит по-прежнему.

    Правило узкое — «своя строка ручного этапа», а не «строки не смотрим».
    """
    ws, api = bench(DiagramStatus.UPLOADED)
    ws.load_diagram(UID, "схема оператора")
    ws._on_stages_updated(UID, [_running("detection", FRESH_S)])
    ws._apply_status(DiagramStatus.UPLOADED)

    assert _btn(ws, "detect") == (False, BLUE)
    ws.cleanup()


# ── замки цикла бегущих стадий: он заводился под раскладку ───────────────

def test_layout_running_row_still_disables_edit_graph(bench):
    """Раскладка — ради чего цикл и написан — глушится как раньше.

    У неё нет своего статуса, кнопку держит только строка стадии.
    """
    ws, api = bench(DiagramStatus.OCR_BOUND)
    ws.load_diagram(UID, "схема оператора")
    ws._on_stages_updated(UID, [_running("layout", FRESH_S)])
    ws._apply_status(DiagramStatus.OCR_BOUND)

    assert _btn(ws, "edit_graph") == (False, BLUE)
    ws.cleanup()


def test_auto_stage_running_row_still_disables_its_button(bench):
    """Любая авто-стадия глушит свою кнопку — поведение не тронуто."""
    ws, api = bench(DiagramStatus.VALIDATED_BBOX)
    ws.load_diagram(UID, "схема оператора")
    ws._on_stages_updated(UID, [_running("segmentation", FRESH_S)])
    ws._apply_status(DiagramStatus.VALIDATED_BBOX)

    assert _btn(ws, "segment") == (False, BLUE)
    ws.cleanup()


def test_stuck_auto_stage_releases_its_button(bench):
    """Выход по пределу у авто-стадии сохранён: повисшая задача не глушит вечно."""
    ws, api = bench(DiagramStatus.VALIDATED_BBOX)
    ws.load_diagram(UID, "схема оператора")
    ws._on_stages_updated(UID, [_running("segmentation", STALE_S)])
    ws._apply_status(DiagramStatus.VALIDATED_BBOX)

    assert _btn(ws, "segment") == (True, YELLOW)
    ws.cleanup()


def test_pending_row_does_not_disable_anything(bench):
    """`pending` без старта кнопку не глушит — как и было."""
    ws, api = bench(DiagramStatus.UPLOADED)
    ws.load_diagram(UID, "схема оператора")
    row = _running("detection", FRESH_S)
    row["status"] = "pending"
    ws._on_stages_updated(UID, [row])
    ws._apply_status(DiagramStatus.UPLOADED)

    assert _btn(ws, "detect")[0] is not False or _btn(ws, "detect")[1] != BLUE
    ws.cleanup()

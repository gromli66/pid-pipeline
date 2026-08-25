# -*- coding: utf-8 -*-
"""Вход в пройденный этап фазы B — что клиент делает СЕГОДНЯ (блок 3, до правки).

Дефект. Оператор, кликнувший по уже пройденной бусине, получает ОДИН диалог на
все кнопки: «Этап «val_graph» уже пройден. Откатить до «built»?». Ответ «Да»
разрушает конвейер (артефакты последующих этапов удаляются), ответ «Нет» не
открывает ничего — то есть посмотреть на пройденный этап НЕЛЬЗЯ. Для фазы B
(«Проверка схемы» ⇄ «Контуры» ⇄ «Распознавание» ⇄ «Привязка») это прямо против
решений Максима №7/№8: этапы там разные, а вход в пройденный — не откат.

Что судится. Ветка `DiagramWorkspace._on_button_click` на живом воркспейсе:
какие клики уходят в обработчик молча, какие спрашивают и о чём именно.
Обработчик подменён журналом — тяжёлые вкладки к решению «откат или вход»
отношения не имеют, а их конструкторы качают артефакты.

Класс правки — [сма]: эта редакция снята с НЕТРОНУТОГО клиента и зелена на нём.
Пункты Н3+/Н4/5-1 переписывают её тем же коммитом, что и код, — по диффу видно,
какие именно клетки сменили исход.

⚠ `QMessageBox` подменён (`PROTOCOL §5`): без подмены красный прогон не падает,
а ВИСНЕТ на живой модалке, и на CI это выглядит как «долго».
"""
import os
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                              # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QObject, Signal          # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox    # noqa: E402

import ui.widgets.diagram_workspace as dw                  # noqa: E402
from ui.services.api_client import DiagramStatus           # noqa: E402

UID = str(uuid.UUID("b7a5e011-1111-2222-3333-444455556666"))

# Ключи фазы B: свободный вход по решениям №7/№8.
PHASE_B_KEYS = ("val_graph", "contours", "ocr_binding", "edit_graph")

# Ключи фазы A: конвейер там линейный, повторный проход разрушающий —
# диалог отката остаётся.
PHASE_A_KEYS = ("frame", "detect", "segment", "pipe", "junction", "graph")

# Два ключа со своим поведением, к откату отношения не имеющие.
OWN_BRANCH_KEYS = ("cvat", "fxml")

# Статус, при котором ВСЕ тринадцать этапов числятся пройденными.
ALL_DONE = DiagramStatus.COMPLETED


# ── харнесс ──────────────────────────────────────────────────────────────

class FakeMsgBox:
    """Подмена модалки: журнал (вид, заголовок, текст) + заданный ответ."""

    StandardButton = QMessageBox.StandardButton
    calls = []
    answer = QMessageBox.StandardButton.Yes

    @classmethod
    def question(cls, parent, title, text, *args, **kwargs):
        cls.calls.append(("question", title, text))
        return cls.answer

    @classmethod
    def warning(cls, parent, title, text="", *args, **kwargs):
        cls.calls.append(("warning", title, text))
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, parent, title, text="", *args, **kwargs):
        cls.calls.append(("information", title, text))
        return cls.StandardButton.Ok


class FakeDiagramInfo:
    def __init__(self, status):
        self.status = status
        self.error_stage = None
        self.project_code = "thermohydraulics"


class FakeAPI:
    """Поверхность APIClient, которой воркспейс пользуется в этом сценарии."""

    def __init__(self, status):
        self.status = status
        self.rollbacks = []

    def get_diagram(self, uid):
        return FakeDiagramInfo(self.status)

    def get_stages(self, uid):
        return []

    def get_ocr_status(self, uid):
        return {"has_ocr_result": True}

    def rollback_diagram(self, uid, target_status, preserve_ocr=False,
                         preserve_contours=False):
        self.rollbacks.append({
            "target": target_status,
            "preserve_ocr": preserve_ocr,
            "preserve_contours": preserve_contours,
        })
        return {"deleted_artifacts": 0}


class FakeStatusProvider(QObject):
    status_updated = Signal(str, object)
    stages_updated = Signal(str, object)

    def watch(self, uid):
        pass

    def unwatch(self, uid):
        pass

    def is_watching(self, uid):
        return False


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bench(qapp, monkeypatch):
    """Воркспейс на заданном статусе + журнал кликов.

    Свои виджеты набор сносит сам, детерминированно (`PROTOCOL §5`, правило
    1-32): брошенные на сборщик мусора виджеты оставляют отложенные удаления,
    которые детонируют у соседа, первым провернувшего очередь событий.
    """
    FakeMsgBox.calls = []
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)

    made = []

    def _make(status=ALL_DONE):
        api = FakeAPI(status)
        ws = dw.DiagramWorkspace(api, FakeStatusProvider())
        ws.load_diagram(UID, "схема оператора")
        made.append(ws)
        opened = []
        return ws, api, opened

    yield _make

    for ws in made:
        ws.hide()
        ws.setParent(None)
        ws.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _click(ws, key, opened):
    """Клик по кнопке этапа с подменённым обработчиком."""
    ws._on_button_click(key, lambda: opened.append(key))


# ── сторожа стенда ───────────────────────────────────────────────────────

def test_bench_really_puts_every_stage_into_completed(bench):
    """Все тринадцать кнопок при `completed` числятся пройденными.

    Без этого утверждения тесты ниже могли бы проходить просто потому, что
    ветка «этап пройден» не достигается вовсе.
    """
    ws, _api, _opened = bench()
    _available, completed, _processing = dw._buttons_for_status(ALL_DONE)
    assert set(PHASE_A_KEYS) | set(PHASE_B_KEYS) | {"ocr"} <= completed
    assert ws._last_status is ALL_DONE


def test_key_sets_cover_every_button():
    """Перебор ведётся полным списком кнопок, а не выборкой (`PROTOCOL §3`)."""
    declared = {k for k, _label in dw.DiagramWorkspace._BUTTON_DEFS}
    assert declared == set(PHASE_A_KEYS) | set(PHASE_B_KEYS) | set(OWN_BRANCH_KEYS) | {"ocr"}


# ── фаза B: вход свободный ───────────────────────────────────────────────

@pytest.mark.parametrize("key", PHASE_B_KEYS)
def test_phase_b_entry_offers_a_rollback_today(key, bench):
    """СЕГОДНЯ вход в пройденный этап фазы B — это откат с диалогом."""
    ws, api, opened = bench()
    _click(ws, key, opened)

    assert [c[0] for c in FakeMsgBox.calls] == ["question"], key
    assert len(api.rollbacks) == 1, key
    assert api.rollbacks[0]["target"] == ws._ROLLBACK_TARGET[key], key
    assert opened == [key], key


@pytest.mark.parametrize("key", PHASE_B_KEYS)
def test_phase_b_refusal_opens_nothing_today(key, bench):
    """Второй берег того же дефекта: «Нет» — и вкладка не открывается вовсе."""
    ws, api, opened = bench()
    FakeMsgBox.answer = QMessageBox.StandardButton.No
    _click(ws, key, opened)

    assert len(FakeMsgBox.calls) == 1, key
    assert api.rollbacks == [], key
    assert opened == [], f"{key}: посмотреть на пройденный этап нельзя"


# ── распознавание: свой диалог, не откатный ──────────────────────────────

def test_ocr_offers_a_rollback_today(bench):
    """`ocr` сегодня идёт той же откатной веткой, что и все остальные."""
    ws, api, opened = bench()
    _click(ws, "ocr", opened)

    kind, title, _text = FakeMsgBox.calls[0]
    assert kind == "question"
    assert title == "Откат"
    assert api.rollbacks == [{"target": "validated_graph", "preserve_ocr": False,
                              "preserve_contours": False}]


def test_ocr_rollback_destroys_contours_today(bench):
    """Дефект пункта 5-1, замеренный на ЖИВОМ клиенте: контуры гибнут.

    Оба preserve-флага считаются по кортежу `("graph", "val_graph",
    "contours")`, ключа `ocr` в нём нет — значит «Переделать OCR» откатывает
    до `validated_graph` БЕЗ сохранения контуров, и SAM2-разметка, посчитанная
    поточечно руками, уходит вместе с ним. `preserve_ocr` при «Переделать OCR»
    должен оставаться False — старые OCR-артефакты обязаны сноситься.
    """
    ws, api, opened = bench()
    _click(ws, "ocr", opened)

    assert api.rollbacks[0]["preserve_contours"] is False
    assert api.rollbacks[0]["preserve_ocr"] is False


# ── фаза A: откат остаётся, но диалог честный (Н4) ───────────────────────

@pytest.mark.parametrize("key", PHASE_A_KEYS)
def test_phase_a_still_offers_rollback(key, bench):
    """Фаза A линейна, повторный проход разрушающий — диалог отката на месте."""
    ws, api, opened = bench()
    _click(ws, key, opened)

    assert [c[0] for c in FakeMsgBox.calls] == ["question"], key
    assert len(api.rollbacks) == 1, key
    assert api.rollbacks[0]["target"] == ws._ROLLBACK_TARGET[key], key
    assert opened == [key], key


@pytest.mark.parametrize("key", PHASE_A_KEYS)
def test_rollback_dialog_shows_technical_keys_today(key, bench):
    """Н4, дефект: в диалоге стоят технические ключи и сырые статусы."""
    ws, _api, opened = bench()
    _click(ws, key, opened)

    _kind, _title, text = FakeMsgBox.calls[0]
    assert f"«{key}»" in text, f"{key}: ключ в диалоге не найден: {text}"
    assert f"«{ws._ROLLBACK_TARGET[key]}»" in text, key


@pytest.mark.parametrize("key", PHASE_A_KEYS)
def test_rollback_dialog_says_nothing_about_the_canvas_today(key, bench):
    """Н4, второй дефект: о гибели правок «Ручной правки» диалог молчит."""
    ws, _api, opened = bench()
    _click(ws, key, opened)

    _kind, _title, text = FakeMsgBox.calls[0]
    assert "Ручной правке" not in text, key
    assert "холст" not in text.lower(), key


# ── два ключа со своей веткой ────────────────────────────────────────────

@pytest.mark.parametrize("key", OWN_BRANCH_KEYS)
def test_own_branch_keys_never_rollback(key, bench):
    """`cvat` и `fxml` откат не предлагают — у них свои диалоги внутри."""
    ws, api, opened = bench()
    _click(ws, key, opened)

    assert api.rollbacks == [], key
    assert opened == [key], key

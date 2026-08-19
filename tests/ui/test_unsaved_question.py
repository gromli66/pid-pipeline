# -*- coding: utf-8 -*-
"""Пункт 1.21 дороги — вопрос о несохранённом при уходе из вкладки.

Зачем. Вопрос «Сохранить перед…» стоял в ДВУХ местах с разной логикой
(`DiagramWorkspace._close_active_tab` и `MainWindow._confirm_unsaved`, второе
завёл пункт 1.18), а одна вкладка не участвовала в нём вовсе. Всё замерено
ДО правки (MEASUREMENTS §81):

1. **«Очистка рамки» уходит молча на ОБОИХ путях.** `FrameTab` — единственная
   вкладка без `has_unsaved_changes()` (инвентарь: 9 классов, контракт у 8),
   а обе двери начинаются с `hasattr(tab, 'has_unsaved_changes')`. Оператор
   обвёл полигон, нажал «← Назад» — диалогов 0, работа уходит.
2. **Двери расходятся в четырёх клетках:** текст вопроса; «Да» у вкладки без
   метода сохранения («← Назад» оставляет вкладку открытой БЕЗ объяснения —
   тупик, ✕ окна закрывается с записью в лог); след в логе на «Нет»; строка
   в статусбаре при отказе сохранения.
3. **«Отмена» на «← Назад» убивает автосохранение.** `_autosave.stop()` стоял
   ДО вопроса, поэтому отказ закрывать оставлял вкладку открытой с мёртвым
   таймером (замер: `isActive()` False, `_tab` None). На пути ✕ этого не было.

Проверяются ДАННЫЕ (принцип набора 0.4): журнал диалогов, журнал вызовов
сервера, что вернул `QWidget.close()`, состояние таймера автосохранения. Ни
одного утверждения про внутренние флаги вкладок — набор обязан пережить
декомпозицию UI (этап 10).

Модальный диалог подменяется утверждением о ФАКТЕ вызова, а не таймаутом
(`PROTOCOL §5`): без подмены набор повис бы, а не упал. Свои виджеты набор
сносит сам и детерминированно (пятый исход зонда, замер 1-32): брошенные на
сборщик мусора Qt-виджеты детонируют у того, кто первым крутит очередь событий.
"""
import os
import time
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                               # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal                  # noqa: E402
from PySide6.QtGui import QImage                            # noqa: E402
from PySide6.QtWidgets import (                             # noqa: E402
    QApplication, QHBoxLayout, QMessageBox, QVBoxLayout, QWidget,
)

import ui.tabs.frame_tab as ft                              # noqa: E402
import ui.tabs.pipe_tab as pt                               # noqa: E402
import ui.widgets.diagram_workspace as dw                   # noqa: E402
import ui.windows.main_window as mw                         # noqa: E402
from ui.services.api_client import APIError, DiagramStatus  # noqa: E402
from ui.services.autosave import AutoSaveService            # noqa: E402
from ui.services.ui_settings import UISettings              # noqa: E402

UID = "0fc9d04c-1111-2222-3333-444455556666"

# Литералы сценария. Намеренно НЕ импортируются из проверяемого модуля: тест,
# вычисляющий свой вход из проверяемой константы, зелен при любом её значении
# (запрет `PROTOCOL §3`).
ASK_ON_TAB_CLOSE = "Есть несохранённые изменения. Сохранить перед закрытием?"
ASK_ON_EXIT = "Есть несохранённые изменения. Сохранить перед выходом?"


# ── харнесс ──────────────────────────────────────────────────────────────

class FakeDiagram:
    status = DiagramStatus.UPLOADED
    error_stage = None
    project_code = "thermohydraulics"


class FakeAPI:
    """Поверхность `APIClient`, которой пользуются окно, воркспейс и вкладка."""

    def __init__(self, *args, **kwargs):
        self.base_url = "http://fake"
        self.saved_images = []
        self.completed = []
        self.save_fails = False

    def health_check(self):
        return True

    def list_projects(self):
        return [{"code": "thermohydraulics", "name": "Термогидравлика"}]

    def list_diagrams(self):
        return []

    def get_diagram(self, uid):
        return FakeDiagram()

    def get_stages(self, uid):
        return []

    def get_ocr_status(self, uid):
        return {"has_ocr_result": False}

    def download_artifact(self, uid, kind, dest):
        img = QImage(40, 30, QImage.Format.Format_ARGB32)
        img.fill(0xFFFFFFFF)
        img.save(str(dest), "PNG")

    def save_cleaned_image(self, uid, path):
        if self.save_fails:
            raise APIError("сервер отказал")
        self.saved_images.append(str(path))

    def complete_frame_removal(self, uid):
        self.completed.append(uid)

    def close(self):
        pass


class FakeStatusProvider(QObject):
    status_updated = Signal(str, object)
    stages_updated = Signal(str, object)
    error_occurred = Signal(str, str)

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.watched = []
        self.unwatched = []

    def watch(self, uid):
        self.watched.append(uid)

    def unwatch(self, uid):
        self.unwatched.append(uid)

    def is_watching(self, uid):
        return uid in self.watched and uid not in self.unwatched


class FakeMsgBox:
    """Подмена модального диалога (`PROTOCOL §5`)."""

    StandardButton = QMessageBox.StandardButton
    calls = []
    answer = QMessageBox.StandardButton.Yes

    @classmethod
    def question(cls, *args, **kwargs):
        cls.calls.append(("question", args[1] if len(args) > 1 else "",
                          args[2] if len(args) > 2 else ""))
        return cls.answer

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append(("warning", args[1] if len(args) > 1 else "",
                          args[2] if len(args) > 2 else ""))
        return cls.StandardButton.Ok


class GraphLikeTab(QWidget):
    """Вкладка с полным контрактом — замок «старое поведение не поехало»."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.unsaved = True
        self.save_ok = True
        self.saves = []
        root = QVBoxLayout(self)
        root.addLayout(QHBoxLayout())

    def has_unsaved_changes(self):
        return self.unsaved

    def _save_graph(self):
        self.saves.append(True)
        if self.save_ok:
            self.unsaved = False
        return self.save_ok


class NoSaveTab(QWidget):
    """Вкладка умеет сказать «грязная», но сохраняться не умеет (родня CvatTab)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.addLayout(QHBoxLayout())

    def has_unsaved_changes(self):
        return True


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, monkeypatch):
    """Главное окно на поддельном сервере, с открытой диаграммой."""
    FakeMsgBox.calls = []
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes
    api = FakeAPI()
    monkeypatch.setattr(mw, "APIClient", lambda *a, **k: api)
    monkeypatch.setattr(mw, "StatusProvider", FakeStatusProvider)
    monkeypatch.setattr(mw, "QMessageBox", FakeMsgBox)
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)
    monkeypatch.setattr(ft, "QMessageBox", FakeMsgBox)

    win = mw.MainWindow()
    win.show()
    win.workspace.load_diagram(UID, "схема оператора")
    win.api = api
    yield win
    # Детерминированный снос: вкладку и окно убираем сами, очередь удалений
    # прокручиваем здесь же — иначе она детонирует у соседнего теста (1-32).
    win.workspace._force_close_tab()
    win.hide()
    win.deleteLater()
    qapp.processEvents()


def _open_frame_tab(win, edits=1):
    """Оператор открыл «Очистку рамки» и применил `edits` операций."""
    tab = ft.FrameTab(UID, "схема оператора", win.api)
    win.workspace._open_tab(tab, "frame")
    QApplication.processEvents()          # singleShot(0) — загрузка исходника
    for _ in range(edits):
        tab.editor.undo_stack.append(tab.editor.image.copy())
    return tab


def _open_stub(win, tab):
    win.workspace._open_tab(tab, "val_graph")
    return tab


# ── буква пункта: «Очистка рамки» уходила молча ──────────────────────────

def test_frame_tab_with_edits_is_asked_on_back(window):
    """«← Назад» с применённой очисткой задаёт вопрос — ровно один раз."""
    _open_frame_tab(window)

    window.workspace._close_active_tab()

    assert [c[0] for c in FakeMsgBox.calls] == ["question"], (
        f"«← Назад» прошёл мимо вопроса о несохранённом: {FakeMsgBox.calls}"
    )


def test_frame_tab_with_edits_is_asked_on_window_close(window):
    """✕ окна с применённой очисткой тоже задаёт вопрос."""
    _open_frame_tab(window)

    window.close()

    assert [c[0] for c in FakeMsgBox.calls] == ["question"], (
        f"выход прошёл мимо вопроса о несохранённом: {FakeMsgBox.calls}"
    )


def test_frame_tab_yes_saves_the_cleaned_image(window):
    """«Да» → очищенное изображение ушло на сервер, вкладка закрыта."""
    _open_frame_tab(window)
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes

    window.workspace._close_active_tab()

    assert len(window.api.saved_images) == 1, (
        f"очистка не сохранена перед закрытием: {window.api.saved_images}"
    )
    assert window.workspace._active_tab is None, "вкладка осталась открытой"


def test_frame_tab_yes_saves_the_cleaned_image_on_exit(window):
    """То же на пути ✕ окна: обе двери обязаны знать ОДИН список методов.

    Замок против расползания дверей: `_save_image` — единственный метод,
    которого не было в кортеже окна, пока кортежей было два.
    """
    _open_frame_tab(window)
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes

    closed = window.close()

    assert len(window.api.saved_images) == 1, (
        f"выход не сохранил очистку: {window.api.saved_images}"
    )
    assert closed is True, "клиент не закрылся после успешного сохранения"


def test_frame_tab_yes_does_not_complete_the_stage(window):
    """Сохранение при уходе — не «Подтвердить»: этап не завершается."""
    _open_frame_tab(window)
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes

    window.workspace._close_active_tab()

    assert window.api.completed == [], (
        f"уход из вкладки завершил этап за оператора: {window.api.completed}"
    )


def test_frame_tab_clean_closes_without_a_question(window):
    """Вкладка без единой операции вопроса не поднимает."""
    _open_frame_tab(window, edits=0)

    window.workspace._close_active_tab()

    assert FakeMsgBox.calls == [], (
        f"лишний вопрос на нетронутой очистке: {FakeMsgBox.calls}"
    )
    assert window.workspace._active_tab is None, "вкладка не закрылась"


def test_frame_tab_saved_edits_are_not_asked_again(window):
    """После 💾 вкладка чистая — второй вопрос оператору не задают."""
    tab = _open_frame_tab(window)
    tab._on_save_only()
    FakeMsgBox.calls = []

    window.workspace._close_active_tab()

    assert FakeMsgBox.calls == [], (
        f"вопрос о уже сохранённой очистке: {FakeMsgBox.calls}"
    )


def test_frame_tab_edits_after_save_are_asked_again(window):
    """Новая операция после 💾 снова делает вкладку грязной."""
    tab = _open_frame_tab(window)
    tab._on_save_only()
    tab.editor.undo_stack.append(tab.editor.image.copy())
    FakeMsgBox.calls = []

    window.workspace._close_active_tab()

    assert [c[0] for c in FakeMsgBox.calls] == ["question"], (
        f"правка после сохранения ушла молча: {FakeMsgBox.calls}"
    )


def test_frame_tab_cancel_keeps_tab_and_edits(window):
    """«Отмена» → вкладка на месте, операции целы, сервер не тронут."""
    tab = _open_frame_tab(window)
    FakeMsgBox.answer = QMessageBox.StandardButton.Cancel

    window.workspace._close_active_tab()

    assert window.workspace._active_tab is tab, "вкладка снесена при отмене"
    assert len(tab.editor.undo_stack) == 1, "правки оператора потеряны"
    assert window.api.saved_images == [], "отмена вызвала сохранение"


def test_frame_tab_failed_save_keeps_tab_open(window):
    """Сервер отказал → вкладка не закрыта, работа не потеряна."""
    _open_frame_tab(window)
    window.api.save_fails = True
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes

    window.workspace._close_active_tab()

    assert window.workspace._active_tab is not None, (
        "вкладка снесена при неудаче сохранения — очистка потеряна"
    )


def test_frame_tab_no_closes_without_saving(window):
    """«Не сохранять» → уход без обращения к серверу, это выбор оператора."""
    _open_frame_tab(window)
    FakeMsgBox.answer = QMessageBox.StandardButton.No

    window.workspace._close_active_tab()

    assert window.api.saved_images == [], "отказ сохранять всё равно сохранил"
    assert window.workspace._active_tab is None, "вкладка не закрылась"


# ── одна дверь: обе двери ведут себя одинаково ───────────────────────────

def test_both_doors_ask_with_their_own_wording(window):
    """Вопрос один, слово о действии — своё у каждого пути."""
    _open_frame_tab(window)
    FakeMsgBox.answer = QMessageBox.StandardButton.Cancel
    window.workspace._close_active_tab()
    back = FakeMsgBox.calls[-1][2]

    FakeMsgBox.calls = []
    window.close()
    exit_ = FakeMsgBox.calls[-1][2]

    assert back == ASK_ON_TAB_CLOSE, f"текст вопроса «← Назад»: {back!r}"
    assert exit_ == ASK_ON_EXIT, f"текст вопроса ✕ окна: {exit_!r}"


def test_tab_without_save_method_is_not_a_dead_end_on_back(window):
    """«Да» у вкладки, которая сохраняться не умеет, не запирает оператора."""
    tab = NoSaveTab()
    _open_stub(window, tab)
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes

    window.workspace._close_active_tab()

    assert window.workspace._active_tab is None, (
        "вкладка без метода сохранения не закрылась — оператор заперт"
    )


def test_tab_without_save_method_is_not_a_dead_end_on_exit(window):
    """То же самое на пути ✕ окна — двери обязаны отвечать одинаково."""
    _open_stub(window, NoSaveTab())
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes

    closed = window.close()

    assert closed is True, "клиент не закрылся из-за вкладки без сохранения"


def test_graph_tab_back_still_asks_and_saves(window):
    """Замок: у вкладки с полным контрактом поведение не поехало."""
    tab = GraphLikeTab()
    _open_stub(window, tab)
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes

    window.workspace._close_active_tab()

    assert [c[0] for c in FakeMsgBox.calls] == ["question"], (
        f"графовая вкладка потеряла вопрос: {FakeMsgBox.calls}"
    )
    assert tab.saves == [True], f"сохранение не вызвано: {tab.saves}"
    assert window.workspace._active_tab is None, "вкладка не закрылась"


# ── автосохранение переживает отказ закрывать ────────────────────────────

def test_cancel_keeps_autosave_alive(window):
    """«Отмена» оставила вкладку — автосохранение обязано остаться с ней."""
    tab = GraphLikeTab()
    _open_stub(window, tab)
    FakeMsgBox.answer = QMessageBox.StandardButton.Cancel

    window.workspace._close_active_tab()

    assert window.workspace._active_tab is tab, "вкладка снесена при отмене"
    assert window.workspace._autosave._timer.isActive(), (
        "отказ закрывать оставил открытую вкладку без автосохранения"
    )


def test_failed_save_keeps_autosave_alive(window):
    """Отказ сервера оставил вкладку — автосохранение обязано остаться с ней."""
    tab = GraphLikeTab()
    tab.save_ok = False
    _open_stub(window, tab)
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes

    window.workspace._close_active_tab()

    assert window.workspace._active_tab is tab, "вкладка снесена при отказе"
    assert window.workspace._autosave._timer.isActive(), (
        "неудача сохранения оставила открытую вкладку без автосохранения"
    )


def test_closed_tab_stops_autosave(window):
    """Контроль с другой стороны: вкладка закрыта — таймер стоит."""
    tab = GraphLikeTab()
    tab.unsaved = False
    _open_stub(window, tab)

    window.workspace._close_active_tab()

    assert window.workspace._active_tab is None, "вкладка не закрылась"
    assert not window.workspace._autosave._timer.isActive(), (
        "автосохранение осталось крутиться на закрытой вкладке"
    )


# ── инвариант, на котором держится «без вопроса» у _force_close_tab ──────

def test_header_back_is_unreachable_while_a_tab_is_open(window):
    """`_force_close_tab` молчит законно ровно потому, что жеста к нему нет.

    Пути «← Назад в список» (`_on_back_to_list` → `cleanup`) и «смена
    диаграммы» (`load_diagram`) сносят вкладку без вопроса. Пока открыта
    вкладка, кнопка возврата в список СКРЫТА (header спрятан), а список
    диаграмм лежит за ней — жестом туда не попасть. Замер §81. Покраснел
    этот тест — значит жест появился, и молчание стало дефектом.
    """
    _open_stub(window, GraphLikeTab())

    assert window.workspace._active_tab is not None, "вкладка не открылась"
    assert not window.workspace.header_panel.isVisible(), (
        "header виден при открытой вкладке — «← Назад в список» стал достижим"
    )
    assert not window.workspace.btn_back_header.isVisible(), (
        "кнопка возврата в список видна при открытой вкладке"
    )


# ── ГРАНИЦА пункта: фоновый диалог автосохранения ───────────────────────
#
# Заявлено отдельно и НЕ лечится здесь. Диалог поднимает не сервис, а сами
# методы сохранения: `QMessageBox.warning` на отказе есть у всех пяти
# (`BaseGraphTab._save_graph`, `ContourTab._save_graph`, `JunctionTab.
# _save_masks`, `PipeTab._save_mask`, `OcrBindingTab._save_binding`), а
# `_save_graph` вдобавок спрашивает о записи вслепую (пункт 1.23). Лечение —
# неинтерактивный режим сохранения у ПЯТИ классов вкладок; два из этих файлов
# в работе у соседних пунктов, и Р-9(г) запрещает их трогать. Два теста ниже
# запирают ФАКТ, а не одобряют его: когда придёт пункт лечения, красное здесь
# скажет, что фоновый тик замолчал.

def test_autosave_tick_reaches_the_tab_save_method(qapp, monkeypatch):
    """Таймер (не жест оператора) доходит до метода сохранения вкладки."""
    saves = []

    class PipeTab(QWidget):
        """Имя класса — ключ карты `AutoSaveService._SAVE_METHODS`."""

        def has_unsaved_changes(self):
            return True

        def _save_mask(self):
            saves.append("tick")
            return False

    monkeypatch.setattr(UISettings, "autosave_enabled",
                        property(lambda self: True))
    tab = PipeTab()
    service = AutoSaveService()
    service.start(tab)
    service._timer.setInterval(10)      # 120 с боевого интервала не ждут
    # ЗАПАС, А НЕ ОЖИДАНИЕ: тик приходит сразу, но только в чистом процессе.
    # Замер 1.x11 (§85и): после тяжёлых UI-файлов набора первое срабатывание
    # свежего 10-мс таймера в этом же процессе занимает 1.4–1.9 с локально
    # (`test_direction_retry_deadend.py` — 1.78 с в одиночку, мой файл — 0.06 с),
    # и на раннере CI прежние 5 с кончались раньше тика: тест краснел не на
    # автосохранении, а на бюджете. Порог поднят до 30 с — зелёный прогон от
    # этого не удлиняется (цикл выходит по первому тику), а красный по-прежнему
    # означает «тик не дошёл».
    deadline = time.monotonic() + 30
    while not saves and time.monotonic() < deadline:
        qapp.processEvents()
    service.stop()
    tab.deleteLater()
    qapp.processEvents()

    assert saves == ["tick"], (
        f"тик автосохранения не дошёл до сохранения вкладки: {saves}"
    )


def test_pipe_save_opens_a_modal_when_the_server_refuses(monkeypatch, tmp_path):
    """Боевое сохранение на отказе поднимает модальный диалог.

    Вызывается настоящая `PipeTab._save_mask` — со стороны сервиса её ничто
    не глушит, значит вместе с тестом выше это «модалка по таймеру».
    """
    monkeypatch.setattr(pt, "QMessageBox", FakeMsgBox)
    FakeMsgBox.calls = []

    def refuse(path):
        raise APIError("сервер отказал")

    fake_tab = types.SimpleNamespace(
        _editor=types.SimpleNamespace(save_mask=refuse),
        temp_dir=tmp_path,
    )

    result = pt.PipeTab._save_mask(fake_tab)

    assert result is False, "отказ сервера выдан за успешное сохранение"
    assert [c[0] for c in FakeMsgBox.calls] == ["warning"], (
        f"боевое сохранение промолчало об отказе: {FakeMsgBox.calls}"
    )

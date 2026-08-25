# -*- coding: utf-8 -*-
"""Блок 5 линии «Ручная правка + FXML», пункт 5.2 — полный Ctrl+Z и вопрос
о несохранённом.

Боль. Оператор правит схему, потом отменяет всё до последнего шага — дерево
байт в байт то же, что он открыл, — а на выходе вкладка всё равно спрашивает
«Есть несохранённые изменения. Сохранить?». Замер этой сессии (пробник на
корпусной схеме `d74eb9f1`, узел `node_11`):

    свежая вкладка : rev 0 / saved 0 / depth 0 / dirty False / bbox [25,215,45,251]
    после протяжки : rev 1 / saved 0 / depth 1 / dirty True  / bbox [490,382,510,418]
    после Ctrl+Z   : rev 2 / saved 0 / depth 0 / dirty True  / bbox [25,215,45,251]
    после Ctrl+Y   : rev 3 / saved 0 / depth 1 / dirty True  / bbox [490,382,510,418]

Третья строка и есть дефект: рамка вернулась в исходную, стек пуст, а флаг
грязный. Причина — `has_unsaved_changes()` сравнивал `undo_mgr.revision`
с `_saved_revision`, а `revision` растёт на КАЖДОЙ мутации, включая undo и redo
(`undo_manager.py:_committed` зовётся из всех четырёх дверей).

Лечение — identity-маркер: на сохранении запоминается сам объект команды
`undo_stack[-1]` (или `None` у пустого стека), сравнение по `is`. Позиция в
стеке (`stack_depth`) для этого не годится и отвергнута самим кодом:
`deque(maxlen=100)` при переполнении застывает, и глубина ложно совпадает
(коммент `undo_manager.py:109-111`). Маркер переполнения не боится — выброшенный
из очереди объект тождественным уже не станет.

⛔ Что этот набор НЕ утверждает: он не делает флаг «честным к превью». Живое
превью панели «Размеры» идёт мимо стека команд, флаг его не видит — и это
несущее решение пункта 1.5 (иначе автосейв повезёт на сервер неподтверждённую
картинку). Здесь сторожится ровно одно: дерево вернулось к точке сохранения.

Проверяется наблюдаемое: геометрия узла, ответ `has_unsaved_changes()` и то,
задала ли ДВЕРЬ ухода (`DiagramWorkspace.confirm_discard_active_tab`, пункт
1.18 — она одна на «← Назад» и ✕ окна) вопрос оператору. Обе полярности
заперты: после полной отмены вопроса нет, при живой правке — есть.

⚠ `QMessageBox` двери подменён с утверждением о ФАКТЕ вызова, а не таймаутом:
модальный диалог в пути инъекции подвешивает набор вместо падения
(`PROTOCOL §5`).
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                    # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal                       # noqa: E402
from PySide6.QtGui import QImage, QColor                         # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox          # noqa: E402

from tools import corpus                                         # noqa: E402

UID = "d74eb9f1"          # 66 узлов / 63 ребра, корпус-фикстура в git
NID = "node_11"           # bbox [25, 215, 45, 251], centroid [233, 35]

BASE_BBOX = [25, 215, 45, 251]
DRAG_TO = (500.0, 400.0)
DRAGGED_BBOX = [490.0, 382.0, 510.0, 418.0]     # рамка 20×36 вокруг (500, 400)
SECOND_DRAG_TO = (700.0, 300.0)
SECOND_BBOX = [690.0, 282.0, 710.0, 318.0]
TOL = 1e-6


# ── харнесс ──────────────────────────────────────────────────────────────

class FakeAPI:
    """Сервер, который помнит только КУДА писали: содержимое здесь не судят."""

    def __init__(self):
        self.uploads = []

    def upload_canvas_graph(self, uid, path):
        self.uploads.append(("graph_canvas", uid))
        return True

    def upload_validated_graph(self, uid, path):
        self.uploads.append(("graph_validated", uid))
        return True


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
    """Подмена модального диалога двери ухода (`PROTOCOL §5`)."""

    StandardButton = QMessageBox.StandardButton
    calls = []
    answer = QMessageBox.StandardButton.No

    @classmethod
    def question(cls, *args, **kwargs):
        cls.calls.append(("question", args[1] if len(args) > 1 else ""))
        return cls.answer

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append(("warning", args[1] if len(args) > 1 else ""))
        return cls.StandardButton.Ok


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def raster(qapp, tmp_path_factory):
    """Белый растр размером с корпусную схему — редактор грузит граф + картинку."""
    path = corpus.graph_path(UID)
    assert path is not None, f"корпус-фикстура {UID} не найдена (tools/corpus.py)"
    h, w = json.loads(path.read_text(encoding="utf-8"))["graph"]["image_size"]
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    png = tmp_path_factory.mktemp("raster") / f"{UID}.png"
    assert img.save(str(png))
    return str(png)


@pytest.fixture
def tab(qapp, raster, monkeypatch):
    """Вкладка «Ручная правка» с корпусной схемой и подставным сервером."""
    from ui.tabs.base_graph_tab import BaseGraphTab
    from ui.tabs.advanced_graph_tab import AdvancedGraphTab
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    monkeypatch.setattr(BaseGraphTab, "_download_artifacts", lambda self: None)

    widget = AdvancedGraphTab(UID, "проба 5.2", FakeAPI())
    editor = AdvancedGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(raster, str(corpus.graph_path(UID)))
    editor.resize(1400, 900)
    widget._editor = editor
    widget._on_editor_ready()
    yield widget
    widget.cleanup()
    widget.deleteLater()


@pytest.fixture
def door(qapp, monkeypatch):
    """Дверь ухода из вкладки — настоящая, с подменённым диалогом.

    Вкладка сажается в `_active_tab` напрямую: `_open_tab` вшивает в её тулбар
    кнопку «← Назад» и останавливает опрос статуса, а дверь читает ровно
    `_active_tab` — лишние виджеты в UI-наборе стоят дорого (замер 1-42).
    """
    from ui.widgets import diagram_workspace as dw

    FakeMsgBox.calls = []
    FakeMsgBox.answer = QMessageBox.StandardButton.No
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)

    workspace = dw.DiagramWorkspace(FakeAPI(), FakeStatusProvider())
    yield workspace
    workspace.deleteLater()


def _drag(editor, nid, x, y):
    """Честная правка через стек команд: протяжка узла в точку (x, y)."""
    editor.start_drag_node(nid)
    editor.drag_node_to(x, y)
    editor.end_drag_node()


def _bbox(editor, nid=NID):
    return list(editor.nodes[nid]["bbox"])


# ── дёрти-флаг: дерево вернулось к точке сохранения ──────────────────────

def test_full_undo_returns_the_tab_to_the_loaded_tree(tab):
    """Отменил всё → дерево то же, что открыл, и несохранённого нет.

    Средним утверждением заперта РАЗНИЦА, а не совпадение: счётчик мутаций
    к этому моменту уже разошёлся с отметкой сохранения, то есть прежнее
    правило по-прежнему сказало бы «грязно», — и именно поэтому ответ
    `False` доказывает лечение, а не пустой стек.
    """
    ed = tab._editor
    assert tab.has_unsaved_changes() is False, "вкладка грязная до правки"

    _drag(ed, NID, *DRAG_TO)
    assert _bbox(ed) == pytest.approx(DRAGGED_BBOX, abs=TOL)
    assert tab.has_unsaved_changes() is True, "правка не подняла дёрти-флаг"

    ed.undo()

    assert _bbox(ed) == pytest.approx(BASE_BBOX, abs=TOL), "Ctrl+Z не вернул узел"
    assert ed.undo_mgr.revision != tab._saved_revision, (
        "счётчик мутаций не вырос — обстановка теста не та, дефект не воспроизведён")
    assert tab.has_unsaved_changes() is False, (
        "полный Ctrl+Z вернул исходное дерево, а вкладка считает его несохранённым")


def test_redo_after_full_undo_is_unsaved_again(tab):
    """Обратная полярность: вернул отменённое — снова есть что сохранять."""
    ed = tab._editor
    _drag(ed, NID, *DRAG_TO)
    ed.undo()
    assert tab.has_unsaved_changes() is False

    ed.redo()

    assert _bbox(ed) == pytest.approx(DRAGGED_BBOX, abs=TOL)
    assert tab.has_unsaved_changes() is True, "redo вернул правку, а флаг чист"


def test_two_edits_undone_one_by_one_reach_the_clean_state(tab):
    """Две правки снимаются по одной: чисто только на ПОСЛЕДНЕЙ отмене."""
    ed = tab._editor
    _drag(ed, NID, *DRAG_TO)
    _drag(ed, NID, *SECOND_DRAG_TO)
    assert _bbox(ed) == pytest.approx(SECOND_BBOX, abs=TOL)

    ed.undo()
    assert _bbox(ed) == pytest.approx(DRAGGED_BBOX, abs=TOL)
    assert tab.has_unsaved_changes() is True, "одна правка ещё на месте, а флаг чист"

    ed.undo()

    assert _bbox(ed) == pytest.approx(BASE_BBOX, abs=TOL)
    assert tab.has_unsaved_changes() is False


def test_undo_past_the_save_point_is_unsaved(tab):
    """Точка отсчёта — СОХРАНЕНИЕ, а не открытие вкладки.

    Прогон идёт по предыстории, а не с чистого листа: сначала оператор
    сохранился, и только потом жмёт Ctrl+Z. Отмена сохранённой правки — это
    расхождение с сервером, о нём спросить обязаны; возврат redo-ем на точку
    сохранения расхождение снимает.
    """
    ed = tab._editor
    _drag(ed, NID, *DRAG_TO)
    assert tab._save_graph() is True, "сохранение не прошло — судить нечем"
    assert tab.api_client.uploads == [("graph_canvas", UID)]
    assert tab.has_unsaved_changes() is False

    ed.undo()

    assert _bbox(ed) == pytest.approx(BASE_BBOX, abs=TOL)
    assert tab.has_unsaved_changes() is True, (
        "отменена СОХРАНЁННАЯ правка — на сервере лежит другое, а вкладка чиста")

    ed.redo()

    assert _bbox(ed) == pytest.approx(DRAGGED_BBOX, abs=TOL)
    assert tab.has_unsaved_changes() is False, (
        "дерево вернулось к сохранённому, а вкладка всё ещё несохранённая")


# ── дверь ухода: что видит оператор ──────────────────────────────────────

def test_exit_after_full_undo_asks_nothing(tab, door):
    """Шов вкладки и двери: отменил всё → вышел без единого вопроса."""
    ed = tab._editor
    _drag(ed, NID, *DRAG_TO)
    ed.undo()

    door._active_tab = tab

    assert door.confirm_discard_active_tab() is True
    assert FakeMsgBox.calls == [], (
        f"вопрос о несохранённом на нетронутом дереве: {FakeMsgBox.calls}")


def test_exit_with_a_live_edit_still_asks(tab, door):
    """Замок с другой стороны: правка на месте → вопрос задан."""
    ed = tab._editor
    _drag(ed, NID, *DRAG_TO)

    door._active_tab = tab

    assert door.confirm_discard_active_tab() is True     # ответ «Нет» — уходим
    assert [c[0] for c in FakeMsgBox.calls] == ["question"], (
        f"уход с живой правкой прошёл мимо вопроса: {FakeMsgBox.calls}")

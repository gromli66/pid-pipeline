# -*- coding: utf-8 -*-
"""Пункт 1.5 дороги (Р13) — дёрти-флаг и то, что уходит на сервер.

Зачем. Дёрти-флаг вкладки — `undo_mgr.revision != _saved_revision`
(`base_graph_tab.py:1103-1106`). Он считает только мутации, прошедшие через
стек команд. Живое превью панели «Размеры» мутирует МОДЕЛЬ мимо стека, поэтому
флаг его не видит — и это правильно: превью без «Применить» оператор ещё не
подтвердил, оно не «несохранённое изменение», а промежуточная картинка. Сделать
флаг грязным от превью значило бы поручить автосейву отправлять именно его —
ровно та ошибка, ради которой 1.5 стоит ПОСЛЕ 1.4, а не до.

Дыра была в другом месте: `_save_graph` сериализовал модель КАК ЕСТЬ, то есть
вместе с живым превью. Пункт 1.4 научил редактор откатывать брошенное превью
при выходе из режима — и тем развёл модель с сервером. Замер §50 (до правки):

  • «превью → Сохранить → выход»: на сервере `node_11` = [-10.0, 188.0, 80.0,
    278.0], в модели после выхода — исходные [25, 215, 45, 251];
  • «протяжка → превью → тик автосейва → выход»: на сервере [455.0, 355.0,
    545.0, 445.0] (протяжка + превью), в модели [490.0, 382.0, 510.0, 418.0]
    (одна протяжка). Автосейв увёз именно брошенную порчу.

Инвариант пункта: **на сервер уходит ровно то, что считает дёрти-флаг** —
зафиксированное состояние. Незафиксированное превью снимается перед записью,
любым из четырёх путей сохранения (кнопка, «Подтвердить», диалог закрытия,
автосейв — все они зовут `_save_graph`).

Что проверяется — только ДАННЫЕ (принцип набора 0.4): геометрия узлов в JSON,
который вкладка отдала API-клиенту, и значение дёрти-флага. Ни одного
утверждения про `_current_mode` и внутреннюю кухню редактора.

Числа абсолютные и заперты с двух сторон: и «пришло исходное», и «не пришло
превью» — иначе тест остался бы зелёным на пустом графе.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication              # noqa: E402
from PySide6.QtGui import QImage, QColor                # noqa: E402

from tools import corpus                                # noqa: E402

UID = "d74eb9f1"          # 66 узлов / 63 ребра, корпус-фикстура в git
CLASS = "armatura_ruchn"  # 17 экземпляров, все — боксы
NID = "node_11"           # bbox [25, 215, 45, 251] = 20×36, centroid [233, 35]

BASE_BBOX = [25, 215, 45, 251]
SIDE = 90                                 # цель превью: квадрат 90×90
# 90×90 вокруг центроида [233, 35] — то, что видит оператор до «Применить»
PREVIEW_BBOX = [-10.0, 188.0, 80.0, 278.0]
DRAG_TO = (500.0, 400.0)                             # куда тянем узел командой
DRAGGED_BBOX = [490.0, 382.0, 510.0, 418.0]          # рамка 20×36 вокруг (500, 400)
DRAGGED_PREVIEW_BBOX = [455.0, 355.0, 545.0, 445.0]  # она же после превью 90×90
TOL = 1e-6


# ── харнесс ──────────────────────────────────────────────────────────────

class FakeAPI:
    """Сервер: помнит РАЗОБРАННОЕ содержимое каждой заливки графа.

    Файл читается сразу: временный каталог вкладки живёт до её `cleanup()`,
    а сверяться с содержимым надо после.
    """

    def __init__(self):
        self.uploads = []

    def _remember(self, kind, uid, path):
        with open(path, encoding="utf-8") as fh:
            self.uploads.append((kind, uid, json.load(fh)))
        return True

    def upload_canvas_graph(self, uid, path):
        return self._remember("canvas", uid, path)

    def upload_validated_graph(self, uid, path):
        return self._remember("validated", uid, path)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def raster(tmp_path_factory):
    """Белый растр размером с корпусную схему (редактор грузит граф + картинку)."""
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
    """Вкладка «Ручная правка» с корпусной схемой и подставным сервером.

    Скачивание артефактов снято: сети в тесте нет, а редактор собирается из той
    же фикстуры напрямую. Всё остальное — как в бою: редактор в холсте, колбэки
    панели «Размеры» подключены тем же `_on_editor_ready`, которым их подключает
    вкладка после загрузки. Без них панель не участвовала бы в снятии превью, и
    обстановка теста разошлась бы с боевой (PROTOCOL §5).
    """
    from ui.tabs.base_graph_tab import BaseGraphTab
    from ui.tabs.advanced_graph_tab import AdvancedGraphTab
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    monkeypatch.setattr(BaseGraphTab, "_download_artifacts", lambda self: None)

    widget = AdvancedGraphTab(UID, "проба 1.5", FakeAPI())
    editor = AdvancedGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(raster, str(corpus.graph_path(UID)))
    editor.resize(1400, 900)
    widget._editor = editor
    widget._on_editor_ready()
    yield widget
    widget.cleanup()


@pytest.fixture
def armed(tab):
    """Вкладка + режим «Размер объектов» на классе CLASS."""
    ed = tab._editor
    ed.set_mode("resize_objects")
    ed.set_resize_class(CLASS)
    assert ed._resize_kind() == "box", "набор должен быть чисто боксовым"
    assert list(ed.nodes[NID]["bbox"]) == BASE_BBOX, "фикстура сдвинулась"
    return tab


def _sent_bbox(tab, nid=NID):
    """bbox узла в ПОСЛЕДНЕМ графе, который вкладка отдала серверу."""
    uploads = tab.api_client.uploads
    assert uploads, "на сервер не ушло ничего — сверять нечего"
    for node in uploads[-1][2]["nodes"]:
        if node.get("id") == nid:
            return node.get("bbox")
    raise AssertionError(f"{nid} нет в отправленном графе")


def _drag(editor, nid, x, y):
    """Честная правка через стек команд: протяжка узла в точку (x, y)."""
    editor.start_drag_node(nid)
    editor.drag_node_to(x, y)
    editor.end_drag_node()


def _autosave_tick(tab):
    """Тик автосейва по живой вкладке — тем же кодом, что в бою."""
    from ui.services.autosave import AutoSaveService

    service = AutoSaveService()
    service._tab = tab
    service._on_tick()


# ── дёрти-флаг: что он обязан считать и чего не обязан ───────────────────

def test_live_preview_does_not_make_tab_dirty(armed):
    """Превью без «Применить» флаг НЕ поднимает.

    Обратное сделало бы автосейв отправителем брошенного превью — ровно та
    ошибка, ради которой 1.5 стоит после 1.4, а не до неё.
    """
    ed = armed._editor
    assert armed.has_unsaved_changes() is False

    ed.preview_resize(width=SIDE, height=SIDE)

    assert ed.nodes[NID]["bbox"] == pytest.approx(PREVIEW_BBOX, abs=TOL)
    assert armed.has_unsaved_changes() is False


def test_committed_change_makes_tab_dirty_until_save(armed):
    """Правка через команду флаг поднимает, успешное сохранение — снимает."""
    ed = armed._editor
    _drag(ed, NID, *DRAG_TO)

    assert ed.nodes[NID]["bbox"] == pytest.approx(DRAGGED_BBOX, abs=TOL)
    assert armed.has_unsaved_changes() is True

    assert armed._save_graph() is True

    assert armed.has_unsaved_changes() is False


# ── на сервер уходит только зафиксированное ──────────────────────────────

def test_save_during_live_preview_uploads_original(armed):
    """«Сохранить» посреди живого превью отдаёт серверу ИСХОДНУЮ геометрию.

    Было (замер §50): на сервере [-10.0, 188.0, 80.0, 278.0], а в модели после
    выхода из режима — [25, 215, 45, 251]. Сервер расходился с моделью.
    """
    ed = armed._editor
    ed.preview_resize(width=SIDE, height=SIDE)

    assert armed._save_graph() is True

    sent = _sent_bbox(armed)
    assert sent == pytest.approx(BASE_BBOX, abs=TOL)
    assert sent != pytest.approx(PREVIEW_BBOX, abs=TOL)


def test_autosave_does_not_carry_abandoned_preview(armed):
    """Автосейв увозит зафиксированную протяжку и НЕ увозит живое превью.

    Замер §50: на сервере было [455.0, 355.0, 545.0, 445.0] — протяжка вместе
    с превью, при том что в модели после выхода оставалось [490.0, 382.0,
    510.0, 418.0]. Это и есть «автосейв заливает брошенную порчу».
    """
    ed = armed._editor
    _drag(ed, NID, *DRAG_TO)
    ed.preview_resize(width=SIDE, height=SIDE)
    assert ed.nodes[NID]["bbox"] == pytest.approx(DRAGGED_PREVIEW_BBOX, abs=TOL)

    _autosave_tick(armed)

    sent = _sent_bbox(armed)
    assert sent == pytest.approx(DRAGGED_BBOX, abs=TOL)
    assert sent != pytest.approx(DRAGGED_PREVIEW_BBOX, abs=TOL)


def test_confirm_during_live_preview_uploads_original(armed, monkeypatch):
    """«Подтвердить» — тот же путь записи, та же гарантия.

    Вопрос о несохранённом здесь не всплывает (флаг чист), но кнопка сохраняет
    безусловно — и обязана сохранить зафиксированное.

    Диалог подменяется не для удобства: `_on_confirm` открывает МОДАЛЬНЫЙ
    `QMessageBox.question` при грязном флаге, и регрессия «превью поднимает
    флаг» подвесила бы весь набор вместо того, чтобы его уронить (поймано
    инъекцией И4). Подмена превращает зависание в внятное красное.
    """
    from PySide6.QtWidgets import QMessageBox

    asked = []
    from ui.tabs.blind_overwrite import BlindOverwriteGuard
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **kw: asked.append(a) or QMessageBox.StandardButton.Yes)
    # С пункта 5.3 «Подтвердить» спрашивает своими русскими кнопками.
    monkeypatch.setattr(
        BlindOverwriteGuard, "_ask_yes_cancel",
        lambda self, title, text: asked.append((self, title, text)) or True)

    ed = armed._editor
    ed.preview_resize(width=SIDE, height=SIDE)

    armed._on_confirm()

    assert asked == [], "превью не изменение — вопроса о несохранённом быть не должно"
    sent = _sent_bbox(armed)
    assert sent == pytest.approx(BASE_BBOX, abs=TOL)
    assert sent != pytest.approx(PREVIEW_BBOX, abs=TOL)


def test_open_spoil_close_without_saving_leaves_server_untouched(armed):
    """Гейт пункта: открыть — испортить — закрыть без сохранения.

    Закрытие вкладки (`diagram_workspace._close_active_tab`) спрашивает про
    сохранение только при грязном флаге; превью его не поднимает, значит
    диалога нет и на сервер не уходит ничего вовсе.
    """
    ed = armed._editor
    ed.preview_resize(width=SIDE, height=SIDE)

    assert armed.has_unsaved_changes() is False   # диалога не будет
    ed.set_mode("idle")                           # выход из режима = закрытие

    assert armed.api_client.uploads == []
    assert ed.nodes[NID]["bbox"] == pytest.approx(BASE_BBOX, abs=TOL)


# ── характеризация: снятие превью не крадёт подтверждённое ───────────────

def test_applied_resize_reaches_server(armed):
    """«Применить» → «Сохранить»: новый размер на сервере.

    Запирает правку с другой стороны: снимать надо незафиксированное превью,
    а не результат «Применить».
    """
    ed = armed._editor
    ed.preview_resize(width=SIDE, height=SIDE)
    ed.apply_resize(width=SIDE, height=SIDE)

    assert armed._save_graph() is True

    sent = _sent_bbox(armed)
    assert sent[2] - sent[0] == pytest.approx(SIDE, abs=TOL)
    assert sent[3] - sent[1] == pytest.approx(SIDE, abs=TOL)


def test_save_without_any_preview_uploads_model_as_is(armed):
    """Без режима «Размеры» путь записи не изменился: что в модели, то и ушло."""
    ed = armed._editor
    ed.set_mode("idle")
    _drag(ed, NID, *DRAG_TO)

    assert armed._save_graph() is True

    assert _sent_bbox(armed) == pytest.approx(DRAGGED_BBOX, abs=TOL)


def test_dropping_preview_leaves_undo_stack_alone(armed):
    """Снятие превью при сохранении не трогает стек отмены оператора."""
    ed = armed._editor
    _drag(ed, NID, *DRAG_TO)
    depth = len(ed.undo_mgr.undo_stack)
    ed.preview_resize(width=SIDE, height=SIDE)

    assert armed._save_graph() is True

    assert len(ed.undo_mgr.undo_stack) == depth
    ed.undo()
    assert ed.nodes[NID]["bbox"] == pytest.approx(BASE_BBOX, abs=TOL)

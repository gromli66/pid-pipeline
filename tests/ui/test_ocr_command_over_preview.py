# -*- coding: utf-8 -*-
"""Пункт 1.x8 дороги, носитель 1 — OCR-команда поверх живого превью «Размеров».

Продолжение 1.4 (`test_preview_not_welded_into_command.py`) в СОСЕДНЕЙ подсистеме.
Доработка 1.4 закрыла четыре команды-на-месте в самом `advanced_graph_editor`, но
`OcrLayerMixin` строит свои точки возврата СВОИМ помощником — `_ocr_push_snapshot`
(`ocr_layer_mixin.py:524`), и в него дроп превью не приходил:

    SnapshotCommand(self.model, self._redraw_all)   # `_before` = модель КАК ЕСТЬ

То есть ровно та же болезнь: `_before` снимается вместе с превью, дальше
`undo_mgr.revision` растёт, `_resize_baseline_alive()` объявляет базлайн мёртвым —
и лечение 1.4 «протух → бросить без отката» превью уже не трогает. Ctrl+Z
возвращает превью, «Сохранить» увозит его на сервер (замер §73б).

**Первый вопрос пункта был про достижимость, и ответ ЗАМЕРЕН: достижимо.**
«Состояние» (`display_regime`) и «инструмент» (`_current_mode`) — независимые
оси: `set_display_regime` сбрасывает в idle только инструменты своего состояния
(`advanced_graph_editor.py:446-449`), а `resize_objects` в этом списке НЕТ, и
кнопка «Размеры» лежит в общем ряду «всегда видимых». Поэтому «ОКР привязка» +
«Размеры» включаются одновременно, панель ОКР остаётся на экране, выделение
блоков переживает смену инструмента, а кнопка «Распознать добавленные» при
живом превью доступна (замер §73а — прогон ЧЕРЕЗ КНОПКИ ТУЛБАРА, не через API).

Пути, у которых сторож — только `display_regime`, поэтому «Размеры» их не
перехватывают: Delete (`:4778`), Ctrl+V→Ctrl+ЛКМ (`:4790`, призрак вставки
обрабатывается в `mousePressEvent` ДО диспетчера режимов), Ctrl+2ЛКМ по блоку
и по оборудованию (`mouseDoubleClickEvent` `:5613` — проверки режима там нет
вовсе) и асинхронный приход результата OCR (`_on_recognize_done` — сторожа нет
в принципе: кнопка «Размеры» на время потока НЕ гасится).

Инвариант пункта — тот же, что у 1.4: **Ctrl+Z после любой OCR-команды, отданной
при живом превью, возвращает схему к состоянию ДО превью.** Плюс обратная
граница 1.4 (И5): **команда, точки возврата НЕ построившая, превью не снимает** —
картинку у оператора не отбирают.

Проверяются ДАННЫЕ: геометрия узлов, JSON серверу, содержимое модели. Ни одного
утверждения про `_current_mode` (правило гейта [ui]) — режим только выставляется,
и выставляется кликом по настоящей кнопке. Числа абсолютные и заперты с двух
сторон: и «рамка вернулась к 20×36», и «это не 90×90 превью».

⚠ Обстановка: модальные диалоги подменены с утверждением о ФАКТЕ вызова.
Без подмены `QDialog.exec()` в правке KKS и `QInputDialog.getText` в правке
текста подвешивают набор вместо падения — четвёртый исход `PROTOCOL §5`,
на этой дороге замеренный трижды (1.5 → 1.3 → 1.4).
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                     # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QEvent                             # noqa: E402
from PySide6.QtGui import QImage, QColor, QKeyEvent               # noqa: E402
from PySide6.QtWidgets import (                                   # noqa: E402
    QApplication, QDialog, QInputDialog, QLineEdit, QMessageBox,
)

from tools import corpus                                          # noqa: E402

UID = "d74eb9f1"          # 66 узлов / 63 ребра, корпус-фикстура в git
CLASS = "armatura_ruchn"  # 17 экземпляров, все — боксы
NID = "node_11"           # bbox [25, 215, 45, 251] = 20×36
KKS_NID = "node_11"       # тот же узел: правка KKS идёт по оборудованию

BASE_BBOX = [25, 215, 45, 251]
BASE_W, BASE_H = 20, 36                   # исходный размер рамки
SIDE = 90                                 # цель превью: квадрат 90×90
PREVIEW_BBOX = [-10.0, 188.0, 80.0, 278.0]
TOL = 1e-6

#: текст-блок вне рамок оборудования — его двигают OCR-команды
BLOCK_BBOX = [800.0, 400.0, 900.0, 440.0]
BLOCK_TEXT = "AA"

#: текст-блоков в самой корпус-фикстуре (подопытный добавляется сверх них)
CORPUS_BLOCKS = 48


# ── харнесс ──────────────────────────────────────────────────────────────

class FakeAPI:
    """Сервер: помнит РАЗОБРАННОЕ содержимое каждой заливки графа."""

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
def dialogs(monkeypatch):
    """Все модальные диалоги → журнал фактов вызова.

    Зелёный путь молчит, а КРАСНЫЙ без подмены вешает набор: `QDialog.exec()`
    правки KKS и `QInputDialog.getText` правки текста модальны. Висящий набор
    на CI читается как «долго», а не как «сломано» (`PROTOCOL §5`).
    """
    calls = []
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **kw: calls.append(("warning", a[1:3]))))
    monkeypatch.setattr(QMessageBox, "critical",
                        staticmethod(lambda *a, **kw: calls.append(("critical", a[1:3]))))
    return calls


@pytest.fixture
def tab(qapp, raster, monkeypatch, dialogs):
    """Вкладка «Ручная правка» в состоянии «ОКР привязка» + инструменте «Размеры».

    Обстановка набирается ТЕМИ ЖЕ кликами, что у оператора: кнопка состояния,
    Shift по блоку, кнопка инструмента. Превью не поднимается — его поднимает
    `_preview()` в самом тесте, чтобы «до превью» тоже было наблюдаемо.
    """
    from ui.tabs.base_graph_tab import BaseGraphTab
    from ui.tabs.advanced_graph_tab import AdvancedGraphTab
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    monkeypatch.setattr(BaseGraphTab, "_download_artifacts", lambda self: None)

    t = AdvancedGraphTab(UID, "проба 1.x8", FakeAPI())
    editor = AdvancedGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(raster, str(corpus.graph_path(UID)))
    editor.resize(1400, 900)
    t._editor = editor
    t._on_editor_ready()

    blk = editor.model.create_text_block(BLOCK_BBOX, text=BLOCK_TEXT, source="manual")
    editor.model.add_text_block(blk)
    t._block_id = blk["id"]

    t.btn_regime_ocr.click()                      # состояние «ОКР привязка»
    editor._ocr_shift_press(850.0, 420.0)         # Shift+ЛКМ по блоку — выделить
    assert editor._selected_ocr == {blk["id"]}, "блок не выделился — тест бессмыслен"
    t.btn_resize_objects.click()                  # инструмент «Размеры»
    editor.set_resize_class(CLASS)

    assert editor._resize_kind() == "box", "набор должен быть чисто боксовым"
    assert list(editor.nodes[NID]["bbox"]) == BASE_BBOX, "фикстура сдвинулась"
    assert editor._selected_ocr == {blk["id"]}, \
        "выделение блоков не пережило вход в «Размеры» — сценарий недостижим"
    yield t
    t.cleanup()


def _geom(editor):
    """Снимок геометрии всех узлов — база сравнений «данные вернулись»."""
    return json.dumps({nid: [nd.get("bbox"), nd.get("centroid"),
                             nd.get("segmentation"), nd.get("area")]
                       for nid, nd in editor.nodes.items()}, sort_keys=True)


def _size(editor, nid=NID):
    """(ширина, высота) рамки узла — то, что оператор видит на схеме."""
    bb = editor.nodes[nid]["bbox"]
    return (bb[2] - bb[0], bb[3] - bb[1])


def _sent_bbox(t, nid=NID):
    """bbox узла в ПОСЛЕДНЕМ графе, который вкладка отдала серверу."""
    uploads = t.api_client.uploads
    assert uploads, "на сервер не ушло ничего — сверять нечего"
    for node in uploads[-1][2]["nodes"]:
        if node.get("id") == nid:
            return node.get("bbox")
    raise AssertionError(f"{nid} нет в отправленном графе")


def _preview(t):
    """Живое превью 90×90 на наборе — то, что оператор видит до «Применить»."""
    ed = t._editor
    ed.preview_resize(width=SIDE, height=SIDE)
    assert ed.nodes[NID]["bbox"] == pytest.approx(PREVIEW_BBOX, abs=TOL), \
        "превью ничего не изменило — тест бессмыслен"


def _press(editor, key, mods=Qt.KeyboardModifier.NoModifier):
    """Настоящее событие клавиши в редактор — не вызов метода напрямую."""
    editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods))


def _blocks(editor):
    """Активные (не слитые) текст-блоки модели.

    Корпус-фикстура несёт 48 своих блоков — счёт всегда ведётся от них, а
    подопытный блок ищется по id (`tab._block_id`), а не по позиции в списке.
    """
    return [b for b in editor.model.text_blocks if b.get("merged_into") is None]


def _block_ids(editor):
    return {b["id"] for b in _blocks(editor)}


def _returns_to_state_before_preview(t, act):
    """Общая форма проверки: превью → команда → Ctrl+Z → всё как до превью."""
    ed = t._editor
    geom0 = _geom(ed)

    _preview(t)
    act(t, ed)

    assert _size(ed) == pytest.approx((BASE_W, BASE_H), abs=TOL)
    assert _size(ed) != pytest.approx((SIDE, SIDE), abs=TOL)

    ed.undo()

    assert _geom(ed) == geom0
    assert _size(ed) == pytest.approx((BASE_W, BASE_H), abs=TOL)


# ── шов: OCR-команда поверх живого превью ────────────────────────────────

def test_delete_selected_blocks_over_preview_undo_returns_to_state_before_preview(
        tab, dialogs):
    """Delete по выделенным блокам: Ctrl+Z возвращает схему к состоянию до превью."""
    _returns_to_state_before_preview(
        tab, lambda t, ed: _press(ed, Qt.Key.Key_Delete))
    assert dialogs == [], "путь ушёл в диалог — судить нечем"


def test_delete_selected_blocks_over_preview_does_not_upload_it(tab, dialogs):
    """…и «Сохранить» после неё увозит на сервер исходную рамку, а не превью."""
    ed = tab._editor
    _preview(tab)
    _press(ed, Qt.Key.Key_Delete)
    tab._save_graph()

    assert _sent_bbox(tab) == pytest.approx(BASE_BBOX, abs=TOL)
    assert _sent_bbox(tab) != pytest.approx(PREVIEW_BBOX, abs=TOL)
    assert dialogs == [], "путь ушёл в диалог — судить нечем"


def test_delete_selected_blocks_over_preview_still_deletes_the_blocks(tab):
    """Лекарство не отменяет саму команду: блоки удалены, Ctrl+Z их возвращает."""
    ed = tab._editor
    ids0 = _block_ids(ed)
    assert len(ids0) == CORPUS_BLOCKS + 1, "фикстура сдвинулась по числу блоков"

    _preview(tab)
    _press(ed, Qt.Key.Key_Delete)

    assert _block_ids(ed) == ids0 - {tab._block_id}, "команда оператора не отработала"
    assert len(_block_ids(ed)) == CORPUS_BLOCKS

    ed.undo()

    assert _block_ids(ed) == ids0


def test_paste_blocks_over_preview_undo_returns_to_state_before_preview(tab, dialogs):
    """Ctrl+V → Ctrl+ЛКМ (фиксация призрака вставки) поверх превью."""
    def _act(t, ed):
        ed._ocr_clipboard = [(list(BLOCK_BBOX), BLOCK_TEXT)]
        n0 = len(_blocks(ed))
        ed._ocr_paste_blocks(30.0, 30.0)
        assert len(_blocks(ed)) == n0 + 1, "вставка не отработала — тест бессмыслен"

    _returns_to_state_before_preview(tab, _act)
    assert dialogs == [], "путь ушёл в диалог — судить нечем"


def test_edit_block_text_over_preview_undo_returns_to_state_before_preview(
        tab, dialogs, monkeypatch):
    """Ctrl+2ЛКМ по блоку → правка текста поверх превью."""
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **kw: ("НОВЫЙ", True)))

    def _act(t, ed):
        ed.edit_ocr_block_text(t._block_id)
        assert ed.model.find_text_block(t._block_id)["text"] == "НОВЫЙ",             "правка не отработала"

    _returns_to_state_before_preview(tab, _act)
    assert dialogs == [], "путь ушёл в диалог — судить нечем"


def test_manual_kks_over_preview_undo_returns_to_state_before_preview(
        tab, dialogs, monkeypatch):
    """Ctrl+2ЛКМ по оборудованию → ручной KKS поверх превью.

    Диалог подменён так, как его закрывает оператор: поле заполнено, нажат OK.
    """
    def _exec(self):
        for edit in self.findChildren(QLineEdit):
            edit.setText("10LAB10AA001")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", _exec)

    def _act(t, ed):
        ed._open_kks_edit_dialog(KKS_NID)
        assert ed._node_kks(KKS_NID) == "10LAB10AA001", "KKS не записан"

    _returns_to_state_before_preview(tab, _act)
    assert dialogs == [], "путь ушёл в диалог — судить нечем"


def test_async_recognize_result_over_preview_undo_returns_to_state_before_preview(
        tab, dialogs):
    """Асинхронный приход результата OCR поверх превью — путь §56.20 из 1.4.

    Сторожа у него нет в принципе: пока поток распознавания жив, гасятся только
    «Распознать», «Сохранить» и «Подтвердить», а «Размеры» остаются доступны
    (замер §73а). Значит оператор успевает поднять превью до прихода ответа.
    """
    def _act(t, ed):
        blk = ed.model.find_text_block(t._block_id)
        blk["text"] = ""            # пустой блок = кандидат на распознавание
        n = ed.apply_ocr_results([blk["id"]],
                                 [{"text": "XYZ", "confidence": 0.9}])
        assert n == 1, "результат не приземлился — тест бессмыслен"

    _returns_to_state_before_preview(tab, _act)
    assert dialogs == [], "путь ушёл в диалог — судить нечем"


# ── обратная граница (И5 из 1.4): у оператора не отбирают картинку ───────

def test_ocr_command_that_pushes_nothing_keeps_the_preview_alive(tab, dialogs):
    """Удаление блоков БЕЗ выделения точки возврата не строит — превью живёт.

    Зеркало `test_command_that_pushes_nothing_keeps_the_preview_alive` из 1.4:
    превью снимается там, где команда СТРОИТСЯ, а не там, где нажали клавишу.

    ⚠ Вход здесь ПРЯМОЙ, а не клавишей, и это измеренная необходимость, а не
    удобство: `Key_Delete` при пустом `_selected_ocr` до OCR-слоя не доходит
    вовсе — условие `keyPressEvent` (`:4778`) ложно, и жест проваливается
    в `batch_delete` редактора, чью границу уже стережёт 1.4. Инъекция
    в OCR-слой при входе клавишей осталась бы зелёной (замер §73ж, зонд И2):
    тест сдавал бы экзамен за соседний механизм.
    """
    ed = tab._editor
    ed._selected_ocr.clear()
    _preview(tab)
    depth0 = len(ed.undo_mgr.undo_stack)

    ed._delete_selected_ocr_blocks()

    assert len(ed.undo_mgr.undo_stack) == depth0, \
        "команда всё-таки построилась — граница проверяется не тем действием"
    assert ed.nodes[NID]["bbox"] == pytest.approx(PREVIEW_BBOX, abs=TOL), \
        "превью снято командой, которой не было — картинку отобрали"
    assert dialogs == [], "путь ушёл в диалог — судить нечем"


def test_delete_without_ocr_selection_falls_through_to_batch_delete(tab, dialogs):
    """Характеризация того самого провала жеста — чтобы он был назван, а не подразумевался.

    Delete при пустом выделении блоков в состоянии «ОКР привязка» уходит
    не в OCR-слой, а в `batch_delete` редактора. В режиме «Размеры» узлы
    не выделяются, поэтому команда не строится и превью остаётся живым —
    это граница 1.4 (`batch_delete` без выделения), а не 1.x8.
    """
    ed = tab._editor
    ed._selected_ocr.clear()
    ids0 = _block_ids(ed)
    _preview(tab)
    depth0 = len(ed.undo_mgr.undo_stack)

    _press(ed, Qt.Key.Key_Delete)

    assert _block_ids(ed) == ids0, "блоки удалять было нечем — жест ушёл не туда"
    assert len(ed.undo_mgr.undo_stack) == depth0
    assert ed.nodes[NID]["bbox"] == pytest.approx(PREVIEW_BBOX, abs=TOL)
    assert dialogs == [], "путь ушёл в диалог — судить нечем"


def test_unchanged_block_text_keeps_the_preview_alive(tab, dialogs, monkeypatch):
    """Правка текста, ничего не изменившая, тоже не строит точки возврата."""
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **kw: (BLOCK_TEXT, True)))
    ed = tab._editor
    _preview(tab)
    depth0 = len(ed.undo_mgr.undo_stack)

    ed.edit_ocr_block_text(tab._block_id)

    assert len(ed.undo_mgr.undo_stack) == depth0
    assert ed.nodes[NID]["bbox"] == pytest.approx(PREVIEW_BBOX, abs=TOL), \
        "превью снято командой, которой не было — картинку отобрали"
    assert dialogs == [], "путь ушёл в диалог — судить нечем"

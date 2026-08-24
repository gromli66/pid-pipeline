# -*- coding: utf-8 -*-
"""Пункт 1-40 дороги (1.x15) — девять сценариев ОТКРЫТОГО состояния редактора.

Что общего у всех девяти. У «Ручной правки» есть состояния, которые живут
ДОЛЬШЕ одного жеста: открытый снимок ручек OCR-блока, набор панели «Размеры»
с его жёлтыми рамками, живое превью размеров, точка возврата «Применить».
Каждое из них построено в момент T0 и потребляется в момент T1, а между T0 и T1
успевает случиться либо чужое действие оператора, либо фоновый тик таймера.
Проверяется ровно то, что видит оператор в ДАННЫХ: геометрия узлов, пины рёбер,
текст блоков, JSON, ушедший серверу, глубина стека. Ни одного утверждения про
`_current_mode` (принцип набора 0.4).

Происхождение сценариев. Четыре — находки red-team, НЕ вошедшие в возврат 1-6
(`MEASUREMENTS §83.30–83.33`); ревизор их честно не перемерял. Четыре — адреса
четвёртой ревизии 1-6 (§103.18 и три кандидата red-team), тоже без замера. Два
уточнения дала пятая ревизия (§110.25 «лотерея» на деле ЖЕСТ, §110.26 пины
полигонов). Поэтому пункт НАЧАЛСЯ с воспроизведения: числа «до» — в
`MEASUREMENTS §121`, и каждый тест ниже краснел на нетронутом дереве.

  S1 (§83.33)  `_redraw_all` сносит жёлтые рамки набора: они рисуются на
               `zValue 9`, а фильтр перерисовки сносит всё `> 0` и обратно их
               не строит. Набор при этом ЖИВ. Отсюда и дожили остальные:
               оператор не видит, что инструмент «Размеры» ещё держит 17 узлов.
  S2 (§83.30)  Снимок ручек OCR-блока снимался при ПОКАЗЕ ручек и переживал
               смену инструмента: чужая подтверждённая команда попадала внутрь
               его `_before`, и один Ctrl+Z откатывал ЕЁ, а не размер блока.
  S3 (§83.31)  Правка диаметра ребра писала мимо стека: `revision` не рос,
               `has_unsaved_changes()` её не видел, первый же Ctrl+Z по соседней
               команде стирал её без следа. Соседний путь того же жеста (KKS
               по Ctrl+2ЛКМ) точку возврата строит — асимметрия, а не замысел.
  S4 (§83.32)  Тик автосохранения снимал живое превью БЕЗ ЖЕСТА оператора
               (родня 1-38: фоновый тик вмешивается в работу). Ручное
               сохранение обязано снимать превью по-прежнему (инвариант 1.5) —
               здесь заперты ОБЕ полярности.
  S5 (§103.18) Точка возврата «Применить» — снимок ВСЕЙ модели, а сторож судит
               по 4 полям узлов НАБОРА. Чужой откат ВНЕ отпечатков вваривался
               в `cmd._before`, и отмена до дна ВОСКРЕШАЛА отменённый перенос
               при стеке 0 — вернуть нечем.
  S6           Та же точка возврата и УДАЛЕНИЕ: узел, воскрешённый Ctrl+Z между
               превью и «Применить», откат «Применить» удалял снова.
  S7 (§110.25) Redo углового ресайза ТОЙ ЖЕ ширины: отпечаток пина совпадает
               СТРУКТУРНО (обе стороны считают его одной формулой от одной
               базы), и откат превью писал базу поверх redo-рамки. Это ЖЕСТ,
               воспроизводимый действием оператора, а не совпадение битов.
  S8           Поля, созданные превью «из ничего», rollback не снимает
               (гарды `base[...] is not None`). ⛔ ВЕРДИКТ — ОБСТАНОВКА, НЕ
               ДЕФЕКТ: предпосылка недостижима, и здесь заперта именно она.
  S9 (§110.26) Пины полигонов: на боксах превью идемпотентно, потому что
               `_apply_sizes_from_base` возвращает боксу базовый `bbox`; у
               полигонов не возвращало, и `rescale_edge_pins` считал масштаб
               от ПРЕДЫДУЩЕГО тика. Второй тик и «Применить» уводили пин в
               произвольное место.

⚠ Обстановка. Диалоги подменены и каждый тест утверждает ФАКТ их невызова:
кнопки глушат исключения в `QMessageBox`, и падение внутри подвесило бы набор
модалкой вместо падения (четвёртый исход `PROTOCOL §5`). Виджеты сносятся
детерминированно (`tab.cleanup()`): брошенные на сборщик мусора виджеты Qt
детонируют отложенным удалением у соседа, который первым крутит очередь
событий (замер 1-32). Ожиданий по стенным часам нет ни одного (1-39).
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox, QDialog, QLineEdit  # noqa: E402
from PySide6.QtGui import QImage, QColor                                     # noqa: E402

from tools import corpus                                                     # noqa: E402

# ── корпус боксов: та же git-фикстура, что у семьи превью ────────────────
BOX_UID = "d74eb9f1"          # 66 узлов / 63 ребра / 48 текст-блоков
BOX_CLASS = "armatura_ruchn"  # 17 экземпляров — набор панели «Размеры»
BOX_SET_SIZE = 17
BOX_NID = "node_11"
BOX_BBOX = [25, 215, 45, 251]                 # 20×36
BOX_W, BOX_H = 20, 36
BOX_AREA = 720
SIDE = 90                                     # цель превью: квадрат 90×90
BOX_PREVIEW = [-10.0, 188.0, 80.0, 278.0]
BOX_PREVIEW_AREA = 8100.0
OUTSIDER = "node_50"          # узел ВНЕ набора — «остальная модель»
OUTSIDER_CENTROID = [844, 1380]               # [y, x]
OUTSIDER_MOVED = [844.0, 1387.0]              # тот же узел после переноса на +7

# Угловой ресайз РОВНО до размера превью — «та же ширина» из §110.25.
CORNER_BBOX = [25.0, 215.0, 115.0, 305.0]     # 90×90, но вокруг ДРУГОГО центра
CORNER_AREA = 8100.0
PIN_EDGE = ("node_11", "node_13")
PIN_ROLE = "source"
PIN_DX, PIN_DY = 10.0, 0.0                    # пин оператора до всего
PIN_SCALED = 45.0                             # 10.0 × (90/20) — и превью, и ресайз

# ── корпус полигонов ────────────────────────────────────────────────────
POLY_UID = "089feca2"
POLY_SET_CLASS = "unknow"                     # 7 узлов, из них 4 с контуром
POLY_SET = {"node_4", "node_6", "node_29", "node_30"}
POLY_NID = "node_30"                          # 605×75, 8 вершин
POLY_SIZE = (605.0, 75.0)
POLY_PIN_DX, POLY_PIN_DY = 10.0, 4.0

TOL = 1e-6


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


def _white_raster(uid, tmp_path_factory):
    path = corpus.graph_path(uid)
    assert path is not None, f"корпус-фикстура {uid} не найдена (tools/corpus.py)"
    h, w = json.loads(path.read_text(encoding="utf-8"))["graph"]["image_size"]
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    png = tmp_path_factory.mktemp(f"raster_{uid}") / f"{uid}.png"
    assert img.save(str(png))
    return str(png)


@pytest.fixture(scope="module")
def box_raster(tmp_path_factory):
    return _white_raster(BOX_UID, tmp_path_factory)


@pytest.fixture(scope="module")
def poly_raster(tmp_path_factory):
    return _white_raster(POLY_UID, tmp_path_factory)


@pytest.fixture
def dialogs(monkeypatch):
    """Все модальные диалоги → журнал ФАКТОВ вызова (`PROTOCOL §5`)."""
    calls = []
    for name in ("warning", "critical", "information", "question"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, _n=name, **kw: calls.append((_n, a[1:3]))))
    return calls


def _make_tab(uid, raster, monkeypatch):
    from ui.tabs.base_graph_tab import BaseGraphTab
    from ui.tabs.advanced_graph_tab import AdvancedGraphTab
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    monkeypatch.setattr(BaseGraphTab, "_download_artifacts", lambda self: None)

    tab = AdvancedGraphTab(uid, "проба 1-40", FakeAPI())
    editor = AdvancedGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(raster, str(corpus.graph_path(uid)))
    editor.resize(1400, 900)
    tab._editor = editor
    tab._on_editor_ready()
    return tab


@pytest.fixture
def box_tab(qapp, box_raster, monkeypatch):
    tab = _make_tab(BOX_UID, box_raster, monkeypatch)
    ed = tab._editor
    assert list(ed.nodes[BOX_NID]["bbox"]) == BOX_BBOX, "фикстура сдвинулась"
    assert list(ed.nodes[OUTSIDER]["centroid"]) == OUTSIDER_CENTROID, \
        "фикстура сдвинулась"
    yield tab
    tab.cleanup()


@pytest.fixture
def poly_tab(qapp, poly_raster, monkeypatch):
    tab = _make_tab(POLY_UID, poly_raster, monkeypatch)
    ed = tab._editor
    assert _size(ed, POLY_NID) == pytest.approx(POLY_SIZE, abs=TOL), \
        "фикстура сдвинулась"
    yield tab
    tab.cleanup()


# ── измерители: только данные ────────────────────────────────────────────

def _size(editor, nid):
    """(ширина, высота) рамки узла — то, что оператор видит на схеме."""
    bb = editor.nodes[nid]["bbox"]
    return (bb[2] - bb[0], bb[3] - bb[1])


def _frames_on_scene(editor):
    """Сколько жёлтых рамок РЕАЛЬНО на сцене, а не в списке редактора."""
    return sum(1 for it in editor._resize_frames if it.scene() is not None)


def _pin(editor, edge=PIN_EDGE, role=PIN_ROLE):
    from ui.editors import port_model
    e = next((x for x in editor.edges_data
              if (x.get("source"), x.get("target")) == edge), None)
    return port_model.edge_pin(e, role) if e is not None else None


def _set_pin(editor, nid=BOX_NID, dx=PIN_DX, dy=PIN_DY):
    from ui.editors import port_model
    node = editor.nodes[nid]
    cy, cx = node["centroid"]
    e = next(x for x in editor.edges_data
             if (x.get("source"), x.get("target")) == PIN_EDGE)
    port_model.set_edge_pin(node, e, PIN_ROLE, cx + dx, cy + dy)
    assert _pin(editor) == {"dx": dx, "dy": dy}, "пин не встал — тест бессмыслен"


def _sent_node(tab, nid):
    """Узел в ПОСЛЕДНЕМ графе, который вкладка отдала серверу."""
    uploads = tab.api_client.uploads
    assert uploads, "на сервер не ушло ничего — сверять нечего"
    for node in uploads[-1][2]["nodes"]:
        if node.get("id") == nid:
            return node
    raise AssertionError(f"{nid} нет в отправленном графе")


def _sent_pin(tab, edge=PIN_EDGE, role=PIN_ROLE):
    uploads = tab.api_client.uploads
    assert uploads, "на сервер не ушло ничего — сверять нечего"
    for e in uploads[-1][2]["links"]:
        if (e.get("source"), e.get("target")) == edge:
            return e.get(f"pin_{role}")
    raise AssertionError(f"ребра {edge} нет в отправленном графе")


# ── жесты оператора ──────────────────────────────────────────────────────

def _open_resize(editor, cls=BOX_CLASS):
    editor.set_mode("resize_objects")
    editor.set_resize_class(cls)


def _open_poly_set(editor):
    """Штатный полигонный набор: класс целиком → кнопка «только полигоны»."""
    editor.set_mode("resize_objects")
    editor.set_resize_class(POLY_SET_CLASS)
    editor.resize_filter("poly")
    assert editor._resize_sel == POLY_SET, "состав набора сдвинулся"


def _box_preview(editor):
    editor.preview_resize(width=SIDE, height=SIDE)
    assert editor.nodes[BOX_NID]["bbox"] == pytest.approx(BOX_PREVIEW, abs=TOL), \
        "превью ничего не изменило — тест бессмыслен"


def _foreign_move(editor, nid, dx=7.0):
    """ЧУЖОЕ действие оператора: перенос узла (команда, правящая НА МЕСТЕ)."""
    editor.set_mode("idle")
    cy, cx = editor.nodes[nid]["centroid"]
    editor.start_drag_node(nid)
    editor.drag_node_to(cx + dx, cy)
    editor.end_drag_node()
    assert editor.undo_mgr.can_undo, "перенос не встал в стек — тест бессмыслен"


def _corner_resize(editor, nid=BOX_NID, bbox=CORNER_BBOX):
    """ЧУЖОЕ действие №2: ШТАТНЫЙ угловой ресайз бокса (Ctrl+2ЛКМ по боксу)."""
    editor._enter_resize_mode(nid)
    editor._on_node_resized(nid, list(bbox))      # on_resize оверлея
    editor._commit_resize(nid)                    # on_commit оверлея
    assert editor.undo_mgr.can_undo, "ресайз не встал в стек — тест бессмыслен"


def _live_blocks(editor):
    return [b for b in editor.model.text_blocks if b.get("merged_into") is None]


def _drag_ocr_handle(editor, bid, grow=30.0):
    """Оператор тянет угловую ручку блока и отпускает — ровно два колбэка
    `ResizableNodeOverlay`: кадр протяжки (`on_resize`) и отпускание
    (`on_commit`)."""
    ov = editor._ocr_resize_overlay
    assert ov is not None, "ручки не показаны — тест бессмыслен"
    bb = list(editor.model.find_text_block(bid)["bbox"])
    ov.start_drag("br")
    ov.drag_to(bb[2] + grow, bb[3] + grow)
    ov.end_drag()


def _edit_diameter(editor, edge_key, text, monkeypatch):
    """Ctrl+2ЛКМ по ребру в состоянии «ОКР привязка» → диалог диаметра → ОК."""
    def fake_exec(self):
        for w in self.findChildren(QLineEdit):
            w.setText(text)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    editor._open_diameter_edit_dialog(edge_key)


def _autosave_tick(tab):
    """Ровно тик таймера автосохранения — без единого жеста оператора."""
    from ui.services.autosave import AutoSaveService
    from ui.services.ui_settings import UISettings

    UISettings.instance().autosave_enabled = True
    svc = AutoSaveService()
    svc._tab = tab
    svc._on_tick()
    return svc


# =========================================================================
# S1 — жёлтые рамки набора против `_redraw_all` (§83.33)
# =========================================================================

def test_edge_color_change_keeps_the_yellow_frames_of_the_live_set(
        box_tab, dialogs):
    """Шторка «Оформление» → цвет рёбер, при живом наборе «Размеры».

    `set_edge_color` зовёт `_redraw_all`, а тот сносит со сцены всё
    `zValue > 0` — жёлтые рамки лежат на `zValue 9`. Набор оставался живым
    и невидимым: оператор не знает, что инструмент держит 17 узлов, и все
    остальные ловушки этого файла становятся незаметны глазом.
    """
    ed = box_tab._editor
    _open_resize(ed)
    assert len(ed._resize_sel) == BOX_SET_SIZE, "набор не собрался"
    assert _frames_on_scene(ed) == BOX_SET_SIZE, "рамок не было и до правки"

    ed.set_edge_color(QColor("#ff0000"))          # жест оператора в шторке

    assert len(ed._resize_sel) == BOX_SET_SIZE, "набор пропал — тест бессмыслен"
    assert _frames_on_scene(ed) == BOX_SET_SIZE
    assert len(ed._resize_frames) == BOX_SET_SIZE, "в списке остались мёртвые item'ы"
    assert dialogs == []


def test_any_full_redraw_keeps_the_yellow_frames_of_the_live_set(box_tab):
    """Тот же инвариант на ОБЩЕМ пути: любая перерисовка сцены.

    `_redraw_all` зовут ~30 мест — откат снимочной команды, правка KKS,
    смена состояния, «Применить». Запирается сам метод, а не один зовущий.
    """
    ed = box_tab._editor
    _open_resize(ed)
    ed._redraw_all()
    assert _frames_on_scene(ed) == BOX_SET_SIZE
    assert len(ed._resize_frames) == BOX_SET_SIZE

    ed._exit_resize_objects()                     # набора нет — и рамок нет
    assert _frames_on_scene(ed) == 0
    ed._redraw_all()
    assert _frames_on_scene(ed) == 0
    assert ed._resize_frames == []


# =========================================================================
# S2 — открытый снимок ручек OCR-блока (§83.30)
# =========================================================================

def test_one_undo_after_the_block_resize_keeps_the_foreign_committed_command(
        box_tab, dialogs):
    """Ручки блока → ЧУЖАЯ подтверждённая команда → протяжка ручки → Ctrl+Z.

    Снимок ручек снимался при их ПОКАЗЕ и жил до первого отпускания; чужая
    команда, подтверждённая между этими моментами, попадала внутрь его
    `_before`. Один Ctrl+Z откатывал ЕЁ — оператор терял подтверждённую
    работу и не мог её вернуть иначе как redo размера блока.
    """
    ed = box_tab._editor
    ed.set_display_regime("ocr")
    blocks = _live_blocks(ed)
    victim, doomed = blocks[0]["id"], blocks[1]["id"]

    ed._show_ocr_block_resize(victim)             # ручки открыты (момент T0)
    ed.set_mode("idle")                           # оператор ушёл с ручек
    assert ed._ocr_resize_overlay is not None, "ручки сняты — тест бессмыслен"

    ed.delete_ocr_block(doomed)                   # ЧУЖАЯ подтверждённая команда
    assert ed.model.find_text_block(doomed) is None, "блок не удалён"
    assert ed.undo_mgr.stack_depth == 1

    before = list(ed.model.find_text_block(victim)["bbox"])
    _drag_ocr_handle(ed, victim)                  # протяжка ручки + отпускание
    assert ed.undo_mgr.stack_depth == 2, "размер блока не стал ОТДЕЛЬНЫМ шагом"
    assert ed.model.find_text_block(victim)["bbox"] != before, "ручка не тянулась"

    ed.undo()                                     # ОДИН Ctrl+Z

    assert ed.model.find_text_block(victim)["bbox"] == pytest.approx(
        before, abs=TOL), "Ctrl+Z не вернул размер блока"
    assert ed.model.find_text_block(doomed) is None, \
        "один Ctrl+Z откатил ЧУЖУЮ подтверждённую команду"
    assert ed.undo_mgr.stack_depth == 1
    assert dialogs == []


def test_the_open_handles_do_not_capture_anything_until_the_drag_starts(
        box_tab):
    """Обратная полярность: пока ручку не тянули, шага в стеке нет вовсе.

    Иначе лечение сводилось бы к «класть шаг всегда» — и каждый показ ручек
    засорял бы стек пустым шагом.
    """
    ed = box_tab._editor
    ed.set_display_regime("ocr")
    bid = _live_blocks(ed)[0]["id"]

    ed._show_ocr_block_resize(bid)
    ed.set_mode("idle")
    ed._hide_ocr_block_resize()

    assert ed.undo_mgr.stack_depth == 0
    assert ed.undo_mgr.can_undo is False


# =========================================================================
# S3 — правка диаметра ребра мимо стека (§83.31)
# =========================================================================

def test_diameter_edit_is_a_step_the_tab_and_the_stack_both_see(
        box_tab, dialogs, monkeypatch):
    """Правка диаметра обязана быть ШАГОМ: её видит и стек, и дёрти-флаг.

    Без шага вкладка считала, что несохранённого нет, и закрывалась без
    вопроса; автосохранение по той же причине не срабатывало вовсе.
    Соседний путь того же жеста (KKS по Ctrl+2ЛКМ) шаг строит — здесь
    заперта та же форма.
    """
    ed = box_tab._editor
    box_tab._saved_revision = ed.undo_mgr.revision
    assert box_tab.has_unsaved_changes() is False, "вкладка грязная до правки"

    key = ed.model.edge_key(*PIN_EDGE)
    assert ed.model.find_edge_data(key).get("diameter_text") is None

    _edit_diameter(ed, key, "200", monkeypatch)

    assert ed.model.find_edge_data(key).get("diameter_text") == "200"
    assert ed.model.find_edge_data(key).get("diameter_value") == 200
    assert ed.undo_mgr.stack_depth == 1
    assert box_tab.has_unsaved_changes() is True
    assert dialogs == []

    ed.undo()
    assert ed.model.find_edge_data(key).get("diameter_text") is None
    ed.redo()
    assert ed.model.find_edge_data(key).get("diameter_text") == "200"


def test_undo_of_a_neighbour_command_does_not_erase_the_diameter(
        box_tab, dialogs, monkeypatch):
    """ЧУЖАЯ снимочная команда → правка диаметра → Ctrl+Z.

    Минимальная форма дефекта: `_before` соседней команды снят ДО правки, и
    один Ctrl+Z по ней стирал диаметр вместе с ней — обе правки уходили за
    один шаг, а вернуть диаметр было нечем.
    """
    ed = box_tab._editor
    ed.set_display_regime("ocr")
    doomed = _live_blocks(ed)[0]["id"]
    ed.delete_ocr_block(doomed)                   # соседняя снимочная команда

    key = ed.model.edge_key(*PIN_EDGE)
    _edit_diameter(ed, key, "200", monkeypatch)
    assert ed.undo_mgr.stack_depth == 2, "правка диаметра не стала своим шагом"

    ed.undo()                                     # первый Ctrl+Z — по диаметру
    assert ed.model.find_edge_data(key).get("diameter_text") is None
    assert ed.model.find_text_block(doomed) is None, \
        "первый Ctrl+Z откатил СОСЕДНЮЮ команду вместо диаметра"

    ed.undo()                                     # второй — по соседней команде
    assert ed.model.find_text_block(doomed) is not None
    assert ed.undo_mgr.stack_depth == 0
    assert dialogs == []


# =========================================================================
# S4 — тик автосохранения против живого превью (§83.32)
# =========================================================================

def test_autosave_tick_keeps_the_live_preview_and_still_saves_the_committed(
        box_tab, dialogs):
    """Фоновый тик пишет ЗАФИКСИРОВАННОЕ и не трогает холст оператора.

    Родня 1-38: тик таймера — не жест оператора. Прежде тик заходил в общий
    путь записи, тот снимал незафиксированное превью (лечение 1.5), и размер,
    который оператор в эту минуту подбирал бегунком, исчезал с холста — без
    единого его действия и без следа в стеке.

    ⛔ Заперты ОБА инварианта разом, иначе лечение съедает соседний: 1.5
    требует, чтобы серверу досталось зафиксированное состояние (и оно ему
    достаётся), §83.32 — чтобы холст оператора остался нетронутым.
    """
    ed = box_tab._editor
    _foreign_move(ed, OUTSIDER)                   # вкладке есть что сохранять
    _open_resize(ed)
    _box_preview(ed)

    _autosave_tick(box_tab)

    assert ed.nodes[BOX_NID]["bbox"] == pytest.approx(BOX_PREVIEW, abs=TOL), \
        "тик снял живое превью"
    assert len(box_tab.api_client.uploads) == 1, "тик не сохранил вообще ничего"
    assert _sent_node(box_tab, BOX_NID)["bbox"] == pytest.approx(BOX_BBOX, abs=TOL), \
        "превью уехало на сервер (инвариант 1.5)"
    assert ed.nodes[OUTSIDER]["centroid"] == pytest.approx(OUTSIDER_MOVED, abs=TOL)
    assert dialogs == []


def test_the_preview_the_tick_gave_back_is_still_a_working_preview(
        box_tab, dialogs):
    """Вернуть картинку мало — вернуть надо РАБОТОСПОСОБНОЕ превью.

    Иначе лечение выродилось бы в «нарисовать те же рамки»: базлайн после
    тика обязан описывать текущую модель, чтобы «Применить» дало ровно 90×90,
    а один Ctrl+Z вернул исходные 20×36. Проверяется РАЗНИЦА, а не совпадение
    с состоянием «до».
    """
    ed = box_tab._editor
    _foreign_move(ed, OUTSIDER)
    _open_resize(ed)
    _box_preview(ed)

    _autosave_tick(box_tab)
    assert ed.nodes[BOX_NID]["bbox"] == pytest.approx(BOX_PREVIEW, abs=TOL), \
        "превью не вернулось на холст — дальше проверять нечего"

    ed.apply_resize(width=SIDE, height=SIDE)
    assert _size(ed, BOX_NID) == pytest.approx((SIDE, SIDE), abs=TOL)

    ed.undo()
    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)
    assert ed.nodes[BOX_NID]["bbox"] == pytest.approx(BOX_BBOX, abs=TOL)
    assert dialogs == []


def test_manual_save_still_drops_the_preview(box_tab, dialogs):
    """Обратная полярность (инвариант 1.5): ЖЕСТ оператора превью снимает.

    Без этой половины лечение S4 выродилось бы в «не снимать превью никогда»,
    и превью снова уезжало бы на сервер.
    """
    ed = box_tab._editor
    _open_resize(ed)
    _box_preview(ed)

    assert box_tab._save_graph() is True
    assert _sent_node(box_tab, BOX_NID)["bbox"] == pytest.approx(BOX_BBOX, abs=TOL)
    assert ed.nodes[BOX_NID]["bbox"] == pytest.approx(BOX_BBOX, abs=TOL)
    assert dialogs == []


def test_autosave_tick_saves_normally_without_a_preview(box_tab, dialogs):
    """И третья полярность: без превью тик работает как работал.

    Возврат обязан быть УЗКИМ: жетон пуст — значит `preview_resize` после
    записи не зовётся вовсе и протухшее превью не воскресает.
    """
    ed = box_tab._editor
    _foreign_move(ed, OUTSIDER)

    _autosave_tick(box_tab)

    assert len(box_tab.api_client.uploads) == 1
    assert box_tab.save_refusal == ""
    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)
    assert dialogs == []


def test_a_preview_applied_before_the_tick_is_not_re_previewed_by_it(
        box_tab, dialogs):
    """Граница возврата с другой стороны: подтверждённое превью не повторяется.

    После «Применить» жетон обязан быть пуст. Иначе тик после подтверждения
    накатил бы те же размеры ВТОРОЙ раз — на полигонах это масштаб в квадрате
    (§97.2), то есть порча данных, а не лишняя перерисовка.
    """
    ed = box_tab._editor
    _open_resize(ed)
    _box_preview(ed)
    ed.apply_resize(width=SIDE, height=SIDE)
    applied = list(ed.nodes[BOX_NID]["bbox"])

    _autosave_tick(box_tab)

    assert ed.nodes[BOX_NID]["bbox"] == pytest.approx(applied, abs=TOL)
    assert _sent_node(box_tab, BOX_NID)["bbox"] == pytest.approx(applied, abs=TOL)
    assert dialogs == []


# =========================================================================
# S5 — точка возврата «Применить» и ОСТАЛЬНАЯ модель (§103.18)
# =========================================================================

def test_apply_does_not_resurrect_a_move_the_operator_undid(box_tab, dialogs):
    """Перенос ВНЕ набора → превью → Ctrl+Z → «Применить» → отмена до дна.

    Точка возврата «Применить» — снимок ВСЕЙ модели, снятый до превью; сторож
    же судит по 4 полям узлов НАБОРА. Чужой откат ВНЕ отпечатков оставался
    базлайну невидим, вваривался в `cmd._before`, и отмена до дна воскрешала
    отменённый перенос при стеке 0 — вернуть было нечем.
    """
    ed = box_tab._editor
    _foreign_move(ed, OUTSIDER)
    assert ed.nodes[OUTSIDER]["centroid"] == pytest.approx(OUTSIDER_MOVED, abs=TOL)

    _open_resize(ed)
    _box_preview(ed)
    ed.undo()                                     # Ctrl+Z по чужому переносу
    assert ed.nodes[OUTSIDER]["centroid"] == pytest.approx(
        OUTSIDER_CENTROID, abs=TOL), "откат не сработал — тест бессмыслен"

    ed.apply_resize(width=SIDE, height=SIDE)
    assert _size(ed, BOX_NID) == pytest.approx((SIDE, SIDE), abs=TOL), \
        "«Применить» не применило размер — тест бессмыслен"

    steps = 0
    while ed.undo_mgr.can_undo and steps < 20:
        ed.undo()
        steps += 1

    assert ed.nodes[OUTSIDER]["centroid"] == pytest.approx(
        OUTSIDER_CENTROID, abs=TOL), "отменённый перенос воскрешён"
    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)
    assert dialogs == []


def test_undo_of_apply_returns_the_schema_the_operator_saw_before_it(
        box_tab, dialogs):
    """Один Ctrl+Z по «Применить» возвращает ровно то, что было перед ним.

    Утверждается РАЗНИЦА, а не совпадение с состоянием «до»: перед
    «Применить» оператор уже отменил свой перенос, значит после отката
    «Применить» узел обязан стоять на ОТКАТНОМ месте, а размеры — быть
    исходными.
    """
    ed = box_tab._editor
    _foreign_move(ed, OUTSIDER)
    _open_resize(ed)
    _box_preview(ed)
    ed.undo()

    ed.apply_resize(width=SIDE, height=SIDE)
    ed.undo()                                     # ровно ОДИН Ctrl+Z

    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)
    assert ed.nodes[OUTSIDER]["centroid"] == pytest.approx(
        OUTSIDER_CENTROID, abs=TOL)
    assert dialogs == []


# =========================================================================
# S6 — та же точка возврата и УДАЛЕНИЕ
# =========================================================================

def test_undo_of_apply_does_not_delete_the_node_the_operator_brought_back(
        box_tab, dialogs):
    """Удаление → превью → Ctrl+Z (узел воскрес) → «Применить» → Ctrl+Z.

    Базлайн снят, пока узла НЕ БЫЛО; отпечатки набора его не видят, поэтому
    базлайн считался живым и уезжал в `cmd._before`. Откат «Применить»
    удалял воскрешённый узел снова — и стек после этого пуст, вернуть нечем.
    """
    ed = box_tab._editor
    ed.set_mode("idle")
    assert ed.delete_node(OUTSIDER) is True
    assert OUTSIDER not in ed.nodes

    _open_resize(ed)
    _box_preview(ed)
    ed.undo()                                     # Ctrl+Z по удалению
    assert OUTSIDER in ed.nodes, "узел не воскрес — тест бессмыслен"

    ed.apply_resize(width=SIDE, height=SIDE)
    assert OUTSIDER in ed.nodes, "«Применить» удалило узел"

    ed.undo()                                     # откат «Применить»
    assert OUTSIDER in ed.nodes, "откат «Применить» удалил воскрешённый узел"
    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)
    assert dialogs == []


# =========================================================================
# S7 — redo углового ресайза ТОЙ ЖЕ ширины: ЖЕСТ, а не лотерея (§110.25)
# =========================================================================

def test_save_after_redo_of_a_corner_resize_keeps_frame_pin_and_area_together(
        box_tab, dialogs):
    """Пин → угловой ресайз 90×90 → Ctrl+Z → превью 90×90 → Ctrl+Y → «Сохранить».

    Отпечаток пина совпадает СТРУКТУРНО: обе стороны считают его одной
    формулой (10.0 × 90/20) от одной базы, поэтому «в пине ещё наш след»
    отвечает «да» и на чужой redo-рамке. Узловые поля при этом расходятся
    частично — `segmentation` пуст у обоих, `area` совпадает по значению, —
    и откат превью писал в узел, которым уже владеет чужая команда: рамка
    redo, площадь и пин от ИСХОДНОЙ рамки. Рассинхрон персистентен в JSON.
    """
    ed = box_tab._editor
    _set_pin(ed)
    _corner_resize(ed)
    assert _pin(ed) == {"dx": PIN_SCALED, "dy": PIN_DY}, \
        "ресайз не отмасштабировал пин — тест бессмыслен"

    ed.undo()
    assert _pin(ed) == {"dx": PIN_DX, "dy": PIN_DY}

    _open_resize(ed)
    _box_preview(ed)
    assert _pin(ed) == {"dx": PIN_SCALED, "dy": PIN_DY}, \
        "превью дало другой след — сценарий не про совпадение"

    ed.redo()                                     # Ctrl+Y по угловому ресайзу
    assert ed.nodes[BOX_NID]["bbox"] == pytest.approx(CORNER_BBOX, abs=TOL), \
        "redo не вернул рамку — тест бессмыслен"

    assert box_tab._save_graph() is True
    assert dialogs == []

    sent = _sent_node(box_tab, BOX_NID)
    assert sent["bbox"] == pytest.approx(CORNER_BBOX, abs=TOL)
    assert sent["area"] == pytest.approx(CORNER_AREA, abs=TOL), \
        "площадь от ИСХОДНОЙ рамки при redo-рамке"
    assert _sent_pin(box_tab) == {"dx": PIN_SCALED, "dy": PIN_DY}, \
        "пин от ИСХОДНОЙ рамки при redo-рамке"
    # Модель и сервер сошлись — рассинхрона не осталось и в оперативной памяти.
    assert ed.nodes[BOX_NID]["area"] == pytest.approx(CORNER_AREA, abs=TOL)
    assert _pin(ed) == {"dx": PIN_SCALED, "dy": PIN_DY}


def test_the_rest_of_the_set_is_still_rolled_back_after_that_redo(
        box_tab, dialogs):
    """Граница лечения S7: чужой redo забирает ОДИН узел, а не весь набор.

    Иначе «не трогать узел, которым владеет чужая команда» выродилось бы
    в «не откатывать превью вовсе» — и превью остальных шестнадцати уехало
    бы на сервер (ровно дефект третьего возврата 1-6).
    """
    ed = box_tab._editor
    witness = "node_14"
    witness_size = _size(ed, witness)
    assert witness_size != (SIDE, SIDE), "свидетель уже 90×90 — тест бессмыслен"

    _set_pin(ed)
    _corner_resize(ed)
    ed.undo()
    _open_resize(ed)
    assert witness in ed._resize_sel, "свидетель не в наборе"
    _box_preview(ed)
    ed.redo()

    assert box_tab._save_graph() is True
    assert _size(ed, witness) == pytest.approx(witness_size, abs=TOL)
    assert _sent_node(box_tab, witness)["bbox"] == pytest.approx(
        ed.nodes[witness]["bbox"], abs=TOL)
    assert dialogs == []


# =========================================================================
# S8 — поля «из ничего»: ВЕРДИКТ ОБСТАНОВКА, заперта ПРЕДПОСЫЛКА
# =========================================================================

def test_no_equipment_node_in_the_git_corpus_lacks_area_or_bbox():
    """⛔ Почему здесь нет лечения, а есть сторож предпосылки.

    `_rollback_owned_preview` возвращает поле, только если оно было в базлайне
    (`base['area'] is not None`, `if base['bbox']`). Поле, которое превью
    создало «из ничего», откат оставил бы на схеме навсегда. Класс реален,
    но состояние недостижимо: ни один узел оборудования ни в одной фикстуре
    не приходит без `area`/`bbox`, и оба пути создания узла в редакторе
    (`create_equipment_node`, рамка) кладут оба поля. Замер по всему
    локальному корпусу — `MEASUREMENTS §121`: 0 из 13061.

    Поэтому лечится не код, а ПРЕДПОСЫЛКА: этот сторож покраснеет в тот
    день, когда такой узел появится, — то есть ровно тогда, когда гард
    станет дефектом.
    """
    for uid in sorted(corpus.fixture_paths()):
        graph = corpus.load_graph(uid)
        equipment = [n for n in graph["nodes"] if n.get("type") == "equipment"]
        assert equipment, f"{uid}: оборудования нет — фикстура не про то"
        assert [n["id"] for n in equipment if n.get("area") is None] == []
        assert [n["id"] for n in equipment if not n.get("bbox")] == []


# =========================================================================
# S9 — пины полигонов под превью (§110.26)
# =========================================================================

def test_polygon_preview_scales_the_pin_from_the_baseline_every_tick(
        poly_tab, dialogs):
    """Бегунок масштаба: каждый тик считается от БАЗЛАЙНА, а не от прошлого тика.

    На боксах `_apply_sizes_from_base` возвращает узлу базовый `bbox`, и
    `rescale_edge_pins` получает пару «база → цель». У полигонов `bbox` не
    возвращался, и пара становилась «прошлый тик → цель»: от базы 10.0
    ×2 давало 20.0, ×3 — 15.0 вместо 30.0, возврат к ×2 — 6.67 вместо 20.0.
    Геометрия при этом была верна на каждом тике, поэтому глазом это
    не видно вовсе.
    """
    from ui.editors import port_model

    ed = poly_tab._editor
    edge = next(e for e in ed.edges_data
                if POLY_NID in (e.get("source"), e.get("target")))
    role = "source" if edge.get("source") == POLY_NID else "target"
    node = ed.nodes[POLY_NID]
    cy, cx = node["centroid"]
    port_model.set_edge_pin(node, edge, role,
                            cx + POLY_PIN_DX, cy + POLY_PIN_DY)

    def pin():
        return dict(port_model.edge_pin(edge, role))

    _open_poly_set(ed)

    ed.preview_resize(scale=2.0)
    assert _size(ed, POLY_NID) == pytest.approx(
        (POLY_SIZE[0] * 2, POLY_SIZE[1] * 2), abs=TOL)
    assert pin() == pytest.approx({"dx": 20.0, "dy": 8.0}, abs=TOL)

    ed.preview_resize(scale=3.0)
    assert _size(ed, POLY_NID) == pytest.approx(
        (POLY_SIZE[0] * 3, POLY_SIZE[1] * 3), abs=TOL)
    assert pin() == pytest.approx({"dx": 30.0, "dy": 12.0}, abs=TOL)

    ed.preview_resize(scale=2.0)                  # бегунок поехал НАЗАД
    assert pin() == pytest.approx({"dx": 20.0, "dy": 8.0}, abs=TOL)
    assert dialogs == []


def test_apply_on_polygons_sends_the_pin_that_belongs_to_the_new_shape(
        poly_tab, dialogs):
    """«Применить» ×2 на полигонах: серверу уходит пин НОВОЙ формы.

    `_apply_sizes_from_base` сначала возвращает пин к базлайну, потом
    масштабирует его парой «старый bbox → новый». Без возврата `bbox`
    пара вырождалась в «цель → цель», множитель становился 1.0, и на
    сервер уезжал пин ИСХОДНОЙ формы при удвоенной рамке: конец трубы
    оказывался внутри контура, а не на его границе.
    """
    from ui.editors import port_model

    ed = poly_tab._editor
    edge = next(e for e in ed.edges_data
                if POLY_NID in (e.get("source"), e.get("target")))
    role = "source" if edge.get("source") == POLY_NID else "target"
    node = ed.nodes[POLY_NID]
    cy, cx = node["centroid"]
    port_model.set_edge_pin(node, edge, role,
                            cx + POLY_PIN_DX, cy + POLY_PIN_DY)

    _open_poly_set(ed)
    ed.preview_resize(scale=2.0)
    ed.apply_resize(scale=2.0)

    assert _size(ed, POLY_NID) == pytest.approx(
        (POLY_SIZE[0] * 2, POLY_SIZE[1] * 2), abs=TOL)
    assert port_model.edge_pin(edge, role) == pytest.approx(
        {"dx": 20.0, "dy": 8.0}, abs=TOL)

    assert poly_tab._save_graph() is True
    sent = _sent_pin(poly_tab, (edge["source"], edge["target"]), role)
    assert sent == pytest.approx({"dx": 20.0, "dy": 8.0}, abs=TOL)
    assert sent != pytest.approx({"dx": POLY_PIN_DX, "dy": POLY_PIN_DY}, abs=TOL)
    assert dialogs == []

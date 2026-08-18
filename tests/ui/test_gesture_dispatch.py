# -*- coding: utf-8 -*-
"""Пункт 0.4 дороги — характеризационные тесты ДИСПЕТЧЕРА мыши/клавиш редактора.

Зачем. На `mousePressEvent`/`mouseMoveEvent`/`mouseReleaseEvent` графового
редактора не было ни одного теста: соседние наборы (`test_interactive_drag.py`,
`test_mode_exit_finishes_gesture.py`) дёргают ВНУТРЕННИЕ методы жеста
(`start_drag_node`/`drag_node_to`/`end_drag_node`, `on_exit` хендлера) — то есть
проверяют исполнителя, а не того, кто раздаёт клики. Здесь наоборот: жест
подаётся настоящими `QMouseEvent`/`QKeyEvent` через публичные обработчики
(как их зовёт Qt), а проверяются только ДАННЫЕ, которые из этого вышли.

Что фиксируется (характеризация: «как есть сегодня», не «как правильно»):
геометрия узлов, состав узлов/рёбер, глубина стека undo и число ВИДИМЫХ
ручек размера на сцене. Ни одного утверждения про `_current_mode`,
`_current_handler`, `_ctrl_lmb_*` и прочую внутреннюю кухню — она уезжает
вместе с декомпозицией UI (этап 10), а инварианты данных остаются.

Харнесс портирован из `_scratch/audit_2026-08-06/scripts/j4_lib.py` (358 строк,
вне git): те же `_ev`/`press`/`move`/`release`, но корпус берётся загрузчиком
КД7 (`tools/corpus.py`, пункт 0.8) — фикстура `d74eb9f1` лежит в git, поэтому
набор исполняется и в CI, и на чистом клоне.

Ловушки, замеренные при написании (MEASUREMENTS §36):
  • координаты двойственны (CODING_GUIDE §6): `centroid` = [y, x],
    `bbox` = [x1, y1, x2, y2]; сцена — (x, y);
  • `mapFromScene` отдаёт ЦЕЛЫЙ пиксель вьюпорта, а масштаб вида здесь 0.33 —
    обратный перевод даёт до ~1.2 px расхождения по сцене. Поэтому сдвиги
    сверяются с допуском, а побайтово сравнивается только то, что вернул undo;
  • защёлка `ctrl_pressed` — состояние КЛАВИАТУРЫ: ветки расширения выделения
    в `AdvancedGraphEditor.mousePressEvent` читают её ДО того, как базовый
    класс обновит её из модификаторов события. Поэтому «оператор держит Ctrl»
    эмулируется настоящим `KeyPress` Ctrl (`_ctrl_down`), а не присваиванием.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QGraphicsRectItem   # noqa: E402
from PySide6.QtCore import Qt, QPointF, QEvent                  # noqa: E402
from PySide6.QtGui import (QMouseEvent, QKeyEvent, QFocusEvent,  # noqa: E402
                           QImage, QColor)

from tools import corpus                                        # noqa: E402

CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
NONE = Qt.KeyboardModifier.NoModifier
LMB = Qt.MouseButton.LeftButton
RMB = Qt.MouseButton.RightButton

UID = "d74eb9f1"          # 66 узлов / 63 ребра, корпус-фикстура в git
DRAG_TOL = 4.0            # допуск на округление сцена -> вьюпорт -> сцена


# ── харнесс ──────────────────────────────────────────────────────────────

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
def ed(qapp, raster):
    """Редактор с корпусной схемой — так его собирает вкладка:
    `_canvas_mode` ставится ДО `load_data`, иначе сцена соберётся не как холст."""
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    editor = AdvancedGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(raster, str(corpus.graph_path(UID)))
    editor.resize(1400, 900)
    return editor


def _ev(kind, sx, sy, editor, buttons=LMB, button=LMB, mods=NONE):
    """Событие мыши в точке СЦЕНЫ (sx, sy) — ровно как его отдаёт Qt."""
    vp = QPointF(editor.mapFromScene(QPointF(sx, sy)))
    kinds = {"press": QEvent.Type.MouseButtonPress,
             "move": QEvent.Type.MouseMove,
             "release": QEvent.Type.MouseButtonRelease,
             "dclick": QEvent.Type.MouseButtonDblClick}
    return QMouseEvent(kinds[kind], vp, vp, button, buttons, mods)


def _press(editor, x, y, mods=NONE, button=LMB):
    editor.mousePressEvent(_ev("press", x, y, editor, buttons=button,
                               button=button, mods=mods))


def _move(editor, x, y, mods=NONE, down=True):
    editor.mouseMoveEvent(_ev("move", x, y, editor,
                              buttons=LMB if down else Qt.MouseButton.NoButton,
                              button=Qt.MouseButton.NoButton, mods=mods))


def _release(editor, x, y, mods=NONE):
    editor.mouseReleaseEvent(_ev("release", x, y, editor,
                                 buttons=Qt.MouseButton.NoButton, mods=mods))


def _dclick(editor, x, y, mods=NONE):
    editor.mouseDoubleClickEvent(_ev("dclick", x, y, editor, mods=mods))


def _drag(editor, x0, y0, dx, dy, mods=CTRL, steps=4):
    """Полный жест протяжки: нажать, провести в несколько кадров, отпустить."""
    _press(editor, x0, y0, mods=mods)
    for k in range(1, steps + 1):
        _move(editor, x0 + dx * k / steps, y0 + dy * k / steps, mods=mods)
    _release(editor, x0 + dx, y0 + dy, mods=mods)


def _ctrl_down(editor):
    """Оператор нажал и ДЕРЖИТ Ctrl (защёлка живёт до keyRelease)."""
    editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Control, NONE))


def _esc(editor):
    editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, NONE))


def _nodes_with_bbox(editor, min_side=20.0):
    """Оборудование с реальной рамкой, по центроиду которого клик попадает
    именно в него (иначе жест адресован не тому объекту, и тест бессмыслен)."""
    out = []
    for nid, nd in editor.nodes.items():
        bb = nd.get("bbox")
        if nd.get("type") != "equipment" or not bb or len(bb) != 4:
            continue
        if bb[2] - bb[0] <= min_side or bb[3] - bb[1] <= min_side:
            continue
        if editor.find_node_at(*_cxy(editor, nid)) != nid:
            continue
        out.append(nid)
    return out


def _body_point(editor, nid):
    """Точка внутри рамки, по которой `find_node_at` молчит (он смотрит радиус
    вокруг центроида) — так оператор кликает по ТЕЛУ бокса, а не по центру."""
    bb = editor.nodes[nid]["bbox"]
    for fx, fy in ((0.8, 0.8), (0.2, 0.2), (0.8, 0.2), (0.2, 0.8)):
        px = bb[0] + (bb[2] - bb[0]) * fx
        py = bb[1] + (bb[3] - bb[1]) * fy
        if editor.find_node_at(px, py) is None:
            return px, py
    return None


def _cxy(editor, nid):
    """Центроид узла в координатах сцены: centroid = [y, x] -> (x, y)."""
    c = editor.nodes[nid]["centroid"]
    return float(c[1]), float(c[0])


def _geom(editor):
    """Снимок геометрии всех узлов — база всех сравнений «данные не тронуты»."""
    return json.dumps({nid: [nd.get("bbox"), nd.get("centroid"),
                             nd.get("segmentation")]
                       for nid, nd in editor.nodes.items()}, sort_keys=True)


def _edges(editor):
    return json.dumps([[e.get("source"), e.get("target"), e.get("source_point"),
                        e.get("target_point"), e.get("waypoints")]
                       for e in editor.edges_data], sort_keys=True)


def _visible_handles(editor):
    """Число ВИДИМЫХ ручек размера на сцене.

    Считаем по самой сцене (квадрат `HANDLE_SIZE` на слое `HANDLE_Z`), а не по
    полю `_resize_overlay`: ручки — это то, что видит оператор, и проверка
    переживёт перенос ресайза с режима на оверлей (пункт 10.1).
    """
    from ui.editors.resize_overlay import ResizableNodeOverlay as R

    n = 0
    for item in editor.scene.items():
        if isinstance(item, QGraphicsRectItem) and item.zValue() == R.HANDLE_Z:
            r = item.rect()
            if abs(r.width() - R.HANDLE_SIZE) < 0.6 and \
                    abs(r.height() - R.HANDLE_SIZE) < 0.6:
                n += 1
    return n


def _depth(editor):
    return editor.undo_mgr.stack_depth


# ── клик против протяжки (порог `DRAG_THRESHOLD`) ────────────────────────

def test_ctrl_drag_moves_only_the_grabbed_node(ed):
    """Ctrl+ЛКМ с проводкой = перетаскивание: узел уехал на дельту жеста,
    записан РОВНО один шаг undo, остальные узлы не тронуты."""
    nid = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, nid)
    others = {n: list(ed.nodes[n]["centroid"]) for n in ed.nodes if n != nid}
    before = list(ed.nodes[nid]["centroid"])
    depth0 = _depth(ed)

    _drag(ed, x, y, 60.0, 30.0)

    after = ed.nodes[nid]["centroid"]
    assert after[1] == pytest.approx(before[1] + 60.0, abs=DRAG_TOL)
    assert after[0] == pytest.approx(before[0] + 30.0, abs=DRAG_TOL)
    assert _depth(ed) == depth0 + 1, "жест обязан дать ровно один шаг undo"
    for other, c0 in others.items():
        assert ed.nodes[other]["centroid"] == c0, \
            f"перетаскивание {nid} сдвинуло чужой узел {other}"


def test_ctrl_drag_undo_restores_geometry_bytewise(ed):
    """Ctrl+Z после протяжки возвращает геометрию побайтово, Ctrl+Shift+Z —
    обратно (снимок всех узлов, а не только сдвинутого)."""
    nid = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, nid)

    g0 = _geom(ed)
    _drag(ed, x, y, 60.0, 30.0)
    g1 = _geom(ed)
    assert g1 != g0, "протяжка обязана была изменить данные (иначе тест пуст)"

    ed.undo()
    assert _geom(ed) == g0, "undo вернул не то состояние"
    ed.redo()
    assert _geom(ed) == g1, "redo вернул не пост-жестовое состояние"


def test_tremor_under_two_pixels_is_a_click_not_a_drag(ed):
    """Дрожание руки на 2 px — это КЛИК: ни геометрии, ни шага undo.

    Число абсолютное, не `DRAG_THRESHOLD`: порог фиксируется С ОБЕИХ сторон
    (этот тест и `test_deliberate_ten_pixel_move_is_a_drag`), иначе сама
    константа могла бы уехать в 0 или в 50, а набор бы этого не заметил.
    Сегодня порог — 5 px."""
    nid = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, nid)
    g0, depth0 = _geom(ed), _depth(ed)

    _drag(ed, x, y, 2.0, 0.0, steps=2)

    assert _geom(ed) == g0, "дрожание в 2 px сдвинуло данные"
    assert _depth(ed) == depth0, "дрожание в 2 px записало шаг undo"


def test_deliberate_ten_pixel_move_is_a_drag(ed):
    """Осознанное движение на 10 px — уже протяжка: узел едет, шаг undo есть.
    Вторая половина замка на порог различия клик/протяжка."""
    nid = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, nid)
    before = list(ed.nodes[nid]["centroid"])
    depth0 = _depth(ed)

    _drag(ed, x, y, 10.0, 0.0)

    assert ed.nodes[nid]["centroid"][1] == pytest.approx(before[1] + 10.0,
                                                         abs=DRAG_TOL)
    assert _depth(ed) == depth0 + 1


# ── защёлка Ctrl и брошенные жесты ───────────────────────────────────────

def test_plain_lmb_press_disarms_stale_ctrl_latch(ed):
    """Защёлка `ctrl_pressed` переживает alt-tab (keyRelease уходит другому
    окну). Следующий ЛКМ БЕЗ Ctrl обязан её снять — иначе первый же клик по
    холсту начинает таскать узел, о котором оператор не просил."""
    nid = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, nid)
    _ctrl_down(ed)                      # защёлка взведена и осталась висеть
    g0, depth0 = _geom(ed), _depth(ed)

    _drag(ed, x, y, 60.0, 30.0, mods=NONE)

    assert _geom(ed) == g0, "узел уехал по жесту без Ctrl (защёлка не снята)"
    assert _depth(ed) == depth0


def test_button_less_move_ends_drag_and_node_stops_following(ed):
    """«Залипший drag» (замер на боевом c2f79462: 5 узлов уехали до 305 px).
    У вьюпорта включён mouseTracking, поэтому move приходит и с ОТПУЩЕННОЙ
    кнопкой: такой кадр обязан завершить жест, а не везти узел дальше."""
    nid = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, nid)
    depth0 = _depth(ed)

    _press(ed, x, y, mods=CTRL)
    _move(ed, x + 40.0, y, mods=CTRL)
    frozen = list(ed.nodes[nid]["centroid"])
    assert frozen[1] == pytest.approx(x + 40.0, abs=DRAG_TOL), \
        "протяжка не началась (иначе тест пуст)"

    _move(ed, x + 300.0, y, mods=CTRL, down=False)     # кнопка отпущена
    _move(ed, x + 600.0, y, mods=CTRL, down=False)

    assert ed.nodes[nid]["centroid"] == frozen, "узел ехал за курсором без кнопки"
    assert _depth(ed) == depth0 + 1, "брошенный жест обязан лечь одним шагом undo"


def test_focus_loss_mid_drag_finishes_gesture(ed):
    """Уход фокуса посреди протяжки (alt-tab, модальный диалог) добивает жест:
    данные замирают на последнем кадре, шаг undo записан, курсор больше узел
    не везёт."""
    nid = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, nid)
    g0, depth0 = _geom(ed), _depth(ed)

    _press(ed, x, y, mods=CTRL)
    _move(ed, x + 40.0, y, mods=CTRL)
    frozen = list(ed.nodes[nid]["centroid"])

    ed.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
    _move(ed, x + 400.0, y, mods=CTRL)

    assert ed.nodes[nid]["centroid"] == frozen, "узел поехал после потери фокуса"
    assert _depth(ed) == depth0 + 1
    ed.undo()
    assert _geom(ed) == g0, "undo после брошенного жеста не вернул геометрию"


# ── правая кнопка: удаление ──────────────────────────────────────────────

def test_ctrl_rmb_deletes_node_with_its_edges_and_undo_restores_both(ed):
    """Ctrl+ПКМ по узлу — удаление: уходит и узел, и все инцидентные рёбра;
    Ctrl+Z возвращает и то, и другое побайтово."""
    nid = next(n for n in _nodes_with_bbox(ed)
               if any(e.get("source") == n or e.get("target") == n
                      for e in ed.edges_data))
    x, y = _cxy(ed, nid)
    incident = sum(1 for e in ed.edges_data
                   if e.get("source") == nid or e.get("target") == nid)
    n0, e0 = len(ed.nodes), len(ed.edges_data)
    g0, edges0, depth0 = _geom(ed), _edges(ed), _depth(ed)

    _press(ed, x, y, mods=CTRL, button=RMB)

    assert nid not in ed.nodes, "Ctrl+ПКМ не удалил узел"
    assert len(ed.nodes) == n0 - 1
    assert len(ed.edges_data) == e0 - incident, "рёбра удалённого узла остались"
    assert _depth(ed) == depth0 + 1

    ed.undo()
    assert _geom(ed) == g0, "undo не вернул узел"
    assert _edges(ed) == edges0, "undo не вернул рёбра удалённого узла"


def test_ctrl_rmb_on_empty_space_changes_nothing(ed):
    """Ctrl+ПКМ мимо всего — пустой жест: ни данных, ни шага undo."""
    empty = next((x, y) for x in (5.0, 12.0, 25.0) for y in (5.0, 12.0, 25.0)
                 if ed.find_node_at(x, y) is None)
    g0, edges0, depth0 = _geom(ed), _edges(ed), _depth(ed)

    _press(ed, *empty, mods=CTRL, button=RMB)

    assert (_geom(ed), _edges(ed), _depth(ed)) == (g0, edges0, depth0)


# ── активный инструмент: клик уходит инструменту, а не узлу ──────────────

def test_active_tool_keeps_node_geometry_untouched(ed):
    """При активном инструменте («Точки изгиба») тот же Ctrl+ЛКМ с проводкой
    по узлу узел НЕ двигает — жест принадлежит инструменту. Инструмент
    включается как кнопкой тулбара; проверяются только данные."""
    nid = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, nid)
    before = list(ed.nodes[nid]["centroid"])
    ed.set_mode("edit_waypoint")

    _drag(ed, x, y, 60.0, 30.0)

    assert ed.nodes[nid]["centroid"] == before, \
        "инструмент активен, а узел уехал за курсором"


def test_add_connector_tool_creates_node_without_moving_the_clicked_one(ed):
    """Инструмент «добавить коннектор»: тот же жест по узлу создаёт НОВЫЙ узел
    и не двигает узел под курсором."""
    nid = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, nid)
    before = list(ed.nodes[nid]["centroid"])
    n0 = len(ed.nodes)
    ed.set_mode("add_connector")

    _drag(ed, x, y, 60.0, 30.0)

    assert len(ed.nodes) == n0 + 1, "инструмент не создал узел"
    assert ed.nodes[nid]["centroid"] == before, "узел под курсором уехал"


# ── выделение: кому достался клик ────────────────────────────────────────

def test_shift_click_selects_node_without_touching_data(ed):
    """Shift+ЛКМ — выделение: узел в наборе выделенных, данные не тронуты."""
    nid = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, nid)
    g0, depth0 = _geom(ed), _depth(ed)

    _press(ed, x, y, mods=SHIFT)
    _release(ed, x, y, mods=SHIFT)

    assert nid in ed.selected_nodes
    assert (_geom(ed), _depth(ed)) == (g0, depth0)


def test_ctrl_click_on_box_body_extends_selection(ed):
    """Расширение выделения: при активном выделении Ctrl+ЛКМ по ТЕЛУ рамки
    (не по центроиду — там `find_node_at` слеп) добавляет узел к выделению.
    Ветка читает защёлку Ctrl, поэтому Ctrl нажимается по-настоящему."""
    boxes = _nodes_with_bbox(ed)
    first = boxes[0]
    second, body = next((nid, _body_point(ed, nid)) for nid in boxes[1:]
                        if _body_point(ed, nid) is not None)
    x, y = _cxy(ed, first)
    _press(ed, x, y, mods=SHIFT)
    _release(ed, x, y, mods=SHIFT)
    g0 = _geom(ed)

    _ctrl_down(ed)
    _press(ed, *body, mods=CTRL)
    _release(ed, *body, mods=CTRL)

    assert ed.selected_nodes == {first, second}
    assert _geom(ed) == g0, "клик по телу рамки сдвинул данные"


def test_ctrl_click_on_edge_extends_selection_with_edge(ed):
    """То же расширение, но целью выступает ребро: в наборе выделенных рёбер
    появляется ровно одно, узлы и данные не тронуты."""
    first = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, first)
    _press(ed, x, y, mods=SHIFT)
    _release(ed, x, y, mods=SHIFT)
    g0 = _geom(ed)

    edge = max((e for e in ed.edges_data
                if e.get("source_point") and e.get("target_point")
                and not e.get("waypoints")),
               key=lambda e: (e["source_point"][0] - e["target_point"][0]) ** 2
               + (e["source_point"][1] - e["target_point"][1]) ** 2)
    mid = ((edge["source_point"][1] + edge["target_point"][1]) / 2.0,
           (edge["source_point"][0] + edge["target_point"][0]) / 2.0)

    _ctrl_down(ed)
    _press(ed, *mid, mods=CTRL)
    _release(ed, *mid, mods=CTRL)

    assert len(ed.selected_edges) == 1, "ребро не попало в выделение"
    assert ed.selected_nodes == {first}
    assert _geom(ed) == g0


def test_batch_drag_moves_every_selected_node_by_the_same_delta(ed):
    """Протяжка за один из выделенных узлов везёт ВСЁ выделение на одну и ту же
    дельту (одним шагом undo), Ctrl+Z возвращает всех."""
    first, second = _nodes_with_bbox(ed)[:2]
    for nid in (first, second):
        x, y = _cxy(ed, nid)
        _press(ed, x, y, mods=SHIFT)
        _release(ed, x, y, mods=SHIFT)
    assert ed.selected_nodes == {first, second}

    g0, depth0 = _geom(ed), _depth(ed)
    c_first = list(ed.nodes[first]["centroid"])
    c_second = list(ed.nodes[second]["centroid"])
    x, y = _cxy(ed, first)

    _drag(ed, x, y, 60.0, 30.0)

    d_first = [ed.nodes[first]["centroid"][i] - c_first[i] for i in (0, 1)]
    d_second = [ed.nodes[second]["centroid"][i] - c_second[i] for i in (0, 1)]
    assert d_first[1] == pytest.approx(60.0, abs=DRAG_TOL)
    assert d_second == pytest.approx(d_first), \
        "выделение поехало вразнобой: дельты узлов разошлись"
    assert _depth(ed) == depth0 + 1, "групповая протяжка обязана быть одним шагом"

    ed.undo()
    assert _geom(ed) == g0


# ── видимые ручки размера ────────────────────────────────────────────────

def test_ctrl_double_click_shows_four_resize_handles(ed):
    """Ctrl+2ЛКМ по рамке — единственный вход в правку размера: на сцене
    появляются 4 ручки, данные при этом не меняются."""
    nid = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, nid)
    g0, depth0 = _geom(ed), _depth(ed)
    assert _visible_handles(ed) == 0

    _ctrl_down(ed)
    _dclick(ed, x, y, mods=CTRL)

    assert _visible_handles(ed) == 4, "ручки размера не появились"
    assert (_geom(ed), _depth(ed)) == (g0, depth0), \
        "показ ручек изменил данные"


def test_handle_drag_resizes_bbox_and_undo_restores_it(ed):
    """Протяжка за угловую ручку меняет рамку узла (одним шагом undo), Ctrl+Z
    возвращает её побайтово."""
    nid = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, nid)
    _ctrl_down(ed)
    _dclick(ed, x, y, mods=CTRL)
    bbox0 = list(ed.nodes[nid]["bbox"])
    g0, depth0 = _geom(ed), _depth(ed)

    _drag(ed, bbox0[2], bbox0[3], 30.0, 20.0)      # ручка правого-нижнего угла

    bbox1 = ed.nodes[nid]["bbox"]
    assert bbox1[0] == bbox0[0] and bbox1[1] == bbox0[1], \
        "тянули правый-нижний угол, а уехал левый-верхний"
    assert bbox1[2] == pytest.approx(bbox0[2] + 30.0, abs=DRAG_TOL)
    assert bbox1[3] == pytest.approx(bbox0[3] + 20.0, abs=DRAG_TOL)
    assert _depth(ed) == depth0 + 1

    ed.undo()
    assert _geom(ed) == g0, "undo не вернул рамку"


def test_escape_hides_resize_handles_without_touching_geometry(ed):
    """Esc убирает ручки со сцены и не трогает данные."""
    nid = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, nid)
    _ctrl_down(ed)
    _dclick(ed, x, y, mods=CTRL)
    assert _visible_handles(ed) == 4
    g0, depth0 = _geom(ed), _depth(ed)

    _esc(ed)

    assert _visible_handles(ed) == 0, "ручки размера пережили Esc"
    assert (_geom(ed), _depth(ed)) == (g0, depth0)


def test_plain_double_click_is_inert(ed):
    """Двойной клик БЕЗ Ctrl не делает ничего: ни ручек, ни данных, ни undo."""
    nid = _nodes_with_bbox(ed)[0]
    x, y = _cxy(ed, nid)
    g0, depth0 = _geom(ed), _depth(ed)

    _dclick(ed, x, y, mods=NONE)

    assert _visible_handles(ed) == 0
    assert (_geom(ed), _depth(ed)) == (g0, depth0)

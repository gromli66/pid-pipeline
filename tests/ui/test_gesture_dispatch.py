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
UID_POLY = "089feca2"     # 45 equipment, из них 4 КОНТУРНЫХ; в d74eb9f1 их 0 (замер 1.6)
DRAG_TOL = 4.0            # допуск на округление сцена -> вьюпорт -> сцена


# ── харнесс ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _white_raster(uid, directory):
    """Белый растр размером с корпусную схему (редактор грузит граф + картинку)."""
    path = corpus.graph_path(uid)
    assert path is not None, f"корпус-фикстура {uid} не найдена (tools/corpus.py)"
    h, w = json.loads(path.read_text(encoding="utf-8"))["graph"]["image_size"]
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    png = directory / f"{uid}.png"
    assert img.save(str(png))
    return str(png)


@pytest.fixture(scope="module")
def raster(tmp_path_factory):
    return _white_raster(UID, tmp_path_factory.mktemp("raster"))


@pytest.fixture(scope="module")
def raster_poly(tmp_path_factory):
    return _white_raster(UID_POLY, tmp_path_factory.mktemp("raster_poly"))


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


@pytest.fixture
def ed_poly(qapp, raster_poly):
    """«Ручная правка» на схеме, где есть контурные узлы."""
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    editor = AdvancedGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(raster_poly, str(corpus.graph_path(UID_POLY)))
    editor.resize(1400, 900)
    return editor


@pytest.fixture
def simple_poly(qapp, raster_poly):
    """«Проверка схемы» на той же схеме: у простого редактора свой двойной клик
    (`SimpleGraphEditor.mouseDoubleClickEvent`), который «Ручная правка» перекрывает."""
    from ui.editors.simple_graph_editor import SimpleGraphEditor

    editor = SimpleGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(raster_poly, str(corpus.graph_path(UID_POLY)))
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


def _contour_nodes(editor):
    """Оборудование, у которого ФОРМА — контур, а не рамка (клик по центроиду
    адресован именно ему). Рамка bbox у таких узлов тоже есть — на неё и садились
    ручки размера до 1.6."""
    out = []
    for nid, nd in editor.nodes.items():
        seg = nd.get("segmentation")
        bb = nd.get("bbox")
        if nd.get("type") != "equipment" or not (seg and len(seg) >= 6):
            continue
        if not bb or len(bb) != 4:
            continue
        if editor.find_node_at(*_cxy(editor, nid)) != nid:
            continue
        out.append(nid)
    return out


def _unlinked_pair(editor):
    """Два узла с рамкой, между которыми ещё НЕТ ребра — жест `add_edge` на них
    наблюдаем в данных."""
    nids = _nodes_with_bbox(editor)
    for i, a in enumerate(nids):
        for b in nids[i + 1:]:
            if not editor.model.edge_exists(a, b):
                return a, b
    raise AssertionError("в корпусе нет пары узлов без ребра")


def _tool_click(editor, nid):
    """Клик инструментом по узлу: инструмент получает клик только под Ctrl
    (`base_graph_editor.mousePressEvent:1645`), простой ЛКМ уходит во вьюпорт."""
    x, y = _cxy(editor, nid)
    _press(editor, x, y, mods=CTRL)
    _release(editor, x, y, mods=CTRL)


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


def _handle_centres(editor):
    """Центры ВИДИМЫХ ручек размера, плоским отсортированным списком координат.

    Считаем по самой сцене (квадрат `HANDLE_SIZE` на слое `HANDLE_Z`), а не по
    полю `_resize_overlay`: ручки — это то, что видит оператор, и проверка
    переживёт перенос ресайза с режима на оверлей (пункт 10.1). Центр ручки —
    угол правимой рамки (`resize_overlay._update_handle_positions`), поэтому по
    этому же списку видно, НА КАКОМ узле сидят ручки.
    """
    from ui.editors.resize_overlay import ResizableNodeOverlay as R

    out = []
    for item in editor.scene.items():
        if isinstance(item, QGraphicsRectItem) and item.zValue() == R.HANDLE_Z:
            r = item.rect()
            if abs(r.width() - R.HANDLE_SIZE) < 0.6 and \
                    abs(r.height() - R.HANDLE_SIZE) < 0.6:
                out.append((r.x() + r.width() / 2, r.y() + r.height() / 2))
    return [c for xy in sorted(out) for c in xy]


def _visible_handles(editor):
    """Число ВИДИМЫХ ручек размера на сцене."""
    return len(_handle_centres(editor)) // 2


def _bbox_corners(editor, nid):
    """Четыре угла рамки узла — в том же виде, что отдаёт `_handle_centres`."""
    x1, y1, x2, y2 = editor.nodes[nid]["bbox"]
    return [c for xy in sorted([(x1, y1), (x2, y1), (x1, y2), (x2, y2)])
            for c in xy]


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


# ── пункт 1.6: выход из правки размера возвращает инструмент ──────────────

def test_repeated_double_click_leaves_the_tool_alive_after_escape(ed):
    """Повторный Ctrl+2ЛКМ по узлу, УЖЕ находящемуся в правке размера, не должен
    забирать инструмент навсегда.

    Наблюдается по данным: после Esc жест `add_edge` (два клика по узлам) обязан
    дать ребро. До 1.6 второй двойной клик сохранял 'resize_node' как «предыдущий
    режим», и любой выход возвращал редактор в него же — ребро не появлялось, а
    клик снова открывал ручки (замер: рёбер 63 -> 63 против 63 -> 64 в контроле)."""
    a, b = _unlinked_pair(ed)
    ed.set_mode("add_edge")
    _ctrl_down(ed)

    _dclick(ed, *_cxy(ed, a), mods=CTRL)          # вход в правку размера
    assert _visible_handles(ed) == 4
    _dclick(ed, *_cxy(ed, a), mods=CTRL)          # повтор жеста по тому же узлу
    _esc(ed)
    assert _visible_handles(ed) == 0, "ручки пережили Esc"

    edges0, depth0 = len(ed.edges_data), _depth(ed)
    _tool_click(ed, a)
    _tool_click(ed, b)

    assert len(ed.edges_data) == edges0 + 1, "инструмент не вернулся после Esc"
    assert _depth(ed) == depth0 + 1
    assert _visible_handles(ed) == 0, "клик инструментом снова открыл ручки размера"


def test_escape_returns_the_tool_after_a_single_double_click(ed):
    """Контроль к предыдущему: с ОДНИМ двойным кликом инструмент возвращался и
    до 1.6 — тест заперт с обеих сторон и не зеленеет от сломанного `add_edge`."""
    a, b = _unlinked_pair(ed)
    ed.set_mode("add_edge")
    _ctrl_down(ed)

    _dclick(ed, *_cxy(ed, a), mods=CTRL)
    _esc(ed)

    edges0, depth0 = len(ed.edges_data), _depth(ed)
    _tool_click(ed, a)
    _tool_click(ed, b)

    assert len(ed.edges_data) == edges0 + 1
    assert _depth(ed) == depth0 + 1


# ── пункт 1.6: ручки размера — только по рамке, не по контуру ─────────────

def test_double_click_on_contour_node_gives_no_size_handles(simple_poly):
    """«Проверка схемы»: Ctrl+2ЛКМ по КОНТУРНОМУ узлу не открывает ручки размера.

    Рамка у контурного узла есть, но форма — контур: ручки правили бы bbox и
    центроид, оставляя `segmentation` на месте (замер до 1.6 на node_4 схемы
    089feca2: bbox уехал на 30x20 px, контур не сдвинулся ни на пиксель)."""
    nid = _contour_nodes(simple_poly)[0]
    g0, depth0 = _geom(simple_poly), _depth(simple_poly)
    simple_poly.ctrl_pressed = True

    _dclick(simple_poly, *_cxy(simple_poly, nid), mods=CTRL)

    assert _visible_handles(simple_poly) == 0, "ручки размера сели на контурный узел"
    assert (_geom(simple_poly), _depth(simple_poly)) == (g0, depth0)


def test_click_from_resize_mode_does_not_move_handles_onto_a_contour_node(ed_poly):
    """Вторая дверь: из правки размера рамки клик по контурному узлу не переносит
    ручки на него. Дверь общая для обоих редакторов — она в `ResizeNodeHandler`,
    поэтому развилка по контуру в двойном клике её не закрывает."""
    box = _nodes_with_bbox(ed_poly)[0]
    contour = _contour_nodes(ed_poly)[0]
    _ctrl_down(ed_poly)
    _dclick(ed_poly, *_cxy(ed_poly, box), mods=CTRL)
    assert _visible_handles(ed_poly) == 4
    g0, depth0 = _geom(ed_poly), _depth(ed_poly)

    _tool_click(ed_poly, contour)

    assert _visible_handles(ed_poly) == 0, "ручки размера перешли на контурный узел"
    assert (_geom(ed_poly), _depth(ed_poly)) == (g0, depth0)


def test_click_from_resize_mode_still_switches_between_boxes(ed_poly):
    """Контроль к предыдущему: переключение ручек между РАМОЧНЫМИ узлами — живой
    штатный жест, развилка по контуру не имеет права его гасить."""
    box_a, box_b = _nodes_with_bbox(ed_poly)[:2]
    _ctrl_down(ed_poly)
    _dclick(ed_poly, *_cxy(ed_poly, box_a), mods=CTRL)
    g0, depth0 = _geom(ed_poly), _depth(ed_poly)

    _tool_click(ed_poly, box_b)

    assert _visible_handles(ed_poly) == 4, "ручки не переехали на соседнюю рамку"
    assert _handle_centres(ed_poly) == pytest.approx(_bbox_corners(ed_poly, box_b)), \
        "ручки сидят не на той рамке, по которой кликнули"
    assert (_geom(ed_poly), _depth(ed_poly)) == (g0, depth0)


# ── пункт 1.8: липкий ресайз и брошенная тяга ─────────────────────────────

@pytest.fixture
def simple(qapp, raster):
    """«Проверка схемы» на той же схеме, что `ed`: липкость воспроизводима
    только у простого редактора — «Ручная правка» в режиме ресайза не отдаёт
    press перехвату узла (`_node_drag_allowed` там False вне idle)."""
    from ui.editors.simple_graph_editor import SimpleGraphEditor

    editor = SimpleGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(raster, str(corpus.graph_path(UID)))
    editor.resize(1400, 900)
    return editor


def _edges_full(editor):
    """Полные словари всех рёбер: откат сравнивается ЦЕЛИКОМ, а не по перечню
    полей — так был пойман дефект 1.7 (`_src_side`/`_tgt_side`, заведённые
    ресайзом, переживали восстановление части полей)."""
    return json.dumps([{k: v for k, v in sorted(e.items())}
                       for e in editor.edges_data], sort_keys=True, default=str)


def _free_spot(editor, margin=60.0):
    """Точка холста, где нет ни одного узла в радиусе `margin` — для узла,
    который тест создаёт сам (сцена в canvas-режиме — 1920×1080)."""
    for x in range(80, 1840, 120):
        for y in range(80, 1000, 120):
            if all(((x - nd["centroid"][1]) ** 2
                    + (y - nd["centroid"][0]) ** 2) ** 0.5 > margin
                   for nd in editor.nodes.values()):
                return float(x), float(y)
    raise AssertionError("на холсте нет свободного места под тестовый узел")


def _free_corner_node(editor, need_edges=True):
    """Оборудование с рамкой, у которого правый-нижний угол лежит ВНЕ радиуса
    перехвата всех центроидов (`find_node_at` по углу молчит): press по такому
    углу уходит хендлеру напрямую — настоящая протяжка за ручку. Возвращает
    (nid, (x, y) угла)."""
    for nid, nd in editor.nodes.items():
        bb = nd.get("bbox")
        seg = nd.get("segmentation")
        if nd.get("type") != "equipment" or not bb or len(bb) != 4:
            continue
        if seg and len(seg) >= 6:
            continue                      # контурный: ручки не сядут (1.6)
        if bb[2] - bb[0] <= 20.0 or bb[3] - bb[1] <= 20.0:
            continue
        if editor.find_node_at(*_cxy(editor, nid)) != nid:
            continue
        if need_edges and not any(nid in (e.get("source"), e.get("target"))
                                  for e in editor.edges_data):
            continue                      # откат обязан вернуть и рёбра
        corner = (float(bb[2]), float(bb[3]))
        if editor.find_node_at(*corner) is None:
            return nid, corner
    raise AssertionError("в корпусе нет рамки со свободным правым-нижним углом")


def test_click_on_captured_handle_does_not_start_sticky_resize(simple):
    """«Липкий ресайз» (замер 2026-08-11; механизм — MEASUREMENTS §56): у
    свежедобавленного узла 20×20 ручка живёт в радиусе перехвата
    `find_node_at` (в холсте `CLICK_THRESHOLD` = 8 px от центроида), поэтому
    press по ней уходит отложенным решением клик/drag, а release превращается
    в синтетический `on_press(event=None)` — тяга стартует УЖЕ ПОСЛЕ
    отпускания кнопки. Дальше рамку везёт голый mouseMove (mouseTracking
    включён), и дельта вписывается в undo следующим коммитом, о котором
    оператор не просил.

    Одиночный клик по ручке обязан оставить рамку, глубину undo и сами
    ручки на месте."""
    _ctrl_down(simple)
    x, y = _free_spot(simple)
    nid = simple.add_equipment_node(x, y, 1, "test_eq", width=20, height=20)
    assert _visible_handles(simple) == 4, "ручки не открылись после добавления"
    bbox0 = list(simple.nodes[nid]["bbox"])
    depth0 = _depth(simple)

    # Точка «перехваченной ручки»: на диагонали к правому-нижнему углу,
    # в 5.5 px от центроида — внутри перехвата узла (< 8) и внутри ручки
    # (полудиагональ 14.14 − 5.5 = 8.6 px от угла < 12).
    cx, cy = _cxy(simple, nid)
    gx, gy = bbox0[2], bbox0[3]
    diag = ((gx - cx) ** 2 + (gy - cy) ** 2) ** 0.5
    px = cx + (gx - cx) * (5.5 / diag)
    py = cy + (gy - cy) * (5.5 / diag)
    assert simple.find_node_at(px, py) == nid, \
        "точка не перехвачена узлом — сценарий липкости не собрался, тест слеп"

    _press(simple, px, py, mods=CTRL)
    _release(simple, px, py, mods=CTRL)            # одиночный клик по ручке
    _move(simple, px + 76.0, py + 44.0, mods=CTRL, down=False)
    _move(simple, px + 152.0, py + 88.0, mods=CTRL, down=False)

    assert list(simple.nodes[nid]["bbox"]) == bbox0, \
        "рамка поехала за курсором при отпущенной кнопке (липкий ресайз)"
    assert _depth(simple) == depth0, "движение без кнопки записало шаг undo"
    assert _handle_centres(simple) == pytest.approx(_bbox_corners(simple, nid)), \
        "ручки уехали с углов рамки"


def test_handle_drag_resizes_bbox_in_simple_editor(simple):
    """Контроль к фиксу липкости: НАСТОЯЩАЯ протяжка за ручку (press-move-
    release по свободному углу) в «Проверке схемы» жива — рамка растёт, жест
    ложится одним шагом undo. Фикс не имеет права глушить `start_drag` вовсе."""
    nid, corner = _free_corner_node(simple, need_edges=False)
    _ctrl_down(simple)
    _dclick(simple, *_cxy(simple, nid), mods=CTRL)
    assert _visible_handles(simple) == 4
    bbox0 = list(simple.nodes[nid]["bbox"])
    depth0 = _depth(simple)

    _drag(simple, *corner, 30.0, 20.0)

    bbox1 = simple.nodes[nid]["bbox"]
    assert bbox1[2] == pytest.approx(bbox0[2] + 30.0, abs=DRAG_TOL)
    assert bbox1[3] == pytest.approx(bbox0[3] + 20.0, abs=DRAG_TOL)
    assert _depth(simple) == depth0 + 1, "жест обязан дать ровно один шаг undo"


@pytest.mark.parametrize("editor_fixture", ["ed", "simple"])
def test_escape_mid_drag_reverts_the_uncommitted_resize(request, editor_fixture):
    """«Брошенная тяга» (оба редактора): Esc посреди протяжки за ручку —
    кнопка ещё нажата, коммита не было. Статусная строка обещает отмену,
    значит рамка, центроид, площадь и ПОЛНЫЕ словари рёбер обязаны вернуться
    к состоянию до тяги, шага undo нет, ручки сняты. До 1.8 модель оставалась
    в состоянии последнего кадра тяги при undo=0 и чистом дёрти-флаге
    (замер 2026-08-11)."""
    ed = request.getfixturevalue(editor_fixture)
    nid, corner = _free_corner_node(ed)
    _ctrl_down(ed)
    _dclick(ed, *_cxy(ed, nid), mods=CTRL)
    assert _visible_handles(ed) == 4
    g0, edges0, depth0 = _geom(ed), _edges_full(ed), _depth(ed)

    _press(ed, *corner, mods=CTRL)
    for k in range(1, 5):
        _move(ed, corner[0] + 30.0 * k / 4, corner[1] + 20.0 * k / 4, mods=CTRL)
    assert _geom(ed) != g0, "тяга не изменила данные — жест мимо ручки, тест слеп"

    _esc(ed)                                       # кнопка НЕ отпускалась

    assert _geom(ed) == g0, "Esc не вернул рамку: брошенная тяга осталась в модели"
    assert _edges_full(ed) == edges0, \
        "Esc не вернул рёбра (сравнение словарей целиком — как в 1.7)"
    assert _depth(ed) == depth0, "отменённая тяга записала шаг undo"
    assert _visible_handles(ed) == 0, "ручки пережили Esc"

    _release(ed, corner[0] + 30.0, corner[1] + 20.0, mods=CTRL)
    assert (_geom(ed), _edges_full(ed), _depth(ed)) == (g0, edges0, depth0), \
        "запоздалый release после Esc снова тронул данные"


def test_escape_reverts_only_the_tail_after_a_committed_drag(ed):
    """Точность отката: снимок живёт в трёх точках (вход в режим, commit,
    «start для следующего drag» — 1.7). Esc после «потянул-отпустил-потянул»
    обязан вернуть модель к ЗАКОММИЧЕННОМУ состоянию, а не к входу в режим —
    иначе отмена хвоста молча стирала бы принятую работу. Ctrl+Z затем
    возвращает исходное."""
    nid, corner = _free_corner_node(ed)
    _ctrl_down(ed)
    _dclick(ed, *_cxy(ed, nid), mods=CTRL)
    g0, depth0 = _geom(ed), _depth(ed)

    _drag(ed, *corner, 30.0, 20.0)                 # закоммиченная протяжка
    g1, edges1, depth1 = _geom(ed), _edges_full(ed), _depth(ed)
    assert depth1 == depth0 + 1 and g1 != g0

    bb = ed.nodes[nid]["bbox"]
    new_corner = (float(bb[2]), float(bb[3]))
    assert ed.find_node_at(*new_corner) is None, \
        "после первой протяжки угол попал под перехват — обстановка не собралась"
    _press(ed, *new_corner, mods=CTRL)
    _move(ed, new_corner[0] + 40.0, new_corner[1], mods=CTRL)
    assert _geom(ed) != g1, "вторая тяга не началась (иначе тест пуст)"

    _esc(ed)

    assert _geom(ed) == g1, "Esc откатил дальше последнего коммита"
    assert _edges_full(ed) == edges1
    assert _depth(ed) == depth1, "Esc тронул стек undo"

    ed.undo()
    assert _geom(ed) == g0, "Ctrl+Z после отменённого хвоста не вернул исходное"

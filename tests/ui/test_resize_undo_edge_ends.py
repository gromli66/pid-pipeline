# -*- coding: utf-8 -*-
"""Пункт 1.7 дороги — Ctrl+Z после правки размера обязан вернуть КОНЦЫ рёбер.

Замер до правки (MEASUREMENTS §54, корпус `d74eb9f1`, «Ручная правка»): протяжка
угловой ручки на +30/+20 px пересаживает концы инцидентных рёбер
(`_reseat_after_resize`), а `ResizeNodeCommand.undo` возвращает только рамку,
центроид, площадь и пины Э5 — маршрут ребра остаётся в состоянии «после
ресайза». Числа: узел `node_2`, ребро `node_2 -> node_3`, `source_point`
[13, 1547] -> [27.2555…, 1577.7918…] и обратно НЕ возвращается
(Δ = 14.26 px по y, 30.79 px по x).

Почему проверяется по ДАННЫМ, а не по полям редактора (принцип набора 0.4):
`_resizing_node`/`_resize_start_*` уезжают вместе с декомпозицией UI (этап 10),
а `source_point`/`target_point`/`waypoints` рёбер — это то, что уходит на сервер.

Ловушки, замеренные при написании:
  • ресайз мутирует у инцидентного ребра ПЯТЬ полей, а не два:
    `source_point`, `target_point`, `waypoints`, `_src_side`, `_tgt_side`
    (замер: сравнение полных словарей рёбер до/после жеста). Тест сравнивает
    словарь ЦЕЛИКОМ — иначе восстановление части полей выдаст себя за откат;
  • вход в правку размера — только Ctrl+2ЛКМ по рамке, и «Ручная правка»
    переопределяет `_reseat_after_resize` движком, поэтому проверять надо
    на `AdvancedGraphEditor`: у простого редактора другой путь посадки;
  • `mapFromScene` округляет до целого пикселя вьюпорта — на дельту жеста
    допуск, но на РЕЗУЛЬТАТ отката допуска нет: undo обязан вернуть данные
    побайтово, как это уже требуется от рамки узла.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication                        # noqa: E402
from PySide6.QtCore import Qt, QPointF, QEvent                    # noqa: E402
from PySide6.QtGui import QMouseEvent, QKeyEvent, QImage, QColor  # noqa: E402

from tools import corpus                                          # noqa: E402

CTRL = Qt.KeyboardModifier.ControlModifier
NONE = Qt.KeyboardModifier.NoModifier
LMB = Qt.MouseButton.LeftButton

UID = "d74eb9f1"          # 66 узлов / 63 ребра, корпус-фикстура в git
DX, DY = 30.0, 20.0       # дельта протяжки угловой ручки
DRAG_TOL = 4.0            # допуск сцена -> вьюпорт -> сцена (замер 0.4)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def raster(tmp_path_factory):
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
    """«Ручная правка» с корпусной схемой — так её собирает вкладка."""
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    editor = AdvancedGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(raster, str(corpus.graph_path(UID)))
    editor.resize(1400, 900)
    return editor


def _ev(kind, sx, sy, editor, buttons=LMB, button=LMB, mods=NONE):
    vp = QPointF(editor.mapFromScene(QPointF(sx, sy)))
    kinds = {"press": QEvent.Type.MouseButtonPress,
             "move": QEvent.Type.MouseMove,
             "release": QEvent.Type.MouseButtonRelease,
             "dclick": QEvent.Type.MouseButtonDblClick}
    return QMouseEvent(kinds[kind], vp, vp, button, buttons, mods)


def _ctrl_down(editor):
    editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Control, NONE))


def _dclick(editor, x, y):
    editor.mouseDoubleClickEvent(_ev("dclick", x, y, editor, mods=CTRL))


def _drag(editor, x0, y0, dx, dy, steps=4):
    editor.mousePressEvent(_ev("press", x0, y0, editor, mods=CTRL))
    for k in range(1, steps + 1):
        editor.mouseMoveEvent(_ev("move", x0 + dx * k / steps, y0 + dy * k / steps,
                                  editor, mods=CTRL, button=Qt.MouseButton.NoButton))
    editor.mouseReleaseEvent(_ev("release", x0 + dx, y0 + dy, editor,
                                 buttons=Qt.MouseButton.NoButton, mods=CTRL))


def _degree(editor, nid):
    return sum(1 for e in editor.edges_data
               if nid in (e.get("source"), e.get("target")))


def _resizable_node(editor, degree):
    """Оборудование с настоящей рамкой, по центроиду которого клик попадает
    именно в него, и ровно с нужным числом инцидентных рёбер."""
    for nid, nd in editor.nodes.items():
        bb = nd.get("bbox")
        if nd.get("type") != "equipment" or not bb or len(bb) != 4:
            continue
        if bb[2] - bb[0] <= 20.0 or bb[3] - bb[1] <= 20.0:
            continue
        if editor.find_node_at(float(nd["centroid"][1]),
                               float(nd["centroid"][0])) != nid:
            continue
        if _degree(editor, nid) != degree:
            continue
        return nid
    return None


def _edges_snapshot(editor):
    """Полные словари всех рёбер — сравниваются целиком, а не по трём полям."""
    return json.dumps([{k: v for k, v in sorted(e.items())}
                       for e in editor.edges_data], sort_keys=True, default=str)


def _incident_dicts(editor, nid):
    return [dict(e) for e in editor.edges_data
            if nid in (e.get("source"), e.get("target"))]


def _enter_resize(editor, nid):
    _ctrl_down(editor)
    _dclick(editor, float(editor.nodes[nid]["centroid"][1]),
            float(editor.nodes[nid]["centroid"][0]))


@pytest.mark.parametrize("degree", [1, 2])
def test_undo_after_resize_restores_edge_ends(ed, degree):
    """Гейт пункта 1.7: Ctrl+Z после протяжки ручки возвращает и рамку узла,
    и концы всех инцидентных рёбер — побайтово, к состоянию до жеста."""
    nid = _resizable_node(ed, degree)
    assert nid is not None, f"в корпусе {UID} нет правимой рамки степени {degree}"

    bbox0 = list(ed.nodes[nid]["bbox"])
    edges0 = _edges_snapshot(ed)
    depth0 = ed.undo_mgr.stack_depth

    _enter_resize(ed, nid)
    _drag(ed, bbox0[2], bbox0[3], DX, DY)

    # жест действительно состоялся: рамка выросла и концы рёбер уехали
    bbox1 = ed.nodes[nid]["bbox"]
    assert bbox1[2] == pytest.approx(bbox0[2] + DX, abs=DRAG_TOL)
    assert bbox1[3] == pytest.approx(bbox0[3] + DY, abs=DRAG_TOL)
    assert ed.undo_mgr.stack_depth == depth0 + 1, "resize не записал шаг undo"
    assert _edges_snapshot(ed) != edges0, \
        "ресайз не тронул ни одного ребра — жест адресован не туда, тест слеп"

    ed.undo()

    assert list(ed.nodes[nid]["bbox"]) == bbox0, "undo не вернул рамку узла"
    assert ed.undo_mgr.stack_depth == depth0
    assert _edges_snapshot(ed) == edges0, "undo не вернул концы рёбер"


def test_resize_touches_exactly_the_incident_edges(ed):
    """Контроль области: жест меняет ровно рёбра правимого узла, и после
    Ctrl+Z ни у одного из них не остаётся ни одного изменённого поля."""
    nid = _resizable_node(ed, 2)
    assert nid is not None

    before = {(e["source"], e["target"]): e for e in _incident_dicts(ed, nid)}
    others0 = json.dumps([{k: v for k, v in sorted(e.items())}
                          for e in ed.edges_data
                          if nid not in (e.get("source"), e.get("target"))],
                         sort_keys=True, default=str)
    bbox0 = list(ed.nodes[nid]["bbox"])

    _enter_resize(ed, nid)
    _drag(ed, bbox0[2], bbox0[3], DX, DY)

    moved = [k for k, b in before.items()
             for e in _incident_dicts(ed, nid)
             if (e["source"], e["target"]) == k and e != b]
    assert len(moved) == 2, \
        f"ресайз узла степени 2 тронул {len(moved)} рёбер, а не 2"

    ed.undo()

    stale = {}
    for e in _incident_dicts(ed, nid):
        b = before[(e["source"], e["target"])]
        diff = sorted(k for k in set(b) | set(e) if b.get(k) != e.get(k))
        if diff:
            stale[(e["source"], e["target"])] = diff
    assert stale == {}, f"после undo у рёбер остались изменённые поля: {stale}"

    others1 = json.dumps([{k: v for k, v in sorted(e.items())}
                          for e in ed.edges_data
                          if nid not in (e.get("source"), e.get("target"))],
                         sort_keys=True, default=str)
    assert others1 == others0, "жест тронул рёбра, не касающиеся правимого узла"

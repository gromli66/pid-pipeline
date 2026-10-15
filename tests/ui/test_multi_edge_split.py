# -*- coding: utf-8 -*-
"""Две трубы между одной парой узлов — обе живые, призраков нет.

Ключ ребра в редакторе — пара узлов, поэтому вторая труба между теми же
аппаратами (обычное дело: два патрубка теплообменника) своего адреса не
получала. Замер до правки (граф оператора ae30deb4, 3 такие пары): холст
рисовал обе линии, `edge_items` знал одну; `remove_edge` сносил из данных обе
записи, а со сцены одну — оставшаяся линия не удалялась ничем; undo возвращал
одну трубу из двух. Лечение — разрез второй трубы коннектором при загрузке.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QGraphicsPathItem   # noqa: E402
from PySide6.QtGui import QImage, QColor                        # noqa: E402

IMG_W, IMG_H = 800, 600


def _node(nid, y, x, kind="equipment"):
    return {"id": nid, "type": kind, "centroid": [float(y), float(x)],
            "bbox": [x - 40.0, y - 40.0, x + 40.0, y + 40.0] if kind == "equipment" else None,
            "segmentation": None,
            "class_id": 1 if kind == "equipment" else -1,
            "class_name": "nasos" if kind == "equipment" else "connector",
            "degree": 1}


def _link(eid, a, b, sy, sx, ty, tx, waypoints=None):
    return {"id": eid, "source": a, "target": b,
            "source_point": [float(sy), float(sx)],
            "target_point": [float(ty), float(tx)],
            "waypoints": waypoints or []}


def _graph():
    """a—b ДВЕ трубы (низом и верхом), a—c одна."""
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": [_node("a", 150, 150), _node("b", 150, 500), _node("c", 450, 150)],
            "links": [_link("e1", "a", "b", 120, 190, 120, 460),
                      _link("e2", "a", "b", 180, 190, 180, 460),
                      _link("e3", "a", "c", 190, 150, 410, 150)],
            "text_blocks": [], "bindings": []}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _editor(qapp, tmp_path, graph=None):
    from ui.editors.simple_graph_editor import SimpleGraphEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph or _graph()), encoding="utf-8")

    ed = SimpleGraphEditor()
    assert ed.load_data(str(ip), str(gp))
    return ed


def _lines(ed):
    """Линии рёбер на сцене (у ребра Z=1)."""
    return sum(1 for it in ed.scene.items()
               if isinstance(it, QGraphicsPathItem) and it.zValue() == 1)


# ── разрез ───────────────────────────────────────────────────────────────

def test_second_pipe_gets_its_own_keys(qapp, tmp_path):
    """Вторая труба разрезана коннектором: кратных пар не остаётся."""
    ed = _editor(qapp, tmp_path)
    assert ed.model.multi_edges_split == 1
    keys = [ed.model.edge_key(e["source"], e["target"]) for e in ed.model.edges_data]
    assert len(keys) == len(set(keys)) == 4      # a—b, a—c и две половины
    assert len(ed.nodes) == 4                    # добавился один коннектор


def test_split_keeps_the_geometry(qapp, tmp_path):
    """Коннектор садится НА трубу — концы половин совпадают с исходными."""
    ed = _editor(qapp, tmp_path)
    halves = [e for e in ed.model.edges_data
              if e["source"].startswith("node_manual") or e["target"].startswith("node_manual")]
    assert len(halves) == 2
    head = next(h for h in halves if h["source"] == "a")
    tail = next(h for h in halves if h["target"] == "b")
    assert head["source_point"] == [180.0, 190.0]      # начало исходной трубы
    assert tail["target_point"] == [180.0, 460.0]      # её конец
    assert head["target_point"] == tail["source_point"] == [180.0, 325.0]


def test_split_is_idempotent(qapp, tmp_path):
    """Повторная загрузка уже разрезанного графа ничего не режет."""
    ed = _editor(qapp, tmp_path)
    saved = tmp_path / "saved.json"
    assert ed.model.save(str(saved))

    from ui.editors.graph_data import GraphDataModel
    again = GraphDataModel()
    assert again.load(str(saved))
    assert again.multi_edges_split == 0


def test_waypoints_survive_the_split(qapp, tmp_path):
    """У трубы с изломами коннектор садится на средний излом."""
    g = _graph()
    g["links"][1]["waypoints"] = [[180.0, 300.0], [200.0, 350.0], [180.0, 400.0]]
    ed = _editor(qapp, tmp_path, g)
    halves = [e for e in ed.model.edges_data
              if e["source"].startswith("node_manual") or e["target"].startswith("node_manual")]
    head = next(h for h in halves if h["source"] == "a")
    tail = next(h for h in halves if h["target"] == "b")
    assert head["waypoints"] == [[180.0, 300.0]]
    assert head["target_point"] == [200.0, 350.0]      # средний излом
    assert tail["waypoints"] == [[180.0, 400.0]]


# ── призраки ─────────────────────────────────────────────────────────────

def test_no_unmanaged_line_on_the_canvas(qapp, tmp_path):
    """Линий на холсте ровно столько, сколько рёбер, и все под учётом."""
    ed = _editor(qapp, tmp_path)
    assert _lines(ed) == len(ed.model.edges) == len(ed.edge_items) == 4


def test_deleting_one_pipe_leaves_no_line_and_keeps_the_other(qapp, tmp_path):
    """Удаление половины второй трубы не трогает первую и не оставляет линию."""
    ed = _editor(qapp, tmp_path)
    half = next(e for e in ed.model.edges_data if e["source"] == "a"
                and e["target"].startswith("node_manual"))
    assert ed.remove_edge(half["source"], half["target"])

    assert _lines(ed) == len(ed.model.edges) == 3
    assert ed.model.edge_exists("a", "b")        # первая труба на месте

    ed.undo()
    assert _lines(ed) == len(ed.model.edges) == 4


def test_deleting_the_node_takes_both_pipes_with_it(qapp, tmp_path):
    """Удалён узел — ни одна из его труб не осталась висеть на холсте."""
    ed = _editor(qapp, tmp_path)
    assert ed.delete_node("b")
    assert _lines(ed) == len(ed.model.edges)

    ed.undo()
    assert _lines(ed) == len(ed.model.edges) == 4


def test_edge_item_is_never_duplicated(qapp, tmp_path):
    """Один ключ — один предмет: повторная отрисовка не плодит линию."""
    ed = _editor(qapp, tmp_path)
    before = _lines(ed)
    key = ed.model.edge_key("a", "c")
    ed.create_edge_item(key, ed.model.find_edge_data(key))
    assert _lines(ed) == before

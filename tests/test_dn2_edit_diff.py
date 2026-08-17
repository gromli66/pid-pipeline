# -*- coding: utf-8 -*-
"""Классификатор правок оператора (ДН2, `tools/dn2_edit_diff.py`).

Числа отчёта 0.9 стоят на одном различении: намерение оператора против его
следствия. Конец ребра, переехавший вслед за своим узлом, и пересчитанный
`waypoints` — не отдельные правки; посчитать их наравне с перемещением узла
значит утроить вес одного действия. Тесты фиксируют именно это, а не пороги.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.dn2_edit_diff import EPS, INTENT, diff_pair  # noqa: E402


def _canvas(nodes, links):
    return {"nodes": nodes, "links": links}


def _node(nid, y, x, w=10.0, h=10.0, ntype="equipment"):
    """Узел холста: центроид `[y, x]`, bbox `[x1, y1, x2, y2]` — оси разные."""
    return {"id": nid, "type": ntype, "centroid": [y, x],
            "bbox": [x - w / 2, y - h / 2, x + w / 2, y + h / 2]}


def _edge(eid, src, tgt, sp, tp, waypoints=None):
    return {"id": eid, "source": src, "target": tgt,
            "source_point": list(sp), "target_point": list(tp),
            "waypoints": waypoints or []}


def test_node_move_drags_its_edge_end_and_that_is_not_a_second_edit():
    before = _canvas([_node("n1", 100.0, 100.0), _node("n2", 100.0, 300.0)],
                     [_edge("e1", "n1", "n2", (100.0, 105.0), (100.0, 295.0))])
    after = _canvas([_node("n1", 140.0, 100.0), _node("n2", 100.0, 300.0)],
                    [_edge("e1", "n1", "n2", (140.0, 105.0), (100.0, 295.0))])

    d = diff_pair(before, after)

    assert d["node_moved"] == 1
    assert d["edge_end_follow"] == 1
    assert d["edge_end_own"] == 0
    assert sum(d[k] for k in INTENT) == 1


def test_end_moved_without_node_move_is_an_operator_intent():
    before = _canvas([_node("n1", 100.0, 100.0), _node("n2", 100.0, 300.0)],
                     [_edge("e1", "n1", "n2", (100.0, 105.0), (100.0, 295.0))])
    after = _canvas([_node("n1", 100.0, 100.0), _node("n2", 100.0, 300.0)],
                    [_edge("e1", "n1", "n2", (95.0, 105.0), (100.0, 295.0))])

    d = diff_pair(before, after)

    assert (d["edge_end_own"], d["edge_end_follow"], d["node_moved"]) == (1, 0, 0)
    assert sum(d[k] for k in INTENT) == 1


def test_resize_is_counted_apart_from_move_and_also_drags_the_end():
    before = _canvas([_node("n1", 100.0, 100.0, w=10.0, h=10.0)],
                     [_edge("e1", "n1", "n1", (100.0, 105.0), (100.0, 105.0))])
    after = _canvas([_node("n1", 100.0, 100.0, w=40.0, h=10.0)],
                    [_edge("e1", "n1", "n1", (100.0, 120.0), (100.0, 120.0))])

    d = diff_pair(before, after)

    assert (d["node_resized"], d["node_moved"]) == (1, 0)
    assert (d["edge_end_follow"], d["edge_end_own"]) == (1, 0)


def test_reroute_is_a_cache_not_an_intent():
    before = _canvas([_node("n1", 100.0, 100.0), _node("n2", 100.0, 300.0)],
                     [_edge("e1", "n1", "n2", (100.0, 105.0), (100.0, 295.0))])
    after = _canvas([_node("n1", 100.0, 100.0), _node("n2", 100.0, 300.0)],
                    [_edge("e1", "n1", "n2", (100.0, 105.0), (100.0, 295.0),
                           waypoints=[[120.0, 200.0]])])

    d = diff_pair(before, after)

    assert d["edge_rerouted"] == 1
    assert "edge_rerouted" not in INTENT
    assert sum(d[k] for k in INTENT) == 0


def test_add_remove_and_rewire_are_counted_by_id():
    before = _canvas([_node("n1", 100.0, 100.0), _node("n2", 100.0, 300.0)],
                     [_edge("e1", "n1", "n2", (100.0, 105.0), (100.0, 295.0)),
                      _edge("e2", "n1", "n2", (100.0, 105.0), (100.0, 295.0))])
    after = _canvas([_node("n1", 100.0, 100.0), _node("n3", 500.0, 500.0)],
                    [_edge("e1", "n1", "n3", (100.0, 105.0), (100.0, 295.0))])

    d = diff_pair(before, after)

    assert (d["node_added"], d["node_removed"]) == (1, 1)
    assert (d["edge_added"], d["edge_removed"]) == (0, 1)
    assert d["edge_rewired"] == 1


def test_subpixel_jitter_is_not_an_edit():
    before = _canvas([_node("n1", 100.0, 100.0)], [])
    after = _canvas([_node("n1", 100.0 + EPS / 2, 100.0)], [])

    assert diff_pair(before, after)["node_moved"] == 0

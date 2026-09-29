"""Кратные рёбра (Р7): `modules/graph/core/parallel_edges.py`, правило на синтетике.

Корпусный замер (26 storage-графов: пары 27 -> 0 в builder, 26 -> 0 в to_canvas)
живёт в MEASUREMENTS.md — корпуса нет в git. Координаты точек — [y, x].
"""
import re
from copy import deepcopy

from modules.graph.core.parallel_edges import DUP_PX, normalize_parallel_edges

CANVAS = ("source", "target")
BUILDER = ("from", "to")


def node(nid, cls="connector", **kw):
    return dict({"id": nid, "type": "connector" if cls == "connector" else "equipment",
                 "class_name": cls, "centroid": [0, 0], "degree": 0}, **kw)


def cedge(eid, a, b, pa, pb, **kw):
    return dict({"id": eid, "source": a, "target": b, "source_point": list(pa),
                 "target_point": list(pb), "waypoints": []}, **kw)


def degree(edges, nid, keys=CANVAS):
    return sum((e.get(keys[0]) == nid) + (e.get(keys[1]) == nid) for e in edges)


def pairs(edges, keys=CANVAS):
    seen = {}
    for e in edges:
        a, b = e.get(keys[0]), e.get(keys[1])
        if a is None or b is None or a == b:
            continue
        seen[frozenset((a, b))] = seen.get(frozenset((a, b)), 0) + 1
    return sum(v > 1 for v in seen.values())


def ab(cls_a="nasos", cls_b="nasos"):
    return [node("A", cls_a), node("B", cls_b)]


# T1 — дубль удаляется, целым остаётся меньший числовой id, а не первый в файле
def test_t1_dup_removed_lower_id_kept():
    nodes = ab()
    edges = [cedge("edge_12", "A", "B", (100, 3), (100, 205)),
             cedge("edge_5", "A", "B", (100, 0), (100, 200))]
    st = normalize_parallel_edges(nodes, edges, keys=CANVAS)
    assert [e["id"] for e in edges] == ["edge_5"]
    assert len(nodes) == 2
    assert (st["deleted"], st["split"], st["deleted_ids"]) == (1, 0, ["edge_12"])


# T2 — порог с ОБЕИХ сторон, строго меньше DUP_PX
def test_t2_threshold_both_ends():
    assert DUP_PX == 8.0
    nodes = ab()
    edges = [cedge("edge_1", "A", "B", (100, 0), (100, 200)),
             cedge("edge_2", "A", "B", (107.9, 0), (107.9, 200))]
    st = normalize_parallel_edges(nodes, edges, keys=CANVAS)
    assert (len(edges), len(nodes), st["deleted"]) == (1, 2, 1)

    nodes = ab()
    edges = [cedge("edge_1", "A", "B", (100, 0), (100, 200)),
             cedge("edge_2", "A", "B", (108.0, 0), (100, 200))]
    st = normalize_parallel_edges(nodes, edges, keys=CANVAS)
    assert (len(edges), len(nodes), st["deleted"], st["split"]) == (3, 3, 0, 1)


# T3 — настоящая вторая труба: connector посередине, поля ребра в обеих половинах
def test_t3_second_pipe_split_keeps_fields():
    nodes = ab()
    edges = [cedge("edge_1", "A", "B", (100, 0), (100, 200)),
             cedge("edge_2", "A", "B", (100, 0), (235, 200),
                   diameter_value=50, render_color="#ff0000")]
    old_ids = {e["id"] for e in edges}
    st = normalize_parallel_edges(nodes, edges, keys=CANVAS)
    assert len(edges) == 3 and pairs(edges) == 0
    conn = next(n for n in nodes if n["id"] == "parconn_1")
    assert conn["type"] == "connector" and conn["class_name"] == "connector"
    assert degree(edges, "parconn_1") == 2
    first = next(e for e in edges if e["id"] == "edge_2")
    second = next(e for e in edges if e["id"] not in old_ids)
    assert re.fullmatch(r"edge_\d+", second["id"]) and second["id"] not in old_ids
    assert (first["source"], first["target"]) == ("A", "parconn_1")
    assert (second["source"], second["target"]) == ("parconn_1", "B")
    for half in (first, second):
        assert half["diameter_value"] == 50 and half["render_color"] == "#ff0000"
    assert first["target_point"] == second["source_point"] == conn["centroid"]
    assert conn["centroid"] == [167, 100]          # середина хорды (100,0)-(235,200)
    assert st["new_nodes"] == ["parconn_1"] and st["new_edges"] == [second["id"]]


# T4 — у пары с unknow дубль не удаляется никогда, только рассекается
def test_t4_unknow_never_deleted():
    for cls_a, cls_b in (("unknow", "nasos"), ("nasos", "unknow")):
        nodes = ab(cls_a, cls_b)
        edges = [cedge("edge_1", "A", "B", (100, 0), (100, 200)),
                 cedge("edge_2", "A", "B", (100, 0), (100, 200))]
        st = normalize_parallel_edges(nodes, edges, keys=CANVAS)
        assert (len(edges), st["deleted"], st["split"], st["split_unknow_dup"]) == (3, 0, 1, 1)
        assert pairs(edges) == 0

    nodes = ab("unknow", "nasos")
    edges = [cedge(f"edge_{i}", "A", "B", (100, 0), (100, 200)) for i in (1, 2, 3)]
    st = normalize_parallel_edges(nodes, edges, keys=CANVAS)
    assert (len(edges), st["deleted"], st["split"]) == (5, 0, 2)
    assert pairs(edges) == 0


# T5 — тройка без unknow: один дубль удалён, одна нитка рассечена
def test_t5_triple_without_unknow():
    nodes = ab()
    edges = [cedge("edge_1", "A", "B", (100, 0), (100, 200)),
             cedge("edge_2", "A", "B", (102, 0), (102, 200)),
             cedge("edge_3", "A", "B", (100, 0), (300, 200))]
    st = normalize_parallel_edges(nodes, edges, keys=CANVAS)
    assert (len(edges), len(nodes)) == (3, 3)
    assert (st["deleted_ids"], st["split_ids"]) == (["edge_2"], ["edge_3"])


# T6 — идемпотентность
def test_t6_idempotent():
    nodes = ab()
    edges = [cedge("edge_1", "A", "B", (100, 0), (100, 200)),
             cedge("edge_2", "A", "B", (102, 0), (102, 200)),
             cedge("edge_3", "A", "B", (100, 0), (300, 200))]
    normalize_parallel_edges(nodes, edges, keys=CANVAS)
    n1, e1 = deepcopy(nodes), deepcopy(edges)
    st = normalize_parallel_edges(nodes, edges, keys=CANVAS)
    assert (nodes, edges) == (n1, e1)
    assert all(not v for v in st.values())


# T7 — граф без пар не трогается, даже протухшая степень
def test_t7_noop_keeps_stale_degree():
    nodes = ab() + [node("C", degree=7)]
    edges = [cedge("edge_1", "A", "B", (100, 0), (100, 200)),
             cedge("edge_2", "B", "C", (100, 200), (100, 400))]
    n0, e0 = deepcopy(nodes), deepcopy(edges)
    st = normalize_parallel_edges(nodes, edges, keys=CANVAS)
    assert (nodes, edges) == (n0, e0)
    assert nodes[2]["degree"] == 7 and st["pairs"] == 0


# T8 — терминальный конец и петля — не пары
def test_t8_terminal_and_loop_untouched():
    nodes = [node("A")]
    edges = [{"id": "edge_1", "from": "A", "to": None, "path": [[0, 0], [0, 5], [0, 9]]},
             {"id": "edge_2", "from": "A", "to": None, "path": [[0, 0], [0, 5], [0, 9]]},
             {"id": "edge_3", "from": "A", "to": "A", "path": [[0, 0], [5, 5], [0, 0]]},
             {"id": "edge_4", "from": "A", "to": "A", "path": [[0, 0], [5, 5], [0, 0]]}]
    e0 = deepcopy(edges)
    st = normalize_parallel_edges(nodes, edges, keys=BUILDER)
    assert edges == e0 and len(nodes) == 1 and st["pairs"] == 0


# T9 — формат builder: рассечение по середине path, id первой половины исходный
def test_t9_builder_path_split():
    nodes = [node("A", "nasos"), node("B", "nasos")]
    straight = [[100, x] for x in range(0, 201, 2)]
    u_path = [[100 + i, 0] for i in range(50)] + [[150, 0]] + \
        [[150 - i, 200] for i in range(49, -1, -1)]
    assert len(u_path) == 101
    edges = [{"id": "edge_1", "from": "A", "to": "B", "source_point": [100, 0],
              "target_point": [100, 200], "path": straight},
             {"id": "edge_7", "from": "A", "to": "B", "source_point": [100, 0],
              "target_point": [150, 200], "path": u_path, "diameter_value": 80}]
    st = normalize_parallel_edges(nodes, edges, keys=BUILDER)
    assert st["split"] == 1 and len(edges) == 3
    conn = nodes[-1]
    assert conn["id"] == "parconn_1" and conn["centroid"] == u_path[50]
    first = next(e for e in edges if e["id"] == "edge_7")
    second = next(e for e in edges if e["id"] == st["new_edges"][0])
    assert st["new_edges"] == ["edge_8"]
    assert len(first["path"]) == len(second["path"]) == 51
    assert (first["from"], first["to"], second["from"], second["to"]) == \
        ("A", "parconn_1", "parconn_1", "B")
    assert first["diameter_value"] == second["diameter_value"] == 80


# T10 — без точек прикрепления резать нечем: не режем, считаем
def test_t10_no_points_skipped():
    nodes = ab()
    edges = [{"id": "edge_1", "source": "A", "target": "B"},
             {"id": "edge_2", "source": "A", "target": "B"}]
    e0 = deepcopy(edges)
    st = normalize_parallel_edges(nodes, edges, keys=CANVAS)
    assert edges == e0 and len(nodes) == 2
    assert (st["skipped"], st["split"], st["deleted"]) == (1, 0, 0)

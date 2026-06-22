"""
Тесты пост-процесса direction-узлов (napravlenie как узел графа).

Запуск:  python tests/test_direction_nodes.py   |   pytest tests/test_direction_nodes.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.graph.core.direction_nodes import (  # noqa: E402
    paint_direction_boxes_on_mask, carve_boxes_from_mask,
    annotate_direction_nodes, apply_direction_rules, cap_dangling_ends,
    stitch_collinear_stubs, drop_degenerate_stubs, collapse_straight_connectors,
    detach_degenerate_box_stubs, set_direction_pass_through, NAPRAVLENIE_CLASS_ID,
)

BOX = (100, 100, 160, 140)  # (x_min,y_min,x_max,y_max)


def _ann(direction, box=BOX, idx=1):
    return {"class_id": NAPRAVLENIE_CLASS_ID, "idx": idx, "bbox": box,
            "attributes": {"direction": direction}}


def _dnode(nid, d, cx=130, cy=120):
    return {"id": nid, "type": "equipment", "class_id": 40, "class_name": "napravlenie",
            "centroid": [cy, cx], "bbox": list(BOX), "degree": 0,
            "direction_node": True, "flow_direction": d}


def _edge(eid, frm, to, sp, tp, path=None, length=None):
    return {"id": eid, "from": frm, "to": to, "source_point": sp, "target_point": tp,
            "path": path or [sp, tp], "length": length if length is not None else 2,
            "is_terminal": to is None, "color": None}


def test_paint_and_carve():
    import numpy as np
    eq = np.zeros((200, 200), dtype=bool)
    conn = np.ones((200, 200), dtype=bool)
    assert paint_direction_boxes_on_mask(eq, [_ann("right")]) == 1
    assert carve_boxes_from_mask(conn, [_ann("right")]) == 1
    assert eq[100:140, 100:160].all() and not conn[100:140, 100:160].any()


def test_annotate_keeps_equipment_type():
    nodes = [{"id": "N", "type": "equipment", "class_id": 40, "centroid": [120, 130]}]
    annotate_direction_nodes(nodes, [], [_ann("right")])
    assert nodes[0]["type"] == "equipment" and nodes[0]["direction_node"] is True
    assert nodes[0]["flow_direction"] == "right"


def test_axis_edges_kept_with_roles():
    nodes = [_dnode("N", "right"), {"id": "a"}, {"id": "b"}]
    edges = [_edge("e0", "a", "N", [120, 40], [120, 101]),
             _edge("e1", "N", "b", [120, 159], [120, 220])]
    st = apply_direction_rules(nodes, edges)
    e_in = [e for e in edges if e["to"] == "N"][0]
    e_out = [e for e in edges if e["from"] == "N"][0]
    assert st["axis_kept"] == 2
    assert e_in["flow_role"] == "in" and e_in["direction"] == "right"
    assert e_out["flow_role"] == "out"


def test_single_perpendicular_to_connector_not_lost():
    # бокс down; реальный перпендикуляр-выход (RIGHT) → connector->far, НЕ потерян
    nodes = [_dnode("N", "down"), {"id": "top"}, {"id": "far"}]
    edges = [_edge("ein", "top", "N", [40, 130], [101, 130], length=60),
             _edge("eout", "far", "N", [130, 400], [130, 160], length=240)]
    st = apply_direction_rules(nodes, edges)
    assert any(e["to"] == "N" and e.get("flow_role") == "in" for e in edges)
    eo = [e for e in edges if e["id"] == "eout"][0]
    assert eo["from"] == "far" and eo["to"] != "N" and str(eo["to"]).startswith("dirconn_")
    assert st["perp_capped"] == 1


def test_box_noise_stub_dropped():
    nodes = [_dnode("N", "down"), {"id": "top"}, {"id": "far"}]
    edges = [_edge("ein", "top", "N", [40, 130], [101, 130], length=60),
             _edge("noiseL", "N", None, [139, 110], [139, 95], length=15),
             _edge("eout", "far", "N", [130, 400], [130, 160], length=240)]
    st = apply_direction_rules(nodes, edges)
    assert st["noise_dropped"] == 1
    ids = {e["id"] for e in edges}
    assert "noiseL" not in ids
    eo = [e for e in edges if e["id"] == "eout"][0]
    assert eo["to"] != "N" and str(eo["to"]).startswith("dirconn_")


def test_detach_box_stub_from_perp_connector():
    # короткий стаб бокс->connector перпендикулярной трубы (degree 3) отцепляется
    box = _dnode("BOX", "down")
    conn = {"id": "C", "type": "connector", "centroid": [120, 165]}
    nodes = [box, conn, {"id": "L"}, {"id": "R"}]
    edges = [
        {"id": "stub", "from": "BOX", "to": "C", "source_point": [120, 160],
         "target_point": [120, 165], "length": 6},
        {"id": "p1", "from": "L", "to": "C", "source_point": [120, 60],
         "target_point": [120, 165], "length": 100},
        {"id": "p2", "from": "C", "to": "R", "source_point": [120, 165],
         "target_point": [120, 300], "length": 130},
    ]
    st = detach_degenerate_box_stubs(nodes, edges)
    assert st["detached"] == 1
    assert "stub" not in {e["id"] for e in edges}


def test_detach_keeps_axis_role_edge():
    # осевое ребро (flow_role) к боксу не отцепляется, даже если короткое
    box = _dnode("BOX", "down")
    conn = {"id": "C", "type": "connector", "centroid": [120, 165]}
    nodes = [box, conn, {"id": "R"}]
    edges = [
        {"id": "axis", "from": "BOX", "to": "C", "flow_role": "out",
         "source_point": [120, 160], "target_point": [120, 165], "length": 6},
        {"id": "p2", "from": "C", "to": "R", "source_point": [120, 165],
         "target_point": [120, 300], "length": 130},
        {"id": "p3", "from": "C", "to": "R", "source_point": [120, 165],
         "target_point": [300, 165], "length": 130},
    ]
    assert detach_degenerate_box_stubs(nodes, edges)["detached"] == 0


def test_perpendicular_through_merged():
    nodes = [_dnode("N", "up"), {"id": "l"}, {"id": "r"}]
    edges = [_edge("t1", "l", "N", [120, 40], [120, 100], [[120, 40], [120, 70], [120, 100]]),
             _edge("t2", "N", "r", [120, 160], [120, 220], [[120, 160], [120, 190], [120, 220]])]
    st = apply_direction_rules(nodes, edges)
    assert st["through_merged"] == 1
    merged = [e for e in edges if e["from"] == "l" and e["to"] == "r"]
    assert len(merged) == 1 and all("N" not in (e["from"], e["to"]) for e in edges)


def test_cap_caps_all_dangling_incl_box_edge():
    # cap закрывает ВСЕ висячие концы connector'ом, в т.ч. перпендикуляр-поворот
    # у грани бокса (раньше его ошибочно выбрасывал drop _in_box → терялась труба).
    box = _dnode("BOX", "down")
    far = {"id": "P", "type": "equipment", "centroid": [300, 130]}
    nodes = [box, far]
    edges = [{"id": "ein", "from": "BOX", "to": None, "source_point": [120, 130], "target_point": [130, 130], "length": 6},
             {"id": "eout", "from": "P", "to": None, "source_point": [300, 130], "target_point": [310, 130], "length": 10}]
    st = cap_dangling_ends(nodes, edges)
    ids = {e["id"] for e in edges}
    assert "ein" in ids and "eout" in ids          # ничего не выброшено
    assert st["capped"] == 2                         # оба конца закрыты
    assert all(not e["is_terminal"] for e in edges)  # концы теперь на connector'ах


def test_stitch_collinear():
    import numpy as np
    skel = np.zeros((100, 100), dtype=bool); skel[10:91, 50] = True
    nodes = [{"id": "A", "type": "equipment", "centroid": [20, 50]},
             {"id": "B", "type": "equipment", "centroid": [180, 50]},
             {"id": "cA", "type": "connector", "centroid": [20, 50]},
             {"id": "cB", "type": "connector", "centroid": [85, 50]}]
    edges = [{"id": "e0", "from": "A", "to": "cA", "source_point": [5, 50], "target_point": [20, 50], "length": 15},
             {"id": "e1", "from": "B", "to": "cB", "source_point": [95, 50], "target_point": [85, 50], "length": 10}]
    st = stitch_collinear_stubs(nodes, edges, skel)
    assert st["stitched"] == 1
    assert any({e["from"], e["to"]} == {"A", "B"} for e in edges)


def test_stitch_skips_box_to_box():
    import numpy as np
    skel = np.zeros((200, 200), dtype=bool); skel[10:191, 50] = True
    nodes = [_dnode("B1", "down", cx=50, cy=20), _dnode("B2", "down", cx=50, cy=180),
             {"id": "c1", "type": "connector", "centroid": [40, 50]},
             {"id": "c2", "type": "connector", "centroid": [160, 50]}]
    edges = [{"id": "e0", "from": "B1", "to": "c1", "source_point": [20, 50], "target_point": [40, 50], "length": 20},
             {"id": "e1", "from": "B2", "to": "c2", "source_point": [180, 50], "target_point": [160, 50], "length": 20}]
    assert stitch_collinear_stubs(nodes, edges, skel)["stitched"] == 0


def test_drop_degenerate():
    nodes = [{"id": "A", "type": "equipment"}, {"id": "cX", "type": "connector", "centroid": [1, 1]}]
    edges = [{"id": "e0", "from": "A", "to": "cX", "length": 2}]
    assert drop_degenerate_stubs(nodes, edges)["dropped"] == 1 and len(edges) == 0


def test_collapse_straight_and_keep_turn():
    nodes = [{"id": "A", "type": "equipment"}, {"id": "C", "type": "connector", "centroid": [50, 50]}, {"id": "B", "type": "equipment"}]
    edges = [{"id": "e0", "from": "A", "to": "C", "source_point": [50, 10], "target_point": [50, 50], "path": [[50, 10], [50, 50]]},
             {"id": "e1", "from": "C", "to": "B", "source_point": [50, 50], "target_point": [50, 90], "path": [[50, 50], [50, 90]]}]
    assert collapse_straight_connectors(nodes, edges)["collapsed"] == 1 and not any(n["id"] == "C" for n in nodes)
    nodes = [{"id": "A", "type": "equipment"}, {"id": "C", "type": "connector", "centroid": [50, 50]}, {"id": "B", "type": "equipment"}]
    edges = [{"id": "e0", "from": "A", "to": "C", "source_point": [10, 50], "target_point": [50, 50], "path": [[10, 50], [50, 50]]},
             {"id": "e1", "from": "C", "to": "B", "source_point": [50, 50], "target_point": [50, 90], "path": [[50, 50], [50, 90]]}]
    assert collapse_straight_connectors(nodes, edges)["collapsed"] == 0


if __name__ == "__main__":
    import traceback
    _t = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    _p, _f = 0, []
    for fn in _t:
        try:
            fn(); print("PASS", fn.__name__); _p += 1
        except Exception as e:
            print("FAIL", fn.__name__, "->", e); traceback.print_exc(); _f.append(fn.__name__)
    print(f"\n{_p}/{len(_t)} passed" + (f"  FAILED {_f}" if _f else ""))
    sys.exit(1 if _f else 0)

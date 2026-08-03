# -*- coding: utf-8 -*-
"""Э5a — пины на ребре (modules/graph/core/ports.py).

Модель «пин входа — свойство конца ребра» (утверждена 2026-08-03):
edge['pin_source'|'pin_target'] = {'dx','dy'} — локальное смещение (x, y)
от центроида узла конца; пин на коннекторе запрещён; pinned_port читает
пин ребра ПЕРВЕЕ легаси node['_ports'] (легаси живёт до миграции Э5b).
Координаты: centroid [y, x], API портов (x, y) — CODING_GUIDE §6.
"""
import math

from modules.graph.core import ports


def _box(nid="b", cy=200.0, cx=200.0):
    return {"id": nid, "type": "equipment", "centroid": [cy, cx],
            "bbox": [cx - 40.0, cy - 40.0, cx + 40.0, cy + 40.0],
            "segmentation": None, "class_id": 99, "class_name": "unknow"}


def _conn(nid="c", cy=200.0, cx=400.0):
    return {"id": nid, "type": "connector", "centroid": [cy, cx],
            "bbox": None, "segmentation": None,
            "class_id": -1, "class_name": "connector"}


def _edge(src="b", tgt="c"):
    return {"id": "edge_1", "source": src, "target": tgt,
            "source_point": [200.0, 240.0], "target_point": [200.0, 400.0],
            "waypoints": []}


def test_set_get_clear_roundtrip():
    node, e = _box(), _edge()
    pin = ports.set_edge_pin(node, e, "source", 240.0, 225.0)
    assert pin == {"dx": 40.0, "dy": 25.0}, \
        "пин — локальное смещение (x, y) от центроида"
    assert e["pin_source"] is pin
    assert ports.edge_pin(e, "source") == pin
    assert ports.edge_pin(e, "target") is None
    # перезапись, не дубль
    ports.set_edge_pin(node, e, "source", 200.0, 240.0)
    assert e["pin_source"] == {"dx": 0.0, "dy": 40.0}
    assert ports.clear_edge_pin(e, "source") is True
    assert "pin_source" not in e
    assert ports.clear_edge_pin(e, "source") is False


def test_pin_forbidden_on_connector():
    conn, e = _conn(), _edge()
    assert ports.set_edge_pin(conn, e, "target", 405.0, 203.0) is None
    assert "pin_target" not in e, "пин на коннекторе запрещён: конец == центроид"


def test_pin_role_and_pinned_on_node():
    box, conn, e = _box(), _conn(), _edge()
    assert ports.pin_role(box, e) == "source"
    assert ports.pin_role(conn, e) == "target"
    assert ports.pin_role(_box("чужой"), e) is None
    assert ports.pinned_on_node(box, e) is None
    ports.set_edge_pin(box, e, "source", 240.0, 225.0)
    assert ports.pinned_on_node(box, e) == {"dx": 40.0, "dy": 25.0}
    assert ports.pinned_on_node(conn, e) is None, \
        "пин source не виден с узла target"


def test_pinned_port_reads_edge_pin_first():
    box, e = _box(), _edge()
    # легаси-якорь в node._ports по паре узлов — другая точка
    box["_ports"] = [{"dx": -40.0, "dy": 0.0, "edge": "b|c"}]
    ports.set_edge_pin(box, e, "source", 240.0, 225.0)
    p = ports.pinned_port(box, e)
    assert (p[0], p[1]) == (240.0, 225.0), "пин ребра первее легаси node._ports"
    assert p[4] is True
    ln = math.hypot(40.0, 25.0)
    assert abs(p[2] - 40.0 / ln) < 1e-9 and abs(p[3] - 25.0 / ln) < 1e-9, \
        "нормаль — от центроида к пину"


def test_pinned_port_legacy_fallback_until_migration():
    box, e = _box(), _edge()
    box["_ports"] = [{"dx": -40.0, "dy": 0.0, "edge": "b|c"}]
    p = ports.pinned_port(box, e)
    assert (p[0], p[1]) == (160.0, 200.0), \
        "легаси node._ports обязан работать до миграции Э5b"


def test_rescale_edge_pins_scales_only_own_ends():
    box, conn = _box(), _conn()
    e1 = _edge()                              # box = source
    e2 = {"id": "edge_2", "source": "x", "target": "b",
          "source_point": [100.0, 100.0], "target_point": [225.0, 160.0],
          "waypoints": []}                    # box = target
    ports.set_edge_pin(box, e1, "source", 240.0, 225.0)   # dx 40, dy 25
    ports.set_edge_pin(box, e2, "target", 160.0, 225.0)   # dx -40, dy 25
    e1["pin_target"] = {"dx": 1.0, "dy": 2.0}  # чужой конец (коннектор c)
    old_bb = list(box["bbox"])
    new_bb = [160.0, 160.0, 320.0, 240.0]      # w x2, h x1
    ports.rescale_edge_pins(box, [e1, e2], old_bb, new_bb)
    assert e1["pin_source"] == {"dx": 80.0, "dy": 25.0}
    assert e2["pin_target"] == {"dx": -80.0, "dy": 25.0}
    assert e1["pin_target"] == {"dx": 1.0, "dy": 2.0}, \
        "пин чужого конца не масштабируется с этим узлом"


def test_breaks_anchor_sees_edge_pins():
    from modules.graph.core import edit_smooth

    box, conn, e = _box(), _conn(), _edge()
    graph = {"nodes": [box, conn], "links": [e]}
    assert edit_smooth._breaks_anchor(graph, "b") is False
    ports.set_edge_pin(box, e, "source", 240.0, 225.0)
    assert edit_smooth._breaks_anchor(graph, "b") is True
    # легаси-ветка жива до миграции
    del e["pin_source"]
    box["_ports"] = [{"dx": 40.0, "dy": 25.0, "edge": "b|c"}]
    assert edit_smooth._breaks_anchor(graph, "b") is True

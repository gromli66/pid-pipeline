"""Кратные рёбра (Р7) на пути to_canvas и после действий оператора.

to_canvas нормализует кратные рёбра на копии: старые graph_validated несут их до
пересборки. Сценарии S1–S3 — оператор правит граф после серверной нормализации
(GraphDataModel без Qt), сохраняет, и холст собирается заново. Координаты — [y, x].
"""
import json
from copy import deepcopy

from modules.binding.diameter_lines import DiameterMark, LineRules, apply_marks, build_lines
from modules.graph.core import canvas_state
from modules.graph.core.canvas_input import to_canvas
from modules.graph.core.parallel_edges import normalize_parallel_edges
from ui.editors.graph_data import GraphDataModel

KEYS = ("source", "target")


def _pairs(links):
    seen = {}
    for e in links:
        k = frozenset((e["source"], e["target"]))
        seen[k] = seen.get(k, 0) + 1
    return sum(v > 1 for v in seen.values())


def _node(nid, cls, c, degree, bbox=None):
    return {"id": nid, "type": "connector" if cls == "connector" else "equipment",
            "class_name": cls, "class_id": -1 if cls == "connector" else 5,
            "centroid": c, "bbox": bbox, "area": 0, "degree": degree}


def _edge(eid, a, b, pa, pb, **kw):
    return dict({"id": eid, "source": a, "target": b, "source_point": pa,
                 "target_point": pb, "waypoints": []}, **kw)


def validated():
    """A–B: целая труба edge_3 и обходная edge_40 (нитка); C–D: дубль edge_9 к edge_8;
    E–F: непричастная пара, у E протухшая degree=7."""
    nodes = [_node("A", "nasos", [200, 100], 2, [80, 180, 120, 220]),
             _node("B", "nasos", [200, 900], 2, [880, 180, 920, 220]),
             _node("C", "connector", [600, 100], 2),
             _node("D", "connector", [600, 900], 2),
             _node("E", "connector", [800, 100], 7),
             _node("F", "connector", [800, 900], 1)]
    links = [_edge("edge_3", "A", "B", [200, 120], [200, 880]),
             _edge("edge_40", "A", "B", [220, 100], [400, 900]),
             _edge("edge_8", "C", "D", [600, 100], [600, 900]),
             _edge("edge_9", "C", "D", [602, 100], [603, 900]),
             _edge("edge_50", "E", "F", [800, 100], [800, 900])]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [1000, 2000]},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def test_to_canvas_normalizes_on_copy():
    src = validated()
    src0 = deepcopy(src)
    sha0 = canvas_state.graph_projection_sha(src)
    canvas, _t = to_canvas(src)
    assert src == src0                                   # вход не мутирован
    assert _pairs(canvas["links"]) == 0
    ids = [e["id"] for e in canvas["links"]]
    assert "edge_9" not in ids and "edge_40" in ids      # на id edge_40 — метка Ду
    assert len(canvas["links"]) == 5
    byid = {n["id"]: n for n in canvas["nodes"]}
    assert byid["parconn_1"]["degree"] == 2
    assert byid["C"]["degree"] == 1 and byid["D"]["degree"] == 1
    assert byid["E"]["degree"] == 7                      # непричастный узел не пересчитан
    tr = canvas_state.stamp(canvas, src, layout_applied=False)
    assert tr["source_sha"] == sha0
    assert canvas_state.is_stale(canvas, src)[0] is False

    rep = apply_marks(canvas["links"], build_lines(canvas["nodes"], canvas["links"], _rules()),
                      [DiameterMark("edge_40", 50)], canvas["nodes"])
    assert not rep.orphan_marks


def _rules():
    return LineRules(transit=frozenset({"connector"}), stop=frozenset({"nasos", "unknow"}))


def _server_normalized():
    g = validated()
    normalize_parallel_edges(g["nodes"], g["links"], keys=KEYS)
    return g


def _load(tmp_path, g, name="graph_validated.json"):
    p = tmp_path / name
    p.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    m = GraphDataModel()
    assert m.load(str(p))
    return m


def _save_reload(tmp_path, m):
    p = tmp_path / "saved.json"
    assert m.save(str(p))
    return json.loads(p.read_text(encoding="utf-8"))


# S1 — удаление трубы жестом по паре снимает одно ребро, а не «призрака» с ним
def test_s1_remove_by_pair_removes_one_pipe(tmp_path):
    raw = _load(tmp_path, validated(), "raw.json")
    n0 = len(raw.edges_data)
    raw.remove_edge(raw.edge_key("A", "B"))
    assert n0 - len(raw.edges_data) == 2                 # без нормализации — обе трубы

    g = _server_normalized()
    canvas0, _ = to_canvas(g)
    m = _load(tmp_path, g)
    snap = m.snapshot()
    before = len(m.edges_data)
    m.remove_edge(m.edge_key("A", "B"))
    assert before - len(m.edges_data) == 1
    left = {(e["source"], e["target"]) for e in m.edges_data}
    assert {("A", "parconn_1"), ("parconn_1", "B")} <= left

    m.restore(snap)                                      # undo
    assert len(m.edges_data) == before
    m.remove_edge(m.edge_key("A", "B"))

    saved = _save_reload(tmp_path, m)
    st = normalize_parallel_edges(deepcopy(saved["nodes"]), deepcopy(saved["links"]), keys=KEYS)
    assert st["pairs"] == 0
    canvas, _ = to_canvas(saved)
    assert len(canvas["links"]) == len(canvas0["links"]) - 1


# S2 — оператор удалил узел-рассечку: кратная пара не воскресает
def test_s2_remove_parconn_no_resurrection(tmp_path):
    m = _load(tmp_path, _server_normalized())
    m.remove_node("parconn_1")
    saved = _save_reload(tmp_path, m)
    st = normalize_parallel_edges(deepcopy(saved["nodes"]), deepcopy(saved["links"]), keys=KEYS)
    assert st["pairs"] == 0
    canvas, _ = to_canvas(saved)
    ab = [e for e in canvas["links"] if {e["source"], e["target"]} == {"A", "B"}]
    assert [e["id"] for e in ab] == ["edge_3"]
    assert not any(n["id"].startswith("parconn_") for n in canvas["nodes"])


# S3 — Ду на первой половине доезжает до второй через connector степени 2
def test_s3_diameter_reaches_both_halves(tmp_path):
    g = _server_normalized()
    second = next(e["id"] for e in g["links"] if e["source"] == "parconn_1")
    m = _load(tmp_path, g)
    saved = _save_reload(tmp_path, m)
    canvas, _ = to_canvas(saved)
    links = canvas["links"]
    rep = apply_marks(links, build_lines(canvas["nodes"], links, _rules()),
                      [DiameterMark("edge_40", 50)], canvas["nodes"])
    assert rep.ok
    byid = {e["id"]: e for e in links}
    assert byid["edge_40"]["diameter_value"] == byid[second]["diameter_value"] == 50
    assert "diameter_value" not in byid["edge_3"]

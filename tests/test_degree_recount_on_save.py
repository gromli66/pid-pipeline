# -*- coding: utf-8 -*-
"""Поле `degree` в сохранённом файле обязано совпадать с рёбрами того же файла.

Репро 2026-08-26 (`c78e1f02`, graph_validated.json после правок оператора):
`degree` посчитан сборкой ДО правок и не пересчитан после — у четырёх узлов
поле разошлось с инцидентностью в `links`, причём `node_232`/`node_233`
остались с `degree: 0`, хотя оператор приклеил к ним ручные рёбра. В том же
файле `graph.num_isolated_nodes` (он-то считается по рёбрам) равен нулю —
файл спорил сам с собой.

Qt не нужен: GraphDataModel — чистый Python.
"""
import collections
import json

from modules.graph.core import canvas_state
from ui.editors.graph_data import GraphDataModel


def _node(nid, x, y, degree, ntype="connector"):
    return {"id": nid, "type": ntype, "centroid": [float(y), float(x)],
            "area": 100.0, "bbox": [x - 5, y - 5, x + 5, y + 5],
            "segmentation": None, "class_id": -1, "class_name": ntype,
            "degree": degree}


def _edge(eid, src, tgt, sp, tp):
    return {"id": eid, "source": src, "target": tgt,
            "source_point": list(sp), "target_point": list(tp),
            "waypoints": [], "length": 10, "is_terminal": False}


def _model(tmp_path):
    """Цепочка n1—bend—n2, плюс два узла без рёбер (как node_232/node_233)."""
    graph = {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [600, 800]},
        "nodes": [
            _node("n1", 100, 100, 1, "equipment"),
            _node("bend", 200, 100, 2),
            _node("n2", 300, 100, 1, "equipment"),
            _node("n3", 100, 300, 0, "equipment"),
            _node("n4", 300, 300, 0, "equipment"),
        ],
        "links": [
            _edge("edge_1", "n1", "bend", [100, 100], [100, 200]),
            _edge("edge_2", "bend", "n2", [100, 200], [100, 300]),
        ],
        "text_blocks": [], "bindings": [],
    }
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")
    m = GraphDataModel()
    assert m.load(str(gp))
    return m


def _incidence(saved):
    inc = collections.Counter()
    for link in saved["links"]:
        inc[link["source"]] += 1
        inc[link["target"]] += 1
    return inc


def _save_and_read(model, tmp_path, name="out.json"):
    out = tmp_path / name
    assert model.save(str(out))
    return json.loads(out.read_text(encoding="utf-8"))


def test_degree_matches_links_after_operator_edits(tmp_path):
    """Репро замера: узел удалён, ручное ребро добавлено — поле обязано сойтись."""
    m = _model(tmp_path)

    # Оператор сносит узел излома (аналог пропавших bendconn_*): цепочка рвётся.
    m.remove_node("bend")
    # ...и вручную приклеивает два «изолированных» узла — как edge_manual_2/_3.
    data = m.create_edge_data("n3", "n4", [300, 100], [300, 300])
    m.add_edge("n3", "n4", data)

    saved = _save_and_read(m, tmp_path)
    inc = _incidence(saved)

    stale = [(n["id"], n["degree"], inc[n["id"]])
             for n in saved["nodes"] if n["degree"] != inc[n["id"]]]
    assert not stale, f"degree разошёлся с links: {stale}"
    # Ровно тот случай из замера: узлы с рождения degree 0, получившие ребро.
    by_id = {n["id"]: n for n in saved["nodes"]}
    assert by_id["n3"]["degree"] == 1
    assert by_id["n4"]["degree"] == 1
    # А брошенные концы удалённого излома — обратно в ноль.
    assert by_id["n1"]["degree"] == 0
    assert by_id["n2"]["degree"] == 0


def test_manual_node_gets_degree_from_its_edge(tmp_path):
    """Ручной узел рождается с degree 0 и остаётся таким без пересчёта."""
    m = _model(tmp_path)
    node = m.create_connector_node(400.0, 100.0)
    assert node["degree"] == 0
    m.add_node(node)
    data = m.create_edge_data("n2", node["id"], [100, 300], [100, 400])
    m.add_edge("n2", node["id"], data)

    saved = _save_and_read(m, tmp_path)
    by_id = {n["id"]: n for n in saved["nodes"]}
    assert by_id[node["id"]]["degree"] == 1
    assert by_id["n2"]["degree"] == 2


def test_field_agrees_with_num_isolated_nodes(tmp_path):
    """Метаданные считаются по рёбрам — поле обязано давать то же число."""
    m = _model(tmp_path)
    m.remove_node("bend")

    saved = _save_and_read(m, tmp_path)
    by_field = sum(1 for n in saved["nodes"] if n["degree"] == 0)
    assert by_field == saved["graph"]["num_isolated_nodes"] == 4


def test_recount_does_not_move_canvas_sha(tmp_path):
    """Пересчёт не обязан делать холст устаревшим: degree вне проекции."""
    m = _model(tmp_path)
    before = canvas_state.graph_projection_sha(m.graph_data)
    saved = _save_and_read(m, tmp_path)
    assert canvas_state.graph_projection_sha(saved) == before

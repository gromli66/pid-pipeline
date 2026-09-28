# -*- coding: utf-8 -*-
"""Популяции `@model` стендов редактора (Р1, блок 3).

Холсты срезов (`smooth_corpus/raw`, `edited`) сняты до модели точек
прикрепления и метки `canvas_transform.attach_model` не несут: после блока 3
редактор правит их по правилам 238fe6b. Модель стенды видят только на
популяции `@model` — те же файлы после `tools.corpus.as_model` (метка, талия
клапана на поперечной координате центроида, посадка каноном сервера).
Координаты: точки [y, x], bbox [x1, y1, x2, y2].
"""
from __future__ import annotations

import copy
import json
import sys

from modules.graph.core import contact, edit_checks
from tools import corpus
from tools import edit_bench as eb
from tools import smooth_bench as sb

# armatura_electro = ELECTRIC_VLV, рабочая рамка 42 x 37.8, ось H
W, H = 42.0, 37.8
CY, CX = 300.0, 500.0


def _old_canvas() -> dict:
    """Старый холст (метки нет): клапан H, центроид = центр рамки, трубы
    слева и справа приходят в середину грани, как садил 238fe6b."""
    def conn(nid, y, x):
        return {"id": nid, "type": "connector", "class_name": "connector",
                "centroid": [y, x], "bbox": None}

    return {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [1080, 1920],
                  "canvas_transform": {"s": 1.0, "offx": 0, "offy": 0}},
        "nodes": [
            conn("c1", CY, 400.0),
            {"id": "v1", "type": "equipment", "class_name": "armatura_electro",
             "_axis": "H", "centroid": [CY, CX],
             "bbox": [CX - W / 2, CY - H / 2, CX + W / 2, CY + H / 2]},
            conn("c2", CY, 600.0),
        ],
        "links": [
            {"id": "e1", "source": "c1", "target": "v1",
             # конец сбит с линии на 8 px: его возвращает только посадка
             "source_point": [CY, 400.0], "target_point": [CY + 8.0, CX - W / 2],
             "waypoints": []},
            {"id": "e2", "source": "v1", "target": "c2",
             "source_point": [CY, CX + W / 2], "target_point": [CY, 600.0],
             "waypoints": []},
        ],
        "text_blocks": [], "bindings": [],
    }


def _node(g, nid):
    return next(n for n in g["nodes"] if n["id"] == nid)


def test_as_model_marks_canvas_and_puts_waist_on_centroid():
    g = corpus.as_model(_old_canvas())
    off = contact.waist_offset(_node(g, "v1"), "H", W, H)
    assert off > 1.0                                   # талия не в центре
    assert contact.canvas_model(g) == contact.ATTACH_MODEL
    assert all(contact.LEGACY_KEY not in n for n in g["nodes"])
    v = _node(g, "v1")
    assert v["centroid"] == [CY, CX]
    x1, y1, x2, y2 = v["bbox"]
    assert (x1, x2) == (CX - W / 2, CX + W / 2)
    assert abs(y1 - (CY - off - H / 2)) < 1e-9
    assert abs(y2 - y1 - H) < 1e-9
    assert abs(contact.waist_centroid(v, v["bbox"])[0] - CY) < 1e-9
    # концы сели в талию (поперечная координата центроида), на свои грани
    e1, e2 = g["links"]
    assert abs(e1["target_point"][0] - CY) < 1e-6
    assert abs(e1["target_point"][1] - x1) < 1e-6
    assert abs(e2["source_point"][0] - CY) < 1e-6
    assert abs(e2["source_point"][1] - x2) < 1e-6


def test_as_model_drops_the_legacy_key_of_a_loaded_canvas():
    g = _old_canvas()
    for n in g["nodes"]:
        n[contact.LEGACY_KEY] = True
    g = corpus.as_model(g)
    assert all(contact.LEGACY_KEY not in n for n in g["nodes"])
    assert _node(g, "v1")["bbox"] == corpus.as_model(_old_canvas())[
        "nodes"][1]["bbox"]


def test_smooth_bench_model_populations_are_the_same_files_modelled(tmp_path):
    assert sb.POPULATIONS == ("raw", "edited", "raw@model", "edited@model")
    for pop in ("raw", "edited"):
        assert (sb.population_paths(pop + "@model")
                == sb.population_paths(pop))
    p = tmp_path / "graph_edited_x.json"
    p.write_text(json.dumps(_old_canvas(), ensure_ascii=False),
                 encoding="utf-8")
    assert sb.input_graph(p, False) == _old_canvas()
    assert sb.input_graph(p, True) == corpus.as_model(_old_canvas())


def test_edit_bench_model_measures_modelled_canvas_into_own_baseline(
        tmp_path, monkeypatch):
    p = tmp_path / "graph_edited_x.json"
    p.write_text(json.dumps(_old_canvas(), ensure_ascii=False),
                 encoding="utf-8")
    plain_base = tmp_path / "edit_baseline.json"
    model_base = tmp_path / "edit_baseline_model.json"
    monkeypatch.setattr(eb, "BASELINE", plain_base)
    monkeypatch.setattr(eb, "BASELINE_MODEL", model_base)
    monkeypatch.setattr(sys, "argv",
                        ["edit_bench.py", str(p), "--model", "--write-baseline"])
    assert eb.main() == 0
    assert not plain_base.exists()
    got = json.loads(model_base.read_text(encoding="utf-8"))["files"][p.name]
    want = edit_checks.check_canvas(
        corpus.as_model(_old_canvas()))["counts"]
    plain = edit_checks.check_canvas(copy.deepcopy(_old_canvas()))["counts"]
    assert got == want
    assert got != plain                  # замер видит модель, а не файл
    monkeypatch.setattr(sys, "argv",
                        ["edit_bench.py", str(p), "--model", "--check"])
    assert eb.main() == 0

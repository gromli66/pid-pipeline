# -*- coding: utf-8 -*-
"""Э12: сбор остаточных очагов после раскладки (modules/graph/core/layout/residual).

Три сорта остатка (§7.1 EDITOR_AFTER_LAYOUT_PLAN): невидимые трубы,
боксы на чужих нарисованных трубах, нелегальные наложения. Наложения из
детекции легальны и в остаток не входят (судья строгий — Э13, tol=0).
"""
from copy import deepcopy

import pytest

pytest.importorskip("shapely")

from modules.graph.core.layout.residual import collect_residual


def _node(nid, bbox, centroid_yx):
    return {"id": nid, "type": "equipment", "class_name": "unknow",
            "bbox": bbox, "centroid": centroid_yx, "segmentation": None}


def _graph():
    """Синтетика: по одному очагу каждого сорта + легальная пара.

    n1-n2 — невидимая труба (зазор форм 2 px < floor 6);
    e2 (n3→n4) — магистраль, прошивающая чужой бокс n5;
    n6/n7 — нелегальное наложение (в orig разнесены);
    n8/n9 — наложены уже в orig (легальны, в остаток не входят).
    """
    return {
        "nodes": [
            _node("n1", [100, 100, 140, 140], [120.0, 120.0]),
            _node("n2", [142, 100, 182, 140], [120.0, 162.0]),
            _node("n3", [600, 80, 640, 120], [100.0, 620.0]),
            _node("n4", [860, 80, 900, 120], [100.0, 880.0]),
            _node("n5", [740, 80, 780, 120], [100.0, 760.0]),
            _node("n6", [400, 400, 460, 460], [430.0, 430.0]),
            _node("n7", [430, 400, 490, 460], [430.0, 460.0]),
            _node("n8", [700, 700, 760, 760], [730.0, 730.0]),
            _node("n9", [730, 700, 790, 760], [730.0, 760.0]),
        ],
        "links": [
            {"id": "e1", "source": "n1", "target": "n2",
             "source_point": [120.0, 140.0], "target_point": [120.0, 142.0],
             "waypoints": []},
            {"id": "e2", "source": "n3", "target": "n4",
             "source_point": [100.0, 640.0], "target_point": [100.0, 860.0],
             "waypoints": []},
        ],
        "graph": {},
    }


def _orig():
    """Вход раскладки: n6/n7 ещё разнесены, n8/n9 уже наложены."""
    g = deepcopy(_graph())
    n7 = next(n for n in g["nodes"] if n["id"] == "n7")
    n7["bbox"] = [500, 400, 560, 460]
    n7["centroid"] = [430.0, 530.0]
    return g


def test_collects_three_kinds_and_amnesties_detection_overlap():
    r = collect_residual(_graph(), _orig())

    assert [d["edge_id"] for d in r["invisible_edges"]] == ["e1"]
    d = r["invisible_edges"][0]
    assert d["nodes"] == ["n1", "n2"]
    assert d["gap"] == pytest.approx(2.0, abs=0.01)
    # Точка — середина нарисованных концов, [x, y] холста.
    assert d["point"] == pytest.approx([141.0, 120.0])

    assert [b["node_id"] for b in r["box_on_magi"]] == ["n5"]
    assert r["box_on_magi"][0]["point"] == pytest.approx([760.0, 100.0])

    # n6/n7 — нелегальное наложение; n8/n9 наложены в orig — легальны.
    assert [o["nodes"] for o in r["overlaps"]] == [["n6", "n7"]]
    assert r["overlaps"][0]["area_px2"] > 0

    assert r["total"] == 3


def test_clean_layout_gives_empty_residual():
    g = _orig()   # разнесённый вариант без e1-дефекта
    g["links"] = [g["links"][1]]          # только магистраль
    n5 = next(n for n in g["nodes"] if n["id"] == "n5")
    n5["bbox"] = [740, 200, 780, 240]     # бокс увели с магистрали
    n5["centroid"] = [220.0, 760.0]
    n8 = next(n for n in g["nodes"] if n["id"] == "n8")
    n8["bbox"] = [600, 700, 660, 760]     # развели легальную пару
    n8["centroid"] = [730.0, 630.0]
    r = collect_residual(g, g)
    assert r["total"] == 0
    assert r["invisible_edges"] == []
    assert r["box_on_magi"] == []
    assert r["overlaps"] == []

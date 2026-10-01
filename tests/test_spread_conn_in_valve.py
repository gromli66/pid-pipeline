# -*- coding: utf-8 -*-
"""Раздвигание не заводит коннектор трубы клапана в рамку этого клапана.

Решение Максима 30.09 (MEASUREMENTS §133.4, §138). Замер 8d517a35: после
расстановки коннектор node_115 стоял в 1.95 px под рамкой armatura_ruchn
node_114; ход раздвигания по чужому ребру поднял ряд труб вместе с ним — в
тело клапана (зазор −10.5). Приёмка хода этого не видела: ребро клапана и так
было дефектным (зазор < FLOOR), счёт дефектов упал за счёт чужого ребра.
Сторож — `spread.conn_in_valve` в приёмке `try_shift`, сравнение с базой
входа слоя (пары, пришедшие из расстановки, законны).

Синтетика повторяет механизм: клапан V прижат к верху листа, под ним ряд
коннекторов l–c–r (c — труба клапана, зазор 2.05), справа под рядом высокий
бокс y до низа листа (труба r–y, зазор 1.95). Ребро r–y — первое в очереди
(меньший зазор); вылечить его можно, только подняв ряд, — и c уходит в V.
"""
from __future__ import annotations

import copy

import pytest

from modules.graph.core.graph_access import nodes_by_id
from modules.graph.core.layout import spread
from modules.graph.core.seating import reseat_all_endpoints


def _box(nid, x1, y1, x2, y2, cn="unknow", **kw):
    n = {"id": nid, "type": "equipment", "class_name": cn,
         "centroid": [(y1 + y2) / 2.0, (x1 + x2) / 2.0],
         "bbox": [x1, y1, x2, y2]}
    n.update(kw)
    return n


def _conn(nid, x, y):
    return {"id": nid, "type": "connector", "class_name": "connector",
            "centroid": [y, x], "bbox": None}


def _edge(eid, s, t):
    return {"id": eid, "source": s, "target": t, "waypoints": []}


def _sheet(cn="armatura_ruchn"):
    row = 42.0 + 2.05
    nodes = [_box("v", 89.5, 0.0, 110.5, 42.0, cn=cn, _axis="V"),
             _conn("l", 40.0, row), _conn("c", 100.0, row),
             _conn("r", 160.0, row),
             _box("y", 140.0, row + 1.95, 180.0, 1080.0)]
    links = [_edge("e_vc", "v", "c"), _edge("e_lc", "l", "c"),
             _edge("e_cr", "c", "r"), _edge("e_ry", "r", "y")]
    g = {"nodes": nodes, "links": links, "graph": {}}
    reseat_all_endpoints(g)
    return g


def _run(g):
    spread.spread(g, copy.deepcopy(g), base_v16=copy.deepcopy(g),
                  verbose=False)
    return nodes_by_id(g)


def test_conn_in_valve_predicate():
    """Строго внутри рамки клапана, с которым коннектор связан ребром;
    касание грани, чужой клапан и не клапан — не в счёт."""
    g = _sheet()
    byid = nodes_by_id(g)
    assert spread.conn_in_valve(g, byid) == set()
    byid["c"]["centroid"] = [41.9, 100.0]                    # внутри v
    assert spread.conn_in_valve(g, byid) == {("c", "v")}
    assert spread.conn_in_valve(g, byid, [g["links"][1]]) == set()
    byid["c"]["centroid"] = [42.0, 100.0]                    # на грани
    assert spread.conn_in_valve(g, byid) == set()
    byid["l"]["centroid"] = [20.0, 100.0]                    # не связан с v
    assert spread.conn_in_valve(g, byid) == set()
    plain = _sheet(cn="unknow")
    pb = nodes_by_id(plain)
    pb["c"]["centroid"] = [20.0, 100.0]
    assert spread.conn_in_valve(plain, pb) == set()          # не клапан


def test_spread_does_not_pull_valve_connector_into_its_frame():
    """Ход по ребру r–y, поднимающий ряд в тело клапана, отклонён: c
    остаётся под рамкой V (на входе слоя пары не было)."""
    g = _sheet()
    assert spread.conn_in_valve(g, nodes_by_id(g)) == set()

    byid = _run(g)

    assert spread.conn_in_valve(g, byid) == set()
    assert byid["c"]["centroid"][0] >= 42.0, "коннектор трубы в теле клапана"
    assert byid["v"]["bbox"] == [89.5, 0.0, 110.5, 42.0]


@pytest.mark.parametrize("cn", ["unknow"])
def test_plain_box_is_outside_the_rule(cn):
    """Не клапан — правило не действует (решение 30.09 — о клапане): ряд
    поднимается в бокс, как до сторожа."""
    g = _sheet(cn=cn)
    byid = _run(g)
    assert byid["c"]["centroid"][0] < 42.0

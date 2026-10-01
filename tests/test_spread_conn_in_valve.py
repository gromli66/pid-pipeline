# -*- coding: utf-8 -*-
"""Раздвигание не заводит коннектор трубы в рамку узла на её другом конце.

Решение Максима 30.09 — о клапане (MEASUREMENTS §133.4, §138). Замер
8d517a35: после расстановки коннектор node_115 стоял в 1.95 px под рамкой
armatura_ruchn node_114; ход раздвигания по чужому ребру поднял ряд труб
вместе с ним — в тело клапана (зазор −10.5). Приёмка хода этого не видела:
ребро клапана и так было дефектным (зазор < FLOOR), счёт дефектов упал за
счёт чужого ребра. Решение 01.10 — рамка ЛЮБОГО узла: со сторожем клапана
тот же ряд уходил в рамку стрелки направления (edge_manual_90, −4.35).
Сторож — `spread.conn_in_frame` в приёмке `try_shift`, сравнение с базой
входа слоя (пары, пришедшие из расстановки, законны).

Синтетика повторяет механизм: узел V прижат к верху листа, под ним ряд
коннекторов l–c–r (c — труба V, зазор 2.05), справа под рядом высокий
бокс y до низа листа (труба r–y, зазор 1.95). Ребро r–y — первое в очереди
(меньший зазор); вылечить его можно, только подняв ряд, — и c уходит в V.
"""
from __future__ import annotations

import copy

import pytest

from modules.graph.core.graph_access import nodes_by_id
from modules.graph.core.layout import spread
from modules.graph.core.layout._gap import FLOOR, gap_and_seam, shape_box
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


def test_conn_in_frame_predicate():
    """Строго внутри рамки узла, с которым коннектор связан ребром (клапан
    или нет); касание грани и несвязанный узел — не в счёт."""
    for cn in ("armatura_ruchn", "unknow"):
        g = _sheet(cn=cn)
        byid = nodes_by_id(g)
        assert spread.conn_in_frame(g, byid) == set()
        byid["c"]["centroid"] = [41.9, 100.0]                # внутри v
        assert spread.conn_in_frame(g, byid) == {("c", "v")}
        assert spread.conn_in_frame(g, byid, [g["links"][1]]) == set()
        byid["c"]["centroid"] = [42.0, 100.0]                # на грани
        assert spread.conn_in_frame(g, byid) == set()
        byid["l"]["centroid"] = [20.0, 100.0]                # не связан с v
        assert spread.conn_in_frame(g, byid) == set()


@pytest.mark.parametrize("cn", ["armatura_ruchn", "unknow", "napravlenie"])
def test_spread_does_not_pull_connector_into_a_frame(cn):
    """Ход по ребру r–y, поднимающий ряд в тело узла, отклонён — клапан
    (30.09), бокс и стрелка направления (01.10): c остаётся под рамкой v
    (на входе слоя пары не было)."""
    g = _sheet(cn=cn)
    assert spread.conn_in_frame(g, nodes_by_id(g)) == set()

    byid = _run(g)

    assert spread.conn_in_frame(g, byid) == set()
    assert byid["c"]["centroid"][0] >= 42.0, "коннектор трубы в теле узла"
    assert byid["v"]["bbox"] == [89.5, 0.0, 110.5, 42.0]


def _sheet_with_pair_from_placement():
    """Вход слоя УЖЕ с коннектором в рамке: c в теле V (под центром, ребро
    V–c вертикально по центроидам — c едет с V при ходе по X). Выйти из V c
    не может: сверху A и снизу B впритык до краёв листа, слева L, справа Y
    (выход — в чужой бокс, труба длиннее MAGI). Ребро V–Y (зазор 1) лечится
    только ходом V влево вместе с c — c остаётся в рамке V."""
    x1, y1, x2, y2 = 1500.0, 470.0, 1700.0, 610.0
    nodes = [_box("V", x1, y1, x2, y2),
             _conn("c", (x1 + x2) / 2.0, (y1 + y2) / 2.0 + 10.0),
             _box("Y", x2 + 1.0, y1, 1920.0, y2),
             _box("L", 0.0, y1, x1 - 12.0, y2),
             _box("A", 0.0, 0.0, 1920.0, y1 - 1.0),
             _box("B", 0.0, y2 + 1.0, 1920.0, 1080.0)]
    links = [_edge("e_vc", "V", "c"), _edge("e_vy", "V", "Y")]
    g = {"nodes": nodes, "links": links, "graph": {}}
    reseat_all_endpoints(g)
    return g


def test_pair_from_placement_does_not_block_moves():
    """Сравнение с БАЗОЙ входа, а не с нулём: пара (c, V) пришла из
    расстановки, ход, который её сохраняет (V и c едут вместе), принят —
    ребро V–Y вылечено. Сравнение с нулём отвергло бы и его."""
    g = _sheet_with_pair_from_placement()
    byid = nodes_by_id(g)
    assert spread.conn_in_frame(g, byid) == {("c", "V")}
    assert set(spread.defects(g, byid)) == {"e_vc", "e_vy"}

    byid = _run(g)

    gap, _p, _q = gap_and_seam(shape_box(byid["V"]), shape_box(byid["Y"]))
    assert gap >= FLOOR, "ход, сохраняющий пару из расстановки, отвергнут"
    assert spread.conn_in_frame(g, byid) == {("c", "V")}
    assert set(spread.defects(g, byid)) == {"e_vc"}

# -*- coding: utf-8 -*-
"""Осевая расстановка: трубы клапана вместо обрыва attempt(0.0).

Синтетика без данных заказчика. Дефект, который здесь сторожится
(c78e1f02: 25 пар из 79): клапан стоит талией на трубе, его коннектор — в
той же колонке и на входе ВНУТРИ рамки клапана. Если ни одна доля порогов
> 0 не влезает в холст, `place` раньше брал attempt(0.0): без труб вовсе,
и коннектор оставался в рамке клапана — раздвигание пару уже не разводит.

Кирпичи: `valve_pipes` (какие трубы — «клапан на своей оси»), `valve_floor`
(такая труба не наследует входную длину короче VALVE_PIPE), `prune_pipes`
(ослабляется только труба на критическом пути, сепарация блок-блок —
никогда) и сама ступень «только трубы клапанов» в `place`.
"""
from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from modules.graph.core.contact import CLASS_NAME_TO_SKIN
from modules.graph.core.layout import axial

VALVE = sorted(c for c, s in CLASS_NAME_TO_SKIN.items()
               if s[0] == "ValveControl")[0]


def _it(key, hw=0.0, hh=0.0):
    return {"key": key, "hw": hw, "hh": hh, "ox": 0.0, "oy": 0.0}


def _node(nid, cx, cy, hw=0.0, hh=0.0, cls="connector", **kw):
    n = {"id": nid, "class_name": cls, "centroid": [cy, cx]}     # [y, x]
    if cls == "connector":
        n["type"] = "connector"
        n["bbox"] = [cx, cy, cx, cy]
    else:
        n["bbox"] = [cx - hw, cy - hh, cx + hw, cy + hh]           # [x1,y1,x2,y2]
    n.update(kw)
    return n


def _column(height, room):
    """Колонка x=100: коннекторы u и c на входе внутри рамки клапана v
    (hh=12, ось V). Высокие блоки A (ряд v) и B (ряд ниже) почти во весь
    холст: их ряды расходятся не больше чем на `room` px. Рядом блок P с
    коннектором q в своей рамке: при любой доле > 0 труба P–q требует
    касания (+10 px) на той же цепочке рядов — при room=16 не влезает ни
    одна доля > 0, а касание трубы клапана (12 px) влезает."""
    hh = (height - 1 - room) / 2.0
    return {"nodes": [_node("A", 300, 90, 20, hh, "apparat"),
                      _node("B", 450, 110, 20, hh, "apparat"),
                      _node("v", 100, 90, 8, 12, VALVE, _axis="V"),
                      _node("u", 100, 84), _node("c", 100, 96),
                      _node("P", 200, 96, 8, 10, "apparat"),
                      _node("q", 200, 104)],
            "edges": [{"id": "e1", "source": "v", "target": "u"},
                      {"id": "e2", "source": "v", "target": "c"},
                      {"id": "e3", "source": "P", "target": "q"}]}


def _ys(g):
    return {n["id"]: n["centroid"][0] for n in g["nodes"]}


def test_cliff_keeps_valve_pipes_connector_leaves_frame():
    """Ни одна доля > 0 не влезла -> ступень «только трубы клапанов»:
    трубы не исчезают, коннектор не остаётся внутри рамки клапана."""
    g = _column(200, 16)
    st = axial.place(g, canvas=(600.0, 200.0))
    y = _ys(g)
    assert st["scale_eff"] == 0.0 and st["over"] <= axial.PRUNE_SLACK
    assert st["pipes_y"] == 2                      # v–u и v–c, не 0
    assert st["pruned"]["y"] == {"touch": 1, "drop": 0}
    # c — на критическом пути (к ряду B): до касания, на кромке рамки
    assert y["c"] - y["v"] >= 12.0 - 1e-6
    # u — вне пути: полный пол VALVE_PIPE сверх полугабарита
    assert y["v"] - y["u"] >= 12.0 + axial.VALVE_PIPE - 1e-6


def test_no_cliff_no_valve_only_step():
    """Холст с запасом: доля 1 влезает, ступень не включается, а трубы
    клапана всё равно не короче VALVE_PIPE (вход — след рамки, не труба)."""
    g = _column(400, 60)
    st = axial.place(g, canvas=(600.0, 400.0))
    y = _ys(g)
    assert st["scale_eff"] == 1.0 and st["pruned"] == {}
    assert y["v"] - y["u"] >= 12.0 + axial.VALVE_PIPE - 1e-6
    assert y["c"] - y["v"] >= 12.0 + axial.VALVE_PIPE - 1e-6
    assert y["q"] - y["P"] == pytest.approx(10.0)   # не клапан: кап входа


def test_valve_pipes_only_along_valve_axis():
    g = {"nodes": [{"id": "v", "class_name": VALVE, "_axis": "V"},
                   {"id": "a", "class_name": "connector"},
                   {"id": "b", "class_name": "connector"},
                   {"id": "p", "class_name": "nasos"}],
         "edges": [{"id": "e1", "source": "v", "target": "a"},   # вдоль V
                   {"id": "e2", "source": "v", "target": "b"},   # поперёк
                   {"id": "e3", "source": "p", "target": "a"}]}  # не клапан
    xa = {"v": 0, "a": 0, "b": 1, "p": 0}
    ya = {"v": 1, "a": 0, "b": 1, "p": 2}
    assert axial.valve_pipes(g, xa, ya) == {("v", "a")}


def test_valve_floor_lifts_only_valve_pipes():
    by_key = {"v": _it("v", 10, 20), "a": _it("a"), "p": _it("p", 5, 5)}
    pipes = {"x": [], "y": [("v", "a", 20.0 + 1.0), ("p", "a", 5.0 + 1.0)]}
    out = axial.valve_floor(pipes, {("v", "a")}, by_key, 6.0)
    assert out["y"] == [("v", "a", 26.0), ("p", "a", 6.0)]
    assert axial.valve_floor(pipes, {("v", "a")}, by_key, 0.0) is pipes


def test_prune_touches_critical_pipe_first():
    by_key = {k: _it(k) for k in "abc"}
    idx = {"a": 0, "b": 1, "c": 2}
    pipes = [("a", "b", 10.0), ("b", "c", 10.0)]
    kept, st = axial.prune_pipes(3, [(0.0, 0.0)] * 3, [], pipes, by_key, idx,
                                 "oy", "hh", 12.0)
    assert st == {"touch": 1, "drop": 0}
    assert sorted(kept) == [("a", "b", 10.0), ("b", "c", 0.0)]


def test_prune_never_touches_block_separation():
    by_key = {"a": _it("a", 0, 10), "b": _it("b", 0, 10), "c": _it("c")}
    idx = {"a": 0, "c": 1, "b": 2}
    seps = [("a", "b", 40.0)]              # блок-блок сам шире холста
    pipes = [("a", "c", 10.0 + 6.0)]       # труба мимо критического пути
    kept, st = axial.prune_pipes(3, [(10.0, 10.0), (0.0, 0.0), (10.0, 10.0)],
                                 seps, pipes, by_key, idx, "oy", "hh", 30.0)
    assert st == {"touch": 0, "drop": 0} and kept == pipes


def test_prune_drops_after_touch_when_touch_not_enough():
    by_key = {"a": _it("a", 0, 10), "b": _it("b", 0, 10)}
    idx = {"a": 0, "b": 1}
    kept, st = axial.prune_pipes(2, [(10.0, 10.0)] * 2, [],
                                 [("a", "b", 26.0)], by_key, idx, "oy", "hh",
                                 30.0)
    assert st == {"touch": 0, "drop": 1} and kept == []

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
    трубы не исчезают, коннектор не остаётся внутри рамки клапана
    (минимальная труба влезает в холст — она и держится)."""
    g = _column(200, 16)
    st = axial.place(g, canvas=(600.0, 200.0))
    y = _ys(g)
    assert st["scale_eff"] == 0.0 and st["over"] <= axial.PRUNE_SLACK
    assert st["pipes_y"] == 2                      # v–u и v–c, не 0
    assert st["pruned"]["y"] == {"touch": 1, "drop": 0}
    # c — на критическом пути (к ряду B): до минимальной трубы — снаружи
    # рамки на клиренс VALVE_CLEAR (решение 01.10), а не на кромке
    assert y["c"] - y["v"] == pytest.approx(12.0 + axial.VALVE_CLEAR)
    # u — вне пути: полный пол VALVE_PIPE сверх полугабарита
    assert y["v"] - y["u"] >= 12.0 + axial.VALVE_PIPE - 1e-6


@pytest.mark.parametrize("room", [4, 8, 10])
def test_cliff_tight_sheet_canvas_first(room):
    """Тесный лист: минимальные трубы клапана не влезают, а прежняя ступень
    со снятием влезает. «Холст первым» (решение Максима 01.10): лист не
    выходит за холст — берётся прежняя ступень (труба клапана на
    критическом пути снята, коннектор в рамке v), а не запасной attempt с
    минимальными трубами ценой вылета (d729fb9: c78e1f02 0 -> 71 px)."""
    g = _column(200, room)
    st = axial.place(g, canvas=(600.0, 200.0))
    y = _ys(g)
    assert st["scale_eff"] == 0.0 and st["over"] <= axial.PRUNE_SLACK
    assert st["pruned"]["y"]["drop"] >= 1          # прежняя ступень, не пол
    assert y["c"] - y["v"] < 12.0                  # плата: c в рамке v


@pytest.mark.parametrize("room", [-2, -10])
def test_cliff_no_step_fits_fallback_without_pipes(room):
    """Не влезает и прежняя ступень (сами блоки выше холста на -room px):
    запасной attempt — без труб вовсе, как до d729fb9 (решение Максима
    01.10: лист, который не влезает ни в одном варианте, держит меньший
    выход за лист). Вылет — только сами блоки, а не + полугабарит v и
    VALVE_CLEAR с минимальной трубой клапана на критическом пути (первая
    редакция; 0aea61c0: рамки за 1080 по Y 215.5 px при 129.0 без труб);
    плата — коннектор c в рамке v."""
    g = _column(200, room)
    st = axial.place(g, canvas=(600.0, 200.0))
    y = _ys(g)
    assert st["scale_eff"] == 0.0 and st["pruned"] == {}   # запасной attempt
    assert st["pipes_y"] == 0 and st["valve_pipe_eff"] == 0.0
    with_min_pipe = -room + 12.0 + axial.VALVE_CLEAR       # c снаружи рамки v
    assert st["over"] == pytest.approx(-room)              # только блоки
    assert st["over"] < with_min_pipe - 1.0
    assert abs(y["c"] - y["v"]) < 12.0                     # плата: c в рамке v


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


def test_valve_only_step_stats_split_pipe_and_valve_floor():
    """Статистика ступени: прочие трубы сняты (pipe_eff 0), пол клапана —
    отдельным полем; на обычной доле pipe_eff = доля·12, пол = min(., 6)."""
    st = axial.place(_column(200, 16), canvas=(600.0, 200.0))
    assert (st["pipe_eff"], st["valve_pipe_eff"]) == (0.0, axial.VALVE_PIPE)
    st = axial.place(_column(400, 60), canvas=(600.0, 400.0))
    assert (st["pipe_eff"], st["valve_pipe_eff"]) == (12.0, axial.VALVE_PIPE)


def test_valve_pipe_gap_not_monotone_at_step_edge():
    """Объявленный разрыв (AUTO_LAYOUT): запас холста больше — труба клапана
    короче. room 22 — ступень, пол VALVE_PIPE; room 23 — влезла доля > 0,
    пол min(доля·12, 6) < 6, зато чужой коннектор q выходит на кромку P;
    room 28 — доля 1, снова 6. Сторож: менять — только с замером корпуса."""
    def gaps(room):
        g = _column(200, room)
        st = axial.place(g, canvas=(600.0, 200.0))
        y = _ys(g)
        return st["scale_eff"], y["c"] - y["v"] - 12.0, y["q"] - y["P"] - 10.0
    s22, c22, q22 = gaps(22)
    s23, c23, q23 = gaps(23)
    s28, c28, _ = gaps(28)
    assert s22 == 0.0 and c22 == pytest.approx(axial.VALVE_PIPE)
    assert q22 < -1.0                      # ступень: q остаётся в рамке P
    assert 0.0 < s23 < 1.0 and 0.0 < c23 < axial.VALVE_PIPE - 1.0
    assert q23 == pytest.approx(0.0, abs=1e-6)
    assert s28 == 1.0 and c28 == pytest.approx(axial.VALVE_PIPE)


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


def test_prune_with_floor_stops_at_min_pipe():
    """floor: ослабление до полугабаритов + floor, труба не снимается, даже
    если и так не влезает (ступень «только трубы клапанов», VALVE_CLEAR)."""
    by_key = {"a": _it("a", 0, 10), "b": _it("b", 0, 10)}
    idx = {"a": 0, "b": 1}
    kept, st = axial.prune_pipes(2, [(10.0, 10.0)] * 2, [],
                                 [("a", "b", 26.0)], by_key, idx, "oy", "hh",
                                 30.0, floor=axial.VALVE_CLEAR)
    assert st == {"touch": 1, "drop": 0}
    assert kept == [("a", "b", 20.0 + axial.VALVE_CLEAR)]
    kept, st = axial.prune_pipes(2, [(10.0, 10.0)] * 2, [],
                                 [("a", "b", 26.0)], by_key, idx, "oy", "hh",
                                 60.0, floor=axial.VALVE_CLEAR)
    assert st == {"touch": 0, "drop": 0} and kept == [("a", "b", 26.0)]

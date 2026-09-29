# -*- coding: utf-8 -*-
"""Р5, блок 5: поворот одиночного клапана по трубе (`layout.valve_turn`).

Клапан степени 1 с трубой поперёк оси: поворот (рамка w↔h вокруг талии) и
сдвиг поперёк новой оси до линии трубы — прямая в талию; сдвиг упёрся в
соседа — только поворот (Z); поворот упёрся — ось прежняя, крючок в
осевую талию (не больше 3 изломов); predohran не поворачивается.
Координаты: точки данных [y, x], bbox [x1, y1, x2, y2].
"""
from __future__ import annotations

import pytest

pytest.importorskip("shapely", reason="судьи раскладки считаются на shapely")

from modules.graph.core import contact  # noqa: E402
from modules.graph.core.layout import spread  # noqa: E402
from modules.graph.core.layout._avoid_binding import avoid_available  # noqa: E402
from modules.graph.core.layout.valve_turn import turn_single_valves  # noqa: E402
from modules.graph.core.seating import reseat_all_endpoints  # noqa: E402


def _valve(cn, w, h, cx=200.0, cy=100.0, axis="H"):
    n = {"id": "v", "type": "equipment", "class_name": cn, "_axis": axis,
         "centroid": [cy, cx]}
    n["bbox"] = contact.box_around_waist(n, n["centroid"], w, h)
    return n


def _conn(nid, cx, cy):
    return {"id": nid, "type": "connector", "class_name": "connection",
            "centroid": [cy, cx]}


def _block(nid, bb):
    return {"id": nid, "type": "block", "class_name": "testblock",
            "centroid": [(bb[1] + bb[3]) / 2, (bb[0] + bb[2]) / 2],
            "bbox": list(bb)}


def _g(*extra, valve=None):
    return {"nodes": [valve or _valve("armatura_ruchn", 42.0, 21.0),
                      _conn("c", 205.0, 300.0)] + list(extra),
            "links": [{"id": "e", "source": "v", "target": "c",
                       "waypoints": []}]}


def _route(g):
    from modules.graph.core.layout.avoid_router import route_graph
    reseat_all_endpoints(g, model=True, slots=True, axial=True)
    ends = {}
    got = route_graph(g, ends_out=ends)
    e = g["links"][0]
    sp, tp = ends.get("e", (e["source_point"], e["target_point"]))
    return [sp] + got.get("e", []) + [tp]


def _bends(pts):
    return sum(1 for i in range(1, len(pts) - 1)
               if (abs(pts[i][0] - pts[i - 1][0]) > 1e-6)
               != (abs(pts[i + 1][0] - pts[i][0]) > 1e-6))


@pytest.mark.parametrize("cn,w,h,bbox", [
    ("armatura_ruchn", 42.0, 21.0, [194.5, 79.0, 215.5, 121.0]),
    # асимметричная талия (ELECTRIC_VLV V, REVERSE): рамка левее талии
    ("armatura_electro", 42.0, 37.8, [177.7084, 79.0, 215.5084, 121.0]),
])
def test_single_valve_turns_and_waist_lands_on_pipe(cn, w, h, bbox):
    """Труба вниз к коннектору (205, 300) у клапана H: поворот в V и сдвиг
    на 5 px — талия x = 205 на линии трубы, вход прямой в B."""
    g = _g(valve=_valve(cn, w, h))
    st = turn_single_valves(g)
    v = g["nodes"][0]
    assert (st["turned"], st["turn_shift_blocked"], st["turn_blocked"]) \
        == (1, 0, 0)
    assert st["shift_px"] == [5.0]
    assert v["_axis"] == "V"
    assert v["centroid"] == [pytest.approx(100.0), pytest.approx(205.0)]
    assert v["bbox"] == [pytest.approx(x) for x in bbox]
    reseat_all_endpoints(g, model=True, slots=True, axial=True)
    assert g["links"][0]["source_point"] == [121.0, pytest.approx(205.0)]


def test_turn_along_axis_pipe_untouched():
    """Труба вдоль оси — поворота нет, клапан бит-в-бит."""
    g = _g()
    g["nodes"][1]["centroid"] = [104.0, 400.0]
    before = dict(g["nodes"][0])
    st = turn_single_valves(g)
    assert st["turned"] == st["turn_blocked"] == 0
    assert g["nodes"][0] == before


@pytest.mark.parametrize("legal", [None, {("b", "v")}])
def test_turn_shift_blocked_gives_z(legal):
    """Сосед мешает сдвигу (рамка после сдвига задела бы его), но не
    повороту: клапан только повёрнут, ось V, центроид на месте, наложений
    нет; труба к коннектору — ортогональная, с изломом. Пара легальна
    (наложение из построения, судья наложений её не считает) — сдвиг всё
    равно стоит: «упирается в соседа» судит `graph_access.box_hits`."""
    blk = _block("b", [213.0, 40.0, 253.0, 85.0])
    g = _g(blk)
    st = turn_single_valves(g, legal)
    v = g["nodes"][0]
    assert (st["turned"], st["turn_shift_blocked"], st["turn_blocked"]) \
        == (1, 1, 0)
    assert v["_axis"] == "V"
    assert v["centroid"] == [100.0, 200.0]
    assert v["bbox"] == [189.5, 79.0, 210.5, 121.0]
    assert spread.overlaps(g) == 0
    if avoid_available():
        pts = _route(g)
        assert pts[0] == [121.0, 200.0]                 # вход в талию B
        assert 1 <= _bends(pts) <= 3


def test_turn_blocked_keeps_axis_and_hooks_into_waist():
    """Повёрнутая рамка (y 79..121) задела бы соседа сверху: ось прежняя,
    turn_blocked; конец — в осевой талии (L/R на y = 100), крючок не больше
    3 изломов, без косых."""
    blk = _block("b", [180.0, 40.0, 220.0, 85.0])
    g = _g(blk)
    st = turn_single_valves(g)
    v = g["nodes"][0]
    assert (st["turned"], st["turn_blocked"]) == (0, 1)
    assert st["blocked_ids"] == ["v"]
    assert v["_axis"] == "H" and v["bbox"] == [179.0, 89.5, 221.0, 110.5]
    reseat_all_endpoints(g, model=True, slots=True, axial=True)
    sp = g["links"][0]["source_point"]
    assert sp[0] == pytest.approx(100.0) and sp[1] in (179.0, 221.0)
    if avoid_available():
        pts = _route(g)
        assert pts[0][0] == pytest.approx(100.0) \
            and pts[0][1] in (179.0, 221.0)
        assert all(min(abs(pts[i][0] - pts[i - 1][0]),
                       abs(pts[i][1] - pts[i - 1][1])) <= 1e-6
                   for i in range(1, len(pts)))
        assert 1 <= _bends(pts) <= 3


def test_turn_rejected_when_defects_grow():
    """Судьи раскладки поверх box_hits: коннектор в 14.5 px под клапаном —
    повёрнутая рамка (низ 121) оставила бы трубе 4 px < FLOOR. Дефект
    вырос бы — поворота нет."""
    g = _g()
    g["nodes"][1]["centroid"] = [125.0, 200.0]
    st = turn_single_valves(g)
    assert (st["turned"], st["turn_blocked"]) == (0, 1)
    assert g["nodes"][0]["_axis"] == "H"


def test_relief_valve_not_turned():
    """predohran (RELIEF_VLV, угловой) — вне правила: не поворачивается."""
    g = _g(valve=_valve("predohran", 42.0, 31.5))
    before = dict(g["nodes"][0])
    st = turn_single_valves(g)
    assert st["turned"] == st["turn_blocked"] == 0
    assert g["nodes"][0] == before


def test_pinned_or_legacy_valve_not_turned():
    """Пин оператора на конце у клапана и старый холст — не трогаются."""
    g = _g()
    g["links"][0]["pin_source"] = {"dx": 0.0, "dy": 10.5}   # на B
    before = dict(g["nodes"][0])
    assert turn_single_valves(g)["turned"] == 0
    assert g["nodes"][0] == before
    g = _g()
    g["nodes"][0][contact.LEGACY_KEY] = True
    assert turn_single_valves(g)["turned"] == 0
    assert g["nodes"][0]["_axis"] == "H"


# ─────────── стенд layout_bench: концы клапанов по граням (Р5) ───────────

def _bench():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "lb_valve_ends", Path(__file__).resolve().parents[1]
        / "tools" / "layout_bench.py")
    lb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lb)
    return lb


def _faces_graph(valve=None):
    """Клапан H рамка [179, 89.5, 221, 110.5], талия y = 100: e1 в талии L,
    e2 на T (неосевая грань)."""
    g = _g(valve=valve)
    g["nodes"].append(_conn("a", 100.0, 100.0))
    g["links"] = [
        {"id": "e1", "source": "v", "target": "a", "waypoints": [],
         "source_point": [100.0, 179.0], "target_point": [100.0, 100.0]},
        {"id": "e2", "source": "v", "target": "c", "waypoints": [],
         "source_point": [89.5, 200.0], "target_point": [300.0, 205.0]}]
    return g


def test_bench_valve_ends_by_faces():
    """Судья стенда: неосевой конец клапана степени 1–2 назван поимённо;
    клапан turn_blocked — в отдельном списке; пин — вне счёта; осевой конец
    мимо талии — не «в талии», но и не неосевой; степень 3+ — отчётно;
    predohran — свой счётчик."""
    ve = _bench().valve_ends
    g = _faces_graph()
    r = ve(g)
    assert (r["ends"], r["in_waist"], r["nonaxial_3p"], r["relief"]) \
        == (2, 1, 0, 0)
    assert r["nonaxial_12"] == ["v/e2"] and r["nonaxial_blocked"] == []
    r = ve(g, {"v"})
    assert r["nonaxial_12"] == [] and r["nonaxial_blocked"] == ["v/e2"]
    g["links"][1]["pin_source"] = {"dx": 0.0, "dy": -10.5}   # пин на T
    assert ve(g)["nonaxial_12"] == []
    g = _faces_graph()
    g["links"][0]["source_point"] = [105.0, 179.0]             # L мимо талии
    r = ve(g)
    assert r["in_waist"] == 0 and r["nonaxial_12"] == ["v/e2"]
    g = _faces_graph()
    g["nodes"].append(_conn("d", 300.0, 100.0))
    g["links"].append({"id": "e3", "source": "v", "target": "d",
                       "waypoints": [], "source_point": [100.0, 221.0],
                       "target_point": [100.0, 300.0]})
    r = ve(g)
    assert (r["in_waist"], r["nonaxial_3p"], r["nonaxial_12"]) == (2, 1, [])
    r = ve(_faces_graph(_valve("predohran", 42.0, 31.5)))
    assert (r["ends"], r["relief"], r["in_waist"], r["nonaxial_12"]) \
        == (2, 2, 0, [])


def test_bench_check_flags_nonaxial_and_names_blocked(tmp_path, monkeypatch):
    """check(): неосевой вход у степени 1–2 — регрессия стенда; у клапана из
    stats valve_turn.blocked_ids (turn_blocked) — принят поимённо."""
    import json
    lb = _bench()
    (tmp_path / "tiny.json").write_text(json.dumps(_g()), encoding="utf-8")
    r = lb.check("tiny", str(tmp_path), with_text=False)
    assert (r["turned"], r["valve_ends"], r["valve_in_waist"]) == (1, 1, 1)
    assert r["nonaxial_12"] == [] and r["regressions"] == []

    real_ve, real_layout = lb.valve_ends, lb.layout

    def fake_ve(graph, exempt=()):
        key = "nonaxial_blocked" if "v" in exempt else "nonaxial_12"
        return dict(real_ve(graph, exempt), **{key: ["v/e"]})

    monkeypatch.setattr(lb, "valve_ends", fake_ve)
    r = lb.check("tiny", str(tmp_path), with_text=False)
    assert r["regressions"] == ["неосевой вход клапана степени 1–2"]

    def blocked_layout(g, p, stages=None):
        out, st = real_layout(g, p, stages=stages)
        st["valve_turn"]["blocked_ids"] = ["v"]
        return out, st

    monkeypatch.setattr(lb, "layout", blocked_layout)
    r = lb.check("tiny", str(tmp_path), with_text=False)
    assert r["nonaxial_blocked"] == ["v/e"] and r["regressions"] == []

# -*- coding: utf-8 -*-
"""Модель точек прикрепления (решение заказчика 2026-09-22) в ядре посадки.

  * клапан холста — выверенный порт «талии» (жёсткий: ради прямой не уступает);
  * середина прочей рамки — уступает ради прямой не дальше MID_SNAP (3 px);
  * конец без замка оси — в опорную точку грани, а не «куда-то»;
  * несколько концов на грани — слоты вокруг опорной точки;
  * пин оператора — истина;
  * канон (`model=False`) — прежний: внутренние стадии раскладки и растр.
Координаты: точки данных [y, x], bbox [x1, y1, x2, y2].
"""
from __future__ import annotations

import pytest

from modules.graph.core import contact, edit_engine, ports, seating
from modules.graph.core.graph_access import nodes_by_id

# armatura_electro по оси H: бокс 42x37.8, талия ниже центра на 8.3916
VALVE_H = {"id": "v", "type": "equipment", "class_name": "armatura_electro",
           "_axis": "H", "centroid": [100.0, 200.0],
           "bbox": [179.0, 81.1, 221.0, 118.9]}
WAIST_Y = 100.0 + 0.222 * 37.8           # 108.3916


def _box(nid, cx, cy, w=40.0, h=40.0, cn="unknow"):
    return {"id": nid, "type": "equipment", "class_name": cn,
            "centroid": [cy, cx],
            "bbox": [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]}


def _conn(nid, cx, cy):
    return {"id": nid, "type": "connector", "class_name": "connector",
            "centroid": [cy, cx], "bbox": None}


def _edge(eid, s, t, sp=None, tp=None, wps=None):
    return {"id": eid, "source": s, "target": t,
            "source_point": sp, "target_point": tp,
            "waypoints": list(wps or [])}


def _graph(nodes, links):
    return {"nodes": nodes, "links": links, "graph": {}}


# ─────────────────────────────── порты ───────────────────────────────

def test_candidate_ports_of_valve_sit_in_waist():
    got = ports.candidate_ports(VALVE_H)
    right, left, top, bottom = got
    assert right[:2] == (221.0, pytest.approx(WAIST_Y))
    assert left[:2] == (179.0, pytest.approx(WAIST_Y))
    # неосевые грани — середина
    assert top[:2] == (200.0, 81.1) and bottom[:2] == (200.0, 118.9)


def test_candidate_ports_of_box_unchanged():
    b = _box("b", 100.0, 100.0)
    assert ports.candidate_ports(b) == [
        (120.0, 100.0, 1.0, 0.0, False), (80.0, 100.0, -1.0, 0.0, False),
        (100.0, 80.0, 0.0, -1.0, False), (100.0, 120.0, 0.0, 1.0, False)]


def test_raster_valve_ports_unchanged():
    """Клапан без `_axis` (растр) — порты в центрах граней, как прежде."""
    v = dict(VALVE_H)
    v.pop("_axis")
    assert ports.candidate_ports(v)[0][:2] == (221.0, pytest.approx(100.0))


def test_side_slots_center_on_waist_and_keep_corner_margin():
    one = ports.side_slots(VALVE_H, "R", 1)
    assert one[0][:2] == (221.0, pytest.approx(WAIST_Y))
    two = ports.side_slots(VALVE_H, "R", 2)
    ys = [s[1] for s in two]
    # К7 (блок 2): до нижнего угла от талии 10.5 px, веер с шагом
    # min(18, 37.8/3) = 12.6 вокруг талии упёрся бы в угол — он сдвинут
    # вверх до отступа 6 px, шаг не сжат (до К7: 9.016 вокруг талии).
    assert ys == pytest.approx([100.3, 112.9])
    assert max(ys) == pytest.approx(118.9 - ports.VERTEX_MARGIN)


def test_side_slots_of_box_bit_identical():
    b = _box("b", 100.0, 100.0)
    s = ports.side_slots(b, "R", 3)                   # min(18, 40/4) = 10
    assert [p[1] for p in s] == [90.0, 100.0, 110.0]


# ─────────── веер у смещённой талии (К7, блок 2, шаг 2b′) ───────────

def _valve(cn, axis, w, h, cx=100.0, cy=100.0):
    return {"id": cn, "type": "equipment", "class_name": cn, "_axis": axis,
            "centroid": [cy, cx],
            "bbox": [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]}


def _along(slots, side):
    return [s[0] if side in ("T", "B") else s[1] for s in slots]


def _side_slots_pre_k7(node, side, k, pitch=None):
    """Копия `ports.side_slots` до К7 (aab3642): эталон «бит-в-бит»."""
    x1, y1, x2, y2 = node["bbox"]
    lo, hi = (x1, x2) if side in ("T", "B") else (y1, y2)
    center = (lo + hi) / 2.0
    ref = (contact.face_refs(node, node["bbox"]) or {}).get(side)
    if k == 1:
        pitch = 0.0
    elif pitch is None:
        pitch = min(ports.SLOT_PITCH, (hi - lo) / (k + 1))
    if ref is not None:
        center = ref
        if k > 1:
            half = min(center - lo, hi - center) - ports.VERTEX_MARGIN
            pitch = max(0.0, min(pitch, 2.0 * half / (k - 1)))
    return [center + (j - (k - 1) / 2.0) * pitch for j in range(k)]


# рабочие рамки FIXED_SIZES: regulator_electro (CTRL_VLV_ELEC, 0.314) H
# 42x37.8 — от талии до угла 7.03 px; armatura_seroprivod (AOC_VLV, 0.341)
# V 28x42 — 4.45 px. До К7 шаг веера сжимался до 2.06 и 0 px.
K7_FACES = [("regulator_electro", "H", 42.0, 37.8, "L"),
            ("regulator_electro", "H", 42.0, 37.8, "R"),
            ("armatura_seroprivod", "V", 28.0, 42.0, "T"),
            ("armatura_seroprivod", "V", 28.0, 42.0, "B")]


@pytest.mark.parametrize("k", [2, 3])
@pytest.mark.parametrize("cn,axis,w,h,side", K7_FACES)
def test_fan_at_offset_waist_moves_inside_the_face(cn, axis, w, h, side, k):
    """Шаг не сжимается (зазор между краями линий >= INK_GAP), веер
    симметричен вокруг сдвинутого центра, крайний слот — ровно на отступе
    VERTEX_MARGIN от угла, центр ближе к середине грани, чем талия."""
    v = _valve(cn, axis, w, h)
    x1, y1, x2, y2 = v["bbox"]
    lo, hi = (x1, x2) if side in ("T", "B") else (y1, y2)
    mid = (lo + hi) / 2.0
    waist = contact.face_refs(v, v["bbox"])[side]
    got = _along(ports.side_slots(v, side, k), side)
    pitch = min(ports.SLOT_PITCH, (hi - lo) / (k + 1))
    gaps = [b - a for a, b in zip(got, got[1:])]
    assert gaps == pytest.approx([pitch] * (k - 1)), "шаг сжат"
    assert min(gaps) - ports.INK_DEFAULT >= ports.INK_GAP, "чернила слиплись"
    c = sum(got) / k
    assert [a + b for a, b in zip(got, reversed(got))] \
        == pytest.approx([2.0 * c] * k), "веер не симметричен"
    assert abs(c - mid) < abs(waist - mid), "веер не сдвинут внутрь"
    assert min(min(got) - lo, hi - max(got)) \
        == pytest.approx(ports.VERTEX_MARGIN), "не упёрся в отступ"


def test_fan_at_offset_waist_literals():
    """Абсолютные числа (Т5 блока 1 — armatura_seroprivod V, bbox x
    100..128, k=2 на T-грани: до К7 оба слота 123.548, шаг 0)."""
    aoc = _valve("armatura_seroprivod", "V", 28.0, 42.0, cx=114.0)
    assert _along(ports.side_slots(aoc, "T", 2), "T") \
        == pytest.approx([112.667, 122.0], abs=1e-3)
    assert _along(ports.side_slots(aoc, "B", 3), "B") \
        == pytest.approx([108.0, 115.0, 122.0])
    reg = _valve("regulator_electro", "H", 42.0, 37.8)        # талия 111.869
    assert _along(ports.side_slots(reg, "R", 2), "R") \
        == pytest.approx([100.3, 112.9])
    assert _along(ports.side_slots(reg, "R", 3), "R") \
        == pytest.approx([94.0, 103.45, 112.9])


def test_fan_that_fits_around_the_waist_is_bit_identical():
    """ELECTROMAGNETIC_VLV (0.149): веер 2 x 12.6 вокруг талии влезает —
    центр ровно талия, выход бит-в-бит с до-К7."""
    v = _valve("armatura_membr_electro", "H", 42.0, 37.8)
    got = _along(ports.side_slots(v, "R", 2), "R")
    assert got == _side_slots_pre_k7(v, "R", 2)
    assert sum(got) / 2 == pytest.approx(contact.face_refs(v, v["bbox"])["R"])


def test_fan_that_does_not_fit_goes_to_the_middle_and_shrinks():
    """Шаг вызывающего 12 при k=4 на грани 37.8: веер 36 > 37.8 - 2·6 —
    в середину грани, шаг (37.8 - 12) / 3 = 8.6, крайние на отступе."""
    v = _valve("regulator_electro", "H", 42.0, 37.8)
    got = _along(ports.side_slots(v, "R", 4, pitch=12.0), "R")
    assert got == pytest.approx([87.1, 95.7, 104.3, 112.9])


@pytest.mark.parametrize("pitch", [None, 8.0, 30.0])
@pytest.mark.parametrize("k", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("side", ["L", "R", "T", "B"])
@pytest.mark.parametrize("node", [
    _box("b", 100.0, 100.0),
    _box("n", 100.0, 100.0, w=45.0, h=45.0, cn="nasos"),
    {k_: v_ for k_, v_ in VALVE_H.items() if k_ != "_axis"},     # растр
    _valve("armatura_ruchn", "H", 42.0, 21.0),        # HANDLE_VLV, доля 0
    _valve("klapan_obratn", "V", 21.0, 42.0),         # CHECK_VLV, доля 0
], ids=["box", "nasos", "raster_valve", "handle_h", "check_v"])
def test_side_slots_without_offset_waist_bit_identical(node, side, k, pitch):
    """У не-клапанов и клапанов с талией в центре К7 не меняет ни бита."""
    got = _along(ports.side_slots(node, side, k, pitch=pitch), side)
    assert got == _side_slots_pre_k7(node, side, k, pitch=pitch)


# ─────────────────────────── канон и модель ───────────────────────────

def test_legacy_canon_unchanged_by_default():
    """Бокс и коннектор на 10 px ниже середины: канон — прямая «куда-то»."""
    g = _graph([_box("a", 100.0, 100.0), _conn("c", 300.0, 110.0)],
               [_edge("e", "a", "c")])
    seating.reseat_all_endpoints(g)
    assert g["links"][0]["source_point"] == [110.0, 120.0]


def test_model_middle_yields_within_mid_snap_only():
    g = _graph([_box("a", 100.0, 100.0), _conn("c", 300.0, 102.0)],
               [_edge("e", "a", "c")])
    seating.reseat_all_endpoints(g, model=True)
    assert g["links"][0]["source_point"] == [102.0, 120.0]   # прямая

    g = _graph([_box("a", 100.0, 100.0), _conn("c", 300.0, 110.0)],
               [_edge("e", "a", "c")])
    seating.reseat_all_endpoints(g, model=True)
    assert g["links"][0]["source_point"] == [100.0, 120.0]   # середина


def test_model_valve_port_is_hard_and_neighbour_aligns_to_it():
    """Бокс напротив талии (в пределах своего допуска) выравнивается на
    линию талии; сам клапан из талии не уходит."""
    box = _box("b", 320.0, WAIST_Y - 2.0)             # середина на 2 px выше
    g = _graph([dict(VALVE_H), box], [_edge("e", "v", "b")])
    seating.reseat_all_endpoints(g, model=True)
    sp, tp = g["links"][0]["source_point"], g["links"][0]["target_point"]
    assert sp == [pytest.approx(WAIST_Y), 221.0]
    assert tp == [pytest.approx(WAIST_Y), 300.0]


def test_model_valve_port_does_not_yield_to_connector():
    """Коннектор на центральной линии бокса клапана (а не на талии): порт
    не уступает — конец в талии, ступеньку разводит маршрут."""
    g = _graph([dict(VALVE_H), _conn("c", 400.0, 100.0)],
               [_edge("e", "v", "c")])
    seating.reseat_all_endpoints(g, model=True)
    assert g["links"][0]["source_point"] == [pytest.approx(WAIST_Y), 221.0]


def test_model_anchor_without_lock_is_the_port_not_the_neighbour():
    """Сосед далеко по диагонали: канон зажал бы его координату в грань,
    модель ставит конец в опорную точку."""
    b = _box("a", 100.0, 100.0)
    assert seating.node_anchor(b, 400.0, 115.0) == (120.0, 115.0)
    assert seating.node_anchor(b, 400.0, 115.0, model=True) == (120.0, 100.0)
    assert seating.node_anchor(VALVE_H, 400.0, 90.0, model=True) \
        == (221.0, pytest.approx(WAIST_Y))


def test_model_pin_is_truth():
    e = _edge("e", "a", "c")
    e["pin_source"] = {"dx": 20.0, "dy": -12.0}         # правая грань, выше
    g = _graph([_box("a", 100.0, 100.0), _conn("c", 300.0, 100.0)], [e])
    seating.reseat_all_endpoints(g, model=True)
    assert g["links"][0]["source_point"] == [88.0, 120.0]


# ─────────────────────────────── слоты ───────────────────────────────

def test_server_slots_around_waist_ordered_by_far_ends():
    far_hi = _box("u", 400.0, 60.0)                     # выше
    far_lo = _box("d", 400.0, 160.0)                    # ниже
    g = _graph([dict(VALVE_H), far_hi, far_lo],
               [_edge("e1", "v", "d"), _edge("e2", "v", "u")])
    seating.reseat_all_endpoints(g, model=True, slots=True)
    ys = {e["id"]: e["source_point"][0] for e in g["links"]}
    assert ys["e2"] < ys["e1"]                          # порядок по дальним
    # веер у талии 108.39 сдвинут внутрь грани (К7): 106.6 ∓ 6.3
    assert (ys["e2"], ys["e1"]) == pytest.approx((100.3, 112.9))
    assert all(e["source_point"][1] == 221.0 for e in g["links"])


def test_server_slots_partner_follows_when_it_can_yield():
    """Две прямые трубы из одной грани бокса в два бокса напротив: концы
    разъехались слотами, партнёры (середины в пределах допуска) доехали на
    те же линии — трубы остались прямыми."""
    a = _box("a", 100.0, 100.0, h=60.0)
    p = _box("p", 300.0, 100.0, h=60.0)
    q = _box("q", 300.0, 100.0, h=60.0)
    q["id"] = "q"
    g = _graph([a, p, q], [_edge("e1", "a", "p"), _edge("e2", "a", "q")])
    seating.reseat_all_endpoints(g, model=True, slots=True)
    for e in g["links"]:
        assert e["source_point"][0] != 100.0            # слот, не середина
    # слоты симметричны середине a
    ys = sorted(e["source_point"][0] for e in g["links"])
    assert (ys[0] + ys[1]) / 2 == pytest.approx(100.0)


def test_slot_faces_counts_only_shared_faces():
    g = _graph([dict(VALVE_H), _box("u", 400.0, 60.0), _box("d", 400.0, 160.0)],
               [_edge("e1", "v", "d"), _edge("e2", "v", "u")])
    seating.reseat_all_endpoints(g, model=True, slots=True)
    assert seating.slot_faces(g) == {("v", "R")}


# ───────────────────────── страховка прямизны ─────────────────────────

def test_straighten_unrouted_uses_full_face_for_boxes():
    """Ступенька 10 px без маршрута: страховка выпрямляет, середина бокса
    уступает на всю грань."""
    g = _graph([_box("a", 100.0, 100.0), _conn("c", 300.0, 110.0)],
               [_edge("e", "a", "c", sp=[100.0, 120.0], tp=[110.0, 300.0])])
    assert seating.straighten_unrouted(g) == 1
    assert g["links"][0]["source_point"] == [110.0, 120.0]


def test_straighten_unrouted_box_yields_before_valve_port():
    """Сначала уступает середина бокса (на всю грань) — порт клапана держится;
    ребро помечается `_slack=1`, и повторная посадка держит его прямым."""
    box = _box("b", 320.0, 100.0)                      # середина бокса 100
    g = _graph([dict(VALVE_H), box],
               [_edge("e", "v", "b", sp=[WAIST_Y, 221.0], tp=[100.0, 300.0])])
    stats = {}
    assert seating.straighten_unrouted(g, stats=stats) == 1
    e = g["links"][0]
    assert e["source_point"] == [pytest.approx(WAIST_Y), 221.0]
    assert e["target_point"] == [pytest.approx(WAIST_Y), 300.0]
    assert e["_slack"] == 1 and stats["ports_yielded"] == 0
    seating.reseat_all_endpoints(g, model=True, slots=True)
    assert e["target_point"] == [pytest.approx(WAIST_Y), 300.0]


def test_straighten_unrouted_last_resort_yields_valve_port():
    """Обе точки жёсткие (порт клапана и коннектор) и разошлись, маршрута нет:
    последний рубеж — порт уступает, труба прямая, а не косая; `_slack=2`,
    счётчик ports_yielded."""
    g = _graph([dict(VALVE_H), _conn("c", 400.0, 100.0)],
               [_edge("e", "v", "c", sp=[WAIST_Y, 221.0], tp=[100.0, 400.0])])
    stats = {}
    assert seating.straighten_unrouted(g, stats=stats) == 1
    e = g["links"][0]
    assert e["source_point"] == [100.0, 221.0]
    assert e["_slack"] == 2 and stats["ports_yielded"] == 1
    seating.reseat_all_endpoints(g, model=True, slots=True)
    assert e["source_point"] == [100.0, 221.0]          # повтор не ломает


# ─────────────── сборка холста: клапан на ряд линией талии ───────────────

def _fixed(node, pts):
    """apply_fixed_sizes над одним узлом с точками подключения pts [(y, x)]."""
    from modules.graph.core import pretransform
    g = {"nodes": [node], "links": []}
    adj = {node["id"]: [(f"e{i}", p) for i, p in enumerate(pts)]}
    pretransform.apply_fixed_sizes(g, adj)
    return g["nodes"][0]


def test_valve_centroid_goes_to_raster_pipe_line_and_box_to_its_waist():
    """Горизонтальный electro-клапан: труба по растру на 5 px ниже центроида
    детекции — центроид встаёт на линию трубы, бокс так, что талия на ней."""
    n = _fixed({"id": "v", "class_name": "armatura_electro",
                "centroid": [100.0, 200.0]},
               [(105.0, 170.0), (105.0, 230.0)])
    assert n["_axis"] == "H"
    assert n["centroid"] == [105.0, 200.0]
    x1, y1, x2, y2 = n["bbox"]
    assert (y2 - y1) == pytest.approx(37.8)
    port = contact.face_refs(n, tuple(n["bbox"]))["L"]
    assert port == pytest.approx(105.0)                   # талия на трубе
    assert (y1 + y2) / 2 == pytest.approx(105.0 - 0.222 * 37.8)


@pytest.mark.parametrize("cn,y1,y2", [
    # CTRL_VLV_ELEC 0.314 (Р6; было 0.15): центр рамки ВЫШЕ трубы на 11.8692
    ("regulator_electro", 74.2308, 112.0308),
    # CHECK_HYDRO_VLV −0.041 (Р6; было 0.15): талия ВЫШЕ центра — центр
    # рамки НИЖЕ трубы на 1.5498, бокс уходит вниз от трубы
    ("klapan_obratn_seroprivod", 87.6498, 125.4498),
    # ELECTROMAGNETIC_VLV 0.149 (Р6; строки не было — бокс стоял по центру)
    ("armatura_membr_electro", 80.4678, 118.2678),
])
def test_valve_box_on_pipe_by_measured_waist(cn, y1, y2):
    """Сборка холста (T4 блока 2): труба по растру y=105 — центроид на
    трубе, рамка 42x37.8 стоит так, что талия по таблице Р6 на трубе.
    Литералы абсолютные (не из таблицы)."""
    n = _fixed({"id": "v", "class_name": cn, "centroid": [100.0, 200.0]},
               [(105.0, 170.0), (105.0, 230.0)])
    assert n["_axis"] == "H"
    assert n["centroid"] == [105.0, 200.0]
    assert n["bbox"] == pytest.approx([179.0, y1, 221.0, y2], abs=1e-9)
    refs = contact.face_refs(n, tuple(n["bbox"]))
    assert refs["L"] == refs["R"] == pytest.approx(105.0, abs=1e-9)


def test_valve_without_axis_points_puts_waist_on_detected_centre():
    """Точек на осевых гранях нет (синтетика, точки в центроиде) — линией
    трубы считается центроид детекции, талия садится на неё."""
    n = _fixed({"id": "v", "class_name": "armatura_seroprivod",
                "centroid": [100.0, 200.0]},
               [(100.0, 200.0)])
    port = contact.face_refs(n, tuple(n["bbox"]))
    side = "L" if n["_axis"] == "H" else "T"
    along = n["centroid"][0] if n["_axis"] == "H" else n["centroid"][1]
    assert port[side] == pytest.approx(along)
    assert along == pytest.approx(100.0 if n["_axis"] == "H" else 200.0)


def test_valve_pipe_line_outside_box_falls_back_to_centre():
    n = _fixed({"id": "v", "class_name": "armatura_electro",
                "centroid": [100.0, 200.0]},
               [(125.0, 100.0), (125.0, 300.0)])     # 25 px ниже — вне бокса
    assert n["_axis"] == "H"
    assert n["centroid"][0] == pytest.approx(100.0)
    assert contact.face_refs(n, tuple(n["bbox"]))["L"] == pytest.approx(100.0)


def test_non_valve_skin_box_stays_centred():
    n = _fixed({"id": "p", "class_name": "nasos",
                "centroid": [100.0, 200.0]},
               [(104.0, 170.0), (104.0, 230.0)])
    assert n["centroid"] == [100.0, 200.0]
    x1, y1, x2, y2 = n["bbox"]
    assert ((x1 + x2) / 2, (y1 + y2) / 2) == (200.0, 100.0)


# ─────────────────────── движок редактора (seat_end) ───────────────────────

def _seat(node, other, e, role="s"):
    byid = nodes_by_id({"nodes": [node, other]})
    ref = other["centroid"]
    return edit_engine.seat_end(byid[node["id"]], byid[other["id"]], e, role,
                                None, ref[1], ref[0], True, 8.0,
                                node_edges=[e])


def test_editor_seats_valve_end_in_waist():
    other = _box("b", 400.0, 100.0)
    e = _edge("e", "v", "b")
    x, y = _seat(dict(VALVE_H), other, e)
    assert (x, y) == (221.0, pytest.approx(WAIST_Y))


def test_editor_box_end_yields_within_mid_snap():
    near = _conn("c", 400.0, 102.5)
    e = _edge("e", "a", "c", sp=[100.0, 120.0], tp=[102.5, 400.0])
    assert _seat(_box("a", 100.0, 100.0), near, e) == (120.0, 102.5)
    far = _conn("c", 400.0, 108.0)
    e = _edge("e", "a", "c", sp=[100.0, 120.0], tp=[108.0, 400.0])
    assert _seat(_box("a", 100.0, 100.0), far, e) == (120.0, 100.0)


def test_mid_snap_constant():
    assert contact.MID_SNAP == 3.0


# ─────────────────── одна грань слотами (блок 1, К1) ───────────────────

def _two_faces_graph():
    """Две грани по две трубы: R клапана `v` и R бокса `a`; концы стоят
    не в слотах (все в опорной точке грани)."""
    a = _box("a", 100.0, 300.0)                         # R-грань x=120
    g = _graph([dict(VALVE_H), a, _box("u", 400.0, 60.0),
                _box("d", 400.0, 160.0), _conn("c1", 300.0, 290.0),
                _conn("c2", 300.0, 310.0)],
               [_edge("e1", "v", "d", sp=[WAIST_Y, 221.0], tp=[160.0, 380.0],
                      wps=[[WAIST_Y, 300.0], [160.0, 300.0]]),
                _edge("e2", "v", "u", sp=[WAIST_Y, 221.0], tp=[60.0, 380.0],
                      wps=[[WAIST_Y, 300.0], [60.0, 300.0]]),
                _edge("e3", "a", "c1", sp=[300.0, 120.0], tp=[290.0, 300.0],
                      wps=[[300.0, 200.0], [290.0, 200.0]]),
                _edge("e4", "a", "c2", sp=[300.0, 120.0], tp=[310.0, 300.0],
                      wps=[[300.0, 200.0], [310.0, 200.0]])])
    return g


def test_seat_face_slots_only_its_face_like_seat_slots():
    import copy
    full, one = _two_faces_graph(), _two_faces_graph()
    before = copy.deepcopy(one)
    assert seating.seat_slots(full) == 4
    moved = seating.seat_face(one, nodes_by_id(one), "v", "R")
    assert sorted(e["id"] for e, _k in moved) == ["e1", "e2"]
    L = {e["id"]: e for e in one["links"]}
    F = {e["id"]: e for e in full["links"]}
    B = {e["id"]: e for e in before["links"]}
    for eid in ("e1", "e2"):                            # как у seat_slots
        assert L[eid]["source_point"] == F[eid]["source_point"]
        assert L[eid]["waypoints"] == F[eid]["waypoints"]
        assert L[eid]["source_point"] != B[eid]["source_point"]
    for eid in ("e3", "e4"):                            # чужая грань цела
        assert L[eid] == B[eid]
        assert F[eid]["source_point"] != B[eid]["source_point"]
    assert seating.seat_face(one, nodes_by_id(one), "a", "L") == []


def test_seat_face_uses_given_rect():
    """`rect_of` — рамка граней редактора: концы на грани чужой рамки
    (x=240) рамкой по умолчанию не видны, переданной — раскладываются."""
    g = _two_faces_graph()
    for e in g["links"][:2]:
        e["source_point"] = [WAIST_Y, 240.0]
    byid = nodes_by_id(g)
    assert seating.seat_face(g, byid, "v", "R") == []
    wide = lambda n: [179.0, 81.1, 240.0, 118.9] if n["id"] == "v" \
        else seating._anchor_rect(n)
    moved = seating.seat_face(g, byid, "v", "R", rect_of=wide)
    ys = sorted(e["source_point"][0] for e, _k in moved)
    assert len(ys) == 2 and ys[0] < WAIST_Y < ys[1]
    assert all(e["source_point"][1] == 240.0 for e, _k in moved)


# ─────────────────── судья «упирается в соседа» (К1) ───────────────────

def test_box_hits_neighbour_frames():
    from modules.graph.core.graph_access import box_hits
    g = _graph([_box("a", 100.0, 100.0), _box("b", 200.0, 100.0),
                _conn("c", 150.0, 100.0)], [])
    # b: [180, 80, 220, 120]
    assert box_hits(g, "a", [170.0, 90.0, 190.0, 110.0])        # налезла
    assert not box_hits(g, "a", [160.0, 90.0, 180.0, 110.0])    # касание
    assert box_hits(g, "a", [160.0, 90.0, 180.0, 110.0], margin=0.5)
    assert not box_hits(g, "b", [170.0, 90.0, 190.0, 110.0])    # сам себя
    assert not box_hits(g, "a", [140.0, 95.0, 160.0, 105.0])    # коннектор
    # тот же ответ по словарю {id: node} и по списку узлов
    assert box_hits(nodes_by_id(g), "a", [170.0, 90.0, 190.0, 110.0])
    assert box_hits(g["nodes"], "a", [170.0, 90.0, 190.0, 110.0])
    assert not box_hits(nodes_by_id(g), "b", [170.0, 90.0, 190.0, 110.0])


# ─────────── осевые входы клапана степени 1–2 (Р5, блок 5) ───────────

def _deg2(extra_nodes=(), extra_links=(), valve=None):
    """Клапан H степени 2: коннектор a слева на линии талии, c над клапаном."""
    return _graph([dict(valve or VALVE_H), _conn("a", 100.0, WAIST_Y),
                   _conn("c", 200.0, 20.0)] + list(extra_nodes),
                  [_edge("ea", "a", "v"), _edge("ec", "v", "c")]
                  + list(extra_links))


def _pts(g):
    return {e["id"]: (e["source_point"], e["target_point"])
            for e in g["links"]}


def test_degree2_side_neighbour_goes_to_free_axial_face():
    """Сосед сверху у клапана степени 2: вход не на T (V-замок через
    середину ±3), а в свободную осевую талию R — колено за клапаном."""
    g = _deg2()
    seating.reseat_all_endpoints(g, model=True, slots=True, axial=True)
    p = _pts(g)
    assert p["ea"][1] == [pytest.approx(WAIST_Y), 179.0]
    assert p["ec"][0] == [pytest.approx(WAIST_Y), 221.0]
    # без правила — прежнее: вход сверху
    g = _deg2()
    seating.reseat_all_endpoints(g, model=True, slots=True)
    assert _pts(g)["ec"][0] == [81.1, 200.0]


def test_degree3_keeps_side_entry():
    g = _deg2([_conn("d", 200.0, 300.0)], [_edge("ed", "v", "d")])
    seating.reseat_all_endpoints(g, model=True, slots=True, axial=True)
    assert _pts(g)["ec"][0] == [81.1, 200.0]


def test_relief_valve_exempt():
    """predohran (RELIEF_VLV, угловой) — вне правила: вход сверху в середину
    грани T."""
    v = {"id": "v", "type": "equipment", "class_name": "predohran",
         "_axis": "H", "centroid": [100.0, 200.0],
         "bbox": [179.0, 84.25, 221.0, 115.75]}
    g = _deg2(valve=v)
    seating.reseat_all_endpoints(g, model=True, slots=True, axial=True)
    assert _pts(g)["ec"][0] == [84.25, 200.0]


def test_legacy_canvas_valve_untouched():
    """Узел старого холста (`LEGACY_KEY`, Р1) — без правила, даже если
    вызывающий попросил axial: посадка бит-в-бит как без него."""
    v = dict(VALVE_H)
    v[contact.LEGACY_KEY] = True
    assert not contact.axial_only(v, 2)
    g = _deg2(valve=v)
    seating.reseat_all_endpoints(g, axial=True)
    ref = _deg2(valve=v)
    seating.reseat_all_endpoints(ref)
    assert _pts(g) == _pts(ref)
    rect = seating._anchor_rect(v)
    sp = _pts(g)["ec"][0]
    assert seating._end_side(rect, sp[1], sp[0]) == "T"


def test_candidate_ports_sides_filter():
    got = ports.candidate_ports(VALVE_H, sides={"L", "R"})
    assert [p[:2] for p in got] == [(221.0, pytest.approx(WAIST_Y)),
                                    (179.0, pytest.approx(WAIST_Y))]
    assert len(ports.candidate_ports(VALVE_H)) == 4
    # коннектор фильтр не трогает; пустой итог — без фильтра
    assert ports.candidate_ports(_conn("c", 1.0, 2.0), sides={"L"}) \
        == [(1.0, 2.0, 0.0, 0.0, False)]
    assert len(ports.candidate_ports(VALVE_H, sides={"X"})) == 4


def test_choose_port_sides_keeps_current_when_all_backside():
    """Сосед прямо над клапаном, разрешены L/R: обе с изнанки — остаётся
    текущий R, а не L тай-брейком по x."""
    got = ports.choose_port(VALVE_H, (221.0, WAIST_Y), (200.0, 20.0), 8.0,
                            sides={"L", "R"})
    assert got == (221.0, pytest.approx(WAIST_Y))


def test_straighten_does_not_turn_axial_valve_sideways():
    """Страховка: ступенька к соседу сверху не выпрямляется входом в T."""
    links = [_edge("ea", "a", "v", sp=[WAIST_Y, 100.0], tp=[WAIST_Y, 179.0]),
             _edge("ec", "v", "c", sp=[WAIST_Y, 221.0], tp=[20.0, 200.0])]
    nodes = [dict(VALVE_H), _conn("a", 100.0, WAIST_Y), _conn("c", 200.0, 20.0)]
    g = _graph(nodes, links)
    assert seating.straighten_unrouted(g, axial=True) == 0
    assert _pts(g)["ec"][0] == [WAIST_Y, 221.0]
    # без правила — прежнее: середина T уступает, труба прямая сверху
    g = _graph([dict(n) for n in nodes], [dict(e) for e in links])
    assert seating.straighten_unrouted(g) == 1
    assert _pts(g)["ec"][0] == [81.1, 200.0]


def test_pin_beats_axial_rule():
    """Пин — истина и вне правила: вход на T держится. Пиннутый конец в
    осевой грани L занимает её — свободный конец берёт R, хотя его сосед
    тоже слева."""
    g = _deg2()
    g["links"][1]["pin_source"] = {"dx": 0.0, "dy": -18.9}      # T, середина
    seating.reseat_all_endpoints(g, model=True, slots=True, axial=True)
    assert _pts(g)["ec"][0] == [81.1, 200.0]

    ea = _edge("ea", "a", "v", tp=[WAIST_Y, 179.0])
    ea["pin_target"] = {"dx": -21.0, "dy": WAIST_Y - 100.0}      # L, талия
    g = _graph([dict(VALVE_H), _conn("a", 100.0, WAIST_Y),
                _conn("b", 60.0, 300.0)], [ea, _edge("eb", "v", "b")])
    seating.reseat_all_endpoints(g, model=True, slots=True, axial=True)
    assert _pts(g)["ea"][1] == [pytest.approx(WAIST_Y), 179.0]
    assert _pts(g)["eb"][0] == [pytest.approx(WAIST_Y), 221.0]


def test_two_pipes_to_one_partner_share_a_face():
    """Кратные рёбра к ОДНОМУ партнёру (старые холсты): разных граней не
    требуют — обе трубы в R, к соседу (справа снизу, без замка прямой), а
    не одна в обход клапана."""
    g = _graph([dict(VALVE_H), _box("b", 320.0, 200.0)],
               [_edge("e1", "v", "b"), _edge("e2", "v", "b")])
    seating.reseat_all_endpoints(g, model=True, slots=True, axial=True)
    assert all(p[0][1] == 221.0 for p in _pts(g).values())


def test_degrees_counts_multi_edges():
    from modules.graph.core.graph_access import degrees
    g = _graph([dict(VALVE_H), _box("u", 400.0, 100.0), _conn("c", 1.0, 1.0)],
               [_edge("e1", "v", "u"), _edge("e2", "u", "v"),
                _edge("e3", "v", "c")])
    assert degrees(g) == {"v": 3, "u": 2, "c": 1}

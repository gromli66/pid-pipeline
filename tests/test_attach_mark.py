# -*- coding: utf-8 -*-
"""Метка модели точек прикрепления на холсте (Р1, блок 3) — ядро и сервер.

Холст, собранный сервером по модели, несёт `canvas_transform.attach_model`
(пишет `canvas_state.stamp`). Холст без метки — старый: посадка каноном
238fe6b (без слотов, порты в середине граней, letterbox старой рамки) и
выгрузка со старой поправкой контрола по замороженной таблице.

Литералы «как 238fe6b» сняты прогоном того же холста в дереве deploy 238fe6b
(`pretransform.seat_edge_endpoints` и `canvas_to_fxml.generate_canvas_fxml`):
там строки совпали байт в байт (MEASUREMENTS, блок 3).
Координаты: точки [y, x], bbox [x1, y1, x2, y2].
"""
from __future__ import annotations

import copy
import hashlib
import json

from modules.canvas_to_fxml import generate_canvas_fxml
from modules.graph.core import canvas_state, contact, pretransform, seating


def _canvas(mark=None) -> dict:
    """Холст: клапаны H/V (ELECTRIC_VLV, CTRL_VLV_ELEC, AOC_CTRL_VLV) и
    клапан в старой рамке 42x60 (letterbox 238fe6b != bbox)."""
    tr = {"s": 1.0, "offx": 0, "offy": 0}
    if mark:
        tr["attach_model"] = mark

    def conn(nid, y, x):
        return {"id": nid, "type": "connector", "class_name": "connector",
                "centroid": [y, x], "bbox": None}

    def eq(nid, cn, axis, c, bb):
        return {"id": nid, "type": "equipment", "class_name": cn,
                "_axis": axis, "centroid": c, "bbox": bb}

    def edge(eid, s, t, sp, tp):
        return {"id": eid, "source": s, "target": t, "source_point": sp,
                "target_point": tp, "waypoints": []}

    return {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [1080, 1920], "canvas_transform": tr},
        "nodes": [
            conn("c9", 200.0, 200.0),
            eq("h1", "armatura_electro", "H", [200.0, 300.0],
               [279.0, 181.1, 321.0, 218.9]),
            {"id": "u1", "type": "equipment", "class_name": "unknow",
             "centroid": [200.0, 400.0], "bbox": [380.0, 180.0, 420.0, 220.0]},
            eq("v1", "armatura_electro", "V", [221.0, 318.9],
               [300.0, 200.0, 337.8, 242.0]),
            eq("r1", "regulator_electro", "V", [521.0, 618.9],
               [600.0, 500.0, 637.8, 542.0]),
            eq("a1", "regulator_seroprivod", "H", [700.0, 300.0],
               [279.0, 684.25, 321.0, 715.75]),
            conn("c8", 460.0, 618.9),
            conn("c7", 700.0, 200.0),
            conn("c6", 100.0, 300.0),
            eq("h2", "armatura_electro", "H", [200.0, 500.0],
               [479.0, 170.0, 521.0, 230.0]),
            conn("c5", 100.0, 500.0),
            conn("c4", 210.0, 470.0),
        ],
        "links": [
            edge("e1", "c9", "h1", [200.0, 200.0], [200.0, 279.0]),
            edge("e2", "h1", "u1", [200.0, 321.0], [200.0, 380.0]),
            edge("e3", "c8", "r1", [460.0, 618.9], [500.0, 618.9]),
            edge("e4", "c7", "a1", [700.0, 200.0], [700.0, 279.0]),
            edge("e5", "c6", "h1", [100.0, 300.0], [181.1, 300.0]),
            edge("e6", "c5", "h2", [100.0, 500.0], [170.0, 500.0]),
            # в 10 px от середины грани: канон держит прямую, модель — нет
            edge("e7", "u1", "c4", [200.0, 420.0], [210.0, 470.0]),
        ],
        "text_blocks": [], "bindings": [],
    }


def _controls(xml: str) -> dict:
    return {line.split('fx:id="')[1].split('"')[0]: line.strip()
            for line in xml.splitlines() if "<ValveControl " in line}


def _points(g) -> dict:
    return {e["id"]: (e["source_point"], e["target_point"])
            for e in g["links"]}


# ───────────────────── сторож таблицы и метки ─────────────────────

def test_table_guard_raises_model_mark():
    """Правка `SKIN_CONTACT_OFFSET` меняет геометрию помеченных холстов, а
    is_stale её не видит. Красный здесь = поднять `ATTACH_MODEL` (новая
    метка) и `ATTACH_MODEL_TABLE_SHA` вместе с правкой таблицы."""
    canon = json.dumps(sorted((k, [float(a), float(b)]) for k, (a, b)
                              in contact.SKIN_CONTACT_OFFSET.items()),
                       separators=(",", ":"))
    sha = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert (contact.ATTACH_MODEL, sha) == \
        ("waist-1", contact.ATTACH_MODEL_TABLE_SHA)


# ───────────────────── метка -> режим узлов ─────────────────────

def test_canvas_model_reads_mark():
    assert contact.canvas_model(_canvas()) is None
    assert contact.canvas_model(_canvas("waist-1")) == "waist-1"
    assert contact.canvas_model({"nodes": []}) is None
    assert contact.canvas_model({"graph": None}) is None


def test_apply_attach_mode_marks_and_unmarks_nodes():
    g = _canvas()
    assert contact.apply_attach_mode(g) is True
    assert contact.apply_attach_mode(g) is True          # идемпотентна
    assert all(n[contact.LEGACY_KEY] is True for n in g["nodes"])
    # истина — метка: холст пересобран сервером -> ключ снимается
    g["graph"]["canvas_transform"]["attach_model"] = contact.ATTACH_MODEL
    assert contact.apply_attach_mode(g) is False
    assert not any(contact.LEGACY_KEY in n for n in g["nodes"])


def test_strip_attach_mode_removes_key_only():
    g = _canvas()
    contact.apply_attach_mode(g)
    contact.strip_attach_mode(g["nodes"])
    assert g["nodes"] == _canvas()["nodes"]


def test_legacy_valve_has_no_waist_port():
    """Старый холст: порта-талии нет — середина грани, центроид = центр."""
    v = copy.deepcopy(_canvas()["nodes"][1])              # h1, ELECTRIC H
    rect = tuple(v["bbox"])
    assert contact.face_refs(v, rect) == {"L": 208.3916, "R": 208.3916}
    v[contact.LEGACY_KEY] = True
    assert contact.face_refs(v, rect) is None
    assert contact.face_ref(v, rect, "L") == (200.0, False)
    assert contact.waist_centroid(v, v["bbox"]) == [200.0, 300.0]
    assert contact.waist_offset(v, "H", 42.0, 37.8) == 0.0


def test_anchor_rect_letterbox_only_for_legacy_skin():
    g = _canvas()
    h2 = copy.deepcopy(g["nodes"][9])                    # рамка 42x60
    u1 = copy.deepcopy(g["nodes"][2])                    # unknow
    assert seating._anchor_rect(h2) == (479.0, 170.0, 521.0, 230.0)
    h2[contact.LEGACY_KEY] = u1[contact.LEGACY_KEY] = True
    assert seating._anchor_rect(h2) == (479.0, 181.1, 521.0, 218.9)
    assert seating._anchor_rect(u1) == (380.0, 180.0, 420.0, 220.0)


# ───────────────────── открытие: посадка по метке ─────────────────────

def test_legacy_seating_is_238fe6b_canon():
    """Без метки: канон 238fe6b — концы на середине грани (без талии и
    слотов), на старой рамке 42x60 — на letterbox графики, а не на bbox;
    прямая к коннектору в 10 px от середины бокса держится (модель увела бы
    конец в середину: MID_SNAP 3 px)."""
    g = _canvas()
    legacy = contact.apply_attach_mode(g)
    assert legacy is True
    assert pretransform.seat_edge_endpoints(g, legacy=legacy) == 2
    contact.strip_attach_mode(g["nodes"])
    want = _points(_canvas())
    want["e6"] = ([100.0, 500.0], [181.1, 500.0])        # верх letterbox
    want["e7"] = ([210.0, 420.0], [210.0, 470.0])        # прямая
    assert _points(g) == want


def test_model_seating_differs_on_same_canvas():
    """Двойник: тот же холст с меткой — модель (талия, bbox)."""
    g = _canvas("waist-1")
    assert contact.apply_attach_mode(g) is False
    assert pretransform.seat_edge_endpoints(g) > 0
    p = _points(g)
    assert p["e1"][1] == [208.3916, 279.0]
    # h2 — клапан степени 1, труба сверху прямая: осевое правило (Р5) без
    # роутера сделало бы её косой — гейт «не хуже входа», верх bbox
    assert p["e6"][1] == [170.0, 500.0]
    assert p["e7"][0] == [200.0, 420.0]


# ───────────────────── выгрузка: поправка по метке ─────────────────────

# 238fe6b: строка в строку (снято прогоном дерева deploy)
_CONTROLS_238FE6B = {
    "h1": '<ValveControl fx:id="h1" layoutX="279.0" layoutY="172.7" '
          'prefWidth="42.0" prefHeight="37.8" skinType="ELECTRIC_VLV" '
          'orientation="HORIZONTAL" equipmentType="MOV" '
          'valueVisible="false" />',
    "v1": '<ValveControl fx:id="v1" layoutX="290.3" layoutY="202.1" '
          'prefWidth="42.0" prefHeight="37.8" skinType="ELECTRIC_VLV" '
          'orientation="VERTICAL_REVERSE" equipmentType="MOV" '
          'valueVisible="false" />',
    "r1": '<ValveControl fx:id="r1" layoutX="592.2" layoutY="502.1" '
          'prefWidth="42.0" prefHeight="37.8" skinType="CTRL_VLV_ELEC" '
          'orientation="VERTICAL_REVERSE" equipmentType="CNT" '
          'valueVisible="false" />',
    "a1": '<ValveControl fx:id="a1" layoutX="279.0" layoutY="679.5" '
          'prefWidth="42.0" prefHeight="31.5" skinType="AOC_CTRL_VLV" '
          'orientation="HORIZONTAL" equipmentType="CNT" '
          'valueVisible="false" />',
    "h2": '<ValveControl fx:id="h2" layoutX="479.0" layoutY="156.7" '
          'prefWidth="42.0" prefHeight="60.0" skinType="ELECTRIC_VLV" '
          'orientation="HORIZONTAL" equipmentType="MOV" '
          'valueVisible="false" />',
}


def test_export_without_mark_is_238fe6b():
    """T1: холст без метки — старая поправка по ЗАМОРОЖЕННОЙ таблице.
    V ELECTRIC_VLV 297.9 − 0.20·37.8 = 290.3 (новая доля 0.222 дала бы
    289.5), CTRL_VLV_ELEC 597.9 − 0.15·37.8 = 592.2 (новая — 586.0)."""
    assert _controls(generate_canvas_fxml(_canvas())) == _CONTROLS_238FE6B


def test_export_with_mark_has_no_offset():
    """T2: холст по модели — контрол 1:1 с рамкой, поправки нет."""
    c = _controls(generate_canvas_fxml(_canvas("waist-1")))
    assert 'layoutY="181.1"' in c["h1"]
    assert 'layoutX="297.9"' in c["v1"]
    assert 'layoutX="597.9"' in c["r1"]
    assert 'layoutY="684.2"' in c["a1"]
    assert 'layoutY="170.0"' in c["h2"]


def test_export_does_not_mutate_input():
    g = _canvas()
    generate_canvas_fxml(g)
    assert g == _canvas()


# ───────────────────── метка вне устаревания ─────────────────────

def test_mark_not_in_staleness():
    """stamp пишет метку; is_stale и layout_version её не видят: старый
    холст не устаревает от её отсутствия (Р1)."""
    validated = _canvas()
    version = canvas_state.layout_version()
    marked = copy.deepcopy(validated)
    canvas_state.stamp(marked, validated, layout_applied=True)
    assert marked["graph"]["canvas_transform"]["attach_model"] == "waist-1"
    bare = copy.deepcopy(marked)
    del bare["graph"]["canvas_transform"]["attach_model"]
    assert canvas_state.is_stale(marked, validated) == (False, None)
    assert canvas_state.is_stale(bare, validated) == (False, None)
    assert canvas_state.graph_projection_sha(marked) == \
        canvas_state.graph_projection_sha(bare)
    assert canvas_state.layout_version() == version

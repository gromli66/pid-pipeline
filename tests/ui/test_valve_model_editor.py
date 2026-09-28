# -*- coding: utf-8 -*-
"""Клапан «Ручной правки» по модели точек прикрепления (блок 1, К3).

Инвариант холста: у клапана задан `_axis`, центроид — точка талии, рамка
стоит вокруг талии. Его обязаны держать жесты, которые рамку создают или
меняют: создание узла (`add_equipment_node`), «Размеры»
(`_resize_node_box`), угловой оверлей (`_on_node_resized`) и вставка
(`_paste_node_clipboard`). До правки новый клапан шёл без `_axis` (труба в
САПФИР мимо талии на 5–14 px), а «Размеры» тем же размером сдвигали рамку
на долю талии.

Литералы — на `armatura_electro` ТОЛЬКО по оси H (h-доля 0.222, таблицу Р6
блок 2 не меняет) и на симметричном `armatura_ruchn`.

Координаты двойственны (CODING_GUIDE §6): centroid / source_point /
target_point = [y, x]; bbox = [x1, y1, x2, y2].
"""
import copy
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPointF                      # noqa: E402
from PySide6.QtGui import QColor, QImage                 # noqa: E402
from PySide6.QtWidgets import QApplication               # noqa: E402

from modules.graph.core import contact                   # noqa: E402

IMG_W, IMG_H = 1920, 1080
H_FRAC = 0.222                      # ELECTRIC_VLV по оси H
VALVE_BBOX = [479.0, 286.0, 521.0, 314.0]           # 42×28, центр y=300
WAIST_Y = 300.0 + H_FRAC * 28.0                     # 306.216


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _canvas():
    """Кусок серверного холста: коннектор — H-клапан — коннектор на линии
    талии (метка раскладки есть: полнографный auto_fix заперт, как в бою)."""
    nodes = [
        {"id": "c1", "type": "connector", "class_id": -1,
         "class_name": "connector", "centroid": [WAIST_Y, 400.0],
         "bbox": None, "segmentation": None, "degree": 1},
        {"id": "v", "type": "equipment", "class_id": 3,
         "class_name": "armatura_electro", "_axis": "H",
         "centroid": [WAIST_Y, 500.0], "bbox": list(VALVE_BBOX),
         "segmentation": None, "degree": 2},
        {"id": "c2", "type": "connector", "class_id": -1,
         "class_name": "connector", "centroid": [WAIST_Y, 600.0],
         "bbox": None, "segmentation": None, "degree": 1},
    ]
    links = [
        {"id": "e1", "source": "c1", "target": "v",
         "source_point": [WAIST_Y, 400.0], "target_point": [WAIST_Y, 479.0],
         "waypoints": []},
        {"id": "e2", "source": "v", "target": "c2",
         "source_point": [WAIST_Y, 521.0], "target_point": [WAIST_Y, 600.0],
         "waypoints": []},
    ]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W],
                      "canvas_transform": {"layout_applied": True}},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


@pytest.fixture
def make_editor(qapp, tmp_path):
    made = []

    def _make(graph=None, canvas=True):
        from ui.editors.advanced_graph_editor import AdvancedGraphEditor

        img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
        img.fill(QColor("white"))
        ip = tmp_path / f"raster{len(made)}.png"
        assert img.save(str(ip))
        gp = tmp_path / f"graph{len(made)}.json"
        gp.write_text(json.dumps(graph or _canvas()), encoding="utf-8")
        ed = AdvancedGraphEditor()
        ed._canvas_mode = canvas        # так делает вкладка: флаг ДО load_data
        assert ed.load_data(str(ip), str(gp))
        made.append(ed)
        return ed

    yield _make
    # Снос детерминированный (очередь отложенных удалений Qt выпить здесь,
    # см. test_apply_resize_undo_invariant.py).
    for ed in made:
        ed.set_mode("idle")
        ed.scene.clear()
        ed.setParent(None)
        ed.deleteLater()
    qapp.processEvents()


def _state(ed):
    return copy.deepcopy((ed.model.graph_data.get("nodes"),
                          ed.model.graph_data.get("links")))


def _links(ed):
    return {e["id"]: e for e in ed.edges_data}


def _assert_waist_invariant(node):
    assert node.get("_axis") in ("H", "V"), "клапан холста без оси"
    assert node["centroid"] == pytest.approx(
        contact.waist_centroid(node, node["bbox"]), abs=0.01), \
        "центроид клапана — не талия его рамки"


# ── создание ────────────────────────────────────────────────────────────

def test_new_valve_on_canvas_gets_axis_and_waist(make_editor):
    """Рамка — там, где её поставил оператор; ось — по рамке; центроид —
    талия; ось свободна до первого ребра. Отмена/возврат — тем же узлом."""
    ed = make_editor()
    nid = ed.add_equipment_node(500.0, 700.0, 3, "armatura_electro",
                                width=42.0, height=28.0, enter_resize=False)
    n = ed.nodes[nid]
    assert n["bbox"] == [479.0, 686.0, 521.0, 714.0], "рамка сдвинута"
    assert n["_axis"] == "H"
    assert n["_axis_free"] is True
    assert n["centroid"] == pytest.approx([700.0 + H_FRAC * 28.0, 500.0],
                                          abs=0.01)
    _assert_waist_invariant(n)

    ed.undo()
    assert nid not in ed.nodes
    ed.redo()
    assert ed.nodes[nid]["_axis"] == "H"
    assert ed.nodes[nid]["centroid"] == pytest.approx(n["centroid"])


def test_new_vertical_symmetric_valve_and_relief_and_box(make_editor):
    """Ось по пропорциям рамки (V у высокой); симметричный клапан — талия в
    центре; predohran ось получает, но не свободен (не поворачивается);
    не-клапан модели не касается."""
    ed = make_editor()
    vid = ed.add_equipment_node(800.0, 700.0, 1, "armatura_ruchn",
                                width=28.0, height=42.0, enter_resize=False)
    v = ed.nodes[vid]
    assert v["_axis"] == "V" and v["_axis_free"] is True
    assert v["centroid"] == pytest.approx([700.0, 800.0])

    rid = ed.add_equipment_node(900.0, 700.0, 9, "predohran",
                                width=42.0, height=28.0, enter_resize=False)
    r = ed.nodes[rid]
    assert r["_axis"] == "H" and "_axis_free" not in r
    _assert_waist_invariant(r)

    bid = ed.add_equipment_node(1000.0, 700.0, 99, "unknow",
                                width=42.0, height=28.0, enter_resize=False)
    b = ed.nodes[bid]
    assert "_axis" not in b and "_axis_free" not in b
    assert b["centroid"] == [700.0, 1000.0]


def test_new_valve_in_raster_stays_raster(make_editor):
    """Растр («Проверка схемы»): бокс — детекция, модели талии нет."""
    ed = make_editor(canvas=False)
    nid = ed.add_equipment_node(500.0, 700.0, 3, "armatura_electro",
                                width=42.0, height=28.0, enter_resize=False)
    n = ed.nodes[nid]
    assert "_axis" not in n and "_axis_free" not in n
    assert n["centroid"] == [700.0, 500.0]


def test_overlay_resize_of_new_valve_keeps_centroid_in_waist(make_editor):
    """Угловой оверлей нового клапана: центроид — талия НОВОЙ рамки (до
    правки у нового клапана не было `_axis`, и центроид шёл в центр)."""
    ed = make_editor()
    nid = ed.add_equipment_node(500.0, 700.0, 3, "armatura_electro",
                                width=42.0, height=28.0, enter_resize=False)
    ed._on_node_resized(nid, [479.0, 686.0, 521.0, 726.0])     # h 28 -> 40
    n = ed.nodes[nid]
    assert n["centroid"][0] == pytest.approx(686.0 + 20.0 + H_FRAC * 40.0,
                                             abs=0.01)
    _assert_waist_invariant(n)


# ── «Размеры» ───────────────────────────────────────────────────────────

def _open_sizes(ed):
    ed.set_mode("resize_objects")
    ed.set_resize_class("armatura_electro")
    assert ed._resize_sel == {"v"}, "набор не собрался"


def test_sizes_same_size_moves_nothing(make_editor):
    """«Размеры» тем же размером — ни рамка, ни концы не сдвинулись (до
    правки рамка центрировалась на талию и уезжала на 6.216 px вниз)."""
    ed = make_editor()
    before = _state(ed)
    _open_sizes(ed)
    ed.apply_resize(width=28.0, height=42.0)
    nodes, links = _state(ed)
    assert nodes[1]["bbox"] == pytest.approx(VALVE_BBOX, abs=1e-9)
    assert nodes[1]["centroid"] == before[0][1]["centroid"]
    for got, was in zip(links, before[1]):
        assert got["source_point"] == pytest.approx(was["source_point"],
                                                    abs=1e-9)
        assert got["target_point"] == pytest.approx(was["target_point"],
                                                    abs=1e-9)
        assert not got.get("waypoints"), "появилась ступенька"


def test_sizes_new_size_keeps_waist_on_the_pipe(make_editor):
    """42×28 -> 63×42: рамка растёт вокруг талии, обе трубы остаются
    прямыми в талии; один Ctrl+Z — исходное."""
    ed = make_editor()
    before = _state(ed)
    _open_sizes(ed)
    ed.apply_resize(width=42.0, height=63.0)
    v = ed.nodes["v"]
    assert v["bbox"] == pytest.approx([468.5, 275.892, 531.5, 317.892],
                                      abs=0.01)
    assert v["centroid"] == pytest.approx([WAIST_Y, 500.0], abs=0.01)
    _assert_waist_invariant(v)
    links = _links(ed)
    assert links["e1"]["target_point"] == pytest.approx([WAIST_Y, 468.5],
                                                        abs=0.01)
    assert links["e2"]["source_point"] == pytest.approx([WAIST_Y, 531.5],
                                                        abs=0.01)
    assert not links["e1"].get("waypoints") \
        and not links["e2"].get("waypoints"), "ступенька у талии"

    ed.undo()
    assert _state(ed) == before


def test_sizes_after_foreign_drag_and_undo(make_editor):
    """После чужого жеста оператора: перенос клапана -> Ctrl+Z -> «Размеры»
    63×42 — тот же итог, что на нетронутом холсте (талия на трубе)."""
    ed = make_editor()
    before = _state(ed)
    ed.start_drag_node("v")
    ed.drag_node_to(520.0, WAIST_Y + 30.0)
    ed.end_drag_node()
    assert ed.nodes["v"]["centroid"] != before[0][1]["centroid"], \
        "перенос не состоялся — тест бессмыслен"
    ed.undo()
    assert _state(ed) == before

    _open_sizes(ed)
    ed.apply_resize(width=42.0, height=63.0)
    v = ed.nodes["v"]
    assert v["bbox"] == pytest.approx([468.5, 275.892, 531.5, 317.892],
                                      abs=0.01)
    _assert_waist_invariant(v)
    links = _links(ed)
    assert links["e1"]["target_point"][0] == pytest.approx(WAIST_Y, abs=0.01)
    assert links["e2"]["source_point"][0] == pytest.approx(WAIST_Y, abs=0.01)


def test_sizes_pins_stay_on_the_frame(make_editor):
    """Пин оператора на осевой грани (смещение от центроида-талии) после
    «Размеров» остаётся на грани и в талии новой рамки."""
    from ui.editors import port_model

    g = _canvas()
    g["links"][0]["pin_target"] = {"dx": -21.0, "dy": 0.0}
    ed = make_editor(g)
    _open_sizes(ed)
    ed.apply_resize(width=42.0, height=63.0)
    v = ed.nodes["v"]
    e1 = _links(ed)["e1"]
    pin = port_model.edge_pin(e1, "target")
    assert pin == pytest.approx({"dx": -31.5, "dy": 0.0})
    x = v["centroid"][1] + pin["dx"]
    y = v["centroid"][0] + pin["dy"]
    assert x == pytest.approx(v["bbox"][0]), "пин повис вне рамки"
    assert y == pytest.approx(contact.face_refs(v, v["bbox"])["L"],
                              abs=0.01), "пин мимо талии новой рамки"


# ── вставка ─────────────────────────────────────────────────────────────

def test_paste_valve_without_axis_enters_the_model(make_editor, monkeypatch):
    """Копия клапана, созданного до модели (нет `_axis`, центроид в центре
    рамки): вставка даёт ось и талию, конец ребра — в талию, у копии ребра
    нет `_slack`; вся вставка — один Ctrl+Z."""
    g = _canvas()
    w = {"id": "w", "type": "equipment", "class_id": 3,
         "class_name": "armatura_electro", "centroid": [800.0, 500.0],
         "bbox": [479.0, 786.0, 521.0, 814.0], "segmentation": None,
         "degree": 1}
    k = {"id": "k", "type": "connector", "class_id": -1,
         "class_name": "connector", "centroid": [800.0, 400.0],
         "bbox": None, "segmentation": None, "degree": 1}
    g["nodes"] += [w, k]
    g["links"].append({"id": "ek", "source": "k", "target": "w",
                       "source_point": [800.0, 400.0],
                       "target_point": [800.0, 479.0],
                       "waypoints": [], "_slack": 1})
    ed = make_editor(g)
    monkeypatch.setattr(ed, "_cursor_scene_pos",
                        lambda: QPointF(1500.0, 100.0))    # пустое место
    ed.selected_nodes = {"w", "k"}
    ed.selected_edges = {ed.model.edge_key("k", "w")}
    ed._copy_selected_nodes()
    assert len(ed._node_clipboard["nodes"]) == 2
    assert len(ed._node_clipboard["edges"]) == 1
    before = _state(ed)

    ed._paste_node_clipboard(0.0, 100.0)

    new = [n for n in ed.nodes.values()
           if n.get("class_name") == "armatura_electro"
           and n["id"] not in ("v", "w")]
    assert len(new) == 1
    nv = new[0]
    assert nv["bbox"] == [479.0, 886.0, 521.0, 914.0]
    assert nv["_axis"] == "H"
    assert nv["centroid"] == pytest.approx([900.0 + H_FRAC * 28.0, 500.0],
                                           abs=0.01)
    _assert_waist_invariant(nv)
    ne = [e for e in ed.edges_data if nv["id"] in (e["source"], e["target"])]
    assert len(ne) == 1
    assert "_slack" not in ne[0], "копия унесла метку прогона сервера"
    assert ne[0]["target_point"] == pytest.approx(
        [900.0 + H_FRAC * 28.0, 479.0], abs=0.01), "конец не в талии"
    assert ed._edge_is_ortho(ne[0]), "труба к талии легла косой"
    assert _links(ed)["ek"]["_slack"] == 1, "источник тронут"

    ed.undo()
    assert _state(ed) == before

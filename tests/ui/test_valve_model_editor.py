# -*- coding: utf-8 -*-
"""Клапан «Ручной правки» по модели точек прикрепления (блок 1, К3–К4).

Инвариант холста: у клапана задан `_axis`, центроид — точка талии, рамка
стоит вокруг талии. Его обязаны держать жесты, которые рамку создают или
меняют: создание узла (`add_equipment_node`), «Размеры»
(`_resize_node_box`), угловой оверлей (`_on_node_resized`) и вставка
(`_paste_node_clipboard`). До правки новый клапан шёл без `_axis` (труба в
САПФИР мимо талии на 5–14 px), а «Размеры» тем же размером сдвигали рамку
на долю талии. К4 (Р3): первое поперечное ребро поворачивает новый клапан
и сдвигает его талией на трубу — одним шагом отмены.

Литералы — на `armatura_electro` (h-доля 0.222, v-доля 0.222 с Р6) и на
симметричном `armatura_ruchn`; литералы повёрнутого (V) armatura_electro
пересняты в блоке 2 (Р6: v 0.20 → 0.222, талия правее центра рамки на
0.222·28 = 6.216 px вместо 5.6).

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


# ── Р3: первое ребро закрепляет ось нового клапана (К4) ────────────────
#
# Новый клапан (`_axis_free`) один раз поворачивается первым ребром,
# пришедшим поперёк оси: рамка w↔h вокруг талии, затем сдвиг, чтобы талия
# легла на трубу, если сдвинутая рамка не задевает соседей. Ребро, поворот
# и сдвиг — один шаг отмены.
#
# ⚠ Литералы повёрнутой рамки зависят от V-доли ELECTRIC_VLV
# (armatura_electro — REVERSE-класс, талия правее центра на v·w).
# Пересняты в блоке 2 (Р6, v 0.20 → 0.222): талия x=505 → центр рамки
# 505 − 0.222·28 = 498.784 (было 499.4), рамка левее на 0.616 px; y не зависит
# от доли.

TURNED_BBOX = [484.784, 285.216, 512.784, 327.216]   # Р6: было 485.4 … 513.4


def _conn(nid, y, x):
    return {"id": nid, "type": "connector", "class_id": -1,
            "class_name": "connector", "centroid": [y, x], "bbox": None,
            "segmentation": None, "degree": 0}


def _sheet(*nodes):
    """Пустой лист холста с заданными узлами (рёбер нет)."""
    g = _canvas()
    g["nodes"], g["links"] = list(nodes), []
    return g


def _new_valve(ed, cls="armatura_electro", class_id=3):
    """Клапан «Ручной правки»: рамка 42×28 [479, 286, 521, 314] (ось H)."""
    return ed.add_equipment_node(500.0, 300.0, class_id, cls, width=42.0,
                                 height=28.0, enter_resize=False)


def test_first_transverse_edge_turns_valve_onto_the_pipe(make_editor):
    """Коннектор над H-клапаном со сдвигом 5 px: клапан повёрнут (V, рамка
    28×42 вокруг талии), сдвинут на линию трубы (талия x=505), труба прямая
    в талию, скин повёрнут. Ctrl+Z — одним шагом до «клапан свободен, ребра
    нет»; Ctrl+Y — снова «после». Второе ребро ось не трогает."""
    ed = make_editor(_sheet(_conn("k", 150.0, 505.0),
                            _conn("k2", WAIST_Y, 700.0)))
    ed.set_show_skins(True)
    nid = _new_valve(ed)
    before = _state(ed)
    depth = ed.undo_mgr.stack_depth

    assert ed.add_edge(nid, "k")
    n = ed.nodes[nid]
    assert n["_axis"] == "V" and "_axis_free" not in n
    assert n["bbox"] == pytest.approx(TURNED_BBOX, abs=0.01)
    assert n["centroid"] == pytest.approx([WAIST_Y, 505.0], abs=0.01)
    _assert_waist_invariant(n)
    e = ed.edges_data[0]
    assert e["source_point"] == pytest.approx([TURNED_BBOX[1], 505.0],
                                              abs=0.01), "конец не в талии"
    assert e["target_point"] == [150.0, 505.0]
    assert not e.get("waypoints"), "труба не прямая"
    assert ed._node_orientation(nid) == "VERTICAL_REVERSE"
    skin = ed._skin_items[nid][0]
    assert skin.sceneBoundingRect().getCoords() == pytest.approx(
        tuple(n["bbox"]), abs=0.01), "скин не вписан в повёрнутую рамку"
    assert skin.pixmap().height() > skin.pixmap().width(), "скин не повёрнут"
    c = ed.node_items[nid].rect().center()
    assert (c.x(), c.y()) == pytest.approx((505.0, WAIST_Y), abs=0.01), \
        "маркер центроида остался на старом месте"
    assert ed.undo_mgr.stack_depth == depth + 1, "не один шаг отмены"
    after = _state(ed)

    ed.undo()
    assert _state(ed) == before
    assert ed.nodes[nid]["_axis_free"] is True
    ed.redo()
    assert _state(ed) == after

    assert ed.add_edge(nid, "k2")
    n = ed.nodes[nid]
    assert n["_axis"] == "V", "второе ребро повернуло клапан"
    assert n["bbox"] == after[0][-1]["bbox"]
    assert n["centroid"] == after[0][-1]["centroid"]


def test_first_edge_with_waypoints_turns_onto_operator_waypoint(make_editor):
    """Ребро с изломом оператора: поворот по смежному waypoint, талия — на
    его линии (x=503), waypoints оператора бит-в-бит; один Ctrl+Z."""
    ed = make_editor(_sheet(_conn("k", 200.0, 700.0)))
    nid = _new_valve(ed)
    before = _state(ed)

    assert ed.add_edge_with_waypoints(nid, "k", [[200.0, 503.0]])
    n = ed.nodes[nid]
    assert n["_axis"] == "V" and "_axis_free" not in n
    assert n["centroid"] == pytest.approx([WAIST_Y, 503.0], abs=0.01)
    _assert_waist_invariant(n)
    e = ed.edges_data[0]
    assert e["waypoints"] == [[200.0, 503.0]], "излом оператора тронут"
    assert e["source_point"][1] == pytest.approx(503.0, abs=0.01)
    assert ed._edge_is_ortho(e)

    ed.undo()
    assert _state(ed) == before


def test_first_axial_edge_frees_nothing_but_the_flag(make_editor):
    """Ребро вдоль оси: поворота и сдвига нет, `_axis_free` снят (второе
    поперечное ребро клапан уже не повернёт); Ctrl+Z возвращает флаг."""
    ed = make_editor(_sheet(_conn("k", WAIST_Y, 650.0),
                            _conn("k2", 150.0, 505.0)))
    nid = _new_valve(ed)
    before = _state(ed)

    assert ed.add_edge(nid, "k")
    n = ed.nodes[nid]
    assert n["bbox"] == [479.0, 286.0, 521.0, 314.0]
    assert n["_axis"] == "H" and "_axis_free" not in n
    assert ed.edges_data[0]["source_point"] == pytest.approx(
        [WAIST_Y, 521.0], abs=0.01)

    assert ed.add_edge(nid, "k2")
    assert ed.nodes[nid]["_axis"] == "H", "поперечное ВТОРОЕ ребро повернуло"
    assert ed.nodes[nid]["bbox"] == [479.0, 286.0, 521.0, 314.0]

    ed.undo()
    ed.undo()
    assert _state(ed) == before


def test_neighbour_on_the_shift_path_leaves_only_the_turn(make_editor):
    """Сосед задевает сдвинутую рамку (`box_hits`) — клапан только повёрнут,
    талия остаётся на x=500, труба доходит маршрутом (ортогонально)."""
    from modules.graph.core.graph_access import box_hits

    box = {"id": "b", "type": "equipment", "class_id": 99,
           "class_name": "unknow", "centroid": [305.0, 459.0],
           "bbox": [440.0, 290.0, 478.0, 320.0], "segmentation": None,
           "degree": 0}
    ed = make_editor(_sheet(_conn("k", 150.0, 495.0), box))
    nid = _new_valve(ed)
    assert ed.add_edge(nid, "k")
    n = ed.nodes[nid]
    assert n["_axis"] == "V"
    assert n["centroid"] == pytest.approx([WAIST_Y, 500.0], abs=0.01), \
        "клапан сдвинут в соседа"
    assert n["bbox"] == pytest.approx(      # Р6: было 480.4 … 508.4 (v 0.20)
        [479.784, 285.216, 507.784, 327.216], abs=0.01)
    moved = [n["bbox"][0] - 5.0, n["bbox"][1], n["bbox"][2] - 5.0,
             n["bbox"][3]]
    assert box_hits(ed.nodes, nid, moved), "сосед не на пути — тест пуст"
    e = ed.edges_data[0]
    assert e["source_point"][1] == pytest.approx(500.0, abs=0.01)
    assert e["target_point"] == [150.0, 495.0]
    assert ed._edge_is_ortho(e), "труба легла косой"
    assert ed.nodes["b"]["bbox"] == [440.0, 290.0, 478.0, 320.0]


def _box_below(y1):
    """Бокс под H-клапаном [479, 286, 521, 314]: верх на y1."""
    return {"id": "b", "type": "equipment", "class_id": 99,
            "class_name": "unknow", "centroid": [y1 + 15.0, 500.0],
            "bbox": [480.0, y1, 520.0, y1 + 30.0], "segmentation": None,
            "degree": 0}


def test_neighbour_in_the_turned_frame_blocks_the_turn(make_editor):
    """Решение Максима 28.09: повёрнутая рамка (ещё до сдвига) задела бы
    соседа — клапан НЕ поворачивается. Сосед в 1 px под нижней гранью (рамка
    растёт вниз до 327.2): ось H и рамка по-прежнему, `_axis_free` снят,
    ребро садится как обычно; Ctrl+Z одним шагом. Сосед далеко (y=340) —
    поворот и сдвиг, как без соседа."""
    from modules.graph.core.graph_access import box_hits

    ed = make_editor(_sheet(_conn("k", 150.0, 505.0), _box_below(315.0)))
    nid = _new_valve(ed)
    frame = list(ed.nodes[nid]["bbox"])
    assert frame == [479.0, 286.0, 521.0, 314.0]
    assert not box_hits(ed.nodes, nid, frame), "сосед задет и без поворота"
    assert box_hits(ed.nodes, nid, TURNED_BBOX), "поворот соседа не задел бы"
    before = _state(ed)
    depth = ed.undo_mgr.stack_depth

    assert ed.add_edge(nid, "k")
    n = ed.nodes[nid]
    assert n["_axis"] == "H" and "_axis_free" not in n, "клапан повёрнут"
    assert n["bbox"] == frame and n["centroid"] == [WAIST_Y, 500.0]
    e = ed.edges_data[0]
    assert e["source_point"][0] == pytest.approx(286.0, abs=0.01),         "ребро не на верхней грани"
    assert e["target_point"] == [150.0, 505.0]
    assert ed._edge_is_ortho(e), "труба легла косой"
    assert ed.nodes["b"]["bbox"] == [480.0, 315.0, 520.0, 345.0]
    assert ed.undo_mgr.stack_depth == depth + 1, "не один шаг отмены"
    ed.undo()
    assert _state(ed) == before

    ed = make_editor(_sheet(_conn("k", 150.0, 505.0), _box_below(340.0)))
    nid = _new_valve(ed)
    assert ed.add_edge(nid, "k")
    n = ed.nodes[nid]
    assert n["_axis"] == "V", "далёкий сосед запретил поворот"
    assert n["bbox"] == pytest.approx(TURNED_BBOX, abs=0.01)


def test_relief_valve_does_not_turn(make_editor):
    """predohran (RELIEF_VLV) оси не освобождает: поперечное первое ребро
    его не поворачивает и не двигает."""
    ed = make_editor(_sheet(_conn("k", 150.0, 505.0)))
    nid = _new_valve(ed, "predohran", 9)
    assert ed.add_edge(nid, "k")
    n = ed.nodes[nid]
    assert n["_axis"] == "H"
    assert n["bbox"] == [479.0, 286.0, 521.0, 314.0]


def test_turn_after_foreign_sizes_undo_and_drag(make_editor):
    """Д1: после чужих действий оператора («Размеры» тем же размером ->
    Ctrl+Z, перенос коннектора на +5 px) первое ребро всё так же ставит
    талию на трубу."""
    ed = make_editor(_sheet(_conn("k", 150.0, 500.0)))
    nid = _new_valve(ed)
    ed.set_mode("resize_objects")
    ed.set_resize_class("armatura_electro")
    assert ed._resize_sel == {nid}
    ed.apply_resize(width=28.0, height=42.0)
    ed.undo()
    ed.set_mode("idle")
    assert ed.nodes[nid]["_axis_free"] is True

    ed.start_drag_node("k")
    ed.drag_node_to(505.0, 150.0)
    ed.end_drag_node()
    kx = ed.nodes["k"]["centroid"][1]
    assert kx == pytest.approx(505.0, abs=0.01), "перенос не состоялся"

    assert ed.add_edge(nid, "k")
    n = ed.nodes[nid]
    assert n["_axis"] == "V"
    _assert_waist_invariant(n)
    assert n["centroid"][1] == pytest.approx(kx, abs=0.01)
    e = ed.edges_data[0]
    assert e["source_point"][1] == pytest.approx(kx, abs=0.01)
    assert not e.get("waypoints")


def test_turned_valve_exports_pipe_into_the_waist(make_editor, tmp_path):
    """Сквозной: новый клапан -> два поперечных ребра -> запись -> FXML:
    обе трубы входят в талию контрола (промах ≤ 0.1 px). Талия считается
    по самому контролу FXML: центр + v·prefHeight (VERTICAL_REVERSE)."""
    import re
    from modules.canvas_to_fxml import generate_canvas_fxml

    ed = make_editor(_sheet(_conn("k", 150.0, 505.0),
                            _conn("k2", 450.0, 505.0)))
    nid = _new_valve(ed)
    assert ed.add_edge(nid, "k") and ed.add_edge(nid, "k2")
    out = tmp_path / "out.json"
    assert ed.save_graph(str(out))
    fx = generate_canvas_fxml(json.loads(out.read_text(encoding="utf-8")))

    tag = re.search(r'<ValveControl fx:id="%s"[^>]*>' % nid, fx).group(0)

    def num(k):
        return float(re.search(k + r'="([-\d.]+)"', tag).group(1))

    assert 'orientation="VERTICAL_REVERSE"' in tag
    v_frac = contact.SKIN_CONTACT_OFFSET["ELECTRIC_VLV"][1]
    waist_x = num("layoutX") + num("prefWidth") / 2 + v_frac * num("prefHeight")
    xs = [float(v) for line in re.findall(r'<Line[^>]*>', fx)
          for v in re.findall(r'(?:startX|endX)="([-\d.]+)"', line)]
    assert len(xs) == 4
    assert max(abs(x - waist_x) for x in xs) <= 0.1


def test_edge_add_remove_redraws_skin_orientation(make_editor):
    """Клапан без `_axis` (холст до модели): ориентация — по сторонам рёбер.
    Ребро в верхнюю грань делает его вертикальным — скин обязан повернуться
    при добавлении, отмене, возврате и удалении ребра (`AddEdgeCommand`/
    `RemoveEdgeCommand`)."""
    w = {"id": "w", "type": "equipment", "class_id": 3,
         "class_name": "armatura_electro", "centroid": [300.0, 500.0],
         "bbox": [479.0, 286.0, 521.0, 314.0], "segmentation": None,
         "degree": 0}
    ed = make_editor(_sheet(_conn("k", 150.0, 500.0), w))
    ed.set_show_skins(True)

    def tall():
        pm = ed._skin_items["w"][0].pixmap()
        return pm.height() > pm.width()

    assert not tall()
    assert ed.add_edge("w", "k")
    assert ed._node_orientation("w") == "VERTICAL_REVERSE" and tall()
    ed.undo()
    assert not tall()
    ed.redo()
    assert tall()
    assert ed.remove_edge("w", "k")
    assert not tall()
    ed.undo()
    assert tall()


def test_pipe_line_off_the_turned_face_turns_without_a_jump(make_editor):
    """Линия трубы вне пролёта осевой грани повёрнутой рамки (коннектор над
    клапаном, но правее рамки 28×42): клапан повёрнут, но не прыгает к
    трубе — она доходит L-маршрутом в талию."""
    ed = make_editor(_sheet(_conn("k", 150.0, 510.0)))
    nid = _new_valve(ed)
    assert ed.add_edge(nid, "k")
    n = ed.nodes[nid]
    assert n["_axis"] == "V"
    assert n["centroid"] == pytest.approx([WAIST_Y, 500.0], abs=0.01)
    e = ed.edges_data[0]
    assert e["source_point"][1] == pytest.approx(500.0, abs=0.01)
    assert e["target_point"] == [150.0, 510.0]
    assert ed._edge_is_ortho(e)


# ── К5: слоты всей гранью после добавления ребра ──────────────────────
#
# Ребро пришло в грань, где уже сидят концы, — ВСЕ концы грани
# раскладываются слотами симметрично вокруг опорной точки (середина рамки,
# у клапана — талия), той же раскладкой, что у сервера: открытие холста
# после этого ничего не двигает. До правки новый конец брал свободный слот
# среди k+1, а соседи стояли в слотах для k (асимметрия, которую открытие
# потом перекладывало). Чужие грани и маршруты не трогаются.

BOX_BBOX = [80.0, 80.0, 120.0, 120.0]            # unknow 40×40, без скина


def _box():
    return {"id": "b", "type": "equipment", "class_id": 99,
            "class_name": "unknow", "centroid": [100.0, 100.0],
            "bbox": list(BOX_BBOX), "segmentation": None, "degree": 0}


def _end_at(e, nid):
    return e["source_point"] if e["source"] == nid else e["target_point"]


def _face_ys(ed, nid, x):
    """{партнёр: y конца у nid} для концов на вертикальной грани x."""
    return {(e["target"] if e["source"] == nid else e["source"]):
            _end_at(e, nid)[0] for e in ed.edges_data
            if nid in (e["source"], e["target"])
            and abs(_end_at(e, nid)[1] - x) <= 0.01}


def _reopen_moves(ed, tmp_path):
    """Сохранить и открыть холст: True — открытие что-то пересадило."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = tmp_path / f"reopen{len(list(tmp_path.glob('reopen*')))}.json"
    assert ed.save_graph(str(p))
    return _reseat_canvas_endpoints(p)


def _box_sheet():
    return _sheet(_box(), _conn("kb", 300.0, 100.0), _conn("k1", 100.0, 300.0),
                  _conn("k2", 60.0, 300.0), _conn("k3", 140.0, 300.0))


def test_new_pipe_spreads_the_whole_box_face(make_editor, tmp_path):
    """Бокс без скина: вторая труба в R-грань — обе в слотах 100∓6.667
    (прямая соседка уходит маршрутом, не косой), третья — 90/100/110.
    Труба в нижней грани бит-в-бит; открытие холста ничего не двигает."""
    ed = make_editor(_box_sheet())
    assert ed.add_edge("b", "kb") and ed.add_edge("b", "k1")
    foreign = copy.deepcopy(ed.edges_data[0])
    assert _face_ys(ed, "b", 120.0) == {"k1": 100.0}

    assert ed.add_edge("b", "k2")
    assert _face_ys(ed, "b", 120.0) == pytest.approx(
        {"k2": 100.0 - 20.0 / 3.0, "k1": 100.0 + 20.0 / 3.0}, abs=1e-6)
    assert all(ed._edge_is_ortho(e) for e in ed.edges_data), "косая"
    assert not _reopen_moves(ed, tmp_path), "открытие переложило грань"

    assert ed.add_edge("b", "k3")
    assert _face_ys(ed, "b", 120.0) == pytest.approx(
        {"k2": 90.0, "k1": 100.0, "k3": 110.0}, abs=1e-6)
    assert all(ed._edge_is_ortho(e) for e in ed.edges_data), "косая"
    assert ed.edges_data[0] == foreign, "тронута труба чужой грани"
    for e in ed.edges_data:                  # сцена перерисована по данным
        p0 = ed.edge_items[ed.model.edge_key(e["source"], e["target"])] \
            .path().elementAt(0)
        assert [p0.y, p0.x] == pytest.approx(e["source_point"], abs=0.01)
    assert not _reopen_moves(ed, tmp_path), "открытие переложило грань"


def test_new_pipe_spreads_the_valve_face_around_the_waist(make_editor,
                                                          tmp_path):
    """Клапан холста (H, талия y=306.216): две трубы в R-грань встают
    симметрично вокруг ТАЛИИ, а не середины рамки; третья — в талию.
    Труба в L-грани бит-в-бит; открытие ничего не двигает.

    ⚠ Слоты 304.432/308.0 — веер у смещённой талии, зажатый отступом от
    угла (`ports.side_slots`); ПЕРЕСНЯТЬ В БЛОКЕ 2 (сдвиг веера 2b′)."""
    ed = make_editor(_sheet(
        {"id": "v", "type": "equipment", "class_id": 3,
         "class_name": "armatura_electro", "_axis": "H",
         "centroid": [WAIST_Y, 500.0], "bbox": list(VALVE_BBOX),
         "segmentation": None, "degree": 0},
        _conn("kl", WAIST_Y, 300.0), _conn("k1", WAIST_Y - 40.0, 700.0),
        _conn("k2", WAIST_Y + 40.0, 700.0), _conn("k3", WAIST_Y, 700.0)))
    assert ed.add_edge("kl", "v") and ed.add_edge("v", "k1")
    foreign = copy.deepcopy(ed.edges_data[0])
    assert _face_ys(ed, "v", 521.0) == pytest.approx({"k1": WAIST_Y})

    assert ed.add_edge("v", "k2")
    got = _face_ys(ed, "v", 521.0)
    assert got == pytest.approx({"k1": 304.432, "k2": 308.0}, abs=1e-6)
    assert (got["k1"] + got["k2"]) / 2 == pytest.approx(WAIST_Y, abs=1e-6), \
        "веер не вокруг талии"
    assert all(ed._edge_is_ortho(e) for e in ed.edges_data), "косая"
    assert not _reopen_moves(ed, tmp_path), "открытие переложило грань"

    assert ed.add_edge("v", "k3")
    assert _face_ys(ed, "v", 521.0) == pytest.approx(
        {"k1": 304.432, "k3": WAIST_Y, "k2": 308.0}, abs=1e-6)
    assert all(ed._edge_is_ortho(e) for e in ed.edges_data), "косая"
    assert ed.edges_data[0] == foreign, "тронута труба чужой грани"
    assert not _reopen_moves(ed, tmp_path), "открытие переложило грань"


def test_face_spread_after_foreign_drag_and_undo(make_editor, tmp_path):
    """Д1: после чужого жеста (перенос коннектора соседней трубы) третья
    труба всё так же раскладывает грань 90/100/110. Ребро и раскладка
    соседей — ОДИН шаг отмены: Ctrl+Z возвращает соседей бит-в-бит,
    Ctrl+Y — снова «после»; повторное добавление после отмены даёт то же."""
    ed = make_editor(_box_sheet())
    assert ed.add_edge("b", "k1") and ed.add_edge("b", "k2")
    ed.start_drag_node("k2")
    ed.drag_node_to(300.0, 50.0)
    ed.end_drag_node()
    assert ed.nodes["k2"]["centroid"] == pytest.approx([50.0, 300.0])
    before = _state(ed)
    depth = ed.undo_mgr.stack_depth

    assert ed.add_edge("b", "k3")
    assert _face_ys(ed, "b", 120.0) == pytest.approx(
        {"k2": 90.0, "k1": 100.0, "k3": 110.0}, abs=1e-6)
    assert all(ed._edge_is_ortho(e) for e in ed.edges_data), "косая"
    assert ed.undo_mgr.stack_depth == depth + 1, "не один шаг отмены"
    after = _state(ed)

    ed.undo()
    assert _state(ed) == before, "Ctrl+Z не вернул соседей грани"
    ed.redo()
    assert _state(ed) == after
    ed.undo()
    assert ed.add_edge("b", "k3")
    again = _state(ed)
    again[1][-1]["id"] = after[1][-1]["id"]      # счётчик id рёбер идёт вперёд
    assert again == after
    assert not _reopen_moves(ed, tmp_path), "открытие переложило грань"


def test_face_spread_rolls_back_when_a_sibling_would_turn_oblique(
        make_editor, monkeypatch):
    """Гейт «не хуже входа»: прямая соседка, уехавшая в слот, не нашла
    маршрута (роутер отказал) — вся грань откатывается: соседка прямая на
    прежнем месте, новая труба в своём слоте `_slot_seat` (100−6.667)."""
    ed = make_editor(_box_sheet())
    assert ed.add_edge("b", "k1")
    sibling = copy.deepcopy(ed.edges_data[0])
    real = ed._route_orthogonal

    def refuse_sibling(edge_data, alive=False):
        if "k1" in (edge_data.get("source"), edge_data.get("target")):
            return False
        return real(edge_data, alive)

    monkeypatch.setattr(ed, "_route_orthogonal", refuse_sibling)
    assert ed.add_edge("b", "k2")
    assert ed.edges_data[0] == sibling, "соседка не откатилась"
    assert _face_ys(ed, "b", 120.0) == pytest.approx(
        {"k1": 100.0, "k2": 100.0 - 20.0 / 3.0}, abs=1e-6)
    assert all(ed._edge_is_ortho(e) for e in ed.edges_data), "косая"


def test_face_spread_is_off_in_raster(make_editor):
    """Растр («Проверка схемы») — вне модели (`_waist_model()`): соседи по
    грани не двигаются, как до правки."""
    ed = make_editor(_box_sheet(), canvas=False)
    assert ed.add_edge("b", "k1") and ed.add_edge("b", "k2")
    assert _face_ys(ed, "b", 120.0)["k1"] == 100.0


# ── К6: метка вынужденной прямой `_slack` ───────────────────────────────
# Сервер (`seating.straighten_unrouted`) метит прямую, которой не нашлось
# ступеньки в талию: 2 — порт клапана уступил. Открытие держит её прямой.
# Жест оператора, пересчитавший ребро, метку снимает (геометрия теперь его),
# Ctrl+Z — возвращает; откат гейта «не хуже входа» к прямой — тоже.
# Фикстура — неподвижная точка открытия: вывод `straighten_unrouted` после
# канона (замер пробой k6/probe_sizes.py).

SLACK_Y = 296.108                      # прямая в створе клапана и бокса
SLACK_BOX = [580.0, 270.0, 620.0, 302.0]  # unknow 40×32, талия 306.216 вне


def _slack_sheet(target_y=SLACK_Y):
    g = _sheet(
        {"id": "v", "type": "equipment", "class_id": 3,
         "class_name": "armatura_electro", "_axis": "H",
         "centroid": [WAIST_Y, 500.0], "bbox": list(VALVE_BBOX),
         "segmentation": None, "degree": 1},
        {"id": "b", "type": "equipment", "class_id": 99,
         "class_name": "unknow", "centroid": [286.0, 600.0],
         "bbox": list(SLACK_BOX), "segmentation": None, "degree": 1})
    g["links"] = [{"id": "e1", "source": "v", "target": "b",
                   "source_point": [SLACK_Y, 521.0],
                   "target_point": [target_y, 580.0],
                   "waypoints": [], "_slack": 2}]
    return g


def _saved_edge(ed, tmp_path):
    p = tmp_path / f"slack{len(list(tmp_path.glob('slack*')))}.json"
    assert ed.save_graph(str(p))
    return json.loads(p.read_text(encoding="utf-8"))["links"][0]


def test_open_and_save_without_gestures_keep_slack(make_editor, tmp_path):
    """Без жестов метка живёт: запись её сохраняет, открытие не двигает."""
    ed = make_editor(_slack_sheet())
    assert _saved_edge(ed, tmp_path)["_slack"] == 2
    assert not _reopen_moves(ed, tmp_path), "фикстура — не точка открытия"


def test_drag_drops_slack_and_undo_returns_it(make_editor, tmp_path):
    """Д1: чужой жест («Размеры» бокса другим размером) снимает метку,
    Ctrl+Z возвращает (снимок команды). Затем перенос бокса на +5 px:
    метки нет ни в ребре, ни в файле; Ctrl+Z — метка 2 и всё бит-в-бит;
    Ctrl+Y — снова без метки."""
    ed = make_editor(_slack_sheet())
    before = _state(ed)
    ed.set_mode("resize_objects")
    ed.set_resize_class("unknow")
    ed.apply_resize(width=36.0, height=44.0)
    assert "_slack" not in ed.edges_data[0], "«Размеры» не сняли метку"
    ed.undo()
    assert _state(ed) == before
    ed.set_mode("idle")

    ed.start_drag_node("b")
    ed.drag_node_to(600.0, 291.0)
    ed.end_drag_node()
    e = ed.edges_data[0]
    assert ed.nodes["b"]["centroid"] == pytest.approx([291.0, 600.0])
    assert "_slack" not in e, "перенос не снял метку"
    assert "_slack" not in _saved_edge(ed, tmp_path)
    after = _state(ed)

    ed.undo()
    assert ed.edges_data[0]["_slack"] == 2, "Ctrl+Z не вернул метку"
    assert _state(ed) == before
    ed.redo()
    assert _state(ed) == after


def test_sizes_rolled_back_to_the_straight_keep_slack(make_editor, tmp_path,
                                                       monkeypatch):
    """Гейт «не хуже входа» откатил ребро к прямой сервера (роутер
    отказал) — метка вернулась с геометрией: без неё открытие посадило бы
    порт в талию (306.216) и развалило прямую в косую."""
    ed = make_editor(_slack_sheet())
    before = copy.deepcopy(ed.edges_data[0])
    monkeypatch.setattr(ed, "_build_avoid_session", lambda *a, **k: None)
    monkeypatch.setattr(ed, "_route_orthogonal", lambda *a, **k: False)
    _open_sizes(ed)
    ed.apply_resize(width=28.0, height=42.0)
    assert ed.edges_data[0] == before
    assert not _reopen_moves(ed, tmp_path), "открытие развалило прямую"


def test_overlay_resize_drops_slack_and_undo_returns_it(make_editor):
    """Угловой оверлей (ResizeNodeCommand): метка снята, Ctrl+Z вернул её
    через ROUTE_KEYS, Ctrl+Y — снова без метки."""
    ed = make_editor(_slack_sheet())
    before = _state(ed)
    ed._start_resize("b")
    ed._on_node_resized("b", [580.0, 266.0, 624.0, 302.0])
    ed._commit_resize("b")
    assert "_slack" not in ed.edges_data[0]
    after = _state(ed)
    ed.undo()
    assert ed.edges_data[0]["_slack"] == 2
    # рёбра бит-в-бит (узлу откат ResizeNodeCommand дописывает area — не К6)
    assert _state(ed)[1] == before[1]
    ed.redo()
    assert _state(ed)[1] == after[1]


def test_optimize_drops_slack_and_undo_returns_it(make_editor):
    """«Оптимизировать» (OptimizeEdgeCommand): метка снята, Ctrl+Z вернул,
    Ctrl+Y снова снял."""
    ed = make_editor(_slack_sheet())
    assert ed.optimize_edge("v", "b")
    assert "_slack" not in ed.edges_data[0]
    ed.undo()
    assert ed.edges_data[0]["_slack"] == 2
    ed.redo()
    assert "_slack" not in ed.edges_data[0]


def test_cycle_sides_drops_slack_and_undo_returns_it(make_editor):
    """Цикл стороны (`_recalculate_edge` -> `_engine_finish_ends` на живом
    ребре): метка снята, Ctrl+Z — снимок вернул."""
    ed = make_editor(_slack_sheet())
    before = _state(ed)
    ed._cycle_node_sides("b")
    assert "_slack" not in ed.edges_data[0]
    ed.undo()
    assert _state(ed) == before


def test_endpoint_drag_drops_slack_and_undo_returns_it(make_editor):
    """Перенос конца: кадр протяжки и страховка отпускания (жест в обход
    кадров) снимают метку; Ctrl+Z — снимок жеста вернул её."""
    from ui.editors.undo_manager import SnapshotCommand

    ed = make_editor(_slack_sheet())
    before = _state(ed)
    key = ed.model.edge_key("v", "b")
    ed._start_endpoint_drag((key, "target"))
    ed._drag_endpoint_to(580.0, 280.0)
    ed._end_endpoint_drag()
    assert "_slack" not in ed.edges_data[0]
    ed.undo()
    assert _state(ed) == before

    ed._dragging_endpoint = (key, "target")      # в обход кадров
    ed._ep_drag_armed = True
    ed._ep_on_port = False
    ed._ep_snap_cmd = SnapshotCommand(ed.model, ed._redraw_all)
    ed._ep_snap_cmd.execute()
    ed._end_endpoint_drag()
    assert "_slack" not in ed.edges_data[0]
    ed.undo()
    assert _state(ed) == before


def test_smoothing_route_drops_slack(make_editor):
    """Сглаживание проложило косое ребро коленом — метка снята; один Ctrl+Z
    операции вернул всё, включая метку."""
    ed = make_editor(_slack_sheet(target_y=290.0))
    before = _state(ed)
    stats = ed.smooth_canvas()
    assert stats["колено"] == 1, stats
    assert "_slack" not in ed.edges_data[0]
    ed.undo()
    assert _state(ed) == before


def test_slack_is_kept_in_raster(make_editor):
    """Растр — вне модели (`_waist_model()`): перенос метку не трогает."""
    ed = make_editor(_slack_sheet(), canvas=False)
    ed.start_drag_node("b")
    ed.drag_node_to(600.0, 291.0)
    ed.end_drag_node()
    assert ed.edges_data[0]["_slack"] == 2


# ── приёмка red-team блока 1 ────────────────────────────────────────────
#
# Повтор (Ctrl+Y) команд создания вставлял ЖИВОЙ словарь, который следующие
# жесты уже поправили на месте (К5 — раскладка грани, Р3 — поворот);
# узкий класс armatura_seroprivod (рамка сервера по оси H 28×42) получал
# ось по пропорциям наоборот; «Размеры» и вставка обходили `_waist_model()`.

def test_redo_first_pipe_returns_it_to_its_own_seat(make_editor):
    """Труба 1 в пустую грань бокса, труба 2 в ту же грань (обе в слоты) ->
    Ctrl+Z, Ctrl+Z, Ctrl+Y: труба 1 снова одна и стоит в середине грани, как
    сразу после своего добавления, а не в слоте раскладки на две трубы.
    Второй Ctrl+Y — снова обе в слотах."""
    ed = make_editor(_box_sheet())
    assert ed.add_edge("b", "k1")
    one = _state(ed)
    assert _face_ys(ed, "b", 120.0) == {"k1": 100.0}
    assert ed.add_edge("b", "k2")
    two = _state(ed)
    assert _face_ys(ed, "b", 120.0)["k1"] == pytest.approx(
        100.0 + 20.0 / 3.0, abs=1e-6), "раскладки нет — тест пуст"

    ed.undo()
    ed.undo()
    assert ed.edges_data == []
    ed.redo()
    assert _state(ed) == one, "повтор вернул трубу 1 в слот раскладки"
    ed.redo()
    assert _state(ed) == two


def test_redo_new_valve_returns_it_free_and_unturned(make_editor):
    """Новый клапан -> поперечное первое ребро (поворот Р3) -> Ctrl+Z,
    Ctrl+Z, Ctrl+Y: клапан возвращается, каким был создан (ось H, свободна,
    рамка на месте), и следующая труба по оси H входит в талию без
    поворота."""
    ed = make_editor(_sheet(_conn("k", 150.0, 505.0),
                            _conn("kl", WAIST_Y, 300.0)))
    nid = _new_valve(ed)
    created = _state(ed)
    assert ed.add_edge(nid, "k")
    assert ed.nodes[nid]["_axis"] == "V", "поворота нет — тест пуст"

    ed.undo()
    ed.undo()
    assert nid not in ed.nodes
    ed.redo()
    assert _state(ed) == created, "повтор вернул клапан повёрнутым"
    n = ed.nodes[nid]
    assert n["_axis"] == "H" and n["_axis_free"] is True

    assert ed.add_edge(nid, "kl")
    n = ed.nodes[nid]
    assert n["_axis"] == "H" and "_axis_free" not in n
    assert n["bbox"] == [479.0, 286.0, 521.0, 314.0]
    assert _end_at(ed.edges_data[0], nid) == pytest.approx(
        [WAIST_Y, 479.0], abs=0.01), "труба мимо талии"


def _narrow_waist_y(bbox):
    """Талия armatura_seroprivod по оси H на рамке bbox (доля AOC_VLV —
    из таблицы, не литерал: блок 2 её меняет)."""
    return contact.face_refs({"class_name": "armatura_seroprivod",
                              "_axis": "H"}, bbox)["L"]


NARROW_BBOX = [486.0, 279.0, 514.0, 321.0]        # 28×42 — рамка сервера по H


def test_narrow_valve_axis_follows_the_server_frame(make_editor):
    """armatura_seroprivod нарисован рамкой сервера 28×42 -> ось H (по
    пропорциям было бы V); родная горизонтальная труба его не поворачивает
    и входит в талию; «Размеры» тем же размером рамку не трогают. Рамка
    42×28 -> ось V."""
    wy = _narrow_waist_y(NARROW_BBOX)
    ed = make_editor(_sheet(_conn("kl", wy, 300.0)))
    nid = ed.add_equipment_node(500.0, 300.0, 7, "armatura_seroprivod",
                                width=28.0, height=42.0, enter_resize=False)
    n = ed.nodes[nid]
    assert n["_axis"] == "H", "ось узкого класса — по пропорциям наоборот"
    assert n["bbox"] == NARROW_BBOX
    _assert_waist_invariant(n)
    assert n["centroid"][0] == pytest.approx(wy, abs=0.01)

    ed.set_mode("resize_objects")
    ed.set_resize_class("armatura_seroprivod")
    assert ed._resize_sel == {nid}
    ed.apply_resize(width=28.0, height=42.0)
    assert ed.nodes[nid]["bbox"] == pytest.approx(NARROW_BBOX, abs=1e-9), \
        "«Размеры» тем же размером перевернули рамку"
    ed.undo()
    ed.set_mode("idle")

    assert ed.add_edge(nid, "kl")
    n = ed.nodes[nid]
    assert n["_axis"] == "H" and "_axis_free" not in n, "родная труба повернула"
    assert n["bbox"] == NARROW_BBOX
    e = ed.edges_data[0]
    assert _end_at(e, nid) == pytest.approx([wy, 486.0], abs=0.01), \
        "труба мимо талии"
    assert not e.get("waypoints")

    nid2 = ed.add_equipment_node(500.0, 700.0, 7, "armatura_seroprivod",
                                 width=42.0, height=28.0, enter_resize=False)
    assert ed.nodes[nid2]["_axis"] == "V"
    _assert_waist_invariant(ed.nodes[nid2])


def test_paste_narrow_valve_without_axis_and_edges(make_editor, monkeypatch):
    """Вставка одиночного armatura_seroprivod 28×42 без `_axis`: ось — по
    рамке сервера (H), а не по пропорциям."""
    g = _sheet({"id": "w", "type": "equipment", "class_id": 7,
                "class_name": "armatura_seroprivod",
                "centroid": [300.0, 500.0], "bbox": list(NARROW_BBOX),
                "segmentation": None, "degree": 0})
    ed = make_editor(g)
    monkeypatch.setattr(ed, "_cursor_scene_pos",
                        lambda: QPointF(1500.0, 100.0))    # пустое место
    ed.selected_nodes = {"w"}
    ed._copy_selected_nodes()
    ed._paste_node_clipboard(0.0, 200.0)
    new = [n for n in ed.nodes.values() if n["id"] != "w"]
    assert len(new) == 1
    assert new[0]["_axis"] == "H"
    _assert_waist_invariant(new[0])


def test_sizes_and_paste_ask_the_model_switch(make_editor, monkeypatch):
    """Модель выключена (`_waist_model()` False — так блок 3 откроет старый
    холст): «Размеры» тем же размером ставят рамку вокруг центроида (клапан
    старого холста, центроид в центре рамки, не сдвигается), вставка
    сохраняет `_slack` у копии ребра."""
    g = _canvas()
    for n in g["nodes"]:                    # линия — по центру рамки, не талии
        n["centroid"][0] = 300.0
    for e in g["links"]:
        e["source_point"][0] = e["target_point"][0] = 300.0
    g["links"][0]["_slack"] = 1
    ed = make_editor(g)
    monkeypatch.setattr(ed, "_waist_model", lambda: False)

    _open_sizes(ed)
    ed.apply_resize(width=28.0, height=42.0)
    assert ed.nodes["v"]["bbox"] == pytest.approx(VALVE_BBOX, abs=1e-9), \
        "«Размеры» мимо переключателя сдвинули клапан старого холста"
    assert ed.nodes["v"]["centroid"] == [300.0, 500.0]
    ed.set_mode("idle")

    monkeypatch.setattr(ed, "_cursor_scene_pos",
                        lambda: QPointF(1500.0, 100.0))    # пустое место
    ed.selected_nodes = {"c1", "v"}
    ed.selected_edges = {ed.model.edge_key("c1", "v")}
    ed._copy_selected_nodes()
    ed._paste_node_clipboard(0.0, 300.0)
    copies = [e for e in ed.edges_data if e["id"] not in ("e1", "e2")]
    assert len(copies) == 1
    assert copies[0].get("_slack") == 1, "вставка сняла `_slack` мимо переключателя"

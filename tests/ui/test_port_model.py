# -*- coding: utf-8 -*-
"""Этап A — портовая модель посадки (`ui/editors/port_model.py`).

Жалоба заказчика (§2 п.2-3 EDITOR_AFTER_LAYOUT_PLAN): «подключение гуляет по
периметру» при переносе соседей. Спека этапа A:
  1. точка входа СТОИТ на выбранном порту, пока не наступает обязательная
     смена (изнанка);
  2. смена порта по нужде — ровно один раз, без хлопанья при колебании ±2px;
  3. ручные порты: drag конца ребра в свободное место границы рождает
     постоянный порт (node['_ports'], локальные смещения), порт едет с
     узлом, undo побайтово, save/load живёт, sha проекции не меняется;
  4. полигон без скина: кандидаты — середины прямых участков контура
     >= 16px, конец не гуляет, пришпиленный порт переживает перенос;
  5. reseat-при-открытии не срывает конец с порта.

Вызываются те же внутренние методы, что дёргает mouse-механика редактора
(паттерн tests/ui/test_interactive_drag.py). Координаты двойственны
(CODING_GUIDE §6): centroid/source_point/target_point = [y, x];
bbox/segmentation = [x, y].
"""
import copy
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

IMG_W, IMG_H = 800, 600


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _wrap(nodes, links):
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def _assert_canonical(graph):
    from modules.graph.core.seating import reseat_edge

    g = copy.deepcopy(graph)
    byid = {n["id"]: n for n in g["nodes"]}
    for e_orig, e in zip(graph["links"], g["links"]):
        reseat_edge(byid, e)
        assert e["source_point"] == e_orig["source_point"], \
            f"фикстура не канонична: sp {e_orig['source_point']} -> {e['source_point']}"
        assert e["target_point"] == e_orig["target_point"], \
            f"фикстура не канонична: tp {e_orig['target_point']} -> {e['target_point']}"


def _editor(qapp, tmp_path, graph):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")

    ed = AdvancedGraphEditor()
    assert ed.load_data(str(ip), str(gp))
    return ed


def _drag(ed, node_id, x, y):
    ed.start_drag_node(node_id)
    ed.drag_node_to(x, y)
    ed.end_drag_node()


def _local_offset(ed, node_id, pt_yx):
    """Смещение конца [y, x] от центроида узла: (dx, dy)."""
    c = ed.nodes[node_id]["centroid"]
    return (round(pt_yx[1] - c[1], 3), round(pt_yx[0] - c[0], 3))


# ── Фикстура: бокс 80x80 + коннектор справа, канонично-прямая труба ──────

def _graph_box_conn_right():
    nodes = [
        {"id": "b", "type": "equipment", "centroid": [200.0, 200.0],
         "bbox": [160.0, 160.0, 240.0, 240.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "c", "type": "connector", "centroid": [200.0, 400.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 1},
    ]
    links = [{"id": "edge_1", "source": "b", "target": "c",
              "source_point": [200.0, 240.0], "target_point": [200.0, 400.0],
              "waypoints": []}]
    return _wrap(nodes, links)


# =====================================================================
# (1) ГЛАВНЫЙ: точка входа стоит на порту, а не ползёт по периметру
# =====================================================================

def test_entry_point_pinned_to_port_during_drag(qapp, tmp_path):
    """Жалоба заказчика: при ray-посадке конец на переносимом узле ПОЛЗ по
    грани (поперечная координата = clamp(toward)). Этап A: конец сидит в
    порту (центр правой грани) — на протяжке по дуге вокруг соседа
    ЛОКАЛЬНАЯ точка входа одна на всех кадрах."""
    g = _graph_box_conn_right()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("b", "c"))

    frames = [(200.0, 260.0), (200.0, 275.0), (200.0, 290.0), (200.0, 300.0)]
    frames += [(x, 300.0) for x in range(215, 351, 15)]

    ed.start_drag_node("b")
    offsets = []
    for fx, fy in frames:
        ed.drag_node_to(fx, fy)
        offsets.append(_local_offset(ed, "b", e["source_point"]))
    ed.end_drag_node()

    assert len(set(offsets)) == 1, \
        f"точка входа гуляла по периметру: {sorted(set(offsets))}"
    assert offsets[0] == (40.0, 0.0), \
        f"конец не в порту (центр правой грани): {offsets[0]}"
    # дальний конец (коннектор) неприкосновенен
    assert e["target_point"] == [200.0, 400.0]


def test_port_switches_once_and_does_not_flap(qapp, tmp_path):
    """Сосед оказался с другой стороны — порт сменился на обращённый РОВНО
    один раз (обязательная смена: вход с изнанки); колебание ±2px вокруг
    точки смены порт не хлопает (порог смены >> порога «остаться»).

    А->В (Э2b, переобъявление): фазы скольжения по строгой прямой между
    портами больше НЕТ — конец всегда жёстко в порту («прямая важнее
    порта» отменена заказчиком 2026-07-31). Последовательность обязана
    быть монотонной R+ T+ — правый порт, верхний порт, ровно одна смена,
    без единого возврата и без внепортовых позиций."""
    g = _graph_box_conn_right()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("b", "c"))

    frames = [(float(x), 300.0) for x in range(200, 351, 15)]   # R-фаза
    frames += [(float(x), 300.0) for x in range(365, 436, 15)]  # прямая (V)
    frames += [(444.0, 300.0),
               (442.0, 300.0), (446.0, 300.0),
               (442.0, 300.0), (446.0, 300.0)]       # колебание ±2px
    frames += [(float(x), 300.0) for x in range(460, 491, 15)]  # T-фаза

    ed.start_drag_node("b")
    states = []
    for fx, fy in frames:
        ed.drag_node_to(fx, fy)
        off = _local_offset(ed, "b", e["source_point"])
        if off == (40.0, 0.0):
            states.append("R")
        elif off == (0.0, -40.0):
            states.append("T")
        else:
            raise AssertionError(
                f"конец вне порта (А->В: порты жёсткие): {off}")
    ed.end_drag_node()

    compact = [s for i, s in enumerate(states)
               if i == 0 or states[i - 1] != s]
    assert compact == ["R", "T"], \
        f"порт хлопал/возвращался: {''.join(states)}"


# =====================================================================
# (3) Ручные порты: создание, переезд, undo, save/load, sha
# =====================================================================

def _graph_box_conn_small():
    """Фикстура test_interactive_drag._graph_box_conn: бокс [80,160,240] +
    коннектор на общей оси y=200."""
    nodes = [
        {"id": "box", "type": "equipment", "centroid": [200.0, 120.0],
         "bbox": [80.0, 160.0, 160.0, 240.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "conn", "type": "connector", "centroid": [200.0, 300.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 1},
    ]
    links = [{"id": "edge_1", "source": "box", "target": "conn",
              "source_point": [200.0, 160.0], "target_point": [200.0, 300.0],
              "waypoints": []}]
    return _wrap(nodes, links)


def _drop_endpoint(ed, key, endpoint, x, y):
    """Полный жест переноса конца ребра тем же путём, что мышь
    (EditWaypointHandler): press -> move -> release."""
    ed._start_endpoint_drag((key, endpoint))
    ed._drag_endpoint_to(x, y)
    ed._end_endpoint_drag()


def test_endpoint_drop_free_creates_manual_port(qapp, tmp_path):
    """Отпускание конца в свободном месте границы создаёт постоянный ЯКОРЬ
    входа: node['_ports'] — ЛОКАЛЬНОЕ смещение от центроида + владелец-ребро.

    ПЕРЕОБЪЯВЛЕН 2026-08-02 (решение заказчика «нужен классический обход, но
    с фиксированным входом»): флаг _manual_route здесь больше НЕ ставится —
    он исключал трубу из всех роутингов навсегда, и она оставалась голой
    прямой сквозь чужие блоки. Вход держит якорь, форму трубы считает
    роутер."""
    g = _graph_box_conn_small()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    key = ed.model.edge_key("box", "conn")

    # (165, 225) -> проекция на границу (160, 225); до всех портов > snap 20
    _drop_endpoint(ed, key, "source", 165.0, 225.0)

    node = ed.model.nodes["box"]
    e = ed.model.find_edge_data(key)
    assert e["source_point"] == [225.0, 160.0]
    assert e.get("_manual_route") is None, \
        "закрепление ВХОДА не должно замораживать МАРШРУТ"
    ports = node.get("_ports") or []
    assert len(ports) == 1, f"якорь не создан: {ports}"
    assert (ports[0]["dx"], ports[0]["dy"]) == (40.0, 25.0), \
        f"якорь не локальный: {ports[0]}"
    assert ports[0].get("edge") == "box|conn", \
        f"у якоря нет владельца-ребра: {ports[0]}"


def test_endpoint_drop_on_candidate_port_snaps_to_it(qapp, tmp_path):
    """Конец липнет к порту-кандидату (центр грани).

    ПЕРЕОБЪЯВЛЕН 2026-08-02: раньше проверялось, что якорь при этом НЕ
    создаётся. Теперь выбор оператора закрепляется ВСЕГДА — иначе его негде
    хранить, и первый же пересчёт уводит конец обратно в середину грани
    (замер аудита). Липкость к кандидату при этом сохраняется."""
    g = _graph_box_conn_small()
    ed = _editor(qapp, tmp_path, g)
    key = ed.model.edge_key("box", "conn")

    # (158, 205): 5.4px от исходного конца (drag армирован), 5.4px до
    # порта-кандидата (160, 200) — конец прилип к нему
    _drop_endpoint(ed, key, "source", 158.0, 205.0)

    e = ed.model.find_edge_data(key)
    assert e["source_point"] == [200.0, 160.0], "конец обязан прилипнуть к порту"
    ports = ed.model.nodes["box"].get("_ports") or []
    assert len(ports) == 1 and ports[0].get("edge") == "box|conn", \
        f"выбор оператора обязан закрепиться якорем: {ports}"


def test_manual_port_rides_with_node(qapp, tmp_path):
    """Перенос узла: ручной порт — локальная точка узла, конец едет с ним."""
    g = _graph_box_conn_small()
    ed = _editor(qapp, tmp_path, g)
    key = ed.model.edge_key("box", "conn")
    _drop_endpoint(ed, key, "source", 165.0, 225.0)

    _drag(ed, "box", 140.0, 220.0)      # центроид (120,200) -> (140,220)

    e = ed.model.find_edge_data(key)
    assert e["source_point"] == pytest.approx([245.0, 180.0]), \
        "пришпиленный конец обязан ехать с узлом (центроид + смещение порта)"
    # смещение якоря не изменилось (владелец-ребро в словаре — не помеха)
    pt = ed.model.nodes["box"]["_ports"][0]
    assert (pt["dx"], pt["dy"]) == (40.0, 25.0), f"якорь уехал: {pt}"


def test_manual_port_undo_redo_bytewise(qapp, tmp_path):
    """Undo создания порта — узел и ребро побайтово исходные; redo — порт
    снова жив."""
    g = _graph_box_conn_small()
    ed = _editor(qapp, tmp_path, g)
    key = ed.model.edge_key("box", "conn")

    def state():
        n = ed.model.nodes["box"]
        e = ed.model.find_edge_data(key)
        return json.dumps({"ports": n.get("_ports"),
                           "manual": e.get("_manual_route"),
                           "sp": e["source_point"], "tp": e["target_point"],
                           "wps": e.get("waypoints")}, sort_keys=True)

    s0 = state()
    _drop_endpoint(ed, key, "source", 165.0, 225.0)
    s1 = state()
    assert s1 != s0, "жест обязан был создать порт (иначе тест пуст)"

    ed.undo()
    assert state() == s0, "undo не вернул состояние без порта"
    ed.redo()
    assert state() == s1, "redo не вернул порт"


def test_manual_port_survives_save_load(qapp, tmp_path):
    """node['_ports'] переживает save/load модели."""
    g = _graph_box_conn_small()
    ed = _editor(qapp, tmp_path, g)
    key = ed.model.edge_key("box", "conn")
    _drop_endpoint(ed, key, "source", 165.0, 225.0)

    out = tmp_path / "saved.json"
    assert ed.model.save(str(out))
    saved = json.loads(out.read_text(encoding="utf-8"))
    want = {"dx": 40.0, "dy": 25.0, "edge": "box|conn"}
    node = next(n for n in saved["nodes"] if n["id"] == "box")
    assert node.get("_ports") == [want], \
        f"якорь (со владельцем) не пережил save: {node.get('_ports')}"

    ed2 = _editor(qapp, tmp_path, saved)
    assert ed2.model.nodes["box"].get("_ports") == [want], \
        "якорь не пережил load — вход перестанет быть фиксированным"


def test_ports_do_not_change_projection_sha(qapp, tmp_path):
    """'_ports' вне canvas_state._NODE_KEYS: sha геометрической проекции
    холста от появления портов не меняется."""
    from modules.graph.core.canvas_state import graph_projection_sha

    g = _graph_box_conn_small()
    sha0 = graph_projection_sha(g)
    g2 = copy.deepcopy(g)
    g2["nodes"][0]["_ports"] = [{"dx": 40.0, "dy": 25.0}]
    assert graph_projection_sha(g2) == sha0, \
        "_ports обязан быть невидим для sha проекции холста"


def test_manual_port_rescaled_on_resize(qapp, tmp_path):
    """Resize узла: смещения ручного порта масштабируются с рамкой —
    порт остаётся на границе."""
    g = _graph_box_conn_small()
    ed = _editor(qapp, tmp_path, g)
    key = ed.model.edge_key("box", "conn")
    _drop_endpoint(ed, key, "source", 165.0, 225.0)   # порт (160, 225)

    # bbox [80,160,160,240] -> [80,160,240,320]: рамка x2 в обе стороны
    ed._on_node_resized("box", [80.0, 160.0, 240.0, 320.0])

    node = ed.model.nodes["box"]
    pt = node["_ports"][0]
    assert (pt["dx"], pt["dy"]) == (80.0, 50.0), f"якорь не отмасштабирован: {pt}"
    assert pt.get("edge") == "box|conn", "resize потерял владельца якоря"
    # порт в абсолюте: центроид (160, 240) + (80, 50) = (240, 290) — на грани
    from ui.editors import port_model
    px, py = port_model.manual_ports(node)[0][:2]
    assert (px, py) == (240.0, 290.0)


def test_manual_port_survives_resize_undo_bytewise(qapp, tmp_path):
    """Находка скептика этапа A: undo РЕАЛЬНОГО resize-жеста обязан вернуть
    и смещения ручных портов — иначе порт остаётся отмасштабированным при
    старой рамке и вылетает за границу узла."""
    g = _graph_box_conn_small()
    ed = _editor(qapp, tmp_path, g)
    key = ed.model.edge_key("box", "conn")
    _drop_endpoint(ed, key, "source", 165.0, 225.0)   # порт {dx:25, dy:40}? нет:
    node = ed.model.nodes["box"]
    ports_before = [dict(p) for p in node["_ports"]]
    bbox_before = list(node["bbox"])

    # реальный жест: старт -> протяжка ручки -> коммит
    ed._start_resize("box")
    ed._on_node_resized("box", [80.0, 160.0, 240.0, 320.0])
    ed._commit_resize("box")
    assert node["_ports"] != ports_before          # порт отмасштабирован

    ed.undo_mgr.undo()
    node = ed.model.nodes["box"]
    assert node["bbox"] == bbox_before
    assert node["_ports"] == ports_before, \
        "undo не вернул смещения ручного порта (порт вне узла)"

    ed.undo_mgr.redo()
    node = ed.model.nodes["box"]
    assert node["bbox"] == [80.0, 160.0, 240.0, 320.0]
    assert node["_ports"] != ports_before          # redo вернул масштаб


# =====================================================================
# (4) Полигон без скина: порты на прямых участках контура
# =====================================================================

# Ромб (300,200)-(400,300)-(300,400)-(200,300): 4 прямых участка по ~141px.
DIAMOND = [300.0, 200.0, 400.0, 300.0, 300.0, 400.0, 200.0, 300.0]


def test_polygon_candidate_ports_are_straight_run_midpoints():
    """Кандидаты полигона — середины прямых участков контура >= 16px,
    нормали наружу; короткая фаска (< 16px) порта не рождает."""
    from ui.editors import port_model

    node = {"id": "p", "type": "equipment", "centroid": [300.0, 300.0],
            "bbox": [200.0, 200.0, 400.0, 400.0],
            "segmentation": list(DIAMOND), "class_name": "unknow"}
    ports = port_model.candidate_ports(node)
    got = sorted((round(p[0]), round(p[1])) for p in ports)
    assert got == [(250, 250), (250, 350), (350, 250), (350, 350)]
    for px, py, nx, ny, manual in ports:
        assert manual is False
        # нормаль наружу: сонаправлена с (порт - центроид)
        assert (px - 300.0) * nx + (py - 300.0) * ny > 0, \
            f"нормаль порта ({px},{py}) смотрит внутрь"

    # фаска 10*sqrt(2) < 16px между длинными гранями — порта нет
    chamfer = {"id": "q", "type": "equipment", "centroid": [255.0, 250.0],
               "bbox": [200.0, 200.0, 310.0, 300.0],
               "segmentation": [200.0, 200.0, 300.0, 200.0, 310.0, 210.0,
                                310.0, 300.0, 200.0, 300.0],
               "class_name": "unknow"}
    mids = [(round(p[0]), round(p[1]))
            for p in port_model.candidate_ports(chamfer)]
    assert (305, 205) not in mids, "короткая фаска не должна рожать порт"
    assert len(mids) == 4


def test_polygon_entry_pinned_during_drag(qapp, tmp_path):
    """Конец на полигоне сидит в порту прямого участка и не ползёт по
    контуру на протяжке."""
    nodes = [
        {"id": "poly", "type": "equipment", "centroid": [300.0, 300.0],
         "bbox": [200.0, 200.0, 400.0, 400.0], "segmentation": list(DIAMOND),
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "conn", "type": "connector", "centroid": [300.0, 600.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 1},
    ]
    links = [{"id": "edge_1", "source": "poly", "target": "conn",
              "source_point": [300.0, 400.0], "target_point": [300.0, 600.0],
              "waypoints": []}]
    g = _wrap(nodes, links)
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("poly", "conn"))

    # увод вниз на 150px: ось конна (y=300) больше не пересекает контур —
    # прямизны нет, конец обязан сидеть в порту прямого участка
    ed.start_drag_node("poly")
    offsets = []
    for fx, fy in ((300.0, 450.0), (300.0, 460.0), (295.0, 470.0),
                   (290.0, 480.0)):
        ed.drag_node_to(fx, fy)
        offsets.append(_local_offset(ed, "poly", e["source_point"]))
    ed.end_drag_node()

    assert len(set(offsets)) == 1, \
        f"конец полигона гулял по контуру: {sorted(set(offsets))}"
    assert offsets[0] == (50.0, -50.0), \
        f"конец не на середине прямого участка (верхне-правая грань): {offsets[0]}"
    assert e["target_point"] == [300.0, 600.0], "конец коннектора тронут"


def test_polygon_manual_port_survives_move(qapp, tmp_path):
    """Пришпиленный (ручной) порт на контуре переживает перенос узла."""
    nodes = [
        {"id": "poly", "type": "equipment", "centroid": [300.0, 300.0],
         "bbox": [200.0, 200.0, 400.0, 400.0], "segmentation": list(DIAMOND),
         "class_id": 99, "class_name": "unknow", "degree": 1,
         "_ports": [{"dx": 20.0, "dy": -80.0}]},     # (320, 220) на контуре
        {"id": "conn", "type": "connector", "centroid": [300.0, 600.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 1},
    ]
    links = [{"id": "edge_1", "source": "poly", "target": "conn",
              "source_point": [220.0, 320.0], "target_point": [300.0, 600.0],
              "waypoints": [], "_manual_route": True}]
    g = _wrap(nodes, links)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("poly", "conn"))

    _drag(ed, "poly", 350.0, 340.0)     # +50 по x, +40 по y

    assert e["source_point"] == pytest.approx([260.0, 370.0]), \
        "пришпиленный порт обязан ехать с полигоном"
    assert ed.model.nodes["poly"]["_ports"] == [{"dx": 20.0, "dy": -80.0}]


# =====================================================================
# (5) Reseat-при-открытии: порт не срывается
# =====================================================================

def _open_canvas(tmp_path, nodes, links):
    g = _wrap(nodes, links)
    p = tmp_path / "graph_canvas.json"
    p.write_text(json.dumps(g), encoding="utf-8")
    return p


def test_open_keeps_end_on_candidate_port(tmp_path):
    """Конец на каталожном порту (центр грани): канон-луч пересадил бы его
    в угол — открытие оставляет конец В ПОРТУ (пересаживается в порт)."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    nodes = [
        {"id": "a", "type": "equipment", "class_name": "unknow",
         "centroid": [100.0, 100.0], "bbox": [80.0, 80.0, 120.0, 120.0]},
        {"id": "b", "type": "connector", "class_name": "connector",
         "centroid": [180.0, 300.0], "bbox": None},
    ]
    # sp [100,120] — центр правой грани (порт); канон-луч дал бы [120,120]
    links = [{"id": "e1", "source": "a", "target": "b",
              "source_point": [100.0, 120.0], "target_point": [180.0, 300.0],
              "waypoints": []}]
    p = _open_canvas(tmp_path, nodes, links)

    assert not _reseat_canvas_endpoints(p), \
        "конец на порту — чинить нечего, файл не переписывается"
    g = json.loads(p.read_text(encoding="utf-8"))
    assert g["links"][0]["source_point"] == [100.0, 120.0]


def test_open_straightness_beats_port(tmp_path):
    """Соосные пары продолжают давать прямые: канон посадил конец строгой
    прямой к ref — прямизна важнее порта (конец уезжает с центра грани)."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    nodes = [
        {"id": "a", "type": "equipment", "class_name": "unknow",
         "centroid": [100.0, 100.0], "bbox": [80.0, 80.0, 120.0, 120.0]},
        {"id": "b", "type": "connector", "class_name": "connector",
         "centroid": [110.0, 300.0], "bbox": None},
    ]
    links = [{"id": "e1", "source": "a", "target": "b",
              "source_point": [100.0, 120.0], "target_point": [110.0, 300.0],
              "waypoints": []}]
    p = _open_canvas(tmp_path, nodes, links)

    assert _reseat_canvas_endpoints(p)
    g = json.loads(p.read_text(encoding="utf-8"))
    assert g["links"][0]["source_point"] == [110.0, 120.0], \
        "ось конна накрыта гранью — канон обязан дать строгую прямую"


def test_open_keeps_end_on_manual_port(tmp_path):
    """Конец обычного (не _manual_route) ребра на РУЧНОМ порту узла —
    открытие не срывает его канон-лучом."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    nodes = [
        {"id": "a", "type": "equipment", "class_name": "unknow",
         "centroid": [100.0, 100.0], "bbox": [80.0, 80.0, 120.0, 120.0],
         "_ports": [{"dx": 20.0, "dy": 15.0}]},      # порт (120, 115)
        {"id": "b", "type": "connector", "class_name": "connector",
         "centroid": [180.0, 300.0], "bbox": None},
    ]
    links = [{"id": "e1", "source": "a", "target": "b",
              "source_point": [115.0, 120.0], "target_point": [180.0, 300.0],
              "waypoints": []}]
    p = _open_canvas(tmp_path, nodes, links)

    assert not _reseat_canvas_endpoints(p)
    g = json.loads(p.read_text(encoding="utf-8"))
    assert g["links"][0]["source_point"] == [115.0, 120.0], \
        "reseat-при-открытии сорвал конец с ручного порта"


def test_open_still_fixes_off_canon_ends(tmp_path):
    """Страховка §8.3.1 жива: конец ВНЕ всякого порта чинится каноном
    (коннектор-конец в 4px от центроида -> в центроид)."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    nodes = [
        {"id": "a", "type": "equipment", "class_name": "unknow",
         "centroid": [100.0, 100.0], "bbox": [80.0, 80.0, 120.0, 120.0]},
        {"id": "b", "type": "connector", "class_name": "connector",
         "centroid": [100.0, 300.0], "bbox": None},
    ]
    links = [{"id": "e1", "source": "a", "target": "b",
              "source_point": [100.0, 120.0], "target_point": [100.0, 296.0],
              "waypoints": []}]
    p = _open_canvas(tmp_path, nodes, links)

    assert _reseat_canvas_endpoints(p)
    g = json.loads(p.read_text(encoding="utf-8"))
    assert g["links"][0]["target_point"] == [100.0, 300.0]
    assert g["links"][0]["source_point"] == [100.0, 120.0]


# =====================================================================
# Ручная смена порта: дальний конец разворачивается навстречу
# =====================================================================

def _seg_cuts_bbox(sp, tp, bbox, shrink=0.75):
    """Отрезок [y,x]->[y,x] заходит в нутро bbox (Liang-Barsky)."""
    x1, y1, x2, y2 = (bbox[0] + shrink, bbox[1] + shrink,
                      bbox[2] - shrink, bbox[3] - shrink)
    if x1 >= x2 or y1 >= y2:
        return False
    ax, ay, bx, by = sp[1], sp[0], tp[1], tp[0]
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax - x1), (dx, x2 - ax), (-dy, ay - y1), (dy, y2 - ay)):
        if abs(p) < 1e-12:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return t0 <= t1


def _graph_two_boxes_side_by_side():
    """Два бокса с зазором по x; труба входит в ВЕРХНЮЮ грань правого —
    то есть в грань, которая левому боксу не смотрит."""
    nodes = [
        {"id": "left", "type": "equipment", "centroid": [200.0, 120.0],
         "bbox": [80.0, 90.0, 160.0, 150.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "right", "type": "equipment", "centroid": [200.0, 260.0],
         "bbox": [220.0, 90.0, 300.0, 300.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
    ]
    links = [
        {"id": "e1", "source": "left", "target": "right",
         "source_point": [120.0, 160.0],     # правая грань левого
         "target_point": [90.0, 260.0],      # ВЕРХНЯЯ грань правого
         "waypoints": []},
    ]
    return _wrap(nodes, links)


def test_endpoint_drag_turns_far_end_to_face(qapp, tmp_path):
    """Репро заказчика (graph_edited_fix.json, 2026-08-02): «вручную сменил
    порт — диагональ идёт через чужой блок».

    Протяжка двигает только ближний конец; если дальний остался на грани,
    которая новому положению не смотрит, прямая между ними режет блок
    насквозь. Судья это не ловит: through_box исключает свои концевые узлы.
    На отпускании дальний конец обязан развернуться навстречу — а конец,
    поставленный оператором, остаться бит-в-бит."""
    from ui.editors.undo_manager import SnapshotCommand

    ed = _editor(qapp, tmp_path, _graph_two_boxes_side_by_side())
    key = ed.model.edge_key("left", "right")
    e = ed.model.find_edge_data(key)
    assert _seg_cuts_bbox(e["source_point"], e["target_point"],
                          ed.nodes["right"]["bbox"]), \
        "фикстура обязана воспроизводить дефект: труба режет правый бокс"

    ed._dragging_endpoint = (key, "source")
    ed._ep_drag_armed = True
    ed._ep_on_port = False
    ed._ep_snap_cmd = SnapshotCommand(ed.model, ed._redraw_all)
    ed._ep_snap_cmd.execute()
    ed._drag_endpoint_to(163.0, 140.0)       # тянем конец ниже по правой грани
    chosen = list(e["source_point"])
    ed._end_endpoint_drag()

    assert e["source_point"] == chosen, "конец оператора обязан остаться на месте"
    assert not _seg_cuts_bbox(e["source_point"], e["target_point"],
                              ed.nodes["right"]["bbox"]), \
        "труба всё ещё режет блок — дальний конец не развернулся"


def test_endpoint_drag_far_end_untouched_without_drag(qapp, tmp_path):
    """Разворот — только по факту жеста: простой клик по концу (без протяжки,
    _ep_drag_armed=False) дальний конец не трогает."""
    ed = _editor(qapp, tmp_path, _graph_two_boxes_side_by_side())
    key = ed.model.edge_key("left", "right")
    e = ed.model.find_edge_data(key)
    far0 = list(e["target_point"])

    ed._dragging_endpoint = (key, "source")
    ed._ep_drag_armed = False                # клик, а не протяжка
    ed._end_endpoint_drag()

    assert e["target_point"] == far0, "без протяжки дальний конец неприкосновенен"


# =====================================================================
# Фиксированный вход + обязательный обход (решение заказчика 2026-08-02)
# =====================================================================

def _graph_pipe_past_obstacles():
    """Длинная труба снизу вверх; на прямом пути стоят два чужих блока —
    прямая между концами обязана их резать, обойти можно только коленом."""
    nodes = [
        {"id": "low", "type": "equipment", "centroid": [400.0, 220.0],
         "bbox": [200.0, 380.0, 240.0, 420.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "high", "type": "equipment", "centroid": [120.0, 220.0],
         "bbox": [200.0, 100.0, 240.0, 140.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "block1", "type": "equipment", "centroid": [300.0, 220.0],
         "bbox": [190.0, 280.0, 250.0, 320.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 0},
        {"id": "block2", "type": "equipment", "centroid": [220.0, 220.0],
         "bbox": [190.0, 200.0, 250.0, 240.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 0},
    ]
    links = [
        {"id": "e1", "source": "low", "target": "high",
         "source_point": [380.0, 220.0], "target_point": [140.0, 220.0],
         "waypoints": []},
    ]
    return _wrap(nodes, links)


def test_pinned_entry_gets_detour_not_straight_line(qapp, tmp_path):
    """Репро заказчика (graph_edited_fix2.json): «сменил вход — диагональ
    через чужие блоки без обхода».

    Решение заказчика: вход фиксирован там, куда его поставил оператор, но
    труба ОБЯЗАНА обойти препятствия. Раньше протяжка входа замораживала
    маршрут (_manual_route) и оставляла голую прямую сквозь всё подряд."""
    from ui.editors.undo_manager import SnapshotCommand

    ed = _editor(qapp, tmp_path, _graph_pipe_past_obstacles())
    key = ed.model.edge_key("low", "high")
    e = ed.model.find_edge_data(key)

    ed._dragging_endpoint = (key, "source")
    ed._ep_drag_armed = True
    ed._ep_on_port = False
    ed._ep_snap_cmd = SnapshotCommand(ed.model, ed._redraw_all)
    ed._ep_snap_cmd.execute()
    ed._drag_endpoint_to(198.0, 400.0)         # вход на левую грань низа
    chosen = list(e["source_point"])
    ed._end_endpoint_drag()

    assert e["source_point"] == chosen, "вход оператора обязан остаться на месте"
    assert e.get("_manual_route") is None, "маршрут не должен замораживаться"
    pts = [e["source_point"]] + list(e.get("waypoints") or []) + [e["target_point"]]
    assert len(pts) > 2, "труба обязана получить обход, а не остаться прямой"
    for a, b in zip(pts, pts[1:]):
        assert min(abs(b[1] - a[1]), abs(b[0] - a[0])) <= 1.5, \
            f"сегмент не ортогонален: {a} -> {b}"
    for nid in ("block1", "block2"):
        bb = ed.nodes[nid]["bbox"]
        assert not any(_seg_cuts_bbox(a, b, bb) for a, b in zip(pts, pts[1:])), \
            f"обход не обошёл {nid}"


def test_pinned_entry_survives_neighbour_drag(qapp, tmp_path):
    """Якорь входа — настоящий якорь: перенос СОСЕДА не сдвигает его ни на
    пиксель (прежний ручной порт был лишь подсказкой судье и терялся на
    уводе оси, замер аудита)."""
    from ui.editors.undo_manager import SnapshotCommand

    ed = _editor(qapp, tmp_path, _graph_pipe_past_obstacles())
    key = ed.model.edge_key("low", "high")
    e = ed.model.find_edge_data(key)
    ed._dragging_endpoint = (key, "source")
    ed._ep_drag_armed = True
    ed._ep_on_port = False
    ed._ep_snap_cmd = SnapshotCommand(ed.model, ed._redraw_all)
    ed._ep_snap_cmd.execute()
    ed._drag_endpoint_to(198.0, 400.0)
    ed._end_endpoint_drag()
    pinned = list(e["source_point"])

    _drag(ed, "high", 300.0, 130.0)            # таскаем ДАЛЬНИЙ узел

    assert e["source_point"] == pytest.approx(pinned), \
        f"якорь входа сорвало переносом соседа: {e['source_point']} != {pinned}"

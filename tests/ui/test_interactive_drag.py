# -*- coding: utf-8 -*-
"""T-C (Э0) — интерактивный drag в AdvancedGraphEditor против seating-контракта.

План: docs/planning/EDITOR_AFTER_LAYOUT_PLAN.md §4 T-C (пп. 1, 2, 4, 5).
Вызываются те же внутренние методы, что дёргает mouse-механика редактора:
start_drag_node / drag_node_to / end_drag_node
(advanced_graph_editor.py:3464/3474/3496 и mode_handlers/advanced_handlers.py:89-103).

Фикстуры — маленькие графы в координатах сцены, посаженные КАНОНОМ
(`modules/graph/core/seating.py`); каждая проверяется guard'ом
«reseat_edge — no-op», т.е. вход эквивалентен свежему выходу раскладки.

Координаты двойственны (CODING_GUIDE §6): centroid / source_point /
target_point / waypoints = [y, x]; bbox / segmentation = [x, y].

Известные нарушения оформлены @pytest.mark.xfail(strict=True): починка
(Э1/Э3) сломает xfail и заставит снять маркер. Числа в reason замерены
фактически (probe 2026-07-30, до правок).
"""
import copy
import json
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

IMG_W, IMG_H = 600, 400


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _wrap(nodes, links):
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def _graph_two_boxes(extra_nodes=(), extra_links=()):
    """Два бокса без скина, общая H-ось y=235 (канон: mid(200,270),
    зажатый в пересечение Y-диапазонов [160,240])."""
    nodes = [
        {"id": "box_a", "type": "equipment", "centroid": [200.0, 140.0],
         "bbox": [100.0, 160.0, 180.0, 240.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "box_b", "type": "equipment", "centroid": [270.0, 440.0],
         "bbox": [400.0, 140.0, 480.0, 400.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
    ] + list(extra_nodes)
    links = [
        {"id": "edge_1", "source": "box_a", "target": "box_b",
         "source_point": [235.0, 180.0], "target_point": [235.0, 400.0],
         "waypoints": []},
    ] + list(extra_links)
    return _wrap(nodes, links)


def _graph_box_conn():
    """Бокс + коннектор на общей оси y=200; конец у коннектора — центроид
    (жёсткое правило канона для connector-концов)."""
    nodes = [
        {"id": "box", "type": "equipment", "centroid": [200.0, 120.0],
         "bbox": [80.0, 160.0, 160.0, 240.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "conn", "type": "connector", "centroid": [200.0, 300.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 1},
    ]
    links = [
        {"id": "edge_1", "source": "box", "target": "conn",
         "source_point": [200.0, 160.0], "target_point": [200.0, 300.0],
         "waypoints": []},
    ]
    return _wrap(nodes, links)


# Ромб, вписанный в bbox [200,200,400,400] — контур ≠ bbox на гранях.
DIAMOND = [300.0, 200.0, 400.0, 300.0, 300.0, 400.0, 200.0, 300.0]


def _graph_poly(source_point):
    """Полигонный узел без скина + коннектор справа; конец на poly задаётся
    параметром (на контуре или на bbox-грани)."""
    nodes = [
        {"id": "poly", "type": "equipment", "centroid": [300.0, 300.0],
         "bbox": [200.0, 200.0, 400.0, 400.0], "segmentation": list(DIAMOND),
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "conn", "type": "connector", "centroid": [300.0, 600.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 1},
    ]
    links = [
        {"id": "edge_1", "source": "poly", "target": "conn",
         "source_point": list(source_point), "target_point": [300.0, 600.0],
         "waypoints": []},
    ]
    return _wrap(nodes, links)


def _assert_canonical(graph):
    """Guard: фикстура эквивалентна свежему выходу раскладки —
    reseat_edge канона не двигает ни один конец."""
    from modules.graph.core.seating import reseat_edge

    g = copy.deepcopy(graph)
    byid = {n["id"]: n for n in g["nodes"]}
    for e_orig, e in zip(graph["links"], g["links"]):
        reseat_edge(byid, e)
        assert e["source_point"] == e_orig["source_point"], \
            f"фикстура не канонична: sp {e_orig['source_point']} -> {e['source_point']}"
        assert e["target_point"] == e_orig["target_point"], \
            f"фикстура не канонична: tp {e_orig['target_point']} -> {e['target_point']}"


def _editor(qapp, tmp_path, graph, canvas=False):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")

    ed = AdvancedGraphEditor()
    if canvas:
        # Так делает вкладка (ui/tabs/base_graph_tab.py): флаг ставится
        # ДО load_data, сцена собирается как холст.
        ed._canvas_mode = True
    assert ed.load_data(str(ip), str(gp))
    return ed


def _drag(ed, node_id, x, y):
    """Полный жест drag тем же путём, что и мышь."""
    ed.start_drag_node(node_id)
    ed.drag_node_to(x, y)
    ed.end_drag_node()


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


# ── T-C.1: drag узла — дальний конец неприкосновенен ─────────────────────

def test_drag_node_far_end_untouched(qapp, tmp_path):
    """Семантика adjusting=End (§2 п.4 плана): при сдвиге узла A конец ребра
    у нетронутого узла B не меняется (метрика far_end_moved == 0, §3.2).

    Э1: xfail снят — drag по-прежнему пересчитывает ОБА конца (семантика
    adjusting=End — Э3), но пересчёт идёт каноном `seating.reseat_edge`,
    а канон на каноничном входе ИДЕМПОТЕНТЕН: конец у нетронутого box_b
    попадает в ту же точку [235,400] и waypoints не рождаются. До Э1
    distribute_connection_points уводил его на 35px (середина грани)."""
    g = _graph_two_boxes()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("box_a", "box_b"))
    tp_before = list(e["target_point"])

    _drag(ed, "box_a", 150.0, 200.0)   # +10px по x, сторона входа та же

    assert e["target_point"] == tp_before, (
        f"far_end_moved: {tp_before} -> {e['target_point']} "
        f"({_dist(tp_before, e['target_point']):.2f}px)")


def test_drag_node_leaves_nonincident_edges_alone(qapp, tmp_path):
    """Рёбра, не инцидентные сдвинутому узлу, не меняются вовсе
    (концы И waypoints) — сейчас это выполняется: _move_single_node
    пересчитывает только affected-рёбра."""
    box_c = {"id": "box_c", "type": "equipment", "centroid": [270.0, 540.0],
             "bbox": [520.0, 240.0, 560.0, 300.0], "segmentation": None,
             "class_id": 99, "class_name": "unknow", "degree": 1}
    # Горизонтальная труба box_b -> box_c по общей оси y=270 с коллинеарным
    # ручным waypoint'ом (проверяем, что и waypoints не трогаются).
    edge_2 = {"id": "edge_2", "source": "box_b", "target": "box_c",
              "source_point": [270.0, 480.0], "target_point": [270.0, 520.0],
              "waypoints": [[270.0, 500.0]]}
    g = _graph_two_boxes(extra_nodes=[box_c], extra_links=[edge_2])
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)

    e2 = ed.model.find_edge_data(ed.model.edge_key("box_b", "box_c"))
    before = json.dumps({"sp": e2["source_point"], "tp": e2["target_point"],
                         "wps": e2["waypoints"]}, sort_keys=True)

    _drag(ed, "box_a", 150.0, 200.0)

    after = json.dumps({"sp": e2["source_point"], "tp": e2["target_point"],
                        "wps": e2["waypoints"]}, sort_keys=True)
    assert after == before, "нетронутое ребро box_b—box_c изменилось"


# ── T-C.2: drag коннектора — конец в центроиде ───────────────────────────

def test_drag_connector_end_stays_at_centroid(qapp, tmp_path):
    """Канон (seating.node_anchor): connector-конец == центроид, жёстко.

    Э1: xfail снят — посадка в _recalculate_edge идёт каноном, виртуальный
    bbox r=CONNECTOR_MARKER_RADIUS из ПОСАДКИ убран (до Э1 конец садился
    на его ободок, замер 8.00px). Сам виртуальный bbox остаётся в
    hit-тестах/routing — это не посадка."""
    g = _graph_box_conn()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("box", "conn"))

    _drag(ed, "conn", 320.0, 200.0)    # +20px по x вдоль общей оси

    c = ed.nodes["conn"]["centroid"]
    d = _dist(e["target_point"], c)
    assert d <= 0.5, (
        f"конец не в центроиде коннектора: centroid={c}, "
        f"target_point={e['target_point']}, расстояние {d:.2f}px")


# ── T-C.4: undo/redo побайтово ───────────────────────────────────────────

def _state(ed, node_id, edge_key):
    """Проекция состояния: геометрия узла + концы/waypoints ребра.

    Сравниваем контрактные данные; служебные кэши _src_side/_tgt_side,
    которые drag дописывает в edge_data и undo не убирает, — вне контракта
    файла (в graph JSON их нет)."""
    n = ed.nodes[node_id]
    e = ed.model.find_edge_data(edge_key)
    return json.dumps({
        "centroid": n["centroid"], "bbox": n.get("bbox"),
        "segmentation": n.get("segmentation"),
        "sp": e["source_point"], "tp": e["target_point"],
        "wps": e["waypoints"],
    }, sort_keys=True)


def test_drag_undo_restores_bytewise(qapp, tmp_path):
    """Drag -> Ctrl+Z: центроид, bbox, концы и waypoints вернулись побайтово
    (json-снимок; образец строгости — test_square_size_ops T9).
    Drag здесь и двигает оба конца, и рождает waypoints — undo обязан
    вернуть всё."""
    g = _graph_two_boxes()
    ed = _editor(qapp, tmp_path, g)
    key = ed.model.edge_key("box_a", "box_b")

    s0 = _state(ed, "box_a", key)
    _drag(ed, "box_a", 150.0, 200.0)
    s1 = _state(ed, "box_a", key)
    assert s1 != s0, "drag обязан был изменить состояние (иначе тест пуст)"

    ed.undo()
    assert _state(ed, "box_a", key) == s0, "undo вернул не то состояние"

    ed.redo()
    assert _state(ed, "box_a", key) == s1, "redo вернул не пост-drag состояние"

    ed.undo()
    assert _state(ed, "box_a", key) == s0, "повторный undo сломал состояние"


# ── T-C.5: экран == файл (H6) ────────────────────────────────────────────

def test_visual_ends_are_what_is_drawn(qapp, tmp_path):
    """Механизм честности замера: QPainterPath ребра строится ровно из
    _visual_edge_ends (create_edge_item/_update_edge_path) — значит,
    сравнение _visual_edge_ends с данными и есть сравнение экрана с файлом."""
    ed = _editor(qapp, tmp_path, _graph_poly([340.0, 400.0]), canvas=True)
    key = ed.model.edge_key("poly", "conn")
    e = ed.model.find_edge_data(key)

    vsp, vtp = ed._visual_edge_ends(key, e)
    path = ed.edge_items[key].path()
    first = path.elementAt(0)
    last = path.elementAt(path.elementCount() - 1)
    # path в (x, y); vsp/vtp в [y, x]
    assert (first.x, first.y) == pytest.approx((vsp[1], vsp[0]))
    assert (last.x, last.y) == pytest.approx((vtp[1], vtp[0]))


def test_screen_equals_file_end_on_contour(qapp, tmp_path):
    """Конец, посаженный НА контур (канон), рисуется как есть — доводка
    _contour_endpoint не трогает точки на контуре (tol 0.5px)."""
    # [300, 400] — канон: луч вдоль общей оси y=300 (центроид коннектора)
    # бьёт в контур ромба в точке (400, 300); гвард ниже это подтверждает.
    g = _graph_poly([300.0, 400.0])
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g, canvas=True)
    key = ed.model.edge_key("poly", "conn")
    e = ed.model.find_edge_data(key)

    vsp, vtp = ed._visual_edge_ends(key, e)
    assert _dist(vsp, e["source_point"]) <= 0.5
    assert _dist(vtp, e["target_point"]) <= 0.5


@pytest.mark.xfail(strict=True, reason=(
    "H6 (экран != файл): конец на bbox-грани [340,400] рисуется лучом из "
    "центроида в вершину контура [300,400] — расхождение 40.00px; файл "
    "содержит одно, оператор видит другое. Э1 это НЕ закрыл: инструменты "
    "больше не сажают на bbox-грань, но уже сохранённый некононичный конец "
    "при загрузке отрисовывается доводкой _contour_endpoint по-старому "
    "(чинится Э3 «экран == итог» + метрика screen_vs_file §3.2)"))
def test_screen_equals_file_end_on_bbox_face(qapp, tmp_path):
    """Конец, пересаженный редактором на bbox-грань (так делают инструменты
    1-4 из §1 плана), при нарисованном контуре уезжает: _contour_endpoint
    (advanced_graph_editor.py:326-355) доводит его лучом из центроида."""
    # [340, 400] — правая грань bbox; контур (ромб) в этой точке на x=360.
    ed = _editor(qapp, tmp_path, _graph_poly([340.0, 400.0]), canvas=True)
    key = ed.model.edge_key("poly", "conn")
    e = ed.model.find_edge_data(key)

    vsp, vtp = ed._visual_edge_ends(key, e)
    # Конец у коннектора экран==файл (проходит и сейчас).
    assert _dist(vtp, e["target_point"]) <= 0.5
    # Конец у полигонного узла — H6.
    d = _dist(vsp, e["source_point"])
    assert d <= 0.5, (
        f"screen_vs_file: данные {e['source_point']}, нарисовано {vsp}, "
        f"расхождение {d:.2f}px")

# -*- coding: utf-8 -*-
"""T-C (Э0/Э3) — интерактивный drag в AdvancedGraphEditor против seating-контракта.

План: docs/planning/EDITOR_AFTER_LAYOUT_PLAN.md §4 T-C (пп. 1, 2, 4, 5) +
Э3a (§7): семантика adjusting=End, честный предпросмотр, подписи-якоря,
«экран == файл» (H6) + Э6/Э7-a (§7, H7): ортогональный маршрут при уводе
с оси (drag и add_edge), гистерезис почти-прямых, предпросмотр маршрута.
Вызываются те же внутренние методы, что дёргает mouse-механика редактора:
start_drag_node / drag_node_to / end_drag_node
(advanced_graph_editor.py:3464/3474/3496 и mode_handlers/advanced_handlers.py:89-103).

Фикстуры — маленькие графы в координатах сцены, посаженные КАНОНОМ
(`modules/graph/core/seating.py`); каждая проверяется guard'ом
«reseat_edge — no-op», т.е. вход эквивалентен свежему выходу раскладки.

Координаты двойственны (CODING_GUIDE §6): centroid / source_point /
target_point / waypoints = [y, x]; bbox / segmentation = [x, y].
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


def _full_path_xy(e):
    """Полный путь ребра в (x, y): source_point -> waypoints -> target_point."""
    pts = [(e["source_point"][1], e["source_point"][0])]
    pts += [(w[1], w[0]) for w in (e.get("waypoints") or [])]
    pts.append((e["target_point"][1], e["target_point"][0]))
    return pts


def _assert_orthogonal(pts):
    """Каждый сегмент пути строго H или V (tol 0.5px)."""
    for a, b in zip(pts, pts[1:]):
        assert abs(a[0] - b[0]) <= 0.5 or abs(a[1] - b[1]) <= 0.5, \
            f"диагональный сегмент {a} -> {b}"


def _seg_crosses_bbox(a, b, bbox):
    """Ортогональный сегмент a->b (x,y) заходит в НУТРО bbox [x1,y1,x2,y2]?"""
    x1, y1, x2, y2 = bbox
    if abs(a[1] - b[1]) <= 0.5:      # H
        return y1 < a[1] < y2 and min(a[0], b[0]) < x2 and max(a[0], b[0]) > x1
    if abs(a[0] - b[0]) <= 0.5:      # V
        return x1 < a[0] < x2 and min(a[1], b[1]) < y2 and max(a[1], b[1]) > y1
    return True                       # диагональ — консервативно «пересекает»


# ── T-C.1: drag узла — дальний конец неприкосновенен ─────────────────────

def test_drag_node_far_end_untouched(qapp, tmp_path):
    """Семантика adjusting=End (§2 п.4 плана): при сдвиге узла A конец ребра
    у нетронутого узла B не меняется (метрика far_end_moved == 0, §3.2).

    Э3: drag пересаживает ТОЛЬКО ближний конец (канон `seating.node_anchor`
    к дальнему концу/первому waypoint'у) — дальний конец не трогается по
    построению. Здесь сдвиг малый (вдоль оси), дальний конец совпадал и при
    старой механике (канон идемпотентен); жёсткий случай — перпендикулярный
    drag в test_drag_perpendicular_far_end_bitexact."""
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


# ── Э3.1: adjusting=End — перпендикулярный drag не двигает дальний конец ─

def test_drag_perpendicular_far_end_bitexact(qapp, tmp_path):
    """Drag узла A перпендикулярно оси прямого ребра A—B. Старая механика
    (reseat_edge целиком на drag-пути) выводила H-lock из НОВОЙ геометрии и
    пересаживала ОБА конца: конец у нетронутого box_b уезжал на новую общую
    ось (замер этой фикстуры: [235,400] -> [310,400], 75px). При adjusting=End
    конец у B байт-в-байт прежний; ближний конец сажается каноном
    `node_anchor` к дальнему концу (ось y=235 вне нового Y-диапазона узла
    [310,390] -> грань к соседу, поперечная координата зажата в рамку).

    Э6/Э7-a: увод оси (75px) больше порога snap_threshold — ребро получает
    ортогональный маршрут (до подключения роутера тут утверждалось
    waypoints == [] — drag оставлял косую диагональ).

    Этап A: порт — конец без эффективного замка прямизны садится в ПОРТ
    (центр правой грани, y=350), а не ray-клампом в угол ([310,180])."""
    g = _graph_two_boxes()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("box_a", "box_b"))
    tp_before = json.dumps(e["target_point"])

    _drag(ed, "box_a", 140.0, 350.0)   # вниз на 150px, ось сломана

    assert json.dumps(e["target_point"]) == tp_before, (
        f"far_end_moved: {tp_before} -> {e['target_point']}")
    assert e["source_point"] == pytest.approx([350.0, 180.0])
    assert e["waypoints"], "увод 75px > порога — маршрут обязан родиться (Э7-a)"
    _assert_orthogonal(_full_path_xy(e))


def test_drag_keeps_waypoints_intact(qapp, tmp_path):
    """Э3: у ребра с waypoint'ом drag узла не трогает ни промежуточные точки,
    ни дальний конец; ближний конец сажается к ПЕРВОМУ waypoint'у (не к
    центроиду соседа). Старая механика стирала waypoints и строила маршрут
    заново.

    Этап A: порт — ось waypoint'а (y=235) больше не накрыта рамкой узла,
    конец садится в порт (центр правой грани, y=350) вместо ray-клампа в
    угол ([310,180])."""
    g = _graph_two_boxes()
    g["links"][0]["waypoints"] = [[235.0, 300.0]]
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("box_a", "box_b"))

    _drag(ed, "box_a", 140.0, 350.0)

    assert e["waypoints"] == [[235.0, 300.0]], "waypoints тронуты"
    assert e["target_point"] == [235.0, 400.0], "дальний конец тронут"
    assert e["source_point"] == pytest.approx([350.0, 180.0])


# ── Э3.2: честный предпросмотр — до отпускания == после ──────────────────

def _edge_proj(e):
    return json.dumps({"sp": e["source_point"], "tp": e["target_point"],
                       "wps": e["waypoints"]}, sort_keys=True)


def test_drag_preview_equals_result(qapp, tmp_path):
    """Решение заказчика (§8.3): «что видишь при перетаскивании, то и
    получишь после отпускания». Концы и waypoints после drag_node_to
    (ДО end_drag_node) байт-в-байт равны состоянию после end_drag_node."""
    g = _graph_two_boxes()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("box_a", "box_b"))

    ed.start_drag_node("box_a")
    ed.drag_node_to(140.0, 350.0)      # жёсткий случай: ось ломается
    preview = _edge_proj(e)
    ed.end_drag_node()

    assert _edge_proj(e) == preview, "отпускание изменило то, что показывал drag"


# ── Э3.3: подписи — привязанный блок едет за узлом, непривязанный стоит ──

def _graph_with_blocks():
    g = _graph_two_boxes()
    g["text_blocks"] = [
        {"id": "tb_bound", "bbox": [120.0, 130.0, 160.0, 150.0],
         "text": "10LAB10", "source": "manual"},
        {"id": "tb_free", "bbox": [500.0, 60.0, 540.0, 80.0],
         "text": "прочее", "source": "manual"},
    ]
    # Привязка БЕЗ side: производной позиции (_bound_block_bbox) нет —
    # следование обязан дать сам drag (side-привязки следуют производной
    # позицией и без него).
    g["bindings"] = [{"block_id": "tb_bound", "node_id": "box_a",
                      "kind": "node", "text": "10LAB10"}]
    return g


def test_drag_bound_label_rides_unbound_stays(qapp, tmp_path):
    """Решение заказчика (§8.3): ПРИВЯЗАННЫЙ текст-блок едет за узлом на ту
    же дельту; непривязанный стоит."""
    g = _graph_with_blocks()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)

    _drag(ed, "box_a", 150.0, 200.0)      # дельта (+10, 0)

    assert ed.model.find_text_block("tb_bound")["bbox"] == \
        [130.0, 130.0, 170.0, 150.0], "привязанный блок не поехал за узлом"
    assert ed.model.find_text_block("tb_free")["bbox"] == \
        [500.0, 60.0, 540.0, 80.0], "непривязанный блок сдвинулся"


def test_drag_labels_undo_restores_both(qapp, tmp_path):
    """Undo после drag возвращает оба блока (и привязанный, и непривязанный)
    побайтово; redo — обратно."""
    g = _graph_with_blocks()
    ed = _editor(qapp, tmp_path, g)

    def blocks_state():
        return json.dumps([ed.model.find_text_block("tb_bound")["bbox"],
                           ed.model.find_text_block("tb_free")["bbox"]])

    s0 = blocks_state()
    _drag(ed, "box_a", 150.0, 200.0)
    s1 = blocks_state()
    assert s1 != s0, "drag обязан был сдвинуть привязанный блок (иначе тест пуст)"

    ed.undo()
    assert blocks_state() == s0, "undo не вернул текст-блоки"
    ed.redo()
    assert blocks_state() == s1, "redo не вернул пост-drag состояние блоков"


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
    (json-снимок; образец строгости — test_square_size_ops T9)."""
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


def test_drag_across_neighbor_flips_far_side(qapp, tmp_path):
    """Скрин заказчика 2026-07-31: узел перетащен НА ДРУГУЮ СТОРОНУ соседа.
    Жёсткое «дальний конец неприкосновенен» оставляло конец на изнаночной
    грани — труба прошивала блок насквозь. Смена стороны здесь — «нужда»:
    дальний конец перелетает на обращённую грань, труба прямая, прошивания
    нет."""
    g = _graph_two_boxes()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    key = ed.model.edge_key("box_a", "box_b")

    # box_a (слева, bbox 100..180) уводится далеко ВПРАВО за box_b (400..480)
    _drag(ed, "box_a", 600.0, 235.0)

    e = ed.model.find_edge_data(key)
    # дальний конец у box_b перелетел на ПРАВУЮ грань (x=480)
    assert e["target_point"][1] == pytest.approx(480.0, abs=0.5)
    # ближний конец — на ЛЕВОЙ грани уехавшего box_a (bbox теперь 560..640)
    assert e["source_point"][1] == pytest.approx(560.0, abs=0.5)
    # А->В (Э2b, переобъявление): концы жёстко в ПОРТАХ — общей slack-оси
    # больше нет («прямая важнее порта» отменена заказчиком 2026-07-31).
    # Ближний — середина левой грани box_a (y=235), дальний — середина
    # правой грани box_b (y=270); их связывает ортогональное колено.
    assert e["source_point"][0] == pytest.approx(235.0, abs=0.5)
    assert e["target_point"][0] == pytest.approx(270.0, abs=0.5)
    pts = _full_path_xy(e)
    _assert_orthogonal(pts)
    bb = next(n for n in ed.model.graph_data["nodes"]
              if n["id"] == "box_b")["bbox"]
    for a, b in zip(pts, pts[1:]):
        assert not _seg_crosses_bbox(a, b, bb), f"сегмент {a}->{b} прошивает box_b"


def test_drag_across_neighbor_offaxis_flips_and_routes_same_frame(qapp, tmp_path):
    """Второй скрин заказчика: флип стороны должен происходить ДО роутинга —
    иначе на отпускании оставалась голая диагональ. Узел уводится за соседа
    И с оси: дальний конец перелетает на обращённую грань, и В ТОМ ЖЕ кадре
    рождается ортогональный маршрут (без диагоналей, без прошивания)."""
    nodes = [
        {"id": "box_a", "type": "equipment", "centroid": [200.0, 140.0],
         "bbox": [100.0, 160.0, 180.0, 240.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "box_b", "type": "equipment", "centroid": [240.0, 440.0],
         "bbox": [400.0, 200.0, 480.0, 280.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
    ]
    links = [{"id": "edge_1", "source": "box_a", "target": "box_b",
              "source_point": [220.0, 180.0], "target_point": [220.0, 400.0],
              "waypoints": []}]
    g = _wrap(nodes, links)
    ed = _editor(qapp, tmp_path, g)
    key = ed.model.edge_key("box_a", "box_b")

    # box_a уводится далеко вправо-вниз ЗА box_b, вне слабины (dy=110)
    _drag(ed, "box_a", 620.0, 350.0)

    e = ed.model.find_edge_data(key)
    # дальний конец перелетел на обращённую (правую) грань box_b
    assert e["target_point"][1] == pytest.approx(480.0, abs=0.5)
    # ортогональный маршрут родился в том же жесте, диагоналей нет
    assert e.get("waypoints"), "маршрут не родился — осталась диагональ"
    pts = _full_path_xy(e)
    _assert_orthogonal(pts)
    bb = next(n for n in ed.model.graph_data["nodes"]
              if n["id"] == "box_b")["bbox"]
    for a, b in zip(pts, pts[1:]):
        assert not _seg_crosses_bbox(a, b, bb), f"сегмент {a}->{b} прошивает box_b"


def test_straight_coaxial_pipe_routes_around_foreign_box(qapp, tmp_path):
    """Скрин заказчика (CAPS, 2026-07-31): вертикальная СООСНАЯ труба шла
    сквозь чужой бокс — прошивание нутра чужого оборудования рождает обход
    так же, как увод с оси, даже когда div == 0."""
    nodes = [
        {"id": "top", "type": "equipment", "centroid": [80.0, 300.0],
         "bbox": [260.0, 40.0, 340.0, 120.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "mid", "type": "equipment", "centroid": [200.0, 300.0],
         "bbox": [260.0, 160.0, 340.0, 240.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 0},
        {"id": "bot", "type": "equipment", "centroid": [330.0, 300.0],
         "bbox": [260.0, 290.0, 340.0, 370.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
    ]
    links = [{"id": "edge_1", "source": "top", "target": "bot",
              "source_point": [120.0, 300.0], "target_point": [290.0, 300.0],
              "waypoints": []}]
    g = _wrap(nodes, links)
    ed = _editor(qapp, tmp_path, g)
    key = ed.model.edge_key("top", "bot")

    # лёгкий сдвиг вдоль оси: пара остаётся соосной (div=0), но прямая
    # прошивает mid — обход обязан родиться
    _drag(ed, "top", 300.0, 90.0)

    e = ed.model.find_edge_data(key)
    assert e.get("waypoints"), "обход не родился — труба сквозь бокс"
    pts = _full_path_xy(e)
    _assert_orthogonal(pts)
    mid_bb = next(n for n in ed.model.graph_data["nodes"]
                  if n["id"] == "mid")["bbox"]
    for a, b in zip(pts, pts[1:]):
        assert not _seg_crosses_bbox(a, b, mid_bb), \
            f"сегмент {a}->{b} прошивает чужой бокс"
    # КЛИРЕНС: обходной сегмент не липнет к грани обходимого бокса вплотную
    # (скрин заказчика «вдоль границы»); допуск чуть мягче ROUTE_CLEARANCE=6.
    x1, y1, x2, y2 = mid_bb
    for a, b in zip(pts, pts[1:]):
        if abs(a[0] - b[0]) <= 0.5:      # V-сегмент вдоль вертикальных граней
            lo, hi = min(a[1], b[1]), max(a[1], b[1])
            if hi > y1 and lo < y2 and x1 - 10 < a[0] < x2 + 10:
                gap = min(abs(a[0] - x1), abs(a[0] - x2))
                assert gap >= 5.0, f"V-сегмент x={a[0]:.1f} липнет к грани (зазор {gap:.2f})"


def _hug_gap_ok(pts, bbox, min_gap=5.0):
    """Осевые сегменты пути держат зазор >= min_gap от граней bbox
    (проверяются только сегменты, перекрывающиеся с bbox вдоль грани)."""
    x1, y1, x2, y2 = bbox
    for a, b in zip(pts, pts[1:]):
        if abs(a[0] - b[0]) <= 0.5:      # V-сегмент у вертикальных граней
            lo, hi = min(a[1], b[1]), max(a[1], b[1])
            if hi > y1 and lo < y2 and x1 - 20 < a[0] < x2 + 20:
                gap = min(abs(a[0] - x1), abs(a[0] - x2))
                assert gap >= min_gap, \
                    f"V-сегмент x={a[0]:.1f} липнет к грани (зазор {gap:.2f})"
        elif abs(a[1] - b[1]) <= 0.5:    # H-сегмент у горизонтальных граней
            lo, hi = min(a[0], b[0]), max(a[0], b[0])
            if hi > x1 and lo < x2 and y1 - 20 < a[1] < y2 + 20:
                gap = min(abs(a[1] - y1), abs(a[1] - y2))
                assert gap >= min_gap, \
                    f"H-сегмент y={a[1]:.1f} липнет к грани (зазор {gap:.2f})"


def _graph_pipe_free_box(side_cx=500.0, side_cy=200.0, extra_nodes=()):
    """Прямая V-труба top->bot (x=300, y 120..290) + свободный бокс side
    (80x80, без рёбер) в стороне — его надвигают на трубу СБОКУ."""
    nodes = [
        {"id": "top", "type": "equipment", "centroid": [80.0, 300.0],
         "bbox": [260.0, 40.0, 340.0, 120.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "bot", "type": "equipment", "centroid": [330.0, 300.0],
         "bbox": [260.0, 290.0, 340.0, 370.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "side", "type": "equipment", "centroid": [side_cy, side_cx],
         "bbox": [side_cx - 40.0, side_cy - 40.0,
                  side_cx + 40.0, side_cy + 40.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 0},
    ] + list(extra_nodes)
    links = [{"id": "edge_1", "source": "top", "target": "bot",
              "source_point": [120.0, 300.0], "target_point": [290.0, 300.0],
              "waypoints": []}]
    return _wrap(nodes, links)


def test_incident_drag_puts_pipe_along_foreign_face_births_route(qapp, tmp_path):
    """Инцидентный вариант: drag узла ставит его трубу вдоль чужой грани с
    зазором 2px (перекрытие 80px > порога) — обход рождается, хотя нутро
    не прошито (раньше триггер ловил только прошивание)."""
    # mid СТАТИЧЕН: левая грань x=302, труба x=300 -> зазор 2px сбоку
    mid = {"id": "mid", "type": "equipment", "centroid": [200.0, 342.0],
           "bbox": [302.0, 160.0, 382.0, 240.0], "segmentation": None,
           "class_id": 99, "class_name": "unknow", "degree": 0}
    g = _graph_pipe_free_box(extra_nodes=[mid])
    # side далеко (x=500) — не участвует
    ed = _editor(qapp, tmp_path, g)
    key = ed.model.edge_key("top", "bot")

    # сдвиг вдоль оси: пара соосна (div=0), нутро mid не прошито,
    # но труба лежит в 2px от его грани — обход обязан родиться
    _drag(ed, "top", 300.0, 90.0)

    e = ed.model.find_edge_data(key)
    assert e.get("waypoints"), "прижатие к чужой грани не родило обход"
    pts = _full_path_xy(e)
    _assert_orthogonal(pts)
    mid_bb = next(n for n in ed.model.graph_data["nodes"]
                  if n["id"] == "mid")["bbox"]
    for a, b in zip(pts, pts[1:]):
        assert not _seg_crosses_bbox(a, b, mid_bb), \
            f"сегмент {a}->{b} прошивает чужой бокс"
    _hug_gap_ok(pts, mid_bb)


def test_drag_offaxis_seats_on_port_with_honest_bend(qapp, tmp_path):
    """А->В (Э2b, переобъявление теста слабины Э3): бокс сдвинут на 20px по
    Y — раньше слабина прямизны СКОЛЬЗИЛА ближний конец по грани на ось
    дальнего (вариант «Б», отменён заказчиком 2026-07-31). Теперь конец
    ЖЁСТКО в порту (середина грани сдвинутого бокса), увод оси даёт
    честное ортогональное колено; дальний конец байт-в-байт. Прямизну
    возвращает микро-доводка сдвигом узла (Э4), не сползание конца."""
    graph = {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [1080, 1920]},
        "nodes": [
            {"id": "a", "type": "equipment", "class_name": "unknow",
             "centroid": [200.0, 130.0], "bbox": [100, 170, 160, 230],
             "degree": 1},
            {"id": "b", "type": "equipment", "class_name": "unknow",
             "centroid": [200.0, 430.0], "bbox": [400, 170, 460, 230],
             "degree": 1},
        ],
        "links": [{"id": "e1", "source": "a", "target": "b",
                   "source_point": [200.0, 160.0],
                   "target_point": [200.0, 400.0], "waypoints": []}],
        "text_blocks": [], "bindings": [],
    }
    ed = _editor(qapp, tmp_path, graph)
    key = ed.model.edge_key("a", "b")
    far_before = list(ed.model.find_edge_data(key)["target_point"])

    # сдвиг бокса a вверх на 20px: пара уже НЕ соосна по центроидам,
    # но в слабине прямизны (полувысоты 30+30 + tol 3 >= 20)
    _drag(ed, "a", 130.0, 180.0)

    e = ed.model.find_edge_data(key)
    assert e["target_point"] == far_before          # дальний конец не тронут
    # ближний конец: ПОРТ — середина правой грани сдвинутого бокса
    # (y=180, x=160), НЕ ось дальнего (200): скольжение отменено
    assert e["source_point"][0] == pytest.approx(180.0, abs=0.5)
    assert e["source_point"][1] == pytest.approx(160.0, abs=0.5)
    # увод оси даёт честное колено: маршрут есть и он ортогонален
    assert e.get("waypoints"), "колено обязано быть маршрутом, не диагональю"
    _assert_orthogonal(_full_path_xy(e))


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


def test_screen_equals_file_end_on_bbox_face(qapp, tmp_path):
    """H6 (Э3): конец, лежащий на канонической границе СВОЕГО узла
    (здесь — bbox-грань [340,400]), рисуется КАК ЕСТЬ — «экран == файл».
    До Э3 _contour_endpoint доводил его лучом из центроида в вершину контура
    [300,400] (расхождение 40.00px): файл содержал одно, оператор видел
    другое. Доводка лучом остаётся только для по-настоящему неканоничных
    концов (старые файлы, конец вне всякой канонической границы)."""
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


# ── Э6/Э7-a: ортогональный маршрут при drag и add_edge ───────────────────
#
# Требование заказчика (2026-07-31): при переносе узла и создании ребра
# «путь ортогональный, без пересечения узлов, минимальной длины» —
# вместо косой диагонали. Роутер — существующий route_edge_v2
# (ui/editors/edge_routing.py), подключение — _route_orthogonal.

def _obstacle_node():
    """Чужой узел между box_a и box_b (ниже прямой оси y=235)."""
    return {"id": "obs", "type": "equipment", "centroid": [320.0, 300.0],
            "bbox": [260.0, 280.0, 340.0, 360.0], "segmentation": None,
            "class_id": 99, "class_name": "unknow", "degree": 0}


def test_drag_offaxis_births_orthogonal_route(qapp, tmp_path):
    """Большой увод с оси (55px > snap_threshold 20): ребро получает
    ортогональный маршрут — все сегменты H/V, путь НЕ заходит в bbox чужого
    узла (R4 route_edge_v2), дальний конец байт-в-байт, ближний конец на
    грани сдвинутого узла и подводящий сегмент ⟂ грани (не луч в угол)."""
    g = _graph_two_boxes(extra_nodes=[_obstacle_node()])
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("box_a", "box_b"))
    tp_before = json.dumps(e["target_point"])

    _drag(ed, "box_a", 140.0, 330.0)   # вниз на 130px: слабина и порог позади

    assert json.dumps(e["target_point"]) == tp_before, "дальний конец тронут"
    wps = e["waypoints"]
    assert wps, "маршрут обязан был родиться"
    pts = _full_path_xy(e)
    _assert_orthogonal(pts)
    obs_bbox = ed.nodes["obs"]["bbox"]
    for a, b in zip(pts, pts[1:]):
        assert not _seg_crosses_bbox(a, b, obs_bbox), \
            f"сегмент {a} -> {b} прошивает чужой узел obs {obs_bbox}"
    # ближний конец на правой грани нового положения box_a, не в углу:
    sp = e["source_point"]
    bbox_a = ed.nodes["box_a"]["bbox"]
    assert sp[1] == pytest.approx(bbox_a[2]), "конец не на правой грани"
    assert bbox_a[1] - 0.5 <= sp[0] <= bbox_a[3] + 0.5
    # подводящий сегмент ⟂ грани (горизонтален): первый waypoint на оси конца
    assert wps[0][0] == pytest.approx(sp[0], abs=0.5), \
        "подводящий сегмент не перпендикулярен грани"


def test_drag_route_preview_equals_result(qapp, tmp_path):
    """H7: маршрут виден уже НА кадре протяжки и байт-в-байт равен итогу
    после отпускания — никакого «маршрут появился после отпускания»."""
    g = _graph_two_boxes(extra_nodes=[_obstacle_node()])
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("box_a", "box_b"))

    ed.start_drag_node("box_a")
    ed.drag_node_to(140.0, 330.0)
    assert e["waypoints"], "маршрут обязан быть уже на кадре протяжки"
    preview = _edge_proj(e)
    ed.end_drag_node()

    assert _edge_proj(e) == preview, "отпускание изменило показанный маршрут"


def test_drag_small_offaxis_seats_on_port(qapp, tmp_path):
    """Этап A: порт. Раньше при мёртвой слабине конец полз к кромке грани
    (ray-кламп: [206,160]) и остаток расхождения 6px < порога 20 оставлял
    «честную лёгкую диагональ». С портовой моделью точка входа ФИКСИРОВАНА
    в центре грани (y=246) — расхождение осей равно уводу (46 >= 20), и
    вместо диагонали рождается ортогональный маршрут. Дальний конец
    (коннектор) байт-в-байт."""
    g = _graph_box_conn()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("box", "conn"))
    # порог фикстуры: медианная ширина бокса 80 -> grid 40 -> snap 20
    assert ed.snap_threshold == 20

    _drag(ed, "box", 120.0, 246.0)   # вниз на 46px: слабина (до 40px) мертва

    assert e["target_point"] == [200.0, 300.0], "конец у коннектора тронут"
    assert e["source_point"] == pytest.approx([246.0, 160.0]), \
        "конец обязан сидеть в порту (центр правой грани)"
    assert e["waypoints"], "вход фиксирован на порту — маршрут обязан родиться"
    _assert_orthogonal(_full_path_xy(e))


def test_drag_undo_after_route_restores_bytewise(qapp, tmp_path):
    """Undo после drag с рождённым маршрутом — побайтово (waypoints входят
    в снапшоты DragNodeCommand); redo возвращает маршрут."""
    g = _graph_two_boxes()
    ed = _editor(qapp, tmp_path, g)
    key = ed.model.edge_key("box_a", "box_b")

    s0 = _state(ed, "box_a", key)
    _drag(ed, "box_a", 140.0, 350.0)
    s1 = _state(ed, "box_a", key)
    assert ed.model.find_edge_data(key)["waypoints"], \
        "маршрут обязан был родиться (иначе тест пуст)"

    ed.undo()
    assert _state(ed, "box_a", key) == s0, "undo не вернул состояние до drag"
    ed.redo()
    assert _state(ed, "box_a", key) == s1, "redo не вернул маршрут"


def test_add_edge_between_offset_nodes_routes_orthogonally(qapp, tmp_path):
    """add_edge между разнесёнными по диагонали узлами: вместо косой прямой —
    ортогональный маршрут; концы каноничны (повторный reseat_edge канона —
    no-op байт-в-байт, как в матрице T-B), подводящие сегменты ⟂ граням."""
    nodes = [
        {"id": "p", "type": "equipment", "centroid": [140.0, 140.0],
         "bbox": [100.0, 100.0, 180.0, 180.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 0},
        {"id": "q", "type": "equipment", "centroid": [340.0, 440.0],
         "bbox": [400.0, 300.0, 480.0, 380.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 0},
    ]
    ed = _editor(qapp, tmp_path, _wrap(nodes, []))

    assert ed.add_edge("p", "q")

    e = ed.model.find_edge_data(ed.model.edge_key("p", "q"))
    assert e["waypoints"], "маршрут обязан был родиться (увод 120px > порога)"
    pts = _full_path_xy(e)
    _assert_orthogonal(pts)
    # концы каноничны: пересадка каноном — no-op
    from modules.graph.core.seating import reseat_edge
    c = copy.deepcopy(e)
    reseat_edge(ed.nodes, c)
    assert c["source_point"] == e["source_point"], "конец p не каноничен"
    assert c["target_point"] == e["target_point"], "конец q не каноничен"
    assert c["waypoints"] == e["waypoints"]


# ── H7: batch-паритет, «увёл-вернул» (Э7-c), гистерезис (два порога) ─────

def _graph_batch_routable():
    """a1,a2 слева (выделение), c1,c2 справа; два прямых H-ребра — оба
    routable (без waypoints оператора, не _manual_route)."""
    def box(nid, cx, cy):
        return {"id": nid, "type": "equipment",
                "centroid": [float(cy), float(cx)],
                "bbox": [cx - 40.0, cy - 40.0, cx + 40.0, cy + 40.0],
                "segmentation": None, "class_id": 99, "class_name": "unknow",
                "degree": 1}
    nodes = [box("a1", 100, 100), box("a2", 100, 280),
             box("c1", 480, 100), box("c2", 480, 280)]
    links = [
        {"id": "e1", "source": "a1", "target": "c1",
         "source_point": [100.0, 140.0], "target_point": [100.0, 440.0],
         "waypoints": []},
        {"id": "e2", "source": "a2", "target": "c2",
         "source_point": [280.0, 140.0], "target_point": [280.0, 440.0],
         "waypoints": []},
    ]
    return _wrap(nodes, links)


def test_batch_drag_route_preview_equals_result(qapp, tmp_path):
    """H7-паритет (дефект Гаусса-Зейделя): batch-drag с >= 2 routable
    boundary-рёбрами — отпускание НИЧЕГО не пересчитывает, итог жеста
    байт-в-байт равен последнему кадру протяжки. Раньше отпускание
    перескорировало маршрут каждого ребра против СВЕЖИХ путей соседей
    (кадры скорили против прошлого кадра) — маршрут и даже посаженный
    конец прыгали (32/46 расхождений в репро)."""
    g = _graph_batch_routable()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e1 = ed.model.find_edge_data(ed.model.edge_key("a1", "c1"))
    e2 = ed.model.find_edge_data(ed.model.edge_key("a2", "c2"))

    ed.selected_nodes = {"a1", "a2"}
    ed.start_drag_node("a1")
    # многокадровая протяжка вниз-вправо: оба ребра сходят с осей на 60px
    for fx, fy in ((133.0, 120.0), (166.0, 140.0), (200.0, 160.0)):
        ed.drag_node_to(fx, fy)
    assert e1["waypoints"] and e2["waypoints"], \
        "оба boundary-ребра обязаны нести маршрут (иначе тест пуст)"
    preview = (_edge_proj(e1), _edge_proj(e2))
    ed.end_drag_node()

    assert (_edge_proj(e1), _edge_proj(e2)) == preview, \
        "отпускание batch-drag изменило показанный маршрут/конец"


def test_drag_away_and_back_keeps_ports_nonport_pair(qapp, tmp_path):
    """А->В (Э2b, переобъявление «увёл-вернул»): исходная прямая фикстуры —
    slack-ось y=235, НЕ порт (середина грани box_a — y=200). После жеста
    туда-обратно конец живёт в ПОРТУ, а не в старой slack-точке: увод оси
    (порт 200 vs дальний 235) остаётся честным ортогональным коленом.
    Дальний конец байт-в-байт (C6). Байт-восстановление прямой при
    увёл-вернул гарантируется только ПОРТОВЫМ парам — см. следующий тест."""
    g = _graph_two_boxes()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("box_a", "box_b"))

    _drag(ed, "box_a", 140.0, 330.0)          # увод вниз: маршрут родился
    assert e["waypoints"], "маршрут обязан был родиться"
    assert e.get("_auto_route") is True, "авто-маршрут обязан нести флаг"

    _drag(ed, "box_a", 140.0, 200.0)          # возврат ровно в исходную
    assert e["source_point"] == [200.0, 180.0]      # порт: середина грани
    assert e["target_point"] == [235.0, 400.0]      # дальний байт-в-байт
    assert e["waypoints"], "увод порт-ось обязан остаться коленом"
    _assert_orthogonal(_full_path_xy(e))


def test_drag_away_and_back_restores_straight_port_pair(qapp, tmp_path):
    """Э7-c («увёл-вернул») в мире портов: пара, строго соосная ЧЕРЕЗ ПОРТЫ
    (середины граней на одной оси), после жеста туда-обратно снова прямая:
    waypoints пусты, флаг снят, концы байт-в-байт исходные."""
    nodes = [
        {"id": "box_a", "type": "equipment", "centroid": [200.0, 140.0],
         "bbox": [100.0, 160.0, 180.0, 240.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "box_b", "type": "equipment", "centroid": [200.0, 440.0],
         "bbox": [400.0, 160.0, 480.0, 240.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
    ]
    links = [{"id": "edge_1", "source": "box_a", "target": "box_b",
              "source_point": [200.0, 180.0], "target_point": [200.0, 400.0],
              "waypoints": []}]
    g = _wrap(nodes, links)
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("box_a", "box_b"))

    _drag(ed, "box_a", 140.0, 330.0)          # увод вниз: маршрут родился
    assert e["waypoints"], "маршрут обязан был родиться"
    assert e.get("_auto_route") is True

    _drag(ed, "box_a", 140.0, 200.0)          # возврат ровно в исходную
    assert e["waypoints"] == [], "мёртвое авто-колено осталось в данных"
    assert "_auto_route" not in e, "флаг обязан гаснуть вместе с маршрутом"
    assert e["source_point"] == [200.0, 180.0]
    assert e["target_point"] == [200.0, 400.0]


def test_drag_hysteresis_no_flicker(qapp, tmp_path):
    """Э6/H7-гистерезис (два порога): рождение при div >= snap_threshold
    (20), гашение при div < snap_threshold/2 (10), между порогами
    существующее состояние сохраняется. Колебание div 19.5 <-> 20.5 вокруг
    порога рождения не меняет форму — маршрут, родившись, живёт (не мигает
    «родился/умер» на соседних кадрах); гаснет только ниже 10.

    Фикстура box+conn: при y бокса > 240 конн-ось y=200 ниже Y-диапазона
    бокса.

    Этап A: порт — конец сидит в центре грани, div равен уводу центра с
    оси (y - 200), а не прижатию к кромке: на y=260.5 div 60.5, маршрут
    жив во всей полосе колебаний. Гашение — когда оживает слабина
    прямизны (грань снова накрывает ось конна: y <= 240.5) и конец
    возвращается на прямую."""
    g = _graph_box_conn()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("box", "conn"))
    assert ed.snap_threshold == 20

    ed.start_drag_node("box")
    ed.drag_node_to(120.0, 260.5)      # div 60.5 >= 20 — маршрут родился
    assert e["waypoints"], "маршрут обязан родиться (div 60.5)"
    shape = len(e["waypoints"])
    for fy in (259.5, 260.5, 259.5, 260.5):
        ed.drag_node_to(120.0, fy)     # колебание ±1px — порт и маршрут стоят
        assert e["waypoints"], f"маршрут мигнул (умер) на y={fy}"
        assert len(e["waypoints"]) == shape, f"форма изменилась на y={fy}"
        _assert_orthogonal(_full_path_xy(e))
    # А->В (Э2b): слабины больше нет — гашение только при СТРОГОЙ
    # соосности: порт-середина грани ложится ровно на ось конна (y=200)
    ed.drag_node_to(120.0, 200.0)
    assert e["waypoints"] == [], "строгая соосность обязана погасить маршрут"
    assert "_auto_route" not in e
    assert e["source_point"] == pytest.approx([200.0, 160.0]), \
        "конец обязан сидеть в порту на общей оси"
    ed.end_drag_node()


# ── Полигонные препятствия: наложения судятся по РЕАЛЬНОМУ контуру ───────
#
# Файл заказчика graph_edited_star.json: коннекторы живут в «кармане»
# невыпуклого гиганта (деаэратор node_28, bbox 616x373, заполненность
# ~0.54) — маршрут над ПУСТЫМ углом габарита браковался за «пересечение»
# bbox, роутинг молча отказывал, диагонали оставались. Известная ловушка
# проекта (modules/graph/core/layout/_shapes.py): габарит невыпуклого
# контура почти вдвое больше фигуры — наложения судятся по реальной форме.

# L-контур «станции»: левая колонна x[200,280] + нижняя плита y[420,500];
# карман (пустой угол bbox [200,200,600,500]) — x>280, y<420.
L_STATION = [200.0, 200.0, 280.0, 200.0, 280.0, 420.0,
             600.0, 420.0, 600.0, 500.0, 200.0, 500.0]


def _graph_poly_pocket(extra_nodes=()):
    """Невыпуклый полигон без скина + коннектор в его кармане + коннектор
    снаружи справа; ребро между коннекторами (концы = центроиды, канон)."""
    nodes = [
        {"id": "station", "type": "equipment", "centroid": [350.0, 240.0],
         "bbox": [200.0, 200.0, 600.0, 500.0],
         "segmentation": list(L_STATION),
         "class_id": 99, "class_name": "unknow", "degree": 0},
        {"id": "conn_a", "type": "connector", "centroid": [300.0, 450.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 1},
        {"id": "conn_b", "type": "connector", "centroid": [120.0, 700.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 1},
    ] + list(extra_nodes)
    links = [
        {"id": "edge_1", "source": "conn_a", "target": "conn_b",
         "source_point": [300.0, 450.0], "target_point": [120.0, 700.0],
         "waypoints": []},
    ]
    return _wrap(nodes, links)


def test_drag_connector_in_poly_pocket_births_route_over_empty_corner(
        qapp, tmp_path):
    """Синтетика «звезды»: коннектор в кармане невыпуклого полигона, граф
    большой (BOUNDED). Обход при drag РОЖДАЕТСЯ над пустым углом bbox
    полигона (раньше блокировался браковкой по габариту + запирался
    негативным кэшем fail_anchor) и реальный контур не прошит."""
    from ui.editors.advanced_graph_editor import _seg_pierces_polygon

    # 41 дальний филлер → узлов 44, препятствий > ROUTE_EXACT_MAX_OBS (40):
    # режим BOUNDED — тот же, что на боевом листе заказчика.
    fillers = [
        {"id": f"filler_{i}", "type": "equipment",
         "centroid": [100.0 + (i // 7) * 60.0 + 10.0,
                      1000.0 + (i % 7) * 60.0 + 10.0],
         "bbox": [1000.0 + (i % 7) * 60.0, 100.0 + (i // 7) * 60.0,
                  1020.0 + (i % 7) * 60.0, 120.0 + (i // 7) * 60.0],
         "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 0}
        for i in range(41)
    ]
    g = _graph_poly_pocket(extra_nodes=fillers)
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g, canvas=True)
    e = ed.model.find_edge_data(ed.model.edge_key("conn_a", "conn_b"))

    ed.start_drag_node("conn_a")
    ctx = ed._drag_route_ctx
    assert ctx["bounded"], "фикстура обязана попадать в BOUNDED-режим"
    assert [nid for nid, _seg in ctx["polys"]] == ["station"], \
        "полигон без скина обязан лежать в кэше контуров, не в bbox-узлах"
    assert "station" not in [nid for nid, _bb in ctx["nodes"]]
    ed.drag_node_to(450.0, 380.0)      # увод в кармане: div 130 >> порога
    ed.end_drag_node()

    assert e["waypoints"], \
        "обход над пустым углом bbox полигона обязан родиться"
    assert e.get("_auto_route") is True
    pts = _full_path_xy(e)
    _assert_orthogonal(pts)
    for a, b in zip(pts, pts[1:]):
        assert not _seg_pierces_polygon(a[0], a[1], b[0], b[1], L_STATION), \
            f"сегмент {a} -> {b} прошивает реальный контур станции"


def test_route_through_poly_contour_still_rejected(qapp, tmp_path):
    """Негатив: маршрут СКВОЗЬ реальный контур полигона бракуется — над
    пустым углом bbox легален лишь путь, не задевающий фигуру. Роутер
    подменяется: сперва отдаёт маршрут сквозь левую колонну L-контура
    (reject, waypoints не появляются), затем маршрут по карману (принят)."""
    import ui.editors.advanced_graph_editor as age

    g = _graph_poly_pocket()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("conn_a", "conn_b"))
    orig = age.route_edge_v2

    # сквозь колонну: (450,300) -> (240,300) -> (240,120) -> (700,120)
    piercing = [[300.0, 240.0], [120.0, 240.0]]
    # по карману: (450,300) -> (700,300) -> (700,120)
    clean = [[300.0, 700.0]]
    try:
        age.route_edge_v2 = lambda **kw: [list(w) for w in piercing]
        # ступень 1: главный роутер бракует прошивание реального контура
        assert ed._route_orthogonal_main(e) is False, \
            "маршрут сквозь реальный контур обязан браковаться"
        assert e["waypoints"] == [], "бракованный маршрут попал в данные"
        assert "_auto_route" not in e

        # Э3-лестница: отказ главного НЕ оставляет диагональ — L-фолбэк
        # находит чистое колено по карману (контур запрещён и фолбэку,
        # кандидат сквозь колонну отброшен)
        assert ed._route_orthogonal(e) is True
        assert e["waypoints"] == clean
        assert e.get("_auto_route") is True
        assert "_route_defect" not in e
        e["waypoints"] = []
        e.pop("_auto_route", None)

        age.route_edge_v2 = lambda **kw: [list(w) for w in clean]
        assert ed._route_orthogonal(e) is True, \
            "маршрут над пустым углом bbox (мимо фигуры) обязан приниматься"
        assert e["waypoints"] == clean
    finally:
        age.route_edge_v2 = orig


def test_route_ladder_marks_defect_when_no_way(qapp, tmp_path):
    """Э3, ступень 3: главный роутер отказал, оба L-колена прошивают чужие
    боксы — диагональ остаётся, но помечается _route_defect (не в sha);
    успешный маршрут потом снимает пометку."""
    import ui.editors.advanced_graph_editor as age

    nodes = [
        {"id": "ca", "type": "connector", "centroid": [100.0, 100.0],
         "bbox": None, "class_name": "connector", "degree": 1},
        {"id": "cb", "type": "connector", "centroid": [300.0, 300.0],
         "bbox": None, "class_name": "connector", "degree": 1},
        # сплошная стена поперёк коридора (+40px запас перебора Z):
        # ни один H/V-сегмент между ca и cb её не минует
        {"id": "wall", "type": "equipment", "centroid": [200.0, 200.0],
         "bbox": [40.0, 140.0, 360.0, 260.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 0},
    ]
    links = [{"id": "e1", "source": "ca", "target": "cb",
              "source_point": [100.0, 100.0], "target_point": [300.0, 300.0],
              "waypoints": []}]
    g = _wrap(nodes, links)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("ca", "cb"))
    orig = age.route_edge_v2
    try:
        age.route_edge_v2 = lambda **kw: []          # главный отказал
        assert ed._route_orthogonal(e) is False
        assert e["waypoints"] == []                  # диагональ осталась
        assert e.get("_route_defect") is True        # но помечена

        age.route_edge_v2 = orig                     # роутер ожил
        if ed._route_orthogonal(e):
            assert "_route_defect" not in e          # успех снял пометку
    finally:
        age.route_edge_v2 = orig


# ── Э2d: resize через движок ─────────────────────────────────────────────

def test_resize_reseats_only_near_end(qapp, tmp_path):
    """Э2d: resize пересаживает ТОЛЬКО концы у изменённого узла (движок,
    порт); дальний конец соседа байт-в-байт. Легаси-путь переписывал ОБА
    конца по центроидам (терял порты, рушил C6)."""
    g = _graph_two_boxes()
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("box_a", "box_b"))
    tp_before = list(e["target_point"])

    node = ed.nodes["box_a"]
    node["bbox"] = [90.0, 150.0, 190.0, 250.0]   # раздули вокруг центра
    ed._reseat_after_resize("box_a")

    assert e["target_point"] == tp_before          # C6: дальний не тронут
    assert e["source_point"] == [200.0, 190.0]     # порт новой грани


def test_size_panel_rescales_manual_ports(qapp, tmp_path):
    """Э2d: панель «Размеры» масштабирует ручные порты (раньше теряла)."""
    g = _graph_two_boxes()
    ed = _editor(qapp, tmp_path, g)
    node = ed.nodes["box_a"]
    node["segmentation"] = [100.0, 160.0, 180.0, 160.0,
                            180.0, 240.0, 100.0, 240.0]
    node["_ports"] = [{"dx": 40.0, "dy": 0.0}]

    ed._resize_node_poly(node, 2.0)
    assert node["_ports"] == [{"dx": 80.0, "dy": 0.0}]


# ── 2026-08-01: чужие рёбра при drag НЕПРИКОСНОВЕННЫ (уступание удалено) ──

def _edge_bytes(e):
    return json.dumps({"sp": e.get("source_point"), "tp": e.get("target_point"),
                       "wps": e.get("waypoints"), "auto": e.get("_auto_route")},
                      sort_keys=True)


def test_foreign_pipe_is_untouchable_under_drag(qapp, tmp_path):
    """Решение заказчика (третья итерация «ребро убегает от бокса»):
    drag трогает ТОЛЬКО рёбра таскаемого узла. Чужая труба байт-в-байт
    при сближении, наезде и уводе; наложение — честный дефект (судья:
    «сквозь узел»), лечат доводка (Э4) или оператор."""
    g = _graph_pipe_free_box()
    _assert_canonical(g)
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("top", "bot"))
    before = _edge_bytes(e)

    for fx in (342.0, 322.0, 300.0, 338.0, 500.0):   # сближение/наезд/увод
        _drag(ed, "side", fx, 200.0)
        assert _edge_bytes(e) == before, f"чужая труба шевельнулась на x={fx}"

    ed.undo()
    e = ed.model.find_edge_data(ed.model.edge_key("top", "bot"))
    assert _edge_bytes(e) == before


def test_batch_drag_foreign_pipe_untouchable(qapp, tmp_path):
    """Batch-вариант: группа наезжает на чужую трубу — труба байт-в-байт."""
    buddy = {"id": "buddy", "type": "equipment", "centroid": [350.0, 500.0],
             "bbox": [460.0, 310.0, 540.0, 390.0], "segmentation": None,
             "class_id": 99, "class_name": "unknow", "degree": 0}
    g = _graph_pipe_free_box(extra_nodes=[buddy])
    ed = _editor(qapp, tmp_path, g)
    e = ed.model.find_edge_data(ed.model.edge_key("top", "bot"))
    before = _edge_bytes(e)

    ed.selected_nodes = {"side", "buddy"}
    ed.start_drag_node("side")
    ed.drag_node_to(322.0, 200.0)
    assert _edge_bytes(e) == before, "batch тронул чужую трубу на кадре"
    ed.end_drag_node()
    assert _edge_bytes(e) == before, "отпускание batch тронуло чужую трубу"


def _graph_two_straight_pipes():
    """Бокс с двумя строго горизонтальными трубами одной грани (слоты
    pitch=18: y 11.05/28.95 при рамке 0..40) к двум коннекторам."""
    nodes = [
        {"id": "a", "type": "equipment", "centroid": [20.0, 20.0],
         "bbox": [0.0, 0.0, 40.0, 40.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 2},
        {"id": "c1", "type": "connector", "centroid": [13.333333333333332, 200.0],
         "bbox": None, "class_name": "connector", "degree": 1},
        {"id": "c2", "type": "connector", "centroid": [26.666666666666668, 200.0],
         "bbox": None, "class_name": "connector", "degree": 1},
    ]
    links = [
        {"id": "e1", "source": "a", "target": "c1",
         "source_point": [13.333333333333332, 40.0], "target_point": [13.333333333333332, 200.0],
         "waypoints": []},
        {"id": "e2", "source": "a", "target": "c2",
         "source_point": [26.666666666666668, 40.0], "target_point": [26.666666666666668, 200.0],
         "waypoints": []},
    ]
    return _wrap(nodes, links)


def test_width_change_gate_reverts_instead_of_diagonal(qapp, tmp_path):
    """Гейт «не хуже входа» при разводе толщиной (репро заказчика:
    «утолщаю — разъезжаются, но одно ребро становится диагональным»):
    вся лестница маршрутов отказала => ребро НЕ смеет стать косым —
    полный откат в прежний слот (нахлёст чернил честнее косой)."""
    g = _graph_two_straight_pipes()
    ed = _editor(qapp, tmp_path, g)
    e1 = ed.model.find_edge_data(ed.model.edge_key("a", "c1"))
    e2 = ed.model.find_edge_data(ed.model.edge_key("a", "c2"))
    sp1, sp2 = list(e1["source_point"]), list(e2["source_point"])

    ed.set_edge_brush_size(16)
    orig = ed._route_orthogonal
    ed._route_orthogonal = lambda e, alive=False: False   # лестница мертва
    try:
        ed.apply_edge_style_at(ed.model.edge_key("a", "c1"), "size")
        ed.apply_edge_style_at(ed.model.edge_key("a", "c2"), "size")
    finally:
        ed._route_orthogonal = orig

    assert e1["render_width"] == 16 and e2["render_width"] == 16
    assert ed._edge_is_ortho(e1) and ed._edge_is_ortho(e2), \
        "ортогональное ребро стало косым от развода толщиной"
    assert e1["source_point"] == sp1 and e2["source_point"] == sp2, \
        "при мёртвой лестнице ребро обязано остаться в прежнем слоте"


def test_width_change_spreads_with_bends_stays_ortho(qapp, tmp_path):
    """Позитив: канал есть — утолщение разводит слоты (шаг по чернилам),
    увод оси закрывается коленом, ВСЕ сегменты ортогональны."""
    g = _graph_two_straight_pipes()
    ed = _editor(qapp, tmp_path, g)
    e1 = ed.model.find_edge_data(ed.model.edge_key("a", "c1"))
    e2 = ed.model.find_edge_data(ed.model.edge_key("a", "c2"))

    ed.set_edge_brush_size(16)
    ed.apply_edge_style_at(ed.model.edge_key("a", "c1"), "size")
    ed.apply_edge_style_at(ed.model.edge_key("a", "c2"), "size")

    y1, y2 = e1["source_point"][0], e2["source_point"][0]
    assert abs(y2 - y1) >= 20.0 - 1e-6, "слоты обязаны разойтись по чернилам"
    _assert_orthogonal(_full_path_xy(e1))
    _assert_orthogonal(_full_path_xy(e2))

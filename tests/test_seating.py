# -*- coding: utf-8 -*-
"""T-A (Э0, §4 EDITOR_AFTER_LAYOUT_PLAN): юнит-тесты канона посадки.

Канон — modules/graph/core/seating.py (node_anchor / reseat_edge /
reseat_all_endpoints) + pretransform.reproject_edge_endpoints. Первые прямые
юнит-тесты этого модуля: до Э0 seating не имел ни одного.

Координаты двойственны (CODING_GUIDE §6) — на каждом assert сверять ось:
    centroid / source_point / target_point / waypoints = [y, x]
    bbox = [x1, y1, x2, y2];  segmentation = [x, y, x, y, ...]
    node_anchor(...) возвращает (x, y)

Известные баги оформлены @pytest.mark.xfail(strict=True): сьют зелёный,
а починка (Э2) сломает xfail и заставит снять маркер.

Тесты чистые: stdlib + pytest, без Qt/shapely/numpy/torch.
"""
import math

import pytest

from modules.graph.core import seating
from modules.graph.core.graph_access import is_connector
from modules.graph.core.pretransform import (FIXED_SIZES,
                                             _skin_content_rect,
                                             point_on_polygon,
                                             reproject_edge_endpoints)
from modules.graph.core.seating import (STRAIGHT_TOL, _pick_axis_coord,
                                        _seg_lock, node_anchor,
                                        reseat_all_endpoints)

# ---------------------------------------------------------------------------
# Фабрики синтетики (детерминированные, никакого random/времени)
# ---------------------------------------------------------------------------


def _conn(nid, y, x):
    """Коннектор: centroid=[y, x], bbox не нужен."""
    return {"id": nid, "type": "connector", "class_name": "connector",
            "centroid": [float(y), float(x)]}


def _box(nid, bbox, cls="unknow"):
    """Прямоугольный узел, centroid в центре bbox ([x1, y1, x2, y2])."""
    x1, y1, x2, y2 = (float(v) for v in bbox)
    return {"id": nid, "type": "equipment", "class_name": cls,
            "centroid": [(y1 + y2) / 2.0, (x1 + x2) / 2.0],
            "bbox": [x1, y1, x2, y2]}


# C-образный (вогнутый) контур 100x100 с выемкой справа (y растёт вниз):
# верхняя и нижняя перекладины + левый хребет x=[0..20].
C_SEG = [0.0, 0.0, 100.0, 0.0, 100.0, 20.0, 20.0, 20.0,
         20.0, 80.0, 100.0, 80.0, 100.0, 100.0, 0.0, 100.0]


def _cpoly(nid, offx=0.0, offy=0.0):
    """Полигонный узел без скина: контур C_SEG, центроид в левом хребте."""
    seg = [C_SEG[i] + (offx if i % 2 == 0 else offy)
           for i in range(len(C_SEG))]
    return {"id": nid, "type": "equipment", "class_name": "unknow",
            "centroid": [50.0 + offy, 10.0 + offx],
            "bbox": [offx, offy, offx + 100.0, offy + 100.0],
            "segmentation": seg}


def _graph(nodes, links):
    return {"nodes": nodes, "links": links}


# letterbox-сценарий требует aspect_hw скина (tools/skin_geometry.json +
# CLASS_NAME_TO_SKIN); без них канон деградирует до bbox и тест беспредметен.
_LETTERBOX = _skin_content_rect(
    {"class_name": "armatura_ruchn", "bbox": [100.0, 100.0, 142.0, 138.0]})
needs_skin_geometry = pytest.mark.skipif(
    _LETTERBOX is None,
    reason="skin_geometry.json/CLASS_NAME_TO_SKIN недоступны — "
           "letterbox-след скина неисчислим")


# ---------------------------------------------------------------------------
# 1. Коннектор -> центроид, жёстко (deg 1/2/3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("deg", [1, 2, 3])
def test_connector_end_is_centroid(deg):
    """Конец ребра у коннектора == его centroid [y, x], сколько бы рёбер
    к нему ни шло и в какой бы роли (source/target) он ни был."""
    k = _conn("k", 200.0, 300.0)          # y=200, x=300
    b1 = _box("b1", [420, 180, 460, 220])  # справа, H-связь
    b2 = _box("b2", [280, 100, 320, 140])  # сверху, V-связь
    b3 = _box("b3", [280, 260, 320, 300])  # снизу, V-связь
    all_links = [{"source": "k", "target": "b1"},
                 {"source": "b2", "target": "k"},
                 {"source": "k", "target": "b3"}]
    g = _graph([k, b1, b2, b3], all_links[:deg])
    reseat_all_endpoints(g)

    for e in g["links"]:
        key = "source_point" if e["source"] == "k" else "target_point"
        assert e[key] == [200.0, 300.0], \
            f"конец у коннектора обязан быть centroid [y,x], ребро {e}"
        # канон: ключ waypoints обязан существовать и быть списком
        assert e["waypoints"] == []


def test_node_anchor_connector_ignores_lock():
    """node_anchor коннектора возвращает центроид (x, y) при любом lock."""
    k = _conn("k", 200.0, 300.0)
    assert node_anchor(k, 999.0, 999.0) == (300.0, 200.0)
    assert node_anchor(k, 999.0, 999.0, ("H", 123.0)) == (300.0, 200.0)
    assert node_anchor(k, 999.0, 999.0, ("V", 123.0)) == (300.0, 200.0)


# ---------------------------------------------------------------------------
# 2. FIXED_SIZES-скин -> граница _skin_content_rect (letterbox), НЕ bbox
# ---------------------------------------------------------------------------


@needs_skin_geometry
def test_skin_letterbox_anchor_on_content_rect_not_bbox():
    """armatura_ruchn (aspect_hw=0.5) в боксе 42x38: графика 42x21 с полями
    8.5px сверху/снизу. Подход сверху садится на КОНТЕНТ (y=108.5),
    а не на бокс (y=100)."""
    assert "armatura_ruchn" in FIXED_SIZES
    skin = _box("s", [100, 100, 142, 138], cls="armatura_ruchn")
    assert _skin_content_rect(skin) == (100.0, 108.5, 142.0, 129.5)

    # прямой вызов: подход сверху -> верхняя грань КОНТЕНТА
    assert node_anchor(skin, 121.0, 50.0) == (121.0, 108.5)
    # подход справа: боковые грани контента совпадают с bbox (letterbox по Y)
    assert node_anchor(skin, 300.0, 119.0) == (142.0, 119.0)


@needs_skin_geometry
def test_skin_letterbox_end_to_end_vertical_link():
    """Сквозной путь reseat_all_endpoints: коннектор строго над скином ->
    V-замок -> конец на верхней грани content-rect [108.5, 121], не [100, 121]."""
    skin = _box("s", [100, 100, 142, 138], cls="armatura_ruchn")
    g = _graph([_conn("k", 50.0, 121.0), skin],
               [{"source": "k", "target": "s"}])
    reseat_all_endpoints(g)
    e = g["links"][0]
    assert e["source_point"] == [50.0, 121.0]      # [y, x] коннектора
    assert e["target_point"] == [108.5, 121.0]     # [y, x]: y контента
    assert e["target_point"][0] != 100.0, \
        "конец сел на bbox, а не на letterbox-след графики"


# ---------------------------------------------------------------------------
# 3. Полигон без скина -> контур (луч вдоль lock-оси / из центроида)
# ---------------------------------------------------------------------------


def test_polygon_anchor_ray_from_centroid_hits_contour_not_bbox():
    """Без lock луч идёт из центроида в соседа и садится на КОНТУР.
    Сосед на (200, 50): луч горизонтален (центроид y=50) -> правая стенка
    хребта x=20, а не граница bbox x=100."""
    p = _cpoly("p")
    pt = node_anchor(p, 200.0, 50.0)
    assert pt == (20.0, 50.0)
    assert pt != (100.0, 50.0), "конец сел на bbox, а не на контур"
    assert point_on_polygon(p["segmentation"], *pt)


def test_polygon_anchor_ray_along_lock_axis():
    """С lock=('H', 30) луч идёт ВДОЛЬ оси прямизны (горизонталь y=30
    из-под центроида), а не из центроида в соседа."""
    p = _cpoly("p")
    pt = node_anchor(p, 200.0, 30.0, ("H", 30.0))
    assert pt == (20.0, 30.0)
    assert pt[1] == 30.0, "посадка обязана лежать на lock-оси"
    assert point_on_polygon(p["segmentation"], *pt)


def test_polygon_concave_takes_farthest_intersection():
    """Вогнутость: луч из центроида (10, 50) к (120, 5) пересекает контур
    трижды (выход из хребта, вход в верхнюю перекладину, выход из неё).
    Канон берёт ДАЛЬНЕЕ пересечение = внешняя граница контура (x=100),
    а не первое (x=20). Фиксируем как контракт project_ray_to_polygon."""
    p = _cpoly("p")
    x, y = node_anchor(p, 120.0, 5.0)
    assert x == pytest.approx(100.0)
    assert y == pytest.approx(50.0 - 45.0 * 90.0 / 110.0)   # 13.1818...
    assert point_on_polygon(p["segmentation"], x, y)


# ---------------------------------------------------------------------------
# 4. КРАСНЫЙ: баг «H раньше V» (seating.reseat_edge, чинится Э2)
# ---------------------------------------------------------------------------
# Геометрия: коннектор в 0.5px ПОД нижней гранью бокса, строго по центру X —
# связь геометрически вертикальная (dx=0). Но Y-диапазон коннектора вырожден
# ([cy, cy]) и попадает в допуск H-проверки (cy <= y2+0.75), поэтому H-замок
# ставится ПЕРВЫМ и V-ветка недостижима. Итог сегодня: конец на УГЛУ боковой
# грани [80, 140], нарисованная труба 20.006px при зазоре форм 0.5px
# (паттерн «0.00 рисуется 10.50» со стенда, §15).


def _h_before_v_case():
    b = _box("b", [100, 40, 140, 80])          # центр (120, 60)
    k = _conn("k", 80.5, 120.0)                # y=80.5=y2+0.5, x=120=центр
    g = _graph([k, b], [{"source": "k", "target": "b"}])
    reseat_all_endpoints(g)
    return g["links"][0]


def test_h_before_v_vertical_link_must_seat_on_bottom_face():
    """Бывший красный тест Э0 (xfail снят Э2): у коннектора с вырожденным
    диапазоном H-замок ставился всегда, и вертикальная связь садилась на
    боковую грань [80, 140] вместо нижней [80, 120] (нарисовано 20.0px при
    зазоре форм 0.5px — паттерн «0.00/10.50»). Теперь ось выбирается от
    реальной геометрии связи."""
    e = _h_before_v_case()
    # коннектор жёстко в центроиде — это верно и до, и после Э2
    assert e["source_point"] == [80.5, 120.0]
    # правильная посадка вертикальной связи: нижняя грань соседа, [y, x]
    assert e["target_point"] == [80.0, 120.0]


def test_h_before_v_expected_seat_reachable_with_v_lock():
    """Контроль ожидания красного теста: при ПРАВИЛЬНОМ (V) замке
    node_anchor даёт ровно ожидаемую точку (120, 80) — т.е. ожидание
    xfail-теста достижимо самим каноном, чинить надо только выбор оси."""
    b = _box("b", [100, 40, 140, 80])
    assert node_anchor(b, 120.0, 80.5, ("V", 120.0)) == (120.0, 80.0)


# ---------------------------------------------------------------------------
# 5. pretransform.reproject_edge_endpoints — вход раскладки
# ---------------------------------------------------------------------------


def test_reproject_leaves_connectors_and_plain_boxes_alone():
    """Коннекторы и узлы вне FIXED_SIZES (без полигона) не трогаются,
    даже если их конец заведомо не в каноне."""
    k = _conn("k", 200.0, 300.0)
    n = _box("n", [600, 100, 660, 160])        # plain box, не FIXED_SIZES
    e = {"source": "k", "target": "n",
         "source_point": [190.0, 290.0],       # нарочно мимо центроида
         "target_point": [90.0, 620.0]}        # нарочно мимо бокса
    g = _graph([k, n], [e])
    assert reproject_edge_endpoints(g) == 0
    assert e["source_point"] == [190.0, 290.0]
    assert e["target_point"] == [90.0, 620.0]


def test_reproject_fixed_sizes_seats_on_rect_border():
    """FIXED_SIZES-узел: конец проецируется на грань рамки скина.
    Бокс 42x21 точного аспекта: content-rect == bbox (letterbox нулевой)."""
    k = _conn("k", 200.0, 300.0)
    s = _box("s", [400, 189.5, 442, 210.5], cls="armatura_ruchn")
    e = {"source": "k", "target": "s",
         "source_point": [190.0, 290.0],
         "target_point": [150.0, 380.0]}       # выше и левее бокса
    g = _graph([k, s], [e])
    assert reproject_edge_endpoints(g) == 1
    # |dy| > |dx| от центра (421, 200) -> верхняя грань, x зажат в рамку
    assert e["target_point"] == [189.5, 400.0]
    # коннекторный конец не тронут (пусть и мимо центроида)
    assert e["source_point"] == [190.0, 290.0]


def test_reproject_polygon_seats_on_contour_farthest():
    """Полигон без скина: конец проецируется лучом центроид->старая точка
    на ДАЛЬНЕЕ пересечение с контуром (внешняя граница)."""
    p = _cpoly("g")
    k = _conn("k", 200.0, 300.0)
    e = {"source": "g", "target": "k",
         "source_point": [30.0, 60.0],         # [y, x] в выемке C
         "target_point": [222.0, 333.0]}
    g = _graph([p, k], [e])
    assert reproject_edge_endpoints(g) == 1
    # луч (10,50)->(60,30) прошивает хребет и верхнюю перекладину насквозь:
    # дальнее пересечение (100, 14) — на ПРОТИВОПОЛОЖНОЙ стороне перекладины
    assert e["source_point"][0] == pytest.approx(14.0)
    assert e["source_point"][1] == pytest.approx(100.0)
    assert e["target_point"] == [222.0, 333.0]  # коннектор не тронут


def test_reproject_overwrites_terminal_waypoint_and_path_MINE():
    """ЗАДОКУМЕНТИРОВАННОЕ ТЕКУЩЕЕ ПОВЕДЕНИЕ, не одобрение: мина под этап
    роутинга (Э7). reproject дублирует новый конец в ТЕРМИНАЛЬНУЮ точку
    path/waypoints (pretransform.py:571-575), тогда как FXML и редактор
    считают waypoints ПРОМЕЖУТОЧНЫМИ (полилиния = start + wp + end).
    Сегодня на данных рёбер с waypoints нет — мертво; но первая же правка,
    давшая рёбрам маршруты, потеряет здесь последнюю промежуточную точку.
    Починка мины сломает этот тест — тогда переписать ожидания."""
    k = _conn("k", 200.0, 300.0)
    s1 = _box("s1", [400, 189.5, 442, 210.5], cls="armatura_ruchn")
    s2 = _box("s2", [500, 300.0, 542, 321.0], cls="armatura_ruchn")
    e_wp = {"source": "k", "target": "s1",
            "source_point": [190.0, 290.0],
            "target_point": [150.0, 380.0],
            "waypoints": [[200.0, 340.0], [180.0, 360.0]]}
    e_path = {"source": "k", "target": "s2",
              "source_point": [190.0, 290.0],
              "target_point": [340.0, 560.0],
              "path": [[195.0, 400.0], [330.0, 550.0]]}
    g = _graph([k, s1, s2], [e_wp, e_path])
    assert reproject_edge_endpoints(g) == 2

    # target-конец сел на рамку...
    assert e_wp["target_point"] == [189.5, 400.0]
    # ...и ЗАТЁР последнюю промежуточную точку маршрута собой:
    assert e_wp["waypoints"] == [[200.0, 340.0], [189.5, 400.0]]

    # то же самое для ключа path (|dx|>=|dy| от центра (521,310.5) -> правая
    # грань x2=542, y зажат в рамку [300..321])
    assert e_path["target_point"] == [321.0, 542.0]
    assert e_path["path"] == [[195.0, 400.0], [321.0, 542.0]]


# ---------------------------------------------------------------------------
# 6. Граничные случаи допусков
# ---------------------------------------------------------------------------


def _straight_slack_case(dy_c):
    """Два бокса с пересекающимися Y-диапазонами рамок; центроиды смещены от
    центров рамок на |dy_c| = сумма полувысот (40) + излишек. Излишек <= 3
    (STRAIGHT_TOL) -> H-замок и строгая прямая; > 3 -> замка нет."""
    a = _box("a", [0, 0, 40, 40])
    a["centroid"] = [2.0, 20.0]                # y у верхней кромки
    b = _box("b", [100, 35, 140, 75])
    b["centroid"] = [2.0 + dy_c, 120.0]        # y у нижней кромки
    g = _graph([a, b], [{"source": "a", "target": "b"}])
    reseat_all_endpoints(g)
    e = g["links"][0]
    return e["source_point"], e["target_point"]


def test_straight_tol_3_slack_29_locks_axis():
    assert STRAIGHT_TOL == 3.0
    sp, tp = _straight_slack_case(42.9)        # излишек 2.9 — ловится
    assert sp == [35.0, 40.0]
    assert tp == [35.0, 100.0]
    assert sp[0] == tp[0], "замок обязан дать строгую горизонталь"


def test_straight_tol_3_slack_31_no_lock():
    sp, tp = _straight_slack_case(43.1)        # излишек 3.1 — не ловится
    assert sp == [40.0, 40.0]
    assert tp == [35.0, 100.0]
    assert sp[0] != tp[0], "замка нет — концы на своих гранях"
    # Замечание: «зазор между РАМКАМИ 2.9/3.1» напрямую не проверить —
    # при непересекающихся диапазонах не-коннекторов _pick_axis_coord
    # возвращает None (lo > hi) и замок не ставится вовсе; допуск 3.0
    # наблюдаем именно через слабину |Δcy| <= Σполувысот + STRAIGHT_TOL.


def test_lock_seat_window_05_on_rect():
    """node_anchor: замок садится на рамку с допуском 0.5px.
    lock на 0.4 за гранью — принят (y зажат в рамку, грань боковая);
    на 0.6 за гранью — проигнорирован (обычная посадка: нижняя грань)."""
    b = _box("b", [100, 40, 140, 80])
    assert node_anchor(b, 120.0, 80.4, ("H", 80.4)) == (140.0, 80.0)
    assert node_anchor(b, 120.0, 80.6, ("H", 80.6)) == (120.0, 80.0)


def test_pick_axis_coord_connector_tolerance_075():
    """Ось через коннектор: центроид принимается в пределах 0.75px от
    диапазона общей оси. Снаружи это окно затенено (посадочное окно 0.5
    и клампы дают тот же конец), поэтому проверяем сам выбор оси."""
    b = _box("b", [100, 40, 140, 80])
    k_in = _conn("k", 39.3, 0.0)     # 0.7 над верхней гранью: lo=40, hi=39.3
    k_out = _conn("k", 39.2, 0.0)    # 0.8 — уже мимо
    assert _pick_axis_coord(k_in, b, 40.0, 39.3, "H") == 39.3
    assert _pick_axis_coord(k_out, b, 40.0, 39.2, "H") is None


def test_seg_lock_tolerance_10():
    """Ось первого/последнего сегмента у рёбер с waypoints: допуск 1.0px,
    ось берётся ПО ОПОРНОЙ ТОЧКЕ (ref), не по старому концу."""
    assert _seg_lock((100.0, 50.0), (200.0, 50.9)) == ("H", 50.9)
    assert _seg_lock((100.0, 50.0), (200.0, 51.1)) is None
    assert _seg_lock((100.0, 50.0), (100.9, 200.0)) == ("V", 100.9)
    assert _seg_lock((100.0, 50.0), (101.1, 200.0)) is None


def test_waypoint_edge_seats_on_first_segment_axis():
    """Ребро с waypoints: конец садится на ось первого сегмента (H)."""
    a = _box("a", [20, 80, 60, 120])           # центр (40, 100)
    b = _box("b", [300, 260, 340, 300])
    e = {"source": "a", "target": "b",
         "source_point": [100.0, 50.0],        # [y, x] на старой оси
         "target_point": [280.0, 320.0],
         "waypoints": [[100.0, 200.0], [280.0, 200.0]]}
    g = _graph([a, b], [e])
    reseat_all_endpoints(g)
    # первый сегмент горизонтален (y=100) -> исток на правой грани по оси
    assert e["source_point"] == [100.0, 60.0]
    # последний сегмент горизонтален (y=280) -> сток на левой грани по оси
    assert e["target_point"] == [280.0, 300.0]
    # waypoints остались промежуточными и нетронутыми
    assert e["waypoints"] == [[100.0, 200.0], [280.0, 200.0]]


# ---------------------------------------------------------------------------
# 7. Свойство П1 (§3.5): выход перпендикулярен грани, сегментов вдоль
#    своей грани нет
# ---------------------------------------------------------------------------

_COS45 = math.cos(math.radians(45.0))


def _dist_point_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / L2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _faces_at_point(node, px, py, eps=0.01):
    """Грани формы узла, на которых лежит точка: [(имя, (dx, dy) грани)].

    Форма — та же, что у канона посадки: контур для полигона без скина,
    _anchor_rect (content-rect/bbox) для остальных. Сторож == судья."""
    seg = node.get("segmentation")
    has_poly = bool(seg) and isinstance(seg, list) and len(seg) >= 6
    if has_poly and node.get("class_name") not in FIXED_SIZES \
            and not node.get("_axis"):
        pts = [(seg[i], seg[i + 1]) for i in range(0, len(seg), 2)]
        faces = []
        for i in range(len(pts)):
            ax, ay = pts[i]
            bx, by = pts[(i + 1) % len(pts)]
            if _dist_point_seg(px, py, ax, ay, bx, by) <= 0.75:
                faces.append((f"contour[{i}]", (bx - ax, by - ay)))
        return faces
    rect = seating._anchor_rect(node)
    if rect is None:
        return []
    x1, y1, x2, y2 = rect
    faces = []
    if abs(px - x1) <= eps and y1 - eps <= py <= y2 + eps:
        faces.append(("left", (0.0, 1.0)))
    if abs(px - x2) <= eps and y1 - eps <= py <= y2 + eps:
        faces.append(("right", (0.0, 1.0)))
    if abs(py - y1) <= eps and x1 - eps <= px <= x2 + eps:
        faces.append(("top", (1.0, 0.0)))
    if abs(py - y2) <= eps and x1 - eps <= px <= x2 + eps:
        faces.append(("bottom", (1.0, 0.0)))
    return faces


def _p1_violations(g):
    """Нарушения П1: посаженный конец, чей последний сегмент ПАРАЛЛЕЛЕН
    (угол < 45°) грани, на которой сидит. Возвращает список описаний.

    Вторая половина П1 (standoff: первый излом не ближе N px от грани)
    здесь не проверяется: посадка сама изломов не рождает, на синтетике
    без waypoints условие пусто. Появится с этапом роутинга (Э7)."""
    byid = {n["id"]: n for n in g["nodes"]}
    out = []
    for e in g["links"]:
        sp, tp = e["source_point"], e["target_point"]
        wps = e.get("waypoints") or []
        # полилиния в (x, y); sp/tp/waypoints хранятся [y, x]
        pl = [(p[1], p[0]) for p in [sp] + list(wps) + [tp]]
        for nid, end_pt, nbr_pt in ((e["source"], pl[0], pl[1]),
                                    (e["target"], pl[-1], pl[-2])):
            node = byid[nid]
            if is_connector(node):
                continue                        # у точки граней нет
            vx, vy = nbr_pt[0] - end_pt[0], nbr_pt[1] - end_pt[1]
            vlen = math.hypot(vx, vy)
            if vlen < 1e-9:
                continue
            for fname, (fx, fy) in _faces_at_point(node, *end_pt):
                flen = math.hypot(fx, fy)
                cosang = abs(vx * fx + vy * fy) / (vlen * flen)
                if cosang > _COS45:
                    ang = math.degrees(math.acos(min(cosang, 1.0)))
                    out.append(
                        f"{e['source']}->{e['target']} у {nid}: сегмент "
                        f"{vlen:.1f}px под {ang:.1f}° к своей грани {fname}")
    return out


def _clean_synthetic():
    """Детерминированная синтетика: H-цепочка через коннектор, V-цепочка,
    letterbox-скин, полигон на lock-оси, пара со слабиной в допуске."""
    nodes = [
        _box("A", [100, 180, 140, 220]), _conn("C1", 200.0, 300.0),
        _box("B", [400, 180, 440, 220]),
        _box("D", [580, 100, 620, 140]), _conn("C2", 200.0, 600.0),
        _box("E", [580, 260, 620, 300]),
        _box("S", [660, 181, 702, 219], cls="armatura_ruchn"),
        _conn("C3", 200.0, 760.0),
        _cpoly("P", offx=800.0, offy=300.0), _conn("C4", 330.0, 1000.0),
        _box("F", [100, 500, 140, 540]), _box("G", [400, 502, 440, 542]),
    ]
    links = [
        {"source": "A", "target": "C1"}, {"source": "C1", "target": "B"},
        {"source": "D", "target": "C2"}, {"source": "C2", "target": "E"},
        {"source": "S", "target": "C3"}, {"source": "P", "target": "C4"},
        {"source": "F", "target": "G"},
    ]
    return _graph(nodes, links)


def test_p1_perpendicular_exit_on_clean_synthetic():
    """На синтетике без вырожденных пар канон держит П1: каждый посаженный
    конец выходит перпендикулярно своей грани (допуск 45°)."""
    g = _clean_synthetic()
    reseat_all_endpoints(g)
    viol = _p1_violations(g)
    assert viol == [], "П1 нарушено:\n" + "\n".join(viol)


def test_p1_perpendicular_exit_with_degenerate_pair():
    """Та же синтетика + вырожденная пара из теста 4: свойство П1 держится
    и на ней (бывший xfail Э0 — до Э2 коннектор в 0.5px под гранью соседа
    давал сегмент 20.0px под 1.4° к нижней грани, вдоль неё)."""
    g = _clean_synthetic()
    g["nodes"] += [_box("H", [1100, 40, 1140, 80]),
                   _conn("C5", 80.5, 1120.0)]
    g["links"].append({"source": "C5", "target": "H"})
    reseat_all_endpoints(g)
    viol = _p1_violations(g)
    assert viol == [], "П1 нарушено:\n" + "\n".join(viol)

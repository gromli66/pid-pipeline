"""
Graph Geometry Helpers — перпендикулярное соединение узлов P&ID.

Извлечено из pipeline_prototype.py.
Чистая геометрия без Qt-зависимостей.
"""

import math
from typing import Tuple, List


# =====================================================================
# GEOMETRY HELPERS: Перпендикулярное соединение узлов
# =====================================================================

def _segments_overlap_1d(a1: float, a2: float, b1: float, b2: float) -> Tuple[bool, float, float]:
    """Проверить перекрытие двух отрезков на одной оси."""
    start = max(min(a1, a2), min(b1, b2))
    end = min(max(a1, a2), max(b1, b2))
    if start <= end:
        return True, start, end
    return False, 0, 0


def _point_to_segment_closest(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> Tuple[float, float, float]:
    """Ближайшая точка на отрезке ab к точке p. Returns: (x, y, distance)"""
    dx, dy = bx - ax, by - ay
    length_sq = dx*dx + dy*dy
    if length_sq < 1e-9:
        return ax, ay, math.sqrt((px-ax)**2 + (py-ay)**2)
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return cx, cy, math.sqrt((px - cx)**2 + (py - cy)**2)


def _edge_normal(p1: Tuple[float, float], p2: Tuple[float, float]) -> Tuple[float, float]:
    """Нормаль к ребру (единичный вектор)."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = math.sqrt(dx*dx + dy*dy)
    if length < 1e-9:
        return (0, 0)
    return (-dy / length, dx / length)


def _polygon_to_edges(polygon: List[float]) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Конвертировать плоский список [x1,y1,x2,y2,...] в список рёбер."""
    n = len(polygon) // 2
    edges = []
    for i in range(n):
        x1, y1 = polygon[i * 2], polygon[i * 2 + 1]
        x2, y2 = polygon[((i + 1) % n) * 2], polygon[((i + 1) % n) * 2 + 1]
        edges.append(((x1, y1), (x2, y2)))
    return edges


def _project_to_boundary_axis(cx, cy, target_coord, edges, axis):
    """
    Проецирует точку (cx, cy) на ближайшее ребро полигона вдоль оси.

    Для axis='vertical': ищет пересечение x=cx с рёбрами, выбирает ближайшее к target_coord по y.
    Для axis='horizontal': ищет пересечение y=cy с рёбрами, выбирает ближайшее к target_coord по x.

    Если пересечений нет — возвращает (cx, cy) как fallback.
    """
    hits = []
    if axis == 'vertical':
        for p1, p2 in edges:
            if min(p1[0], p2[0]) <= cx <= max(p1[0], p2[0]):
                if abs(p2[0] - p1[0]) > 1e-9:
                    t = (cx - p1[0]) / (p2[0] - p1[0])
                    if 0 <= t <= 1:
                        py = p1[1] + t * (p2[1] - p1[1])
                        hits.append((cx, py))
        if hits:
            # Pick the hit closest to target_coord (y)
            return min(hits, key=lambda h: abs(h[1] - target_coord))
    else:  # horizontal
        for p1, p2 in edges:
            if min(p1[1], p2[1]) <= cy <= max(p1[1], p2[1]):
                if abs(p2[1] - p1[1]) > 1e-9:
                    t = (cy - p1[1]) / (p2[1] - p1[1])
                    if 0 <= t <= 1:
                        px = p1[0] + t * (p2[0] - p1[0])
                        hits.append((px, cy))
        if hits:
            # Pick the hit closest to target_coord (x)
            return min(hits, key=lambda h: abs(h[0] - target_coord))
    return (cx, cy)


def _closest_points_between_segments(a1, a2, b1, b2) -> Tuple[Tuple[float, float], Tuple[float, float], float]:
    """Найти ближайшие точки между двумя отрезками."""
    candidates = []
    for p in [a1, a2]:
        cx, cy, d = _point_to_segment_closest(p[0], p[1], b1[0], b1[1], b2[0], b2[1])
        candidates.append((p, (cx, cy), d))
    for p in [b1, b2]:
        cx, cy, d = _point_to_segment_closest(p[0], p[1], a1[0], a1[1], a2[0], a2[1])
        candidates.append(((cx, cy), p, d))
    return min(candidates, key=lambda x: x[2])


def connect_bbox_bbox(bbox_a: List[float], bbox_b: List[float], required_axis: str = None) -> Tuple[Tuple[float, float], Tuple[float, float], str]:
    """
    Соединить два bbox. Приоритет: строго перпендикулярно (вертикаль/горизонталь).

    Args:
        bbox_a, bbox_b: координаты боксов [x1, y1, x2, y2]
        required_axis: 'vertical'/'horizontal'/None — если задано, искать только эту ось

    Логика приоритетов:
    1. Перпендикуляр из центра A попадает на стенку B
    2. Перпендикуляр из центра B попадает на стенку A
    3. Перпендикуляр через overlap (не через центр)
    4. Наименее диагональное соединение стенка-стенка

    Returns: (point_a, point_b, connection_type)
    """
    ax1, ay1, ax2, ay2 = bbox_a
    bx1, by1, bx2, by2 = bbox_b
    acx, acy = (ax1 + ax2) / 2, (ay1 + ay2) / 2
    bcx, bcy = (bx1 + bx2) / 2, (by1 + by2) / 2

    candidates = []  # [(point_a, point_b, distance, priority, connection_type)]

    # === ПРИОРИТЕТ 1: Перпендикуляр из центра A ===

    # Вертикаль x = acx: попадает на верхнюю/нижнюю стенку B?
    if (required_axis is None or required_axis == 'vertical') and bx1 <= acx <= bx2:
        if acy < by1:  # A выше B
            pa = (acx, ay2)  # нижняя стенка A
            pb = (acx, by1)  # верхняя стенка B
            dist = by1 - ay2
            if dist >= 0:
                candidates.append((pa, pb, dist, 1, "vertical_center_a"))
        elif acy > by2:  # A ниже B
            pa = (acx, ay1)  # верхняя стенка A
            pb = (acx, by2)  # нижняя стенка B
            dist = ay1 - by2
            if dist >= 0:
                candidates.append((pa, pb, dist, 1, "vertical_center_a"))

    # Горизонталь y = acy: попадает на левую/правую стенку B?
    if (required_axis is None or required_axis == 'horizontal') and by1 <= acy <= by2:
        if acx < bx1:  # A левее B
            pa = (ax2, acy)  # правая стенка A
            pb = (bx1, acy)  # левая стенка B
            dist = bx1 - ax2
            if dist >= 0:
                candidates.append((pa, pb, dist, 1, "horizontal_center_a"))
        elif acx > bx2:  # A правее B
            pa = (ax1, acy)  # левая стенка A
            pb = (bx2, acy)  # правая стенка B
            dist = ax1 - bx2
            if dist >= 0:
                candidates.append((pa, pb, dist, 1, "horizontal_center_a"))

    # === ПРИОРИТЕТ 2: Перпендикуляр из центра B ===

    # Вертикаль x = bcx: попадает на верхнюю/нижнюю стенку A?
    if (required_axis is None or required_axis == 'vertical') and ax1 <= bcx <= ax2:
        if bcy < ay1:  # B выше A
            pb = (bcx, by2)  # нижняя стенка B
            pa = (bcx, ay1)  # верхняя стенка A
            dist = ay1 - by2
            if dist >= 0:
                candidates.append((pa, pb, dist, 2, "vertical_center_b"))
        elif bcy > ay2:  # B ниже A
            pb = (bcx, by1)  # верхняя стенка B
            pa = (bcx, ay2)  # нижняя стенка A
            dist = by1 - ay2
            if dist >= 0:
                candidates.append((pa, pb, dist, 2, "vertical_center_b"))

    # Горизонталь y = bcy: попадает на левую/правую стенку A?
    if (required_axis is None or required_axis == 'horizontal') and ay1 <= bcy <= ay2:
        if bcx < ax1:  # B левее A
            pb = (bx2, bcy)  # правая стенка B
            pa = (ax1, bcy)  # левая стенка A
            dist = ax1 - bx2
            if dist >= 0:
                candidates.append((pa, pb, dist, 2, "horizontal_center_b"))
        elif bcx > ax2:  # B правее A
            pb = (bx1, bcy)  # левая стенка B
            pa = (ax2, bcy)  # правая стенка A
            dist = bx2 - ax1
            if dist >= 0:
                candidates.append((pa, pb, dist, 2, "horizontal_center_b"))

    # === ПРИОРИТЕТ 3: Перпендикуляр через overlap (не через центр) ===

    overlap_x, ox_start, ox_end = _segments_overlap_1d(ax1, ax2, bx1, bx2)
    overlap_y, oy_start, oy_end = _segments_overlap_1d(ay1, ay2, by1, by2)

    if (required_axis is None or required_axis == 'vertical') and overlap_x and not overlap_y:
        # Вертикальная линия через середину overlap
        x = (ox_start + ox_end) / 2
        if acy < bcy:
            pa = (x, ay2)
            pb = (x, by1)
            dist = by1 - ay2
        else:
            pa = (x, ay1)
            pb = (x, by2)
            dist = ay1 - by2
        if dist >= 0:
            candidates.append((pa, pb, abs(dist), 3, "vertical_overlap"))

    if (required_axis is None or required_axis == 'horizontal') and overlap_y and not overlap_x:
        # Горизонтальная линия через середину overlap
        y = (oy_start + oy_end) / 2
        if acx < bcx:
            pa = (ax2, y)
            pb = (bx1, y)
            dist = bx1 - ax2
        else:
            pa = (ax1, y)
            pb = (bx2, y)
            dist = ax1 - bx2
        if dist >= 0:
            candidates.append((pa, pb, abs(dist), 3, "horizontal_overlap"))

    # Если overlap по обеим осям — боксы пересекаются
    if overlap_x and overlap_y:
        return ((acx, acy), (bcx, bcy), "overlapping")

    # === Выбираем лучшего кандидата из приоритетов 1-3 ===
    if candidates:
        # Сортируем: сначала по приоритету, потом по расстоянию
        candidates.sort(key=lambda c: (c[3], c[2]))
        best = candidates[0]
        return (best[0], best[1], best[4])

    # === ПРИОРИТЕТ 4: Наименее диагональное стенка-стенка ===
    sides_a = [
        ((ax1, ay1), (ax2, ay1), 'top'),     # верх
        ((ax1, ay2), (ax2, ay2), 'bottom'),  # низ
        ((ax1, ay1), (ax1, ay2), 'left'),    # лево
        ((ax2, ay1), (ax2, ay2), 'right'),   # право
    ]
    sides_b = [
        ((bx1, by1), (bx2, by1), 'top'),
        ((bx1, by2), (bx2, by2), 'bottom'),
        ((bx1, by1), (bx1, by2), 'left'),
        ((bx2, by1), (bx2, by2), 'right'),
    ]

    best_score = -1.0
    best_dist = float('inf')
    best_pa, best_pb = (acx, acy), (bcx, bcy)
    best_type = "diagonal"

    for (a1, a2, a_side) in sides_a:
        for (b1, b2, b_side) in sides_b:
            pa, pb, dist = _closest_points_between_segments(a1, a2, b1, b2)
            dx, dy = pb[0] - pa[0], pb[1] - pa[1]
            score, axis = global_axis_perpendicularity(dx, dy)

            # Если задана required_axis, учитываем только совпадающие
            if required_axis is not None and axis != required_axis:
                continue

            # Приоритет: перпендикулярность, при равной — меньшее расстояние
            if score > best_score or (abs(score - best_score) < 1e-9 and dist < best_dist):
                best_score = score
                best_pa, best_pb = pa, pb
                best_dist = dist
                best_type = f"diagonal_{axis}"

    return (best_pa, best_pb, best_type)


def connect_bbox_polygon(bbox: List[float], polygon: List[float], required_axis: str = None) -> Tuple[Tuple[float, float], Tuple[float, float], dict]:
    """
    Соединить bbox с полигоном. Приоритет: глобальная перпендикулярность (к осям).

    Args:
        bbox: координаты бокса [x1, y1, x2, y2]
        polygon: плоский список координат полигона [x1,y1,x2,y2,...]
        required_axis: 'vertical'/'horizontal'/None — если задано, искать только эту ось

    Логика приоритетов:
    1. Перпендикуляр из центра bbox попадает на ребро полигона
    2. Перпендикуляр из центра полигона попадает на стенку bbox
    3. Наименее диагональное соединение стенка-ребро

    Returns: (point_on_bbox, point_on_polygon, info)
    """
    bx1, by1, bx2, by2 = bbox
    bcx, bcy = (bx1 + bx2) / 2, (by1 + by2) / 2

    edges = _polygon_to_edges(polygon)
    n_verts = len(polygon) // 2
    poly_cx = sum(polygon[::2]) / n_verts
    poly_cy = sum(polygon[1::2]) / n_verts

    candidates = []  # [(bbox_point, poly_point, distance, priority, info)]

    # === ПРИОРИТЕТ 1: Перпендикуляр из центра bbox ===

    # Вертикаль x = bcx
    if required_axis is None or required_axis == 'vertical':
        for p1, p2 in edges:
            if min(p1[0], p2[0]) <= bcx <= max(p1[0], p2[0]):
                if abs(p2[0] - p1[0]) > 1e-9:
                    t = (bcx - p1[0]) / (p2[0] - p1[0])
                    if 0 <= t <= 1:
                        py = p1[1] + t * (p2[1] - p1[1])
                        poly_point = (bcx, py)

                        if py < by1:
                            bbox_point = (bcx, by1)
                            dist = by1 - py
                        elif py > by2:
                            bbox_point = (bcx, by2)
                            dist = py - by2
                        else:
                            continue

                        if dist >= 0:
                            candidates.append((bbox_point, poly_point, dist, 1,
                                              {'perpendicularity': 1.0, 'axis': 'vertical'}))

    # Горизонталь y = bcy
    if required_axis is None or required_axis == 'horizontal':
        for p1, p2 in edges:
            if min(p1[1], p2[1]) <= bcy <= max(p1[1], p2[1]):
                if abs(p2[1] - p1[1]) > 1e-9:
                    t = (bcy - p1[1]) / (p2[1] - p1[1])
                    if 0 <= t <= 1:
                        px = p1[0] + t * (p2[0] - p1[0])
                        poly_point = (px, bcy)

                        if px < bx1:
                            bbox_point = (bx1, bcy)
                            dist = bx1 - px
                        elif px > bx2:
                            bbox_point = (bx2, bcy)
                            dist = px - bx2
                        else:
                            continue

                        if dist >= 0:
                            candidates.append((bbox_point, poly_point, dist, 1,
                                              {'perpendicularity': 1.0, 'axis': 'horizontal'}))

    # === ПРИОРИТЕТ 2: Перпендикуляр из центра полигона ===

    # Вертикаль x = poly_cx попадает на bbox?
    if (required_axis is None or required_axis == 'vertical') and bx1 <= poly_cx <= bx2:
        if poly_cy < by1:
            poly_point = (poly_cx, poly_cy)
            bbox_point = (poly_cx, by1)
            dist = by1 - poly_cy
            candidates.append((bbox_point, poly_point, dist, 2,
                              {'perpendicularity': 1.0, 'axis': 'vertical'}))
        elif poly_cy > by2:
            poly_point = (poly_cx, poly_cy)
            bbox_point = (poly_cx, by2)
            dist = poly_cy - by2
            candidates.append((bbox_point, poly_point, dist, 2,
                              {'perpendicularity': 1.0, 'axis': 'vertical'}))

    # Горизонталь y = poly_cy попадает на bbox?
    if (required_axis is None or required_axis == 'horizontal') and by1 <= poly_cy <= by2:
        if poly_cx < bx1:
            poly_point = (poly_cx, poly_cy)
            bbox_point = (bx1, poly_cy)
            dist = bx1 - poly_cx
            candidates.append((bbox_point, poly_point, dist, 2,
                              {'perpendicularity': 1.0, 'axis': 'horizontal'}))
        elif poly_cx > bx2:
            poly_point = (poly_cx, poly_cy)
            bbox_point = (bx2, poly_cy)
            dist = poly_cx - bx2
            candidates.append((bbox_point, poly_point, dist, 2,
                              {'perpendicularity': 1.0, 'axis': 'horizontal'}))

    # === Выбираем лучшего кандидата из приоритетов 1-2 ===
    if candidates:
        candidates.sort(key=lambda c: (c[3], c[2]))
        best = candidates[0]
        return (best[0], best[1], best[4])

    # === ПРИОРИТЕТ 3: Наименее диагональное стенка-ребро ===
    sides_bbox = [
        ((bx1, by1), (bx2, by1)),  # верх
        ((bx1, by2), (bx2, by2)),  # низ
        ((bx1, by1), (bx1, by2)),  # лево
        ((bx2, by1), (bx2, by2)),  # право
    ]

    best_score = -1.0
    best_result = None
    best_dist = float('inf')

    for b1, b2 in sides_bbox:
        for p1, p2 in edges:
            pa, pb, dist = _closest_points_between_segments(b1, b2, p1, p2)
            dx, dy = pb[0] - pa[0], pb[1] - pa[1]
            score, axis = global_axis_perpendicularity(dx, dy)

            # Если задана required_axis, учитываем только совпадающие
            if required_axis is not None and axis != required_axis:
                continue

            if score > best_score or (abs(score - best_score) < 1e-9 and dist < best_dist):
                best_score = score
                best_dist = dist
                best_result = {
                    'bbox_point': pa,
                    'poly_point': pb,
                    'perpendicularity': score,
                    'axis': axis
                }

    if best_result:
        return (best_result['bbox_point'], best_result['poly_point'], best_result)

    # Fallback
    return ((bcx, bcy), (poly_cx, poly_cy), {'fallback': True})


def _closest_point_on_bbox_to_direction(bbox: List[float], from_point: Tuple[float, float],
                                         direction: Tuple[float, float]) -> Tuple[float, float]:
    """Найти точку на bbox на луче из from_point в направлении direction."""
    x1, y1, x2, y2 = bbox
    dx, dy = direction
    length = math.sqrt(dx*dx + dy*dy)
    if length < 1e-9:
        return ((x1+x2)/2, (y1+y2)/2)
    dx, dy = dx / length, dy / length

    sides = [((x1, y1), (x1, y2)), ((x2, y1), (x2, y2)),
             ((x1, y1), (x2, y1)), ((x1, y2), (x2, y2))]

    best_t = float('inf')
    best_point = ((x1+x2)/2, (y1+y2)/2)

    for (sx1, sy1), (sx2, sy2) in sides:
        sdx, sdy = sx2 - sx1, sy2 - sy1
        denom = dx * sdy - dy * sdx
        if abs(denom) < 1e-9:
            continue
        t = ((sx1 - from_point[0]) * sdy - (sy1 - from_point[1]) * sdx) / denom
        s = ((sx1 - from_point[0]) * dy - (sy1 - from_point[1]) * dx) / denom
        if t > 0 and 0 <= s <= 1 and t < best_t:
            best_t = t
            best_point = (from_point[0] + t * dx, from_point[1] + t * dy)

    return best_point


def connect_polygon_polygon(polygon_a: List[float], polygon_b: List[float], required_axis: str = None) -> Tuple[Tuple[float, float], Tuple[float, float], dict]:
    """
    Соединить два полигона. Приоритет: глобальная перпендикулярность (к осям).

    Args:
        polygon_a, polygon_b: плоские списки координат [x1,y1,x2,y2,...]
        required_axis: 'vertical'/'horizontal'/None — если задано, искать только эту ось

    Логика приоритетов:
    1. Перпендикуляр из центра A попадает на ребро B
    2. Перпендикуляр из центра B попадает на ребро A
    3. Наименее диагональное соединение ребро-ребро

    Returns: (point_a, point_b, info)
    """
    edges_a = _polygon_to_edges(polygon_a)
    edges_b = _polygon_to_edges(polygon_b)

    n_a = len(polygon_a) // 2
    n_b = len(polygon_b) // 2
    ca_x = sum(polygon_a[::2]) / n_a
    ca_y = sum(polygon_a[1::2]) / n_a
    cb_x = sum(polygon_b[::2]) / n_b
    cb_y = sum(polygon_b[1::2]) / n_b

    candidates = []  # [(point_a, point_b, distance, priority, info)]

    # === ПРИОРИТЕТ 1: Перпендикуляр из центра A ===

    # Вертикаль x = ca_x
    if required_axis is None or required_axis == 'vertical':
        for p1, p2 in edges_b:
            if min(p1[0], p2[0]) <= ca_x <= max(p1[0], p2[0]):
                if abs(p2[0] - p1[0]) > 1e-9:
                    t = (ca_x - p1[0]) / (p2[0] - p1[0])
                    if 0 <= t <= 1:
                        py = p1[1] + t * (p2[1] - p1[1])
                        point_b = (ca_x, py)
                        # Project point_a onto boundary of polygon_a (x=ca_x)
                        point_a = _project_to_boundary_axis(ca_x, ca_y, py, edges_a, 'vertical')
                        dist = abs(point_a[1] - point_b[1])
                        candidates.append((point_a, point_b, dist, 1,
                                          {'perpendicularity': 1.0, 'axis': 'vertical'}))

    # Горизонталь y = ca_y
    if required_axis is None or required_axis == 'horizontal':
        for p1, p2 in edges_b:
            if min(p1[1], p2[1]) <= ca_y <= max(p1[1], p2[1]):
                if abs(p2[1] - p1[1]) > 1e-9:
                    t = (ca_y - p1[1]) / (p2[1] - p1[1])
                    if 0 <= t <= 1:
                        px = p1[0] + t * (p2[0] - p1[0])
                        point_b = (px, ca_y)
                        # Project point_a onto boundary of polygon_a (y=ca_y)
                        point_a = _project_to_boundary_axis(ca_x, ca_y, px, edges_a, 'horizontal')
                        dist = abs(point_a[0] - point_b[0])
                        candidates.append((point_a, point_b, dist, 1,
                                          {'perpendicularity': 1.0, 'axis': 'horizontal'}))

    # === ПРИОРИТЕТ 2: Перпендикуляр из центра B ===

    # Вертикаль x = cb_x
    if required_axis is None or required_axis == 'vertical':
        for p1, p2 in edges_a:
            if min(p1[0], p2[0]) <= cb_x <= max(p1[0], p2[0]):
                if abs(p2[0] - p1[0]) > 1e-9:
                    t = (cb_x - p1[0]) / (p2[0] - p1[0])
                    if 0 <= t <= 1:
                        py = p1[1] + t * (p2[1] - p1[1])
                        point_a = (cb_x, py)
                        # Project point_b onto boundary of polygon_b (x=cb_x)
                        point_b = _project_to_boundary_axis(cb_x, cb_y, py, edges_b, 'vertical')
                        dist = abs(point_a[1] - point_b[1])
                        candidates.append((point_a, point_b, dist, 2,
                                          {'perpendicularity': 1.0, 'axis': 'vertical'}))

    # Горизонталь y = cb_y
    if required_axis is None or required_axis == 'horizontal':
        for p1, p2 in edges_a:
            if min(p1[1], p2[1]) <= cb_y <= max(p1[1], p2[1]):
                if abs(p2[1] - p1[1]) > 1e-9:
                    t = (cb_y - p1[1]) / (p2[1] - p1[1])
                    if 0 <= t <= 1:
                        px = p1[0] + t * (p2[0] - p1[0])
                        point_a = (px, cb_y)
                        # Project point_b onto boundary of polygon_b (y=cb_y)
                        point_b = _project_to_boundary_axis(cb_x, cb_y, px, edges_b, 'horizontal')
                        dist = abs(point_a[0] - point_b[0])
                        candidates.append((point_a, point_b, dist, 2,
                                          {'perpendicularity': 1.0, 'axis': 'horizontal'}))

    # === Выбираем лучшего кандидата из приоритетов 1-2 ===
    if candidates:
        candidates.sort(key=lambda c: (c[3], c[2]))
        best = candidates[0]
        return (best[0], best[1], best[4])

    # === ПРИОРИТЕТ 3: Наименее диагональное ребро-ребро ===
    best_score = -1.0
    best_result = None
    best_dist = float('inf')

    for a1, a2 in edges_a:
        for b1, b2 in edges_b:
            pa, pb, dist = _closest_points_between_segments(a1, a2, b1, b2)
            if dist < 1e-9:
                continue

            dx, dy = pb[0] - pa[0], pb[1] - pa[1]
            score, axis = global_axis_perpendicularity(dx, dy)

            # Если задана required_axis, учитываем только совпадающие
            if required_axis is not None and axis != required_axis:
                continue

            if score > best_score or (abs(score - best_score) < 1e-9 and dist < best_dist):
                best_score = score
                best_dist = dist
                best_result = {
                    'point_a': pa,
                    'point_b': pb,
                    'perpendicularity': score,
                    'axis': axis
                }

    if best_result:
        return (best_result['point_a'], best_result['point_b'], best_result)

    # Fallback
    return ((ca_x, ca_y), (cb_x, cb_y), {'fallback': True})


def connect_point_bbox(point: Tuple[float, float], bbox: List[float], required_axis: str = None) -> Tuple[Tuple[float, float], Tuple[float, float], str]:
    """
    Соединить точку (коннектор) с bbox.
    Приоритет: строгий перпендикуляр (вертикаль/горизонталь) от точки к стенке.

    Args:
        point: координаты точки (x, y)
        bbox: координаты бокса [x1, y1, x2, y2]
        required_axis: 'vertical'/'horizontal'/None — если задано, искать только эту ось

    Логика приоритетов:
    1. Вертикаль x = px попадает на горизонтальную стенку bbox
    2. Горизонталь y = py попадает на вертикальную стенку bbox
    3. Наименее диагональное соединение

    Returns: (point, point_on_bbox, connection_type)
    """
    px, py = point
    x1, y1, x2, y2 = bbox

    candidates = []  # [(point, bbox_point, distance, priority, connection_type)]

    # === ПРИОРИТЕТ 1: Вертикаль x = px ===
    if (required_axis is None or required_axis == 'vertical') and x1 <= px <= x2:
        if py < y1:
            bbox_point = (px, y1)
            dist = y1 - py
            candidates.append((point, bbox_point, dist, 1, "vertical_to_top"))
        elif py > y2:
            bbox_point = (px, y2)
            dist = py - y2
            candidates.append((point, bbox_point, dist, 1, "vertical_to_bottom"))

    # === ПРИОРИТЕТ 2: Горизонталь y = py ===
    if (required_axis is None or required_axis == 'horizontal') and y1 <= py <= y2:
        if px < x1:
            bbox_point = (x1, py)
            dist = x1 - px
            candidates.append((point, bbox_point, dist, 2, "horizontal_to_left"))
        elif px > x2:
            bbox_point = (x2, py)
            dist = px - x2
            candidates.append((point, bbox_point, dist, 2, "horizontal_to_right"))

    # === Выбираем лучшего кандидата из приоритетов 1-2 ===
    if candidates:
        candidates.sort(key=lambda c: (c[3], c[2]))
        best = candidates[0]
        return (best[0], best[1], best[4])

    # === ПРИОРИТЕТ 3: Наименее диагональное ===
    sides = [
        ((x1, y1), (x2, y1), 'top'),
        ((x1, y2), (x2, y2), 'bottom'),
        ((x1, y1), (x1, y2), 'left'),
        ((x2, y1), (x2, y2), 'right'),
    ]

    best_score = -1.0
    best_dist = float('inf')
    best_point = ((x1 + x2) / 2, (y1 + y2) / 2)
    best_type = "diagonal"

    for p1, p2, side_name in sides:
        cx, cy, dist = _point_to_segment_closest(px, py, p1[0], p1[1], p2[0], p2[1])
        dx, dy = cx - px, cy - py
        score, axis = global_axis_perpendicularity(dx, dy)

        # Если задана required_axis, учитываем только совпадающие
        if required_axis is not None and axis != required_axis:
            continue

        if score > best_score or (abs(score - best_score) < 1e-9 and dist < best_dist):
            best_score = score
            best_dist = dist
            best_point = (cx, cy)
            best_type = f"diagonal_{axis}_to_{side_name}"

    return (point, best_point, best_type)


def connect_point_polygon(point: Tuple[float, float], polygon: List[float], required_axis: str = None) -> Tuple[Tuple[float, float], Tuple[float, float], dict]:
    """
    Соединить точку (коннектор) с полигоном.
    Приоритет: строгий перпендикуляр (вертикаль/горизонталь) от точки к ребру.

    Args:
        point: координаты точки (x, y)
        polygon: плоский список координат [x1,y1,x2,y2,...]
        required_axis: 'vertical'/'horizontal'/None — если задано, искать только эту ось

    Логика приоритетов:
    1. Вертикаль x = px пересекает ребро полигона
    2. Горизонталь y = py пересекает ребро полигона
    3. Наименее диагональное соединение

    Returns: (point, point_on_polygon, info)
    """
    px, py = point
    n = len(polygon) // 2
    edges = _polygon_to_edges(polygon)

    candidates = []  # [(point, poly_point, distance, priority, info)]

    # === ПРИОРИТЕТ 1: Вертикаль x = px ===
    if required_axis is None or required_axis == 'vertical':
        for i, (p1, p2) in enumerate(edges):
            if min(p1[0], p2[0]) <= px <= max(p1[0], p2[0]):
                if abs(p2[0] - p1[0]) > 1e-9:
                    t = (px - p1[0]) / (p2[0] - p1[0])
                    if 0 <= t <= 1:
                        poly_y = p1[1] + t * (p2[1] - p1[1])
                        poly_point = (px, poly_y)
                        dist = abs(poly_y - py)
                        candidates.append((point, poly_point, dist, 1,
                                          {'perpendicularity': 1.0, 'axis': 'vertical', 'edge_idx': i}))

    # === ПРИОРИТЕТ 2: Горизонталь y = py ===
    if required_axis is None or required_axis == 'horizontal':
        for i, (p1, p2) in enumerate(edges):
            if min(p1[1], p2[1]) <= py <= max(p1[1], p2[1]):
                if abs(p2[1] - p1[1]) > 1e-9:
                    t = (py - p1[1]) / (p2[1] - p1[1])
                    if 0 <= t <= 1:
                        poly_x = p1[0] + t * (p2[0] - p1[0])
                        poly_point = (poly_x, py)
                        dist = abs(poly_x - px)
                        candidates.append((point, poly_point, dist, 2,
                                          {'perpendicularity': 1.0, 'axis': 'horizontal', 'edge_idx': i}))

    # === Выбираем лучшего кандидата из приоритетов 1-2 ===
    if candidates:
        candidates.sort(key=lambda c: (c[3], c[2]))
        best = candidates[0]
        return (best[0], best[1], best[4])

    # === ПРИОРИТЕТ 3: Наименее диагональное ===
    best_score = -1.0
    best_dist = float('inf')
    best_result = None

    for i, (p1, p2) in enumerate(edges):
        cx, cy, dist = _point_to_segment_closest(px, py, p1[0], p1[1], p2[0], p2[1])
        if dist < 1e-9:
            continue

        dx, dy = cx - px, cy - py
        score, axis = global_axis_perpendicularity(dx, dy)

        # Если задана required_axis, учитываем только совпадающие
        if required_axis is not None and axis != required_axis:
            continue

        if score > best_score or (abs(score - best_score) < 1e-9 and dist < best_dist):
            best_score = score
            best_dist = dist
            best_result = {
                'poly_point': (cx, cy),
                'edge_idx': i,
                'perpendicularity': score,
                'axis': axis,
            }

    if best_result:
        return (point, best_result['poly_point'], best_result)

    # Fallback: центроид полигона
    poly_cx = sum(polygon[::2]) / n
    poly_cy = sum(polygon[1::2]) / n
    return (point, (poly_cx, poly_cy), {'fallback': True})


# =====================================================================
# EDGE PERPENDICULARITY ANALYSIS
# =====================================================================

# Порог "хорошей" перпендикулярности: 1° от оси
# score = 1 - sin(угол), для 1°: 1 - sin(1°) ≈ 0.983
PERPENDICULARITY_THRESHOLD = 1.0


def global_axis_perpendicularity(dx: float, dy: float) -> Tuple[float, str]:
    """
    Вычислить глобальную перпендикулярность — близость к осям координат.

    Args:
        dx, dy: направление (не обязательно нормализованное)

    Returns:
        (score, axis): score 0-1, axis = 'horizontal'/'vertical'/'diagonal'
    """
    length = math.sqrt(dx * dx + dy * dy)
    if length < 1e-9:
        return 1.0, 'point'

    dx_norm = abs(dx / length)
    dy_norm = abs(dy / length)

    # Близость к горизонтали: |dy| → 0
    horiz_score = 1.0 - dy_norm
    # Близость к вертикали: |dx| → 0
    vert_score = 1.0 - dx_norm

    if horiz_score >= vert_score:
        return horiz_score, 'horizontal'
    else:
        return vert_score, 'vertical'


def compute_edge_perpendicularity(
    edge_start: Tuple[float, float],
    edge_end: Tuple[float, float],
    source_geometry: dict,
    target_geometry: dict
) -> dict:
    """
    Вычислить перпендикулярность ребра — близость к глобальным осям (вертикаль/горизонталь).

    Args:
        edge_start: (x, y) начало ребра
        edge_end: (x, y) конец ребра
        source_geometry: {'type': 'bbox'/'polygon'/'point', 'data': ...} (не используется в новой логике)
        target_geometry: аналогично (не используется в новой логике)

    Returns:
        {
            'score': float 0-1 (близость к оси),
            'axis': 'horizontal'/'vertical'/'point',
            'angle': float (градусы отклонения от оси),
            'is_good': bool (score > threshold)
        }
    """
    sx, sy = edge_start
    tx, ty = edge_end

    dx, dy = tx - sx, ty - sy

    score, axis = global_axis_perpendicularity(dx, dy)

    # Угол отклонения от идеальной оси
    # score = 1 - |sin(angle)|, поэтому angle = arcsin(1 - score)
    angle = math.degrees(math.asin(min(1.0, 1.0 - score)))

    return {
        'score': score,
        'axis': axis,
        'angle': angle,
        'is_good': score >= PERPENDICULARITY_THRESHOLD,
        # Для обратной совместимости
        'source_perp': score,
        'target_perp': score,
        'source_angle': angle,
        'target_angle': angle,
    }



# =====================================================================
# ORTHOGONAL ROUTING (Фаза 2)
# =====================================================================

def determine_exit_direction(
    src_x: float, src_y: float,
    src_bbox: List[float] | None,
    src_type: str
) -> str | None:
    """
    Определить направление выхода ребра из узла.
    Returns: 'H' (horizontal first), 'V' (vertical first), or None (unknown).
    """
    if src_type == 'connector' or not src_bbox or len(src_bbox) != 4:
        return None

    x1, y1, x2, y2 = src_bbox
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2

    # Определяем к какой стенке ближе connection point
    dist_left = abs(src_x - x1)
    dist_right = abs(src_x - x2)
    dist_top = abs(src_y - y1)
    dist_bottom = abs(src_y - y2)

    min_dist = min(dist_left, dist_right, dist_top, dist_bottom)

    if min_dist == dist_left or min_dist == dist_right:
        return 'H'  # Выход через бок → горизонтальный сегмент первым
    else:
        return 'V'  # Выход через верх/низ → вертикальный сегмент первым


def compute_l_route(
    src_x: float, src_y: float,
    tgt_x: float, tgt_y: float,
    snap_threshold: float = 12.0,
    src_exit: str | None = None,
    tgt_exit: str | None = None,
    offset: float = 15.0,
) -> list:
    """
    Чистый ортогональный route между точками (offset обеспечивается снаружи через virtual points).

    H+H: Z-shape (2 wp), средний вертикальный
    V+V: Z-shape (2 wp), средний горизонтальный
    H+V: L-shape (1 wp)
    V+H: L-shape (1 wp)
    """
    dx = tgt_x - src_x
    dy = tgt_y - src_y

    if abs(dx) < snap_threshold and abs(dy) < snap_threshold:
        return []

    if src_exit == 'H' and tgt_exit == 'H':
        if abs(dy) < snap_threshold:
            return []
        mid_x = (src_x + tgt_x) / 2
        return [[src_y, mid_x], [tgt_y, mid_x]]

    elif src_exit == 'V' and tgt_exit == 'V':
        if abs(dx) < snap_threshold:
            return []
        mid_y = (src_y + tgt_y) / 2
        return [[mid_y, src_x], [mid_y, tgt_x]]

    elif src_exit == 'H' and tgt_exit == 'V':
        # L: горизонтально от src → вертикально в tgt
        return [[src_y, tgt_x]]

    elif src_exit == 'V' and tgt_exit == 'H':
        # L: вертикально от src → горизонтально в tgt
        return [[tgt_y, src_x]]

    else:
        if abs(dy) < snap_threshold:
            return []
        if abs(dx) < snap_threshold:
            return []
        mid_x = (src_x + tgt_x) / 2
        return [[src_y, mid_x], [tgt_y, mid_x]]


# =====================================================================
# BBOX / NODE GEOMETRY HELPERS
# Перенесено из graph_editor.py (этап 1.1 рефакторинга)
# =====================================================================

def bbox_exit_side(bbox: List[float], from_cx: float, from_cy: float,
                   to_cx: float, to_cy: float) -> str:
    """Определить сторону bbox через которую выходить к цели.

    Сравнивает относительное смещение по X и Y с учётом размеров bbox.

    Args:
        bbox: [x1, y1, x2, y2]
        from_cx, from_cy: центр «своего» узла
        to_cx, to_cy: центр «целевого» узла

    Returns:
        'left' | 'right' | 'top' | 'bottom'
    """
    x1, y1, x2, y2 = bbox
    dx = to_cx - from_cx
    dy = to_cy - from_cy

    w = max(x2 - x1, 1)
    h = max(y2 - y1, 1)

    if abs(dx) / w >= abs(dy) / h:
        return 'right' if dx > 0 else 'left'
    else:
        return 'bottom' if dy > 0 else 'top'


def closest_bbox_side(bbox: List[float], x: float, y: float) -> str:
    """Ближайшая сторона bbox к точке (x, y).

    Returns:
        'left' | 'right' | 'top' | 'bottom'
    """
    x1, y1, x2, y2 = bbox
    distances = {
        'left': abs(x - x1),
        'right': abs(x - x2),
        'top': abs(y - y1),
        'bottom': abs(y - y2),
    }
    return min(distances, key=distances.get)


def bbox_side_midpoint(bbox: List[float], side: str) -> Tuple[float, float]:
    """Середина указанной стороны bbox.

    Args:
        bbox: [x1, y1, x2, y2]
        side: 'left' | 'right' | 'top' | 'bottom'

    Returns:
        (x, y)
    """
    x1, y1, x2, y2 = bbox
    mid_x = (x1 + x2) / 2
    mid_y = (y1 + y2) / 2
    if side == 'left':
        return (x1, mid_y)
    elif side == 'right':
        return (x2, mid_y)
    elif side == 'top':
        return (mid_x, y1)
    elif side == 'bottom':
        return (mid_x, y2)
    return (mid_x, mid_y)


def _project_point_to_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> Tuple[float, float, float]:
    """Ближайшая точка на отрезке AB к точке P.

    Returns:
        (x, y, dist2) — координаты проекции и квадрат расстояния до неё.
    """
    abx, aby = bx - ax, by - ay
    ab2 = abx * abx + aby * aby
    if ab2 == 0.0:
        t = 0.0
    else:
        t = ((px - ax) * abx + (py - ay) * aby) / ab2
        t = max(0.0, min(1.0, t))
    cx, cy = ax + t * abx, ay + t * aby
    d2 = (px - cx) ** 2 + (py - cy) ** 2
    return cx, cy, d2


def project_point_to_bbox_border(bbox: List[float], x: float, y: float) -> Tuple[float, float]:
    """Ближайшая точка на периметре прямоугольника к точке (x, y).

    Позволяет свободно «скользить» точкой прикрепления вдоль границы bbox,
    а не только по центрам сторон.

    Args:
        bbox: [x1, y1, x2, y2]
        x, y: исходная точка

    Returns:
        (x, y) на периметре bbox
    """
    x1, y1, x2, y2 = bbox
    edges = [
        (x1, y1, x2, y1),  # top
        (x2, y1, x2, y2),  # right
        (x2, y2, x1, y2),  # bottom
        (x1, y2, x1, y1),  # left
    ]
    best = None
    for ax, ay, bx, by in edges:
        cx, cy, d2 = _project_point_to_segment(x, y, ax, ay, bx, by)
        if best is None or d2 < best[2]:
            best = (cx, cy, d2)
    return best[0], best[1]


def project_point_to_polygon_border(
    poly_flat: List[float], x: float, y: float
) -> Tuple[float, float]:
    """Ближайшая точка на периметре полигона к точке (x, y).

    Args:
        poly_flat: плоский список координат [x1, y1, x2, y2, ...]
        x, y: исходная точка

    Returns:
        (x, y) на периметре полигона (или исходная точка, если полигон вырожден)
    """
    pts = [(poly_flat[i], poly_flat[i + 1]) for i in range(0, len(poly_flat) - 1, 2)]
    if len(pts) < 2:
        return x, y
    best = None
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]  # замыкаем контур
        cx, cy, d2 = _project_point_to_segment(x, y, ax, ay, bx, by)
        if best is None or d2 < best[2]:
            best = (cx, cy, d2)
    return best[0], best[1]


def get_node_geometry(node: dict) -> dict:
    """Геометрия узла: приоритет polygon > bbox > point.

    Используется для расчёта перпендикулярности рёбер.

    Args:
        node: dict с полями type, segmentation, bbox, centroid

    Returns:
        {'type': 'polygon'|'bbox'|'point', 'data': ...}
        - polygon: data = [x1, y1, x2, y2, ...] (плоский список)
        - bbox: data = [x1, y1, x2, y2]
        - point: data = (cx, cy) или None
    """
    node_type = node.get('type', 'connector')
    seg = node.get('segmentation')
    bbox = node.get('bbox')

    if seg and isinstance(seg, list) and len(seg) >= 6:
        return {'type': 'polygon', 'data': seg}
    if bbox and len(bbox) == 4:
        return {'type': 'bbox', 'data': bbox}

    centroid = node.get('centroid')
    if centroid and len(centroid) >= 2:
        return {'type': 'point', 'data': (centroid[1], centroid[0])}
    return {'type': 'point', 'data': None}

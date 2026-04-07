"""
Geometry utilities — расстояние от OCR bbox до ребра графа.

Координаты:
- bbox: [x1, y1, x2, y2]
- source_point / target_point: [y, x] (!)
- waypoints: [[y, x], ...]
"""

import math
from typing import Optional


def bbox_center(bbox: list) -> tuple[float, float]:
    """Центр bbox → (x, y)."""
    return (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2


def bbox_boundary_points(bbox: list, n_per_side: int = 3) -> list[tuple[float, float]]:
    """
    Точки на границе bbox (для расчёта расстояния до ребра).
    Возвращает точки на 4 сторонах bbox.
    """
    x1, y1, x2, y2 = bbox
    points = []
    # Top & bottom
    for i in range(n_per_side):
        t = i / max(n_per_side - 1, 1)
        x = x1 + t * (x2 - x1)
        points.append((x, y1))
        points.append((x, y2))
    # Left & right (exclude corners)
    for i in range(1, n_per_side - 1):
        t = i / max(n_per_side - 1, 1)
        y = y1 + t * (y2 - y1)
        points.append((x1, y))
        points.append((x2, y))
    return points


def point_to_segment_dist(px: float, py: float,
                          ax: float, ay: float,
                          bx: float, by: float) -> float:
    """Расстояние от точки до отрезка."""
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def edge_to_xy_segments(edge: dict) -> list[tuple[float, float, float, float]]:
    """
    Ребро → список отрезков [(x1,y1,x2,y2), ...] в координатах (x, y).
    source_point / target_point хранятся как [y, x].
    """
    sp = edge.get("source_point")
    tp = edge.get("target_point")
    if not sp or not tp:
        return []
    # [y, x] → (x, y)
    points = [(sp[1], sp[0])]
    for wp in edge.get("waypoints", []):
        points.append((wp[1], wp[0]))
    points.append((tp[1], tp[0]))

    segments = []
    for i in range(len(points) - 1):
        segments.append((*points[i], *points[i + 1]))
    return segments


def edge_midpoint(edge: dict) -> Optional[tuple[float, float]]:
    """Середина ребра → (x, y)."""
    sp = edge.get("source_point")
    tp = edge.get("target_point")
    if not sp or not tp:
        return None
    wps = edge.get("waypoints", [])
    if wps:
        mid = wps[len(wps) // 2]
        return mid[1], mid[0]
    return (sp[1] + tp[1]) / 2, (sp[0] + tp[0]) / 2


def point_to_segment_dist_ex(px: float, py: float,
                             ax: float, ay: float,
                             bx: float, by: float) -> tuple[float, bool]:
    """Расстояние от точки до отрезка + on_segment флаг.
    
    on_segment=True если ортогональная проекция точки попадает
    на отрезок (t ∈ [0,1]), т.е. точка "над" отрезком.
    """
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 < 1e-9:
        return math.hypot(px - ax, py - ay), True
    t = ((px - ax) * dx + (py - ay) * dy) / l2
    on_segment = 0.0 <= t <= 1.0
    tc = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + tc * dx), py - (ay + tc * dy)), on_segment


def bbox_to_edge_distance(bbox: list, edge: dict) -> Optional[float]:
    """Расстояние от центра bbox до ребра."""
    segments = edge_to_xy_segments(edge)
    if not segments:
        return None
    cx, cy = bbox_center(bbox)
    min_dist = float("inf")
    for ax, ay, bx, by in segments:
        d = point_to_segment_dist(cx, cy, ax, ay, bx, by)
        if d < min_dist:
            min_dist = d
    return min_dist if min_dist < float("inf") else None


def bbox_to_edge_distance_ex(bbox: list, edge: dict) -> tuple[Optional[float], bool]:
    """Расстояние от центра bbox до ребра + on_segment флаг.
    
    on_segment=True если центр bbox проецируется на один из
    отрезков ребра. Это значит ребро "под" текстом.
    """
    segments = edge_to_xy_segments(edge)
    if not segments:
        return None, False
    cx, cy = bbox_center(bbox)
    min_dist = float("inf")
    any_on = False
    for ax, ay, bx, by in segments:
        d, on = point_to_segment_dist_ex(cx, cy, ax, ay, bx, by)
        if on:
            any_on = True
        if d < min_dist:
            min_dist = d
    return (min_dist if min_dist < float("inf") else None), any_on


def edge_orientation(edge: dict) -> Optional[str]:
    """
    Определить ориентацию ребра: 'H' (горизонтальное), 'V' (вертикальное), 'D' (диагональное).
    Для рёбер с waypoints — по преобладающей ориентации.
    """
    segments = edge_to_xy_segments(edge)
    if not segments:
        return None
    # Суммарный dx, dy по всем сегментам
    total_dx, total_dy = 0.0, 0.0
    for ax, ay, bx, by in segments:
        total_dx += abs(bx - ax)
        total_dy += abs(by - ay)
    if total_dx + total_dy < 1e-3:
        return None
    ratio = min(total_dx, total_dy) / max(total_dx, total_dy)
    if ratio < 0.3:
        return "H" if total_dx > total_dy else "V"
    return "D"


def edge_intersects_bbox(bbox: list, edge: dict) -> bool:
    """
    Проверить: ребро пересекает или проходит через bbox?
    Если да — это перемычка сквозь текст, не целевое ребро.
    """
    x1, y1, x2, y2 = bbox
    segments = edge_to_xy_segments(edge)
    for ax, ay, bx, by in segments:
        # Проверка пересечения отрезка с прямоугольником (Cohen-Sutherland style)
        # Если оба конца внутри — проходит через
        a_inside = x1 <= ax <= x2 and y1 <= ay <= y2
        b_inside = x1 <= bx <= x2 and y1 <= by <= y2
        if a_inside or b_inside:
            return True
        # Проверка пересечения отрезка со сторонами bbox
        for sx1, sy1, sx2, sy2 in [
            (x1, y1, x2, y1),  # top
            (x1, y2, x2, y2),  # bottom
            (x1, y1, x1, y2),  # left
            (x2, y1, x2, y2),  # right
        ]:
            if _segments_intersect(ax, ay, bx, by, sx1, sy1, sx2, sy2):
                return True
    return False


def _segments_intersect(ax, ay, bx, by, cx, cy, dx, dy) -> bool:
    """Проверка пересечения двух отрезков (включая коллинеарное перекрытие)."""
    def cross(ox, oy, px, py, qx, qy):
        return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)

    def on_segment(px, py, qx, qy, rx, ry):
        """Точка r лежит на отрезке pq (при условии коллинеарности)."""
        return (min(px, qx) <= rx <= max(px, qx) and
                min(py, qy) <= ry <= max(py, qy))

    d1 = cross(cx, cy, dx, dy, ax, ay)
    d2 = cross(cx, cy, dx, dy, bx, by)
    d3 = cross(ax, ay, bx, by, cx, cy)
    d4 = cross(ax, ay, bx, by, dx, dy)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    # Коллинеарные случаи
    EPS = 1e-9
    if abs(d1) < EPS and on_segment(cx, cy, dx, dy, ax, ay):
        return True
    if abs(d2) < EPS and on_segment(cx, cy, dx, dy, bx, by):
        return True
    if abs(d3) < EPS and on_segment(ax, ay, bx, by, cx, cy):
        return True
    if abs(d4) < EPS and on_segment(ax, ay, bx, by, dx, dy):
        return True
    return False


def edge_length(edge: dict) -> float:
    """Полная длина ребра (сумма сегментов)."""
    segments = edge_to_xy_segments(edge)
    total = 0.0
    for ax, ay, bx, by in segments:
        total += math.hypot(bx - ax, by - ay)
    return total


def bbox_to_edge_distance_weighted(bbox: list, edge: dict,
                                    short_edge_threshold: float = 80.0,
                                    short_edge_penalty: float = 3.0,
                                    diagonal_penalty: float = 2.0) -> Optional[float]:
    """
    Расстояние от центра bbox до ребра со штрафами.

    На P&ID диаметр подписывает длинную трубу, а не короткую перемычку.
    Штрафы (кумулятивные):
    - Короткие рёбра (< short_edge_threshold) — distance × short_edge_penalty
    - Диагональные рёбра — distance × diagonal_penalty
    """
    d = bbox_to_edge_distance(bbox, edge)
    if d is None:
        return None
    length = edge_length(edge)
    if length < short_edge_threshold:
        d *= short_edge_penalty
    orient = edge_orientation(edge)
    if orient == "D":
        d *= diagonal_penalty
    return d


def bbox_center_to_edge_distance(bbox: list, edge: dict) -> Optional[float]:
    """Расстояние от центра bbox до ребра."""
    segments = edge_to_xy_segments(edge)
    if not segments:
        return None
    cx, cy = bbox_center(bbox)
    min_dist = float("inf")
    for ax, ay, bx, by in segments:
        d = point_to_segment_dist(cx, cy, ax, ay, bx, by)
        if d < min_dist:
            min_dist = d
    return min_dist if min_dist < float("inf") else None

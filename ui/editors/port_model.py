# -*- coding: utf-8 -*-
"""port_model.py — портовая модель посадки концов рёбер (этап A).

Жалоба заказчика (§2 п.2-3 EDITOR_AFTER_LAYOUT_PLAN): «подключение гуляет по
периметру» — ray-посадка канона сажает конец лучом в соседа, и при переносе
точка входа ползёт вдоль грани/контура. Модель портов (аналоги: yFiles port
candidates, Visio connection point glue, DEXPI ConnectionPoints — §6.1 плана):

  * ПОРТЫ-КАНДИДАТЫ — производные, НЕ хранятся (`candidate_ports`):
      бокс / словарный скин  → центры четырёх граней прямоугольника посадки
                               (`seating._anchor_rect`: у скина content-rect);
      полигон без скина      → середины ПРЯМЫХ участков контура длиной
                               >= MIN_POLY_EDGE (перпендикулярный вход);
      коннектор              → центроид (единственный порт).
  * РУЧНЫЕ ПОРТЫ — хранятся в node['_ports'] = [{'dx','dy'}, ...] —
    ЛОКАЛЬНЫЕ смещения от центроида: при переносе узла порт едет с ним,
    при resize смещения масштабируются (`rescale_manual_ports`).
    Ключ '_ports' не входит в canvas_state._NODE_KEYS — sha проекции холста
    не меняется; в FXML не течёт (graph_to_fxml читает известные поля).
  * ВЫБОР ПОРТА (`choose_port`) — по качеству маршрута с гистерезисом:
    остаёмся на текущем порту, пока (а) вход через него не с изнанки
    (обязательная смена — обобщение side-flip) и (б) альтернатива не
    выигрывает РАДИКАЛЬНО (короче на >= RADICAL_LEN_FACTOR * snap_threshold
    И не больше колен). Порог смены >> порога «остаться» — порт не хлопает.

Канон `modules/graph/core/seating` НЕ меняется (сервер на нём, бит-эталон):
модель живёт только в редакторских путях (drag / reseat-при-открытии).
Чистый python без Qt и shapely — импортируется и вкладкой, и тестами.
"""
from __future__ import annotations

import math

MIN_POLY_EDGE = 16.0      # px: минимальная длина прямого участка контура
PORT_MATCH_TOL = 0.75     # px: «конец сидит на порту»
BACKSIDE_EPS = 0.5        # px: допуск изнанки (dot нормали с направлением)
RADICAL_LEN_FACTOR = 1.5  # выигрыш длины (в snap_threshold) для смены порта
STRAIGHT_TOL = 3.0        # px: соосность порт—ref => маршрут без колен
_COLLINEAR_SIN = 0.02     # слияние почти-коллинеарных рёбер контура (~1.15°)


# ────────────────────────── геометрия узла ──────────────────────────

def _node_cxy(node):
    c = node.get("centroid") or [0.0, 0.0]
    return float(c[1]), float(c[0])


def _poly_contour(node):
    """Контур полигонного узла БЕЗ скина — та же ветка, что канон посадки."""
    from modules.graph.core import seating

    seg = node.get("segmentation")
    if seg and isinstance(seg, list) and len(seg) >= 6 \
            and node.get("class_name") not in seating.FIXED_SIZES:
        return seg
    return None


def _pt_in_poly(px, py, pts):
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > py) != (yj > py) and \
                px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _poly_ports(seg):
    """Середины прямых участков контура (соседние коллинеарные рёбра
    сливаются в один участок), длиной >= MIN_POLY_EDGE; нормаль — наружу
    (проба точкой: внутрь контура => перевернуть)."""
    pts = [(float(seg[i]), float(seg[i + 1]))
           for i in range(0, len(seg) - 1, 2)]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts.pop()
    n = len(pts)
    if n < 3:
        return []

    def _dir(i):
        ax, ay = pts[i % n]
        bx, by = pts[(i + 1) % n]
        return bx - ax, by - ay

    def _collinear(d1, d2):
        l1, l2 = math.hypot(*d1), math.hypot(*d2)
        if l1 <= 1e-9 or l2 <= 1e-9:
            return True
        cross = d1[0] * d2[1] - d1[1] * d2[0]
        dot = d1[0] * d2[0] + d1[1] * d2[1]
        return dot > 0 and abs(cross) / (l1 * l2) <= _COLLINEAR_SIN

    start = 0                       # вершина, где направление ломается
    for i in range(n):
        if not _collinear(_dir(i - 1), _dir(i)):
            start = i
            break
    ports = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and _collinear(_dir(start + j), _dir(start + j + 1)):
            j += 1
        ax, ay = pts[(start + i) % n]
        bx, by = pts[(start + j + 1) % n]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length >= MIN_POLY_EDGE:
            mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
            nx, ny = dy / length, -dx / length
            if _pt_in_poly(mx + nx * 2.0, my + ny * 2.0, pts):
                nx, ny = -nx, -ny
            ports.append((mx, my, nx, ny, False))
        i = j + 1
    return ports


# ────────────────────────── порты узла ──────────────────────────

def candidate_ports(node):
    """Производные порты-кандидаты [(x, y, nx, ny, manual=False), ...].

    (nx, ny) — наружная нормаль грани/участка; (0, 0) — точечный порт
    (коннектор/фолбэк на центроид)."""
    from modules.graph.core import seating
    from modules.graph.core.graph_access import is_connector

    if is_connector(node):
        cx, cy = _node_cxy(node)
        return [(cx, cy, 0.0, 0.0, False)]
    seg = _poly_contour(node)
    if seg is not None:
        ports = _poly_ports(seg)
        if ports:
            return ports
    rect = seating._anchor_rect(node)
    if rect is not None:
        x1, y1, x2, y2 = rect
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        return [(x2, cy, 1.0, 0.0, False), (x1, cy, -1.0, 0.0, False),
                (cx, y1, 0.0, -1.0, False), (cx, y2, 0.0, 1.0, False)]
    cx, cy = _node_cxy(node)
    return [(cx, cy, 0.0, 0.0, False)]


def manual_ports(node):
    """Ручные порты из node['_ports'] в абсолютных координатах.

    Нормаль — от центроида к порту (для границы выпуклой формы это наружу)."""
    cx, cy = _node_cxy(node)
    out = []
    for p in node.get("_ports") or []:
        px = cx + float(p.get("dx", 0.0))
        py = cy + float(p.get("dy", 0.0))
        dx, dy = px - cx, py - cy
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            out.append((px, py, 0.0, 0.0, True))
        else:
            out.append((px, py, dx / length, dy / length, True))
    return out


def all_ports(node):
    return manual_ports(node) + candidate_ports(node)


def add_manual_port(node, x, y):
    """Создать постоянный ручной порт узла в точке (x, y) — хранится как
    локальное смещение от центроида."""
    cx, cy = _node_cxy(node)
    entry = {"dx": float(x - cx), "dy": float(y - cy)}
    node.setdefault("_ports", []).append(entry)
    return entry


def rescale_manual_ports(node, old_bbox, new_bbox):
    """Resize узла: смещения ручных портов масштабируются вместе с рамкой
    (перепроекция на границу — как геометрия в _on_node_resized)."""
    ports = node.get("_ports")
    if not ports or not old_bbox or not new_bbox \
            or len(old_bbox) != 4 or len(new_bbox) != 4:
        return
    ow, oh = old_bbox[2] - old_bbox[0], old_bbox[3] - old_bbox[1]
    nw, nh = new_bbox[2] - new_bbox[0], new_bbox[3] - new_bbox[1]
    if ow <= 0 or oh <= 0:
        return
    sx, sy = nw / ow, nh / oh
    for p in ports:
        p["dx"] = float(p.get("dx", 0.0)) * sx
        p["dy"] = float(p.get("dy", 0.0)) * sy


def nearest_port(node, x, y, radius):
    """Ближайший порт узла (ручной или кандидат) в радиусе, иначе None."""
    best, best_d = None, float(radius)
    for p in all_ports(node):
        d = math.hypot(p[0] - x, p[1] - y)
        if d <= best_d:
            best, best_d = p, d
    return best


def is_on_port(node, x, y, tol=PORT_MATCH_TOL):
    return nearest_port(node, x, y, tol) is not None


# ────────────────────────── выбор порта ──────────────────────────

def _l1(p, rx, ry):
    return abs(rx - p[0]) + abs(ry - p[1])


def _backside(p, rx, ry):
    """Вход в порт с изнанки: ref в полуплоскости ПОЗАДИ нормали порта —
    маршрут потребовал бы оборота вокруг своего символа."""
    nx, ny = p[2], p[3]
    if nx == 0.0 and ny == 0.0:
        return False                       # точечный порт (коннектор)
    return (rx - p[0]) * nx + (ry - p[1]) * ny < -BACKSIDE_EPS


def _bends(p, rx, ry):
    """Оценка числа колен ортогонального маршрута порт → ref: 0 — соосно и
    наружу; 1 — L; 2 — ref сбоку от плоскости грани (S); 3 — изнанка."""
    x, y, nx, ny, _manual = p
    dx, dy = rx - x, ry - y
    if nx == 0.0 and ny == 0.0:
        return 0 if min(abs(dx), abs(dy)) <= STRAIGHT_TOL else 1
    dot = dx * nx + dy * ny
    aligned = abs(dy) <= STRAIGHT_TOL if abs(nx) >= abs(ny) \
        else abs(dx) <= STRAIGHT_TOL
    if dot > BACKSIDE_EPS:
        return 0 if aligned else 1
    return 3 if dot < -BACKSIDE_EPS else 2


def lock_respected(pt, lock):
    """Посадка (x, y) реально легла на ось замка ('H', y)|('V', x)?
    node_anchor молча игнорирует замок, когда форма не накрывает ось, —
    тогда прямизны нет и решает портовая модель."""
    if not lock or pt is None:
        return False
    axis, coord = lock
    val = pt[1] if axis == "H" else pt[0]
    return abs(val - coord) <= 0.51


def choose_port(node, cur_xy, ref_xy, snap_threshold):
    """Порт для посадки конца, обращённого к ref_xy. Возвращает (x, y).

    Гистерезис: текущий порт (ближайший к cur_xy, в пределах snap_threshold)
    удерживается, пока вход через него не с изнанки и альтернатива не
    выигрывает радикально (короче на >= RADICAL_LEN_FACTOR * snap_threshold
    при не большем числе колен). cur_xy=None (нет текущего) — лучший порт.
    """
    rx, ry = ref_xy
    ports = all_ports(node)
    if not ports:
        return _node_cxy(node)
    if len(ports) == 1:
        return ports[0][0], ports[0][1]

    cx, cy = _node_cxy(node)
    ux, uy = rx - cx, ry - cy
    ul = math.hypot(ux, uy) or 1.0
    ux, uy = ux / ul, uy / ul

    def _key(p):
        # колени → длина → нормаль по доминирующему направлению → координаты
        return (_bends(p, rx, ry), _l1(p, rx, ry),
                -(p[2] * ux + p[3] * uy), p[0], p[1])

    pool = [p for p in ports if not _backside(p, rx, ry)] or ports
    best = min(pool, key=_key)

    cur = None
    if cur_xy is not None:
        cand = min(ports, key=lambda p: (p[0] - cur_xy[0]) ** 2
                   + (p[1] - cur_xy[1]) ** 2)
        if math.hypot(cand[0] - cur_xy[0], cand[1] - cur_xy[1]) \
                <= max(float(snap_threshold), 4.0):
            cur = cand
    if cur is None or _backside(cur, rx, ry):
        return best[0], best[1]
    if _l1(cur, rx, ry) - _l1(best, rx, ry) \
            >= RADICAL_LEN_FACTOR * float(snap_threshold) \
            and _bends(best, rx, ry) <= _bends(cur, rx, ry):
        return best[0], best[1]
    return cur[0], cur[1]

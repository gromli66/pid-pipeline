# -*- coding: utf-8 -*-
"""ports.py — портовая модель посадки концов рёбер (этап A, общая логика).

Родина модели — редактор (`ui/editors/port_model.py`, коммит 8344b32); сюда
вынесена чистая часть, потому что финал этапа A сажает в порты и СЕРВЕРНУЮ
раскладку: пины libavoid-роутинга (`layout/avoid_router.py`) выбираются тем же
судьёй, что и drag в редакторе — сторож == судья, свежий холст выходит уже
портовым. Редакторский модуль остаётся тонким реэкспортом.

Модель (аналоги: yFiles port candidates, Visio connection point glue, DEXPI
ConnectionPoints — §6.1 EDITOR_AFTER_LAYOUT_PLAN):

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
порт решает, КУДА сажать, канон — КАК конец лежит на форме.
Чистый python (stdlib) без Qt и shapely.
"""
from __future__ import annotations

import math

MIN_POLY_EDGE = 16.0      # px: минимальная длина прямого участка контура
SLOT_PITCH = 18.0         # px: шаг слотов вокруг середины грани (Э2b)
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


def poly_runs(seg):
    """ПРЯМЫЕ участки контура (соседние коллинеарные рёбра сливаются):
    [(ax, ay, bx, by, nx, ny), ...] — концы участка + наружная нормаль
    (проба точкой: внутрь контура => перевернуть). Участки любой длины;
    порты из них строит `_poly_ports` (фильтр >= MIN_POLY_EDGE),
    распределение концов по участку — движок редактора (Э2b+)."""
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
    runs = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and _collinear(_dir(start + j), _dir(start + j + 1)):
            j += 1
        ax, ay = pts[(start + i) % n]
        bx, by = pts[(start + j + 1) % n]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length > 1e-9:
            nx, ny = dy / length, -dx / length
            if _pt_in_poly((ax + bx) / 2.0 + nx * 2.0,
                           (ay + by) / 2.0 + ny * 2.0, pts):
                nx, ny = -nx, -ny
            runs.append((ax, ay, bx, by, nx, ny))
        i = j + 1
    return runs


def _poly_ports(seg):
    """Середины прямых участков контура длиной >= MIN_POLY_EDGE
    (участки и нормали — `poly_runs`)."""
    ports = []
    for ax, ay, bx, by, nx, ny in poly_runs(seg):
        if math.hypot(bx - ax, by - ay) >= MIN_POLY_EDGE:
            ports.append(((ax + bx) / 2.0, (ay + by) / 2.0, nx, ny, False))
    return ports


# ────────────────────────── порты узла ──────────────────────────

def candidate_ports(node, rect=None):
    """Производные порты-кандидаты [(x, y, nx, ny, manual=False), ...].

    (nx, ny) — наружная нормаль грани/участка; (0, 0) — точечный порт
    (коннектор/фолбэк на центроид). rect — переопределение рамки посадки
    (редактор с 2026-08-01 передаёт bbox: «символ тянется на рамку»);
    None — серверный канон `seating._anchor_rect` (бит-эталон)."""
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
    if rect is None:
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


def all_ports(node, rect=None):
    return manual_ports(node) + candidate_ports(node, rect)


def edge_ref(edge_data):
    """Устойчивый идентификатор ребра для владения портом: пара узлов."""
    if not edge_data:
        return None
    a, b = edge_data.get("source"), edge_data.get("target")
    if a is None or b is None:
        return None
    return f"{a}|{b}" if str(a) <= str(b) else f"{b}|{a}"


def add_manual_port(node, x, y, edge_data=None):
    """Создать постоянный ручной порт узла в точке (x, y) — хранится как
    локальное смещение от центроида.

    edge_data задаёт ВЛАДЕЛЬЦА порта (решение заказчика 2026-08-02: «нужен
    классический обход, но с фиксированным входом»). Порт владельца —
    настоящий якорь: `pinned_port` возвращает его безусловно, минуя конкурс
    кандидатов. Без владельца порт остаётся прежней «подсказкой судье»,
    которую гистерезис вправе отбросить (совместимость со старыми холстами).

    Повторный вызов для того же ребра ПЕРЕЗАПИСЫВАЕТ его порт, а не плодит
    новые: оператор двигает вход много раз за жест."""
    cx, cy = _node_cxy(node)
    ref = edge_ref(edge_data)
    entry = {"dx": float(x - cx), "dy": float(y - cy)}
    if ref is not None:
        entry["edge"] = ref
        for p in node.get("_ports") or []:
            if p.get("edge") == ref:
                p.update(entry)
                return p
    node.setdefault("_ports", []).append(entry)
    return entry


def _pin_entry(cx, cy, px, py):
    """Кортеж порта (x, y, nx, ny, True) из абсолютной точки пина."""
    dx, dy = px - cx, py - cy
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return (px, py, 0.0, 0.0, True)
    return (px, py, dx / length, dy / length, True)


def pinned_port(node, edge_data):
    """Порт-ЯКОРЬ этого ребра (x, y, nx, ny, True) или None.

    Якорь безусловен: он не участвует в конкурсе портов, не отбрасывается
    гистерезисом при уводе оси и не теряется на контурных узлах — оператор
    поставил вход сюда, значит вход здесь. Хранится в локальных координатах
    от центроида, поэтому едет с узлом и переживает resize.

    Источник (Э5, модель «пин на ребре»): edge['pin_source'|'pin_target']
    конца, сидящего на этом узле. Легаси node['_ports'] с владельцем-парой
    читается ДО миграции открытия (Э5b) — новые записи туда не делаются."""
    if not node or not edge_data:
        return None
    cx, cy = _node_cxy(node)
    pin = pinned_on_node(node, edge_data)
    if pin is not None:
        return _pin_entry(cx, cy,
                          cx + float(pin.get("dx", 0.0)),
                          cy + float(pin.get("dy", 0.0)))
    # ЛЕГАСИ (до миграции Э5b): якорь в node['_ports'] по паре узлов.
    ref = edge_ref(edge_data)
    if ref is None:
        return None
    for p in node.get("_ports") or []:
        if p.get("edge") != ref:
            continue
        return _pin_entry(cx, cy,
                          cx + float(p.get("dx", 0.0)),
                          cy + float(p.get("dy", 0.0)))
    return None


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


# ────────────────────── пины на ребре (Э5) ──────────────────────
# Модель «пин входа — свойство КОНЦА РЕБРА» (утверждена 2026-08-03):
# edge['pin_source'|'pin_target'] = {'dx','dy'} — локальное смещение (x, y)
# от центроида узла этого конца: пин едет с узлом и переживает resize
# (rescale_edge_pins). Пин на КОННЕКТОРЕ запрещён — конец коннектора
# всегда центроид. Ключи pin_* не входят в canvas_state._EDGE_KEYS —
# sha проекции холста не меняется; в FXML не текут.

PIN_KEYS = {"source": "pin_source", "target": "pin_target"}


def pin_role(node, edge_data):
    """Роль конца ребра на этом узле: 'source' | 'target' | None."""
    if not node or not edge_data:
        return None
    nid = node.get("id")
    if nid is None:
        return None
    if edge_data.get("source") == nid:
        return "source"
    if edge_data.get("target") == nid:
        return "target"
    return None


def edge_pin(edge_data, role):
    """Запись пина конца ребра ({'dx','dy'}) или None."""
    if not edge_data or role not in PIN_KEYS:
        return None
    pin = edge_data.get(PIN_KEYS[role])
    if isinstance(pin, dict) and "dx" in pin and "dy" in pin:
        return pin
    return None


def pinned_on_node(node, edge_data):
    """Пин конца edge_data, сидящего на ЭТОМ узле, или None."""
    role = pin_role(node, edge_data)
    return edge_pin(edge_data, role) if role else None


def set_edge_pin(node, edge_data, role, x, y):
    """Закрепить вход конца ребра в точке (x, y): {'dx','dy'} от центроида.

    Повторный вызов перезаписывает пин (оператор двигает вход много раз за
    жест). На КОННЕКТОРЕ пин не создаётся (конец == центроид) — None."""
    from modules.graph.core.graph_access import is_connector

    if role not in PIN_KEYS or not node or edge_data is None:
        return None
    if is_connector(node):
        return None
    cx, cy = _node_cxy(node)
    pin = {"dx": float(x - cx), "dy": float(y - cy)}
    edge_data[PIN_KEYS[role]] = pin
    return pin


def clear_edge_pin(edge_data, role):
    """Отвязать вход: удалить пин конца. True, если пин был."""
    if edge_data is None or role not in PIN_KEYS:
        return False
    return edge_data.pop(PIN_KEYS[role], None) is not None


def rescale_edge_pins(node, edges, old_bbox, new_bbox):
    """Resize узла: смещения пинов ИНЦИДЕНТНЫХ рёбер масштабируются с рамкой
    (аналог rescale_manual_ports для модели «пин на ребре»)."""
    if not node or not old_bbox or not new_bbox \
            or len(old_bbox) != 4 or len(new_bbox) != 4:
        return
    ow, oh = old_bbox[2] - old_bbox[0], old_bbox[3] - old_bbox[1]
    nw, nh = new_bbox[2] - new_bbox[0], new_bbox[3] - new_bbox[1]
    if ow <= 0 or oh <= 0:
        return
    sx, sy = nw / ow, nh / oh
    nid = node.get("id")
    for e in edges or []:
        for role in PIN_KEYS:
            if e.get(role) != nid:
                continue
            pin = edge_pin(e, role)
            if pin is not None:
                pin["dx"] = float(pin["dx"]) * sx
                pin["dy"] = float(pin["dy"]) * sy


def side_slots(node, side, k, rect=None, pitch=None):
    """k слотов на грани рамки посадки, симметрично вокруг середины (Э2b).

    Решение заказчика 2026-07-31: одна труба в грань — ровно середина
    (k=1 бит-равен candidate_ports), несколько — равномерные слоты вокруг
    середины, шаг min(SLOT_PITCH, длина_грани/(k+1)) — крайние слоты не
    доходят до углов по построению. side из {'L','R','T','B'}; порядок —
    по возрастанию координаты вдоль грани. rect — переопределение рамки
    (редактор: bbox). [(x, y, nx, ny, False), ...]."""
    from modules.graph.core import seating

    if rect is None:
        rect = seating._anchor_rect(node)
    if rect is None or k < 1:
        return []
    x1, y1, x2, y2 = rect
    horiz = side in ("T", "B")               # ось грани — x
    lo, hi = (x1, x2) if horiz else (y1, y2)
    center = (lo + hi) / 2.0
    if k == 1:
        pitch = 0.0
    elif pitch is None:
        pitch = min(SLOT_PITCH, (hi - lo) / (k + 1))
    # pitch задан вызывающим (движок: шаг «по чернилам», Э10-лайт)
    offs = [(j - (k - 1) / 2.0) * pitch for j in range(k)]
    if side == "L":
        return [(x1, center + o, -1.0, 0.0, False) for o in offs]
    if side == "R":
        return [(x2, center + o, 1.0, 0.0, False) for o in offs]
    if side == "T":
        return [(center + o, y1, 0.0, -1.0, False) for o in offs]
    return [(center + o, y2, 0.0, 1.0, False) for o in offs]


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
    p = choose_port_entry(node, cur_xy, ref_xy, snap_threshold)
    return p[0], p[1]


def choose_port_entry(node, cur_xy, ref_xy, snap_threshold, rect=None):
    """То же, что choose_port, но возвращает ПОЛНЫЙ кортеж кандидата
    (x, y, nx, ny, manual) — движку редактора (Э2b) нужны нормаль (сторона
    грани) и признак ручного порта. Поведение выбора бит-идентично;
    rect — переопределение рамки посадки (редактор: bbox)."""
    rx, ry = ref_xy
    ports = all_ports(node, rect)
    if not ports:
        cx, cy = _node_cxy(node)
        return (cx, cy, 0.0, 0.0, False)
    if len(ports) == 1:
        return ports[0]

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
        return best
    if _l1(cur, rx, ry) - _l1(best, rx, ry) \
            >= RADICAL_LEN_FACTOR * float(snap_threshold) \
            and _bends(best, rx, ry) <= _bends(cur, rx, ry):
        return best
    return cur

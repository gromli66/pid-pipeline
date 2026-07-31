# -*- coding: utf-8 -*-
"""edit_engine.py — единый движок геометрии рёбер «Ручной правки» (Э2).

Одни ворота: всякая посадка конца ребра в редакторе проходит здесь.
Пересборка 2026-07 (план tender-launching-umbrella) сводит сюда все
дубль-реализации; редакторские методы — тонкие делегаты.

Целевой контракт движка (по этапам):
  C1 конец на порту: бокс — слоты вокруг середины стороны, полигон —
     контур/прямые участки, коннектор — центроид, ручной порт свят (Э2b);
  C2 конец НИКОГДА в углу рамки (Э2b, приоритет над всем);
  C3 полилиния строго ортогональна, запись со снапом к оси (Э3);
  C4 не сквозь бокс, контур приоритетнее bbox (Э3);
  C5 не вдоль границы ближе клиренса (Э3);
  C6 дальний конец жеста неприкосновенен (инвариант вызова);
  C7 предпросмотр == итог (инвариант вызова: один и тот же код на
     протяжке и отпускании).
Приоритет при конфликте: C2 > C3 («прямая важнее порта» ОТМЕНЕНО
решением заказчика 2026-07-31 «А->В»: конец всегда жёстко в порту,
малое колено честно остаётся — его лечит микро-доводка сдвигом узла) > C1.

Судьи дефектов — `edit_checks` (сторож == судья, импорт не копия).
Чистый stdlib: shapely/numpy/Qt в requirements/ui.txt нет.

Координаты (CODING_GUIDE §6): точки данных [y, x]; внутренняя математика
и возвраты — (x, y), как у `seating.node_anchor`.
"""
from __future__ import annotations

import math

from . import ports as port_model
from . import seating

VERTEX_MARGIN = 6.0   # px: конец не ближе к вершине контура (реш. 2026-08-01)


_NORMAL_SIDE = {(1.0, 0.0): "R", (-1.0, 0.0): "L",
                (0.0, -1.0): "T", (0.0, 1.0): "B"}


def _rect_seated(node) -> bool:
    """Конец узла сидит на рамке посадки (не контур, не коннектор)."""
    from . import edit_checks
    return edit_checks._rect_seated(node)


def _port_side(rect, px, py, tol=1.5):
    """Грань рамки, на которой лежит точка: 'L'|'R'|'T'|'B'|None."""
    x1, y1, x2, y2 = rect
    if abs(px - x1) <= tol and y1 - tol <= py <= y2 + tol:
        return "L"
    if abs(px - x2) <= tol and y1 - tol <= py <= y2 + tol:
        return "R"
    if abs(py - y1) <= tol and x1 - tol <= px <= x2 + tol:
        return "T"
    if abs(py - y2) <= tol and x1 - tol <= px <= x2 + tol:
        return "B"
    return None


def _slot_seat(node, node_edges, edge_data, side, ref_x, ref_y):
    """Слот на грани side для edge_data среди рёбер узла на той же грани.

    Членство: авто-рёбра узла, чей текущий конец лежит на грани
    (`_manual_route` не участвуют — их концы там, где поставил оператор).
    Порядок слотов — по проекции ДАЛЬНЕГО ориентира ребра (смежный
    waypoint, иначе противоположный конец) на ось грани: при drag узла
    дальние концы неподвижны — порядок стабилен по построению, слоты не
    мерцают. Тай-брейк — id ребра. Возвращает (x, y)."""
    rect = seating._anchor_rect(node)
    nid = node.get("id")
    horiz = side in ("T", "B")

    def _proj_ref(e, end_key):
        wps = e.get("waypoints") or []
        refp = (wps[0] if end_key == "source_point" else wps[-1]) if wps \
            else e.get("target_point" if end_key == "source_point"
                       else "source_point")
        if refp is None:
            return None
        return float(refp[1]) if horiz else float(refp[0])

    entries = [(ref_x if horiz else ref_y, str(edge_data.get("id")), True)]
    for e in node_edges or []:
        if e is edge_data or e.get("_manual_route"):
            continue
        if (e.get("source") or e.get("from")) == nid:
            end_key = "source_point"
        elif (e.get("target") or e.get("to")) == nid:
            end_key = "target_point"
        else:
            continue
        p = e.get(end_key)
        if p is None \
                or _port_side(rect, float(p[1]), float(p[0])) != side:
            continue
        proj = _proj_ref(e, end_key)
        if proj is None:
            continue
        entries.append((proj, str(e.get("id")), False))
    entries.sort(key=lambda t: (t[0], t[1]))
    idx = next(i for i, t in enumerate(entries) if t[2])
    slots = port_model.side_slots(node, side, len(entries))
    return slots[idx][0], slots[idx][1]


def _contour_seated(node) -> bool:
    from . import edit_checks
    return edit_checks._contour_seated(node)


def _poly_adjust(node, node_edges, edge_data, role, px, py, ref_x, ref_y):
    """Решение заказчика 2026-08-01: посадка на контур «как получились», НО
    (а) не ближе VERTEX_MARGIN к вершине участка; (б) несколько труб в один
    прямой участок распределяются вдоль него (шаг min(SLOT_PITCH,
    длина/(k+1)), симметрично вокруг середины участка), а не в одну точку.

    Членство и порядок — как у рамочных слотов (`_slot_seat`): авто-рёбра
    узла с концами на ТОМ ЖЕ участке, порядок по проекции дальних
    ориентиров на направление участка, тай-брейк id. Позиция, занятая
    концом соседа (< 2px), не выдаётся — берётся свободная (репро
    graph_edited0598: оба конца в одном слоте из-за разных снимков
    членства по кадрам жеста).

    ПОБОЧНЫЙ ЭФФЕКТ (единственные ворота геометрии — право движка):
    если у ребра есть waypoints и сдвиг конца вдоль участка перекосил бы
    перпендикулярный подводящий стаб, СМЕЖНОЕ колено сдвигается на ту же
    дельту — ортогональность стаба сохраняется (репро graph_edited0598:
    диагональные хвостики 18px). Неперпендикулярный стаб — распределение
    пропускается (перекос хуже скопления)."""
    seg = node.get("segmentation")
    if not seg:
        return px, py
    nid = node.get("id")
    for ax, ay, bx, by, _nx, _ny in port_model.poly_runs(seg):
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        t = ((px - ax) * dx + (py - ay) * dy) / (length * length)
        qx, qy = ax + t * dx, ay + t * dy
        if math.hypot(px - qx, py - qy) > 0.75 or not -0.01 <= t <= 1.01:
            continue

        def _t_of(rx, ry):
            return ((rx - ax) * dx + (ry - ay) * dy) / length

        entries = []
        for e in node_edges or []:
            if e is edge_data or e.get("_manual_route"):
                continue
            if (e.get("source") or e.get("from")) == nid:
                end_key = "source_point"
            elif (e.get("target") or e.get("to")) == nid:
                end_key = "target_point"
            else:
                continue
            p = e.get(end_key)
            if p is None:
                continue
            ex, ey = float(p[1]), float(p[0])
            tt = ((ex - ax) * dx + (ey - ay) * dy) / (length * length)
            if not -0.01 <= tt <= 1.01 \
                    or math.hypot(ex - (ax + tt * dx),
                                  ey - (ay + tt * dy)) > 0.75:
                continue
            wps = e.get("waypoints") or []
            refp = (wps[0] if end_key == "source_point" else wps[-1]) if wps \
                else e.get("target_point" if end_key == "source_point"
                           else "source_point")
            if refp is None:
                continue
            entries.append((_t_of(float(refp[1]), float(refp[0])),
                            str(e.get("id")), False))
        wps = edge_data.get("waypoints") or []
        adj = None
        if wps:
            adj = wps[0] if role == "s" else wps[-1]
            stub_dot = abs((float(adj[1]) - px) * dx
                           + (float(adj[0]) - py) * dy) / length
            if stub_dot > 1.0:
                return px, py       # стаб не перпендикулярен — не косить

        def _place(s):
            s = min(max(s, 0.0), length)
            nx2, ny2 = ax + (s / length) * dx, ay + (s / length) * dy
            if adj is not None:
                # колено едет вдоль участка вместе с концом — стаб прям
                adj[1] = float(adj[1]) + (nx2 - px)
                adj[0] = float(adj[0]) + (ny2 - py)
            return nx2, ny2

        if not entries:
            m = min(VERTEX_MARGIN, length / 2.0)
            return _place(min(max(t * length, m), length - m))
        entries.append((_t_of(ref_x, ref_y), str(edge_data.get("id")), True))
        entries.sort(key=lambda r: (r[0], r[1]))
        idx = next(i for i, r in enumerate(entries) if r[2])
        k = len(entries)
        pitch = min(port_model.SLOT_PITCH, length / (k + 1))
        slots = [length / 2.0 + (j - (k - 1) / 2.0) * pitch for j in range(k)]
        # позиции, занятые концами соседей (< 2px), не выдаются
        taken = []
        for e in node_edges or []:
            if e is edge_data:
                continue
            for pk in ("source_point", "target_point"):
                p2 = e.get(pk)
                if p2 is None:
                    continue
                ex, ey = float(p2[1]), float(p2[0])
                tt = ((ex - ax) * dx + (ey - ay) * dy) / (length * length)
                if -0.01 <= tt <= 1.01 and math.hypot(
                        ex - (ax + tt * dx), ey - (ay + tt * dy)) <= 0.75:
                    taken.append(tt * length)
        order = sorted(range(k), key=lambda j: (abs(j - idx), j))
        for j in order:
            if all(abs(slots[j] - s2) >= 2.0 for s2 in taken):
                return _place(slots[j])
        return _place(slots[idx])
    return px, py


def seat_end(node, other_node, edge_data, role, cur, ref_x, ref_y,
             try_slack, snap_threshold, node_edges=None):
    """Посадка конца ребра на узел — единые ворота (Э2a/Э2b).

    Коннектор и полигон (посадка «как получились», спека заказчика):
      1. станция Э10 (`seating._poly_even_seat`) — канон, приоритетнее;
      2. ЭФФЕКТИВНЫЙ замок прямизны (`_seg_lock`, слабина
         `straight_slack_lock` при try_slack) — только если форма реально
         накрыла ось (`lock_respected`);
      3. порт с гистерезисом (`choose_port`).

    Рамочные узлы (бокс/скин) — Э2b, решение заказчика «А->В»
    (2026-07-31): конец ВСЕГДА жёстко в порту — ручной порт свят, иначе
    слот вокруг середины выбранной грани (`side_slots`; одна труба —
    ровно середина). Замки прямизны конец по грани НЕ скользят («прямая
    важнее порта» отменено): малый увод оси соседа даёт честное колено,
    его лечит микро-доводка сдвигом узла (Э4). Сторона и ручной порт
    держатся гистерезисом `choose_port_entry`; слоты не мерцают
    (порядок — по дальним ориентирам, см. `_slot_seat`). Угол рамки
    непредставим по построению: слоты не доходят до углов, замковый
    кламп в угол исключён вместе с замками.

    cur — текущий конец [y, x] (гистерезис «остаться на своём порту»),
    role — 's'|'t' для станции Э10; node_edges — рёбра узла (для слотов;
    None => k=1, чистая середина). Возвращает (x, y).
    """
    station = seating._poly_even_seat(node, edge_data, role, (ref_x, ref_y))
    if station:
        return station

    if _rect_seated(node):
        p = port_model.choose_port_entry(
            node, (cur[1], cur[0]) if cur else None, (ref_x, ref_y),
            float(snap_threshold))
        if p[4]:                                   # ручной порт свят
            return p[0], p[1]
        side = _NORMAL_SIDE.get((p[2], p[3]))
        if side is None:                           # точечный фолбэк
            return p[0], p[1]
        return _slot_seat(node, node_edges, edge_data, side, ref_x, ref_y)

    lock = None
    if cur:
        lock = seating._seg_lock((cur[1], cur[0]), (ref_x, ref_y))
        if lock and not port_model.lock_respected(
                seating.node_anchor(node, ref_x, ref_y, lock), lock):
            lock = None
    if lock is None and try_slack and other_node is not None:
        lock = seating.straight_slack_lock(node, other_node, ref_x, ref_y)
        if lock and not port_model.lock_respected(
                seating.node_anchor(node, ref_x, ref_y, lock), lock):
            lock = None
    if lock:
        pt = seating.node_anchor(node, ref_x, ref_y, lock)
    else:
        pt = port_model.choose_port(
            node, (cur[1], cur[0]) if cur else None, (ref_x, ref_y),
            float(snap_threshold))
    if _contour_seated(node):
        # решение 2026-08-01: отступ от вершин контура + распределение
        # нескольких труб по прямому участку («не в одну точку»)
        return _poly_adjust(node, node_edges, edge_data, role, pt[0], pt[1],
                            ref_x, ref_y)
    return pt

# -*- coding: utf-8 -*-
"""seating.py — контрактная посадка концов рёбер (одна на всю систему).

Источник: стенд `_scratch/layout_align/harness/shared_geom.py` (строки 30-226).
Перенесено без изменения логики — см. docs/planning/AUTO_LAYOUT_INTEGRATION.md, Э2.

Правила из contract-разведки (FXML + WYSIWYG-редактор):
  * connector-конец: sp/tp ДОЛЖНЫ равняться centroid коннектора
    (FXML игнорирует source_point для connector-концов и берёт центроид).
  * узел из FIXED_SIZES (скин): конец на границе _skin_content_rect
    (letterbox-след графики внутри bbox), поперечная координата в пределах рамки.
  * узел с сегментацией без скина: конец на контуре. Луч идёт ВДОЛЬ оси
    прямизны, если она задана и пересекает контур, иначе — из центроида
    (см. `_poly_hit`).
  * прочие (скин вне FIXED_SIZES, rectangle-fallback): конец на границе bbox.
  * waypoints — только промежуточные точки [y, x], ключ обязан существовать.

Вызывать reseat_all_endpoints(graph) ПОСЛЕ расстановки узлов. Прямизна: если
ребро без waypoints и концы почти на одной оси — конец сажается на общую ось.

Лежит рядом с pretransform, а не в пакете layout: посадка одна на всю систему
(§3.4 плана), а импорт подмодуля layout тянул бы shapely, которого в
requirements/ui.txt нет.
"""
from __future__ import annotations

from .pretransform import (
    FIXED_SIZES,
    _skin_content_rect,
    project_ray_to_polygon,
)
from .graph_access import (edge_ends, edges, is_connector, node_cxy,
                           nodes_by_id)

STRAIGHT_TOL = 3.0   # px: концы почти на оси -> строгая прямая


def _anchor_rect(node):
    """Прямоугольник посадки: след скина для FIXED_SIZES, иначе bbox."""
    bb = node.get("bbox")
    if not bb or len(bb) != 4:
        return None
    if node.get("class_name") in FIXED_SIZES:
        return _skin_content_rect(node) or tuple(bb)
    return tuple(bb)


def _poly_hit(node, seg, toward_x, toward_y, lock):
    """Точка на контуре: луч ВДОЛЬ оси прямизны, иначе из центроида.

    Прежде луч всегда шёл из центроида в соседа, и ветка возвращалась РАНЬШЕ,
    чем проверялся `lock` (он работает только в прямоугольной ветке ниже).
    Для крупного контура это значит, что труба рисуется диагональю по
    построению: конец садится на прямую центроид->сосед, а центроид
    невыпуклого контура лежит далеко от места врезки. Замер c2f79462
    (деаэратор node_28, bbox 616x373, 13 рёбер): все 13 труб неортогональны
    на холсте отрисовки ДО всякой раскладки; с лучом вдоль оси — ноль.
    """
    cx, cy = node_cxy(node)
    if lock:
        xs, ys = seg[0::2], seg[1::2]
        if lock[0] == "H" and min(ys) <= lock[1] <= max(ys):
            pt = project_ray_to_polygon(seg, cx, lock[1], toward_x, lock[1])
            if pt:
                return pt
        elif lock[0] == "V" and min(xs) <= lock[1] <= max(xs):
            pt = project_ray_to_polygon(seg, lock[1], cy, lock[1], toward_y)
            if pt:
                return pt
    return project_ray_to_polygon(seg, cx, cy, toward_x, toward_y)


def node_anchor(node, toward_x, toward_y, lock=None):
    """Точка подключения на форме узла, обращённая к (toward_x, toward_y).

    lock: None | ('H', y) | ('V', x) — посадить на общую ось, если возможно.
    Возвращает (x, y).
    """
    if is_connector(node):
        return node_cxy(node)

    seg = node.get("segmentation")
    has_poly = bool(seg) and isinstance(seg, list) and len(seg) >= 6
    rect = _anchor_rect(node)

    if rect is None:
        if has_poly:
            pt = _poly_hit(node, seg, toward_x, toward_y, lock)
            return pt if pt else node_cxy(node)
        return node_cxy(node)

    # полигонный узел без скина: FXML эмитит контур
    if has_poly and node.get("class_name") not in FIXED_SIZES \
            and not node.get("_axis"):
        pt = _poly_hit(node, seg, toward_x, toward_y, lock)
        if pt:
            return pt

    x1, y1, x2, y2 = rect
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0

    if lock and lock[0] == "H" and y1 - 0.5 <= lock[1] <= y2 + 0.5:
        y = min(max(lock[1], y1), y2)
        return (x2 if toward_x >= cx else x1, y)
    if lock and lock[0] == "V" and x1 - 0.5 <= lock[1] <= x2 + 0.5:
        x = min(max(lock[1], x1), x2)
        return (x, y2 if toward_y >= cy else y1)

    dx, dy = toward_x - cx, toward_y - cy
    if abs(dx) >= abs(dy):
        return (x2 if dx > 0 else x1, min(max(toward_y, y1), y2))
    return (min(max(toward_x, x1), x2), y2 if dy > 0 else y1)


def poly_station_port(node, side, frac):
    """Порт станции равномерки Э10 на контуре полигонного узла без скина
    (ПОПРАВКА контракта, решение заказчика 2026-07-21): пересечение
    ПЕРПЕНДИКУЛЯРНОГО стороне луча на доле frac с контуром."""
    seg = node.get("segmentation")
    bb = node.get("bbox")
    if not seg or not isinstance(seg, list) or len(seg) < 6 or not bb:
        return None
    far = 10000.0
    if side in ("L", "R"):
        y = bb[1] + frac * (bb[3] - bb[1])
        o, t = ((bb[0] - far, y), (bb[2] + far, y)) if side == "R" \
            else ((bb[2] + far, y), (bb[0] - far, y))
    else:
        x = bb[0] + frac * (bb[2] - bb[0])
        o, t = ((x, bb[1] - far), (x, bb[3] + far)) if side == "B" \
            else ((x, bb[3] + far), (x, bb[1] - far))
    pt = project_ray_to_polygon(seg, o[0], o[1], t[0], t[1])
    return (float(pt[0]), float(pt[1])) if pt else None


def _poly_even_seat(node, e, key, ref, tol=1.5):
    """Станция Э10 из атрибутов ребра (_poly_side_*/_poly_frac_*), если
    подход ref уже перпендикулярен станции; иначе None (старый осевой луч)."""
    if node is None or e is None:
        return None
    side = e.get("_poly_side_" + key)
    frac = e.get("_poly_frac_" + key)
    if side is None or frac is None:
        return None
    bb = node.get("bbox")
    if not bb:
        return None
    if side in ("L", "R"):
        st = bb[1] + frac * (bb[3] - bb[1])
        if abs(ref[1] - st) > tol:
            return None
    else:
        st = bb[0] + frac * (bb[2] - bb[0])
        if abs(ref[0] - st) > tol:
            return None
    return poly_station_port(node, side, frac)


def _axis_range(node):
    """Диапазон, в котором может лежать общая ось через узел: (y1,y2,x1,x2)."""
    if is_connector(node):
        cx, cy = node_cxy(node)
        return (cy, cy, cx, cx)
    rect = _anchor_rect(node)
    if rect is None:
        cx, cy = node_cxy(node)
        return (cy, cy, cx, cx)
    x1, y1, x2, y2 = rect
    return (y1, y2, x1, x2)


def reseat_edge(byid, e, straight_tol=STRAIGHT_TOL):
    src = byid.get(e.get("source") or e.get("from"))
    tgt = byid.get(e.get("target") or e.get("to"))
    if not src or not tgt:
        return
    wps = e.get("waypoints") or []
    scx, scy = node_cxy(src)
    tcx, tcy = node_cxy(tgt)

    # опорные точки направления
    s_ref = (wps[0][1], wps[0][0]) if wps else (tcx, tcy)
    t_ref = (wps[-1][1], wps[-1][0]) if wps else (scx, scy)

    s_lock = t_lock = None
    if not wps:
        sy1, sy2, sx1, sx2 = _axis_range(src)
        ty1, ty2, tx1, tx2 = _axis_range(tgt)
        # H-прямая: диапазоны Y пересекаются (с допуском)
        ylo, yhi = max(sy1, ty1), min(sy2, ty2)
        xlo, xhi = max(sx1, tx1), min(sx2, tx2)
        h_lock = v_lock = None
        if ylo - yhi <= straight_tol and abs(scy - tcy) <= \
                (sy2 - sy1) / 2 + (ty2 - ty1) / 2 + straight_tol:
            y = _pick_axis_coord(src, tgt, ylo, yhi, axis="H")
            if y is not None:
                h_lock = ("H", y)
        if xlo - xhi <= straight_tol and abs(scx - tcx) <= \
                (sx2 - sx1) / 2 + (tx2 - tx1) / 2 + straight_tol:
            x = _pick_axis_coord(src, tgt, xlo, xhi, axis="V")
            if x is not None:
                v_lock = ("V", x)
        # Э2: ось — от реальной геометрии связи, не от порядка веток.
        # Прежний код брал H всегда, когда H достижима; у коннектора
        # диапазон вырожден в точку, и когда его координата попадала в
        # окно рамки соседа, V-ветка была недостижима — конец садился на
        # боковую грань при вертикальной связи (паттерн «зазор форм 0.00,
        # нарисовано 10.50», §15 стенда). Правило сужено до корня бага:
        # геометрия решает только в парах С КОННЕКТОРОМ; у пары блоков
        # оба диапазона широкие, прежний H-приоритет там осмыслен, а его
        # смена двигает оси решателя по всему корпусу (замер: 610→22
        # против 610→16, дефекты +3 на 51b и a6d — не-Парето).
        if h_lock and v_lock and (is_connector(src) or is_connector(tgt)) \
                and abs(tcy - scy) > abs(tcx - scx):
            s_lock = t_lock = v_lock
        else:
            s_lock = t_lock = h_lock or v_lock
    else:
        # конец сажаем на ось первого/последнего сегмента, если он H/V
        sp = e.get("source_point")
        tp = e.get("target_point")
        if sp is not None:
            s_lock = _seg_lock((sp[1], sp[0]), s_ref)
        if tp is not None:
            t_lock = _seg_lock((tp[1], tp[0]), t_ref)

    sp2 = _poly_even_seat(src, e, "s", s_ref)
    tp2 = _poly_even_seat(tgt, e, "t", t_ref)
    sx, sy = sp2 if sp2 else node_anchor(src, s_ref[0], s_ref[1], s_lock)
    tx, ty = tp2 if tp2 else node_anchor(tgt, t_ref[0], t_ref[1], t_lock)
    e["source_point"] = [sy, sx]
    e["target_point"] = [ty, tx]
    if "waypoints" not in e or e["waypoints"] is None:
        e["waypoints"] = []


def _pick_axis_coord(src, tgt, lo, hi, axis):
    """Координата общей оси. Центроид коннектора — жёсткая точка."""
    s_conn, t_conn = is_connector(src), is_connector(tgt)
    scx, scy = node_cxy(src)
    tcx, tcy = node_cxy(tgt)
    s_val = scy if axis == "H" else scx
    t_val = tcy if axis == "H" else tcx
    if s_conn and t_conn:
        return s_val if abs(s_val - t_val) <= 0.75 else None
    if s_conn:
        return s_val if lo - 0.75 <= s_val <= hi + 0.75 else None
    if t_conn:
        return t_val if lo - 0.75 <= t_val <= hi + 0.75 else None
    if lo > hi:
        return None
    mid = (s_val + t_val) / 2.0
    return min(max(mid, lo), hi)


def _seg_lock(endpoint_xy, ref_xy, tol=1.0):
    ex, ey = endpoint_xy
    rx, ry = ref_xy
    if abs(ey - ry) <= tol:
        return ("H", ry)
    if abs(ex - rx) <= tol:
        return ("V", rx)
    return None


def straight_slack_lock(src, tgt, anchor_x, anchor_y, straight_tol=STRAIGHT_TOL):
    """Замок прямизны для посадки ОДНОГО конца при неподвижном втором
    (drag adjusting=End, Э3): условия слабины — те же, что в `reseat_edge`
    для рёбер без waypoints, но ось проходит через ЯКОРЬ (неподвижный
    дальний конец), а не через середину пары. Дальний конец не двигается,
    ближний садится на его ось: почти-соосная пара даёт строго прямую
    трубу с концом на грани (не лучом в угол). -> ('H', y)|('V', x)|None.

    `reseat_edge` сознательно НЕ переведён на этот хелпер: его ось — mid
    пары (обоюдная пересадка), и любая правка меняет бит-эталон корпуса.
    """
    scx, scy = node_cxy(src)
    tcx, tcy = node_cxy(tgt)
    sy1, sy2, sx1, sx2 = _axis_range(src)
    ty1, ty2, tx1, tx2 = _axis_range(tgt)
    ylo, yhi = max(sy1, ty1), min(sy2, ty2)
    xlo, xhi = max(sx1, tx1), min(sx2, tx2)
    # Ось обязана реально накрываться гранью сажаемого узла: иначе
    # node_anchor клампит координату в край диапазона — конец в УГЛУ и
    # чуть косая «прямая» (скрин заказчика 2026-07-31). За пределами
    # грани честнее луч/маршрут, чем угол.
    h = (ylo - yhi <= straight_tol and abs(scy - tcy) <=
         (sy2 - sy1) / 2 + (ty2 - ty1) / 2 + straight_tol
         and sy1 - 0.5 <= anchor_y <= sy2 + 0.5)
    v = (xlo - xhi <= straight_tol and abs(scx - tcx) <=
         (sx2 - sx1) / 2 + (tx2 - tx1) / 2 + straight_tol
         and sx1 - 0.5 <= anchor_x <= sx2 + 0.5)
    if h and v:
        # как в Э2: при двух достижимых осях — доминирующее направление
        if abs(tcx - scx) >= abs(tcy - scy):
            v = False
        else:
            h = False
    if h:
        return ("H", anchor_y)
    if v:
        return ("V", anchor_x)
    return None


def reseat_all_endpoints(graph, straight_tol=STRAIGHT_TOL):
    byid = nodes_by_id(graph)
    for e in edges(graph):
        reseat_edge(byid, e, straight_tol)

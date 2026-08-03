# -*- coding: utf-8 -*-
"""edit_checks.py — судья геометрии холста «Ручной правки» (Э0 пересборки).

Предикаты дефектов, которые видит глаз оператора: концы в углах рамки,
диагональные и почти-осевые сегменты, пробеги вдоль границы своего/чужого
узла, прошивание нутра чужого узла, классификация посадки концов.

Правило проекта «сторож == судья»: эти же функции обязаны использовать
судья (tools/edit_bench.py) и движок редактирования (edit_engine, Э2+) —
импорт, не копия. Чистый stdlib: shapely/numpy в requirements/ui.txt нет.

Две системы отсчёта — сознательно:
  * посадочные проверки (corner_ends, end_classes) меряют по РЕДАКТОРСКОЙ
    рамке посадки `seat_rect` (bbox, скины включительно — «символ тянется
    на рамку», 2026-08-01);
  * препятственные (through_box, along_border) — по ВИЗУАЛЬНОЙ форме:
    реальный контур, где он есть (segmentation, класс вне FIXED_SIZES),
    иначе bbox. Урок node_28: ложный гигант `unknow` 616x374 по bbox даёт
    15 фантомных прошиваний, по контуру — 0.

Соглашения координат (CODING_GUIDE §6):
    centroid / source_point / target_point / waypoints = [y, x]
    bbox = [x1, y1, x2, y2];  segmentation = [x, y, x, y, ...]
Вся внутренняя математика — в (x, y).
"""
from __future__ import annotations

from .graph_access import edge_ends, edge_polyline, edges, is_connector, \
    node_cxy, nodes_by_id
from .seating import FIXED_SIZES
from . import ports as port_model

# Пороги — согласованы с существующими судьями:
# floor=6/угловой допуск у _gate._orient, STRAIGHT_TOL=3 у канона,
# клиренс маршрута ROUTE_CLEARANCE=6 у drag-роутера.
DIAG_TOL = 1.5        # отклонение от оси больше — диагональ
NEAR_MIN = 0.25       # отклонение меньше — числовой шум, не дефект
SEG_FLOOR = 6.0       # сегменты короче не судим (мосты/стабы датчиков)
CORNER_TOL = 1.0      # конец «в углу» рамки посадки
CORNER_NEAR = 4.0     # конец «у угла» (жалоба заказчика ловится и так)
CLEARANCE = 6.0       # пробег вдоль границы ближе — прилипание
OVERLAP_MIN = 8.0     # минимальный пробег вдоль чужой границы, чтобы судить
EXIT_RUN = 12.0       # выйдя из своего узла, труба обязана отойти за столько
AXIS_TOL = 1.5        # сегмент считается осевым при отклонении до этого
SAMPLE_STEP = 2.0     # шаг сэмплирования сегмента при замере прилегания
BOX_SHRINK = 1.0      # усадка bbox перед тестом прошивания (касание легально)
MID_TOL = 1.0         # конец «в середине грани»
PORT_TOL = 0.75       # совпадение с ручным портом (== ports.PORT_MATCH_TOL)


# ---------------------------------------------------------------- полигоны

def pt_in_polygon(px: float, py: float, pts: list) -> bool:
    """Ray-casting: точка (px, py) внутри полигона [(x, y), ...]."""
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


def seg_pierces_polygon(ax: float, ay: float,
                        bx: float, by: float, seg: list) -> bool:
    """Сегмент (ax,ay)-(bx,by) прошивает РЕАЛЬНЫЙ контур (плоский [x,y,...])?

    Прошивание = строгое пересечение с ребром контура ИЛИ середина сегмента
    внутри полигона (сегмент целиком в нутре). Касание контура (скользящий
    коллинеарный сегмент, конец на контуре) прошиванием не считается.
    Проход над ПУСТЫМ углом габарита невыпуклого контура легален — решение
    проекта: наложения судятся по реальной форме, не по bbox."""
    pts = list(zip(seg[0::2], seg[1::2]))
    if len(pts) < 3:
        return False
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if max(ax, bx) < min(xs) or min(ax, bx) > max(xs) \
            or max(ay, by) < min(ys) or min(ay, by) > max(ys):
        return False

    def cross(ox, oy, px, py, qx, qy):
        return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)

    n = len(pts)
    for i in range(n):
        cx1, cy1 = pts[i]
        cx2, cy2 = pts[(i + 1) % n]
        d1 = cross(ax, ay, bx, by, cx1, cy1)
        d2 = cross(ax, ay, bx, by, cx2, cy2)
        d3 = cross(cx1, cy1, cx2, cy2, ax, ay)
        d4 = cross(cx1, cy1, cx2, cy2, bx, by)
        if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) \
                and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
            return True
    return pt_in_polygon((ax + bx) / 2.0, (ay + by) / 2.0, pts)


def poly_contour(node) -> list | None:
    """Реальный контур узла-препятствия [(x, y), ...] или None.

    Семантика препятствия (== `_poly_obstacle` редактора): контур есть и
    класс вне словаря FIXED_SIZES — форму судим контуром, не bbox."""
    seg = node.get("segmentation")
    if seg and isinstance(seg, list) and len(seg) >= 6 \
            and node.get("class_name") not in FIXED_SIZES:
        return list(zip(seg[0::2], seg[1::2]))
    return None


def _contour_seated(node) -> bool:
    """Конец узла сидит на контуре (семантика ПОСАДКИ канона seating)."""
    if node is None or is_connector(node):
        return False
    seg = node.get("segmentation")
    return bool(seg and isinstance(seg, list) and len(seg) >= 6
                and node.get("class_name") not in FIXED_SIZES
                and not node.get("_axis"))


def seat_rect(node):
    """РЕДАКТОРСКАЯ рамка посадки: весь bbox, включая скины.

    Решение заказчика 2026-08-01 («символ тянется на рамку», репро
    graph_edited_3edge): скин рисуется растянутым на bbox, как контрол в
    FXML/SceneBuilder, и конец трубы сидит на рамке. Letterbox-канон
    (`seating._anchor_rect` -> _skin_content_rect) остаётся СЕРВЕРНЫМ
    (бит-эталон раскладки); перевод сервера — отдельный этап с
    пере-EXPECT. Возвращает (x1, y1, x2, y2) или None."""
    if node is None:
        return None
    bb = node.get("bbox")
    if not bb or len(bb) != 4:
        return None
    return tuple(float(v) for v in bb)


def _rect_seated(node) -> bool:
    """Конец узла сидит на рамке посадки, а не на контуре."""
    if node is None or is_connector(node):
        return False
    if _contour_seated(node):
        return False
    return seat_rect(node) is not None


def _dist_to_contour(px: float, py: float, pts: list) -> float:
    """Расстояние от точки до ломаной контура (замкнутой)."""
    best = float("inf")
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        ln2 = dx * dx + dy * dy
        if ln2 <= 1e-12:
            d = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ln2))
            qx, qy = ax + t * dx, ay + t * dy
            d = ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5
        if d < best:
            best = d
    return best


def on_rect_border(rect, x: float, y: float, tol: float = 0.5) -> bool:
    """Точка лежит на периметре прямоугольника (x1, y1, x2, y2), tol px.

    Допуск тот же, что у канона (point_on_polygon / seat_violations §3.2)."""
    x1, y1, x2, y2 = rect
    in_x = x1 - tol <= x <= x2 + tol
    in_y = y1 - tol <= y <= y2 + tol
    on_v = in_y and (abs(x - x1) <= tol or abs(x - x2) <= tol)
    on_h = in_x and (abs(y - y1) <= tol or abs(y - y2) <= tol)
    return on_v or on_h


# ------------------------------------------------------------ конец в углу

def corner_ends(graph, tol: float = CORNER_TOL) -> list[dict]:
    """Концы рёбер в углах рамки посадки.

    Угол — обе координаты конца на границах `seat_rect` (tol px; с
    2026-08-01 рамка редактора = bbox, скины включительно).
    Полигонные узлы без скина не меряются: их конец сидит на контуре,
    у контура нет «угла рамки». (Логика == tools/corner_probe.corner_ends.)"""
    byid = nodes_by_id(graph)
    out = []
    for e in edges(graph):
        s, t = edge_ends(e)
        for nid, key in ((s, "source_point"), (t, "target_point")):
            p = e.get(key)
            node = byid.get(nid)
            if p is None or not _rect_seated(node):
                continue
            x1, y1, x2, y2 = seat_rect(node)
            x, y = float(p[1]), float(p[0])
            dc = max(min(abs(x - x1), abs(x - x2)),
                     min(abs(y - y1), abs(y - y2)))
            if min(abs(x - x1), abs(x - x2)) <= tol \
                    and min(abs(y - y1), abs(y - y2)) <= tol:
                out.append({"edge": e.get("id"), "node": nid, "key": key,
                            "x": round(x, 2), "y": round(y, 2),
                            "d_corner": round(dc, 2)})
    return out


# ----------------------------------------------------- диагонали / почти-оси

def diag_segments(graph, tol: float = DIAG_TOL,
                  floor: float = SEG_FLOOR) -> dict:
    """Сегменты полилиний вне осей.

    dev = min(|dx|, |dy|) — отклонение от ближайшей оси в px.
      dev > tol            -> 'diag'  (диагональ, дефект);
      NEAR_MIN < dev <= tol -> 'near' (почти-ось: глаз видит, пороги прощают
                                       — цель snap_write Э3 и микро-доводки).
    Сегменты короче floor по обеим осям не судятся (мосты, стабы датчиков)."""
    diag, near = [], []
    max_dev = 0.0
    for e in edges(graph):
        pts = edge_polyline(e)
        for i in range(len(pts) - 1):
            (ax, ay), (bx, by) = pts[i], pts[i + 1]
            adx, ady = abs(bx - ax), abs(by - ay)
            if max(adx, ady) < floor:
                continue
            dev = min(adx, ady)
            if dev <= NEAR_MIN:
                continue
            item = {"edge": e.get("id"), "seg": i, "dev": round(dev, 2),
                    "len": round(max(adx, ady), 1),
                    "a": (round(ax, 1), round(ay, 1)),
                    "b": (round(bx, 1), round(by, 1))}
            if dev > tol:
                diag.append(item)
            else:
                near.append(item)
            if dev > max_dev:
                max_dev = dev
    diag.sort(key=lambda d: -d["dev"])
    near.sort(key=lambda d: -d["dev"])
    return {"diag": diag, "near": near, "max_dev": round(max_dev, 2)}


# -------------------------------------------------------- вдоль границы

def _node_border(node) -> list | None:
    """Граница визуальной формы узла как замкнутая ломаная [(x, y), ...]."""
    cont = poly_contour(node)
    if cont:
        return cont
    bb = node.get("bbox")
    if bb and len(bb) == 4:
        x1, y1, x2, y2 = bb
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    return None


def _hug_runs(ax, ay, bx, by, border, clearance, step=SAMPLE_STEP):
    """Сэмплирование сегмента: список пробегов ближе клиренса к границе.

    Возвращает [(t_start_px, run_px, min_gap), ...] по расстоянию до
    ломаной границы (контур любой ориентации, не только осевые грани)."""
    ln = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
    if ln < step:
        return []
    n = max(2, int(ln / step) + 1)
    runs = []
    run_start, min_gap = None, None
    for i in range(n + 1):
        t = min(1.0, i / n)
        d = _dist_to_contour(ax + (bx - ax) * t, ay + (by - ay) * t, border)
        if d < clearance:
            if run_start is None:
                run_start, min_gap = t, d
            elif d < min_gap:
                min_gap = d
        elif run_start is not None:
            runs.append((run_start * ln, (t - run_start) * ln, min_gap))
            run_start = None
    if run_start is not None:
        runs.append((run_start * ln, (1.0 - run_start) * ln, min_gap))
    return runs


def along_border(graph, clearance: float = CLEARANCE,
                 overlap_min: float = OVERLAP_MIN,
                 exit_run: float = EXIT_RUN) -> list[dict]:
    """Пробеги рёбер вдоль границы узла ближе клиренса.

    Определение заказчика (2026-07-31): «вдоль» — ребро ВЫШЛО из узла
    (из угла или середины грани — неважно) и не отошло от него, а бежит
    вдоль его же границы. Правило: терминальный сегмент обязан отойти от
    границы СВОЕГО узла в пределах exit_run px (перпендикулярный выход
    отходит за clearance px и легален по построению). Прочие случаи —
    пробег ближе клиренса длиной от overlap_min вдоль своей или чужой
    границы (жёлтая полоса судьи). Расстояние меряется до реальной
    границы (контур любой ориентации, не осевые грани bbox)."""
    out = []
    borders = []
    for n in graph.get("nodes", []):
        if is_connector(n):
            continue
        b = _node_border(n)
        if not b:
            continue
        xs = [p[0] for p in b]
        ys = [p[1] for p in b]
        borders.append((n["id"], b, (min(xs) - clearance, min(ys) - clearance,
                                     max(xs) + clearance, max(ys) + clearance)))
    for e in edges(graph):
        s, t = edge_ends(e)
        pts = edge_polyline(e)
        nseg = len(pts) - 1
        for i in range(nseg):
            (ax, ay), (bx, by) = pts[i], pts[i + 1]
            sx1, sy1 = min(ax, bx) - 0.1, min(ay, by) - 0.1
            sx2, sy2 = max(ax, bx) + 0.1, max(ay, by) + 0.1
            for nid, border, (rx1, ry1, rx2, ry2) in borders:
                if sx2 < rx1 or sx1 > rx2 or sy2 < ry1 or sy1 > ry2:
                    continue
                own = nid in (s, t)
                # терминальный сегмент своего узла судим от точки выхода
                term_fwd = own and i == 0 and nid == s
                term_bwd = own and i == nseg - 1 and nid == t
                if term_bwd:                      # мерим от конца — развернём
                    runs = _hug_runs(bx, by, ax, ay, border, clearance)
                elif term_fwd:
                    runs = _hug_runs(ax, ay, bx, by, border, clearance)
                else:
                    runs = _hug_runs(ax, ay, bx, by, border, clearance)
                for start_px, run_px, gap in runs:
                    if (term_fwd or term_bwd) and start_px < SAMPLE_STEP:
                        # пробег от самой точки выхода: отход обязан
                        # случиться в пределах exit_run
                        if run_px < exit_run:
                            continue
                    elif run_px < overlap_min:
                        continue
                    out.append({"edge": e.get("id"), "seg": i, "node": nid,
                                "own": own, "gap": round(gap, 2),
                                "overlap": round(run_px, 1),
                                "a": (round(ax, 1), round(ay, 1)),
                                "b": (round(bx, 1), round(by, 1))})
                    break                       # одного попадания на узел хватит
    # один физический дефект = одна находка: прилегание, разрезанное
    # изломами полилинии на куски, склеивается по паре (ребро, узел)
    merged = {}
    for d in out:
        k = (d["edge"], d["node"])
        m = merged.get(k)
        if m is None:
            merged[k] = dict(d)
        else:
            m["overlap"] = round(m["overlap"] + d["overlap"], 1)
            if d["gap"] < m["gap"]:
                m["gap"], m["seg"] = d["gap"], d["seg"]
                m["a"], m["b"] = d["a"], d["b"]
    out = list(merged.values())
    out.sort(key=lambda d: (d["gap"], -d["overlap"]))
    return out


# ------------------------------------------------------- сквозь чужой узел

def _seg_clip_rect(ax, ay, bx, by, x1, y1, x2, y2):
    """Длина куска сегмента внутри прямоугольника (Liang-Barsky), 0 если нет."""
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax - x1), (dx, x2 - ax), (-dy, ay - y1), (dy, y2 - ay)):
        if abs(p) < 1e-12:
            if q < 0:
                return 0.0
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return 0.0
            if r > t0:
                t0 = r
        else:
            if r < t0:
                return 0.0
            if r < t1:
                t1 = r
    if t1 <= t0:
        return 0.0
    return ((dx * (t1 - t0)) ** 2 + (dy * (t1 - t0)) ** 2) ** 0.5


def through_box(graph, shrink: float = BOX_SHRINK) -> list[dict]:
    """Рёбра, прошивающие нутро ЧУЖОГО узла.

    Узел с реальным контуром судится контуром (node_28: по bbox 15 фантомов,
    по контуру 0), остальные — bbox, усаженным на shrink (касание легально).
    Свои концевые узлы ребра исключены. Пересечение труба×труба — мост,
    здесь не судится."""
    byid = nodes_by_id(graph)
    obstacles = []
    for n in graph.get("nodes", []):
        if is_connector(n):
            continue
        cont = poly_contour(n)
        if cont:
            flat = [c for p in cont for c in p]
            xs = [p[0] for p in cont]
            ys = [p[1] for p in cont]
            obstacles.append((n["id"], "poly", flat,
                              (min(xs), min(ys), max(xs), max(ys))))
        else:
            bb = n.get("bbox")
            if bb and len(bb) == 4 and bb[2] - bb[0] > 2 * shrink \
                    and bb[3] - bb[1] > 2 * shrink:
                r = (bb[0] + shrink, bb[1] + shrink,
                     bb[2] - shrink, bb[3] - shrink)
                obstacles.append((n["id"], "rect", None, r))
    out = []
    for e in edges(graph):
        s, t = edge_ends(e)
        pts = edge_polyline(e)
        hits = {}
        for i in range(len(pts) - 1):
            (ax, ay), (bx, by) = pts[i], pts[i + 1]
            sx1, sy1 = min(ax, bx), min(ay, by)
            sx2, sy2 = max(ax, bx), max(ay, by)
            for nid, kind, flat, (rx1, ry1, rx2, ry2) in obstacles:
                if nid in (s, t):
                    continue
                if sx2 < rx1 or sx1 > rx2 or sy2 < ry1 or sy1 > ry2:
                    continue
                if kind == "poly":
                    if seg_pierces_polygon(ax, ay, bx, by, flat):
                        hits[nid] = hits.get(nid, 0.0) + 1.0
                else:
                    ln = _seg_clip_rect(ax, ay, bx, by, rx1, ry1, rx2, ry2)
                    if ln > 0.5:
                        hits[nid] = hits.get(nid, 0.0) + ln
        for nid, ln in hits.items():
            out.append({"edge": e.get("id"), "node": nid,
                        "inside_len": round(ln, 1)})
    out.sort(key=lambda d: -d["inside_len"])
    return out


# ------------------------------------------------------ классификация концов

def end_classes(graph, mid_tol: float = MID_TOL,
                port_tol: float = PORT_TOL) -> dict:
    """Куда сел каждый конец: словарь класс -> список находок.

    Классы: conn_ok/conn_off (коннектор == центроид), poly_ok/poly_off
    (контурная посадка), corner / manual / midpoint / side / adrift
    (рамочная посадка). После Э2 (слоты) 'side' разделится на slot/side."""
    byid = nodes_by_id(graph)
    res = {k: [] for k in ("conn_ok", "conn_off", "poly_ok", "poly_off",
                           "corner", "manual", "midpoint", "side", "adrift")}
    for e in edges(graph):
        s, t = edge_ends(e)
        for nid, key in ((s, "source_point"), (t, "target_point")):
            p = e.get(key)
            node = byid.get(nid)
            if p is None or node is None:
                continue
            x, y = float(p[1]), float(p[0])
            item = {"edge": e.get("id"), "node": nid, "key": key,
                    "x": round(x, 2), "y": round(y, 2)}
            if is_connector(node):
                cx, cy = node_cxy(node)
                cls = "conn_ok" if abs(x - cx) <= 0.5 and abs(y - cy) <= 0.5 \
                    else "conn_off"
                res[cls].append(item)
                continue
            if _contour_seated(node):
                cont = poly_contour(node) or []
                d = _dist_to_contour(x, y, cont) if cont else float("inf")
                res["poly_ok" if d <= port_tol else "poly_off"].append(item)
                continue
            rect = seat_rect(node)
            if rect is None:
                continue
            x1, y1, x2, y2 = rect
            on_v = min(abs(x - x1), abs(x - x2)) <= 1.0 \
                and y1 - 1.0 <= y <= y2 + 1.0
            on_h = min(abs(y - y1), abs(y - y2)) <= 1.0 \
                and x1 - 1.0 <= x <= x2 + 1.0
            if not on_v and not on_h:
                res["adrift"].append(item)
                continue
            if on_v and on_h:
                res["corner"].append(item)
                continue
            if port_model.pinned_on_node(node, e) is not None \
                    or (port_model.is_on_port(node, x, y, tol=port_tol)
                        and node.get("_ports")):
                res["manual"].append(item)
                continue
            if on_v:
                off = abs(y - (y1 + y2) / 2.0)
            else:
                off = abs(x - (x1 + x2) / 2.0)
            res["midpoint" if off <= mid_tol else "side"].append(item)
    return res


# --------------------------------------------------------------- агрегатор

def check_canvas(graph, clearance: float = CLEARANCE) -> dict:
    """Полный прогон судьи по холсту: счётчики + находки."""
    dg = diag_segments(graph)
    al = along_border(graph, clearance=clearance)
    th = through_box(graph)
    cr = corner_ends(graph)
    cr_near = corner_ends(graph, tol=CORNER_NEAR)
    ec = end_classes(graph)
    counts = {
        "diag": len(dg["diag"]),
        "near_ortho": len(dg["near"]),
        "max_dev": dg["max_dev"],
        "along_own": sum(1 for a in al if a["own"]),
        "along_foreign": sum(1 for a in al if not a["own"]),
        "through": len(th),
        "corner": len(cr),
        "corner_near": len(cr_near),
        "conn_off": len(ec["conn_off"]),
        "poly_off": len(ec["poly_off"]),
        "adrift": len(ec["adrift"]),
        "midpoint": len(ec["midpoint"]),
        "side": len(ec["side"]),
        "manual": len(ec["manual"]),
    }
    return {"counts": counts,
            "findings": {"diag": dg["diag"], "near": dg["near"],
                         "along": al, "through": th, "corner": cr,
                         "corner_near": cr_near, "ends": ec}}

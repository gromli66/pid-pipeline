# -*- coding: utf-8 -*-
"""edit_smooth.py — Э4: адресное сглаживание после ручных правок.

Решение заказчика 2026-08-02: «оно будет сглаживать мелкие зигзаги и
диагонали после ручных правок»; двигать оборудование «можно, но
ограниченно — если ступенька одна или её легко превратить в прямую без
супер движений, то есть чуть сдвинуть коннектор, узел».

ФОРМА (задана замерами, а не вкусом — см. ниже): не решатель по всему
листу, а ЛЕСТНИЦА ЛЕКАРСТВ от дешёвого к дорогому, применяемая АДРЕСНО по
списку дефектов судьи, где каждый ход проходит гейт и откатывается, если
стало хуже.

  1. КОЛЕНО — маршрут вместо косой. Не двигает ничего. На холстах
     заказчика закрывает 41 диагональ из 46 (89%);
  2. СКОЛЬЖЕНИЕ КОНЦА по своей грани/участку — двигается только точка
     стыка, оборудование стоит;
  3. СДВИГ КОННЕКТОРА на величину ступеньки — коннектор это точка, у него
     нет ни формы, ни идентичности; таких случаев больше половины
     (68 дефектов из 124 имеют коннектор на конце);
  4. СДВИГ УЗЛА — последнее средство, в пределах бюджета.

Замер по 19 холстам заказчика (tools/smooth_bench.py повторяет):
  * диагонали (2 точки): 46 шт., смещение медиана 62px — им нужно КОЛЕНО,
    двигать узлы на такое нельзя;
  * зигзаги с ОДНОЙ ступенькой: 60 шт., смещение медиана 12px, 75% <= 14,
    максимум 22 — ровно зона «чуть сдвинуть». Отсюда BUDGET = 22.
  * многоступенчатые: 13 шт. — в автомат НЕ берутся (ходов несколько,
    выравнивание связано: выпрямляя одну трубу, сбиваешь соседнюю —
    замерено 4->5 и 4->6 на серверном корпусе), уходят в очаги оператору.

ПОЧЕМУ НЕ ГЛОБАЛЬНЫЙ РЕШАТЕЛЬ: замер пяти движков (наш vpsc, cola с
ограничениями выравнивания, graphviz prism/voronoi/vpsc/ipsep) на 7 разных
чертежах — НИ ОДИН не улучшил ортогональность, лучший результат у «ничего
не трогать». Подробности — в истории сессии 2026-08-02.

НЕПРИКОСНОВЕННОЕ (полный список, «со всех сторон»):
  * ЯКОРЬ ВХОДА (`ports.pinned_port`) — точка, которую поставил оператор.
    Не скользит, не едет, и узел-владелец не двигается, если это порвало бы
    якорь. Проверяется у ОБОИХ концов каждого кандидата;
  * КОННЕКТОР как посадка: его конец — всегда центроид (канон), поэтому
    «скольжение конца» к нему неприменимо: двигается сам коннектор;
  * КОРОТКИЕ РЁБРА-ДАТЧИКИ (< SEG_FLOOR): структурный пол, не судятся;
  * ТОПОЛОГИЯ: ни одно лекарство не меняет состав рёбер и узлов;
  * ХОЛСТ 1920x1080: выход за границу отменяет ход.

Чистый stdlib: ни Qt, ни shapely. Роутинг приходит колбэком `route_fn`
(редактор отдаёт свою лестницу), потому что он живёт в UI-слое.
"""
from __future__ import annotations

import math
from copy import deepcopy

from . import edit_checks as ec
from . import ports as port_model
from .graph_access import edges as g_edges, is_connector, nodes_by_id

# ── пороги ─────────────────────────────────────────────────────────────
BUDGET = 22.0        # px: максимум «чуть сдвинуть». Выбран замером и
                     # утверждён заказчиком 2026-08-02: у ступенек его
                     # холстов максимум ровно 21.6px, то есть 22 закрывает
                     # ВСЕ, оставаясь меньше клетки сетки (24). Сравнение на
                     # корпусе из 20 холстов: 16px -> ступенек 96->51,
                     # 22px -> 96->40, побочных ухудшений нет в обоих.
STEP_MAX = 24.0      # px: длиннее — это осмысленный изгиб, а не ступенька
FACE_MARGIN = 6.0    # px: конец не подходит к углу грани ближе (C2: угол
                     # непредставим) — как VERTEX_MARGIN у контуров
OCCUPIED_TOL = 2.0   # px: ближе — считается занятым соседним концом
CANVAS_W, CANVAS_H = 1920.0, 1080.0
MAX_ROUNDS = 4       # проходов жадного цикла (сходится за 2-4 по замеру)


# ── геометрия рёбер ────────────────────────────────────────────────────
def edge_pts(e):
    """Полилиния ребра в (x, y) или None."""
    sp, tp = e.get("source_point"), e.get("target_point")
    if not sp or not tp:
        return None
    out = [(float(sp[1]), float(sp[0]))]
    for w in e.get("waypoints") or []:
        out.append((float(w[1]), float(w[0])))
    out.append((float(tp[1]), float(tp[0])))
    return out


def _ortho_seg(a, b, tol=ec.DIAG_TOL):
    return min(abs(b[0] - a[0]), abs(b[1] - a[1])) <= tol


def is_ortho(e, tol=ec.DIAG_TOL):
    pts = edge_pts(e)
    if not pts:
        return True
    return all(_ortho_seg(a, b, tol) for a, b in zip(pts, pts[1:]))


def diag_segments_of(e):
    """[(индекс, отклонение)] косых сегментов ребра (короткие не судятся)."""
    pts = edge_pts(e)
    if not pts:
        return []
    out = []
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        if math.hypot(b[0] - a[0], b[1] - a[1]) < ec.SEG_FLOOR:
            continue
        dev = min(abs(b[0] - a[0]), abs(b[1] - a[1]))
        if dev > ec.DIAG_TOL:
            out.append((i, dev))
    return out


def single_step(e, step_max=STEP_MAX):
    """Зигзаг с ОДНОЙ ступенькой -> (ось, смещение d) или None.

    Ступенька — короткий сегмент между двумя ПАРАЛЛЕЛЬНЫМИ: труба идёт,
    делает шажок вбок и идёт дальше в ту же сторону. Ось — та, вдоль
    которой надо сдвинуть, чтобы шажок исчез ('x' | 'y')."""
    pts = edge_pts(e)
    if not pts or len(pts) < 4:
        return None
    found = None
    for i in range(1, len(pts) - 2):
        a, b, c, d = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        L = math.hypot(c[0] - b[0], c[1] - b[1])
        if L > step_max or L < 1e-6:
            continue
        horiz = (abs(a[1] - b[1]) <= ec.DIAG_TOL
                 and abs(c[1] - d[1]) <= ec.DIAG_TOL)
        vert = (abs(a[0] - b[0]) <= ec.DIAG_TOL
                and abs(c[0] - d[0]) <= ec.DIAG_TOL)
        if not (horiz or vert):
            continue
        if found is not None:
            return None                     # ступенек больше одной — не наш
        # горизонтальные соседи => шажок по y, сдвигать надо вдоль y
        found = ("y" if horiz else "x", abs(c[1] - b[1]) if horiz
                 else abs(c[0] - b[0]))
    return found


def shift_target(e, step_max=STEP_MAX):
    """Что и на сколько сдвинуть, чтобы труба стала прямой -> (ось, d).

    Два случая, оба — «легко превратить в прямую» в формулировке заказчика:
      * зигзаг с ОДНОЙ ступенькой: d — смещение шажка;
      * ПРЯМАЯ ДИАГОНАЛЬ (2 точки): d — её отклонение от оси; сдвигать надо
        вдоль той оси, по которой концы разошлись меньше.
    Многоступенчатые и косые внутри длинных маршрутов не возвращаются —
    они уходят оператору (связанность, см. шапку модуля).
    """
    step = single_step(e, step_max)
    if step is not None:
        return step
    pts = edge_pts(e)
    if not pts or len(pts) != 2:
        return None
    a, b = pts
    if math.hypot(b[0] - a[0], b[1] - a[1]) < ec.SEG_FLOOR:
        return None                          # ребро-датчик: структурный пол
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    if min(dx, dy) <= ec.DIAG_TOL:
        return None
    return ("x", dx) if dx < dy else ("y", dy)


# ── классификация концов ───────────────────────────────────────────────
def end_kind(node, e):
    """Как посажен конец ребра e на узле node.

    'anchor'    — якорь оператора (неприкосновенен);
    'connector' — точка-центроид (двигается сам коннектор);
    'contour'   — точка на контурном участке;
    'rect'      — порт/слот на грани рамки;
    'none'      — узла нет либо посадка неизвестна.
    """
    if node is None:
        return "none"
    if port_model.pinned_port(node, e) is not None:
        return "anchor"
    if is_connector(node):
        return "connector"
    if ec._contour_seated(node):
        return "contour"
    if ec._rect_seated(node):
        return "rect"
    return "none"


def _face_of(node, pt):
    """Грань рамки, на которой лежит точка: ('L'|'R', x) / ('T'|'B', y)."""
    r = ec.seat_rect(node)
    if not r:
        return None
    x, y = pt
    tol = 1.0
    if abs(x - r[0]) <= tol:
        return ("L", r[1], r[3])
    if abs(x - r[2]) <= tol:
        return ("R", r[1], r[3])
    if abs(y - r[1]) <= tol:
        return ("T", r[0], r[2])
    if abs(y - r[3]) <= tol:
        return ("B", r[0], r[2])
    return None


def _neighbour_ends(graph, node_id, skip_edge):
    """Точки концов ДРУГИХ рёбер на этом узле — чтобы не сесть в занятое."""
    out = []
    for e in g_edges(graph):
        if e is skip_edge:
            continue
        for key, nk in (("source_point", "source"), ("target_point", "target")):
            if e.get(nk) == node_id and e.get(key):
                out.append((float(e[key][1]), float(e[key][0])))
    return out


# ── лекарства ──────────────────────────────────────────────────────────
def snap_waypoints(e, budget=BUDGET):
    """Лекарство 1б: косой сегмент ВНУТРИ маршрута — подтянуть ИЗЛОМ на ось.

    Мутирует waypoints ребра, ничего кроме излома не двигая (посаженные
    концы неприкосновенны — их держат порты/якоря). Это и есть «сглаживание
    мелких зигзагов»: маршрут с дрожащим коленом становится ровным.
    Возвращает число подтянутых изломов.

    Двигается ТОЛЬКО waypoint: если косой сегмент упирается в посаженный
    конец, этот конец не трогаем — им занимаются скольжение/сдвиг ниже.
    """
    wps = e.get("waypoints") or []
    if not wps:
        return 0

    def total_dev(edge):
        return sum(d for _i, d in diag_segments_of(edge))

    fixed = 0
    for _ in range(len(wps) + 2):           # до неподвижной точки
        pts = edge_pts(e)
        if pts is None:
            break
        base = total_dev(e)
        if base <= 0.0:
            break
        best = None                          # (остаточное отклонение, wi, точка)
        for i, (a, b) in enumerate(zip(pts, pts[1:])):
            if math.hypot(b[0] - a[0], b[1] - a[1]) < ec.SEG_FLOOR:
                continue
            dev = min(abs(b[0] - a[0]), abs(b[1] - a[1]))
            if dev <= ec.DIAG_TOL or dev > budget:
                continue
            vertical = abs(b[0] - a[0]) < abs(b[1] - a[1])
            # pts[k] — waypoint при 1 <= k <= len(pts)-2; посаженные концы
            # не трогаем (их держат порты и якоря)
            for k, other in ((i, b), (i + 1, a)):
                wi = k - 1
                if not (0 <= wi < len(wps)):
                    continue
                w = list(wps[wi])
                cand = [w[0], other[0]] if vertical else [other[1], w[1]]
                wps[wi] = cand
                after = total_dev(e)
                wps[wi] = w                  # откат пробы
                # Выбор по ИТОГУ, а не по порядку: подтягивание одного излома
                # умеет ломать соседний сегмент (поймано тестом), поэтому
                # берём ход, который уменьшает СУММАРНОЕ отклонение сильнее.
                if after < base - 0.5 and (best is None or after < best[0]):
                    best = (after, wi, cand)
        if best is None:
            break
        wps[best[1]] = best[2]
        fixed += 1
    return fixed


def step_index(e, step_max=STEP_MAX):
    """Индекс сегмента-шажка в полилинии (между pts[i] и pts[i+1]) или None."""
    pts = edge_pts(e)
    if not pts or len(pts) < 4:
        return None
    found = None
    for i in range(1, len(pts) - 2):
        a, b, c, d = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        L = math.hypot(c[0] - b[0], c[1] - b[1])
        if not (1e-6 < L <= step_max):
            continue
        horiz = (abs(a[1] - b[1]) <= ec.DIAG_TOL
                 and abs(c[1] - d[1]) <= ec.DIAG_TOL)
        vert = (abs(a[0] - b[0]) <= ec.DIAG_TOL
                and abs(c[0] - d[0]) <= ec.DIAG_TOL)
        if not (horiz or vert):
            continue
        if found is not None:
            return None
        found = i
    return found


def shift_run(e, side, axis, delta, idx=None):
    """Сдвинуть УЧАСТОК маршрута со стороны side на delta вдоль axis.

    idx — индекс сегмента-шажка, ВЗЯТЫЙ ДО мутаций: после сдвига конца
    ступенька уже не распознаётся (соседний сегмент успел скоситься), и
    поиск на месте вернул бы None — участок остался бы на старом месте.

    Ключевая деталь, без которой сдвиг бессмыслен: двигая конец на величину
    шажка, надо тащить за собой ВЕСЬ его участок (изломы до шажка), иначе
    ступенька не исчезает, а переезжает в соседний сегмент и делает его
    косым — гейт такой ход справедливо откатывает.
    Возвращает True, если что-то сдвинулось."""
    if idx is None:
        idx = step_index(e)
    wps = e.get("waypoints") or []
    if idx is None or not wps:
        return False
    # pts[k] = wps[k-1] при 1 <= k <= len(wps); шажок между pts[idx], pts[idx+1]
    rng = range(0, idx) if side == "s" else range(idx, len(wps))
    moved = False
    for k in rng:
        w = wps[k]
        wps[k] = [w[0] + delta, w[1]] if axis == "y" else [w[0], w[1] + delta]
        moved = True
    return moved


def drop_collinear(e, tol=ec.DIAG_TOL):
    """Убрать изломы, ставшие лишними: точка лежит на прямой между соседями.

    Это и есть ЗАВЕРШЕНИЕ сдвига: подвинув конец (или узел) на величину
    шажка, мы делаем его точки коллинеарными — но сами они остаются в
    данных, и труба выглядит прежней ступенькой, а первый сегмент ещё и
    косеет. Поэтому любой сдвиг обязан схлопывать то, что стало лишним.
    Возвращает число убранных изломов."""
    wps = e.get("waypoints") or []
    if not wps:
        return 0
    removed = 0
    i = 0
    while i < len(wps):
        pts = edge_pts(e)
        if pts is None or len(pts) < 3:
            break
        a, b, c = pts[i], pts[i + 1], pts[i + 2]
        # b лишняя, если a-b-c укладываются в одну ось
        same_x = abs(a[0] - b[0]) <= tol and abs(b[0] - c[0]) <= tol
        same_y = abs(a[1] - b[1]) <= tol and abs(b[1] - c[1]) <= tol
        if same_x or same_y:
            wps.pop(i)
            removed += 1
            continue
        i += 1
    return removed


def _slide_end(graph, e, role, axis, delta):
    """Лекарство 2: сдвинуть КОНЕЦ вдоль его грани/участка на delta.

    Возвращает новую точку (x, y) или None, если скользить нельзя: якорь,
    коннектор (у него конец — центроид), уход за грань, занятая позиция.
    """
    byid = nodes_by_id(graph)
    key = "source_point" if role == "s" else "target_point"
    nid = e.get("source") if role == "s" else e.get("target")
    node = byid.get(nid)
    kind = end_kind(node, e)
    if kind in ("anchor", "connector", "none"):
        return None
    p = e.get(key)
    if not p:
        return None
    x, y = float(p[1]), float(p[0])
    nx, ny = (x, y + delta) if axis == "y" else (x + delta, y)

    if kind == "rect":
        face = _face_of(node, (x, y))
        if face is None:
            return None
        side, lo, hi = face
        # скользить можно только ВДОЛЬ грани: у боковых — по y, у верх/низ — по x
        if side in ("L", "R") and axis != "y":
            return None
        if side in ("T", "B") and axis != "x":
            return None
        v = ny if axis == "y" else nx
        if not (lo + FACE_MARGIN <= v <= hi - FACE_MARGIN):
            return None
    else:                                   # контурный участок
        # poly_runs ждёт ПЛОСКИЙ список координат (как в данных узла), а
        # ec.poly_contour отдаёт список точек — берём сырую segmentation
        seg = node.get("segmentation")
        if not (seg and isinstance(seg, list) and len(seg) >= 6):
            return None
        runs = port_model.poly_runs(seg)
        on = None
        for (ax, ay, bx, by, rnx, rny) in runs:
            L2 = (bx - ax) ** 2 + (by - ay) ** 2
            if L2 <= 1e-9:
                continue
            t = ((x - ax) * (bx - ax) + (y - ay) * (by - ay)) / L2
            if -0.01 <= t <= 1.01:
                px, py = ax + t * (bx - ax), ay + t * (by - ay)
                if math.hypot(x - px, y - py) <= 0.75:
                    on = (ax, ay, bx, by, math.hypot(bx - ax, by - ay))
                    break
        if on is None:
            return None
        ax, ay, bx, by, L = on
        # участок должен идти вдоль оси сдвига
        if axis == "y" and abs(bx - ax) > ec.DIAG_TOL:
            return None
        if axis == "x" and abs(by - ay) > ec.DIAG_TOL:
            return None
        lo_v, hi_v = ((min(ay, by), max(ay, by)) if axis == "y"
                      else (min(ax, bx), max(ax, bx)))
        v = ny if axis == "y" else nx
        if not (lo_v + ec.CLEARANCE <= v <= hi_v - ec.CLEARANCE):
            return None

    for qx, qy in _neighbour_ends(graph, nid, e):
        if math.hypot(qx - nx, qy - ny) < OCCUPIED_TOL:
            return None                     # место занято соседней трубой
    return (nx, ny)


def _shift_node(graph, node_id, dx, dy):
    """Сдвинуть узел (и концы всех его рёбер) на (dx, dy). Для коннектора
    это движение точки, для оборудования — рамки с контуром."""
    byid = nodes_by_id(graph)
    n = byid.get(node_id)
    if n is None:
        return
    c = n.get("centroid")
    if c:
        n["centroid"] = [float(c[0]) + dy, float(c[1]) + dx]
    bb = n.get("bbox")
    if bb and len(bb) == 4:
        n["bbox"] = [bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy]
    seg = n.get("segmentation")
    if seg and isinstance(seg, list) and len(seg) >= 6:
        n["segmentation"] = [v + (dx if i % 2 == 0 else dy)
                             for i, v in enumerate(seg)]
    for e in g_edges(graph):
        for key, nk in (("source_point", "source"), ("target_point", "target")):
            if e.get(nk) == node_id and e.get(key):
                p = e[key]
                e[key] = [float(p[0]) + dy, float(p[1]) + dx]


def _node_is_anchor_role(graph, node_id, big_area):
    """Узел-якорь по роли: крупный блок — такие не двигаются (правило Э4
    из плана: крупные и прошитые магистралью узлы держат лист)."""
    n = nodes_by_id(graph).get(node_id)
    if n is None:
        return True
    bb = n.get("bbox")
    if bb and len(bb) == 4 and (bb[2] - bb[0]) * (bb[3] - bb[1]) >= big_area:
        return True
    return False


def _breaks_anchor(graph, node_id):
    """У узла есть якорь оператора -> его сдвиг допустим только вместе с
    якорем (якорь локален, едет с узлом) — но НЕ допустим, если якорь
    принадлежит ребру, второй конец которого мы тоже двигаем."""
    n = nodes_by_id(graph).get(node_id)
    if not n:
        return False
    if n.get("_ports"):                    # ЛЕГАСИ до миграции Э5b
        return True
    return any(port_model.pinned_on_node(n, e) for e in g_edges(graph))


# ── судья и гейт ───────────────────────────────────────────────────────
def count_steps(graph, step_max=STEP_MAX):
    """Сколько на холсте ступенек-шажков.

    Судья их НЕ считает дефектом и не может: ступенька строго ортогональна
    (H-V-H), отклонение от осей у неё ноль. Поэтому метрика своя — иначе
    гейт не видит улучшения от их устранения и откатывает ход (репро
    graph_edited_av: 10 ступенек пережили сглаживание нетронутыми)."""
    n = 0
    for e in g_edges(graph):
        pts = edge_pts(e)
        if not pts or len(pts) < 4:
            continue
        for i in range(1, len(pts) - 2):
            a, b, c, d = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
            L = math.hypot(c[0] - b[0], c[1] - b[1])
            if not (1e-6 < L <= step_max):
                continue
            horiz = (abs(a[1] - b[1]) <= ec.DIAG_TOL
                     and abs(c[1] - d[1]) <= ec.DIAG_TOL)
            vert = (abs(a[0] - b[0]) <= ec.DIAG_TOL
                    and abs(c[0] - d[0]) <= ec.DIAG_TOL)
            if horiz or vert:
                n += 1
    return n


def score(graph):
    """Метрики в порядке приоритета заказчика: ортогональность -> прочее."""
    c = ec.check_canvas(graph)["counts"]
    dev = 0.0
    for e in g_edges(graph):
        for _i, d in diag_segments_of(e):
            dev += d
    xs, ys = [], []
    for n in graph.get("nodes") or []:
        bb = n.get("bbox")
        if bb and len(bb) == 4:
            xs += [bb[0], bb[2]]
            ys += [bb[1], bb[3]]
    return {
        "diag": c["diag"], "dev": round(dev, 2),
        "steps": count_steps(graph),
        "near": c["near_ortho"], "along_own": c["along_own"],
        "along_foreign": c["along_foreign"], "through": c["through"],
        "corner": c["corner"], "adrift": c["adrift"],
        "conn_off": c["conn_off"], "poly_off": c["poly_off"],
        "w": (max(xs) - min(xs)) if xs else 0.0,
        "h": (max(ys) - min(ys)) if ys else 0.0,
    }


_NOT_WORSE = ("along_own", "along_foreign", "through", "corner",
              "adrift", "conn_off", "poly_off", "near")


def accepts(before, after):
    """Гейт хода: выигрыш ЛЕКСИКОГРАФИЧЕСКИ (косые -> отклонение ->
    ступеньки), остальное не хуже, холст цел.

    Ступеньки — третьим приоритетом, а НЕ в списке «не хуже»: ход, который
    убирает косую ценой одного шажка, обязан приниматься (косая заметнее
    ступеньки), иначе теряется главный выигрыш — обходы. Зато чистое
    устранение шажка (косых столько же, отклонение то же) теперь принимается,
    раньше гейт его не видел вовсе."""
    if after["w"] > CANVAS_W + 0.5 or after["h"] > CANVAS_H + 0.5:
        return False
    for k in _NOT_WORSE:
        if after[k] > before[k]:
            return False
    if after["diag"] != before["diag"]:
        return after["diag"] < before["diag"]
    if abs(after["dev"] - before["dev"]) > 0.5:
        return after["dev"] < before["dev"]
    return after["steps"] < before["steps"]


# ── главный цикл ───────────────────────────────────────────────────────
def smooth(graph, route_fn=None, reseat_fn=None, budget=BUDGET,
           allow_node_shift=True, big_area=40000.0, max_rounds=MAX_ROUNDS):
    """Сгладить холст. Мутирует graph. Возвращает статистику ходов.

    route_fn(edge) -> bool — построить ортогональный маршрут (лестница
    редактора); reseat_fn(node_id) -> None — пересадить концы рёбер узла
    после сдвига (мини-жест редактора). Без них лекарства 1 и 3/4
    вырождаются: движок не изобретает своей геометрии маршрутов.
    """
    stats = {"колено": 0, "излом": 0, "скольжение": 0, "коннектор": 0,
             "узел": 0, "отклонено": 0, "осталось": 0, "раунды": 0}
    for _round in range(max_rounds):
        stats["раунды"] += 1
        applied = 0
        for e in list(g_edges(graph)):
            # Кандидат — не только КОСОЕ ребро: ступенька строго ортогональна
            # (H-V-H), и отбор «только косые» проходил мимо неё вовсе — ровно
            # то, на что заказчик указал по graph_edited_av («не исправило
            # ступеньки»). Берём и ортогональные с одиночным шажком.
            if is_ortho(e) and single_step(e) is None:
                continue
            if _try_remedies(graph, e, stats, route_fn, reseat_fn,
                             budget, allow_node_shift, big_area):
                applied += 1
        if not applied:
            break
    stats["осталось"] = sum(1 for e in g_edges(graph) if not is_ortho(e))
    return stats


def _try_remedies(graph, e, stats, route_fn, reseat_fn, budget,
                  allow_node_shift, big_area):
    """Лестница лекарств для одного ребра. True — ход принят.

    ГАРАНТИЯ: ход отклонён => холст не изменился ни на бит. Держится
    try/finally, а не дисциплиной вызовов restore() по веткам: замер на
    graph_edited_fix показал, что одна ветка отката всё-таки не отрабатывала
    и мусор просачивался мимо гейта (along_foreign 0 -> 1)."""
    before = score(graph)
    snap_nodes = [deepcopy(n) for n in (graph.get("nodes") or [])]
    snap_edges = [deepcopy(dict(x)) for x in g_edges(graph)]

    def restore_all():
        # deepcopy обязателен: update() копирует dict ПОВЕРХНОСТНО, и без
        # него живое ребро получило бы тот же список waypoints, что лежит в
        # снимке. Дальше drop_collinear делает по нему pop(), снимок
        # укорачивается навсегда, и следующий откат возвращает уже порчу.
        for cur, old in zip(graph.get("nodes") or [], snap_nodes):
            cur.clear()
            cur.update(deepcopy(old))
        for cur, old in zip(g_edges(graph), snap_edges):
            cur.clear()
            cur.update(deepcopy(old))

    ok = False
    try:
        ok = _attempt(graph, e, stats, route_fn, reseat_fn, budget,
                      allow_node_shift, big_area, before)
        return ok
    finally:
        if not ok:
            restore_all()


def _attempt(graph, e, stats, route_fn, reseat_fn, budget,
             allow_node_shift, big_area, before):
    """Собственно лестница (см. _try_remedies)."""
    # Снимок ПОЭЛЕМЕНТНЫЙ, а не подменой списков: редактор держит индекс
    # {id -> тот же самый dict}, и подмена graph['nodes'] осиротила бы его —
    # модель продолжила бы указывать на выброшенные объекты.
    snap_nodes = [deepcopy(n) for n in (graph.get("nodes") or [])]
    snap_edges = [deepcopy(dict(x)) for x in g_edges(graph)]

    def restore():
        # deepcopy — см. restore_all(): снимок обязан пережить любое число
        # проб. Здесь это критично вдвойне: restore() в ветке КОЛЕНА ниже
        # зовётся БЕЗУСЛОВНО, как только движку передан роутер (а редактор
        # передаёт его всегда), то есть на боевом пути алиас взводился
        # раньше первой же пробы со сдвигом.
        for cur, old in zip(graph.get("nodes") or [], snap_nodes):
            cur.clear()
            cur.update(deepcopy(old))
        for cur, old in zip(g_edges(graph), snap_edges):
            cur.clear()
            cur.update(deepcopy(old))

    eid = e.get("id")

    def live_edge():
        for x in g_edges(graph):
            if x.get("id") == eid:
                return x
        return None

    # ── 1. КОЛЕНО: ничего не двигаем ───────────────────────────────────
    if route_fn is not None:
        cur = live_edge()
        if cur is not None and route_fn(cur):
            if accepts(before, score(graph)):
                stats["колено"] += 1
                return True
        restore()

    # ── 1б. ПОДТЯНУТЬ ИЗЛОМ: тоже ничего не двигаем, кроме колена ──────
    cur = live_edge()
    if cur is not None and snap_waypoints(cur, budget):
        if accepts(before, score(graph)):
            stats["излом"] += 1
            return True
        restore()

    step = shift_target(e)
    if step is None:
        stats["отклонено"] += 1
        return False        # многоступенчатый / косая внутри маршрута — оператору
    axis, d = step
    if d > budget:
        stats["отклонено"] += 1
        return False        # «супер движение» — не наш случай, в очаги
    sidx = step_index(e)    # ДО мутаций: после сдвига шажок не распознать

    byid = nodes_by_id(graph)
    ends = (("s", e.get("source")), ("t", e.get("target")))

    # ── 2. СКОЛЬЖЕНИЕ КОНЦА: оборудование стоит ────────────────────────
    for role, nid in ends:
        for sign in (1.0, -1.0):
            cur = live_edge()
            if cur is None:
                break
            new = _slide_end(graph, cur, role, axis, sign * d)
            if new is None:
                continue
            key = "source_point" if role == "s" else "target_point"
            cur[key] = [new[1], new[0]]
            shift_run(cur, role, axis, sign * d, sidx)  # участок едет с концом
            drop_collinear(cur)          # схлопнуть ставший лишним шажок
            if route_fn is not None:
                route_fn(cur)
            if accepts(before, score(graph)):
                stats["скольжение"] += 1
                return True
            restore()

    # ── 3. КОННЕКТОР: точка, двигать дёшево ────────────────────────────
    for role, nid in ends:
        node = byid.get(nid)
        if node is None or not is_connector(node):
            continue
        if end_kind(node, e) == "anchor":
            continue
        for sign in (1.0, -1.0):
            dx, dy = (0.0, sign * d) if axis == "y" else (sign * d, 0.0)
            _shift_node(graph, nid, dx, dy)
            cur = live_edge()
            if cur is not None:
                shift_run(cur, role, axis, sign * d, sidx)
                drop_collinear(cur)      # схлопнуть ставший лишним шажок
                if route_fn is not None:
                    route_fn(cur)
            if reseat_fn is not None:
                reseat_fn(nid)
            if accepts(before, score(graph)):
                stats["коннектор"] += 1
                return True
            restore()

    # ── 4. УЗЕЛ: последнее средство, под бюджетом ──────────────────────
    if allow_node_shift:
        for role, nid in ends:
            node = byid.get(nid)
            if node is None or is_connector(node):
                continue
            if end_kind(node, e) == "anchor":
                continue           # якорь оператора — узел не двигаем
            if _node_is_anchor_role(graph, nid, big_area):
                continue           # крупный блок держит лист
            for sign in (1.0, -1.0):
                dx, dy = (0.0, sign * d) if axis == "y" else (sign * d, 0.0)
                _shift_node(graph, nid, dx, dy)
                cur = live_edge()
                if cur is not None:
                    shift_run(cur, role, axis, sign * d, sidx)
                    drop_collinear(cur)  # схлопнуть ставший лишним шажок
                    if route_fn is not None:
                        route_fn(cur)
                if reseat_fn is not None:
                    reseat_fn(nid)
                if accepts(before, score(graph)):
                    stats["узел"] += 1
                    return True
                restore()

    stats["отклонено"] += 1
    return False

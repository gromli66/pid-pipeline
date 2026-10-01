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

  0. «д» — НЕОСЕВОЙ КОНЕЦ КЛАПАНА В СВОБОДНУЮ ОСЕВУЮ ТАЛИЮ (Р4/Р5, блок
     6): грань — план Р5 `seating.axial_plan`, узел стоит; свой гейт
     `accepts_axial` (неосевых строго меньше, косых не больше);
  0б. «б» — КЛАПАН ИЛИ КОННЕКТОР НА ЛИНИЮ ТРУБЫ (Р4, блок 6): клапан
     модели — строго поперёк оси на «линия − опорная точка» (талия, в
     веере слотов — точка конца), коннектор — на «линия − свой конец»;
     проба засчитывается, только если сняла излом;
  1. КОЛЕНО — маршрут вместо косой. Не двигает ничего. На холстах
     заказчика закрывает 41 диагональ из 46 (89%);
  2. СКОЛЬЖЕНИЕ КОНЦА по своей грани/участку — двигается только точка
     стыка, оборудование стоит;
  3. СДВИГ КОННЕКТОРА на величину ступеньки — коннектор это точка, у него
     нет ни формы, ни идентичности; таких случаев больше половины
     (68 дефектов из 124 имеют коннектор на конце);
  3б. «е» — КЛАПАН ВДОЛЬ ОСИ ПРИ НЕХВАТКЕ КЛИРЕНСА (Р4, блок 6): стаб из
     талии короче `ec.CLEARANCE` — клапан отходит от следующей точки
     маршрута на CLEARANCE − вылет + 0.5 (не больше бюджета);
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
  * ХОЛСТ 1920x1080: выход за границу отменяет ход;
  * НЕ НАЕЗЖАТЬ НА СОСЕДЕЙ (Р4, блок 6): число пар узлов с наложенными
    рамками (`overlap`) ход не увеличивает;
  * В КЛАПАН — ТОЛЬКО В ТАЛИЮ (Р4/Р5, блок 6): число неосевых концов
    клапанов степени 1–2 (`offaxis`, правило `contact.axial_only`) ход не
    увеличивает.

Гейт хода (`accepts`) — лексикографически: косые -> отклонение ->
ступеньки -> изломы (4-й приоритет, блок 6), прочие дефекты не хуже.
Лестница пробует ВСЕ ступени и применяет ОДИН ход — лучшую пробу по тому
же гейту (блок 6, `_attempt`); порядок ступеней решает только ничьи и
несравнимые пробы: остаётся более ранняя.
После мини-жеста пересадки (сдвиг коннектора/узла) непрямые трубы узла
прокладываются лестницей маршрута ещё раз («г», `_with_reroute`): новый
маршрут остаётся только без косой, с меньшим числом изломов и если
счётчики холста (косые, вдоль чужого, сквозь узел, изломы) не хуже, чем
сразу после мини-жеста.

Судья хода — `score` по всему холсту, но на время кнопки — с памятью
(`_Judge`, блок 6, 01.10): вклад ребра пересчитывается, только если
изменилось оно, его конец или узел рядом; снимки проб — байты marshal
(`_capture`/`_put`). Числа и холст — бит-в-бит прежние, кнопка быстрее
вдвое (MEASUREMENTS §137): прежде полный судья на каждую пробу съедал
45–60 % времени.

Чистый stdlib: ни Qt, ни shapely. Роутинг приходит колбэком `route_fn`
(редактор отдаёт свою лестницу), потому что он живёт в UI-слое.
"""
from __future__ import annotations

import marshal
import math
from copy import deepcopy

from . import contact
from . import edit_checks as ec
from . import ports as port_model
from . import seating
from .graph_access import degrees, edge_ends, edges as g_edges, \
    is_connector, nodes_by_id

# ── пороги ─────────────────────────────────────────────────────────────
BUDGET = 22.0        # px: максимум «чуть сдвинуть». Выбран замером и
                     # утверждён заказчиком 2026-08-02: у ступенек его
                     # холстов максимум ровно 21.6px, то есть 22 закрывает
                     # ВСЕ, оставаясь меньше клетки сетки (24). Сравнение на
                     # корпусе из 20 холстов: 16px -> ступенек 96->51,
                     # 22px -> 96->40, побочных ухудшений нет в обоих.
STEP_MAX = 24.0      # px: длиннее — это осмысленный изгиб, а не ступенька
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


def is_straight(e, tol=ec.DIAG_TOL):
    """Труба УЖЕ ПРЯМАЯ: два конца, между ними один сегмент по оси.

    Не то же самое, что `is_ortho`, и разница замерена (§MEFX4A): лестница
    из десяти изломов тоже «идёт по осям», но её маршрут роутер умеет
    упростить — запрет на вызов поверх неё поднял изломы на девяти холстах
    корпуса. Прямую же улучшить нельзя: маршрут поверх неё только добавит
    обход, и гейт откатит уже готовый ход.
    """
    pts = edge_pts(e)
    return bool(pts) and len(pts) == 2 and _ortho_seg(pts[0], pts[1], tol)


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
    """Лекарство 2: сдвинуть КОНЕЦ вдоль его участка контура на delta.

    Возвращает новую точку (x, y) или None, если скользить нельзя: якорь,
    коннектор (у него конец — центроид), РАМОЧНЫЙ узел, уход за участок,
    занятая позиция.

    Рамочный конец (бокс, скин) не скользит с 2026-09-22 (модель точек
    прикрепления): он живёт в опорной точке грани — выверенном порту клапана
    или середине с допуском MID_SNAP, — и увод входа вдоль грани сделал бы
    холст неверным ещё до выгрузки. Ступеньку лечит сдвиг узла или
    коннектора (лекарства 1 и 3), а не увод входа.
    """
    byid = nodes_by_id(graph)
    key = "source_point" if role == "s" else "target_point"
    nid = e.get("source") if role == "s" else e.get("target")
    node = byid.get(nid)
    kind = end_kind(node, e)
    if kind in ("anchor", "connector", "none", "rect"):
        return None
    p = e.get(key)
    if not p:
        return None
    x, y = float(p[1]), float(p[0])
    nx, ny = (x, y + delta) if axis == "y" else (x + delta, y)

    # контурный участок: poly_runs ждёт ПЛОСКИЙ список координат (как в
    # данных узла), а ec.poly_contour отдаёт список точек — берём сырую
    # segmentation
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


def _line_moves(graph, e, step, sidx, budget, big_area):
    """«б» (блок 6, Р4): кто встанет на линию трубы — клапан поперёк оси
    или коннектор. -> [(role, nid, ход, delta, line)] в порядке проб.

    line — поперечная координата трубы за ступенькой: дальняя сторона
    шажка (pts[sidx+1] для s, pts[sidx] для t), без шажка (прямая
    диагональ) — противоположный конец. delta — на сколько сдвинуть узел:
      * КЛАПАН с талией (`contact.face_refs`: клапан холста модели с
        `_axis`, не старый холст), не крупный, конец не пин и лежит на
        ОСЕВОЙ грани (`seating._AXIAL_FACES`), сдвиг строго поперёк оси.
        Опорная точка — куда конец посадит мини-жест: талия, если конец на
        грани один, иначе (веер слотов, k >= 2) — текущая точка конца.
        delta = line − опорная; сдвиг «на d» мини-жест возвращал бы в
        талию мимо линии (трасса 971/edge_62: ступенька 8.39 после сдвига);
      * КОННЕКТОР (модель не важна): delta = line − свой конец.
    |delta| < 0.25 или > budget — не ход. Порядок проб — меньшая степень,
    затем меньший |delta|: при равном гейте остаётся более ранняя проба.
    """
    pts = edge_pts(e)
    if not pts or step is None or step[1] > budget:
        return []
    axis = step[0]
    byid = nodes_by_id(graph)
    deg = degrees(graph)
    out = []
    for role, nid in (("s", e.get("source")), ("t", e.get("target"))):
        node = byid.get(nid)
        if node is None or end_kind(node, e) == "anchor":
            continue
        if sidx is not None:
            far = pts[sidx + 1] if role == "s" else pts[sidx]
        else:
            far = pts[-1] if role == "s" else pts[0]
        line = far[1] if axis == "y" else far[0]
        mx, my = pts[0] if role == "s" else pts[-1]
        mine = my if axis == "y" else mx
        rect = seating._anchor_rect(node)
        refs = contact.face_refs(node, rect) if rect is not None else None
        if refs:
            if (node["_axis"] == "H") != (axis == "y"):
                continue                     # только поперёк оси клапана
            if _node_is_anchor_role(graph, nid, big_area):
                continue
            side = seating._end_side(rect, mx, my)
            if side not in seating._AXIAL_FACES[node["_axis"]]:
                continue                     # неосевой конец — это «д»
            k = 0
            for x in g_edges(graph):
                for xid, key in zip(edge_ends(x),
                                    ("source_point", "target_point")):
                    p = x.get(key)
                    if xid == nid and p and seating._end_side(
                            rect, float(p[1]), float(p[0])) == side:
                        k += 1
            ref = refs[side] if k == 1 else mine
            kind = "узел"
        elif is_connector(node):
            ref, kind = mine, "коннектор"
        else:
            continue
        delta = line - ref
        if abs(delta) < 0.25 or abs(delta) > budget:
            continue
        out.append((deg.get(nid, 0), abs(delta), role, nid, kind, delta,
                    line))
    out.sort(key=lambda c: (c[0], c[1]))
    return [c[2:] for c in out]


def _clearance_moves(graph, e, budget, big_area):
    """«е» (блок 6, Р4): клапан ВДОЛЬ оси, когда стабу из талии не хватает
    клиренса. -> [(role, nid, dx, dy)].

    Конец клапана модели на осевой грани F (не пин, клапан не крупный); q —
    следующая точка маршрута после конца (первый излом, иначе другой
    конец); g — вылет q за грань наружу. При 0 <= g < ec.CLEARANCE
    лестнице маршрута негде поставить колено у клапана (стаб короче
    клиренса), и клапан отходит от q вдоль оси на CLEARANCE − g + 0.5
    (не больше бюджета)."""
    pts = edge_pts(e)
    if not pts or len(pts) < 2:
        return []
    byid = nodes_by_id(graph)
    out = []
    for role, nid in (("s", e.get("source")), ("t", e.get("target"))):
        node = byid.get(nid)
        if node is None or end_kind(node, e) == "anchor":
            continue
        rect = seating._anchor_rect(node)
        refs = contact.face_refs(node, rect) if rect is not None else None
        if not refs or _node_is_anchor_role(graph, nid, big_area):
            continue
        mx, my = pts[0] if role == "s" else pts[-1]
        qx, qy = pts[1] if role == "s" else pts[-2]
        side = seating._end_side(rect, mx, my)
        if side not in refs:
            continue
        nx, ny = {"L": (-1, 0), "R": (1, 0), "T": (0, -1), "B": (0, 1)}[side]
        face = {"L": rect[0], "R": rect[2], "T": rect[1], "B": rect[3]}[side]
        g = ((qx if nx else qy) - face) * (nx or ny)
        if not (0.0 <= g < ec.CLEARANCE):
            continue
        s = ec.CLEARANCE - g + 0.5
        if s > budget:
            continue
        out.append((role, nid, -nx * s, -ny * s))
    return out


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


def count_bends(graph):
    """(изломов всего, рёбер с геометрией).

    Излом — промежуточная точка, в которой маршрут МЕНЯЕТ направление.
    Нужен блоку 6: слияние двух ступенек в одно колено счётчику `steps`
    невидимо (20+20 -> 40 — по-прежнему «не ступенька»), а изломов при этом
    становится меньше. Предикат «лишняя точка» тот же, что у
    `drop_collinear`, — сторож и судья не должны расходиться. Перенесён из
    `tools/smooth_bench.py` бит-в-бит (блок 6, шаг 1): стенд зовёт отсюда.
    """
    tol = ec.DIAG_TOL
    bends = edges = 0
    for e in g_edges(graph):
        pts = edge_pts(e)
        if not pts:
            continue
        edges += 1
        for a, b, c in zip(pts, pts[1:], pts[2:]):
            same_x = abs(a[0] - b[0]) <= tol and abs(b[0] - c[0]) <= tol
            same_y = abs(a[1] - b[1]) <= tol and abs(b[1] - c[1]) <= tol
            if not (same_x or same_y):
                bends += 1
    return bends, edges


def count_overlaps(graph):
    """Пар узлов с наложенными рамками (Р4, блок 6: «не наезжать на соседей»).

    Предикат — `graph_access.box_hits` / `_spread_overlaps` редактора:
    пересечение по ОБЕИМ осям строго больше нуля (касание — не наложение);
    узлы без рамки (коннекторы) не считаются. Наложения из построения
    легальны — гейт судит только прирост (`_NOT_WORSE`). Развёртка по x1:
    score зовётся на каждую пробу хода, перебор всех пар дорог.
    """
    boxes = sorted((float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3]))
                   for bb in (n.get("bbox") for n in graph.get("nodes") or ())
                   if bb and len(bb) == 4)
    n = 0
    for i, (x1, y1, x2, y2) in enumerate(boxes):
        for u1, v1, u2, v2 in boxes[i + 1:]:
            if u1 >= x2:
                break                   # дальше x1 только больше — мимо
            if min(x2, u2) - u1 > 0 and min(y2, v2) - max(y1, v1) > 0:
                n += 1
    return n


def offaxis_ends(graph):
    """[(node_id, edge_id)] неосевых концов клапанов под правилом Р5.

    Правило — `contact.axial_only` (клапан холста с `_axis`, степень 1–2 по
    рёбрам, без RELIEF и узлов старого холста `LEGACY_KEY`), грань —
    `seating._end_side` по рамке посадки `seating._anchor_rect`, осевые
    грани — `seating._AXIAL_FACES`: тот же судья, что посадка блока 5
    (`seating.axial_faces`) и счётчик `layout_bench.valve_ends`. Конец с
    пином оператора не судится (пин — истина).
    """
    return [(nid, e.get("id")) for nid, e in _offaxis(graph)]


def _offaxis(graph):
    """[(node_id, ребро)] — тело `offaxis_ends` (ребро — живой dict)."""
    byid = nodes_by_id(graph)
    deg = degrees(graph)
    return [(nid, e) for e in g_edges(graph)
            for nid in _offaxis_of(e, byid, deg)]


def _offaxis_of(e, byid, deg):
    """[node_id] неосевых концов ОДНОГО ребра — правило `_offaxis`
    (`_Judge` судит ребро отдельно; степень — по рёбрам всего холста)."""
    out = []
    for nid, key in zip(edge_ends(e), ("source_point", "target_point")):
        n = byid.get(nid)
        if n is None or not contact.axial_only(n, deg.get(nid, 0)):
            continue
        pt, rect = e.get(key), seating._anchor_rect(n)
        if not pt or rect is None:
            continue
        if port_model.pinned_port(n, e) is not None:
            continue
        side = seating._end_side(rect, float(pt[1]), float(pt[0]))
        if side not in seating._AXIAL_FACES[n["_axis"]]:
            out.append(nid)
    return out


_FACE_NAME = {"L": "left", "R": "right", "T": "top", "B": "bottom"}


def _pair(e):
    """Ключ ребра редактора (`model.edge_key`): неупорядоченная пара узлов."""
    return tuple(sorted(str(v) for v in edge_ends(e)))


def _axial_moves(graph, e):
    """«д» (блок 6, Р4/Р5): неосевой конец клапана -> свободная осевая
    талия. -> [(node_id, [(ребро, ключ точки, ключ стороны, точка)])].

    Грани — план `seating.axial_plan` (правило Р5 `seating.axial_faces`,
    тот же, что посадка сервера и редактора; своего правила нет):
    степень 2 — соосный сосед берёт грань к себе, поперечный — вторую
    осевую, колено уходит за клапан; степень 1 — осевая грань к соседу,
    маршрут — колено или крючок (ось клапана редактор не поворачивает,
    Р3). Двигаются все концы клапана, чья грань расходится с планом:
    конец — ровно в талию грани (`contact.face_refs`). Пропуск: пин (в
    плане его нет), две трубы в одну грань (нужен веер слотов — дело
    мини-жеста), кратная пара узлов (лестница редактора адресует ребро
    ПАРОЙ и переложила бы чужое, Р7).
    """
    off = [nid for nid, x in _offaxis(graph) if x is e]
    if not off:
        return []
    plan = seating.axial_plan(graph)
    byid = nodes_by_id(graph)
    pairs = {}
    for x in g_edges(graph):
        pairs[_pair(x)] = pairs.get(_pair(x), 0) + 1
    out = []
    for nid in dict.fromkeys(off):
        n = byid[nid]
        rect = seating._anchor_rect(n)
        refs = contact.face_refs(n, rect) if rect is not None else None
        if not refs:
            continue
        moves, faces, ok = [], [], True
        for x in g_edges(graph):
            s, t = edge_ends(x)
            for role, xid, key, sk in (
                    ("source", s, "source_point", "_src_side"),
                    ("target", t, "target_point", "_tgt_side")):
                if xid != nid:
                    continue
                f = plan.get((id(x), role))
                if f is None:
                    continue
                faces.append(f)
                p = x.get(key)
                if p and seating._end_side(rect, float(p[1]),
                                           float(p[0])) == f:
                    continue
                if pairs[_pair(x)] > 1:
                    ok = False
                w = refs[f]
                pt = ([w, rect[0] if f == "L" else rect[2]]
                      if f in ("L", "R") else
                      [rect[1] if f == "T" else rect[3], w])
                moves.append((x, key, sk, f, pt))
        if ok and moves and len(faces) == len(set(faces)):
            out.append((nid, moves))
    return out


def score(graph):
    """Метрики в порядке приоритета заказчика: ортогональность -> прочее.

    Блок 6: `bends` — изломы (`count_bends`, 4-й приоритет гейта),
    `overlap` — пар наложенных рамок (`count_overlaps`), `offaxis` —
    неосевых концов клапанов степени 1–2 (`offaxis_ends`); последние два —
    в `_NOT_WORSE`."""
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
        "bends": count_bends(graph)[0],
        "overlap": count_overlaps(graph),
        "offaxis": len(offaxis_ends(graph)),
    }


# ── судья с памятью (ускорение кнопки, блок 6) ─────────────────────────
def _sig(obj):
    """Отпечаток словаря холста: marshal версии 2 — типы, порядок ключей и
    значения, без ссылок (равное содержимое -> равные байты; равные байты
    -> равное содержимое). Не JSON-тип внутри — None: «изменился всегда»."""
    try:
        return marshal.dumps(obj, 2)
    except ValueError:
        return None


def _zone(n):
    """Рамка, вне которой узел не влияет на «вдоль»/«сквозь» чужого ребра:
    граница судьи (`ec._node_border` — контур или bbox) + клиренс + 1 px.
    Коннектор и узел без границы — None (судья их пропускает)."""
    if is_connector(n):
        return None
    b = ec._node_border(n)
    if not b:
        return None
    m = ec.CLEARANCE + 1.0
    xs = [p[0] for p in b]
    ys = [p[1] for p in b]
    return (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)


def _meets(a, b):
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


class _Judge:
    """`score` с памятью по рёбрам — бит-в-бит то же число (зонд —
    tests/test_edit_smooth.py::test_judge_equals_full_score).

    Зачем: кнопка судит холст ЦЕЛИКОМ на каждую пробу лестницы, вход ребра и
    повтор «г» (профиль §137: 60 % времени кнопки — `ec.along_border`), а
    проба трогает одно-два ребра и узел. Счётчики судьи аддитивны по рёбрам:
    вклад ребра (косые, почти-оси, отклонение, ступеньки, изломы, вдоль,
    сквозь, углы, классы концов, неосевые) зависит только от самого ребра,
    его концевых узлов (и их степени) и узлов, чья зона (`_zone`) задевает
    рамку ребра. Ребро пересчитывается, если изменилось оно (отпечаток
    `_sig`), его конец, степень конца или узел, чья старая либо новая зона
    задевает его рамку; иначе вклад берётся из памяти. Узлы сверяются
    отпечатком на каждом вызове. Пересчёт — те же функции `edit_checks` на
    мини-холсте {узлы зоны + концы, само ребро}. Отклонение суммируется в том
    же порядке, что у `score` (сумма float не ассоциативна); «вдоль» склеено
    по паре (id ребра, узел) первым вхождением в порядке рёбер — как
    склейка `ec.along_border`. Габарит — по всем узлам, наложения — заново,
    если сдвинулся хоть один узел."""

    def __init__(self):
        self._nodes = None      # узлы прошлого вызова (те же dict)
        self._nsig = []
        self._nzone = []
        # id(ребро) -> (ребро, отпечаток, степени концов, рамка, вклад)
        self._edges = {}
        self._overlap = None

    def __call__(self, graph):
        nodes = graph.get("nodes") or []
        links = g_edges(graph)
        deg = degrees(graph)
        if (self._nodes is None or len(nodes) != len(self._nodes)
                or any(a is not b for a, b in zip(nodes, self._nodes))):
            self._nodes = list(nodes)
            self._nsig = [None] * len(nodes)
            self._nzone = [None] * len(nodes)
            self._edges = {}
            self._overlap = None
        moved, zones = set(), []
        for i, n in enumerate(nodes):
            s = _sig(n)
            if s is not None and s == self._nsig[i]:
                continue
            old, new = self._nzone[i], _zone(n)
            self._nsig[i], self._nzone[i] = s, new
            moved.add(n.get("id"))
            zones += [z for z in (old, new) if z is not None]
        if moved:
            self._overlap = None
        parts, live = [], {}
        for e in links:
            s = _sig(e)
            ends = edge_ends(e)
            dk = (deg.get(ends[0], 0), deg.get(ends[1], 0))
            ent = self._edges.get(id(e))
            if (ent is None or ent[0] is not e or s is None or ent[1] != s
                    or ent[2] != dk or ends[0] in moved or ends[1] in moved
                    or (ent[3] is not None
                        and any(_meets(z, ent[3]) for z in zones))):
                box = self._box(e)
                ent = (e, s, dk, box, self._part(e, ends, box, nodes, deg))
            live[id(e)] = ent
            parts.append(ent[4])
        self._edges = live
        if self._overlap is None:
            self._overlap = count_overlaps(graph)

        diag = near = steps = bends = thr = corner = 0
        adrift = conn_off = poly_off = offaxis = 0
        dev = 0.0
        seen = {}
        for e, p in zip(links, parts):
            diag += p[0]
            near += p[1]
            for d in p[2]:
                dev += d
            steps += p[3]
            bends += p[4]
            eid = e.get("id")
            for nid, own in p[5]:
                seen.setdefault((eid, nid), own)
            thr += p[6]
            corner += p[7]
            adrift += p[8]
            conn_off += p[9]
            poly_off += p[10]
            offaxis += p[11]
        own = sum(1 for v in seen.values() if v)
        xs, ys = [], []
        for n in nodes:
            bb = n.get("bbox")
            if bb and len(bb) == 4:
                xs += [bb[0], bb[2]]
                ys += [bb[1], bb[3]]
        return {
            "diag": diag, "dev": round(dev, 2), "steps": steps,
            "near": near, "along_own": own, "along_foreign": len(seen) - own,
            "through": thr, "corner": corner, "adrift": adrift,
            "conn_off": conn_off, "poly_off": poly_off,
            "w": (max(xs) - min(xs)) if xs else 0.0,
            "h": (max(ys) - min(ys)) if ys else 0.0,
            "bends": bends, "overlap": self._overlap, "offaxis": offaxis,
        }

    @staticmethod
    def _box(e):
        pts = ec.edge_polyline(e)
        if len(pts) < 2:
            return None                     # сегментов нет — соседи не важны
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs) - 1.0, min(ys) - 1.0, max(xs) + 1.0, max(ys) + 1.0)

    def _part(self, e, ends, box, nodes, deg):
        """Вклад одного ребра — функции судьи на мини-холсте."""
        sub = [n for n, z in zip(nodes, self._nzone)
               if n.get("id") in ends
               or (box is not None and z is not None and _meets(z, box))]
        mini = {"nodes": sub, "links": [e]}
        dg = ec.diag_segments(mini)
        cl = ec.end_classes(mini)
        return (len(dg["diag"]), len(dg["near"]),
                tuple(d for _i, d in diag_segments_of(e)),
                count_steps(mini), count_bends(mini)[0],
                tuple((a["node"], a["own"]) for a in ec.along_border(mini)),
                len(ec.through_box(mini)), len(ec.corner_ends(mini)),
                len(cl["adrift"]), len(cl["conn_off"]), len(cl["poly_off"]),
                len(_offaxis_of(e, nodes_by_id(mini), deg)))


_ACTIVE = None      # (холст, судья) идущего `smooth` — см. `_score`


def _score(graph):
    """`score` холста идущей кнопки — судьёй с памятью, иначе полный.

    Судья живёт в модуле, а не в аргументах: `_try_remedies` подменяют
    снаружи с прежней сигнатурой (стенд `tools/smooth_bench.py`), и
    аргумент не дошёл бы до лестницы."""
    if _ACTIVE is not None and _ACTIVE[0] is graph:
        return _ACTIVE[1](graph)
    return score(graph)


_NOT_WORSE =("along_own", "along_foreign", "through", "corner",
              "adrift", "conn_off", "poly_off", "near", "overlap", "offaxis")

# Причины отказа (К-4): те же три ветки `_attempt`, которые снаружи
# восстанавливал стенд (`tools/smooth_bench.py:119`), — теперь движок
# называет их сам, а оператору есть что показать кроме «осталось N».
REFUSAL_KINDS = ("нет кандидата", "сверх бюджета", "лестница исчерпана")


def _refuse(stats, kind):
    """Отказ с НАЗВАННОЙ причиной. Общий счётчик остаётся суммой трёх
    вёдер: его читают стенд (`tools/smooth_bench.py:418`) и тесты."""
    stats["отклонено"] += 1
    stats[kind] += 1


def better(a, b):
    """Состояние a лучше b по приоритету гейта — ЛЕКСИКОГРАФИЧЕСКИ: косые
    -> отклонение (шум 0.5 px) -> ступеньки -> изломы. Равенство — не лучше.

    Выигрыш хода в `accepts`; через `accepts` же — выбор лучшей пробы
    лестницы в `_attempt` (блок 6): сторож и судья не расходятся."""
    if a["diag"] != b["diag"]:
        return a["diag"] < b["diag"]
    if abs(a["dev"] - b["dev"]) > 0.5:
        return a["dev"] < b["dev"]
    if a["steps"] != b["steps"]:
        return a["steps"] < b["steps"]
    return a.get("bends", 0) < b.get("bends", 0)


def accepts(before, after):
    """Гейт хода: выигрыш ЛЕКСИКОГРАФИЧЕСКИ (косые -> отклонение ->
    ступеньки -> изломы, `better`), остальное не хуже, холст цел.

    Ступеньки — третьим приоритетом, а НЕ в списке «не хуже»: ход, который
    убирает косую ценой одного шажка, обязан приниматься (косая заметнее
    ступеньки), иначе теряется главный выигрыш — обходы. Зато чистое
    устранение шажка (косых столько же, отклонение то же) теперь принимается,
    раньше гейт его не видел вовсе.

    Изломы — ЧЕТВЁРТЫМ приоритетом (блок 6, «а″»): при равных косых,
    отклонении и ступеньках ход, снимающий изломы, принимается, добавляющий
    — нет. Слияние двух ступенек в одно колено счётчику ступенек невидимо,
    а изломов становится меньше. Словари без ключа (`bends`, `overlap`,
    `offaxis`) судятся как 0 — поведение прежнее."""
    if after["w"] > CANVAS_W + 0.5 or after["h"] > CANVAS_H + 0.5:
        return False
    for k in _NOT_WORSE:
        if after.get(k, 0) > before.get(k, 0):
            return False
    return better(after, before)


def accepts_axial(before, after):
    """Гейт хода «осевая талия» (блок 6, «д»): неосевых входов клапана
    СТРОГО меньше; косых не больше, отклонение не больше (шум 0.5 px);
    прочие дефекты `_NOT_WORSE` не хуже; холст цел. Ступеньки и изломы
    могут расти — колено выносится за клапан (Р5): правило «в клапан
    только через талию» старше красоты маршрута."""
    if after["w"] > CANVAS_W + 0.5 or after["h"] > CANVAS_H + 0.5:
        return False
    for k in _NOT_WORSE:
        if k != "offaxis" and after.get(k, 0) > before.get(k, 0):
            return False
    if after.get("offaxis", 0) >= before.get("offaxis", 0):
        return False
    if after["diag"] > before["diag"]:
        return False
    return after["dev"] <= before["dev"] + 0.5


# ── главный цикл ───────────────────────────────────────────────────────
def smooth(graph, route_fn=None, reseat_fn=None, budget=BUDGET,
           allow_node_shift=True, big_area=40000.0, max_rounds=MAX_ROUNDS):
    """Сгладить холст. Мутирует graph. Возвращает статистику ходов.

    route_fn(edge) -> bool — построить ортогональный маршрут (лестница
    редактора); reseat_fn(node_id) -> None — пересадить концы рёбер узла
    после сдвига (мини-жест редактора). Без них лекарства 1 и 3/4
    вырождаются: движок не изобретает своей геометрии маршрутов.

    Заданы оба колбэка — мини-жест обёрнут повтором лестницы маршрута
    («г», `_with_reroute`); обёртка живёт здесь, поэтому подмена
    `reseat_fn` снаружи (стенд, тесты) видит и её.
    """
    global _ACTIVE
    if reseat_fn is not None and route_fn is not None:
        reseat_fn = _with_reroute(graph, reseat_fn, route_fn)
    stats = {"колено": 0, "излом": 0, "скольжение": 0, "коннектор": 0,
             "узел": 0, "осевая талия": 0, "отклонено": 0, "осталось": 0,
             "раунды": 0, **{k: 0 for k in REFUSAL_KINDS}}
    prev, _ACTIVE = _ACTIVE, (graph, _Judge())
    try:
        _rounds(graph, stats, route_fn, reseat_fn, budget, allow_node_shift,
                big_area, max_rounds)
    finally:
        _ACTIVE = prev
    stats["осталось"] = sum(1 for e in g_edges(graph) if not is_ortho(e))
    return stats


def _rounds(graph, stats, route_fn, reseat_fn, budget, allow_node_shift,
            big_area, max_rounds):
    """Жадные раунды `smooth` (вынесены, чтобы судья с памятью жил ровно
    время кнопки)."""
    for _round in range(max_rounds):
        stats["раунды"] += 1
        applied = 0
        # неосевые входы клапана (Р5) — тоже кандидаты, даже прямые («д»)
        off = {id(x) for _nid, x in _offaxis(graph)}
        for e in list(g_edges(graph)):
            # Кандидат — не только КОСОЕ ребро: ступенька строго ортогональна
            # (H-V-H), и отбор «только косые» проходил мимо неё вовсе — ровно
            # то, на что заказчик указал по graph_edited_av («не исправило
            # ступеньки»). Берём и ортогональные с одиночным шажком.
            if is_ortho(e) and single_step(e) is None and id(e) not in off:
                continue
            if _try_remedies(graph, e, stats, route_fn, reseat_fn,
                             budget, allow_node_shift, big_area):
                applied += 1
        if not applied:
            break


def _edge_bends(e):
    """Изломов одного ребра — тот же предикат, что `count_bends`."""
    return count_bends({"links": [e]})[0]


# Счётчики холста, которые повтор прокладки не имеет права ухудшить
# против состояния сразу после мини-жеста (решение Максима 30.09).
_REROUTE_GUARD = ("diag", "along_foreign", "through", "bends")


def _with_reroute(graph, reseat_fn, route_fn):
    """«г» (блок 6): после мини-жеста повторить лестницу маршрута.

    Мини-жест редактора (`_reseat_after_resize`) перекладывает рёбра узла
    оконной libavoid-сессией с нуджингом и умеет вернуть лесенку там, где
    лестница `route_fn` строит Z (трасса 971/edge_52: 5 изломов против 2).
    Поэтому после него для каждого НЕ прямого ребра узла лестница
    повторяется. Новый маршрут остаётся, только если он построен (ok), без
    пометки `_route_defect`, БЕЗ КОСОЙ (решение Максима 24.09), изломов у
    ребра меньше либо мини-жест оставил косую, И счётчики холста — косые,
    вдоль чужого, сквозь узел, изломы (`_REROUTE_GUARD`) — не хуже, чем
    сразу после мини-жеста (с учётом уже оставленных повторов; решение
    Максима 30.09: на 971 маршрут повтора без косой шёл вдоль чужой трубы
    и сквозь узел, гейт хода отвергал весь ход, и косая edge_manual_1
    оставалась). Иначе ребро возвращается ровно в состояние после
    мини-жеста — снимком словаря целиком: ключи, которых не было
    (`_src_side`, `_tgt_side`, `_route_defect`), удаляются, снятая `_slack`
    возвращается. Оставленный маршрут — жест: `_slack` снимается
    (`_drop_slack`).

    Рёбра с неуникальной парой узлов пропускаются: `route_fn` редактора
    адресует ребро парой (`model.edge_key`), и у кратного ребра переложил
    бы чужое (Р7: кратные рёбра остаются у пар с `unknow`).
    """
    def reseat(nid):
        out = reseat_fn(nid)
        s0 = None
        pairs = {}
        for e in g_edges(graph):
            pairs[_pair(e)] = pairs.get(_pair(e), 0) + 1
        for e in list(g_edges(graph)):
            if nid not in edge_ends(e) or pairs[_pair(e)] > 1:
                continue
            if edge_pts(e) is None or is_straight(e):
                continue
            snap = deepcopy(dict(e))
            b0 = _edge_bends(e)
            diag0 = bool(diag_segments_of(e))
            if s0 is None:
                s0 = _score(graph)             # холст сразу после мини-жеста
            ok = route_fn(e)
            if (ok and not e.get("_route_defect")
                    and not diag_segments_of(e)
                    and (_edge_bends(e) < b0 or diag0)):
                s1 = _score(graph)
                if all(s1[k] <= s0[k] for k in _REROUTE_GUARD):
                    s0 = s1
                    _drop_slack(graph, e)
                    continue
            e.clear()
            e.update(snap)
        return out
    return reseat


def _try_remedies(graph, e, stats, route_fn, reseat_fn, budget,
                  allow_node_shift, big_area):
    """Лестница лекарств для одного ребра. True — ход принят.

    ГАРАНТИЯ: ход отклонён => холст не изменился ни на бит. Держится
    try/finally, а не дисциплиной вызовов restore() по веткам: замер на
    graph_edited_fix показал, что одна ветка отката всё-таки не отрабатывала
    и мусор просачивался мимо гейта (along_foreign 0 -> 1)."""
    before = _score(graph)
    snap = _capture(graph)          # он же вход ребра для проб `_attempt`

    ok = False
    try:
        ok = _attempt(graph, e, stats, route_fn, reseat_fn, budget,
                      allow_node_shift, big_area, before, snap)
        return ok
    finally:
        if not ok:
            _put(graph, snap)       # свежие копии снимка (см. `_put`)


def _drop_slack(graph, e):
    """Принятый ход — жест оператора: метка вынужденной прямой сервера
    (`_slack`) у ребра-кандидата снимается (шов с блоком 1 п.5: «д»
    зовёт лестницу маршрута и мини-жест, но не всегда — снять метку
    больше некому). На старом холсте (`contact.LEGACY_KEY`) метку не
    трогаем — как редактор (`_waist_model()` ложно)."""
    if e is None or "_slack" not in e:
        return
    byid = nodes_by_id(graph)
    if any((byid.get(nid) or {}).get(contact.LEGACY_KEY)
           for nid in edge_ends(e)):
        return
    e.pop("_slack", None)


def _freeze(d):
    """Копия словаря для снимка: байты marshal (C-уровень, ссылки внутри
    словаря сохраняются, как у deepcopy), иначе — deepcopy (не JSON-тип)."""
    try:
        return marshal.dumps(d)
    except ValueError:
        return deepcopy(d)


def _thaw(old):
    return marshal.loads(old) if isinstance(old, bytes) else deepcopy(old)


def _capture(graph):
    """Снимок ПОЭЛЕМЕНТНЫЙ (узлы, рёбра), а не подменой списков: редактор
    держит индекс {id -> тот же самый dict}, и подмена graph['nodes']
    осиротила бы его — модель продолжила бы указывать на выброшенные
    объекты. Элемент — `_freeze` (профиль §137: deepcopy всего холста на
    каждую пробу стоил до 6 % времени кнопки)."""
    return ([_freeze(n) for n in (graph.get("nodes") or [])],
            [_freeze(dict(x)) for x in g_edges(graph)])


def _put(graph, snap):
    """Вернуть холст в снимок `_capture` на месте (те же dict).

    Каждый элемент — СВЕЖАЯ копия снимка (`_thaw`): update() копирует dict
    ПОВЕРХНОСТНО, и без копии живое ребро получило бы тот же список
    waypoints, что лежит в снимке. Дальше drop_collinear делает по нему
    pop(), снимок укорачивается навсегда, и следующий откат возвращает уже
    порчу. Здесь это критично вдвойне: снимок входа ребра возвращается
    после КАЖДОЙ пробы лестницы."""
    nodes, links = snap
    for cur, old in zip(graph.get("nodes") or [], nodes):
        cur.clear()
        cur.update(_thaw(old))
    for cur, old in zip(g_edges(graph), links):
        cur.clear()
        cur.update(_thaw(old))


def _attempt(graph, e, stats, route_fn, reseat_fn, budget,
             allow_node_shift, big_area, before, base=None):
    """Собственно лестница (см. _try_remedies).

    ЛУЧШАЯ ПРОБА, а не первая (блок 6): каждая проба лестницы судится
    гейтом от входа ребра и откатывается; из прошедших берётся лучшая по
    ТОМУ ЖЕ гейту — поздняя проба вытесняет запомненную, только если
    `accepts(запомненная, поздняя)`: выигрыш по приоритету (`better`:
    косые -> отклонение -> ступеньки -> изломы) и ни один дефект
    `_NOT_WORSE` не хуже, чем у запомненной. Несравнимые (одна чище по
    изломам, другая — по «сквозь узел») и равные — остаётся более ранняя,
    дешёвая ступень. Применяется ОДИН ход. Первая прошедшая проба
    вытесняла следующую, которая была лучше: на graph_edited70827 ход
    «только изломы» по edge_144 (коннектор, ступенек 3, изломов 18)
    занимал место пробы, снимавшей ступеньку (ступенек 2, изломов 16).
    Сравнение одним приоритетом, без «не хуже», проигрывало на
    graph_edited_222222: сдвиг узла (изломов на 3 меньше) вытеснял сдвиг
    коннектора, снимавший 5 прошиваний и 3 «вдоль чужого»."""
    if base is None:
        base = _capture(graph)
    best = []                       # [score, ход, снимок] лучшей пробы

    def restore():
        _put(graph, base)

    def judge(kind, fewer_bends=False, gate=accepts):
        """Проба сделана: в гейт; прошла и гейт принимает переход от
        лучшей к ней — запомнить. Холст возвращается во вход ребра.
        fewer_bends — проба «б» обязана ещё и снять излом; gate — свой
        гейт входа у «д» (`accepts_axial`), сравнение с лучшей — общее."""
        after = _score(graph)
        if gate(before, after) and (not best or accepts(best[0], after)) \
                and (not fewer_bends
                     or after.get("bends", 0) < before.get("bends", 0)):
            best[:] = [after, kind, _capture(graph)]
        restore()

    def done(refusal):
        if best:
            _put(graph, best[2])
            stats[best[1]] += 1
            _drop_slack(graph, live_edge())
            return True
        _refuse(stats, refusal)
        return False

    eid = e.get("id")

    def live_edge():
        for x in g_edges(graph):
            if x.get("id") == eid:
                return x
        return None

    pts0 = edge_pts(e)
    step = shift_target(e)
    sidx = step_index(e)    # ДО мутаций: после сдвига шажок не распознать

    # ── 0. «д»: НЕОСЕВОЙ КОНЕЦ КЛАПАНА -> СВОБОДНАЯ ОСЕВАЯ ТАЛИЯ (Р4/Р5) ─
    # Узел стоит. Две пробы: (1) конец — в талию грани плана Р5, маршрут
    # строит лестница редактора (колено за клапан; отказ или пометка
    # дефектом — пробы нет); (2) мини-жест узла без сдвига — посадка
    # редактора по тому же плану и оконная libavoid-сессия с нуджингом.
    for nid, moves in _axial_moves(graph, e):
        for x, key, sk, f, pt in moves:
            x[key] = list(pt)
            x[sk] = _FACE_NAME[f]
            x["waypoints"] = []
        routed = True
        for x, *_rest in moves:
            if route_fn is not None and not is_straight(x):
                if not route_fn(x) or x.get("_route_defect"):
                    routed = False
                    break
        if routed:
            judge("осевая талия", gate=accepts_axial)
        else:
            restore()
        if reseat_fn is not None:
            reseat_fn(nid)
            judge("осевая талия", gate=accepts_axial)

    # ── 0б. «б»: КЛАПАН ИЛИ КОННЕКТОР НА ЛИНИЮ ТРУБЫ (Р4, блок 6) ──────
    # Клапан — поперёк оси на «линия − талия» (`_line_moves`); проба
    # принимается, только если гейт её пропускает И изломов стало меньше.
    for role, nid, kind, delta, line in _line_moves(
            graph, e, step, sidx, budget, big_area):
        axis = step[0]
        key = "source_point" if role == "s" else "target_point"
        dx, dy = (0.0, delta) if axis == "y" else (delta, 0.0)
        _shift_node(graph, nid, dx, dy)
        cur = live_edge()
        if cur is not None:
            p = list(cur[key])
            i = 0 if axis == "y" else 1          # [y, x]
            run_d = line - (p[i] - delta)        # от конца ДО сдвига
            if kind == "узел":
                p[i] = line                      # конец — ровно на линию
                cur[key] = p
            if len(pts0) <= 4:
                cur["waypoints"] = []
            else:
                shift_run(cur, role, axis, run_d if kind == "узел"
                          else delta, sidx)
                drop_collinear(cur)
            if route_fn is not None and not is_straight(cur):   # И2
                route_fn(cur)
        if reseat_fn is not None:
            reseat_fn(nid)
        judge(kind, fewer_bends=True)

    # ── 1. КОЛЕНО: ничего не двигаем ───────────────────────────────────
    # Гейта И2 здесь НЕТ и он не нужен: кандидатом ребро становится только
    # косым или со ступенькой (см. отбор в `smooth`), то есть ПРЯМЫМ сюда
    # не приходит — замер по 20 холстам корпуса дал 0 срабатываний из 95
    # (§MEFX4A). Ставить его тут значит завести мёртвую ветку.
    if route_fn is not None:
        cur = live_edge()
        if cur is not None and route_fn(cur):
            judge("колено")
        else:
            restore()               # отказ лестницы тоже мог писать в ребро

    # ── 1б. ПОДТЯНУТЬ ИЗЛОМ: тоже ничего не двигаем, кроме колена ──────
    cur = live_edge()
    if cur is not None and snap_waypoints(cur, budget):
        judge("излом")

    if step is None:
        # многоступенчатый / косая внутри маршрута — оператору
        return done("нет кандидата")
    axis, d = step
    if d > budget:
        return done("сверх бюджета")   # «супер движение» — в очаги

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
            # И2 (решение Максима №5): роутер не зовётся поверх УЖЕ ПРЯМОЙ
            # трубы — сдвиг только что её выпрямил, улучшать нечего, а
            # маршрут, построенный заново, стоит лишней libavoid-сессии и
            # умеет отменить готовый ход: гейт видит новые косые и
            # откатывает всё лекарство. Граница именно `is_straight`, а не
            # `is_ortho` — см. докстринг предиката.
            if route_fn is not None and not is_straight(cur):   # И2
                route_fn(cur)
            judge("скольжение")

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
                if route_fn is not None and not is_straight(cur):   # И2
                    route_fn(cur)
            if reseat_fn is not None:
                reseat_fn(nid)
            judge("коннектор")

    # ── 3б. «е»: КЛАПАН ВДОЛЬ ОСИ ПРИ НЕХВАТКЕ КЛИРЕНСА (Р4, блок 6) ───
    # Стаб из талии короче клиренса лестницы маршрута — колену у клапана
    # негде встать; клапан отходит от следующей точки вдоль своей оси
    # (`_clearance_moves`), маршрут и мини-жест — как у сдвига узла.
    for role, nid, dx, dy in _clearance_moves(graph, e, budget, big_area):
        _shift_node(graph, nid, dx, dy)
        cur = live_edge()
        if cur is not None and route_fn is not None and not is_straight(cur):
            route_fn(cur)
        if reseat_fn is not None:
            reseat_fn(nid)
        judge("узел")

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
                    if route_fn is not None and not is_straight(cur):   # И2
                        route_fn(cur)
                if reseat_fn is not None:
                    reseat_fn(nid)
                judge("узел")

    return done("лестница исчерпана")

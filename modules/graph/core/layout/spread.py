# -*- coding: utf-8 -*-
"""spread.py — раздвигание схлопнутых труб после осевой расстановки.

Источник: стенд `_scratch/layout_align/spread_E2_skew.py` (строки 249-1566).
Перенесено без изменения логики — см. docs/planning/AUTO_LAYOUT_INTEGRATION.md, Э2.

Расстановка оставляет часть труб невидимыми (зазор между формами < FLOOR).
Слой чинит их лестницей механизмов (куст -> каскад -> расталкивание ->
выпрямление -> составной ход -> расцепление -> перенос полос); каждый ход
проходит приёмку из 10 условий, любое нарушение — откат вместе с точками.

Два правила, на которых всё держится (стоили стенду многих итераций):
  * любой запрет сравнивается С БАЗОЙ ВХОДА, а не с нулём;
  * сторож обязан мерить то же, что судья — посадка концов делается ВНУТРИ
    пробы, а не в конце.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict

from . import _gate as topo_gate
from . import _shapes
from ._gap import FLOOR, gap_and_seam, shape_box
from ..graph_access import (edge_ends, edge_polyline, edges, is_connector,
                            move_node, node_cxy, nodes_by_id)
from ..seating import reseat_all_endpoints, reseat_edge

logger = logging.getLogger(__name__)

TARGET = 12.0     # px: желаемая длина трубы (порог заказчика)
TAIL_MAX = 12     # узлов: до этого размера сторона считается «концевой веткой»
AXIS_TOL = 1.0    # px: допуск соосности
CANVAS = (1920.0, 1080.0)
MAGI = 60.0       # px: «чужая труба» для запрета «бокс на магистрали»
SKEW_MAX = 16.0   # px: скос, который считаем «почти осевым» и выпрямляем.
                  # База: 5.0. РАЗВЁРТКА (a6d28736, 51b339ab везде 10):
                  #   5 -> 9 (0 попыток: весь косой остаток круче порога)
                  #   8 -> 7, 12 -> 7 (результат бит-в-бит тот же)
                  #  16 -> 6, 20 -> 6, 24 -> 6 — НАСЫЩЕНИЕ на 16, и при 20/24
                  #  раскладка совпадает с 16 бит-в-бит: лишние попытки
                  #  (9 и 12 против 6) все отклонены приёмкой.
                  # Ключ --skew-max.
DEBUG_COMP = False   # ключ --debug-comp: печатать, чем отклонён составной ход
COMPOUND_MAX = 40.0  # px: скос, до которого имеет смысл СОСТАВНОЙ ход. Выше —
                     # это уже честная диагональ схемы, а не сбитая ось; её
                     # выпрямление увезёт узел из его ряда (ключ --compound-max)
PASSES = 14       # проходов по списку дефектов (сходится сам:
                  # цикл рвётся, когда за полный проход 0 принятых.
                  # На 4 обрывался недосходившимся — замер edge_947:
                  # ход был возможен, но до него не доходила очередь)

# ─── B_fill: параметры полосового переноса места ───
GRP_TOL = 2.0      # px: узлы с координатой в пределах допуска — один ряд
BAND_REACH = 600.0  # px: докуда искать компенсирующий разрез. Пробы: 350 ->
                    # (2, 12) дефектов, 600 -> (2, 9), 900 -> (2, 9). На 600
                    # НАСЫЩЕНИЕ: дальше место уже не нужно, то есть ходы и так
                    # остаются локальными. Порядок проб — от БЛИЖНЕГО разреза,
                    # так что 600 — это предел фолбэка, а не обычная дальность.
BAND_TRIES = 16     # компенсирующих разрезов на цель (ближние — первыми)
BAND_ROUNDS = 3     # раундов на ось (после раунда группы пересобираются)


# ───────────────────────── геометрия состояния ─────────────────────────

def adjacency(graph):
    adj = defaultdict(list)
    for e in edges(graph):
        s, t = edge_ends(e)
        adj[s].append((t, e))
        adj[t].append((s, e))
    return adj


def cent_orient(e, byid, tol=AXIS_TOL):
    """Ориентация ребра по ЦЕНТРОИДАМ: 'H' | 'V' | 'D' | 'P' | None."""
    s, t = edge_ends(e)
    a, b = byid.get(s), byid.get(t)
    if a is None or b is None:
        return None
    ax, ay = node_cxy(a)
    bx, by = node_cxy(b)
    dx, dy = abs(bx - ax), abs(by - ay)
    if dx <= tol and dy <= tol:
        return "P"
    if dy <= tol:
        return "H"
    if dx <= tol:
        return "V"
    return "D"


def pts_orient(e, tol=AXIS_TOL):
    """Ориентация НАРИСОВАННОГО отрезка: 'H'|'V'|'D'|'P'|None."""
    pl = edge_polyline(e)
    if len(pl) < 2:
        return None
    (x1, y1), (x2, y2) = pl[0], pl[-1]
    dx, dy = abs(x2 - x1), abs(y2 - y1)
    if dx <= tol and dy <= tol:
        return "P"
    if dy <= tol:
        return "H"
    if dx <= tol:
        return "V"
    return "D"


def move_axis(e, byid, o_byid, want_source=False):
    """Ось РАЗДВИГАНИЯ: 'Y' для вертикального ребра, 'X' для горизонтального.

    Порядок источников:
      1. ЦЕНТРОИДЫ v16 — основной (узлы стоят там, где стоят их оси);
      2. если центроиды дали 'P' (совпали) — ОРИГИНАЛ;
      3. если центроиды дали 'D' — НАРИСОВАННЫЕ точки. Это не костыль:
         у ребра к крупному блоку центроид блока лежит глубоко внутри, и по
         центроидам получается диагональ, хотя труба входит в бок строго по
         оси. Замер 8d517a35: 45 отказов «ось не определена», и у 8 из 15
         остаточных дефектов точки дают честные H/V.
    want_source=True -> (axis, 'cent'|'orig'|'pts'): знак хода надо считать
    по ТОМУ ЖЕ источнику, иначе поедем не в ту сторону.
    """
    o, src = cent_orient(e, byid), "cent"
    if o not in ("H", "V"):
        op = pts_orient(e)                      # 2. нарисованные точки
        if op in ("H", "V"):
            o, src = op, "pts"
    if o not in ("H", "V"):
        oo = cent_orient(e, o_byid)             # 3. ОРИГИНАЛ — эталон намерения
        if oo in ("H", "V"):
            o, src = oo, "orig"
    axis = "Y" if o == "V" else "X" if o == "H" else None
    return (axis, src) if want_source else axis


def pierced(nid, axis, byid, adj, tol=AXIS_TOL):
    """Лежит ли узел на сквозной прямой, ПЕРПЕНДИКУЛЯРНОЙ оси хода.

    Такой узел двигать вдоль axis нельзя: прямая сломается в зигзаг.
    Ход по Y -> смотрим горизонтальные связи в обе стороны (и наоборот).
    """
    n = byid.get(nid)
    if n is None:
        return False
    cx, cy = node_cxy(n)
    neg = pos = False
    for v, _e in adj.get(nid, ()):
        m = byid.get(v)
        if m is None:
            continue
        mx, my = node_cxy(m)
        if axis == "Y":
            if abs(my - cy) <= tol and abs(mx - cx) > tol:
                neg |= mx < cx
                pos |= mx > cx
        else:
            if abs(mx - cx) <= tol and abs(my - cy) > tol:
                neg |= my < cy
                pos |= my > cy
    return neg and pos


GROW_MAX = 80     # узлов: предохранитель против куста размером в пол-листа


def group_of(seed, axis, byid, adj, free=True, grow_max=GROW_MAX,
             block_e=None):
    """Куст, который едет вместе. None — ход невозможен.

    block_e (D3) — id() ребра, ЧЕРЕЗ КОТОРОЕ рост не идёт. Нужно только для
    хода поперёк ребра: иначе куст увозит второй конец, и формы не
    расцепляются. При обычном ходе block_e=None, поведение базы.

    free=True (по умолчанию): рост по ПЕРПЕНДИКУЛЯРНЫМ ходу рёбрам идёт до
    конца цепочки, БЕЗ запрета на «прошитые» узлы. Почему запрет был лишним:
    если едет ВСЯ горизонтальная цепочка, ни одно H-ребро не наклоняется, а
    вертикальные связи от сдвига по Y не ломаются вообще — узел не меняет X,
    поэтому прямая, проходящая через него, остаётся прямой и просто тянется.
    Замер: старое правило рвало куст от node_779 на первом шаге, на клапане
    node_786 (две H-связи: к node_779 и node_787), хотя это обычная короткая
    ветка, которая обязана ехать целиком. Единственная реальная проверка —
    наложения у того, что едет, её делает приёмка.

    free=False — прежнее поведение (стоп на прошитом узле), оставлено для
    сравнения замером.
    """
    perp = "H" if axis == "Y" else "V"
    grp, stack = {seed}, [seed]
    while stack:
        u = stack.pop()
        for v, e in adj.get(u, ()):
            if block_e is not None and id(e) == block_e:
                continue
            if v in grp or cent_orient(e, byid) != perp:
                continue
            if not free and pierced(v, axis, byid, adj):
                return None
            if len(grp) >= grow_max:
                return None
            grp.add(v)
            stack.append(v)
    return grp


def grow_cascade(seed, axis, sgn, delta, byid, adj, floor=FLOOR,
                 grow_max=GROW_MAX, rounds=40, block_e=None):
    """Куст + КАСКАД: везём и тех, кого сдвиг иначе сжал бы. None — нельзя.

    Зачем. Сдвинуть клапан вниз мешает не место, а следующий за ним элемент:
    ребро к нему укорачивается ниже порога, суммарный счёт дефектов не падает,
    и строгая приёмка ход отклоняет (замер edge_160 -> ломалось edge_199).
    Лечение — толкать цепочку: втягиваем соседа ПО ОСИ ХОДА, если он стоит
    впереди по направлению движения И запаса его ребра не хватает на delta.
    Втянутый сосед приезжает вместе со своим перпендикулярным кустом, иначе
    наклонятся его H-рёбра. Позади стоящие не втягиваются: их рёбра тянутся.
    """
    grp = group_of(seed, axis, byid, adj, grow_max=grow_max, block_e=block_e)
    if grp is None:
        return None
    along = "V" if axis == "Y" else "H"
    k = 1 if axis == "Y" else 0
    for _r in range(rounds):
        add = set()
        for u in grp:
            pu = node_cxy(byid[u])[k]
            for v, e in adj.get(u, ()):
                if block_e is not None and id(e) == block_e:
                    continue      # D3: через целевое ребро каскад не тянет
                if v in grp or cent_orient(e, byid) != along:
                    continue
                if sgn * (node_cxy(byid[v])[k] - pu) <= 0:
                    continue                      # позади хода — ребро тянется
                g, _p, _q = gap_and_seam(shape_box(byid[u]), shape_box(byid[v]))
                if g - delta >= floor:
                    continue                      # запаса хватает без каскада
                sub = group_of(v, axis, byid, adj, grow_max=grow_max,
                               block_e=block_e)
                if sub is None:
                    return None
                add |= sub
        add -= grp
        if not add:
            return grp
        if len(grp) + len(add) > grow_max:
            return None
        grp |= add
    return grp


def box_on_magi_drawn(graph, byid, magi=None, shrink=2.0):
    """Боксы, пересечённые чужой трубой — ПО НАРИСОВАННОЙ полилинии.

    Повторяет геометрию арбитра (triggers.detect: сжатие bbox на 2px, порог
    длины MAGI, полилиния source_point->waypoints->target_point), но через
    STRtree — в приёмке она зовётся сотни раз. Нужна отдельно от
    box_on_pipe_depth, которая считает по линии центроид->центроид: замер
    a6d28736 показал, что эти две метрики расходятся до противоположных
    выводов (по центроидам стало лучше 93.8->79.3, по арбитру +1 бокс).
    -> множество id боксов.
    """
    from shapely.geometry import LineString, box as shp_box
    from shapely.strtree import STRtree
    magi = MAGI if magi is None else magi
    bx = [(k, b) for k, b in _boxes(graph)
          if b[2] - b[0] > 2 * shrink and b[3] - b[1] > 2 * shrink]
    if not bx:
        return set()
    geoms = [shp_box(b[0] + shrink, b[1] + shrink, b[2] - shrink,
                     b[3] - shrink) for _k, b in bx]
    keys = [k for k, _b in bx]
    tree = STRtree(geoms)
    out = set()
    for e in edges(graph):
        pl = edge_polyline(e)
        if len(pl) < 2:
            continue
        if sum(math.hypot(pl[i + 1][0] - pl[i][0], pl[i + 1][1] - pl[i][1])
               for i in range(len(pl) - 1)) <= magi:
            continue
        s2, t2 = edge_ends(e)
        ls = LineString(pl)
        for j in tree.query(ls, predicate="intersects"):
            j = int(j)
            if keys[j] in (s2, t2) or not ls.intersects(geoms[j]):
                continue
            out.add(keys[j])
    return out


def coaxial(nid, axis, byid, tol=AXIS_TOL):
    """Узлы на ТОЙ ЖЕ оси, что и nid, поперёк хода (его ряд/колонка).

    Ход по Y -> одинаковая cy, то есть горизонтальный ряд. Везти их надо
    вместе, даже если они не связаны рёбрами: иначе ряд разъедется по высоте
    (заказчик про мешающих соседей: «они же на оси»).
    """
    k = 1 if axis == "Y" else 0
    n = byid.get(nid)
    if n is None:
        return {nid}
    c = node_cxy(n)[k]
    return {m for m, v in byid.items()
            if "centroid" in v and abs(node_cxy(v)[k] - c) <= tol}


def overlap_partners(graph, grp):
    """Узлы ВНЕ grp, с чьими боксами боксы grp сейчас перекрываются."""
    from shapely.geometry import box as shp_box
    from shapely.strtree import STRtree
    bx = _boxes(graph)
    if not bx:
        return set()
    geoms = [shp_box(*b) for _k, b in bx]
    keys = [k for k, _b in bx]
    tree = STRtree(geoms)
    out = set()
    for i, g in enumerate(geoms):
        if keys[i] not in grp:
            continue
        for j in tree.query(g, predicate="intersects"):
            j = int(j)
            if keys[j] in grp:
                continue
            if g.intersection(geoms[j]).area > 1e-9:
                out.add(keys[j])
    return out


def grow_push(seed_grp, axis, delta, byid, adj, graph, rounds=3,
              grow_max=GROW_MAX, block_e=None):
    """РАСТАЛКИВАНИЕ: втянуть тех, в кого сдвиг упирается. None — нельзя.

    Каскад везёт соседей ПО РЁБРАМ, а мешать может узел, с которым связи нет
    вовсе — он просто стоит рядом. Такой ход раньше отклонялся по «наложение
    боксов». Здесь мешающий втягивается вместе со СВОЕЙ осью (coaxial) и своим
    перпендикулярным кустом, то есть едет целым рядом, а не в одиночку.
    """
    grp = set(seed_grp)
    dx, dy = _vec(axis, delta)
    for _r in range(rounds):
        for k in grp:
            move_node(byid[k], dx, dy)
        hits = overlap_partners(graph, grp)
        for k in grp:
            move_node(byid[k], -dx, -dy)
        if not hits:
            return grp
        add = set()
        for h in hits:
            add |= coaxial(h, axis, byid)
            g2 = group_of(h, axis, byid, adj, grow_max=grow_max,
                          block_e=block_e)
            if g2:
                add |= g2
        add -= grp
        if not add or len(grp) + len(add) > grow_max:
            return None
        grp |= add
    return None


def unlock_axes(a_box, b_box):
    """D1+D2: чем и на сколько РАСЦЕПИТЬ перекрывшиеся формы.

    -> [(axis, depth, sgn_s), ...], дешёвая ось первой; [] — формы не
    перекрыты ни по одной оси (расцеплять нечего).

    depth — проникновение ПО ЭТОЙ ОСИ: ход на depth + floor гарантированно
    даёт зазор floor, потому что после расхождения по одной оси зазор равен
    расхождению по ней (по второй оси формы всё ещё против друг друга, и её
    вклад в зазор нулевой). sgn_s — знак хода для КОНЦА-ИСТОЧНИКА (a): в ту
    сторону, откуда он ближе к кромке b, то есть по дешёвому направлению.
    Геометрия та же, что в gap_and_seam: ox/oy <= 0 означает перекрытие.
    """
    ax1, ay1, ax2, ay2 = a_box
    bx1, by1, bx2, by2 = b_box
    lx, rx = bx1 - ax2, ax1 - bx2          # a слева от b / a справа от b
    ty, by_ = by1 - ay2, ay1 - by2
    ox, oy = max(lx, rx), max(ty, by_)
    if ox > 0.0 or oy > 0.0:
        return []
    out = [("X", -ox, -1.0 if lx >= rx else 1.0),
           ("Y", -oy, -1.0 if ty >= by_ else 1.0)]
    out.sort(key=lambda z: z[1])
    return out


def solo_ok(nid, axis, byid, adj):
    """Механизм «а»: узел можно двигать один, если перпендикулярных рёбер нет."""
    perp = "H" if axis == "Y" else "V"
    return not any(cent_orient(e, byid) == perp
                   for _v, e in adj.get(nid, ()))


def side_size(e, side, adj, limit):
    """Размер компоненты со стороны side при мысленном удалении ребра.

    Обход прекращается на limit+1 узле: точное число не нужно, нужен ответ
    «это концевая ветка или пол-графа».
    """
    s, t = edge_ends(e)
    start, banned = (s, t) if side == "src" else (t, s)
    eid = id(e)
    seen, stack = {start}, [start]
    while stack and len(seen) <= limit:
        u = stack.pop()
        for v, ee in adj.get(u, ()):
            if id(ee) == eid or v in seen:
                continue
            if v == banned:
                continue
            seen.add(v)
            stack.append(v)
    return len(seen)


# ────────────────────────── измерители запретов ──────────────────────────

def _boxes(graph):
    out = []
    for n in graph.get("nodes", []):
        if "centroid" not in n or is_connector(n):
            continue
        bb = n.get("bbox")
        if bb and len(bb) == 4 and bb[2] > bb[0] and bb[3] > bb[1]:
            out.append((n["id"], tuple(bb)))
    return out


def _frozen_nodes(graph, legal=None):
    """Узлы НЕЛЕГАЛЬНО наложенных пар — их слой не двигает.

    Форма узла — нарисованная (`segmentation`, иначе bbox), пары из
    `legal` (наложены в детектированной геометрии) не считаются: их право
    там быть — решение заказчика, и замораживать из-за них соседей нельзя.
    """
    from shapely.strtree import STRtree
    ids, geoms = [], []
    for n in graph.get("nodes", []):
        if "centroid" not in n or is_connector(n):
            continue
        g = _shapes.shape_of(n)
        if g is None or g.is_empty or g.area <= 0.0:
            continue
        ids.append(n["id"])
        geoms.append(g)
    if not geoms:
        return set()
    legal = legal or ()
    tree = STRtree(geoms)
    out = set()
    for i, g in enumerate(geoms):
        for j in tree.query(g, predicate="intersects"):
            j = int(j)
            if j <= i or g.intersection(geoms[j]).area <= 1e-9:
                continue
            if tuple(sorted((ids[i], ids[j]))) in legal:
                continue
            out.add(ids[i])
            out.add(ids[j])
    return out


def overlaps(graph, legal=None):
    """Число перекрывающихся пар (касание НЕ считается).

    Меряет ТО ЖЕ, ЧТО СУДЬЯ: по нарисованной форме узла и без ЛЕГАЛЬНЫХ
    пар. Габаритный счёт врал в обе стороны — засчитывал наложением узел
    в пустом углу невыпуклого контура и не отличал законную пару из
    детекции от созданной ходом.
    """
    from shapely.strtree import STRtree
    ids, geoms = [], []
    for n in graph.get("nodes", []):
        if "centroid" not in n or is_connector(n):
            continue
        g = _shapes.shape_of(n)
        if g is None or g.is_empty or g.area <= 0.0:
            continue
        ids.append(n["id"])
        geoms.append(g)
    if not geoms:
        return 0
    legal = legal or ()
    tree = STRtree(geoms)
    cnt = 0
    for i, g in enumerate(geoms):
        for j in tree.query(g, predicate="intersects"):
            j = int(j)
            if (j > i and g.intersection(geoms[j]).area > 1e-9
                    and tuple(sorted((ids[i], ids[j]))) not in legal):
                cnt += 1
    return cnt


PEN_TOL = 12.0    # px: допуск на СУММАРНОЕ ухудшение глубины по всему листу.
                  # Жёсткий ноль запрещал бы ход, который портит пару пикселей
                  # в одном месте, но снимает дефект в другом (решение
                  # заказчика: «не прям сильно»). 12px ~ один клапан.


def penetration_sum(graph, byid):
    """Суммарная ГЛУБИНА взаимного проникновения форм концов по всем рёбрам."""
    tot = 0.0
    for e in edges(graph):
        s, t = edge_ends(e)
        a, b = byid.get(s), byid.get(t)
        if a is None or b is None:
            continue
        g, _p1, _p2 = gap_and_seam(shape_box(a), shape_box(b))
        if g < 0.0:
            tot -= g
    return tot


def box_on_pipe_depth(graph, byid, magi=None):
    """(пары «бокс x чужая труба», СУММАРНАЯ глубина захода трубы в боксы).

    Глубина одной пары — расстояние от трубы до ближайшей ПАРАЛЛЕЛЬНОЙ ей
    кромки бокса: труба по кромке даёт 0, труба посередине — максимум. Именно
    это различие бинарный счётчик пар не видел (магистраль y=696.7 лежала на
    кромке bbox 654.7..696.7, а после хода пошла внутри 666.7..708.7).
    """
    from shapely.geometry import LineString, box as shp_box
    from shapely.strtree import STRtree
    magi = MAGI if magi is None else magi
    bx = _boxes(graph)
    geoms = [shp_box(*b) for _k, b in bx]
    keys = [k for k, _b in bx]
    tree = STRtree(geoms)
    pairs, depth = set(), 0.0
    for e in edges(graph):
        s, t = edge_ends(e)
        a, b = byid.get(s), byid.get(t)
        if a is None or b is None:
            continue
        ax, ay = node_cxy(a)
        bx2, by2 = node_cxy(b)
        if math.hypot(bx2 - ax, by2 - ay) <= magi:
            continue
        ls = LineString([(ax, ay), (bx2, by2)])
        for j in tree.query(ls, predicate="intersects"):
            j = int(j)
            if keys[j] in (s, t):
                continue
            x1, y1, x2, y2 = bx[j][1]
            pairs.add((str(e.get("id")), keys[j]))
            if abs(by2 - ay) <= AXIS_TOL:
                depth += max(0.0, min(ay - y1, y2 - ay))
            elif abs(bx2 - ax) <= AXIS_TOL:
                depth += max(0.0, min(ax - x1, x2 - ax))
    return pairs, depth


def box_on_pipe(graph, byid):
    """Пары «бокс x ЧУЖАЯ труба длиннее MAGI» по линии центроид->центроид."""
    from shapely.geometry import LineString, box as shp_box
    from shapely.strtree import STRtree
    bx = _boxes(graph)
    geoms = [shp_box(*b) for _k, b in bx]
    keys = [k for k, _b in bx]
    tree = STRtree(geoms)
    out = set()
    for e in edges(graph):
        s, t = edge_ends(e)
        a, b = byid.get(s), byid.get(t)
        if a is None or b is None:
            continue
        ax, ay = node_cxy(a)
        bx2, by2 = node_cxy(b)
        if math.hypot(bx2 - ax, by2 - ay) <= MAGI:
            continue
        ls = LineString([(ax, ay), (bx2, by2)])
        for j in tree.query(ls, predicate="intersects"):
            j = int(j)
            if keys[j] in (s, t):
                continue
            out.add((str(e.get("id")), keys[j]))
    return out


def defects(graph, byid, floor=FLOOR):
    """Рёбра с зазором форм < floor. -> {edge_id: gap}."""
    out = {}
    for e in edges(graph):
        s, t = edge_ends(e)
        a, b = byid.get(s), byid.get(t)
        if a is None or b is None:
            continue
        g, _p1, _p2 = gap_and_seam(shape_box(a), shape_box(b))
        if g < floor:
            out[str(e.get("id"))] = g
    return out


def new_diagonals(graph, byid, o_byid):
    """Рёбра, ставшие диагональными там, где в ОРИГИНАЛЕ были H/V.

    Классификация — ТЕМ ЖЕ критерием, что у независимого арбитра topo_gate
    (осевое = угол <= 20 град от оси при длине >= 6px), а НЕ «отклонение более
    1px». Иначе почти-осевое ребро оригинала выпадает из-под охраны и алгоритм
    доводит его до настоящей диагонали: замер a6d28736, edge_82 — в оригинале
    dx=-30.7 dy=103.2 (скос 16.6 град), арбитр считает V, моя прежняя метрика
    считала D («не мой случай»), и ход наклонил ребро до 21.9 град. На 51b
    расхождения не было (вход ортогонален на 97.9%), на a6d28736 — 5 рёбер
    (вход 93.3%). Сторож обязан мерить то же, что судья.
    """
    n = 0
    for e in edges(graph):
        if topo_gate._edge_orient(e, o_byid) not in ("H", "V"):
            continue
        if topo_gate._edge_orient(e, byid) == "D":
            n += 1
    return n


MARGIN = 0.0      # px: поле («рамка») — узлы не выходят за него. Задаётся
                  # ключом --margin и должно совпадать с inset расстановки:
                  # v16 кладёт внутрь поля, а этот слой иначе выталкивает узлы
                  # обратно на кромку (замер: при поле 12px граф всё равно
                  # касался верха, отступ сверху оставался 2.5px).


def margin_violation(graph, canvas=CANVAS, margin=None):
    """Суммарный ВЫХОД боксов за поле, px. 0 — все внутри рамки."""
    m = MARGIN if margin is None else margin
    tot = 0.0
    for _k, bb in _boxes(graph):
        tot += (max(0.0, m - bb[0]) + max(0.0, m - bb[1])
                + max(0.0, bb[2] - (canvas[0] - m))
                + max(0.0, bb[3] - (canvas[1] - m)))
    return tot


def in_canvas(graph, canvas=CANVAS, margin=None):
    """Совместимость: True, если за рамку никто не выходит."""
    return margin_violation(graph, canvas, margin) <= 1e-9


# ─────────────── B_fill: ряды/колонки, разрезы и их слак ───────────────

def axis_groups(byid, ids, axis, tol=GRP_TOL):
    """РЯДЫ (axis='Y') или КОЛОНКИ ('X'): узлы с близкой координатой.

    Ход полосой двигает ряд ЦЕЛИКОМ — иначе узел выпадет из своей оси, а
    рёбра внутри ряда наклонятся. Группы отсортированы по координате, между
    соседними — разрез с тем же индексом, что у левой/верхней группы.
    -> (groups, gidx, gcoord)
    """
    k = 1 if axis == "Y" else 0
    order = sorted(ids, key=lambda i: node_cxy(byid[i])[k])
    groups, gidx, sums = [], {}, []
    prev = None
    for i in order:
        v = node_cxy(byid[i])[k]
        if prev is None or v - prev > tol:
            groups.append([])
            sums.append([])
        groups[-1].append(i)
        sums[-1].append(v)
        gidx[i] = len(groups) - 1
        prev = v
    return groups, gidx, [sum(s) / len(s) for s in sums]


def box_slack(byid, ids, gidx, ncuts, axis):
    """На сколько можно СЖАТЬ каждый разрез, не столкнув формы.

    Учитываются только пары форм, у которых пересекаются проекции на
    ПЕРПЕНДИКУЛЯРНУЮ ось: те, что разъехались вбок, при сжатии не встретятся.
    """
    import numpy as np
    if ncuts <= 0:
        return np.zeros(0)
    B = np.array([shape_box(byid[i]) for i in ids], dtype=float)
    g = np.array([gidx[i] for i in ids])
    if axis == "Y":
        a1, a2, p1, p2 = B[:, 1], B[:, 3], B[:, 0], B[:, 2]
    else:
        a1, a2, p1, p2 = B[:, 0], B[:, 2], B[:, 1], B[:, 3]
    povl = (p1[:, None] < p2[None, :]) & (p1[None, :] < p2[:, None])
    clear = a1[None, :] - a2[:, None]      # [i,k]: k ниже/правее i
    lo = g[:, None]
    hi = g[None, :]
    out = np.full(ncuts, 1e9)
    for j in range(ncuts):
        m = povl & (lo <= j) & (hi > j)
        if m.any():
            out[j] = clear[m].min()
    return out


def edge_slack(graph, byid, gidx, ncuts, floor=FLOOR):
    """На сколько можно сжать разрез, не сделав дефектным ни одно ребро."""
    out = [1e9] * ncuts
    for e in edges(graph):
        s, t = edge_ends(e)
        a, b = byid.get(s), byid.get(t)
        if a is None or b is None:
            continue
        gs, gt = gidx.get(s), gidx.get(t)
        if gs is None or gt is None or gs == gt:
            continue
        g, _p, _q = gap_and_seam(shape_box(a), shape_box(b))
        room = g - floor
        for j in range(min(gs, gt), max(gs, gt)):
            if room < out[j]:
                out[j] = room
    return out


def seam_cuts(e, byid, axis, gidx, gcoord):
    """Разрезы, расширение которых реально раздвинет концы ребра e.

    Берутся только те, что попадают в ЩЕЛЬ между формами (или между
    центроидами, если формы уже перекрылись): разрез внутри бокса расширять
    бессмысленно — бокс поедет вместе с рядом.
    """
    s, t = edge_ends(e)
    a, b = byid.get(s), byid.get(t)
    gs, gt = gidx.get(s), gidx.get(t)
    if a is None or b is None or gs is None or gt is None or gs == gt:
        return []
    k = 1 if axis == "Y" else 0
    lo, hi = (gs, gt) if gs < gt else (gt, gs)
    na, nb = (a, b) if gs < gt else (b, a)
    sa, sb = shape_box(na), shape_box(nb)
    c1, c2 = sa[k + 2], sb[k]                 # низ верхней формы, верх нижней
    if c1 > c2:                               # формы перекрылись — по центроидам
        c1, c2 = node_cxy(na)[k], node_cxy(nb)[k]
    inside = [j for j in range(lo, hi)
              if c1 - 1.0 <= (gcoord[j] + gcoord[j + 1]) / 2.0 <= c2 + 1.0]
    if inside:
        return inside
    mid = (c1 + c2) / 2.0
    return sorted(range(lo, hi),
                  key=lambda j: abs((gcoord[j] + gcoord[j + 1]) / 2.0 - mid))[:2]


# ──────────────────────────── раздвигание ────────────────────────────

def spread(graph, orig, floor=FLOOR, target=TARGET, passes=PASSES,
           verbose=True, base_v16=None, band="both", unlock_zero=False,
           compound=False, comp_push=False, legal=None):
    """base_v16 задан -> в приёмку добавляется ЗАПРЕТ ПЕРЕСТАНОВОК.

    Замер показал, зачем он нужен: без него узел за несколько проходов уезжал
    на 30 px и перепрыгивал соседний ряд (34 пары с исходной дельтой > 20 px).
    Порядок «левее/выше» vs v16 — это узнаваемость схемы, её терять нельзя.
    """
    byid, o_byid = nodes_by_id(graph), nodes_by_id(orig)
    adj = adjacency(graph)
    e_by_id = {str(e.get("id")): e for e in edges(graph)}

    base_bop, base_pipe_pen = box_on_pipe_depth(graph, byid)
    base_magi = box_on_magi_drawn(graph, byid)
    base_pen = penetration_sum(graph, byid)
    base_ovl = overlaps(graph, legal)
    # БАЗА ДИАГОНАЛЕЙ, а не абсолютный ноль: v16 сам оставляет диагонали
    # относительно оригинала (a6d28736 — 7, 8d517a35 — 6, 13d1ef5f — 3,
    # 6e7144d5 — 1; на 51b случайно 0). Абсолютное «> 0» блокировало ВСЕ ходы
    # на таких графах: условие нарушено ещё до первого хода.
    base_diag = new_diagonals(graph, byid, o_byid)
    # НЕ «внутри рамки», а «не хуже, чем было»: расстановка с полем кладёт
    # часть боксов ровно на границу, абсолютная проверка отвергала ВСЕ ходы
    # (замер a6d28736 с полем 24px: 128 -> 128, ноль принятых).
    base_marg = margin_violation(graph)
    # узлы с наложением не трогаем вовсе (правило заказчика). Считаем тем же
    # судьёй, что и приёмка: по РЕАЛЬНОЙ ФОРМЕ и без ЛЕГАЛЬНЫХ пар. Прежний
    # предикат был габаритным и амнистии не знал — на 13d1ef5f из 15
    # замороженных пар 10 законны (узел стоит на баке ещё в детектированной
    # геометрии), и слой не чинил трубы, которые чинить можно.
    frozen = _frozen_nodes(graph, legal)

    stats = {"accepted": 0, "by_group": 0, "by_solo": 0,
             "by_cascade": 0, "by_push": 0, "aligned": 0, "symmetric": 0,
             "rejected": 0, "no_axis": 0, "frozen": 0, "no_mover": 0,
             "band_pre": 0, "band_post": 0, "band_tried": 0,
             "by_unlock": 0, "unlock_tried": 0,
             "by_compound": 0, "comp_tried": 0, "align_tried": 0, "moves": []}

    # ─── опоры для ЛОКАЛЬНОГО ГЕЙТА полосовых ходов ───
    # base_v16 нет (--free-order) -> гейт выключается: сравнивать не с чем.
    ortho_long, allowed_sides = set(), {}
    if base_v16 is not None:
        v16_e = {str(e2.get("id")): e2 for e2 in edges(base_v16)}
        orig_e = {str(e2.get("id")): e2 for e2 in edges(orig)}
        v16_b = nodes_by_id(base_v16)
        for eid2, ve in v16_e.items():
            if topo_gate._drawn_len(ve) > topo_gate.MAGI \
                    and topo_gate._edge_ortho(ve):
                ortho_long.add(eid2)
            oe = orig_e.get(eid2)
            if oe is None:
                continue
            po = {nid: (x, y) for nid, x, y in topo_gate._block_endpoints(oe)}
            pv = {nid: (x, y) for nid, x, y in topo_gate._block_endpoints(ve)}
            for nid in set(po) & set(pv):
                allow = set()
                n0, n1 = o_byid.get(nid), v16_b.get(nid)
                if n0 and n0.get("bbox"):
                    allow |= topo_gate._side_set(*po[nid], n0["bbox"])
                if n1 and n1.get("bbox"):
                    allow |= topo_gate._side_set(*pv[nid], n1["bbox"])
                if allow:
                    allowed_sides[(eid2, nid)] = allow

    def gate_local(t_edges):
        """True — ход ломает прямизну магистрали v16 или сторону входа.

        Базовая приёмка этих двух инвариантов не мерит вовсе, хотя
        check_result считает оба РЕГРЕССИЕЙ, то есть провалом варианта.
        Задуман был как страховка для полосы (она двигает пол-листа), но
        замер a6d28736 показал, что сторону входа ломает и ОБЫЧНЫЙ ход:
        v50B без гейта дал 10 дефектов при side_changed=1. Поэтому гейт
        включён по умолчанию для всех ходов. Проверяются только ЗАТРОНУТЫЕ
        рёбра: у остальных концы не двигались, их вердикт измениться не может.
        """
        if base_v16 is None:
            return False
        for e2 in t_edges:
            eid2 = str(e2.get("id"))
            if eid2 in ortho_long and not topo_gate._edge_ortho(e2):
                return True
            for nid, x, y in topo_gate._block_endpoints(e2):
                allow = allowed_sides.get((eid2, nid))
                if not allow:
                    continue
                n2 = byid.get(nid)
                if n2 is None or is_connector(n2) or not n2.get("bbox"):
                    continue
                if not (topo_gate._side_set(x, y, n2["bbox"]) & allow):
                    return True
        return False

    def try_shift(moves, cur_def, target_eid=None, strict=True,
                  pen_tol=PEN_TOL, extra_gate=True, dbg=None):
        """moves = [(набор узлов, dx, dy)]. -> True, если ход принят.

        target_eid — ребро, ради которого ход делается. Без этой проверки
        принимался ход, который целевое ребро НЕ вылечил, но и суммарно не
        ухудшил: с 7-го прохода алгоритм зацикливался (по 5 принятых ходов
        за проход при неизменных 17 дефектах — узлы гоняло туда-обратно).
        """
        # ЗАТРОНУТЫЕ РЁБРА пересаживаем сразу: во время оптимизации
        # source_point/target_point стоят на месте, и приёмка судила бы по
        # устаревшей линии, а арбитр потом — по фактической. Замер a6d28736:
        # по центроидам ход улучшал (93.8->79.3), а по нарисованной геометрии
        # загонял трубу в бокс с 1.9px до 9.6px, и это ловилось только постфактум.
        t_edges, seen_e = [], set()
        for grp, _dx0, _dy0 in moves:
            for k in grp:
                for _v, e2 in adj.get(k, ()):
                    if id(e2) not in seen_e:
                        seen_e.add(id(e2))
                        t_edges.append(e2)
        saved = [(e2, list(e2.get("source_point") or []),
                  list(e2.get("target_point") or [])) for e2 in t_edges]
        for grp, dx, dy in moves:
            for k in grp:
                move_node(byid[k], dx, dy)
        for e2 in t_edges:
            reseat_edge(byid, e2)
        bop, pipe_pen = box_on_pipe_depth(graph, byid)
        now = defects(graph, byid, floor)
        bad = (margin_violation(graph) > base_marg + 1e-6
               or (target_eid is not None and target_eid in now)
               or overlaps(graph, legal) > base_ovl
               or len(bop - base_bop) > 0
               or bool(box_on_magi_drawn(graph, byid) - base_magi)
               or pipe_pen > base_pipe_pen + pen_tol
               or penetration_sum(graph, byid) > base_pen + pen_tol
               or new_diagonals(graph, byid, o_byid) > base_diag
               # СТРОГО меньше, а не «не больше»: ход, который лечит своё
               # ребро и ломает соседнее (нетто 0), запускал качели — проходы
               # 7..14 принимали по 5 ходов при неизменных 17 дефектах
               or (len(now) >= len(cur_def) if strict
                   else len(now) > len(cur_def))
               or (base_v16 is not None
                   and order_broken_local(graph, base_v16) > 0))
        if not bad and extra_gate:
            bad = gate_local(t_edges)
        if bad and dbg:
            # ЗАМЕР: чем именно отклонён ход. Пересчёт условий по одному —
            # только в отладочном прогоне, на поведение не влияет.
            why = []
            if margin_violation(graph) > base_marg + 1e-6:
                why.append("рамка")
            if target_eid is not None and target_eid in now:
                why.append("целевое не вылечено")
            if overlaps(graph, legal) > base_ovl:
                why.append("наложение боксов")
            if len(bop - base_bop) > 0:
                why.append("труба в чужом боксе")
            if box_on_magi_drawn(graph, byid) - base_magi:
                why.append("бокс-на-магистрали")
            if pipe_pen > base_pipe_pen + pen_tol:
                why.append(f"глубина трубы {pipe_pen - base_pipe_pen:+.1f}")
            if penetration_sum(graph, byid) > base_pen + pen_tol:
                why.append("проникновение форм")
            if new_diagonals(graph, byid, o_byid) > base_diag:
                why.append("новые диагонали")
            if (len(now) >= len(cur_def) if strict else len(now) > len(cur_def)):
                why.append(f"счёт дефектов {len(cur_def)}->{len(now)}")
            if base_v16 is not None and order_broken_local(graph, base_v16) > 0:
                why.append("перестановки")
            if gate_local(t_edges):
                why.append("гейт: сторона входа / прямизна")
            logger.debug("[comp] %s: отказ — %s", dbg, ", ".join(why))
        if bad:
            for grp, dx, dy in moves:
                for k in grp:
                    move_node(byid[k], -dx, -dy)
            for e2, sp2, tp2 in saved:
                if sp2:
                    e2["source_point"] = sp2
                if tp2:
                    e2["target_point"] = tp2
            return False
        return True

    def try_align(e, eid):
        """МИКРО-ВЫРАВНИВАНИЕ: убрать малый скос, сделав ребро осевым.

        Для рёбер, косых во ВСЕХ источниках (центроиды v16, нарисованные точки,
        оригинал) оси хода не существует — раздвигать нечего вдоль. Замер
        8d517a35: таких 7 из 18 остатка, скос 2.5-4.0 px при длине 12-26 px,
        то есть чертёж почти осевой, но не точно.

        Ход ПОДГОТОВИТЕЛЬНЫЙ: длину трубы он почти не меняет и сам дефект не
        лечит — он лишь делает ребро осевым, после чего обычное раздвигание
        получает ось и работает. Поэтому приёмка мягче (strict=False): не
        требуем вылечить целевое ребро, требуем «не хуже» по всем остальным
        запретам. Повторно ход не сработает: у выпрямленного ребра скос 0.
        """
        s2, t2 = edge_ends(e)
        a2, b2 = byid.get(s2), byid.get(t2)
        if a2 is None or b2 is None:
            return False
        ax2, ay2 = node_cxy(a2)
        bx2, by2 = node_cxy(b2)
        dx, dy = bx2 - ax2, by2 - ay2
        axis2, skew = ("Y", dy) if abs(dx) >= abs(dy) else ("X", dx)
        if not (0.01 < abs(skew) <= SKEW_MAX):
            return False
        cur2 = defects(graph, byid, floor)
        stats["align_tried"] += 1
        for who, sgn in ((s2, 1.0), (t2, -1.0)):
            grp = group_of(who, axis2, byid, adj)
            if grp is None:
                continue
            if try_shift([(grp, *_vec(axis2, sgn * skew))], cur2, None,
                         strict=False):
                stats["aligned"] += 1
                stats["moves"].append({"edge": eid, "how": "align",
                                       "axis": axis2, "px": round(skew, 2)})
                return True
        return False

    def try_compound(e, eid):
        """E2-2. СОСТАВНОЙ ХОД: выпрямить по одной оси И раздвинуть по другой.

        Одна проба = ОДНА приёмка обычной строгости (целевое ребро обязано
        вылечиться, суммарный счёт дефектов — упасть). Порядок внутри пробы:
          * куст конца едет ПОПЕРЁК ребра на величину скоса -> ребро осевое;
          * при ВРЕМЕННО применённом выпрямлении считается зазор (он меняется:
            gap = hypot по осям, и после выпрямления перпендикулярная
            составляющая исчезает), нужда и составы кустов ВДОЛЬ ребра;
          * выпрямление откатывается, и оба сдвига идут в try_shift списком.
        Куст берётся тот же, что у обычного хода (group_of / каскад), поэтому
        ни один инвариант не проверяется слабее, чем в базе.
        """
        s2, t2 = edge_ends(e)
        if s2 in frozen or t2 in frozen:
            return False
        a2, b2 = byid.get(s2), byid.get(t2)
        if a2 is None or b2 is None:
            return False
        ax2, ay2 = node_cxy(a2)
        bx2, by2 = node_cxy(b2)
        dx0, dy0 = bx2 - ax2, by2 - ay2
        if abs(dx0) >= abs(dy0):
            al_axis, skew, sp_axis, k_sp = "Y", dy0, "X", 0
        else:
            al_axis, skew, sp_axis, k_sp = "X", dx0, "Y", 1
        if not (0.01 < abs(skew) <= COMPOUND_MAX):
            return False
        cur2 = defects(graph, byid, floor)
        stats["comp_tried"] += 1
        al_plan = []
        for who_al, sgn_al in ((s2, 1.0), (t2, -1.0)):
            g0 = group_of(who_al, al_axis, byid, adj)
            if g0 is None:
                continue
            al_plan.append((set(g0), sgn_al, False))
            # E2-2b: РАСТАЛКИВАНИЕ ПОД ВЫПРЯМЛЕНИЕ. Замер (--debug-comp,
            # a6d28736): все отказы составного хода на edge_342/369 — «наложение
            # боксов», то есть мешает не приёмка по трубам, а сосед, стоящий
            # на пути поперечного сдвига. Ровно на этот случай в базе есть
            # grow_push: мешающий уезжает вместе со своим рядом.
            if comp_push:
                gp = grow_push(g0, al_axis, sgn_al * skew, byid, adj, graph)
                if gp is not None and set(gp) != set(g0):
                    al_plan.append((set(gp), sgn_al, True))
        for grp_al, sgn_al, al_pushed in al_plan:
            dxa, dya = _vec(al_axis, sgn_al * skew)
            for k in grp_al:
                move_node(byid[k], dxa, dya)
            try:
                g2, _p2, _q2 = gap_and_seam(shape_box(byid[s2]),
                                            shape_box(byid[t2]))
                d_sp = node_cxy(byid[s2])[k_sp] - node_cxy(byid[t2])[k_sp]
                sgn_s = 1.0 if d_sp >= 0 else -1.0
                cand = []
                for need in (target - g2, floor - g2 + 0.5):
                    if need <= 0:
                        continue
                    for who_sp, sgn_sp in ((s2, sgn_s), (t2, -sgn_s)):
                        grp_sp = group_of(who_sp, sp_axis, byid, adj)
                        how = "comp"
                        if grp_sp is None:
                            if not solo_ok(who_sp, sp_axis, byid, adj):
                                continue
                            grp_sp, how = {who_sp}, "comp_solo"
                        cand.append((set(grp_sp), sgn_sp * need, how, need,
                                     False))
                        casc = grow_cascade(who_sp, sp_axis, sgn_sp, need,
                                            byid, adj, floor)
                        if casc is not None and set(casc) != set(grp_sp):
                            cand.append((set(casc), sgn_sp * need,
                                         "comp_cascade", need, False))
                        if comp_push:
                            base_p = casc if casc is not None else grp_sp
                            gp2 = grow_push(base_p, sp_axis, sgn_sp * need,
                                            byid, adj, graph)
                            if gp2 is not None and set(gp2) != set(base_p):
                                cand.append((set(gp2), sgn_sp * need,
                                             "comp_push", need, True))
            finally:
                for k in grp_al:
                    move_node(byid[k], -dxa, -dya)
            for grp_sp, delta, how, need, sp_pushed in cand:
                mv = [(set(grp_al), dxa, dya),
                      (grp_sp, *_vec(sp_axis, delta))]
                # расталкивание везёт целые ряды — ему допуск на порчу глубины
                # не положен (то же правило, что у обычного push в базе)
                ptol = 0.0 if (al_pushed or sp_pushed) else PEN_TOL
                if try_shift(mv, cur2, eid, pen_tol=ptol,
                             dbg=(f"{eid} {how} выпр.{al_axis}{skew:+.1f}"
                                  f"{'+push' if al_pushed else ''} n={len(grp_al)}"
                                  f" разд.{sp_axis}{delta:+.1f} n={len(grp_sp)}"
                                  if DEBUG_COMP else None)):
                    stats["accepted"] += 1
                    stats["by_compound"] += 1
                    stats["moves"].append(
                        {"edge": eid, "how": how, "align_axis": al_axis,
                         "skew": round(skew, 2), "axis": sp_axis,
                         "px": round(need, 2), "n": len(grp_sp),
                         "push": bool(al_pushed or sp_pushed)})
                    return True
        return False

    def fix_skew(e, eid):
        """Лестница косых ходов: сначала выпрямление, потом составной ход."""
        if try_align(e, eid):
            return True
        return compound and try_compound(e, eid)

    # ───────────────── B_fill: перенос места полосой ─────────────────

    def band_targets(axis, gidx, gcoord, cur):
        """Разрезы, которые надо РАСШИРИТЬ. -> {разрез: (need, eid)}.

        Дефекты агрегируются по разрезу: одна гребёнка из пяти клапанов —
        это один разрез и один ход, а не пять отдельных попыток.
        """
        tg = {}
        for eid, gap in cur.items():
            e = e_by_id.get(eid)
            if e is None:
                continue
            if move_axis(e, byid, o_byid) != axis:
                continue
            need = floor - gap + 0.5
            if need <= 0:
                continue
            for j in seam_cuts(e, byid, axis, gidx, gcoord):
                old = tg.get(j)
                if old is None or need > old[0]:
                    tg[j] = (need, eid)
        return tg

    def try_band(axis, j, need, eid, groups, gcoord, slack, eslack, cur):
        """Полоса между разрезом j и компенсирующим j2. -> True, если принято.

        ЖЁСТКИЙ отбор — только по box-слаку: столкнуть формы нельзя ничем.
        edge-слак (сожмётся ли труба ниже порога) идёт в ПОРЯДОК проб, а не в
        фильтр: замер показал, что жёсткий edge-фильтр не оставлял ни одного
        кандидата (почти каждый разрез пересечён хотя бы одной короткой трубой),
        и Y-полоса не делала ни одного хода. Ход, который лечит пять труб и
        поджимает одну, приёмка и так примет — она считает НЕТТО.
        """
        m = len(groups)
        cand = sorted((j2 for j2 in range(m - 1)
                       if j2 != j and slack[j2] >= need
                       and abs(gcoord[j2] - gcoord[j]) <= BAND_REACH),
                      key=lambda j2: (0 if eslack[j2] >= need else 1,
                                      abs(gcoord[j2] - gcoord[j])))
        for j2 in cand[:BAND_TRIES]:
            if j2 > j:
                band = {k for gg in range(j + 1, j2 + 1) for k in groups[gg]}
                d = need
            else:
                band = {k for gg in range(j2 + 1, j + 1) for k in groups[gg]}
                d = -need
            stats["band_tried"] += 1
            if try_shift([(band, *_vec(axis, d))], cur, eid, extra_gate=True):
                slack[j] += need         # место переехало: разрез j стал шире,
                slack[j2] -= need        # j2 — уже, ровно на столько же
                eslack[j] += need
                eslack[j2] -= need
                stats["moves"].append(
                    {"edge": eid, "how": "band", "axis": axis,
                     "px": round(need, 2), "cut": j, "pay": j2, "n": len(band)})
                return True
        return False

    def band_pass(tag, rounds=BAND_ROUNDS):
        """Проход переноса места по обеим осям. -> число принятых ходов."""
        total = 0
        ids = [n["id"] for n in graph.get("nodes", []) if "centroid" in n]
        for axis in ("Y", "X"):
            for _r in range(rounds):
                groups, gidx, gcoord = axis_groups(byid, ids, axis)
                if len(groups) < 3:
                    break
                cur = defects(graph, byid, floor)
                tg = band_targets(axis, gidx, gcoord, cur)
                if not tg:
                    break
                slack = [float(v) for v in
                         box_slack(byid, ids, gidx, len(groups) - 1, axis)]
                eslack = edge_slack(graph, byid, gidx, len(groups) - 1, floor)
                acc = 0
                for j, (need, eid) in sorted(tg.items(),
                                             key=lambda kv: -kv[1][0]):
                    cur = defects(graph, byid, floor)
                    if eid not in cur:
                        continue
                    if try_band(axis, j, need, eid, groups, gcoord, slack,
                                eslack, cur):
                        acc += 1
                total += acc
                if verbose:
                    logger.info(
                        "полоса %s/%s раунд %d: принято %d, дефектов %d",
                        tag, axis, _r + 1, acc,
                        len(defects(graph, byid, floor)))
                if not acc:
                    break
        stats["band_" + tag] += total
        stats["accepted"] += total
        return total

    if band in ("pre", "both"):
        band_pass("pre")

    for p in range(passes):
        cur = defects(graph, byid, floor)
        if not cur:
            break
        todo = sorted(cur.items(), key=lambda kv: (kv[1], kv[0]))
        n_before = stats["accepted"]
        for eid, gap in todo:
            e = e_by_id.get(eid)
            if e is None:
                continue
            cur = defects(graph, byid, floor)
            if eid not in cur:
                continue                      # уже вылечено соседним ходом
            gap = cur[eid]
            axis, ax_src = move_axis(e, byid, o_byid, want_source=True)
            if axis is None and not (gap < 0.0 or (unlock_zero and gap <= 0.0)):
                if not fix_skew(e, eid):
                    stats["no_axis"] += 1
                continue
            s, t = edge_ends(e)
            if s in frozen or t in frozen:
                stats["frozen"] += 1
                continue

            def _sign(axis, ax_src, s=s, t=t, e=e):
                """знак: конец уезжает ОТ второго; при совпадении — из оригинала."""
                a, b = byid[s], byid[t]
                ax, ay = node_cxy(a)
                bx2, by2 = node_cxy(b)
                if ax_src == "pts":
                    pl = edge_polyline(e)
                    (px1, py1), (px2, py2) = pl[0], pl[-1]
                    d = (py1 - py2) if axis == "Y" else (px1 - px2)
                elif ax_src == "orig":
                    oa, ob2 = o_byid.get(s), o_byid.get(t)
                    if oa is None or ob2 is None:
                        return None
                    oax, oay = node_cxy(oa)
                    obx, oby = node_cxy(ob2)
                    d = (oay - oby) if axis == "Y" else (oax - obx)
                else:
                    d = (ay - by2) if axis == "Y" else (ax - bx2)
                if abs(d) <= AXIS_TOL:
                    oa, ob = o_byid.get(s), o_byid.get(t)
                    if oa is None or ob is None:
                        return None
                    oax, oay = node_cxy(oa)
                    obx, oby = node_cxy(ob)
                    d = (oay - oby) if axis == "Y" else (oax - obx)
                return 1.0 if d >= 0 else -1.0

            # ЛЕСТНИЦА ОСЕЙ. Сначала РАСЦЕПЛЕНИЕ (D1/D2) — только при gap < 0
            # и по оси минимального проникновения, на глубину по ЭТОЙ оси;
            # потом обычная ось ребра. Ни одна проба базы не теряется.
            plan = []            # (ось, знак для src, лестница нужд, block_e)
            # unlock_zero — ЗАМЕР В СТОРОНУ, по умолчанию выключен: считать ли
            # касание (gap == 0) сцеплением. Правило стратегии D — только
            # gap < 0.
            if gap < 0.0 or (unlock_zero and gap <= 0.0):
                for uax, dep, usgn in unlock_axes(shape_box(byid[s]),
                                                  shape_box(byid[t])):
                    plan.append((uax, usgn,
                                 [dep + target, dep + floor + 0.5], id(e)))
            if axis is not None:
                _sg = _sign(axis, ax_src)
                if _sg is not None:
                    plan.append((axis, _sg,
                                 [target - gap, floor - gap + 0.5], None))
            if not plan:
                if not fix_skew(e, eid):
                    stats["no_axis"] += 1
                continue

            # кто хвост: меньшая сторона
            n_s = side_size(e, "src", adj, TAIL_MAX)
            n_t = side_size(e, "tgt", adj, TAIL_MAX)
            # ЛЕСТНИЦА ПРОБ. Все варианты, а не «либо-либо»: замер по edge_904
            # показал, что при двух больших сторонах симметрия отклонялась
            # (её второй куст давал наложение), тогда как ОДНОСТОРОННИЙ ход
            # через node_604 проходил чисто — и не пробовался вовсе.
            if min(n_s, n_t) <= TAIL_MAX:
                first = s if n_s <= n_t else t
                second = t if first == s else s
                movers = [(first, 1.0), (second, 1.0), ("__sym__", 0.5)]
            else:
                movers = [("__sym__", 0.5), (s, 1.0), (t, 1.0)]

            done = False
            for axis, sgn_s, need_list, blk in plan:
                if done:
                    break
                unl = blk is not None      # это ход-расцепление (D)
                if unl:
                    stats["unlock_tried"] += 1
                for need in need_list:
                    if need <= 0 or done:
                        continue
                    for who, frac in movers:
                        if who == "__sym__":
                            gs = group_of(s, axis, byid, adj, block_e=blk)
                            gt = group_of(t, axis, byid, adj, block_e=blk)
                            if gs is None or gt is None or gs & gt:
                                continue
                            h = need * frac
                            mv = [(gs, *_vec(axis, sgn_s * h)),
                                  (gt, *_vec(axis, -sgn_s * h))]
                            if try_shift(mv, cur, eid):
                                stats["accepted"] += 1
                                stats["symmetric"] += 1
                                stats["by_unlock"] += 1 if unl else 0
                                stats["moves"].append(
                                    {"edge": eid, "how": "sym", "axis": axis,
                                     "px": round(need, 2),
                                     "gap0": round(gap, 2), "unlock": unl})
                                done = True
                                break
                            continue
                        sgn = sgn_s if who == s else -sgn_s
                        grp = group_of(who, axis, byid, adj, block_e=blk)
                        how = "group"
                        if grp is None:
                            if not solo_ok(who, axis, byid, adj):
                                continue
                            grp, how = {who}, "solo"
                        # ЛЕСТНИЦА МЕХАНИЗМОВ: сначала сам куст, потом КАСКАД —
                        # он дополнительно везёт впереди стоящих, которых сдвиг
                        # иначе сжал бы (замер edge_160: клапан опускался чисто,
                        # но схлопывал следующее ребро вниз -> нетто 0 -> отказ).
                        casc = grow_cascade(who, axis, sgn, need, byid, adj,
                                            floor, block_e=blk)
                        tries_grp = [(grp, how)]
                        if casc is not None and casc != grp:
                            tries_grp.append((casc, "cascade"))
                        base_for_push = casc if casc is not None else grp
                        push = grow_push(base_for_push, axis, sgn * need, byid,
                                         adj, graph, block_e=blk)
                        if push is not None and push != base_for_push:
                            tries_grp.append((push, "push"))
                        for gg, hh in tries_grp:
                            # РАСТАЛКИВАНИЕ везёт целые ряды — ему допуск на
                            # порчу не положен: замер показал ход, который снял
                            # один дефект и загнал соседний с 0.00 в -10.50
                            # (глубина уложилась в общий допуск 12px и прошла).
                            ptol = 0.0 if hh == "push" else PEN_TOL
                            if try_shift([(gg, *_vec(axis, sgn * need))], cur,
                                         eid, pen_tol=ptol):
                                stats["accepted"] += 1
                                stats["by_" + ("group" if hh == "group"
                                               else "solo" if hh == "solo"
                                               else "push" if hh == "push"
                                               else "cascade")] += 1
                                stats["by_unlock"] += 1 if unl else 0
                                stats["moves"].append(
                                    {"edge": eid, "how": hh, "axis": axis,
                                     "px": round(need, 2),
                                     "gap0": round(gap, 2), "n": len(gg),
                                     "unlock": unl})
                                done = True
                                break
                        if done:
                            break
            if not done:
                # ОБЫЧНЫЙ ХОД НЕ ПРОШЁЛ. Если ребро косое (у него нашлась ось
                # из pts/оригинала, но по центроидам оно диагональ), даём
                # последний шанс составному ходу: он приводит ребро к оси и
                # раздвигает разом.
                if not (compound and try_compound(e, eid)):
                    stats["rejected"] += 1
        if verbose:
            logger.info(
                "проход %d: принято %d, дефектов осталось %d",
                p + 1, stats["accepted"] - n_before,
                len(defects(graph, byid, floor)))
        if stats["accepted"] == n_before:
            # РАЗДВИГАНИЕ ВСТАЛО. На входе v16 полосе нечего переносить (слак
            # по Y = 0 на всех разрезах), а вот здесь место уже появилось:
            # обычные ходы разредили часть рядов. Пробуем перенести его в
            # оставшиеся гребёнки и, если удалось, продолжаем обычные проходы.
            if band in ("post", "both") and band_pass("post") > 0:
                continue
            break
    return stats


def _vec(axis, v):
    return (0.0, v) if axis == "Y" else (v, 0.0)


ORDER_TIE = 6.0      # px: разрыв, ниже которого узлы считаются стоящими
                     # ВРОВЕНЬ и порядок между ними не установлен. Порог 2px
                     # (допуск осей) для суждения о перестановке мал: замер
                     # 8d517a35 — пары «было 2.9px левее, стало 2.6px правее»
                     # запрещали ход, хотя глазом такое не читается как
                     # перестановка. 6px = порог видимости 0.75*min_gap.
ORDER_NEAR = 100.0   # px: до какого расстояния по ПЕРПЕНДИКУЛЯРНОЙ оси пары
                     # считаются визуально сравнимыми (шаг колонок ~300 px)


def order_broken_local(after, base, near=ORDER_NEAR, tie=ORDER_TIE):
    """Перестановки порядка ТОЛЬКО между визуально сравнимыми узлами.

    Глобальный topo_gate._order_broken сравнивает все пары подряд и на
    локальном сдвиге даёт ложняк: замер по edge_908 показал 7 «перестановок»
    с узлами, удалёнными на 698-1787 px по X, где исходная разница по Y была
    2.8-4.7 px (шум снапа осей). Глаз такую пару рядом не видит, mental map
    от неё не страдает. Пара учитывается, только если по ДРУГОЙ оси узлы
    ближе near.
    """
    import numpy as np
    sa = {n["id"]: node_cxy(n) for n in after.get("nodes", [])
          if "centroid" in n}
    sb = {n["id"]: node_cxy(n) for n in base.get("nodes", [])
          if "centroid" in n}
    ids = sorted(set(sa) & set(sb))
    if len(ids) < 2:
        return 0
    A = np.array([sb[i] for i in ids], dtype=float)
    B = np.array([sa[i] for i in ids], dtype=float)
    iu = np.triu_indices(len(ids), k=1)
    broke = np.zeros(len(iu[0]), dtype=bool)
    for ax in (0, 1):
        da = (A[:, ax][:, None] - A[:, ax][None, :])[iu]
        db = (B[:, ax][:, None] - B[:, ax][None, :])[iu]
        perp = np.abs((A[:, 1 - ax][:, None] - A[:, 1 - ax][None, :])[iu])
        est = (np.abs(da) >= tie) & (np.abs(db) >= tie) & (perp <= near)
        broke |= est & (np.sign(da) != np.sign(db))
    return int(broke.sum())

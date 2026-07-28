# -*- coding: utf-8 -*-
"""axial.py — осевая расстановка: переменные решателя это ряды и колонки.

Источник: стенд `_scratch/layout_align/axial_solve.py` (строки 102-762).
Перенесено без изменения логики — см. docs/planning/AUTO_LAYOUT_INTEGRATION.md, Э2.

Узел не имеет собственной позиции: он стоит там, где стоит его ось. Отсюда
сохраняются ряды и колонки исходной схемы, а масштаб листа подбирается
бисекцией (fit_scale) под зазор блок-блок и минимальную длину трубы.
"""
from __future__ import annotations

from . import _vpsc
from ._axes import MODE as AXIS_MODE, TOL as AXIS_TOL, build_axes
from ._graph import (edge_ends, edges, is_connector, node_cxy, node_wh,
                     nodes_by_id, set_node_pos)

GAP = 14.0        # px: требуемый зазор блок-блок (порог заказчика, W3)
GAP_MIN = 1.0     # px: минимальный зазор, если 14 не влезает в холст.
                  # Ноль нельзя: на точном касании строгий арбитр
                  # (verify_overlaps) считает боксы пересекающимися
INSET = 0.5       # px: не сажать бокс РОВНО на кромку холста — строгий
                  # арбитр (verify_overlaps) считает касание выходом
ROUNDS = 12       # круги «конфликты -> X -> Y»
RETRIES = 6       # попыток ужать зазор, если результат не влез в холст
WALL_W = 1e9      # вес стенки холста (практически закреплена)
ORDER_GAP = 0.0   # порядок осей: >= 0, совпадение координат разрешено
BIAS = 1.0        # перекос раздачи пар в пользу X (>1 — «раскладывать вширь»)
PIPE_MIN = 12.0   # px: минимальная длина трубы (порог заказчика, W2).
                  # 0 — требование выключено
_SAME = object()  # сентинел: pipe_base по умолчанию наследует gap base
                  # (None — валидное значение «без капа», поэтому не годится)


# ────────────────────────────── элементы ──────────────────────────────

def items_of(graph):
    """[{key, cx, cy, hw, hh, ox, oy, conn}] по узлам с centroid, по ключу.

    ox/oy — смещение ЦЕНТРА bbox от центроида: у узлов P&ID они не совпадают,
    и без поправки зазор считался бы не между боксами.
    Коннектор — точка (hw = hh = 0): у него нулевая площадь, он не участвует
    в наложениях блок-блок (единственные, которые считает приёмка W1).
    """
    out = []
    for n in graph.get("nodes", []):
        if "centroid" not in n:
            continue
        cx, cy = node_cxy(n)
        conn = is_connector(n)
        bb = n.get("bbox")
        if conn or not bb or len(bb) != 4 or bb[2] <= bb[0] or bb[3] <= bb[1]:
            out.append({"key": n["id"], "cx": cx, "cy": cy, "hw": 0.0,
                        "hh": 0.0, "ox": 0.0, "oy": 0.0, "conn": True})
            continue
        w, h = node_wh(n)
        out.append({"key": n["id"], "cx": cx, "cy": cy,
                    "hw": w / 2.0, "hh": h / 2.0,
                    "ox": (bb[0] + bb[2]) / 2.0 - cx,
                    "oy": (bb[1] + bb[3]) / 2.0 - cy, "conn": False})
    return sorted(out, key=lambda it: str(it["key"]))


# ────────────────────────── осевая модель ──────────────────────────

def _axis_lists(graph, tol, mode):
    """build_axes -> два списка [{coord, members}], отсортированных по coord."""
    ax = build_axes(graph, tol=tol, mode=mode)
    return ({"x": [{"coord": a["coord"], "members": list(a["members"])}
                   for a in ax["x"]],
             "y": [{"coord": a["coord"], "members": list(a["members"])}
                   for a in ax["y"]]})


def _index(axlist):
    idx = {}
    for i, a in enumerate(axlist):
        for m in a["members"]:
            idx[m] = i
    return idx


def split_cells(xaxes, xa, ya, keys):
    """Расщепить ЯЧЕЙКИ: >1 узла на одной паре (колонка, ряд).

    Осевая модель такую пару не разводит в принципе — у обоих узлов обе
    координаты общие. Всем, кроме первого по id, выдаётся СВОЯ колонка с той
    же координатой, вставленная сразу за исходной (порядок осей сохранён,
    ограничение порядка с gap=0 разрешает совпадение).
    -> (новый xaxes, новый xa, число расщеплённых узлов)
    """
    cells = {}
    for k in sorted(keys):
        cells.setdefault((xa[k], ya[k]), []).append(k)
    extra = {}
    for (xi, _yi), ks in sorted(cells.items()):
        if len(ks) > 1:
            extra.setdefault(xi, []).extend(sorted(ks)[1:])
    if not extra:
        return xaxes, xa, 0
    new = []
    n_split = 0
    for i, a in enumerate(xaxes):
        moved = set(extra.get(i, ()))
        new.append({"coord": a["coord"],
                    "members": [m for m in a["members"] if m not in moved]})
        for k in sorted(moved):
            new.append({"coord": a["coord"], "members": [k]})
            n_split += 1
    new = [a for a in new if a["members"]]
    return new, _index(new), n_split


# ─────────────────────── конфликты и ограничения ───────────────────────

def _clear0(a, b, base):
    """Зазор пары НА ВХОДЕ (по разделяющей оси): >0 — столько px между
    боксами, <=0 — пара уже наложена во входном графе."""
    ax, ay = base[a["key"]]
    bx, by = base[b["key"]]
    return max(abs((ax + a["ox"]) - (bx + b["ox"])) - a["hw"] - b["hw"],
               abs((ay + a["oy"]) - (by + b["oy"])) - a["hh"] - b["hh"])


def conflicts(items, gap, eps=1e-6, base=None, floor=0.0):
    """Пары боксов, у которых зазор по ОБЕИМ осям меньше требуемого.

    Требуемый зазор пары — НЕ ПРОСТО gap, а «не хуже, чем было»:
        req = min(gap, max(floor, зазор этой пары на входе)).
    Почему так. Приёмка считает НОВЫЕ тесные пары и НОВЫЕ короткие трубы, а
    не абсолютные: вход 51b339ab сам содержит 1077 пар с зазором < 14 px и
    597 труб короче 12 px. Требовать 14 px от пары, которая на входе стояла
    в 3 px, — значит платить холстом за дефект, которого приёмка не
    засчитывает; именно это раздувало требуемую высоту до 1409 px при холсте
    1080. Пара, наложенная на входе, получает floor (её всё равно надо
    развести — это ворота W1).
    Пары с коннектором пропускаются: коннектор — точка нулевой площади,
    наложением он не считается.
    -> [(ka, kb, need_x, need_y, ov_x, ov_y)], где need_* — требуемое
    расстояние между ЦЕНТРАМИ БОКСОВ по оси, ov_* — насколько его не хватает.
    eps: пара, разведённая РОВНО на требуемый зазор, выходит из солвера с
    нехваткой ~1e-13 (сумма квадратов); без допуска круги никогда не сходятся.
    """
    from shapely.geometry import box as shp_box
    from shapely.strtree import STRtree

    blocks = [it for it in items if not it["conn"]]
    half = gap / 2.0
    geoms = [shp_box(it["cx"] + it["ox"] - it["hw"] - half,
                     it["cy"] + it["oy"] - it["hh"] - half,
                     it["cx"] + it["ox"] + it["hw"] + half,
                     it["cy"] + it["oy"] + it["hh"] + half) for it in blocks]
    tree = STRtree(geoms)
    out = []
    for i, g in enumerate(geoms):
        for j in tree.query(g, predicate="intersects"):
            j = int(j)
            if j <= i:
                continue
            a, b = blocks[i], blocks[j]
            req = gap if base is None else min(
                gap, max(floor, _clear0(a, b, base)))
            need_x = a["hw"] + b["hw"] + req
            need_y = a["hh"] + b["hh"] + req
            dx = abs((a["cx"] + a["ox"]) - (b["cx"] + b["ox"]))
            dy = abs((a["cy"] + a["oy"]) - (b["cy"] + b["oy"]))
            ov_x, ov_y = need_x - dx, need_y - dy
            if ov_x <= eps or ov_y <= eps:
                continue          # уже разведены хотя бы по одной оси
            out.append((a["key"], b["key"], need_x, need_y, ov_x, ov_y))
    return sorted(out, key=lambda t: (str(t[0]), str(t[1])))


def assign_axis(pairs, xa, ya, sep, canvas=(1920.0, 1080.0), bias=BIAS):
    """Раздать парам ось сепарации ОДИН РАЗ и запомнить в sep.

    Правило: общая колонка — разводим по y, общий ряд — по x, иначе по той
    оси, где не хватает меньше В ДОЛЯХ ХОЛСТА (ov_x/W против ov_y/H).
    Нормировка на холст, а не сырое сравнение, — потому что холст 1920x1080
    не квадратный: сырое «где меньше» грузит вертикаль, у которой запаса
    вдвое меньше (замер на 51b339ab: требуемая высота 1456 px против 1346 px
    при нормировке, потолок 1079). Она же отвечает пожеланию заказчика
    «плотную вертикальную схему разложить горизонтально».
    Пара, уже получившая ось в предыдущем круге, не пересматривается —
    иначе круги колеблются.
    -> число новых назначений; пары «одна ячейка» невозможны (расщеплены).
    """
    n_new = 0
    for ka, kb, need_x, need_y, ov_x, ov_y in pairs:
        pk = (ka, kb)
        if pk in sep["x"] or pk in sep["y"]:
            continue
        same_x, same_y = xa[ka] == xa[kb], ya[ka] == ya[kb]
        if same_x and same_y:
            continue                      # не должно случаться после split_cells
        if same_x:
            axis = "y"
        elif same_y:
            axis = "x"
        else:
            axis = "x" if ov_x / (canvas[0] * bias) <= ov_y / canvas[1]                 else "y"
        sep[axis][pk] = (ka, kb, need_x if axis == "x" else need_y)
        n_new += 1
    return n_new


def pipe_specs(graph, xa, ya, by_key, pipe_min, base=None):
    """Требование «труба >= pipe_min px», выраженное ЧЕРЕЗ ОСИ.

    В строго осевой схеме ребро горизонтально ровно тогда, когда его концы
    делят Y-ось, и вертикально — когда делят X-ось. Тогда длина трубы =
    расстояние между осями минус полугабариты концов, и требование заказчика
    становится обычным ограничением сепарации по той же оси:
        X[j] - X[i] >= hw_a + hw_b + pipe_min.
    Диагональные рёбра (концы не делят ни одной оси) пропускаются: их длина
    осями не выражается, ими займётся следующий этап.
    Требование тоже «не хуже, чем было»: min(pipe_min, длина на входе). Вход
    полон труб короче 12 px (597 на 51b339ab), и приёмка засчитывает их как
    унаследованные; растягивать их до 12 — платить холстом за чужой дефект.
    -> ({'x': [...], 'y': [...]}) в формате спеков сепарации.
    """
    out = {"x": [], "y": []}
    if pipe_min <= 0:
        return out
    for e in edges(graph):
        s, t = edge_ends(e)
        a, b = by_key.get(s), by_key.get(t)
        if a is None or b is None:
            continue
        if ya.get(s) == ya.get(t) and xa.get(s) != xa.get(t):
            want = pipe_min if base is None else min(pipe_min, max(
                0.0, abs((base[s][0] + a["ox"]) - (base[t][0] + b["ox"]))
                - a["hw"] - b["hw"]))
            out["x"].append((s, t, a["hw"] + b["hw"] + want))
        elif xa.get(s) == xa.get(t) and ya.get(s) != ya.get(t):
            want = pipe_min if base is None else min(pipe_min, max(
                0.0, abs((base[s][1] + a["oy"]) - (base[t][1] + b["oy"]))
                - a["hh"] - b["hh"]))
            out["y"].append((s, t, a["hh"] + b["hh"] + want))
    return {k: sorted(v, key=lambda z: (str(z[0]), str(z[1])))
            for k, v in out.items()}


def _axis_margins(axlist, by_key, off_key, half_key):
    """[(влево, вправо)] — на сколько бокс самого крупного члена выходит
    за координату оси. Именно эти числа и есть «габариты в ограничениях»."""
    out = []
    for a in axlist:
        left = right = 0.0
        for m in a["members"]:
            it = by_key.get(m)
            if it is None:
                continue
            o, h = it[off_key], it[half_key]
            left = max(left, h - o)
            right = max(right, h + o)
        out.append((left, right))
    return out


def sep_gaps(specs, by_key, idx, off_key):
    """Спеки сепарации -> [(i, j, gap)] в координатах ПЕРЕМЕННЫХ (i < j).

    need задан между ЦЕНТРАМИ БОКСОВ, а переменная — координата оси, то есть
    центроид; отсюда поправка на ox/oy. Между одной парой осей остаётся
    максимум — это и есть «полугабариты пересекающихся узлов + зазор».
    """
    out = {}
    for ka, kb, need in specs:
        a, b = by_key[ka], by_key[kb]
        ia, ib = idx[ka], idx[kb]
        if ia == ib:
            continue
        if ia > ib:
            ia, ib, a, b = ib, ia, b, a
        g = need + a[off_key] - b[off_key]
        if g > out.get((ia, ib), float("-inf")):
            out[(ia, ib)] = g
    return sorted((i, j, g) for (i, j), g in out.items())


def min_extent(n, floor, seps, marg):
    """Ширина ЛЕВОЙ ПРЕДЕЛЬНОЙ раскладки — минимально возможная при этих
    ограничениях. Все дуги идут по возрастанию индекса (граф — DAG, индексы
    уже топологически упорядочены), поэтому это один линейный проход.
    """
    pos = [0.0] * n
    adj = {}
    for i, g in enumerate(floor):
        adj.setdefault(i, []).append((i + 1, g))
    for i, j, g in seps:
        adj.setdefault(i, []).append((j, g))
    for i in range(n):
        for j, g in adj.get(i, ()):
            if pos[j] < pos[i] + g:
                pos[j] = pos[i] + g
    lo = min(pos[i] - marg[i][0] for i in range(n))
    hi = max(pos[i] + marg[i][1] for i in range(n))
    return hi - lo


def fit_keep(coords, seps, marg, usable, keep_max=1.0, iters=30):
    """Наибольшая доля keep <= keep_max, при которой раскладка с полом
    «соседние оси не сближаются сильнее чем в keep раз» ещё влезает в холст.

    Зачем пол вообще. Без него VPSC отыгрывает место под сепарацию на тех
    промежутках между осями, где ограничений НЕТ: на 51b339ab при fill=1
    сжималось 67 промежутков из 113, суммарно 461 px. Ряд с трубой при этом
    подъезжает к соседнему ряду с арматурой — и труба начинает идти СКВОЗЬ
    блок. Это прямой источник провала ворот W4 (новых проходов было 274).
    """
    n = len(coords)
    if n < 2:
        return keep_max

    def ext(k):
        floor = [max(0.0, k * (coords[i + 1] - coords[i]))
                 for i in range(n - 1)]
        return min_extent(n, floor, seps, marg)

    if ext(keep_max) <= usable:
        return keep_max
    lo, hi = 0.0, keep_max
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if ext(mid) <= usable:
            lo = mid
        else:
            hi = mid
    return lo


def _demand(items, xaxes, yaxes, xa, ya, by_key, canvas, gap, inset,
            pipes=None, base=None, floor=0.0, bias=BIAS):
    """Минимально необходимая ширина/высота при данном зазоре: (need_x, need_y).

    Считается ЛЕВОЙ ПРЕДЕЛЬНОЙ раскладкой (min_extent) по свежему раздаванию
    осей — то есть это нижняя граница, ниже неё не уложит никакой солвер.
    """
    sep = {"x": {}, "y": {}}
    pipes = pipes or {"x": [], "y": []}
    assign_axis(conflicts(items, gap, base=base, floor=floor), xa, ya, sep,
                canvas, bias)
    out = []
    for axis, axlist, idx, okey, hkey in (("x", xaxes, xa, "ox", "hw"),
                                          ("y", yaxes, ya, "oy", "hh")):
        marg = _axis_margins(axlist, by_key, okey, hkey)
        seps = sep_gaps(list(sep[axis].values()) + pipes[axis], by_key, idx,
                        okey)
        out.append(min_extent(len(axlist), [0.0] * (len(axlist) - 1), seps,
                              marg))
    return out[0], out[1]


def thresholds(scale, gap, gap_min, pipe_min):
    """Пороги заказчика, ужатые общим коэффициентом: (зазор, длина трубы).

    scale=1 — ровно требования заказчика (зазор 14 px, труба 12 px);
    scale=0 — только гарантия «нет наложений» (зазор gap_min, труба не
    требуется). Один коэффициент на оба порога, а не два независимых, —
    чтобы «сколько требований этот холст выдерживает» было ОДНИМ числом
    в отчёте, а не парой, которую нечем сравнить.
    """
    return gap_min + scale * (gap - gap_min), scale * pipe_min


def fit_scale(graph, items, xaxes, yaxes, xa, ya, by_key, canvas, gap,
              gap_min, pipe_min, base, inset=INSET, iters=14, bias=BIAS,
              pipe_base=_SAME):
    """Наибольшая доля порогов, при которой осевая раскладка ВЛЕЗАЕТ В ХОЛСТ.

    Зачем. Зазор 14 px и труба 12 px — пороги заказчика, но выполнимы они не
    на всяком графе: в осевой модели пара разводится вместе со ВСЕМИ рядами,
    и на 51b339ab при полных порогах требуется 1409 px по вертикали при
    холсте 1080. Выбор «пороги или холст» решается в пользу холста: узел за
    кромкой FXML клампит МОЛЧА и фигура разъезжается с трубами, а тесная пара
    — это дефект списка, который доваллидирует оператор. Достигнутая доля
    отдаётся в статистике (scale_eff/gap_eff/pipe_eff): занижать её молча
    нельзя, это и есть цена осевой модели на плотном графе.

    base — кап зазора блок-блок; pipe_base — кап длины трубы. По умолчанию
    (pipe_base=_SAME) труба капается тем же base, что и зазор (v16/v22). v23c
    задаёт pipe_base=None (полный pipe_min по границам) при base=base_pos.
    """
    if pipe_base is _SAME:
        pipe_base = base
    usable = (canvas[0] - 2 * inset, canvas[1] - 2 * inset)

    def demand(sc):
        g, pm = thresholds(sc, gap, gap_min, pipe_min)
        return _demand(items, xaxes, yaxes, xa, ya, by_key, canvas, g, inset,
                       pipe_specs(graph, xa, ya, by_key, pm, pipe_base),
                       base, gap_min, bias)

    def fits(d):
        return d[0] <= usable[0] and d[1] <= usable[1]

    d1 = demand(1.0)
    if fits(d1):
        return 1.0, d1
    lo, hi = 0.0, 1.0
    best = (0.0, demand(0.0))
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        d = demand(mid)
        if fits(d):
            best = (mid, d)
            lo = mid
        else:
            hi = mid
    return best


def solve_axis(axlist, by_key, idx, specs, desired, limit, off_key, half_key,
               base_coords, inset=INSET, keep=1.0):
    """VPSC по одной оси. -> (координаты осей, ok, stats солвера, keep_eff).

    Ограничения (все ярус 0, все направлены по возрастанию индекса оси,
    поэтому система — DAG и всегда совместна):
      * порядок осей и ПОЛ ПРОМЕЖУТКА: C(v_i, v_{i+1}, gap = keep*d_i), где
        d_i — расстояние между осями НА ВХОДЕ (base_coords, не текущее: иначе
        пол уезжал бы вслед за результатом круга). keep=0 — только порядок;
        keep=1 — соседние оси не сближаются вовсе. Фактический keep
        подбирается под холст (fit_keep);
      * сепарация пары узлов: C(v_i, v_j, gap = hw_a+hw_b+gap + ox_a - ox_b);
      * холст: две стенки веса 1e9 на 0 и limit, каждая ось обязана влезть
        между ними своим самым крупным членом + inset.
    """
    n = len(axlist)
    coords = list(base_coords)
    marg = _axis_margins(axlist, by_key, off_key, half_key)
    seps = sep_gaps(specs, by_key, idx, off_key)
    keep_eff = fit_keep(coords, seps, marg, limit - 2 * inset, keep)
    var = [_vpsc.Variable(("ax", i), desired[i]) for i in range(n)]
    lo = _vpsc.Variable("__lo", 0.0, WALL_W)
    hi = _vpsc.Variable("__hi", float(limit), WALL_W)
    cons = []
    for i in range(n - 1):
        g = max(ORDER_GAP, keep_eff * (coords[i + 1] - coords[i]))
        cons.append(_vpsc.Constraint(var[i], var[i + 1], g, tag="order"))
    for i, j, g in seps:
        cons.append(_vpsc.Constraint(var[i], var[j], g, tag="sep"))
    for i, (ml, mr) in enumerate(marg):
        cons.append(_vpsc.Constraint(lo, var[i], ml + inset, tag="canvas"))
        cons.append(_vpsc.Constraint(var[i], hi, mr + inset, tag="canvas"))
    solver = _vpsc.VPSC(var + [lo, hi], cons)
    # solve(), а не satisfy(). satisfy_VPSC отдаёт ЛЮБУЮ допустимую точку, и
    # при желаемых координатах, уже растянутых на весь холст (fill=1), эта
    # точка оказывалась шире холста: стенки веса 1e9 просто отжимались
    # (замерено на 51b339ab — на 355 px, 145 узлов за кромкой). Оптимум же
    # обязан прижать стенки к 0 и limit, пока допустимая точка внутри холста
    # вообще существует, — за это отвечает их вес.
    ok = solver.solve()
    return [v.position for v in var], ok, solver.stats, keep_eff


def _desired(axlist, by_key, off_key, half_key, limit, fill, inset=INSET):
    """Желаемые координаты осей: вход, аффинно растянутый под холст.

    fill=0 — вход как есть; fill=1 — растяжка до кромок (заполнение листа).
    """
    marg = _axis_margins(axlist, by_key, off_key, half_key)
    coords = [a["coord"] for a in axlist]
    if not coords:
        return []
    need_lo = min(c - m[0] for c, m in zip(coords, marg))
    need_hi = max(c + m[1] for c, m in zip(coords, marg))
    span = need_hi - need_lo
    if span <= 1e-9 or fill <= 0.0:
        return list(coords)
    s_full = (limit - 2 * inset) / span
    s = 1.0 + fill * (s_full - 1.0)
    c_old = (need_lo + need_hi) / 2.0
    c_new = c_old + fill * (limit / 2.0 - c_old)
    return [c_new + (c - c_old) * s for c in coords]


# ──────────────────────────── главный вход ────────────────────────────

def place(graph, canvas=(1920.0, 1080.0), gap=GAP, gap_min=GAP_MIN,
          tol=AXIS_TOL, mode=AXIS_MODE, rounds=ROUNDS, fill=1.0, keep=1.0,
          pipe_min=PIPE_MIN, bias=BIAS, inset=INSET, retries=RETRIES,
          cap_input=True, cap_gap=None, cap_pipe=None):
    """Осевая расстановка: мутирует graph, возвращает статистику.

    cap_input=True (v16): пороги «не хуже чем было» — зазор/труба капаются
    ВХОДНОЙ длиной (по центроидам), крам наследуется. cap_input=False
    (ДЕКОМПРЕССИЯ v22): base=None -> разнесение по РЕАЛЬНЫМ ГРАНИЦАМ (полный
    gap/pipe_min), keep=0 даёт равномерную упаковку, трубы становятся видимыми.

    cap_gap/cap_pipe (v23c, ХИРУРГ. UNCAP): раздельный кап зазора блок-блок и
    длины трубы. None -> наследует cap_input (v16/v22 без изменений). Задать
    cap_gap=True, cap_pipe=False значит: зазор блок-блок остаётся капнутым «не
    хуже чем было» (спрос не взрывается, как при полном uncap v22, где сняли
    ОБА кап), а труба получает полный pipe_min по РЕАЛЬНЫМ границам концов.
    """
    items = items_of(graph)
    by_key = {it["key"]: it for it in items}
    keys = [it["key"] for it in items]
    axl = _axis_lists(graph, tol, mode)
    xaxes, yaxes = axl["x"], axl["y"]
    xa, ya = _index(xaxes), _index(yaxes)
    missing = [k for k in keys if k not in xa or k not in ya]
    if missing:
        raise RuntimeError("axial_solve: узлы вне осевой модели: %r"
                           % missing[:5])
    xaxes, xa, n_split = split_cells(xaxes, xa, ya, keys)

    # СНАП: узел садится ровно на координаты своих осей
    snap_max = 0.0
    for it in items:
        nx, ny = xaxes[xa[it["key"]]]["coord"], yaxes[ya[it["key"]]]["coord"]
        snap_max = max(snap_max, abs(nx - it["cx"]), abs(ny - it["cy"]))
        it["cx"], it["cy"] = nx, ny

    base_pos = {it["key"]: (it["cx"], it["cy"]) for it in items}
    # ДЕКОМПРЕССИЯ: cap_input=False -> base=None -> разнесение по ГРАНИЦАМ
    # (полный gap/pipe_min, без «не хуже чем было»), а не по центроидам входа.
    # РАЗДЕЛЬНЫЙ КАП (v23c): зазор и труба капаются входом независимо;
    # cap_gap/cap_pipe=None наследуют cap_input.
    _cg = cap_input if cap_gap is None else cap_gap
    _cp = cap_input if cap_pipe is None else cap_pipe
    gap_base = base_pos if _cg else None
    pipe_base = base_pos if _cp else None
    cx0 = [a["coord"] for a in xaxes]
    cy0 = [a["coord"] for a in yaxes]
    dx = _desired(xaxes, by_key, "ox", "hw", canvas[0], fill, inset)
    dy = _desired(yaxes, by_key, "oy", "hh", canvas[1], fill, inset)
    scale0, demand = fit_scale(graph, items, xaxes, yaxes, xa, ya, by_key,
                               canvas, gap, gap_min, pipe_min, gap_base,
                               inset, bias=bias, pipe_base=pipe_base)

    st = {"axes_x": len(xaxes), "axes_y": len(yaxes), "cells_split": n_split,
          "snap_max_px": round(snap_max, 2), "gap_want": gap,
          "pipe_want": pipe_min,
          "demand_x": round(demand[0], 1), "demand_y": round(demand[1], 1)}

    def attempt(scale):
        """Полный проход при доле порогов scale. -> (info, коорд. x, коорд. y)."""
        g, pm = thresholds(scale, gap, gap_min, pipe_min)
        pipes = pipe_specs(graph, xa, ya, by_key, pm, pipe_base)
        for a, c in zip(xaxes, cx0):
            a["coord"] = c
        for a, c in zip(yaxes, cy0):
            a["coord"] = c
        for it in items:
            it["cx"] = xaxes[xa[it["key"]]]["coord"]
            it["cy"] = yaxes[ya[it["key"]]]["coord"]
        sep = {"x": {}, "y": {}}
        inf = {"scale_eff": round(scale, 3), "gap_eff": round(g, 2),
               "pipe_eff": round(pm, 2), "pipes_x": len(pipes["x"]),
               "pipes_y": len(pipes["y"]), "rounds": 0, "vpsc_iters": 0,
               "vpsc_splits": 0, "solver_capped": 0, "keep_x": None,
               "keep_y": None}
        residual = None
        for _r in range(rounds):
            inf["rounds"] += 1
            # ПЕРВЫЙ круг требует полный зазор, последующие — только gap_min.
            # Пары второго круга — это пары, которых во ВХОДЕ не было: их
            # свела наша же раскладка. Требовать от них 14 px значит
            # раскручивать спираль (замерено на 51b339ab: круг 2 поднимал
            # требуемую высоту с 1079 до 1156 px при холсте 1080, и внешняя
            # бисекция была вынуждена ронять пороги до 7 % от заказанных).
            # Для них цель одна — ворота W1, то есть отсутствие наложения.
            assign_axis(conflicts(items, g if _r == 0 else gap_min,
                                  base=gap_base, floor=gap_min),
                        xa, ya, sep, canvas, bias)
            for axis, axlist, idx, des, lim, okey, hkey, ckey, c0 in (
                    ("x", xaxes, xa, dx, canvas[0], "ox", "hw", "cx", cx0),
                    ("y", yaxes, ya, dy, canvas[1], "oy", "hh", "cy", cy0)):
                pos, good, sst, keff = solve_axis(
                    axlist, by_key, idx,
                    list(sep[axis].values()) + pipes[axis], des, lim,
                    okey, hkey, c0, inset, keep)
                for i, a in enumerate(axlist):
                    a["coord"] = pos[i]
                for it in items:
                    it[ckey] = pos[idx[it["key"]]]
                inf["vpsc_iters"] += sst.get("iters", 0)
                inf["vpsc_splits"] += sst.get("splits", 0)
                inf["keep_" + axis] = round(keff, 4)
                if not good:
                    inf["solver_capped"] += 1
            # круги сходятся по ЖЁСТКОМУ инварианту (нет пары ближе gap_min),
            # а не по полному зазору: полный зазор для пар, порождённых самой
            # раскладкой, сознательно не требуется (см. выше)
            residual = len(conflicts(items, gap_min, base=base_pos,
                                     floor=gap_min))
            if residual == 0:
                break
        # ХОЛСТ, доводка. satisfy_VPSC ищет ДОПУСТИМУЮ точку, а не оптимум, и
        # при малом запасе может отжать стенку. Лечится ЧИСТЫМ СДВИГОМ всех
        # осей: он не меняет ни одного взаимного расстояния, значит не может
        # испортить ни W1/W3, ни W6/W7.
        over = 0.0
        for axlist, idx, okey, hkey, ckey, lim in (
                (xaxes, xa, "ox", "hw", "cx", canvas[0]),
                (yaxes, ya, "oy", "hh", "cy", canvas[1])):
            marg = _axis_margins(axlist, by_key, okey, hkey)
            pos = [a["coord"] for a in axlist]
            p_lo = min(p - m[0] for p, m in zip(pos, marg))
            p_hi = max(p + m[1] for p, m in zip(pos, marg))
            shift = 0.0
            if p_lo < inset:
                shift = inset - p_lo
            if p_hi + shift > lim - inset:
                shift = min(shift, lim - inset - p_hi)
            if abs(shift) > 1e-9:
                for a in axlist:
                    a["coord"] += shift
                for it in items:
                    it[ckey] = axlist[idx[it["key"]]]["coord"]
            o = max(0.0, (p_hi - p_lo) - (lim - 2 * inset))
            inf["over_" + ckey[1]] = round(o, 2)
            over = max(over, o)
        inf["over"] = round(over, 2)
        inf["sep_x"], inf["sep_y"] = len(sep["x"]), len(sep["y"])
        inf["residual_tight_pairs"] = residual
        inf["overlaps_left"] = len(conflicts(items, 0.0))
        return inf, [a["coord"] for a in xaxes], [a["coord"] for a in yaxes]

    # ПОРОГИ ДОСУЖИВАЮТСЯ ПО ФАКТУ, а не по нижней оценке. fit_scale считает
    # demand по ОДНОМУ раздаванию осей на входных координатах; круги находят
    # конфликты, которых на входе не было, и требование растёт (на 51b339ab
    # — на 16 px, на a6d28736 — на 25 px). Бисекция ДВУСТОРОННЯЯ: ранний
    # выход на первой влезшей доле занижал зазор (3.5 px вместо 6.2 на
    # 51b339ab — это +19 тесных пар), поэтому бюджет попыток тратится весь.
    tried = []
    inf, px, py = attempt(scale0)
    tried.append((round(scale0, 3), inf["over_x"], inf["over_y"]))
    tries = 1
    best = (inf, px, py) if inf["over"] <= 0.0 else None
    if best is None:
        lo, hi = 0.0, scale0
        while tries <= retries and hi - lo > 0.01:
            mid = (lo + hi) / 2.0
            inf, px, py = attempt(mid)
            tried.append((round(mid, 3), inf["over_x"], inf["over_y"]))
            tries += 1
            if inf["over"] <= 0.0:
                best = (inf, px, py)
                lo = mid
            else:
                hi = mid
    if best is None:
        inf, px, py = attempt(0.0)
        tries += 1
        best = (inf, px, py)
    inf, px, py = best
    for a, c in zip(xaxes, px):
        a["coord"] = c
    for a, c in zip(yaxes, py):
        a["coord"] = c
    for it in items:
        it["cx"] = xaxes[xa[it["key"]]]["coord"]
        it["cy"] = yaxes[ya[it["key"]]]["coord"]
    st.update(inf)
    st["attempts"] = tries
    st["tried"] = tried

    byid = nodes_by_id(graph)
    disp = 0.0
    n_moved = 0
    for it in items:
        n = byid.get(it["key"])
        if n is None:
            continue
        ocx, ocy = node_cxy(n)
        set_node_pos(n, it["cx"], it["cy"])
        d = abs(it["cx"] - ocx) + abs(it["cy"] - ocy)
        disp += d
        if d > 0.5:
            n_moved += 1
    st["nodes"] = len(items)
    st["moved"] = n_moved
    st["mean_disp_px"] = round(disp / len(items), 2) if items else 0.0
    return st

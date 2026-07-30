# -*- coding: utf-8 -*-
"""_vpsc.py — блочный VPSC-решатель (перенос стенда без изменений).

Источник: стенд `_scratch/layout_align/vpsc.py` (строки 62-532).
Перенесено без изменения логики — см. docs/planning/AUTO_LAYOUT_INTEGRATION.md, Э2.
"""

import heapq
from itertools import count

EPS = 1e-7          # px: слэк в пределах — считаем ограничение выполненным
PRIO_BONUS = 1e6    # смещение множителя за ярус приоритета (см. докстринг)
MAX_SPLITS = 4      # сколько раз одно ограничение может быть расщеплено


class Variable:
    """Переменная одной оси: желаемая координата d, вес w."""

    __slots__ = ("key", "desired", "weight", "offset", "block", "out", "inc")

    def __init__(self, key, desired, weight=1.0):
        self.key = key
        self.desired = float(desired)
        self.weight = float(weight)
        self.offset = 0.0
        self.block = None
        self.out = []     # ограничения, где переменная — left
        self.inc = []     # ограничения, где переменная — right

    @property
    def position(self):
        return self.block.posn + self.offset

    def __repr__(self):
        return "Var(%s, d=%.2f, x=%.2f)" % (self.key, self.desired,
                                            self.position)


class Constraint:
    """x_right - x_left >= gap. priority: 0 — самый важный ярус."""

    __slots__ = ("left", "right", "gap", "priority", "active",
                 "unsatisfiable", "lm", "splits", "tag")

    def __init__(self, left, right, gap, priority=0, tag=""):
        self.left = left
        self.right = right
        self.gap = float(gap)
        self.priority = int(priority)
        self.active = False
        self.unsatisfiable = False
        self.lm = 0.0
        self.splits = 0
        self.tag = tag

    @property
    def slack(self):
        return (self.right.block.posn + self.right.offset
                - self.left.block.posn - self.left.offset - self.gap)

    def __repr__(self):
        return "C(%s -> %s, gap=%.2f, slack=%.2f, %s)" % (
            self.left.key, self.right.key, self.gap, self.slack, self.tag)


class Block:
    """Множество переменных с фиксированными смещениями друг относительно друга.

    posn — позиция блока, вокруг которой живут смещения; оптимум блока при
    фиксированных смещениях = wposn/weight (производная суммы квадратов).
    ts — таймстемп для фикса 2006 (см. докстринг модуля).
    """

    __slots__ = ("vars", "posn", "wposn", "weight", "ts")

    def __init__(self, v, ts):
        self.vars = [v]
        self.weight = v.weight
        self.wposn = v.weight * v.desired
        self.posn = v.desired
        self.ts = ts
        v.block = self
        v.offset = 0.0


def _eff_lm(c):
    return c.lm - PRIO_BONUS * c.priority


class VPSC:
    """Солвер одной оси. Мутирует переданные Variable (position/offset/block)."""

    def __init__(self, variables, constraints):
        self.vars = list(variables)
        self.cons = list(constraints)
        self._ts = 0
        self._seq = count()
        self.stats = {"merges": 0, "splits": 0, "unsat": 0, "stale": 0,
                      "sweeps": 0, "iters": 0, "capped": 0}
        for v in self.vars:
            v.out = []
            v.inc = []
            self._ts += 1
            Block(v, self._ts)
        for c in self.cons:
            c.active = False
            c.unsatisfiable = False
            c.splits = 0
            c.left.out.append(c)
            c.right.inc.append(c)

    # ------------------------------------------------------------- служебное
    def _stamp(self):
        self._ts += 1
        return self._ts

    @staticmethod
    def _key(c):
        return max(c.left.block.ts, c.right.block.ts)

    def _push(self, heap, c):
        heapq.heappush(heap, (c.slack, next(self._seq), c, self._key(c)))

    # --------------------------------------------------------------- слияние
    def _merge(self, c):
        """Сделать c активным, слив блоки его концов. Меньший вливается в больший."""
        l, r = c.left.block, c.right.block
        # тугое ограничение: posn(r) = posn(l) + dist
        dist = c.gap + c.left.offset - c.right.offset
        if len(l.vars) >= len(r.vars):
            keep, drop, sign = l, r, 1.0
        else:
            keep, drop, sign = r, l, -1.0
        d = dist * sign
        for v in drop.vars:
            v.offset += d
            v.block = keep
        keep.wposn += drop.wposn - d * drop.weight
        keep.weight += drop.weight
        keep.vars.extend(drop.vars)
        keep.posn = keep.wposn / keep.weight
        keep.ts = self._stamp()
        c.active = True
        self.stats["merges"] += 1

    # ------------------------------------------------- лагранжианы и разрезы
    @staticmethod
    def _tree(root):
        """Обход активного дерева блока от root. -> (порядок, дети, родитель)."""
        order = []
        children = {}
        parent = {}
        seen = {id(root)}
        stack = [(root, None)]
        while stack:
            v, pc = stack.pop()
            order.append(v)
            ch = []
            for c in v.out:
                if c.active and c is not pc and id(c.right) not in seen:
                    seen.add(id(c.right))
                    ch.append((c, c.right, True))
                    parent[id(c.right)] = (c, v)
                    stack.append((c.right, c))
            for c in v.inc:
                if c.active and c is not pc and id(c.left) not in seen:
                    seen.add(id(c.left))
                    ch.append((c, c.left, False))
                    parent[id(c.left)] = (c, v)
                    stack.append((c.left, c))
            children[id(v)] = ch
        return order, children, parent

    def _compute_lm(self, root):
        """Множители Лагранжа активных ограничений блока (корень — root).

        df/dv узла = 2 w (x - d) плюс вклад поддеревьев; множитель ребра
        дерева = производная поддерева, висящего на нём. Итеративно, без
        рекурсии: блок на плотном листе доходит до сотен переменных.
        """
        order, children, parent = self._tree(root)
        dfdv = {}
        for v in reversed(order):
            d = 2.0 * v.weight * (v.block.posn + v.offset - v.desired)
            for c, w, is_out in children[id(v)]:
                if is_out:
                    c.lm = dfdv[id(w)]
                    d += c.lm
                else:
                    c.lm = -dfdv[id(w)]
                    d -= c.lm
            dfdv[id(v)] = d
        return parent

    @staticmethod
    def _active_path(a, b):
        """Есть ли путь по АКТИВНЫМ ограничениям строго вперёд a -> ... -> b."""
        seen = {id(a)}
        stack = [a]
        while stack:
            v = stack.pop()
            if v is b:
                return True
            for c in v.out:
                if c.active and id(c.right) not in seen:
                    seen.add(id(c.right))
                    stack.append(c.right)
        return False

    def _split_between(self, vl, vr, prio=None, strict=False):
        """Разрезать блок на пути vl..vr по минимальному эффективному множителю.

        prio — приоритет ограничения, ради которого режем: рвать можно только
        то, что НЕ ВАЖНЕЕ него (priority >= prio), иначе младший ярус ломал бы
        старший. strict=True (случай активного цикла) требует строго младшего:
        если на пути одни ровесники, цикл действительно неразрешим.
        """
        parent = self._compute_lm(vl)
        path = []
        cur = vr
        while id(cur) in parent:
            c, up = parent[id(cur)]
            path.append(c)
            cur = up
        if not path:
            return None
        cand = [c for c in path if c.splits < MAX_SPLITS
                and (prio is None
                     or (c.priority > prio if strict else c.priority >= prio))]
        if not cand:
            return None
        sc = min(cand, key=_eff_lm)
        sc.active = False
        sc.splits += 1
        self._rebuild(sc.left)
        self._rebuild(sc.right)
        self.stats["splits"] += 1
        return sc

    def _rebuild(self, seed):
        """Пересобрать блок компоненты активного дерева, содержащей seed."""
        comp = []
        seen = {id(seed)}
        stack = [seed]
        while stack:
            v = stack.pop()
            comp.append(v)
            for c in v.out:
                if c.active and id(c.right) not in seen:
                    seen.add(id(c.right))
                    stack.append(c.right)
            for c in v.inc:
                if c.active and id(c.left) not in seen:
                    seen.add(id(c.left))
                    stack.append(c.left)
        # кадр нормируется по seed, поэтому его смещение читается ДО сборки
        # блока (конструктор Block обнуляет offset — на этом уже погорело)
        base = seed.offset
        b = Block.__new__(Block)
        b.vars = comp
        b.ts = self._stamp()
        b.weight = 0.0
        b.wposn = 0.0
        for v in comp:
            v.offset -= base
            v.block = b
            b.weight += v.weight
            b.wposn += v.weight * (v.desired - v.offset)
        b.posn = b.wposn / b.weight

    # ----------------------------------------------------------------- цикл
    def _sweep(self, heap, pool):
        """Точный линейный проход: вернуть в кучу все реально нарушенные."""
        self.stats["sweeps"] += 1
        found = 0
        for c in pool:
            if c.active or c.unsatisfiable:
                continue
            if c.slack < -EPS:
                self._push(heap, c)
                found += 1
        return found

    def _run(self, pool, max_iter):
        heap = []
        for c in pool:
            if not c.active and not c.unsatisfiable:
                self._push(heap, c)
        it = 0
        while True:
            if not heap:
                if self._sweep(heap, pool) == 0:
                    return True
            slack, _s, c, stamp = heapq.heappop(heap)
            if c.active or c.unsatisfiable:
                continue
            cur = self._key(c)
            if cur > stamp:
                # ФИКС 2006: запись протухла после слияния — пересчитать
                self.stats["stale"] += 1
                self._push(heap, c)
                continue
            if slack >= -EPS:
                # куча отсортирована по слэку, но точность гарантирует только
                # полный проход (устаревшая запись могла лежать глубже свежей)
                if self._sweep(heap, pool) == 0:
                    return True
                continue
            it += 1
            self.stats["iters"] += 1
            if it > max_iter:
                self.stats["capped"] += 1
                return False
            lb, rb = c.left.block, c.right.block
            if lb is not rb:
                self._merge(c)
                continue
            # нарушение ВНУТРИ блока
            cyc = self._active_path(c.right, c.left)
            # цикл активных ограничений неразрешим, ПОКА в нём нет ограничения
            # младшего яруса: если есть — рвём его, ради того и ярусы
            sc = self._split_between(c.left, c.right, c.priority, strict=cyc)
            if sc is None:
                c.unsatisfiable = True
                self.stats["unsat"] += 1
                continue
            self._push(heap, sc)
            if c.slack < -EPS:
                self._merge(c)

    def satisfy(self, tiered=True, max_iter=200000):
        """Довести систему до допустимой точки. -> True, если уложились в лимит.

        tiered=True: ярусы приоритетов вводятся по возрастанию (см. докстринг
        модуля) — при несовместности жертвуется младший ярус.
        """
        ok = True
        if tiered:
            tiers = sorted({c.priority for c in self.cons})
            pool = []
            for t in tiers:
                pool = pool + [c for c in self.cons if c.priority == t]
                ok = self._run(pool, max_iter) and ok
        else:
            ok = self._run(list(self.cons), max_iter)
        return ok

    def solve(self, max_iter=200000, rounds=20):
        """solve_VPSC: satisfy + доведение до оптимума расщеплением по lm<0."""
        ok = self.satisfy(max_iter=max_iter)
        for _ in range(rounds):
            done = True
            roots = {}
            for v in self.vars:
                roots.setdefault(id(v.block), v)
            for v in roots.values():
                self._compute_lm(v)
            worst = None
            for c in self.cons:
                if c.active and _eff_lm(c) < -EPS and c.splits < MAX_SPLITS:
                    if worst is None or _eff_lm(c) < _eff_lm(worst):
                        worst = c
            if worst is not None:
                worst.active = False
                worst.splits += 1
                self._rebuild(worst.left)
                self._rebuild(worst.right)
                self.stats["splits"] += 1
                done = False
            if done:
                break
            ok = self._run(list(self.cons), max_iter) and ok
        return ok

    def solve_keep_tier(self, max_iter=200000, rounds=20, tier=0):
        """`solve`, но расщепляются ТОЛЬКО ограничения яруса `tier`.

        Штатный `solve` ищет худшее по `_eff_lm`, а тот вычитает PRIO_BONUS у
        всего, что priority > 0, — то есть старший ярус рвётся ПЕРВЫМ. Для
        ограничений, которые заданы ПАРОЙ ВСТРЕЧНЫХ неравенств (створ блока —
        цикл в графе ограничений), это значит, что пара не доживает до
        оптимума никогда. Здесь порядок обратный: старшие ярусы держатся, а
        доводка идёт по нулевому.
        """
        ok = self.satisfy(max_iter=max_iter)
        for _ in range(rounds):
            roots = {}
            for v in self.vars:
                roots.setdefault(id(v.block), v)
            for v in roots.values():
                self._compute_lm(v)
            worst = None
            for c in self.cons:
                if (c.active and c.priority == tier and c.lm < -EPS
                        and c.splits < MAX_SPLITS):
                    if worst is None or c.lm < worst.lm:
                        worst = c
            if worst is None:
                break
            worst.active = False
            worst.splits += 1
            self._rebuild(worst.left)
            self._rebuild(worst.right)
            self.stats["splits"] += 1
            ok = self._run(list(self.cons), max_iter) and ok
        return ok

    # ------------------------------------------------------------ результаты
    def positions(self):
        return {v.key: v.position for v in self.vars}

    def violations(self, tol=1e-6):
        return [c for c in self.cons if c.slack < -tol]


# --------------------------------------------------------------------------
# генерация ограничений сепарации (R1)
# --------------------------------------------------------------------------
def _bx(it):
    return it["cx"] + it.get("ox", 0.0)


def _by(it):
    return it["cy"] + it.get("oy", 0.0)


def overlap_pairs(items, expand=0.0):
    """Пары прямоугольников, перекрывающихся с учётом собственных полей pad.

    items: список dict {key, cx, cy, hw, hh, pad} + необязательные ox, oy —
    смещение ЦЕНТРА БОКСА от координаты переменной (у узлов P&ID центроид не
    обязан совпадать с центром bbox; без этой поправки сепарация «касается»
    там, где бокс ещё перекрыт, и коннектор оказывается внутри блока).
    Требуемый зазор пары по оси
    = hw_i + hw_j + 2*min(pad_i, pad_j): пара «полноценных» боксов разводится
    на свой зазор, а бокс с точкой (коннектором, у которого pad=0) — только
    до касания, коннектор имеет право сидеть НА грани. Кандидаты ищутся
    STRtree (shapely в зависимостях проекта) — на 400-900 узлах этого
    достаточно, scan-line из статьи не нужен.

    -> [(i, j, ox, oy)], ox/oy — на сколько px пара перекрыта по каждой оси.
    """
    from shapely.geometry import box as _box
    from shapely.strtree import STRtree

    geoms = []
    for it in items:
        p = it["pad"] + expand
        bx = it["cx"] + it.get("ox", 0.0)
        by = it["cy"] + it.get("oy", 0.0)
        geoms.append(_box(bx - it["hw"] - p, by - it["hh"] - p,
                          bx + it["hw"] + p, by + it["hh"] + p))
    tree = STRtree(geoms)
    out = []
    for i, g in enumerate(geoms):
        for j in tree.query(g):
            j = int(j)
            if j <= i:
                continue
            a, b = items[i], items[j]
            pad = 2 * min(a["pad"], b["pad"])
            req_x = a["hw"] + b["hw"] + pad + 2 * expand
            req_y = a["hh"] + b["hh"] + pad + 2 * expand
            ox = req_x - abs(_bx(a) - _bx(b))
            oy = req_y - abs(_by(a) - _by(b))
            if ox > 1e-9 and oy > 1e-9:
                out.append((i, j, ox, oy))
    return out


def split_axis(items, pairs, force=None):
    """Разложить перекрытые пары по осям: чиним ту, где двигать меньше.

    force: {(ключ_a, ключ_b) отсортированный: 'x'|'y'} — пары, ось которых
    задана снаружи. Нужно там, где ось выбирает не геометрия, а смысл: если
    коннектор выровнен на грань блока равенством по y, разводить его с этим
    блоком МОЖНО только по x — иначе сепарация (ярус 0) и выравнивание
    (ярус 1) требуют противоположного и система заведомо несовместна.

    Классический removeOverlaps делает полный горизонтальный проход, потом
    вертикальный; здесь вход уже почти без наложений (их создаёт само
    выравнивание), поэтому дешевле чинить каждую пару по её короткой оси —
    это меньше рвёт узнаваемость (R2).

    Зазор возвращается в координатах ПЕРЕМЕННОЙ (не центра бокса): к нему
    добавлена разность смещений ox/oy, поэтому ограничение можно ставить
    прямо на переменную узла.

    -> (x_specs, y_specs), spec = (i_left, i_right, gap)
    """
    xs, ys = [], []
    force = force or {}
    for i, j, ox, oy in pairs:
        a, b = items[i], items[j]
        pad = 2 * min(a["pad"], b["pad"])
        ka, kb = a["key"], b["key"]
        pk = (ka, kb) if str(ka) <= str(kb) else (kb, ka)
        want = force.get(pk)
        if want == "x" or (want is None and ox <= oy):
            base = a["hw"] + b["hw"] + pad
            if (_bx(a), str(ka)) <= (_bx(b), str(kb)):
                xs.append((i, j, base + a.get("ox", 0.0) - b.get("ox", 0.0)))
            else:
                xs.append((j, i, base + b.get("ox", 0.0) - a.get("ox", 0.0)))
        else:
            base = a["hh"] + b["hh"] + pad
            if (_by(a), str(ka)) <= (_by(b), str(kb)):
                ys.append((i, j, base + a.get("oy", 0.0) - b.get("oy", 0.0)))
            else:
                ys.append((j, i, base + b.get("oy", 0.0) - a.get("oy", 0.0)))
    return xs, ys

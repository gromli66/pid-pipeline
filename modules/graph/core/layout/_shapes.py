# -*- coding: utf-8 -*-
"""_shapes.py — реальная форма узла и таблица легальных наложений.

Раскладка исторически представляла узел ПРЯМОУГОЛЬНИКОМ (bbox). Для крупного
ручного контура это неверно вдвойне:

  * габарит невыпуклого контура почти вдвое больше самой фигуры (замер
    c2f79462, деаэратор node_28: полигон 123326 px² при bbox 229848,
    заполненность 0.537). Из 34 пар «узел внутри габарита» реально касаются
    фигуры 10 — остальные 24 стоят в пустых углах и наложением не являются;
  * расталкивая эти 24 пары, расстановка выселяет из бака всю его обвязку
    (замер: внутри габарита было 30 узлов, оставалось 0) и растягивает трубы
    блока с 954 до 3604 px.

Второе — решение заказчика (2026-07-29): **наложения, пришедшие из построения
и проверки, легальны**, причём легальны перекрытия ИЗНАЧАЛЬНО ДЕТЕКТИРОВАННЫХ
размеров, а не появившиеся после подмены bbox словарём `FIXED_SIZES`. Замер по
корпусу (наложений в детекции / после словаря): 51b339ab 5/387, a6d28736
4/128, 8d517a35 24/151, 89ca7583 4/62, 13d1ef5f 33/70, 6e7144d5 5/24,
d74eb9f1 0/0, c2f79462 33/44 — то есть на плотных схемах 95%+ наложений
СОЗДАНЫ словарём и амнистии не подлежат.

Инвариант расстановки становится «наложений не больше, чем в детектированной
геометрии», а не «ноль».
"""
from __future__ import annotations

from shapely.affinity import translate
from shapely.geometry import Point, Polygon, box as shp_box
from shapely.strtree import STRtree

from ..graph_access import is_connector, node_cxy

# px холста: узел считается стоящим НА границе фигуры, если он к ней ближе.
# Замер c2f79462: 9 из 20 «угловых» узлов лежат в 0.31-2.46 px от контура —
# при масштабе холста 0.3072 это 1-8 растровых пикселей, то есть шум
# трассировки, а не расстояние; следующий сосед уже на 10.64 px. Разрыв в
# распределении реальный, 3 px попадают в его середину. Проверка на
# устойчивость: tol=6 даёт побитово тот же результат — порог стоит на плато.
BORDER_TOL = 3.0


def shape_of(node):
    """Нарисованная форма узла: полигон из `segmentation`, иначе bbox.

    `segmentation` едет вместе с узлом (`graph_access.move_node`), поэтому у
    графа, который уже разложен, форма актуальна.
    """
    seg = node.get("segmentation")
    if seg and isinstance(seg, list) and len(seg) >= 6:
        p = Polygon([(seg[i], seg[i + 1]) for i in range(0, len(seg), 2)])
        if not p.is_valid:
            p = p.buffer(0)
        if not p.is_empty and p.area > 0.0:
            return p
    bb = node.get("bbox")
    if bb and len(bb) == 4 and bb[2] > bb[0] and bb[3] > bb[1]:
        return shp_box(*(float(v) for v in bb))
    c = node.get("centroid")
    return Point(float(c[1]), float(c[0])) if c and len(c) >= 2 else None


def has_polygon(node):
    seg = node.get("segmentation")
    return bool(seg) and isinstance(seg, list) and len(seg) >= 6


def legal_pairs(graph, detected_bbox=None, tol=BORDER_TOL):
    """Пары узлов, наложенные в ДЕТЕКТИРОВАННОЙ геометрии. -> {(a, b)}, a < b.

    `detected_bbox = {node_id: [x1,y1,x2,y2]}` — габариты ДО подмены словарём
    размеров, их кладёт `canvas_input.to_canvas`. Центроиды и `segmentation` к
    этому моменту те же, что были: `apply_fixed_sizes` двигает только bbox
    (и снимает контур у словарных классов, а таких с контуром во всём корпусе
    ноль). Без `detected_bbox` считается по текущей геометрии — это уже НЕ
    детекция, и амнистия окажется шире положенного.

    Допуск `tol` («узел стоит на границе фигуры») действует, только если хотя
    бы у одного узла пары есть настоящий полигон: на графах без контуров
    правило вырождается в строгое пересечение.
    """
    shapes, poly = {}, {}
    for n in graph.get("nodes", []):
        if is_connector(n) or "centroid" not in n:
            continue                    # коннектор — точка, не накладывается
        if detected_bbox is not None and not has_polygon(n):
            bb = detected_bbox.get(n["id"])
            g = (shp_box(*(float(v) for v in bb))
                 if bb and len(bb) == 4 and bb[2] > bb[0] and bb[3] > bb[1]
                 else None)
        else:
            g = shape_of(n)
        if g is not None:
            shapes[n["id"]] = g
            poly[n["id"]] = has_polygon(n)
    keys = sorted(shapes)
    geoms = [shapes[k] for k in keys]
    if not geoms:
        return set()
    tree = STRtree(geoms)
    probe = [g.buffer(tol, join_style=2) if tol > 0 else g for g in geoms]
    out = set()
    for i, g in enumerate(probe):
        for j in tree.query(g, predicate="intersects"):
            j = int(j)
            if j <= i:
                continue
            if geoms[i].intersection(geoms[j]).area > 0.0:
                out.add((keys[i], keys[j]))
            elif tol > 0.0 and (poly[keys[i]] or poly[keys[j]]) \
                    and geoms[i].distance(geoms[j]) < tol:
                out.add((keys[i], keys[j]))
    return out


class ShapeIndex:
    """Формы узлов, привязанные к их центроидам, + таблица легальных пар.

    Расстановка двигает `items` (cx/cy), не трогая граф до самого конца,
    поэтому форму нельзя читать из `segmentation` по ходу решения: она там
    ещё на старом месте. Держим форму относительно центроида и переносим на
    лету.
    """

    __slots__ = ("_base", "legal")

    def __init__(self, graph, legal=()):
        self._base = {}
        for n in graph.get("nodes", []):
            if "centroid" not in n:
                continue
            g = shape_of(n)
            if g is None:
                continue
            cx, cy = node_cxy(n)
            self._base[n["id"]] = (g, cx, cy)
        self.legal = {tuple(sorted(p)) for p in legal}

    def at(self, nid, cx, cy):
        """Форма узла, перенесённая в текущее положение центроида."""
        rec = self._base.get(nid)
        if rec is None:
            return None
        g, x0, y0 = rec
        dx, dy = cx - x0, cy - y0
        return g if (dx == 0.0 and dy == 0.0) else translate(g, dx, dy)

    def is_legal(self, a, b):
        return (a, b) in self.legal if a < b else (b, a) in self.legal

# -*- coding: utf-8 -*-
"""text_import.py — перенос подписей и привязок на холст раскладки.

Дыра, которую этот модуль закрывает (§3.8 плана AUTO_LAYOUT_INTEGRATION.md).
Раскладка стартует после контуров, то есть ДО OCR: в этот момент в графе нет
ни одного text_block. Значит холст, собранный задачей, придёт к оператору
пустым по тексту — а он к тому времени уже прошёл привязку. Поэтому текст на
холст импортируется из ТЕКУЩЕГО graph_validated, и делает это один модуль:
его зовёт и клиент при открытии «Ручной правки», и воркер при экспорте FXML
(иначе холст, не открытый оператором, дал бы FXML без единого <Text> и без KKS).

Здесь же живут якоря §3.9 — и это единственная точка, где они исполнимы:
только тут доступны обе геометрии сразу. До-раскладочная восстанавливается
`canvas_input.to_canvas(graph_validated)` (раскладка исходный файл не трогает),
после-раскладочная — сам холст.

ЗАЧЕМ ЯКОРЬ. Замер на корпусе: привязанных блоков 0 или 2 из сотен, то есть
96-100 % подписей ни к чему не привязаны. Раскладка переставляет ~99 % узлов,
а подписи не трогает — и без якоря ближайший элемент меняют 308 подписей из
312 (51b339ab). Это не косметика: непривязанный блок уходит в FXML как <Text>.

ГЛАВНОЕ, ЧТО ЛЕГКО ПРОЧИТАТЬ НЕВЕРНО: блок с якорем сохраняет положение
ОТНОСИТЕЛЬНО СВОЕГО ЭЛЕМЕНТА, а не относительно листа. Абсолютная точка при
этом может уехать далеко — ровно настолько, насколько уехал элемент. Оставить
подпись «там, где она была на листе» — это и есть сломанное поведение.

Оси двойственны (CODING_GUIDE §6): centroid и точки рёбер — [y, x], а bbox —
[x1, y1, x2, y2]. Вся арифметика ниже ведётся в (x, y) и переворачивает оси
явно на границах.

Qt здесь нет: модуль зовут и клиент, и воркер.
"""
from __future__ import annotations

import math
from copy import deepcopy

from .canvas_input import to_canvas

# Порог якоря: дальше этого ближайший элемент подписи не «свой», и блок
# остаётся там, куда его поставило масштабирование (штамп, рамка, легенда —
# они и не относятся ни к чему). Числа в SOLUTION.md нет: стенд текстом не
# занимался. Ориентир плана — 64 px холста; выверяется глазами (Э7 п. 1).
ANCHOR_MAX = 64.0

# Зазор привязанного блока от грани цели — как в редакторе (_BIND_GAP).
BIND_GAP = 6.0

# Ниже этой разницы якорь-узел предпочитается якорю-ребру: раскладка стирает
# waypoints и переориентирует участки H<->V, поэтому доля вдоль ребра и знак
# перпендикуляра на новой геометрии — грубое приближение (аудит Д2).
NODE_PREFERENCE = 6.0

_SIDES = ("top", "right", "left", "bottom")


# ───────────────────────────── геометрия ─────────────────────────────

def _active(graph):
    return [t for t in (graph.get("text_blocks") or [])
            if t.get("merged_into") is None and t.get("bbox")]


def _edges(graph):
    return graph.get("edges") if "edges" in graph else graph.get("links", [])


def _edge_ends(e):
    return (e.get("source") or e.get("from"), e.get("target") or e.get("to"))


def _polyline(e):
    """[(x, y), ...]: source_point -> waypoints|path -> target_point."""
    pts = []
    sp, tp = e.get("source_point"), e.get("target_point")
    mid = e.get("waypoints") or e.get("path") or []
    if sp:
        pts.append((float(sp[1]), float(sp[0])))
    for p in mid:
        pts.append((float(p[1]), float(p[0])))
    if tp:
        pts.append((float(tp[1]), float(tp[0])))
    return pts


def _node_xy(node):
    c = node.get("centroid")
    return (float(c[1]), float(c[0])) if c else None


def _node_box(node):
    """Прямоугольник узла; коннектор без bbox — вырожденный в центроид."""
    bb = node.get("bbox")
    if bb and len(bb) == 4 and bb[2] > bb[0] and bb[3] > bb[1]:
        return (float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3]))
    xy = _node_xy(node)
    return (xy[0], xy[1], xy[0], xy[1]) if xy else None


def _center(bbox):
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _pt_rect(px, py, r):
    return math.hypot(max(r[0] - px, 0.0, px - r[2]),
                      max(r[1] - py, 0.0, py - r[3]))


def _pt_seg(px, py, ax, ay, bx, by):
    """(расстояние, доля вдоль сегмента)."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx
                                               + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)), t


def _polyline_at(pl, frac):
    """Точка на доле frac длины полилинии + единичный вектор направления."""
    segs = [(pl[i], pl[i + 1]) for i in range(len(pl) - 1)]
    lens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in segs]
    total = sum(lens)
    if total <= 0:
        return pl[0], (1.0, 0.0)
    want = max(0.0, min(1.0, frac)) * total
    run = 0.0
    for (a, b), L in zip(segs, lens):
        if run + L >= want or (a, b) == segs[-1]:
            k = 0.0 if L <= 0 else (want - run) / L
            ux, uy = ((b[0] - a[0]) / L, (b[1] - a[1]) / L) if L > 0 else (1.0, 0.0)
            return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k), (ux, uy)
        run += L
    return pl[-1], (1.0, 0.0)


# ───────────────────────────── привязки ─────────────────────────────

def nearest_bind_side(target_bbox, ref_bbox) -> str:
    """Сторона цели по «выходам» центра блока за её грани.

    Повторяет `OcrLayerMixin._nearest_bind_side` один в один: привязки из
    вкладки привязки идут БЕЗ side, а производная позиция работает только при
    заданной стороне — без синтеза «блок едет за целью» не сработал бы ни для
    одной привязки корпуса.
    """
    tx1, ty1, tx2, ty2 = target_bbox
    cx, cy = _center(ref_bbox)
    dx = (cx - tx2) if cx > tx2 else (cx - tx1) if cx < tx1 else 0.0
    dy = (cy - ty2) if cy > ty2 else (cy - ty1) if cy < ty1 else 0.0
    if dx or dy:
        if abs(dx) >= abs(dy):
            return "right" if dx > 0 else "left"
        return "bottom" if dy > 0 else "top"
    side, best = "right", tx2 - cx
    for s, d in (("left", cx - tx1), ("top", cy - ty1), ("bottom", ty2 - cy)):
        if d < best:
            side, best = s, d
    return side


def bound_block_bbox(target_bbox, side, gap, w, h):
    """Производный bbox привязанного блока — как рисует редактор."""
    tcx, tcy = _center(target_bbox)
    if side == "top":
        x1, y1 = tcx - w / 2.0, target_bbox[1] - gap - h
    elif side == "bottom":
        x1, y1 = tcx - w / 2.0, target_bbox[3] + gap
    elif side == "left":
        x1, y1 = target_bbox[0] - gap - w, tcy - h / 2.0
    else:
        x1, y1 = target_bbox[2] + gap, tcy - h / 2.0
    return [x1, y1, x1 + w, y1 + h]


def _target_bbox(binding, byid, edges_by_key):
    """bbox цели привязки в заданной геометрии."""
    nid = binding.get("node_id")
    if nid and nid in byid:
        return _node_box(byid[nid])
    key = binding.get("edge_key")
    if key and "|" in str(key):
        e = edges_by_key.get(_norm_key(str(key)))
        if e:
            pl = _polyline(e)
            if len(pl) >= 2:
                mx = (pl[0][0] + pl[-1][0]) / 2.0
                my = (pl[0][1] + pl[-1][1]) / 2.0
                return (mx, my, mx, my)
    return None


def _norm_key(key: str) -> str:
    a, b = key.split("|", 1)
    return f"{min(a, b)}|{max(a, b)}"


def _index(graph):
    """(узлы по id, рёбра по ключу привязки, рёбра списком).

    Список — по позиции: раскладка порядок рёбер не меняет, поэтому индекс
    служит стабильным ключом якоря между двумя геометриями.
    """
    byid = {n["id"]: n for n in graph.get("nodes", [])}
    elist = list(_edges(graph))
    ekey = {}
    for e in elist:
        s, t = _edge_ends(e)
        if s and t:
            ekey[_norm_key(f"{s}|{t}")] = e
    return byid, ekey, elist


# ───────────────────────────── якоря ─────────────────────────────

def _find_anchor(bbox, byid, elist, anchor_max):
    """Ближайший элемент до раскладки. -> (вид, ключ, параметры) или None."""
    cx, cy = _center(bbox)

    best_node, dn = None, float("inf")
    for nid, n in byid.items():
        box = _node_box(n)
        if box is None:
            continue
        d = _pt_rect(cx, cy, box)
        if d < dn:
            best_node, dn = nid, d

    best_edge, de, best_t = None, float("inf"), 0.0
    for idx, e in enumerate(elist):
        pl = _polyline(e)
        if len(pl) < 2:
            continue
        segs = [(pl[i], pl[i + 1]) for i in range(len(pl) - 1)]
        lens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in segs]
        total = sum(lens) or 1.0
        run = 0.0
        for (a, b), L in zip(segs, lens):
            d, t = _pt_seg(cx, cy, a[0], a[1], b[0], b[1])
            if d < de:
                best_edge, de, best_t = idx, d, (run + t * L) / total
            run += L

    if min(dn, de) > anchor_max:
        return None
    # При сравнимой близости побеждает узел: якорь-ребро переживает перекладку
    # хуже (waypoints стираются, участки переориентируются).
    if best_node is not None and dn <= de + NODE_PREFERENCE:
        n = byid[best_node]
        nx, ny = _center(_node_box(n))
        return ("node", best_node, (cx - nx, cy - ny))
    if best_edge is not None:
        pt, (ux, uy) = _polyline_at(_polyline(elist[best_edge]), best_t)
        off = (cx - pt[0]) * (-uy) + (cy - pt[1]) * ux    # знаковый перпендикуляр
        return ("edge", best_edge, (best_t, off))
    return None


def _apply_anchor(anchor, byid_after, elist_after, w, h):
    """Положение блока в после-раскладочной геометрии. None — не применился."""
    kind, key, params = anchor
    if kind == "node":
        n = byid_after.get(key)
        if n is None:
            return None
        nx, ny = _center(_node_box(n))
        cx, cy = nx + params[0], ny + params[1]
    else:
        if key >= len(elist_after):
            return None
        pl = _polyline(elist_after[key])
        if len(pl) < 2:
            return None
        t, off = params
        pt, (ux, uy) = _polyline_at(pl, t)
        cx, cy = pt[0] + (-uy) * off, pt[1] + ux * off
    return [cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0]


# ───────────────────────────── вход модуля ─────────────────────────────

def import_text(canvas_graph, validated_graph, *, anchor_max=ANCHOR_MAX,
                bind_gap=BIND_GAP):
    """Перенести подписи и привязки из graph_validated на холст. -> статистика.

    `canvas_graph` мутируется: в него кладутся `text_blocks` (в координатах
    холста, у своих элементов) и `bindings` (с синтезированными side/gap).
    Отметку `text_imported_sha` ставит вызывающий — он же решает, когда импорт
    разрешён (после первой правки текста на холсте первоисточником становится
    сам холст, §3.8 п. 3).
    """
    pre, _transform = to_canvas(validated_graph)
    blocks = _active(pre)
    if not blocks:
        canvas_graph["text_blocks"] = []
        canvas_graph["bindings"] = []
        return {"blocks": 0, "bound": 0, "anchored_node": 0,
                "anchored_edge": 0, "no_anchor": 0}

    byid_pre, ekey_pre, elist_pre = _index(pre)
    byid_new, ekey_new, elist_new = _index(canvas_graph)

    bindings = deepcopy(validated_graph.get("bindings") or [])
    by_block = {b.get("block_id"): b for b in bindings if b.get("block_id")}

    stats = {"blocks": len(blocks), "bound": 0, "anchored_node": 0,
             "anchored_edge": 0, "no_anchor": 0}
    out_blocks = []

    for blk in blocks:
        blk = deepcopy(blk)
        bb = [float(v) for v in blk["bbox"]]
        w = max(1.0, bb[2] - bb[0])
        h = max(1.0, bb[3] - bb[1])
        binding = by_block.get(blk.get("id"))

        if binding is not None:
            # Привязанный блок: позиция производная от цели. side/gap
            # синтезируем по исходному взаимному положению, если их нет.
            tb_pre = _target_bbox(binding, byid_pre, ekey_pre)
            if binding.get("side") not in _SIDES and tb_pre is not None:
                binding["side"] = nearest_bind_side(tb_pre, bb)
                binding.setdefault("gap", bind_gap)
            tb_new = _target_bbox(binding, byid_new, ekey_new)
            if tb_new is not None and binding.get("side") in _SIDES:
                blk["bbox"] = bound_block_bbox(
                    tb_new, binding["side"], float(binding.get("gap", bind_gap)),
                    w, h)
                stats["bound"] += 1
                out_blocks.append(blk)
                continue

        anchor = _find_anchor(bb, byid_pre, elist_pre, anchor_max)
        if anchor is None:
            # Штамп, рамка, легенда: они и не относятся ни к чему — остаются
            # там, куда их поставило масштабирование. Для штампа это и есть
            # правильное поведение.
            stats["no_anchor"] += 1
            out_blocks.append(blk)
            continue

        placed = _apply_anchor(anchor, byid_new, elist_new, w, h)
        if placed is None:
            stats["no_anchor"] += 1
        else:
            blk["bbox"] = placed
            stats["anchored_node" if anchor[0] == "node" else "anchored_edge"] += 1
        out_blocks.append(blk)

    canvas_graph["text_blocks"] = out_blocks
    canvas_graph["bindings"] = bindings
    return stats

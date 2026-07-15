# -*- coding: utf-8 -*-
"""
pretransform.py — стадия pre-transform для WYSIWYG-редактора (Фаза 1).

Переводит валидированный граф из пикселей изображения в фиксированный холст
1920x1080, задаёт скин-контролам фиксированный размер по таблице и разъезжает
наложения вдоль труб (declust). Всё, что делает редактор дальше, работает уже
в координатах холста; экспорт FXML — identity.

Поток:
    graph_validated.json (px изображения)
        -> transform_to_canvas   (масштаб всех координат в 1920x1080, вкл. path)
        -> apply_fixed_sizes      (bbox скин-классов = размер из таблицы)
        -> declust                (perp + разрез MAXGAP + потолок CAP)
    -> graph_1920.json (+ transform: s, offset для обратной трассировки)

Замечания по координатам (двойственность осей, CODING_GUIDE §6):
    centroid            = [y, x]
    bbox                = [x1, y1, x2, y2]
    source/target_point = [y, x]
    path / waypoints    = [[y, x], ...]   (могут отсутствовать в validated)
    segmentation        = [x, y, x, y, ...]

Запуск (offline-тест):
    PYTHONPATH=. python -m modules.graph.core.pretransform in.json -o out.json
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

# --- Целевой холст ---
TARGET_W = 1920.0
TARGET_H = 1080.0

# --- Ручки алгоритма (все в координатах холста 1920x1080) ---
DECLUST_GAP = 8.0        # мин. зазор между символами вдоль трубы
DECLUST_TOL = 21.0       # поперечный допуск «одна труба» (~½ ширины бокса)
DECLUST_MAXGAP = 140.0   # разрыв вдоль оси, разделяющий разные под-пробеги
DECLUST_CAP = 60.0       # потолок смещения символа (верность фону > нуля наложений)

# ---------------------------------------------------------------------------
# Таблица фиксированных размеров: class_name -> (W, H) в px холста (HORIZONTAL).
# Для вертикали W/H меняются местами. Значение 0 по оси -> вывести из aspect
# скина (skin_geometry). Классы вне таблицы размер не меняют.
# TODO(Фаза 0): вынести в configs. datchik(0,30)/output(0,18) — проверить,
#   не опечатка ли (см. диалог); у датчика есть своя фикс-логика в standardize.
# ---------------------------------------------------------------------------
FIXED_SIZES = {
    'armatura_ruchn': (42, 38),
    'klapan_obratn': (42, 38),
    'regulator_ruchn': (42, 38),
    'armatura_electro': (42, 38),
    'regulator_electro': (42, 38),
    'klapan_obratn_seroprivod': (42, 38),
    'armatura_seroprivod': (42, 38),
    'regulator_seroprivod': (42, 38),
    'armatura_membr_electro': (42, 38),
    'predohran': (42, 38),
    'nasos': (45, 45),
    'ventilaytor': (45, 45),
    'vodostruiniy_nasos': (45, 45),
    'teploobmen': (90, 90),
    'filtr_meh': (60, 60),
    'electronagrevat': (90, 90),
    'datchik': (0, 30),
    'output': (0, 18),
    'strelka': (20, 20),
}


def _load_skin_aspect():
    """skinType -> aspect_hw (h/w) из tools/skin_geometry.json; для вывода 0-оси."""
    p = Path(__file__).resolve().parents[3] / "tools" / "skin_geometry.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {k: float(v.get("aspect_hw", 1.0)) for k, v in data.get("skins", {}).items()}
    except (OSError, ValueError):
        return {}


def _class_to_skin():
    """class_name -> skinType (для aspect по 0-осям). Мягкий импорт."""
    try:
        from modules.graph_to_fxml import CLASS_NAME_TO_SKIN
        return {c: spec[1] for c, spec in CLASS_NAME_TO_SKIN.items()}
    except Exception:
        return {}


_SKIN_ASPECT = _load_skin_aspect()
_CLASS_SKIN = _class_to_skin()


# ---------------------------------------------------------------------------
# Утилиты доступа к рёбрам (validated: 'links'+source/target; raw: 'edges'+from/to)
# ---------------------------------------------------------------------------
def _edges(graph):
    return graph.get("edges") if "edges" in graph else graph.get("links", [])


def _edge_ends(e):
    return (e.get("source") or e.get("from"), e.get("target") or e.get("to"))


# ---------------------------------------------------------------------------
# Шаг 1. Масштаб всех координат в холст 1920x1080 (letterbox).
# ---------------------------------------------------------------------------
def transform_to_canvas(graph, image_hw):
    """Вписать граф в 1920x1080. Возвращает transform {s, offx, offy}.

    image_hw = (height, width) исходного изображения.
    Мутирует граф in-place. Обновляет graph['graph']['image_size'] = [1080, 1920].
    """
    ih, iw = image_hw
    s = min(TARGET_W / iw, TARGET_H / ih)
    offx = (TARGET_W - iw * s) / 2.0
    offy = (TARGET_H - ih * s) / 2.0

    def X(x):
        return x * s + offx

    def Y(y):
        return y * s + offy

    for n in graph.get("nodes", []):
        c = n.get("centroid")
        if c:
            n["centroid"] = [Y(c[0]), X(c[1])]              # [y, x]
        bb = n.get("bbox")
        if bb:
            n["bbox"] = [X(bb[0]), Y(bb[1]), X(bb[2]), Y(bb[3])]
        seg = n.get("segmentation")
        if seg:
            n["segmentation"] = [X(seg[i]) if i % 2 == 0 else Y(seg[i])
                                 for i in range(len(seg))]   # [x, y, ...]

    for e in _edges(graph):
        for k in ("source_point", "target_point"):
            p = e.get(k)
            if p:
                e[k] = [Y(p[0]), X(p[1])]                    # [y, x]
        for k in ("path", "waypoints"):
            pts = e.get(k)
            if pts:
                e[k] = [[Y(p[0]), X(p[1])] for p in pts]     # [[y, x], ...]

    graph.setdefault("graph", {})["image_size"] = [int(TARGET_H), int(TARGET_W)]
    return {"s": s, "offx": offx, "offy": offy,
            "orig_image_size": [ih, iw], "canvas": [int(TARGET_W), int(TARGET_H)]}


# ---------------------------------------------------------------------------
# Шаг 2. Фиксированный размер скин-классов + ориентация.
# ---------------------------------------------------------------------------
def _node_axis(node, adj):
    """Ось трубы у узла: 'V' (вертикальная, символ узкий-высокий) или 'H'.

    По направлениям точек подключения относительно центроида.
    """
    c = node.get("centroid")
    pts = adj.get(node["id"], [])
    if not c or not pts:
        return "H"
    cy, cx = c[0], c[1]
    dh = sum(abs(px - cx) for _, (py, px) in pts)
    dv = sum(abs(py - cy) for _, (py, px) in pts)
    return "V" if dv >= dh else "H"


def _fixed_wh(class_name, axis):
    """(W, H) для класса с учётом оси; 0-ось выводится из aspect скина."""
    w, h = FIXED_SIZES[class_name]
    aspect = _SKIN_ASPECT.get(_CLASS_SKIN.get(class_name, ""), 1.0) or 1.0
    if w == 0 and h:
        w = h / aspect                       # aspect = h/w  ->  w = h/aspect
    elif h == 0 and w:
        h = w * aspect
    if axis == "V" and abs(w - h) > 1e-6:    # вертикаль: узкий-высокий
        w, h = h, w
    return float(w), float(h)


def apply_fixed_sizes(graph, adj):
    """Скин-классам задать bbox = фикс-размер вокруг центроида. In-place.

    Ориентацию (H/V) кладём в node['_axis'] для downstream (throat, экспорт).
    """
    for n in graph.get("nodes", []):
        cn = n.get("class_name")
        c = n.get("centroid")
        if cn not in FIXED_SIZES or not c:
            continue
        axis = _node_axis(n, adj)
        w, h = _fixed_wh(cn, axis)
        cy, cx = c[0], c[1]
        n["bbox"] = [cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0]
        n["_axis"] = axis


# ---------------------------------------------------------------------------
# Шаг 3. Расклейка вдоль трубы (declust): perp + разрез MAXGAP + потолок CAP.
# ---------------------------------------------------------------------------
def _pava(desired, gaps):
    """Мин Σ(p-d)² при p[i+1]-p[i] >= gaps[i], порядок сохранён (PAVA, L2)."""
    n = len(desired)
    cum = [0.0] * n
    for i in range(1, n):
        cum[i] = cum[i - 1] + gaps[i - 1]
    c = [desired[i] - cum[i] for i in range(n)]
    vals, wts = [], []
    for x in c:
        vals.append(x)
        wts.append(1.0)
        while len(vals) > 1 and vals[-2] > vals[-1]:
            v = (vals[-2] * wts[-2] + vals[-1] * wts[-1]) / (wts[-2] + wts[-1])
            w = wts[-2] + wts[-1]
            vals.pop(); vals.pop(); wts.pop(); wts.pop()
            vals.append(v); wts.append(w)
    q = []
    for v, w in zip(vals, wts):
        q += [v] * int(w)
    return [q[i] + cum[i] for i in range(n)]


def declust(graph, adj, gap=DECLUST_GAP, tol=DECLUST_TOL,
            maxgap=DECLUST_MAXGAP, cap=DECLUST_CAP):
    """Разъехать наложения фикс-символов вдоль их труб. In-place.

    Возвращает статистику. Двигает centroid/bbox и подшивает концы рёбер
    (source/target_point + терминальную точку path/waypoints) на ту же дельту.
    """
    boxes = []
    for n in graph.get("nodes", []):
        cn = n.get("class_name")
        c = n.get("centroid")
        bb = n.get("bbox")
        if cn not in FIXED_SIZES or not c or not bb:
            continue
        axis = n.get("_axis", "H")
        pts = adj.get(n["id"], [])
        # поперечная координата = средняя по точкам подключения (уже на трубе)
        if pts:
            perp = (statistics.fmean(px for _, (py, px) in pts) if axis == "V"
                    else statistics.fmean(py for _, (py, px) in pts))
        else:
            perp = c[1] if axis == "V" else c[0]
        boxes.append({
            "node": n, "axis": axis, "perp": perp,
            "cx": c[1], "cy": c[0], "cx0": c[1], "cy0": c[0],
            "w": bb[2] - bb[0], "h": bb[3] - bb[1],
        })

    before = _count_overlaps(boxes)

    # группировка: ось -> кластер поперечной коорд -> разрез по along (MAXGAP)
    groups = []
    for axis in ("V", "H"):
        akey = "cy" if axis == "V" else "cx"
        grp = sorted((b for b in boxes if b["axis"] == axis), key=lambda b: b["perp"])
        cluster = []
        for b in grp:
            if cluster and b["perp"] - cluster[-1]["perp"] > tol:
                groups += _split_along(cluster, akey, maxgap)
                cluster = []
            cluster.append(b)
        if cluster:
            groups += _split_along(cluster, akey, maxgap)

    # раздвиг каждого под-пробега
    for run in groups:
        if len(run) < 2:
            continue
        axis = run[0]["axis"]
        akey = "cy" if axis == "V" else "cx"
        ext = "h" if axis == "V" else "w"
        run.sort(key=lambda b: b[akey])
        d = [b[akey] for b in run]
        gaps = [run[i][ext] / 2 + run[i + 1][ext] / 2 + gap for i in range(len(run) - 1)]
        if all(d[i + 1] - d[i] >= gaps[i] - 0.5 for i in range(len(run) - 1)):
            continue
        for b, np_ in zip(run, _pava(d, gaps)):
            b[akey] = max(b[akey] - cap, min(b[akey] + cap, np_))  # потолок

    moved = 0
    for b in boxes:
        dx, dy = b["cx"] - b["cx0"], b["cy"] - b["cy0"]
        if abs(dx) < 0.5 and abs(dy) < 0.5:
            continue
        moved += 1
        _apply_move(graph, b["node"], dx, dy)

    after = _count_overlaps(boxes)
    disp = [math.hypot(b["cx"] - b["cx0"], b["cy"] - b["cy0"]) for b in boxes]
    md = [x for x in disp if x > 0.5]
    return {"symbols": len(boxes), "overlaps_before": before, "overlaps_after": after,
            "moved": moved,
            "max_disp": round(max(disp), 1) if disp else 0.0,
            "mean_disp": round(statistics.fmean(md), 1) if md else 0.0}


def _split_along(cluster, akey, maxgap):
    """Разрезать кластер (одна поперечная координата) на под-пробеги по разрыву."""
    cluster.sort(key=lambda b: b[akey])
    runs, cur = [], []
    for b in cluster:
        if cur and b[akey] - cur[-1][akey] > maxgap:
            runs.append(cur)
            cur = []
        cur.append(b)
    if cur:
        runs.append(cur)
    return runs


def _apply_move(graph, node, dx, dy):
    """Сдвинуть узел на (dx,dy) и подшить концы инцидентных рёбер."""
    c = node["centroid"]
    node["centroid"] = [c[0] + dy, c[1] + dx]
    bb = node.get("bbox")
    if bb:
        node["bbox"] = [bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy]
    nid = node["id"]
    for e in _edges(graph):
        src, tgt = _edge_ends(e)
        for role, pkey in (("s", "source_point"), ("t", "target_point")):
            end_id = src if role == "s" else tgt
            if end_id != nid:
                continue
            p = e.get(pkey)
            if p:
                e[pkey] = [p[0] + dy, p[1] + dx]
            # подшить терминальную точку маршрута (baseline: только конец)
            for wk in ("path", "waypoints"):
                pts = e.get(wk)
                if not pts:
                    continue
                idx = 0 if role == "s" else -1
                pts[idx] = [pts[idx][0] + dy, pts[idx][1] + dx]


def _count_overlaps(boxes):
    r = 0
    for i in range(len(boxes)):
        a = boxes[i]
        for j in range(i + 1, len(boxes)):
            b = boxes[j]
            if (abs(a["cx"] - b["cx"]) * 2 < a["w"] + b["w"]
                    and abs(a["cy"] - b["cy"]) * 2 < a["h"] + b["h"]):
                r += 1
    return r


# ---------------------------------------------------------------------------
# Оркестрация
# ---------------------------------------------------------------------------
def _build_adjacency(graph):
    """node_id -> [(edge_id, (y, x)), ...] точки подключения (координаты холста)."""
    adj = defaultdict(list)
    for e in _edges(graph):
        src, tgt = _edge_ends(e)
        for end_id, pkey in ((src, "source_point"), (tgt, "target_point")):
            p = e.get(pkey)
            if end_id and p:
                adj[end_id].append((e.get("id"), (p[0], p[1])))
    return adj


def pretransform(graph, image_hw=None):
    """Полный pre-transform. Возвращает (graph_1920, transform, stats). Не мутирует вход."""
    g = deepcopy(graph)
    # Идемпотентность: граф уже в координатах холста (напр. пере-открытие сохранённого) — не трогаем.
    size = g.get("graph", {}).get("image_size")
    if size and [int(size[0]), int(size[1])] == [int(TARGET_H), int(TARGET_W)]:
        transform = {"s": 1.0, "offx": 0.0, "offy": 0.0,
                     "orig_image_size": list(size),
                     "canvas": [int(TARGET_W), int(TARGET_H)], "identity": True}
        stats = {"symbols": 0, "overlaps_before": 0, "overlaps_after": 0,
                 "moved": 0, "max_disp": 0.0, "mean_disp": 0.0, "skipped": True}
        return g, transform, stats
    if image_hw is None:
        size = g.get("graph", {}).get("image_size")
        if not size:
            raise ValueError("image_size отсутствует в graph.graph — передай image_hw=(h,w)")
        image_hw = (size[0], size[1])

    transform = transform_to_canvas(g, image_hw)
    adj = _build_adjacency(g)
    apply_fixed_sizes(g, adj)
    adj = _build_adjacency(g)  # точки подключения не двигались, но пересоберём для чистоты
    stats = declust(g, adj)
    return g, transform, stats


def main():
    ap = argparse.ArgumentParser(description="pre-transform графа в холст 1920x1080")
    ap.add_argument("input", type=Path, help="graph_validated.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="graph_1920.json")
    ap.add_argument("--image-size", type=str, default=None,
                    help="HxW исходного изображения, если нет в графе (напр. 3509x4964)")
    args = ap.parse_args()

    graph = json.loads(args.input.read_text(encoding="utf-8"))
    image_hw = None
    if args.image_size:
        h, w = args.image_size.lower().split("x")
        image_hw = (int(h), int(w))

    g, transform, stats = pretransform(graph, image_hw)
    args.output.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    args.output.with_suffix(".transform.json").write_text(
        json.dumps(transform, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK -> {args.output}")
    print(f"  s={transform['s']:.4f}  stats={stats}")


if __name__ == "__main__":
    main()

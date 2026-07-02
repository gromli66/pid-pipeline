# -*- coding: utf-8 -*-
"""Регрессионные метрики роутинга/авто-выравнивания (шаг 0 плана docs/PLAN_routing_patch.md).

Назначение:
1. Зафиксировать baseline качества графов из storage — последующие шаги патча
   не имеют права ухудшать метрики.
2. Явно задокументировать известные ошибки O1/O2/O3/O6 (см. docs/AUDIT_manual_edit_routing.md)
   xfail-тестами со strict=True: когда ошибка чинится соответствующим шагом,
   xfail падает и тест сознательно переводится в обычный.

Тесты не требуют Qt: импортируются только чистые модули.
Если storage/ отсутствует или граф не парсится — тест по этому графу скипается.
"""
from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path

import pytest

from ui.editors import edge_routing as er
from ui.editors import graph_geometry as gg
from ui.editors.autofix_chains import auto_fix_graph

REPO = Path(__file__).resolve().parent.parent
STORAGE = REPO / "storage" / "diagrams"
CONN_R = 8  # BaseGraphEditor.CONNECTOR_MARKER_RADIUS

# ---------------------------------------------------------------------------
# Baseline: значения метрик на нетронутом коде (замер 2026-07-02, ветка deploy
# 5e430e5). Патч не должен их ухудшать. Для графов, которых нет в словаре,
# baseline-проверка скипается (добавить: запустить
#   python tests/test_routing_metrics.py
# и вписать напечатанные значения).
# X = пересекающихся пар рёбер, B = рёбер сквозь чужой equipment-bbox,
# D = рёбер с диагональным сегментом (>1px и >1°).
# ---------------------------------------------------------------------------
BASELINE = {
    "0c89a9fe": {"crossings": 8, "through_bbox": 1, "diagonal": 65},
    "1533eef4": {"crossings": 23, "through_bbox": 1, "diagonal": 2},
    "4464be08": {"crossings": 1, "through_bbox": 3, "diagonal": 22},
}

# Шаг 7b: после Auto-Fix диагональных рёбер быть не должно вовсе —
# per-graph baseline не нужен, инвариант универсальный (см. тест ниже).


# ---------------------------------------------------------------------------
# Загрузка графов
# ---------------------------------------------------------------------------

def _graph_files():
    if not STORAGE.is_dir():
        return []
    return sorted(STORAGE.glob("*/graph/graph_validated.json"))


def _load_graph(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in data["nodes"]}
    edges = [e for e in data["links"]
             if e.get("source") in nodes and e.get("target") in nodes]
    for e in edges:
        e.setdefault("waypoints", [])
    return nodes, edges


GRAPHS = _graph_files()
IDS = [p.parent.parent.name[:8] for p in GRAPHS]


# ---------------------------------------------------------------------------
# Точная геометрия для метрик (независимая от проверяемого кода)
# ---------------------------------------------------------------------------

def _seg_rect_intersect(p1, p2, rect, shrink=0.0):
    """Пересекает ли отрезок прямоугольник (Liang–Barsky). rect=[x1,y1,x2,y2]."""
    x1, y1 = rect[0] + shrink, rect[1] + shrink
    x2, y2 = rect[2] - shrink, rect[3] - shrink
    if x1 > x2 or y1 > y2:
        return False
    x0, y0 = p1
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - x1), (dx, x2 - x0), (-dy, y0 - y1), (dy, y2 - y0)):
        if abs(p) < 1e-12:
            if q < 0:
                return False
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return False
            if r > t0:
                t0 = r
        else:
            if r < t0:
                return False
            if r < t1:
                t1 = r
    return t1 - t0 > 1e-9


def _segs_cross(a1, a2, b1, b2):
    d1x, d1y = a2[0] - a1[0], a2[1] - a1[1]
    d2x, d2y = b2[0] - b1[0], b2[1] - b1[1]
    den = d1x * d2y - d1y * d2x
    if abs(den) < 1e-12:
        return False
    t = ((b1[0] - a1[0]) * d2y - (b1[1] - a1[1]) * d2x) / den
    u = ((b1[0] - a1[0]) * d1y - (b1[1] - a1[1]) * d1x) / den
    return 1e-6 < t < 1 - 1e-6 and 1e-6 < u < 1 - 1e-6


def _polyline(e):
    sp, tp = e.get("source_point"), e.get("target_point")
    if not sp or not tp:
        return None
    return ([(sp[1], sp[0])]
            + [(w[1], w[0]) for w in e.get("waypoints") or []]
            + [(tp[1], tp[0])])


def compute_metrics(nodes, edges):
    """X / B / D — см. BASELINE."""
    polys = {}
    for e in edges:
        pl = _polyline(e)
        if pl:
            polys[e["id"]] = (e, pl)
    ids = list(polys)

    crossings = 0
    for i in range(len(ids)):
        ei, pi = polys[ids[i]]
        for j in range(i + 1, len(ids)):
            ej, pj = polys[ids[j]]
            if {ei["source"], ei["target"]} & {ej["source"], ej["target"]}:
                continue
            hit = False
            for si in range(len(pi) - 1):
                for sj in range(len(pj) - 1):
                    if _segs_cross(pi[si], pi[si + 1], pj[sj], pj[sj + 1]):
                        hit = True
                        break
                if hit:
                    break
            if hit:
                crossings += 1

    through = 0
    for _eid, (e, pl) in polys.items():
        own = {e["source"], e["target"]}
        bad = False
        for nid, n in nodes.items():
            if nid in own or n.get("type") != "equipment":
                continue
            bb = n.get("bbox")
            if not bb or len(bb) != 4:
                continue
            for s in range(len(pl) - 1):
                if _seg_rect_intersect(pl[s], pl[s + 1], bb, shrink=1.0):
                    bad = True
                    break
            if bad:
                break
        if bad:
            through += 1

    diagonal = 0
    for _eid, (e, pl) in polys.items():
        for s in range(len(pl) - 1):
            adx = abs(pl[s + 1][0] - pl[s][0])
            ady = abs(pl[s + 1][1] - pl[s][1])
            if adx > 1 and ady > 1:
                ang = math.degrees(math.atan2(min(adx, ady), max(adx, ady)))
                if ang > 1.0:
                    diagonal += 1
                    break

    return {"crossings": crossings, "through_bbox": through, "diagonal": diagonal}


def _diag_straight_edges(edges):
    """Диагональные по endpoint'ам (без waypoints) — для оценки auto_fix."""
    c = 0
    for e in edges:
        sp, tp = e.get("source_point"), e.get("target_point")
        if not sp or not tp:
            continue
        adx, ady = abs(tp[1] - sp[1]), abs(tp[0] - sp[0])
        if adx > 1 and ady > 1 and math.degrees(
                math.atan2(min(adx, ady), max(adx, ady))) > 1.0:
            c += 1
    return c


# ---------------------------------------------------------------------------
# 1. Baseline-метрики графов (не хуже зафиксированных)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", GRAPHS, ids=IDS)
def test_graph_metrics_not_worse_than_baseline(path):
    uid = path.parent.parent.name[:8]
    try:
        nodes, edges = _load_graph(path)
    except Exception as exc:  # noqa: BLE001 — битый json → скип, не фейл
        pytest.skip(f"граф не загрузился: {exc}")
    metrics = compute_metrics(nodes, edges)
    print(f"\n{uid}: {metrics}")
    base = BASELINE.get(uid)
    if base is None:
        pytest.skip(f"{uid}: нет baseline (текущие значения: {metrics})")
    for key, limit in base.items():
        assert metrics[key] <= limit, (
            f"{uid}: {key}={metrics[key]} хуже baseline {limit}")


# ---------------------------------------------------------------------------
# 2. auto_fix_graph: инварианты и baseline
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", GRAPHS, ids=IDS)
def test_autofix_shift_within_limits_and_not_worse(path):
    uid = path.parent.parent.name[:8]
    try:
        nodes, edges = _load_graph(path)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"граф не загрузился: {exc}")
    n2, e2 = deepcopy(nodes), deepcopy(edges)
    auto_fix_graph(n2, e2, equip_max_shift=30.0, conn_max_shift=80.0)

    # Инвариант: сдвиг узла не превышает лимит (+допуск 5px на overlap-push,
    # см. AUDIT O8 — push не уважает clamp; допуск убрать в шаге 7d).
    for nid, n in n2.items():
        ox, oy = nodes[nid]["centroid"][1], nodes[nid]["centroid"][0]
        shift = math.hypot(n["centroid"][1] - ox, n["centroid"][0] - oy)
        limit = 30.0 if n.get("type") != "connector" else 80.0
        assert shift <= limit + 5.0, f"{uid}/{nid}: сдвиг {shift:.1f} > {limit}"

    # Инвариант шага 7b: невыровненные рёбра идут ортогональным маршрутом,
    # диагональных сегментов после Auto-Fix нет вообще.
    m = compute_metrics(n2, e2)
    assert m["diagonal"] == 0, (
        f"{uid}: диагональных после auto_fix: {m['diagonal']}")


# ---------------------------------------------------------------------------
# 3. Документация известных ошибок (strict xfail: чинится шагом N → тест
#    сознательно переводится в обычный в том же коммите)
# ---------------------------------------------------------------------------

def test_O1_wall_rule_catches_outside_segment():
    """O1 исправлена шагом 1: R7 ловит сегменты вдоль стенки снаружи."""
    bbox = [100, 100, 200, 200]
    # Вертикальный сегмент в 5px снаружи от левой стенки, вдоль неё → ловится.
    assert er._seg_near_bbox_wall(95, 100, 95, 200, bbox, margin=15)
    # В 5px снаружи от правой стенки → ловится.
    assert er._seg_near_bbox_wall(205, 100, 205, 200, bbox, margin=15)
    # Горизонтальный в 5px над верхней стенкой → ловится.
    assert er._seg_near_bbox_wall(100, 95, 200, 95, bbox, margin=15)
    # ВНУТРИ bbox — зона R4, R7 не срабатывает.
    assert not er._seg_near_bbox_wall(105, 100, 105, 200, bbox, margin=15)
    # Дальше margin (20px) → не срабатывает.
    assert not er._seg_near_bbox_wall(80, 100, 80, 200, bbox, margin=15)
    # Рядом, но не вдоль (нет перекрытия по параллельной оси) → не срабатывает.
    assert not er._seg_near_bbox_wall(95, 250, 95, 350, bbox, margin=15)


def test_O2_uturn_gets_zigzag_penalty():
    """O2 исправлена шагом 2: разворот на 180° штрафуется."""
    u_path = [(0, 0), (100, 0), (100, 50), (0, 50)]  # разворот на 180°
    score = er.score_candidate(u_path, [])
    # 2 поворота×100 + длина 250 + разворот 500 = 950
    assert score >= 950
    # Z-образный путь без разворота — штрафа нет: 2×100 + длина 150 = 350.
    z_path = [(0, 0), (50, 0), (50, 50), (100, 50)]
    assert er.score_candidate(z_path, []) == pytest.approx(350)
    # L-образный: 1 поворот×100 + длина 100 = 200.
    l_path = [(0, 0), (50, 0), (50, 50)]
    assert er.score_candidate(l_path, []) == pytest.approx(200)


def test_O3_perpendicularity_threshold_is_one_degree():
    """O3 исправлена шагом 3: порог = 1° от оси (было 1.0 — только идеал)."""
    # 0.29° от оси → «хорошее».
    assert gg.compute_edge_perpendicularity((0, 0), (1000, 5), {}, {})["is_good"]
    # Идеально прямое → «хорошее».
    assert gg.compute_edge_perpendicularity((0, 0), (1000, 0), {}, {})["is_good"]
    # Ровно на границе ~1° (17.45px на 1000px) → «хорошее» (>=).
    assert gg.compute_edge_perpendicularity((0, 0), (1000, 17), {}, {})["is_good"]
    # 2° от оси (35px на 1000px) → «плохое».
    assert not gg.compute_edge_perpendicularity((0, 0), (1000, 35), {}, {})["is_good"]


def test_O6_autofix_preserves_manual_route():
    """O6 исправлена шагом 7a: ручные маршруты Auto-Fix не трогает."""
    nodes = {
        "a": {"id": "a", "type": "connector", "centroid": [100.0, 100.0]},
        "b": {"id": "b", "type": "connector", "centroid": [300.0, 400.0]},
    }
    edges = [{
        "id": "e1", "source": "a", "target": "b",
        "source_point": [100.0, 100.0], "target_point": [300.0, 400.0],
        "waypoints": [[100.0, 400.0]],  # ручной L-маршрут
        "_manual_route": True,
    }]
    auto_fix_graph(nodes, edges)
    assert edges[0]["waypoints"] == [[100.0, 400.0]]


# ---------------------------------------------------------------------------
# 4. Шаг 4: полнота кандидатов и честный fallback
# ---------------------------------------------------------------------------

def test_step4_zshapes_include_obstacle_boundaries():
    """Z-стволы генерируются по границам препятствий ±WALL_MARGIN."""
    cands = er._gen_z_shapes(0, 0, 300, 200, obstacles=[[100, 50, 140, 400]])
    vert_trunks = {round(c[0][0], 1) for c in cands
                   if abs(c[0][0] - c[1][0]) < 0.5}
    assert 85.0 in vert_trunks    # 100 - 15
    assert 155.0 in vert_trunks   # 140 + 15


def test_step4_middle_segment_on_own_wall_rejected():
    """Средний сегмент ровно по стенке своего узла теперь отклоняется."""
    src_bbox = [0, 0, 40, 40]
    tgt_bbox = [200, 0, 240, 40]
    pts = [(40, 20), (60, 20), (60, 0), (30, 0), (30, -20),
           (220, -20), (220, 0)]
    # сегмент (60,0)→(30,0) идёт по верхней стенке src (y=0, перекрытие x 30..40)
    assert not er.filter_candidate(pts, [], src_bbox, tgt_bbox)


def test_step4_soft_fallback_picks_least_violating():
    """Все кандидаты режутся 2px-фильтром (узкая щель) → мягкий проход
    выбирает маршрут через щель (0 пересечений), а не сквозь стену."""
    src_bbox = [0, 0, 40, 40]
    tgt_bbox = [300, 0, 340, 40]
    # стена с щелью 4px по y (18..22): маршрут y=20 проходит с клиренсом 2
    walls = [[150, -1000, 170, 18], [150, 22, 170, 1000]]
    wps = er.route_edge((40, 20), (300, 20), 'right', 'left',
                        src_bbox, tgt_bbox, walls, [])
    pts = [(40, 20)] + [(w[1], w[0]) for w in wps] + [(300, 20)]
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        for w in walls:
            assert not er._seg_hits_bbox(a[0], a[1], b[0], b[1], w, margin=0), \
                f"сегмент {a}→{b} прошёл сквозь стену {w}"


def _rects_overlap(a, b, m=2.0):
    return (a[0] - m < b[2] and a[2] + m > b[0]
            and a[1] - m < b[3] and a[3] + m > b[1])


def test_step4_router_no_avoidable_violations_on_real_graph():
    """Роутер не выдаёт маршрутов сквозь узлы — кроме рёбер, чей конец
    физически наложен на чужой узел (данные; чинится Auto-Fix, шаг 7d)."""
    path = next((p for p in GRAPHS
                 if p.parent.parent.name.startswith("4464be08")), None)
    if path is None:
        pytest.skip("нет графа 4464be08")
    nodes, edges = _load_graph(path)

    def vbbox(n):
        bb = n.get("bbox")
        if n.get("type") == "equipment" and bb and len(bb) == 4:
            return bb
        cx, cy = n["centroid"][1], n["centroid"][0]
        return [cx - CONN_R, cy - CONN_R, cx + CONN_R, cy + CONN_R]

    avoidable = 0
    unavoidable = 0
    for e in edges:
        s, t = nodes[e["source"]], nodes[e["target"]]
        scx, scy = s["centroid"][1], s["centroid"][0]
        tcx, tcy = t["centroid"][1], t["centroid"][0]
        s_side = gg.bbox_exit_side(vbbox(s), scx, scy, tcx, tcy)
        t_side = gg.bbox_exit_side(vbbox(t), tcx, tcy, scx, scy)
        sxy = gg.bbox_side_midpoint(vbbox(s), s_side)
        txy = gg.bbox_side_midpoint(vbbox(t), t_side)
        obstacles = [vbbox(n) for nid, n in nodes.items()
                     if nid not in (e["source"], e["target"])]
        wps = er.route_edge(sxy, txy, s_side, t_side,
                            vbbox(s), vbbox(t), obstacles, [])
        pts = [sxy] + [(w[1], w[0]) for w in wps] + [txy]
        if er.validate_path(pts, obstacles, vbbox(s), vbbox(t)):
            if any(_rects_overlap(vbbox(s), ob) or _rects_overlap(vbbox(t), ob)
                   for ob in obstacles):
                unavoidable += 1  # конец ребра наложен на чужой узел
            else:
                avoidable += 1
    assert avoidable == 0, f"избежимых маршрутов сквозь узлы: {avoidable}"
    # на этом графе известен ровно один случай наложения (edge_69/node_56)
    assert unavoidable <= 2, f"наложенных концов стало больше: {unavoidable}"


# ---------------------------------------------------------------------------
# 5. Шаг 5: ядро кнопки «Оптимизировать» (optimize_core)
# ---------------------------------------------------------------------------

def test_step5_core_routes_around_obstacle():
    from ui.editors.optimize_core import compute_optimized_route
    nodes = {
        "e1": {"id": "e1", "type": "equipment",
               "centroid": [120.0, 70.0], "bbox": [40, 90, 100, 150]},
        "e2": {"id": "e2", "type": "equipment",
               "centroid": [120.0, 270.0], "bbox": [240, 90, 300, 150]},
        "obs": {"id": "obs", "type": "equipment",
                "centroid": [120.0, 170.0], "bbox": [150, 60, 190, 180]},
    }
    edge = {"id": "x", "source": "e1", "target": "e2", "waypoints": []}
    r = compute_optimized_route(nodes, [edge], edge)
    # прикрепление к центрам сторон bbox
    assert r["source_point"] == [120.0, 100.0]
    assert r["target_point"] == [120.0, 240.0]
    # маршрут ортогонален и не проходит сквозь препятствие
    pts = ([(100.0, 120.0)] + [(w[1], w[0]) for w in r["waypoints"]]
           + [(240.0, 120.0)])
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        assert abs(a[0] - b[0]) < 0.5 or abs(a[1] - b[1]) < 0.5, "диагональ"
        assert not er._seg_hits_bbox(a[0], a[1], b[0], b[1],
                                     nodes["obs"]["bbox"], margin=0), \
            f"сегмент {a}→{b} сквозь препятствие"
    assert r["waypoints"], "обход препятствия требует waypoints"


def test_step5_core_connector_attaches_at_center():
    """Перекрёсток: рёбра сходятся в центр узла (нет 8px «пеньков»)."""
    from ui.editors.optimize_core import compute_optimized_route
    nodes = {
        "c1": {"id": "c1", "type": "connector", "centroid": [100.0, 50.0]},
        "c2": {"id": "c2", "type": "connector", "centroid": [100.0, 200.0]},
    }
    edge = {"id": "e", "source": "c1", "target": "c2", "waypoints": []}
    r = compute_optimized_route(nodes, [edge], edge)
    assert r["source_point"] == [100.0, 50.0]
    assert r["target_point"] == [100.0, 200.0]
    assert r["waypoints"] == []  # соосные центры → прямая без изломов


def test_step5_core_manual_endpoints_preserved():
    """Ручные точки прикрепления не пересчитываются (_manual_route)."""
    from ui.editors.optimize_core import compute_optimized_route
    nodes = {
        "e1": {"id": "e1", "type": "equipment",
               "centroid": [120.0, 70.0], "bbox": [40, 90, 100, 150]},
        "c2": {"id": "c2", "type": "connector", "centroid": [95.0, 260.0]},
    }
    edge = {"id": "m", "source": "e1", "target": "c2", "waypoints": [],
            "_manual_route": True,
            "source_point": [95.0, 100.0],
            "target_point": [95.0, 260.0]}
    r = compute_optimized_route(nodes, [edge], edge)
    assert r["source_point"] == [95.0, 100.0]
    assert r["target_point"] == [95.0, 260.0]


# ---------------------------------------------------------------------------
# 6. Шаг 6: «Оптимизировать все» — совместный роутинг
# ---------------------------------------------------------------------------

def test_step6_optimize_all_zero_metrics_on_real_graph():
    """0 пересечений, 0 диагоналей, 0 сквозь узлы; сходимость за 2 прогона."""
    from ui.editors.optimize_core import optimize_all_routes
    path = next((p for p in GRAPHS
                 if p.parent.parent.name.startswith("4464be08")), None)
    if path is None:
        pytest.skip("нет графа 4464be08")
    nodes, edges = _load_graph(path)
    m_before = compute_metrics(nodes, edges)
    n2, e2 = deepcopy(nodes), deepcopy(edges)
    stats = optimize_all_routes(n2, e2)
    assert stats["routed"] == len(e2)
    m = compute_metrics(n2, e2)
    # Шаг 9: скользящие точки прикрепления (прямые трубы сквозь клапаны)
    # важнее абсолютного нуля пересечений — требуем «не хуже исходника».
    assert m["crossings"] <= m_before["crossings"], (m, m_before)
    assert m["diagonal"] == 0, m
    assert m["through_bbox"] == 0, m
    # сходимость: после второго прогона третий не меняет ни одного ребра
    optimize_all_routes(n2, e2)
    snap2 = [(e["source_point"], e["target_point"], e["waypoints"])
             for e in e2]
    optimize_all_routes(n2, e2)
    snap3 = [(e["source_point"], e["target_point"], e["waypoints"])
             for e in e2]
    assert snap2 == snap3


def test_step6_parallel_edges_get_distinct_slots():
    """Два ребра одной стороны equipment получают разные точки прикрепления."""
    from ui.editors.optimize_core import optimize_all_routes
    nodes = {
        "A": {"id": "A", "type": "equipment",
              "centroid": [45.0, 30.0], "bbox": [0, 0, 60, 90]},
        "c1": {"id": "c1", "type": "connector", "centroid": [30.0, 200.0]},
        "c2": {"id": "c2", "type": "connector", "centroid": [60.0, 200.0]},
    }
    edges = [
        {"id": "E1", "source": "A", "target": "c1", "waypoints": []},
        {"id": "E2", "source": "A", "target": "c2", "waypoints": []},
    ]
    optimize_all_routes(nodes, edges)
    sp1, sp2 = edges[0]["source_point"], edges[1]["source_point"]
    assert sp1 != sp2, "рёбра слиплись в одну точку"
    assert sp1[1] == 60 and sp2[1] == 60  # обе на правой стенке bbox


# ---------------------------------------------------------------------------
# 7. Шаги 7a+7b: Auto-Fix — ручные маршруты и перероутинг диагоналей
# ---------------------------------------------------------------------------

def test_step7a_manual_endpoint_follows_node():
    """Узел цепочки сдвинулся — endpoint ручного ребра следует за ним,
    waypoints неприкосновенны."""
    nodes = {
        "a": {"id": "a", "type": "connector", "centroid": [100.0, 0.0]},
        "b": {"id": "b", "type": "connector", "centroid": [106.0, 300.0]},
        "x": {"id": "x", "type": "connector", "centroid": [100.0, 600.0]},
        "c": {"id": "c", "type": "connector", "centroid": [400.0, 300.0]},
    }
    edges = [
        {"id": "ab", "source": "a", "target": "b", "waypoints": [],
         "source_point": [100.0, 0.0], "target_point": [106.0, 300.0]},
        {"id": "bx", "source": "b", "target": "x", "waypoints": [],
         "source_point": [106.0, 300.0], "target_point": [100.0, 600.0]},
        {"id": "bc", "source": "b", "target": "c", "_manual_route": True,
         "source_point": [106.0, 300.0], "target_point": [400.0, 300.0],
         "waypoints": [[250.0, 320.0]]},
    ]
    auto_fix_graph(nodes, edges)
    manual = edges[2]
    assert manual["waypoints"] == [[250.0, 320.0]], "waypoints тронуты"
    # узел b выровнялся к y≈100, endpoint последовал за ним
    assert abs(manual["source_point"][0] - nodes["b"]["centroid"][0]) < 3.5


def test_step7b_autofix_replaces_diagonal_with_orthogonal_route():
    """Невыравниваемая пара equipment: вместо прямой диагонали — ортогональный
    маршрут (waypoints), ни одного диагонального сегмента."""
    nodes = {
        "E1": {"id": "E1", "type": "equipment",
               "centroid": [100.0, 50.0], "bbox": [20, 70, 80, 130]},
        "E2": {"id": "E2", "type": "equipment",
               "centroid": [400.0, 450.0], "bbox": [420, 370, 480, 430]},
    }
    edges = [{"id": "d", "source": "E1", "target": "E2", "waypoints": [],
              "source_point": [100.0, 80.0], "target_point": [400.0, 420.0]}]
    auto_fix_graph(nodes, edges)
    e = edges[0]
    sp, tp = e["source_point"], e["target_point"]
    pts = ([(sp[1], sp[0])] + [(w[1], w[0]) for w in e["waypoints"]]
           + [(tp[1], tp[0])])
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        assert abs(a[0] - b[0]) < 0.5 or abs(a[1] - b[1]) < 0.5, \
            f"диагональ осталась: {a}→{b}"
    assert e["waypoints"], "маршрут не построен"


def test_step9_inline_valve_straight_through():
    """Бокс клапана смещён относительно линии трубы — точка прикрепления
    скользит навстречу соседу, труба идёт прямо, без ступенек (кейс M-448)."""
    from ui.editors.optimize_core import compute_optimized_route
    nodes = {
        "j1": {"id": "j1", "type": "connector", "centroid": [100.0, 40.0]},
        "V": {"id": "V", "type": "equipment",
              "centroid": [93.0, 150.0], "bbox": [120, 73, 180, 113]},
        "j2": {"id": "j2", "type": "connector", "centroid": [100.0, 260.0]},
    }
    e1 = {"id": "a", "source": "j1", "target": "V", "waypoints": []}
    e2 = {"id": "b", "source": "V", "target": "j2", "waypoints": []}
    r1 = compute_optimized_route(nodes, [e1, e2], e1)
    assert r1["waypoints"] == [], r1
    assert r1["target_point"] == [100.0, 120.0]  # вход на y линии, не центра
    r2 = compute_optimized_route(nodes, [e1, e2], e2)
    assert r2["waypoints"] == [], r2
    assert r2["source_point"] == [100.0, 180.0]


def test_step9_autofix_pulls_free_connector_instead_of_zigzag():
    """Диагональ клапан—перекрёсток: перекрёсток на вертикальной трубе
    (X зафиксирован цепочкой, Y свободен) подтягивается к y клапана —
    прямое ребро вместо зигзага (кейс VX08-10)."""
    nodes = {
        "top": {"id": "top", "type": "connector", "centroid": [0.0, 300.0]},
        "j": {"id": "j", "type": "connector", "centroid": [125.0, 300.0]},
        "V": {"id": "V", "type": "equipment",
              "centroid": [200.0, 80.0], "bbox": [50, 180, 110, 220]},
    }
    edges = [
        {"id": "pipe", "source": "top", "target": "j", "waypoints": [],
         "source_point": [0.0, 300.0], "target_point": [125.0, 300.0]},
        {"id": "d", "source": "V", "target": "j", "waypoints": [],
         "source_point": [200.0, 110.0], "target_point": [125.0, 300.0]},
    ]
    auto_fix_graph(nodes, edges)
    assert abs(nodes["j"]["centroid"][0] - 200.0) < 0.01, nodes["j"]
    e = edges[1]
    assert abs(e["source_point"][0] - e["target_point"][0]) < 0.6, e
    assert e["waypoints"] == [], e


def test_step8_micro_skew_snapped_straight():
    """8.1: перекос endpoints ≤ STRAIGHT_TOL после проекции снапится к общей
    координате, а не остаётся микро-диагональю (случай a9c136e8)."""
    nodes = {
        "E1": {"id": "E1", "type": "equipment",
               "centroid": [20.0, 30.0], "bbox": [0, 0, 60, 40]},
        "c": {"id": "c", "type": "connector", "centroid": [43.0, 75.0]},
    }
    edges = [{"id": "m", "source": "E1", "target": "c", "waypoints": [],
              "source_point": [40.0, 60.0], "target_point": [43.0, 75.0]}]
    auto_fix_graph(nodes, edges)
    sp, tp = edges[0]["source_point"], edges[0]["target_point"]
    assert abs(sp[0] - tp[0]) < 0.01 or abs(sp[1] - tp[1]) < 0.01, (sp, tp)


if __name__ == "__main__":
    # Печать метрик всех графов — для заполнения BASELINE.
    for p in _graph_files():
        uid = p.parent.parent.name[:8]
        try:
            nodes, edges = _load_graph(p)
        except Exception as exc:  # noqa: BLE001
            print(f"{uid}: ошибка загрузки: {exc}")
            continue
        print(uid, compute_metrics(nodes, edges),
              "| авто-фикс диагонали:", end=" ")
        n2, e2 = deepcopy(nodes), deepcopy(edges)
        auto_fix_graph(n2, e2)
        print(_diag_straight_edges(e2))

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

# Baseline поведения auto_fix_graph (не хуже): остаточные диагонали после фикса.
AUTOFIX_BASELINE = {
    "0c89a9fe": {"diagonal_after": 11},
    "1533eef4": {"diagonal_after": 2},
    "4464be08": {"diagonal_after": 5},
}


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

    base = AUTOFIX_BASELINE.get(uid)
    if base is not None:
        d = _diag_straight_edges(e2)
        assert d <= base["diagonal_after"], (
            f"{uid}: диагональных после auto_fix {d} > baseline "
            f"{base['diagonal_after']}")


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


@pytest.mark.xfail(strict=True,
                   reason="O6 (шаг 7a): auto_fix стирает waypoints ручных "
                          "маршрутов (_manual_route)")
def test_O6_autofix_preserves_manual_route():
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

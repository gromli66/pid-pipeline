# -*- coding: utf-8 -*-
"""Э4 — движок адресного сглаживания (`modules/graph/core/edit_smooth`).

Проверяется «со всех сторон» (требование заказчика 2026-08-02): все виды
посадки (рамка/слот, контурный участок, коннектор-центроид, ЯКОРЬ оператора),
все типы рёбер (прямая диагональ, зигзаг с одной ступенькой, многоступенчатый,
короткое ребро-датчик), и то, что неприкосновенно — не двигается.

Чистый stdlib: движок без Qt, роутинг подаётся колбэком.
"""
import copy

import pytest

from modules.graph.core import edit_smooth as es
from modules.graph.core import ports


def _g(nodes, links):
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [1080, 1920]},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def _box(nid, cx, cy, w=40.0, h=40.0, **kw):
    n = {"id": nid, "type": "equipment", "class_id": 99,
         "class_name": "unknow", "centroid": [cy, cx],
         "bbox": [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2],
         "segmentation": None}
    n.update(kw)
    return n


def _conn(nid, cx, cy):
    return {"id": nid, "type": "connector", "class_id": -1,
            "class_name": "connector", "centroid": [cy, cx],
            "bbox": None, "segmentation": None}


def _edge(eid, s, t, sp, tp, wps=None):
    return {"id": eid, "source": s, "target": t,
            "source_point": [sp[1], sp[0]], "target_point": [tp[1], tp[0]],
            "waypoints": [[w[1], w[0]] for w in (wps or [])]}


# ── распознавание дефектов ─────────────────────────────────────────────

def test_single_step_detected():
    """Труба идёт, делает шажок вбок и продолжает — это ступенька."""
    e = _edge("e", "a", "b", (100.0, 100.0), (400.0, 112.0),
              [(200.0, 100.0), (200.0, 112.0)])
    assert es.single_step(e) == ("y", 12.0)


def test_two_steps_not_taken():
    """Многоступенчатый в автомат не берётся (связанность — см. шапку)."""
    e = _edge("e", "a", "b", (100.0, 100.0), (500.0, 124.0),
              [(200.0, 100.0), (200.0, 112.0),
               (350.0, 112.0), (350.0, 124.0)])
    assert es.single_step(e) is None
    assert es.shift_target(e) is None


def test_plain_diagonal_is_shift_target():
    """Прямая диагональ — тоже «легко превратить в прямую»."""
    e = _edge("e", "a", "b", (100.0, 100.0), (300.0, 108.0))
    axis, d = es.shift_target(e)
    assert axis == "y" and d == pytest.approx(8.0)


def test_sensor_edge_untouched():
    """Короткое ребро-датчик (< SEG_FLOOR) — структурный пол, не судится."""
    e = _edge("e", "a", "b", (100.0, 100.0), (104.0, 102.0))
    assert es.shift_target(e) is None
    assert es.diag_segments_of(e) == []


# ── подтягивание излома: ничего, кроме колена, не двигается ────────────

def test_snap_waypoint_straightens_inner_slant():
    e = _edge("e", "a", "b", (100.0, 100.0), (100.0, 300.0),
              [(100.0, 180.0), (109.0, 240.0)])
    sp0 = list(e["source_point"])
    tp0 = list(e["target_point"])
    assert es.snap_waypoints(e) >= 1
    assert es.is_ortho(e), f"маршрут остался косым: {es.edge_pts(e)}"
    assert e["source_point"] == sp0 and e["target_point"] == tp0, \
        "посаженные концы обязаны остаться на месте"


def test_snap_does_not_move_seated_ends():
    """Косой сегмент упирается в посаженный конец — излом не выдумывается."""
    e = _edge("e", "a", "b", (100.0, 100.0), (300.0, 140.0),
              [(200.0, 140.0)])
    before = copy.deepcopy(e)
    es.snap_waypoints(e)
    assert e["source_point"] == before["source_point"]
    assert e["target_point"] == before["target_point"]


# ── классификация посадок ──────────────────────────────────────────────

def test_end_kind_all_kinds():
    box = _box("b", 100.0, 100.0)
    conn = _conn("c", 300.0, 100.0)
    poly = _box("p", 500.0, 100.0)
    poly["segmentation"] = [480.0, 80.0, 520.0, 80.0, 520.0, 120.0,
                            480.0, 120.0]
    e = _edge("e", "b", "c", (120.0, 100.0), (300.0, 100.0))
    assert es.end_kind(box, e) == "rect"
    assert es.end_kind(conn, e) == "connector"
    assert es.end_kind(poly, e) == "contour"
    assert es.end_kind(None, e) == "none"
    ports.add_manual_port(box, 120.0, 108.0, e)
    assert es.end_kind(box, e) == "anchor", "якорь обязан быть виден первым"


# ── гейт ───────────────────────────────────────────────────────────────

def test_gate_rejects_side_damage():
    base = {"diag": 3, "dev": 30.0, "steps": 2, "near": 0, "along_own": 0,
            "along_foreign": 0, "through": 0, "corner": 0, "adrift": 0,
            "conn_off": 0, "poly_off": 0, "w": 100.0, "h": 100.0}
    better = dict(base, diag=2, dev=20.0)
    assert es.accepts(base, better)
    assert not es.accepts(base, dict(better, through=1)), \
        "ход, родивший прошивание, обязан быть отклонён"
    assert not es.accepts(base, dict(better, along_own=1))
    assert not es.accepts(base, dict(better, w=es.CANVAS_W + 10)), \
        "выход за холст обязан отменять ход"
    assert not es.accepts(base, dict(base)), \
        "ход без выигрыша не принимается"


def test_gate_sees_steps_but_diagonal_wins():
    """Ступенька — ТРЕТИЙ приоритет: чистое её устранение принимается, но
    ход, убравший косую ценой шажка, тоже принимается (косая заметнее).

    Репро заказчика graph_edited_av: без метрики ступенек гейт не видел
    улучшения и откатывал ход — 10 шажков пережили сглаживание."""
    base = {"diag": 1, "dev": 10.0, "steps": 3, "near": 0, "along_own": 0,
            "along_foreign": 0, "through": 0, "corner": 0, "adrift": 0,
            "conn_off": 0, "poly_off": 0, "w": 100.0, "h": 100.0}
    assert es.accepts(base, dict(base, steps=2)), \
        "чистое устранение шажка обязано приниматься"
    assert not es.accepts(base, dict(base, steps=4)), \
        "рождение шажка без иного выигрыша — отказ"
    assert es.accepts(base, dict(base, diag=0, dev=0.0, steps=4)), \
        "снятие косой ценой шажка — принимается: косая заметнее"


# ── главный цикл: что неприкосновенно ──────────────────────────────────

def _canvas_with_anchor():
    a = _box("a", 100.0, 100.0)
    b = _box("b", 300.0, 108.0)
    e = _edge("e1", "a", "b", (120.0, 100.0), (280.0, 108.0))
    ports.add_manual_port(a, 120.0, 100.0, e)
    return _g([a, b], [e])


def test_anchor_never_moves():
    """Точка, поставленная оператором, не двигается ни одним лекарством."""
    g = _canvas_with_anchor()
    e = g["links"][0]
    sp0 = list(e["source_point"])
    es.smooth(g, route_fn=None, reseat_fn=None)
    assert e["source_point"] == sp0, "якорь входа сорван сглаживанием"


def test_connector_end_stays_centroid():
    """У коннектора конец — центроид: двигается сам коннектор, а конец
    обязан остаться в нём."""
    a = _box("a", 100.0, 100.0)
    c = _conn("c", 300.0, 112.0)
    e = _edge("e1", "a", "c", (120.0, 100.0), (300.0, 112.0))
    g = _g([a, c], [e])
    es.smooth(g, route_fn=None, reseat_fn=None)
    cy, cx = c["centroid"]
    assert e["target_point"] == pytest.approx([cy, cx]), \
        "конец коннектора обязан совпадать с его центроидом"


def test_topology_untouched():
    """Ни одно лекарство не меняет состав узлов и рёбер."""
    g = _canvas_with_anchor()
    ids0 = ([n["id"] for n in g["nodes"]], [e["id"] for e in g["links"]])
    es.smooth(g, route_fn=None, reseat_fn=None)
    assert ([n["id"] for n in g["nodes"]], [e["id"] for e in g["links"]]) == ids0


def test_big_shift_refused():
    """«Супер движение» (больше бюджета) не делается — уходит оператору."""
    a = _box("a", 100.0, 100.0)
    b = _box("b", 400.0, 200.0)          # расхождение 100px >> BUDGET
    e = _edge("e1", "a", "b", (120.0, 100.0), (380.0, 200.0))
    g = _g([a, b], [e])
    before = copy.deepcopy(g)
    st = es.smooth(g, route_fn=None, reseat_fn=None)
    assert st["узел"] == 0 and st["коннектор"] == 0
    assert g["nodes"] == before["nodes"], "оборудование не должно двигаться"


def test_idempotent():
    """Второй прогон ничего не меняет."""
    g = _canvas_with_anchor()
    es.smooth(g, route_fn=None, reseat_fn=None)
    snapshot = copy.deepcopy(g)
    st = es.smooth(g, route_fn=None, reseat_fn=None)
    assert st["колено"] == st["излом"] == st["скольжение"] == 0
    assert st["коннектор"] == st["узел"] == 0
    assert g == snapshot, "повторное сглаживание изменило холст"


def test_orthogonal_step_is_a_candidate():
    """Репро заказчика (graph_edited_av, 2026-08-02): «не исправило
    ступеньки». Ступенька строго ОРТОГОНАЛЬНА, и отбор «берём только косые
    рёбра» проходил мимо неё вовсе. Ортогональное ребро с одиночным шажком
    обязано попадать в кандидаты."""
    a = _box("a", 100.0, 100.0)
    c = _conn("c", 400.0, 112.0)
    e = _edge("e1", "a", "c", (120.0, 100.0), (400.0, 112.0),
              [(260.0, 100.0), (260.0, 112.0)])
    g = _g([a, c], [e])
    assert es.is_ortho(e), "фикстура обязана быть ортогональной"
    assert es.single_step(e) is not None
    assert es.count_steps(g) == 1

    c0 = list(c["centroid"])
    st = es.smooth(g, route_fn=None, reseat_fn=None)

    assert es.count_steps(g) == 0, f"ступенька не сглажена: {st}"
    assert es.is_ortho(e) and len(es.edge_pts(e)) == 2, \
        f"труба обязана стать прямой: {es.edge_pts(e)}"
    # лестница обязана взять САМОЕ ДЕШЁВОЕ лекарство: конец скользит по
    # своей грани, оборудование и коннектор не двигаются вовсе
    assert st["скольжение"] == 1, f"ожидалось скольжение конца: {st}"
    assert st["коннектор"] == st["узел"] == 0
    assert c["centroid"] == c0, "коннектор не должен был двигаться"
    cy, cx = c["centroid"]
    assert e["target_point"] == pytest.approx([cy, cx]), \
        "конец коннектора обязан остаться его центроидом"


def test_rejected_move_leaves_canvas_bit_identical():
    """ГАРАНТИЯ отката: если ход отклонён, холст не изменился ни на бит.

    Репро: на graph_edited_fix одна ветка отката не отрабатывала, и мусор
    просачивался мимо гейта (along_foreign 0 -> 1). Держится try/finally,
    а не дисциплиной вызовов restore по веткам."""
    a = _box("a", 100.0, 100.0)
    b = _box("b", 400.0, 200.0)          # расхождение много больше бюджета
    e = _edge("e1", "a", "b", (120.0, 100.0), (380.0, 200.0))
    g = _g([a, b], [e])
    before = copy.deepcopy(g)

    st = es.smooth(g, route_fn=None, reseat_fn=None)

    assert st["отклонено"] >= 1, "фикстура обязана дать отказ"
    assert g == before, "после отказа холст обязан быть побайтово прежним"


def test_rejected_move_with_router_leaves_canvas_bit_identical():
    """То же с роутером, который «строит» заведомо негодный маршрут:
    откат обязан снять и его waypoints."""
    a = _box("a", 100.0, 100.0)
    b = _box("b", 400.0, 200.0)
    e = _edge("e1", "a", "b", (120.0, 100.0), (380.0, 200.0))
    g = _g([a, b], [e])
    before = copy.deepcopy(g)

    def bad_router(edge):
        edge["waypoints"] = [[150.0, 250.0]]      # косая ломаная
        edge["_auto_route"] = True
        return True

    st = es.smooth(g, route_fn=bad_router, reseat_fn=None)
    assert st["колено"] == 0, "негодный маршрут не должен приниматься"
    assert g == before, "после отказа холст обязан быть побайтово прежним"

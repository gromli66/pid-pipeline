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
    ports.set_edge_pin(box, e, "source", 120.0, 108.0)
    assert es.end_kind(box, e) == "anchor", "пин обязан быть виден первым"


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


def test_gate_bends_fourth_priority():
    """Блок 6, «а″»: изломы — ЧЕТВЁРТЫЙ приоритет. При равных косых,
    отклонении и ступеньках ход, снявший излом, принимается, добавивший —
    нет; ступенька старше изломов; словари без ключа — прежнее поведение."""
    base = {"diag": 1, "dev": 10.0, "steps": 2, "bends": 10, "near": 0,
            "along_own": 0, "along_foreign": 0, "through": 0, "corner": 0,
            "adrift": 0, "conn_off": 0, "poly_off": 0, "overlap": 0,
            "offaxis": 0, "w": 100.0, "h": 100.0}
    assert es.accepts(base, dict(base, bends=9)), \
        "снятие излома при прочих равных обязано приниматься"
    assert not es.accepts(base, dict(base, bends=11)), \
        "рождение излома без иного выигрыша — отказ"
    assert not es.accepts(base, dict(base, bends=10)), "без выигрыша — отказ"
    assert not es.accepts(base, dict(base, steps=3, bends=0)), \
        "ступенька старше изломов"
    assert es.accepts(base, dict(base, steps=1, bends=12)), \
        "снятие ступеньки ценой изломов — принимается (3-й приоритет)"
    assert not es.accepts(base, dict(base, bends=9, overlap=1)), \
        "наслоение узлов — в «не хуже»"
    assert not es.accepts(base, dict(base, bends=9, offaxis=1)), \
        "неосевой вход клапана — в «не хуже»"
    old = {k: v for k, v in base.items()
           if k not in ("bends", "overlap", "offaxis")}
    assert not es.accepts(old, dict(old)), \
        "словари без изломов — прежнее поведение (отказ без выигрыша)"
    assert es.accepts(old, dict(old, steps=1))


def test_count_overlaps_threshold_both_sides():
    """Наслоение — пересечение по обеим осям строго > 0 (как `box_hits`):
    касание — 0, заход на 0.01 px — 1; коннектор без рамки не считается."""
    a = _box("a", 100.0, 100.0)                    # [80, 80, 120, 120]
    touch = _box("b", 140.0, 100.0)                # x1 = 120 — касание
    g = _g([a, touch, _conn("c", 100.0, 100.0)], [])
    assert es.count_overlaps(g) == 0
    touch["bbox"][0] -= 0.01
    assert es.count_overlaps(g) == 1
    far = _box("d", 90.0, 140.0, w=20.0)           # y1 = 120 — касание
    g["nodes"].append(far)
    assert es.count_overlaps(g) == 1
    far["bbox"][1] = 119.99
    assert es.count_overlaps(g) == 2
    assert es.score(g)["overlap"] == 2


def _valve(nid, bbox, axis="H", cls="armatura_electro", **kw):
    x1, y1, x2, y2 = bbox
    n = {"id": nid, "type": "equipment", "class_id": 7, "class_name": cls,
         "centroid": [(y1 + y2) / 2, (x1 + x2) / 2], "bbox": list(bbox),
         "segmentation": None, "_axis": axis}
    n.update(kw)
    return n


def _offaxis_canvas(bottom=True):
    """H-клапан степени 2: ребро A в правой грани, ребро B — в нижней
    (bottom=True) или в левой грани (False)."""
    v = _valve("v", (400.0, 300.0, 440.0, 340.0))
    r = _box("r", 600.0, 320.0)
    q = _box("q", 300.0, 500.0)
    a = _edge("A", "v", "r", (440.0, 320.0), (580.0, 320.0))
    b = (_edge("B", "v", "q", (420.0, 340.0), (300.0, 480.0),
               [(420.0, 480.0)]) if bottom else
         _edge("B", "v", "q", (400.0, 320.0), (300.0, 480.0),
               [(300.0, 320.0)]))
    return _g([v, r, q], [a, b])


def test_offaxis_ends_rule_of_block5():
    """Неосевой конец клапана степени 1–2 считается; тот же конец — нет у
    степени 3, RELIEF (predohran), пина на конце и узла старого холста."""
    from modules.graph.core import contact

    assert es.offaxis_ends(_offaxis_canvas(bottom=False)) == []
    g = _offaxis_canvas()
    assert es.offaxis_ends(g) == [("v", "B")]
    assert es.score(g)["offaxis"] == 1
    # степень 3 — правила нет
    g3 = _offaxis_canvas()
    g3["nodes"].append(_box("t", 420.0, 200.0))
    g3["links"].append(_edge("C", "v", "t", (420.0, 300.0), (420.0, 220.0)))
    assert es.offaxis_ends(g3) == []
    # RELIEF_VLV — исключение Р5
    gr = _offaxis_canvas()
    gr["nodes"][0]["class_name"] = "predohran"
    assert es.offaxis_ends(gr) == []
    # старый холст (метки модели нет) — без правила
    gl = _offaxis_canvas()
    gl["nodes"][0][contact.LEGACY_KEY] = True
    assert es.offaxis_ends(gl) == []
    # пин оператора на неосевом конце — истина
    gp = _offaxis_canvas()
    ports.set_edge_pin(gp["nodes"][0], gp["links"][1], "source", 420.0, 340.0)
    assert es.offaxis_ends(gp) == []


def test_node_shift_refused_into_neighbour():
    """«Не наезжать на соседей»: ступенька 10 px между рамками закрывается
    только сдвигом узла, а оба сдвига (a вниз, b вверх) заходят на соседа —
    ход отклонён, холст бит-в-бит. Без соседей тот же холст лечится
    сдвигом узла."""
    def canvas(neighbours):
        a = _box("a", 100.0, 140.0, w=30.0, h=30.0)      # [85,125,115,155]
        b = _box("b", 620.0, 150.0)                      # [600,130,640,170]
        e = _edge("e1", "a", "b", (115.0, 140.0), (620.0, 150.0),
                  [(200.0, 140.0), (200.0, 150.0)])
        nodes = [a, b]
        if neighbours:
            nodes += [_box("n", 100.0, 173.0, w=30.0, h=34.0),   # y1 = 156
                      _box("m", 620.0, 107.5, h=35.0)]           # y2 = 125
        return _g(nodes, [e])

    free = canvas(False)
    st = es.smooth(free, route_fn=None, reseat_fn=None)
    assert st["узел"] == 1 and es.count_steps(free) == 0, st

    g = canvas(True)
    assert es.count_overlaps(g) == 0
    before = copy.deepcopy(g)
    st = es.smooth(g, route_fn=None, reseat_fn=None)
    assert st["узел"] == 0 and st["лестница исчерпана"] == 1, st
    assert g == before, "отклонённый ход обязан оставить холст бит-в-бит"


def test_count_bends():
    """Излом — точка, где маршрут меняет направление; коллинеарная точка
    в пределах DIAG_TOL (0.4 px поперёк) изломом не считается — тот же
    предикат, что у `drop_collinear`. Стенд зовёт движок (блок 6, шаг 1)."""
    from tools import smooth_bench as sb

    z = _g([_conn("a", 100.0, 100.0), _conn("b", 300.0, 150.0)],
           [_edge("e", "a", "b", (100.0, 100.0), (300.0, 150.0),
                  [(200.0, 100.0), (200.0, 150.0)])])
    assert es.count_bends(z) == (2, 1)
    line = _g([_conn("a", 100.0, 100.0), _conn("b", 300.0, 100.0)],
              [_edge("e", "a", "b", (100.0, 100.0), (300.0, 100.0),
                     [(200.0, 100.4)])])
    assert es.count_bends(line) == (0, 1)
    for g in (z, line):
        assert sb.count_bends(g) == es.count_bends(g)


# ── главный цикл: что неприкосновенно ──────────────────────────────────

def _canvas_with_anchor():
    a = _box("a", 100.0, 100.0)
    b = _box("b", 300.0, 108.0)
    e = _edge("e1", "a", "b", (120.0, 100.0), (280.0, 108.0))
    ports.set_edge_pin(a, e, "source", 120.0, 100.0)
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

    sp0 = list(e["source_point"])
    st = es.smooth(g, route_fn=None, reseat_fn=None)

    assert es.count_steps(g) == 0, f"ступенька не сглажена: {st}"
    assert es.is_ortho(e) and len(es.edge_pts(e)) == 2, \
        f"труба обязана стать прямой: {es.edge_pts(e)}"
    # ПЕРЕОБЪЯВЛЕНО 2026-09-22 (модель точек прикрепления): рамочный конец
    # не скользит по грани — он в опорной точке; ступеньку закрывает
    # следующее лекарство, сдвиг КОННЕКТОРА, а вход остаётся на месте
    assert st["коннектор"] == 1, f"ожидался сдвиг коннектора: {st}"
    assert st["скольжение"] == st["узел"] == 0
    assert e["source_point"] == pytest.approx(sp0), \
        "вход на рамке обязан остаться в опорной точке"
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


# ── откат пробы: снимок обязан быть ПРИВАТНЫМ (пункт 1.1 дороги) ───────
#
# Предыдущий блок закрывает откат РЕБРА целиком (внешний try/finally в
# `_try_remedies`). Здесь — откат ОДНОЙ ПРОБЫ внутри лестницы: `restore()`
# клал в живое ребро тот же самый список `waypoints`, что лежит в снимке
# (`cur.update(old)` копирует dict поверхностно). Дальше `drop_collinear`
# делал по этому списку `pop()`, снимок укорачивался НАВСЕГДА, и откат
# следующей пробы возвращал уже испорченное состояние.
#
# Взводится это только на БОЕВОМ пути: `restore()` в ветке КОЛЕНА зовётся
# безусловно, как только движку передан роутер, — а редактор передаёт его
# всегда (`advanced_graph_editor.smooth_canvas`). Поэтому фикстуры ниже
# отличаются от соседних ровно одним: роутером, который ничего не сделал.


def _declining_router(_edge):
    """Роутер, который не смог построить колено и НЕ изменил ребро.

    Штатный исход для большинства рёбер: `smooth_canvas` отдаёт движку
    лестницу маршрутов редактора, и та возвращает False, когда колено не
    строится. Сам по себе такой ответ не имеет права менять ничего."""
    return False


def _canvas_jog_and_step():
    """Труба рамка -> коннектор: излом 2px в начале и ступенька 10px в конце.

    Точки (x, y): (115,140) (200,140) (200,142) (400,140) (400,150) (620,150).
    """
    a = _box("a", 100.0, 140.0, w=30.0, h=30.0)
    b = _conn("b", 620.0, 150.0)
    e = _edge("e1", "a", "b", (115.0, 140.0), (620.0, 150.0),
              [(200.0, 140.0), (200.0, 142.0), (400.0, 140.0), (400.0, 150.0)])
    return _g([a, b], [e])


def _canvas_single_step():
    """Та же труба с одной ступенькой 10px и без излома.

    Точки (x, y): (115,140) (200,140) (200,150) (620,150).
    """
    a = _box("a", 100.0, 140.0, w=30.0, h=30.0)
    b = _conn("b", 620.0, 150.0)
    e = _edge("e1", "a", "b", (115.0, 140.0), (620.0, 150.0),
              [(200.0, 140.0), (200.0, 150.0)])
    return _g([a, b], [e])


def test_declining_router_does_not_crash_the_ladder():
    """Лестница падала IndexError, если роутер просто вернул False.

    `shift_run` получает индекс шажка, снятый ДО мутаций, и обращается по
    нему к `waypoints`; укоротившийся через алиас снимок делает индекс
    недействительным. Здесь труба обязана выпрямиться, а не упасть."""
    g = _canvas_jog_and_step()
    e = g["links"][0]
    a, b = g["nodes"]

    st = es.smooth(g, route_fn=_declining_router, reseat_fn=None)

    assert st["излом"] == 1 and st["коннектор"] == 1, \
        f"взяты не те лекарства: {st}"
    assert st["колено"] == st["скольжение"] == st["узел"] == 0, \
        f"лишние ходы: {st}"
    assert e["source_point"] == [140.0, 115.0]
    assert e["target_point"] == [140.0, 620.0]
    assert e["waypoints"] == [], f"труба не стала прямой: {e['waypoints']}"
    assert a["centroid"] == [140.0, 100.0] and \
        a["bbox"] == [85.0, 125.0, 115.0, 155.0], "оборудование не двигалось"
    assert b["centroid"] == [140.0, 620.0], "коннектор обязан переехать на 10px"


def test_declining_router_does_not_promote_node_shift():
    """Тихая половина дефекта: без падения, но лестница проскакивала
    дешёвые лекарства и доезжала до СДВИГА ОБОРУДОВАНИЯ.

    Ступенька 10px на конце-коннекторе закрывается лекарством 3 (двигается
    точка). После испорченного отката пробы её брало лекарство 4 — рамка
    уезжала на 10px вниз, чего заказчик прямо не разрешал (шапка модуля:
    сдвиг узла — последнее средство)."""
    g = _canvas_single_step()
    e = g["links"][0]
    a, b = g["nodes"]

    st = es.smooth(g, route_fn=_declining_router, reseat_fn=None)

    assert st["коннектор"] == 1, f"ступеньку закрыл не коннектор: {st}"
    assert st["узел"] == 0, f"оборудование двигать не требовалось: {st}"
    assert a["centroid"] == [140.0, 100.0], "рамка уехала"
    assert a["bbox"] == [85.0, 125.0, 115.0, 155.0], "рамка уехала"
    assert b["centroid"] == [140.0, 620.0]
    assert e["source_point"] == [140.0, 115.0]
    assert e["target_point"] == [140.0, 620.0]
    assert e["waypoints"] == []


@pytest.mark.parametrize("canvas", [_canvas_jog_and_step, _canvas_single_step])
def test_declining_router_changes_nothing(canvas):
    """Инвариант: роутер, который ничего не изменил и вернул False, не имеет
    права изменить исход — иначе откат делит память с живым холстом."""
    g_none, g_router = canvas(), canvas()

    st_none = es.smooth(g_none, route_fn=None, reseat_fn=None)
    st_router = es.smooth(g_router, route_fn=_declining_router, reseat_fn=None)

    assert st_router == st_none, "роутер-отказ изменил выбор лекарств"
    assert g_router == g_none, "роутер-отказ изменил холст"


# ── И2: роутер не зовётся поверх уже прямой трубы (блок 4.2) ───────────
#
# Решение Максима №5 (`pains-manual-edit-fxml`, блок 4) — вторая
# «бесплатная правка» решения №16. Вызовов `route_fn` в лестнице ЧЕТЫРЕ:
# колено и по одному после каждого сдвига (скольжение / коннектор / узел).
# После сдвига `drop_collinear` уже выпрямил трубу, и маршрут, построенный
# поверх прямой, в лучшем случае стоит лишней libavoid-сессии, а в худшем
# отменяет ход, который гейт принял бы.


class _SpyRouter:
    """Роутер-наблюдатель: считает вызовы и отдельно — вызовы на ПРЯМОЙ.

    Возвращает False («колено не построилось») и ничего не меняет —
    штатный ответ лестницы редактора для большинства рёбер.
    """

    def __init__(self):
        self.calls = 0
        self.over_straight = 0

    def __call__(self, edge):
        self.calls += 1
        if es.is_straight(edge):
            self.over_straight += 1
        return False


class _JoggingRouter:
    """Роутер, который на ПРЯМОЙ трубе строит лишнее колено вбок.

    Модель боевой лестницы в худшем: она строит маршрут ЗАНОВО (libavoid) и
    не обязана повторить прямую. Поверх выпрямленного хода это отменяет сам
    ход — гейт видит две новые косые и откатывает всё лекарство.
    """

    def __call__(self, edge):
        pts = es.edge_pts(edge)
        if not pts or len(pts) != 2:
            return False
        (x0, y0), (x1, y1) = pts
        edge["waypoints"] = [[y0 + 40.0, (x0 + x1) / 2.0]]
        return True


def _canvas_step_slides():
    """Ступенька 12px, которую закрывает СКОЛЬЖЕНИЕ конца по прямому участку
    КОНТУРА (лекарство 2): оборудование и коннектор не двигаются.

    ПЕРЕОБЪЯВЛЕНА 2026-09-22 (модель точек прикрепления): рамочный конец
    больше не скользит — он живёт в опорной точке грани (выверенный порт
    клапана или середина с допуском), — поэтому площадка проверяется
    контурным узлом, где скольжение по участку осталось.

    ПЕРЕОБЪЯВЛЕНА 2026-09-30 (блок 6, «б»): партнёр — бокс unknow вместо
    коннектора. Проба «б» (коннектор на линию трубы) стоит в лестнице
    РАНЬШЕ скольжения и при равном гейте остаётся она — коннектор закрывал
    бы ступеньку первым, и площадка 2 не проверялась бы. Бокс «б» не
    двигает, сдвиг узла стоит позже скольжения."""
    a = _box("a", 100.0, 100.0,
             segmentation=[80.0, 80.0, 120.0, 80.0, 120.0, 120.0,
                           80.0, 120.0])
    c = _box("c", 400.0, 112.0)                          # [380,92,420,132]
    e = _edge("e1", "a", "c", (120.0, 100.0), (380.0, 112.0),
              [(260.0, 100.0), (260.0, 112.0)])
    return _g([a, c], [e])


def _canvas_step_between_boxes():
    """Та же ступенька 10px, но оба конца на РАМКАХ и посажены ВНУТРЬ:
    скользить некуда (конец не на грани), коннектора нет — лестница
    доходит до последнего лекарства, СДВИГА УЗЛА."""
    a = _box("a", 100.0, 140.0, w=30.0, h=30.0)
    b = _box("b", 620.0, 150.0)
    e = _edge("e1", "a", "b", (115.0, 140.0), (620.0, 150.0),
              [(200.0, 140.0), (200.0, 150.0)])
    return _g([a, b], [e])


# Площадки вызова `route_fn` сняты грепом (`route_fn` в edit_smooth.py — 4
# штуки: колено :668, скольжение :709, коннектор :730, узел :756), и на
# каждую заведена своя фикстура: холст назван лекарством, которым лестница
# его закрывает. Проверено зондом — снятие гейта на любой из четырёх
# площадок краснит хотя бы один случай ниже (таблица в MEASUREMENTS §MEFX4A).
_STRAIGHT_PIPE_CASES = [
    ("коннектор", _canvas_single_step),      # площадки 1 (колено) и 3
    ("скольжение", _canvas_step_slides),     # площадка 2
    ("узел", _canvas_step_between_boxes),    # площадка 4
]


@pytest.mark.parametrize("remedy, canvas", _STRAIGHT_PIPE_CASES)
def test_router_is_not_called_over_a_straight_pipe(remedy, canvas):
    """И2: лекарство берётся то же, что без роутера, и ни один из четырёх
    вызовов не приходится на уже прямую трубу."""
    g = canvas()
    spy = _SpyRouter()

    st = es.smooth(g, route_fn=spy, reseat_fn=None)

    assert st[remedy] == 1, f"лекарство изменилось: {st}"
    assert spy.over_straight == 0, \
        f"роутер позвали поверх прямой трубы {spy.over_straight} раз(а)"


def test_router_is_still_called_on_a_diagonal():
    """Обратная полярность: колено — лекарство №1, и запрет не имеет права
    его отменить. Без этого теста «роутер не зовут» было бы зелёным и у
    правки, выключившей роутинг вовсе."""
    a = _box("a", 100.0, 100.0)
    b = _box("b", 400.0, 200.0)
    e = _edge("e1", "a", "b", (120.0, 100.0), (380.0, 200.0))
    g = _g([a, b], [e])
    assert not es.is_ortho(e), "фикстура обязана быть косой"
    spy = _SpyRouter()

    es.smooth(g, route_fn=spy, reseat_fn=None)

    assert spy.calls >= 1, "роутер не позвали на косой трубе — колено умерло"
    assert spy.over_straight == 0


@pytest.mark.parametrize("remedy, canvas", _STRAIGHT_PIPE_CASES)
def test_a_straightened_pipe_survives_a_router_that_rebuilds_it(remedy,
                                                                canvas):
    """РАЗНИЦА, ради которой И2 и делается: ход, выпрямивший трубу, больше
    не отменяется маршрутом, построенным поверх него.

    До правки роутер звался безусловно, лишнее колено рождало две косые,
    гейт откатывал ход целиком — и ступенька доживала до оператора."""
    g = canvas()
    e = g["links"][0]

    st = es.smooth(g, route_fn=_JoggingRouter(), reseat_fn=None)

    assert st[remedy] == 1, f"выпрямление отменено роутером: {st}"
    assert es.count_steps(g) == 0, "ступенька пережила сглаживание"
    assert e["waypoints"] == [], f"труба не стала прямой: {e['waypoints']}"


# ── К-4: причина отказа названа (блок 4.3) ─────────────────────────────
#
# Инкременты «отклонено» стоят в трёх РАЗНЫХ ветках лестницы, а наружу
# уходили суммой — оператор видел только «осталось N». Вёдра отдаются
# раздельно; сумма остаётся прежней, её читают стенд
# (`tools/smooth_bench.py:418`) и отчёт редактора.


def _refusals(st):
    return {k: st[k] for k in es.REFUSAL_KINDS}


def _edge_without_candidate():
    """Косая ВНУТРИ маршрута: одиночной ступеньки нет, прямой диагонали
    тоже — сдвигать нечего."""
    a = _box("a", 100.0, 100.0)
    b = _box("b", 300.0, 150.0)
    e = _edge("e1", "a", "b", (100.0, 100.0), (300.0, 150.0),
              [(200.0, 150.0)])
    return [a, b], e


def _edge_over_budget():
    """Прямая диагональ с расхождением 100px — «супер движение»."""
    a = _box("a2", 700.0, 100.0)
    b = _box("b2", 1000.0, 200.0)
    e = _edge("e2", "a2", "b2", (720.0, 100.0), (980.0, 200.0))
    return [a, b], e


def _edge_exhausted_ladder():
    """Кандидат В бюджете, но лекарства нечем взять: якорь оператора на
    ОБОИХ концах — конец не скользит, узлы не двигаются."""
    a = _box("a3", 1300.0, 500.0)
    b = _box("b3", 1500.0, 508.0)
    e = _edge("e3", "a3", "b3", (1320.0, 500.0), (1480.0, 508.0))
    ports.set_edge_pin(a, e, "source", 1320.0, 500.0)
    ports.set_edge_pin(b, e, "target", 1480.0, 508.0)
    return [a, b], e


def test_refusal_no_candidate_is_its_own_bucket():
    nodes, e = _edge_without_candidate()
    assert es.shift_target(e) is None, "фикстура обязана быть без кандидата"

    st = es.smooth(_g(nodes, [e]), route_fn=None, reseat_fn=None)

    assert _refusals(st) == {"нет кандидата": 1, "сверх бюджета": 0,
                             "лестница исчерпана": 0}, st
    assert st["отклонено"] == 1


def test_refusal_over_budget_is_its_own_bucket():
    nodes, e = _edge_over_budget()
    # Порог заперт с ДВУХ сторон (`PROTOCOL §3`): вход абсолютный, и сам
    # факт «фикстура лежит за порогом» сказан вслух — подъём BUDGET
    # покраснеет, а не ослепит сторож молча.
    assert es.shift_target(e) == ("y", 100.0)
    assert es.BUDGET < 100.0, "фикстура обязана лежать ЗА бюджетом"

    st = es.smooth(_g(nodes, [e]), route_fn=None, reseat_fn=None)

    assert _refusals(st) == {"нет кандидата": 0, "сверх бюджета": 1,
                             "лестница исчерпана": 0}, st
    assert st["отклонено"] == 1


def test_refusal_exhausted_ladder_is_its_own_bucket():
    """Третье ведро план не называл вовсе (нашёл редтим): все лекарства
    срезал гейт `accepts`."""
    nodes, e = _edge_exhausted_ladder()
    assert es.shift_target(e) == ("y", 8.0)
    assert 8.0 <= es.BUDGET, "фикстура обязана лежать В бюджете"

    st = es.smooth(_g(nodes, [e]), route_fn=None, reseat_fn=None)

    assert _refusals(st) == {"нет кандидата": 0, "сверх бюджета": 0,
                             "лестница исчерпана": 1}, st
    assert st["отклонено"] == 1


def test_refusal_buckets_sum_to_the_total_on_a_mixed_canvas():
    """Агрегат проверяется на СМЕШАННОМ холсте (`PROTOCOL §3`): на
    однородном сумма сошлась бы и с одним ведром на всех."""
    nodes, edges = [], []
    for make in (_edge_without_candidate, _edge_over_budget,
                 _edge_exhausted_ladder):
        ns, e = make()
        nodes += ns
        edges.append(e)

    st = es.smooth(_g(nodes, edges), route_fn=None, reseat_fn=None)

    assert _refusals(st) == {"нет кандидата": 1, "сверх бюджета": 1,
                             "лестница исчерпана": 1}, st
    assert st["отклонено"] == sum(_refusals(st).values()) == 3


# ── лестница берёт ЛУЧШУЮ пробу, а не первую (блок 6) ─────────────────
#
# Репро graph_edited70827 / edge_144: проба «только изломы» (ступенек
# столько же, изломов меньше) проходила гейт первой и занимала место
# следующей пробы, которая снимала ступеньку. Холст ниже — та же
# ситуация в миниатюре: рамка a -> коннектор b, ступенька 10 px у рамки и
# U-обход 40 px (не ступенька: длиннее STEP_MAX). Лестница маршрута
# (подставная, детерминированная) строит Z со ступенькой — изломов 6 -> 2,
# ступенек 1 -> 1; сдвиг коннектора на 10 px выпрямляет трубу целиком.


def _canvas_bends_vs_step():
    a = _box("a", 100.0, 140.0, w=30.0, h=30.0)          # [85,125,115,155]
    b = _conn("b", 620.0, 150.0)
    e = _edge("e1", "a", "b", (115.0, 140.0), (620.0, 150.0),
              [(200.0, 140.0), (200.0, 150.0), (400.0, 150.0),
               (400.0, 190.0), (450.0, 190.0), (450.0, 150.0)])
    return _g([a, b], [e])


def _z_router(edge):
    """Лестница маршрута: соосные концы — прямая, иначе Z с шажком x=200."""
    sy, ty = edge["source_point"][0], edge["target_point"][0]
    edge["waypoints"] = ([] if abs(sy - ty) < 0.5
                         else [[sy, 200.0], [ty, 200.0]])
    return True


def test_ladder_takes_best_probe_not_first():
    g = _canvas_bends_vs_step()
    assert es.count_steps(g) == 1 and es.count_bends(g) == (6, 1), \
        "фикстура: одна ступенька и U-обход"
    assert es.shift_target(g["links"][0]) == ("y", 10.0)

    # один раунд — ровно одна проба применяется: берётся та, что сняла
    # ступеньку, а не первая прошедшая гейт («колено», только изломы)
    g1 = _canvas_bends_vs_step()
    st = es.smooth(g1, route_fn=_z_router, reseat_fn=None, max_rounds=1)
    assert st["коннектор"] == 1 and st["колено"] == 0, st
    assert es.count_steps(g1) == 0 and es.count_bends(g1) == (0, 1)
    assert g1["links"][0]["waypoints"] == []
    assert g1["nodes"][1]["centroid"] == [140.0, 620.0], \
        "коннектор обязан встать на линию рамки"

    # полный прогон: тот же итог ОДНИМ ходом (первая проба дала бы два:
    # колено, затем коннектор)
    st = es.smooth(g, route_fn=_z_router, reseat_fn=None)
    assert (st["колено"], st["коннектор"]) == (0, 1), st
    assert g == g1


def _canvas_bends_vs_through():
    """Тот же холст, но U-обход прошивает бокс x ([415,170,435,195]), а
    конец b — бокс [620,140,640,160]: сдвиг узла (последняя ступень
    лестницы) снимает ступеньку, а прошивание оставляет. Бокс, а не
    коннектор: коннектор взяла бы проба «б», стоящая ДО колена."""
    g = _canvas_bends_vs_step()
    g["nodes"][1] = _box("b", 630.0, 150.0, w=20.0, h=20.0)
    g["links"][0]["target_point"] = [150.0, 620.0]
    g["nodes"].append(_box("x", 425.0, 182.5, w=20.0, h=25.0))
    return g


def _z_router_aligned_refuses(edge):
    """Лестница маршрута как у редактора: соосные концы без конфликта —
    отказ без записи; иначе Z с шажком x=500 (мимо бокса x)."""
    sy, ty = edge["source_point"][0], edge["target_point"][0]
    if abs(sy - ty) < 0.5:
        return False
    edge["waypoints"] = [[sy, 500.0], [ty, 500.0]]
    return True


def test_ladder_best_probe_is_judged_by_the_whole_gate():
    """Лучшая проба — по ВСЕМУ гейту, а не по одному приоритету: поздняя
    проба (узел: ступенек 1 -> 0) не вытесняет раннюю (колено: изломов
    6 -> 2, прошиваний 1 -> 0), потому что прошивание у неё хуже, чем у
    запомненной. Репро graph_edited_222222: сдвиг узла с меньшим числом
    изломов вытеснял сдвиг коннектора, снимавший 5 прошиваний."""
    g = _canvas_bends_vs_through()
    sc = es.score(g)
    assert (sc["through"], sc["steps"], sc["bends"]) == (1, 1, 6), sc

    g1 = _canvas_bends_vs_through()
    st = es.smooth(g1, route_fn=_z_router_aligned_refuses, reseat_fn=None,
                   max_rounds=1)
    assert st["колено"] == 1 and st["узел"] == 0, st
    sc = es.score(g1)
    assert (sc["through"], sc["steps"], sc["bends"]) == (0, 1, 2), sc

    # полный прогон: колено, затем узел — прямая без прошивания
    st = es.smooth(g, route_fn=_z_router_aligned_refuses, reseat_fn=None)
    assert (st["колено"], st["узел"]) == (1, 1), st
    sc = es.score(g)
    assert (sc["through"], sc["steps"], sc["bends"]) == (0, 0, 0), sc


# ── «б»: клапан поперёк оси на «линия − талия» (блок 6, Р4) ───────────
#
# Зеркало edge_62 на graph_edited971 (координаты холста до блока 3):
# электроарматура node_39 (_axis H, талия 0.222·h ниже центра) и обратный
# клапан node_40, конец которого стоит на линии y = 339.3482. Конец
# электроарматуры — в центре левой грани (329.1697), как во входе, а
# мини-жест (подставной, как `_reseat_after_resize`) сажает его в талию.
# Сдвиг «на d» (10.18) увёл бы талию на 347.74 — ступенька 8.39 вернётся.

_V39 = (475.8584, 310.2697, 517.8584, 348.0697)
_V40 = (350.4178, 328.8482, 392.4178, 349.8482)
_LINE62 = 339.3482


def _waist_reseat(g):
    """Мини-жест: концы рёбер клапана на осевой грани — в его талию."""
    from modules.graph.core import contact, seating

    def reseat(nid):
        n = next(x for x in g["nodes"] if x["id"] == nid)
        if not n.get("bbox"):
            return                                   # коннектор
        rect = tuple(n["bbox"])
        refs = contact.face_refs(n, rect)
        if not refs:
            return
        for e in g["links"]:
            for xid, key in (("source", "source_point"),
                             ("target", "target_point")):
                if e[xid] != nid:
                    continue
                y, x = e[key]
                side = seating._end_side(rect, x, y)
                if side in refs:
                    e[key] = ([refs[side], x] if side in ("L", "R")
                              else [y, refs[side]])
    return reseat


def _canvas_edge62(neighbour=False, bottom=False):
    v39 = _valve("node_39", _V39, cls="armatura_electro")
    v40 = _valve("node_40", _V40, cls="klapan_obratn")
    cy39 = (_V39[1] + _V39[3]) / 2                       # 329.1697
    if bottom:
        # конец на НИЖНЕЙ грани, ступенька поперёк оси — «б» не для него
        c = _conn("c", 700.0, 370.0)
        e = _edge("edge_62", "node_39", "c", (496.8584, _V39[3]),
                  (700.0, 370.0), [(496.8584, 360.0), (560.0, 360.0),
                                   (560.0, 370.0)])
        return _g([v39, c], [e])
    e = _edge("edge_62", "node_40", "node_39", (_V40[2], _LINE62),
              (_V39[0], cy39), [(460.8584, _LINE62), (460.8584, cy39)])
    nodes = [v40, v39]
    if neighbour:
        nodes.append(_box("nb", 500.0, 364.25, w=40.0, h=31.5))  # y1 348.5
    return _g(nodes, [e])


def test_valve_waist_onto_line():
    """T3: клапан сдвигается поперёк оси на «линия − талия» (+1.7887 px),
    труба — прямая из двух точек, партнёр неподвижен."""
    from modules.graph.core import contact

    g = _canvas_edge62()
    v39 = g["nodes"][1]
    assert contact.face_refs(v39, _V39)["L"] == pytest.approx(337.5613,
                                                             abs=1e-4)
    assert es.shift_target(g["links"][0]) == ("y", pytest.approx(10.1785,
                                                                 abs=1e-4))
    v40 = copy.deepcopy(g["nodes"][0])

    st = es.smooth(g, route_fn=None, reseat_fn=_waist_reseat(g))

    e = g["links"][0]
    assert st["узел"] == 1, st
    assert v39["bbox"][1] == pytest.approx(312.0584, abs=0.01)
    assert v39["bbox"][1] - _V39[1] == pytest.approx(1.7887, abs=0.01)
    assert len(es.edge_pts(e)) == 2 and es.count_bends(g) == (0, 1)
    assert e["target_point"][0] == pytest.approx(_LINE62, abs=0.01)
    assert g["nodes"][0] == v40, "партнёр обязан стоять"


def test_valve_shift_skips_nonaxial_end():
    """T4: конец клапана на нижней (неосевой) грани — «б» его не двигает
    (это «д»); ступеньку закрывает коннектор, рамка клапана бит-в-бит."""
    g = _canvas_edge62(bottom=True)
    bb0 = list(g["nodes"][0]["bbox"])
    assert es.shift_target(g["links"][0]) == ("y", 10.0)

    st = es.smooth(g, route_fn=None, reseat_fn=_waist_reseat(g))

    assert g["nodes"][0]["bbox"] == bb0, st
    assert st["узел"] == 0 and st["коннектор"] == 1, st


def test_valve_shift_refused_into_neighbour():
    """T5: сдвиг на +1.79 заводит рамку клапана (y2 = 349.86) на соседа
    (y1 = 348.5) — ход отклонён гейтом соседей, клапан на месте."""
    g = _canvas_edge62(neighbour=True)
    assert es.count_overlaps(g) == 0

    es.smooth(g, route_fn=None, reseat_fn=_waist_reseat(g))

    assert g["nodes"][1]["bbox"] == list(_V39)
    assert es.count_overlaps(g) == 0


# ── «д»: неосевой конец клапана -> свободная осевая талия (блок 6, Р4) ─

def _ell_router(edge):
    """Лестница маршрута: L — сначала по горизонтали от source."""
    sy, sx = edge["source_point"]
    ty, tx = edge["target_point"]
    edge["waypoints"] = [[sy, tx]]
    return True


def test_offaxis_end_to_free_axial_waist():
    """T7: H-клапан степени 2 — A в правой грани (соосный сосед r), B из
    середины нижней грани к боксу q слева-снизу. План Р5
    (`seating.axial_faces`): B — в свободную левую талию, колено
    наружу; неосевых 1 -> 0, ход «осевая талия»."""
    from modules.graph.core import contact

    g = _offaxis_canvas()
    v = g["nodes"][0]
    waist = contact.face_refs(v, tuple(v["bbox"]))["L"]
    assert waist == pytest.approx(320.0 + 0.222 * 40.0)
    assert es.score(g)["offaxis"] == 1

    st = es.smooth(g, route_fn=_ell_router, reseat_fn=None)

    b = g["links"][1]
    assert st["осевая талия"] == 1, st
    assert b["source_point"] == pytest.approx([waist, 400.0], abs=0.01)
    assert b["_src_side"] == "left"
    assert b["waypoints"] and b["waypoints"][0][1] < 400.0, \
        "колено обязано уйти наружу за клапан"
    assert es.score(g)["offaxis"] == 0
    assert v["bbox"] == [400.0, 300.0, 440.0, 340.0], "клапан стоит"


def _offaxis_variant(kind):
    from modules.graph.core import contact

    g = _offaxis_canvas()
    v = g["nodes"][0]
    if kind == "степень 3":
        g["nodes"].append(_box("t", 420.0, 200.0))
        g["links"].append(_edge("C", "v", "t", (420.0, 300.0),
                                (420.0, 220.0)))
    elif kind == "predohran":
        v["class_name"] = "predohran"
    elif kind == "пин":
        ports.set_edge_pin(v, g["links"][1], "source", 420.0, 340.0)
    elif kind == "холст без метки":
        v[contact.LEGACY_KEY] = True
    elif kind == "кратная пара":
        # B ведёт в тот же r, что и A; конец левее середины нижней грани —
        # план отдал бы ему свободную левую талию (грани A и B разные)
        b = g["links"][1]
        b["target"] = "r"
        b["source_point"] = [340.0, 410.0]
        b["target_point"] = [340.0, 600.0]
        b["waypoints"] = [[380.0, 410.0], [380.0, 600.0]]
    return g


@pytest.mark.parametrize("kind", ["степень 3", "predohran", "пин",
                                  "холст без метки", "кратная пара"])
def test_offaxis_move_skipped(kind):
    """T7: без правила Р5 (степень 3, predohran, пин, старый холст) и у
    кратной пары (лестница адресует ребро парой) — «д» не ходит, холст
    бит-в-бит."""
    g = _offaxis_variant(kind)
    before = copy.deepcopy(g)

    def u_router(edge):
        """Кратная пара: из левой талии — U под клапаном к нижней грани r
        (маршрут годный — отказ обязан прийти от правила, а не от гейта)."""
        sy, sx = edge["source_point"]
        edge["waypoints"] = [[sy, sx - 20.0], [380.0, sx - 20.0],
                             [380.0, 600.0]]
        return True

    router = u_router if kind == "кратная пара" else _ell_router
    st = es.smooth(g, route_fn=router, reseat_fn=None)

    assert st["осевая талия"] == 0, (kind, st)
    assert g == before, kind


# ── «е»: клапан вдоль оси при нехватке клиренса (блок 6, Р4) ──────────
#
# Мотив — edge_manual_4 (graph_edited971): труба из правой талии клапана
# уходит вертикалью в 3.4 px от его грани. Лестница маршрута держит
# клиренс (`ROUTE_CLEARANCE`) и вертикаль так близко к рамке бракует —
# колену негде встать, косая остаётся. Подставная лестница ниже строит
# только L «сначала вбок, потом вниз» и бракует вертикаль ближе
# `ec.CLEARANCE` к боковой грани любой рамки, которую она проходит по y.

_V86 = (280.3, 280.0, 322.3, 318.0)             # H-клапан, талия 307.436


def _clearance_router(g):
    def route(edge):
        sy, sx = edge["source_point"]
        ty, tx = edge["target_point"]
        lo, hi = min(sy, ty), max(sy, ty)
        for n in g["nodes"]:
            bb = n.get("bbox")
            if not bb or bb[3] <= lo or bb[1] >= hi:
                continue
            if min(abs(tx - bb[0]), abs(tx - bb[2])) < es.ec.CLEARANCE:
                return False                   # вертикаль липнет к рамке
        edge["waypoints"] = [[sy, tx]]
        return True
    return route


def _canvas_manual4(target_x=325.7):
    from modules.graph.core import contact

    v = _valve("v", _V86)
    waist = contact.face_refs(v, _V86)["R"]
    q = _box("q", target_x, 520.0)                       # верх y = 500
    e = _edge("em4", "v", "q", (_V86[2], waist), (target_x, 500.0))
    return _g([v, q], [e])


def test_valve_along_axis_for_clearance():
    """T9: вылет 3.4 px < клиренса 6 — клапан отходит вдоль оси от
    следующей точки на 6 − 3.4 + 0.5 = 3.1 px (x2 322.3 -> 319.2), лестница
    строит L с одним изломом, косой больше нет."""
    g = _canvas_manual4()
    assert es.score(g)["diag"] == 1

    st = es.smooth(g, route_fn=_clearance_router(g), reseat_fn=None)

    v, e = g["nodes"][0], g["links"][0]
    assert st["узел"] == 1, st
    assert v["bbox"][2] == pytest.approx(319.2, abs=0.01)
    assert v["bbox"][1] == _V86[1] and v["bbox"][3] == _V86[3], \
        "сдвиг — только вдоль оси клапана"
    assert es.score(g)["diag"] == 0
    assert es.count_bends(g) == (1, 1) and len(es.edge_pts(e)) == 3


def test_valve_along_axis_needs_short_stub():
    """Клиренса хватает (вылет 7.7 px): «е» не ходит, колено строится на
    месте, клапан стоит."""
    g = _canvas_manual4(target_x=330.0)

    st = es.smooth(g, route_fn=_clearance_router(g), reseat_fn=None)

    assert st["колено"] == 1 and st["узел"] == 0, st
    assert g["nodes"][0]["bbox"] == list(_V86)


def test_engine_clearance_is_the_editor_route_clearance():
    """Сторож = судья: клиренс «е» — тот же, что держит лестница маршрута
    редактора."""
    pytest.importorskip("PySide6")
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    assert es.ec.CLEARANCE == AdvancedGraphEditor.ROUTE_CLEARANCE


# ── принятый ход снимает _slack у ребра-кандидата (блок 6, S10) ────────

@pytest.mark.parametrize("legacy", [False, True])
def test_accepted_move_drops_slack_of_candidate(legacy):
    """Ход кнопки — жест оператора: метка вынужденной прямой сервера
    `_slack` у ребра-кандидата снимается (шов с блоком 1 п.5). Старый
    холст (`LEGACY_KEY`) живёт по правилам 238fe6b — метка остаётся."""
    from modules.graph.core import contact

    g = _canvas_single_step()
    e = g["links"][0]
    e["_slack"] = True
    if legacy:
        for n in g["nodes"]:
            n[contact.LEGACY_KEY] = True

    st = es.smooth(g, route_fn=None, reseat_fn=None)

    assert st["коннектор"] == 1 and es.count_steps(g) == 0, st
    assert ("_slack" in e) == legacy


def test_refused_move_keeps_slack():
    """Отказ — холст бит-в-бит, метка тоже."""
    a = _box("a", 100.0, 100.0)
    b = _box("b", 400.0, 200.0)                      # за бюджетом
    e = _edge("e1", "a", "b", (120.0, 100.0), (380.0, 200.0))
    e["_slack"] = True
    g = _g([a, b], [e])
    before = copy.deepcopy(g)

    st = es.smooth(g, route_fn=None, reseat_fn=None)

    assert st["отклонено"] == 1 and g == before


# ── «г»: повтор лестницы маршрута после мини-жеста (блок 6, задача 6) ──
#
# Холст `_canvas_single_step` (ступенька 10 px рамка -> коннектор b,
# закрывается сдвигом коннектора) плюс второе ребро e2 от b к боксу c.
# Подставной мини-жест при каждом вызове кладёт e2 лесенкой на 5 изломов
# (так libavoid-сессия с нуджингом возвращала лесенку на 971/edge_52), а
# подставная лестница маршрута строит для e2 заданный маршрут.

_STAIR = [(620.0, 180.0), (660.0, 180.0), (660.0, 220.0), (700.0, 220.0),
          (700.0, 300.0)]


def _canvas_reroute(dup=False):
    g = _canvas_single_step()
    # пин на конце рамки a: сдвиг узла a (лестница берёт лучшую пробу, и
    # он не трогал бы e2) закрыт — ступеньку закрывает только коннектор b,
    # то есть мини-жест обязан случиться
    ports.set_edge_pin(g["nodes"][0], g["links"][0], "source", 115.0, 140.0)
    b = g["nodes"][1]
    c = _box("c", 800.0, 300.0)                      # [780,280,820,320]
    g["nodes"].append(c)
    g["links"].append(_edge("e2", "b", "c", (620.0, 150.0), (780.0, 300.0),
                            [(620.0, 300.0)]))
    if dup:                                          # кратное ребро пары b-c
        g["links"].append(_edge("e3", "c", "b", (800.0, 280.0),
                                (620.0, 150.0), [(800.0, 150.0)]))
    assert b["id"] == "b"
    return g


class _StairReseat:
    """Мини-жест: e2 — лесенка на 5 изломов от нового центроида b;
    снимок ребра сразу после жеста — в `after`."""

    def __init__(self, g):
        self.g, self.after = g, None

    def __call__(self, nid):
        if nid != "b":
            return
        e2 = next(e for e in self.g["links"] if e["id"] == "e2")
        cy, cx = next(n for n in self.g["nodes"] if n["id"] == "b")["centroid"]
        e2["source_point"] = [cy, cx]
        e2["waypoints"] = [[y, x] for x, y in _STAIR]
        for e3 in self.g["links"]:
            if e3["id"] == "e3":                 # кратное: L по линии b
                e3["waypoints"] = [[cy, 800.0]]
                e3["target_point"] = [cy, cx]
        self.after = copy.deepcopy(e2)


def _route_e2(build):
    """Лестница маршрута: e1 — отказ; e2 — маршрут `build(e2)`."""
    calls = []

    def route(edge):
        if edge["id"] != "e2":
            return False
        calls.append(edge["id"])
        return build(edge)
    route.calls = calls
    return route


def _bends_of(e):
    return es.count_bends({"links": [e]})[0]


def test_reroute_after_reseat_keeps_fewer_bends():
    """Повтор лестницы после мини-жеста: L (1 излом) вместо лесенки (5)."""
    g = _canvas_reroute()
    reseat = _StairReseat(g)

    def ell(e):
        cy, cx = e["source_point"]
        e["waypoints"] = [[300.0, cx]]
        e["_src_side"] = "bottom"
        return True
    st = es.smooth(g, route_fn=_route_e2(ell), reseat_fn=reseat)

    e2 = g["links"][1]
    assert st["коннектор"] == 1, st
    assert _bends_of(reseat.after) == 5, "фикстура: жест кладёт лесенку"
    assert _bends_of(e2) == 1 and e2["waypoints"] == [[300.0, 620.0]], \
        f"маршрут повтора не остался: {e2['waypoints']}"
    assert e2["_src_side"] == "bottom"


@pytest.mark.parametrize("why, build", [
    ("изломов больше", lambda e: (
        e.update(waypoints=[[180.0, 620.0], [180.0, 640.0], [200.0, 640.0],
                            [200.0, 660.0], [220.0, 660.0], [220.0, 700.0],
                            [300.0, 700.0]], _src_side="bottom") or True)),
    ("косая (edge_manual_1)", lambda e: (
        e.update(waypoints=[[300.0, 640.0]], _src_side="bottom") or True)),
    ("пометка _route_defect", lambda e: (
        e.update(waypoints=[[300.0, 620.0]], _route_defect=True) or True)),
    ("лестница отказала", lambda e: (
        e.update(waypoints=[[300.0, 620.0]], _src_side="left"), False)[1]),
])
def test_reroute_after_reseat_rolls_back_exactly(why, build):
    """Повтор, который хуже (изломов больше), с КОСОЙ, помеченный или
    отказавший, — отвергается: ребро равно словарю сразу после мини-жеста,
    ключей, которых не было, в нём нет. Ход коннектора при этом принят."""
    g = _canvas_reroute()
    reseat = _StairReseat(g)
    route = _route_e2(build)

    st = es.smooth(g, route_fn=route, reseat_fn=reseat)

    assert route.calls, "повтор лестницы обязан был случиться"
    assert st["коннектор"] == 1, (why, st)
    assert g["links"][1] == reseat.after, why
    assert "_src_side" not in g["links"][1] \
        and "_route_defect" not in g["links"][1]


def test_reroute_skips_non_unique_pair():
    """Кратное ребро пары (b, c): лестница редактора адресует ребро парой и
    переложила бы чужое — повтора нет, e2 остаётся после мини-жеста."""
    g = _canvas_reroute(dup=True)
    reseat = _StairReseat(g)

    def ell(e):
        e["waypoints"] = [[300.0, 620.0]]
        return True
    route = _route_e2(ell)
    st = es.smooth(g, route_fn=route, reseat_fn=reseat)

    assert st["коннектор"] == 1, st
    assert route.calls == [], "кратное ребро обязано пропускаться"
    assert g["links"][1] == reseat.after


def _diag_reseat(g, wps):
    """Мини-жест оставляет e2 с косым первым сегментом: от нового
    центроида b по `wps` (x, y) к c."""
    after = {}

    def reseat(nid):
        if nid != "b":
            return
        e2 = g["links"][1]
        e2["source_point"] = list(g["nodes"][1]["centroid"])
        e2["waypoints"] = [[y, x] for x, y in wps]
        after.update(copy.deepcopy(e2))
    reseat.after = after
    return reseat


def _ell(e):
    e["waypoints"] = [[300.0, 620.0]]
    return True


def test_reroute_after_reseat_takes_orthogonal_over_diagonal():
    """Мини-жест оставил косую в маршруте с одним изломом: повтор без
    косой остаётся и при РАВНОМ числе изломов (у ребра — «или косая»,
    у холста изломов не больше) — косая старше изломов."""
    g = _canvas_reroute()
    reseat = _diag_reseat(g, [(640.0, 300.0)])     # косой (620,140)->(640,300)

    st = es.smooth(g, route_fn=_route_e2(_ell), reseat_fn=reseat)

    e2 = g["links"][1]
    assert st["коннектор"] == 1, st
    assert es.diag_segments_of(reseat.after) and \
        _bends_of(reseat.after) == 1, "фикстура: жест оставил косую, 1 излом"
    assert e2["waypoints"] == [[300.0, 620.0]] and _bends_of(e2) == 1
    assert not es.diag_segments_of(e2)


def test_reroute_refused_when_it_adds_a_bend_to_the_canvas():
    """Решение Максима 30.09 (счётчики холста не хуже,
    чем сразу после мини-жеста, — и изломы тоже): мини-жест оставил чистую
    косую (0 изломов), повтор строит L (1 излом) — изломов у холста
    стало бы больше, повтор отвергнут. Косая остаётся, и гейт хода
    отклоняет сам ход коннектора: холст бит-в-бит."""
    g = _canvas_reroute()
    before = copy.deepcopy(g)
    reseat = _diag_reseat(g, [])                    # (620,140)->(780,300)
    route = _route_e2(_ell)

    st = es.smooth(g, route_fn=route, reseat_fn=reseat)

    assert route.calls, "повтор лестницы обязан был случиться"
    assert es.diag_segments_of(reseat.after) and _bends_of(reseat.after) == 0
    assert st["коннектор"] == 0, st
    assert g == before


def test_reroute_kept_only_if_canvas_not_worse():
    """Повтор без косой и с меньшим числом изломов, но сквозь чужой бокс f
    (сквозь узел 0 -> 1 против холста сразу после мини-жеста) —
    отвергается (решение Максима 30.09: счётчики холста — косые, вдоль
    чужого, сквозь узел, изломы — не хуже, чем после мини-жеста); ребро
    равно словарю после мини-жеста, ход коннектора принят."""
    g = _canvas_reroute()
    g["nodes"].append(_box("f", 620.0, 250.0, w=20.0, h=20.0))
    reseat = _StairReseat(g)
    assert es.score(g)["through"] == 1, "фикстура: L входа прошивает f"

    def ell(e):
        cy, cx = e["source_point"]
        e["waypoints"] = [[300.0, cx]]
        return True
    route = _route_e2(ell)
    st = es.smooth(g, route_fn=route, reseat_fn=reseat)

    assert route.calls, "повтор лестницы обязан был случиться"
    assert st["коннектор"] == 1, st
    assert g["links"][1] == reseat.after
    assert es.score(g)["through"] == 0


# ── судья с памятью: то же число, что полный score (ускорение, блок 6) ──
def _crop971(model):
    """Левая часть корпусного холста 971 (x2 <= 900: 50 узлов, 40 рёбер —
    вдоль своего/чужого, сквозь узел, наложения и неосевые есть)."""
    import json
    from pathlib import Path
    from tools import corpus
    p = (Path(__file__).resolve().parents[1] / "tools" / "bench"
         / "smooth_corpus" / "edited" / "graph_edited971.json")
    g = json.loads(p.read_text(encoding="utf-8"))
    keep = {n["id"] for n in g["nodes"]
            if (n["bbox"][2] if n.get("bbox") else n["centroid"][1]) <= 900}
    g["nodes"] = [n for n in g["nodes"] if n["id"] in keep]
    g["links"] = [e for e in g["links"]
                  if e["source"] in keep and e["target"] in keep]
    return corpus.as_model(g) if model else g


@pytest.mark.parametrize("model", [False, True], ids=["старый", "модель"])
def test_judge_equals_full_score(model):
    """`_Judge` (память по рёбрам) == `score` бит-в-бит под случайными
    правками: сдвиг узла (его рёбра и ЧУЖИЕ рёбра у его зоны), излом,
    новый излом, конец ребра, узел без своих концов, откат и новый снимок
    (`_put`/`_capture`)."""
    import random
    g = _crop971(model)
    rnd = random.Random(5)
    judge = es._Judge()
    nodes, links = g["nodes"], g["links"]
    snap = es._capture(g)
    assert judge(g) == es.score(g)
    for step in range(40):
        k = rnd.random()
        if k < 0.4:
            n = rnd.choice(nodes)
            es._shift_node(g, n["id"], rnd.uniform(-25, 25),
                           rnd.uniform(-25, 25))
        elif k < 0.6:
            e = rnd.choice(links)
            if e.get("waypoints"):
                w = e["waypoints"]
                i = rnd.randrange(len(w))
                w[i] = [w[i][0] + rnd.uniform(-15, 15), w[i][1]]
            else:
                e["waypoints"] = [[e["source_point"][0], e["target_point"][1]]]
        elif k < 0.7:
            e = rnd.choice(links)
            e["source_point"] = [e["source_point"][0] + rnd.uniform(-4, 4),
                                 e["source_point"][1]]
        elif k < 0.8:
            # узел без своих концов (коннектор — зоны у него нет): судят
            # концы его рёбер (conn_off, adrift, углы)
            n = rnd.choice(nodes)
            dx, dy = rnd.uniform(-3, 3), rnd.uniform(-3, 3)
            n["centroid"] = [n["centroid"][0] + dy, n["centroid"][1] + dx]
            if n.get("bbox"):
                b = n["bbox"]
                n["bbox"] = [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]
        elif k < 0.9:
            es._put(g, snap)
        else:
            snap = es._capture(g)
        assert judge(g) == es.score(g), f"шаг {step}"


def test_snapshot_round_trip_is_exact():
    """`_capture`/`_put` возвращают холст бит-в-бит (типы, порядок ключей)
    свежими копиями: int остаётся int, вложенный список — не тот же объект."""
    g = _g([_box("a", 100.0, 100.0)],
           [{"id": "e", "source": "a", "target": "a", "w": [1, 2.5],
             "source_point": [100, 120], "target_point": [80, 100]}])
    import json
    before = json.dumps(g)
    snap = es._capture(g)
    g["links"][0]["source_point"][0] = 7.0
    g["links"][0]["z"] = 1
    es._put(g, snap)
    assert json.dumps(g) == before
    es._put(g, snap)
    w = g["links"][0]["w"]
    w.append(3)
    es._put(g, snap)
    assert json.dumps(g) == before and g["links"][0]["w"] is not w


# ── «ж»: перемычка клапан–клапан талия-в-талию (решение Максима 01.10) ──
# Числа — graph_edited971@model после хода «узел» по edge_52: электроарматура
# node_36 и обратный клапан node_37 впритык (зазор 0.72 px), талии 321.686 и
# 327.215, перемычка edge_53 — L из сегментов 0.72 и 5.53 px (оба короче
# SEG_FLOOR: косой судья не видит, окна H-V-H у трёх точек нет).
_V36 = (1034.3, 294.39, 1076.3, 332.19)
_V37 = (1077.02, 316.72, 1119.02, 337.72)


def _canvas_jumper(neighbour=False, legacy=False, gap=0.72, jog=None):
    """node_37 -> node_36 перемычкой из левой талии node_37 в правую талию
    node_36; у node_37 — прямая к боксу r по своей талии, у node_36 —
    колено к коннектору c из левой талии. neighbour — бокс под node_36
    (y1 = 334: сдвиг вниз на 5.53 до y2 = 337.72 на него заезжает)."""
    from modules.graph.core import contact

    v36 = _valve("node_36", _V36, cls="armatura_electro")
    x1 = _V36[2] + gap
    v37 = _valve("node_37", (x1, _V37[1], x1 + 42.0, _V37[3]),
                 cls="klapan_obratn")
    w36 = contact.face_refs(v36, tuple(_V36))["R"]
    w37 = contact.face_refs(v37, tuple(v37["bbox"]))["L"]
    if jog is not None:                       # смещение концов перемычки
        w36 = w37 - jog
    r = _box("r", 1250.0, w37)
    c = _conn("c", 1000.0, 450.0)
    links = [
        _edge("edge_52", "node_36", "c", (_V36[0], w36), (1000.0, 450.0),
              [(1000.0, w36)]),
        _edge("edge_53", "node_37", "node_36", (x1, w37), (_V36[2], w36),
              [(_V36[2], w37)] if gap < es.ec.SEG_FLOOR else None),
        _edge("edge_56", "node_37", "r", (x1 + 42.0, w37), (1230.0, w37)),
    ]
    nodes = [v36, v37, r, c]
    if neighbour:
        nodes.append(_box("nb", 1050.0, 344.0, w=30.0, h=20.0))
    if legacy:
        for n in (v36, v37):
            n[contact.LEGACY_KEY] = True
    return _g(nodes, links)


def test_judge_sees_waist_step_between_valves():
    """Судья: перемычка с талиями вразнобой 5.53 px — одна ступенька
    (`count_steps`), хотя косой и окна H-V-H у неё нет. Не ступенька: старый
    холст (талии нет), смещение в допуске оси, длинная косая (её видит
    судья косых — двойного счёта нет)."""
    g = _canvas_jumper()
    e53 = g["links"][1]
    assert es.diag_segments_of(e53) == [] and es._step_windows(e53) == 0
    assert es.count_steps(g) == 1 and es.score(g)["steps"] == 1
    assert es.count_steps(_canvas_jumper(legacy=True)) == 0
    assert es.count_steps(_canvas_jumper(jog=1.0)) == 0
    far = _canvas_jumper(gap=30.0)               # прямая косая 30 x 5.53
    assert len(es.edge_pts(far["links"][1])) == 2
    assert es.score(far)["diag"] == 1 and es.count_steps(far) == 0


def test_waist_jumper_onto_one_line():
    """Кнопка: node_36 — строго поперёк оси на разницу талий (+5.529),
    перемычка — прямая из двух точек на одной линии, талии клапанов на
    одной линии, ступенек 0; node_37 (его прямая к r косила бы) стоит."""
    from modules.graph.core import contact

    g = _canvas_jumper()
    v36, v37 = g["nodes"][0], g["nodes"][1]
    bb37 = list(v37["bbox"])

    st = es.smooth(g, route_fn=_ell_router, reseat_fn=_waist_reseat(g))

    e53 = g["links"][1]
    assert st["узел"] >= 1, st
    assert v37["bbox"] == bb37, "второй клапан обязан стоять"
    assert v36["bbox"][0] == _V36[0] and v36["bbox"][2] == _V36[2]
    assert v36["bbox"][1] - _V36[1] == pytest.approx(5.529, abs=0.01)
    w36 = contact.face_refs(v36, tuple(v36["bbox"]))["R"]
    w37 = contact.face_refs(v37, tuple(v37["bbox"]))["L"]
    assert w36 == pytest.approx(w37, abs=0.01)
    pts = es.edge_pts(e53)
    assert len(pts) == 2 and pts[0][1] == pytest.approx(pts[1][1], abs=0.01)
    assert es.count_steps(g) == 0 and es.score(g)["diag"] == 0


def test_waist_jumper_refused_into_neighbour():
    """Сдвиг node_36 вниз на 5.53 заводит его рамку (y2 337.72) на бокс nb
    (y1 334) — гейт соседей (`box_hits`) не пускает; node_37 не пускает
    гейт хода (`accepts_waist`: его прямая к r стала бы косой). Клапаны
    стоят, ступенька талий остаётся видимой."""
    g = _canvas_jumper(neighbour=True)
    bbs = [list(n["bbox"]) for n in g["nodes"][:2]]
    assert es.count_overlaps(g) == 0
    moves = es._waist_moves(g, g["links"][1], es.BUDGET, 40000.0)
    assert [m[0] for m in moves] == ["node_37"], "node_36 — мимо box_hits"

    es.smooth(g, route_fn=_ell_router, reseat_fn=_waist_reseat(g))

    assert [n["bbox"] for n in g["nodes"][:2]] == bbs
    assert es.count_overlaps(g) == 0 and es.count_steps(g) == 1


@pytest.mark.parametrize("kind", ["пин", "веер"])
def test_waist_jumper_skipped(kind):
    """Хода «ж» нет: конец перемычки — пин оператора (прямая переставила бы
    и его) или на грани перемычки вторая труба (веер слотов, конец не в
    талии). Пин — кнопка не двигает ни клапан, ни конец."""
    g = _canvas_jumper()
    e53 = g["links"][1]
    if kind == "пин":
        y, x = e53["target_point"]
        ports.set_edge_pin(g["nodes"][0], e53, "target", x, y)
    else:
        w = e53["source_point"][0] + 6.0
        g["nodes"].append(_conn("c2", 1100.0, 400.0))
        g["links"].append(_edge("edge_x", "node_37", "c2",
                                (e53["source_point"][1], w), (1100.0, 400.0),
                                [(1070.0, w), (1070.0, 400.0)]))
    assert es._waist_moves(g, e53, es.BUDGET, 40000.0) == []
    if kind == "пин":
        before = copy.deepcopy(g)
        es.smooth(g, route_fn=_ell_router, reseat_fn=_waist_reseat(g))
        assert g["nodes"] == before["nodes"]
        assert e53["target_point"] == before["links"][1]["target_point"]


# ── крючок — не ступенька (решение Максима 01.10) ──────────────────────
# Окно H-V-H (V-H-V) с коротким средним сегментом: плечи в одну сторону —
# ступенька (линия сдвинута и продолжается, Z), в разные — крючок
# (U-разворот: так «д» обходит клапан к свободной осевой талии). Крючок
# судья считает отдельно (`hooks`), в гейт и в `_NOT_WORSE` он не входит.
# Кандидатом лестницы окно крючка остаётся (`single_step`): сдвиг на его
# шажок снимает изломы — отбор только ступенек поднял изломы edited
# 421 -> 445 (MEASUREMENTS §138).

def _hook_edge(eid="e", far_x=260.0, mid=18.0, vertical=False):
    """Бокс a (грань R, x=120) -> вправо до x=300, шажок вниз на `mid`,
    дальше к x=far_x: far_x < 300 — крючок (плечи навстречу), far_x > 300 —
    ступенька. vertical — то же, повёрнутое на 90° (окно V-H-V)."""
    pts = [(120.0, 100.0), (300.0, 100.0), (300.0, 100.0 + mid),
           (far_x, 100.0 + mid)]
    if vertical:
        pts = [(y, x) for x, y in pts]
    return _edge(eid, "a", "c", pts[0], pts[-1], pts[1:-1])


@pytest.mark.parametrize("vertical", [False, True], ids=["H-V-H", "V-H-V"])
def test_hook_is_not_a_step(vertical):
    hook = _hook_edge(vertical=vertical)
    step = _hook_edge(far_x=420.0, vertical=vertical)
    assert es._windows(hook) == (0, 1) and es._windows(step) == (1, 0)
    for e, steps, hooks in ((hook, 0, 1), (step, 1, 0)):
        g = _g([_box("a", 100.0, 100.0)], [e])
        assert (es.count_steps(g), es.count_hooks(g)) == (steps, hooks)
        sc = es.score(g)
        assert (sc["steps"], sc["hooks"]) == (steps, hooks)
    # лестница берёт оба окна (сдвиг на шажок), судья — только ступеньку
    axis = "x" if vertical else "y"
    for e in (hook, step):
        assert es.single_step(e) == (axis, 18.0) and es.step_index(e) == 1
    # длиннее STEP_MAX — осмысленный изгиб: ни ступенька, ни крючок
    assert es._windows(_hook_edge(mid=es.STEP_MAX + 1.0,
                                  vertical=vertical)) == (0, 0)


def test_hook_next_to_a_step_counts_one_step():
    """Ребро с крючком и ступенькой: судья — одна ступенька и один крючок
    (до 01.10 — две ступеньки)."""
    e = _edge("e", "a", "c", (120.0, 100.0), (150.0, 130.0),
              [(300.0, 100.0), (300.0, 118.0), (200.0, 118.0),
               (200.0, 130.0)])
    assert es._windows(e) == (1, 1)
    g = _g([_box("a", 100.0, 100.0), _conn("c", 150.0, 130.0)], [e])
    assert (es.count_steps(g), es.count_hooks(g)) == (1, 1)


def test_hooks_are_not_in_the_gate():
    """Крючок справочный: рост `hooks` хода не отклоняет, падение — не
    выигрыш; в «не хуже» его нет."""
    assert "hooks" not in es._NOT_WORSE
    base = {"diag": 0, "dev": 0.0, "steps": 2, "bends": 10, "near": 0,
            "along_own": 0, "along_foreign": 0, "through": 0, "corner": 0,
            "adrift": 0, "conn_off": 0, "poly_off": 0, "overlap": 0,
            "offaxis": 0, "waist": 0, "hooks": 3, "w": 100.0, "h": 100.0}
    assert es.accepts(base, dict(base, steps=1, hooks=7))
    assert not es.accepts(base, dict(base, hooks=0)), "крючок — не выигрыш"
    assert es.accepts_axial(dict(base, offaxis=1), dict(base, hooks=9))


def test_hook_is_straightened_for_bends_not_for_steps():
    """Крючок — кандидат лестницы: сдвиг коннектора на шажок 18 px кладёт
    трубу в прямую (изломов 2 -> 0). Ход принят по изломам: ступенек судья
    не видел ни до, ни после (до 01.10 — по «ступеньке» 1 -> 0)."""
    a, c = _box("a", 100.0, 100.0), _conn("c", 260.0, 118.0)
    g = _g([a, c], [_hook_edge()])
    s0 = es.score(g)
    assert (s0["steps"], s0["hooks"], s0["bends"]) == (0, 1, 2)

    st = es.smooth(g, route_fn=None, reseat_fn=None)

    assert st["коннектор"] == 1, st
    s1 = es.score(g)
    assert (s1["steps"], s1["hooks"], s1["bends"]) == (0, 0, 0)
    assert es.is_straight(g["links"][0])
    assert c["centroid"] == pytest.approx([100.0, 260.0])


def test_judge_counts_hooks_like_full_score():
    """`_Judge` (память по рёбрам) считает крючки как `score`: крючок,
    ставший ступенькой, и обратно — пересчитаны."""
    a, c = _box("a", 100.0, 100.0), _conn("c", 260.0, 118.0)
    g = _g([a, c], [_hook_edge()])
    judge = es._Judge()
    assert judge(g) == es.score(g) and judge(g)["hooks"] == 1
    e = g["links"][0]
    e["target_point"] = [118.0, 420.0]              # -> ступенька
    assert judge(g) == es.score(g)
    assert (judge(g)["steps"], judge(g)["hooks"]) == (1, 0)
    e["target_point"] = [118.0, 260.0]              # -> снова крючок
    assert judge(g) == es.score(g) and judge(g)["hooks"] == 1


# ── Р-а: зазор форм не ближе пола раскладки (решение Максима 01.10) ─────
@pytest.mark.parametrize("model", [False, True], ids=["старый", "модель"])
def test_count_gaps_is_layout_defects(model):
    """`gap` судьи кнопки — тот же счёт, что `layout.spread.defects` с полом
    раскладки (6 px): судья хода и детектор раскладки не расходятся."""
    from modules.graph.core.graph_access import nodes_by_id
    from modules.graph.core.layout import LayoutParams, spread
    g = _crop971(model)
    want = len(spread.defects(g, nodes_by_id(g), LayoutParams().floor))
    assert want >= 1, "фикстура: на кропе 971 есть ребро ближе пола"
    assert es.count_gaps(g) == want
    assert es.score(g)["gap"] == want == es._Judge()(g)["gap"]
    assert "gap" in es._NOT_WORSE


def _canvas_gap(dx):
    """Ступенька 10 px между коннекторами c1 (100, 140) и c2 (400, 150):
    закрыть её можно только сдвигом коннектора (c1 вниз или c2 вверх). У
    каждого второй сосед — бокс по диагонали в dx px по x: c1 вниз и c2
    вверх подходят к нему на hypot(dx, 5) (b1 под c1 слева, b2 над c2
    справа; рёбра e1 вдоль рамок не идут)."""
    c1, c2 = _conn("c1", 100.0, 140.0), _conn("c2", 400.0, 150.0)
    b1 = _box("b1", 100.0 - dx - 20.0, 175.0)     # x2 = 100 - dx, y1 = 155
    b2 = _box("b2", 400.0 + dx + 20.0, 116.0)     # x1 = 400 + dx, y2 = 136
    e1 = _edge("e1", "c1", "c2", (100.0, 140.0), (400.0, 150.0),
               [(200.0, 140.0), (200.0, 150.0)])
    e2 = _edge("e2", "c1", "b1", (100.0, 140.0), (100.0 - dx, 175.0),
               [(100.0, 175.0)])
    e3 = _edge("e3", "c2", "b2", (400.0, 150.0), (400.0 + dx, 116.0),
               [(400.0, 116.0)])
    return _g([c1, c2, b1, b2], [e1, e2, e3])


def test_connector_shift_refused_below_gap_floor():
    """Р-а (гейт блока 6, §142.2: ae30deb4 0 -> 4): сдвиг коннектора, который
    закрывает ступеньку ценой зазора форм меньше пола (hypot(3, 5) = 5.83 <
    6), отклонён — холст бит-в-бит. В 9 px от бокса (10.3 после сдвига) тот
    же холст лечится сдвигом коннектора."""
    free = _canvas_gap(9.0)
    st = es.smooth(free)
    assert st["коннектор"] == 1 and es.count_steps(free) == 0, st
    assert es.count_gaps(free) == 0

    g = _canvas_gap(3.0)
    assert es.count_gaps(g) == 0 and es.count_steps(g) == 1
    before = copy.deepcopy(g)
    st = es.smooth(g)
    assert st["коннектор"] == 0 and st["лестница исчерпана"] == 1, st
    assert g == before, "отклонённый ход обязан оставить холст бит-в-бит"


# ── Р-б: ход судится так, как его покажет следующее открытие (01.10) ───
def _poly(nid, x1, y1, x2, y2):
    n = _box(nid, (x1 + x2) / 2, (y1 + y2) / 2, w=x2 - x1, h=y2 - y1)
    n["segmentation"] = [x1, y1, x2, y1, x2, y2, x1, y2]
    return n


def _mid_canon(canvas):
    """Канон-заглушка открытия: конец на правом участке контура — в середину
    участка (как MID_SNAP у рамки, только без допуска)."""
    byid = {n["id"]: n for n in canvas["nodes"]}
    for e in canvas["links"]:
        for key, nk in (("source_point", "source"), ("target_point", "target")):
            seg = byid[e[nk]].get("segmentation")
            if seg and abs(e[key][1] - max(seg[0::2])) < 0.5:
                e[key] = [(min(seg[1::2]) + max(seg[1::2])) / 2, e[key][1]]


def _canvas_reopen():
    """Ступенька 6 px у контура A (x 100..140, середина правого участка
    y 120): скольжение конца по участку выпрямляет трубу на y 126, вне
    середины — открытие вернёт конец в середину, то есть в косую. Коннектор
    c держит прямая e2 к боксу B (его сдвиг скосил бы e2). Вне игры —
    e3: прямая от контура D в 6 px от середины, канон её двигает и без
    кнопки (вход не каноничен)."""
    A = _poly("A", 100, 100, 140, 140)
    c = _conn("c", 300.0, 126.0)
    B = _box("B", 420.0, 126.0)
    D = _poly("D", 100, 300, 140, 340)
    d = _conn("d", 300.0, 326.0)
    return _g([A, c, B, D, d], [
        _edge("e1", "A", "c", (140.0, 120.0), (300.0, 126.0),
              [(220.0, 120.0), (220.0, 126.0)]),
        _edge("e2", "c", "B", (300.0, 126.0), (400.0, 126.0)),
        _edge("e3", "D", "d", (140.0, 326.0), (300.0, 326.0))])


def test_canon_judges_the_probe_as_reopen_shows_it():
    """Без канона ход — скольжение конца с середины участка (ступенька
    снята, но открытие вернёт конец в середину — косая). С каноном та же
    проба после канона косая и проигрывает; ход — сдвиг узла A, конец
    остаётся в середине. Итог — неподвижная точка канона; e3, которую
    канон двигает и без кнопки, кнопка не трогает."""
    bare = _canvas_reopen()
    st = es.smooth(bare)
    assert st["скольжение"] == 1 and st["узел"] == 0, st
    assert bare["links"][0]["source_point"] == [126.0, 140.0]

    g, g0 = _canvas_reopen(), _canvas_reopen()
    st = es.smooth(g, canon_fn=_mid_canon)
    assert st["узел"] == 1 and st["скольжение"] == 0, st
    assert es.count_steps(g) == 0 and es.score(g)["diag"] == 0
    assert g["links"][2] == g0["links"][2], "e3 — не дело кнопки"
    h = copy.deepcopy(g)
    _mid_canon(h)
    assert h["links"][:2] == g["links"][:2], "сохранить -> открыть сдвигает"


def test_accepted_move_is_polished_refused_is_not(monkeypatch):
    """Дожим шума (`_Canon.polish`) — ровно на принятых ходах, с входом
    ребра того же хода; отклонённая лестница его не зовёт."""
    seen = []
    real = es._Canon.polish

    def spy(self, graph, base):
        seen.append(base)
        return real(self, graph, base)

    monkeypatch.setattr(es._Canon, "polish", spy)
    st = es.smooth(_canvas_reopen(), canon_fn=_mid_canon)
    assert len(seen) == sum(st[k] for k in ("узел", "скольжение",
                                            "коннектор", "колено", "излом",
                                            "осевая талия")) == 1
    seen.clear()
    es.smooth(_canvas_reopen(), allow_node_shift=False, canon_fn=_mid_canon)
    assert seen == []


def test_canon_refused_move_leaves_canvas_bit_identical():
    """Сдвига узла нет (allow_node_shift=False): единственный ход —
    скольжение, которое открытие превратит в косую, — отклонён, холст
    бит-в-бит (включая e3, которую канон сдвинул бы)."""
    g, before = _canvas_reopen(), _canvas_reopen()
    st = es.smooth(g, allow_node_shift=False, canon_fn=_mid_canon)
    assert st["скольжение"] == 0 and st["лестница исчерпана"] == 1, st
    assert g == before


def test_canon_settle_moves_neighbour_only_for_the_probe():
    """`_Canon.settle`: нетронутое ребро получает геометрию канона, только
    если канон двигает его ИЗ-ЗА пробы. Канон-заглушка ставит e2 на высоту
    конца e1 (сосед по слоту); проба сдвинула e1 — e2 едет. Вход, где канон
    двигает e2 и без пробы, а проба его не меняет, — e2 стоит."""
    def canon(canvas):
        e1, e2 = canvas["links"]
        e2["source_point"] = [e1["source_point"][0] + 10.0,
                              e2["source_point"][1]]

    def canvas(y2):
        return _g([_box("A", 120.0, 120.0), _conn("c", 300.0, 120.0),
                   _conn("d", 300.0, 130.0)],
                  [_edge("e1", "A", "c", (140.0, 120.0), (300.0, 120.0)),
                   _edge("e2", "A", "d", (140.0, y2), (300.0, 130.0))])

    g = canvas(130.0)                      # вход — неподвижная точка канона
    base = es._capture(g)
    g["links"][0]["source_point"] = [114.0, 140.0]          # проба
    es._Canon(canon).settle(g, base)
    assert g["links"][1]["source_point"] == [124.0, 140.0]

    g = canvas(127.0)                      # канон двигает e2 и без пробы
    base = es._capture(g)
    g["links"][0]["target_point"] = [120.0, 301.0]          # e1 тронута,
    es._Canon(canon).settle(g, base)                        # конец у A — нет
    assert g["links"][1]["source_point"] == [127.0, 140.0]


def test_canon_runs_to_its_fixed_point():
    """Канон открытия не идемпотентен в последнем знаке (edited@model
    graph_edited87537_2 edge_162: 816.9176266602942 -> ...943 вторым
    открытием) — принятый ход `_Canon.polish` дожимает до неподвижной
    точки: после settle и polish ещё одно открытие не двигает ничего."""
    def canon(canvas):                     # 126 -> 120 + 1e-12 -> 120
        for e in canvas["links"]:
            y = e["source_point"][0]
            if abs(y - 120.0) > 1.0:
                e["source_point"] = [120.0 + 1e-12, e["source_point"][1]]
            elif y != 120.0:
                e["source_point"] = [120.0, e["source_point"][1]]

    g = _g([_box("A", 120.0, 120.0), _conn("c", 300.0, 126.0)],
           [_edge("e1", "A", "c", (140.0, 120.0), (300.0, 126.0))])
    base = es._capture(g)
    g["links"][0]["source_point"] = [126.0, 140.0]          # проба
    c = es._Canon(canon)
    c.settle(g, base)
    assert g["links"][0]["source_point"] == [120.0 + 1e-12, 140.0]
    c.polish(g, base)
    h = copy.deepcopy(g)
    canon(h)
    assert h == g and g["links"][0]["source_point"] == [120.0, 140.0]


def test_canon_sees_slack_dropped_by_the_move():
    """Принятый ход снимает метку вынужденной прямой `_slack`, а канон её
    читает (держит трубу прямой). Канон в судье видит ребро уже без метки:
    скольжение, которое открытие без метки вернуло бы в середину участка,
    проигрывает сдвигу узла; итог — неподвижная точка канона без `_slack`."""
    def canon(canvas):                     # `_mid_canon`, но `_slack` держит
        keep = {e["id"]: e["source_point"] for e in canvas["links"]
                if e.get("_slack")}
        _mid_canon(canvas)
        for e in canvas["links"]:
            if e["id"] in keep:
                e["source_point"] = keep[e["id"]]

    g = _canvas_reopen()
    g["links"][0]["_slack"] = 1
    st = es.smooth(g, canon_fn=canon)
    assert st["узел"] == 1 and st["скольжение"] == 0, st
    assert "_slack" not in g["links"][0]
    h = copy.deepcopy(g)
    canon(h)
    assert h["links"][:2] == g["links"][:2]


def test_canon_does_not_chase_its_own_drift():
    """Канон двигает waypoints при каждом открытии и без кнопки (свежие
    26747a10, 089feca2, ca1f6ea2: 0.05–0.17 px за открытие). Дожимается
    только шум арифметики; дрейф больше шума — геометрия одного открытия,
    а не трёх."""
    def canon(canvas):                     # дрейф колена на 0.17 px
        for e in canvas["links"]:
            e["waypoints"] = [[w[0], w[1] - 0.17] for w in e["waypoints"]]

    g = _g([_box("A", 120.0, 120.0), _conn("c", 300.0, 160.0)],
           [_edge("e1", "A", "c", (140.0, 120.0), (300.0, 160.0),
                  [(220.0, 120.0), (220.0, 160.0)])])
    base = es._capture(g)
    g["links"][0]["target_point"] = [160.0, 301.0]          # проба
    c = es._Canon(canon)
    c.settle(g, base)
    c.polish(g, base)
    assert g["links"][0]["waypoints"] == [[120.0, 219.83], [160.0, 219.83]]


def test_polish_touches_only_edges_of_the_move():
    """Дожим шума — только рёбра, которые изменил ход: шум канона на
    нетронутом ребре (его вход так же неустойчив) — не дело кнопки."""
    def canon(canvas):                     # e2: шум в последнем знаке
        e2 = canvas["links"][1]
        e2["source_point"] = [e2["source_point"][0] + 1e-12,
                              e2["source_point"][1]]

    g = _g([_box("A", 120.0, 120.0), _conn("c", 300.0, 120.0),
            _conn("d", 300.0, 130.0)],
           [_edge("e1", "A", "c", (140.0, 120.0), (300.0, 120.0)),
            _edge("e2", "A", "d", (140.0, 130.0), (300.0, 130.0))])
    base = es._capture(g)
    g["links"][0]["target_point"] = [120.0, 301.0]          # ход трогает e1
    es._Canon(canon).polish(g, base)
    assert g["links"][1]["source_point"] == [130.0, 140.0]

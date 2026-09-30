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
    контурным узлом, где скольжение по участку осталось."""
    a = _box("a", 100.0, 100.0,
             segmentation=[80.0, 80.0, 120.0, 80.0, 120.0, 120.0,
                           80.0, 120.0])
    c = _conn("c", 400.0, 112.0)
    e = _edge("e1", "a", "c", (120.0, 100.0), (400.0, 112.0),
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
    лестницы) снимает ступеньку, а прошивание оставляет."""
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

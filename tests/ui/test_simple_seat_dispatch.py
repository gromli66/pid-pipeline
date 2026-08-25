# -*- coding: utf-8 -*-
"""Посадка «Проверки схемы» диспетчером «Контуров» (блок 4, пункты 4.2/4.4).

Решение Максима 2026-08-25 («да, как в Контурах», живая претензия,
предусмотренная пунктом 8-4 дороги): новые рёбра и пересадка при ресайзе в
«Проверке схемы» идут алгоритмом Контуров — СТРОГАЯ ОСЬ важнее центра. Раньше
обе точки считал канон `modules/graph/core/seating`: он сажает конец на
проекцию партнёра, а партнёра вне створа клампит в угол — труба уезжала
в диагональ.

Здесь проверяется НАБЛЮДАЕМОЕ поведение вкладки (инварианты данных ребра),
а не `_current_mode` и не внутренности диспетчера: его бит-в-бит держит
`test_contour_seat_golden.py`.

Разворот Г5б (вопрос A6, решение Максима 2026-08-25): конец МОЖЕТ уйти от
центроида коннектора примерно на радиус его виртуального бокса ради строгой
оси. Это осознанная смена контракта Э1, а не дефект, — заперто
`test_connector_end_may_leave_the_centroid_for_a_strict_axis`.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

IMG_W, IMG_H = 900, 700
SKIN_CLASS = "nasos"        # входит в FIXED_SIZES
PLAIN_CLASS = "unknow"      # вне FIXED_SIZES


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _box(nid, x1, y1, x2, y2, cls=PLAIN_CLASS, seg=None):
    return {"id": nid, "type": "equipment",
            "centroid": [(y1 + y2) / 2.0, (x1 + x2) / 2.0],
            "bbox": [float(x1), float(y1), float(x2), float(y2)],
            "segmentation": seg, "class_id": 1, "class_name": cls,
            "degree": 0}


def _conn(nid, cx, cy):
    return {"id": nid, "type": "connector", "centroid": [float(cy), float(cx)],
            "bbox": None, "segmentation": None, "class_id": -1,
            "class_name": "connector", "degree": 0}


def _editor(qapp, tmp_path, nodes, links=()):
    from ui.editors.simple_graph_editor import SimpleGraphEditor

    graph = {"directed": False, "multigraph": False,
             "graph": {"image_size": [IMG_H, IMG_W]},
             "nodes": list(nodes), "links": list(links),
             "text_blocks": [], "bindings": []}
    tmp_path.mkdir(parents=True, exist_ok=True)
    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")

    ed = SimpleGraphEditor()
    assert ed.load_data(str(ip), str(gp))
    return ed


def _ends(ed, a, b):
    e = ed.model.find_edge_data(ed.model.edge_key(a, b))
    assert e is not None
    return e["source_point"], e["target_point"]     # оба [y, x]


def _axis(sp, tp, tol=1e-6):
    """'H' | 'V' | 'diag' — по КОНЦАМ ребра, не по центроидам."""
    if abs(sp[0] - tp[0]) <= tol:
        return "H"
    if abs(sp[1] - tp[1]) <= tol:
        return "V"
    return "diag"


# ── 1. Смещённые центры → строго H/V ─────────────────────────────────────

def test_offset_centres_still_give_a_strict_axis_bbox_bbox(qapp, tmp_path):
    """Две рамки со СМЕЩЁННЫМИ центрами, но перекрытыми створами по Y.

    Центры разъехались (y 200 против y 260), поэтому канон сажал конец на
    проекцию центроида партнёра и труба шла по диагонали. Диспетчер держит
    ось через перекрытие створов.
    """
    a = _box("a", 100, 150, 200, 250)      # створ Y 150..250, центр y=200
    b = _box("b", 500, 210, 600, 310)      # створ Y 210..310, центр y=260
    ed = _editor(qapp, tmp_path, [a, b])

    assert ed.add_edge("a", "b")
    sp, tp = _ends(ed, "a", "b")
    assert _axis(sp, tp) == "H", f"диагональ вместо оси: {sp} → {tp}"
    # конец сидит на грани своей рамки, а не в её середине
    assert sp[1] == 200.0 and tp[1] == 500.0


def test_offset_centres_still_give_a_strict_axis_conn_conn(qapp, tmp_path):
    """conn↔conn — 32 % рёбер вкладки и пара, недостижимая в «Контурах».

    Штатный путь диспетчера (виртуальный бокс партнёра) принят Максимом:
    ось важнее центра.
    """
    a = _conn("a", 200.0, 200.0)
    b = _conn("b", 600.0, 205.0)           # центры разъехались на 5 px по Y
    ed = _editor(qapp, tmp_path, [a, b])

    assert ed.add_edge("a", "b")
    sp, tp = _ends(ed, "a", "b")
    assert _axis(sp, tp) == "H", f"диагональ вместо оси: {sp} → {tp}"


def test_offset_centres_still_give_a_strict_axis_conn_bbox(qapp, tmp_path):
    a = _conn("a", 200.0, 205.0)
    b = _box("b", 500, 150, 600, 250)
    ed = _editor(qapp, tmp_path, [a, b])

    assert ed.add_edge("a", "b")
    sp, tp = _ends(ed, "a", "b")
    assert _axis(sp, tp) == "H", f"диагональ вместо оси: {sp} → {tp}"


def test_connector_end_may_leave_the_centroid_for_a_strict_axis(qapp, tmp_path):
    """РАЗВОРОТ Г5б (решение Максима 2026-08-25, вопрос A6).

    Прежний контракт Э1 — «конец коннектора ЖЁСТКО центроид». Ради строгой
    оси конец теперь может отойти от центроида на радиус его виртуального
    бокса. Число заперто с двух сторон: не ноль и не больше радиуса.
    """
    a = _conn("a", 200.0, 200.0)
    b = _conn("b", 600.0, 205.0)
    ed = _editor(qapp, tmp_path, [a, b])
    r = ed.CONNECTOR_MARKER_RADIUS

    assert ed.add_edge("a", "b")
    sp, tp = _ends(ed, "a", "b")

    away = max(abs(sp[1] - 200.0), abs(tp[1] - 600.0))
    assert away > 0.0, "конец не сдвинулся — разворот Г5б не состоялся"
    assert away <= r, f"конец ушёл дальше радиуса коннектора: {away} > {r}"


# ── 2. Симметрия порядка кликов (4.4.3) ──────────────────────────────────

def test_seat_does_not_depend_on_the_click_order(qapp, tmp_path):
    """`dispatch(s,t) != dispatch(t,s)` — пара нормализуется В ВЫЗОВЕ,
    поэтому геометрия от порядка кликов не зависит.

    ⚠ Фикстура подобрана ПОД АСИММЕТРИЮ, а не на глаз: у `connect_bbox_bbox`
    приоритет 1 — «перпендикуляр из центра A», приоритет 2 — «из центра B»,
    и сортировка по приоритету предпочитает первый. Здесь срабатывают ОБА
    (центр a на створе b и центр b на створе a), поэтому смена порядка
    меняет выбранную высоту. Первая редакция теста брала рамки, у которых
    работал только приоритет 3 (перекрытие створов), — он симметричен, и
    тест оставался зелёным при СНЯТОЙ нормализации (поймано зондом).

    Утверждается РАЗНИЦА ролей при совпадении геометрии: иначе тест был бы
    зелён и на несимметричном коде, где обе точки просто меняются местами.
    """
    nodes = [_box("a", 100, 150, 200, 250),      # центр y=200
             _box("b", 500, 100, 600, 400)]      # центр y=250, створ 100..400
    ed1 = _editor(qapp, tmp_path / "ab", nodes)
    ed2 = _editor(qapp, tmp_path / "ba", nodes)

    assert ed1.add_edge("a", "b")
    assert ed2.add_edge("b", "a")

    sp1, tp1 = _ends(ed1, "a", "b")        # source=a, target=b
    sp2, tp2 = _ends(ed2, "a", "b")        # source=b, target=a
    assert (sp1, tp1) == (tp2, sp2), (
        f"порядок кликов сдвинул геометрию: {sp1},{tp1} против {tp2},{sp2}")


# ── 3. Идемпотентность ───────────────────────────────────────────────────

def test_reseating_the_same_pair_does_not_move_the_ends(qapp, tmp_path):
    """Повторная посадка не двигает концы (иначе ресайз «дышал» бы)."""
    a = _box("a", 100, 150, 200, 250)
    b = _box("b", 500, 210, 600, 310)
    ed = _editor(qapp, tmp_path, [a, b])
    assert ed.add_edge("a", "b")
    before = _ends(ed, "a", "b")

    ed._reseat_after_resize("a")
    ed._reseat_after_resize("b")

    assert _ends(ed, "a", "b") == before


# ── 4. Пин Э5 первее посадки ─────────────────────────────────────────────

def test_pinned_end_survives_the_new_seating(qapp, tmp_path):
    """Конец с ПИНОМ диспетчер не трогает — приоритет Э5 сохранён.

    Проверяется РАЗНИЦА: пинованный конец стоит на своём, а НЕ пинованный
    в том же жесте уезжает.
    """
    from ui.editors import port_model

    a = _box("a", 100, 150, 200, 250)
    b = _box("b", 500, 210, 600, 310)
    ed = _editor(qapp, tmp_path, [a, b])
    assert ed.add_edge("a", "b")
    edge = ed.model.find_edge_data(ed.model.edge_key("a", "b"))

    port_model.set_edge_pin(ed.nodes["a"], edge, "source", 200.0, 160.0)
    pinned = port_model.pinned_port(ed.nodes["a"], edge)
    assert pinned is not None, "пин не встал — тест судил бы не то"
    free_before = list(edge["target_point"])

    ed._on_node_resized("b", [520.0, 400.0, 620.0, 500.0])

    assert edge["source_point"] == [pinned[1], pinned[0]], "пин перезаписан"
    assert edge["target_point"] != free_before, "свободный конец не поехал"


# ── 5. Г5а: пересекающиеся рамки не сажают конец в середину узла ─────────

def test_overlapping_boxes_do_not_seat_into_the_node_centre(qapp, tmp_path):
    """`connect_bbox_bbox` на пересекающихся рамках отдаёт пару ЦЕНТРОИДОВ
    (graph_geometry.py:225). В «Контурах» ветка недостижима, здесь дала бы
    конец в середине узла — до 89 px мимо формы. Такая пара идёт каноном.
    """
    a = _box("a", 100, 100, 300, 300)
    b = _box("b", 200, 200, 400, 400)      # перекрытие по обеим осям
    ed = _editor(qapp, tmp_path, [a, b])

    assert ed.add_edge("a", "b")
    sp, tp = _ends(ed, "a", "b")

    assert [sp[0], sp[1]] != list(ed.nodes["a"]["centroid"]), "конец в центре a"
    assert [tp[0], tp[1]] != list(ed.nodes["b"]["centroid"]), "конец в центре b"
    for point, nid in ((sp, "a"), (tp, "b")):
        x1, y1, x2, y2 = ed.nodes[nid]["bbox"]
        on_border = (min(abs(point[1] - x1), abs(point[1] - x2)) <= 0.5
                     or min(abs(point[0] - y1), abs(point[0] - y2)) <= 0.5)
        assert on_border, f"конец {nid} мимо формы: {point}"


# ── 6. Лифт letterbox (Г5в) — только bbox-веткам ─────────────────────────

def test_contour_end_is_not_lifted_off_the_drawn_shape(qapp, tmp_path):
    """У узла с контуром конец остаётся НА КОНТУРЕ, а не на рамке.

    Лифт скиновых узлов вытолкнул бы его «мимо нарисованного» — поэтому
    он применяется только концам из bbox-веток. Класс узла НАРОЧНО скиновый
    (FIXED_SIZES): именно на нём лифт и сработал бы.
    """
    seg = [150.0, 100.0, 200.0, 200.0, 150.0, 300.0, 100.0, 200.0]   # ромб
    a = _box("a", 100, 100, 200, 300, cls=SKIN_CLASS, seg=seg)
    b = _conn("b", 600.0, 200.0)
    ed = _editor(qapp, tmp_path, [a, b])

    assert ed.add_edge("a", "b")
    sp, _tp = _ends(ed, "a", "b")

    # правая вершина ромба, а не правая грань рамки (x=200 против x=200 —
    # различаем по Y: на рамке конец сел бы на створ партнёра y=200 тоже,
    # поэтому берём точку строго на контуре и проверяем принадлежность)
    assert _on_polygon(sp, seg), f"конец ушёл с контура: {sp}"


def _on_polygon(pt_yx, seg, tol=0.5):
    import math
    pts = [(seg[i], seg[i + 1]) for i in range(0, len(seg) - 1, 2)]
    x, y = pt_yx[1], pt_yx[0]
    best = float("inf")
    for i in range(len(pts)):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % len(pts)]
        dx, dy = bx - ax, by - ay
        d2 = dx * dx + dy * dy
        t = 0.0 if d2 < 1e-12 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / d2))
        best = min(best, math.hypot(x - (ax + t * dx), y - (ay + t * dy)))
    return best <= tol


def test_bbox_end_of_a_skin_node_sits_on_the_frame(qapp, tmp_path):
    """Скиновый узел БЕЗ контура: конец обязан быть на рамке (лифт Г5в).

    Дефект bc41b77 («концы в letterbox-полосе») жил именно на этой вкладке.
    """
    a = _box("a", 100, 150, 200, 250, cls=SKIN_CLASS)
    b = _conn("b", 600.0, 200.0)
    ed = _editor(qapp, tmp_path, [a, b])

    assert ed.add_edge("a", "b")
    sp, _tp = _ends(ed, "a", "b")

    x1, y1, x2, y2 = ed.nodes["a"]["bbox"]
    assert (min(abs(sp[1] - x1), abs(sp[1] - x2)) <= 1e-9
            or min(abs(sp[0] - y1), abs(sp[0] - y2)) <= 1e-9), \
        f"конец скинового узла не на рамке: {sp}"


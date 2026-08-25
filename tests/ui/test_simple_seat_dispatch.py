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

    ⚠ Контур ВПИСАН в рамку с зазором (правая вершина x=180 при x2=200).
    Первая редакция брала ромб по серединам граней — его правая вершина
    лежала РОВНО на рамке, поэтому `_lift_to_seat_rect` уходил по ветке
    «уже на рамке или снаружи» и тест был зелен ДАЖЕ БЕЗ сторожа (поймано
    ревизией, инъекция И6). Зазор возвращает сторожу смысл: без него лифт
    двигает конец с контура на рамку, и утверждение ниже краснеет.
    """
    seg = [150.0, 120.0, 180.0, 200.0, 150.0, 280.0, 120.0, 200.0]   # ромб В рамке
    a = _box("a", 100, 100, 200, 300, cls=SKIN_CLASS, seg=seg)
    b = _conn("b", 600.0, 200.0)
    ed = _editor(qapp, tmp_path, [a, b])

    # замок фикстуры: контур обязан лежать СТРОГО внутри рамки, иначе лифт
    # не сработал бы и без сторожа — тест снова стал бы декоративным
    x1, y1, x2, y2 = a["bbox"]
    assert min(seg[0::2]) > x1 and max(seg[0::2]) < x2, "контур касается рамки по X"
    assert min(seg[1::2]) > y1 and max(seg[1::2]) < y2, "контур касается рамки по Y"

    assert ed.add_edge("a", "b")
    sp, _tp = _ends(ed, "a", "b")

    assert _on_polygon(sp, seg), f"конец ушёл с контура: {sp}"
    assert sp[1] < x2, "конец вытолкнут на рамку — лифт применён к контурной ветке"


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


# ── 7. Фолбэк выбирает БЛИЖНЮЮ стенку (4.4.7, решение №7 редтима) ────────

def test_fallback_prefers_the_near_wall_not_the_far_one():
    """`score = 1 - |dy|/len` монотонно растёт с расстоянием, поэтому при
    ОДИНАКОВОМ отклонении от оси дальняя стенка получала балл выше ближней
    и выигрывала. Судья заменён на отклонение В ПИКСЕЛЯХ + ближе.

    Фикстура заперта с двух сторон: обе стенки дают ОДНО отклонение (иначе
    тест проверял бы не тот механизм), а расстояния до них различаются
    заметно — сторож не декоративен.
    """
    from ui.editors.graph_geometry import connect_point_bbox

    bbox = [150.0, 150.0, 250.0, 250.0]
    point = (500.0, 50.0)                 # справа-выше: створа нет
    _pt, seat, _kind = connect_point_bbox(point, bbox)

    near, far = (250.0, 150.0), (150.0, 150.0)     # правая-верхняя / левая-верхняя
    assert abs(near[1] - point[1]) == abs(far[1] - point[1]) == 100.0, \
        "фикстура сломана: отклонения стенок разошлись"
    d_near = ((near[0] - point[0]) ** 2 + (near[1] - point[1]) ** 2) ** 0.5
    d_far = ((far[0] - point[0]) ** 2 + (far[1] - point[1]) ** 2) ** 0.5
    assert d_far - d_near > 90.0, "стенки слишком близки — сторож декоративен"

    assert seat == near, f"выбрана дальняя стенка: {seat}"


def test_resize_out_of_span_lands_on_the_near_corner(qapp, tmp_path):
    """Тот же дефект в боевом жесте: ресайз вывел партнёра из створа.

    В «Контурах» ветка редкая, здесь — типовая, и она НЕ ловится гейтами
    «мимо формы = 0» и «доля H/V»: конец на форме и труба осевая, просто
    идёт сквозь весь узел.
    """
    a = _box("a", 150, 150, 250, 250)
    b = _conn("b", 500.0, 50.0)
    ed = _editor(qapp, tmp_path, [a, b])
    assert ed.add_edge("a", "b")

    sp, _tp = _ends(ed, "a", "b")
    assert sp == [150.0, 250.0], f"ближний угол потерян: {sp}"


# ── 8. Контракт ошибок на стороне «Проверки схемы» ───────────────────────

def test_broken_geometry_falls_back_to_the_canon_not_to_zero(qapp, tmp_path,
                                                             monkeypatch):
    """Диспетчер отказал — пара садится КАНОНОМ, а не тихим [0, 0].

    У «Контуров» при отказе есть старые точки, у нового ребра их нет:
    `create_edge_data` подставил бы [0, 0] (graph_data.py:471-472), и труба
    уехала бы в левый верхний угол листа. Подмена проверяется на факт вызова —
    переезд `dispatch_connect` в другой модуль сделал бы её немой.
    """
    a = _box("a", 100, 150, 200, 250)
    b = _conn("b", 600.0, 200.0)
    ed = _editor(qapp, tmp_path, [a, b])

    calls = []

    def _boom(*args, **kwargs):
        calls.append(1)
        raise ValueError("геометрия не сошлась")

    monkeypatch.setattr("ui.editors.simple_graph_editor.dispatch_connect", _boom)
    assert ed.add_edge("a", "b")

    assert calls, "подмена не сработала — посадка идёт мимо диспетчера"
    sp, tp = _ends(ed, "a", "b")
    assert sp != [0.0, 0.0] and tp != [0.0, 0.0], "конец сел в [0, 0]"
    # канон сажает конец на правую грань рамки, конец коннектора — в центроид
    assert sp == [200.0, 200.0]
    assert tp == [200.0, 600.0]


# ── 9. Ближняя стенка — сторож на КАЖДУЮ из пяти connect_* (возврат В1) ──
#
# Прежний сторож (§8 выше) бил в одну функцию из пяти, а зонд патчил ОБЩИЙ
# `axis_deviation` — поэтому краснел и создавал впечатление покрытия. Ревизия
# откатила судью ПОФУНКЦИОНАЛЬНО и показала: `connect_bbox_bbox` (через который
# идёт основная масса дрейфа) и три полигонные функции не покрыты ничем.
# Список функций снят КОМАНДОЙ, а не выбран на глаз:
#     grep "^def connect_" ui/editors/graph_geometry.py
#
# Одна геометрия закрывает все пять. Рамка A и партнёр B разнесены так, что ни
# один приоритет 1-3 не срабатывает (нет створов, нет перекрытий), и обе пары
# стенок-кандидатов дают ОДНО осевое отклонение 60 px, но разное расстояние:
#   ближняя пара: правая стенка A (x=250) — левая стенка B (x=500), 257.1 px
#   дальняя пара: левая  стенка A (x=150) — правая стенка B (x=600), 454.0 px
# Старый нормированный балл (1 - |dy|/len) у дальней ВЫШЕ (0.868 против 0.767)
# — ровно поэтому она и выигрывала.

FALLBACK_A = [150.0, 150.0, 250.0, 250.0]     # рамка A, центр (200, 200)
FALLBACK_B = [500.0, 10.0, 600.0, 90.0]       # партнёр справа-ВЫШЕ, без створа
FALLBACK_POINT = (200.0, 200.0)               # центр A как коннектор


def _square(x1, y1, x2, y2):
    """Контур, повторяющий рамку: полигонные ветки идут по тем же стенкам."""
    return [x1, y1, x2, y1, x2, y2, x1, y2]


def _near_far_walls():
    """((ближняя A, ближняя B), (дальняя A, дальняя B)) для этой геометрии."""
    ax1, ay1, ax2, _ay2 = FALLBACK_A
    bx1, by1, bx2, by2 = FALLBACK_B
    near = ((ax2, ay1), (bx1, by2))            # (250,150) — (500,90)
    far = ((ax1, ay1), (bx2, by2))             # (150,150) — (600,90)
    return near, far


def test_fallback_fixture_is_locked_on_both_sides():
    """Фикстура таблицы ниже: отклонения РАВНЫ, расстояния РАЗНЫЕ.

    Без этого замка таблица проверяла бы не тот механизм: при разных
    отклонениях ближнюю выбрал бы и старый судья.
    """
    (na, nb), (fa, fb) = _near_far_walls()
    dev_near = min(abs(nb[0] - na[0]), abs(nb[1] - na[1]))
    dev_far = min(abs(fb[0] - fa[0]), abs(fb[1] - fa[1]))
    assert dev_near == dev_far == 60.0, "отклонения стенок разошлись"

    d_near = ((nb[0] - na[0]) ** 2 + (nb[1] - na[1]) ** 2) ** 0.5
    d_far = ((fb[0] - fa[0]) ** 2 + (fb[1] - fa[1]) ** 2) ** 0.5
    assert d_far - d_near > 190.0, "стенки слишком близки — таблица декоративна"

    # приоритеты 1-3 обязаны промахнуться, иначе фолбэк не исполнится вовсе
    ax1, ay1, ax2, ay2 = FALLBACK_A
    bx1, by1, bx2, by2 = FALLBACK_B
    assert not (bx1 <= (ax1 + ax2) / 2 <= bx2), "центр A попал в створ B по X"
    assert not (by1 <= (ay1 + ay2) / 2 <= by2), "центр A попал в створ B по Y"
    assert not (ax1 <= (bx1 + bx2) / 2 <= ax2), "центр B попал в створ A по X"
    assert not (ay1 <= (by1 + by2) / 2 <= ay2), "центр B попал в створ A по Y"
    assert max(ax1, bx1) > min(ax2, bx2), "рамки перекрылись по X"
    assert max(ay1, by1) > min(ay2, by2), "рамки перекрылись по Y"


def _fallback_cases():
    """[(имя функции, аргументы, ожидаемая пара точек)] — по строке на функцию."""
    from ui.editors.graph_geometry import (
        connect_bbox_bbox, connect_bbox_polygon, connect_point_bbox,
        connect_point_polygon, connect_polygon_polygon,
    )
    near, _far = _near_far_walls()
    a_poly, b_poly = _square(*FALLBACK_A), _square(*FALLBACK_B)
    return [
        ("connect_bbox_bbox", connect_bbox_bbox,
         (FALLBACK_A, FALLBACK_B), near),
        ("connect_bbox_polygon", connect_bbox_polygon,
         (FALLBACK_A, b_poly), near),
        ("connect_polygon_polygon", connect_polygon_polygon,
         (a_poly, b_poly), near),
        ("connect_point_bbox", connect_point_bbox,
         (FALLBACK_POINT, FALLBACK_B), (FALLBACK_POINT, near[1])),
        ("connect_point_polygon", connect_point_polygon,
         (FALLBACK_POINT, b_poly), (FALLBACK_POINT, near[1])),
    ]


@pytest.mark.parametrize("name", [c[0] for c in _fallback_cases()])
def test_every_connect_function_prefers_the_near_wall(name):
    """Откат судьи В ЭТОЙ функции обязан покраснить ИМЕННО эту строку.

    Числа абсолютные: ожидание берётся из геометрии фикстуры (стенка, которая
    ближе), а не из вызова проверяемой функции.
    """
    case = next(c for c in _fallback_cases() if c[0] == name)
    _name, fn, args, expected = case
    pa, pb, _kind = fn(*args)
    assert (pa, pb) == expected, (
        f"{name}: выбрана дальняя стенка {(pa, pb)}, ожидалась ближняя {expected}")


def test_fallback_table_covers_every_connect_function():
    """Таблица обязана покрывать ВЕСЬ список `connect_*`, снятый с модуля.

    Список берётся из самого модуля, а не переписывается сюда руками: новая
    `connect_*` без строки в таблице роняет тест, а не проходит незамеченной.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "ui" / "editors" / "graph_geometry.py"
    declared = set(re.findall(r"^def (connect_\w+)",
                              src.read_text(encoding="utf-8"), re.M))
    covered = {c[0] for c in _fallback_cases()}
    assert declared == covered, (
        f"не покрыты: {sorted(declared - covered)}; "
        f"лишние в таблице: {sorted(covered - declared)}")


# ── 10. Вырожденная пара не клампит конец в УГОЛ (возврат В4) ────────────

def test_degenerate_pair_keeps_the_face_instead_of_the_corner(qapp, tmp_path):
    """Фолбэк вырожденной пары садит конец на ГРАНЬ, а не в угол.

    Прежний `add_edge` считал второй конец от РЕАЛЬНОЙ точки партнёра
    (цепочка), а первая редакция фолбэка — симметрично, оба конца от
    центроидов. Симметрия читалась аккуратнее, но клампила конец в угол чаще:
    на корпусе 7 против 3 (замер §P4-rev-fix.2). Клампить в угол — ровно тот
    дефект, который лечит пункт 4.4.1, поэтому фолбэк держит цепочку.

    Числа абсолютные: [200,300] — правая грань `a`, [300,300] — её угол.
    """
    a = _box("a", 100, 100, 300, 300)
    b = _box("b", 200, 200, 400, 400)          # перекрытие по обеим осям
    ed = _editor(qapp, tmp_path, [a, b])
    assert ed._pair_is_degenerate("a", "b", ed.nodes["a"], ed.nodes["b"]), \
        "фикстура не вырожденная — тест проверял бы не ту ветку"

    assert ed.add_edge("a", "b")
    sp, _tp = _ends(ed, "a", "b")

    assert sp == [200.0, 300.0], f"конец не на грани: {sp}"
    assert sp != [300.0, 300.0], "конец склампился в УГОЛ рамки"


# ── 11. Контур мимо своей рамки не даёт тихий [0,0] (возврат В3) ─────────

def test_contour_placed_away_from_its_bbox_seats_on_the_frame(qapp, tmp_path):
    """Шесть нулей в `segmentation` проходят мерку диспетчера — и садили
    конец в [0, 0], левый верхний угол листа.

    Исключения при этом НЕТ, поэтому `except` не срабатывал, а докстрока
    обещала «канонную пару, а не тихий [0, 0]». Вход закрыт до вызова
    диспетчера (`_pair_is_degenerate`), обещание стало правдой.
    На корпусе таких узлов 0 из 47 с контуром — правка бесплатна.
    """
    a = _box("a", 500, 500, 600, 600, seg=[0.0] * 6)
    b = _box("b", 100, 540, 200, 560)
    ed = _editor(qapp, tmp_path, [a, b])
    assert ed._contour_misses_own_bbox(ed.nodes["a"]), \
        "фикстура не та: контур пересекается со своей рамкой"

    assert ed.add_edge("a", "b")
    sp, _tp = _ends(ed, "a", "b")

    assert sp != [0.0, 0.0], "конец сел в левый верхний угол листа"
    x1, y1, x2, y2 = ed.nodes["a"]["bbox"]
    assert x1 - 0.5 <= sp[1] <= x2 + 0.5 and y1 - 0.5 <= sp[0] <= y2 + 0.5, \
        f"конец вне собственной рамки узла: {sp}"


def test_a_normal_contour_slightly_outside_its_bbox_is_not_degenerate(qapp, tmp_path):
    """Замок с другой стороны: контур, чьи вершины выходят за рамку на доли
    пикселя, вырожденным НЕ считается.

    Такой узел на корпусе реален (`ca1f6ea2/node_337`: контур до 1312.9 при
    рамке до 1312) — сторож, ловящий шум в пиксель, отправил бы на канон
    здоровые пары и тихо съел бы всю правку.
    """
    seg = [500.0, 500.0, 600.9, 500.0, 600.9, 600.0, 500.0, 600.0]
    a = _box("a", 500, 500, 600, 600, seg=seg)
    ed = _editor(qapp, tmp_path, [a, _box("b", 100, 540, 200, 560)])

    assert not ed._contour_misses_own_bbox(ed.nodes["a"]), \
        "контур с выходом на доли пикселя объявлен вырожденным"

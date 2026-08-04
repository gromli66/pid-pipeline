# -*- coding: utf-8 -*-
"""Лифт конца из нутра скинового узла на рамку bbox (простой редактор).

Репро бага 2026-08-04: вкладка «Проверка схемы» (SimpleGraphTab /
SimpleGraphEditor) сажала концы каноном `seating.reseat_edge`, который для
FIXED_SIZES-узлов кладёт конец на letterbox-след скина ВНУТРИ bbox; без
`_axis` (граф до раскладки) полоса ложится поперёк вертикальной арматуры и
конец оказывается в середине узла (боевые файлы: 8d517a35 — 41 конец,
c2f79462 — 4). Фикс: `BaseGraphEditor._lift_to_seat_rect` — выталкивание на
рамку вдоль луча посадки в `get_connection_point`.
"""

from ui.editors.base_graph_editor import BaseGraphEditor

lift = BaseGraphEditor._lift_to_seat_rect

# Реальный узел боевого репро: вертикальная арматура node_133 (c2f79462).
ARMATURA = {
    "id": "node_133",
    "class_name": "armatura_ruchn",
    "bbox": [2452, 2110, 2494, 2185],   # x1, y1, x2, y2 (портрет 42x75)
    "centroid": [2147, 2473],
}


def test_lifts_band_end_to_top_face():
    # Канон без _axis сажает подход сверху на верх горизонтальной полосы
    # (y=2137, середина узла) — лифт выводит на верхнюю грань y1=2110.
    assert lift(ARMATURA, [2137.0, 2473.0], 2473.0, 2050.0) == [2110.0, 2473.0]


def test_lifts_band_end_to_bottom_face():
    assert lift(ARMATURA, [2158.0, 2472.0], 2472.0, 2300.0) == [2185.0, 2472.0]


def test_pure_axis_preserved():
    # Вертикальный луч: x не должен приобрести дробь от арифметики.
    y, x = lift(ARMATURA, [2137.0, 2473.0], 2473.0, 1000.0)
    assert x == 2473.0


def test_end_on_face_untouched():
    pt = [2110.0, 2473.0]
    assert lift(ARMATURA, pt, 2473.0, 2050.0) == pt


def test_contour_class_untouched():
    node = dict(ARMATURA, class_name="unknow")   # не FIXED_SIZES
    pt = [2137.0, 2473.0]
    assert lift(node, pt, 2473.0, 2050.0) == pt


def test_connector_without_bbox_untouched():
    node = {"id": "c", "class_name": "connector", "bbox": None,
            "centroid": [100, 100]}
    pt = [100.0, 100.0]
    assert lift(node, pt, 100.0, 50.0) == pt

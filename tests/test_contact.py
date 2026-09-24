# -*- coding: utf-8 -*-
"""contact — одна модель точек прикрепления для посадки и выгрузки.

Держит три обещания модуля:
  * таблицы скинов переехали из `graph_to_fxml` без изменений (реэкспорт —
    те же объекты, значит и экспортёр, и ядро видят одно);
  * `orientation` — дословно прежнее правило выгрузки
    (`canvas_to_fxml._axis_and_orientation` + «датчик всегда H»);
  * порт клапана и сдвиг контрола в выгрузке — из одного источника:
    порт минус центр рамки == минус сдвиг `apply_contact_offset`.
"""
from __future__ import annotations

import itertools

import pytest

from modules import graph_to_fxml
from modules.graph.core import contact
from modules.graph.core.contact import (
    CLASS_NAME_TO_SKIN,
    REVERSE_VERTICAL_CLASSES,
    SKIN_CONTACT_OFFSET,
    face_ref,
    face_refs,
    is_valve,
    orientation,
)

VALVES = sorted(c for c, spec in CLASS_NAME_TO_SKIN.items()
                if spec[0] == 'ValveControl')


@pytest.mark.parametrize("name", [
    "CLASS_NAME_TO_SKIN", "SKIP_CLASS_NAMES", "FORCE_HORIZONTAL_CONTROLS",
    "DIRECTION_TO_ORIENTATION", "_DIRECTION_TO_AXIS",
    "DIRECTION_ORIENT_CLASSES", "REVERSE_VERTICAL_CLASSES",
    "SKIN_CONTACT_OFFSET",
])
def test_exporter_reexports_same_objects(name):
    assert getattr(graph_to_fxml, name) is getattr(contact, name)


def test_valves_are_the_ten_valve_controls():
    assert VALVES == sorted([
        'armatura_electro', 'armatura_membr_electro', 'armatura_ruchn',
        'armatura_seroprivod', 'klapan_obratn', 'klapan_obratn_seroprivod',
        'predohran', 'regulator_electro', 'regulator_ruchn',
        'regulator_seroprivod'])
    assert all(is_valve({'class_name': c}) for c in VALVES)
    assert not is_valve({'class_name': 'nasos'})
    assert not is_valve({'class_name': 'connector'})
    assert not is_valve({'class_name': 'unknow'})


# ── orientation: дословно прежнее правило выгрузки ──────────────────────────

def _legacy_orientation(node, sides):
    """Копия правила до переезда (canvas_to_fxml._axis_and_orientation +
    переопределение датчика в generate_canvas_control)."""
    cn = node.get('class_name')
    spec = CLASS_NAME_TO_SKIN.get(cn)
    d = node.get('direction') or node.get('flow_direction')
    if cn in graph_to_fxml.DIRECTION_ORIENT_CLASSES \
            and d in graph_to_fxml.DIRECTION_TO_ORIENTATION:
        axis = 'V' if graph_to_fxml._DIRECTION_TO_AXIS[d] == 'VERTICAL' \
            else 'H'
        res = axis, graph_to_fxml.DIRECTION_TO_ORIENTATION[d]
    else:
        axis = node.get('_axis')
        if axis not in ('H', 'V'):
            if sides:
                axis = 'H' if ({'LEFT', 'RIGHT'} & sides) else 'V'
            else:
                b = graph_to_fxml.parse_bbox(node.get('bbox'))
                axis = 'H' if (not b or b['width'] >= b['height']) else 'V'
        emit = 'HORIZONTAL' if axis == 'H' else 'VERTICAL'
        if axis == 'V' and cn in REVERSE_VERTICAL_CLASSES:
            emit = 'VERTICAL_REVERSE'
        res = axis, emit
    if spec and spec[0] in graph_to_fxml.FORCE_HORIZONTAL_CONTROLS:
        res = 'H', 'HORIZONTAL'
    return res


_CLASSES = sorted(CLASS_NAME_TO_SKIN) + ['unknow', 'connector']
_AXES = [None, 'H', 'V', 'X']
_DIRS = [None, 'up', 'down', 'left', 'right']
_SIDES = [set(), {'LEFT'}, {'TOP'}, {'TOP', 'RIGHT'}, {'BOTTOM'}]
_BBOXES = [None, [0, 0, 40, 20], [0, 0, 20, 40], [0, 0, 30, 30]]


@pytest.mark.parametrize("cn", _CLASSES)
def test_orientation_matches_legacy_rule(cn):
    for axis, d, sides, bb in itertools.product(_AXES, _DIRS, _SIDES,
                                                 _BBOXES):
        node = {'class_name': cn}
        if axis is not None:
            node['_axis'] = axis
        if d is not None:
            node['direction'] = d
        if bb is not None:
            node['bbox'] = bb
        assert orientation(node, sides) == _legacy_orientation(node, sides), \
            (node, sides)


def test_orientation_accepts_short_side_names():
    node = {'class_name': 'armatura_electro', 'bbox': [0, 0, 20, 40]}
    assert orientation(node, {'L'}) == ('H', 'HORIZONTAL')
    assert orientation(node, {'T'}) == ('V', 'VERTICAL_REVERSE')


# ── face_refs: порт клапана и сдвиг выгрузки из одного источника ────────────

def _shift(skin, emit, cross):
    """Сдвиг контрола выгрузкой (dx, dy) при поперечнике cross."""
    x, y = graph_to_fxml.apply_contact_offset(skin, emit, 0.0, 0.0, cross)
    return x, y


@pytest.mark.parametrize("cn", VALVES)
@pytest.mark.parametrize("axis", ['H', 'V'])
def test_port_is_minus_export_shift(cn, axis):
    rect = (100.0, 200.0, 142.0, 237.8) if axis == 'H' \
        else (100.0, 200.0, 137.8, 242.0)
    node = {'class_name': cn, '_axis': axis, 'bbox': list(rect)}
    skin = CLASS_NAME_TO_SKIN[cn][1]
    _ax, emit = orientation(node)
    refs = face_refs(node, rect)
    cx, cy = (rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0
    if axis == 'H':
        cross = rect[3] - rect[1]
        dx, dy = _shift(skin, emit, cross)
        assert set(refs) == {'L', 'R'}
        assert refs['L'] == refs['R'] == pytest.approx(cy - dy)
        assert dx == 0.0
    else:
        cross = rect[2] - rect[0]
        dx, dy = _shift(skin, emit, cross)
        assert set(refs) == {'T', 'B'}
        assert refs['T'] == refs['B'] == pytest.approx(cx - dx)
        assert dy == 0.0


def test_known_ports_in_canvas_px():
    """Числа, показанные заказчику 2026-09-22 (сдвиг от середины, px)."""
    def off(cn, axis, w, h):
        rect = (0.0, 0.0, w, h)
        node = {'class_name': cn, '_axis': axis}
        refs = face_refs(node, rect)
        if axis == 'H':
            return refs['L'] - h / 2.0
        return refs['T'] - w / 2.0

    assert off('armatura_electro', 'H', 42, 37.8) == pytest.approx(8.3916)
    assert off('armatura_electro', 'V', 37.8, 42) == pytest.approx(7.56)
    assert off('armatura_seroprivod', 'H', 28, 42) == pytest.approx(14.112)
    # predohran не разворачивается (VERTICAL): талия ЛЕВЕЕ центра
    assert off('predohran', 'V', 31.5, 42) == pytest.approx(-4.725)
    # симметричные клапаны — центр, но порт всё равно выверенный (жёсткий)
    assert off('armatura_ruchn', 'H', 42, 21) == 0.0
    assert off('klapan_obratn', 'V', 21, 42) == 0.0
    assert off('armatura_membr_electro', 'H', 42, 37.8) == 0.0


def test_non_valves_have_no_ports():
    rect = (0.0, 0.0, 45.0, 45.0)
    for cn in ('nasos', 'teploobmen', 'datchik', 'output', 'unknow',
               'connector'):
        assert face_refs({'class_name': cn, '_axis': 'H'}, rect) is None
    assert face_refs({'class_name': 'armatura_electro', '_axis': 'H'},
                     None) is None
    assert face_refs({'class_name': 'armatura_electro', '_axis': 'H'},
                     (0.0, 0.0, 0.0, 10.0)) is None


def test_raster_valve_without_axis_has_no_port():
    """Узел растра («Проверка схемы») — без `_axis`: бокс детекции, талия
    к нему не приложима — середина, как прежде."""
    rect = (0.0, 0.0, 42.0, 37.8)
    assert face_refs({'class_name': 'armatura_electro'}, rect) is None
    assert face_ref({'class_name': 'armatura_electro'}, rect, 'L') \
        == (18.9, False)


def test_face_ref_hard_only_on_axis_faces():
    rect = (0.0, 0.0, 42.0, 37.8)
    node = {'class_name': 'armatura_electro', '_axis': 'H'}
    y, hard = face_ref(node, rect, 'L')
    assert hard and y == pytest.approx(18.9 + 8.3916)
    # неосевая грань клапана — середина, уступчивая
    assert face_ref(node, rect, 'T') == (21.0, False)
    assert face_ref({'class_name': 'nasos'}, rect, 'R') == (18.9, False)


def test_table_sign_convention_documented():
    """h — талия НИЖЕ центра, v — REVERSE правее / VERTICAL левее: у всех
    доли положительны (иначе знаки в face_refs врут)."""
    assert all(h >= 0 and v >= 0 for h, v in SKIN_CONTACT_OFFSET.values())


# ───────── геометрия талии как функции ядра (блок 1, К1) ─────────
# Литералы по оси H — armatura_electro (0.222, таблица Р6 его не меняет);
# по оси V — ELECTRIC_VLV v=0.20: литерал ПЕРЕСНИМАЕТСЯ в блоке 2 (Р6: 0.222).

def test_waist_offset_in_canvas_px():
    node = {'class_name': 'armatura_electro'}           # оси на узле нет
    assert contact.waist_offset(node, 'H', 42, 28) == pytest.approx(6.216)
    # REVERSE-класс по V: талия ПРАВЕЕ центра (блок 2 переснимет: 6.216)
    assert contact.waist_offset(node, 'V', 28, 42) == pytest.approx(5.6)
    assert 'axis' not in node and '_axis' not in node   # узел не тронут
    assert contact.waist_offset({'class_name': 'armatura_ruchn'},
                                'H', 42, 21) == 0.0
    assert contact.waist_offset({'class_name': 'nasos'}, 'H', 45, 45) == 0.0


def test_waist_centroid_is_waist_for_canvas_valve_only():
    bb = [479.0, 286.0, 521.0, 314.0]                   # 42x28
    valve = {'class_name': 'armatura_electro', '_axis': 'H'}
    assert contact.waist_centroid(valve, bb) == [pytest.approx(306.216),
                                                 500.0]
    # растр (без _axis), не клапан, симметричный клапан — центр рамки
    assert contact.waist_centroid({'class_name': 'armatura_electro'}, bb) \
        == [300.0, 500.0]
    assert contact.waist_centroid({'class_name': 'unknow'}, bb) \
        == [300.0, 500.0]
    assert contact.waist_centroid({'class_name': 'armatura_ruchn',
                                   '_axis': 'H'}, bb) == [300.0, 500.0]
    vv = {'class_name': 'armatura_electro', '_axis': 'V'}
    assert contact.waist_centroid(vv, [485.4, 279.0, 513.4, 321.0]) \
        == [300.0, pytest.approx(505.0)]


def test_box_around_waist_is_inverse_of_waist_centroid():
    valve = {'class_name': 'armatura_electro', '_axis': 'H'}
    assert contact.box_around_waist(valve, [306.216, 500.0], 42, 28) \
        == pytest.approx([479.0, 286.0, 521.0, 314.0])
    vv = {'class_name': 'armatura_electro', '_axis': 'V'}
    assert contact.box_around_waist(vv, [300.0, 505.0], 28, 42) \
        == pytest.approx([485.4, 279.0, 513.4, 321.0])
    # «Размеры»: другой размер вокруг той же талии — талия на месте
    for node, c in ((valve, [306.216, 500.0]), (vv, [300.0, 505.0])):
        for w, h in ((42, 63), (63, 42), (30, 30)):
            bb = contact.box_around_waist(node, c, w, h)
            assert bb[2] - bb[0] == pytest.approx(w)
            assert bb[3] - bb[1] == pytest.approx(h)
            assert contact.waist_centroid(node, bb) == pytest.approx(c)
    # не клапан / растр — рамка вокруг центроида
    for node in ({'class_name': 'unknow'},
                 {'class_name': 'armatura_electro'}):
        assert contact.box_around_waist(node, [300.0, 500.0], 42, 28) \
            == [479.0, 286.0, 521.0, 314.0]

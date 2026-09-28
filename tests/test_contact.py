# -*- coding: utf-8 -*-
"""contact — одна модель точек прикрепления для посадки и выгрузки.

Держит три обещания модуля:
  * таблицы скинов переехали из `graph_to_fxml` без изменений (реэкспорт —
    те же объекты, значит и экспортёр, и ядро видят одно);
  * `orientation` — дословно прежнее правило выгрузки
    (`canvas_to_fxml._axis_and_orientation` + «датчик всегда H»);
  * порт клапана и сдвиг контрола в выгрузке — одно соглашение о знаке:
    порт минус центр рамки == минус сдвиг `apply_contact_offset` при той
    же таблице;
  * таблица талий — замер на рабочем размере (Р6, `tools/skin_waist_probe.py`),
    растровая выгрузка — на замороженной `SKIN_CONTACT_OFFSET_LEGACY`
    (побайтово deploy 238fe6b).
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
    SKIN_CONTACT_OFFSET_LEGACY,
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
    "SKIN_CONTACT_OFFSET", "SKIN_CONTACT_OFFSET_LEGACY",
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

def _shift(skin, emit, cross, table=None):
    """Сдвиг контрола выгрузкой (dx, dy) при поперечнике cross."""
    x, y = graph_to_fxml.apply_contact_offset(skin, emit, 0.0, 0.0, cross,
                                              table=table)
    return x, y


@pytest.mark.parametrize("cn", VALVES)
@pytest.mark.parametrize("axis", ['H', 'V'])
def test_port_is_minus_export_shift(cn, axis):
    """Знак порта и сдвига выгрузки согласованы — при ОДНОЙ таблице
    (новой; растровый путь по умолчанию берёт LEGACY, см. ниже)."""
    rect = (100.0, 200.0, 142.0, 237.8) if axis == 'H' \
        else (100.0, 200.0, 137.8, 242.0)
    node = {'class_name': cn, '_axis': axis, 'bbox': list(rect)}
    skin = CLASS_NAME_TO_SKIN[cn][1]
    _ax, emit = orientation(node)
    refs = face_refs(node, rect)
    cx, cy = (rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0
    if axis == 'H':
        cross = rect[3] - rect[1]
        dx, dy = _shift(skin, emit, cross, SKIN_CONTACT_OFFSET)
        assert set(refs) == {'L', 'R'}
        assert refs['L'] == refs['R'] == pytest.approx(cy - dy)
        assert dx == 0.0
    else:
        cross = rect[2] - rect[0]
        dx, dy = _shift(skin, emit, cross, SKIN_CONTACT_OFFSET)
        assert set(refs) == {'T', 'B'}
        assert refs['T'] == refs['B'] == pytest.approx(cx - dx)
        assert dy == 0.0


# Старая таблица deploy 238fe6b (graph_to_fxml.py:223-232) — литералом:
# замороженная константа не вычисляется из проверяемой.
_LEGACY_238FE6B = {
    'ELECTRIC_VLV':     (0.222, 0.20),
    'AOC_VLV':          (0.336, 0.336),
    'AOC_VLV_V2_NPP':   (0.20,  0.20),
    'CHECK_HYDRO_VLV':  (0.15,  0.15),
    'CTRL_VLV_ELEC':    (0.15,  0.15),
    'RELIEF_VLV':       (0.15,  0.15),
    'CTRL_VLV_HANDLE':  (0.10,  0.111),
    'AOC_CTRL_VLV':     (0.15,  0.111),
}


def test_legacy_table_is_frozen_238fe6b():
    assert SKIN_CONTACT_OFFSET_LEGACY == _LEGACY_238FE6B
    assert list(SKIN_CONTACT_OFFSET_LEGACY) == list(_LEGACY_238FE6B)


def test_raster_export_shift_uses_legacy_table():
    """Р6: растровый `graph_to_fxml` (рамка детекции) — на старой таблице,
    растровый FXML не меняется. CTRL_VLV_ELEC H при высоте 37.8 едет на
    0.15·37.8 = 5.67 px (новая доля дала бы 11.8692); у ELECTROMAGNETIC
    строки в старой таблице нет — сдвига нет (новая дала бы 5.6322)."""
    assert _shift('CTRL_VLV_ELEC', 'HORIZONTAL', 37.8) \
        == (0.0, pytest.approx(-5.67))
    assert _shift('ELECTROMAGNETIC_VLV', 'HORIZONTAL', 37.8) == (0.0, 0.0)
    assert _shift('CHECK_HYDRO_VLV', 'VERTICAL_REVERSE', 37.8) \
        == (pytest.approx(-5.67), 0.0)
    assert _shift('ELECTRIC_VLV', 'VERTICAL', 37.8) \
        == (pytest.approx(7.56), 0.0)
    for skin in set(SKIN_CONTACT_OFFSET) | set(SKIN_CONTACT_OFFSET_LEGACY):
        for emit in ('HORIZONTAL', 'VERTICAL', 'VERTICAL_REVERSE'):
            assert _shift(skin, emit, 37.8) \
                == _shift(skin, emit, 37.8, _LEGACY_238FE6B), (skin, emit)


def _port_off(cn, axis, w, h):
    """Сдвиг порта клапана холста от середины грани, px (+ ниже / правее)."""
    rect = (0.0, 0.0, w, h)
    refs = face_refs({'class_name': cn, '_axis': axis}, rect)
    if axis == 'H':
        assert refs['L'] == refs['R']
        return refs['L'] - h / 2.0
    assert refs['T'] == refs['B']
    return refs['T'] - w / 2.0


# (class_name, ось, W, H рабочей рамки) -> сдвиг порта, px, по таблице Р6.
# V у REVERSE-классов — ПРАВЕЕ центра (+), у predohran (VERTICAL) — левее.
_PORTS_P6 = [
    ('armatura_electro', 'H', 42, 37.8, 8.3916),
    ('armatura_electro', 'V', 37.8, 42, 8.3916),         # было 7.56 (v 0.20)
    ('armatura_membr_electro', 'H', 42, 37.8, 5.6322),   # было 0.0
    ('armatura_membr_electro', 'V', 37.8, 42, 5.6322),
    ('regulator_electro', 'H', 42, 37.8, 11.8692),       # было 5.67
    ('regulator_electro', 'V', 37.8, 42, 11.8692),
    ('regulator_ruchn', 'H', 42, 37.8, 4.9518),          # было 3.78
    ('regulator_ruchn', 'V', 37.8, 42, 4.9518),          # было 4.1958
    ('klapan_obratn_seroprivod', 'H', 42, 37.8, -1.5498),  # талия ВЫШЕ центра
    ('klapan_obratn_seroprivod', 'V', 37.8, 42, -1.5498),  # REVERSE: ЛЕВЕЕ
    ('regulator_seroprivod', 'H', 42, 31.5, 8.064),      # было 4.725
    ('regulator_seroprivod', 'V', 31.5, 42, 8.064),      # было 3.4965
    ('armatura_seroprivod', 'H', 28, 42, 14.322),        # было 14.112
    ('armatura_seroprivod', 'V', 42, 28, 14.322),
    ('predohran', 'H', 42, 31.5, 5.2605),                # было 4.725
    ('predohran', 'V', 31.5, 42, -5.2605),               # VERTICAL: левее
    ('armatura_ruchn', 'H', 42, 21, 0.0),
    ('armatura_ruchn', 'V', 21, 42, 0.0),
    ('klapan_obratn', 'H', 42, 21, 0.0),
    ('klapan_obratn', 'V', 21, 42, 0.0),
]


@pytest.mark.parametrize("cn,axis,w,h,px", _PORTS_P6,
                         ids=[f"{c}-{a}" for c, a, *_ in _PORTS_P6])
def test_known_ports_in_canvas_px(cn, axis, w, h, px):
    """Порт клапана на рабочей рамке, px от середины грани (Р6, 2026-09-28).
    Порт выверенный (жёсткий) и у симметричных клапанов."""
    assert _port_off(cn, axis, w, h) == pytest.approx(px, abs=1e-9)
    assert face_ref({'class_name': cn, '_axis': axis}, (0.0, 0.0, w, h),
                    'L' if axis == 'H' else 'T')[1] is True


def test_every_valve_skin_has_a_port_row():
    """Контракт таблицы по всем скинам клапанов: 8 несимметричных — строка,
    симметричные HANDLE/CHECK — без строки (центр); мёртвой строки нет
    (AOC_VLV_V2_NPP — только в LEGACY)."""
    skins = {CLASS_NAME_TO_SKIN[c][1] for c in VALVES}
    assert set(SKIN_CONTACT_OFFSET) == skins - {'HANDLE_VLV', 'CHECK_VLV'}
    assert {c for c, *_ in _PORTS_P6} == set(VALVES)


# Замер JavaFX-рендером на рабочем размере (tools/skin_waist_probe.py,
# MEASUREMENTS §130.2), px: таблица (3 знака доли) обязана попадать в
# талию скина с допуском 0.05 px.
_MEASURED_PX = [
    ('armatura_electro', 'H', 42, 37.8, 8.400),
    ('armatura_membr_electro', 'H', 42, 37.8, 5.640),
    ('regulator_electro', 'H', 42, 37.8, 11.880),
    ('regulator_ruchn', 'H', 42, 37.8, 4.968),
    ('klapan_obratn_seroprivod', 'H', 42, 37.8, -1.540),
    ('regulator_seroprivod', 'H', 42, 31.5, 8.068),
    ('armatura_seroprivod', 'H', 28, 42, 14.318),
    ('predohran', 'V', 31.5, 42, -5.250),
]


@pytest.mark.parametrize("cn,axis,w,h,px", _MEASURED_PX,
                         ids=[c for c, *_ in _MEASURED_PX])
def test_ports_hit_measured_waist(cn, axis, w, h, px):
    assert abs(_port_off(cn, axis, w, h) - px) <= 0.05


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
    """h — талия НИЖЕ центра, v — REVERSE правее / VERTICAL левее. Доля
    ЗНАКОВАЯ: у CHECK_HYDRO_VLV (klapan_obratn_seroprivod) талия ВЫШЕ
    центра — h < 0 и v < 0; у прочих строк обе доли > 0, h == v (замер
    Р6 одинаков во всех ориентациях)."""
    for skin, (h, v) in SKIN_CONTACT_OFFSET.items():
        assert h == v, skin
        if skin == 'CHECK_HYDRO_VLV':
            assert h < 0 and v < 0
        else:
            assert h > 0 and v > 0, skin
    assert SKIN_CONTACT_OFFSET['ELECTROMAGNETIC_VLV'] == (0.149, 0.149)


# ───────── геометрия талии как функции ядра (блок 1, К1) ─────────
# Литералы — armatura_electro. По оси H доля 0.222 (Р6 её не меняет); по оси
# V литералы пересняты в блоке 2 (Р6: v 0.20 → 0.222, на поперечнике 28 талия
# правее центра на 6.216 px вместо 5.6; рамка V сдвинута на 0.616 px, чтобы
# талия осталась на x=505).

def test_waist_offset_in_canvas_px():
    node = {'class_name': 'armatura_electro'}           # оси на узле нет
    assert contact.waist_offset(node, 'H', 42, 28) == pytest.approx(6.216)
    # REVERSE-класс по V: талия ПРАВЕЕ центра (Р6: было 5.6)
    assert contact.waist_offset(node, 'V', 28, 42) == pytest.approx(6.216)
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
    assert contact.waist_centroid(vv, [484.784, 279.0, 512.784, 321.0]) \
        == [300.0, pytest.approx(505.0)]


def test_box_around_waist_is_inverse_of_waist_centroid():
    valve = {'class_name': 'armatura_electro', '_axis': 'H'}
    assert contact.box_around_waist(valve, [306.216, 500.0], 42, 28) \
        == pytest.approx([479.0, 286.0, 521.0, 314.0])
    vv = {'class_name': 'armatura_electro', '_axis': 'V'}
    assert contact.box_around_waist(vv, [300.0, 505.0], 28, 42) \
        == pytest.approx([484.784, 279.0, 512.784, 321.0])
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

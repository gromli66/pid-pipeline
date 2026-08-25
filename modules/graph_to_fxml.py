#!/usr/bin/env python3
"""
P&ID Graph to FXML Converter v2

python graph_to_fxml_v2.py input.json -o output.fxml

Конвертирует JSON-граф P&ID в FXML разметку для SceneBuilder.
Поддерживает:
- Скины из библиотеки custom-control-skin-based-1_3_0
- Привязку KKS к контролам (kks, kksVisible)
- Масштабирование толщины линий по diameter_value
- Простые Rectangle для элементов без скинов
- Линии соединений между элементами
- Автоматическое определение ориентации (HORIZONTAL/VERTICAL)
- Позиционирование по точкам подключения рёбер

ВАЖНО: Формат координат в JSON:
- bbox: [x1, y1, x2, y2]
- centroid: [y, x]
- source_point/target_point: [y, x]
- image_size: [height, width]

Изменения v2 (по сравнению с v1):
- Маппинг по class_name вместо class_id (надёжнее, всегда присутствует)
- Старый маппинг CLASS_TO_SKIN по class_id сохранён как fallback
- Добавлена привязка KKS: kks, kksVisible, kksFontSize, kksTextOffset
- valueVisible="false" только для DetectorControl (убирает артефакт "0")
- Единый цвет линий (#333333)
- Масштабирование strokeWidth по diameter_value
- Диаметр добавлен в комментарий к каждой линии
"""

import json
import math
import argparse
from pathlib import Path
from dataclasses import dataclass
from typing import NamedTuple, Optional
from xml.sax.saxutils import escape

# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================

# ---------------------------------------------------------------------------
# НОВЫЙ маппинг class_name -> (ControlClass, SkinType, [EquipmentType])
#
# Проверено по:
#   - ControlSkinType.class  (ValveControl)
#   - PumpSkinType.class     (PumpControl)
#   - HeaterControlSkinType  (HeaterControl)
#   - DetectorSkinType       (DetectorControl)
#   - FunctionControlSkinType(FunctionControl)
#   - ButtonSkinType         (ButtonControl)
#   - IndicationSkinType     (FunctionControl с indication)
# ---------------------------------------------------------------------------
#
# 3-й элемент tuple — equipmentType (дефолт по типу оборудования):
#   MOV — электрическая задвижка
#   VLV — ручная задвижка / ручной регулятор
#   PMP — насос / вентилятор
#   CNT — регулятор (с приводом)
#   XMA — датчик
# Для классов без явного указания equipmentType не ставится.
# ---------------------------------------------------------------------------
CLASS_NAME_TO_SKIN = {
    # --- Арматура / задвижки ---
    'armatura_ruchn':       ('ValveControl', 'HANDLE_VLV', 'VLV'),
    'armatura_electro':     ('ValveControl', 'ELECTRIC_VLV', 'MOV'),
    'armatura_seroprivod':  ('ValveControl', 'AOC_VLV'),
    'armatura_membr_electro': ('ValveControl', 'ELECTROMAGNETIC_VLV'),

    # --- Клапаны ---
    'klapan_obratn':        ('ValveControl', 'CHECK_VLV'),
    'klapan_obratn_seroprivod': ('ValveControl', 'CHECK_HYDRO_VLV'),

    # --- Регуляторы ---
    'regulator_electro':    ('ValveControl', 'CTRL_VLV_ELEC', 'CNT'),
    'regulator_ruchn':      ('ValveControl', 'CTRL_VLV_HANDLE', 'VLV'),
    'regulator_seroprivod': ('ValveControl', 'AOC_CTRL_VLV', 'CNT'),

    # drossel — НЕ маппим на скин, нет аналога в библиотеке

    # --- Предохранительный клапан ---
    'predohran':            ('ValveControl', 'RELIEF_VLV'),

    # --- Насос ---
    'nasos':                ('PumpControl', 'PUMP', 'PMP'),
    'vodostruiniy_nasos':   ('PumpControl', 'PUMP', 'PMP'),

    # --- Вентилятор ---
    'ventilaytor':          ('PumpControl', 'FAN', 'PMP'),

    # --- Теплообмен / нагрев ---
    'teploobmen':           ('HeaterControl', 'HEATER_VOL_TUBE'),  # теплообменник (кожухотрубный)
    'electronagrevat':      ('HeaterControl', 'HEATER'),           # электронагреватель
    # dearator — НЕ маппим на скин, используется Polygon из segmentation

    # --- Датчики ---
    # Param/Unit НЕ ставим: тип датчика (T/P/F/L) из графа не определить,
    # остаются библиотечные дефолты ("T:" / "°C")
    'datchik':              ('DetectorControl', 'MINSK', 'XMA'),

    # --- Расходомерная шайба ---
    'rashodomernaya_shaiba': ('FunctionControl', 'FLOWMETER'),

    # --- Фильтр ---
    'filtr_meh':            ('FunctionControl', 'FILTER'),

    # --- Переход (стрелка-портал) ---
    'perehod':              ('FunctionControl', 'ARROW'),

    # --- Стрелка ---
    'strelka':              ('FunctionControl', 'ARROW'),

    # --- Выход схемы (портал-переход) ---
    'output':               ('ButtonControl', 'TEXT_BUTTON', 'TRANSITION_BUTTON'),
}

# СТАРЫЙ маппинг class_id -> skin (fallback, если class_name не найден)
CLASS_ID_TO_SKIN = {
    0:  ('ValveControl', 'HANDLE_VLV', 'VLV'),
    1:  ('ValveControl', 'CHECK_VLV'),
    2:  ('ValveControl', 'CTRL_VLV_HANDLE', 'VLV'),
    3:  ('ValveControl', 'ELECTRIC_VLV', 'MOV'),
    4:  ('ValveControl', 'CTRL_VLV_ELEC', 'CNT'),
    6:  ('FunctionControl', 'ARROW'),
    7:  ('ValveControl', 'CHECK_HYDRO_VLV'),
    8:  ('ValveControl', 'AOC_VLV_V2_NPP'),
    10: ('ValveControl', 'ELECTROMAGNETIC_VLV'),
    11: ('PumpControl', 'PUMP', 'PMP'),
    12: ('PumpControl', 'FAN_NVART', 'PMP'),
    13: ('ValveControl', 'SAFETY_VLV_NPP'),
    15: ('FunctionControl', 'FLOWMETER'),
    17: ('HeaterControl', 'HEATER_VOL_TUBE'),
    22: ('FunctionControl', 'FILTER'),
    26: ('ValveControl', 'CTRL_VLV_REDUCING'),
    31: ('HeaterControl', 'HEATER'),
    32: ('ValveControl', 'STOP_CTRL_VLV'),
    33: ('DetectorControl', 'MINSK', 'XMA'),
    35: ('ButtonControl', 'TEXT_BUTTON', 'TRANSITION_BUTTON'),
}


# class_name'ы которые НЕ должны маппиться на скины
SKIP_CLASS_NAMES = {
    'connector', 'voronka', 'annotation', 'truba', 'background',
}

# Контролы, которые всегда рисуются HORIZONTAL,
# независимо от ориентации трубы (датчики)
FORCE_HORIZONTAL_CONTROLS = {'DetectorControl'}

# ---------------------------------------------------------------------------
# Направление потока (от direction-классификатора) → orientation скина.
#
# `orientation` у контролов библиотеки — это BlockOrientation с 4 значениями:
#   HORIZONTAL, HORIZONTAL_REVERSE, VERTICAL, VERTICAL_REVERSE.
# Дефолт отрисовки скинов в библиотеке: вертикаль = «вверх», горизонталь =
# «вправо». Отсюда соответствие 4 направлений классификатора:
#   up    → VERTICAL          (скин как нарисован по умолчанию)
#   down  → VERTICAL_REVERSE  (разворот на 180°)
#   right → HORIZONTAL        (скин как нарисован по умолчанию)
#   left  → HORIZONTAL_REVERSE
# Если при визуальной проверке REVERSE окажется перепутан — правится здесь,
# в одном месте, после чего FXML перегенерируется.
# ---------------------------------------------------------------------------
DIRECTION_TO_ORIENTATION = {
    'up':    'VERTICAL',
    'down':  'VERTICAL_REVERSE',
    'right': 'HORIZONTAL',
    'left':  'HORIZONTAL_REVERSE',
}

# Базовая ось направления — нужна для геометрии (swap width/height и пересчёт
# layout выполняется так же, как для обычной VERTICAL-ориентации).
_DIRECTION_TO_AXIS = {
    'up': 'VERTICAL', 'down': 'VERTICAL',
    'left': 'HORIZONTAL', 'right': 'HORIZONTAL',
}

# Классы оборудования, чей скин разворачиваем по направлению классификатора.
# (Совпадает с direction_classification.classes минус napravlenie, который
#  рисуется треугольником, и strelka, исключённой из графа.)
DIRECTION_ORIENT_CLASSES = {
    'nasos', 'rashodomernaya_shaiba',
}

# Классы арматуры/регуляторов/клапанов, которые в вертикальном положении рисуем
# развёрнутыми на 180° (VERTICAL_REVERSE вместо VERTICAL). Библиотечный дефолт
# вертикали для них — «вниз», а на схемах нужен разворот. Ось (geometry.orientation)
# и swap/layout не меняются: REVERSE лишь зеркалит графику, из-за чего талия
# уезжает на 1-waist (учитывается в fxml_standardize) и знак contact-поправки
# инвертируется (apply_contact_offset). predohran (RELIEF_VLV) намеренно НЕ входит.
REVERSE_VERTICAL_CLASSES = {
    'armatura_ruchn', 'klapan_obratn', 'armatura_membr_electro',
    'regulator_ruchn', 'regulator_electro', 'armatura_electro',
    'klapan_obratn_seroprivod', 'regulator_seroprivod', 'armatura_seroprivod',
}

# Класс «направление» — рисуется треугольником по направлению потока,
# а не библиотечным скином.
NAPRAVLENIE_CLASS_NAME = 'napravlenie'
NAPRAVLENIE_COLOR = '#E1BEE7'

# ---------------------------------------------------------------------------
# Поправка оси контакта скинов.
#
# У части скинов визуальная точка контакта (талия «бабочки») не совпадает
# с центром бокса контрола, т.к. сверху в канве скина нарисован привод
# (мотор/мембрана). Геометрия вычислена из fxml-ресурсов внутри jar
# (напр. electricVlv.fxml: канва 100x90, мотор 40px + тело 50px,
# талия на 65/90 = 0.72 высоты) и сверена по скриншотам SceneBuilder.
#
# Формат: skin -> (h_frac, v_frac) — доля ПОПЕРЕЧНОГО размера контрола:
#   h_frac: HORIZONTAL — талия НИЖЕ центра на h_frac*height
#           → поправка layoutY -= h_frac * height
#   v_frac: VERTICAL — после rotate(90) талия ЛЕВЕЕ центра на v_frac*height
#           → поправка layoutX += v_frac * height
# Значения подбираются по калибровочному листу (skin_calibration.fxml).
# ---------------------------------------------------------------------------
# Значения откалиброваны вручную по skin_calibration_v3_ladder.fxml
SKIN_CONTACT_OFFSET = {
    'ELECTRIC_VLV':     (0.222, 0.20),
    'AOC_VLV':          (0.336, 0.336),
    'AOC_VLV_V2_NPP':   (0.20,  0.20),
    'CHECK_HYDRO_VLV':  (0.15,  0.15),
    'CTRL_VLV_ELEC':    (0.15,  0.15),
    'RELIEF_VLV':       (0.15,  0.15),
    'CTRL_VLV_HANDLE':  (0.10,  0.111),
    'AOC_CTRL_VLV':     (0.15,  0.111),
}


def apply_contact_offset(skin_type, orientation, layout_x, layout_y, height):
    """Сдвигает layout так, чтобы ось контакта скина легла на трубу."""
    offs = SKIN_CONTACT_OFFSET.get(skin_type)
    if not offs:
        return layout_x, layout_y
    h_frac, v_frac = offs
    if orientation.startswith('HORIZONTAL'):
        # Reverse по горизонтали зеркалит вдоль оси трубы, поперечное (h_frac) смещение талии не меняется.
        layout_y -= h_frac * height
    elif orientation == 'VERTICAL_REVERSE':
        # Разворот на 180°: талия уходит на противоположную сторону от центра.
        layout_x -= v_frac * height
    else:  # VERTICAL
        layout_x += v_frac * height
    return layout_x, layout_y


# Дефолтные Param/Unit датчиков
# Обычный датчик — датчик давления
DETECTOR_DEFAULT_PARAM = 'P'
DETECTOR_DEFAULT_UNIT = 'кПа'
# Дефолтный KKS датчика (если у узла нет kks_full)
DETECTOR_DEFAULT_KKS = 'fff'
# Авто-датчик у расходомерной шайбы — датчик расхода
FLOW_DETECTOR_PARAM = 'G'
FLOW_DETECTOR_UNIT = 'м3/ч'


def get_skin_info(node):
    """
    Определяет скин для узла. Приоритет: class_name → class_id → None.
    Возвращает tuple (ControlClass, SkinType, [EquipmentType]) или None.

    class_name из SKIP_CLASS_NAMES всегда возвращает None
    (даже если class_id попадает в CLASS_ID_TO_SKIN).
    """
    class_name = node.get('class_name', '')

    # Явный пропуск
    if class_name in SKIP_CLASS_NAMES:
        return None

    if class_name in CLASS_NAME_TO_SKIN:
        return CLASS_NAME_TO_SKIN[class_name]

    class_id = node.get('class_id', -1)
    if class_id in CLASS_ID_TO_SKIN:
        return CLASS_ID_TO_SKIN[class_id]

    return None


# Цвета для элементов без скинов (по class_name)
CLASS_COLORS = {
    'armatura_ruchn': '#FF6B6B',
    'klapan_obratn': '#45B7D1',
    'regulator_ruchn': '#96CEB4',
    'armatura_electro': '#4ECDC4',
    'regulator_electro': '#45B7AA',
    'drossel': '#DDA0DD',
    'perehod': '#FFEAA7',
    'nasos': '#85C1E9',
    'ventilaytor': '#82E0AA',
    'predohran': '#F1948A',
    'condensatootvod': '#D7BDE2',
    'rashodomernaya_shaiba': '#A9CCE3',
    'teploobmen': '#FAD7A0',
    'zaglushka': '#D5DBDB',
    'gidrozatvor': '#AED6F1',
    'bak': '#F9E79F',
    'voronka': '#FADBD8',
    'filtr_meh': '#D5F5E3',
    'separator': '#FCF3CF',
    'dearator': '#FEF9E7',
    'electronagrevat': '#FFCCBC',
    'smotrowoe_steclo': '#B2EBF2',
    'datchik': '#C8E6C9',
    'output': '#FFE0B2',
    'strelka': '#E1BEE7',
    'napravlenie': NAPRAVLENIE_COLOR,
    'unknow': '#CCCCCC',
    'klapan_obratn_seroprivod': '#45B7D1',
    'armatura_seroprivod': '#4ECDC4',
    'regulator_seroprivod': '#45B7AA',
    'armatura_membr_electro': '#4ECDC4',
    'vodostruiniy_nasos': '#85C1E9',
    'kapleulov': '#D7BDE2',
    'celindr_turb': '#AED6F1',
    'redukcion_ustr': '#96CEB4',
    'bistro_redukc_ustr': '#96CEB4',
    'separator_paro': '#FCF3CF',
    'silfonnii_kompensator': '#D5DBDB',
    'annotation': '#EEEEEE',
    'truba': '#BBBBBB',
    'background': '#FFFFFF',
}

DEFAULT_COLOR = '#AAAAAA'

# Фон подложки (style AnchorPane)
PANE_BACKGROUND = 'linear-gradient(to bottom right, #d4d4d4, #d4d4d4)'

# Цвет линий (единый для всех)
DEFAULT_LINE_COLOR = '#333333'
LINE_STROKE_WIDTH = 2.0

# ---------------------------------------------------------------------------
# Масштабирование толщины линий по diameter_value
# ---------------------------------------------------------------------------
# Формула: stroke = base * (diameter / DIAMETER_REFERENCE)
# Clamp: [DIAMETER_STROKE_MIN .. DIAMETER_STROKE_MAX]
DIAMETER_REFERENCE = 50.0    # Dv50 → base_stroke
DIAMETER_STROKE_MIN = 0.5
DIAMETER_STROKE_MAX = 12.0

# Размеры страниц в мм (landscape)
PAGE_SIZES_MM = {
    'A4': (297, 210),
    'A3': (420, 297),
    'A2': (594, 420),
    'A1': (841, 594),
    'A0': (1189, 841),
}

# Пиксели на мм (72 DPI, стандарт JavaFX/PDF)
PX_PER_MM = 72 / 25.4  # ≈ 2.835

# Отступы в мм
PAGE_MARGIN_MM = 10

# ---------------------------------------------------------------------------
# Мост (bridge) — отрисовка как разрыв линии: — | —
# ---------------------------------------------------------------------------
# Мост определяется ГЕОМЕТРИЧЕСКИ: две трубы пересекаются, но узла в точке
# пересечения нет (если бы соединялись — там был бы узел). Это устойчиво к
# потере/порче поля color (раньше его затирала ручная покраска).
# В точке пересечения рвётся БОЛЕЕ ГОРИЗОНТАЛЬНАЯ труба, вертикальная проходит
# сверху через разрыв (символ «— | —»).
BRIDGE_GAP_STROKE_FACTOR = 3.0  # ширина разрыва ≈ k × strokeWidth верхней трубы
BRIDGE_GAP_MIN_MM = 2.0         # минимум разрыва (всегда виден на любом листе)
# Узел ближе этого расстояния к пересечению ⇒ это соединение (узел), не мост.
# В исходных пикселях; масштабируется вместе с листом (× graph_scale).
BRIDGE_NODE_CLEARANCE_PX = 15.0


# ============================================================================
# СТРУКТУРЫ ДАННЫХ
# ============================================================================

@dataclass
class Connection:
    """Подключение ребра к узлу"""
    edge_id: str
    role: str  # 'source' or 'target'
    side: str  # 'LEFT', 'RIGHT', 'TOP', 'BOTTOM'
    point: tuple  # (x, y) - уже преобразованные координаты
    rel_pos: float  # 0..1


@dataclass
class SkinGeometry:
    """Геометрия скина"""
    orientation: str  # 'HORIZONTAL' or 'VERTICAL'
    width: float
    height: float
    layout_x: float
    layout_y: float


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def convert_point(point):
    """
    Конвертирует точку из формата JSON [y, x] в (x, y).
    """
    if not point or len(point) != 2:
        return None
    return (point[1], point[0])  # [y, x] -> (x, y)


def parse_bbox(bbox):
    """
    Парсит bbox из формата [x1, y1, x2, y2] в словарь.
    ИСПРАВЛЕНО: bbox в JSON хранится как [x1, y1, x2, y2], а не [y1, x1, y2, x2]
    """
    if not bbox or len(bbox) != 4:
        return None
    x1, y1, x2, y2 = bbox
    return {
        'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2,
        'width': x2 - x1,
        'height': y2 - y1
    }


def determine_side(point_xy, bbox):
    """
    Определяет к какой стороне bbox ближе всего точка.
    Возвращает (side, rel_pos).
    point_xy: (x, y) - уже преобразованная точка
    """
    px, py = point_xy
    b = parse_bbox(bbox)
    if not b:
        return 'LEFT', 0.5

    distances = {
        'LEFT': abs(px - b['x1']),
        'RIGHT': abs(px - b['x2']),
        'TOP': abs(py - b['y1']),
        'BOTTOM': abs(py - b['y2'])
    }

    side = min(distances, key=distances.get)

    # Вычисляем относительную позицию на стенке
    if side in ('LEFT', 'RIGHT'):
        rel_pos = (py - b['y1']) / max(b['height'], 1)
    else:
        rel_pos = (px - b['x1']) / max(b['width'], 1)

    # Clamp to [0, 1]
    rel_pos = max(0.0, min(1.0, rel_pos))

    return side, rel_pos


def get_node_connections(node_id, edges, nodes):
    """
    Получает все подключения рёбер к узлу.
    """
    connections = []
    node = nodes.get(node_id)

    if not node or node['type'] != 'equipment' or not node.get('bbox'):
        return connections

    bbox = node['bbox']

    for edge in edges:
        for role, pt_key, id_key in [
            ('source', 'source_point', 'source'),
            ('target', 'target_point', 'target')
        ]:
            if edge[id_key] != node_id:
                continue

            pt_raw = edge.get(pt_key)
            if not pt_raw:
                continue

            # Конвертируем точку из [y, x] в (x, y)
            pt = convert_point(pt_raw)
            if not pt:
                continue

            side, rel_pos = determine_side(pt, bbox)

            connections.append(Connection(
                edge_id=edge['id'],
                role=role,
                side=side,
                point=pt,
                rel_pos=rel_pos
            ))

    return connections


def calculate_skin_geometry(node, connections) -> SkinGeometry:
    """
    Вычисляет геометрию скина на основе подключений рёбер.
    Ключевой принцип: стенки скина должны проходить через точки подключения.
    """
    bbox = parse_bbox(node.get('bbox'))

    if not bbox:
        # Fallback для узлов без bbox
        centroid_raw = node.get('centroid', [0, 0])
        centroid = convert_point(centroid_raw) or (0, 0)
        return SkinGeometry(
            orientation='HORIZONTAL',
            width=50, height=30,
            layout_x=centroid[0] - 25,
            layout_y=centroid[1] - 15
        )

    orig_width = bbox['width']
    orig_height = bbox['height']

    if not connections:
        # Нет подключений — используем оригинальные размеры и позицию
        return SkinGeometry(
            orientation='HORIZONTAL' if orig_width >= orig_height else 'VERTICAL',
            width=orig_width,
            height=orig_height,
            layout_x=bbox['x1'],
            layout_y=bbox['y1']
        )

    sides = {c.side for c in connections}

    # Определяем ориентацию по подключениям
    if 'LEFT' in sides or 'RIGHT' in sides:
        orientation = 'HORIZONTAL'
    else:
        orientation = 'VERTICAL'

    # Группируем подключения по сторонам (берём первое для каждой стороны)
    conns_by_side = {}
    for c in connections:
        if c.side not in conns_by_side:
            conns_by_side[c.side] = c

    # Вычисляем геометрию в зависимости от ориентации
    if orientation == 'HORIZONTAL':
        left_conn = conns_by_side.get('LEFT')
        right_conn = conns_by_side.get('RIGHT')

        if left_conn and right_conn:
            skin_width = right_conn.point[0] - left_conn.point[0]
            skin_height = orig_height
            layout_x = left_conn.point[0]
            layout_y = left_conn.point[1] - skin_height * left_conn.rel_pos
        elif left_conn:
            skin_width = orig_width
            skin_height = orig_height
            layout_x = left_conn.point[0]
            layout_y = left_conn.point[1] - skin_height * left_conn.rel_pos
        elif right_conn:
            skin_width = orig_width
            skin_height = orig_height
            layout_x = right_conn.point[0] - skin_width
            layout_y = right_conn.point[1] - skin_height * right_conn.rel_pos
        else:
            skin_width = orig_width
            skin_height = orig_height
            layout_x = bbox['x1']
            layout_y = bbox['y1']
    else:  # VERTICAL
        top_conn = conns_by_side.get('TOP')
        bottom_conn = conns_by_side.get('BOTTOM')

        if top_conn and bottom_conn:
            skin_height = bottom_conn.point[1] - top_conn.point[1]
            skin_width = orig_width
            layout_x = top_conn.point[0] - skin_width * top_conn.rel_pos
            layout_y = top_conn.point[1]
        elif top_conn:
            skin_width = orig_width
            skin_height = orig_height
            layout_x = top_conn.point[0] - skin_width * top_conn.rel_pos
            layout_y = top_conn.point[1]
        elif bottom_conn:
            skin_width = orig_width
            skin_height = orig_height
            layout_x = bottom_conn.point[0] - skin_width * bottom_conn.rel_pos
            layout_y = bottom_conn.point[1] - skin_height
        else:
            skin_width = orig_width
            skin_height = orig_height
            layout_x = bbox['x1']
            layout_y = bbox['y1']

    # Защита от отрицательных или нулевых размеров
    skin_width = max(1.0, skin_width)
    skin_height = max(1.0, skin_height)

    return SkinGeometry(
        orientation=orientation,
        width=skin_width,
        height=skin_height,
        layout_x=layout_x,
        layout_y=layout_y
    )


def get_line_endpoints(edge, nodes):
    """
    Вычисляет конечные точки линии для ребра.
    - Для equipment: использует source_point/target_point
    - Для connector: использует centroid

    Возвращает ((x1, y1), (x2, y2)) - уже преобразованные координаты
    """
    source_node = nodes.get(edge['source'])
    target_node = nodes.get(edge['target'])

    if not source_node or not target_node:
        return None

    # Начальная точка
    if source_node['type'] == 'equipment':
        start_raw = edge.get('source_point')
        if not start_raw:
            start_raw = source_node.get('centroid', [0, 0])
    else:
        start_raw = source_node.get('centroid', [0, 0])

    # Конечная точка
    if target_node['type'] == 'equipment':
        end_raw = edge.get('target_point')
        if not end_raw:
            end_raw = target_node.get('centroid', [0, 0])
    else:
        end_raw = target_node.get('centroid', [0, 0])

    # Конвертируем из [y, x] в (x, y)
    start = convert_point(start_raw) or (0, 0)
    end = convert_point(end_raw) or (0, 0)

    return start, end


def calculate_diameter_stroke(diameter_value, base_stroke, graph_scale=1.0):
    """
    Вычисляет толщину линии на основе diameter_value.

    Args:
        diameter_value: Значение диаметра (например 50 для Dv50)
        base_stroke: Базовая толщина линии
        graph_scale: Коэффициент масштабирования графа (1.0 = без масштабирования)

    Returns:
        Масштабированная толщина линии
    """
    if not diameter_value or diameter_value <= 0:
        return base_stroke

    scaled = base_stroke * (diameter_value / DIAMETER_REFERENCE)

    # MAX зависит от масштаба — не допустить слияния труб
    effective_max = max(DIAMETER_STROKE_MIN, DIAMETER_STROKE_MAX * graph_scale)

    return max(DIAMETER_STROKE_MIN, min(effective_max, scaled))


# ============================================================================
# АВТО-ДАТЧИК РАСХОДА ДЛЯ РАСХОДОМЕРНОЙ ШАЙБЫ
# ============================================================================

# Размер авто-датчика, если на схеме нет других датчиков (px до масштабирования)
FLOW_DETECTOR_DEFAULT_SIZE = 50.0
# Зазор между шайбой и датчиком = factor * высота датчика
FLOW_DETECTOR_GAP_FACTOR = 0.8
# Радиус поиска существующего датчика рядом с шайбой
# (factor * max(width, height) шайбы)
FLOW_DETECTOR_SEARCH_FACTOR = 1.0


def shaiba_has_detector(shaiba, nodes, adjacency):
    """
    True, если у расходомерной шайбы уже есть привязанный датчик:
    - прямое ребро к узлу class_name='datchik', либо
    - свободный датчик рядом: центроид в радиусе
      FLOW_DETECTOR_SEARCH_FACTOR от размера шайбы И датчик
      не привязан рёбрами ни к какому другому узлу.
    """
    for nb_id in adjacency.get(shaiba['id'], ()):
        nb = nodes.get(nb_id)
        if nb and nb.get('class_name') == 'datchik':
            return True

    b = parse_bbox(shaiba.get('bbox'))
    if not b:
        return False
    cx = (b['x1'] + b['x2']) / 2
    cy = (b['y1'] + b['y2']) / 2
    max_dist = max(b['width'], b['height']) * FLOW_DETECTOR_SEARCH_FACTOR

    for nid, n in nodes.items():
        if n.get('class_name') != 'datchik':
            continue
        # Датчик уже привязан к другому узлу — не считается
        if adjacency.get(nid):
            continue
        db = parse_bbox(n.get('bbox'))
        if not db:
            continue
        ncx = (db['x1'] + db['x2']) / 2
        ncy = (db['y1'] + db['y2']) / 2
        if ((ncx - cx) ** 2 + (ncy - cy) ** 2) ** 0.5 <= max_dist:
            return True
    return False


def generate_flow_detectors(nodes, edges, graph_scale=1.0):
    """
    Для каждой расходомерной шайбы без привязанного датчика генерирует
    DetectorControl (датчик расхода) и линию связи к шайбе.

    Размещение: над шайбой (горизонтальная труба) или справа
    (вертикальная труба). Возвращает (control_elements, line_elements).
    """
    adjacency = {}
    for e in edges:
        adjacency.setdefault(e['source'], []).append(e['target'])
        adjacency.setdefault(e['target'], []).append(e['source'])

    # Типовой размер датчика — среднее по существующим датчикам схемы
    det_sizes = []
    for n in nodes.values():
        if n.get('class_name') == 'datchik':
            db = parse_bbox(n.get('bbox'))
            if db:
                det_sizes.append((db['width'], db['height']))
    if det_sizes:
        det_w = sum(w for w, _ in det_sizes) / len(det_sizes)
        det_h = sum(h for _, h in det_sizes) / len(det_sizes)
    else:
        det_w = det_h = FLOW_DETECTOR_DEFAULT_SIZE * graph_scale

    # Авто-датчик шайбы тоже в 3 раза меньше оригинала
    det_w /= 3.0
    det_h /= 3.0

    line_stroke = max(0.3, 1.0 * graph_scale) if graph_scale < 1.0 else 1.0

    controls, lines = [], []
    for node_id, node in nodes.items():
        if node.get('class_name') != 'rashodomernaya_shaiba':
            continue
        b = parse_bbox(node.get('bbox'))
        if not b:
            continue
        if shaiba_has_detector(node, nodes, adjacency):
            continue

        cx = (b['x1'] + b['x2']) / 2
        cy = (b['y1'] + b['y2']) / 2
        gap = det_h * FLOW_DETECTOR_GAP_FACTOR

        # Ориентация трубы у шайбы — по сторонам подключения рёбер
        conns = get_node_connections(node_id, edges, nodes)
        sides = {c.side for c in conns}
        vertical_pipe = bool(sides) and not ({'LEFT', 'RIGHT'} & sides)

        if vertical_pipe:
            # Датчик справа от шайбы
            det_x = b['x2'] + gap
            det_y = cy - det_h / 2
            line_start = (b['x2'], cy)
        else:
            # Датчик над шайбой
            det_x = cx - det_w / 2
            det_y = b['y1'] - gap - det_h
            line_start = (cx, b['y1'])

        det_x = max(0.0, det_x)
        det_y = max(0.0, det_y)
        det_cx = det_x + det_w / 2
        det_cy = det_y + det_h / 2

        det_id = f"{node_id}_fm_det"
        comment = f'<!-- auto: датчик расхода для шайбы {node_id} -->'
        attrs = [
            f'fx:id="{escape(det_id)}"',
            f'layoutX="{det_x:.1f}"',
            f'layoutY="{det_y:.1f}"',
            f'prefWidth="{det_w:.1f}"',
            f'prefHeight="{det_h:.1f}"',
            'skinType="MINSK"',
            'orientation="HORIZONTAL"',
            'equipmentType="XMA"',
            f'param="{FLOW_DETECTOR_PARAM}"',
            f'unit="{FLOW_DETECTOR_UNIT}"',
            f'kks="{DETECTOR_DEFAULT_KKS}"',
            'kksVisible="true"',
            f'kksFontSize="{TEXT_STYLES["kks"].size:.1f}"',
            'kksTextOffset="1.0"',
            'valueVisible="false"',
        ]
        controls.append(f'        {comment}\n        <DetectorControl {" ".join(attrs)} />')

        line_attrs = [
            f'fx:id="{escape(det_id)}_link"',
            f'startX="{line_start[0]:.1f}"',
            f'startY="{line_start[1]:.1f}"',
            f'endX="{det_cx:.1f}"',
            f'endY="{det_cy:.1f}"',
            f'stroke="{DEFAULT_LINE_COLOR}"',
            f'strokeWidth="{line_stroke:.2f}"',
        ]
        lines.append(f'        {comment}\n        <Line {" ".join(line_attrs)} />')

    return controls, lines


# ============================================================================
# ГЕНЕРАЦИЯ FXML
# ============================================================================

# Порог «вертикальный» текст-блок: высота заметно больше ширины (не на чуть-чуть).
_TEXT_VERTICAL_RATIO = 1.3
# Кегль шрифта ≈ короткой стороне блока (перпендикуляр к направлению чтения).
_TEXT_FONT_RATIO = 0.9
_TEXT_COLOR = "#000000"


class TextStyle(NamedTuple):
    """Стиль подписи: семейство шрифта, кегль, жирность."""
    family: str
    size: float
    bold: bool


#: Стили подписей — ОДНА таблица на выгрузку и на экран (решение Максима
#: 2026-08-25 №3: кегль фиксированный, 18 для всех видов подписи).
#: ⛔ Кегль НЕ доля от размера бокса: доли `0.35·min(w,h)` и `0.4·min(w,h)`
#: давали на одном листе 5.1 и 7.2 (замер §MEFX3) — отсюда жалоба «на одном
#: листе подписи разного размера». Прежний кегль `<Text>` был 40.0, и 125
#: подписей корпуса из 158 не влезали в свою рамку; при 18 их 51.
#: ⚠ `family` — СЕМЕЙСТВО, а НЕ начертание: полное имя начертания в атрибуте
#: `name` («Tahoma Bold») JavaFX молча подменяет на System (баг JDK-8089450),
#: поэтому жирность уходит отдельным inline-стилем.
#: Границы охвата (редтим 2026-08-25): у скиновой KKS-подписи семейство и
#: начертание атрибутами не задаются вовсе — в файл уходит только кегль;
#: подпись диаметра в FXML не печатается совсем, её кегль читает редактор.
#: Редактор берёт отсюда ТОЛЬКО кегль — Tahoma в клиент не бандлим (решение №7).
TEXT_STYLES = {
    'text_block': TextStyle('Tahoma', 18.0, True),   # OCR-блок → <Text>
    'kks': TextStyle('Tahoma', 18.0, True),          # оборудование → kksFontSize
    'diameter': TextStyle('Tahoma', 18.0, True),     # диаметр — только экран
}
_TEXT_BOLD_STYLE = "-fx-font-weight: bold;"


def build_node_kks_map(graph_data: dict) -> dict:
    """KKS оборудования = текст блока, привязанного к узлу (graph.bindings).

    Единственный источник — bindings; node.kks_full НЕ используется.
    Возвращает {node_id: text} (только непустой текст).
    """
    kks_map = {}
    for b in (graph_data.get('bindings') or []):
        if b.get('kind') == 'edge':
            continue
        nid = b.get('node_id')
        if not nid:
            continue
        t = (b.get('text') or '').strip()
        if t:
            kks_map[nid] = t
    return kks_map


def generate_fxml_text(block: dict):
    """Непривязанный OCR-блок → <Text>.

    Сам блок остаётся на месте (bbox из редактора не меняется) — задаётся только
    раскладка текста внутри bbox:
      • горизонтальный блок — текст как есть (слева направо);
      • вертикальный (height > width * _TEXT_VERTICAL_RATIO) — текст повёрнут на
        90° влево (CCW), читается снизу-вверх, внутри того же bbox.
    Кегль и шрифт — из `TEXT_STYLES['text_block']`, цвет чёрный.

    ⭐ Выравнивание отдаёт ФОРМАТ, а не расчёт (решение Максима 2026-08-25,
    вариант Б): `layoutX` — край рамки, `wrappingWidth` — её ширина, дальше
    работает `textAlignment="CENTER"`. Раньше координата считалась из ОЦЕНКИ
    длины строки (`len(text) * size * 0.55`), калиброванной под System: у
    другого семейства символ шире, и подпись уезжала по X тем сильнее, чем
    длиннее строка. ⚠ `textAlignment` стоял в выгрузке и раньше, но для
    однострочного `<Text>` без `wrappingWidth` он мёртв — потому и жила оценка.
    """
    bbox = block.get('bbox')
    if not bbox or len(bbox) != 4:
        return None
    text = (block.get('text') or '').strip()
    if not text:
        return None
    x1, y1, x2, y2 = [float(v) for v in bbox]
    w = max(1.0, x2 - x1)
    h = max(1.0, y2 - y1)
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0

    vertical = h > w * _TEXT_VERTICAL_RATIO
    style = TEXT_STYLES['text_block']
    font_size = style.size
    esc = escape(text, {'"': '&quot;', "'": '&apos;'})

    if vertical:
        # Поворот 90° влево (angle=-90) вокруг локальной точки (0,0):
        # локальная (px,py) → (py,-px). Повёрнутая строка занимает по x толщину
        # [layout_x .. layout_x+F], по y длину [layout_y-W .. layout_y], где W —
        # `wrappingWidth`. Берём W = высоте рамки и сажаем нижний конец на её
        # нижнюю грань: строка ложится ровно вдоль рамки и центрируется в ней
        # средствами формата. Толщина строки центрируется кеглем, не оценкой.
        layout_x = cx - font_size / 2.0
        layout_y = y2
        wrapping = h
    else:
        # Горизонтальный: строка занимает всю ширину рамки от её левого края,
        # центрируется внутри неё; по Y — центр рамки минус половина кегля.
        layout_x = x1
        layout_y = cy - font_size / 2.0
        wrapping = w

    head = (f'        <Text layoutX="{max(0.0, layout_x):.1f}"'
            f' layoutY="{max(0.0, layout_y):.1f}"'
            f' wrappingWidth="{wrapping:.1f}" text="{esc}" fill="{_TEXT_COLOR}"'
            f' textAlignment="CENTER" textOrigin="TOP"')
    if style.bold:
        head += f' style="{_TEXT_BOLD_STYLE}"'
    lines = [
        head + '>',
        f'            <font><Font name="{style.family}" size="{font_size:.1f}"/></font>',
    ]
    if vertical:
        lines.append('            <transforms><Rotate angle="-90.0" pivotX="0.0" pivotY="0.0"/></transforms>')
    lines.append('        </Text>')
    return "\n".join(lines)


def generate_fxml_control(node, geometry: SkinGeometry, node_id: str,
                          graph_scale: float = 1.0, kks: str = None) -> str:
    """
    Генерирует FXML элемент для скина.
    Добавляет KKS если доступен.
    """
    skin_info = get_skin_info(node)

    if not skin_info:
        return None

    control_class = skin_info[0]
    skin_type = skin_info[1]
    equipment_type = skin_info[2] if len(skin_info) > 2 else None

    # --- WYSIWYG: узел прошёл pretransform (фикс-размер в холсте 1920x1080) ---
    # Размер берём строго из bbox, а не из calculate_skin_geometry, которая
    # растягивает скин между точками подключения — иначе редактор и SceneBuilder
    # показывают разное. Ось задал pretransform в node['_axis']; ветки ниже
    # (FORCE_HORIZONTAL, DIRECTION, REVERSE, VERTICAL-swap) отрабатывают поверх
    # неё как обычно — swap на VERTICAL как раз вернёт канонический размер в
    # H-рамке, которую ждёт OrientationService.
    is_canvas = node.get('_axis') in ('H', 'V')
    if is_canvas:
        canvas_bbox = parse_bbox(node.get('bbox'))
        if canvas_bbox:
            geometry = SkinGeometry(
                orientation='VERTICAL' if node['_axis'] == 'V' else 'HORIZONTAL',
                width=canvas_bbox['width'],
                height=canvas_bbox['height'],
                layout_x=canvas_bbox['x1'],
                layout_y=canvas_bbox['y1'],
            )
        else:
            is_canvas = False   # нет bbox — эмитим по-старому

    # Датчик всегда горизонтально, независимо от направления трубы.
    # Геометрия — по исходному bbox (без растяжки между точками подключения).
    if control_class in FORCE_HORIZONTAL_CONTROLS and geometry.orientation == 'VERTICAL':
        bbox = parse_bbox(node.get('bbox'))
        if bbox:
            geometry = SkinGeometry(
                orientation='HORIZONTAL',
                width=bbox['width'],
                height=bbox['height'],
                layout_x=bbox['x1'],
                layout_y=bbox['y1'],
            )
        else:
            geometry = SkinGeometry(
                orientation='HORIZONTAL',
                width=geometry.height,
                height=geometry.width,
                layout_x=geometry.layout_x,
                layout_y=geometry.layout_y,
            )

    # --- Переопределение ориентации по направлению классификатора ---
    # Для nasos/rashodomernaya_shaiba направление от классификатора важнее
    # геометрии рёбер: оно задаёт и ось (горизонт/вертикаль), и разворот
    # (REVERSE). Геометрию пересчитываем строго из bbox по оси направления,
    # чтобы swap/layout ниже отработал согласованно.
    emit_orientation = geometry.orientation
    _dir = node.get('direction') or node.get('flow_direction')
    if node.get('class_name') in DIRECTION_ORIENT_CLASSES and _dir in DIRECTION_TO_ORIENTATION:
        axis = _DIRECTION_TO_AXIS[_dir]
        emit_orientation = DIRECTION_TO_ORIENTATION[_dir]
        bbox = parse_bbox(node.get('bbox'))
        if bbox:
            geometry = SkinGeometry(
                orientation=axis,
                width=bbox['width'],
                height=bbox['height'],
                layout_x=bbox['x1'],
                layout_y=bbox['y1'],
            )
        elif axis != geometry.orientation:
            geometry = SkinGeometry(
                orientation=axis,
                width=geometry.height,
                height=geometry.width,
                layout_x=geometry.layout_x,
                layout_y=geometry.layout_y,
            )

    # --- Разворот вертикальной арматуры/регуляторов/клапанов на 180° ---
    # Ось и swap/layout ниже НЕ трогаем — REVERSE лишь зеркалит графику.
    # nasos/rashodomernaya_shaiba сюда не попадают (их разворот уже задан
    # выше по направлению потока и не является VERTICAL).
    if node.get('class_name') in REVERSE_VERTICAL_CLASSES and emit_orientation == 'VERTICAL':
        emit_orientation = 'VERTICAL_REVERSE'

    width = geometry.width
    height = geometry.height
    layout_x = geometry.layout_x
    layout_y = geometry.layout_y

    # Для ВСЕХ скинов при VERTICAL нужно:
    # 1. Поменять width/height, т.к. OrientationService применяет rotate(90)
    # 2. Скорректировать layout чтобы визуальный элемент после поворота совпал с bbox
    if geometry.orientation == 'VERTICAL':
        bbox = parse_bbox(node.get('bbox'))
        if bbox:
            orig_w = bbox['width']
            orig_h = bbox['height']
            layout_x = bbox['x1'] + (orig_w - orig_h) / 2
            layout_y = bbox['y1'] + (orig_h - orig_w) / 2
        # Swap размеров
        width, height = height, width

    # --- Поправка оси контакта (привод смещает талию от центра бокса) ---
    # Передаём emit_orientation (а не ось), чтобы VERTICAL_REVERSE инвертировал знак.
    # В холсте пропускаем: редактор рисует скин вписанным в bbox без поправки, а
    # концы труб уже посажены на границу фикс-бокса (reproject_edge_endpoints).
    # Сдвиг здесь разъехался бы с картинкой, которую видел оператор.
    if not is_canvas:
        layout_x, layout_y = apply_contact_offset(
            skin_type, emit_orientation, layout_x, layout_y, height)

    # --- Датчик в 3 раза меньше оригинала (сжатие вокруг центра) ---
    # В холсте не делим: FIXED_SIZES['datchik'] уже финальный (30x30), а /3 дал бы
    # 10px при минимуме скина ~20px.
    if control_class == 'DetectorControl' and not is_canvas:
        new_w = width / 3.0
        new_h = height / 3.0
        layout_x += (width - new_w) / 2.0
        layout_y += (height - new_h) / 2.0
        width, height = new_w, new_h

    # --- Базовые атрибуты ---
    layout_x = max(0.0, layout_x)
    layout_y = max(0.0, layout_y)
    attrs = [
        f'fx:id="{escape(str(node_id))}"',
        f'layoutX="{layout_x:.1f}"',
        f'layoutY="{layout_y:.1f}"',
        f'prefWidth="{width:.1f}"',
        f'prefHeight="{height:.1f}"',
        f'skinType="{skin_type}"',
        f'orientation="{emit_orientation}"',
    ]

    if equipment_type:
        attrs.append(f'equipmentType="{equipment_type}"')

    # --- KKS привязка (источник — graph.bindings; см. build_node_kks_map) ---
    # У датчиков KKS по дефолту, даже если не распознан
    if not kks and control_class == 'DetectorControl':
        kks = DETECTOR_DEFAULT_KKS
    if kks:
        attrs.append(f'kks="{escape(str(kks), {chr(34): "&quot;", chr(39): "&apos;"})}"')
        attrs.append('kksVisible="true"')
        # Кегль KKS — из общей таблицы, а НЕ доля от размера элемента: доли
        # 0.35 и 0.4 давали на одном листе 5.1 и 7.2 (замер §MEFX3), то есть
        # ровно ту жалобу «на одном листе подписи разного размера», ради
        # которой решение №3 и принято.
        attrs.append(f'kksFontSize="{TEXT_STYLES["kks"].size:.1f}"')
        # KKS ближе к узлу
        attrs.append('kksTextOffset="1.0"')

    # Дефолтные Param/Unit для датчиков (датчик давления)
    if control_class == 'DetectorControl':
        attrs.append(f'param="{DETECTOR_DEFAULT_PARAM}"')
        attrs.append(f'unit="{DETECTOR_DEFAULT_UNIT}"')

    # Скрыть дефолтное значение "0" / "0%" (valueText скина).
    # Проверено по skin_calibration_novalue.fxml: рендеринг не ломается.
    if control_class in ('DetectorControl', 'ValveControl'):
        attrs.append('valueVisible="false"')

    # --- Комментарий ---
    class_name = node.get('class_name', '?')
    kks_comment = f' kks={kks}' if kks else ''
    comment = f'<!-- {class_name}{kks_comment} -->'

    return f'        {comment}\n        <{control_class} {" ".join(attrs)} />'


def generate_fxml_rectangle(node, geometry: SkinGeometry, node_id: str,
                            graph_scale: float = 1.0, kks: str = None) -> str:
    """
    Генерирует FXML Rectangle для элементов без скинов.
    Добавляет KKS как Tooltip через вложенный Text (или комментарий).
    """
    class_name = node.get('class_name', 'unknown')
    color = CLASS_COLORS.get(class_name, DEFAULT_COLOR)

    # strokeWidth масштабируется по graph_scale
    elem_stroke = max(0.3, 1.0 * graph_scale) if graph_scale < 1.0 else 1.0

    attrs = [
        f'fx:id="{escape(str(node_id))}"',
        f'layoutX="{max(0.0, geometry.layout_x):.1f}"',
        f'layoutY="{max(0.0, geometry.layout_y):.1f}"',
        f'width="{geometry.width:.1f}"',
        f'height="{geometry.height:.1f}"',
        f'fill="{color}"',
        f'stroke="#333333"',
        f'strokeWidth="{elem_stroke:.2f}"',
    ]

    kks_comment = f' kks={kks}' if kks else ''
    comment = f'<!-- {class_name}{kks_comment} -->'

    return f'        {comment}\n        <Rectangle {" ".join(attrs)} />'


def generate_fxml_polygon(node, node_id: str, graph_scale: float = 1.0, kks: str = None) -> str:
    """
    Генерирует FXML Polygon для элементов с segmentation.
    Points нормализованы относительно layoutX/layoutY (JavaFX семантика).
    """
    segmentation = node.get('segmentation')
    if not segmentation or len(segmentation) < 6:
        return None

    class_name = node.get('class_name', 'unknown')
    color = CLASS_COLORS.get(class_name, DEFAULT_COLOR)

    # Compute layoutX/layoutY from segmentation bounds
    xs = [segmentation[i] for i in range(0, len(segmentation), 2)]
    ys = [segmentation[i] for i in range(1, len(segmentation), 2)]
    poly_layout_x = min(xs) if xs else 0.0
    poly_layout_y = min(ys) if ys else 0.0

    # Points ОТНОСИТЕЛЬНО layoutX/layoutY (чтобы JavaFX не складывал дважды)
    rel_points = []
    for i in range(0, len(segmentation), 2):
        rel_points.append(segmentation[i] - poly_layout_x)
        rel_points.append(segmentation[i + 1] - poly_layout_y)
    points_str = ",".join(f"{v:.1f}" for v in rel_points)

    # strokeWidth масштабируется по graph_scale
    elem_stroke = max(0.3, 1.0 * graph_scale) if graph_scale < 1.0 else 1.0

    attrs = [
        f'fx:id="{escape(str(node_id))}"',
        f'layoutX="{max(0.0, poly_layout_x):.1f}"',
        f'layoutY="{max(0.0, poly_layout_y):.1f}"',
        f'points="{points_str}"',
        f'fill="{color}"',
        f'stroke="#333333"',
        f'strokeWidth="{elem_stroke:.2f}"',
    ]

    kks_comment = f' kks={kks}' if kks else ''
    comment = f'<!-- {class_name}{kks_comment} -->'

    return f'        {comment}\n        <Polygon {" ".join(attrs)} />'


def napravlenie_triangle_points(bbox, direction) -> Optional[list]:
    """Абсолютные вершины треугольника-стрелки `napravlenie`: [(x, y) x3] или None.

    ВЕРШИНА смотрит в сторону направления, ОСНОВАНИЕ лежит на противоположной
    («входной») грани bbox. Так входящее ребро упирается в основание, а
    исходящее выходит из вершины (а не из пустоты), если граф так построен.

    Соответствие грани входа — то же правило, что в direction_nodes._IN_FACE:
        right → основание слева,  вершина справа
        left  → основание справа, вершина слева
        down  → основание сверху, вершина снизу
        up    → основание снизу,  вершина сверху

    Без Qt и без побочных эффектов — общий источник правды для FXML-экспорта и
    для отрисовки стрелки в редакторе (WYSIWYG: обе картинки обязаны совпадать).
    """
    if direction not in ('up', 'down', 'left', 'right'):
        return None
    b = parse_bbox(bbox)
    if not b:
        return None

    x1, y1, x2, y2 = b['x1'], b['y1'], b['x2'], b['y2']
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0

    if direction == 'right':
        return [(x1, y1), (x1, y2), (x2, cy)]
    if direction == 'left':
        return [(x2, y1), (x2, y2), (x1, cy)]
    if direction == 'down':
        return [(x1, y1), (x2, y1), (cx, y2)]
    return [(x1, y2), (x2, y2), (cx, y1)]   # up


def napravlenie_incoming_color(node, node_id, edges, nodes, direction) -> Optional[str]:
    """Цвет трубы, упирающейся в ОСНОВАНИЕ стрелки (входящей), или None.

    Стрелка красится в цвет своей трубы. «Входящая» = ребро, чья точка
    подключения ближе всего к центру входной грани (грань напротив вершины).
    Если у неё цвет не задан — берём любое инцидентное ребро с цветом.

    Без Qt — общий источник правды для FXML-экспорта и редактора.
    Точки подключения в формате [y, x].
    """
    b = parse_bbox(node.get('bbox'))
    if not b or direction not in ('up', 'down', 'left', 'right'):
        return None

    x1, y1, x2, y2 = b['x1'], b['y1'], b['x2'], b['y2']
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    # Вершина смотрит ПО направлению → основание на противоположной грани.
    face = {
        'right': (x1, cy),
        'left': (x2, cy),
        'down': (cx, y1),
        'up': (cx, y2),
    }[direction]

    incident = [e for e in (edges or [])
                if e.get('source') == node_id or e.get('target') == node_id]

    best, best_d = None, None
    for e in incident:
        p = e.get('source_point') if e.get('source') == node_id else e.get('target_point')
        if not p:
            continue
        d = (p[1] - face[0]) ** 2 + (p[0] - face[1]) ** 2
        if best_d is None or d < best_d:
            best_d, best = d, e

    if best is not None and best.get('render_color'):
        return best['render_color']
    for e in incident:                       # входящая без цвета → любая цветная
        if e.get('render_color'):
            return e['render_color']
    return None


def generate_fxml_triangle(node, node_id: str, graph_scale: float = 1.0, kks: str = None,
                           edges=None, nodes=None) -> Optional[str]:
    """
    Генерирует FXML Polygon-треугольник для узла `napravlenie`.

    Геометрия — napravlenie_triangle_points, цвет — napravlenie_incoming_color
    (те же источники, что у редактора).
    Возвращает строку FXML или None, если нет bbox/направления.
    """
    direction = node.get('flow_direction') or node.get('direction')
    pts = napravlenie_triangle_points(node.get('bbox'), direction)
    if not pts:
        return None

    # Polygon points в JavaFX задаём ОТНОСИТЕЛЬНО layoutX/layoutY.
    layout_x = min(p[0] for p in pts)
    layout_y = min(p[1] for p in pts)
    rel = []
    for px, py in pts:
        rel.append(px - layout_x)
        rel.append(py - layout_y)
    points_str = ",".join(f"{v:.1f}" for v in rel)

    # Заливка — цвет своей трубы; обводка остаётся контрастной (#333333),
    # иначе стрелка сливается с линией.
    color = (napravlenie_incoming_color(node, node_id, edges, nodes, direction)
             or CLASS_COLORS.get(NAPRAVLENIE_CLASS_NAME, NAPRAVLENIE_COLOR))
    elem_stroke = max(0.3, 1.0 * graph_scale) if graph_scale < 1.0 else 1.0

    attrs = [
        f'fx:id="{escape(str(node_id))}"',
        f'layoutX="{max(0.0, layout_x):.1f}"',
        f'layoutY="{max(0.0, layout_y):.1f}"',
        f'points="{points_str}"',
        f'fill="{color}"',
        f'stroke="#333333"',
        f'strokeWidth="{elem_stroke:.2f}"',
    ]

    kks_comment = f' kks={kks}' if kks else ''
    comment = f'<!-- napravlenie dir={direction}{kks_comment} -->'
    return f'        {comment}\n        <Polygon {" ".join(attrs)} />'


def _infer_napravlenie_direction(node, node_id, edges, nodes):
    """Направление стрелки для napravlenie-узла, у которого нет flow_direction.

    Ручной узел не проходит annotate_direction_nodes (нет классификатора),
    поэтому направление выводим здесь:
      • не подключён к ребру          → 'up' (дефолт);
      • у ребра есть e['direction']   → берём его;
      • иначе                         → ось из ориентации ребра
        (гориз → left/right, верт → up/down), сторона — вершина смотрит
        ПРОЧЬ от подключённого соседа (ребро входит в основание, а не в вершину).
    Центроиды в формате [y, x].
    """
    incident = [e for e in edges
                if e.get('source') == node_id or e.get('target') == node_id]
    if not incident:
        return 'up'
    for e in incident:
        d = e.get('direction')
        if d in ('up', 'down', 'left', 'right'):
            return d
    c = node.get('centroid')
    if not c:
        return 'up'
    ncx, ncy = c[1], c[0]
    e = incident[0]
    other_id = e.get('target') if e.get('source') == node_id else e.get('source')
    other = nodes.get(other_id) if other_id else None
    if not other or not other.get('centroid'):
        return 'up'
    ocx, ocy = other['centroid'][1], other['centroid'][0]
    dx, dy = ocx - ncx, ocy - ncy
    # Вершина смотрит ПРОЧЬ от соседа: ребро входит в основание треугольника.
    # Сосед справа → вершина влево; сосед снизу → вершина вверх; и т.д.
    if abs(dx) >= abs(dy):
        return 'left' if dx >= 0 else 'right'
    return 'up' if dy >= 0 else 'down'


def _edge_polyline_xy(edge, nodes):
    """Полилиния ребра в (x, y): start + waypoints + end (масштабированные)."""
    endpoints = get_line_endpoints(edge, nodes)
    if not endpoints:
        return None
    start, end = endpoints
    pts = [start]
    for wp in edge.get('waypoints', []):
        c = convert_point(wp)
        if c:
            pts.append(c)
    pts.append(end)
    return pts


def _edge_stroke_width(edge, base_stroke, use_diameter, graph_scale):
    """Толщина линии ребра — та же логика, что в generate_fxml_line."""
    rw = edge.get('render_width')
    if rw:
        return max(0.3, float(rw) * graph_scale)
    dv = edge.get('diameter_value')
    if use_diameter and dv:
        return calculate_diameter_stroke(dv, base_stroke, graph_scale)
    return base_stroke


def _segment_intersection(p1, p2, p3, p4):
    """Точка пересечения отрезков p1p2 и p3p4 или None."""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-9:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = ((x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)) / denom
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
    return None


def _cumulative_lengths(points):
    """Накопленная длина вдоль полилинии в каждой вершине."""
    cum = [0.0]
    for i in range(len(points) - 1):
        ax, ay = points[i]
        bx, by = points[i + 1]
        cum.append(cum[-1] + math.hypot(bx - ax, by - ay))
    return cum


def _horizontality(a, b):
    """0..1: насколько отрезок горизонтален (1 — горизонталь, 0 — вертикаль)."""
    dx = abs(b[0] - a[0])
    dy = abs(b[1] - a[1])
    tot = dx + dy
    return 0.5 if tot < 1e-9 else dx / tot


def compute_bridge_cuts(edges, nodes, base_stroke, use_diameter, graph_scale,
                        bridge_gap_factor: float = BRIDGE_GAP_STROKE_FACTOR):
    """Найти мосты геометрически и вычислить разрывы.

    Мост = пересечение двух рёбер без узла рядом (рёбра не делят общий узел).
    Рвётся более ТОЛСТОЕ ребро (на тонкой линии разрыв теряется); при равной
    толщине — более горизонтальное, вертикальное проходит сверху (— | —).

    Returns:
        dict edge_id -> list[(s, gap)] — позиция разрыва по длине ребра и ширина.
    """
    node_xy = [(n['centroid'][1], n['centroid'][0])
               for n in nodes.values() if n.get('centroid')]
    clearance_sq = (BRIDGE_NODE_CLEARANCE_PX * graph_scale) ** 2
    min_gap = BRIDGE_GAP_MIN_MM * PX_PER_MM

    info = []
    for e in edges:
        pl = _edge_polyline_xy(e, nodes)
        if pl and len(pl) >= 2:
            info.append((e, pl, _edge_stroke_width(e, base_stroke, use_diameter, graph_scale)))

    def near_node(px, py):
        for nx, ny in node_xy:
            dx = px - nx
            dy = py - ny
            if dx * dx + dy * dy < clearance_sq:
                return True
        return False

    cuts: dict = {}
    n = len(info)
    for i in range(n):
        ea, pa, wa = info[i]
        a_ends = {ea['source'], ea['target']}
        for j in range(i + 1, n):
            eb, pb, wb = info[j]
            # Рёбра с общим узлом соединены — это не мост
            if a_ends & {eb['source'], eb['target']}:
                continue
            for ai in range(len(pa) - 1):
                for bj in range(len(pb) - 1):
                    p = _segment_intersection(pa[ai], pa[ai + 1], pb[bj], pb[bj + 1])
                    if not p or near_node(p[0], p[1]):
                        continue
                    # Разрыв делаем на более ТОЛСТОЙ трубе — на тонкой линии
                    # разрыв визуально теряется. При равной толщине рвём более
                    # горизонтальную (вертикальная проходит сверху, — | —).
                    if wa > wb:
                        under_e, under_pl, seg_idx, over_w = ea, pa, ai, wb
                    elif wb > wa:
                        under_e, under_pl, seg_idx, over_w = eb, pb, bj, wa
                    elif _horizontality(pa[ai], pa[ai + 1]) >= _horizontality(pb[bj], pb[bj + 1]):
                        under_e, under_pl, seg_idx, over_w = ea, pa, ai, wb
                    else:
                        under_e, under_pl, seg_idx, over_w = eb, pb, bj, wa
                    # Разрыв достаточно широкий, чтобы был виден и на толстой линии:
                    # учитываем обе трубы (перекрывающую и разрываемую).
                    under_w = wa if under_e is ea else wb
                    gap = max(bridge_gap_factor * over_w,
                              bridge_gap_factor * under_w, min_gap)
                    ucum = _cumulative_lengths(under_pl)
                    s = ucum[seg_idx] + math.hypot(
                        p[0] - under_pl[seg_idx][0], p[1] - under_pl[seg_idx][1])
                    cuts.setdefault(under_e['id'], []).append((s, gap))
    return cuts


def _point_at_arclen(points, cum, s):
    """Точка на полилинии на расстоянии s от начала."""
    for i in range(len(points) - 1):
        if s <= cum[i + 1] or i == len(points) - 2:
            seg = cum[i + 1] - cum[i]
            t = 0.0 if seg < 1e-9 else (s - cum[i]) / seg
            ax, ay = points[i]
            bx, by = points[i + 1]
            return (ax + t * (bx - ax), ay + t * (by - ay))
    return points[-1]


def _split_polyline_with_gaps(points, cuts_s):
    """Разбить полилинию на под-линии, вырезав интервалы [s-gap/2, s+gap/2].

    Returns: list[list[(x, y)]] — список под-полилиний (без вырезанных кусков).
    """
    cum = _cumulative_lengths(points)
    total = cum[-1]
    if total < 1e-9:
        return [points]

    intervals = []
    for s, gap in cuts_s:
        a = max(0.0, s - gap / 2.0)
        b = min(total, s + gap / 2.0)
        if b > a:
            intervals.append([a, b])
    if not intervals:
        return [points]

    intervals.sort()
    merged = [intervals[0]]
    for a, b in intervals[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])

    subpaths = []
    cursor = 0.0
    for a, b in merged:
        if a > cursor + 1e-6:
            subpaths.append(_subpath_between(points, cum, cursor, a))
        cursor = b
    if cursor < total - 1e-6:
        subpaths.append(_subpath_between(points, cum, cursor, total))
    return subpaths


def _subpath_between(points, cum, s0, s1):
    """Под-полилиния между арк-длинами s0 и s1 (с интерполяцией концов)."""
    pts = [_point_at_arclen(points, cum, s0)]
    for i in range(len(points)):
        if s0 < cum[i] < s1:
            pts.append(points[i])
    pts.append(_point_at_arclen(points, cum, s1))
    return pts


# ---------------------------------------------------------------------------
# B2: посадка конца трубы на КОНТУР полигона (SAM2 segmentation), а не на
# detection-bbox узла. Труба тянется вдоль своей оси до реального ребра контура.
# ---------------------------------------------------------------------------
def _point_in_poly(x, y, pts):
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def _ray_seg_t(px, py, dx, dy, ax, ay, bx, by):
    """Параметр t>=0, где луч (px,py)+t*(dx,dy) пересекает отрезок AB, иначе None."""
    ex, ey = bx - ax, by - ay
    den = dx * ey - dy * ex
    if abs(den) < 1e-9:
        return None
    t = ((ax - px) * ey - (ay - py) * ex) / den
    u = ((ax - px) * dy - (ay - py) * dx) / den
    return t if (t >= -1e-6 and -1e-6 <= u <= 1 + 1e-6) else None


def project_endpoint_to_contour(point, adjacent, segmentation):
    """Двигает конец трубы (point) вдоль оси сегмента (направление от adjacent к
    концу и глубже) до границы контура segmentation. Ортогонально; если конец уже
    внутри контура или пересечения нет — возвращает point без изменений."""
    seg = segmentation
    if seg and isinstance(seg[0], list):
        seg = [v for poly in seg for v in poly]
    if not seg or len(seg) < 6:
        return point
    pts = [(seg[i], seg[i + 1]) for i in range(0, len(seg) - 1, 2)]
    px, py = point
    if _point_in_poly(px, py, pts):
        return point
    dx, dy = px - adjacent[0], py - adjacent[1]
    if dx == 0 and dy == 0:
        return point
    if abs(dy) >= abs(dx):
        dx, dy = 0.0, (1.0 if dy > 0 else -1.0)
    else:
        dx, dy = (1.0 if dx > 0 else -1.0), 0.0
    best = None
    for i in range(len(pts)):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % len(pts)]
        t = _ray_seg_t(px, py, dx, dy, ax, ay, bx, by)
        if t is not None and t >= 0 and (best is None or t < best):
            best = t
    if best is None:
        return point
    return (px + dx * best, py + dy * best)


def generate_fxml_line(edge, nodes, edge_id: str,
                       base_stroke: float = LINE_STROKE_WIDTH,
                       use_diameter: bool = True,
                       graph_scale: float = 1.0,
                       cuts=None,
                       contour_snap: bool = True) -> Optional[str]:
    """
    Генерирует FXML Line или Polyline для ребра.

    Если ребро содержит waypoints — генерируется <Polyline> через все точки.
    Иначе — простая <Line> от start до end.

    contour_snap: посадка конца на SAM2-контур (B2). 1:1-экспорт холста
    (canvas_to_fxml) выключает её: концы там уже посажены редактором, вторая
    посадка сдвинула бы линию относительно картинки, которую видел оператор.

    Note:
        Координаты source_point/target_point/waypoints в формате [y, x].
        convert_point() конвертирует [y, x] → (x, y) для FXML.
    """
    endpoints = get_line_endpoints(edge, nodes)
    if not endpoints:
        return None

    start, end = endpoints

    # Цвет: индивидуальный цвет ребра (режим «Размер и цвет») имеет приоритет,
    # иначе — общий цвет по умолчанию (белый в редакторе ↔ #333333 в FXML).
    line_color = edge.get('render_color') or DEFAULT_LINE_COLOR

    # Толщина линии.
    diameter_value = edge.get('diameter_value')
    diameter_text = edge.get('diameter_text', '')
    render_width = edge.get('render_width')

    if render_width:
        # Ручной размер (режим «Размер ребра») полностью заменяет авто-толщину.
        stroke_width = max(0.3, float(render_width) * graph_scale)
    elif use_diameter and diameter_value:
        stroke_width = calculate_diameter_stroke(diameter_value, base_stroke, graph_scale)
    else:
        stroke_width = base_stroke

    # Комментарий с диаметром
    diam_info = f' {diameter_text}' if diameter_text else ''
    propagated = ' (propagated)' if edge.get('diameter_propagated') else ''
    comment = f'<!-- {edge_id}{diam_info}{propagated} -->'

    # Полный список точек ребра: start + waypoints + end
    all_points = [start]
    for wp in edge.get('waypoints', []):
        converted = convert_point(wp)
        if converted:
            all_points.append(converted)
    all_points.append(end)

    # B2: если конец ребра соединён с ПОЛИГОН-узлом (есть segmentation и нет скина),
    # посадить конец трубы на контур, а не на detection-bbox.
    if contour_snap and len(all_points) >= 2:
        _src = nodes.get(edge.get('source'))
        _tgt = nodes.get(edge.get('target'))
        if _src and _src.get('segmentation') and get_skin_info(_src) is None:
            all_points[0] = project_endpoint_to_contour(all_points[0], all_points[1], _src['segmentation'])
        if _tgt and _tgt.get('segmentation') and get_skin_info(_tgt) is None:
            all_points[-1] = project_endpoint_to_contour(all_points[-1], all_points[-2], _tgt['segmentation'])

    # Пунктир (dashed): JavaFX пишет его как CSS-стиль -fx-stroke-dash-array.
    # Размер штриха ЕДИНЫЙ для всех линий — не зависит от толщины конкретной
    # трубы (иначе широкие трубы получали неадекватно крупный пунктир), а
    # считается от базовой толщины и масштаба листа. Одно число ⇒ штрих=пробел.
    _dash_len = max(4.0, round(3.3 * base_stroke * graph_scale, 1))
    dash_style = (
        f' style="-fx-stroke-dash-array: {_dash_len:g};"'
        if edge.get('dashed') else ''
    )

    def _emit(points, fid):
        """<Line> для 2 точек, иначе <Polyline>."""
        if len(points) <= 2:
            (sx, sy), (ex, ey) = points[0], points[-1]
            attrs = [
                f'fx:id="{escape(str(fid))}"',
                f'startX="{sx:.1f}"', f'startY="{sy:.1f}"',
                f'endX="{ex:.1f}"', f'endY="{ey:.1f}"',
                f'stroke="{line_color}"',
                f'strokeWidth="{stroke_width:.1f}"',
            ]
            return f'<Line {" ".join(attrs)}{dash_style} />'
        pts_str = ",".join(f"{c:.1f}" for x, y in points for c in (x, y))
        attrs = [
            f'fx:id="{escape(str(fid))}"',
            f'points="{pts_str}"',
            f'stroke="{line_color}"',
            f'strokeWidth="{stroke_width:.1f}"',
        ]
        return f'<Polyline {" ".join(attrs)}{dash_style} />'

    # Мост: рвём линию в местах пересечения, каждый сегмент — отдельный элемент
    if cuts:
        usable = [sp for sp in _split_polyline_with_gaps(all_points, cuts)
                  if len(sp) >= 2]
        if not usable:
            return None
        parts = []
        for k, sp in enumerate(usable):
            fid = f"{edge_id}_b{k}" if len(usable) > 1 else edge_id
            parts.append(f'        {_emit(sp, fid)}')
        return f'        {comment}\n' + "\n".join(parts)

    return f'        {comment}\n        {_emit(all_points, edge_id)}'


def scale_graph_to_page(graph_data: dict, page_size: str = None,
                        margin_mm: float = PAGE_MARGIN_MM) -> tuple:
    """
    Масштабировать граф из пиксельных координат в координаты листа.

    Args:
        graph_data: Исходный граф (модифицируется in-place!)
        page_size: 'A4', 'A3', 'A2', 'A1', 'A0' или None (без масштабирования)
        margin_mm: Отступы от краёв в мм

    Returns:
        (page_width_px, page_height_px, scale) — размер панели и коэффициент масштабирования
    """
    image_size = graph_data.get('graph', {}).get('image_size', [3509, 4964])
    orig_h, orig_w = image_size  # [height, width]

    if not page_size or page_size not in PAGE_SIZES_MM:
        return orig_w, orig_h, 1.0

    page_w_mm, page_h_mm = PAGE_SIZES_MM[page_size]
    margin_px = margin_mm * PX_PER_MM
    page_w_px = page_w_mm * PX_PER_MM
    page_h_px = page_h_mm * PX_PER_MM

    # Рабочая область
    work_w = page_w_px - 2 * margin_px
    work_h = page_h_px - 2 * margin_px

    # Масштаб: вписать с сохранением пропорций
    scale = min(work_w / max(orig_w, 1), work_h / max(orig_h, 1))

    # Смещение для центрирования
    offset_x = margin_px + (work_w - orig_w * scale) / 2
    offset_y = margin_px + (work_h - orig_h * scale) / 2

    def _sx(x):
        return offset_x + x * scale

    def _sy(y):
        return offset_y + y * scale

    # Масштабировать узлы
    for node in graph_data.get('nodes', []):
        c = node.get('centroid')
        if c and len(c) == 2:
            node['centroid'] = [_sy(c[0]), _sx(c[1])]

        bb = node.get('bbox')
        if bb and len(bb) == 4:
            node['bbox'] = [_sx(bb[0]), _sy(bb[1]), _sx(bb[2]), _sy(bb[3])]

        seg = node.get('segmentation')
        if seg and isinstance(seg, list):
            # Flatten nested list (COCO format: [[x1,y1,...],[x2,y2,...]])
            if seg and isinstance(seg[0], list):
                seg = [coord for polygon in seg for coord in polygon]
                node['segmentation'] = seg
            if len(seg) >= 6:
                for i in range(0, len(seg), 2):
                    seg[i] = _sx(seg[i])
                    seg[i + 1] = _sy(seg[i + 1])

        # Масштабировать contours_all (множественные полигоны из contour_extractor)
        contours_all = node.get('contours_all')
        if contours_all and isinstance(contours_all, list):
            for contour in contours_all:
                for pt in contour:
                    pt[0] = _sx(pt[0])
                    pt[1] = _sy(pt[1])

    # Масштабировать рёбра
    for edge in graph_data.get('links', []):
        sp = edge.get('source_point')
        if sp and len(sp) == 2:
            edge['source_point'] = [_sy(sp[0]), _sx(sp[1])]

        tp = edge.get('target_point')
        if tp and len(tp) == 2:
            edge['target_point'] = [_sy(tp[0]), _sx(tp[1])]

        wps = edge.get('waypoints', [])
        for wp in wps:
            if len(wp) == 2:
                wp[0] = _sy(wp[0])
                wp[1] = _sx(wp[1])

    # Масштабировать текст-блоки OCR (bbox [x1, y1, x2, y2])
    for blk in (graph_data.get('text_blocks') or []):
        bb = blk.get('bbox')
        if bb and len(bb) == 4:
            blk['bbox'] = [_sx(bb[0]), _sy(bb[1]), _sx(bb[2]), _sy(bb[3])]

    # Обновить image_size
    graph_data.setdefault('graph', {})['image_size'] = [page_h_px, page_w_px]

    return page_w_px, page_h_px, scale


def generate_fxml(graph_data: dict, stroke_width: float = LINE_STROKE_WIDTH,
                  page_size: str = None, use_diameter: bool = True,
                  bridge_gap_factor: float = BRIDGE_GAP_STROKE_FACTOR) -> str:
    """
    Генерирует полный FXML документ из графа.

    Args:
        graph_data: JSON-граф (НЕ мутируется — внутри создаётся deepcopy)
        stroke_width: Базовая толщина линий
        page_size: 'A4', 'A3', 'A2' и т.д. или None (пиксельные координаты)
        use_diameter: Масштабировать толщину по diameter_value

    Note:
        Координаты в graph_data используют формат [y, x] (row, col).
        scale_graph_to_page масштабирует все координаты включая waypoints.
    """
    import copy
    graph_data = copy.deepcopy(graph_data)

    # Масштабирование (модифицирует копию graph_data in-place)
    pane_width, pane_height, graph_scale = scale_graph_to_page(graph_data, page_size)

    nodes = {n['id']: n for n in graph_data['nodes']}
    edges = graph_data['links']

    # KKS оборудования — ТОЛЬКО из graph.bindings (node.kks_full не используется).
    kks_by_node = build_node_kks_map(graph_data)
    # Блоки с привязкой (к узлу или ребру) как <Text> не печатаем.
    bound_block_ids = {
        b.get('block_id') for b in (graph_data.get('bindings') or [])
        if b.get('block_id')
    }

    # Масштабировать stroke_width пропорционально scale графа
    if page_size and page_size in PAGE_SIZES_MM:
        stroke_width = max(0.3, stroke_width * graph_scale)

    # Собираем элементы
    control_elements = []
    rectangle_elements = []
    polygon_elements = []
    line_elements = []

    # Счётчики для статистики
    stats = {
        'controls': 0,
        'controls_with_kks': 0,
        'rectangles': 0,
        'rectangles_with_kks': 0,
        'polygons': 0,
        'napravlenie_triangles': 0,
        'lines_with_diameter': 0,
    }

    # Обрабатываем узлы
    for node_id, node in nodes.items():
        if node['type'] != 'equipment':
            continue

        # napravlenie → треугольник по направлению потока (приоритет над
        # скином/полигоном/прямоугольником). Ручной узел может не иметь
        # flow_direction (его ставит только annotate_direction_nodes при сборке),
        # поэтому выводим направление здесь: не подключён → 'up', подключён →
        # по ребру. Иначе узел ошибочно рисуется прямоугольником.
        if node.get('class_name') == NAPRAVLENIE_CLASS_NAME or node.get('direction_node'):
            if not (node.get('flow_direction') or node.get('direction')):
                node['direction'] = _infer_napravlenie_direction(node, node_id, edges, nodes)
            tri = generate_fxml_triangle(node, node_id, graph_scale,
                                         kks=kks_by_node.get(node_id),
                                         edges=edges, nodes=nodes)
            if tri:
                polygon_elements.append(tri)
                stats['polygons'] += 1
                stats['napravlenie_triangles'] = stats.get('napravlenie_triangles', 0) + 1
                continue

        # Получаем подключения
        connections = get_node_connections(node_id, edges, nodes)

        # Вычисляем геометрию
        geometry = calculate_skin_geometry(node, connections)

        # Пробуем найти скин
        skin_info = get_skin_info(node)

        if skin_info:
            fxml = generate_fxml_control(node, geometry, node_id, graph_scale, kks=kks_by_node.get(node_id))
            if fxml:
                control_elements.append(fxml)
                stats['controls'] += 1
                if kks_by_node.get(node_id):
                    stats['controls_with_kks'] += 1
        elif node.get('contours_all'):
            # Множественные полигоны из contour_extractor
            # (drossel = 2 полигона, voronka = 1-2 полигона и т.д.)
            class_name = node.get('class_name', 'unknown')
            color = CLASS_COLORS.get(class_name, DEFAULT_COLOR)
            kks = kks_by_node.get(node_id)
            kks_comment = f' kks={kks}' if kks else ''
            elem_stroke = max(0.3, 1.0 * graph_scale) if graph_scale < 1.0 else 1.0

            for idx, contour_pts in enumerate(node['contours_all']):
                if len(contour_pts) < 3:
                    continue
                xs = [p[0] for p in contour_pts]
                ys = [p[1] for p in contour_pts]
                lx, ly = min(xs), min(ys)
                rel = []
                for px, py in contour_pts:
                    rel.append(px - lx)
                    rel.append(py - ly)
                points_str = ",".join(f"{v:.1f}" for v in rel)

                sub_id = f"{node_id}_p{idx}" if idx > 0 else node_id
                attrs = [
                    f'fx:id="{escape(str(sub_id))}"',
                    f'layoutX="{max(0.0, lx):.1f}"',
                    f'layoutY="{max(0.0, ly):.1f}"',
                    f'points="{points_str}"',
                    f'fill="{color}"',
                    f'stroke="#333333"',
                    f'strokeWidth="{elem_stroke:.2f}"',
                ]
                comment = f'<!-- {class_name}{kks_comment} -->'
                polygon_elements.append(f'        {comment}\n        <Polygon {" ".join(attrs)} />')
                stats['polygons'] += 1
        elif node.get('segmentation'):
            fxml = generate_fxml_polygon(node, node_id, graph_scale, kks=kks_by_node.get(node_id))
            if fxml:
                polygon_elements.append(fxml)
                stats['polygons'] += 1
        else:
            fxml = generate_fxml_rectangle(node, geometry, node_id, graph_scale, kks=kks_by_node.get(node_id))
            if fxml:
                rectangle_elements.append(fxml)
                stats['rectangles'] += 1
                if kks_by_node.get(node_id):
                    stats['rectangles_with_kks'] += 1

    # Авто-датчики расхода для шайб без привязанного датчика
    auto_det_controls, auto_det_lines = generate_flow_detectors(
        nodes, edges, graph_scale)
    control_elements.extend(auto_det_controls)
    line_elements.extend(auto_det_lines)
    stats['auto_flow_detectors'] = len(auto_det_controls)

    # Мосты: предрасчёт разрывов (— | —) по пересечениям BLUE×YELLOW
    bridge_cuts = compute_bridge_cuts(
        edges, nodes, base_stroke=stroke_width,
        use_diameter=use_diameter, graph_scale=graph_scale,
        bridge_gap_factor=bridge_gap_factor,
    )
    stats['bridge_gaps'] = sum(len(v) for v in bridge_cuts.values())

    # Обрабатываем рёбра
    for edge in edges:
        fxml = generate_fxml_line(edge, nodes, edge['id'], stroke_width,
                                  use_diameter=use_diameter, graph_scale=graph_scale,
                                  cuts=bridge_cuts.get(edge['id']))
        if fxml:
            line_elements.append(fxml)
            if edge.get('diameter_value'):
                stats['lines_with_diameter'] += 1

    # Текст-блоки OCR (непривязанные) → <Text>. Привязанные к узлу идут в kks,
    # к ребру (диаметры) — пока не печатаем.
    text_elements = []
    for blk in (graph_data.get('text_blocks') or []):
        if blk.get('merged_into') is not None:
            continue
        if blk.get('id') in bound_block_ids:
            continue
        if not (blk.get('text') or '').strip():
            continue
        t = generate_fxml_text(blk)
        if t:
            text_elements.append(t)
    stats['text_blocks'] = len(text_elements)

    # Собираем FXML
    fxml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '',
        '<?import javafx.scene.layout.*?>',
        '<?import javafx.scene.shape.*?>',
        '<?import javafx.scene.text.*?>',
        '<?import javafx.scene.transform.*?>',
        '<?import ru.get.common.controls.*?>',
        '',
        f'<AnchorPane fx:id="root" xmlns="http://javafx.com/javafx/17" xmlns:fx="http://javafx.com/fxml/1"',
        f'    prefWidth="{pane_width}" prefHeight="{pane_height}"',
        f'    style="-fx-background-color: {PANE_BACKGROUND};">',
        '    <children>',
        '',
        '        <!-- ======================================== -->',
        '        <!-- Lines (edges) with diameter-scaled width -->',
        '        <!-- ======================================== -->',
    ]

    fxml_lines.extend(line_elements)

    fxml_lines.extend([
        '',
        '        <!-- ======================================== -->',
        '        <!-- Equipment with library skins + KKS       -->',
        '        <!-- ======================================== -->',
    ])

    fxml_lines.extend(control_elements)

    fxml_lines.extend([
        '',
        '        <!-- ======================================== -->',
        '        <!-- Equipment without skins (rectangles)     -->',
        '        <!-- ======================================== -->',
    ])

    fxml_lines.extend(rectangle_elements)

    fxml_lines.extend([
        '',
        '        <!-- ======================================== -->',
        '        <!-- Equipment with polygons                  -->',
        '        <!-- ======================================== -->',
    ])

    fxml_lines.extend(polygon_elements)

    fxml_lines.extend([
        '',
        '        <!-- ======================================== -->',
        '        <!-- OCR text blocks (unbound labels)         -->',
        '        <!-- ======================================== -->',
    ])

    fxml_lines.extend(text_elements)

    fxml_lines.extend([
        '',
        '    </children>',
        '</AnchorPane>',
    ])

    # Сохраняем статистику для вывода
    generate_fxml._stats = stats

    return '\n'.join(fxml_lines)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Convert P&ID graph JSON to FXML for SceneBuilder (v2: KKS + diameter)'
    )
    parser.add_argument(
        'input',
        type=Path,
        help='Input JSON graph file'
    )
    parser.add_argument(
        '-o', '--output',
        type=Path,
        default=None,
        help='Output FXML file (default: input.fxml)'
    )
    parser.add_argument(
        '--stroke-width',
        type=float,
        default=LINE_STROKE_WIDTH,
        help=f'Base line stroke width (default: {LINE_STROKE_WIDTH})'
    )
    parser.add_argument(
        '--page-size',
        type=str,
        default=None,
        choices=['A0', 'A1', 'A2', 'A3', 'A4'],
        help='Target page size (default: original pixel coordinates)'
    )
    parser.add_argument(
        '--no-diameter',
        action='store_true',
        help='Disable diameter-based stroke scaling'
    )
    parser.add_argument(
        '--image',
        type=Path,
        default=None,
        help='Original P&ID image (for contour extraction of nodes without skins)'
    )
    parser.add_argument(
        '--pipe-mask',
        type=Path,
        default=None,
        help='Pipe mask image (for contour extraction of nodes without skins)'
    )

    args = parser.parse_args()

    # Определяем выходной файл
    output_path = args.output or args.input.with_suffix('.fxml')

    # Загружаем граф
    print(f"Loading graph from {args.input}...")
    with open(args.input, 'r', encoding='utf-8') as f:
        graph_data = json.load(f)

    # Обогащение контурами (если указаны пути к изображению и маске)
    if args.image and args.pipe_mask:
        try:
            from modules.contour_extractor import enrich_graph_with_contours
        except ImportError:
            from contour_extractor import enrich_graph_with_contours
        print(f"Extracting contours from {args.image}...")
        enrich_graph_with_contours(
            graph_data,
            image_path=str(args.image),
            pipe_mask_path=str(args.pipe_mask),
            skin_mapped_classes=set(CLASS_NAME_TO_SKIN.keys()),
        )

    # Генерируем FXML
    page_info = f" → {args.page_size} landscape" if args.page_size else " (original pixels)"
    diam_info = "" if not args.no_diameter else " [diameter scaling OFF]"
    print(f"Generating FXML{page_info}{diam_info}...")

    fxml_content = generate_fxml(
        graph_data,
        args.stroke_width,
        args.page_size,
        use_diameter=not args.no_diameter
    )

    # Сохраняем
    print(f"Saving to {output_path}...")
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(fxml_content)

    # Статистика
    nodes = graph_data['nodes']
    edges = graph_data['links']
    equipment_count = sum(1 for n in nodes if n['type'] == 'equipment')
    connector_count = sum(1 for n in nodes if n['type'] == 'connector')
    kks_count = len(build_node_kks_map(graph_data))

    stats = getattr(generate_fxml, '_stats', {})

    print(f"\nDone!")
    print(f"  Equipment nodes: {equipment_count}")
    print(f"  Connector nodes: {connector_count}")
    print(f"  Edges: {len(edges)}")
    print(f"  Nodes with KKS: {kks_count}")
    print()
    print(f"  Controls (with library skins): {stats.get('controls', 0)}")
    print(f"    ↳ with KKS bound: {stats.get('controls_with_kks', 0)}")
    print(f"  Rectangles (fallback):         {stats.get('rectangles', 0)}")
    print(f"    ↳ with KKS in comment: {stats.get('rectangles_with_kks', 0)}")
    print(f"  Polygons:                      {stats.get('polygons', 0)}")
    print(f"    ↳ napravlenie triangles:     {stats.get('napravlenie_triangles', 0)}")
    print(f"  Lines with diameter scaling:   {stats.get('lines_with_diameter', 0)}")
    print(f"  Auto flow detectors (шайбы):   {stats.get('auto_flow_detectors', 0)}")

    # Показываем маппинг class_name → skin
    print("\n  Skin mapping applied:")
    from collections import Counter
    mapped = Counter()
    unmapped = Counter()
    for n in nodes:
        if n['type'] != 'equipment':
            continue
        cn = n.get('class_name', '?')
        if get_skin_info(n):
            mapped[cn] += 1
        else:
            unmapped[cn] += 1

    for cn, cnt in sorted(mapped.items()):
        info = CLASS_NAME_TO_SKIN.get(cn)
        if not info:
            # Для class_id fallback, ищем первый node с этим class_name
            for nn in nodes:
                if nn.get('class_name') == cn:
                    info = CLASS_ID_TO_SKIN.get(nn.get('class_id', -1))
                    break
        skin_name = info[1] if info else '?'
        ctrl = info[0] if info else '?'
        print(f"    ✓ {cn} ({cnt}) → {ctrl} / {skin_name}")

    if unmapped:
        print("  No skin (Rectangle fallback):")
        for cn, cnt in sorted(unmapped.items()):
            print(f"    ✗ {cn} ({cnt})")

    # Показываем диапазон диаметров
    if not args.no_diameter:
        diameters = sorted(set(
            e.get('diameter_value', 0) for e in edges if e.get('diameter_value')
        ))
        if diameters:
            print(f"\n  Diameter range: {diameters}")
            print(f"  Stroke range: "
                  f"{calculate_diameter_stroke(min(diameters), args.stroke_width):.1f} — "
                  f"{calculate_diameter_stroke(max(diameters), args.stroke_width):.1f} "
                  f"(base={args.stroke_width})")


if __name__ == '__main__':
    main()

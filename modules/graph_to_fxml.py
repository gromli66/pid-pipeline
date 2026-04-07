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
import argparse
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
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
CLASS_NAME_TO_SKIN = {
    # --- Арматура / задвижки ---
    'armatura_ruchn':       ('ValveControl', 'HANDLE_VLV'),
    'armatura_electro':     ('ValveControl', 'ELECTRIC_VLV'),
    'armatura_seroprivod':  ('ValveControl', 'AOC_VLV'),
    'armatura_membr_electro': ('ValveControl', 'ELECTROMAGNETIC_VLV'),

    # --- Клапаны ---
    'klapan_obratn':        ('ValveControl', 'CHECK_VLV'),
    'klapan_obratn_seroprivod': ('ValveControl', 'CHECK_HYDRO_VLV'),

    # --- Регуляторы ---
    'regulator_electro':    ('ValveControl', 'CTRL_VLV_ELEC'),
    'regulator_ruchn':      ('ValveControl', 'CTRL_VLV_HANDLE'),
    'regulator_seroprivod': ('ValveControl', 'AOC_CTRL_VLV'),

    # drossel — НЕ маппим на скин, нет аналога в библиотеке

    # --- Предохранительный клапан ---
    'predohran':            ('ValveControl', 'RELIEF_VLV'),

    # --- Насос ---
    'nasos':                ('PumpControl', 'PUMP'),
    'vodostruiniy_nasos':   ('PumpControl', 'PUMP'),

    # --- Вентилятор ---
    'ventilaytor':          ('PumpControl', 'FAN'),

    # --- Теплообмен / нагрев ---
    'teploobmen':           ('HeaterControl', 'HEATER'),
    'electronagrevat':      ('HeaterControl', 'HEATER'),
    # dearator — НЕ маппим на скин, используется Polygon из segmentation

    # --- Датчики ---
    'datchik':              ('DetectorControl', 'MINSK'),

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
    0:  ('ValveControl', 'HANDLE_VLV'),
    1:  ('ValveControl', 'CHECK_VLV'),
    2:  ('ValveControl', 'CTRL_VLV_HANDLE'),
    3:  ('ValveControl', 'ELECTRIC_VLV'),
    4:  ('ValveControl', 'CTRL_VLV_ELEC'),
    6:  ('FunctionControl', 'ARROW'),
    7:  ('ValveControl', 'CHECK_HYDRO_VLV'),
    8:  ('ValveControl', 'AOC_VLV_V2_NPP'),
    10: ('ValveControl', 'ELECTROMAGNETIC_VLV'),
    11: ('PumpControl', 'PUMP'),
    12: ('PumpControl', 'FAN_NVART'),
    13: ('ValveControl', 'SAFETY_VLV_NPP'),
    15: ('FunctionControl', 'FLOWMETER'),
    17: ('HeaterControl', 'HEATER_VOL_TUBE'),
    22: ('FunctionControl', 'FILTER'),
    26: ('ValveControl', 'CTRL_VLV_REDUCING'),
    31: ('HeaterControl', 'HEATER'),
    32: ('ValveControl', 'STOP_CTRL_VLV'),
    33: ('DetectorControl', 'MINSK'),
    35: ('ButtonControl', 'TEXT_BUTTON', 'TRANSITION_BUTTON'),
}


# class_name'ы которые НЕ должны маппиться на скины
SKIP_CLASS_NAMES = {
    'connector', 'voronka', 'annotation', 'truba', 'background',
}


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
# ГЕНЕРАЦИЯ FXML
# ============================================================================

def generate_fxml_control(node, geometry: SkinGeometry, node_id: str,
                          graph_scale: float = 1.0) -> str:
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
        f'orientation="{geometry.orientation}"',
    ]

    if equipment_type:
        attrs.append(f'equipmentType="{equipment_type}"')

    # --- KKS привязка ---
    kks = node.get('kks_full')
    if kks:
        attrs.append(f'kks="{escape(str(kks))}"')
        attrs.append('kksVisible="true"')
        # Размер шрифта KKS: пропорционален размеру элемента,
        # но с scale-aware минимумом чтобы текст оставался читаемым
        vis_w = geometry.width
        vis_h = geometry.height
        kks_font = min(vis_w, vis_h) * 0.35
        # При сильном масштабировании (A4) элементы маленькие →
        # min clamp обеспечивает читаемость (не менее 40% высоты элемента)
        min_font = max(3.0, min(vis_w, vis_h) * 0.4) if graph_scale < 0.2 else 3.0
        kks_font = max(min_font, kks_font)
        attrs.append(f'kksFontSize="{kks_font:.1f}"')
        # KKS ближе к узлу
        attrs.append('kksTextOffset="1.0"')

    # Скрыть дефолтное значение "0" только у DetectorControl
    # (у других контролов valueVisible="false" ломает рендеринг скина)
    if control_class == 'DetectorControl':
        attrs.append('valueVisible="false"')

    # --- Комментарий ---
    class_name = node.get('class_name', '?')
    kks_comment = f' kks={kks}' if kks else ''
    comment = f'<!-- {class_name}{kks_comment} -->'

    return f'        {comment}\n        <{control_class} {" ".join(attrs)} />'


def generate_fxml_rectangle(node, geometry: SkinGeometry, node_id: str,
                            graph_scale: float = 1.0) -> str:
    """
    Генерирует FXML Rectangle для элементов без скинов.
    Добавляет KKS как Tooltip через вложенный Text (или комментарий).
    """
    class_name = node.get('class_name', 'unknown')
    color = CLASS_COLORS.get(class_name, DEFAULT_COLOR)
    kks = node.get('kks_full')

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


def generate_fxml_polygon(node, node_id: str, graph_scale: float = 1.0) -> str:
    """
    Генерирует FXML Polygon для элементов с segmentation.
    Points нормализованы относительно layoutX/layoutY (JavaFX семантика).
    """
    segmentation = node.get('segmentation')
    if not segmentation or len(segmentation) < 6:
        return None

    class_name = node.get('class_name', 'unknown')
    color = CLASS_COLORS.get(class_name, DEFAULT_COLOR)
    kks = node.get('kks_full')

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


def generate_fxml_line(edge, nodes, edge_id: str,
                       base_stroke: float = LINE_STROKE_WIDTH,
                       use_diameter: bool = True,
                       graph_scale: float = 1.0) -> Optional[str]:
    """
    Генерирует FXML Line или Polyline для ребра.

    Если ребро содержит waypoints — генерируется <Polyline> через все точки.
    Иначе — простая <Line> от start до end.

    Note:
        Координаты source_point/target_point/waypoints в формате [y, x].
        convert_point() конвертирует [y, x] → (x, y) для FXML.
    """
    endpoints = get_line_endpoints(edge, nodes)
    if not endpoints:
        return None

    start, end = endpoints

    # Все линии одного цвета
    line_color = DEFAULT_LINE_COLOR

    # Масштабирование толщины по диаметру
    diameter_value = edge.get('diameter_value')
    diameter_text = edge.get('diameter_text', '')

    if use_diameter and diameter_value:
        stroke_width = calculate_diameter_stroke(diameter_value, base_stroke, graph_scale)
    else:
        stroke_width = base_stroke

    # Комментарий с диаметром
    diam_info = f' {diameter_text}' if diameter_text else ''
    propagated = ' (propagated)' if edge.get('diameter_propagated') else ''
    comment = f'<!-- {edge_id}{diam_info}{propagated} -->'

    waypoints = edge.get('waypoints', [])
    if waypoints:
        # Polyline: start + waypoints + end
        all_points = [start]
        for wp in waypoints:
            converted = convert_point(wp)
            if converted:
                all_points.append(converted)
        all_points.append(end)
        # Плоский список координат через запятые (совместимый с JavaFX формат)
        points_str = ",".join(f"{c:.1f}" for x, y in all_points for c in (x, y))

        attrs = [
            f'fx:id="{escape(str(edge_id))}"',
            f'points="{points_str}"',
            f'stroke="{line_color}"',
            f'strokeWidth="{stroke_width:.1f}"',
        ]
        return f'        {comment}\n        <Polyline {" ".join(attrs)} />'
    else:
        attrs = [
            f'fx:id="{escape(str(edge_id))}"',
            f'startX="{start[0]:.1f}"',
            f'startY="{start[1]:.1f}"',
            f'endX="{end[0]:.1f}"',
            f'endY="{end[1]:.1f}"',
            f'stroke="{line_color}"',
            f'strokeWidth="{stroke_width:.1f}"',
        ]
        return f'        {comment}\n        <Line {" ".join(attrs)} />'


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

    # Обновить image_size
    graph_data.setdefault('graph', {})['image_size'] = [page_h_px, page_w_px]

    return page_w_px, page_h_px, scale


def generate_fxml(graph_data: dict, stroke_width: float = LINE_STROKE_WIDTH,
                  page_size: str = None, use_diameter: bool = True) -> str:
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
        'lines_with_diameter': 0,
    }

    # Обрабатываем узлы
    for node_id, node in nodes.items():
        if node['type'] != 'equipment':
            continue

        # Получаем подключения
        connections = get_node_connections(node_id, edges, nodes)

        # Вычисляем геометрию
        geometry = calculate_skin_geometry(node, connections)

        # Пробуем найти скин
        skin_info = get_skin_info(node)

        if skin_info:
            fxml = generate_fxml_control(node, geometry, node_id, graph_scale)
            if fxml:
                control_elements.append(fxml)
                stats['controls'] += 1
                if node.get('kks_full'):
                    stats['controls_with_kks'] += 1
        elif node.get('contours_all'):
            # Множественные полигоны из contour_extractor
            # (drossel = 2 полигона, voronka = 1-2 полигона и т.д.)
            class_name = node.get('class_name', 'unknown')
            color = CLASS_COLORS.get(class_name, DEFAULT_COLOR)
            kks = node.get('kks_full')
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
            fxml = generate_fxml_polygon(node, node_id, graph_scale)
            if fxml:
                polygon_elements.append(fxml)
                stats['polygons'] += 1
        else:
            fxml = generate_fxml_rectangle(node, geometry, node_id, graph_scale)
            if fxml:
                rectangle_elements.append(fxml)
                stats['rectangles'] += 1
                if node.get('kks_full'):
                    stats['rectangles_with_kks'] += 1

    # Обрабатываем рёбра
    for edge in edges:
        fxml = generate_fxml_line(edge, nodes, edge['id'], stroke_width,
                                  use_diameter=use_diameter, graph_scale=graph_scale)
        if fxml:
            line_elements.append(fxml)
            if edge.get('diameter_value'):
                stats['lines_with_diameter'] += 1

    # Собираем FXML
    fxml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '',
        '<?import javafx.scene.layout.*?>',
        '<?import javafx.scene.shape.*?>',
        '<?import ru.get.common.controls.*?>',
        '',
        f'<AnchorPane fx:id="root" xmlns="http://javafx.com/javafx/17" xmlns:fx="http://javafx.com/fxml/1"',
        f'    prefWidth="{pane_width}" prefHeight="{pane_height}">',
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
    kks_count = sum(1 for n in nodes if n.get('kks_full'))

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
    print(f"  Lines with diameter scaling:   {stats.get('lines_with_diameter', 0)}")

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

"""
Интерактивная HTML визуализация графа P&ID схем.

Особенности:
- Разноцветные узлы по классам (из YOLO разметки)
- Развилки/повороты отображаются чуть больше линии (контакт, не полноценный узел)
- Изолированные узлы подсвечиваются красным
- Terminal edges (в пустоту) НЕ визуализируются
- Hover tooltip с названием класса
"""

import numpy as np
import json
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import base64
import io

try:
    from PIL import Image
except ImportError:
    Image = None


# Имена классов из YOLO разметки (индекс = class_id)
CLASS_NAMES = [
    'armatura_ruchn',           # 0
    'klapan_obratn',            # 1
    'regulator_ruchn',          # 2
    'armatura_electro',         # 3
    'regulator_electro',        # 4
    'drossel',                  # 5
    'perehod',                  # 6
    'klapan_obratn_seroprivod', # 7
    'armatura_seroprivod',      # 8
    'regulator_seroprivod',     # 9
    'armatura_membr_electro',   # 10
    'nasos',                    # 11
    'ventilaytor',              # 12
    'predohran',                # 13
    'condensatootvod',          # 14
    'rashodomernaya_shaiba',    # 15
    'vodostruiniy_nasos',       # 16
    'teploobmen',               # 17
    'zaglushka',                # 18
    'gidrozatvor',              # 19
    'bak',                      # 20
    'voronka',                  # 21
    'filtr_meh',                # 22
    'separator',                # 23
    'kapleulov',                # 24
    'celindr_turb',             # 25
    'redukcion_ustr',           # 26
    'bistro_redukc_ustr',       # 27
    'separator_paro',           # 28
    'dearator',                 # 29
    'silfonnii_kompensator',    # 30
    'electronagrevat',          # 31
    'smotrowoe_steclo',         # 32
    'datchik',                  # 33
    'annotation',               # 34
    'output',                   # 35
    'truba',                    # 36
    'unknow',                   # 37
    'strelka',                  # 38
]

# Цветовая палитра для классов узлов (яркие, различимые цвета)
CLASS_COLORS = {
    # Арматура и клапаны
    'armatura_ruchn': '#2ecc71',           # зелёный
    'armatura_electro': '#27ae60',         # тёмно-зелёный
    'armatura_seroprivod': '#1abc9c',      # бирюзовый
    'armatura_membr_electro': '#16a085',   # морской
    
    'klapan_obratn': '#3498db',            # синий
    'klapan_obratn_seroprivod': '#2980b9', # тёмно-синий
    
    'regulator_ruchn': '#9b59b6',          # фиолетовый
    'regulator_electro': '#8e44ad',        # тёмно-фиолетовый
    'regulator_seroprivod': '#6c3483',     # ещё темнее
    
    # Оборудование
    'nasos': '#e74c3c',                    # красный
    'vodostruiniy_nasos': '#c0392b',       # тёмно-красный
    'ventilaytor': '#e67e22',              # оранжевый
    'teploobmen': '#f39c12',               # жёлтый-оранжевый
    'bak': '#f1c40f',                      # жёлтый
    'separator': '#d35400',                # ржавый
    'separator_paro': '#a04000',           # тёмно-ржавый
    'dearator': '#7d3c98',                 # пурпурный
    'filtr_meh': '#5dade2',                # светло-синий
    'celindr_turb': '#48c9b0',             # мятный
    
    # Мелкие элементы
    'drossel': '#85929e',                  # серо-синий
    'perehod': '#aab7b8',                  # светло-серый
    'zaglushka': '#717d7e',                # серый
    'voronka': '#f5b041',                  # золотой
    'predohran': '#ec7063',                # светло-красный
    'condensatootvod': '#76d7c4',          # светло-бирюзовый
    'rashodomernaya_shaiba': '#bb8fce',    # светло-фиолетовый
    'gidrozatvor': '#7fb3d5',              # небесный
    'kapleulov': '#f7dc6f',                # светло-жёлтый
    'redukcion_ustr': '#af7ac5',           # сиреневый
    'bistro_redukc_ustr': '#a569bd',       # тёмно-сиреневый
    'silfonnii_kompensator': '#45b39d',    # зелёно-синий
    'electronagrevat': '#eb984e',          # персиковый
    'smotrowoe_steclo': '#aed6f1',         # бледно-синий
    
    # Датчики и аннотации
    'datchik': '#58d68d',                  # светло-зелёный
    'annotation': '#fadbd8',               # розоватый (бледный)
    'output': '#d5dbdb',                   # светло-серый
    'truba': '#808b96',                    # средний серый
    'strelka': '#5d6d7e',                  # тёмно-серый
    
    # Unknown
    'unknow': '#e74c3c',                   # красный (выделяется!)
    
    # Junction types (развилки - маленькие)
    'junction': '#95a5a6',                 # серый
    'turn': '#bdc3c7',                     # светло-серый
    'connection': '#7f8c8d',               # тёмно-серый
    'invalid_bridge': '#e74c3c',           # красный
    
    # Default
    'default': '#3498db',                  # синий
}

# Размеры для разных типов элементов
ELEMENT_SIZES = {
    'equipment': 12,      # Полноценные узлы - большие
    'junction': 6,        # Развилки - маленькие (контакт линий)
    'turn': 5,            # Повороты - ещё меньше
    'connection': 6,      # Connections
    'invalid_bridge': 8,  # Invalid bridges - средние
    'isolated': 10,       # Изолированные
}

# Типы узлов которые считаются junction (не equipment)
JUNCTION_TYPES = {'junction', 'turn', 'connection', 'invalid_bridge'}

# Минимальная площадь для equipment (меньше = junction)
MIN_EQUIPMENT_AREA = 100


def load_yolo_labels(labels_path: str, image_shape: Tuple[int, int]) -> Dict:
    """
    Загрузить YOLO разметку и конвертировать в словарь bbox -> class.
    
    Args:
        labels_path: Путь к файлу .txt с YOLO разметкой
        image_shape: (height, width) изображения
        
    Returns:
        Словарь {(x_min, y_min, x_max, y_max): class_id}
    """
    labels = {}
    height, width = image_shape
    
    if not Path(labels_path).exists():
        return labels
    
    with open(labels_path, 'r') as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 5:
                class_id = int(parts[0])
                x_center = float(parts[1]) * width
                y_center = float(parts[2]) * height
                w = float(parts[3]) * width
                h = float(parts[4]) * height
                
                x_min = int(x_center - w / 2)
                y_min = int(y_center - h / 2)
                x_max = int(x_center + w / 2)
                y_max = int(y_center + h / 2)
                
                labels[(x_min, y_min, x_max, y_max)] = class_id
    
    return labels


def match_node_to_yolo(node: Dict, yolo_labels: Dict, class_names: List[str] = None) -> Tuple[str, int]:
    """
    Сопоставить узел с YOLO разметкой по IoU.
    
    Args:
        node: Словарь узла с bbox
        yolo_labels: Словарь YOLO меток
        class_names: Список имён классов (если None - используется CLASS_NAMES)
        
    Returns:
        (class_name, class_id)
    """
    if class_names is None:
        class_names = CLASS_NAMES
    
    node_type = node.get('type', 'unknown')
    
    # Если нет YOLO меток
    if not yolo_labels:
        # junction с маленькой площадью = junction
        if node_type == 'junction' and node['area'] < MIN_EQUIPMENT_AREA:
            return 'junction', -1
        # invalid_bridge оставляем как есть
        if node_type == 'invalid_bridge':
            return 'invalid_bridge', -1
        # остальные = unknow
        return 'unknow', 37
    
    node_bbox = node['bbox']  # (x_min, y_min, x_max, y_max)
    nx1, ny1, nx2, ny2 = node_bbox
    node_area = (nx2 - nx1) * (ny2 - ny1)
    
    if node_area <= 0:
        return 'junction', -1
    
    best_iou = 0
    best_class = -1
    
    for (x1, y1, x2, y2), class_id in yolo_labels.items():
        # Вычисляем IoU
        inter_x1 = max(nx1, x1)
        inter_y1 = max(ny1, y1)
        inter_x2 = min(nx2, x2)
        inter_y2 = min(ny2, y2)
        
        if inter_x2 > inter_x1 and inter_y2 > inter_y1:
            inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
            yolo_area = (x2 - x1) * (y2 - y1)
            union_area = node_area + yolo_area - inter_area
            iou = inter_area / union_area if union_area > 0 else 0
            
            if iou > best_iou:
                best_iou = iou
                best_class = class_id
    
    # Порог IoU для совпадения
    if best_iou >= 0.1 and best_class >= 0 and best_class < len(class_names):
        return class_names[best_class], best_class
    else:
        # Не нашли в YOLO
        # Маленькие объекты без разметки = junction
        if node['area'] < MIN_EQUIPMENT_AREA:
            return 'junction', -1
        # Большие объекты без разметки = unknow
        return 'unknow', 37


def get_node_color(class_name: str, is_isolated: bool = False) -> str:
    """Получить цвет для узла по классу."""
    if is_isolated:
        return '#e74c3c'  # Красный для изолированных
    
    # Проверяем точное совпадение
    if class_name in CLASS_COLORS:
        return CLASS_COLORS[class_name]
    
    # Проверяем частичное совпадение
    class_lower = class_name.lower()
    for key, color in CLASS_COLORS.items():
        if key in class_lower or class_lower in key:
            return color
    
    # unknow = красный
    if 'unknow' in class_lower or 'unknown' in class_lower:
        return '#e74c3c'
    
    return CLASS_COLORS['default']


def get_node_size(node: Dict, class_name: str) -> int:
    """Получить размер для узла."""
    if node['degree'] == 0:
        return ELEMENT_SIZES['isolated']
    
    node_type = node.get('type', 'unknown')
    
    # Развилки, повороты, connections - маленькие
    if node_type in JUNCTION_TYPES:
        return ELEMENT_SIZES.get(node_type, ELEMENT_SIZES['junction'])
    
    # Маленькая площадь = junction
    if node['area'] < MIN_EQUIPMENT_AREA:
        return ELEMENT_SIZES['junction']
    
    return ELEMENT_SIZES['equipment']


def create_interactive_html(
    nodes: List[Dict],
    edges: List[Dict],
    skeleton: np.ndarray,
    output_path: str,
    original_image: Optional[np.ndarray] = None,
    yolo_labels_path: Optional[str] = None,
    class_names: Optional[List[str]] = None,
    valid_bridges_mask: Optional[np.ndarray] = None,
    title: str = "P&ID Graph Visualization"
) -> str:
    """
    Создать интерактивную HTML визуализацию.
    
    Args:
        nodes: Список узлов
        edges: Список рёбер
        skeleton: Маска скелета
        output_path: Путь для сохранения HTML
        original_image: Оригинальное изображение (опционально)
        yolo_labels_path: Путь к YOLO разметке
        class_names: Список имён классов
        valid_bridges_mask: Маска валидных мостов
        title: Заголовок страницы
        
    Returns:
        Путь к созданному файлу
    """
    height, width = skeleton.shape
    
    # Загружаем YOLO метки если есть
    yolo_labels = {}
    if yolo_labels_path:
        yolo_labels = load_yolo_labels(yolo_labels_path, skeleton.shape)
    
    # Используем переданные class_names или дефолтные
    names = class_names if class_names else CLASS_NAMES
    
    # Подготавливаем данные узлов
    nodes_data = []
    class_stats = {}
    
    for node in nodes:
        cy, cx = node['centroid']
        is_isolated = node['degree'] == 0
        
        # Определяем класс
        class_name, class_id = match_node_to_yolo(node, yolo_labels, names)
        
        # Определяем, это junction/turn или equipment
        # Если найден класс в YOLO (class_id >= 0) - это equipment
        # junction - маленькие объекты без YOLO разметки
        node_type = node.get('type', 'unknown')
        is_junction = (class_name == 'junction' or 
                       (class_id < 0 and node['area'] < MIN_EQUIPMENT_AREA))
        
        color = get_node_color(class_name, is_isolated)
        size = get_node_size(node, class_name)
        
        # Статистика по классам
        class_stats[class_name] = class_stats.get(class_name, 0) + 1
        
        node_info = {
            'id': node['id'],
            'x': int(cx),
            'y': int(cy),
            'color': color,
            'size': size,
            'class_name': class_name,
            'class_id': class_id,
            'degree': node['degree'],
            'area': node['area'],
            'type': node_type,
            'is_isolated': is_isolated,
            'is_junction': is_junction,
            'bbox': node['bbox']
        }
        nodes_data.append(node_info)
    
    # Подготавливаем данные рёбер
    # ВАЖНО: terminal edges (в пустоту) НЕ визуализируем
    edges_data = []
    skipped_terminal = 0
    
    for edge in edges:
        # Пропускаем terminal edges (ребро в никуда)
        if edge.get('is_terminal', False):
            skipped_terminal += 1
            continue
        
        # Проверяем что есть путь в edge (для JSON без paths)
        path = edge.get('path', [])
        if len(path) < 2:
            # Если нет пути - строим линию между центроидами узлов
            from_node = next((n for n in nodes_data if n['id'] == edge.get('source', edge.get('from'))), None)
            to_node = next((n for n in nodes_data if n['id'] == edge.get('target', edge.get('to'))), None)
            
            if from_node and to_node:
                path = [(from_node['y'], from_node['x']), (to_node['y'], to_node['x'])]
            else:
                continue
        
        # Упрощаем путь для отрисовки (каждый N-й пиксель)
        step = max(1, len(path) // 50)
        simplified_path = path[::step]
        if path[-1] not in simplified_path:
            simplified_path.append(path[-1])
        
        # Определяем цвет ребра
        edge_color = edge.get('color')
        if edge_color == 'BLUE':
            color = '#3498db'  # синий
        elif edge_color == 'YELLOW':
            color = '#f1c40f'  # жёлтый
        else:
            color = '#2ecc71'  # зелёный
        
        edge_info = {
            'from': edge.get('source', edge.get('from')),
            'to': edge.get('target', edge.get('to')),
            'path': [[int(y), int(x)] for y, x in simplified_path],
            'color': color,
            'dash': False,
            'length': edge.get('length', 0),
            'is_terminal': False
        }
        edges_data.append(edge_info)
    
    if skipped_terminal > 0:
        print(f"  Пропущено terminal edges: {skipped_terminal}")
    
    # Создаём фоновое изображение
    if original_image is not None:
        bg_image = original_image
    else:
        bg_image = np.ones((height, width), dtype=np.uint8) * 255
        bg_image[skeleton] = 200
    
    # Конвертируем в base64
    if Image:
        if len(bg_image.shape) == 2:
            img_pil = Image.fromarray(bg_image, mode='L')
        else:
            img_pil = Image.fromarray(bg_image)
        
        buffer = io.BytesIO()
        img_pil.save(buffer, format='PNG', optimize=True)
        bg_base64 = base64.b64encode(buffer.getvalue()).decode()
    else:
        bg_base64 = ""
    
    # Генерируем HTML
    html_content = generate_html_template(
        nodes_data=nodes_data,
        edges_data=edges_data,
        width=width,
        height=height,
        bg_base64=bg_base64,
        title=title,
        class_stats=class_stats,
        class_colors=CLASS_COLORS
    )
    
    # Сохраняем
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print(f"Интерактивная визуализация сохранена: {output_path}")
    return output_path


def generate_html_template(
    nodes_data: List[Dict],
    edges_data: List[Dict],
    width: int,
    height: int,
    bg_base64: str,
    title: str,
    class_stats: Dict,
    class_colors: Dict
) -> str:
    """Генерация HTML шаблона."""
    
    nodes_json = json.dumps(nodes_data)
    edges_json = json.dumps(edges_data)
    class_stats_json = json.dumps(class_stats)
    
    html = f'''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            overflow: hidden;
        }}
        
        .container {{
            display: flex;
            height: 100vh;
        }}
        
        .sidebar {{
            width: 280px;
            background: #16213e;
            padding: 20px;
            overflow-y: auto;
            border-right: 1px solid #0f3460;
        }}
        
        .sidebar h2 {{
            color: #e94560;
            margin-bottom: 15px;
            font-size: 18px;
        }}
        
        .stats-section {{
            margin-bottom: 20px;
        }}
        
        .stat-item {{
            display: flex;
            justify-content: space-between;
            padding: 8px 12px;
            background: #0f3460;
            border-radius: 6px;
            margin-bottom: 6px;
            font-size: 13px;
        }}
        
        .stat-item .color-dot {{
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 8px;
        }}
        
        .stat-item .label {{
            display: flex;
            align-items: center;
        }}
        
        .legend {{
            margin-top: 20px;
        }}
        
        .legend-title {{
            font-size: 14px;
            color: #888;
            margin-bottom: 10px;
        }}
        
        .legend-item {{
            display: flex;
            align-items: center;
            margin-bottom: 6px;
            font-size: 12px;
        }}
        
        .legend-item .line {{
            width: 30px;
            height: 3px;
            margin-right: 10px;
            border-radius: 2px;
        }}
        
        .legend-item .line.dashed {{
            background: repeating-linear-gradient(
                90deg,
                #95a5a6,
                #95a5a6 5px,
                transparent 5px,
                transparent 10px
            );
        }}
        
        .canvas-container {{
            flex: 1;
            position: relative;
            overflow: hidden;
        }}
        
        #graphCanvas {{
            position: absolute;
            cursor: grab;
        }}
        
        #graphCanvas:active {{
            cursor: grabbing;
        }}
        
        .tooltip {{
            position: fixed;
            background: rgba(22, 33, 62, 0.95);
            border: 1px solid #0f3460;
            border-radius: 8px;
            padding: 12px 16px;
            pointer-events: none;
            opacity: 0;
            transition: opacity 0.15s;
            z-index: 1000;
            max-width: 300px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }}
        
        .tooltip.visible {{
            opacity: 1;
        }}
        
        .tooltip-title {{
            font-weight: 600;
            color: #e94560;
            margin-bottom: 8px;
            font-size: 14px;
        }}
        
        .tooltip-row {{
            display: flex;
            justify-content: space-between;
            font-size: 12px;
            margin-bottom: 4px;
        }}
        
        .tooltip-row .label {{
            color: #888;
        }}
        
        .tooltip-row .value {{
            color: #fff;
            font-weight: 500;
        }}
        
        .tooltip-row .value.isolated {{
            color: #e74c3c;
        }}
        
        .controls {{
            position: absolute;
            bottom: 20px;
            right: 20px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}
        
        .control-btn {{
            width: 40px;
            height: 40px;
            background: #16213e;
            border: 1px solid #0f3460;
            border-radius: 8px;
            color: #fff;
            font-size: 18px;
            cursor: pointer;
            transition: all 0.2s;
        }}
        
        .control-btn:hover {{
            background: #0f3460;
            border-color: #e94560;
        }}
        
        .info-panel {{
            position: absolute;
            top: 20px;
            left: 20px;
            background: rgba(22, 33, 62, 0.9);
            padding: 12px 16px;
            border-radius: 8px;
            font-size: 12px;
            border: 1px solid #0f3460;
        }}
        
        .search-box {{
            margin-bottom: 15px;
        }}
        
        .search-box input {{
            width: 100%;
            padding: 10px 12px;
            background: #0f3460;
            border: 1px solid #1a1a2e;
            border-radius: 6px;
            color: #fff;
            font-size: 13px;
        }}
        
        .search-box input:focus {{
            outline: none;
            border-color: #e94560;
        }}
        
        .search-box input::placeholder {{
            color: #666;
        }}
        
        .filter-section {{
            margin-bottom: 15px;
        }}
        
        .filter-section label {{
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 12px;
            margin-bottom: 6px;
            cursor: pointer;
        }}
        
        .filter-section input[type="checkbox"] {{
            accent-color: #e94560;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="sidebar">
            <h2>🔍 P&ID Graph</h2>
            
            <div class="search-box">
                <input type="text" id="searchInput" placeholder="Поиск узла по ID или классу...">
            </div>
            
            <div class="filter-section">
                <label>
                    <input type="checkbox" id="showIsolated" checked>
                    Показать изолированные узлы
                </label>
                <label>
                    <input type="checkbox" id="showJunctions" checked>
                    Показать развилки/повороты
                </label>
                <label>
                    <input type="checkbox" id="showEdges" checked>
                    Показать рёбра
                </label>
            </div>
            
            <div class="stats-section">
                <h3 style="font-size: 14px; margin-bottom: 10px; color: #888;">Статистика</h3>
                <div id="statsContainer"></div>
            </div>
            
            <div class="legend">
                <div class="legend-title">Типы рёбер</div>
                <div class="legend-item">
                    <div class="line" style="background: #2ecc71;"></div>
                    <span>Обычное ребро</span>
                </div>
                <div class="legend-item">
                    <div class="line" style="background: #3498db;"></div>
                    <span>Через мост (pipe 1)</span>
                </div>
                <div class="legend-item">
                    <div class="line" style="background: #f1c40f;"></div>
                    <span>Через мост (pipe 2)</span>
                </div>
                <div class="legend-item">
                    <div class="line dashed"></div>
                    <span>Terminal (концевое)</span>
                </div>
            </div>
        </div>
        
        <div class="canvas-container">
            <canvas id="graphCanvas"></canvas>
            
            <div class="info-panel">
                <div>Узлов: <strong id="nodeCount">0</strong></div>
                <div>Рёбер: <strong id="edgeCount">0</strong></div>
                <div>Масштаб: <strong id="zoomLevel">100%</strong></div>
            </div>
            
            <div class="controls">
                <button class="control-btn" id="zoomIn">+</button>
                <button class="control-btn" id="zoomOut">−</button>
                <button class="control-btn" id="resetView">⟲</button>
            </div>
        </div>
    </div>
    
    <div class="tooltip" id="tooltip">
        <div class="tooltip-title" id="tooltipTitle"></div>
        <div class="tooltip-content" id="tooltipContent"></div>
    </div>
    
    <script>
        // Данные графа
        const nodes = {nodes_json};
        const edges = {edges_json};
        const classStats = {class_stats_json};
        const imageWidth = {width};
        const imageHeight = {height};
        const bgImageBase64 = "{bg_base64}";
        
        // Canvas и контекст
        const canvas = document.getElementById('graphCanvas');
        const ctx = canvas.getContext('2d');
        
        // Состояние просмотра
        let scale = 1;
        let offsetX = 0;
        let offsetY = 0;
        let isDragging = false;
        let lastMouseX = 0;
        let lastMouseY = 0;
        let hoveredNode = null;
        
        // Фильтры
        let showIsolated = true;
        let showJunctions = true;
        let showEdges = true;
        let searchQuery = '';
        
        // Фоновое изображение
        let bgImage = null;
        
        // Инициализация
        function init() {{
            resizeCanvas();
            window.addEventListener('resize', resizeCanvas);
            
            // Загрузка фона
            if (bgImageBase64) {{
                bgImage = new Image();
                bgImage.onload = () => render();
                bgImage.src = 'data:image/png;base64,' + bgImageBase64;
            }}
            
            // События мыши
            canvas.addEventListener('wheel', handleWheel);
            canvas.addEventListener('mousedown', handleMouseDown);
            canvas.addEventListener('mousemove', handleMouseMove);
            canvas.addEventListener('mouseup', handleMouseUp);
            canvas.addEventListener('mouseleave', handleMouseLeave);
            
            // Кнопки управления
            document.getElementById('zoomIn').addEventListener('click', () => zoom(1.2));
            document.getElementById('zoomOut').addEventListener('click', () => zoom(0.8));
            document.getElementById('resetView').addEventListener('click', resetView);
            
            // Фильтры
            document.getElementById('showIsolated').addEventListener('change', (e) => {{
                showIsolated = e.target.checked;
                render();
            }});
            document.getElementById('showJunctions').addEventListener('change', (e) => {{
                showJunctions = e.target.checked;
                render();
            }});
            document.getElementById('showEdges').addEventListener('change', (e) => {{
                showEdges = e.target.checked;
                render();
            }});
            
            // Поиск
            document.getElementById('searchInput').addEventListener('input', (e) => {{
                searchQuery = e.target.value.toLowerCase();
                render();
            }});
            
            // Статистика
            updateStats();
            updateInfo();
            
            // Начальный масштаб
            resetView();
        }}
        
        function resizeCanvas() {{
            const container = canvas.parentElement;
            canvas.width = container.clientWidth;
            canvas.height = container.clientHeight;
            render();
        }}
        
        function resetView() {{
            const container = canvas.parentElement;
            const scaleX = container.clientWidth / imageWidth;
            const scaleY = container.clientHeight / imageHeight;
            scale = Math.min(scaleX, scaleY) * 0.9;
            
            offsetX = (container.clientWidth - imageWidth * scale) / 2;
            offsetY = (container.clientHeight - imageHeight * scale) / 2;
            
            render();
            updateZoomLevel();
        }}
        
        function zoom(factor) {{
            const centerX = canvas.width / 2;
            const centerY = canvas.height / 2;
            
            const worldX = (centerX - offsetX) / scale;
            const worldY = (centerY - offsetY) / scale;
            
            scale *= factor;
            scale = Math.max(0.1, Math.min(10, scale));
            
            offsetX = centerX - worldX * scale;
            offsetY = centerY - worldY * scale;
            
            render();
            updateZoomLevel();
        }}
        
        function handleWheel(e) {{
            e.preventDefault();
            const factor = e.deltaY > 0 ? 0.9 : 1.1;
            
            const rect = canvas.getBoundingClientRect();
            const mouseX = e.clientX - rect.left;
            const mouseY = e.clientY - rect.top;
            
            const worldX = (mouseX - offsetX) / scale;
            const worldY = (mouseY - offsetY) / scale;
            
            scale *= factor;
            scale = Math.max(0.1, Math.min(10, scale));
            
            offsetX = mouseX - worldX * scale;
            offsetY = mouseY - worldY * scale;
            
            render();
            updateZoomLevel();
        }}
        
        function handleMouseDown(e) {{
            isDragging = true;
            lastMouseX = e.clientX;
            lastMouseY = e.clientY;
            canvas.style.cursor = 'grabbing';
        }}
        
        function handleMouseMove(e) {{
            const rect = canvas.getBoundingClientRect();
            const mouseX = e.clientX - rect.left;
            const mouseY = e.clientY - rect.top;
            
            if (isDragging) {{
                offsetX += e.clientX - lastMouseX;
                offsetY += e.clientY - lastMouseY;
                lastMouseX = e.clientX;
                lastMouseY = e.clientY;
                render();
            }} else {{
                // Проверка наведения на узел
                const worldX = (mouseX - offsetX) / scale;
                const worldY = (mouseY - offsetY) / scale;
                
                let found = null;
                for (const node of nodes) {{
                    if (!shouldShowNode(node)) continue;
                    
                    const dx = worldX - node.x;
                    const dy = worldY - node.y;
                    const dist = Math.sqrt(dx * dx + dy * dy);
                    
                    if (dist < node.size * 1.5) {{
                        found = node;
                        break;
                    }}
                }}
                
                if (found !== hoveredNode) {{
                    hoveredNode = found;
                    render();
                    updateTooltip(e.clientX, e.clientY);
                }}
            }}
        }}
        
        function handleMouseUp() {{
            isDragging = false;
            canvas.style.cursor = 'grab';
        }}
        
        function handleMouseLeave() {{
            isDragging = false;
            hoveredNode = null;
            canvas.style.cursor = 'grab';
            hideTooltip();
            render();
        }}
        
        function shouldShowNode(node) {{
            if (!showIsolated && node.is_isolated) return false;
            if (!showJunctions && node.is_junction) return false;
            
            if (searchQuery) {{
                const matchId = node.id.toLowerCase().includes(searchQuery);
                const matchClass = node.class_name.toLowerCase().includes(searchQuery);
                if (!matchId && !matchClass) return false;
            }}
            
            return true;
        }}
        
        function render() {{
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            
            ctx.save();
            ctx.translate(offsetX, offsetY);
            ctx.scale(scale, scale);
            
            // Фон
            if (bgImage) {{
                ctx.drawImage(bgImage, 0, 0, imageWidth, imageHeight);
            }} else {{
                ctx.fillStyle = '#f5f5f5';
                ctx.fillRect(0, 0, imageWidth, imageHeight);
            }}
            
            // Рёбра
            if (showEdges) {{
                for (const edge of edges) {{
                    drawEdge(edge);
                }}
            }}
            
            // Узлы
            for (const node of nodes) {{
                if (shouldShowNode(node)) {{
                    drawNode(node);
                }}
            }}
            
            ctx.restore();
        }}
        
        function drawEdge(edge) {{
            if (edge.path.length < 2) return;
            
            ctx.beginPath();
            ctx.moveTo(edge.path[0][1], edge.path[0][0]);
            
            for (let i = 1; i < edge.path.length; i++) {{
                ctx.lineTo(edge.path[i][1], edge.path[i][0]);
            }}
            
            ctx.strokeStyle = edge.color;
            ctx.lineWidth = 2 / scale;
            
            if (edge.dash) {{
                ctx.setLineDash([5 / scale, 5 / scale]);
            }} else {{
                ctx.setLineDash([]);
            }}
            
            ctx.stroke();
            ctx.setLineDash([]);
        }}
        
        function drawNode(node) {{
            const x = node.x;
            const y = node.y;
            const size = node.size;
            
            // Подсветка при поиске
            const isHighlighted = searchQuery && (
                node.id.toLowerCase().includes(searchQuery) ||
                node.class_name.toLowerCase().includes(searchQuery)
            );
            
            // Подсветка при наведении
            const isHovered = hoveredNode === node;
            
            ctx.beginPath();
            
            if (node.is_isolated) {{
                // Изолированные - крестик
                const crossSize = size;
                ctx.strokeStyle = node.color;
                ctx.lineWidth = 2 / scale;
                ctx.moveTo(x - crossSize, y - crossSize);
                ctx.lineTo(x + crossSize, y + crossSize);
                ctx.moveTo(x + crossSize, y - crossSize);
                ctx.lineTo(x - crossSize, y + crossSize);
                ctx.stroke();
            }} else if (node.is_junction) {{
                // Развилки - маленькие квадраты (контакт линий)
                const halfSize = size / 2;
                ctx.fillStyle = node.color;
                ctx.globalAlpha = isHovered ? 1.0 : 0.8;
                ctx.fillRect(x - halfSize, y - halfSize, size, size);
                
                if (isHovered || isHighlighted) {{
                    ctx.strokeStyle = '#fff';
                    ctx.lineWidth = 2 / scale;
                    ctx.strokeRect(x - halfSize, y - halfSize, size, size);
                }}
            }} else {{
                // Обычные узлы - круги
                ctx.arc(x, y, size, 0, Math.PI * 2);
                ctx.fillStyle = node.color;
                ctx.globalAlpha = isHovered ? 1.0 : 0.85;
                ctx.fill();
                
                if (isHovered || isHighlighted) {{
                    ctx.strokeStyle = '#fff';
                    ctx.lineWidth = 3 / scale;
                    ctx.stroke();
                }}
            }}
            
            ctx.globalAlpha = 1.0;
        }}
        
        function updateTooltip(mouseX, mouseY) {{
            const tooltip = document.getElementById('tooltip');
            
            if (hoveredNode) {{
                const node = hoveredNode;
                
                document.getElementById('tooltipTitle').textContent = node.class_name;
                
                let content = `
                    <div class="tooltip-row">
                        <span class="label">ID:</span>
                        <span class="value">${{node.id}}</span>
                    </div>
                    <div class="tooltip-row">
                        <span class="label">Тип:</span>
                        <span class="value">${{node.type}}</span>
                    </div>
                    <div class="tooltip-row">
                        <span class="label">Degree:</span>
                        <span class="value${{node.is_isolated ? ' isolated' : ''}}">${{node.degree}}${{node.is_isolated ? ' (изолирован)' : ''}}</span>
                    </div>
                    <div class="tooltip-row">
                        <span class="label">Площадь:</span>
                        <span class="value">${{node.area}} px</span>
                    </div>
                    <div class="tooltip-row">
                        <span class="label">Позиция:</span>
                        <span class="value">(${{node.x}}, ${{node.y}})</span>
                    </div>
                `;
                
                if (node.class_id >= 0) {{
                    content += `
                        <div class="tooltip-row">
                            <span class="label">Class ID:</span>
                            <span class="value">${{node.class_id}}</span>
                        </div>
                    `;
                }}
                
                document.getElementById('tooltipContent').innerHTML = content;
                
                tooltip.style.left = (mouseX + 15) + 'px';
                tooltip.style.top = (mouseY + 15) + 'px';
                tooltip.classList.add('visible');
            }} else {{
                hideTooltip();
            }}
        }}
        
        function hideTooltip() {{
            document.getElementById('tooltip').classList.remove('visible');
        }}
        
        function updateStats() {{
            const container = document.getElementById('statsContainer');
            let html = '';
            
            // Сортируем по количеству
            const sorted = Object.entries(classStats).sort((a, b) => b[1] - a[1]);
            
            for (const [className, count] of sorted) {{
                const color = getClassColor(className);
                html += `
                    <div class="stat-item">
                        <span class="label">
                            <span class="color-dot" style="background: ${{color}};"></span>
                            ${{className}}
                        </span>
                        <span>${{count}}</span>
                    </div>
                `;
            }}
            
            container.innerHTML = html;
        }}
        
        function getClassColor(className) {{
            const colors = {{
                'valve': '#2ecc71',
                'pump': '#3498db',
                'tank': '#9b59b6',
                'compressor': '#e74c3c',
                'heat_exchanger': '#f39c12',
                'reactor': '#1abc9c',
                'junction': '#95a5a6',
                'turn': '#bdc3c7',
                'connection': '#7f8c8d',
                'invalid_bridge': '#e74c3c',
                'unknown': '#95a5a6',
                'default': '#3498db'
            }};
            
            if (colors[className]) return colors[className];
            
            // Проверка частичного совпадения
            for (const [key, color] of Object.entries(colors)) {{
                if (className.toLowerCase().includes(key) || key.includes(className.toLowerCase())) {{
                    return color;
                }}
            }}
            
            return colors['default'];
        }}
        
        function updateInfo() {{
            document.getElementById('nodeCount').textContent = nodes.length;
            document.getElementById('edgeCount').textContent = edges.length;
        }}
        
        function updateZoomLevel() {{
            document.getElementById('zoomLevel').textContent = Math.round(scale * 100) + '%';
        }}
        
        // Запуск
        init();
    </script>
</body>
</html>'''
    
    return html


def plot_graph_interactive(
    skeleton: np.ndarray,
    labeled_nodes: np.ndarray,
    nodes: List[Dict],
    edges: List[Dict],
    output_path: str,
    original_image: Optional[np.ndarray] = None,
    yolo_labels_path: Optional[str] = None,
    class_names: Optional[List[str]] = None,
    valid_bridges_mask: Optional[np.ndarray] = None,
    title: str = "P&ID Graph"
):
    """
    Wrapper функция для совместимости с существующим API.
    """
    return create_interactive_html(
        nodes=nodes,
        edges=edges,
        skeleton=skeleton,
        output_path=output_path,
        original_image=original_image,
        yolo_labels_path=yolo_labels_path,
        class_names=class_names,
        valid_bridges_mask=valid_bridges_mask,
        title=title
    )

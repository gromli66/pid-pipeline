"""
Визуализация графа P&ID схем с цветной маркировкой.

Цветовая схема:
- РЁБРА:
  * Зелёный: обычные рёбра (не через мосты)
  * Синий: рёбра через труба 1 моста
  * Жёлтый: рёбра через труба 2 моста
  * Серый (пунктир): terminal edges

- УЗЛЫ:
  * Зелёный (полупрозрачный): связанные узлы
  * Оранжевый (полупрозрачный): невалидные мосты
  * Красный (полупрозрачный): изолированные узлы

- МОСТЫ:
  * Синий bbox: валидные мосты
"""
import numpy as np
import cv2
import matplotlib.pyplot as plt
from typing import List, Dict, Optional, Tuple
from scipy import ndimage


# Цветовая схема (BGR формат для OpenCV)
EDGE_NORMAL = (0, 255, 0)           # зелёный
EDGE_PIPE1 = (255, 0, 0)            # синий
EDGE_PIPE2 = (0, 255, 255)          # жёлтый
EDGE_TERMINAL = (128, 128, 128)     # серый

NODE_CONNECTED = (0, 255, 0)        # зелёный
NODE_INVALID_BRIDGE = (0, 165, 255) # оранжевый
NODE_ISOLATED = (0, 0, 255)         # красный
NODE_ALPHA = 0.3                     # прозрачность

BRIDGE_VALID = (255, 0, 0)          # синий
BRIDGE_THICKNESS = 2

# Толщина линий (одинаковая для всех типов рёбер)
EDGE_THICKNESS = 2


def plot_graph_overlay(skeleton: np.ndarray,
                      labeled_nodes: np.ndarray,
                      nodes: List[Dict],
                      edges: List[Dict],
                      output_path: str,
                      original_image: Optional[np.ndarray] = None,
                      show_node_labels: bool = False,
                      dpi: int = 150,
                      valid_bridges_mask: np.ndarray = None,
                      bridge_info: Dict = None):
    """
    Визуализация графа с цветной маркировкой рёбер и узлов.
    """
    height, width = skeleton.shape

    # Создать RGB изображение
    if original_image is not None:
        # Использовать оригинал как фон
        img_rgb = cv2.cvtColor(original_image, cv2.COLOR_GRAY2BGR)
    else:
        # Белый фон + серый скелет
        img_rgb = np.ones((height, width, 3), dtype=np.uint8) * 255
        img_rgb[skeleton] = [200, 200, 200]

    # === ШАГ 1: Нарисовать УЗЛЫ (полупрозрачные заливки) ===
    for node in nodes:
        label_id = node.get('label_id')
        bbox = node.get('bbox')

        if label_id and labeled_nodes is not None:
            node_mask = (labeled_nodes == label_id)
        elif bbox:
            node_mask = np.zeros((height, width), dtype=bool)
            x_min, y_min, x_max, y_max = bbox
            node_mask[y_min:y_max, x_min:x_max] = True
        else:
            continue

        if node['type'] == 'invalid_bridge':
            color = NODE_INVALID_BRIDGE
        elif node['degree'] == 0:
            # Изолированные узлы - красная заливка
            color = NODE_ISOLATED
        else:
            color = NODE_CONNECTED

        # Всегда рисуем заливку
        overlay = img_rgb.copy()
        overlay[node_mask] = color
        img_rgb = cv2.addWeighted(img_rgb, 1 - NODE_ALPHA, overlay, NODE_ALPHA, 0)

    # === ШАГ 2: Нарисовать РЁБРА ===
    for edge in edges:
        path = edge['path']

        if edge['is_terminal']:
            color = EDGE_TERMINAL
            thickness = 1
            draw_path_dashed(img_rgb, path, color, thickness)
        else:
            edge_color_type = edge.get('color', None)

            if edge_color_type == 'BLUE':
                color = EDGE_PIPE1
            elif edge_color_type == 'YELLOW':
                color = EDGE_PIPE2
            else:
                color = EDGE_NORMAL
            
            # Одинаковая толщина для всех типов рёбер
            draw_path(img_rgb, path, color, EDGE_THICKNESS)

    # === ШАГ 3: BBOX ВАЛИДНЫХ МОСТОВ ===
    if valid_bridges_mask is not None:
        labeled_bridges, num_bridges = ndimage.label(valid_bridges_mask)

        for bridge_id in range(1, num_bridges + 1):
            bridge_mask = (labeled_bridges == bridge_id)
            coords = np.argwhere(bridge_mask)

            if len(coords) > 0:
                y_min, x_min = coords.min(axis=0)
                y_max, x_max = coords.max(axis=0)

                cv2.rectangle(img_rgb, (int(x_min), int(y_min)), (int(x_max), int(y_max)),
                            BRIDGE_VALID, BRIDGE_THICKNESS)

    # === ШАГ 4: ID узлов (опционально) ===
    if show_node_labels:
        for node in nodes:
            cy, cx = node['centroid']
            label = node['id'].split('_')[1]
            cv2.putText(img_rgb, label, (int(cx) + 5, int(cy) - 5),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.3, (0, 0, 0), 1)

    # === ШАГ 5: Легенда ===
    legend = create_legend()
    legend_height, legend_width = legend.shape[:2]
    margin = 20
    y_start = margin
    x_start = width - legend_width - margin

    img_rgb[y_start:y_start + legend_height, x_start:x_start + legend_width] = legend

    # === ШАГ 6: Сохранить ===
    img_rgb_final = cv2.cvtColor(img_rgb, cv2.COLOR_BGR2RGB)

    plt.figure(figsize=(width / 100, height / 100), dpi=dpi)
    plt.imshow(img_rgb_final)
    plt.axis('off')
    plt.tight_layout(pad=0)
    plt.savefig(output_path, dpi=dpi, bbox_inches='tight', pad_inches=0)
    plt.close()

    print(f"Визуализация сохранена: {output_path}")


def draw_path(img, path, color, thickness):
    """Нарисовать сплошную линию."""
    for i in range(len(path) - 1):
        y1, x1 = path[i]
        y2, x2 = path[i + 1]
        cv2.line(img, (int(x1), int(y1)), (int(x2), int(y2)), color, int(thickness))


def draw_path_dashed(img, path, color, thickness, dash_length=5):
    """Нарисовать пунктирную линию."""
    for i in range(len(path) - 1):
        y1, x1 = path[i]
        y2, x2 = path[i + 1]

        dist = np.sqrt((x2 - x1)**2 + (y2 - y1)**2)
        if dist == 0:
            continue

        num_dashes = int(dist / dash_length)

        for j in range(num_dashes):
            if j % 2 == 0:
                t1 = j / num_dashes
                t2 = (j + 1) / num_dashes

                sx1 = int(x1 + (x2 - x1) * t1)
                sy1 = int(y1 + (y2 - y1) * t1)
                sx2 = int(x1 + (x2 - x1) * t2)
                sy2 = int(y1 + (y2 - y1) * t2)

                cv2.line(img, (sx1, sy1), (sx2, sy2), color, int(thickness))


def create_legend():
    """Создать легенду."""
    legend_width = 250
    legend_height = 220
    legend = np.ones((legend_height, legend_width, 3), dtype=np.uint8) * 255
    cv2.rectangle(legend, (0, 0), (legend_width - 1, legend_height - 1), (0, 0, 0), 2)

    y_pos = 25
    cv2.putText(legend, "Legend", (10, y_pos), cv2.FONT_HERSHEY_SIMPLEX,
               0.6, (0, 0, 0), 2)

    y_pos += 30

    items = [
        (EDGE_NORMAL, "Normal edge", False),
        (EDGE_PIPE1, "Pipe 1 (bridge)", False),
        (EDGE_PIPE2, "Pipe 2 (bridge)", False),
        (EDGE_TERMINAL, "Terminal edge", True),
    ]

    for color, label, is_dashed in items:
        x_start = 15
        x_end = 40

        if is_dashed:
            cv2.line(legend, (x_start, y_pos - 3), (x_start + 8, y_pos - 3), color, 1)
            cv2.line(legend, (x_start + 12, y_pos - 3), (x_start + 20, y_pos - 3), color, 1)
            cv2.line(legend, (x_start + 24, y_pos - 3), (x_end, y_pos - 3), color, 1)
        else:
            cv2.line(legend, (x_start, y_pos - 3), (x_end, y_pos - 3), color, 2)

        cv2.putText(legend, label, (x_end + 10, y_pos), cv2.FONT_HERSHEY_SIMPLEX,
                   0.4, (0, 0, 0), 1)

        y_pos += 25

    y_pos += 10

    node_items = [
        (NODE_CONNECTED, "Connected node"),
        (NODE_INVALID_BRIDGE, "Invalid bridge"),
        (NODE_ISOLATED, "Isolated node"),
    ]

    for color, label in node_items:
        cv2.rectangle(legend, (15, y_pos - 10), (35, y_pos + 5), color, -1)
        cv2.putText(legend, label, (45, y_pos), cv2.FONT_HERSHEY_SIMPLEX,
                   0.4, (0, 0, 0), 1)
        y_pos += 25

    cv2.rectangle(legend, (15, y_pos - 10), (35, y_pos + 5), BRIDGE_VALID, 2)
    cv2.putText(legend, "Valid bridge", (45, y_pos), cv2.FONT_HERSHEY_SIMPLEX,
               0.4, (0, 0, 0), 1)

    return legend


def plot_statistics(nodes, edges, output_path):
    """Построить графики статистики."""
    fig, axes = plt.subplots(2, 2, figsize=(12, 10))

    ax1 = axes[0, 0]
    degrees = [n['degree'] for n in nodes]
    ax1.hist(degrees, bins=range(max(degrees) + 2), edgecolor='black')
    ax1.set_xlabel('Degree')
    ax1.set_ylabel('Count')
    ax1.set_title('Node Degree Distribution')
    ax1.grid(True, alpha=0.3)

    ax2 = axes[0, 1]
    lengths = [e['length'] for e in edges]
    ax2.hist(lengths, bins=50, edgecolor='black')
    ax2.set_xlabel('Edge Length (pixels)')
    ax2.set_ylabel('Count')
    ax2.set_title('Edge Length Distribution')
    ax2.grid(True, alpha=0.3)

    ax3 = axes[1, 0]
    edge_types = {
        'Normal': len([e for e in edges if e.get('color') is None and not e['is_terminal']]),
        'Pipe 1': len([e for e in edges if e.get('color') == 'BLUE']),
        'Pipe 2': len([e for e in edges if e.get('color') == 'YELLOW']),
        'Terminal': len([e for e in edges if e['is_terminal']])
    }
    colors_plot = ['green', 'blue', 'yellow', 'gray']
    ax3.bar(edge_types.keys(), edge_types.values(), color=colors_plot, edgecolor='black')
    ax3.set_ylabel('Count')
    ax3.set_title('Edge Types')
    ax3.grid(True, alpha=0.3, axis='y')

    ax4 = axes[1, 1]
    node_types = {
        'Connected': len([n for n in nodes if n['degree'] > 0 and n['type'] != 'invalid_bridge']),
        'Invalid Bridge': len([n for n in nodes if n['type'] == 'invalid_bridge']),
        'Isolated': len([n for n in nodes if n['degree'] == 0])
    }
    colors_plot = ['green', 'orange', 'red']
    ax4.bar(node_types.keys(), node_types.values(), color=colors_plot, edgecolor='black')
    ax4.set_ylabel('Count')
    ax4.set_title('Node Types')
    ax4.grid(True, alpha=0.3, axis='y')

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()

    print(f"Статистика сохранена: {output_path}")


def plot_isolated_nodes_debug(skeleton, labeled_nodes, nodes, edges, output_path, dpi=150):
    """Отладочная визуализация изолированных узлов. (deprecated)"""
    pass


def plot_contact_points_debug(skeleton, labeled_nodes, nodes, output_path, dpi=150):
    """Отладочная визуализация точек контакта. (deprecated)"""
    pass

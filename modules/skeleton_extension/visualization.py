"""
Skeleton Extension - Visualization

Функции визуализации из hybrid_pipe_reconstruction_optimized.py
"""

import cv2
import numpy as np


def visualize_bfs_paths(original, skeleton, nodes, protection_mask, all_bfs_paths,
                        remaining_endpoints, newly_connected_indices):
    """
    Визуализация BFS путей
    """
    # Затемненный фон
    viz = cv2.cvtColor(original, cv2.COLOR_GRAY2BGR)
    viz = (viz * 0.3).astype(np.uint8)

    # Защитная маска - полупрозрачный оранжевый
    mask_overlay = np.zeros_like(viz)
    protection_binary = (protection_mask > 127).astype(np.uint8)
    mask_overlay[protection_binary > 0] = [0, 100, 200]
    viz = cv2.addWeighted(viz, 1.0, mask_overlay, 0.3, 0)

    # Скелет - белый
    skeleton_binary = (skeleton > 127).astype(np.uint8)
    viz[skeleton_binary > 0] = [200, 200, 200]

    # Контуры узлов - голубой
    nodes_binary = (nodes > 127).astype(np.uint8)
    contours, _ = cv2.findContours(nodes_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(viz, contours, -1, (255, 255, 0), 1)

    # Цвета BFS путей
    type_colors = {
        'node_contour': (0, 255, 0),  # Зелёный
        'endpoint': (0, 200, 100),  # Тёмно-зелёный
        'other_skeleton': (100, 255, 100),  # Светло-зелёный
        'no_path': (0, 0, 150)  # Тёмно-красный (для точек без пути)
    }

    # Рисуем BFS пути
    for path_data in all_bfs_paths:
        points = path_data['points']
        path_type = path_data['type']
        color = type_colors.get(path_type, (255, 255, 255))

        if len(points) >= 2:
            for i in range(len(points) - 1):
                p1 = (int(points[i][1]), int(points[i][0]))
                p2 = (int(points[i + 1][1]), int(points[i + 1][0]))
                cv2.line(viz, p1, p2, color, 2)

    # Endpoints
    for i, ep in enumerate(remaining_endpoints):
        y, x = int(ep[0]), int(ep[1])
        # Проверяем по ep_idx из all_bfs_paths
        ep_connected = False
        for path_data in all_bfs_paths:
            if path_data.get('ep_idx') in newly_connected_indices:
                # Это соединённый
                pass

        # Простая проверка: если i в newly_connected_indices
        if i < len(all_bfs_paths) and all_bfs_paths[i]['type'] != 'no_path':
            cv2.rectangle(viz, (x - 2, y - 2), (x + 2, y + 2), (0, 255, 0), -1)
        else:
            cv2.rectangle(viz, (x - 2, y - 2), (x + 2, y + 2), (0, 0, 255), -1)

    # Легенда
    cv2.putText(viz, "BFS Paths", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

    legend_y = 55
    for path_type, color in type_colors.items():
        if path_type != 'no_path':
            cv2.rectangle(viz, (10, legend_y - 10), (25, legend_y + 2), color, -1)
            cv2.putText(viz, path_type, (30, legend_y), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
            legend_y += 18

    return viz


def visualize_all_lines(original, skeleton, nodes, protection_mask, endpoints, all_lines, connected_eps):
    """
    Визуализация ВСЕХ линий с цветовой кодировкой причин остановки

    Цвета линий (2-3px):
    - Зелёный: достигли контура узла (успех)
    - Красный: остановлены защитной маской
    - Синий: остановлены скелетом
    - Жёлтый: достигли max_length
    - Серый: вышли за границы
    """
    # Затемненный фон (30%)
    viz = cv2.cvtColor(original, cv2.COLOR_GRAY2BGR)
    viz = (viz * 0.3).astype(np.uint8)

    # Защитная маска - полупрозрачный жёлтый
    mask_overlay = np.zeros_like(viz)
    mask_overlay[protection_mask > 0] = [0, 180, 255]  # Оранжевый
    viz = cv2.addWeighted(viz, 1.0, mask_overlay, 0.4, 0)

    # Скелет - тонкий белый
    skeleton_binary = (skeleton > 127).astype(np.uint8)
    viz[skeleton_binary > 0] = [200, 200, 200]

    # Контуры узлов - голубой
    nodes_binary = (nodes > 127).astype(np.uint8)
    contours, _ = cv2.findContours(nodes_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(viz, contours, -1, (255, 255, 0), 1)  # Cyan

    # Цвета для причин остановки
    reason_colors = {
        'node_contour': (0, 255, 0),  # Зелёный - успех узел!
        'endpoint': (0, 200, 100),  # Тёмно-зелёный - успех endpoint!
        'other_skeleton': (100, 255, 100),  # Светло-зелёный - успех скелет другой компоненты!
        'mask': (0, 0, 255),  # Красный
        'skeleton': (255, 100, 100),  # Голубой - свой скелет
        'max_length': (0, 255, 255),  # Жёлтый
        'boundary': (128, 128, 128),  # Серый
        'background': (255, 0, 255)  # Магента - вышли на белый фон
    }

    # Рисуем ВСЕ линии
    for line_data in all_lines:
        points = line_data['points']
        reason = line_data['reason']
        color = reason_colors.get(reason, (255, 255, 255))

        if len(points) < 2:
            continue

        # Рисуем линию толщиной 2px
        for i in range(len(points) - 1):
            p1 = (int(points[i][1]), int(points[i][0]))  # (x, y)
            p2 = (int(points[i + 1][1]), int(points[i + 1][0]))
            cv2.line(viz, p1, p2, color, 2)

    # Endpoints - маркеры
    for idx, ep in enumerate(endpoints):
        y, x = int(ep[0]), int(ep[1])
        if idx in connected_eps:
            # Соединённый - зелёный квадрат
            cv2.rectangle(viz, (x - 2, y - 2), (x + 2, y + 2), (0, 255, 0), -1)
        else:
            # Несоединённый - красный квадрат
            cv2.rectangle(viz, (x - 2, y - 2), (x + 2, y + 2), (0, 0, 255), -1)

    # Легенда
    legend_y = 30
    cv2.putText(viz, "Lines Debug Visualization", (10, legend_y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

    legend_y += 25
    for reason, color in reason_colors.items():
        cv2.rectangle(viz, (10, legend_y - 10), (25, legend_y + 2), color, -1)
        cv2.putText(viz, reason, (30, legend_y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        legend_y += 20

    # Статистика
    legend_y += 10
    reason_counts = {}
    for line_data in all_lines:
        r = line_data['reason']
        reason_counts[r] = reason_counts.get(r, 0) + 1

    for reason, count in reason_counts.items():
        cv2.putText(viz, f"{reason}: {count}", (10, legend_y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)
        legend_y += 15

    return viz


def visualize_protection_mask(original, skeleton_work, protection_mask, endpoints):
    """Визуализация защитной маски"""

    viz = cv2.cvtColor(original, cv2.COLOR_GRAY2BGR)
    bg = np.full_like(viz, 128)
    viz = cv2.addWeighted(bg, 0.8, viz, 0.2, 0)

    # Защитная маска - желтый (50% прозрачность)
    mask_color = np.zeros_like(viz)
    mask_color[protection_mask > 0] = [0, 255, 255]
    viz = cv2.addWeighted(viz, 1.0, mask_color, 0.5, 0)

    # Рабочий скелет - красный
    skeleton_binary = (skeleton_work > 127).astype(np.uint8)
    viz[skeleton_binary > 0] = [0, 0, 255]

    # Endpoints - зеленые квадраты 2x2
    for ep in endpoints:
        y, x = int(ep[0]), int(ep[1])
        cv2.rectangle(viz, (x - 1, y - 1), (x + 1, y + 1), (0, 255, 0), -1)

    # Endpoints в маске - красный круг
    for ep in endpoints:
        if protection_mask[ep[0], ep[1]] > 0:
            cv2.circle(viz, (int(ep[1]), int(ep[0])), 5, (0, 0, 255), 2)

    cv2.putText(viz, "Protection Mask Check", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)

    return viz


def visualize_skeleton_contacts(original, skeleton, nodes_mask, endpoints, connected_eps):
    """
    Визуализация контактов скелета с узлами
    
    - Фон: затемнённый оригинал
    - Скелет: ярко-зелёный
    - Синие кружки: контакт скелета с контуром узла
    - Красные кружки: endpoints не соединённые с узлами
    """
    # Затемнённый оригинал
    if len(original.shape) == 2:
        viz = cv2.cvtColor(original, cv2.COLOR_GRAY2BGR)
    else:
        viz = original.copy()
    viz = (viz * 0.3).astype(np.uint8)
    
    skeleton_binary = (skeleton > 127).astype(np.uint8)
    nodes_binary = (nodes_mask > 127).astype(np.uint8)
    
    # Скелет - ярко-зелёный
    viz[skeleton_binary > 0] = [0, 255, 0]
    
    # Контур узлов
    contours, _ = cv2.findContours(nodes_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    node_contour = np.zeros_like(nodes_binary)
    cv2.drawContours(node_contour, contours, -1, 1, thickness=1)
    
    # Найти точки контакта скелета с контуром узлов
    contact_points = np.column_stack(np.where((skeleton_binary > 0) & (node_contour > 0)))
    
    # Также ищем контакты в радиусе 2px (скелет может не точно касаться)
    dilated_skeleton = cv2.dilate(skeleton_binary, np.ones((3, 3), np.uint8), iterations=1)
    contact_points_near = np.column_stack(np.where((dilated_skeleton > 0) & (node_contour > 0)))
    
    # Синие кружки - контакты с узлами
    for pt in contact_points_near:
        y, x = int(pt[0]), int(pt[1])
        cv2.circle(viz, (x, y), 5, (255, 100, 0), -1)  # Синий заполненный
    
    # Красные кружки - несоединённые endpoints
    for idx, ep in enumerate(endpoints):
        if idx not in connected_eps:
            y, x = int(ep[0]), int(ep[1])
            cv2.circle(viz, (x, y), 6, (0, 0, 255), -1)  # Красный заполненный
            cv2.circle(viz, (x, y), 6, (255, 255, 255), 1)  # Белая обводка
    
    # Легенда
    cv2.putText(viz, "Skeleton Contacts", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    cv2.rectangle(viz, (10, 45), (25, 58), (0, 255, 0), -1)
    cv2.putText(viz, "Skeleton", (30, 57), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    cv2.circle(viz, (17, 75), 5, (255, 100, 0), -1)
    cv2.putText(viz, "Node contact", (30, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    cv2.circle(viz, (17, 98), 5, (0, 0, 255), -1)
    cv2.putText(viz, f"Unconnected endpoints ({len(endpoints) - len(connected_eps)})", (30, 103), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    
    return viz

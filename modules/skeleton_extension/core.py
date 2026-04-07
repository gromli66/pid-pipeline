"""
Skeleton Extension - Core Logic

Полная логика продления скелета из hybrid_pipe_reconstruction_optimized.py
"""

import cv2
import numpy as np
from scipy.spatial import cKDTree
from skimage.morphology import skeletonize
from collections import deque
import time


def find_skeleton_endpoints(skeleton):
    """Найти endpoints скелета"""
    kernel = np.array([[1, 1, 1],
                       [1, 10, 1],
                       [1, 1, 1]], dtype=np.uint8)

    neighbors = cv2.filter2D((skeleton > 0).astype(np.uint8), -1, kernel)
    endpoints_mask = (skeleton > 0) & (neighbors == 11)
    endpoints = np.column_stack(np.where(endpoints_mask))

    return endpoints.tolist()


def remove_skeleton_under_nodes(skeleton, nodes_mask, verbose=True):
    """ЭТАП 1: Удалить скелет ПОД узлами"""
    if verbose:
        print(f"\n🧹 ЭТАП 1 - Удалить скелет ПОД узлами...")

    nodes_binary = (nodes_mask > 127).astype(np.uint8)
    skeleton_binary = (skeleton > 127).astype(np.uint8)

    removed = skeleton_binary & nodes_binary
    cleaned = skeleton_binary & (~nodes_binary)

    removed_count = np.sum(removed)
    if verbose:
        print(f"   ✓ Удалено пикселей: {removed_count}")

    return cleaned * 255


def remove_skeleton_under_nodes_simple(skeleton, nodes_mask, verbose=True):
    """
    ЭТАП 1 (SIMPLE): Удалить скелет ВНУТРИ узлов, сохраняя скелет НА КОНТУРЕ.

    ИСПРАВЛЕННАЯ ЛОГИКА:
    1. Извлекаем контур узлов (толщина 1px)
    2. Внутренность узлов = узлы минус контур
    3. Удаляем скелет только из внутренности (не с контура!)

    Это правильнее чем erode, потому что:
    - erode может полностью уничтожить маленькие узлы (< 3x3)
    - контур гарантированно сохраняется независимо от размера узла
    """
    if verbose:
        print(f"\n🧹 ЭТАП 1 (SIMPLE) - Удалить скелет внутри узлов (контур сохраняется)...")

    nodes_binary = (nodes_mask > 127).astype(np.uint8)
    skeleton_binary = (skeleton > 127).astype(np.uint8)

    # Извлекаем контур узлов
    contours, _ = cv2.findContours(nodes_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    node_contour = np.zeros_like(nodes_binary)
    cv2.drawContours(node_contour, contours, -1, 1, thickness=1)

    # Внутренность узлов = узлы минус контур
    node_interior = nodes_binary.copy()
    node_interior[node_contour > 0] = 0

    # Удаляем скелет только из внутренности (НЕ с контура!)
    removed = skeleton_binary & node_interior
    cleaned = skeleton_binary & (~node_interior)

    removed_count = np.sum(removed)
    skeleton_on_contour = np.sum(skeleton_binary & node_contour)

    if verbose:
        print(f"   ✓ Удалено пикселей внутри узлов: {removed_count}")
        print(f"   ✓ Скелет на контуре узлов сохранён: {skeleton_on_contour} px")

    return cleaned * 255, node_contour * 255


def remove_skeleton_around_nodes(skeleton, nodes_mask, expansion=1, verbose=True):
    """ЭТАП 2: Удалить скелет в N px ВОКРУГ узлов"""
    if verbose:
        print(f"\n🧹 ЭТАП 2 - Удалить скелет в {expansion}px вокруг узлов...")

    nodes_binary = (nodes_mask > 127).astype(np.uint8)
    skeleton_binary = (skeleton > 127).astype(np.uint8)

    contours, _ = cv2.findContours(nodes_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contour_mask = np.zeros_like(nodes_binary)
    cv2.drawContours(contour_mask, contours, -1, 1, thickness=1)

    kernel = np.ones((3, 3), np.uint8)
    expanded_contour = cv2.dilate(contour_mask, kernel, iterations=expansion)

    removed = skeleton_binary & expanded_contour
    cleaned = skeleton_binary & (~expanded_contour)

    removed_count = np.sum(removed)
    if verbose:
        print(f"   ✓ Удалено пикселей: {removed_count}")

    return cleaned * 255


def trace_from_endpoint(skeleton_binary, endpoint, max_length):
    """Трассировка от endpoint"""
    h, w = skeleton_binary.shape
    visited = np.zeros_like(skeleton_binary, dtype=bool)

    path = [tuple(endpoint)]
    visited[endpoint[0], endpoint[1]] = True

    current = tuple(endpoint)

    for step in range(max_length):
        neighbors = []
        for dy in [-1, 0, 1]:
            for dx in [-1, 0, 1]:
                if dy == 0 and dx == 0:
                    continue

                ny, nx = current[0] + dy, current[1] + dx

                if not (0 <= ny < h and 0 <= nx < w):
                    continue

                if skeleton_binary[ny, nx] > 0 and not visited[ny, nx]:
                    neighbors.append((ny, nx))

        if len(neighbors) == 0:
            return path, True

        if len(neighbors) > 1:
            return path, True

        current = neighbors[0]
        path.append(current)
        visited[current[0], current[1]] = True

    return path, False


def create_endpoint_protection_mask(skeleton, original_image, trim_work=10, trim_protection=15, mask_width=8, verbose=True):
    """
    ЭТАП 3: Создать защитную маску для endpoints

    ИСПРАВЛЕНИЕ: Защита маленьких компонент от уничтожения.
    Компоненты размером < 2*trim_work+1 не обрезаются, т.к. обрезка
    с обоих endpoints полностью уничтожит их.
    """
    if verbose:
        print(f"\n✂️ ЭТАП 3 - Создание защитной маски...")

    skeleton_binary = (skeleton > 127).astype(np.uint8)
    endpoints = find_skeleton_endpoints(skeleton)

    if verbose:
        print(f"   Endpoints найдено: {len(endpoints)}")

    # === НОВОЕ: Анализ связных компонент для защиты маленьких ===
    num_components, labels, stats, _ = cv2.connectedComponentsWithStats(skeleton_binary)

    # Минимальный размер компоненты для безопасной обрезки
    # Если компонента < 2*trim_work+1, обрезка с обоих концов её уничтожит
    min_component_size = trim_work * 2 + 1

    # Маппинг endpoint → component_id
    endpoint_to_component = {}
    for ep_idx, ep in enumerate(endpoints):
        endpoint_to_component[ep_idx] = labels[ep[0], ep[1]]

    # Размеры компонент
    component_sizes = {i: stats[i, cv2.CC_STAT_AREA] for i in range(1, num_components)}

    # Подсчёт защищённых компонент
    protected_components = set()
    for comp_id, size in component_sizes.items():
        if size < min_component_size:
            protected_components.add(comp_id)

    if verbose and protected_components:
        print(f"   ⚠️ Защищено маленьких компонент (size < {min_component_size}): {len(protected_components)}")

    skeleton_15 = skeleton_binary.copy()
    skeleton_10 = skeleton_binary.copy()

    trimmed_count = 0
    short_branches_kept = 0
    protected_endpoints_count = 0

    for ep_idx, ep in enumerate(endpoints):
        # === НОВОЕ: Проверка защиты компоненты ===
        comp_id = endpoint_to_component.get(ep_idx, 0)
        if comp_id in protected_components:
            # Маленькая компонента - НЕ обрезаем, но удаляем из skeleton_15 для маски
            path, _ = trace_from_endpoint(skeleton_binary, ep, trim_protection)
            for y, x in path:
                skeleton_15[y, x] = 0
            protected_endpoints_count += 1
            continue

        path, stopped = trace_from_endpoint(skeleton_binary, ep, trim_protection)

        path_len = len(path)

        if path_len < trim_work:
            # Короткая ветка (но компонента большая) - НЕ удаляем из skeleton_10
            # Но удаляем из skeleton_15 для создания защитной маски
            for y, x in path:
                skeleton_15[y, x] = 0
            short_branches_kept += 1
        else:
            for i in range(trim_work):
                y, x = path[i]
                skeleton_10[y, x] = 0

            if path_len >= trim_protection:
                for i in range(trim_protection):
                    y, x = path[i]
                    skeleton_15[y, x] = 0
                trimmed_count += 1
            else:
                for y, x in path:
                    skeleton_15[y, x] = 0

    if verbose and protected_endpoints_count > 0:
        print(f"   ✓ Endpoints защищённых компонент: {protected_endpoints_count}")

    kernel = np.ones((mask_width, mask_width), np.uint8)
    protection_mask = cv2.dilate(skeleton_15, kernel, iterations=1)

    protection_mask[skeleton_10 > 0] = 0

    if verbose:
        print(f"   ✓ Защищено пикселей (до обрезки): {np.sum(protection_mask)}")

    original_binary = (original_image < 128).astype(np.uint8)
    protection_mask[original_binary == 0] = 0

    if verbose:
        print(f"   ✓ Защищено пикселей (после обрезки по трубе): {np.sum(protection_mask)}")

    # Очистить 4px вокруг endpoints
    endpoints_work = find_skeleton_endpoints(skeleton_10 * 255)

    clearance_radius = 4

    for ep in endpoints_work:
        y, x = ep[0], ep[1]
        y_min = max(0, y - clearance_radius)
        y_max = min(protection_mask.shape[0], y + clearance_radius + 1)
        x_min = max(0, x - clearance_radius)
        x_max = min(protection_mask.shape[1], x + clearance_radius + 1)

        protection_mask[y_min:y_max, x_min:x_max] = 0

    if verbose:
        print(f"   ✓ Защищено пикселей (после очистки 4px у endpoints): {np.sum(protection_mask)}")
        print(f"   ✓ Обрезано endpoints: {trimmed_count}")
        print(f"   ✓ Коротких веток сохранено: {short_branches_kept}")

    return skeleton_10 * 255, protection_mask


def create_simple_protection_mask(skeleton, node_contour, original_image, mask_width=8, extend_radius=5, verbose=True):
    """
    ЭТАП 3 (SIMPLE): Продление endpoints до контура + защитная маска.

    ИСПРАВЛЕННАЯ ЛОГИКА:
    1. Найти endpoints скелета
    2. Для каждого endpoint НЕ на контуре - продлить до ближайшей точки контура
    3. Создать защитную маску (dilate) БЕЗ обрезки endpoints

    Args:
        skeleton: скелет после удаления внутренности узлов
        node_contour: контур узлов (из remove_skeleton_under_nodes_simple)
        original_image: оригинальное изображение для обрезки маски
        mask_width: ширина защитной маски
        extend_radius: радиус поиска контура для продления

    Returns:
        skeleton_extended: скелет с продлёнными endpoints
        protection_mask: защитная маска
    """
    if verbose:
        print(f"\n🛡️ ЭТАП 3 (SIMPLE) - Продление endpoints + защитная маска...")

    skeleton_binary = (skeleton > 127).astype(np.uint8)
    node_contour_binary = (node_contour > 127).astype(np.uint8)

    endpoints = find_skeleton_endpoints(skeleton)
    if verbose:
        print(f"   Endpoints: {len(endpoints)}")

    # === ПРОДЛЕНИЕ ENDPOINTS ДО КОНТУРА ===
    # Классификация endpoints
    on_contour = []
    near_contour = []

    # KDTree для быстрого поиска ближайшей точки контура
    contour_points = np.column_stack(np.where(node_contour_binary > 0))
    if len(contour_points) > 0:
        contour_tree = cKDTree(contour_points)
    else:
        contour_tree = None

    for i, ep in enumerate(endpoints):
        y, x = ep[0], ep[1]
        if node_contour_binary[y, x] > 0:
            on_contour.append(i)
        elif contour_tree is not None:
            dist, _ = contour_tree.query([y, x])
            if dist <= extend_radius:
                near_contour.append((i, dist))

    if verbose:
        print(f"   On contour: {len(on_contour)}")
        print(f"   Near contour (≤{extend_radius}px): {len(near_contour)}")

    # Продление
    skeleton_extended = skeleton_binary.copy()
    extended_count = 0

    for ep_idx, dist in near_contour:
        ep = endpoints[ep_idx]
        y, x = ep[0], ep[1]

        # Найти ближайшую точку контура
        _, idx = contour_tree.query([y, x])
        target_y, target_x = contour_points[idx]

        # Провести линию
        line_points = get_line_points(y, x, target_y, target_x)
        for py, px in line_points:
            skeleton_extended[py, px] = 1
        extended_count += 1

    if verbose:
        print(f"   Extended: {extended_count} endpoints")

    # === ЗАЩИТНАЯ МАСКА ===
    # Dilate скелета
    kernel = np.ones((mask_width, mask_width), np.uint8)
    protection_mask = cv2.dilate(skeleton_extended, kernel, iterations=1)

    # Вычитаем сам скелет (маска вокруг, не на)
    protection_mask[skeleton_extended > 0] = 0

    if verbose:
        print(f"   ✓ Защищено пикселей (до обрезки): {np.sum(protection_mask)}")

    # Обрезка по трубе (original)
    original_binary = (original_image < 128).astype(np.uint8)
    protection_mask[original_binary == 0] = 0

    if verbose:
        print(f"   ✓ Защищено пикселей (после обрезки по трубе): {np.sum(protection_mask)}")

    # НЕ очищаем вокруг endpoints - они уже на контуре!
    # (убрана очистка clearance_radius которая ломала короткие ветки)

    # Очищаем только вокруг endpoints которые НЕ на контуре
    final_endpoints = find_skeleton_endpoints(skeleton_extended * 255)
    clearance_radius = 4
    cleared_count = 0

    for ep in final_endpoints:
        y, x = ep[0], ep[1]
        # Очищаем только если endpoint НЕ на контуре узла
        if node_contour_binary[y, x] == 0:
            y_min = max(0, y - clearance_radius)
            y_max = min(protection_mask.shape[0], y + clearance_radius + 1)
            x_min = max(0, x - clearance_radius)
            x_max = min(protection_mask.shape[1], x + clearance_radius + 1)
            protection_mask[y_min:y_max, x_min:x_max] = 0
            cleared_count += 1

    if verbose:
        print(f"   ✓ Очищено {clearance_radius}px у {cleared_count} endpoints (не на контуре)")

    return skeleton_extended * 255, protection_mask


def get_endpoint_direction(skeleton_binary, endpoint, direction_trace_length=5):
    """
    Определить направление endpoint
    """
    path, _ = trace_from_endpoint(skeleton_binary, endpoint, direction_trace_length)

    if len(path) < 2:
        return None

    inner_point = path[-1]

    direction = np.array([endpoint[0] - inner_point[0],
                          endpoint[1] - inner_point[1]], dtype=float)

    length = np.linalg.norm(direction)
    if length == 0:
        return None

    direction = direction / length

    return direction


def round_to_4_directions(direction):
    """
    Округлить направление к 4 кардинальным: ↑↓←→
    """
    if direction is None:
        return []

    dy, dx = direction

    cardinal = {
        'up': (-1, 0),
        'down': (1, 0),
        'left': (0, -1),
        'right': (0, 1)
    }

    angles = {}
    for name, (cy, cx) in cardinal.items():
        dot = dy * cy + dx * cx
        angle = np.arccos(np.clip(dot, -1, 1))
        angles[name] = np.degrees(angle)

    closest = min(angles.items(), key=lambda x: x[1])
    closest_name, closest_angle = closest

    if closest_angle < 30:
        return [cardinal[closest_name]]

    if closest_angle <= 60:
        sorted_angles = sorted(angles.items(), key=lambda x: x[1])
        second_name = sorted_angles[1][0]
        return [cardinal[closest_name], cardinal[second_name]]

    return [cardinal[closest_name]]


def get_line_points(y1, x1, y2, x2):
    """
    Получить все пиксели на прямой линии (алгоритм Брезенхема)
    Возвращает список точек [(y, x), ...]
    """
    points = []

    dy = abs(y2 - y1)
    dx = abs(x2 - x1)

    sy = 1 if y1 < y2 else -1
    sx = 1 if x1 < x2 else -1

    err = dx - dy

    y, x = y1, x1

    while True:
        points.append((y, x))

        if y == y2 and x == x2:
            break

        e2 = 2 * err

        if e2 > -dy:
            err -= dy
            x += sx

        if e2 < dx:
            err += dx
            y += sy

    return points


def check_line_to_endpoint(start_y, start_x, end_y, end_x, pipe_mask, white_tolerance=2):
    """
    Проверить можно ли провести прямую от start до end по трубе

    Args:
        start_y, start_x: начальная точка
        end_y, end_x: целевой endpoint
        pipe_mask: маска трубы (1 = труба, 0 = фон)
        white_tolerance: допустимое количество белых пикселей

    Returns:
        (valid, line_points): valid=True если линия допустима, line_points = точки линии
    """
    line_points = get_line_points(start_y, start_x, end_y, end_x)

    white_count = 0
    for y, x in line_points:
        if pipe_mask[y, x] == 0:
            white_count += 1
            if white_count > white_tolerance:
                return False, []

    return True, line_points


def connect_with_directed_lines(endpoints, skeleton, nodes_mask, original_gray, protection_mask, config, verbose=True):
    """
    ЭТАП 4: Направленные линии к узлам И другим endpoints/скелетам (ИСПРАВЛЕННАЯ ВЕРСИЯ)

    ИСПРАВЛЕНИЯ:
    1. Проверка контура КАЖДЫЙ шаг (не раз в 5)
    2. Прямая проверка пикселя node_contour[cy, cx]
    3. Убрана проверка nodes_interior (узлы = только контуры)
    4. Сохраняем ВСЕ линии для визуализации
    5. Линия идёт ТОЛЬКО по трубе (тёмные пиксели оригинала)
    6. Поддержка end-end соединений (разные связные компоненты)
    7. Поиск endpoint в радиусе 5px с проверкой прямой (допуск 2 белых пикселя)
    8. Поиск скелета другой компоненты в радиусе 5px с проверкой прямой

    Приоритет: node_contour → endpoint → other_skeleton
    """
    if verbose:
        print(f"\n🌉 ЭТАП 4 - Направленные линии к узлам и endpoints:")
        print(f"   Endpoints: {len(endpoints)}")

    if len(endpoints) < 1:
        return (np.zeros(skeleton.shape, dtype=np.uint8), [], set(), [])

    skeleton_binary = (skeleton > 127).astype(np.uint8)
    nodes_binary = (nodes_mask > 127).astype(np.uint8)

    # Маска трубы - тёмные пиксели оригинала
    pipe_mask = (original_gray < 128).astype(np.uint8)

    h, w = skeleton.shape

    # ========================================
    # СВЯЗНЫЕ КОМПОНЕНТЫ СКЕЛЕТА
    # ========================================
    num_components, labels = cv2.connectedComponents(skeleton_binary)
    if verbose:
        print(f"   Связных компонент скелета: {num_components - 1}")  # -1 т.к. 0 = фон

    # Маппинг endpoint → component_id
    endpoint_to_component = {}
    for ep_idx, ep in enumerate(endpoints):
        y, x = ep[0], ep[1]
        component_id = labels[y, x]
        endpoint_to_component[ep_idx] = component_id

    # Маска endpoints для быстрой проверки (endpoint_idx + 1, чтобы 0 = нет endpoint)
    endpoint_mask = np.zeros((h, w), dtype=np.int32)
    for ep_idx, ep in enumerate(endpoints):
        y, x = ep[0], ep[1]
        endpoint_mask[y, x] = ep_idx + 1  # +1 чтобы отличать от "нет endpoint"

    # KDTree для поиска endpoints в радиусе
    endpoint_coords = np.array([(ep[0], ep[1]) for ep in endpoints])
    endpoint_kdtree = cKDTree(endpoint_coords)

    # KDTree для поиска скелета в радиусе (с информацией о компоненте)
    skeleton_points = np.column_stack(np.where(skeleton_binary > 0))
    skeleton_kdtree = cKDTree(skeleton_points) if len(skeleton_points) > 0 else None

    # Найти контур узлов (это и есть целевая маска)
    contours, _ = cv2.findContours(nodes_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    node_contour = np.zeros_like(nodes_binary)
    cv2.drawContours(node_contour, contours, -1, 1, thickness=1)

    # Контур background-узлов — к ним НЕ продлевать
    bg_mask = config.get('background_node_mask')
    background_contour = np.zeros_like(nodes_binary)
    if bg_mask is not None:
        bg_binary = (bg_mask > 127).astype(np.uint8)
        bg_contours, _ = cv2.findContours(bg_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(background_contour, bg_contours, -1, 1, thickness=1)

    if verbose:
        print(f"   Пикселей контура узлов: {np.sum(node_contour)}")
        if bg_mask is not None:
            print(f"   Пикселей контура background: {np.sum(background_contour)} (excluded from targets)")

    connections_mask = np.zeros(skeleton.shape, dtype=np.uint8)
    connections_info = []
    connected_eps = set()

    # ========================================
    # СОХРАНЯЕМ ВСЕ ЛИНИИ ДЛЯ ВИЗУАЛИЗАЦИИ
    # ========================================
    all_lines = []  # [(points, reason, ep_idx, direction)]

    debug_stats = {
        'total_lines': 0,
        'no_direction': 0,
        'stopped_by_mask': 0,
        'stopped_by_node_contour': 0,  # Успех - узел!
        'stopped_by_endpoint': 0,  # Успех - другой endpoint!
        'stopped_by_other_skeleton': 0,  # Успех - скелет другой компоненты!
        'stopped_by_skeleton': 0,  # Своя компонента
        'stopped_by_boundary': 0,
        'stopped_by_max_length': 0,
        'stopped_by_background': 0,  # Вышли на белый фон
        'stopped_by_background_node': 0,  # Достигли контура background-узла
    }

    t_start = time.time()

    # Извлекаем параметры из конфига
    DIRECTION_TRACE_LENGTH = config.get('direction_trace_length', 5)
    MAX_LINE_LENGTH = config.get('max_line_length', 600)
    ENDPOINT_SEARCH_RADIUS = config.get('endpoint_search_radius', 5)
    ENDPOINT_LINE_WHITE_TOLERANCE = config.get('endpoint_line_white_tolerance', 2)
    SKELETON_SEARCH_RADIUS = config.get('skeleton_search_radius', 5)
    SKELETON_LINE_WHITE_TOLERANCE = config.get('skeleton_line_white_tolerance', 2)

    for ep_idx, ep_start in enumerate(endpoints):
        if ep_idx in connected_eps:
            continue

        if ep_idx % 50 == 0:
            print(f"      {ep_idx}/{len(endpoints)}...", end='\r')

        direction_vector = get_endpoint_direction(skeleton_binary, ep_start, DIRECTION_TRACE_LENGTH)

        if direction_vector is None:
            debug_stats['no_direction'] += 1
            continue

        directions = round_to_4_directions(direction_vector)

        if len(directions) == 0:
            debug_stats['no_direction'] += 1
            continue

        found = False

        for dy, dx in directions:
            debug_stats['total_lines'] += 1

            line_points = [tuple(ep_start)]  # Начинаем с EP
            current = np.array(ep_start, dtype=int)
            stop_reason = None

            for step in range(1, MAX_LINE_LENGTH + 1):
                current = current + np.array([dy, dx])

                # Границы
                if not (0 <= current[0] < h and 0 <= current[1] < w):
                    stop_reason = 'boundary'
                    break

                cy, cx = current[0], current[1]

                # ❌ Белый фон - линия должна идти только по трубе!
                if pipe_mask[cy, cx] == 0:
                    stop_reason = 'background'
                    break

                # ✅ ПРОВЕРКА ENDPOINT ДРУГОЙ КОМПОНЕНТЫ (точное попадание)
                ep_at_pixel = endpoint_mask[cy, cx]
                if ep_at_pixel > 0:
                    target_ep_idx = ep_at_pixel - 1  # -1 т.к. хранили +1
                    target_component = endpoint_to_component[target_ep_idx]
                    start_component = endpoint_to_component[ep_idx]

                    # Если это endpoint ДРУГОЙ компоненты
                    if target_component != start_component and target_ep_idx not in connected_eps:
                        line_points.append(tuple(current))
                        stop_reason = 'endpoint'  # УСПЕХ!

                        # Нарисовать путь
                        for p in line_points:
                            connections_mask[p[0], p[1]] = 255

                        connections_info.append({
                            'endpoint': ep_idx,
                            'target_endpoint': target_ep_idx,
                            'path_length': len(line_points),
                            'target': 'endpoint',
                            'direction': (dy, dx)
                        })

                        # Помечаем ОБА endpoint как соединённые
                        connected_eps.add(ep_idx)
                        connected_eps.add(target_ep_idx)
                        found = True
                        break

                # ✅ ПОИСК ENDPOINT В РАДИУСЕ 5px
                nearby_eps = endpoint_kdtree.query_ball_point([cy, cx], ENDPOINT_SEARCH_RADIUS)
                for nearby_ep_idx in nearby_eps:
                    if nearby_ep_idx == ep_idx:
                        continue
                    if nearby_ep_idx in connected_eps:
                        continue

                    target_component = endpoint_to_component[nearby_ep_idx]
                    start_component = endpoint_to_component[ep_idx]

                    if target_component == start_component:
                        continue

                    # Нашли endpoint в радиусе! Проверяем прямую
                    target_ep = endpoints[nearby_ep_idx]
                    target_y, target_x = target_ep[0], target_ep[1]

                    valid, correction_points = check_line_to_endpoint(
                        cy, cx, target_y, target_x,
                        pipe_mask,
                        ENDPOINT_LINE_WHITE_TOLERANCE
                    )

                    if valid:
                        # Добавляем текущую точку (если ещё не добавлена)
                        if (cy, cx) not in line_points:
                            line_points.append((cy, cx))

                        # Корректируем путь - добавляем прямую к endpoint (без первой точки - она уже добавлена)
                        for p in correction_points[1:]:
                            if p not in line_points:
                                line_points.append(p)

                        stop_reason = 'endpoint'  # УСПЕХ!

                        # Нарисовать путь
                        for p in line_points:
                            connections_mask[p[0], p[1]] = 255

                        connections_info.append({
                            'endpoint': ep_idx,
                            'target_endpoint': nearby_ep_idx,
                            'path_length': len(line_points),
                            'target': 'endpoint',
                            'direction': (dy, dx),
                            'corrected': True
                        })

                        # Помечаем ОБА endpoint как соединённые
                        connected_eps.add(ep_idx)
                        connected_eps.add(nearby_ep_idx)
                        found = True
                        break

                if found:
                    break

                # ✅ ПРОВЕРКА КОНТУРА УЗЛА - КАЖДЫЙ ШАГ!
                if node_contour[cy, cx] > 0:
                    # Проверить: это background-узел? Если да — не продлевать
                    if background_contour[cy, cx] > 0:
                        stop_reason = 'background_node'
                        break

                    line_points.append(tuple(current))
                    stop_reason = 'node_contour'  # УСПЕХ!

                    # Нарисовать путь
                    for p in line_points:
                        connections_mask[p[0], p[1]] = 255

                    connections_info.append({
                        'endpoint': ep_idx,
                        'path_length': len(line_points),
                        'target': 'node_contour',
                        'direction': (dy, dx)
                    })

                    connected_eps.add(ep_idx)
                    found = True
                    break

                # ✅ ПОИСК СКЕЛЕТА ДРУГОЙ КОМПОНЕНТЫ В РАДИУСЕ 5px
                if skeleton_kdtree is not None:
                    nearby_skeleton_idxs = skeleton_kdtree.query_ball_point([cy, cx], SKELETON_SEARCH_RADIUS)

                    for sk_idx in nearby_skeleton_idxs:
                        sk_y, sk_x = skeleton_points[sk_idx]
                        sk_component = labels[sk_y, sk_x]
                        start_component = endpoint_to_component[ep_idx]

                        if sk_component == start_component:
                            continue  # Своя компонента - пропускаем

                        # Нашли скелет другой компоненты! Проверяем прямую
                        valid, correction_points = check_line_to_endpoint(
                            cy, cx, sk_y, sk_x,
                            pipe_mask,
                            SKELETON_LINE_WHITE_TOLERANCE
                        )

                        if valid:
                            # Добавляем текущую точку
                            if (cy, cx) not in line_points:
                                line_points.append((cy, cx))

                            # Корректируем путь
                            for p in correction_points[1:]:
                                if p not in line_points:
                                    line_points.append(p)

                            stop_reason = 'other_skeleton'  # УСПЕХ!

                            # Нарисовать путь
                            for p in line_points:
                                connections_mask[p[0], p[1]] = 255

                            connections_info.append({
                                'endpoint': ep_idx,
                                'path_length': len(line_points),
                                'target': 'other_skeleton',
                                'target_component': sk_component,
                                'direction': (dy, dx),
                                'corrected': True
                            })

                            connected_eps.add(ep_idx)
                            found = True
                            break

                    if found:
                        break

                # ❌ Protection mask
                if protection_mask[cy, cx] > 0:
                    stop_reason = 'mask'
                    break

                # ❌ Скелет СВОЕЙ компоненты
                if skeleton_binary[cy, cx] > 0:
                    pixel_component = labels[cy, cx]
                    start_component = endpoint_to_component[ep_idx]
                    if pixel_component == start_component:
                        stop_reason = 'skeleton'
                        break

                line_points.append(tuple(current))

            # Если дошли до max_length
            if stop_reason is None:
                stop_reason = 'max_length'

            # Сохраняем линию для визуализации
            all_lines.append({
                'points': line_points.copy(),
                'reason': stop_reason,
                'ep_idx': ep_idx,
                'direction': (dy, dx)
            })

            # Подсчет причин остановки
            if stop_reason == 'mask':
                debug_stats['stopped_by_mask'] += 1
            elif stop_reason == 'node_contour':
                debug_stats['stopped_by_node_contour'] += 1
            elif stop_reason == 'endpoint':
                debug_stats['stopped_by_endpoint'] += 1
            elif stop_reason == 'other_skeleton':
                debug_stats['stopped_by_other_skeleton'] += 1
            elif stop_reason == 'skeleton':
                debug_stats['stopped_by_skeleton'] += 1
            elif stop_reason == 'boundary':
                debug_stats['stopped_by_boundary'] += 1
            elif stop_reason == 'max_length':
                debug_stats['stopped_by_max_length'] += 1
            elif stop_reason == 'background':
                debug_stats['stopped_by_background'] += 1
            elif stop_reason == 'background_node':
                debug_stats['stopped_by_background_node'] += 1

            if found:
                break

    if verbose:
        print()

    elapsed = time.time() - t_start

    if verbose:
        print(f"\n   ⏱️  Время: {elapsed:.1f}s")
        print(f"   ✓ Соединений построено: {len(connections_info)}")
        print(f"   Endpoints соединено: {len(connected_eps)}")
        print(f"   Endpoints осталось: {len(endpoints) - len(connected_eps)}")

    total = debug_stats['total_lines']
    if verbose and total > 0:
        print(f"\n🔍 DEBUG - Статистика направленных линий:")
        print(f"   Всего линий запущено: {total}")
        print(f"   Причины:")
        print(
            f"      ✅ Достигли контура узла: {debug_stats['stopped_by_node_contour']} ({debug_stats['stopped_by_node_contour'] / total * 100:.1f}%)")
        print(
            f"      ✅ Достигли другого endpoint: {debug_stats['stopped_by_endpoint']} ({debug_stats['stopped_by_endpoint'] / total * 100:.1f}%)")
        print(
            f"      ✅ Достигли скелета другой компоненты: {debug_stats['stopped_by_other_skeleton']} ({debug_stats['stopped_by_other_skeleton'] / total * 100:.1f}%)")
        print(f"      ❌ Нет направления: {debug_stats['no_direction']}")
        print(
            f"      ❌ Вышли на белый фон: {debug_stats['stopped_by_background']} ({debug_stats['stopped_by_background'] / total * 100:.1f}%)")
        print(
            f"      ❌ Достигли background-узла: {debug_stats['stopped_by_background_node']} ({debug_stats['stopped_by_background_node'] / total * 100:.1f}%)")
        print(
            f"      ❌ Остановлены маской: {debug_stats['stopped_by_mask']} ({debug_stats['stopped_by_mask'] / total * 100:.1f}%)")
        print(
            f"      ❌ Остановлены своим скелетом: {debug_stats['stopped_by_skeleton']} ({debug_stats['stopped_by_skeleton'] / total * 100:.1f}%)")
        print(
            f"      ❌ Вышли за границы: {debug_stats['stopped_by_boundary']} ({debug_stats['stopped_by_boundary'] / total * 100:.1f}%)")
        print(
            f"      ❌ Достигли max_length: {debug_stats['stopped_by_max_length']} ({debug_stats['stopped_by_max_length'] / total * 100:.1f}%)")

    return connections_mask, connections_info, connected_eps, all_lines


def create_final_protection_mask(skeleton_final, original_image, endpoints, connected_eps, mask_width=8,
                                 clearance_radius=4, verbose=True):
    """
    Создать финальную защитную маску над достроенным скелетом

    1. Dilate скелета на mask_width пикселей
    2. Вычесть белые пиксели оригинала
    3. Очистить clearance_radius вокруг оставшихся endpoints

    Args:
        skeleton_final: финальный скелет (рабочий + достроенный)
        original_image: оригинальное изображение
        endpoints: список всех endpoints
        connected_eps: set соединённых endpoints
        mask_width: ширина маски (dilate)
        clearance_radius: радиус очистки вокруг endpoints

    Returns:
        protection_mask: финальная защитная маска
    """
    if verbose:
        print(f"\n🛡️ Создание финальной защитной маски...")

    skeleton_binary = (skeleton_final > 127).astype(np.uint8)

    # Dilate на mask_width
    kernel = np.ones((mask_width, mask_width), np.uint8)
    protection_mask = cv2.dilate(skeleton_binary, kernel, iterations=1)

    if verbose:
        print(f"   ✓ После dilate {mask_width}px: {np.sum(protection_mask)} пикселей")

    # Вычесть белые пиксели оригинала
    original_binary = (original_image < 128).astype(np.uint8)
    protection_mask = protection_mask & original_binary

    if verbose:
        print(f"   ✓ После вычитания белого фона: {np.sum(protection_mask)} пикселей")

    # Очистить вокруг оставшихся endpoints
    remaining_eps = 0
    for ep_idx, ep in enumerate(endpoints):
        if ep_idx not in connected_eps:
            y, x = ep[0], ep[1]
            y_min = max(0, y - clearance_radius)
            y_max = min(protection_mask.shape[0], y + clearance_radius + 1)
            x_min = max(0, x - clearance_radius)
            x_max = min(protection_mask.shape[1], x + clearance_radius + 1)

            protection_mask[y_min:y_max, x_min:x_max] = 0
            remaining_eps += 1

    if verbose:
        print(f"   ✓ Очищено {clearance_radius}px вокруг {remaining_eps} оставшихся endpoints")
        print(f"   ✓ Финальная маска: {np.sum(protection_mask)} пикселей")

    return protection_mask * 255


def bfs_connect_endpoints(remaining_endpoints, remaining_ep_indices, skeleton_final, nodes_mask,
                          original_gray, protection_mask, endpoint_to_component, all_endpoints,
                          connected_eps, labels, config, verbose=True):
    """
    ЭТАП 5: BFS поиск соединений для оставшихся endpoints

    BFS по реальной трубе с приоритетом целей: node > endpoint > skeleton
    Допускается до BFS_MASK_TOLERANCE пикселей по защитной маске (суммарно за путь)
    Присоединение только к ЧУЖОЙ компоненте скелета

    Args:
        remaining_endpoints: список оставшихся endpoints [(y,x), ...]
        remaining_ep_indices: индексы оставшихся endpoints
        skeleton_final: финальный скелет
        nodes_mask: маска узлов
        original_gray: оригинальное изображение
        protection_mask: защитная маска
        endpoint_to_component: маппинг endpoint → component_id
        all_endpoints: все endpoints
        connected_eps: set уже соединённых endpoints
        labels: метки связных компонент
        config: конфигурация

    Returns:
        bfs_connections_mask: маска BFS соединений
        bfs_info: информация о соединениях
        newly_connected: set новых соединённых endpoints
        all_bfs_paths: все пути для визуализации
    """
    BFS_MAX_DEPTH = config.get('bfs_max_depth', 1000)
    BFS_MASK_TOLERANCE = config.get('bfs_mask_tolerance', 5)

    if verbose:
        print(f"\n🔎 ЭТАП 5 - BFS поиск соединений:")
        print(f"   Оставшихся endpoints: {len(remaining_endpoints)}")
        print(f"   Допуск по маске: {BFS_MASK_TOLERANCE}px")

    if len(remaining_endpoints) == 0:
        if verbose:
            print("   Нет оставшихся endpoints для BFS")
        return np.zeros(skeleton_final.shape, dtype=np.uint8), [], set(), []

    h, w = skeleton_final.shape

    skeleton_binary = (skeleton_final > 127).astype(np.uint8)
    nodes_binary = (nodes_mask > 127).astype(np.uint8)
    pipe_mask = (original_gray < 128).astype(np.uint8)
    protection_binary = (protection_mask > 127).astype(np.uint8)

    # Контур узлов
    contours, _ = cv2.findContours(nodes_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    node_contour = np.zeros_like(nodes_binary)
    cv2.drawContours(node_contour, contours, -1, 1, thickness=1)

    # Контур background-узлов — к ним НЕ продлевать
    bg_mask = config.get('background_node_mask')
    background_contour = np.zeros_like(nodes_binary)
    if bg_mask is not None:
        bg_binary = (bg_mask > 127).astype(np.uint8)
        bg_contours, _ = cv2.findContours(bg_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(background_contour, bg_contours, -1, 1, thickness=1)
    endpoint_mask = np.zeros((h, w), dtype=np.int32)
    for ep_idx, ep in enumerate(all_endpoints):
        y, x = ep[0], ep[1]
        endpoint_mask[y, x] = ep_idx + 1

    # 4-связность
    directions = [(-1, 0), (1, 0), (0, -1), (0, 1)]

    bfs_connections_mask = np.zeros(skeleton_final.shape, dtype=np.uint8)
    bfs_info = []
    newly_connected = set()
    all_bfs_paths = []

    debug_stats = {
        'total_bfs': 0,
        'found_node': 0,
        'found_endpoint': 0,
        'found_skeleton': 0,
        'no_path': 0
    }

    t_start = time.time()

    for i, (ep, ep_idx) in enumerate(zip(remaining_endpoints, remaining_ep_indices)):
        if ep_idx in newly_connected:
            continue

        if i % 20 == 0:
            print(f"      BFS {i}/{len(remaining_endpoints)}...", end='\r')

        debug_stats['total_bfs'] += 1

        start_y, start_x = ep[0], ep[1]
        start_component = endpoint_to_component[ep_idx]

        # BFS с подсчётом пикселей по маске
        # visited хранит минимальное количество mask_count для достижения точки
        visited = {}  # (y, x) → min_mask_count
        parent = {}  # (y, x) → (parent_y, parent_x)

        queue = deque()
        # (y, x, depth, mask_count)
        start_mask = 1 if protection_binary[start_y, start_x] > 0 else 0
        queue.append((start_y, start_x, 0, start_mask))
        visited[(start_y, start_x)] = start_mask
        parent[(start_y, start_x)] = None

        found_target = None
        found_type = None
        found_data = None

        while queue:
            cy, cx, depth, mask_count = queue.popleft()

            if depth >= BFS_MAX_DEPTH:
                continue

            # Пропускаем если уже нашли лучший путь к этой точке
            if (cy, cx) in visited and visited[(cy, cx)] < mask_count:
                continue

            for dy, dx in directions:
                ny, nx = cy + dy, cx + dx

                # Границы
                if not (0 <= ny < h and 0 <= nx < w):
                    continue

                # Должны быть на трубе
                if pipe_mask[ny, nx] == 0:
                    continue

                # Считаем mask_count для нового пикселя
                new_mask_count = mask_count
                if protection_binary[ny, nx] > 0:
                    new_mask_count += 1
                    # Превысили лимит
                    if new_mask_count > BFS_MASK_TOLERANCE:
                        continue

                # Проверяем посещение с учётом mask_count
                if (ny, nx) in visited and visited[(ny, nx)] <= new_mask_count:
                    continue

                visited[(ny, nx)] = new_mask_count
                parent[(ny, nx)] = (cy, cx)

                # ========================================
                # ПРОВЕРКА ЦЕЛЕЙ С ПРИОРИТЕТОМ
                # Присоединение только к ЧУЖОЙ компоненте
                # ========================================

                # 1. Node contour (высший приоритет)
                if node_contour[ny, nx] > 0:
                    # Пропустить background-узлы
                    if background_contour[ny, nx] > 0:
                        continue
                    found_target = (ny, nx)
                    found_type = 'node_contour'
                    break

                # 2. Endpoint другой компоненты
                ep_at = endpoint_mask[ny, nx]
                if ep_at > 0:
                    target_ep_idx = ep_at - 1
                    if target_ep_idx != ep_idx:
                        target_component = endpoint_to_component.get(target_ep_idx, -1)
                        if target_component != start_component and target_ep_idx not in connected_eps and target_ep_idx not in newly_connected:
                            found_target = (ny, nx)
                            found_type = 'endpoint'
                            found_data = target_ep_idx
                            break

                # 3. Скелет ДРУГОЙ компоненты (только чужой!)
                if skeleton_binary[ny, nx] > 0:
                    pixel_component = labels[ny, nx]
                    if pixel_component != start_component and pixel_component > 0:
                        found_target = (ny, nx)
                        found_type = 'other_skeleton'
                        found_data = pixel_component
                        break

                queue.append((ny, nx, depth + 1, new_mask_count))

            if found_target:
                break

        # Восстановить путь
        if found_target:
            path = []
            current = found_target
            while current is not None:
                path.append(current)
                current = parent.get(current)
            path.reverse()

            # Нарисовать путь
            for p in path:
                bfs_connections_mask[p[0], p[1]] = 255

            all_bfs_paths.append({
                'points': path,
                'type': found_type,
                'ep_idx': ep_idx
            })

            bfs_info.append({
                'endpoint': ep_idx,
                'target': found_type,
                'path_length': len(path),
                'target_data': found_data
            })

            newly_connected.add(ep_idx)

            if found_type == 'node_contour':
                debug_stats['found_node'] += 1
            elif found_type == 'endpoint':
                debug_stats['found_endpoint'] += 1
                newly_connected.add(found_data)  # Оба endpoints
            elif found_type == 'other_skeleton':
                debug_stats['found_skeleton'] += 1
        else:
            debug_stats['no_path'] += 1
            all_bfs_paths.append({
                'points': [],
                'type': 'no_path',
                'ep_idx': ep_idx
            })

    if verbose:
        print()

    elapsed = time.time() - t_start

    if verbose:
        print(f"\n   ⏱️  Время BFS: {elapsed:.1f}s")
        print(f"   ✓ Соединений построено: {len(bfs_info)}")
        print(f"   Endpoints соединено: {len(newly_connected)}")

    total = debug_stats['total_bfs']
    if verbose and total > 0:
        print(f"\n🔍 DEBUG - Статистика BFS:")
        print(f"   Всего BFS запущено: {total}")
        print(f"   Результаты:")
        print(
            f"      ✅ Нашли контур узла: {debug_stats['found_node']} ({debug_stats['found_node'] / total * 100:.1f}%)")
        print(
            f"      ✅ Нашли endpoint: {debug_stats['found_endpoint']} ({debug_stats['found_endpoint'] / total * 100:.1f}%)")
        print(
            f"      ✅ Нашли скелет другой компоненты: {debug_stats['found_skeleton']} ({debug_stats['found_skeleton'] / total * 100:.1f}%)")
        print(f"      ❌ Путь не найден: {debug_stats['no_path']} ({debug_stats['no_path'] / total * 100:.1f}%)")

    return bfs_connections_mask, bfs_info, newly_connected, all_bfs_paths


def diagnose_unconnected_endpoints(unconnected_indices, all_endpoints, skeleton, nodes_mask,
                                    original_gray, protection_mask, endpoint_to_component,
                                    labels, config):
    """
    Диагностика почему endpoints не соединились

    Для каждого несоединённого endpoint выводит:
    - Координаты
    - Направление
    - Почему не сработал ЭТАП 4 (направленные линии)
    - Почему не сработал ЭТАП 5 (BFS)
    """
    print(f"\n{'=' * 70}")
    print(f"🔬 ДИАГНОСТИКА НЕСОЕДИНЁННЫХ ENDPOINTS ({len(unconnected_indices)} шт.)")
    print(f"{'=' * 70}")

    if len(unconnected_indices) == 0:
        print("   Все endpoints соединены!")
        return

    skeleton_binary = (skeleton > 127).astype(np.uint8)
    nodes_binary = (nodes_mask > 127).astype(np.uint8)
    pipe_mask = (original_gray < 128).astype(np.uint8)
    protection_binary = (protection_mask > 127).astype(np.uint8)

    h, w = skeleton.shape

    # Контур узлов
    contours, _ = cv2.findContours(nodes_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    node_contour = np.zeros_like(nodes_binary)
    cv2.drawContours(node_contour, contours, -1, 1, thickness=1)

    # Контур background-узлов
    bg_mask = config.get('background_node_mask')
    background_contour = np.zeros_like(nodes_binary)
    if bg_mask is not None:
        bg_binary = (bg_mask > 127).astype(np.uint8)
        bg_contours, _ = cv2.findContours(bg_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(background_contour, bg_contours, -1, 1, thickness=1)

    DIRECTION_TRACE_LENGTH = config.get('direction_trace_length', 5)
    MAX_LINE_LENGTH = config.get('max_line_length', 600)
    BFS_MAX_DEPTH = config.get('bfs_max_depth', 1000)
    BFS_MASK_TOLERANCE = config.get('bfs_mask_tolerance', 5)

    for idx in unconnected_indices:
        ep = all_endpoints[idx]
        y, x = ep[0], ep[1]
        component = endpoint_to_component.get(idx, 0)

        print(f"\n{'─' * 50}")
        print(f"📍 Endpoint #{idx}: ({y}, {x}), компонента={component}")

        # === ДИАГНОСТИКА ЭТАПА 4: Направленные линии ===
        print(f"\n   🌉 ЭТАП 4 - Направленные линии:")

        direction_vector = get_endpoint_direction(skeleton_binary, ep, DIRECTION_TRACE_LENGTH)

        if direction_vector is None:
            print(f"      ❌ Не удалось определить направление (слишком короткая ветка)")
            continue

        directions = round_to_4_directions(direction_vector)
        print(f"      Направление: {direction_vector} → {directions}")

        for dy, dx in directions:
            dir_name = {(-1,0): '↑', (1,0): '↓', (0,-1): '←', (0,1): '→'}.get((dy,dx), '?')
            print(f"\n      Направление {dir_name} ({dy}, {dx}):")

            current = np.array(ep, dtype=int)
            stop_reason = None
            stop_step = 0

            for step in range(1, MAX_LINE_LENGTH + 1):
                current = current + np.array([dy, dx])

                if not (0 <= current[0] < h and 0 <= current[1] < w):
                    stop_reason = f"вышли за границы на шаге {step}"
                    stop_step = step
                    break

                cy, cx = current[0], current[1]

                if pipe_mask[cy, cx] == 0:
                    stop_reason = f"белый фон (не труба) на шаге {step}, позиция ({cy}, {cx})"
                    stop_step = step
                    break

                if node_contour[cy, cx] > 0:
                    if background_contour[cy, cx] > 0:
                        stop_reason = f"❌ background-узел на шаге {step} — НЕ продлевать"
                    else:
                        stop_reason = f"✅ ДОЛЖЕН БЫЛ соединиться с узлом на шаге {step}!"
                    stop_step = step
                    break

                if protection_binary[cy, cx] > 0:
                    stop_reason = f"защитная маска на шаге {step}, позиция ({cy}, {cx})"
                    stop_step = step
                    break

                if skeleton_binary[cy, cx] > 0:
                    pixel_component = labels[cy, cx]
                    if pixel_component == component:
                        stop_reason = f"свой скелет (компонента {component}) на шаге {step}"
                    else:
                        stop_reason = f"✅ ДОЛЖЕН БЫЛ соединиться со скелетом компоненты {pixel_component} на шаге {step}!"
                    stop_step = step
                    break

            if stop_reason is None:
                stop_reason = f"достигли max_length={MAX_LINE_LENGTH}"
                stop_step = MAX_LINE_LENGTH

            print(f"         → {stop_reason}")

        # === ДИАГНОСТИКА ЭТАПА 5: BFS ===
        print(f"\n   🔎 ЭТАП 5 - BFS (max_depth={BFS_MAX_DEPTH}, mask_tolerance={BFS_MASK_TOLERANCE}):")

        # Простой BFS для диагностики
        visited = {}
        queue = [(y, x, 0, 0)]  # y, x, depth, mask_count
        visited[(y, x)] = 0

        max_reached_depth = 0
        stopped_by_depth = False
        stopped_by_mask = False
        found_but_same_component = []

        directions_4 = [(-1, 0), (1, 0), (0, -1), (0, 1)]

        while queue:
            cy, cx, depth, mask_count = queue.pop(0)
            max_reached_depth = max(max_reached_depth, depth)

            if depth >= BFS_MAX_DEPTH:
                stopped_by_depth = True
                continue

            for dy, dx in directions_4:
                ny, nx = cy + dy, cx + dx

                if not (0 <= ny < h and 0 <= nx < w):
                    continue

                if pipe_mask[ny, nx] == 0:
                    continue

                new_mask_count = mask_count + (1 if protection_binary[ny, nx] > 0 else 0)
                if new_mask_count > BFS_MASK_TOLERANCE:
                    stopped_by_mask = True
                    continue

                if (ny, nx) in visited and visited[(ny, nx)] <= new_mask_count:
                    continue

                visited[(ny, nx)] = new_mask_count

                # Проверяем цели
                if node_contour[ny, nx] > 0:
                    if background_contour[ny, nx] > 0:
                        print(f"      ❌ BFS нашёл background-узел на глубине {depth+1} в ({ny}, {nx}) — пропускаем")
                    else:
                        print(f"      ✅ BFS нашёл узел на глубине {depth+1} в ({ny}, {nx})!")
                        print(f"         ⚠️ Но почему-то не соединился - проверьте логику!")

                if skeleton_binary[ny, nx] > 0 and labels[ny, nx] != component and labels[ny, nx] > 0:
                    print(f"      ✅ BFS нашёл скелет компоненты {labels[ny, nx]} на глубине {depth+1}!")
                    print(f"         ⚠️ Но почему-то не соединился - проверьте логику!")

                if skeleton_binary[ny, nx] > 0 and labels[ny, nx] == component:
                    found_but_same_component.append((ny, nx, depth+1))

                queue.append((ny, nx, depth + 1, new_mask_count))

        print(f"      Максимальная достигнутая глубина: {max_reached_depth}")
        print(f"      Посещено пикселей: {len(visited)}")

        if stopped_by_depth:
            print(f"      ❌ Ограничен max_depth={BFS_MAX_DEPTH}")
        if stopped_by_mask:
            print(f"      ❌ Были остановки из-за mask_tolerance={BFS_MASK_TOLERANCE}")
        if found_but_same_component:
            print(f"      ⚠️ Находил свой скелет (та же компонента) {len(found_but_same_component)} раз")

        if max_reached_depth < 50:
            print(f"      ❌ BFS почти не продвинулся - endpoint изолирован (белый фон вокруг?)")

    print(f"\n{'=' * 70}")
    print(f"🔬 КОНЕЦ ДИАГНОСТИКИ")
    print(f"{'=' * 70}")


def remove_orphan_components(skeleton, nodes_mask, touch_distance=3, verbose=True):
    """
    Удалить компоненты скелета которые не касаются узлов.
    Оптимизация: проверка касания через bbox ROI + numpy LUT для удаления.
    """
    skeleton_binary = (skeleton > 127).astype(np.uint8)
    nodes_binary = (nodes_mask > 127).astype(np.uint8)

    contours, _ = cv2.findContours(nodes_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    node_contour = np.zeros_like(nodes_binary)
    cv2.drawContours(node_contour, contours, -1, 1, thickness=1)

    if touch_distance > 0:
        kernel = np.ones((touch_distance*2+1, touch_distance*2+1), np.uint8)
        nodes_touch_zone = cv2.dilate(node_contour, kernel, iterations=1)
    else:
        nodes_touch_zone = node_contour
    nodes_touch_zone = nodes_touch_zone | nodes_binary

    num_comp, labels, stats_cv, centroids = cv2.connectedComponentsWithStats(skeleton_binary)

    # LUT: keep[comp_id] = 1 если касается узла
    keep = np.zeros(num_comp, dtype=np.uint8)
    kept_count = 0
    removed_count = 0
    removed_pixels = 0
    removed_list = []

    for comp_id in range(1, num_comp):
        bx = stats_cv[comp_id, cv2.CC_STAT_LEFT]
        by = stats_cv[comp_id, cv2.CC_STAT_TOP]
        bw = stats_cv[comp_id, cv2.CC_STAT_WIDTH]
        bh = stats_cv[comp_id, cv2.CC_STAT_HEIGHT]

        labels_roi = labels[by:by+bh, bx:bx+bw]
        touch_roi = nodes_touch_zone[by:by+bh, bx:bx+bw]
        comp_roi = (labels_roi == comp_id)

        if np.any(comp_roi & touch_roi):
            keep[comp_id] = 1
            kept_count += 1
        else:
            removed_count += 1
            comp_size = stats_cv[comp_id, cv2.CC_STAT_AREA]
            removed_pixels += comp_size
            cy, cx = int(centroids[comp_id][1]), int(centroids[comp_id][0])
            removed_list.append((comp_id, comp_size, cy, cx))

    # Один проход: removed_mask = все пиксели с keep==0 (кроме фона)
    removed_mask = ((keep[labels] == 0) & (labels > 0)).astype(np.uint8)
    cleaned_skeleton = skeleton_binary & (~removed_mask)

    if verbose:
        print(f"\n🧹 Удаление orphan компонент (touch_distance={touch_distance}px):")
        print(f"   Всего компонент: {num_comp - 1}")
        print(f"   Подключены к узлам: {kept_count}")
        print(f"   Удалено (не подключены): {removed_count}")
        print(f"   Удалено пикселей: {removed_pixels}")

        if removed_list and len(removed_list) <= 10:
            print(f"   Удалённые компоненты:")
            for comp_id, size, cy, cx in sorted(removed_list, key=lambda x: -x[1]):
                print(f"      - {size}px @ ({cy}, {cx})")
        elif removed_list:
            print(f"   Топ-5 крупнейших удалённых:")
            for comp_id, size, cy, cx in sorted(removed_list, key=lambda x: -x[1])[:5]:
                print(f"      - {size}px @ ({cy}, {cx})")

    stats = {
        'total_components': num_comp - 1,
        'kept': kept_count,
        'removed': removed_count,
        'removed_pixels': removed_pixels,
        'removed_list': removed_list
    }

    return cleaned_skeleton * 255, removed_mask * 255, stats


def trim_orphan_components(skeleton, nodes_mask, trim_length=5, touch_distance=3, verbose=True):
    """
    Подрезать endpoints orphan-компонент скелета (не касающихся узлов).

    Компоненты, касающиеся узлов — не трогаем.
    Orphan-компоненты: с каждого endpoint стираем trim_length пикселей.
    Если компонента короче 2*trim_length — может исчезнуть полностью.

    Оптимизация: работа через bbox ROI вместо full-image масок.
    """
    skeleton_binary = (skeleton > 127).astype(np.uint8)
    nodes_binary = (nodes_mask > 127).astype(np.uint8)

    # Зона касания узлов
    contours, _ = cv2.findContours(nodes_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    node_contour = np.zeros_like(nodes_binary)
    cv2.drawContours(node_contour, contours, -1, 1, thickness=1)

    if touch_distance > 0:
        kernel = np.ones((touch_distance * 2 + 1, touch_distance * 2 + 1), np.uint8)
        nodes_touch_zone = cv2.dilate(node_contour, kernel, iterations=1)
    else:
        nodes_touch_zone = node_contour
    nodes_touch_zone = nodes_touch_zone | nodes_binary

    # Связные компоненты скелета
    num_comp, labels, stats_cv, centroids = cv2.connectedComponentsWithStats(skeleton_binary)

    trimmed_mask = np.zeros_like(skeleton_binary)
    orphan_count = 0
    trimmed_endpoints = 0
    fully_removed = 0

    for comp_id in range(1, num_comp):
        # --- ROI из bbox ---
        bx = stats_cv[comp_id, cv2.CC_STAT_LEFT]
        by = stats_cv[comp_id, cv2.CC_STAT_TOP]
        bw = stats_cv[comp_id, cv2.CC_STAT_WIDTH]
        bh = stats_cv[comp_id, cv2.CC_STAT_HEIGHT]

        # Добавляем margin=1 для корректной работы filter2D на краях
        margin = 1
        y1 = max(0, by - margin)
        y2 = min(skeleton_binary.shape[0], by + bh + margin)
        x1 = max(0, bx - margin)
        x2 = min(skeleton_binary.shape[1], bx + bw + margin)

        labels_roi = labels[y1:y2, x1:x2]
        comp_mask_roi = (labels_roi == comp_id).astype(np.uint8)

        # Проверяем касание с узлами (тоже на ROI)
        touch_roi = nodes_touch_zone[y1:y2, x1:x2]
        if np.any(comp_mask_roi & touch_roi):
            continue

        orphan_count += 1

        # Endpoints этой компоненты (на ROI)
        comp_endpoints_roi = find_skeleton_endpoints(comp_mask_roi * 255)

        if not comp_endpoints_roi:
            continue

        # skeleton_binary ROI для trace_from_endpoint
        skel_roi = skeleton_binary[y1:y2, x1:x2]

        for ep_local in comp_endpoints_roi:
            path_local, _ = trace_from_endpoint(skel_roi, ep_local, trim_length)
            for py_local, px_local in path_local:
                # Глобальные координаты
                gy, gx = py_local + y1, px_local + x1
                if skeleton_binary[gy, gx] > 0:
                    trimmed_mask[gy, gx] = 1
                    skeleton_binary[gy, gx] = 0
            trimmed_endpoints += 1

        # Проверяем, не исчезла ли компонента полностью
        remaining_roi = skeleton_binary[y1:y2, x1:x2]
        if not np.any((labels_roi == comp_id) & (remaining_roi > 0)):
            fully_removed += 1

    trimmed_pixels = int(np.sum(trimmed_mask))

    if verbose:
        print(f"\n✂️ Подрезка orphan компонент (trim={trim_length}px, touch={touch_distance}px):")
        print(f"   Всего компонент: {num_comp - 1}")
        print(f"   Orphan (не касаются узлов): {orphan_count}")
        print(f"   Подрезано endpoints: {trimmed_endpoints}")
        print(f"   Удалено пикселей: {trimmed_pixels}")
        print(f"   Полностью исчезли: {fully_removed}")

    stats = {
        'total_components': num_comp - 1,
        'orphan_count': orphan_count,
        'trimmed_endpoints': trimmed_endpoints,
        'trimmed_pixels': trimmed_pixels,
        'fully_removed': fully_removed,
    }

    return skeleton_binary * 255, trimmed_mask * 255, stats

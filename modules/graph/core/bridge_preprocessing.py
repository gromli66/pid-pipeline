"""
Bridge Preprocessing - упрощённый анализ и routing мостов.

Алгоритм (упрощённый):
1. Для каждого моста найти 4 контактные точки со скелетом
2. Разделить точки по сторонам квадрата: север-юг и запад-восток
3. Routing: вход сверху → выход снизу, вход слева → выход справа
4. Невалидные мосты (не 4 точки или не 2+2 распределение) → обычные узлы

Невалидный мост:
- Добавляется в connection_mask как обычный junction
- Трассировка проходит ЧЕРЕЗ него (без телепортации)
- Все входящие трубы соединяются в одном узле
"""
import numpy as np
import cv2
from scipy import ndimage
from typing import Dict, Tuple, List, Optional


def preprocess_bridges(bridge_mask: np.ndarray,
                      connection_mask: np.ndarray,
                      skeleton: np.ndarray,
                      dilation: int = 1,
                      verbose: bool = False) -> Dict:
    """
    Препроцессинг всех мостов: разделение на валидные/невалидные.

    Валидный мост:
        - Ровно 4 контактные точки со скелетом
        - Точки распределены 2+2 по противоположным сторонам
        - Телепортация: вход сверху → выход снизу, вход слева → выход справа

    Невалидный мост:
        - Не 4 точки или не 2+2 распределение
        - Становится обычным узлом (junction)
        - Трассировка проходит ЧЕРЕЗ него без телепортации

    Args:
        bridge_mask: Бинарная маска всех мостов
        connection_mask: Маска connections (для обновления)
        skeleton: Скелет труб
        dilation: Дилатация для поиска контактов
        verbose: Подробный вывод

    Returns:
        Словарь с результатами:
            'valid_bridges_mask': маска валидных мостов
            'invalid_bridges_mask': маска невалидных мостов
            'bridge_routing': {bridge_id: {point: 'pipe1'/'pipe2'}}
            'bridge_info': {bridge_id: {...метаданные...}}
            'updated_connections_mask': connections + invalid bridges
    """
    # Инициализация масок
    valid_bridges_mask = np.zeros_like(bridge_mask, dtype=bool)
    invalid_bridges_mask = np.zeros_like(bridge_mask, dtype=bool)

    # Routing и метаданные
    bridge_routing = {}
    bridge_info = {}

    # Нумерация мостов
    labeled_bridges, num_bridges = ndimage.label(bridge_mask)

    if verbose:
        print(f"\nBridge Preprocessing (simplified):")
        print(f"  Всего мостов: {num_bridges}")

    valid_count = 0
    invalid_count = 0

    # Обработать каждый мост
    for bridge_id in range(1, num_bridges + 1):
        bridge_mask_single = (labeled_bridges == bridge_id)

        # Попытаться создать routing
        routing_result = route_single_bridge(
            bridge_mask=bridge_mask_single,
            skeleton=skeleton,
            dilation=dilation
        )

        if routing_result is not None:
            # ВАЛИДНЫЙ мост
            valid_bridges_mask |= bridge_mask_single
            bridge_routing[bridge_id] = routing_result['routing']
            bridge_info[bridge_id] = routing_result['info']
            valid_count += 1

            if verbose:
                info = routing_result['info']
                print(f"    Bridge {bridge_id}: VALID")
                print(f"      Вертикальная труба (↕): {info['vertical_pipe']}")
                print(f"      Горизонтальная труба (↔): {info['horizontal_pipe']}")
        else:
            # НЕВАЛИДНЫЙ мост → становится обычным узлом
            invalid_bridges_mask |= bridge_mask_single
            invalid_count += 1

            if verbose:
                # Показать причину
                contacts = find_bridge_contacts(bridge_mask_single, skeleton, dilation)
                print(f"    Bridge {bridge_id}: INVALID ({len(contacts)} контактов, нужно 4)")

    # Обновить connections mask: добавить невалидные мосты
    # Невалидные мосты становятся обычными junction узлами
    updated_connections_mask = np.logical_or(connection_mask, invalid_bridges_mask)

    if verbose:
        print(f"\n  Результат:")
        print(f"    Валидные мосты: {valid_count} (телепортация)")
        print(f"    Невалидные мосты: {invalid_count} (станут junction)")
        print(f"    Connections: {np.sum(connection_mask)} → {np.sum(updated_connections_mask)} px")

    return {
        'valid_bridges_mask': valid_bridges_mask,
        'invalid_bridges_mask': invalid_bridges_mask,
        'bridge_routing': bridge_routing,
        'bridge_info': bridge_info,
        'updated_connections_mask': updated_connections_mask
    }


def route_single_bridge(bridge_mask: np.ndarray,
                       skeleton: np.ndarray,
                       dilation: int = 1) -> Optional[Dict]:
    """
    Создать routing для одного моста.

    Упрощённый алгоритм:
    1. Найти 4 контактные точки
    2. Распределить по сторонам квадрата (север/юг/запад/восток)
    3. Вертикальная труба: север ↔ юг
    4. Горизонтальная труба: запад ↔ восток
    5. Если не 2+2 распределение → невалидный

    Args:
        bridge_mask: Маска одного моста
        skeleton: Скелет труб
        dilation: Дилатация для поиска контактов

    Returns:
        None если мост невалидный, иначе:
        {
            'routing': {point: 'pipe1'/'pipe2'},
            'info': {метаданные}
        }
    """
    # Шаг 1: Найти контактные точки
    contact_points = find_bridge_contacts(bridge_mask, skeleton, dilation)

    # Проверка: должно быть ровно 4 точки
    if len(contact_points) != 4:
        return None

    # Шаг 2: Центр моста
    center = ndimage.center_of_mass(bridge_mask)
    center_y, center_x = int(center[0]), int(center[1])

    # Шаг 3: Распределить точки по сторонам квадрата
    sides = {
        'north': [],  # выше центра (y < center_y)
        'south': [],  # ниже центра (y > center_y)
        'west': [],   # левее центра (x < center_x)
        'east': []    # правее центра (x > center_x)
    }

    for point in contact_points:
        py, px = point
        dy = py - center_y
        dx = px - center_x

        # Определить главную ось (какое смещение больше)
        if abs(dy) >= abs(dx):
            # Вертикальная ось доминирует
            if dy < 0:
                sides['north'].append(point)
            else:
                sides['south'].append(point)
        else:
            # Горизонтальная ось доминирует
            if dx < 0:
                sides['west'].append(point)
            else:
                sides['east'].append(point)

    # Шаг 4: Сформировать трубы
    vertical_pipe = sides['north'] + sides['south']    # ↕
    horizontal_pipe = sides['west'] + sides['east']    # ↔

    # Проверка: должно быть 2+2
    if len(vertical_pipe) != 2 or len(horizontal_pipe) != 2:
        return None

    # Шаг 5: Создать routing
    # pipe1 = вертикальная труба (север-юг)
    # pipe2 = горизонтальная труба (запад-восток)
    routing = {}

    for point in vertical_pipe:
        routing[point] = 'pipe1'

    for point in horizontal_pipe:
        routing[point] = 'pipe2'

    # Метаданные
    info = {
        'center': (center_y, center_x),
        'vertical_pipe': tuple(vertical_pipe),
        'horizontal_pipe': tuple(horizontal_pipe),
        'pipe1_points': tuple(vertical_pipe),
        'pipe2_points': tuple(horizontal_pipe)
    }

    return {
        'routing': routing,
        'info': info
    }


def find_bridge_contacts(bridge_mask: np.ndarray,
                        skeleton: np.ndarray,
                        dilation: int = 1) -> List[Tuple[int, int]]:
    """
    Найти контактные точки моста со скелетом.

    Args:
        bridge_mask: Маска одного моста
        skeleton: Скелет труб
        dilation: Дилатация моста

    Returns:
        Список координат точек в формате (y, x)
    """
    bridge_uint8 = (bridge_mask.astype(np.uint8)) * 255

    # Дилатация
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    dilated = cv2.dilate(bridge_uint8, kernel, iterations=dilation)

    # Контур
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    boundary = np.zeros_like(bridge_uint8)
    cv2.drawContours(boundary, contours, -1, 255, 1)

    # Пересечение со скелетом
    skeleton_uint8 = (skeleton.astype(np.uint8)) * 255
    contact_mask = cv2.bitwise_and(skeleton_uint8, boundary)

    # Координаты
    contact_coords = np.argwhere(contact_mask > 0)
    contact_points = [(int(y), int(x)) for y, x in contact_coords]

    return contact_points
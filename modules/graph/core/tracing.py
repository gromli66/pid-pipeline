"""
ЭТАП 2: Трассировка рёбер по скелету.

BFS/DFS трассировка путей от узла до узла.

ИЗМЕНЕНИЯ v3:
- Узлы определяются динамически через identify_node_by_point
- Узлы формируются по ann_idx (equipment) или label_id (connector/unknown)
- source_point и target_point в каждом ребре
"""
import numpy as np
from scipy import ndimage
from typing import List, Dict, Tuple, Optional
from .utils import get_8_neighbors
from .nodes import identify_node_by_point, EXCLUDED_CLASS_IDS


def _polygon_centroid(xs, ys):
    """
    Shoelace centroid — площадной центроид полигона.
    Возвращает (centroid_x, centroid_y) в координатах [x, y].
    Если полигон вырожденный (area ≈ 0) — fallback на среднее вершин.
    """
    n = len(xs)
    if n == 0:
        return None, None
    signed_area = 0.0
    cx_sum = 0.0
    cy_sum = 0.0
    for i in range(n):
        j = (i + 1) % n
        cross = xs[i] * ys[j] - xs[j] * ys[i]
        signed_area += cross
        cx_sum += (xs[i] + xs[j]) * cross
        cy_sum += (ys[i] + ys[j]) * cross
    signed_area *= 0.5
    if abs(signed_area) > 1e-6:
        return int(cx_sum / (6.0 * signed_area)), int(cy_sum / (6.0 * signed_area))
    else:
        return int(sum(xs) / n), int(sum(ys) / n)

def trace_edges_v3(skeleton: np.ndarray,
                   labeled_equipment: np.ndarray,
                   labeled_connectors: np.ndarray,
                   contact_map: np.ndarray,
                   bridge_contact_map: np.ndarray,
                   bridge_routing: Dict,
                   equipment_mask: np.ndarray,
                   connection_mask: np.ndarray,
                   annotations: List[Dict],
                   max_path_length: int = 10000,
                   connector_offset: int = 0,
                   debug: bool = False) -> Tuple[List[Dict], List[Dict]]:
    """
    Трассировка всех рёбер графа с динамическим созданием узлов.

    Args:
        skeleton: Бинарная маска скелета (ОЧИЩЕННАЯ от узлов)
        labeled_equipment: Нумерованная маска equipment (label_id: 1..N)
        labeled_connectors: Нумерованная маска connectors (label_id: 1..M)
        contact_map: Карта точек контакта (label_id: 1..N для equipment, N+1..N+M для connectors)
        bridge_contact_map: Карта точек контакта мостов
        bridge_routing: Словарь routing для мостов
        equipment_mask: Маска оборудования
        connection_mask: Маска connector'ов
        annotations: Список COCO аннотаций (bbox + segmentation)
        max_path_length: Максимальная длина пути
        connector_offset: Сдвиг label_id для connectors (= num_equipment)
        debug: Выводить отладочную информацию

    Returns:
        (edges, nodes) - списки рёбер и динамически созданных узлов
    """
    height, width = skeleton.shape

    visited = np.zeros_like(skeleton, dtype=bool)

    edges = []
    edge_counter = 0
    
    # Динамическое создание узлов
    # Ключ: ('coco', ann_idx) для equipment или ('connector', label_id) или ('unknown', label_id)
    node_registry = {}
    node_counter = 0

    def get_or_create_node(point_y: int, point_x: int) -> Optional[str]:
        """Получить или создать узел по координатам точки контакта."""
        nonlocal node_counter
        
        node_info = identify_node_by_point(
            x=point_x, y=point_y,
            equipment_mask=equipment_mask,
            connection_mask=connection_mask,
            annotations=annotations,
            labeled_equipment=labeled_equipment,
            labeled_connectors=labeled_connectors
        )
        
        if node_info is None:
            return None

        if node_info.get('class_id') in EXCLUDED_CLASS_IDS:
            return None

        # Формируем ключ для registry
        if node_info['ann_idx'] is not None:
            # Equipment с COCO аннотацией
            key = ('coco', node_info['ann_idx'])
        elif node_info['class_name'] == 'connector':
            # Connector — по label_id из labeled_connectors
            key = ('connector', node_info.get('label_id'))
        else:
            # Unknown polygon — по label_id из labeled_equipment
            key = ('unknown', node_info.get('label_id'))
        
        if key not in node_registry:
            node_id = f'node_{node_counter}'
            node_counter += 1
            
            # Вычисляем центроид и area
            # Приоритет: segmentation → bbox COCO → маска компоненты
            area = 0
            centroid_x, centroid_y = None, None

            # 1. Если есть segmentation — центроид полигона (Shoelace)
            if node_info.get('segmentation'):
                seg = node_info['segmentation']
                # segmentation: [x1, y1, x2, y2, ...] — плоский список
                xs = [seg[i] for i in range(0, len(seg), 2)]
                ys = [seg[i] for i in range(1, len(seg), 2)]
                centroid_x, centroid_y = _polygon_centroid(xs, ys)
                # area из полигона (приблизительно через bbox)
                if node_info.get('bbox'):
                    x_min, y_min, x_max, y_max = node_info['bbox']
                    area = (x_max - x_min) * (y_max - y_min)

            # 2. Если есть bbox из COCO (ann_idx не None) — центроид bbox
            elif node_info.get('ann_idx') is not None and node_info.get('bbox'):
                x_min, y_min, x_max, y_max = node_info['bbox']
                centroid_x = (x_min + x_max) // 2
                centroid_y = (y_min + y_max) // 2
                area = (x_max - x_min) * (y_max - y_min)

            # 3. Для connector — центроид из labeled_connectors
            elif node_info['class_name'] == 'connector' and node_info.get('label_id'):
                label_id = node_info['label_id']
                coords = np.argwhere(labeled_connectors == label_id)
                if len(coords) > 0:
                    centroid_y = int(np.mean(coords[:, 0]))
                    centroid_x = int(np.mean(coords[:, 1]))
                    area = len(coords)

            # 4. Для unknown — центроид из labeled_equipment
            elif node_info.get('label_id'):
                label_id = node_info['label_id']
                coords = np.argwhere(labeled_equipment == label_id)
                if len(coords) > 0:
                    centroid_y = int(np.mean(coords[:, 0]))
                    centroid_x = int(np.mean(coords[:, 1]))
                    area = len(coords)

            # 5. Fallback — точка контакта
            if centroid_x is None or centroid_y is None:
                centroid_y, centroid_x = point_y, point_x

            node_registry[key] = {
                'id': node_id,
                'type': node_info['type'],
                'class_id': node_info['class_id'],
                'class_name': node_info['class_name'],
                'bbox': list(node_info['bbox']) if node_info['bbox'] else None,
                'segmentation': node_info.get('segmentation'),  # Полигон из COCO
                'ann_idx': node_info.get('ann_idx'),
                'label_id': (node_info.get('label_id') + connector_offset) if (node_info['class_name'] == 'connector' and node_info.get('label_id') is not None) else node_info.get('label_id'),
                'centroid': [centroid_y, centroid_x],
                'area': area,
                'degree': 0
            }

        return node_registry[key]['id']

    if debug:
        print(f"Начало трассировки рёбер (v3)...")

    # Находим все точки контакта
    contact_coords = np.argwhere(contact_map > 0)

    for start_y, start_x in contact_coords:
        if visited[start_y, start_x]:
            continue

        start_node_id = get_or_create_node(start_y, start_x)
        if start_node_id is None:
            visited[start_y, start_x] = True
            continue

        edge = trace_single_edge_v3(
            start_node_id=start_node_id,
            start_point=(start_y, start_x),
            skeleton=skeleton,
            contact_map=contact_map,
            bridge_contact_map=bridge_contact_map,
            bridge_routing=bridge_routing,
            visited=visited,
            get_or_create_node=get_or_create_node,
            max_path_length=max_path_length,
            debug=debug
        )

        if edge:
            edge['id'] = f'edge_{edge_counter}'
            edges.append(edge)
            edge_counter += 1

    if debug:
        print(f"Трассировка завершена. Найдено рёбер: {len(edges)}, узлов: {len(node_registry)}")

    # ===== ДОБАВЛЕНИЕ НЕДОСТАЮЩИХ COCO УЗЛОВ =====
    # Собираем ann_idx которые уже в графе
    used_ann_idx = set()
    for key in node_registry:
        if key[0] == 'coco':
            used_ann_idx.add(key[1])

    # Исключённые классы
    excluded_classes = {34, 36, 38, 39}  # annotation, truba (не узлы)

    # Добавляем недостающие COCO аннотации как изолированные узлы
    for label in annotations:
        if label['class_id'] in EXCLUDED_CLASS_IDS:
            continue
        if label['idx'] in used_ann_idx:
            continue

        # Эта COCO аннотация не попала через трассировку — добавляем
        node_id = f'node_{node_counter}'
        node_counter += 1

        x_min, y_min, x_max, y_max = label['bbox']
        area = (x_max - x_min) * (y_max - y_min)

        # Центроид: segmentation (Shoelace) → bbox
        if label.get('segmentation'):
            seg = label['segmentation']
            xs = [seg[i] for i in range(0, len(seg), 2)]
            ys = [seg[i] for i in range(1, len(seg), 2)]
            centroid_x, centroid_y = _polygon_centroid(xs, ys)
            if centroid_x is None:
                centroid_x = (x_min + x_max) // 2
                centroid_y = (y_min + y_max) // 2
        else:
            centroid_x = (x_min + x_max) // 2
            centroid_y = (y_min + y_max) // 2

        key = ('coco', label['idx'])
        node_registry[key] = {
            'id': node_id,
            'type': 'equipment',
            'class_id': label['class_id'],
            'class_name': label['class_name'],
            'bbox': list(label['bbox']),
            'segmentation': label.get('segmentation'),  # Полигон из COCO
            'ann_idx': label['idx'],
            'label_id': None,
            'centroid': [centroid_y, centroid_x],
            'area': area,
            'degree': 0
        }

    if debug:
        added_coco = len(node_registry) - len(used_ann_idx) - len([k for k in node_registry if k[0] != 'coco'])
        print(f"Добавлено изолированных COCO узлов: {added_coco}")

    # ===== ДОБАВЛЕНИЕ ИЗОЛИРОВАННЫХ ПОЛИГОНОВ =====
    # Находим компоненты маски которые не имеют контакта со скелетом
    from scipy import ndimage as ndi

    # Какие label_id уже использованы для connectors
    used_connector_label_ids = set()
    # Какие label_id уже использованы для equipment/unknown
    used_equipment_label_ids = set()
    
    for key in node_registry:
        if key[0] == 'connector':
            if key[1] is not None:
                used_connector_label_ids.add(key[1])
        elif key[0] == 'unknown':
            if key[1] is not None:
                used_equipment_label_ids.add(key[1])
        elif key[0] == 'coco':
            # ВАЖНО: также добавляем label_id от COCO узлов!
            node_data = node_registry[key]
            if node_data.get('label_id') is not None:
                used_equipment_label_ids.add(node_data['label_id'])

    # Также добавляем label_id от ВСЕХ COCO аннотаций (включая excluded!)
    # Это нужно чтобы не создавать unknow дубликаты для output и других excluded
    for label in annotations:
        # НЕ пропускаем excluded — нужно учитывать все bbox
        x_min, y_min, x_max, y_max = label['bbox']
        cx = (x_min + x_max) // 2
        cy = (y_min + y_max) // 2
        if 0 <= cy < labeled_equipment.shape[0] and 0 <= cx < labeled_equipment.shape[1]:
            lid = labeled_equipment[cy, cx]
            if lid > 0:
                used_equipment_label_ids.add(int(lid))

        # ВАЖНО: для объектов с segmentation проверяем ВСЕ точки полигона
        # чтобы найти все label_id которые покрывает полигон
        if label.get('segmentation'):
            seg = label['segmentation']
            for i in range(0, len(seg), 2):
                px, py = int(seg[i]), int(seg[i+1])
                if 0 <= py < labeled_equipment.shape[0] and 0 <= px < labeled_equipment.shape[1]:
                    lid = labeled_equipment[py, px]
                    if lid > 0:
                        used_equipment_label_ids.add(int(lid))

    # Проверяем изолированные equipment компоненты
    num_equipment_labels = labeled_equipment.max()
    for label_id in range(1, num_equipment_labels + 1):
        if label_id in used_equipment_label_ids:
            continue

        # Эта компонента не использована
        comp_mask = (labeled_equipment == label_id)
        coords = np.argwhere(comp_mask)
        if len(coords) == 0:
            continue

        # Берём центроид
        cy = int(np.mean(coords[:, 0]))
        cx = int(np.mean(coords[:, 1]))

        # Вычисляем bbox компоненты
        y_min_c, x_min_c = coords.min(axis=0)
        y_max_c, x_max_c = coords.max(axis=0)
        comp_bbox = (int(x_min_c), int(y_min_c), int(x_max_c), int(y_max_c))
        area = len(coords)

        node_id = f'node_{node_counter}'
        node_counter += 1

        # Это изолированный unknown полигон
        key = ('unknown', label_id)
        node_registry[key] = {
            'id': node_id,
            'type': 'equipment',
            'class_id': 37,  # unknow
            'class_name': 'unknow',
            'bbox': list(comp_bbox),
            'ann_idx': None,
            'label_id': label_id,
            'centroid': [cy, cx],
            'area': area,
            'degree': 0
        }

    # Проверяем изолированные connector компоненты
    num_connector_labels = labeled_connectors.max()
    for label_id in range(1, num_connector_labels + 1):
        if label_id in used_connector_label_ids:
            continue

        # Эта компонента не использована
        comp_mask = (labeled_connectors == label_id)
        coords = np.argwhere(comp_mask)
        if len(coords) == 0:
            continue

        # Берём центроид
        cy = int(np.mean(coords[:, 0]))
        cx = int(np.mean(coords[:, 1]))

        # Вычисляем bbox компоненты
        y_min_c, x_min_c = coords.min(axis=0)
        y_max_c, x_max_c = coords.max(axis=0)
        comp_bbox = (int(x_min_c), int(y_min_c), int(x_max_c), int(y_max_c))
        area = len(coords)

        node_id = f'node_{node_counter}'
        node_counter += 1

        # Это изолированный connector
        key = ('connector', label_id)
        node_registry[key] = {
            'id': node_id,
            'type': 'connector',
            'class_id': -1,
            'class_name': 'connector',
            'bbox': list(comp_bbox),
            'ann_idx': None,
            'label_id': label_id + connector_offset,
            'centroid': [cy, cx],
            'area': area,
            'degree': 0
        }

    if debug:
        print(f"Всего узлов после добавления изолированных: {len(node_registry)}")

    # Преобразуем registry в список
    nodes = list(node_registry.values())

    return edges, nodes


def trace_single_edge_v3(start_node_id: str,
                         start_point: Tuple[int, int],
                         skeleton: np.ndarray,
                         contact_map: np.ndarray,
                         bridge_contact_map: np.ndarray,
                         bridge_routing: Dict,
                         visited: np.ndarray,
                         get_or_create_node,
                         max_path_length: int = 10000,
                         min_spur_length: int = 5,
                         debug: bool = False) -> Optional[Dict]:
    """
    Трассировка одного ребра от стартовой точки (v3) с откатом к развилкам.

    ИЗМЕНЕНИЯ:
    - При попадании в тупик откатывается к последней развилке
    - Если путь ведёт к excluded узлу (get_or_create_node returns None) — считается тупиком
    - Пробует альтернативные направления пока не найдёт валидный узел
    """
    height, width = skeleton.shape

    # Локальный visited для текущей трассировки (можем откатывать)
    local_visited = np.zeros_like(visited, dtype=bool)

    path = [start_point]
    current = start_point
    previous = None
    edge_color = None
    just_teleported = False

    # Стек развилок: [(fork_point, path_index, remaining_neighbors, previous_at_fork, edge_color_at_fork), ...]
    fork_stack = []

    local_visited[current[0], current[1]] = True

    def try_rollback_to_fork():
        """Попытка отката к последней развилке. Возвращает True если откат успешен."""
        nonlocal path, current, previous, edge_color, local_visited, fork_stack

        while fork_stack:
            fork_point, fork_path_idx, remaining, prev_at_fork, color_at_fork = fork_stack.pop()

            if remaining:
                # Есть ещё направления для проверки
                # Помечаем тупиковую ветку как visited глобально
                for i in range(fork_path_idx + 1, len(path)):
                    py, px = path[i]
                    visited[py, px] = True

                # Откатываем path до развилки
                path = path[:fork_path_idx + 1]

                # Сбрасываем local_visited для откаченных точек
                # (но они уже в global visited, так что не вернёмся)

                # Берём следующее направление
                next_neighbor = remaining.pop(0)

                # Если остались ещё направления, возвращаем развилку в стек
                if remaining:
                    fork_stack.append((fork_point, fork_path_idx, remaining, prev_at_fork, color_at_fork))

                # Восстанавливаем состояние
                current = next_neighbor
                previous = fork_point
                edge_color = color_at_fork
                path.append(current)
                local_visited[current[0], current[1]] = True

                if debug:
                    print(f"  ↩️ ОТКАТ к развилке {fork_point}, пробуем {next_neighbor}")

                return True

        return False

    while True:
        if len(path) > max_path_length:
            if debug:
                print(f"ПРЕДУПРЕЖДЕНИЕ: Путь превысил {max_path_length} пикселей.")
            # Помечаем весь путь как visited глобально
            for py, px in path:
                visited[py, px] = True
            return None

        current_y, current_x = current

        # Проверка моста
        if not just_teleported and bridge_contact_map[current_y, current_x] > 0:
            bridge_id = int(bridge_contact_map[current_y, current_x])

            if bridge_id in bridge_routing:
                routing = bridge_routing[bridge_id]
                current_tuple = (current_y, current_x)

                if current_tuple in routing:
                    pipe_type = routing[current_tuple]

                    if edge_color is None:
                        if pipe_type == 'pipe1':
                            edge_color = 'BLUE'
                        else:
                            edge_color = 'YELLOW'

                    exit_point = None
                    for point, ptype in routing.items():
                        if ptype == pipe_type and point != current_tuple:
                            exit_point = point
                            break

                    if exit_point is None:
                        if debug:
                            print(f"ОШИБКА: Не найдена пара для моста bridge_id={bridge_id}")
                        # Пробуем откат
                        if try_rollback_to_fork():
                            just_teleported = False
                            continue
                        # Нет развилок — помечаем путь и выходим
                        for py, px in path:
                            visited[py, px] = True
                        return None

                    if debug:
                        print(f"  🌉 ТЕЛЕПОРТАЦИЯ: bridge_id={bridge_id}, {pipe_type}")

                    previous = current
                    current = exit_point
                    path.append(exit_point)
                    local_visited[exit_point[0], exit_point[1]] = True
                    just_teleported = True
                    continue

        just_teleported = False

        # Проверка контакта с узлом (через contact_map)
        if contact_map[current_y, current_x] > 0 and current != start_point:
            end_node_id = get_or_create_node(current_y, current_x)

            if end_node_id is None:
                # Попали в excluded класс — это тупик, пробуем откат
                if debug:
                    print(f"  🚫 Точка {current} — excluded класс, пробуем откат")
                if try_rollback_to_fork():
                    continue
                # Нет развилок — помечаем путь и выходим
                for py, px in path:
                    visited[py, px] = True
                return None

            if end_node_id != start_node_id:
                # Нашли валидный узел - ребро завершено успешно!
                # Помечаем весь путь как visited глобально
                for py, px in path:
                    visited[py, px] = True

                end_point = current
                return {
                    'from': start_node_id,
                    'to': end_node_id,
                    'source_point': [start_point[0], start_point[1]],  # [y, x]
                    'target_point': [end_point[0], end_point[1]],      # [y, x]
                    'path': path,
                    'length': len(path),
                    'is_terminal': False,
                    'color': edge_color
                }

        # Поиск следующего пикселя
        neighbors = get_8_neighbors(current_y, current_x, height, width)

        valid_neighbors = []
        for ny, nx in neighbors:
            if not skeleton[ny, nx]:
                continue
            if previous and (ny, nx) == previous:
                continue
            if local_visited[ny, nx]:
                continue
            if visited[ny, nx]:
                continue
            valid_neighbors.append((ny, nx))

        # Тупик — нет валидных соседей
        if len(valid_neighbors) == 0:
            if debug:
                print(f"  ⊥ Тупик в {current}, длина пути {len(path)}")

            # Пробуем откат к развилке
            if try_rollback_to_fork():
                continue

            # Нет развилок — это terminal edge
            # Помечаем путь как visited
            for py, px in path:
                visited[py, px] = True

            end_point = current
            return {
                'from': start_node_id,
                'to': None,
                'source_point': [start_point[0], start_point[1]],
                'target_point': [end_point[0], end_point[1]],
                'path': path,
                'length': len(path),
                'is_terminal': True,
                'color': edge_color
            }

        # Развилка — больше одного направления
        if len(valid_neighbors) > 1:
            if debug:
                print(f"  🔀 Развилка в {current}, направлений: {len(valid_neighbors)}")
            # Сохраняем развилку в стек (без первого направления, по которому пойдём)
            remaining = valid_neighbors[1:]
            fork_stack.append((current, len(path) - 1, remaining, previous, edge_color))

        # Идём по первому направлению
        next_pixel = valid_neighbors[0]
        previous = current
        current = next_pixel
        path.append(current)
        local_visited[current[0], current[1]] = True


# Оставляем старые функции для обратной совместимости
def trace_edges(skeleton: np.ndarray,
                labeled_nodes: np.ndarray,
                nodes: List[Dict],
                contact_map: np.ndarray,
                bridge_contact_map: np.ndarray,
                bridge_routing: Dict,
                max_path_length: int = 10000,
                debug: bool = False) -> List[Dict]:
    """
    Трассировка всех рёбер графа.

    Проходит от каждого узла по скелету и строит рёбра.

    ИЗМЕНЕНИЯ v2:
    - Каждое ребро содержит source_point и target_point

    Args:
        skeleton: Бинарная маска скелета (ОЧИЩЕННАЯ от узлов)
        labeled_nodes: Массив с метками узлов
        nodes: Список узлов
        contact_map: Карта точек контакта (contact_map[y,x] = label_id узла)
        bridge_contact_map: Карта точек контакта мостов
        bridge_routing: Словарь routing для мостов
        max_path_length: Максимальная длина пути
        debug: Выводить отладочную информацию

    Returns:
        Список рёбер с source_point и target_point
    """
    height, width = skeleton.shape

    visited = np.zeros_like(skeleton, dtype=bool)

    edges = []
    edge_counter = 0

    if debug:
        print(f"Начало трассировки рёбер...")

    for node in nodes:
        node_id = node['id']
        label_id = node['label_id']

        contact_coords = np.argwhere(contact_map == label_id)
        contact_points = [(int(y), int(x)) for y, x in contact_coords]

        for start_point in contact_points:
            y, x = start_point

            if visited[y, x]:
                continue

            edge = trace_single_edge(
                start_node_id=node_id,
                start_point=start_point,
                skeleton=skeleton,
                labeled_nodes=labeled_nodes,
                contact_map=contact_map,
                bridge_contact_map=bridge_contact_map,
                bridge_routing=bridge_routing,
                visited=visited,
                max_path_length=max_path_length,
                debug=debug
            )

            if edge:
                edge['id'] = f'edge_{edge_counter}'
                edges.append(edge)
                edge_counter += 1

    if debug:
        print(f"Трассировка завершена. Найдено рёбер: {len(edges)}")

    return edges


def trace_single_edge(start_node_id: str,
                     start_point: Tuple[int, int],
                     skeleton: np.ndarray,
                     labeled_nodes: np.ndarray,
                     contact_map: np.ndarray,
                     bridge_contact_map: np.ndarray,
                     bridge_routing: Dict,
                     visited: np.ndarray,
                     annotations: List[Dict],
                     max_path_length: int = 10000,
                     min_spur_length: int = 5,
                     debug: bool = False) -> Optional[Dict]:
    """
    Трассировка одного ребра от стартовой точки с откатом к развилкам.

    ИЗМЕНЕНИЯ v3:
    - При попадании в тупик (terminal) откатывается к последней развилке
    - Пробует альтернативные направления пока не найдёт узел или не исчерпает варианты
    - Если путь ведёт к запрещённому узлу (EXCLUDED_CLASS_IDS) — считается тупиком
    - Помечает visited только финальный путь (или тупиковые ветки при откате)

    Args:
        start_node_id: ID узла откуда начинаем
        start_point: Координаты точки контакта (y, x)
        skeleton: Бинарная маска скелета
        labeled_nodes: Массив с метками узлов
        contact_map: Карта точек контакта
        bridge_contact_map: Карта точек контакта мостов
        bridge_routing: Словарь routing для навигации через мосты
        visited: Глобальный массив посещённых пикселей
        annotations: Список YOLO меток для проверки запрещённых классов
        max_path_length: Защита от бесконечного цикла
        min_spur_length: Минимальная длина шпоры (для решения об откате)
        debug: Выводить отладочную информацию

    Returns:
        Словарь с данными ребра включая source_point и target_point, или None
    """
    height, width = skeleton.shape

    # Локальный visited для текущей трассировки (можем откатывать)
    local_visited = np.zeros_like(visited, dtype=bool)

    path = [start_point]
    current = start_point
    previous = None
    edge_color = None
    just_teleported = False

    # Стек развилок: [(fork_point, path_index, remaining_neighbors, previous_at_fork, edge_color_at_fork), ...]
    fork_stack = []

    local_visited[current[0], current[1]] = True

    def try_rollback_to_fork():
        """Попытка отката к последней развилке. Возвращает True если откат успешен."""
        nonlocal path, current, previous, edge_color, local_visited, fork_stack

        while fork_stack:
            fork_point, fork_path_idx, remaining, prev_at_fork, color_at_fork = fork_stack.pop()

            if remaining:
                # Есть ещё направления для проверки
                # Помечаем тупиковую ветку как visited глобально
                for i in range(fork_path_idx + 1, len(path)):
                    py, px = path[i]
                    visited[py, px] = True

                # Откатываем path до развилки
                path = path[:fork_path_idx + 1]

                # Берём следующее направление
                next_neighbor = remaining.pop(0)

                # Если остались ещё направления, возвращаем развилку в стек
                if remaining:
                    fork_stack.append((fork_point, fork_path_idx, remaining, prev_at_fork, color_at_fork))

                # Восстанавливаем состояние
                current = next_neighbor
                previous = fork_point
                edge_color = color_at_fork
                path.append(current)
                local_visited[current[0], current[1]] = True

                if debug:
                    print(f"  ↩️ ОТКАТ к развилке {fork_point}, пробуем {next_neighbor}")

                return True

        return False

    def is_excluded_node(point: Tuple[int, int]) -> bool:
        """Проверяет, принадлежит ли точка запрещённому классу."""
        y, x = point
        # Проверяем через YOLO labels
        for label in annotations:
            if label['class_id'] in EXCLUDED_CLASS_IDS:
                # Проверяем попадание точки в bbox
                x1, y1, x2, y2 = label['bbox']
                if x1 <= x <= x2 and y1 <= y <= y2:
                    if debug:
                        print(f"  🚫 Точка {point} в запрещённом классе {label['class_id']}")
                    return True
        return False

    while True:
        if len(path) > max_path_length:
            if debug:
                print(f"ПРЕДУПРЕЖДЕНИЕ: Путь превысил {max_path_length} пикселей.")
            # Помечаем весь путь как visited глобально
            for py, px in path:
                visited[py, px] = True
            return None

        current_y, current_x = current

        # Проверка моста
        if not just_teleported and bridge_contact_map[current_y, current_x] > 0:
            bridge_id = int(bridge_contact_map[current_y, current_x])

            if bridge_id in bridge_routing:
                routing = bridge_routing[bridge_id]
                current_tuple = (current_y, current_x)

                if current_tuple in routing:
                    pipe_type = routing[current_tuple]

                    if edge_color is None:
                        if pipe_type == 'pipe1':
                            edge_color = 'BLUE'
                        else:
                            edge_color = 'YELLOW'

                    exit_point = None
                    for point, ptype in routing.items():
                        if ptype == pipe_type and point != current_tuple:
                            exit_point = point
                            break

                    if exit_point is None:
                        if debug:
                            print(f"ОШИБКА: Не найдена пара для моста bridge_id={bridge_id}")
                        # Пробуем откат
                        if try_rollback_to_fork():
                            just_teleported = False
                            continue
                        # Нет развилок — помечаем путь и выходим
                        for py, px in path:
                            visited[py, px] = True
                        return None

                    if debug:
                        print(f"  🌉 ТЕЛЕПОРТАЦИЯ: bridge_id={bridge_id}, {pipe_type}")

                    previous = current
                    current = exit_point
                    path.append(exit_point)
                    local_visited[exit_point[0], exit_point[1]] = True
                    just_teleported = True
                    continue

        just_teleported = False

        # Проверка контакта с узлом
        if contact_map[current_y, current_x] > 0:
            end_label = contact_map[current_y, current_x]
            end_node_id = f'node_{end_label - 1}'

            if end_node_id != start_node_id:
                # Проверяем, не запрещённый ли это узел
                if is_excluded_node(current):
                    if debug:
                        print(f"  🚫 Узел {end_node_id} запрещён, пробуем откат")
                    # Считаем как тупик — пробуем откат
                    if try_rollback_to_fork():
                        continue
                    # Нет развилок — возвращаем None
                    for py, px in path:
                        visited[py, px] = True
                    return None

                # Нашли валидный узел - ребро завершено успешно!
                # Помечаем весь путь как visited глобально
                for py, px in path:
                    visited[py, px] = True

                end_point = current
                return {
                    'from': start_node_id,
                    'to': end_node_id,
                    'source_point': [start_point[0], start_point[1]],  # [y, x]
                    'target_point': [end_point[0], end_point[1]],      # [y, x]
                    'path': path,
                    'length': len(path),
                    'is_terminal': False,
                    'color': edge_color
                }

        # Поиск следующего пикселя
        neighbors = get_8_neighbors(current_y, current_x, height, width)

        valid_neighbors = []
        for ny, nx in neighbors:
            if not skeleton[ny, nx]:
                continue
            if previous and (ny, nx) == previous:
                continue
            if local_visited[ny, nx]:
                continue
            if visited[ny, nx]:
                continue
            valid_neighbors.append((ny, nx))

        # Тупик — нет валидных соседей
        if len(valid_neighbors) == 0:
            if debug:
                print(f"  ⊥ Тупик в {current}, длина пути {len(path)}")

            # Пробуем откат к развилке
            if try_rollback_to_fork():
                continue

            # Нет развилок — это terminal edge
            # Помечаем путь как visited
            for py, px in path:
                visited[py, px] = True

            end_point = current
            return {
                'from': start_node_id,
                'to': None,
                'source_point': [start_point[0], start_point[1]],
                'target_point': [end_point[0], end_point[1]],
                'path': path,
                'length': len(path),
                'is_terminal': True,
                'color': edge_color
            }

        # Развилка — больше одного направления
        if len(valid_neighbors) > 1:
            if debug:
                print(f"  🔀 Развилка в {current}, направлений: {len(valid_neighbors)}")
            # Сохраняем развилку в стек (без первого направления, по которому пойдём)
            remaining = valid_neighbors[1:]
            fork_stack.append((current, len(path) - 1, remaining, previous, edge_color))

        # Идём по первому направлению
        next_pixel = valid_neighbors[0]
        previous = current
        current = next_pixel
        path.append(current)
        local_visited[current[0], current[1]] = True


def compute_edge_statistics(edge: Dict) -> Dict:
    """
    Вычислить дополнительные статистики для ребра.

    Args:
        edge: Словарь с данными ребра

    Returns:
        Обновлённый словарь с добавленными полями
    """
    path = edge['path']

    if len(path) >= 2:
        start = path[0]
        end = path[-1]
        from .utils import euclidean_distance
        straight_distance = euclidean_distance(start, end)
        edge['straight_line_distance'] = float(straight_distance)
    else:
        edge['straight_line_distance'] = 0.0

    return edge
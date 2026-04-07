"""
utils.py

Вспомогательные функции для модуля классификации junction'ов.
"""

import os
import json
import yaml
import random
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Any
from collections import defaultdict

import numpy as np
from PIL import Image
from scipy import ndimage


# =============================================================================
# FILE UTILITIES
# =============================================================================

def load_config(config_path: Optional[str] = None) -> Dict:
    """
    Загрузка конфигурации из YAML файла.
    
    Args:
        config_path: Путь к конфигу. Если None, загружает default.yaml
        
    Returns:
        Словарь с конфигурацией
    """
    if config_path is None:
        # Загрузить default config
        module_dir = Path(__file__).parent.parent
        config_path = module_dir / 'config' / 'default.yaml'
    
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    return config


def save_config(config: Dict, output_path: str):
    """Сохранение конфигурации в YAML файл."""
    with open(output_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)


def save_json(data: Any, output_path: str, indent: int = 2):
    """Сохранение данных в JSON файл."""
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)


def load_json(json_path: str) -> Any:
    """Загрузка данных из JSON файла."""
    with open(json_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def ensure_dir(path: str) -> Path:
    """Создание директории если не существует."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def find_images(directory: str, extensions: List[str] = None) -> List[Path]:
    """
    Поиск изображений в директории.
    
    Args:
        directory: Путь к директории
        extensions: Список расширений (по умолчанию ['.png', '.jpg', '.jpeg'])
        
    Returns:
        Список путей к изображениям (отсортированный)
    """
    if extensions is None:
        extensions = ['.png', '.jpg', '.jpeg']
    
    directory = Path(directory)
    images = []
    
    for ext in extensions:
        images.extend(directory.glob(f'*{ext}'))
    
    return sorted(images)


# =============================================================================
# SKELETON ANALYSIS
# =============================================================================

def compute_degree_map(skeleton: np.ndarray) -> np.ndarray:
    """
    Вычисление степени (количества соседей) для каждого пикселя скелета.
    
    Args:
        skeleton: Бинарная маска скелета (0 или 255)
        
    Returns:
        Карта степеней (degree) для каждого пикселя
    """
    # Нормализация в 0/1
    skel_binary = (skeleton > 127).astype(np.uint8)
    
    # Ядро для подсчёта соседей (8-связность)
    kernel = np.array([[1, 1, 1],
                       [1, 0, 1],
                       [1, 1, 1]], dtype=np.uint8)
    
    # Свёртка для подсчёта соседей
    neighbor_count = ndimage.convolve(skel_binary, kernel, mode='constant', cval=0)
    
    # Обнуляем не-скелетные пиксели
    neighbor_count = neighbor_count * skel_binary
    
    return neighbor_count


def find_junctions(skeleton: np.ndarray, min_degree: int = 3,
                   cluster_radius: int = 10) -> List[Dict]:
    """
    Поиск junction'ов (точек пересечения) на скелете.
    
    Junction - точка со степенью ≥ min_degree (обычно 3).
    
    Args:
        skeleton: Бинарная маска скелета
        min_degree: Минимальная степень для junction
        cluster_radius: Радиус кластеризации близких точек
        
    Returns:
        Список junction'ов: [{'pos': (x, y), 'degree': int, 'type': 'junction'}, ...]
    """
    degree_map = compute_degree_map(skeleton)
    
    # Находим все точки с degree >= min_degree
    junction_points = np.argwhere(degree_map >= min_degree)
    
    if len(junction_points) == 0:
        return []
    
    # Формируем список с метаданными
    junctions = []
    for y, x in junction_points:
        junctions.append({
            'pos': (int(x), int(y)),
            'degree': int(degree_map[y, x]),
            'type': 'junction'
        })
    
    # Кластеризация близких точек
    clustered = cluster_points(junctions, cluster_radius, key='degree', mode='max')
    
    return clustered


def find_turns(skeleton: np.ndarray, junctions: List[Dict],
               angle_threshold: float = 170,
               cluster_radius: int = 15,
               exclusion_radius: int = 20) -> List[Dict]:
    """
    Поиск поворотов (изгибов) на скелете.
    
    Поворот - точка со степенью 2 и углом меньше angle_threshold.
    
    Args:
        skeleton: Бинарная маска скелета
        junctions: Список уже найденных junction'ов (для исключения зон)
        angle_threshold: Максимальный угол для поворота (градусы)
        cluster_radius: Радиус кластеризации
        exclusion_radius: Радиус исключения вокруг junction'ов
        
    Returns:
        Список поворотов: [{'pos': (x, y), 'degree': 2, 'type': 'bend', 'angle': float}, ...]
    """
    skel_binary = (skeleton > 127).astype(np.uint8)
    degree_map = compute_degree_map(skeleton)
    
    # Точки со степенью 2
    degree2_points = np.argwhere(degree_map == 2)
    
    if len(degree2_points) == 0:
        return []
    
    # Создаём маску исключения вокруг junction'ов
    exclusion_mask = np.zeros_like(skeleton, dtype=bool)
    for j in junctions:
        x, y = j['pos']
        y_min = max(0, y - exclusion_radius)
        y_max = min(skeleton.shape[0], y + exclusion_radius + 1)
        x_min = max(0, x - exclusion_radius)
        x_max = min(skeleton.shape[1], x + exclusion_radius + 1)
        exclusion_mask[y_min:y_max, x_min:x_max] = True
    
    turns = []
    for y, x in degree2_points:
        # Пропускаем точки в зоне исключения
        if exclusion_mask[y, x]:
            continue
        
        # Вычисляем угол в этой точке
        angle = compute_angle_at_point(skel_binary, x, y)
        
        if angle is not None and angle < angle_threshold:
            turns.append({
                'pos': (int(x), int(y)),
                'degree': 2,
                'type': 'bend',
                'angle': float(angle)
            })
    
    # Кластеризация
    clustered = cluster_points(turns, cluster_radius, key='angle', mode='min')
    
    return clustered


def compute_angle_at_point(skeleton: np.ndarray, x: int, y: int,
                           trace_length: int = 7) -> Optional[float]:
    """
    Вычисление угла в точке скелета.
    
    Трассируем скелет в обе стороны на trace_length пикселей
    и вычисляем угол между направлениями.
    
    Args:
        skeleton: Бинарная маска скелета (0/1)
        x, y: Координаты точки
        trace_length: Длина трассировки в каждую сторону
        
    Returns:
        Угол в градусах или None если не удалось вычислить
    """
    # Находим соседей
    neighbors = []
    for dy in [-1, 0, 1]:
        for dx in [-1, 0, 1]:
            if dx == 0 and dy == 0:
                continue
            ny, nx = y + dy, x + dx
            if 0 <= ny < skeleton.shape[0] and 0 <= nx < skeleton.shape[1]:
                if skeleton[ny, nx] > 0:
                    neighbors.append((nx, ny))
    
    if len(neighbors) != 2:
        return None
    
    # Трассируем в обе стороны
    point_a = trace_skeleton(skeleton, (x, y), neighbors[0], trace_length)
    point_b = trace_skeleton(skeleton, (x, y), neighbors[1], trace_length)
    
    if point_a is None or point_b is None:
        return None
    
    # Вычисляем угол
    vec_a = np.array([point_a[0] - x, point_a[1] - y], dtype=float)
    vec_b = np.array([point_b[0] - x, point_b[1] - y], dtype=float)
    
    len_a = np.linalg.norm(vec_a)
    len_b = np.linalg.norm(vec_b)
    
    if len_a < 1e-6 or len_b < 1e-6:
        return None
    
    cos_angle = np.dot(vec_a, vec_b) / (len_a * len_b)
    cos_angle = np.clip(cos_angle, -1, 1)
    angle = np.degrees(np.arccos(cos_angle))
    
    return angle


def trace_skeleton(skeleton: np.ndarray, start: Tuple[int, int],
                   direction: Tuple[int, int], length: int) -> Optional[Tuple[int, int]]:
    """
    Трассировка скелета в заданном направлении.
    
    Args:
        skeleton: Бинарная маска скелета
        start: Начальная точка (x, y)
        direction: Первый шаг (x, y)
        length: Количество шагов
        
    Returns:
        Конечная точка (x, y) или None
    """
    x, y = direction
    prev_x, prev_y = start
    
    for _ in range(length):
        # Ищем следующего соседа (не предыдущую точку)
        found = False
        for dy in [-1, 0, 1]:
            for dx in [-1, 0, 1]:
                if dx == 0 and dy == 0:
                    continue
                nx, ny = x + dx, y + dy
                if nx == prev_x and ny == prev_y:
                    continue
                if 0 <= ny < skeleton.shape[0] and 0 <= nx < skeleton.shape[1]:
                    if skeleton[ny, nx] > 0:
                        prev_x, prev_y = x, y
                        x, y = nx, ny
                        found = True
                        break
            if found:
                break
        
        if not found:
            break
    
    return (x, y)


def cluster_points(points: List[Dict], radius: float,
                   key: str = 'degree', mode: str = 'max') -> List[Dict]:
    """
    Кластеризация близких точек.
    
    Args:
        points: Список точек с 'pos' ключом
        radius: Радиус кластеризации
        key: Ключ для выбора лучшей точки в кластере
        mode: 'max' или 'min' - как выбирать лучшую точку
        
    Returns:
        Отфильтрованный список точек
    """
    if len(points) == 0:
        return []
    
    clustered = []
    used = set()
    
    for i, p in enumerate(points):
        if i in used:
            continue
        
        x, y = p['pos']
        cluster = [p]
        
        # Находим все близкие точки
        for j, other in enumerate(points):
            if j == i or j in used:
                continue
            ox, oy = other['pos']
            dist = np.sqrt((x - ox) ** 2 + (y - oy) ** 2)
            if dist < radius:
                cluster.append(other)
                used.add(j)
        
        # Выбираем лучшую точку в кластере
        if mode == 'max':
            best = max(cluster, key=lambda p: p.get(key, 0))
        else:
            best = min(cluster, key=lambda p: p.get(key, float('inf')))
        
        clustered.append(best)
        used.add(i)
    
    return clustered


def filter_by_distance(points: List[Dict], min_distance: int) -> List[Dict]:
    """
    Фильтрация точек по минимальному расстоянию между ними.
    
    Args:
        points: Список точек
        min_distance: Минимальное расстояние
        
    Returns:
        Отфильтрованный список
    """
    if len(points) == 0:
        return []
    
    filtered = []
    
    for p in points:
        x, y = p['pos']
        too_close = False
        
        for existing in filtered:
            ex, ey = existing['pos']
            dist = np.sqrt((x - ex) ** 2 + (y - ey) ** 2)
            if dist < min_distance:
                too_close = True
                break
        
        if not too_close:
            filtered.append(p)
    
    return filtered


# =============================================================================
# CROP EXTRACTION
# =============================================================================

def extract_crop(image: np.ndarray, center: Tuple[int, int],
                 crop_size: int, pad_value: int = 0) -> np.ndarray:
    """
    Извлечение crop'а из изображения с центром в заданной точке.
    
    Args:
        image: Исходное изображение (H, W) или (H, W, C)
        center: Центр crop'а (x, y)
        crop_size: Размер crop'а
        pad_value: Значение для заполнения при выходе за границы
        
    Returns:
        Crop размера (crop_size, crop_size) или (crop_size, crop_size, C)
    """
    x, y = center
    half = crop_size // 2
    
    h, w = image.shape[:2]
    
    # Вычисляем границы
    y1, y2 = y - half, y + half
    x1, x2 = x - half, x + half
    
    # Границы в исходном изображении (с учётом краёв)
    src_y1 = max(0, y1)
    src_y2 = min(h, y2)
    src_x1 = max(0, x1)
    src_x2 = min(w, x2)
    
    # Границы в crop'е
    dst_y1 = src_y1 - y1
    dst_y2 = dst_y1 + (src_y2 - src_y1)
    dst_x1 = src_x1 - x1
    dst_x2 = dst_x1 + (src_x2 - src_x1)
    
    # Создаём crop
    if image.ndim == 3:
        crop = np.full((crop_size, crop_size, image.shape[2]), pad_value, dtype=image.dtype)
    else:
        crop = np.full((crop_size, crop_size), pad_value, dtype=image.dtype)
    
    # Копируем данные
    crop[dst_y1:dst_y2, dst_x1:dst_x2] = image[src_y1:src_y2, src_x1:src_x2]
    
    return crop


# =============================================================================
# DATA SPLITTING
# =============================================================================

def split_by_schema(labels_data: List[Dict], train_ratio: float = 0.8,
                    seed: int = 42) -> Tuple[List[Dict], List[Dict]]:
    """
    Разбиение данных на train/val по схемам (не по отдельным crops).
    
    Все crops одной схемы попадают в один split.
    
    Args:
        labels_data: Список данных с 'schema' ключом
        train_ratio: Доля train данных
        seed: Random seed
        
    Returns:
        (train_data, val_data)
    """
    # Группируем по схемам
    schema_to_items = defaultdict(list)
    for item in labels_data:
        schema = item.get('schema', 'unknown')
        schema_to_items[schema].append(item)
    
    schemas = list(schema_to_items.keys())
    
    # Shuffle схем
    random.seed(seed)
    random.shuffle(schemas)
    
    # Разбиваем схемы
    n_train = int(len(schemas) * train_ratio)
    train_schemas = set(schemas[:n_train])
    val_schemas = set(schemas[n_train:])
    
    # Собираем данные
    train_data = []
    val_data = []
    
    for schema, items in schema_to_items.items():
        if schema in train_schemas:
            train_data.extend(items)
        else:
            val_data.extend(items)
    
    return train_data, val_data


def compute_class_weights(labels: List[str], num_classes: int = 3) -> List[float]:
    """
    Вычисление весов классов (inverse frequency).
    
    Args:
        labels: Список меток классов
        num_classes: Количество классов
        
    Returns:
        Список весов для каждого класса
    """
    # Подсчёт частот
    counts = defaultdict(int)
    for label in labels:
        counts[label] += 1
    
    total = len(labels)
    
    # Inverse frequency
    class_names = ['connection', 'bridge', 'turn']
    weights = []
    
    for name in class_names[:num_classes]:
        count = counts.get(name, 1)  # Избегаем деления на 0
        weight = total / (num_classes * count)
        weights.append(weight)
    
    # Нормализация
    max_weight = max(weights)
    weights = [w / max_weight for w in weights]
    
    return weights


# =============================================================================
# VISUALIZATION
# =============================================================================

def create_visualization(image: np.ndarray, skeleton: np.ndarray,
                         points: List[Dict], colors: Dict[str, Tuple[int, int, int]],
                         darken_factor: float = 0.3,
                         skeleton_alpha: float = 0.7,
                         square_size: int = 15) -> np.ndarray:
    """
    Создание визуализации с точками на схеме.
    
    Args:
        image: Оригинальное изображение (RGB)
        skeleton: Маска скелета
        points: Список точек с 'pos' и 'class' ключами
        colors: Цвета для классов {'connection': (R,G,B), ...}
        darken_factor: Коэффициент затемнения (0-1)
        skeleton_alpha: Прозрачность скелета
        square_size: Размер квадратов
        
    Returns:
        Визуализация (RGB)
    """
    import cv2
    
    # Затемнение
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    
    darkened = (image.astype(float) * darken_factor).astype(np.uint8)
    
    # Наложение скелета (зелёным)
    if skeleton is not None:
        skel_mask = skeleton > 127
        skeleton_overlay = np.zeros_like(darkened)
        skeleton_overlay[skel_mask] = [0, 255, 0]
        darkened = cv2.addWeighted(darkened, 1.0, skeleton_overlay, skeleton_alpha, 0)
    
    # Рисуем квадраты на точках
    half = square_size // 2
    
    for p in points:
        x, y = int(p['pos'][0]), int(p['pos'][1])
        cls = p.get('class', 'connection')
        color = colors.get(cls, (255, 255, 255))
        
        # BGR для OpenCV
        color_bgr = (color[2], color[1], color[0])
        
        cv2.rectangle(darkened,
                      (x - half, y - half),
                      (x + half, y + half),
                      color_bgr, -1)
        
        # Обводка
        cv2.rectangle(darkened,
                      (x - half, y - half),
                      (x + half, y + half),
                      (0, 0, 0), 1)
    
    return darkened


# =============================================================================
# MASK CREATION
# =============================================================================

def create_binary_mask(image_shape: Tuple[int, int],
                       points: List[Tuple[int, int]],
                       point_size: int = 5) -> np.ndarray:
    """
    Создание бинарной маски с точками.
    
    Args:
        image_shape: (height, width)
        points: Список координат [(x, y), ...]
        point_size: Размер точки
        
    Returns:
        Бинарная маска (uint8, 0 или 255)
    """
    mask = np.zeros(image_shape, dtype=np.uint8)
    half = point_size // 2
    
    for x, y in points:
        y1 = max(0, y - half)
        y2 = min(image_shape[0], y + half + 1)
        x1 = max(0, x - half)
        x2 = min(image_shape[1], x + half + 1)
        mask[y1:y2, x1:x2] = 255
    
    return mask

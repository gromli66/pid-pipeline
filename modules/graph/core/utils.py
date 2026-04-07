"""
Вспомогательные функции для работы с графом P&ID схем.
"""
import numpy as np
from typing import List, Tuple


def get_8_neighbors(y: int, x: int, height: int, width: int) -> List[Tuple[int, int]]:
    """
    Получить 8 соседей пикселя с проверкой границ.
    
    Args:
        y: Координата y (строка)
        x: Координата x (столбец)
        height: Высота изображения
        width: Ширина изображения
    
    Returns:
        Список координат соседей в формате (y, x)
    """
    neighbors = []
    for dy in [-1, 0, 1]:
        for dx in [-1, 0, 1]:
            if dy == 0 and dx == 0:
                continue
            ny, nx = y + dy, x + dx
            if 0 <= ny < height and 0 <= nx < width:
                neighbors.append((ny, nx))
    return neighbors


def euclidean_distance(point1: Tuple[int, int], point2: Tuple[int, int]) -> float:
    """
    Вычислить евклидово расстояние между двумя точками.
    
    Args:
        point1: Первая точка (y, x)
        point2: Вторая точка (y, x)
    
    Returns:
        Расстояние в пикселях
    """
    y1, x1 = point1
    y2, x2 = point2
    return np.sqrt((y2 - y1)**2 + (x2 - x1)**2)


def normalize_vector(vector: np.ndarray) -> np.ndarray:
    """
    Нормализовать вектор.
    
    Args:
        vector: Вектор для нормализации
    
    Returns:
        Нормализованный вектор (единичной длины)
    """
    norm = np.linalg.norm(vector)
    if norm == 0:
        return vector
    return vector / norm


def angle_between_vectors(v1: np.ndarray, v2: np.ndarray) -> float:
    """
    Вычислить угол между двумя векторами в радианах.
    
    Args:
        v1: Первый вектор
        v2: Второй вектор
    
    Returns:
        Угол в радианах [0, π]
    """
    v1_norm = normalize_vector(v1)
    v2_norm = normalize_vector(v2)
    
    # Clip для избежания численных ошибок
    cos_angle = np.clip(np.dot(v1_norm, v2_norm), -1.0, 1.0)
    return np.arccos(cos_angle)


def load_binary_mask(filepath: str) -> np.ndarray:
    """
    Загрузить бинарную маску из файла.
    
    Args:
        filepath: Путь к файлу PNG
    
    Returns:
        Бинарный numpy array (True/False)
    """
    from PIL import Image
    
    img = Image.open(filepath).convert('L')
    arr = np.array(img)
    
    # Бинаризация: > 127 = True
    binary = arr > 127
    
    return binary


def save_binary_mask(mask: np.ndarray, filepath: str):
    """
    Сохранить бинарную маску в файл.
    
    Args:
        mask: Бинарный numpy array
        filepath: Путь для сохранения
    """
    from PIL import Image
    
    # Конвертация bool -> uint8
    arr = (mask * 255).astype(np.uint8)
    img = Image.fromarray(arr)
    img.save(filepath)
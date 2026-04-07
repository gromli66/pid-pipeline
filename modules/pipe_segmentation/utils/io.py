"""
Утилиты для работы с файлами: загрузка/сохранение изображений, JSON, CSV.
Включает функции бинаризации для P&ID изображений.

[FIX 1.1] Добавлена validate_binarization()
[FIX 1.2] Смягчённые параметры по умолчанию (block_size=31, c=7)
[FIX 1.3] Удалена дублирующая binarize_image(), единая функция binarize_to_black_white()
"""

import cv2
import json
import csv
import numpy as np
import warnings
from pathlib import Path
from typing import List, Dict, Optional, Any, Union

from pipe_segmentation.config.defaults import (
    IMAGE_EXTENSIONS,
    BINARIZE_METHOD,
    BINARIZE_BLOCK_SIZE,
    BINARIZE_C,
    BINARIZE_FIXED_THRESHOLD,
    BINARIZE_INVERT,
    BINARIZE_MIN_BLACK_RATIO,
)


# =============================================================================
# БИНАРИЗАЦИЯ
# =============================================================================

def binarize_to_black_white(
    image: np.ndarray,
    method: str = BINARIZE_METHOD,
    block_size: int = BINARIZE_BLOCK_SIZE,
    c: int = BINARIZE_C,
    threshold: int = BINARIZE_FIXED_THRESHOLD,
    invert: bool = False
) -> np.ndarray:
    """
    Бинаризует изображение P&ID.

    [FIX 1.3] Единая функция вместо двух дублирующих.

    Args:
        image: Входное изображение (grayscale или RGB)
        method: Метод бинаризации ('adaptive', 'otsu', 'fixed')
        block_size: Размер блока для adaptive (нечётное)
        c: Константа для adaptive threshold
        threshold: Порог для fixed метода
        invert: True = трубы белые (255), False = трубы чёрные (0)

    Returns:
        Бинаризованное изображение (0 или 255)
    """
    # Конвертируем в grayscale если нужно
    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    else:
        gray = image.copy()

    # Выбираем метод бинаризации
    if method == 'adaptive':
        binary = cv2.adaptiveThreshold(
            gray, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            block_size, c
        )
    elif method == 'otsu':
        _, binary = cv2.threshold(
            gray, 0, 255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
    else:  # 'fixed'
        _, binary = cv2.threshold(
            gray, threshold, 255,
            cv2.THRESH_BINARY
        )

    # [FIX 1.2] Валидация результата
    validate_binarization(binary, source_info=method)

    if invert:
        binary = cv2.bitwise_not(binary)

    return binary


def validate_binarization(
    binary: np.ndarray,
    min_black_ratio: float = BINARIZE_MIN_BLACK_RATIO,
    source_info: str = ""
) -> bool:
    """
    [FIX 1.2] Проверяет что бинаризация не стёрла всё содержимое.

    Args:
        binary: Бинаризованное изображение
        min_black_ratio: Минимальная доля чёрных пикселей
        source_info: Информация для warning

    Returns:
        True если валидация пройдена
    """
    black_ratio = np.mean(binary == 0)

    if black_ratio < min_black_ratio:
        warnings.warn(
            f"Binarization ({source_info}): only {black_ratio*100:.2f}% black pixels "
            f"(threshold: {min_black_ratio*100:.1f}%). "
            f"Image may be over-binarized — check block_size and c parameters."
        )
        return False

    if black_ratio > 0.5:
        warnings.warn(
            f"Binarization ({source_info}): {black_ratio*100:.1f}% black pixels — "
            f"image may be inverted or corrupted."
        )
        return False

    return True


def ensure_binary(image: np.ndarray) -> np.ndarray:
    """
    Проверяет и конвертирует изображение в строго бинарное (0 и 255).
    """
    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    else:
        gray = image.copy()

    unique_vals = np.unique(gray)

    if len(unique_vals) <= 2:
        if len(unique_vals) == 2:
            min_val, max_val = unique_vals[0], unique_vals[-1]
            gray = np.where(gray == min_val, 0, 255).astype(np.uint8)
        return gray

    _, binary = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
    return binary


# =============================================================================
# ДИРЕКТОРИИ
# =============================================================================

def ensure_dir(path: Union[str, Path]) -> Path:
    """Создаёт директорию если не существует."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_image_files(directory: Union[str, Path], extensions: List[str] = None) -> List[Path]:
    """Получает список всех изображений в директории."""
    directory = Path(directory)
    if not directory.exists():
        return []

    if extensions is None:
        extensions = IMAGE_EXTENSIONS

    image_files = set()
    for ext in extensions:
        if not ext.startswith('.'):
            ext = '.' + ext
        image_files.update(directory.glob(f'*{ext}'))

    return sorted(list(image_files))


# =============================================================================
# ЗАГРУЗКА/СОХРАНЕНИЕ ИЗОБРАЖЕНИЙ
# =============================================================================

def load_image(
    path: Union[str, Path],
    grayscale: bool = False,
    rgb: bool = True,
    binarize: bool = False,
    binarize_method: str = BINARIZE_METHOD
) -> Optional[np.ndarray]:
    """Загружает изображение из файла."""
    try:
        path = Path(path)
        if not path.exists():
            return None

        if grayscale:
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        else:
            img = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if img is not None and rgb:
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        if binarize and img is not None:
            img = binarize_to_black_white(img, method=binarize_method)
            if not grayscale and len(img.shape) == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)

        return img

    except Exception as e:
        print(f"Error loading image {path}: {e}")
        return None


def save_image(
    image: np.ndarray,
    path: Union[str, Path],
    is_rgb: bool = False
) -> bool:
    """Сохраняет изображение в файл."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if is_rgb and len(image.shape) == 3 and image.shape[2] == 3:
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

        cv2.imwrite(str(path), image)
        return True

    except Exception as e:
        print(f"Error saving image {path}: {e}")
        return False


# =============================================================================
# JSON/CSV
# =============================================================================

def load_json(path: Union[str, Path]) -> Optional[Dict]:
    """Загружает JSON файл."""
    try:
        path = Path(path)
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading JSON {path}: {e}")
        return None


def save_json(data: Dict, path: Union[str, Path], indent: int = 2) -> bool:
    """Сохраняет словарь в JSON файл."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving JSON {path}: {e}")
        return False


def save_csv(
    data: List[Dict[str, Any]],
    path: Union[str, Path],
    fieldnames: List[str] = None
) -> bool:
    """Сохраняет список словарей в CSV файл."""
    try:
        if not data:
            return False
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if fieldnames is None:
            fieldnames = list(data[0].keys())
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)
        return True
    except Exception as e:
        print(f"Error saving CSV {path}: {e}")
        return False


def load_csv(path: Union[str, Path]) -> Optional[List[Dict[str, str]]]:
    """Загружает CSV файл."""
    try:
        path = Path(path)
        with open(path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            return list(reader)
    except Exception as e:
        print(f"Error loading CSV {path}: {e}")
        return None


# =============================================================================
# СПИСКИ ФАЙЛОВ
# =============================================================================

def load_file_list(path: Union[str, Path]) -> List[str]:
    """Загружает список имён файлов из текстового файла."""
    try:
        path = Path(path)
        with open(path, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except Exception as e:
        print(f"Error loading file list {path}: {e}")
        return []


def save_file_list(filenames: List[str], path: Union[str, Path]) -> bool:
    """Сохраняет список имён файлов в текстовый файл."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            for filename in filenames:
                f.write(f"{filename}\n")
        return True
    except Exception as e:
        print(f"Error saving file list {path}: {e}")
        return False


def find_matching_file(
    filename: str,
    search_dir: Union[str, Path],
    extensions: List[str] = None
) -> Optional[Path]:
    """Ищет файл с таким же именем (без расширения) в директории."""
    if extensions is None:
        extensions = ['.png', '.PNG', '.jpg', '.JPG', '.jpeg', '.JPEG']

    stem = Path(filename).stem
    search_dir = Path(search_dir)

    if not search_dir.exists():
        return None

    for ext in extensions:
        if not ext.startswith('.'):
            ext = '.' + ext
        candidate = search_dir / f"{stem}{ext}"
        if candidate.exists():
            return candidate

    return None

"""
Предобработка изображений для инференса.

Включает:
- Бинаризацию (трубы чёрные, фон белый)
- Нормализацию
- Подготовку 4-канального входа
"""

import cv2
import torch
import numpy as np
from typing import List, Optional

from pipe_segmentation.config.defaults import (
    IMAGENET_MEAN, 
    IMAGENET_STD,
    NODE_CHANNEL_MEAN,
    NODE_CHANNEL_STD,
    BINARIZE_METHOD,
)
from pipe_segmentation.utils.io import binarize_to_black_white


def preprocess_image(
    image: np.ndarray,
    binarize: bool = True,
    binarize_method: str = BINARIZE_METHOD
) -> np.ndarray:
    """
    Предобрабатывает изображение для инференса.
    
    Выполняет бинаризацию для приведения к формату тренировочных данных:
    трубы чёрные, фон белый.
    
    Args:
        image: RGB изображение [H, W, 3] (uint8 0-255)
        binarize: Применить бинаризацию
        binarize_method: Метод бинаризации ('adaptive', 'otsu', 'fixed')
        
    Returns:
        Предобработанное RGB изображение [H, W, 3] (uint8 0-255)
    """
    if not binarize:
        return image
    
    # Бинаризуем (возвращает grayscale)
    binary = binarize_to_black_white(image, method=binarize_method)
    
    # Конвертируем обратно в RGB
    if len(binary.shape) == 2:
        binary = cv2.cvtColor(binary, cv2.COLOR_GRAY2RGB)
    
    return binary


def normalize_tile(
    tile: np.ndarray,
    mean: List[float] = None,
    std: List[float] = None
) -> np.ndarray:
    """
    Нормализует RGB тайл по ImageNet статистикам.
    
    Args:
        tile: RGB тайл [H, W, 3] (uint8 0-255)
        mean: Средние значения по каналам
        std: Стандартные отклонения по каналам
        
    Returns:
        Нормализованный тайл [H, W, 3] (float32)
    """
    if mean is None:
        mean = IMAGENET_MEAN
    if std is None:
        std = IMAGENET_STD
    
    # [0, 255] -> [0, 1]
    tile = tile.astype(np.float32) / 255.0
    
    # Нормализация
    mean = np.array(mean, dtype=np.float32).reshape(1, 1, 3)
    std = np.array(std, dtype=np.float32).reshape(1, 1, 3)
    
    tile = (tile - mean) / std
    
    return tile


def prepare_tile_batch(
    rgb_tile: np.ndarray,
    node_tile: np.ndarray,
    mean: List[float] = None,
    std: List[float] = None,
    binarize: bool = False,
    binarize_method: str = BINARIZE_METHOD
) -> torch.Tensor:
    """
    Подготавливает 4-канальный тензор для модели.
    
    Args:
        rgb_tile: RGB тайл [H, W, 3] (uint8 0-255)
        node_tile: Маска узлов [H, W] (uint8 0-255)
        mean: Средние для RGB
        std: Стандартные отклонения для RGB
        binarize: Применить бинаризацию к RGB тайлу
        binarize_method: Метод бинаризации
        
    Returns:
        Тензор [1, 4, H, W]
    """
    # Бинаризация если нужно (обычно уже сделана на уровне полного изображения)
    if binarize:
        rgb_tile = preprocess_image(rgb_tile, binarize=True, binarize_method=binarize_method)
    
    # Нормализуем RGB
    rgb_normalized = normalize_tile(rgb_tile, mean, std)
    
    # [FIX 7.1] Нормализуем node mask так же как в training (dataset.py)
    node_normalized = node_tile.astype(np.float32) / 255.0
    node_normalized = (node_normalized - NODE_CHANNEL_MEAN) / NODE_CHANNEL_STD
    
    # Stack: [H, W, 3] + [H, W] -> [H, W, 4]
    tile_4ch = np.concatenate([
        rgb_normalized,
        node_normalized[:, :, np.newaxis]
    ], axis=2)
    
    # To tensor: [H, W, 4] -> [4, H, W] -> [1, 4, H, W]
    tile_tensor = torch.from_numpy(tile_4ch).permute(2, 0, 1).unsqueeze(0)
    
    return tile_tensor


def prepare_tile_batch_rgb(
    rgb_tile: np.ndarray,
    mean: List[float] = None,
    std: List[float] = None,
    binarize: bool = False,
    binarize_method: str = BINARIZE_METHOD
) -> torch.Tensor:
    """
    Подготавливает 3-канальный тензор (RGB only).
    
    Для моделей с in_channels=3, обученных без node_mask.
    
    Returns:
        Тензор [1, 3, H, W]
    """
    if binarize:
        rgb_tile = preprocess_image(rgb_tile, binarize=True, binarize_method=binarize_method)
    
    rgb_normalized = normalize_tile(rgb_tile, mean, std)
    tile_tensor = torch.from_numpy(rgb_normalized).permute(2, 0, 1).unsqueeze(0)
    return tile_tensor


def prepare_batch_from_tiles_rgb(
    rgb_tiles: List[np.ndarray],
    mean: List[float] = None,
    std: List[float] = None,
    binarize: bool = False,
    binarize_method: str = BINARIZE_METHOD
) -> torch.Tensor:
    """3-канальная версия prepare_batch_from_tiles."""
    batch = []
    for rgb in rgb_tiles:
        batch.append(prepare_tile_batch_rgb(
            rgb, mean, std, binarize=binarize, binarize_method=binarize_method
        ))
    return torch.cat(batch, dim=0)


def prepare_batch_from_tiles(
    rgb_tiles: List[np.ndarray],
    node_tiles: List[np.ndarray],
    mean: List[float] = None,
    std: List[float] = None,
    binarize: bool = False,
    binarize_method: str = BINARIZE_METHOD
) -> torch.Tensor:
    """
    Подготавливает батч из списка тайлов.
    
    Примечание: бинаризация по умолчанию False, т.к. предполагается
    что она уже применена к полному изображению в TiledInference.
    
    Args:
        rgb_tiles: Список RGB тайлов
        node_tiles: Список node масок
        mean, std: Параметры нормализации
        binarize: Применить бинаризацию (обычно False)
        binarize_method: Метод бинаризации
        
    Returns:
        Тензор [B, 4, H, W]
    """
    batch = []
    
    for rgb, node in zip(rgb_tiles, node_tiles):
        tile_tensor = prepare_tile_batch(
            rgb, node, mean, std, 
            binarize=binarize, 
            binarize_method=binarize_method
        )
        batch.append(tile_tensor)
    
    return torch.cat(batch, dim=0)

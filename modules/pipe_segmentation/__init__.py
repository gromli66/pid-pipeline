"""
Pipe Segmentation — модуль для сегментации труб на P&ID схемах.

Использование через CLI:
    python -m pipe_segmentation prepare --images ./raw --output ./dataset
    python -m pipe_segmentation train --data ./dataset --output ./training
    python -m pipe_segmentation test --images ./test --checkpoint ./best.pth
    python -m pipe_segmentation infer --images ./new --checkpoint ./best.pth

Основные компоненты:
    - data: подготовка данных, тайлинг, датасеты
    - model: архитектура UNet++, loss функции, метрики
    - training: тренер, callbacks
    - inference: тайловый инференс, TTA, постобработка
    - utils: IO, визуализация
"""

__version__ = '1.1.0'
__author__ = 'P&ID Analysis Team'

from pipe_segmentation.config.defaults import (
    DEFAULT_TILE_SIZE,
    DEFAULT_OVERLAP,
    DEFAULT_THRESHOLD,
)

__all__ = [
    '__version__',
    'DEFAULT_TILE_SIZE',
    'DEFAULT_OVERLAP', 
    'DEFAULT_THRESHOLD',
]

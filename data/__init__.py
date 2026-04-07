"""
Data submodule - подготовка и обработка данных.

Включает:
- preprocessing: предобработка изображений (бинаризация, удаление классов)
- splitting: стратифицированное разбиение данных
- tiling: нарезка на тайлы с forbidden масками
- statistics: анализ датасета
"""

from pid_node_detection.data.preprocessing import (
    binarize_image,
    binarize_for_yolo,
    preprocess_images,
    remove_classes,
)
from pid_node_detection.data.splitting import split_data, stratified_split_tiles
from pid_node_detection.data.tiling import tile_dataset, create_forbidden_mask
from pid_node_detection.data.statistics import (
    analyze_dataset,
    count_classes,
    save_statistics
)

__all__ = [
    "binarize_image",
    "binarize_for_yolo",
    "preprocess_images",
    "remove_classes",
    "split_data",
    "stratified_split_tiles",
    "tile_dataset",
    "create_forbidden_mask",
    "analyze_dataset",
    "count_classes",
    "save_statistics",
]
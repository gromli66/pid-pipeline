"""
P&ID Node Detection Module
==========================

Модуль для детекции узлов (оборудования) на P&ID (Piping and Instrumentation Diagram)
схемах с использованием YOLO.

НАЗНАЧЕНИЕ:
-----------
Этот модуль является частью системы перевода P&ID схем в граф. Он отвечает за
детекцию и классификацию 36 типов оборудования на промышленных схемах.

ОСНОВНЫЕ ВОЗМОЖНОСТИ:
--------------------
1. TRAIN - Полный пайплайн обучения:
   - Предобработка изображений (grayscale)
   - Удаление нерелевантных классов
   - Стратифицированное разбиение данных
   - Тайлинг с forbidden масками
   - Copy-Paste аугментация для редких классов
   - Rotation аугментация
   - Обучение YOLOv8

2. FINETUNE - Дообучение на новых данных:
   - Подготовка новых данных через полный пайплайн
   - Объединение с существующим датасетом
   - Дообучение с загрузкой существующих весов

3. TEST - Оценка качества модели:
   - Инференс на тестовых схемах через SAHI
   - Расчет Precision/Recall/F1
   - Confusion matrix
   - Per-class метрики

4. INFERENCE - Детекция на новых схемах:
   - Адаптивный слайсинг через SAHI
   - Выход в YOLO формате

БЫСТРЫЙ СТАРТ:
-------------
    # Обучение
    python -m pid_node_detection train --config config.yaml

    # Тестирование
    python -m pid_node_detection test --config config.yaml --weights best.pt

    # Инференс
    python -m pid_node_detection inference --config config.yaml --weights best.pt --input ./images

    # Дообучение
    python -m pid_node_detection finetune --config config.yaml --weights best.pt

СТРУКТУРА МОДУЛЯ:
----------------
    pid_node_detection/
    ├── config/           # Конфигурационные файлы
    │   ├── default.yaml  # Дефолтные параметры
    │   └── classes.yaml  # Маппинг классов
    ├── data/             # Подготовка данных
    │   ├── preprocessing.py   # Grayscale, удаление классов
    │   ├── splitting.py       # Разбиение на train/val/test
    │   ├── tiling.py          # Нарезка на тайлы
    │   └── statistics.py      # Анализ датасета
    ├── augmentation/     # Аугментации
    │   ├── copy_paste.py      # Copy-Paste Augmentation
    │   └── rotation.py        # Повороты на 90/180/270
    ├── training/         # Обучение
    │   └── trainer.py         # Обертка над YOLO train
    ├── inference/        # Инференс
    │   └── detector.py        # SAHI adaptive inference
    ├── evaluation/       # Оценка
    │   └── metrics.py         # P/R/F1, confusion matrix
    └── pipelines/        # Оркестрация
        ├── train_pipeline.py
        ├── finetune_pipeline.py
        └── test_pipeline.py

ЗАВИСИМОСТИ:
-----------
    - ultralytics>=8.0.0  # YOLOv8
    - sahi>=0.11.0        # Slicing inference
    - opencv-python>=4.5.0
    - numpy>=1.21.0
    - PyYAML>=6.0
    - tqdm>=4.62.0
    - pandas>=1.3.0
    - matplotlib>=3.4.0
    - seaborn>=0.11.0
    - Pillow>=8.0.0

АВТОРЫ:
------
    P&ID Analysis Team

ВЕРСИЯ:
------
    0.1.0
"""

__version__ = "0.1.0"
__author__ = "P&ID Analysis Team"

# Публичный API
from pid_node_detection.config.loader import load_config, load_classes
from pid_node_detection.pipelines.train_pipeline import TrainPipeline
from pid_node_detection.pipelines.finetune_pipeline import FinetunePipeline
from pid_node_detection.pipelines.test_pipeline import TestPipeline
from pid_node_detection.inference.detector import NodeDetector

__all__ = [
    "load_config",
    "load_classes",
    "TrainPipeline",
    "FinetunePipeline",
    "TestPipeline",
    "NodeDetector",
]

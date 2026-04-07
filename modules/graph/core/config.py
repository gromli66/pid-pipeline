"""
Загрузка и валидация конфигурации.
"""

import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass


@dataclass
class PathsConfig:
    equipment_masks: str
    connection_masks: str
    turn_masks: str
    bridge_masks: str
    skeletons: str
    original_images: str
    output_dir: str


@dataclass
class LabelsConfig:
    format: str  # "yolo" или "coco"
    yolo_labels_dir: str
    coco_annotations: str


@dataclass
class GraphConfig:
    min_spur_length: int
    max_path_length: int
    node_dilation: int


@dataclass
class VisualizationConfig:
    dpi: int
    show_labels: bool
    save_stats: bool
    debug_isolated: bool
    debug_contacts: bool


@dataclass
class ExportConfig:
    json_format: str
    include_paths: bool


@dataclass
class ProcessingConfig:
    workers: int
    verbose: bool


@dataclass
class Config:
    paths: PathsConfig
    labels: LabelsConfig
    graph: GraphConfig
    visualization: VisualizationConfig
    export: ExportConfig
    processing: ProcessingConfig


def load_config(config_path: str) -> Config:
    """
    Загрузить конфигурацию из YAML файла.
    
    Args:
        config_path: Путь к файлу конфигурации
        
    Returns:
        Config объект
    """
    config_path = Path(config_path)
    
    if not config_path.exists():
        raise FileNotFoundError(f"Конфиг не найден: {config_path}")
    
    with open(config_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    
    # Парсинг секций
    paths = PathsConfig(
        equipment_masks=data.get('paths', {}).get('equipment_masks', ''),
        connection_masks=data.get('paths', {}).get('connection_masks', ''),
        turn_masks=data.get('paths', {}).get('turn_masks', ''),
        bridge_masks=data.get('paths', {}).get('bridge_masks', ''),
        skeletons=data.get('paths', {}).get('skeletons', ''),
        original_images=data.get('paths', {}).get('original_images', ''),
        output_dir=data.get('paths', {}).get('output_dir', './output'),
    )
    
    # Секция labels (новая)
    labels_data = data.get('labels', {})
    labels = LabelsConfig(
        format=labels_data.get('format', 'yolo'),
        yolo_labels_dir=labels_data.get('yolo_labels_dir', ''),
        coco_annotations=labels_data.get('coco_annotations', ''),
    )
    
    graph = GraphConfig(
        min_spur_length=data.get('graph', {}).get('min_spur_length', 5),
        max_path_length=data.get('graph', {}).get('max_path_length', 10000),
        node_dilation=data.get('graph', {}).get('node_dilation', 1),
    )
    
    visualization = VisualizationConfig(
        dpi=data.get('visualization', {}).get('dpi', 150),
        show_labels=data.get('visualization', {}).get('show_labels', False),
        save_stats=data.get('visualization', {}).get('save_stats', False),
        debug_isolated=data.get('visualization', {}).get('debug_isolated', False),
        debug_contacts=data.get('visualization', {}).get('debug_contacts', False),
    )
    
    export = ExportConfig(
        json_format=data.get('export', {}).get('json_format', 'node-link'),
        include_paths=data.get('export', {}).get('include_paths', False),
    )
    
    processing = ProcessingConfig(
        workers=data.get('processing', {}).get('workers', 1),
        verbose=data.get('processing', {}).get('verbose', False),
    )
    
    return Config(
        paths=paths,
        labels=labels,
        graph=graph,
        visualization=visualization,
        export=export,
        processing=processing,
    )


def validate_paths(config: Config, check_inputs: bool = True) -> None:
    """
    Проверить существование директорий.
    
    Args:
        config: Конфигурация
        check_inputs: Проверять входные директории
        
    Raises:
        FileNotFoundError: Если директория не найдена
    """
    if not check_inputs:
        return
    
    required = [
        ('equipment_masks', config.paths.equipment_masks),
        ('connection_masks', config.paths.connection_masks),
        ('bridge_masks', config.paths.bridge_masks),
        ('skeletons', config.paths.skeletons),
    ]
    
    missing = []
    for name, path in required:
        if not path:
            missing.append(f"{name}: путь не указан")
        elif not Path(path).exists():
            missing.append(f"{name}: {path}")
    
    if missing:
        raise FileNotFoundError(
            "Не найдены обязательные директории:\n  " + "\n  ".join(missing)
        )
    
    # Опциональные директории - просто предупреждение
    if config.paths.turn_masks and not Path(config.paths.turn_masks).exists():
        print(f"ПРЕДУПРЕЖДЕНИЕ: turn_masks не найден: {config.paths.turn_masks}")
    
    if config.paths.original_images and not Path(config.paths.original_images).exists():
        print(f"ПРЕДУПРЕЖДЕНИЕ: original_images не найден: {config.paths.original_images}")
    
    # Проверка разметки
    if config.labels.format == "coco":
        if config.labels.coco_annotations and not Path(config.labels.coco_annotations).exists():
            print(f"ПРЕДУПРЕЖДЕНИЕ: coco_annotations не найден: {config.labels.coco_annotations}")
    else:
        if config.labels.yolo_labels_dir and not Path(config.labels.yolo_labels_dir).exists():
            print(f"ПРЕДУПРЕЖДЕНИЕ: yolo_labels_dir не найден: {config.labels.yolo_labels_dir}")

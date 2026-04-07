"""
Загрузчик конфигурации из YAML файлов.

Поддерживает:
- Загрузка из файла или использование дефолтов
- Слияние с CLI параметрами
- Валидация параметров
"""

import yaml
from pathlib import Path
from typing import Dict, Any, Optional, Union
from dataclasses import dataclass, field
from copy import deepcopy


# Путь к дефолтному конфигу
DEFAULT_CONFIG_PATH = Path(__file__).parent / "default.yaml"


def load_yaml(path: Union[str, Path]) -> Dict[str, Any]:
    """Загружает YAML файл."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def deep_merge(base: Dict, override: Dict) -> Dict:
    """
    Глубокое слияние двух словарей.
    
    Значения из override перезаписывают значения из base.
    Вложенные словари сливаются рекурсивно.
    """
    result = deepcopy(base)
    
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    
    return result


def get_nested(d: Dict, path: str, default: Any = None) -> Any:
    """
    Получает значение по вложенному пути.
    
    Пример: get_nested(config, "paths.raw_images")
    """
    keys = path.split('.')
    value = d
    
    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return default
    
    return value


def set_nested(d: Dict, path: str, value: Any) -> None:
    """
    Устанавливает значение по вложенному пути.
    
    Пример: set_nested(config, "paths.raw_images", "./data")
    """
    keys = path.split('.')
    current = d
    
    for key in keys[:-1]:
        if key not in current:
            current[key] = {}
        current = current[key]
    
    current[keys[-1]] = value


class Config:
    """
    Класс конфигурации с доступом через атрибуты и словарь.
    
    Пример:
        config = Config.load("my_config.yaml")
        print(config.paths.raw_images)
        print(config["paths"]["raw_images"])
        print(config.get("paths.raw_images"))
    """
    
    def __init__(self, data: Dict[str, Any]):
        self._data = data
        
        # Конвертируем вложенные словари в Config
        for key, value in data.items():
            if isinstance(value, dict):
                setattr(self, key, Config(value))
            else:
                setattr(self, key, value)
    
    def __getitem__(self, key: str) -> Any:
        return self._data[key]
    
    def __contains__(self, key: str) -> bool:
        return key in self._data
    
    def get(self, path: str, default: Any = None) -> Any:
        """Получает значение по пути с точками."""
        return get_nested(self._data, path, default)
    
    def set(self, path: str, value: Any) -> None:
        """Устанавливает значение по пути с точками."""
        set_nested(self._data, path, value)
        
        # Обновляем атрибуты
        keys = path.split('.')
        if len(keys) == 1:
            setattr(self, keys[0], value)
    
    def to_dict(self) -> Dict[str, Any]:
        """Возвращает как словарь."""
        return deepcopy(self._data)
    
    def save(self, path: Union[str, Path]) -> None:
        """Сохраняет в YAML файл."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(path, 'w', encoding='utf-8') as f:
            yaml.dump(self._data, f, default_flow_style=False, allow_unicode=True)
    
    @classmethod
    def load(cls, path: Optional[Union[str, Path]] = None) -> 'Config':
        """
        Загружает конфигурацию.
        
        Если path не указан - использует дефолтный конфиг.
        Если указан - сливает с дефолтным (пользовательский приоритетнее).
        """
        # Загружаем дефолтный
        default_data = load_yaml(DEFAULT_CONFIG_PATH)
        
        if path is None:
            return cls(default_data)
        
        # Загружаем пользовательский и сливаем
        user_data = load_yaml(path)
        merged = deep_merge(default_data, user_data)
        
        return cls(merged)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Config':
        """Создаёт из словаря."""
        return cls(data)
    
    @classmethod
    def get_default(cls) -> 'Config':
        """Возвращает дефолтную конфигурацию."""
        return cls.load(None)


def load_config(
    config_path: Optional[str] = None,
    cli_overrides: Optional[Dict[str, Any]] = None
) -> Config:
    """
    Загружает конфигурацию с поддержкой CLI переопределений.
    
    Args:
        config_path: Путь к YAML конфигу (если None - дефолтный)
        cli_overrides: Словарь с переопределениями из CLI
        
    Returns:
        Config объект
    """
    config = Config.load(config_path)
    
    # Применяем CLI переопределения
    if cli_overrides:
        for path, value in cli_overrides.items():
            if value is not None:  # Пропускаем None значения
                config.set(path, value)
    
    return config


def create_cli_overrides(**kwargs) -> Dict[str, Any]:
    """
    Создаёт словарь переопределений из CLI параметров.
    
    Маппинг CLI параметров на пути в конфиге.
    """
    mapping = {
        # paths
        'images': 'paths.raw_images',
        'coco': 'paths.coco_annotations',
        'pipe_masks': 'paths.masks.pipes',
        'node_masks': 'paths.masks.nodes',
        'output': 'paths.dataset_dir',
        
        # preprocessing
        'binarize': 'preprocessing.binarization.enabled',
        'binarize_method': 'preprocessing.binarization.method',
        
        # tiling
        'tile_size': 'tiling.tile_size',
        'overlap': 'tiling.overlap',
        'min_pipe_pixels': 'tiling.filtering.min_pipe_pixels',
        
        # splitting
        'split': None,  # Обрабатывается отдельно
        'test_files': 'splitting.test_files',
        'seed': 'splitting.random_seed',
        
        # training
        'epochs': 'training.epochs',
        'batch_size': 'training.batch_size',
        'encoder_lr': 'training.learning_rate.encoder',
        'decoder_lr': 'training.learning_rate.decoder',
        'patience': 'training.patience',
        'device': 'training.device',
        
        # finetune
        'checkpoint': 'finetune.checkpoint',
        'lr': 'finetune.learning_rate',
        'freeze_encoder': 'finetune.freeze_encoder_epochs',
        
        # inference
        'threshold': 'inference.threshold',
        'tta': 'inference.tta.enabled',
        'postprocess': 'inference.postprocessing.enabled',
        'save_overlay': 'inference.save_overlay',
    }
    
    overrides = {}
    
    for cli_key, config_path in mapping.items():
        if cli_key in kwargs and kwargs[cli_key] is not None and config_path is not None:
            overrides[config_path] = kwargs[cli_key]
    
    # Обработка split (специальный случай)
    if 'split' in kwargs and kwargs['split']:
        parts = [float(x) for x in kwargs['split'].split('/')]
        if len(parts) == 3:
            overrides['splitting.ratios.train'] = parts[0]
            overrides['splitting.ratios.val'] = parts[1]
            overrides['splitting.ratios.test'] = parts[2]
        elif len(parts) == 2:
            overrides['splitting.ratios_finetune.train'] = parts[0]
            overrides['splitting.ratios_finetune.val'] = parts[1]
    
    return overrides


# Экспорт
__all__ = [
    'Config',
    'load_config',
    'create_cli_overrides',
    'load_yaml',
    'deep_merge',
    'DEFAULT_CONFIG_PATH',
]

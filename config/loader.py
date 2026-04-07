"""
Загрузчик конфигурации для P&ID Node Detection.

НАЗНАЧЕНИЕ:
----------
Этот модуль предоставляет функции для загрузки и валидации конфигурационных
файлов YAML. Поддерживает:
- Загрузку основного конфига (default.yaml или пользовательского)
- Загрузку маппинга классов (classes.yaml)
- Объединение дефолтных значений с пользовательскими
- Валидацию обязательных параметров

ИСПОЛЬЗОВАНИЕ:
-------------
    from pid_node_detection.config import load_config, load_classes
    
    # Загрузка конфигурации
    config = load_config("my_config.yaml")
    
    # Доступ к параметрам
    tile_size = config.tiling.tile_size
    model = config.training.model
    
    # Загрузка классов
    classes = load_classes()
    class_names = classes.class_names

СТРУКТУРА КОНФИГА:
-----------------
Config объект имеет следующие атрибуты (соответствуют секциям YAML):
- paths: пути к данным
- preprocessing: параметры предобработки
- splitting: параметры разбиения
- tiling: параметры тайлинга
- augmentation: параметры аугментации
- training: параметры обучения
- finetune: параметры дообучения
- inference: параметры инференса
- evaluation: параметры оценки
- statistics: параметры статистики
"""

import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Union


# Путь к директории с конфигами (относительно этого файла)
CONFIG_DIR = Path(__file__).parent


def _deep_update(base: dict, update: dict) -> dict:
    """
    Рекурсивное обновление словаря.
    
    Объединяет два словаря, при этом вложенные словари также объединяются,
    а не перезаписываются целиком.
    
    Args:
        base: Базовый словарь (будет изменен)
        update: Словарь с обновлениями
        
    Returns:
        Обновленный базовый словарь
        
    Example:
        >>> base = {"a": {"b": 1, "c": 2}}
        >>> update = {"a": {"b": 10}}
        >>> _deep_update(base, update)
        {"a": {"b": 10, "c": 2}}
    """
    for key, value in update.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


class DictToObject:
    """
    Преобразует словарь в объект с атрибутами.
    
    Позволяет обращаться к значениям словаря через точечную нотацию:
    config.training.epochs вместо config["training"]["epochs"]
    
    Поддерживает вложенные словари.
    Словари с числовыми ключами остаются обычными dict.
    """
    
    def __init__(self, data: dict):
        """
        Args:
            data: Словарь для преобразования
        """
        for key, value in data.items():
            if isinstance(value, dict):
                # Проверяем, есть ли нечисловые ключи - только тогда конвертируем
                if value and all(isinstance(k, str) for k in value.keys()):
                    setattr(self, str(key), DictToObject(value))
                else:
                    # Словарь с числовыми ключами оставляем как есть
                    setattr(self, str(key), value)
            else:
                setattr(self, str(key), value)
    
    def __repr__(self) -> str:
        return f"Config({vars(self)})"
    
    def to_dict(self) -> dict:
        """Преобразует обратно в словарь."""
        result = {}
        for key, value in vars(self).items():
            if isinstance(value, DictToObject):
                result[key] = value.to_dict()
            else:
                result[key] = value
        return result


class Config(DictToObject):
    """
    Конфигурация P&ID Node Detection.
    
    Наследует DictToObject для поддержки точечной нотации.
    Добавляет методы валидации и доступа к специфичным параметрам.
    
    Attributes:
        paths: Пути к данным и результатам
        preprocessing: Параметры предобработки
        splitting: Параметры разбиения данных
        tiling: Параметры тайлинга
        augmentation: Параметры аугментации
        training: Параметры обучения
        finetune: Параметры дообучения
        inference: Параметры инференса
        evaluation: Параметры оценки
        statistics: Параметры статистики
    """
    
    def validate(self) -> None:
        """
        Валидирует конфигурацию.
        
        Проверяет:
        - Наличие обязательных секций
        - Корректность значений параметров
        - Существование указанных путей (опционально)
        
        Raises:
            ValueError: Если конфигурация невалидна
        """
        required_sections = ["paths", "preprocessing", "splitting", "tiling", 
                           "augmentation", "training", "inference"]
        
        for section in required_sections:
            if not hasattr(self, section):
                raise ValueError(f"Отсутствует обязательная секция: {section}")
        
        # Проверка параметров тайлинга
        if hasattr(self, "tiling"):
            if self.tiling.tile_size <= 0:
                raise ValueError("tiling.tile_size должен быть положительным")
            if not 0 <= self.tiling.overlap < 1:
                raise ValueError("tiling.overlap должен быть в диапазоне [0, 1)")
            if not 0 < self.tiling.min_visible_ratio <= 1:
                raise ValueError("tiling.min_visible_ratio должен быть в диапазоне (0, 1]")
        
        # Проверка параметров разбиения
        if hasattr(self, "splitting"):
            if not 0 < self.splitting.test_ratio < 1:
                raise ValueError("splitting.test_ratio должен быть в диапазоне (0, 1)")
            if not 0 < self.splitting.val_ratio < 1:
                raise ValueError("splitting.val_ratio должен быть в диапазоне (0, 1)")
        
        # Проверка параметров обучения
        if hasattr(self, "training"):
            if self.training.epochs <= 0:
                raise ValueError("training.epochs должен быть положительным")
            if self.training.batch_size <= 0:
                raise ValueError("training.batch_size должен быть положительным")
    
    def get_path(self, key: str) -> Path:
        """
        Получить путь по ключу с преобразованием в Path.
        
        Args:
            key: Ключ из секции paths (например, "raw_images")
            
        Returns:
            Path объект
        """
        value = getattr(self.paths, key)
        return Path(value)
    
    def ensure_directories(self) -> None:
        """
        Создает все необходимые выходные директории.
        
        Создает директории из секции paths, которые должны существовать
        для записи результатов.
        """
        output_dirs = ["work_dir", "dataset_dir", "experiments_dir", 
                      "inference_output", "statistics_dir"]
        
        for dir_key in output_dirs:
            if hasattr(self.paths, dir_key):
                path = self.get_path(dir_key)
                path.mkdir(parents=True, exist_ok=True)


@dataclass
class ClassesConfig:
    """
    Конфигурация классов P&ID.
    
    Содержит маппинг классов, информацию о редких классах,
    и параметры переиндексации.
    
    Attributes:
        num_classes: Количество классов
        class_names: Маппинг id -> имя класса
        classes_to_remove: Классы для удаления из исходных данных
        reindex_mapping: Маппинг переиндексации (старый id -> новый id)
        reverse_reindex_mapping: Обратный маппинг для инференса
        rare_classes: Список редких классов
        critical_classes: Список критически редких классов
        augmentation_targets: Целевое количество примеров для аугментации
    """
    num_classes: int
    class_names: Dict[int, str]
    classes_to_remove: List[int]
    reindex_mapping: Dict[int, int]
    reverse_reindex_mapping: Dict[int, int]
    rare_classes: List[int]
    critical_classes: List[int]
    augmentation_targets: Dict[str, int]
    
    def get_class_name(self, class_id: int) -> str:
        """
        Получить имя класса по id.
        
        Args:
            class_id: ID класса
            
        Returns:
            Имя класса или "unknown_N" если не найден
        """
        return self.class_names.get(class_id, f"unknown_{class_id}")
    
    def get_class_id(self, class_name: str) -> Optional[int]:
        """
        Получить id класса по имени.
        
        Args:
            class_name: Имя класса
            
        Returns:
            ID класса или None если не найден
        """
        for cid, cname in self.class_names.items():
            if cname == class_name:
                return cid
        return None
    
    def is_rare(self, class_id: int) -> bool:
        """Проверить, является ли класс редким."""
        return class_id in self.rare_classes
    
    def is_critical(self, class_id: int) -> bool:
        """Проверить, является ли класс критически редким."""
        return class_id in self.critical_classes
    
    def get_target_count(self, class_id: int) -> int:
        """
        Получить целевое количество примеров для аугментации.
        
        Args:
            class_id: ID класса
            
        Returns:
            Целевое количество (200 для критических, 150 для редких, 0 для остальных)
        """
        if self.is_critical(class_id):
            return self.augmentation_targets.get("critical_classes", 200)
        elif self.is_rare(class_id):
            return self.augmentation_targets.get("rare_classes", 150)
        return 0


def load_config(config_path: Optional[Union[str, Path]] = None) -> Config:
    """
    Загрузить конфигурацию.
    
    Загружает конфигурацию из указанного файла или использует дефолтную.
    Пользовательская конфигурация объединяется с дефолтной (дефолтные значения
    используются для отсутствующих параметров).
    
    Args:
        config_path: Путь к YAML файлу конфигурации.
                    Если None, загружается только default.yaml
                    
    Returns:
        Config объект с загруженными параметрами
        
    Raises:
        FileNotFoundError: Если указанный файл не найден
        yaml.YAMLError: Если файл содержит невалидный YAML
        ValueError: Если конфигурация не проходит валидацию
        
    Example:
        >>> config = load_config()  # Загрузить дефолтную
        >>> config = load_config("my_config.yaml")  # Загрузить пользовательскую
        >>> print(config.training.epochs)
        100
    """
    # Загрузить дефолтную конфигурацию
    default_path = CONFIG_DIR / "default.yaml"
    
    if not default_path.exists():
        raise FileNotFoundError(f"Дефолтная конфигурация не найдена: {default_path}")
    
    with open(default_path, "r", encoding="utf-8") as f:
        config_data = yaml.safe_load(f)
    
    # Если указан пользовательский конфиг - объединить
    if config_path is not None:
        config_path = Path(config_path)
        
        if not config_path.exists():
            raise FileNotFoundError(f"Конфигурация не найдена: {config_path}")
        
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f)
        
        if user_config:
            config_data = _deep_update(config_data, user_config)
    
    # Создать Config объект
    config = Config(config_data)
    
    # Валидировать
    config.validate()
    
    return config


def load_classes(classes_path: Optional[Union[str, Path]] = None) -> ClassesConfig:
    """
    Загрузить конфигурацию классов.
    
    Args:
        classes_path: Путь к YAML файлу с классами.
                     Если None, загружается classes.yaml из директории config
                     
    Returns:
        ClassesConfig объект
        
    Raises:
        FileNotFoundError: Если файл не найден
        yaml.YAMLError: Если файл содержит невалидный YAML
        
    Example:
        >>> classes = load_classes()
        >>> print(classes.get_class_name(0))
        "armatura_ruchn"
        >>> print(classes.is_rare(7))
        True
    """
    if classes_path is None:
        classes_path = CONFIG_DIR / "classes.yaml"
    else:
        classes_path = Path(classes_path)
    
    if not classes_path.exists():
        raise FileNotFoundError(f"Файл классов не найден: {classes_path}")
    
    with open(classes_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    
    # Преобразовать ключи class_names в int (YAML может загрузить как строки)
    class_names = {int(k): v for k, v in data["class_names"].items()}
    reindex = {int(k): int(v) for k, v in data.get("reindex_mapping", {}).items()}
    reverse_reindex = {int(k): int(v) for k, v in data.get("reverse_reindex_mapping", {}).items()}
    
    return ClassesConfig(
        num_classes=data["num_classes"],
        class_names=class_names,
        classes_to_remove=data.get("classes_to_remove", []),
        reindex_mapping=reindex,
        reverse_reindex_mapping=reverse_reindex,
        rare_classes=data.get("rare_classes", []),
        critical_classes=data.get("critical_classes", []),
        augmentation_targets=data.get("augmentation_targets", {})
    )


def create_data_yaml(
    dataset_dir: Union[str, Path],
    classes: ClassesConfig,
    train_path: str = "train/images",
    val_path: str = "val/images",
    output_path: Optional[Union[str, Path]] = None
) -> Path:
    """
    Создать data.yaml для обучения YOLO.
    
    Генерирует YAML файл в формате, требуемом ultralytics YOLO.
    
    Args:
        dataset_dir: Корневая директория датасета
        classes: Конфигурация классов
        train_path: Относительный путь к train изображениям
        val_path: Относительный путь к val изображениям
        output_path: Путь для сохранения. Если None, сохраняется в dataset_dir/data.yaml
        
    Returns:
        Path к созданному файлу
        
    Example:
        >>> classes = load_classes()
        >>> yaml_path = create_data_yaml("./dataset", classes)
        >>> print(yaml_path)
        ./dataset/data.yaml
    """
    dataset_dir = Path(dataset_dir)
    
    if output_path is None:
        output_path = dataset_dir / "data.yaml"
    else:
        output_path = Path(output_path)
    
    data = {
        "path": str(dataset_dir.absolute()),
        "train": train_path,
        "val": val_path,
        "nc": classes.num_classes,
        "names": classes.class_names
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, sort_keys=False, allow_unicode=True)
    
    return output_path

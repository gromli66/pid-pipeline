"""
Конфигурация и параметры по умолчанию.

Поддерживает два способа конфигурации:
1. Через defaults.py (программные константы)
2. Через YAML файлы (default.yaml + пользовательский)
"""

from pipe_segmentation.config.defaults import *
from pipe_segmentation.config.schemas import (
    PrepareConfig,
    TrainConfig,
    FinetuneConfig,
    TestConfig,
    InferConfig,
    ModelConfig,
    TilingConfig,
    PostprocessConfig,
)
from pipe_segmentation.config.config_loader import (
    Config,
    load_config,
    create_cli_overrides,
    DEFAULT_CONFIG_PATH,
)

__all__ = [
    # Pydantic schemas
    "PrepareConfig",
    "TrainConfig", 
    "FinetuneConfig",
    "TestConfig",
    "InferConfig",
    "ModelConfig",
    "TilingConfig",
    "PostprocessConfig",
    # YAML config
    "Config",
    "load_config",
    "create_cli_overrides",
    "DEFAULT_CONFIG_PATH",
]

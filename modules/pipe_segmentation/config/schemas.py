"""
Pydantic схемы для валидации конфигураций.

Все конфигурации команд CLI валидируются через эти схемы.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Tuple
from pathlib import Path

from pipe_segmentation.config.defaults import (
    DEFAULT_TILE_SIZE,
    DEFAULT_OVERLAP,
    DEFAULT_MIN_PIPE_PIXELS,
    DEFAULT_MIN_NODE_PIXELS,
    DEFAULT_TRAIN_EPOCHS,
    DEFAULT_TRAIN_BATCH_SIZE,
    DEFAULT_TRAIN_ACCUMULATION_STEPS,
    DEFAULT_TRAIN_ENCODER_LR,
    DEFAULT_TRAIN_DECODER_LR,
    DEFAULT_TRAIN_WEIGHT_DECAY,
    DEFAULT_TRAIN_PATIENCE,
    DEFAULT_FINETUNE_EPOCHS,
    DEFAULT_FINETUNE_BATCH_SIZE,
    DEFAULT_FINETUNE_ACCUMULATION_STEPS,
    DEFAULT_FINETUNE_LR,
    DEFAULT_FINETUNE_PATIENCE,
    DEFAULT_FINETUNE_FREEZE_ENCODER,
    DEFAULT_INFERENCE_BATCH_SIZE,
    DEFAULT_THRESHOLD,
    DEFAULT_USE_TTA,
    DEFAULT_USE_POSTPROCESS,
    DEFAULT_RANDOM_SEED,
    DEFAULT_DEVICE,
    DEFAULT_SPLIT_RATIOS,
    DEFAULT_GRADIENT_CLIP,
    IMAGENET_MEAN,
    IMAGENET_STD,
)


@dataclass
class TilingConfig:
    """Конфигурация тайлинга изображений."""
    
    tile_size: int = DEFAULT_TILE_SIZE
    overlap: int = DEFAULT_OVERLAP
    min_pipe_pixels: int = DEFAULT_MIN_PIPE_PIXELS
    min_node_pixels: int = DEFAULT_MIN_NODE_PIXELS
    
    @property
    def stride(self) -> int:
        """Шаг между тайлами."""
        return self.tile_size - self.overlap
    
    def __post_init__(self):
        if self.tile_size <= 0:
            raise ValueError(f"tile_size must be positive, got {self.tile_size}")
        if self.overlap < 0:
            raise ValueError(f"overlap must be non-negative, got {self.overlap}")
        if self.overlap >= self.tile_size:
            raise ValueError(f"overlap must be less than tile_size")


@dataclass
class ModelConfig:
    """Конфигурация модели U-Net++."""
    
    architecture: str = 'UnetPlusPlus'
    encoder_name: str = 'efficientnet-b3'
    encoder_weights: Optional[str] = 'imagenet'
    in_channels: int = 4
    classes: int = 1
    activation: Optional[str] = None
    decoder_attention_type: Optional[str] = None


@dataclass
class PostprocessConfig:
    """Конфигурация постобработки масок."""
    
    enabled: bool = True
    remove_small_objects: int = 100
    closing_kernel_size: int = 5
    opening_kernel_size: int = 3


@dataclass
class PrepareConfig:
    """Конфигурация команды prepare."""
    
    images_dir: Path
    coco_path: Path
    output_dir: Path
    tile_size: int = DEFAULT_TILE_SIZE
    overlap: int = DEFAULT_OVERLAP
    split: str = DEFAULT_SPLIT_RATIOS
    test_files: Optional[Path] = None
    min_pipe_pixels: int = DEFAULT_MIN_PIPE_PIXELS
    min_node_pixels: int = DEFAULT_MIN_NODE_PIXELS
    seed: int = DEFAULT_RANDOM_SEED
    
    def __post_init__(self):
        self.images_dir = Path(self.images_dir)
        self.coco_path = Path(self.coco_path)
        self.output_dir = Path(self.output_dir)
        if self.test_files:
            self.test_files = Path(self.test_files)
    
    def parse_split_ratios(self) -> Tuple[float, ...]:
        """Парсит строку split в tuple чисел."""
        parts = self.split.split('/')
        ratios = tuple(float(p) for p in parts)
        if abs(sum(ratios) - 1.0) > 0.01:
            raise ValueError(f"Split ratios must sum to 1.0, got {sum(ratios)}")
        return ratios


@dataclass 
class TrainConfig:
    """Конфигурация команды train."""
    
    data_dir: Path
    output_dir: Path
    epochs: int = DEFAULT_TRAIN_EPOCHS
    batch_size: int = DEFAULT_TRAIN_BATCH_SIZE
    accumulation_steps: int = DEFAULT_TRAIN_ACCUMULATION_STEPS
    encoder_lr: float = DEFAULT_TRAIN_ENCODER_LR
    decoder_lr: float = DEFAULT_TRAIN_DECODER_LR
    weight_decay: float = DEFAULT_TRAIN_WEIGHT_DECAY
    patience: int = DEFAULT_TRAIN_PATIENCE
    gradient_clip: float = DEFAULT_GRADIENT_CLIP
    device: str = DEFAULT_DEVICE
    seed: int = DEFAULT_RANDOM_SEED
    num_workers: int = 0
    pin_memory: bool = True
    use_amp: bool = True
    
    # Model config
    model: ModelConfig = field(default_factory=ModelConfig)
    
    def __post_init__(self):
        self.data_dir = Path(self.data_dir)
        self.output_dir = Path(self.output_dir)
    
    @property
    def effective_batch_size(self) -> int:
        """Эффективный размер батча с учётом аккумуляции."""
        return self.batch_size * self.accumulation_steps


@dataclass
class FinetuneConfig:
    """Конфигурация команды finetune."""
    
    data_dir: Path
    checkpoint_path: Path
    output_dir: Path
    epochs: int = DEFAULT_FINETUNE_EPOCHS
    batch_size: int = DEFAULT_FINETUNE_BATCH_SIZE
    accumulation_steps: int = DEFAULT_FINETUNE_ACCUMULATION_STEPS
    lr: float = DEFAULT_FINETUNE_LR
    weight_decay: float = DEFAULT_TRAIN_WEIGHT_DECAY
    patience: int = DEFAULT_FINETUNE_PATIENCE
    freeze_encoder: int = DEFAULT_FINETUNE_FREEZE_ENCODER
    gradient_clip: float = DEFAULT_GRADIENT_CLIP
    device: str = DEFAULT_DEVICE
    seed: int = DEFAULT_RANDOM_SEED
    num_workers: int = 0
    pin_memory: bool = True
    use_amp: bool = True
    
    def __post_init__(self):
        self.data_dir = Path(self.data_dir)
        self.checkpoint_path = Path(self.checkpoint_path)
        self.output_dir = Path(self.output_dir)


@dataclass
class TestConfig:
    """Конфигурация команды test."""
    
    images_dir: Path
    coco_path: Path
    checkpoint_path: Path
    output_dir: Path
    tile_size: int = DEFAULT_TILE_SIZE
    overlap: int = DEFAULT_OVERLAP
    batch_size: int = DEFAULT_INFERENCE_BATCH_SIZE
    threshold: float = DEFAULT_THRESHOLD
    tta: bool = DEFAULT_USE_TTA
    postprocess: bool = DEFAULT_USE_POSTPROCESS
    device: str = DEFAULT_DEVICE
    
    # Postprocess config
    postprocess_config: PostprocessConfig = field(default_factory=PostprocessConfig)
    
    def __post_init__(self):
        self.images_dir = Path(self.images_dir)
        self.coco_path = Path(self.coco_path)
        self.checkpoint_path = Path(self.checkpoint_path)
        self.output_dir = Path(self.output_dir)


@dataclass
class InferConfig:
    """Конфигурация команды infer."""
    
    images_path: Path  # Может быть папкой или файлом
    checkpoint_path: Path
    output_dir: Path
    coco_path: Optional[Path] = None
    tile_size: int = DEFAULT_TILE_SIZE
    overlap: int = DEFAULT_OVERLAP
    batch_size: int = DEFAULT_INFERENCE_BATCH_SIZE
    threshold: float = DEFAULT_THRESHOLD
    tta: bool = DEFAULT_USE_TTA
    postprocess: bool = DEFAULT_USE_POSTPROCESS
    save_overlay: bool = False
    device: str = DEFAULT_DEVICE
    
    # Postprocess config
    postprocess_config: PostprocessConfig = field(default_factory=PostprocessConfig)
    
    def __post_init__(self):
        self.images_path = Path(self.images_path)
        self.checkpoint_path = Path(self.checkpoint_path)
        self.output_dir = Path(self.output_dir)
        if self.coco_path:
            self.coco_path = Path(self.coco_path)
    
    @property
    def has_node_masks(self) -> bool:
        """Есть ли COCO для извлечения node масок."""
        return self.coco_path is not None


@dataclass
class NormalizationConfig:
    """Конфигурация нормализации изображений."""
    
    mean: List[float] = field(default_factory=lambda: list(IMAGENET_MEAN))
    std: List[float] = field(default_factory=lambda: list(IMAGENET_STD))

"""
PyTorch Dataset для сегментации труб.

Поддерживает:
- 4-канальный вход (RGB + node_mask)
- Аугментации через albumentations
- Автоматическое создание DataLoader'ов
"""

import torch
from torch.utils.data import Dataset, DataLoader
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Callable

from pipe_segmentation.utils.io import load_image, get_image_files
from pipe_segmentation.data.augmentations import (
    get_train_augmentations,
    get_val_augmentations,
)
from pipe_segmentation.config.defaults import (
    DEFAULT_TRAIN_BATCH_SIZE,
    DEFAULT_INFERENCE_BATCH_SIZE,
    DEFAULT_NUM_WORKERS,
    DEFAULT_PIN_MEMORY,
    SPLIT_IMAGES_DIR,
    SPLIT_PIPE_MASKS_DIR,
    SPLIT_NODE_MASKS_DIR,
)


class PipeSegmentationDataset(Dataset):
    """
    Dataset для сегментации труб на P&ID схемах.
    
    Структура папки данных:
        data_dir/
        ├── train/
        │   ├── images/
        │   ├── pipe_masks/
        │   └── node_masks/
        ├── val/
        │   └── ...
        └── test/
            └── ...
    
    Пример:
        dataset = PipeSegmentationDataset('./data', 'train', transform=get_train_augmentations())
        sample = dataset[0]
        # sample['image'] - torch.Tensor [4, H, W]
        # sample['mask'] - torch.Tensor [1, H, W]
    """
    
    def __init__(
        self,
        data_dir: str,
        split: str = 'train',
        transform: Optional[Callable] = None
    ):
        """
        Args:
            data_dir: Путь к корневой директории датасета
            split: Название split'а ('train', 'val', 'test')
            transform: albumentations transform pipeline
        """
        self.data_dir = Path(data_dir)
        self.split = split
        self.transform = transform
        
        # Пути к папкам (унифицированные имена)
        self.split_dir = self.data_dir / split
        self.images_dir = self.split_dir / SPLIT_IMAGES_DIR
        self.pipe_masks_dir = self.split_dir / SPLIT_PIPE_MASKS_DIR
        self.node_masks_dir = self.split_dir / SPLIT_NODE_MASKS_DIR
        
        # Проверка существования
        if not self.images_dir.exists():
            raise FileNotFoundError(f"Images directory not found: {self.images_dir}")
        
        # Список файлов
        self.image_files = get_image_files(self.images_dir)
        
        if len(self.image_files) == 0:
            raise ValueError(f"No images found in {self.images_dir}")
    
    def __len__(self) -> int:
        return len(self.image_files)
    
    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        """
        Возвращает sample:
            - 'image': [4, H, W] (RGB normalized + node_mask normalized)
            - 'mask': [1, H, W] (pipe mask)
            - 'filename': str
        """
        img_path = self.image_files[idx]
        filename = img_path.name
        
        # Загрузка изображения (RGB)
        image = load_image(img_path, rgb=True)
        if image is None:
            raise ValueError(f"Failed to load image: {img_path}")
        
        # Загрузка маски труб (GT)
        pipe_mask_path = self.pipe_masks_dir / filename
        if pipe_mask_path.exists():
            pipe_mask = load_image(pipe_mask_path, grayscale=True)
        else:
            pipe_mask = np.zeros(image.shape[:2], dtype=np.uint8)
        
        # Загрузка маски узлов (4-й канал)
        node_mask_path = self.node_masks_dir / filename
        if node_mask_path.exists():
            node_mask = load_image(node_mask_path, grayscale=True)
        else:
            node_mask = np.zeros(image.shape[:2], dtype=np.uint8)
        
        # КРИТИЧНО: Бинаризация масок (НЕ просто деление на 255!)
        # Маски должны быть строго 0 или 1, иначе Dice Loss работает неправильно
        pipe_mask = (pipe_mask > 127).astype(np.float32)  # Строго бинарная: 0 или 1
        
        # node_mask (4-й канал) - тоже бинаризуем, потом нормализуем
        node_mask = (node_mask > 127).astype(np.float32)  # 0 или 1
        
        # Применение аугментаций
        if self.transform:
            # [FIX 7.2] pipe_mask передаётся как 'mask' — именно его читает
            # DashedLineAugmentation через targets_as_params -> params.get("mask")
            # node_mask передаётся через additional_targets как отдельный ключ
            transformed = self.transform(
                image=image,
                mask=pipe_mask,
                node_mask=node_mask
            )
            image = transformed['image']  # [3, H, W] после ToTensorV2
            pipe_mask = transformed['mask']  # [H, W]
            node_mask = transformed['node_mask']  # [H, W]
            
            # node_mask тоже нужно нормализовать и добавить к image
            if isinstance(node_mask, np.ndarray):
                node_mask = torch.from_numpy(node_mask).float()
            
            # [FIX 7.1] Нормализация node_mask через NODE_CHANNEL_MEAN/STD
            # Приводит масштаб 4-го канала к масштабу нормализованных RGB каналов
            from pipe_segmentation.config.defaults import NODE_CHANNEL_MEAN, NODE_CHANNEL_STD
            node_mask = (node_mask - NODE_CHANNEL_MEAN) / NODE_CHANNEL_STD
            
            # Добавляем node_mask как 4-й канал
            if node_mask.dim() == 2:
                node_mask = node_mask.unsqueeze(0)  # [1, H, W]
            
            image = torch.cat([image, node_mask], dim=0)  # [4, H, W]
            
            # Маска в формат [1, H, W]
            if isinstance(pipe_mask, np.ndarray):
                pipe_mask = torch.from_numpy(pipe_mask).float()
            if pipe_mask.dim() == 2:
                pipe_mask = pipe_mask.unsqueeze(0)  # [1, H, W]
        
        else:
            # Без аугментаций — ручная обработка
            image = torch.from_numpy(image).permute(2, 0, 1).float() / 255.0
            node_mask = torch.from_numpy(node_mask).unsqueeze(0).float()
            pipe_mask = torch.from_numpy(pipe_mask).unsqueeze(0).float()
            
            image = torch.cat([image, node_mask], dim=0)
        
        # Генерируем GT скелет для multi-task обучения
        skeleton_gt = self._generate_skeleton_gt(pipe_mask)
        
        return {
            'image': image,
            'mask': pipe_mask,
            'skeleton': skeleton_gt,
            'filename': filename
        }
    
    @staticmethod
    def _generate_skeleton_gt(pipe_mask: torch.Tensor) -> torch.Tensor:
        """
        Генерирует GT скелет из маски труб.
        Скелет = центральная линия трубы (1px ширина).
        
        Используется для multi-task обучения (DualHeadModel).
        """
        import cv2
        
        if isinstance(pipe_mask, torch.Tensor):
            mask_np = pipe_mask.squeeze().numpy()
        else:
            mask_np = pipe_mask
        
        mask_uint8 = (mask_np > 0.5).astype(np.uint8) * 255
        
        if mask_uint8.max() == 0:
            # Пустая маска → пустой скелет
            skel = np.zeros_like(mask_np, dtype=np.float32)
        elif hasattr(cv2, 'ximgproc') and hasattr(cv2.ximgproc, 'thinning'):
            skel_raw = cv2.ximgproc.thinning(mask_uint8)
            skel = (skel_raw > 0).astype(np.float32)
        else:
            # Морфологический fallback
            skel = np.zeros_like(mask_uint8)
            element = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
            temp = mask_uint8.copy()
            while True:
                eroded = cv2.erode(temp, element)
                opened = cv2.dilate(eroded, element)
                subset = cv2.subtract(temp, opened)
                skel = cv2.bitwise_or(skel, subset)
                temp = eroded.copy()
                if cv2.countNonZero(temp) == 0:
                    break
            skel = (skel > 0).astype(np.float32)
        
        skel_tensor = torch.from_numpy(skel).float()
        if skel_tensor.dim() == 2:
            skel_tensor = skel_tensor.unsqueeze(0)  # [1, H, W]
        return skel_tensor
    
    def get_sample_info(self, idx: int) -> Dict:
        """Возвращает информацию о sample без загрузки тензоров."""
        img_path = self.image_files[idx]
        return {
            'filename': img_path.name,
            'path': str(img_path),
            'has_pipe_mask': (self.pipe_masks_dir / img_path.name).exists(),
            'has_node_mask': (self.node_masks_dir / img_path.name).exists()
        }


def create_dataloaders(
    data_dir: str,
    batch_size: int = DEFAULT_TRAIN_BATCH_SIZE,
    num_workers: int = DEFAULT_NUM_WORKERS,
    pin_memory: bool = DEFAULT_PIN_MEMORY,
    shuffle_train: bool = True,
    use_augmentations: bool = True
) -> Tuple[DataLoader, DataLoader, Optional[DataLoader]]:
    """
    Создаёт DataLoader'ы для train, val и опционально test.
    
    Args:
        data_dir: Путь к директории с данными
        batch_size: Размер батча
        num_workers: Количество worker'ов
        pin_memory: Pin memory для GPU
        shuffle_train: Перемешивать train
        use_augmentations: Использовать аугментации для train
        
    Returns:
        (train_loader, val_loader, test_loader или None)
    """
    data_dir = Path(data_dir)
    
    # Transforms
    train_transform = get_train_augmentations() if use_augmentations else get_val_augmentations()
    val_transform = get_val_augmentations()
    
    # Datasets
    train_dataset = PipeSegmentationDataset(data_dir, 'train', train_transform)
    val_dataset = PipeSegmentationDataset(data_dir, 'val', val_transform)
    
    test_dataset = None
    if (data_dir / 'test').exists():
        test_dataset = PipeSegmentationDataset(data_dir, 'test', val_transform)
    
    # DataLoaders
    # [PERF] persistent_workers=True + prefetch_factor=2 когда num_workers>0
    # Это не пересоздаёт workers каждую эпоху и предзагружает данные
    loader_kwargs = {}
    if num_workers > 0:
        loader_kwargs['persistent_workers'] = True
        loader_kwargs['prefetch_factor'] = 2
    
    train_loader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=shuffle_train,
        num_workers=num_workers,
        pin_memory=pin_memory,
        drop_last=True,
        **loader_kwargs
    )
    
    val_loader = DataLoader(
        val_dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=pin_memory,
        drop_last=False,
        **loader_kwargs
    )
    
    test_loader = None
    if test_dataset:
        test_loader = DataLoader(
            test_dataset,
            batch_size=batch_size,
            shuffle=False,
            num_workers=num_workers,
            pin_memory=pin_memory,
            drop_last=False,
            **loader_kwargs
        )
    
    return train_loader, val_loader, test_loader


def verify_dataset(dataset: PipeSegmentationDataset, n_samples: int = 3) -> None:
    """
    Проверяет корректность датасета.
    
    Args:
        dataset: Датасет для проверки
        n_samples: Количество samples для проверки
    """
    print(f"\nVerifying dataset: {dataset.split}")
    print(f"  Total samples: {len(dataset)}")
    
    for i in range(min(n_samples, len(dataset))):
        sample = dataset[i]
        
        image = sample['image']
        mask = sample['mask']
        filename = sample['filename']
        
        print(f"\n  Sample {i}: {filename}")
        print(f"    Image shape: {image.shape}")
        print(f"    Image range: [{image.min():.3f}, {image.max():.3f}]")
        print(f"    Mask shape: {mask.shape}")
        print(f"    Mask unique: {torch.unique(mask).tolist()}")
        
        # Проверки
        assert image.shape[0] == 4, f"Expected 4 channels, got {image.shape[0]}"
        assert mask.shape[0] == 1, f"Expected 1 mask channel, got {mask.shape[0]}"
        assert image.shape[1:] == mask.shape[1:], "Image and mask spatial dims must match"
    
    print("\n  ✓ Dataset verification passed!")

"""
transforms.py

Аугментации и трансформации для обучения.

Ключевая особенность: RGB и Skeleton изображения должны
трансформироваться ОДИНАКОВО (синхронизированно).
"""

import random
from typing import Tuple

import numpy as np
from PIL import Image
import torch
from torchvision import transforms
from torchvision.transforms import functional as TF


class SyncTransform:
    """
    Синхронизированные трансформации для RGB и Skeleton изображений.
    
    Одни и те же геометрические преобразования применяются к обоим
    изображениям, чтобы сохранить соответствие между ними.
    
    Args:
        img_size: Размер выходного изображения
        is_train: Режим обучения (с аугментациями) или inference
        strong_rotation: Использовать сильные повороты (90°, 180°, 270°)
    """
    
    def __init__(self, img_size: int = 224, is_train: bool = True,
                 strong_rotation: bool = True):
        self.img_size = img_size
        self.is_train = is_train
        self.strong_rotation = strong_rotation
        
        # Нормализация для RGB (ImageNet stats)
        self.rgb_normalize = transforms.Normalize(
            mean=[0.485, 0.456, 0.406],
            std=[0.229, 0.224, 0.225]
        )
        
        # Нормализация для Skeleton (простая)
        self.skel_normalize = transforms.Normalize(
            mean=[0.5],
            std=[0.5]
        )
    
    def __call__(self, rgb: Image.Image, skeleton: Image.Image) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Применение трансформаций.
        
        Args:
            rgb: RGB изображение (PIL Image)
            skeleton: Skeleton изображение (PIL Image, grayscale)
            
        Returns:
            (rgb_tensor, skeleton_tensor)
        """
        # Убедимся в правильных режимах
        if rgb.mode != 'RGB':
            rgb = rgb.convert('RGB')
        if skeleton.mode != 'L':
            skeleton = skeleton.convert('L')
        
        # Resize если нужно
        if rgb.size != (self.img_size, self.img_size):
            rgb = rgb.resize((self.img_size, self.img_size), Image.BILINEAR)
            skeleton = skeleton.resize((self.img_size, self.img_size), Image.NEAREST)
        
        if self.is_train:
            # =========================================================
            # АУГМЕНТАЦИИ (синхронизированные)
            # =========================================================
            
            # 1. Горизонтальное отражение (50%)
            if random.random() > 0.5:
                rgb = TF.hflip(rgb)
                skeleton = TF.hflip(skeleton)
            
            # 2. Вертикальное отражение (50%)
            if random.random() > 0.5:
                rgb = TF.vflip(rgb)
                skeleton = TF.vflip(skeleton)
            
            # 3. Повороты
            if self.strong_rotation:
                # Дискретные повороты: 0°, 90°, 180°, 270°
                angle = random.choice([0, 90, 180, 270])
                if angle != 0:
                    rgb = TF.rotate(rgb, angle, expand=False)
                    skeleton = TF.rotate(skeleton, angle, expand=False)
            else:
                # Небольшие случайные повороты
                if random.random() > 0.5:
                    angle = random.uniform(-15, 15)
                    rgb = TF.rotate(rgb, angle, expand=False, fill=255)
                    skeleton = TF.rotate(skeleton, angle, expand=False, fill=0)
            
            # 4. Цветовые аугментации (ТОЛЬКО для RGB)
            if random.random() > 0.5:
                rgb = TF.adjust_brightness(rgb, random.uniform(0.8, 1.2))
            if random.random() > 0.5:
                rgb = TF.adjust_contrast(rgb, random.uniform(0.8, 1.2))
        
        # =========================================================
        # КОНВЕРТАЦИЯ В ТЕНЗОРЫ
        # =========================================================
        
        # RGB: [0, 255] -> [0, 1] -> normalized
        rgb_tensor = TF.to_tensor(rgb)  # [3, H, W], [0, 1]
        rgb_tensor = self.rgb_normalize(rgb_tensor)
        
        # Skeleton: [0, 255] -> [0, 1] -> normalized
        skeleton_tensor = TF.to_tensor(skeleton)  # [1, H, W], [0, 1]
        skeleton_tensor = self.skel_normalize(skeleton_tensor)
        
        return rgb_tensor, skeleton_tensor


class TTATransform:
    """
    Test-Time Augmentation трансформации.
    
    Создаёт несколько версий входного изображения для усреднения предсказаний.
    """
    
    def __init__(self, img_size: int = 224):
        self.img_size = img_size
        self.base_transform = SyncTransform(img_size, is_train=False)
        
        # Варианты TTA
        self.tta_variants = [
            None,           # Оригинал
            'hflip',        # Горизонтальное отражение
            'vflip',        # Вертикальное отражение
            'rot90',        # Поворот на 90°
            'rot180',       # Поворот на 180°
            'rot270',       # Поворот на 270°
        ]
    
    def __call__(self, rgb: Image.Image, skeleton: Image.Image) -> list:
        """
        Создание TTA вариантов.
        
        Returns:
            Список кортежей (rgb_tensor, skeleton_tensor) для каждого варианта
        """
        results = []
        
        for variant in self.tta_variants:
            rgb_aug = rgb.copy()
            skel_aug = skeleton.copy()
            
            if variant == 'hflip':
                rgb_aug = TF.hflip(rgb_aug)
                skel_aug = TF.hflip(skel_aug)
            elif variant == 'vflip':
                rgb_aug = TF.vflip(rgb_aug)
                skel_aug = TF.vflip(skel_aug)
            elif variant == 'rot90':
                rgb_aug = TF.rotate(rgb_aug, 90)
                skel_aug = TF.rotate(skel_aug, 90)
            elif variant == 'rot180':
                rgb_aug = TF.rotate(rgb_aug, 180)
                skel_aug = TF.rotate(skel_aug, 180)
            elif variant == 'rot270':
                rgb_aug = TF.rotate(rgb_aug, 270)
                skel_aug = TF.rotate(skel_aug, 270)
            
            rgb_tensor, skel_tensor = self.base_transform(rgb_aug, skel_aug)
            results.append((rgb_tensor, skel_tensor))
        
        return results


def denormalize_rgb(tensor: torch.Tensor) -> np.ndarray:
    """
    Денормализация RGB тензора обратно в [0, 255].
    
    Args:
        tensor: Нормализованный тензор [3, H, W]
        
    Returns:
        numpy array [H, W, 3] в uint8
    """
    mean = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)
    
    tensor = tensor.cpu() * std + mean
    tensor = torch.clamp(tensor, 0, 1)
    
    # [3, H, W] -> [H, W, 3]
    img = tensor.permute(1, 2, 0).numpy()
    img = (img * 255).astype(np.uint8)
    
    return img


def denormalize_skeleton(tensor: torch.Tensor) -> np.ndarray:
    """
    Денормализация Skeleton тензора обратно в [0, 255].
    
    Args:
        tensor: Нормализованный тензор [1, H, W]
        
    Returns:
        numpy array [H, W] в uint8
    """
    tensor = tensor.cpu() * 0.5 + 0.5
    tensor = torch.clamp(tensor, 0, 1)
    
    # [1, H, W] -> [H, W]
    img = tensor.squeeze(0).numpy()
    img = (img * 255).astype(np.uint8)
    
    return img

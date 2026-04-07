"""
Test-Time Augmentation (TTA) для улучшения предсказаний.

Применяет несколько аугментаций к входу, получает предсказания,
и усредняет результаты для более стабильного результата.
"""

import torch
import numpy as np
from typing import Callable, List, Tuple


def predict_with_tta(
    model: torch.nn.Module,
    tile_batch: torch.Tensor,
    device: str = 'cuda',
    tta_mode: str = 'flip'
) -> torch.Tensor:
    """
    Предсказание с Test-Time Augmentation.
    
    Режимы TTA:
    - 'flip': 4 варианта (original, hflip, vflip, both)
    - 'flip_rotate': 8 вариантов (flip + rot90, rot180, rot270)
    
    Args:
        model: Модель для инференса
        tile_batch: Входной тензор [B, C, H, W]
        device: Устройство
        tta_mode: Режим TTA
        
    Returns:
        Усреднённые предсказания [B, 1, H, W]
    """
    model.eval()
    tile_batch = tile_batch.to(device)
    
    predictions = []
    
    with torch.no_grad():
        if tta_mode == 'flip':
            # Original
            pred = torch.sigmoid(model(tile_batch))
            predictions.append(pred)
            
            # Horizontal flip
            flipped_h = torch.flip(tile_batch, dims=[3])
            pred_h = torch.sigmoid(model(flipped_h))
            pred_h = torch.flip(pred_h, dims=[3])
            predictions.append(pred_h)
            
            # Vertical flip
            flipped_v = torch.flip(tile_batch, dims=[2])
            pred_v = torch.sigmoid(model(flipped_v))
            pred_v = torch.flip(pred_v, dims=[2])
            predictions.append(pred_v)
            
            # Both flips
            flipped_both = torch.flip(tile_batch, dims=[2, 3])
            pred_both = torch.sigmoid(model(flipped_both))
            pred_both = torch.flip(pred_both, dims=[2, 3])
            predictions.append(pred_both)
        
        elif tta_mode == 'flip_rotate':
            # All flip variants
            for hflip in [False, True]:
                for vflip in [False, True]:
                    augmented = tile_batch
                    
                    if hflip:
                        augmented = torch.flip(augmented, dims=[3])
                    if vflip:
                        augmented = torch.flip(augmented, dims=[2])
                    
                    pred = torch.sigmoid(model(augmented))
                    
                    # Reverse augmentations
                    if vflip:
                        pred = torch.flip(pred, dims=[2])
                    if hflip:
                        pred = torch.flip(pred, dims=[3])
                    
                    predictions.append(pred)
            
            # Add 90° rotations (only for square tiles)
            if tile_batch.shape[2] == tile_batch.shape[3]:
                for k in [1, 2, 3]:  # 90°, 180°, 270°
                    rotated = torch.rot90(tile_batch, k=k, dims=[2, 3])
                    pred = torch.sigmoid(model(rotated))
                    pred = torch.rot90(pred, k=-k, dims=[2, 3])
                    predictions.append(pred)
        
        else:
            # No TTA, just original
            pred = torch.sigmoid(model(tile_batch))
            predictions.append(pred)
    
    # Average predictions
    avg_pred = torch.stack(predictions, dim=0).mean(dim=0)
    
    return avg_pred


class TTAWrapper:
    """
    Обёртка для применения TTA к модели.
    
    Пример:
        tta_model = TTAWrapper(model, mode='flip')
        predictions = tta_model(batch)
    """
    
    def __init__(
        self,
        model: torch.nn.Module,
        mode: str = 'flip',
        device: str = 'cuda'
    ):
        """
        Args:
            model: Базовая модель
            mode: Режим TTA ('flip', 'flip_rotate', 'none')
            device: Устройство
        """
        self.model = model
        self.mode = mode
        self.device = device
        self.model.to(device)
        self.model.eval()
    
    def __call__(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x: Входной тензор [B, C, H, W]
            
        Returns:
            Предсказания [B, 1, H, W]
        """
        if self.mode == 'none':
            with torch.no_grad():
                return torch.sigmoid(self.model(x.to(self.device)))
        
        return predict_with_tta(self.model, x, self.device, self.mode)
    
    def eval(self):
        self.model.eval()
        return self
    
    def to(self, device):
        self.device = device
        self.model.to(device)
        return self


def get_tta_transforms() -> List[Tuple[str, Callable, Callable]]:
    """
    Возвращает список TTA трансформаций.
    
    Returns:
        Список кортежей (name, forward_fn, backward_fn)
    """
    transforms = [
        ('original', lambda x: x, lambda x: x),
        ('hflip', lambda x: torch.flip(x, dims=[3]), lambda x: torch.flip(x, dims=[3])),
        ('vflip', lambda x: torch.flip(x, dims=[2]), lambda x: torch.flip(x, dims=[2])),
        ('hvflip', lambda x: torch.flip(x, dims=[2, 3]), lambda x: torch.flip(x, dims=[2, 3])),
    ]
    return transforms

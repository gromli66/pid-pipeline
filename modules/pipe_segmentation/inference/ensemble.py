"""
Ансамблевый инференс — объединяет предсказания двух моделей.

Стратегия OR: пиксель = труба если хотя бы одна модель так считает.
Даёт +4% Recall при том же Precision (проверено на GT).

Поддерживает любые комбинации:
- plain UNet++ + DualHeadModel
- два plain UNet++
- два DualHeadModel
"""

import torch
import numpy as np
import cv2
import time
import logging
from typing import Optional, Dict, List, Tuple
from pathlib import Path

from pipe_segmentation.model.architecture import create_model, load_checkpoint, DualHeadModel
from pipe_segmentation.inference.engine import TiledInference

logger = logging.getLogger(__name__)


class EnsembleInference:
    """
    Ансамблевый инференс двух моделей.
    
    Загружает два чекпоинта, прогоняет оба через TiledInference,
    комбинирует результаты.
    
    Пример:
        ensemble = EnsembleInference(
            checkpoint_a='model_v2.pth',
            checkpoint_b='model_v3.pth',
            dual_head_a=False,
            dual_head_b=True,
        )
        result = ensemble.predict(image, node_mask)
    """
    
    STRATEGIES = ['or', 'and', 'mean', 'weighted_mean']
    
    def __init__(
        self,
        checkpoint_a: str,
        checkpoint_b: str,
        dual_head_a: bool = False,
        dual_head_b: bool = True,
        device: str = 'cuda',
        tile_size: int = 1024,
        overlap: int = 128,
        batch_size: int = 4,
        threshold: float = 0.5,
        use_tta: bool = True,
        binarize: bool = True,
        binarize_method: str = 'adaptive',
        strategy: str = 'or',
        weight_a: float = 0.5,
        weight_b: float = 0.5,
        verbose: bool = True,
    ):
        """
        Args:
            checkpoint_a: Путь к первому чекпоинту
            checkpoint_b: Путь ко второму чекпоинту
            dual_head_a: Первая модель — DualHeadModel?
            dual_head_b: Вторая модель — DualHeadModel?
            strategy: 'or' | 'and' | 'mean' | 'weighted_mean'
            weight_a, weight_b: Веса для weighted_mean
        """
        if strategy not in self.STRATEGIES:
            raise ValueError(f"Unknown strategy '{strategy}'. Use: {self.STRATEGIES}")
        
        self.strategy = strategy
        self.threshold = threshold
        self.weight_a = weight_a
        self.weight_b = weight_b
        self.verbose = verbose
        
        if verbose:
            print("=" * 60)
            print("ENSEMBLE INFERENCE")
            print("=" * 60)
            print(f"Strategy: {strategy}")
            print(f"Device: {device}")
        
        # Загрузка модели A
        if verbose:
            print(f"\nLoading model A (dual_head={dual_head_a})...")
        model_a = create_model(dual_head=dual_head_a, verbose=False)
        load_checkpoint(checkpoint_a, model_a, device=device, verbose=verbose)
        
        self.engine_a = TiledInference(
            model=model_a,
            device=device,
            tile_size=tile_size,
            overlap=overlap,
            batch_size=batch_size,
            threshold=threshold,
            use_tta=use_tta,
            binarize=binarize,
            binarize_method=binarize_method,
        )
        
        # Загрузка модели B
        if verbose:
            print(f"\nLoading model B (dual_head={dual_head_b})...")
        model_b = create_model(dual_head=dual_head_b, verbose=False)
        load_checkpoint(checkpoint_b, model_b, device=device, verbose=verbose)
        
        self.engine_b = TiledInference(
            model=model_b,
            device=device,
            tile_size=tile_size,
            overlap=overlap,
            batch_size=batch_size,
            threshold=threshold,
            use_tta=use_tta,
            binarize=binarize,
            binarize_method=binarize_method,
        )
        
        if verbose:
            print(f"\n{'=' * 60}")
            print("Ensemble ready")
            print(f"{'=' * 60}")
    
    def predict(
        self,
        image: np.ndarray,
        node_mask: Optional[np.ndarray] = None,
        postprocess: bool = True,
        postprocess_config: Optional[dict] = None,
    ) -> Dict:
        """
        Инференс на одном изображении.
        
        Args:
            image: RGB изображение [H, W, 3]
            node_mask: Маска узлов [H, W] uint8 (0/255)
            postprocess: Применить постобработку
            postprocess_config: Конфиг постобработки
            
        Returns:
            {
                'mask': бинарная маска [H, W] uint8 (0/255),
                'mask_a': маска модели A,
                'mask_b': маска модели B,
                'n_tiles': количество тайлов,
                'time_sec': время,
                'coverage_pct': покрытие,
                'strategy': использованная стратегия,
            }
        """
        t_start = time.time()
        
        # Прогоняем обе модели БЕЗ постобработки — она будет после combine
        logger.warning("[ENSEMBLE] === Model A (inference only) ===")
        result_a = self.engine_a.predict(
            image, node_mask,
            postprocess=False,
        )
        t_a = time.time()
        logger.warning("[ENSEMBLE] Model A: %.2fs", t_a - t_start)

        logger.warning("[ENSEMBLE] === Model B (inference only) ===")
        result_b = self.engine_b.predict(
            image, node_mask,
            postprocess=False,
        )
        t_b = time.time()
        logger.warning("[ENSEMBLE] Model B: %.2fs", t_b - t_a)
        
        mask_a = result_a['mask']  # [H, W] uint8 0/255
        mask_b = result_b['mask']
        
        # Комбинируем
        ensemble_mask = self._combine(mask_a, mask_b)
        t_c = time.time()
        logger.warning("[ENSEMBLE] Combine: %.2fs", t_c - t_b)

        # Постобработка ОДИН раз на combined mask
        if postprocess:
            from pipe_segmentation.inference.postprocessing import post_process_mask
            ensemble_mask = post_process_mask(
                ensemble_mask,
                **(postprocess_config or {})
            )
            logger.warning("[ENSEMBLE] Postprocess (once): %.2fs", time.time() - t_c)
        
        total_time = time.time() - t_start
        h, w = ensemble_mask.shape
        coverage = (ensemble_mask > 0).sum() / (h * w) * 100
        logger.warning("[ENSEMBLE] TOTAL: %.2fs (strategy=%s, coverage=%.1f%%)",
                       total_time, self.strategy, coverage)
        
        return {
            'mask': ensemble_mask,
            'mask_a': mask_a,
            'mask_b': mask_b,
            'n_tiles': result_a['n_tiles'] + result_b['n_tiles'],
            'time_sec': total_time,
            'coverage_pct': coverage,
            'strategy': self.strategy,
        }
    
    def _combine(self, mask_a: np.ndarray, mask_b: np.ndarray) -> np.ndarray:
        """Комбинирует две маски согласно стратегии."""
        a = (mask_a > 127).astype(np.uint8)
        b = (mask_b > 127).astype(np.uint8)
        
        if self.strategy == 'or':
            result = a | b
        
        elif self.strategy == 'and':
            result = a & b
        
        elif self.strategy == 'mean':
            avg = (mask_a.astype(np.float32) + mask_b.astype(np.float32)) / 2
            result = (avg > self.threshold * 255).astype(np.uint8)
        
        elif self.strategy == 'weighted_mean':
            avg = (self.weight_a * mask_a.astype(np.float32) + 
                   self.weight_b * mask_b.astype(np.float32))
            avg /= (self.weight_a + self.weight_b)
            result = (avg > self.threshold * 255).astype(np.uint8)
        
        else:
            result = a | b
        
        return result * 255
    
    def free_memory(self):
        """Освобождает GPU память."""
        del self.engine_a, self.engine_b
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

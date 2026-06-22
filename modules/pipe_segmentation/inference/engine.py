"""
Движок инференса с тайлингом для изображений высокого разрешения.

TiledInference разбивает большое изображение на тайлы, обрабатывает
каждый тайл моделью, и собирает результат обратно с блендингом.

Поддерживает бинаризацию "на лету" для приведения входных данных
к формату тренировочных (трубы чёрные, фон белый).
"""

import cv2
import torch
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import time

from pipe_segmentation.config.defaults import (
    DEFAULT_TILE_SIZE,
    DEFAULT_OVERLAP,
    DEFAULT_THRESHOLD,
    DEFAULT_INFERENCE_BATCH_SIZE,
    DEFAULT_USE_TTA,
    BINARIZE_METHOD,
)
from pipe_segmentation.data.tiling import (
    calculate_tile_positions,
    extract_tile,
    create_blending_mask,
)
from pipe_segmentation.inference.preprocessing import (
    prepare_batch_from_tiles,
    preprocess_image,
)
from pipe_segmentation.inference.postprocessing import post_process_mask
from pipe_segmentation.inference.tta import predict_with_tta


class TiledInference:
    """
    Инференс с тайлингом для изображений высокого разрешения.
    
    Алгоритм:
    1. Бинаризация изображения (если включена)
    2. Разбить изображение на тайлы с перекрытием
    3. Нормализовать и подготовить батчи
    4. Получить предсказания модели (опционально с TTA)
    5. Собрать результат с блендингом в областях перекрытия
    6. Бинаризовать и постобработать маску
    
    Пример:
        inference = TiledInference(model, device='cuda', binarize=True)
        result = inference.predict(image, node_mask)
    """
    
    def __init__(
        self,
        model: torch.nn.Module,
        device: str = 'cuda',
        tile_size: int = DEFAULT_TILE_SIZE,
        overlap: int = DEFAULT_OVERLAP,
        batch_size: int = DEFAULT_INFERENCE_BATCH_SIZE,
        threshold: float = DEFAULT_THRESHOLD,
        use_tta: bool = DEFAULT_USE_TTA,
        tta_mode: str = 'flip',
        use_amp: bool = True,
        binarize: bool = True,
        binarize_method: str = BINARIZE_METHOD,
        in_channels: int = 4,
        postprocess_config: Optional[dict] = None,
    ):
        """
        Args:
            in_channels: 4 (RGB + node_mask) или 3 (RGB only).
                         Должно совпадать с in_channels модели.
            postprocess_config: dict с параметрами постобработки из YAML.
                         Используется как дефолт в predict(), если там
                         postprocess_config не передан явно.
        """
        self.model = model
        self.device = device
        self.tile_size = tile_size
        self.overlap = overlap
        self.stride = tile_size - overlap
        self.batch_size = batch_size
        self.threshold = threshold
        self.use_tta = use_tta
        self.tta_mode = tta_mode
        self.use_amp = use_amp and device == 'cuda'
        self.binarize = binarize
        self.binarize_method = binarize_method
        self.in_channels = in_channels
        self.postprocess_config = postprocess_config or {}
        # Переводим модель на устройство
        self.model.to(device)
        self.model.eval()
        
        # Создаём маску блендинга
        self.blend_mask = create_blending_mask(tile_size, overlap)
    
    def predict(
        self,
        image: np.ndarray,
        node_mask: Optional[np.ndarray] = None,
        return_probmap: bool = False,
        postprocess: bool = True,
        postprocess_config: Optional[dict] = None
    ) -> Dict:
        """
        Выполняет инференс на одном изображении.
        
        Args:
            image: RGB изображение [H, W, 3] (uint8 0-255)
            node_mask: Маска узлов [H, W] (uint8 0-255), или None для zeros
            return_probmap: Вернуть probability map
            postprocess: Применить постобработку
            postprocess_config: Параметры постобработки
            
        Returns:
            Dict с ключами:
            - 'mask': бинарная маска [H, W] (0-255)
            - 'probmap': probability map [H, W] (float 0-1) если return_probmap
            - 'n_tiles': количество обработанных тайлов
            - 'time_sec': время инференса
            - 'binarized': True если была применена бинаризация
        """
        start_time = time.time()
        
        height, width = image.shape[:2]
        
        # Бинаризация изображения "на лету"
        if self.binarize:
            image = preprocess_image(
                image, 
                binarize=True, 
                binarize_method=self.binarize_method
            )
        
        # Если node_mask не указан — используем zeros
        if node_mask is None:
            node_mask = np.zeros((height, width), dtype=np.uint8)
        
        # Вычисляем позиции тайлов
        positions = calculate_tile_positions(
            height, width,
            self.tile_size, self.stride
        )
        
        # Создаём буферы для накопления результатов
        prob_accumulator = np.zeros((height, width), dtype=np.float32)
        weight_accumulator = np.zeros((height, width), dtype=np.float32)
        
        # Обрабатываем батчами
        for batch_start in range(0, len(positions), self.batch_size):
            batch_end = min(batch_start + self.batch_size, len(positions))
            batch_positions = positions[batch_start:batch_end]
            
            # Извлекаем тайлы
            rgb_tiles = []
            node_tiles = []
            
            for y, x in batch_positions:
                rgb_tile = extract_tile(image, y, x, self.tile_size)
                rgb_tiles.append(rgb_tile)
                if self.in_channels == 4:
                    node_tile = extract_tile(node_mask, y, x, self.tile_size)
                    node_tiles.append(node_tile)
            
            # Подготавливаем батч (бинаризация уже сделана выше)
            if self.in_channels == 4:
                batch_tensor = prepare_batch_from_tiles(
                    rgb_tiles, node_tiles, binarize=False
                )
            else:
                from pipe_segmentation.inference.preprocessing import (
                    prepare_batch_from_tiles_rgb,
                )
                batch_tensor = prepare_batch_from_tiles_rgb(
                    rgb_tiles, binarize=False
                )
            batch_tensor = batch_tensor.to(self.device)
            
            # Инференс
            with torch.no_grad():
                if self.use_amp:
                    with torch.cuda.amp.autocast():
                        predictions = self._run_inference(batch_tensor)
                else:
                    predictions = self._run_inference(batch_tensor)
            
            # Собираем результаты
            predictions_np = predictions.cpu().numpy()
            
            for i, (y, x) in enumerate(batch_positions):
                pred_tile = predictions_np[i, 0]  # [H, W]
                
                # Добавляем с весами блендинга
                y_end = min(y + self.tile_size, height)
                x_end = min(x + self.tile_size, width)
                
                tile_h = y_end - y
                tile_w = x_end - x
                
                prob_accumulator[y:y_end, x:x_end] += \
                    pred_tile[:tile_h, :tile_w] * self.blend_mask[:tile_h, :tile_w]
                weight_accumulator[y:y_end, x:x_end] += \
                    self.blend_mask[:tile_h, :tile_w]
        
        # Нормализуем по весам
        weight_accumulator = np.maximum(weight_accumulator, 1e-8)
        prob_map = prob_accumulator / weight_accumulator
        
        # Бинаризация
        binary_mask = (prob_map > self.threshold).astype(np.uint8) * 255
        
        # Постобработка
        if postprocess:
            cfg_pp = postprocess_config if postprocess_config is not None else self.postprocess_config
            binary_mask = post_process_mask(
                binary_mask,
                **(cfg_pp or {})
            )
        
        elapsed_time = time.time() - start_time
        
        result = {
            'mask': binary_mask,
            'n_tiles': len(positions),
            'time_sec': elapsed_time,
            'coverage_pct': np.mean(binary_mask > 0) * 100,
            'mean_confidence': float(prob_map[binary_mask > 0].mean()) if binary_mask.max() > 0 else 0.0,
            'binarized': self.binarize
        }
        
        if return_probmap:
            result['probmap'] = prob_map
        
        return result
    
    def _run_inference(self, batch: torch.Tensor) -> torch.Tensor:
        """
        Запускает инференс на батче.
        
        Args:
            batch: Входной тензор [B, 4, H, W]
            
        Returns:
            Предсказания [B, 1, H, W] (probabilities)
        """
        if self.use_tta:
            return predict_with_tta(
                self.model, batch,
                self.device, self.tta_mode
            )
        else:
            return torch.sigmoid(self.model(batch))


def run_inference(
    model: torch.nn.Module,
    image: np.ndarray,
    node_mask: Optional[np.ndarray] = None,
    device: str = 'cuda',
    tile_size: int = DEFAULT_TILE_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    batch_size: int = DEFAULT_INFERENCE_BATCH_SIZE,
    threshold: float = DEFAULT_THRESHOLD,
    use_tta: bool = DEFAULT_USE_TTA,
    postprocess: bool = True,
    binarize: bool = True,
    binarize_method: str = BINARIZE_METHOD
) -> Dict:
    """
    Функция-обёртка для быстрого инференса.
    
    Args:
        model: Модель
        image: RGB изображение
        node_mask: Маска узлов (опционально)
        device: Устройство
        tile_size: Размер тайла
        overlap: Перекрытие
        batch_size: Размер батча
        threshold: Порог
        use_tta: Использовать TTA
        postprocess: Постобработка
        binarize: Бинаризовать изображение
        binarize_method: Метод бинаризации
        
    Returns:
        Результат инференса (см. TiledInference.predict)
    """
    inference = TiledInference(
        model=model,
        device=device,
        tile_size=tile_size,
        overlap=overlap,
        batch_size=batch_size,
        threshold=threshold,
        use_tta=use_tta,
        binarize=binarize,
        binarize_method=binarize_method
    )
    
    return inference.predict(
        image=image,
        node_mask=node_mask,
        postprocess=postprocess
    )

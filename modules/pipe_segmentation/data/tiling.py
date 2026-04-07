"""
Тайлинг изображений и стратифицированный split.

Разбивает большие P&ID диаграммы на тайлы для обучения и инференса.
Поддерживает бинаризацию изображений (трубы чёрные, фон белый).
"""

import cv2
import numpy as np
import random
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Set
from collections import defaultdict
from tqdm import tqdm

from pipe_segmentation.config.defaults import (
    DEFAULT_TILE_SIZE,
    DEFAULT_OVERLAP,
    DEFAULT_MIN_PIPE_PIXELS,
    DEFAULT_MIN_NODE_PIXELS,
    DEFAULT_EMPTY_THRESHOLD,
    DEFAULT_RANDOM_SEED,
    MASKS_PIPES_DIR,
    MASKS_NODES_DIR,
    SPLIT_IMAGES_DIR,
    SPLIT_PIPE_MASKS_DIR,
    SPLIT_NODE_MASKS_DIR,
    BINARIZE_METHOD,
)
from pipe_segmentation.utils.io import (
    load_image,
    save_image,
    save_json,
    save_csv,
    save_file_list,
    get_image_files,
    ensure_dir,
    find_matching_file,
    binarize_to_black_white,
)


def calculate_tile_positions(
    image_height: int,
    image_width: int,
    tile_size: int,
    stride: int
) -> List[Tuple[int, int]]:
    """
    Вычисляет позиции тайлов методом "start from edges".
    
    Все тайлы имеют точный размер tile_size × tile_size.
    Последние тайлы начинаются от края изображения.
    
    Args:
        image_height: Высота изображения
        image_width: Ширина изображения
        tile_size: Размер тайла
        stride: Шаг между тайлами
        
    Returns:
        Список позиций (y, x) для верхнего левого угла каждого тайла
    """
    positions = []
    
    # Позиции по Y
    y_positions = []
    y = 0
    while y + tile_size <= image_height:
        y_positions.append(y)
        y += stride
    
    # Добавить последний тайл от нижнего края
    if y_positions and y_positions[-1] + tile_size < image_height:
        y_positions.append(image_height - tile_size)
    elif not y_positions and image_height >= tile_size:
        y_positions.append(0)
    # [FIX 6.1] Изображения меньше tile_size — один тайл от (0,0)
    elif not y_positions and image_height < tile_size:
        y_positions.append(0)
    
    # Позиции по X
    x_positions = []
    x = 0
    while x + tile_size <= image_width:
        x_positions.append(x)
        x += stride
    
    # Добавить последний тайл от правого края
    if x_positions and x_positions[-1] + tile_size < image_width:
        x_positions.append(image_width - tile_size)
    elif not x_positions and image_width >= tile_size:
        x_positions.append(0)
    # [FIX 6.1] Изображения меньше tile_size — один тайл от (0,0)
    elif not x_positions and image_width < tile_size:
        x_positions.append(0)
    
    # Все комбинации
    for y in y_positions:
        for x in x_positions:
            positions.append((y, x))
    
    return positions


def extract_tile(
    image: np.ndarray,
    y: int,
    x: int,
    tile_size: int
) -> np.ndarray:
    """
    Извлекает тайл из изображения.

    [FIX 6.1] Если изображение меньше tile_size — паддинг белым (255).
    """
    h, w = image.shape[:2]
    y_end = min(y + tile_size, h)
    x_end = min(x + tile_size, w)
    tile = image[y:y_end, x:x_end].copy()

    # Паддинг если тайл меньше tile_size
    th, tw = tile.shape[:2]
    if th < tile_size or tw < tile_size:
        if len(image.shape) == 3:
            padded = np.full((tile_size, tile_size, image.shape[2]), 255, dtype=image.dtype)
        else:
            padded = np.full((tile_size, tile_size), 255 if image.max() > 1 else 0, dtype=image.dtype)
        padded[:th, :tw] = tile
        tile = padded

    return tile


def create_blending_mask(tile_size: int, overlap: int) -> np.ndarray:
    """
    Создаёт маску блендинга для сборки тайлов.
    
    [FIX 8.1] Гауссов блендинг вместо линейного — более гладкие
    переходы на границах, убирает видимые seams на длинных трубах.
    
    Args:
        tile_size: Размер тайла
        overlap: Ширина области перекрытия
        
    Returns:
        Маска весов [tile_size, tile_size]
    """
    if overlap == 0:
        return np.ones((tile_size, tile_size), dtype=np.float32)
    
    # Гауссова маска: sigma подобрана чтобы вес на краю был ~0.1
    sigma = tile_size / 4.0
    center = tile_size / 2.0
    
    y = np.arange(tile_size, dtype=np.float32) - center
    x = np.arange(tile_size, dtype=np.float32) - center
    
    yy, xx = np.meshgrid(y, x, indexing='ij')
    mask = np.exp(-(xx ** 2 + yy ** 2) / (2 * sigma ** 2))
    
    # Нормализуем: центр = 1.0
    mask = mask / mask.max()
    
    # Минимальный вес чтобы не было нулей
    mask = np.clip(mask, 0.05, 1.0)
    
    return mask


def is_tile_empty(
    tile: np.ndarray,
    threshold: float = DEFAULT_EMPTY_THRESHOLD
) -> bool:
    """
    Проверяет, является ли тайл пустым (белый фон).
    
    Args:
        tile: Тайл изображения
        threshold: Порог доли белых пикселей
        
    Returns:
        True если тайл пустой
    """
    if len(tile.shape) == 3:
        white_mask = np.all(tile > 240, axis=2)
    else:
        white_mask = tile > 240
    
    return np.mean(white_mask) > threshold


def should_keep_tile(
    pipe_tile: np.ndarray,
    node_tile: np.ndarray,
    empty_threshold: float = DEFAULT_EMPTY_THRESHOLD,
    min_pipe_pixels: int = DEFAULT_MIN_PIPE_PIXELS,
    min_node_pixels: int = DEFAULT_MIN_NODE_PIXELS
) -> Tuple[bool, str]:
    """
    Определяет, нужно ли сохранять тайл.
    
    Args:
        pipe_tile: Тайл маски труб
        node_tile: Тайл маски узлов
        empty_threshold: Порог пустого тайла
        min_pipe_pixels: Минимум пикселей труб
        min_node_pixels: Минимум пикселей узлов
        
    Returns:
        (keep, reason)
    """
    # Проверка на пустоту (по маске труб)
    if is_tile_empty(pipe_tile, empty_threshold):
        return False, "empty"
    
    pipe_pixels = np.count_nonzero(pipe_tile)
    node_pixels = np.count_nonzero(node_tile)
    
    if pipe_pixels >= min_pipe_pixels or node_pixels >= min_node_pixels:
        return True, f"pipes={pipe_pixels}, nodes={node_pixels}"
    
    return False, f"too_few: pipes={pipe_pixels}, nodes={node_pixels}"


def get_mask_coverage(mask: np.ndarray) -> float:
    """Возвращает процент ненулевых пикселей."""
    if mask.size == 0:
        return 0.0
    return np.count_nonzero(mask) / mask.size * 100


class ImageTiler:
    """
    Класс для тайлинга датасета с поддержкой фиксированного test split.
    
    Поддерживает:
    - Бинаризацию изображений
    - Готовые маски из папок или из COCO
    - Стратифицированный split
    
    Пример:
        tiler = ImageTiler(
            images_dir='./images',
            pipe_masks_dir='./masks/pipes',
            node_masks_dir='./masks/nodes',
            output_dir='./tiles',
            tile_size=1024,
            overlap=128,
            binarize=True
        )
        tiler.process(
            split_ratios=(0.7, 0.15, 0.15),
            test_files=['img1.png', 'img2.png']
        )
    """
    
    def __init__(
        self,
        images_dir: str,
        pipe_masks_dir: str,
        node_masks_dir: str,
        output_dir: str,
        tile_size: int = DEFAULT_TILE_SIZE,
        overlap: int = DEFAULT_OVERLAP,
        min_pipe_pixels: int = DEFAULT_MIN_PIPE_PIXELS,
        min_node_pixels: int = DEFAULT_MIN_NODE_PIXELS,
        binarize: bool = True,
        binarize_method: str = BINARIZE_METHOD
    ):
        """
        Args:
            images_dir: Папка с исходными изображениями
            pipe_masks_dir: Папка с масками труб
            node_masks_dir: Папка с масками узлов
            output_dir: Выходная директория
            tile_size: Размер тайла
            overlap: Перекрытие
            min_pipe_pixels: Мин. пикселей труб для сохранения
            min_node_pixels: Мин. пикселей узлов
            binarize: Бинаризовать изображения (трубы чёрные, фон белый)
            binarize_method: Метод бинаризации
        """
        self.images_dir = Path(images_dir)
        self.pipe_masks_dir = Path(pipe_masks_dir)
        self.node_masks_dir = Path(node_masks_dir)
        self.output_dir = Path(output_dir)
        self.tile_size = tile_size
        self.overlap = overlap
        self.stride = tile_size - overlap
        self.min_pipe_pixels = min_pipe_pixels
        self.min_node_pixels = min_node_pixels
        self.binarize = binarize
        self.binarize_method = binarize_method
    
    def process(
        self,
        split_ratios: Tuple[float, ...] = (0.7, 0.15, 0.15),
        test_files: Optional[List[str]] = None,
        seed: int = DEFAULT_RANDOM_SEED,
        verbose: bool = True
    ) -> Dict:
        """
        Выполняет полный pipeline: split + tiling.
        
        Args:
            split_ratios: Пропорции split (2 или 3 значения)
            test_files: Фиксированный список тестовых файлов
            seed: Random seed
            verbose: Выводить прогресс
            
        Returns:
            Статистика обработки
        """
        random.seed(seed)
        np.random.seed(seed)
        
        # Получаем список изображений
        image_files = get_image_files(self.images_dir)
        
        if verbose:
            print(f"\n{'='*60}")
            print("IMAGE TILING")
            print(f"{'='*60}")
            print(f"Images found: {len(image_files)}")
            print(f"Tile size: {self.tile_size}×{self.tile_size}")
            print(f"Overlap: {self.overlap}")
            print(f"Stride: {self.stride}")
            print(f"Binarize: {self.binarize}")
            if self.binarize:
                print(f"Binarize method: {self.binarize_method}")
        
        # Выполняем split
        splits = self._split_files(
            image_files,
            split_ratios,
            test_files,
            seed,
            verbose
        )
        
        # Тайлим каждый split
        results = {
            'split_ratios': split_ratios,
            'test_files_fixed': test_files is not None,
            'seed': seed,
            'binarize': self.binarize,
            'binarize_method': self.binarize_method if self.binarize else None,
            'splits': {}
        }
        
        for split_name, files in splits.items():
            if verbose:
                print(f"\n--- {split_name.upper()} ({len(files)} images) ---")
            
            if split_name == 'test':
                # Test: сохраняем ОРИГИНАЛЫ, НЕ тайлы.
                # test/infer работают с оригиналами и тайлят на лету.
                split_stats = self._save_test_originals(files, verbose)
            else:
                # Train/Val: тайлим для DataLoader
                split_stats = self._tile_split(split_name, files, verbose)
            
            results['splits'][split_name] = split_stats
        
        # Сохраняем статистику
        self._save_stats(results, verbose)
        
        return results
    
    def _split_files(
        self,
        image_files: List[Path],
        split_ratios: Tuple[float, ...],
        test_files: Optional[List[str]],
        seed: int,
        verbose: bool
    ) -> Dict[str, List[Path]]:
        """
        Разбивает файлы на train/val/test.
        
        Если test_files указан:
        - Эти файлы идут в test
        - Остальные делятся между train/val
        """
        # Собираем информацию о coverage для стратификации
        file_info = []
        for img_path in image_files:
            pipe_mask_path = find_matching_file(
                img_path.name,
                self.pipe_masks_dir,
                ['.png']
            )
            
            if pipe_mask_path:
                mask = load_image(pipe_mask_path, grayscale=True)
                coverage = get_mask_coverage(mask) if mask is not None else 0
            else:
                coverage = 0
            
            file_info.append({
                'path': img_path,
                'name': img_path.name,
                'coverage': coverage
            })
        
        # Определяем test файлы
        test_set: Set[str] = set()
        if test_files:
            test_set = set(test_files)
            if verbose:
                print(f"Fixed test files: {len(test_set)}")
        
        # Разделяем на test и остальные
        test_info = [f for f in file_info if f['name'] in test_set]
        other_info = [f for f in file_info if f['name'] not in test_set]
        
        # Стратифицированный split остальных
        if len(split_ratios) == 3 and not test_files:
            # train/val/test
            train_info, val_info, auto_test_info = self._stratified_split_3way(
                other_info, split_ratios, seed
            )
            test_info = auto_test_info
        elif len(split_ratios) == 2 or test_files:
            # train/val (test уже определён)
            if test_files:
                # Пересчитываем ratios для оставшихся
                train_ratio = split_ratios[0] if len(split_ratios) >= 1 else 0.8
                val_ratio = split_ratios[1] if len(split_ratios) >= 2 else 0.2
                # Нормализуем
                total = train_ratio + val_ratio
                train_ratio /= total
                val_ratio /= total
                ratios = (train_ratio, val_ratio)
            else:
                ratios = split_ratios[:2]
            
            train_info, val_info = self._stratified_split_2way(
                other_info, ratios, seed
            )
        else:
            raise ValueError(f"Invalid split_ratios: {split_ratios}")
        
        splits = {
            'train': [f['path'] for f in train_info],
            'val': [f['path'] for f in val_info],
        }
        
        if test_info:
            splits['test'] = [f['path'] for f in test_info]
        
        if verbose:
            for name, files in splits.items():
                print(f"  {name}: {len(files)} images")
        
        return splits
    
    def _stratified_split_2way(
        self,
        file_info: List[Dict],
        ratios: Tuple[float, float],
        seed: int
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Стратифицированный split на 2 части.
        
        [FIX 6.2] Больше бинов, защита от малых датасетов
        """
        sorted_info = sorted(file_info, key=lambda x: x['coverage'])
        # Адаптивное кол-во бинов: min(5, n//2) чтобы в каждом бине было ≥2 файла
        n_bins = max(1, min(5, len(sorted_info) // 2))
        bins = [[] for _ in range(n_bins)]
        
        for i, info in enumerate(sorted_info):
            bins[i % n_bins].append(info)
        
        train_info, val_info = [], []
        
        for bin_items in bins:
            random.shuffle(bin_items)
            n = len(bin_items)
            n_train = max(1, int(n * ratios[0])) if n > 1 else 1
            
            train_info.extend(bin_items[:n_train])
            val_info.extend(bin_items[n_train:])
        
        # Гарантируем что val не пустой
        if not val_info and len(train_info) > 1:
            val_info.append(train_info.pop())
        
        return train_info, val_info
    
    def _stratified_split_3way(
        self,
        file_info: List[Dict],
        ratios: Tuple[float, float, float],
        seed: int
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        """
        Стратифицированный split на 3 части.
        
        [FIX 6.2] Больше бинов, защита от малых датасетов
        """
        sorted_info = sorted(file_info, key=lambda x: x['coverage'])
        n_bins = max(1, min(5, len(sorted_info) // 3))
        bins = [[] for _ in range(n_bins)]
        
        for i, info in enumerate(sorted_info):
            bins[i % n_bins].append(info)
        
        train_info, val_info, test_info = [], [], []
        
        for bin_items in bins:
            random.shuffle(bin_items)
            n = len(bin_items)
            n_train = max(1, int(n * ratios[0]))
            n_val = max(0, int(n * ratios[1]))
            
            train_info.extend(bin_items[:n_train])
            val_info.extend(bin_items[n_train:n_train + n_val])
            test_info.extend(bin_items[n_train + n_val:])
        
        # Гарантируем что val и test не пустые
        if not val_info and len(train_info) > 2:
            val_info.append(train_info.pop())
        if not test_info and len(train_info) > 2:
            test_info.append(train_info.pop())
        
        return train_info, val_info, test_info
    
    def _save_test_originals(
        self,
        image_files: List[Path],
        verbose: bool
    ) -> Dict:
        """
        Сохраняет ОРИГИНАЛЬНЫЕ изображения и маски для test split.
        
        НЕ нарезает на тайлы — test/infer работают с оригиналами
        и тайлят на лету через TiledInference.
        
        Создаёт:
            test/images/       — оригинальные изображения (бинаризованные если включено)
            test/pipe_masks/   — GT маски труб полного разрешения
            test/node_masks/   — маски узлов полного разрешения
            test/test_originals.txt — список файлов
        """
        import shutil
        
        output_test_dir = self.output_dir / 'test'
        images_out = ensure_dir(output_test_dir / SPLIT_IMAGES_DIR)
        pipe_out = ensure_dir(output_test_dir / SPLIT_PIPE_MASKS_DIR)
        node_out = ensure_dir(output_test_dir / SPLIT_NODE_MASKS_DIR)
        
        stats = {
            'n_images': len(image_files),
            'n_tiles_total': 0,
            'n_tiles_kept': len(image_files),
            'n_tiles_rejected': 0,
            'tiles': [],
            'mode': 'originals',  # отличает от тайлированных split'ов
        }
        
        filenames = []
        
        for img_path in image_files:
            filename = img_path.name
            filenames.append(filename)
            
            # Копируем изображение БЕЗ бинаризации — inference бинаризует на лету
            # (двойная бинаризация убивает чёрные линии)
            shutil.copy2(img_path, images_out / filename)
            
            # Копируем маски
            pipe_mask_path = find_matching_file(filename, self.pipe_masks_dir, ['.png'])
            node_mask_path = find_matching_file(filename, self.node_masks_dir, ['.png'])
            
            if pipe_mask_path:
                shutil.copy2(pipe_mask_path, pipe_out / filename)
            if node_mask_path:
                shutil.copy2(node_mask_path, node_out / filename)
            
            stats['tiles'].append({
                'filename': filename,
                'source': filename,
                'mode': 'original',
            })
        
        # Сохраняем список оригинальных тестовых файлов
        save_file_list(filenames, output_test_dir / 'test_originals.txt')
        
        if verbose:
            print(f"  Saved {len(image_files)} original images (NOT tiled)")
            print(f"  test/images/, test/pipe_masks/, test/node_masks/")
            print(f"  test/test_originals.txt")
        
        return stats
    
    def _tile_split(
        self,
        split_name: str,
        image_files: List[Path],
        verbose: bool
    ) -> Dict:
        """Тайлит изображения одного split."""
        output_split_dir = self.output_dir / split_name
        images_out = ensure_dir(output_split_dir / SPLIT_IMAGES_DIR)
        pipe_out = ensure_dir(output_split_dir / SPLIT_PIPE_MASKS_DIR)
        node_out = ensure_dir(output_split_dir / SPLIT_NODE_MASKS_DIR)
        
        stats = {
            'n_images': len(image_files),
            'n_tiles_total': 0,
            'n_tiles_kept': 0,
            'n_tiles_rejected': 0,
            'tiles': []
        }
        
        iterator = tqdm(image_files, desc=f"Tiling {split_name}") if verbose else image_files
        
        for img_path in iterator:
            tile_stats = self._tile_single_image(
                img_path,
                images_out,
                pipe_out,
                node_out
            )
            
            stats['n_tiles_total'] += tile_stats['total']
            stats['n_tiles_kept'] += tile_stats['kept']
            stats['n_tiles_rejected'] += tile_stats['rejected']
            stats['tiles'].extend(tile_stats['tiles'])
        
        # Сохраняем список файлов
        save_file_list(
            [f['filename'] for f in stats['tiles']],
            output_split_dir / f'{split_name}_files.txt'
        )
        
        if verbose:
            keep_rate = stats['n_tiles_kept'] / max(stats['n_tiles_total'], 1) * 100
            print(f"  Tiles: {stats['n_tiles_kept']}/{stats['n_tiles_total']} kept ({keep_rate:.1f}%)")
        
        return stats
    
    def _tile_single_image(
        self,
        img_path: Path,
        images_out: Path,
        pipe_out: Path,
        node_out: Path
    ) -> Dict:
        """
        Тайлит одно изображение.
        
        [PERF] Оптимизации:
          - Фильтрация ДО копирования (проверяем slice без .copy())
          - np.ascontiguousarray вместо .copy() (быстрее для cv2)
          - PNG compression=1 (быстрее на ~20%, размер +5%)
        """
        stats = {
            'filename': img_path.name,
            'total': 0,
            'kept': 0,
            'rejected': 0,
            'tiles': []
        }
        
        # Загрузка изображения
        image = load_image(img_path, rgb=True)
        if image is None:
            return stats
        
        # Бинаризация если включена
        if self.binarize:
            binary = binarize_to_black_white(image, method=self.binarize_method)
            image = cv2.cvtColor(binary, cv2.COLOR_GRAY2RGB)
        
        # Загрузка масок
        pipe_mask_path = find_matching_file(img_path.name, self.pipe_masks_dir, ['.png'])
        node_mask_path = find_matching_file(img_path.name, self.node_masks_dir, ['.png'])
        
        pipe_mask = load_image(pipe_mask_path, grayscale=True) if pipe_mask_path else \
                    np.zeros(image.shape[:2], dtype=np.uint8)
        node_mask = load_image(node_mask_path, grayscale=True) if node_mask_path else \
                    np.zeros(image.shape[:2], dtype=np.uint8)
        
        # Позиции тайлов
        positions = calculate_tile_positions(
            image.shape[0], image.shape[1],
            self.tile_size, self.stride
        )
        
        base_name = img_path.stem
        tile_idx = 0
        h, w = image.shape[:2]
        needs_padding = h < self.tile_size or w < self.tile_size
        
        # [PERF] PNG compression=1 (быстрее дефолта на бинарных данных)
        png_params = [cv2.IMWRITE_PNG_COMPRESSION, 1]
        
        for y, x in positions:
            stats['total'] += 1
            
            # [PERF] Фильтрация ДО копирования — проверяем slice без аллокации
            y_end = min(y + self.tile_size, h)
            x_end = min(x + self.tile_size, w)
            pipe_slice = pipe_mask[y:y_end, x:x_end]
            node_slice = node_mask[y:y_end, x:x_end]
            
            pipe_pixels = np.count_nonzero(pipe_slice)
            node_pixels = np.count_nonzero(node_slice)
            
            if pipe_pixels < self.min_pipe_pixels and node_pixels < self.min_node_pixels:
                stats['rejected'] += 1
                continue
            
            # Извлекаем тайл только для сохраняемых
            if needs_padding:
                img_tile = extract_tile(image, y, x, self.tile_size)
                pipe_tile = extract_tile(pipe_mask, y, x, self.tile_size)
                node_tile = extract_tile(node_mask, y, x, self.tile_size)
            else:
                # [PERF] contiguous вместо .copy() — быстрее для cv2.imwrite
                img_tile = np.ascontiguousarray(image[y:y+self.tile_size, x:x+self.tile_size])
                pipe_tile = np.ascontiguousarray(pipe_mask[y:y+self.tile_size, x:x+self.tile_size])
                node_tile = np.ascontiguousarray(node_mask[y:y+self.tile_size, x:x+self.tile_size])
            
            tile_name = f"{base_name}_tile_{tile_idx:03d}.png"
            
            # Сохраняем (RGB→BGR для cv2)
            cv2.imwrite(str(images_out / tile_name),
                        cv2.cvtColor(img_tile, cv2.COLOR_RGB2BGR), png_params)
            cv2.imwrite(str(pipe_out / tile_name), pipe_tile, png_params)
            cv2.imwrite(str(node_out / tile_name), node_tile, png_params)
            
            stats['kept'] += 1
            stats['tiles'].append({
                'filename': tile_name,
                'source': img_path.name,
                'y': y,
                'x': x,
                'pipe_coverage': pipe_pixels / (self.tile_size ** 2) * 100,
                'node_coverage': node_pixels / (self.tile_size ** 2) * 100
            })
            
            tile_idx += 1
        
        return stats
    
    def _save_stats(self, results: Dict, verbose: bool):
        """Сохраняет статистику в JSON и CSV."""
        # JSON с полной информацией
        save_json(results, self.output_dir / 'tiling_results.json')
        
        # CSV со статистикой по splits
        csv_data = []
        for split_name, split_stats in results['splits'].items():
            csv_data.append({
                'split': split_name,
                'n_images': split_stats['n_images'],
                'n_tiles_total': split_stats['n_tiles_total'],
                'n_tiles_kept': split_stats['n_tiles_kept'],
                'n_tiles_rejected': split_stats['n_tiles_rejected'],
                'keep_rate_pct': split_stats['n_tiles_kept'] / max(split_stats['n_tiles_total'], 1) * 100
            })
        
        save_csv(csv_data, self.output_dir / 'prepare_stats.csv')
        
        if verbose:
            print(f"\nStats saved to: {self.output_dir}")

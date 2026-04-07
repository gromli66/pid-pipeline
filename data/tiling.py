"""
Тайлинг изображений для P&ID Node Detection.

НАЗНАЧЕНИЕ:
----------
Этот модуль выполняет нарезку больших P&ID схем на тайлы для обучения:
1. Нарезка изображений с перекрытием
2. Трансформация bbox аннотаций
3. Создание forbidden масок (области где нельзя вставлять объекты при CPA)
4. Padding тайлов на границах

ПОЧЕМУ ТАЙЛИНГ НУЖЕН:
--------------------
1. P&ID схемы большие (~4900x3500 при 300 DPI)
2. YOLO работает на изображениях 640-1280 px
3. Объекты (оборудование) относительно мелкие на схеме
4. Тайлинг позволяет сохранить детали и не уменьшать изображение

ПАРАМЕТРЫ ТАЙЛИНГА:
------------------
- tile_size=1280: Оптимально для YOLOv8
- overlap=0.25: 25% перекрытия предотвращает потерю объектов на границах
- min_visible_ratio=0.6: Объекты видимые менее чем на 60% отбрасываются

FORBIDDEN МАСКИ:
---------------
Создаются из масок труб и аннотаций. Показывают области где нельзя
вставлять объекты при Copy-Paste аугментации:
- Поверх труб - объект будет выглядеть неестественно
- Поверх текстовых аннотаций - нарушит разметку

ИСПОЛЬЗОВАНИЕ:
-------------
    from pid_node_detection.data.tiling import tile_dataset
    
    tile_dataset(
        images_dir=Path("./trainval/images"),
        labels_dir=Path("./trainval/labels"),
        output_dir=Path("./tiles"),
        pipe_masks_dir=Path("./masks/pipes"),
        annotation_masks_dir=Path("./masks/annotations"),
        tile_size=1280,
        overlap=0.25
    )
"""

import cv2
import numpy as np
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from tqdm import tqdm


def parse_yolo_labels(
    label_path: Path,
    img_width: int,
    img_height: int
) -> List[Dict]:
    """
    Парсинг YOLO аннотаций в пиксельные координаты.
    
    YOLO формат: class_id x_center y_center width height (все нормализованы 0-1)
    
    Args:
        label_path: Путь к файлу аннотаций
        img_width: Ширина изображения
        img_height: Высота изображения
        
    Returns:
        Список объектов: [{class_id, x_center, y_center, width, height}]
        Координаты в пикселях.
        
    Example:
        >>> objects = parse_yolo_labels(Path("labels/img.txt"), 4900, 3500)
        >>> print(objects[0])
        {"class_id": 0, "x_center": 1200.5, "y_center": 800.0, ...}
    """
    if not label_path.exists():
        return []
    
    objects = []
    
    with open(label_path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) < 5:
                continue
            
            try:
                class_id = int(parts[0])
                x_center = float(parts[1]) * img_width
                y_center = float(parts[2]) * img_height
                width = float(parts[3]) * img_width
                height = float(parts[4]) * img_height
                
                objects.append({
                    "class_id": class_id,
                    "x_center": x_center,
                    "y_center": y_center,
                    "width": width,
                    "height": height
                })
            except (ValueError, IndexError):
                continue
    
    return objects


def calculate_intersection(
    obj: Dict,
    tile_x1: int, tile_y1: int,
    tile_x2: int, tile_y2: int
) -> Tuple[float, float]:
    """
    Вычислить площадь пересечения объекта с тайлом.
    
    Args:
        obj: Объект с координатами {x_center, y_center, width, height}
        tile_x1, tile_y1, tile_x2, tile_y2: Координаты тайла
        
    Returns:
        Tuple (intersection_area, object_area)
    """
    # Bbox объекта
    obj_x1 = obj["x_center"] - obj["width"] / 2
    obj_y1 = obj["y_center"] - obj["height"] / 2
    obj_x2 = obj["x_center"] + obj["width"] / 2
    obj_y2 = obj["y_center"] + obj["height"] / 2
    
    # Пересечение
    inter_x1 = max(obj_x1, tile_x1)
    inter_y1 = max(obj_y1, tile_y1)
    inter_x2 = min(obj_x2, tile_x2)
    inter_y2 = min(obj_y2, tile_y2)
    
    if inter_x1 >= inter_x2 or inter_y1 >= inter_y2:
        return 0.0, obj["width"] * obj["height"]
    
    inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    obj_area = obj["width"] * obj["height"]
    
    return inter_area, obj_area


def clip_bbox_to_tile(
    obj: Dict,
    tile_x1: int, tile_y1: int,
    tile_size: int
) -> Optional[Dict]:
    """
    Обрезать bbox объекта по границам тайла и нормализовать.
    
    Преобразует координаты объекта в локальную систему тайла
    и нормализует относительно tile_size.
    
    Args:
        obj: Объект с пиксельными координатами
        tile_x1, tile_y1: Верхний левый угол тайла
        tile_size: Размер тайла
        
    Returns:
        Объект с нормализованными координатами или None если полностью вне тайла
    """
    # Bbox объекта
    obj_x1 = obj["x_center"] - obj["width"] / 2
    obj_y1 = obj["y_center"] - obj["height"] / 2
    obj_x2 = obj["x_center"] + obj["width"] / 2
    obj_y2 = obj["y_center"] + obj["height"] / 2
    
    tile_x2 = tile_x1 + tile_size
    tile_y2 = tile_y1 + tile_size
    
    # Обрезка
    clipped_x1 = max(tile_x1, obj_x1)
    clipped_y1 = max(tile_y1, obj_y1)
    clipped_x2 = min(tile_x2, obj_x2)
    clipped_y2 = min(tile_y2, obj_y2)
    
    if clipped_x1 >= clipped_x2 or clipped_y1 >= clipped_y2:
        return None
    
    # Локальные координаты
    local_x1 = clipped_x1 - tile_x1
    local_y1 = clipped_y1 - tile_y1
    local_x2 = clipped_x2 - tile_x1
    local_y2 = clipped_y2 - tile_y1
    
    # Нормализация
    new_width = (local_x2 - local_x1) / tile_size
    new_height = (local_y2 - local_y1) / tile_size
    new_x_center = (local_x1 + local_x2) / 2 / tile_size
    new_y_center = (local_y1 + local_y2) / 2 / tile_size
    
    return {
        "class_id": obj["class_id"],
        "x_center": new_x_center,
        "y_center": new_y_center,
        "width": new_width,
        "height": new_height
    }


def find_mask_file(
    scheme_name: str,
    mask_dir: Path,
    suffix: str = ""
) -> Optional[Path]:
    """
    Найти файл маски для схемы.
    
    Пробует найти маску по точному имени, а также со снятым
    префиксом "old_"/"new_" (добавляется при finetune preprocessing).
    
    Args:
        scheme_name: Имя схемы (без расширения)
        mask_dir: Директория с масками
        suffix: Суффикс в имени файла (например, "_mask")
        
    Returns:
        Path к файлу маски или None
    """
    # Варианты имён: точное + без префикса old_/new_
    name_variants = [scheme_name]
    for prefix in ["old_", "new_"]:
        if scheme_name.startswith(prefix):
            name_variants.append(scheme_name[len(prefix):])
    
    for name in name_variants:
        for ext in [".png", ".jpg"]:
            path = mask_dir / f"{name}{suffix}{ext}"
            if path.exists():
                return path
    return None


def load_and_combine_masks(
    scheme_name: str,
    pipe_masks_dir: Optional[Path],
    annotation_masks_dir: Optional[Path],
    dilation_kernel: int = 7
) -> Optional[np.ndarray]:
    """
    Загрузить и объединить маски труб и аннотаций в forbidden маску.
    
    Forbidden маска показывает области где нельзя вставлять объекты при CPA:
    - Поверх труб
    - Поверх текстовых аннотаций
    
    Args:
        scheme_name: Имя схемы
        pipe_masks_dir: Директория с масками труб
        annotation_masks_dir: Директория с масками аннотаций
        dilation_kernel: Размер ядра для расширения (увеличивает запретную зону)
        
    Returns:
        Бинарная маска (0/255) или None если маски не найдены
        
    Note:
        - Маски труб ищутся с суффиксом "_mask" (например, scheme1_mask.png)
        - Маски аннотаций без суффикса (например, scheme1.png)
    """
    forbidden = None
    
    # Маска труб
    if pipe_masks_dir:
        pipe_mask_path = find_mask_file(scheme_name, pipe_masks_dir, "_mask")
        if pipe_mask_path:
            pipe_mask = cv2.imread(str(pipe_mask_path), cv2.IMREAD_GRAYSCALE)
            if pipe_mask is not None:
                forbidden = (pipe_mask > 0).astype(np.uint8)
    
    # Маска аннотаций
    if annotation_masks_dir:
        ann_mask_path = find_mask_file(scheme_name, annotation_masks_dir)
        if ann_mask_path:
            ann_mask = cv2.imread(str(ann_mask_path), cv2.IMREAD_GRAYSCALE)
            if ann_mask is not None:
                if forbidden is None:
                    forbidden = (ann_mask > 0).astype(np.uint8)
                else:
                    forbidden = np.logical_or(forbidden, ann_mask > 0).astype(np.uint8)
    
    if forbidden is None:
        return None
    
    # Дилатация - расширяем запретную зону
    kernel = np.ones((dilation_kernel, dilation_kernel), np.uint8)
    forbidden = cv2.dilate(forbidden * 255, kernel)
    
    return forbidden


def tile_single_image(
    img_path: Path,
    label_path: Path,
    full_mask: Optional[np.ndarray],
    output_images: Path,
    output_labels: Path,
    output_masks: Path,
    tile_size: int,
    overlap: float,
    min_visible_ratio: float,
    keep_empty_ratio: float
) -> Dict:
    """
    Нарезать одно изображение на тайлы.
    
    Args:
        img_path: Путь к изображению
        label_path: Путь к аннотациям
        full_mask: Forbidden маска для всего изображения (или None)
        output_images: Директория для тайлов
        output_labels: Директория для аннотаций тайлов
        output_masks: Директория для forbidden масок тайлов
        tile_size: Размер тайла
        overlap: Перекрытие (0-1)
        min_visible_ratio: Минимальная видимая доля объекта
        keep_empty_ratio: Доля пустых тайлов для сохранения
        
    Returns:
        Статистика тайлинга
    """
    stats = {
        "tiles_with_objects": 0,
        "empty_tiles_kept": 0,
        "empty_tiles_dropped": 0,
        "total_objects": 0,
        "cropped_objects": 0
    }
    
    # Загрузить изображение
    img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        # Попробовать как цветное и конвертировать
        img = cv2.imread(str(img_path))
        if img is None:
            print(f"Ошибка загрузки: {img_path}")
            return stats
        if len(img.shape) == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    # Гарантируем 2D
    if len(img.shape) == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    img_height, img_width = img.shape
    objects = parse_yolo_labels(label_path, img_width, img_height)
    
    # Шаг между тайлами
    stride = int(tile_size * (1 - overlap))
    
    tile_idx = 0
    empty_tiles = []
    
    # Итерация по сетке тайлов
    for y in range(0, img_height, stride):
        for x in range(0, img_width, stride):
            tile_x1 = x
            tile_y1 = y
            tile_x2 = min(x + tile_size, img_width)
            tile_y2 = min(y + tile_size, img_height)
            
            # Вырезать тайл
            tile_img = img[tile_y1:tile_y2, tile_x1:tile_x2].copy()
            
            # Вырезать маску
            if full_mask is not None:
                tile_mask = full_mask[tile_y1:tile_y2, tile_x1:tile_x2].copy()
            else:
                tile_mask = None
            
            # Padding если тайл на границе
            actual_h, actual_w = tile_img.shape[:2]
            if actual_h != tile_size or actual_w != tile_size:
                pad_bottom = tile_size - actual_h
                pad_right = tile_size - actual_w
                
                tile_img = cv2.copyMakeBorder(
                    tile_img,
                    top=0, bottom=pad_bottom,
                    left=0, right=pad_right,
                    borderType=cv2.BORDER_CONSTANT,
                    value=255  # Белый фон
                )
                
                if tile_mask is not None:
                    tile_mask = cv2.copyMakeBorder(
                        tile_mask,
                        top=0, bottom=pad_bottom,
                        left=0, right=pad_right,
                        borderType=cv2.BORDER_CONSTANT,
                        value=0  # Не запрещено (padding можно использовать)
                    )
            
            # Найти объекты в тайле
            tile_objects = []
            
            for obj in objects:
                inter_area, obj_area = calculate_intersection(
                    obj, tile_x1, tile_y1, tile_x2, tile_y2
                )
                
                if inter_area == 0:
                    continue
                
                visible_ratio = inter_area / obj_area if obj_area > 0 else 0
                
                if visible_ratio < min_visible_ratio:
                    stats["cropped_objects"] += 1
                    continue
                
                clipped = clip_bbox_to_tile(obj, tile_x1, tile_y1, tile_size)
                if clipped:
                    tile_objects.append(clipped)
            
            tile_name = f"{img_path.stem}_tile_{tile_idx:04d}"
            
            if len(tile_objects) == 0:
                # Пустой тайл - сохраним позже
                empty_tiles.append((tile_name, tile_img, tile_mask))
            else:
                # Тайл с объектами - сохраняем сразу
                cv2.imwrite(str(output_images / f"{tile_name}.png"), tile_img)
                
                with open(output_labels / f"{tile_name}.txt", "w", encoding="utf-8") as f:
                    for obj in tile_objects:
                        f.write(f"{obj['class_id']} {obj['x_center']:.6f} "
                               f"{obj['y_center']:.6f} {obj['width']:.6f} "
                               f"{obj['height']:.6f}\n")
                
                if tile_mask is not None:
                    cv2.imwrite(str(output_masks / f"{tile_name}_forbidden.png"), tile_mask)
                else:
                    # Пустая маска
                    empty_mask = np.zeros((tile_size, tile_size), dtype=np.uint8)
                    cv2.imwrite(str(output_masks / f"{tile_name}_forbidden.png"), empty_mask)
                
                stats["tiles_with_objects"] += 1
                stats["total_objects"] += len(tile_objects)
            
            tile_idx += 1
    
    # Сохранить часть пустых тайлов
    num_empty_to_keep = int(len(empty_tiles) * keep_empty_ratio)
    if num_empty_to_keep > 0:
        np.random.shuffle(empty_tiles)
        
        for tile_name, tile_img, tile_mask in empty_tiles[:num_empty_to_keep]:
            cv2.imwrite(str(output_images / f"{tile_name}.png"), tile_img)
            
            # Пустой файл аннотаций
            (output_labels / f"{tile_name}.txt").touch()
            
            if tile_mask is not None:
                cv2.imwrite(str(output_masks / f"{tile_name}_forbidden.png"), tile_mask)
            else:
                empty_mask = np.zeros((tile_size, tile_size), dtype=np.uint8)
                cv2.imwrite(str(output_masks / f"{tile_name}_forbidden.png"), empty_mask)
            
            stats["empty_tiles_kept"] += 1
    
    stats["empty_tiles_dropped"] = len(empty_tiles) - num_empty_to_keep
    
    return stats


def tile_dataset(
    images_dir: Path,
    labels_dir: Path,
    output_dir: Path,
    pipe_masks_dir: Optional[Path] = None,
    annotation_masks_dir: Optional[Path] = None,
    tile_size: int = 1280,
    overlap: float = 0.25,
    min_visible_ratio: float = 0.6,
    keep_empty_ratio: float = 0.1,
    dilation_kernel: int = 7
) -> Dict:
    """
    Тайлинг всего датасета с созданием forbidden масок.
    
    Args:
        images_dir: Директория с изображениями
        labels_dir: Директория с аннотациями
        output_dir: Директория для результатов
        pipe_masks_dir: Директория с масками труб (для CPA)
        annotation_masks_dir: Директория с масками аннотаций (для CPA)
        tile_size: Размер тайла в пикселях
        overlap: Перекрытие между тайлами (0-1)
        min_visible_ratio: Минимальная видимая доля объекта для включения
        keep_empty_ratio: Доля пустых тайлов для сохранения
        dilation_kernel: Размер ядра дилатации для forbidden масок
        
    Returns:
        Общая статистика тайлинга
        
    Example:
        >>> stats = tile_dataset(
        ...     images_dir=Path("./trainval/images"),
        ...     labels_dir=Path("./trainval/labels"),
        ...     output_dir=Path("./tiles"),
        ...     pipe_masks_dir=Path("./masks/pipes"),
        ...     tile_size=1280,
        ...     overlap=0.25
        ... )
    """
    images_dir = Path(images_dir)
    labels_dir = Path(labels_dir)
    output_dir = Path(output_dir)
    
    # Создать выходные директории
    output_images = output_dir / "images"
    output_labels = output_dir / "labels"
    output_masks = output_dir / "forbidden_masks"
    
    output_images.mkdir(parents=True, exist_ok=True)
    output_labels.mkdir(parents=True, exist_ok=True)
    output_masks.mkdir(parents=True, exist_ok=True)
    
    print("\n" + "="*60)
    print("ТАЙЛИНГ ДАТАСЕТА")
    print("="*60)
    print(f"Tile size: {tile_size}")
    print(f"Overlap: {overlap}")
    print(f"Min visible ratio: {min_visible_ratio}")
    print(f"Keep empty ratio: {keep_empty_ratio}")
    
    # Общая статистика
    total_stats = {
        "processed_images": 0,
        "images_with_masks": 0,
        "tiles_with_objects": 0,
        "empty_tiles_kept": 0,
        "empty_tiles_dropped": 0,
        "total_objects": 0,
        "cropped_objects": 0
    }
    
    # Найти все изображения
    image_files = sorted(images_dir.glob("*.png"))
    print(f"Изображений для тайлинга: {len(image_files)}")
    
    for img_path in tqdm(image_files, desc="Tiling", unit="img"):
        scheme_name = img_path.stem
        label_path = labels_dir / f"{scheme_name}.txt"
        
        # Загрузить forbidden маску
        full_mask = load_and_combine_masks(
            scheme_name=scheme_name,
            pipe_masks_dir=pipe_masks_dir,
            annotation_masks_dir=annotation_masks_dir,
            dilation_kernel=dilation_kernel
        )
        
        if full_mask is not None:
            total_stats["images_with_masks"] += 1
        
        # Тайлинг
        stats = tile_single_image(
            img_path=img_path,
            label_path=label_path,
            full_mask=full_mask,
            output_images=output_images,
            output_labels=output_labels,
            output_masks=output_masks,
            tile_size=tile_size,
            overlap=overlap,
            min_visible_ratio=min_visible_ratio,
            keep_empty_ratio=keep_empty_ratio
        )
        
        # Аккумулировать статистику
        for key in stats:
            total_stats[key] += stats[key]
        
        total_stats["processed_images"] += 1
    
    # Вывод статистики
    print(f"\n=== Результаты тайлинга ===")
    print(f"Обработано изображений: {total_stats['processed_images']}")
    print(f"Изображений с масками: {total_stats['images_with_masks']}")
    print(f"Тайлов с объектами: {total_stats['tiles_with_objects']}")
    print(f"Пустых тайлов сохранено: {total_stats['empty_tiles_kept']}")
    print(f"Пустых тайлов отброшено: {total_stats['empty_tiles_dropped']}")
    print(f"Всего объектов: {total_stats['total_objects']}")
    print(f"Обрезано объектов (<{min_visible_ratio*100:.0f}%): {total_stats['cropped_objects']}")
    
    return total_stats


def create_forbidden_mask(
    scheme_name: str,
    pipe_masks_dir: Optional[Path],
    annotation_masks_dir: Optional[Path],
    output_path: Path,
    dilation_kernel: int = 7
) -> bool:
    """
    Создать forbidden маску для одной схемы.
    
    Утилитарная функция для создания масок вне основного пайплайна.
    
    Args:
        scheme_name: Имя схемы
        pipe_masks_dir: Директория с масками труб
        annotation_masks_dir: Директория с масками аннотаций  
        output_path: Путь для сохранения результата
        dilation_kernel: Размер ядра дилатации
        
    Returns:
        True если маска создана, False если не удалось
    """
    mask = load_and_combine_masks(
        scheme_name=scheme_name,
        pipe_masks_dir=pipe_masks_dir,
        annotation_masks_dir=annotation_masks_dir,
        dilation_kernel=dilation_kernel
    )
    
    if mask is None:
        return False
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), mask)
    
    return True

"""
Модуль для подготовки данных из COCO JSON

Конвертирует COCO аннотации в бинарные маски:
- masks_truba: маски труб (ground truth)
- masks_nodes: маски узлов оборудования (4-й канал для модели)
- masks_annotation: маски текстовых меток (не используются)
"""

import json
import numpy as np
from PIL import Image, ImageDraw
from pathlib import Path
from pycocotools import mask as mask_utils
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import partial
from typing import Dict, List, Tuple, Optional
import time


def polygon_to_mask(segmentation, height: int, width: int) -> np.ndarray:
    """Конвертирует полигон в бинарную маску"""
    mask = Image.new('L', (width, height), 0)
    if isinstance(segmentation, list):
        for polygon in segmentation:
            if len(polygon) >= 6:  # Минимум 3 точки (x,y пары)
                polygon_coords = [(polygon[i], polygon[i + 1]) for i in range(0, len(polygon), 2)]
                ImageDraw.Draw(mask).polygon(polygon_coords, outline=1, fill=1)
    return np.array(mask)


def bbox_to_mask(bbox, height: int, width: int) -> np.ndarray:
    """Конвертирует bbox в бинарную маску"""
    mask = np.zeros((height, width), dtype=np.uint8)
    x, y, w, h = [int(val) for val in bbox]
    # Проверка границ
    x2 = min(x + w, width)
    y2 = min(y + h, height)
    x = max(0, x)
    y = max(0, y)
    mask[y:y2, x:x2] = 1
    return mask


def rle_to_mask(segmentation, height: int, width: int) -> np.ndarray:
    """Конвертирует RLE в бинарную маску"""
    try:
        if isinstance(segmentation, dict) and 'counts' in segmentation:
            counts = segmentation['counts']

            # Compressed RLE (строка в counts)
            if isinstance(counts, str):
                mask = mask_utils.decode(segmentation)
                return mask

            # Uncompressed RLE (список чисел в counts)
            elif isinstance(counts, list):
                rle_obj = {
                    'counts': counts,
                    'size': [height, width]
                }
                compressed_rle = mask_utils.frPyObjects(rle_obj, height, width)
                mask = mask_utils.decode(compressed_rle)
                return mask

    except Exception:
        pass

    return np.zeros((height, width), dtype=np.uint8)


def process_annotation(ann: Dict, height: int, width: int) -> np.ndarray:
    """Обрабатывает одну аннотацию и возвращает маску"""
    mask = np.zeros((height, width), dtype=np.uint8)

    try:
        has_segmentation = 'segmentation' in ann and ann['segmentation']

        if has_segmentation:
            seg = ann['segmentation']

            if isinstance(seg, dict) and 'counts' in seg:
                mask = rle_to_mask(seg, height, width)

            elif isinstance(seg, list):
                if len(seg) == 0:
                    if 'bbox' in ann:
                        mask = bbox_to_mask(ann['bbox'], height, width)
                elif isinstance(seg[0], (list, tuple)):
                    mask = polygon_to_mask(seg, height, width)
                elif isinstance(seg[0], (int, float)):
                    mask = polygon_to_mask([seg], height, width)

        if mask.sum() == 0 and 'bbox' in ann:
            mask = bbox_to_mask(ann['bbox'], height, width)

    except:
        if 'bbox' in ann:
            try:
                mask = bbox_to_mask(ann['bbox'], height, width)
            except:
                pass

    return mask


def process_single_image(
    img_id: int,
    img_info: Dict,
    annotations_by_image: Dict,
    categories: Dict,
    truba_id: int,
    annotation_id: int,
    output_dirs: Dict
) -> Dict:
    """Обрабатывает одно изображение"""
    height = img_info['height']
    width = img_info['width']
    file_name = img_info['file_name']
    base_name = Path(file_name).stem

    # Инициализация масок
    mask_truba = np.zeros((height, width), dtype=np.uint8)
    mask_annotation = np.zeros((height, width), dtype=np.uint8)
    mask_nodes = np.zeros((height, width), dtype=np.uint8)

    # Получение аннотаций для текущего изображения
    anns = annotations_by_image.get(img_id, [])

    # Счетчики
    count_truba = 0
    count_annotation = 0
    count_nodes = 0

    # Обработка каждой аннотации
    for ann in anns:
        cat_id = ann['category_id']
        ann_mask = process_annotation(ann, height, width)

        if cat_id == truba_id:
            mask_truba = np.maximum(mask_truba, ann_mask)
            count_truba += 1
        elif cat_id == annotation_id:
            mask_annotation = np.maximum(mask_annotation, ann_mask)
            count_annotation += 1
        else:
            # Все остальные категории - это узлы оборудования
            mask_nodes = np.maximum(mask_nodes, ann_mask)
            count_nodes += 1

    # Сохранение только непустых масок
    saved_masks = []

    # Truba (обязательно сохраняем - это ground truth)
    if mask_truba.sum() > 0:
        mask_truba_img = Image.fromarray(mask_truba * 255)
        truba_path = output_dirs['truba'] / f"{base_name}.png"
        mask_truba_img.save(truba_path)
        saved_masks.append(('truba', count_truba))

    # Annotation (сохраняем для истории, но не используем)
    if mask_annotation.sum() > 0:
        mask_annotation_img = Image.fromarray(mask_annotation * 255)
        annotation_path = output_dirs['annotation'] / f"{base_name}.png"
        mask_annotation_img.save(annotation_path)
        saved_masks.append(('annotation', count_annotation))

    # Nodes (сохраняем - это 4-й канал для модели)
    if mask_nodes.sum() > 0:
        mask_nodes_img = Image.fromarray(mask_nodes * 255)
        nodes_path = output_dirs['nodes'] / f"{base_name}.png"
        mask_nodes_img.save(nodes_path)
        saved_masks.append(('nodes', count_nodes))

    return {
        'file_name': file_name,
        'total_anns': len(anns),
        'saved_masks': saved_masks
    }


def create_binary_masks(
    coco_json_path: str,
    images_dir: str,
    output_dir: str,
    num_workers: int = 4,
    verbose: bool = True
) -> Dict:
    """
    Создает бинарные маски из COCO JSON
    
    Args:
        coco_json_path: Путь к COCO JSON файлу
        images_dir: Путь к директории с изображениями
        output_dir: Путь для сохранения масок
        num_workers: Количество параллельных потоков
        verbose: Выводить прогресс
    
    Returns:
        Словарь со статистикой обработки
    """
    start_time = time.time()

    if verbose:
        print(f"\n{'='*80}")
        print("КОНВЕРТАЦИЯ COCO → МАСКИ")
        print(f"{'='*80}\n")
        print(f"Загрузка {coco_json_path}...")

    # Загрузка COCO JSON
    with open(coco_json_path, 'r', encoding='utf-8') as f:
        coco_data = json.load(f)

    # Создание директорий для масок
    output_path = Path(output_dir)
    truba_dir = output_path / "masks_truba"
    annotation_dir = output_path / "masks_annotation"
    nodes_dir = output_path / "masks_nodes"

    for dir_path in [truba_dir, annotation_dir, nodes_dir]:
        dir_path.mkdir(parents=True, exist_ok=True)

    output_dirs = {
        'truba': truba_dir,
        'annotation': annotation_dir,
        'nodes': nodes_dir
    }

    # Получение ID категорий
    categories = {cat['id']: cat['name'] for cat in coco_data['categories']}
    
    if verbose:
        print(f"Найдено категорий: {len(categories)}")
        for cat_id, cat_name in categories.items():
            print(f"  - {cat_id}: {cat_name}")

    # Поиск ID для truba и annotation
    truba_id = None
    annotation_id = None

    for cat_id, cat_name in categories.items():
        if cat_name.lower() == 'truba':
            truba_id = cat_id
        elif cat_name.lower() == 'annotation':
            annotation_id = cat_id

    if verbose:
        print(f"\nЦелевые категории:")
        print(f"  - truba ID: {truba_id}")
        print(f"  - annotation ID: {annotation_id}")
        print(f"  - nodes (остальные): {len(categories) - 2} категорий")

    # Группировка аннотаций по изображениям
    images_dict = {img['id']: img for img in coco_data['images']}
    annotations_by_image = {}

    for ann in coco_data['annotations']:
        img_id = ann['image_id']
        if img_id not in annotations_by_image:
            annotations_by_image[img_id] = []
        annotations_by_image[img_id].append(ann)

    total_images = len(images_dict)
    
    if verbose:
        print(f"\nОбработка {total_images} изображений (используя {num_workers} потоков)...\n")

    # Параллельная обработка
    process_func = partial(
        process_single_image,
        annotations_by_image=annotations_by_image,
        categories=categories,
        truba_id=truba_id,
        annotation_id=annotation_id,
        output_dirs=output_dirs
    )

    # Статистика
    total_saved = {'truba': 0, 'annotation': 0, 'nodes': 0}
    processed = 0

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        # Отправляем задачи
        futures = {}
        for img_id, img_info in images_dict.items():
            future = executor.submit(process_func, img_id, img_info)
            futures[future] = img_info['file_name']

        # Собираем результаты
        for future in as_completed(futures):
            result = future.result()
            processed += 1

            if verbose:
                # Вывод прогресса
                status_parts = []
                for mask_type, count in result['saved_masks']:
                    status_parts.append(f"{mask_type}:{count}")
                    total_saved[mask_type] += 1

                status = " | ".join(status_parts) if status_parts else "пусто - не сохранено"
                print(f"[{processed}/{total_images}] {result['file_name']}: {status}")

    elapsed = time.time() - start_time

    if verbose:
        print(f"\n{'='*80}")
        print("✓ Готово!")
        print(f"{'='*80}")
        print(f"Время выполнения: {elapsed:.2f} секунд")
        print(f"Скорость: {total_images / elapsed:.1f} изображений/сек")
        print(f"\nСтатистика сохраненных масок:")
        print(f"  - Truba:      {total_saved['truba']}/{total_images} изображений")
        print(f"  - Annotation: {total_saved['annotation']}/{total_images} изображений")
        print(f"  - Nodes:      {total_saved['nodes']}/{total_images} изображений")
        print(f"\nМаски сохранены в:")
        print(f"  - {truba_dir}")
        print(f"  - {annotation_dir}")
        print(f"  - {nodes_dir}")
        print(f"{'='*80}\n")

    return {
        'total_images': total_images,
        'saved_masks': total_saved,
        'elapsed_time': elapsed,
        'images_per_second': total_images / elapsed
    }


if __name__ == "__main__":
    # Пример использования
    coco_json = "data/instances_default.json"
    images_dir = "data/images"
    output_dir = "data/processed/masks"
    
    create_binary_masks(coco_json, images_dir, output_dir, num_workers=8)

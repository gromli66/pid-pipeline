"""
inference.py

Команда inference: классификация критических точек на новых схемах.

Этапы:
1. Детекция критических точек на скелетах
2. Извлечение crops "на лету"
3. Batch inference через CNN
4. Генерация масок и визуализаций
"""

import os
import json
from pathlib import Path
from collections import defaultdict
from typing import List, Dict, Tuple

import numpy as np
from PIL import Image
import cv2
import matplotlib.pyplot as plt
from tqdm import tqdm

import torch
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader

from .model import load_model
from .transforms import SyncTransform
from .utils import (
    find_images,
    ensure_dir,
    save_json,
    find_junctions,
    find_turns,
    filter_by_distance,
    extract_crop,
    create_binary_mask,
)


CLASS_NAMES = ['connection', 'bridge', 'turn']
CLASS_COLORS = {
    'connection': (0, 255, 0),    # Зелёный
    'bridge': (255, 0, 0),        # Красный
    'turn': (0, 150, 255),        # Синий
}


class OnTheFlyDataset(Dataset):
    """Dataset для инференса с извлечением crops на лету."""
    
    def __init__(self, crops_data: List[Dict], transform=None):
        self.crops_data = crops_data
        self.transform = transform or SyncTransform(is_train=False)
    
    def __len__(self):
        return len(self.crops_data)
    
    def __getitem__(self, idx):
        item = self.crops_data[idx]
        
        rgb_crop = item['rgb_crop']
        skel_crop = item['skel_crop']
        
        # PIL Images
        rgb = Image.fromarray(rgb_crop)
        skeleton = Image.fromarray(skel_crop)
        
        # Transform
        rgb_tensor, skel_tensor = self.transform(rgb, skeleton)
        
        return rgb_tensor, skel_tensor, idx


def detect_critical_points(skeleton: np.ndarray, 
                           junctions_only: bool = False,
                           angle_threshold: float = 130,
                           junction_cluster_radius: int = 10,
                           bend_cluster_radius: int = 15,
                           junction_exclusion_radius: int = 20,
                           min_distance: int = 8) -> List[Dict]:
    """Детекция критических точек на скелете."""
    
    junctions = find_junctions(skeleton, min_degree=3, cluster_radius=junction_cluster_radius)
    
    if junctions_only:
        turns = []
    else:
        turns = find_turns(
            skeleton, junctions,
            angle_threshold=angle_threshold,
            cluster_radius=bend_cluster_radius,
            exclusion_radius=junction_exclusion_radius
        )
    
    all_points = junctions + turns
    all_points = filter_by_distance(all_points, min_distance=min_distance)
    
    return all_points


def process_schema(image_path: Path,
                   skeleton_path: Path,
                   model: torch.nn.Module,
                   device: torch.device,
                   crop_size: int = 224,
                   batch_size: int = 32,
                   confidence_threshold: float = 0.8,
                   junctions_only: bool = False,
                   angle_threshold: float = 130,
                   junction_cluster_radius: int = 10,
                   bend_cluster_radius: int = 15,
                   junction_exclusion_radius: int = 20,
                   min_distance: int = 8) -> Tuple[List[Dict], np.ndarray, np.ndarray]:
    """
    Обработка одной схемы.
    
    Returns:
        (predictions, image, skeleton)
    """
    # Загрузка
    image_raw = np.array(Image.open(image_path))
    skeleton = np.array(Image.open(skeleton_path).convert('L'))
    
    # Предобработка изображения для работы с цветными схемами
    # 1. Если цветное - берём минимум по каналам (сохраняет все цветные линии)
    if image_raw.ndim == 3:
        image_gray = np.min(image_raw, axis=2).astype(np.uint8)
    else:
        image_gray = image_raw.astype(np.uint8)
    
    # 2. Адаптивная бинаризация
    image_binary = cv2.adaptiveThreshold(
        image_gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 51, 10
    )
    
    # Для визуализации используем оригинал, для анализа - бинаризованное
    image = image_raw  # для визуализации
    image_for_crops = image_binary  # для извлечения crops
    
    # Детекция точек
    points = detect_critical_points(
        skeleton, 
        junctions_only=junctions_only,
        angle_threshold=angle_threshold,
        junction_cluster_radius=junction_cluster_radius,
        bend_cluster_radius=bend_cluster_radius,
        junction_exclusion_radius=junction_exclusion_radius,
        min_distance=min_distance
    )
    
    if len(points) == 0:
        return [], image, skeleton
    
    # Извлечение crops (используем бинаризованное изображение)
    crops_data = []
    for i, point in enumerate(points):
        x, y = point['pos']
        
        # RGB crop из бинаризованного (конвертируем в RGB для модели)
        if image_for_crops.ndim == 2:
            rgb_for_crop = np.stack([image_for_crops] * 3, axis=-1)
        else:
            rgb_for_crop = image_for_crops
        
        rgb_crop = extract_crop(rgb_for_crop, (x, y), crop_size, pad_value=255)
        skel_crop = extract_crop(skeleton, (x, y), crop_size, pad_value=0)
        
        crops_data.append({
            'idx': i,
            'pos': (x, y),
            'type': point['type'],
            'rgb_crop': rgb_crop,
            'skel_crop': skel_crop,
        })
    
    # Inference
    transform = SyncTransform(img_size=crop_size, is_train=False)
    dataset = OnTheFlyDataset(crops_data, transform)
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=False, num_workers=0)
    
    all_predictions = []
    all_confidences = []
    
    model.eval()
    with torch.no_grad():
        for rgb, skel, indices in dataloader:
            rgb = rgb.to(device)
            skel = skel.to(device)
            
            outputs = model(rgb, skel)
            probs = F.softmax(outputs, dim=1)
            confidences, predictions = probs.max(1)
            
            all_predictions.extend(predictions.cpu().numpy())
            all_confidences.extend(confidences.cpu().numpy())
    
    # Сборка результатов
    results = []
    for i, crop_info in enumerate(crops_data):
        pred_class = CLASS_NAMES[all_predictions[i]]
        confidence = float(all_confidences[i])
        
        if confidence >= confidence_threshold:
            results.append({
                'pos': crop_info['pos'],
                'class': pred_class,
                'confidence': confidence,
                'point_type': crop_info['type'],
            })
    
    return results, image, skeleton


def create_visualization(image: np.ndarray,
                        skeleton: np.ndarray,
                        predictions: List[Dict],
                        output_path: Path,
                        square_size: int = 15,
                        darken_factor: float = 0.3):
    """Создание визуализации с цветными квадратами."""
    
    # Подготовка
    if image.ndim == 2:
        vis = np.stack([image] * 3, axis=-1).astype(np.float32)
    else:
        vis = image.copy().astype(np.float32)
    
    # Затемнение
    vis = (vis * darken_factor).astype(np.uint8)
    
    # Skeleton зелёным
    skel_mask = skeleton > 127
    vis[skel_mask] = [0, 200, 0]
    
    # Квадраты на точках
    half = square_size // 2
    
    for pred in predictions:
        x, y = pred['pos']
        color = CLASS_COLORS.get(pred['class'], (255, 255, 255))
        
        y1, y2 = max(0, y-half), min(vis.shape[0], y+half+1)
        x1, x2 = max(0, x-half), min(vis.shape[1], x+half+1)
        
        vis[y1:y2, x1:x2] = color
    
    # Сохранение
    Image.fromarray(vis).save(output_path)


def create_masks(image_shape: Tuple[int, int],
                predictions: List[Dict],
                output_dir: Path,
                schema_name: str,
                point_size: int = 15):
    """Создание бинарных масок в отдельных папках."""
    
    h, w = image_shape[:2]
    
    # Создаём подпапки
    bridges_dir = output_dir / 'bridges'
    junctions_dir = output_dir / 'junctions'
    bridges_dir.mkdir(parents=True, exist_ok=True)
    junctions_dir.mkdir(parents=True, exist_ok=True)
    
    # Маска bridges
    bridge_points = [(p['pos'][0], p['pos'][1]) for p in predictions if p['class'] == 'bridge']
    bridges_mask = create_binary_mask((h, w), bridge_points, point_size)
    
    # Маска junctions (connection + turn)
    junction_points = [(p['pos'][0], p['pos'][1]) for p in predictions 
                      if p['class'] in ['connection', 'turn']]
    junctions_mask = create_binary_mask((h, w), junction_points, point_size)
    
    # Сохранение в отдельные папки
    Image.fromarray(bridges_mask).save(bridges_dir / f'{schema_name}.png')
    Image.fromarray(junctions_mask).save(junctions_dir / f'{schema_name}.png')


def run_inference(args):
    """Основная функция инференса."""
    print("=" * 70)
    print("INFERENCE: Классификация критических точек")
    print("=" * 70)
    
    # Параметры
    images_dir = Path(args.images)
    skeletons_dir = Path(args.skeletons)
    output_dir = Path(args.output)
    
    ensure_dir(output_dir / 'masks')
    if args.visualize:
        ensure_dir(output_dir / 'visualizations')
    
    device = torch.device(args.device if torch.cuda.is_available() else 'cpu')
    
    print(f"\n📁 Входные данные:")
    print(f"   Images: {images_dir}")
    print(f"   Skeletons: {skeletons_dir}")
    print(f"   Checkpoint: {args.checkpoint}")
    print(f"\n📁 Выход: {output_dir}")
    print(f"\n⚙️  Параметры:")
    print(f"   Confidence threshold: {args.confidence}")
    print(f"   Visualize: {args.visualize}")
    print(f"   Device: {device}")
    
    # Загрузка модели
    print(f"\n{'=' * 70}")
    print("[1/3] Загрузка модели...")
    print(f"{'=' * 70}")
    
    model = load_model(args.checkpoint, device=device, num_classes=3)
    print(f"   ✓ Модель загружена")
    
    # Поиск изображений
    image_paths = find_images(images_dir)
    print(f"\n   Найдено схем: {len(image_paths)}")
    
    if len(image_paths) == 0:
        print("❌ Изображения не найдены!")
        return
    
    # Обработка
    print(f"\n{'=' * 70}")
    print("[2/3] Обработка схем...")
    print(f"{'=' * 70}\n")
    
    all_predictions = []
    stats = defaultdict(int)
    schema_stats = {}
    
    for image_path in tqdm(image_paths, desc="Схемы"):
        schema_name = image_path.stem
        skeleton_path = skeletons_dir / f'{schema_name}.png'
        
        if not skeleton_path.exists():
            continue
        
        try:
            predictions, image, skeleton = process_schema(
                image_path, skeleton_path, model, device,
                crop_size=args.crop_size,
                batch_size=args.batch_size,
                confidence_threshold=args.confidence,
                junctions_only=getattr(args, 'junctions_only', False),
                angle_threshold=args.angle_threshold,
                junction_cluster_radius=args.junction_cluster_radius,
                bend_cluster_radius=args.bend_cluster_radius,
                junction_exclusion_radius=args.junction_exclusion_radius,
                min_distance=args.min_distance
            )
            
            if len(predictions) == 0:
                continue
            
            # Создание масок
            create_masks(image.shape, predictions, output_dir / 'masks', schema_name)
            
            # Визуализация
            if args.visualize:
                viz_path = output_dir / 'visualizations' / f'{schema_name}.png'
                create_visualization(image, skeleton, predictions, viz_path)
            
            # Статистика
            for pred in predictions:
                pred['schema'] = schema_name
                all_predictions.append(pred)
                stats[pred['class']] += 1
            
            schema_stats[schema_name] = {
                'total': len(predictions),
                'bridge': sum(1 for p in predictions if p['class'] == 'bridge'),
                'connection': sum(1 for p in predictions if p['class'] == 'connection'),
                'turn': sum(1 for p in predictions if p['class'] == 'turn'),
            }
            
            stats['schemas'] += 1
        
        except Exception as e:
            print(f"\n❌ Ошибка: {schema_name}: {e}")
            continue
    
    # Сохранение результатов
    print(f"\n{'=' * 70}")
    print("[3/3] Сохранение результатов...")
    print(f"{'=' * 70}")
    
    # Predictions JSON
    predictions_path = output_dir / 'predictions.json'
    save_json(all_predictions, str(predictions_path))
    
    # Summary JSON
    summary = {
        'total_schemas': stats['schemas'],
        'total_points': len(all_predictions),
        'bridges': stats['bridge'],
        'connections': stats['connection'],
        'turns': stats['turn'],
        'confidence_threshold': args.confidence,
        'schemas': schema_stats,
    }
    save_json(summary, str(output_dir / 'summary.json'))
    
    # Итоги
    print(f"\n{'=' * 70}")
    print("✅ ИНФЕРЕНС ЗАВЕРШЁН!")
    print(f"{'=' * 70}")
    
    print(f"\n📊 Статистика:")
    print(f"   Обработано схем: {stats['schemas']}")
    print(f"   Всего точек: {len(all_predictions)}")
    print(f"   - Bridge: {stats['bridge']}")
    print(f"   - Connection: {stats['connection']}")
    print(f"   - Turn: {stats['turn']}")
    
    print(f"\n📁 Результаты:")
    print(f"   Маски:")
    print(f"      - {output_dir / 'masks' / 'bridges'}/ (мосты)")
    print(f"      - {output_dir / 'masks' / 'junctions'}/ (соединения)")
    if args.visualize:
        print(f"   Визуализации: {output_dir / 'visualizations'}/")
    print(f"   Predictions: {predictions_path}")
    print(f"   Summary: {output_dir / 'summary.json'}")
    print()

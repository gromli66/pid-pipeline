"""
Copy-Paste Augmentation для P&ID Node Detection.

НАЗНАЧЕНИЕ:
----------
Этот модуль реализует Copy-Paste аугментацию (CPA) для увеличения
количества примеров редких классов. CPA вырезает объекты из тренировочных
изображений и вставляет их на другие тайлы.

ПОЧЕМУ CPA ВАЖНА:
----------------
1. В P&ID датасете сильный дисбаланс классов (от 3 до 3000 примеров)
2. Некоторые виды оборудования встречаются очень редко
3. Стандартные аугментации (flip, rotate) не увеличивают количество объектов
4. CPA позволяет "размножить" редкие объекты на разные контексты

ПРАВИЛА ВСТАВКИ:
---------------
- 70% - полностью видимый объект
- 20% - частично видимый (80-90% объекта)  
- 10% - с перекрытием (имитация загороженного объекта)

АУГМЕНТАЦИИ ПРИ ВСТАВКЕ:
-----------------------
- Повороты: 0°, 90°, 180°, 270°
- Яркость: ±15
- Толщина линий: erode/dilate
- Адаптивная бинаризация

FORBIDDEN МАСКИ:
---------------
Определяют области, куда НЕЛЬЗЯ вставлять объекты:
- Поверх труб (объект будет выглядеть неестественно)
- Поверх текстовых аннотаций (нарушит разметку)
- Поверх существующих объектов (IoU проверка)

ИСПОЛЬЗОВАНИЕ:
-------------
    from pid_node_detection.augmentation import CopyPasteAugmentation
    from pid_node_detection.config import load_config, load_classes
    
    config = load_config()
    classes = load_classes()
    
    cpa = CopyPasteAugmentation(
        rare_classes=classes.rare_classes,
        critical_classes=classes.critical_classes,
        target_counts={"rare": 150, "critical": 200}
    )
    
    cpa.augment(
        images_dir=Path("./train/images"),
        labels_dir=Path("./train/labels"),
        masks_dir=Path("./train/forbidden_masks"),
        output_dir=Path("./train_augmented")
    )
"""

import cv2
import numpy as np
import random
import shutil
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
from collections import defaultdict
from tqdm import tqdm


class CopyPasteAugmentation:
    """
    Copy-Paste Augmentation для увеличения редких классов.
    
    Attributes:
        rare_classes: Список редких классов
        critical_classes: Список критически редких классов
        target_counts: Целевое количество примеров
        placement_rules: Правила размещения (full_visible, partial, occlusion)
        max_attempts: Максимум попыток найти позицию
        forbidden_threshold: Максимальная доля forbidden зоны
        min_object_size: Минимальный размер объекта для вставки
    """
    
    def __init__(
        self,
        rare_classes: List[int],
        critical_classes: Optional[List[int]] = None,
        target_counts: Optional[Dict[str, int]] = None,
        placement_rules: Optional[Dict[str, float]] = None,
        max_attempts: int = 50,
        forbidden_threshold: float = 0.10,
        min_object_size: int = 20,
        random_seed: int = 42
    ):
        """
        Args:
            rare_classes: Список ID редких классов
            critical_classes: Список ID критически редких классов (больше аугментаций)
            target_counts: {"rare": N, "critical": M} - целевое количество
            placement_rules: {"full_visible": 0.7, "partial": 0.2, "occlusion": 0.1}
            max_attempts: Максимум попыток найти позицию для вставки
            forbidden_threshold: Максимальная доля forbidden зоны под объектом
            min_object_size: Минимальный размер объекта в пикселях
            random_seed: Seed для воспроизводимости
        """
        self.rare_classes = set(rare_classes)
        self.critical_classes = set(critical_classes or [])
        
        self.target_counts = target_counts or {"rare": 150, "critical": 200}
        self.placement_rules = placement_rules or {
            "full_visible": 0.70,
            "partial": 0.20,
            "occlusion": 0.10
        }
        
        self.max_attempts = max_attempts
        self.forbidden_threshold = forbidden_threshold
        self.min_object_size = min_object_size
        
        random.seed(random_seed)
        np.random.seed(random_seed)
        
        # Параметры внутренних аугментаций
        self.rotation_angles = [0, 90, 180, 270]
        self.brightness_range = (-15, 15)
        self.brightness_prob = 0.5
        self.thickness_prob = 0.3
        self.binarization_prob = 0.2
    
    def _yolo_to_bbox(
        self,
        yolo_coords: Tuple[float, float, float, float],
        img_w: int,
        img_h: int
    ) -> Tuple[int, int, int, int]:
        """Конвертация YOLO координат в пиксельный bbox."""
        x_center, y_center, w, h = yolo_coords
        
        x1 = int((x_center - w / 2) * img_w)
        y1 = int((y_center - h / 2) * img_h)
        x2 = int((x_center + w / 2) * img_w)
        y2 = int((y_center + h / 2) * img_h)
        
        # Clamp
        x1 = max(0, min(img_w - 1, x1))
        y1 = max(0, min(img_h - 1, y1))
        x2 = max(0, min(img_w - 1, x2))
        y2 = max(0, min(img_h - 1, y2))
        
        return x1, y1, x2, y2
    
    def _bbox_to_yolo(
        self,
        x1: int, y1: int, x2: int, y2: int,
        img_w: int, img_h: int
    ) -> Tuple[float, float, float, float]:
        """Конвертация пиксельного bbox в YOLO координаты."""
        w = (x2 - x1) / img_w
        h = (y2 - y1) / img_h
        x_center = (x1 + x2) / 2.0 / img_w
        y_center = (y1 + y2) / 2.0 / img_h
        
        return x_center, y_center, w, h
    
    def _calculate_iou(
        self,
        bbox1: Tuple[float, float, float, float],
        bbox2: Tuple[float, float, float, float]
    ) -> float:
        """IoU между двумя YOLO bbox."""
        b1_x1 = bbox1[0] - bbox1[2] / 2
        b1_y1 = bbox1[1] - bbox1[3] / 2
        b1_x2 = bbox1[0] + bbox1[2] / 2
        b1_y2 = bbox1[1] + bbox1[3] / 2
        
        b2_x1 = bbox2[0] - bbox2[2] / 2
        b2_y1 = bbox2[1] - bbox2[3] / 2
        b2_x2 = bbox2[0] + bbox2[2] / 2
        b2_y2 = bbox2[1] + bbox2[3] / 2
        
        inter_x1 = max(b1_x1, b2_x1)
        inter_y1 = max(b1_y1, b2_y1)
        inter_x2 = min(b1_x2, b2_x2)
        inter_y2 = min(b1_y2, b2_y2)
        
        if inter_x1 >= inter_x2 or inter_y1 >= inter_y2:
            return 0.0
        
        inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
        b1_area = (b1_x2 - b1_x1) * (b1_y2 - b1_y1)
        b2_area = (b2_x2 - b2_x1) * (b2_y2 - b2_y1)
        
        return inter_area / (b1_area + b2_area - inter_area + 1e-6)
    
    def _extract_object_with_mask(
        self,
        img: np.ndarray,
        bbox: Tuple[int, int, int, int],
        padding: float = 0.05
    ) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """
        Вырезать объект с созданием маски.
        
        Returns:
            Tuple (crop, mask) или (None, None) при ошибке
        """
        x1, y1, x2, y2 = bbox
        h, w = img.shape[:2]
        
        # Padding
        pad_w = int((x2 - x1) * padding)
        pad_h = int((y2 - y1) * padding)
        
        x1 = max(0, x1 - pad_w)
        y1 = max(0, y1 - pad_h)
        x2 = min(w, x2 + pad_w)
        y2 = min(h, y2 + pad_h)
        
        crop = img[y1:y2, x1:x2].copy()
        
        if crop.size == 0:
            return None, None
        
        # Маска: темные пиксели = объект
        mask = (crop < 200).astype(np.uint8) * 255
        
        # Морфология для очистки
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        
        # Белый фон там где нет маски
        crop[mask == 0] = 255
        
        return crop, mask
    
    def _augment_rotation(
        self,
        crop: np.ndarray,
        mask: np.ndarray,
        angle: int
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Поворот кропа и маски."""
        if angle == 0:
            return crop, mask
        
        if angle == 90:
            crop_rot = cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)
            mask_rot = cv2.rotate(mask, cv2.ROTATE_90_CLOCKWISE)
        elif angle == 180:
            crop_rot = cv2.rotate(crop, cv2.ROTATE_180)
            mask_rot = cv2.rotate(mask, cv2.ROTATE_180)
        elif angle == 270:
            crop_rot = cv2.rotate(crop, cv2.ROTATE_90_COUNTERCLOCKWISE)
            mask_rot = cv2.rotate(mask, cv2.ROTATE_90_COUNTERCLOCKWISE)
        else:
            return crop, mask
        
        return crop_rot, mask_rot
    
    def _augment_brightness(
        self,
        crop: np.ndarray,
        delta_range: Tuple[int, int]
    ) -> np.ndarray:
        """Изменение яркости."""
        delta = random.randint(*delta_range)
        return np.clip(crop.astype(np.int16) + delta, 0, 255).astype(np.uint8)
    
    def _augment_thickness(
        self,
        crop: np.ndarray,
        mask: np.ndarray,
        operation: str,
        kernel_size: int
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Изменение толщины линий."""
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        
        if operation == "erode":
            mask_aug = cv2.erode(mask, kernel, iterations=1)
        elif operation == "dilate":
            mask_aug = cv2.dilate(mask, kernel, iterations=1)
        else:
            mask_aug = mask
        
        crop_aug = crop.copy()
        crop_aug[mask_aug == 0] = 255
        
        return crop_aug, mask_aug
    
    def _augment_binarization(
        self,
        crop: np.ndarray,
        mask: np.ndarray
    ) -> np.ndarray:
        """Адаптивная бинаризация."""
        crop_bin = crop.copy()
        _, binary = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        crop_bin[mask > 0] = binary[mask > 0]
        
        return crop_bin
    
    def _apply_augmentations(
        self,
        crop: np.ndarray,
        mask: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Применить случайные аугментации к кропу."""
        # Поворот (всегда)
        angle = random.choice(self.rotation_angles)
        crop, mask = self._augment_rotation(crop, mask, angle)
        
        # Яркость
        if random.random() < self.brightness_prob:
            crop = self._augment_brightness(crop, self.brightness_range)
        
        # Толщина линий
        if random.random() < self.thickness_prob:
            operation = random.choice(["erode", "dilate"])
            kernel_size = random.choice([2, 3])
            crop, mask = self._augment_thickness(crop, mask, operation, kernel_size)
        
        # Бинаризация
        if random.random() < self.binarization_prob:
            crop = self._augment_binarization(crop, mask)
        
        return crop, mask
    
    def _insert_object(
        self,
        tile: np.ndarray,
        crop: np.ndarray,
        mask: np.ndarray,
        x: int,
        y: int
    ) -> np.ndarray:
        """Вставить объект на тайл."""
        tile_h, tile_w = tile.shape[:2]
        crop_h, crop_w = crop.shape[:2]
        
        # Вычислить области вставки
        src_x1 = max(0, -x)
        src_y1 = max(0, -y)
        src_x2 = min(crop_w, tile_w - x)
        src_y2 = min(crop_h, tile_h - y)
        
        dst_x1 = max(0, x)
        dst_y1 = max(0, y)
        dst_x2 = dst_x1 + (src_x2 - src_x1)
        dst_y2 = dst_y1 + (src_y2 - src_y1)
        
        if src_x2 <= src_x1 or src_y2 <= src_y1:
            return tile
        
        crop_part = crop[src_y1:src_y2, src_x1:src_x2]
        mask_part = mask[src_y1:src_y2, src_x1:src_x2]
        tile_part = tile[dst_y1:dst_y2, dst_x1:dst_x2]
        
        # Вставка по маске
        mask_bool = mask_part > 0
        tile_part[mask_bool] = crop_part[mask_bool]
        
        tile[dst_y1:dst_y2, dst_x1:dst_x2] = tile_part
        
        return tile
    
    def _find_position_full_visible(
        self,
        tile: np.ndarray,
        crop: np.ndarray,
        forbidden_mask: Optional[np.ndarray],
        existing_bboxes: List[Tuple[float, float, float, float]]
    ) -> Optional[Tuple[int, int, Tuple[float, float, float, float]]]:
        """Найти позицию для полностью видимого объекта."""
        tile_h, tile_w = tile.shape[:2]
        crop_h, crop_w = crop.shape[:2]
        
        for _ in range(self.max_attempts):
            if crop_w >= tile_w or crop_h >= tile_h:
                return None
            
            x = random.randint(0, tile_w - crop_w)
            y = random.randint(0, tile_h - crop_h)
            
            # Проверка forbidden mask
            if forbidden_mask is not None:
                mask_region = forbidden_mask[y:y + crop_h, x:x + crop_w]
                forbidden_ratio = (mask_region > 0).sum() / mask_region.size
                if forbidden_ratio > self.forbidden_threshold:
                    continue
            
            # Новый bbox
            new_bbox = (
                (x + crop_w / 2) / tile_w,
                (y + crop_h / 2) / tile_h,
                crop_w / tile_w,
                crop_h / tile_h
            )
            
            # Проверка IoU с существующими
            overlap = any(self._calculate_iou(new_bbox, ex) > 0.05 for ex in existing_bboxes)
            if overlap:
                continue
            
            return x, y, new_bbox
        
        return None
    
    def _find_position_partial(
        self,
        tile: np.ndarray,
        crop: np.ndarray,
        forbidden_mask: Optional[np.ndarray],
        existing_bboxes: List[Tuple[float, float, float, float]],
        visibility_range: Tuple[float, float] = (0.8, 0.9)
    ) -> Optional[Tuple[int, int, Tuple[float, float, float, float]]]:
        """Найти позицию для частично видимого объекта (80-90%)."""
        tile_h, tile_w = tile.shape[:2]
        crop_h, crop_w = crop.shape[:2]
        
        for _ in range(self.max_attempts):
            target_visibility = random.uniform(*visibility_range)
            side = random.choice(["top", "bottom", "left", "right"])
            
            if side == "left":
                cutoff = int(crop_w * (1 - target_visibility))
                x = random.randint(-cutoff, 0)
                y = random.randint(0, max(0, tile_h - crop_h))
            elif side == "right":
                cutoff = int(crop_w * (1 - target_visibility))
                x = random.randint(tile_w - crop_w, tile_w - crop_w + cutoff)
                y = random.randint(0, max(0, tile_h - crop_h))
            elif side == "top":
                cutoff = int(crop_h * (1 - target_visibility))
                x = random.randint(0, max(0, tile_w - crop_w))
                y = random.randint(-cutoff, 0)
            else:  # bottom
                cutoff = int(crop_h * (1 - target_visibility))
                x = random.randint(0, max(0, tile_w - crop_w))
                y = random.randint(tile_h - crop_h, tile_h - crop_h + cutoff)
            
            # Видимая область
            vis_x1 = max(0, x)
            vis_y1 = max(0, y)
            vis_x2 = min(tile_w, x + crop_w)
            vis_y2 = min(tile_h, y + crop_h)
            
            vis_w = vis_x2 - vis_x1
            vis_h = vis_y2 - vis_y1
            
            if vis_w < self.min_object_size or vis_h < self.min_object_size:
                continue
            
            # Проверка forbidden mask
            if forbidden_mask is not None:
                mask_region = forbidden_mask[vis_y1:vis_y2, vis_x1:vis_x2]
                if mask_region.size > 0:
                    forbidden_ratio = (mask_region > 0).sum() / mask_region.size
                    if forbidden_ratio > self.forbidden_threshold:
                        continue
            
            visible_bbox = self._bbox_to_yolo(vis_x1, vis_y1, vis_x2, vis_y2, tile_w, tile_h)
            
            overlap = any(self._calculate_iou(visible_bbox, ex) > 0.15 for ex in existing_bboxes)
            if overlap:
                continue
            
            return x, y, visible_bbox
        
        return None
    
    def _find_position_occlusion(
        self,
        tile: np.ndarray,
        crop: np.ndarray,
        forbidden_mask: Optional[np.ndarray],
        existing_bboxes: List[Tuple[float, float, float, float]]
    ) -> Optional[Tuple[int, int, Tuple[float, float, float, float]]]:
        """Найти позицию с перекрытием существующего объекта."""
        tile_h, tile_w = tile.shape[:2]
        crop_h, crop_w = crop.shape[:2]
        
        for _ in range(self.max_attempts):
            x = random.randint(-crop_w // 2, tile_w - crop_w // 2)
            y = random.randint(-crop_h // 2, tile_h - crop_h // 2)
            
            vis_x1 = max(0, x)
            vis_y1 = max(0, y)
            vis_x2 = min(tile_w, x + crop_w)
            vis_y2 = min(tile_h, y + crop_h)
            
            vis_w = vis_x2 - vis_x1
            vis_h = vis_y2 - vis_y1
            
            if vis_w < self.min_object_size or vis_h < self.min_object_size:
                continue
            
            # Проверка forbidden mask (менее строгий порог)
            if forbidden_mask is not None:
                mask_region = forbidden_mask[vis_y1:vis_y2, vis_x1:vis_x2]
                if mask_region.size > 0:
                    forbidden_ratio = (mask_region > 0).sum() / mask_region.size
                    if forbidden_ratio > self.forbidden_threshold * 2:
                        continue
            
            visible_bbox = self._bbox_to_yolo(vis_x1, vis_y1, vis_x2, vis_y2, tile_w, tile_h)
            
            # Целимся на умеренное перекрытие (10-40%)
            iou_values = [self._calculate_iou(visible_bbox, ex) for ex in existing_bboxes]
            max_iou = max(iou_values) if iou_values else 0
            
            if random.random() < 0.5:
                if max_iou < 0.10 or max_iou > 0.24:
                    continue
            else:
                if max_iou < 0.25 or max_iou > 0.40:
                    continue
            
            if max_iou > 0.50:
                continue
            
            return x, y, visible_bbox
        
        return None
    
    def collect_objects(
        self,
        images_dir: Path,
        labels_dir: Path
    ) -> Dict[int, List[Tuple]]:
        """
        Собрать все объекты редких классов из датасета.
        
        Returns:
            {class_id: [(img, bbox, tile_name), ...]}
        """
        objects = {cls: [] for cls in self.rare_classes}
        
        for label_file in tqdm(list(labels_dir.glob("*.txt")), desc="Collecting objects"):
            img_file = images_dir / f"{label_file.stem}.png"
            
            if not img_file.exists():
                continue
            
            img = cv2.imread(str(img_file), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            
            h, w = img.shape
            
            with open(label_file, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) < 5:
                        continue
                    
                    class_id = int(parts[0])
                    if class_id not in self.rare_classes:
                        continue
                    
                    yolo_bbox = tuple(map(float, parts[1:5]))
                    bbox = self._yolo_to_bbox(yolo_bbox, w, h)
                    
                    objects[class_id].append((img, bbox, label_file.stem))
        
        print(f"\nСобрано объектов редких классов:")
        for cls in sorted(self.rare_classes):
            print(f"  Класс {cls:2d}: {len(objects[cls]):3d} объектов")
        
        return objects
    
    def _get_target_count(self, class_id: int) -> int:
        """Получить целевое количество для класса."""
        if class_id in self.critical_classes:
            return self.target_counts.get("critical", 200)
        return self.target_counts.get("rare", 150)
    
    def _load_forbidden_mask(
        self,
        tile_name: str,
        masks_dir: Path
    ) -> Optional[np.ndarray]:
        """Загрузить forbidden маску для тайла."""
        mask_path = masks_dir / f"{tile_name}_forbidden.png"
        
        if not mask_path.exists():
            return None
        
        return cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
    
    def augment(
        self,
        images_dir: Path,
        labels_dir: Path,
        output_dir: Path,
        masks_dir: Optional[Path] = None,
        create_synthetic_tiles: bool = True,
        synthetic_tile_size: int = 1280,
        max_synthetic_tiles: int = 300,
        objects_per_synthetic: Tuple[int, int] = (3, 8)
    ) -> Dict:
        """
        Выполнить Copy-Paste аугментацию.
        
        Args:
            images_dir: Директория с изображениями
            labels_dir: Директория с аннотациями
            output_dir: Директория для результатов
            masks_dir: Директория с forbidden масками
            create_synthetic_tiles: Создавать синтетические тайлы для больших объектов
            synthetic_tile_size: Размер синтетических тайлов
            max_synthetic_tiles: Максимум синтетических тайлов
            objects_per_synthetic: (min, max) объектов на синтетический тайл
            
        Returns:
            Статистика аугментации
        """
        images_dir = Path(images_dir)
        labels_dir = Path(labels_dir)
        output_dir = Path(output_dir)
        
        output_images = output_dir / "images"
        output_labels = output_dir / "labels"
        
        output_images.mkdir(parents=True, exist_ok=True)
        output_labels.mkdir(parents=True, exist_ok=True)
        
        print("\n" + "="*60)
        print("COPY-PASTE AUGMENTATION")
        print("="*60)
        
        # Копировать исходные данные
        print("\nКопирование исходных данных...")
        for img_file in tqdm(list(images_dir.glob("*.png")), desc="Copying"):
            shutil.copy(img_file, output_images / img_file.name)
            label_file = labels_dir / f"{img_file.stem}.txt"
            if label_file.exists():
                shutil.copy(label_file, output_labels / f"{img_file.stem}.txt")
        
        # Собрать объекты редких классов
        objects = self.collect_objects(images_dir, labels_dir)
        
        # Текущее количество
        current_counts = defaultdict(int)
        for label_file in labels_dir.glob("*.txt"):
            with open(label_file, "r") as f:
                for line in f:
                    parts = line.strip().split()
                    if parts:
                        current_counts[int(parts[0])] += 1
        
        # Статистика
        stats = {
            "inserted": defaultdict(int),
            "failed": defaultdict(int),
            "by_type": {
                "full_visible": defaultdict(int),
                "partial": defaultdict(int),
                "occlusion": defaultdict(int)
            }
        }
        
        failed_crops = []  # Для синтетических тайлов
        tile_files = list(output_images.glob("*.png"))
        
        # Фильтрация: вставлять только на тайлы с масками труб/узлов
        if masks_dir:
            tiles_with_masks = []
            for tf in tile_files:
                mask_path = masks_dir / f"{tf.stem}_forbidden.png"
                if mask_path.exists():
                    tiles_with_masks.append(tf)
            print(f"\nТайлов с масками (для CPA): {len(tiles_with_masks)} из {len(tile_files)}")
            if tiles_with_masks:
                tile_files_for_paste = tiles_with_masks
            else:
                print("  Предупреждение: масок не найдено, используем все тайлы")
                tile_files_for_paste = tile_files
        else:
            tile_files_for_paste = tile_files
        
        # Аугментация для каждого редкого класса
        for class_id in self.rare_classes:
            current = current_counts.get(class_id, 0)
            target = self._get_target_count(class_id)
            needed = target - current
            
            if needed <= 0:
                continue
            
            source_objects = objects.get(class_id, [])
            if not source_objects:
                continue
            
            print(f"\nКласс {class_id}: нужно добавить {needed} объектов")
            pbar = tqdm(total=needed, desc="Pasting")
            
            inserted = 0
            attempts = 0
            max_attempts_total = needed * 10
            consecutive_failures = 0
            
            while inserted < needed and attempts < max_attempts_total:
                attempts += 1
                
                # Выбрать случайный объект
                img, bbox, source_name = random.choice(source_objects)
                
                # Вырезать с маской
                crop, mask = self._extract_object_with_mask(img, bbox, padding=0.15)
                
                if crop is None or crop.shape[0] < self.min_object_size or crop.shape[1] < self.min_object_size:
                    continue
                
                # Аугментировать
                crop_aug, mask_aug = self._apply_augmentations(crop, mask)
                
                # Выбрать тип вставки
                rand = random.random()
                if rand < self.placement_rules["full_visible"]:
                    insertion_type = "full_visible"
                elif rand < self.placement_rules["full_visible"] + self.placement_rules["partial"]:
                    insertion_type = "partial"
                else:
                    insertion_type = "occlusion"
                
                # Выбрать случайный тайл (только с масками труб/узлов)
                tile_file = random.choice(tile_files_for_paste)
                tile = cv2.imread(str(tile_file), cv2.IMREAD_GRAYSCALE)
                
                if tile is None:
                    continue
                
                # Загрузить forbidden маску
                forbidden_mask = None
                if masks_dir:
                    forbidden_mask = self._load_forbidden_mask(tile_file.stem, masks_dir)
                
                # Существующие bbox
                label_file = output_labels / f"{tile_file.stem}.txt"
                existing_bboxes = []
                if label_file.exists():
                    with open(label_file, "r") as f:
                        for line in f:
                            parts = line.strip().split()
                            if len(parts) >= 5:
                                existing_bboxes.append(tuple(map(float, parts[1:5])))
                
                # Найти позицию
                result = None
                if insertion_type == "full_visible":
                    result = self._find_position_full_visible(
                        tile, crop_aug, forbidden_mask, existing_bboxes
                    )
                elif insertion_type == "partial":
                    result = self._find_position_partial(
                        tile, crop_aug, forbidden_mask, existing_bboxes
                    )
                else:
                    result = self._find_position_occlusion(
                        tile, crop_aug, forbidden_mask, existing_bboxes
                    )
                
                if result is None:
                    stats["failed"][class_id] += 1
                    consecutive_failures += 1
                    
                    if consecutive_failures >= 5:
                        failed_crops.append((class_id, crop_aug.copy(), mask_aug.copy()))
                        consecutive_failures = 0
                    continue
                
                x, y, new_bbox = result
                
                # Вставить
                tile = self._insert_object(tile, crop_aug, mask_aug, x, y)
                cv2.imwrite(str(tile_file), tile)
                
                # Добавить аннотацию
                with open(label_file, "a") as f:
                    f.write(f"{class_id} {new_bbox[0]:.6f} {new_bbox[1]:.6f} "
                           f"{new_bbox[2]:.6f} {new_bbox[3]:.6f}\n")
                
                stats["inserted"][class_id] += 1
                stats["by_type"][insertion_type][class_id] += 1
                
                inserted += 1
                consecutive_failures = 0
                pbar.update(1)
            
            pbar.close()
        
        # Синтетические тайлы для больших объектов
        if create_synthetic_tiles and failed_crops:
            print(f"\nСоздание синтетических тайлов для {len(failed_crops)} больших объектов...")
            
            random.shuffle(failed_crops)
            
            tile_idx = 0
            crop_idx = 0
            
            while crop_idx < len(failed_crops) and tile_idx < max_synthetic_tiles:
                # Белый тайл
                synthetic_tile = np.ones(
                    (synthetic_tile_size, synthetic_tile_size), dtype=np.uint8
                ) * 255
                
                num_objects = random.randint(*objects_per_synthetic)
                num_objects = min(num_objects, len(failed_crops) - crop_idx)
                
                if num_objects == 0:
                    break
                
                placed_bboxes = []
                placed_objects = []
                
                for _ in range(num_objects):
                    if crop_idx >= len(failed_crops):
                        break
                    
                    class_id, crop, mask = failed_crops[crop_idx]
                    crop_h, crop_w = crop.shape
                    
                    position_found = False
                    
                    for _ in range(self.max_attempts):
                        if crop_w >= synthetic_tile_size or crop_h >= synthetic_tile_size:
                            x = (synthetic_tile_size - crop_w) // 2
                            y = (synthetic_tile_size - crop_h) // 2
                        else:
                            x = random.randint(0, synthetic_tile_size - crop_w)
                            y = random.randint(0, synthetic_tile_size - crop_h)
                        
                        new_bbox = (
                            (x + crop_w / 2) / synthetic_tile_size,
                            (y + crop_h / 2) / synthetic_tile_size,
                            crop_w / synthetic_tile_size,
                            crop_h / synthetic_tile_size
                        )
                        
                        overlap = any(
                            self._calculate_iou(new_bbox, ex) > 0.05
                            for ex in placed_bboxes
                        )
                        
                        if not overlap:
                            synthetic_tile = self._insert_object(
                                synthetic_tile, crop, mask, x, y
                            )
                            placed_bboxes.append(new_bbox)
                            placed_objects.append((class_id, new_bbox))
                            position_found = True
                            break
                    
                    crop_idx += 1 if position_found else 1
                
                if placed_objects:
                    tile_name = f"synthetic_{tile_idx:04d}"
                    
                    cv2.imwrite(str(output_images / f"{tile_name}.png"), synthetic_tile)
                    
                    with open(output_labels / f"{tile_name}.txt", "w") as f:
                        for class_id, bbox in placed_objects:
                            f.write(f"{class_id} {bbox[0]:.6f} {bbox[1]:.6f} "
                                   f"{bbox[2]:.6f} {bbox[3]:.6f}\n")
                            stats["inserted"][class_id] += 1
                    
                    tile_idx += 1
            
            stats["synthetic_tiles_created"] = tile_idx
        
        # Итоговая статистика
        print(f"\n=== Результаты CPA ===")
        print(f"Добавлено объектов:")
        for cls_id in sorted(stats["inserted"].keys()):
            print(f"  Класс {cls_id:2d}: {stats['inserted'][cls_id]}")
        
        if "synthetic_tiles_created" in stats:
            print(f"Синтетических тайлов: {stats['synthetic_tiles_created']}")
        
        return stats

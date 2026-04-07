"""
Rotation Augmentation для P&ID Node Detection.

НАЗНАЧЕНИЕ:
----------
Этот модуль реализует аугментацию поворотами на 90°, 180°, 270°.
Применяется ПОСЛЕ Copy-Paste аугментации для дополнительного 
увеличения датасета.

ПОЧЕМУ ПОВОРОТЫ:
---------------
1. P&ID схемы не имеют "правильной" ориентации
2. Оборудование может быть повернуто в любую сторону
3. Простой способ увеличить датасет в 2-4 раза
4. Не требует дополнительных масок или сложной логики

РЕЖИМЫ:
------
- "all": все 3 поворота + оригинал = 4x данных
- "random_1": случайный 1 поворот + оригинал = 2x данных
- "random_2": случайные 2 поворота + оригинал = 3x данных

ИСПОЛЬЗОВАНИЕ:
-------------
    from pid_node_detection.augmentation import RotationAugmentation
    
    rotation = RotationAugmentation(mode="all")
    rotation.augment(
        images_dir=Path("./train_cpa/images"),
        labels_dir=Path("./train_cpa/labels"),
        output_dir=Path("./train_final")
    )
"""

import cv2
import numpy as np
import random
import shutil
from pathlib import Path
from typing import List, Tuple, Dict
from tqdm import tqdm


class RotationAugmentation:
    """
    Аугментация поворотами на 90°, 180°, 270°.
    
    Attributes:
        mode: Режим аугментации ("all", "random_1", "random_2")
        angles: Список углов поворота
        random_seed: Seed для воспроизводимости
    """
    
    def __init__(
        self,
        mode: str = "all",
        angles: List[int] = [90, 180, 270],
        random_seed: int = 42
    ):
        """
        Args:
            mode: Режим аугментации:
                  - "all": все повороты
                  - "random_1": 1 случайный поворот
                  - "random_2": 2 случайных поворота
            angles: Углы поворота (по умолчанию 90, 180, 270)
            random_seed: Seed для воспроизводимости
        """
        self.mode = mode
        self.angles = angles
        self.random_seed = random_seed
        
        random.seed(random_seed)
        np.random.seed(random_seed)
    
    def _rotate_image(self, img: np.ndarray, angle: int) -> np.ndarray:
        """Поворот изображения на заданный угол."""
        if angle == 90:
            return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
        elif angle == 180:
            return cv2.rotate(img, cv2.ROTATE_180)
        elif angle == 270:
            return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
        return img
    
    def _rotate_bbox(
        self,
        x_center: float,
        y_center: float,
        width: float,
        height: float,
        angle: int
    ) -> Tuple[float, float, float, float]:
        """Поворот YOLO bbox на заданный угол (координаты 0-1)."""
        if angle == 90:
            new_x = 1.0 - y_center
            new_y = x_center
            new_w = height
            new_h = width
        elif angle == 180:
            new_x = 1.0 - x_center
            new_y = 1.0 - y_center
            new_w = width
            new_h = height
        elif angle == 270:
            new_x = y_center
            new_y = 1.0 - x_center
            new_w = height
            new_h = width
        else:
            new_x, new_y, new_w, new_h = x_center, y_center, width, height
        
        return new_x, new_y, new_w, new_h
    
    def _rotate_labels(self, label_path: Path, angle: int) -> List[str]:
        """Поворот всех bbox в файле аннотаций."""
        rotated_lines = []
        
        if not label_path.exists():
            return rotated_lines
        
        with open(label_path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 5:
                    continue
                
                try:
                    class_id = int(parts[0])
                    x_center = float(parts[1])
                    y_center = float(parts[2])
                    width = float(parts[3])
                    height = float(parts[4])
                    
                    new_x, new_y, new_w, new_h = self._rotate_bbox(
                        x_center, y_center, width, height, angle
                    )
                    
                    rotated_lines.append(
                        f"{class_id} {new_x:.6f} {new_y:.6f} {new_w:.6f} {new_h:.6f}\n"
                    )
                except (ValueError, IndexError):
                    continue
        
        return rotated_lines
    
    def _get_angles_for_file(self) -> List[int]:
        """Получить список углов для текущего файла."""
        if self.mode == "all":
            return self.angles.copy()
        elif self.mode == "random_1":
            return [random.choice(self.angles)]
        elif self.mode == "random_2":
            return random.sample(self.angles, min(2, len(self.angles)))
        return self.angles.copy()
    
    def augment(
        self,
        images_dir: Path,
        labels_dir: Path,
        output_dir: Path,
        copy_originals: bool = True
    ) -> Dict:
        """
        Выполнить аугментацию поворотами.
        
        Args:
            images_dir: Директория с изображениями
            labels_dir: Директория с аннотациями
            output_dir: Директория для результатов
            copy_originals: Копировать оригинальные файлы
            
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
        print("ROTATION AUGMENTATION")
        print("="*60)
        print(f"Mode: {self.mode}")
        print(f"Angles: {self.angles}")
        
        stats = {
            "original_files": 0,
            "rotated_files": 0,
            "total_files": 0,
            "by_angle": {angle: 0 for angle in self.angles}
        }
        
        image_files = sorted(images_dir.glob("*.png"))
        stats["original_files"] = len(image_files)
        
        print(f"Исходных файлов: {len(image_files)}")
        
        for img_path in tqdm(image_files, desc="Rotating", unit="img"):
            base_name = img_path.stem
            label_path = labels_dir / f"{base_name}.txt"
            
            img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            
            # Копировать оригинал
            if copy_originals:
                shutil.copy(img_path, output_images / f"{base_name}.png")
                if label_path.exists():
                    shutil.copy(label_path, output_labels / f"{base_name}.txt")
                stats["total_files"] += 1
            
            # Создать повернутые версии
            angles_to_apply = self._get_angles_for_file()
            
            for angle in angles_to_apply:
                rotated_name = f"{base_name}_rot{angle}"
                
                rotated_img = self._rotate_image(img, angle)
                cv2.imwrite(str(output_images / f"{rotated_name}.png"), rotated_img)
                
                rotated_labels = self._rotate_labels(label_path, angle)
                with open(output_labels / f"{rotated_name}.txt", "w", encoding="utf-8") as f:
                    f.writelines(rotated_labels)
                
                stats["rotated_files"] += 1
                stats["by_angle"][angle] += 1
                stats["total_files"] += 1
        
        print(f"\n=== Результаты Rotation ===")
        print(f"Исходных файлов: {stats['original_files']}")
        print(f"Создано поворотов: {stats['rotated_files']}")
        print(f"Итого файлов: {stats['total_files']}")
        
        return stats

"""
Node Detector для P&ID Node Detection.

НАЗНАЧЕНИЕ:
----------
Этот модуль выполняет детекцию узлов на P&ID схемах с использованием
обученной YOLO модели и SAHI (Slicing Aided Hyper Inference).

ПОЧЕМУ SAHI:
-----------
1. P&ID схемы большие (4900x3500+ пикселей)
2. YOLO обучена на тайлах 1280x1280
3. SAHI режет изображение на слайсы, детектирует на каждом, объединяет результаты
4. Автоматически обрабатывает NMS для перекрывающихся детекций

АДАПТИВНЫЕ ПАРАМЕТРЫ:
--------------------
Размер слайса и overlap подбираются автоматически под размер изображения:
- <5000px: 1280x1280, overlap 0.25
- 5000-8000px: 1600x1600, overlap 0.30
- 8000-11000px: 1920x1920, overlap 0.35
- 11000-16000px: 2560x2560, overlap 0.40
- 16000-23000px: 3200x3200, overlap 0.40
- >23000px: 4096x4096, overlap 0.45

ИСПОЛЬЗОВАНИЕ:
-------------
    from pid_node_detection.inference import NodeDetector
    
    detector = NodeDetector(
        weights=Path("./experiments/train_v1/weights/best.pt"),
        confidence=0.8
    )
    
    # Детекция на одном изображении
    detections = detector.detect(Path("./images/scheme1.png"))
    
    # Детекция на директории
    detector.detect_batch(
        input_dir=Path("./images"),
        output_dir=Path("./predictions")
    )
"""

import cv2
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Union
from tqdm import tqdm

from pid_node_detection.data.preprocessing import binarize_for_yolo


class NodeDetector:
    """
    Детектор узлов на P&ID схемах с адаптивным SAHI inference.

    Attributes:
        weights: Путь к весам YOLO модели
        confidence: Порог уверенности
        iou_threshold: Порог IoU для NMS
        device: Устройство для инференса
        use_sahi: Использовать SAHI для слайсинга
    """

    def __init__(
        self,
        weights: Union[str, Path],
        confidence: float = 0.8,
        iou_threshold: float = 0.5,
        device: str = "cuda",
        use_sahi: bool = True,
        fixed_slice_size: int = 1280,
        fixed_overlap: float = 0.25,
        class_names: Optional[Dict[int, str]] = None,
        reverse_reindex: Optional[Dict[int, int]] = None
    ):
        """
        Args:
            weights: Путь к весам YOLO модели
            confidence: Порог уверенности для детекций
            iou_threshold: Порог IoU для NMS
            device: Устройство ("cuda", "cpu")
            use_sahi: Использовать SAHI для слайсинга
            fixed_slice_size: Размер тайла SAHI (пикс.)
            fixed_overlap: Overlap между тайлами (0-1)
            class_names: Маппинг id -> имя класса
            reverse_reindex: Маппинг для обратной переиндексации (34->35, 35->38)
        """
        self.weights = Path(weights)
        self.confidence = confidence
        self.iou_threshold = iou_threshold
        self.device = device
        self.use_sahi = use_sahi
        self.fixed_slice_size = fixed_slice_size
        self.fixed_overlap = fixed_overlap
        self.class_names = class_names or {}
        self.reverse_reindex = reverse_reindex or {}

        self._model = None
        self._sahi_model = None

    def _load_model(self):
        """Ленивая загрузка модели."""
        if self._model is None:
            try:
                from ultralytics import YOLO
            except ImportError:
                raise ImportError(
                    "ultralytics не установлен. Установите: pip install ultralytics"
                )

            if not self.weights.exists():
                raise FileNotFoundError(f"Веса не найдены: {self.weights}")

            self._model = YOLO(str(self.weights))

    def _load_sahi_model(self):
        """Ленивая загрузка SAHI модели."""
        if self._sahi_model is None:
            try:
                from sahi import AutoDetectionModel
            except ImportError:
                raise ImportError(
                    "sahi не установлен. Установите: pip install sahi"
                )

            self._sahi_model = AutoDetectionModel.from_pretrained(
                model_type="yolov8",
                model_path=str(self.weights),
                confidence_threshold=self.confidence,
                device=self.device
            )

    def _get_slice_params(
        self,
        img_width: int,
        img_height: int
    ) -> Tuple[int, float]:
        """
        Получить параметры слайсинга.

        Returns:
            Tuple (slice_size, overlap)
        """
        return self.fixed_slice_size, self.fixed_overlap

    def _convert_to_grayscale(self, img_path: Path) -> np.ndarray:
        """
        Бинаризация + 3 канала для YOLO.

        Использует единую binarize_for_yolo() из preprocessing.py.
        """
        return binarize_for_yolo(img_path, method="full")

    def _apply_reverse_reindex(
        self,
        detections: List[Dict]
    ) -> List[Dict]:
        """Применить обратную переиндексацию классов."""
        if not self.reverse_reindex:
            return detections

        for det in detections:
            class_id = det["class_id"]
            if class_id in self.reverse_reindex:
                det["class_id"] = self.reverse_reindex[class_id]

        return detections

    def detect(
        self,
        image: Union[str, Path, np.ndarray],
        apply_grayscale: bool = True,
        apply_reverse_mapping: bool = True
    ) -> List[Dict]:
        """
        Детекция на одном изображении.

        Args:
            image: Путь к изображению или numpy array
            apply_grayscale: Конвертировать в grayscale перед детекцией
            apply_reverse_mapping: Применять обратную переиндексацию классов

        Returns:
            Список детекций: [{class_id, x_center, y_center, width, height, confidence}]
            Координаты нормализованы (0-1).

        Example:
            >>> detector = NodeDetector(weights=Path("best.pt"))
            >>> detections = detector.detect(Path("scheme.png"))
            >>> for det in detections:
            ...     print(f"Class {det['class_id']}: {det['confidence']:.2f}")
        """
        # Загрузить изображение
        if isinstance(image, (str, Path)):
            image_path = Path(image)
            if apply_grayscale:
                img = self._convert_to_grayscale(image_path)
            else:
                img = cv2.imread(str(image_path))
        else:
            img = image

        if img is None:
            raise ValueError("Не удалось загрузить изображение")

        img_height, img_width = img.shape[:2]

        detections = []

        if self.use_sahi:
            detections = self._detect_with_sahi(img, img_width, img_height)
        else:
            detections = self._detect_without_sahi(img, img_width, img_height)

        # Применить обратную переиндексацию
        if apply_reverse_mapping:
            detections = self._apply_reverse_reindex(detections)

        return detections

    def _detect_with_sahi(
        self,
        img: np.ndarray,
        img_width: int,
        img_height: int
    ) -> List[Dict]:
        """Детекция с использованием SAHI."""
        try:
            from sahi.predict import get_sliced_prediction
        except ImportError:
            raise ImportError(
                "sahi не установлен. Установите: pip install sahi"
            )

        self._load_sahi_model()

        # Получить параметры слайсинга
        slice_size, overlap = self._get_slice_params(img_width, img_height)
        overlap_ratio = overlap

        # SAHI prediction
        result = get_sliced_prediction(
            image=img,
            detection_model=self._sahi_model,
            slice_height=slice_size,
            slice_width=slice_size,
            overlap_height_ratio=overlap_ratio,
            overlap_width_ratio=overlap_ratio,
            perform_standard_pred=False,
            postprocess_type="NMS",
            postprocess_match_metric="IOU",
            postprocess_match_threshold=self.iou_threshold
        )

        detections = []

        for pred in result.object_prediction_list:
            bbox = pred.bbox
            x1, y1, x2, y2 = bbox.minx, bbox.miny, bbox.maxx, bbox.maxy

            # Конвертация в YOLO формат (нормализованные координаты)
            x_center = (x1 + x2) / 2 / img_width
            y_center = (y1 + y2) / 2 / img_height
            width = (x2 - x1) / img_width
            height = (y2 - y1) / img_height

            detections.append({
                "class_id": pred.category.id,
                "class_name": pred.category.name,
                "x_center": x_center,
                "y_center": y_center,
                "width": width,
                "height": height,
                "confidence": pred.score.value
            })

        return detections

    def _detect_without_sahi(
        self,
        img: np.ndarray,
        img_width: int,
        img_height: int
    ) -> List[Dict]:
        """Детекция без SAHI (прямой инференс)."""
        self._load_model()

        results = self._model(
            img,
            conf=self.confidence,
            iou=self.iou_threshold,
            device=self.device,
            verbose=False
        )

        detections = []

        for result in results:
            boxes = result.boxes

            for i in range(len(boxes)):
                xyxy = boxes.xyxy[i].cpu().numpy()
                x1, y1, x2, y2 = xyxy

                x_center = (x1 + x2) / 2 / img_width
                y_center = (y1 + y2) / 2 / img_height
                width = (x2 - x1) / img_width
                height = (y2 - y1) / img_height

                class_id = int(boxes.cls[i].cpu().numpy())
                confidence = float(boxes.conf[i].cpu().numpy())

                detections.append({
                    "class_id": class_id,
                    "class_name": self.class_names.get(class_id, f"class_{class_id}"),
                    "x_center": x_center,
                    "y_center": y_center,
                    "width": width,
                    "height": height,
                    "confidence": confidence
                })

        return detections

    def detect_batch(
        self,
        input_dir: Path,
        output_dir: Path,
        save_format: str = "yolo",
        image_extensions: List[str] = [".png", ".jpg", ".jpeg"],
        apply_grayscale: bool = True,
        apply_reverse_mapping: bool = True,
        save_confidence: bool = False
    ) -> Dict:
        """
        Детекция на всех изображениях в директории.

        Args:
            input_dir: Директория с изображениями
            output_dir: Директория для сохранения результатов
            save_format: Формат сохранения ("yolo", "coco", "json")
            image_extensions: Расширения файлов для обработки
            apply_grayscale: Конвертировать в grayscale
            apply_reverse_mapping: Применять обратную переиндексацию
            save_confidence: Сохранять confidence в YOLO формате

        Returns:
            Статистика: {processed, total_detections, by_class}

        Example:
            >>> detector = NodeDetector(weights=Path("best.pt"))
            >>> stats = detector.detect_batch(
            ...     input_dir=Path("./test/images"),
            ...     output_dir=Path("./predictions")
            ... )
        """
        input_dir = Path(input_dir)
        output_dir = Path(output_dir)

        output_dir.mkdir(parents=True, exist_ok=True)

        # Собрать файлы
        image_files = []
        for ext in image_extensions:
            image_files.extend(input_dir.glob(f"*{ext}"))

        print(f"\nДетекция на {len(image_files)} изображениях...")

        stats = {
            "processed": 0,
            "total_detections": 0,
            "by_class": {}
        }

        for img_path in tqdm(image_files, desc="Detecting", unit="img"):
            try:
                detections = self.detect(
                    img_path,
                    apply_grayscale=apply_grayscale,
                    apply_reverse_mapping=apply_reverse_mapping
                )

                # Сохранить результаты
                if save_format == "yolo":
                    self._save_yolo(
                        detections,
                        output_dir / f"{img_path.stem}.txt",
                        save_confidence=save_confidence
                    )
                elif save_format == "json":
                    self._save_json(
                        detections,
                        output_dir / f"{img_path.stem}.json"
                    )

                # Статистика
                stats["processed"] += 1
                stats["total_detections"] += len(detections)

                for det in detections:
                    class_id = det["class_id"]
                    stats["by_class"][class_id] = stats["by_class"].get(class_id, 0) + 1

            except Exception as e:
                print(f"Ошибка при обработке {img_path.name}: {e}")
                continue

        print(f"\n=== Результаты ===")
        print(f"Обработано: {stats['processed']}")
        print(f"Всего детекций: {stats['total_detections']}")

        return stats

    def _save_yolo(
        self,
        detections: List[Dict],
        output_path: Path,
        save_confidence: bool = False
    ) -> None:
        """Сохранить детекции в YOLO формате."""
        with open(output_path, "w", encoding="utf-8") as f:
            for det in detections:
                line = (f"{det['class_id']} {det['x_center']:.6f} "
                       f"{det['y_center']:.6f} {det['width']:.6f} "
                       f"{det['height']:.6f}")
                if save_confidence:
                    line += f" {det['confidence']:.4f}"
                f.write(line + "\n")

    def _save_json(
        self,
        detections: List[Dict],
        output_path: Path
    ) -> None:
        """Сохранить детекции в JSON формате."""
        import json

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(detections, f, indent=2)


def detect_single_image(
    image_path: Path,
    weights_path: Path,
    output_path: Optional[Path] = None,
    confidence: float = 0.8,
    apply_grayscale: bool = True
) -> List[Dict]:
    """
    Утилитарная функция для детекции на одном изображении.

    Args:
        image_path: Путь к изображению
        weights_path: Путь к весам модели
        output_path: Путь для сохранения результатов (опционально)
        confidence: Порог уверенности
        apply_grayscale: Конвертировать в grayscale

    Returns:
        Список детекций
    """
    detector = NodeDetector(
        weights=weights_path,
        confidence=confidence
    )

    detections = detector.detect(
        image_path,
        apply_grayscale=apply_grayscale
    )

    if output_path:
        detector._save_yolo(detections, output_path)
        print(f"Результаты сохранены: {output_path}")

    return detections
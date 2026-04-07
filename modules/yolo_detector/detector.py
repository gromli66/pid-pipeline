"""
Node Detector - YOLO + SAHI inference для P&ID схем.

Особенности:
- Preprocessing идентичный обучению: NLM → CLAHE → Otsu → Erode
- Адаптивный SAHI слайсинг под размер изображения
- Поддержка CUDA/CPU
- Обратная переиндексация классов (reverse_reindex): 34→35 (output), 35→38 (strelka)
- Class-agnostic NMS для фильтрации дубликатов между разными классами
"""

import cv2
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Union

from modules.yolo_detector.config import CLASS_NAMES, REVERSE_REINDEX


class NodeDetector:
    """
    Детектор узлов на P&ID схемах с адаптивным SAHI inference.

    Пример:
        detector = NodeDetector(weights="path/to/best.pt", confidence=0.8)
        detections = detector.detect("path/to/scheme.png")

        for det in detections:
            print(f"{det['class_name']}: {det['confidence']:.2f}")
    """

    # Параметры preprocessing (фиксированные, как при обучении)
    NLM_H = 10
    NLM_TEMPLATE_WINDOW = 7
    NLM_SEARCH_WINDOW = 21
    CLAHE_CLIP_LIMIT = 2.0
    CLAHE_TILE_SIZE = (8, 8)
    ERODE_KERNEL_SIZE = (2, 2)
    ERODE_ITERATIONS = 1

    def __init__(
        self,
        weights: Union[str, Path],
        confidence: float = 0.8,
        iou_threshold: float = 0.5,
        device: str = "cuda",
        use_sahi: bool = True,
        sahi_slice_size: int = 1280,
        sahi_overlap_ratio: float = 0.25,
        apply_preprocessing: bool = False,
        class_agnostic_nms: bool = False,
        class_names: Optional[Dict[int, str]] = None,
        reverse_reindex: Optional[Dict[int, int]] = None,
    ):
        """
        Args:
            weights: Путь к весам YOLO модели (.pt файл)
            confidence: Порог уверенности (0-1)
            iou_threshold: Порог IoU для NMS
            device: "cuda" или "cpu"
            use_sahi: Использовать SAHI для больших изображений
            sahi_slice_size: Размер тайла SAHI (пикс.)
            sahi_overlap_ratio: Overlap между тайлами (0-1)
            apply_preprocessing: Применять preprocessing (NLM+CLAHE+Otsu+Erode)
            class_agnostic_nms: Cross-class NMS - фильтрация дубликатов между разными классами.
                                Если True, боксы разных классов с высоким IoU будут отфильтрованы,
                                останется бокс с большим confidence. Решает проблему дубликатов
                                на границах тайлов, когда один объект детектируется как разные классы.
            class_names: Маппинг id → имя класса (если None, используется из config)
            reverse_reindex: Маппинг для обратной переиндексации (если None, используется из config)
        """
        self.weights = Path(weights)
        self.confidence = confidence
        self.iou_threshold = iou_threshold
        self.device = device
        self.use_sahi = use_sahi
        self.sahi_slice_size = sahi_slice_size
        self.sahi_overlap_ratio = sahi_overlap_ratio
        self.apply_preprocessing = apply_preprocessing
        self.class_agnostic_nms = class_agnostic_nms

        # Маппинги классов
        self.class_names = class_names if class_names is not None else CLASS_NAMES
        self.reverse_reindex = reverse_reindex if reverse_reindex is not None else REVERSE_REINDEX

        self._model = None
        self._sahi_model = None

    def _load_model(self) -> None:
        """Ленивая загрузка YOLO модели."""
        if self._model is None:
            from ultralytics import YOLO

            if not self.weights.exists():
                raise FileNotFoundError(f"Веса не найдены: {self.weights}")

            self._model = YOLO(str(self.weights))
            print(f"✅ YOLO модель загружена: {self.weights.name}")

    def _load_sahi_model(self) -> None:
        """Ленивая загрузка SAHI модели."""
        if self._sahi_model is None:
            from sahi import AutoDetectionModel

            self._sahi_model = AutoDetectionModel.from_pretrained(
                model_type="yolov8",
                model_path=str(self.weights),
                confidence_threshold=self.confidence,
                device=self.device
            )
            print(f"✅ SAHI модель загружена")

    def _preprocess_image(self, image: np.ndarray) -> np.ndarray:
        """
        Preprocessing идентичный обучению: NLM → CLAHE → Otsu → Erode.

        Args:
            image: BGR изображение (numpy array)

        Returns:
            Предобработанное изображение (3 канала BGR)
        """
        # 1. Конвертация в grayscale
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image

        # 2. NLM - шумоподавление с сохранением краёв
        filtered = cv2.fastNlMeansDenoising(
            gray,
            None,
            h=self.NLM_H,
            templateWindowSize=self.NLM_TEMPLATE_WINDOW,
            searchWindowSize=self.NLM_SEARCH_WINDOW
        )

        # 3. CLAHE - адаптивное выравнивание гистограммы
        clahe = cv2.createCLAHE(
            clipLimit=self.CLAHE_CLIP_LIMIT,
            tileGridSize=self.CLAHE_TILE_SIZE
        )
        enhanced = clahe.apply(filtered)

        # 4. Otsu - автоматическая бинаризация
        _, binary = cv2.threshold(
            enhanced, 0, 255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )

        # 5. Erode - утолщение чёрных линий (erode на белом фоне)
        kernel = np.ones(self.ERODE_KERNEL_SIZE, np.uint8)
        dilated = cv2.erode(binary, kernel, iterations=self.ERODE_ITERATIONS)

        # 6. Обратно в 3 канала для YOLO
        return cv2.cvtColor(dilated, cv2.COLOR_GRAY2BGR)

    def _apply_reverse_reindex(self, detections: List[Dict]) -> List[Dict]:
        """
        Применить обратную переиндексацию классов.

        Модель обучена на классах 0-35, где:
        - 34 = output (изначально было 35 в разметке)
        - 35 = strelka (изначально было 38 в разметке)

        Этот метод возвращает class_id к исходным значениям разметки.
        """
        if not self.reverse_reindex:
            return detections

        for det in detections:
            class_id = det["class_id"]
            if class_id in self.reverse_reindex:
                det["class_id"] = self.reverse_reindex[class_id]
                # Обновляем имя класса если есть
                if det.get("class_name"):
                    new_id = det["class_id"]
                    det["class_name"] = self.class_names.get(new_id, f"class_{new_id}")

        return detections

    def detect(
        self,
        image: Union[str, Path, np.ndarray],
        return_absolute: bool = False,
        apply_reverse_mapping: bool = True,
    ) -> List[Dict]:
        """
        Детекция объектов на изображении.

        Args:
            image: Путь к изображению или numpy array (BGR)
            return_absolute: Вернуть абсолютные координаты (пиксели) вместо нормализованных
            apply_reverse_mapping: Применять обратную переиндексацию классов

        Returns:
            Список детекций:
            [
                {
                    "class_id": int,
                    "class_name": str,
                    "x_center": float,  # нормализованные 0-1 или пиксели
                    "y_center": float,
                    "width": float,
                    "height": float,
                    "confidence": float,
                    "bbox": [x1, y1, x2, y2]  # абсолютные координаты
                },
                ...
            ]
        """
        # Загрузить изображение
        if isinstance(image, (str, Path)):
            img = cv2.imread(str(image))
            if img is None:
                raise ValueError(f"Не удалось загрузить: {image}")
        else:
            img = image.copy()

        # Сохраняем оригинальные размеры
        img_height, img_width = img.shape[:2]

        # Preprocessing
        if self.apply_preprocessing:
            img = self._preprocess_image(img)

        # Выбор метода детекции
        if self.use_sahi:
            detections = self._detect_with_sahi(img, img_width, img_height)
        else:
            detections = self._detect_direct(img, img_width, img_height)

        # Применяем обратную переиндексацию
        if apply_reverse_mapping:
            detections = self._apply_reverse_reindex(detections)

        # Конвертация координат если нужно
        if return_absolute:
            for det in detections:
                det["x_center"] *= img_width
                det["y_center"] *= img_height
                det["width"] *= img_width
                det["height"] *= img_height

        return detections

    def _detect_with_sahi(
        self,
        img: np.ndarray,
        img_width: int,
        img_height: int
    ) -> List[Dict]:
        """Детекция с SAHI слайсингом."""
        from sahi.predict import get_sliced_prediction

        self._load_sahi_model()

        slice_size = self.sahi_slice_size
        overlap = self.sahi_overlap_ratio

        result = get_sliced_prediction(
            image=img,
            detection_model=self._sahi_model,
            slice_height=slice_size,
            slice_width=slice_size,
            overlap_height_ratio=overlap,
            overlap_width_ratio=overlap,
            perform_standard_pred=False,
            postprocess_type="NMS",
            postprocess_match_metric="IOU",
            postprocess_match_threshold=self.iou_threshold,
            postprocess_class_agnostic=self.class_agnostic_nms,
            verbose=0,
        )

        detections = []

        for pred in result.object_prediction_list:
            bbox = pred.bbox
            x1, y1, x2, y2 = bbox.minx, bbox.miny, bbox.maxx, bbox.maxy

            class_id = pred.category.id
            class_name = self.class_names.get(class_id, f"class_{class_id}")

            detections.append({
                "class_id": class_id,
                "class_name": class_name,
                "x_center": (x1 + x2) / 2 / img_width,
                "y_center": (y1 + y2) / 2 / img_height,
                "width": (x2 - x1) / img_width,
                "height": (y2 - y1) / img_height,
                "confidence": pred.score.value,
                "bbox": [x1, y1, x2, y2],
            })

        return detections

    def _detect_direct(
        self,
        img: np.ndarray,
        img_width: int,
        img_height: int
    ) -> List[Dict]:
        """Прямая детекция без SAHI."""
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

                class_id = int(boxes.cls[i].cpu().numpy())
                confidence = float(boxes.conf[i].cpu().numpy())
                class_name = self.class_names.get(class_id, f"class_{class_id}")

                detections.append({
                    "class_id": class_id,
                    "class_name": class_name,
                    "x_center": (x1 + x2) / 2 / img_width,
                    "y_center": (y1 + y2) / 2 / img_height,
                    "width": (x2 - x1) / img_width,
                    "height": (y2 - y1) / img_height,
                    "confidence": confidence,
                    "bbox": [float(x1), float(y1), float(x2), float(y2)],
                })

        return detections


def detections_to_yolo(detections: List[Dict], include_confidence: bool = False) -> str:
    """
    Конвертировать детекции в YOLO формат.

    Args:
        detections: Список детекций от NodeDetector
        include_confidence: Добавить confidence в конец строки (по умолчанию False)

    Returns:
        Строка в YOLO формате:
        - include_confidence=False: "class x_center y_center width height"
        - include_confidence=True:  "class x_center y_center width height conf"
    """
    lines = []
    for det in detections:
        line = f"{det['class_id']} {det['x_center']:.6f} {det['y_center']:.6f} {det['width']:.6f} {det['height']:.6f}"
        if include_confidence:
            line += f" {det['confidence']:.4f}"
        lines.append(line)
    return "\n".join(lines)


def detections_to_coco(
    detections: List[Dict],
    image_id: int,
    image_width: int,
    image_height: int,
) -> List[Dict]:
    """
    Конвертировать детекции в COCO формат аннотаций.

    Args:
        detections: Список детекций от NodeDetector
        image_id: ID изображения
        image_width: Ширина изображения
        image_height: Высота изображения

    Returns:
        Список COCO аннотаций
    """
    annotations = []

    for i, det in enumerate(detections):
        # COCO bbox: [x, y, width, height] в абсолютных координатах
        x_center = det["x_center"] * image_width
        y_center = det["y_center"] * image_height
        w = det["width"] * image_width
        h = det["height"] * image_height

        x = x_center - w / 2
        y = y_center - h / 2

        annotations.append({
            "id": i + 1,
            "image_id": image_id,
            "category_id": det["class_id"],
            "bbox": [x, y, w, h],
            "area": w * h,
            "score": det["confidence"],
            "iscrowd": 0,
        })

    return annotations


def resolve_overlaps(
    detections: List[Dict],
    mutual_overlap_threshold: float = 0.7,
) -> List[Dict]:
    """
    Разрешение перекрытий между детекциями после SAHI + NMS.

    Проверяет каждую пару боксов на взаимное перекрытие (intersection / area).
    Срабатывает только когда ОБОИМ боксам пересечение составляет значительную
    долю площади (mutual overlap). Это отсекает ситуацию, когда большой бокс
    покрывает маленький — там ratio для большого будет низким.

    Правила разрешения:
    - Одинаковый класс (A vs A): оставляем бокс с большей площадью.
    - Разный класс (A vs B): оставляем бокс с большим confidence.

    Args:
        detections: Список детекций от NodeDetector.detect().
            Каждая детекция — dict с ключами:
            class_id, class_name, x_center, y_center, width, height,
            confidence, bbox [x1, y1, x2, y2].
        mutual_overlap_threshold: Порог для ОБОИХ ratio_i и ratio_j (0-1).
            При 0.7 — оба бокса должны перекрываться на ≥70% своей площади.

    Returns:
        Отфильтрованный список детекций (без подавленных дубликатов).
    """
    if len(detections) <= 1:
        return detections

    n = len(detections)
    suppressed = [False] * n

    # Предвычисляем площади из bbox [x1, y1, x2, y2]
    areas = []
    for det in detections:
        x1, y1, x2, y2 = det["bbox"]
        areas.append((x2 - x1) * (y2 - y1))

    for i in range(n):
        if suppressed[i]:
            continue

        for j in range(i + 1, n):
            if suppressed[j]:
                continue

            # Считаем intersection
            x1_i, y1_i, x2_i, y2_i = detections[i]["bbox"]
            x1_j, y1_j, x2_j, y2_j = detections[j]["bbox"]

            inter_x1 = max(x1_i, x1_j)
            inter_y1 = max(y1_i, y1_j)
            inter_x2 = min(x2_i, x2_j)
            inter_y2 = min(y2_i, y2_j)

            if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
                continue  # Нет пересечения

            intersection = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)

            # Mutual overlap: доля пересечения для каждого бокса
            area_i = areas[i]
            area_j = areas[j]

            if area_i <= 0 or area_j <= 0:
                continue

            ratio_i = intersection / area_i
            ratio_j = intersection / area_j

            # Срабатываем только при взаимном перекрытии
            if ratio_i < mutual_overlap_threshold or ratio_j < mutual_overlap_threshold:
                continue

            # --- Разрешение конфликта ---
            class_i = detections[i]["class_id"]
            class_j = detections[j]["class_id"]

            if class_i == class_j:
                # Одинаковый класс → оставляем бокс с большей площадью
                if area_i >= area_j:
                    suppressed[j] = True
                else:
                    suppressed[i] = True
                    break  # i подавлен, переходим к следующему i
            else:
                # Разный класс → оставляем бокс с большим confidence
                conf_i = detections[i]["confidence"]
                conf_j = detections[j]["confidence"]

                if conf_i >= conf_j:
                    suppressed[j] = True
                else:
                    suppressed[i] = True
                    break  # i подавлен, переходим к следующему i

    result = [det for det, sup in zip(detections, suppressed) if not sup]
    return result

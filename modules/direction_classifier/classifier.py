"""
Direction Classifier - инференс направления (↑ → ↓ ←) объекта на P&ID-схеме.

Тонкая обёртка над ultralytics YOLOv8-cls (веса best.pt из
pid_direction_classifier). На вход — изображение + bbox объекта, на выход —
{direction, confidence}.

КРИТИЧНО (соответствие обучению):
- Кроп берётся как bbox + паддинг PAD_FRAC (по умолчанию 0.20 — как в
  extract_crops.py, на котором обучался классификатор), клампится к границам
  изображения, RGB. Никакого NLM/CLAHE (в отличие от детектора) — классификатор
  обучался на «сырых» кропах.
- Ресайз до img_size (128) выполняет сам ultralytics при predict().
- Имена направлений берутся из model.names (ImageFolder сортирует классы
  алфавитно: down/left/right/up), а НЕ хардкодом — порядок индексов задаёт
  обученная модель.

Пример:
    clf = DirectionClassifier(weights="models/direction/best.pt")
    res = clf.predict(image, bbox=[x, y, w, h])  # COCO bbox [x,y,w,h]
    # res == {"direction": "right", "confidence": 0.98}
"""

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

import cv2
import numpy as np

# Препроцессинг кропа идентичен extract_crops.py (на нём обучался классификатор).
PAD_FRAC = 0.20
# Вход модели; должен совпадать с training.img_size (config/default.yaml -> 128).
DEFAULT_IMG_SIZE = 128
# Канонический список направлений (для валидации того, что отдала модель).
DIRECTIONS = ("up", "right", "down", "left")


def crop_with_padding(
    image: np.ndarray,
    bbox_xywh: Sequence[float],
    pad_frac: float = PAD_FRAC,
) -> Optional[np.ndarray]:
    """
    Вырезать кроп вокруг bbox с паддингом — как в extract_crops.py.

    Args:
        image: изображение BGR (numpy, как читает cv2.imread).
        bbox_xywh: COCO bbox [x, y, w, h] в пикселях (левый-верхний угол + размеры).
        pad_frac: доля стороны бокса, добавляемая с каждой стороны.

    Returns:
        Кроп (BGR) или None, если бокс вырожденный / вне изображения.
    """
    if image is None or len(bbox_xywh) < 4:
        return None

    H, W = image.shape[:2]
    x, y, w, h = float(bbox_xywh[0]), float(bbox_xywh[1]), float(bbox_xywh[2]), float(bbox_xywh[3])
    if w <= 0 or h <= 0:
        return None

    pad_x, pad_y = w * pad_frac, h * pad_frac
    left = max(0, int(x - pad_x))
    top = max(0, int(y - pad_y))
    right = min(W, int(x + w + pad_x))
    bottom = min(H, int(y + h + pad_y))

    if right <= left or bottom <= top:
        return None

    return image[top:bottom, left:right]


class DirectionClassifier:
    """
    Классификатор направления объекта на P&ID-схеме (YOLOv8-cls).

    Один классификатор на 4 направления (up/right/down/left). Объектный класс
    известен из детекции — здесь предсказывается только ориентация.

    Пример:
        clf = DirectionClassifier(weights="models/direction/best.pt", device="cuda")
        res = clf.predict(image, bbox=[x, y, w, h])
        results = clf.predict_batch(image, bboxes=[[...], [...]])
    """

    def __init__(
        self,
        weights: Union[str, Path],
        device: str = "cuda",
        img_size: int = DEFAULT_IMG_SIZE,
        pad_frac: float = PAD_FRAC,
    ):
        """
        Args:
            weights: путь к весам YOLOv8-cls (.pt).
            device: "cuda" или "cpu".
            img_size: вход модели (должен совпадать с обучением, 128).
            pad_frac: паддинг кропа (должен совпадать с extract_crops.py, 0.20).
        """
        self.weights = Path(weights)
        self.device = device
        self.img_size = img_size
        self.pad_frac = pad_frac

        self._model = None
        self._names: Dict[int, str] = {}

    def _load_model(self) -> None:
        """Ленивая загрузка YOLO-cls модели."""
        if self._model is not None:
            return

        from ultralytics import YOLO

        if not self.weights.exists():
            raise FileNotFoundError(f"Веса классификатора не найдены: {self.weights}")

        self._model = YOLO(str(self.weights))
        # Порядок классов задаёт обученная модель (ImageFolder, алфавит).
        self._names = dict(self._model.names)

        unknown = set(self._names.values()) - set(DIRECTIONS)
        if unknown:
            # Не падаем — модель могла быть обучена иначе; просто предупреждаем.
            print(
                f"⚠️  DirectionClassifier: неожиданные классы в model.names: {unknown}. "
                f"Ожидались {DIRECTIONS}."
            )
        print(f"✅ DirectionClassifier загружен: {self.weights.name}  классы={self._names}")

    @property
    def names(self) -> Dict[int, str]:
        """Маппинг index -> имя направления (из обученной модели)."""
        self._load_model()
        return self._names

    def predict(
        self,
        image: Union[str, Path, np.ndarray],
        bbox: Sequence[float],
    ) -> Optional[Dict[str, Union[str, float]]]:
        """
        Предсказать направление одного объекта.

        Args:
            image: путь или numpy-изображение (BGR) ПОЛНОЙ схемы.
            bbox: COCO bbox объекта [x, y, w, h] в пикселях.

        Returns:
            {"direction": str, "confidence": float} или None, если кроп
            не удалось извлечь.
        """
        img = self._read_image(image)
        crop = crop_with_padding(img, bbox, self.pad_frac)
        if crop is None:
            return None

        out = self._predict_crops([crop])
        return out[0]

    def predict_batch(
        self,
        image: Union[str, Path, np.ndarray],
        bboxes: Sequence[Sequence[float]],
    ) -> List[Optional[Dict[str, Union[str, float]]]]:
        """
        Предсказать направление для набора bbox на ОДНОМ изображении.

        Кропы извлекаются из общего изображения (картинка читается один раз) и
        прогоняются через модель батчем.

        Args:
            image: путь или numpy-изображение (BGR) полной схемы.
            bboxes: список COCO bbox [x, y, w, h].

        Returns:
            Список того же размера, что bboxes: каждый элемент —
            {"direction", "confidence"} или None (если кроп не извлёкся).
        """
        img = self._read_image(image)

        crops: List[np.ndarray] = []
        index_map: List[int] = []  # позиция кропа в crops -> индекс исходного bbox
        results: List[Optional[Dict]] = [None] * len(bboxes)

        for i, bbox in enumerate(bboxes):
            crop = crop_with_padding(img, bbox, self.pad_frac)
            if crop is not None:
                index_map.append(i)
                crops.append(crop)

        if not crops:
            return results

        preds = self._predict_crops(crops)
        for pos, pred in zip(index_map, preds):
            results[pos] = pred
        return results

    # ------------------------------------------------------------------ #
    # Внутреннее
    # ------------------------------------------------------------------ #
    def _predict_crops(self, crops: List[np.ndarray]) -> List[Dict[str, Union[str, float]]]:
        """Прогнать готовые кропы (BGR) через модель."""
        self._load_model()

        results = self._model.predict(
            source=crops,
            imgsz=self.img_size,
            device=self.device,
            verbose=False,
        )

        out: List[Dict[str, Union[str, float]]] = []
        for r in results:
            probs = r.probs
            top1 = int(probs.top1)
            out.append({
                "direction": self._names.get(top1, str(top1)),
                "confidence": float(probs.top1conf),
            })
        return out

    @staticmethod
    def _read_image(image: Union[str, Path, np.ndarray]) -> np.ndarray:
        if isinstance(image, (str, Path)):
            img = cv2.imread(str(image))
            if img is None:
                raise ValueError(f"Не удалось загрузить изображение: {image}")
            return img
        return image

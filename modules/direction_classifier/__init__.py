"""
Direction Classifier - инференс направления (↑ → ↓ ←) объектов на P&ID-схемах.

Самодостаточная обёртка над ultralytics YOLOv8-cls (веса обучены пакетом
pid_direction_classifier). Объектный класс известен из детекции; здесь
предсказывается только ориентация.

Использование:
    from modules.direction_classifier import DirectionClassifier

    clf = DirectionClassifier(weights="models/direction/best.pt")
    res = clf.predict(image, bbox=[x, y, w, h])          # один объект
    out = clf.predict_batch(image, bboxes=[[...], ...])  # много объектов
"""

from modules.direction_classifier.classifier import (
    DirectionClassifier,
    crop_with_padding,
    DIRECTIONS,
    PAD_FRAC,
    DEFAULT_IMG_SIZE,
)

__all__ = [
    "DirectionClassifier",
    "crop_with_padding",
    "DIRECTIONS",
    "PAD_FRAC",
    "DEFAULT_IMG_SIZE",
]
__version__ = "1.0.0"

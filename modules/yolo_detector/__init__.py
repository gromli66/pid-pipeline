"""
YOLO Node Detector - модуль для inference в P&ID Pipeline.

Использование:
    from modules.yolo_detector import EnsembleDetector

    detector = EnsembleDetector(models=[...])
    detections = detector.detect("path/to/image.png")

    for det in detections:
        print(f"{det['class_name']}: {det['confidence']:.2f}")
"""

from modules.yolo_detector.detector import (
    NodeDetector,
    detections_to_yolo,
    detections_to_coco,
    resolve_overlaps,
)
from modules.yolo_detector.ensemble import EnsembleDetector
from modules.yolo_detector.config import CLASS_NAMES, CLASS_IDS, NUM_CLASSES

__all__ = [
    "NodeDetector",
    "EnsembleDetector",
    "detections_to_yolo",
    "detections_to_coco",
    "resolve_overlaps",
    "CLASS_NAMES",
    "CLASS_IDS",
    "NUM_CLASSES",
]
__version__ = "2.0.0"

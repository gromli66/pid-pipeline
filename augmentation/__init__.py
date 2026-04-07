"""
Augmentation submodule - аугментации для P&ID данных.

Включает:
- copy_paste: Copy-Paste Augmentation для редких классов
- rotation: Повороты на 90/180/270 градусов
"""

from pid_node_detection.augmentation.copy_paste import CopyPasteAugmentation
from pid_node_detection.augmentation.rotation import RotationAugmentation

__all__ = [
    "CopyPasteAugmentation",
    "RotationAugmentation",
]
"""
Inference submodule - детекция на новых изображениях.
"""

from pid_node_detection.inference.detector import NodeDetector
from pid_node_detection.inference.ensemble import EnsembleDetector

__all__ = ["NodeDetector", "EnsembleDetector"]

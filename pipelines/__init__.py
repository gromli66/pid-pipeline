"""
Pipelines submodule - оркестрация пайплайнов train/finetune/test.
"""

from pid_node_detection.pipelines.train_pipeline import TrainPipeline
from pid_node_detection.pipelines.finetune_pipeline import FinetunePipeline
from pid_node_detection.pipelines.test_pipeline import TestPipeline

__all__ = [
    "TrainPipeline",
    "FinetunePipeline",
    "TestPipeline",
]

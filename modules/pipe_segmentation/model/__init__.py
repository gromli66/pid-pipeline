"""Модель U-Net++, функции потерь и метрики."""

from pipe_segmentation.model.architecture import (
    create_model,
    load_checkpoint,
    save_checkpoint,
    freeze_encoder,
    unfreeze_encoder,
    get_parameter_groups,
    DualHeadModel,
)

from pipe_segmentation.model.losses import (
    DiceLoss,
    FocalLoss,
    FocalLossOHEM,
    ClDiceLoss,
    ClCELoss,
    SkeletonBCELoss,
    MultiTaskLoss,
    UltimateLoss,
)

from pipe_segmentation.model.metrics import (
    dice_coeff,
    iou_score,
    DiceScore,
    IoUScore,
    PixelAccuracy,
    PrecisionRecall,
    MetricsTracker,
    calculate_metrics,
)

__all__ = [
    # Architecture
    "create_model",
    "DualHeadModel",
    "load_checkpoint",
    "save_checkpoint",
    "freeze_encoder",
    "unfreeze_encoder",
    "get_parameter_groups",
    
    # Losses
    "DiceLoss",
    "FocalLoss",
    "FocalLossOHEM",
    "ClDiceLoss",
    "ClCELoss",
    "SkeletonBCELoss",
    "MultiTaskLoss",
    "UltimateLoss",
    
    # Metrics
    "dice_coeff",
    "iou_score",
    "DiceScore",
    "IoUScore",
    "PixelAccuracy",
    "PrecisionRecall",
    "MetricsTracker",
    "calculate_metrics",
]

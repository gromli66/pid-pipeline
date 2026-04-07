"""
Метрики для оценки сегментации.

Включает:
- dice_coeff, iou_score: helper functions
- DiceScore, IoUScore: nn.Module классы
- PixelAccuracy, PrecisionRecall
- MetricsTracker: аккумулятор метрик для training
- calculate_metrics: полный расчёт для одного изображения
"""

import torch
import torch.nn as nn
import numpy as np
from typing import Dict, Tuple, List, Optional


def dice_coeff(
    pred: torch.Tensor,
    target: torch.Tensor,
    threshold: float = 0.5,
    smooth: float = 1.0
) -> torch.Tensor:
    """
    Вычисляет Dice coefficient.
    
    Args:
        pred: Предсказания [B, 1, H, W] (logits или probabilities)
        target: Ground truth [B, 1, H, W]
        threshold: Порог бинаризации
        smooth: Сглаживание
        
    Returns:
        Dice score (scalar in [0, 1])
    """
    # Sigmoid если нужно
    if pred.min() < 0 or pred.max() > 1:
        pred = torch.sigmoid(pred)
    
    # Бинаризация
    pred = (pred > threshold).float()
    
    # Flatten
    pred = pred.view(-1)
    target = target.view(-1)
    
    # Dice
    intersection = (pred * target).sum()
    union = pred.sum() + target.sum()
    
    dice = (2.0 * intersection + smooth) / (union + smooth)
    
    return dice


def iou_score(
    pred: torch.Tensor,
    target: torch.Tensor,
    threshold: float = 0.5,
    smooth: float = 1.0
) -> torch.Tensor:
    """
    Вычисляет IoU (Intersection over Union) score.
    
    Args:
        pred: Предсказания [B, 1, H, W]
        target: Ground truth [B, 1, H, W]
        threshold: Порог бинаризации
        smooth: Сглаживание
        
    Returns:
        IoU score (scalar in [0, 1])
    """
    if pred.min() < 0 or pred.max() > 1:
        pred = torch.sigmoid(pred)
    
    pred = (pred > threshold).float()
    
    pred = pred.view(-1)
    target = target.view(-1)
    
    intersection = (pred * target).sum()
    union = pred.sum() + target.sum() - intersection
    
    iou = (intersection + smooth) / (union + smooth)
    
    return iou


class DiceScore(nn.Module):
    """Dice Score как nn.Module."""
    
    def __init__(self, threshold: float = 0.5, smooth: float = 1.0):
        super().__init__()
        self.threshold = threshold
        self.smooth = smooth
    
    def forward(self, pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        return dice_coeff(pred, target, self.threshold, self.smooth)


class IoUScore(nn.Module):
    """IoU Score как nn.Module."""
    
    def __init__(self, threshold: float = 0.5, smooth: float = 1.0):
        super().__init__()
        self.threshold = threshold
        self.smooth = smooth
    
    def forward(self, pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        return iou_score(pred, target, self.threshold, self.smooth)


class PixelAccuracy(nn.Module):
    """
    Pixel Accuracy = (TP + TN) / Total
    """
    
    def __init__(self, threshold: float = 0.5):
        super().__init__()
        self.threshold = threshold
    
    def forward(self, pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        if pred.min() < 0 or pred.max() > 1:
            pred = torch.sigmoid(pred)
        
        pred = (pred > self.threshold).float()
        
        pred = pred.view(-1)
        target = target.view(-1)
        
        correct = (pred == target).float().sum()
        total = target.numel()
        
        return correct / total


class PrecisionRecall(nn.Module):
    """
    Precision = TP / (TP + FP)
    Recall = TP / (TP + FN)
    """
    
    def __init__(self, threshold: float = 0.5, eps: float = 1e-7):
        super().__init__()
        self.threshold = threshold
        self.eps = eps
    
    def forward(
        self,
        pred: torch.Tensor,
        target: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Returns:
            (precision, recall)
        """
        if pred.min() < 0 or pred.max() > 1:
            pred = torch.sigmoid(pred)
        
        pred = (pred > self.threshold).float()
        
        pred = pred.view(-1)
        target = target.view(-1)
        
        tp = (pred * target).sum()
        fp = (pred * (1 - target)).sum()
        fn = ((1 - pred) * target).sum()
        
        precision = tp / (tp + fp + self.eps)
        recall = tp / (tp + fn + self.eps)
        
        return precision, recall


class MetricsTracker:
    """
    Аккумулятор метрик для training/validation.
    
    Пример:
        tracker = MetricsTracker()
        for batch in dataloader:
            tracker.update(outputs, masks)
        metrics = tracker.compute()
    """
    
    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self.dice = DiceScore(threshold)
        self.iou = IoUScore(threshold)
        self.pixel_acc = PixelAccuracy(threshold)
        self.precision_recall = PrecisionRecall(threshold)
        
        self.reset()
    
    def reset(self):
        """Сброс накопленных метрик."""
        self.metrics = {
            'dice': [],
            'iou': [],
            'pixel_accuracy': [],
            'precision': [],
            'recall': []
        }
    
    @torch.no_grad()
    def update(self, pred: torch.Tensor, target: torch.Tensor):
        """
        Обновление метрик новым батчем.
        
        Args:
            pred: Предсказания [B, 1, H, W]
            target: Ground truth [B, 1, H, W]
        """
        dice = self.dice(pred, target)
        iou = self.iou(pred, target)
        pixel_acc = self.pixel_acc(pred, target)
        precision, recall = self.precision_recall(pred, target)
        
        self.metrics['dice'].append(dice.item())
        self.metrics['iou'].append(iou.item())
        self.metrics['pixel_accuracy'].append(pixel_acc.item())
        self.metrics['precision'].append(precision.item())
        self.metrics['recall'].append(recall.item())
    
    def compute(self) -> Dict[str, float]:
        """
        Вычисление средних метрик.
        
        Returns:
            Словарь со средними значениями
        """
        result = {}
        
        for key, values in self.metrics.items():
            if values:
                result[key] = float(np.mean(values))
            else:
                result[key] = 0.0
        
        # F1 score
        if result['precision'] + result['recall'] > 0:
            result['f1'] = (
                2 * result['precision'] * result['recall'] /
                (result['precision'] + result['recall'])
            )
        else:
            result['f1'] = 0.0
        
        return result


def calculate_metrics(
    pred_mask: np.ndarray,
    gt_mask: np.ndarray,
    threshold: float = 0.5
) -> Dict[str, float]:
    """
    Полный расчёт метрик для одного изображения.
    
    Args:
        pred_mask: Предсказанная маска [H, W] (0-255 или 0-1)
        gt_mask: Ground truth маска [H, W] (0-255 или 0-1)
        threshold: Порог бинаризации (для probability maps)
        
    Returns:
        Словарь с метриками
    """
    # Нормализация
    if pred_mask.max() > 1:
        pred_mask = pred_mask / 255.0
    if gt_mask.max() > 1:
        gt_mask = gt_mask / 255.0
    
    # Бинаризация
    pred_binary = (pred_mask > threshold).astype(np.float32)
    gt_binary = (gt_mask > threshold).astype(np.float32)
    
    # Flatten
    pred_flat = pred_binary.flatten()
    gt_flat = gt_binary.flatten()
    
    # TP, FP, FN, TN
    tp = np.sum(pred_flat * gt_flat)
    fp = np.sum(pred_flat * (1 - gt_flat))
    fn = np.sum((1 - pred_flat) * gt_flat)
    tn = np.sum((1 - pred_flat) * (1 - gt_flat))
    
    eps = 1e-7
    
    # Metrics
    dice = (2 * tp + eps) / (2 * tp + fp + fn + eps)
    iou = (tp + eps) / (tp + fp + fn + eps)
    precision = (tp + eps) / (tp + fp + eps)
    recall = (tp + eps) / (tp + fn + eps)
    f1 = 2 * precision * recall / (precision + recall + eps)
    pixel_accuracy = (tp + tn) / (tp + tn + fp + fn + eps)
    
    # Coverage
    coverage_pred = np.mean(pred_binary) * 100
    coverage_gt = np.mean(gt_binary) * 100
    
    return {
        'dice': float(dice),
        'iou': float(iou),
        'precision': float(precision),
        'recall': float(recall),
        'f1': float(f1),
        'pixel_accuracy': float(pixel_accuracy),
        'coverage_pred_pct': float(coverage_pred),
        'coverage_gt_pct': float(coverage_gt),
    }


def compute_summary_stats(metrics_list: List[Dict[str, float]]) -> Dict[str, Dict[str, float]]:
    """
    Вычисляет summary статистику по списку метрик.
    
    Args:
        metrics_list: Список словарей с метриками
        
    Returns:
        Словарь {metric_name: {mean, std, min, max, median}}
    """
    if not metrics_list:
        return {}
    
    # Собираем все ключи
    keys = metrics_list[0].keys()
    
    summary = {}
    for key in keys:
        values = [m[key] for m in metrics_list if key in m]
        if not values or not isinstance(values[0], (int, float)):
            continue
        numeric = [v for v in values if isinstance(v, (int, float))]
        if numeric:
            summary[key] = {
                'mean': float(np.mean(numeric)),
                'std': float(np.std(numeric)),
                'min': float(np.min(numeric)),
                'max': float(np.max(numeric)),
                'median': float(np.median(values)),
                'count': len(values)
            }
    
    return summary

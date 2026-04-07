"""
Утилиты для визуализации: overlay масок, графики обучения.
"""

import cv2
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
from typing import Dict, List, Tuple, Union, Optional


def create_overlay(
    image: np.ndarray,
    mask: np.ndarray,
    color: Tuple[int, int, int] = (0, 255, 0),
    alpha: float = 0.5
) -> np.ndarray:
    """
    Создаёт overlay маски на изображении.
    
    Args:
        image: RGB изображение [H, W, 3]
        mask: Бинарная маска [H, W] (0-255 или 0-1)
        color: Цвет маски (RGB)
        alpha: Прозрачность (0-1)
        
    Returns:
        RGB изображение с overlay
    """
    # Копируем изображение
    if len(image.shape) == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    
    overlay = image.copy()
    
    # Нормализуем маску
    if mask.max() > 1:
        mask_binary = mask > 127
    else:
        mask_binary = mask > 0.5
    
    # Применяем цвет
    overlay[mask_binary] = (
        overlay[mask_binary] * (1 - alpha) + 
        np.array(color) * alpha
    ).astype(np.uint8)
    
    return overlay


def create_comparison_image(
    image: np.ndarray,
    gt_mask: np.ndarray,
    pred_mask: np.ndarray,
    gt_color: Tuple[int, int, int] = (0, 255, 0),
    pred_color: Tuple[int, int, int] = (255, 0, 0),
    overlap_color: Tuple[int, int, int] = (255, 255, 0)
) -> np.ndarray:
    """
    Создаёт изображение сравнения GT и prediction.
    
    Зелёный = GT only
    Красный = Pred only (False Positive)
    Жёлтый = Overlap (True Positive)
    
    Args:
        image: RGB изображение
        gt_mask: Ground truth маска
        pred_mask: Предсказанная маска
        
    Returns:
        RGB изображение с overlay
    """
    if len(image.shape) == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    
    overlay = image.copy()
    
    # Нормализуем маски
    if gt_mask.max() > 1:
        gt_binary = gt_mask > 127
    else:
        gt_binary = gt_mask > 0.5
        
    if pred_mask.max() > 1:
        pred_binary = pred_mask > 127
    else:
        pred_binary = pred_mask > 0.5
    
    # Вычисляем области
    overlap = gt_binary & pred_binary      # True Positive
    gt_only = gt_binary & ~pred_binary     # False Negative
    pred_only = pred_binary & ~gt_binary   # False Positive
    
    alpha = 0.5
    
    # Применяем цвета
    overlay[overlap] = (overlay[overlap] * (1-alpha) + np.array(overlap_color) * alpha).astype(np.uint8)
    overlay[gt_only] = (overlay[gt_only] * (1-alpha) + np.array(gt_color) * alpha).astype(np.uint8)
    overlay[pred_only] = (overlay[pred_only] * (1-alpha) + np.array(pred_color) * alpha).astype(np.uint8)
    
    return overlay


def plot_training_curves(
    history: Dict[str, List[float]],
    save_path: Union[str, Path],
    title: str = "Training Curves"
) -> None:
    """
    Строит графики обучения (loss, dice, iou).
    
    Args:
        history: Словарь с историей метрик
        save_path: Путь для сохранения графика
        title: Заголовок графика
    """
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    
    epochs = range(1, len(history.get('train_loss', [])) + 1)
    
    # 1. Loss
    if 'train_loss' in history and 'val_loss' in history:
        axes[0, 0].plot(epochs, history['train_loss'], 'b-', label='Train', linewidth=2)
        axes[0, 0].plot(epochs, history['val_loss'], 'r-', label='Val', linewidth=2)
        axes[0, 0].set_xlabel('Epoch', fontweight='bold')
        axes[0, 0].set_ylabel('Loss', fontweight='bold')
        axes[0, 0].set_title('Loss', fontweight='bold')
        axes[0, 0].legend()
        axes[0, 0].grid(True, alpha=0.3)
    
    # 2. Dice
    if 'train_dice' in history and 'val_dice' in history:
        axes[0, 1].plot(epochs, history['train_dice'], 'b-', label='Train', linewidth=2)
        axes[0, 1].plot(epochs, history['val_dice'], 'r-', label='Val', linewidth=2)
        axes[0, 1].set_xlabel('Epoch', fontweight='bold')
        axes[0, 1].set_ylabel('Dice Score', fontweight='bold')
        axes[0, 1].set_title('Dice Score', fontweight='bold')
        axes[0, 1].legend()
        axes[0, 1].grid(True, alpha=0.3)
        axes[0, 1].set_ylim([0, 1])
        
        # Отметить лучший epoch
        best_epoch = np.argmax(history['val_dice']) + 1
        best_dice = max(history['val_dice'])
        axes[0, 1].axvline(x=best_epoch, color='green', linestyle='--', alpha=0.7)
        axes[0, 1].annotate(f'Best: {best_dice:.4f}', 
                           xy=(best_epoch, best_dice),
                           xytext=(best_epoch + 2, best_dice - 0.05),
                           fontsize=10, fontweight='bold')
    
    # 3. IoU
    if 'train_iou' in history and 'val_iou' in history:
        axes[1, 0].plot(epochs, history['train_iou'], 'b-', label='Train', linewidth=2)
        axes[1, 0].plot(epochs, history['val_iou'], 'r-', label='Val', linewidth=2)
        axes[1, 0].set_xlabel('Epoch', fontweight='bold')
        axes[1, 0].set_ylabel('IoU Score', fontweight='bold')
        axes[1, 0].set_title('IoU Score', fontweight='bold')
        axes[1, 0].legend()
        axes[1, 0].grid(True, alpha=0.3)
        axes[1, 0].set_ylim([0, 1])
    
    # 4. Precision/Recall
    if 'val_precision' in history and 'val_recall' in history:
        axes[1, 1].plot(epochs, history['val_precision'], 'g-', label='Precision', linewidth=2)
        axes[1, 1].plot(epochs, history['val_recall'], 'm-', label='Recall', linewidth=2)
        if 'val_f1' in history:
            axes[1, 1].plot(epochs, history.get('val_f1', []), 'c-', label='F1', linewidth=2)
        axes[1, 1].set_xlabel('Epoch', fontweight='bold')
        axes[1, 1].set_ylabel('Score', fontweight='bold')
        axes[1, 1].set_title('Precision / Recall / F1', fontweight='bold')
        axes[1, 1].legend()
        axes[1, 1].grid(True, alpha=0.3)
        axes[1, 1].set_ylim([0, 1])
    
    plt.suptitle(title, fontsize=14, fontweight='bold')
    plt.tight_layout()
    
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(save_path, dpi=150, bbox_inches='tight')
    plt.close()


def plot_metrics_summary(
    metrics: Dict[str, Dict[str, float]],
    save_path: Union[str, Path],
    title: str = "Test Metrics Summary"
) -> None:
    """
    Строит bar chart с метриками.
    
    Args:
        metrics: Словарь {metric_name: {mean, std, min, max}}
        save_path: Путь для сохранения
        title: Заголовок
    """
    fig, ax = plt.subplots(figsize=(10, 6))
    
    metric_names = list(metrics.keys())
    means = [metrics[m]['mean'] for m in metric_names]
    stds = [metrics[m].get('std', 0) for m in metric_names]
    
    x = np.arange(len(metric_names))
    bars = ax.bar(x, means, yerr=stds, capsize=5, 
                  color='steelblue', alpha=0.7, edgecolor='black')
    
    ax.set_ylabel('Score', fontweight='bold')
    ax.set_title(title, fontweight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels([m.replace('_', ' ').title() for m in metric_names], rotation=45, ha='right')
    ax.set_ylim([0, 1.1])
    ax.grid(True, alpha=0.3, axis='y')
    
    # Добавить значения на барах
    for bar, mean, std in zip(bars, means, stds):
        height = bar.get_height()
        ax.annotate(f'{mean:.3f}',
                    xy=(bar.get_x() + bar.get_width() / 2, height + std + 0.02),
                    ha='center', va='bottom', fontweight='bold', fontsize=10)
    
    plt.tight_layout()
    
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(save_path, dpi=150, bbox_inches='tight')
    plt.close()


def visualize_predictions(
    images: List[np.ndarray],
    masks_gt: List[np.ndarray],
    masks_pred: List[np.ndarray],
    save_path: Union[str, Path],
    n_samples: int = 4,
    title: str = "Predictions"
) -> None:
    """
    Визуализирует примеры предсказаний.
    
    Args:
        images: Список RGB изображений
        masks_gt: Список GT масок
        masks_pred: Список предсказанных масок
        save_path: Путь для сохранения
        n_samples: Количество примеров
        title: Заголовок
    """
    n_samples = min(n_samples, len(images))
    
    fig, axes = plt.subplots(n_samples, 4, figsize=(16, 4 * n_samples))
    
    if n_samples == 1:
        axes = axes.reshape(1, -1)
    
    for i in range(n_samples):
        # Original
        axes[i, 0].imshow(images[i])
        axes[i, 0].set_title('Original', fontweight='bold')
        axes[i, 0].axis('off')
        
        # GT
        axes[i, 1].imshow(masks_gt[i], cmap='gray')
        axes[i, 1].set_title('Ground Truth', fontweight='bold')
        axes[i, 1].axis('off')
        
        # Prediction
        axes[i, 2].imshow(masks_pred[i], cmap='gray')
        axes[i, 2].set_title('Prediction', fontweight='bold')
        axes[i, 2].axis('off')
        
        # Comparison
        comparison = create_comparison_image(images[i], masks_gt[i], masks_pred[i])
        axes[i, 3].imshow(comparison)
        axes[i, 3].set_title('Comparison\n(G=GT, R=Pred, Y=Overlap)', fontweight='bold', fontsize=9)
        axes[i, 3].axis('off')
    
    plt.suptitle(title, fontsize=14, fontweight='bold')
    plt.tight_layout()
    
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(save_path, dpi=150, bbox_inches='tight')
    plt.close()

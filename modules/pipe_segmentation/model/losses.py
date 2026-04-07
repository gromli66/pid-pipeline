"""
Функции потерь v3 — topology-preserving для пунктирных линий.

[NEW] ClCELoss — centerline Cross-Entropy (MICCAI 2024)
      BCE на скелетах вместо Dice на скелетах.
      Робастнее и не штрафует segmentation accuracy.

[NEW] MultiTaskLoss — для dual-head модели (mask + skeleton)
      Loss = mask_loss + skeleton_weight * skeleton_loss

[UPD] ClDiceLoss — iterations 5→10 для лучшей скелетонизации

Все loss-функции численно стабильны при AMP float16.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional

from pipe_segmentation.config.defaults import POS_WEIGHT_MAX_CLAMP


class DiceLoss(nn.Module):
    """Dice Loss для бинарной сегментации."""

    def __init__(self, smooth: float = 1.0, eps: float = 1e-7):
        super().__init__()
        self.smooth = smooth
        self.eps = eps

    def forward(self, pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        if pred.min() < 0 or pred.max() > 1:
            pred = torch.sigmoid(pred)
        pred = pred.view(-1)
        target = target.view(-1)
        intersection = (pred * target).sum()
        union = pred.sum() + target.sum()
        dice = (2.0 * intersection + self.smooth) / (union + self.smooth + self.eps)
        return 1.0 - dice


class FocalLoss(nn.Module):
    """Focal Loss — численно стабильная через F.binary_cross_entropy_with_logits."""

    def __init__(self, alpha: float = 0.25, gamma: float = 2.0,
                 pos_weight: Optional[float] = None):
        super().__init__()
        self.alpha = alpha
        self.gamma = gamma
        self.pos_weight = pos_weight

    def forward(self, pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        is_logits = pred.min() < 0 or pred.max() > 1
        if is_logits:
            logits = pred
        else:
            pred_c = torch.clamp(pred, 1e-6, 1 - 1e-6)
            logits = torch.log(pred_c / (1 - pred_c))

        logits_flat = logits.view(-1)
        target_flat = target.view(-1)

        pw = None
        if self.pos_weight is not None:
            pw = torch.tensor(min(self.pos_weight, POS_WEIGHT_MAX_CLAMP),
                              device=logits_flat.device, dtype=logits_flat.dtype)

        bce = F.binary_cross_entropy_with_logits(
            logits_flat, target_flat, pos_weight=pw, reduction='none')

        with torch.no_grad():
            pt = torch.sigmoid(logits_flat)
            pt = torch.where(target_flat == 1, pt, 1 - pt)
            focal_weight = (1 - pt) ** self.gamma
            alpha_weight = torch.where(target_flat == 1, self.alpha, 1 - self.alpha)

        return (alpha_weight * focal_weight * bce).mean()


class FocalLossOHEM(nn.Module):
    """Focal Loss + OHEM (top-K% hardest pixels)."""

    def __init__(self, alpha: float = 0.25, gamma: float = 2.0,
                 pos_weight: Optional[float] = None, top_k_ratio: float = 0.5):
        super().__init__()
        self.alpha = alpha
        self.gamma = gamma
        self.pos_weight = pos_weight
        self.top_k_ratio = top_k_ratio

    def forward(self, pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        is_logits = pred.min() < 0 or pred.max() > 1
        logits = pred if is_logits else torch.log(
            torch.clamp(pred, 1e-6, 1-1e-6) / (1 - torch.clamp(pred, 1e-6, 1-1e-6)))

        logits_flat = logits.view(-1)
        target_flat = target.view(-1)

        pw = None
        if self.pos_weight is not None:
            pw = torch.tensor(min(self.pos_weight, POS_WEIGHT_MAX_CLAMP),
                              device=logits_flat.device, dtype=logits_flat.dtype)

        bce = F.binary_cross_entropy_with_logits(
            logits_flat, target_flat, pos_weight=pw, reduction='none')

        with torch.no_grad():
            pt = torch.sigmoid(logits_flat)
            pt = torch.where(target_flat == 1, pt, 1 - pt)
            focal_weight = (1 - pt) ** self.gamma
            alpha_weight = torch.where(target_flat == 1, self.alpha, 1 - self.alpha)

        per_pixel = alpha_weight * focal_weight * bce
        k = max(1, int(per_pixel.numel() * self.top_k_ratio))
        top_k, _ = torch.topk(per_pixel, k, sorted=False)
        return top_k.mean()


# =============================================================================
# SOFT SKELETONIZATION (shared by clDice and clCE)
# =============================================================================

def soft_skeletonize(x: torch.Tensor, iters: int = 10) -> torch.Tensor:
    """
    Дифференцируемая скелетонизация через морфологическую эрозию.

    10 итераций (было 5) — даёт более точный скелет для
    зазоров пунктира шириной 5-20px.
    """
    for _ in range(iters):
        min_pool = -F.max_pool2d(-x, kernel_size=3, stride=1, padding=1)
        contour = F.relu(x - min_pool)
        x = F.relu(x - contour)
    return x


# =============================================================================
# clDice — Topology-Preserving Loss (CVPR 2021)
# =============================================================================

class ClDiceLoss(nn.Module):
    """
    Centerline Dice Loss.

    iterations: 5→10 для лучшей скелетонизации зазоров пунктира.
    """

    def __init__(self, smooth: float = 10.0, iters: int = 10):
        super().__init__()
        self.smooth = smooth
        self.iters = iters

    def forward(self, pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        # Принудительный float32
        pred = pred.float()
        target = target.float()

        if pred.min() < 0 or pred.max() > 1:
            pred = torch.sigmoid(pred)
        pred = torch.clamp(pred, 1e-7, 1 - 1e-7)

        pred_skel = soft_skeletonize(pred, self.iters)
        target_skel = soft_skeletonize(target, self.iters)

        pred_skel = torch.clamp(pred_skel, 1e-7, 1.0)
        target_skel = torch.clamp(target_skel, 1e-7, 1.0)

        # clDice = harmonic mean of precision and sensitivity on skeletons
        def soft_dice(a, b):
            inter = (a * b).sum(dim=[1, 2, 3])
            union = a.sum(dim=[1, 2, 3]) + b.sum(dim=[1, 2, 3])
            return torch.clamp(
                (2.0 * inter + self.smooth) / (union + self.smooth + 1e-8),
                0.0, 1.0)

        tprec = soft_dice(pred_skel, target_skel)
        tsens = soft_dice(pred * target_skel, target * pred_skel)

        denom = tprec + tsens + 1e-8
        cl_dice = torch.clamp((2.0 * tprec * tsens) / denom, 0.0, 1.0)

        loss = 1.0 - cl_dice.mean()
        return torch.clamp(loss, 0.0, 1.0)


# =============================================================================
# clCE — Centerline Cross-Entropy (MICCAI 2024)
# =============================================================================

class ClCELoss(nn.Module):
    """
    Centerline Cross-Entropy (MICCAI 2024).

    Вместо soft-Dice на скелетах — BCE на скелетах.
    Преимущества перед clDice:
    - Не штрафует segmentation accuracy
    - Робастнее к шумной разметке
    - Лучше сходится при AMP

    L_clCE = BCE(pred * target_skel, target_skel) +
             BCE(target * pred_skel, pred_skel)

    Первый член: "предсказание должно покрывать скелет GT"
    Второй член: "GT должен покрывать скелет предсказания"
    """

    def __init__(self, iters: int = 10, weight_sensitivity: float = 0.5,
                 weight_precision: float = 0.5):
        super().__init__()
        self.iters = iters
        self.w_sens = weight_sensitivity
        self.w_prec = weight_precision

    def forward(self, pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        pred = pred.float()
        target = target.float()

        if pred.min() < 0 or pred.max() > 1:
            pred_prob = torch.sigmoid(pred)
            logits = pred
        else:
            pred_prob = pred
            pred_c = torch.clamp(pred, 1e-6, 1 - 1e-6)
            logits = torch.log(pred_c / (1 - pred_c))

        # Soft skeletons
        pred_skel = soft_skeletonize(pred_prob, self.iters)
        target_skel = soft_skeletonize(target, self.iters)

        # Маскированный logits: только пиксели скелета
        # Sensitivity: pred должен покрывать скелет GT
        # Берём logits в позициях target_skel, target = target_skel
        target_skel_flat = target_skel.view(-1)
        mask_sens = target_skel_flat > 0.01  # пиксели скелета GT

        if mask_sens.sum() > 0:
            logits_sens = logits.view(-1)[mask_sens]
            target_sens = target_skel_flat[mask_sens]
            loss_sens = F.binary_cross_entropy_with_logits(
                logits_sens, target_sens, reduction='mean')
        else:
            loss_sens = torch.tensor(0.0, device=pred.device)

        # Precision: GT должен покрывать скелет предсказания
        pred_skel_flat = pred_skel.view(-1)
        mask_prec = pred_skel_flat > 0.01

        if mask_prec.sum() > 0:
            target_flat = target.view(-1)
            target_at_skel = target_flat[mask_prec]
            pred_skel_vals = pred_skel_flat[mask_prec]
            # Конвертируем probabilities в logits для AMP-safe BCE
            pred_skel_clamped = torch.clamp(pred_skel_vals, 1e-6, 1 - 1e-6)
            pred_skel_logits = torch.log(pred_skel_clamped / (1 - pred_skel_clamped))
            loss_prec = F.binary_cross_entropy_with_logits(
                pred_skel_logits, target_at_skel, reduction='mean')
        else:
            loss_prec = torch.tensor(0.0, device=pred.device)

        loss = self.w_sens * loss_sens + self.w_prec * loss_prec

        if torch.isnan(loss) or torch.isinf(loss):
            return torch.tensor(0.0, device=pred.device, requires_grad=True)

        return loss


# =============================================================================
# Multi-Task Loss (mask + skeleton)
# =============================================================================

class SkeletonBCELoss(nn.Module):
    """
    BCE loss для скелетной ветки dual-head модели.
    GT skeleton генерируется из pipe_mask через thinning.
    """

    def __init__(self, pos_weight: float = 20.0):
        super().__init__()
        self.pos_weight = pos_weight

    def forward(self, pred: torch.Tensor, target_skeleton: torch.Tensor) -> torch.Tensor:
        """
        Args:
            pred: [B, 1, H, W] logits скелета
            target_skeleton: [B, 1, H, W] GT скелет (0/1)
        """
        pw = torch.tensor(self.pos_weight, device=pred.device, dtype=pred.dtype)
        return F.binary_cross_entropy_with_logits(
            pred.view(-1), target_skeleton.view(-1).float(),
            pos_weight=pw)


class MultiTaskLoss(nn.Module):
    """
    Loss для dual-head модели: mask + skeleton.

    Total = mask_loss + skeleton_weight * skeleton_loss

    mask_loss: UltimateLoss (Dice + Focal + clCE)
    skeleton_loss: BCE на предсказанном скелете vs GT скелете
    """

    def __init__(self, mask_loss: nn.Module, skeleton_weight: float = 0.5,
                 skeleton_pos_weight: float = 20.0):
        super().__init__()
        self.mask_loss = mask_loss
        self.skeleton_weight = skeleton_weight
        self.skeleton_bce = SkeletonBCELoss(pos_weight=skeleton_pos_weight)

    def forward(self, pred_mask: torch.Tensor, pred_skeleton: torch.Tensor,
                target_mask: torch.Tensor, target_skeleton: torch.Tensor) -> torch.Tensor:
        """
        Args:
            pred_mask: [B, 1, H, W] logits маски
            pred_skeleton: [B, 1, H, W] logits скелета
            target_mask: [B, 1, H, W] GT маска (0/1)
            target_skeleton: [B, 1, H, W] GT скелет (0/1)
        """
        loss_mask = self.mask_loss(pred_mask, target_mask)
        loss_skel = self.skeleton_bce(pred_skeleton, target_skeleton)

        total = loss_mask + self.skeleton_weight * loss_skel

        if torch.isnan(total) or torch.isinf(total):
            total = loss_mask

        return total


# =============================================================================
# UltimateLoss v3 — Dice + Focal(OHEM) + clCE
# =============================================================================

class UltimateLoss(nn.Module):
    """
    Dice + Focal(OHEM) + clCE (вместо clDice).

    v3 изменения:
    - clCE вместо clDice — робастнее, не штрафует accuracy
    - 10 итераций скелетонизации (было 5)
    - Опционально: старый clDice через use_cldice=True
    """

    def __init__(
        self,
        dice_weight: float = 1.0,
        focal_weight: float = 1.0,
        cldice_weight: float = 0.5,
        clce_weight: float = 0.5,
        focal_alpha: float = 0.25,
        focal_gamma: float = 2.0,
        focal_pos_weight: Optional[float] = None,
        cldice_iters: int = 10,
        ohem_enabled: bool = True,
        ohem_top_k_ratio: float = 0.5,
        use_clce: bool = True,
    ):
        """
        Args:
            use_clce: True = clCE (MICCAI 2024), False = clDice (CVPR 2021)
            cldice_weight: вес clDice (если use_clce=False)
            clce_weight: вес clCE (если use_clce=True)
        """
        super().__init__()
        self.dice_weight = dice_weight
        self.focal_weight = focal_weight
        self.use_clce = use_clce

        self.dice_loss = DiceLoss()

        if ohem_enabled:
            self.focal_loss = FocalLossOHEM(
                alpha=focal_alpha, gamma=focal_gamma,
                pos_weight=focal_pos_weight, top_k_ratio=ohem_top_k_ratio)
        else:
            self.focal_loss = FocalLoss(
                alpha=focal_alpha, gamma=focal_gamma,
                pos_weight=focal_pos_weight)

        if use_clce:
            self.topo_loss = ClCELoss(iters=cldice_iters)
            self.topo_weight = clce_weight
        else:
            self.topo_loss = ClDiceLoss(iters=cldice_iters)
            self.topo_weight = cldice_weight

    def forward(self, pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        dice = self.dice_loss(pred, target)
        focal = self.focal_loss(pred, target)

        total = self.dice_weight * dice + self.focal_weight * focal

        if self.topo_weight > 0:
            topo = self.topo_loss(pred, target)
            if not (torch.isnan(topo) or torch.isinf(topo)):
                total = total + self.topo_weight * topo

        if torch.isnan(total) or torch.isinf(total):
            total = dice

        return total

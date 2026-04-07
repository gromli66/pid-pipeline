"""
Loss for junction segmentation.

CenterNet modified focal loss — receives RAW LOGITS, not sigmoid.
Uses F.logsigmoid for numerical stability (no clamp, no log-of-sigmoid).

Math (from Objects as Points, Zhou et al. 2019):
  Let p = sigmoid(logit), Y = GT heatmap

  L_pos = -(1-p)^α * log(p)          where Y = 1  (peak centers)
  L_neg = -(1-Y)^β * p^α * log(1-p)  where Y < 1  (everything else)
  L = (sum L_pos + sum L_neg) / N     where N = number of peaks

Stable computation:
  log(p) = log(sigmoid(x)) = F.logsigmoid(x)
  log(1-p) = log(1-sigmoid(x)) = F.logsigmoid(-x)
  → no clamping needed, correct gradients everywhere
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

from .config import Config


class JunctionLoss(nn.Module):

    def __init__(self, cfg: Config):
        super().__init__()
        self.alpha = cfg.focal_alpha
        self.beta = cfg.focal_beta
        self.aux_weight = cfg.aux_loss_weight
        self.use_aux = cfg.aux_head

    def forward(
        self,
        logits: torch.Tensor,
        gt_heatmaps: torch.Tensor,
        aux_logit: torch.Tensor = None,
        has_positive: torch.Tensor = None,
    ) -> dict:
        """
        Args:
            logits:       [B, 2, H, W] — RAW model output (pre-sigmoid)
            gt_heatmaps:  [B, 2, H, W] — Gaussian peaks, values in [0, 1]
            aux_logit:    [B, 1] or None
            has_positive: [B] binary
        """
        # Force fp32 for loss computation under AMP
        logits = logits.float()
        gt = gt_heatmaps.float()

        # Probabilities (for weighting terms only)
        p = torch.sigmoid(logits)

        # Numerically stable log-probabilities (core improvement)
        log_p = F.logsigmoid(logits)       # log(sigmoid(x)) = -softplus(-x)
        log_1mp = F.logsigmoid(-logits)    # log(1-sigmoid(x)) = -softplus(x)

        # Masks
        pos_mask = gt.eq(1).float()
        neg_mask = gt.lt(1).float()

        # Positive loss: -(1-p)^α * log(p)
        pos_loss = -torch.pow(1 - p, self.alpha) * log_p * pos_mask

        # Negative loss: -(1-Y)^β * p^α * log(1-p)
        neg_loss = -torch.pow(1 - gt, self.beta) * torch.pow(p, self.alpha) * log_1mp * neg_mask

        # Normalize by number of peaks (N in the paper)
        n_pos = pos_mask.sum().clamp(min=1)

        focal_loss = (pos_loss.sum() + neg_loss.sum()) / n_pos

        # Per-channel for logging (same normalization, detached)
        junction_loss = (pos_loss[:, 0].sum() + neg_loss[:, 0].sum()) / n_pos
        bridge_loss = (pos_loss[:, 1].sum() + neg_loss[:, 1].sum()) / n_pos

        total = focal_loss

        # Aux loss
        aux_loss = torch.tensor(0.0, device=logits.device)
        if self.use_aux and aux_logit is not None and has_positive is not None:
            aux_loss = F.binary_cross_entropy_with_logits(
                aux_logit.float().squeeze(-1), has_positive.float()
            )
            total = total + self.aux_weight * aux_loss

        return {
            "total_loss": total,
            "junction_loss": junction_loss.detach(),
            "bridge_loss": bridge_loss.detach(),
            "aux_loss": aux_loss.detach(),
        }

"""
Per-point evaluation metrics for junction/bridge detection.

Pipeline: heatmap → local max NMS → threshold → match with GT → P/R/F1
"""

from typing import Dict, List, Tuple

import numpy as np
import torch
import torch.nn.functional as F


def extract_local_maxima(
    heatmap: np.ndarray,
    threshold: float = 0.7,
    nms_kernel: int = 3,
) -> List[Tuple[int, int, float]]:
    """
    Extract point predictions via 3×3 local max + threshold.

    Returns: list of (x, y, confidence)
    """
    if heatmap.max() < threshold:
        return []

    h_tensor = torch.from_numpy(heatmap).float().unsqueeze(0).unsqueeze(0)
    pad = nms_kernel // 2
    h_max = F.max_pool2d(h_tensor, nms_kernel, stride=1, padding=pad)

    keep = (h_tensor == h_max) & (h_tensor >= threshold)
    keep = keep.squeeze().numpy()

    ys, xs = np.where(keep)
    return [(int(x), int(y), float(heatmap[y, x])) for x, y in zip(xs, ys)]


def match_points(
    predicted: List[Tuple[int, int, float]],
    ground_truth: List[Tuple[int, int]],
    max_distance: int = 15,
) -> Tuple[int, int, int]:
    """Greedy nearest-neighbor matching. Returns (TP, FP, FN)."""
    if not predicted and not ground_truth:
        return 0, 0, 0
    if not predicted:
        return 0, 0, len(ground_truth)
    if not ground_truth:
        return 0, len(predicted), 0

    preds_sorted = sorted(predicted, key=lambda p: -p[2])
    gt_matched = set()
    tp = fp = 0

    for px, py, _ in preds_sorted:
        best_dist = float("inf")
        best_gi = -1
        for gi, (gx, gy) in enumerate(ground_truth):
            if gi in gt_matched:
                continue
            d = ((px - gx) ** 2 + (py - gy) ** 2) ** 0.5
            if d < best_dist:
                best_dist = d
                best_gi = gi
        if best_dist <= max_distance and best_gi >= 0:
            tp += 1
            gt_matched.add(best_gi)
        else:
            fp += 1

    fn = len(ground_truth) - len(gt_matched)
    return tp, fp, fn


def compute_metrics(tp: int, fp: int, fn: int) -> Dict[str, float]:
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-8)
    return {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


def evaluate_heatmaps(
    pred_heatmaps: np.ndarray,
    gt_junctions: List[Tuple[int, int]],
    gt_bridges: List[Tuple[int, int]],
    threshold_junction: float = 0.7,
    threshold_bridge: float = 0.85,
    match_radius: int = 15,
    nms_kernel: int = 3,
) -> Dict[str, Dict[str, float]]:
    """Full evaluation: heatmaps → points → matching → metrics."""
    pred_j = extract_local_maxima(pred_heatmaps[0], threshold_junction, nms_kernel)
    tp_j, fp_j, fn_j = match_points(pred_j, gt_junctions, match_radius)

    pred_b = extract_local_maxima(pred_heatmaps[1], threshold_bridge, nms_kernel)
    tp_b, fp_b, fn_b = match_points(pred_b, gt_bridges, match_radius)

    return {
        "junction": compute_metrics(tp_j, fp_j, fn_j),
        "bridge": compute_metrics(tp_b, fp_b, fn_b),
    }

"""
find_thresholds.py — Find optimal detection thresholds on validation set.

Sweeps thresholds, computes per-point P/R/F1, outputs PR curve and best thresholds.

Usage:
  python -m junction_segmentation.find_thresholds \
    --checkpoint best.pth \
    --data-dir ./data/junction_seg \
    --output-dir ./runs/junction_seg_v1/threshold_analysis
"""

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Tuple

import cv2
import numpy as np
import torch
import torch.nn as nn
from tqdm import tqdm

from .config import Config, load_config
from .dataset import TiledInferenceDataset
from .metrics import extract_local_maxima, match_points, compute_metrics
from .model import JunctionSegModel, count_parameters

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def gaussian_blend_mask(size: int, sigma_ratio: float = 0.25) -> np.ndarray:
    sigma = size * sigma_ratio
    ax = np.arange(size, dtype=np.float32) - size / 2
    xx, yy = np.meshgrid(ax, ax)
    return np.exp(-(xx ** 2 + yy ** 2) / (2 * sigma ** 2))


@torch.no_grad()
def predict_full_image(
    model: nn.Module,
    img: np.ndarray,
    mask: np.ndarray,
    skel: np.ndarray,
    cfg: Config,
    device: torch.device,
    blend: np.ndarray,
) -> np.ndarray:
    """Run tiled inference on one full image, return heatmaps [2, H, W]."""
    h, w = img.shape[:2]
    tiled = TiledInferenceDataset(img, mask, skel, cfg.tile_size, cfg.val_tile_overlap)

    prob_acc = np.zeros((2, h, w), dtype=np.float32)
    weight_acc = np.zeros((h, w), dtype=np.float32)

    batch_tensors = []
    batch_positions = []

    for i in range(len(tiled)):
        tensor, (ty, tx) = tiled.get_tile_tensor(i)
        batch_tensors.append(tensor)
        batch_positions.append((ty, tx))

        if len(batch_tensors) == cfg.batch_size or i == len(tiled) - 1:
            batch_t = torch.stack(batch_tensors).to(device)
            with torch.amp.autocast("cuda", enabled=cfg.amp):
                logits, _ = model(batch_t)
                hm = torch.sigmoid(logits)
            hm_np = hm.cpu().numpy()

            for j, (ty, tx) in enumerate(batch_positions):
                ts = cfg.tile_size
                tile_h = min(ts, h - ty)
                tile_w = min(ts, w - tx)
                prob_acc[:, ty:ty+tile_h, tx:tx+tile_w] += (
                    hm_np[j, :, :tile_h, :tile_w] * blend[:tile_h, :tile_w]
                )
                weight_acc[ty:ty+tile_h, tx:tx+tile_w] += blend[:tile_h, :tile_w]

            batch_tensors.clear()
            batch_positions.clear()

    weight_acc = np.maximum(weight_acc, 1e-8)
    return prob_acc / weight_acc


def sweep_thresholds(
    heatmap: np.ndarray,
    gt_points: List[Tuple[int, int]],
    thresholds: np.ndarray,
    match_radius: int = 15,
    nms_kernel: int = 3,
) -> List[Dict]:
    """Evaluate one heatmap at multiple thresholds."""
    results = []
    for thr in thresholds:
        pred = extract_local_maxima(heatmap, float(thr), nms_kernel)
        tp, fp, fn = match_points(pred, gt_points, match_radius)
        m = compute_metrics(tp, fp, fn)
        m["threshold"] = float(thr)
        results.append(m)
    return results


def main():
    parser = argparse.ArgumentParser(description="Find optimal thresholds")
    parser.add_argument("--checkpoint", type=str, required=True)
    parser.add_argument("--data-dir", type=str, required=True)
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--device", type=str, default="cuda")
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()

    # Load config from checkpoint
    ckpt = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
    cfg_dict = ckpt.get("config", {})
    cfg = Config()
    for k, v in cfg_dict.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    cfg.data_dir = args.data_dir
    cfg.device = args.device
    cfg.batch_size = args.batch_size

    output_dir = Path(args.output_dir or Path(args.checkpoint).parent / "threshold_analysis")
    output_dir.mkdir(parents=True, exist_ok=True)

    device = torch.device(cfg.device if torch.cuda.is_available() else "cpu")

    # Load model
    model = JunctionSegModel(cfg).to(device)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()
    logger.info("Loaded model from epoch %d", ckpt["epoch"])

    # Load val schemas
    data_dir = Path(cfg.data_dir)
    with open(data_dir / cfg.meta_json) as f:
        meta = json.load(f)
    val_schemas = meta["split"]["val"]
    logger.info("Evaluating on %d validation schemas", len(val_schemas))

    # Thresholds to sweep
    thresholds = np.arange(0.1, 0.96, 0.05)

    blend = gaussian_blend_mask(cfg.tile_size)

    # Accumulate per-threshold metrics across all schemas
    junction_accum = {float(t): {"tp": 0, "fp": 0, "fn": 0} for t in thresholds}
    bridge_accum = {float(t): {"tp": 0, "fp": 0, "fn": 0} for t in thresholds}

    for schema in tqdm(val_schemas, desc="Schemas"):
        img = cv2.imread(str(data_dir / "images" / f"{schema}.png"))
        if img is None:
            continue
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        mask = cv2.imread(str(data_dir / "pipe_masks" / f"{schema}.png"), cv2.IMREAD_GRAYSCALE)
        skel = cv2.imread(str(data_dir / "skeletons" / f"{schema}.png"), cv2.IMREAD_GRAYSCALE)
        if mask is None or skel is None:
            continue

        with open(data_dir / "annotations" / f"{schema}.json") as f:
            ann = json.load(f)
        gt_j = [(p["x"], p["y"]) for p in ann["junctions"]]
        gt_b = [(p["x"], p["y"]) for p in ann["bridges"]]

        pred_hm = predict_full_image(model, img, mask, skel, cfg, device, blend)

        # Sweep junction thresholds
        for thr in thresholds:
            pred = extract_local_maxima(pred_hm[0], float(thr), cfg.nms_kernel)
            tp, fp, fn = match_points(pred, gt_j, cfg.match_radius)
            junction_accum[float(thr)]["tp"] += tp
            junction_accum[float(thr)]["fp"] += fp
            junction_accum[float(thr)]["fn"] += fn

        # Sweep bridge thresholds
        for thr in thresholds:
            pred = extract_local_maxima(pred_hm[1], float(thr), cfg.nms_kernel)
            tp, fp, fn = match_points(pred, gt_b, cfg.match_radius)
            bridge_accum[float(thr)]["tp"] += tp
            bridge_accum[float(thr)]["fp"] += fp
            bridge_accum[float(thr)]["fn"] += fn

    # Compute metrics at each threshold
    def build_curve(accum):
        curve = []
        for thr in sorted(accum.keys()):
            m = compute_metrics(accum[thr]["tp"], accum[thr]["fp"], accum[thr]["fn"])
            m["threshold"] = thr
            curve.append(m)
        return curve

    j_curve = build_curve(junction_accum)
    b_curve = build_curve(bridge_accum)

    # Find best thresholds
    best_j = max(j_curve, key=lambda m: m["f1"])
    best_b = max(b_curve, key=lambda m: m["f1"])

    # Also find precision-first thresholds (precision >= 95%)
    prec95_j = [m for m in j_curve if m["precision"] >= 0.95]
    prec95_j_best = max(prec95_j, key=lambda m: m["f1"]) if prec95_j else best_j

    prec95_b = [m for m in b_curve if m["precision"] >= 0.95]
    prec95_b_best = max(prec95_b, key=lambda m: m["f1"]) if prec95_b else best_b

    # Print results
    print("\n" + "=" * 70)
    print("THRESHOLD ANALYSIS RESULTS")
    print("=" * 70)

    print("\n--- Junction PR Curve ---")
    print(f"{'Threshold':>10} {'Precision':>10} {'Recall':>10} {'F1':>10} {'TP':>8} {'FP':>8} {'FN':>8}")
    for m in j_curve:
        marker = " ◄ best F1" if m["threshold"] == best_j["threshold"] else ""
        marker = " ◄ best P≥95%" if m["threshold"] == prec95_j_best["threshold"] and marker == "" else marker
        print(f"{m['threshold']:>10.2f} {m['precision']:>10.1%} {m['recall']:>10.1%} {m['f1']:>10.1%} "
              f"{m['tp']:>8} {m['fp']:>8} {m['fn']:>8}{marker}")

    print("\n--- Bridge PR Curve ---")
    print(f"{'Threshold':>10} {'Precision':>10} {'Recall':>10} {'F1':>10} {'TP':>8} {'FP':>8} {'FN':>8}")
    for m in b_curve:
        marker = " ◄ best F1" if m["threshold"] == best_b["threshold"] else ""
        marker = " ◄ best P≥95%" if m["threshold"] == prec95_b_best["threshold"] and marker == "" else marker
        print(f"{m['threshold']:>10.2f} {m['precision']:>10.1%} {m['recall']:>10.1%} {m['f1']:>10.1%} "
              f"{m['tp']:>8} {m['fp']:>8} {m['fn']:>8}{marker}")

    print(f"\n--- Recommended Thresholds ---")
    print(f"  Best F1 junction:     {best_j['threshold']:.2f}  (P={best_j['precision']:.1%} R={best_j['recall']:.1%} F1={best_j['f1']:.1%})")
    print(f"  Best F1 bridge:       {best_b['threshold']:.2f}  (P={best_b['precision']:.1%} R={best_b['recall']:.1%} F1={best_b['f1']:.1%})")
    print(f"  Precision≥95% junct:  {prec95_j_best['threshold']:.2f}  (P={prec95_j_best['precision']:.1%} R={prec95_j_best['recall']:.1%} F1={prec95_j_best['f1']:.1%})")
    print(f"  Precision≥95% bridge: {prec95_b_best['threshold']:.2f}  (P={prec95_b_best['precision']:.1%} R={prec95_b_best['recall']:.1%} F1={prec95_b_best['f1']:.1%})")

    # Save
    results = {
        "junction_curve": j_curve,
        "bridge_curve": b_curve,
        "best_f1_junction": best_j,
        "best_f1_bridge": best_b,
        "prec95_junction": prec95_j_best,
        "prec95_bridge": prec95_b_best,
        "recommended": {
            "junction_threshold": prec95_j_best["threshold"],
            "bridge_threshold": prec95_b_best["threshold"],
        },
    }

    with open(output_dir / "threshold_analysis.json", "w") as f:
        json.dump(results, f, indent=2)

    logger.info("Saved to %s", output_dir / "threshold_analysis.json")


if __name__ == "__main__":
    main()

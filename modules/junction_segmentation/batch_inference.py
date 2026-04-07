"""
batch_inference.py — Run inference on all validation (or train) schemas with visualization.

Usage:
  python -m junction_segmentation.batch_inference \
    --checkpoint best.pth \
    --data-dir ./data/junction_seg \
    --output-dir ./results/val_check \
    --split val \
    --junction-threshold 0.40 \
    --bridge-threshold 0.45
"""

import argparse
import json
import logging
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from tqdm import tqdm

from .config import Config
from .dataset import TiledInferenceDataset
from .metrics import extract_local_maxima, match_points, compute_metrics
from .model import JunctionSegModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def gaussian_blend_mask(size, sigma_ratio=0.25):
    sigma = size * sigma_ratio
    ax = np.arange(size, dtype=np.float32) - size / 2
    xx, yy = np.meshgrid(ax, ax)
    return np.exp(-(xx ** 2 + yy ** 2) / (2 * sigma ** 2))


def create_visualization(
    image, skeleton, gt_junctions, gt_bridges,
    pred_junctions, pred_bridges,
    square_size=15, darken=0.35,
):
    """
    Overlay with 4 colors:
      - Green filled:  TP junction (predicted & matched GT)
      - Red filled:    TP bridge
      - Yellow border: FN (GT missed by model)
      - Magenta border: FP (model predicted, no GT match)
    """
    vis = (image.astype(np.float32) * darken).astype(np.uint8)

    if skeleton is not None:
        vis[skeleton > 127] = [0, 180, 0]

    half = square_size // 2

    def draw_filled(pts, color):
        for p in pts:
            x, y = p["x"], p["y"]
            y1, y2 = max(0, y - half), min(vis.shape[0], y + half + 1)
            x1, x2 = max(0, x - half), min(vis.shape[1], x + half + 1)
            vis[y1:y2, x1:x2] = color

    def draw_border(pts, color, thickness=2):
        for p in pts:
            x, y = p["x"], p["y"]
            y1, y2 = max(0, y - half), min(vis.shape[0], y + half + 1)
            x1, x2 = max(0, x - half), min(vis.shape[1], x + half + 1)
            t = thickness
            vis[y1:y1+t, x1:x2] = color
            vis[y2-t:y2, x1:x2] = color
            vis[y1:y2, x1:x1+t] = color
            vis[y1:y2, x2-t:x2] = color

    # Draw FN first (background layer) — yellow border
    draw_border(gt_junctions.get("fn", []), [255, 255, 0])
    draw_border(gt_bridges.get("fn", []), [255, 200, 0])

    # Draw FP — magenta border
    draw_border(pred_junctions.get("fp", []), [255, 0, 255])
    draw_border(pred_bridges.get("fp", []), [200, 0, 255])

    # Draw TP on top — filled
    draw_filled(pred_junctions.get("tp", []), [0, 255, 0])
    draw_filled(pred_bridges.get("tp", []), [255, 0, 0])

    return vis


def classify_predictions(pred_points, gt_points_raw, max_dist=15):
    """
    Match predictions to GT. Returns:
      pred_tp, pred_fp (lists of point dicts)
      gt_fn (list of point dicts — unmatched GT)
    """
    gt_points = [(p["x"], p["y"]) for p in gt_points_raw]
    pred_sorted = sorted(pred_points, key=lambda p: -p.get("confidence", 0))

    gt_matched = set()
    tp_list = []
    fp_list = []

    for p in pred_sorted:
        px, py = p["x"], p["y"]
        best_dist = float("inf")
        best_gi = -1
        for gi, (gx, gy) in enumerate(gt_points):
            if gi in gt_matched:
                continue
            d = ((px - gx) ** 2 + (py - gy) ** 2) ** 0.5
            if d < best_dist:
                best_dist = d
                best_gi = gi

        if best_dist <= max_dist and best_gi >= 0:
            tp_list.append(p)
            gt_matched.add(best_gi)
        else:
            fp_list.append(p)

    fn_list = [gt_points_raw[i] for i in range(len(gt_points_raw)) if i not in gt_matched]

    return tp_list, fp_list, fn_list


@torch.no_grad()
def infer_schema(model, img, mask, skel, cfg, device, blend, j_thr, b_thr):
    h, w = img.shape[:2]
    tiled = TiledInferenceDataset(img, mask, skel, cfg.tile_size, cfg.val_tile_overlap)

    prob_acc = np.zeros((2, h, w), dtype=np.float32)
    weight_acc = np.zeros((h, w), dtype=np.float32)

    batch_tensors, batch_positions = [], []
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
                th = min(ts, h - ty)
                tw = min(ts, w - tx)
                prob_acc[:, ty:ty+th, tx:tx+tw] += hm_np[j, :, :th, :tw] * blend[:th, :tw]
                weight_acc[ty:ty+th, tx:tx+tw] += blend[:th, :tw]
            batch_tensors.clear()
            batch_positions.clear()

    weight_acc = np.maximum(weight_acc, 1e-8)
    pred_hm = prob_acc / weight_acc

    j_peaks = extract_local_maxima(pred_hm[0], j_thr, cfg.nms_kernel)
    b_peaks = extract_local_maxima(pred_hm[1], b_thr, cfg.nms_kernel)

    j_points = [{"x": int(x), "y": int(y), "confidence": round(float(c), 4)} for x, y, c in j_peaks]
    b_points = [{"x": int(x), "y": int(y), "confidence": round(float(c), 4)} for x, y, c in b_peaks]

    return j_points, b_points


def main():
    parser = argparse.ArgumentParser(description="Batch inference on validation set")
    parser.add_argument("--checkpoint", type=str, required=True)
    parser.add_argument("--data-dir", type=str, required=True)
    parser.add_argument("--output-dir", type=str, required=True)
    parser.add_argument("--split", type=str, default="val", choices=["val", "train", "all"])
    parser.add_argument("--junction-threshold", type=float, default=0.40)
    parser.add_argument("--bridge-threshold", type=float, default=0.45)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--device", type=str, default="cuda")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "visualizations").mkdir(exist_ok=True)
    (output_dir / "masks").mkdir(exist_ok=True)

    device = torch.device(args.device if torch.cuda.is_available() else "cpu")

    # Load model
    ckpt = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
    cfg = Config()
    for k, v in ckpt.get("config", {}).items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    cfg.batch_size = args.batch_size

    model = JunctionSegModel(cfg).to(device)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()
    logger.info("Loaded model epoch %d", ckpt.get("epoch", -1))

    # Load schemas
    data_dir = Path(args.data_dir)
    with open(data_dir / cfg.meta_json) as f:
        meta = json.load(f)

    if args.split == "val":
        schemas = meta["split"]["val"]
    elif args.split == "train":
        schemas = meta["split"]["train"]
    else:
        schemas = meta["split"]["train"] + meta["split"]["val"]

    logger.info("Running on %d schemas (split=%s), thresholds J=%.2f B=%.2f",
                len(schemas), args.split, args.junction_threshold, args.bridge_threshold)

    blend = gaussian_blend_mask(cfg.tile_size)

    # Aggregate metrics
    total_tp_j = total_fp_j = total_fn_j = 0
    total_tp_b = total_fp_b = total_fn_b = 0
    per_schema = []

    for schema in tqdm(schemas, desc="Inference"):
        img = cv2.imread(str(data_dir / "images" / f"{schema}.png"))
        if img is None:
            continue
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        mask = cv2.imread(str(data_dir / "pipe_masks" / f"{schema}.png"), cv2.IMREAD_GRAYSCALE)
        skel = cv2.imread(str(data_dir / "skeletons" / f"{schema}.png"), cv2.IMREAD_GRAYSCALE)
        if mask is None or skel is None:
            continue

        with open(data_dir / "annotations" / f"{schema}.json") as f:
            ann = json.load(f)
        gt_j_raw = [{"x": p["x"], "y": p["y"]} for p in ann["junctions"]]
        gt_b_raw = [{"x": p["x"], "y": p["y"]} for p in ann["bridges"]]

        # Inference
        pred_j, pred_b = infer_schema(
            model, img_rgb, mask, skel, cfg, device, blend,
            args.junction_threshold, args.bridge_threshold,
        )

        # Match
        j_tp, j_fp, j_fn = classify_predictions(pred_j, gt_j_raw, cfg.match_radius)
        b_tp, b_fp, b_fn = classify_predictions(pred_b, gt_b_raw, cfg.match_radius)

        total_tp_j += len(j_tp)
        total_fp_j += len(j_fp)
        total_fn_j += len(j_fn)
        total_tp_b += len(b_tp)
        total_fp_b += len(b_fp)
        total_fn_b += len(b_fn)

        # Visualization
        vis = create_visualization(
            img_rgb, skel,
            {"fn": j_fn}, {"fn": b_fn},
            {"tp": j_tp, "fp": j_fp}, {"tp": b_tp, "fp": b_fp},
        )
        vis_bgr = cv2.cvtColor(vis, cv2.COLOR_RGB2BGR)
        cv2.imwrite(str(output_dir / "visualizations" / f"{schema}.jpg"), vis_bgr, [cv2.IMWRITE_JPEG_QUALITY, 75])

        # Binary masks
        h, w = img_rgb.shape[:2]
        half = 7
        j_mask = np.zeros((h, w), dtype=np.uint8)
        for p in j_tp + j_fp:
            y1, y2 = max(0, p["y"]-half), min(h, p["y"]+half+1)
            x1, x2 = max(0, p["x"]-half), min(w, p["x"]+half+1)
            j_mask[y1:y2, x1:x2] = 255
        b_mask = np.zeros((h, w), dtype=np.uint8)
        for p in b_tp + b_fp:
            y1, y2 = max(0, p["y"]-half), min(h, p["y"]+half+1)
            x1, x2 = max(0, p["x"]-half), min(w, p["x"]+half+1)
            b_mask[y1:y2, x1:x2] = 255

        cv2.imwrite(str(output_dir / "masks" / f"{schema}_junction.png"), j_mask)
        cv2.imwrite(str(output_dir / "masks" / f"{schema}_bridge.png"), b_mask)

        per_schema.append({
            "schema": schema,
            "junction": {"tp": len(j_tp), "fp": len(j_fp), "fn": len(j_fn)},
            "bridge": {"tp": len(b_tp), "fp": len(b_fp), "fn": len(b_fn)},
        })

    # Summary
    m_j = compute_metrics(total_tp_j, total_fp_j, total_fn_j)
    m_b = compute_metrics(total_tp_b, total_fp_b, total_fn_b)

    print(f"\n{'='*60}")
    print(f"BATCH INFERENCE RESULTS ({len(schemas)} schemas, split={args.split})")
    print(f"{'='*60}")
    print(f"  Junction: P={m_j['precision']:.1%}  R={m_j['recall']:.1%}  F1={m_j['f1']:.1%}  ({total_tp_j}tp {total_fp_j}fp {total_fn_j}fn)")
    print(f"  Bridge:   P={m_b['precision']:.1%}  R={m_b['recall']:.1%}  F1={m_b['f1']:.1%}  ({total_tp_b}tp {total_fp_b}fp {total_fn_b}fn)")
    print(f"\n  Visualizations: {output_dir / 'visualizations'}")
    print(f"  Masks:          {output_dir / 'masks'}")

    # Worst schemas
    worst_j = sorted(per_schema, key=lambda s: s["junction"]["fp"] + s["junction"]["fn"], reverse=True)[:5]
    worst_b = sorted(per_schema, key=lambda s: s["bridge"]["fp"] + s["bridge"]["fn"], reverse=True)[:5]

    print(f"\n  Worst junction schemas (by FP+FN):")
    for s in worst_j:
        j = s["junction"]
        print(f"    {s['schema']}: {j['tp']}tp {j['fp']}fp {j['fn']}fn")

    print(f"\n  Worst bridge schemas (by FP+FN):")
    for s in worst_b:
        b = s["bridge"]
        print(f"    {s['schema']}: {b['tp']}tp {b['fp']}fp {b['fn']}fn")

    # Save
    report = {
        "split": args.split,
        "thresholds": {"junction": args.junction_threshold, "bridge": args.bridge_threshold},
        "junction": m_j,
        "bridge": m_b,
        "per_schema": per_schema,
    }
    with open(output_dir / "report.json", "w") as f:
        json.dump(report, f, indent=2)

    logger.info("Report saved to %s", output_dir / "report.json")


if __name__ == "__main__":
    main()

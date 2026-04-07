"""
inference.py — Production inference for junction/bridge detection.

Input:  original image + validated pipe mask + skeleton
Output: junction_mask.png, bridge_mask.png, points.json, visualization.png

Usage:
  python -m junction_segmentation.inference \
    --checkpoint best.pth \
    --image original.png \
    --pipe-mask pipe_mask_validated.png \
    --skeleton skeleton.png \
    --output-dir ./output \
    --junction-threshold 0.55 \
    --bridge-threshold 0.60
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
from tqdm import tqdm

from .config import Config
from .dataset import TiledInferenceDataset
from .metrics import extract_local_maxima
from .model import JunctionSegModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def gaussian_blend_mask(size: int, sigma_ratio: float = 0.25) -> np.ndarray:
    sigma = size * sigma_ratio
    ax = np.arange(size, dtype=np.float32) - size / 2
    xx, yy = np.meshgrid(ax, ax)
    return np.exp(-(xx ** 2 + yy ** 2) / (2 * sigma ** 2))


def skeletonize_mask(mask: np.ndarray) -> np.ndarray:
    """Generate skeleton from pipe mask if not provided."""
    try:
        return cv2.ximgproc.thinning(
            (mask > 127).astype(np.uint8) * 255,
            thinningType=cv2.ximgproc.THINNING_ZHANGSUEN,
        )
    except AttributeError:
        from skimage.morphology import skeletonize
        return (skeletonize(mask > 127).astype(np.uint8) * 255)


def create_binary_mask(
    height: int,
    width: int,
    points: List[Dict],
    square_size: int = 15,
) -> np.ndarray:
    """Create binary mask with squares at point locations."""
    mask = np.zeros((height, width), dtype=np.uint8)
    half = square_size // 2
    for p in points:
        x, y = p["x"], p["y"]
        y1, y2 = max(0, y - half), min(height, y + half + 1)
        x1, x2 = max(0, x - half), min(width, x + half + 1)
        mask[y1:y2, x1:x2] = 255
    return mask


def create_visualization(
    image: np.ndarray,
    skeleton: np.ndarray,
    junctions: List[Dict],
    bridges: List[Dict],
    square_size: int = 15,
    darken: float = 0.35,
) -> np.ndarray:
    """Overlay: darkened original + skeleton + colored squares."""
    vis = image.astype(np.float32) * darken
    vis = vis.astype(np.uint8)

    if skeleton is not None:
        skel_mask = skeleton > 127
        vis[skel_mask] = [0, 200, 0]

    half = square_size // 2
    for p in junctions:
        x, y = p["x"], p["y"]
        y1, y2 = max(0, y - half), min(vis.shape[0], y + half + 1)
        x1, x2 = max(0, x - half), min(vis.shape[1], x + half + 1)
        vis[y1:y2, x1:x2] = [0, 255, 0]  # green

    for p in bridges:
        x, y = p["x"], p["y"]
        y1, y2 = max(0, y - half), min(vis.shape[0], y + half + 1)
        x1, x2 = max(0, x - half), min(vis.shape[1], x + half + 1)
        vis[y1:y2, x1:x2] = [255, 0, 0]  # red

    return vis


@torch.no_grad()
def run_inference(
    model: JunctionSegModel,
    image: np.ndarray,
    pipe_mask: np.ndarray,
    skeleton: np.ndarray,
    device: torch.device,
    tile_size: int = 512,
    overlap: int = 128,
    batch_size: int = 8,
    junction_threshold: float = 0.55,
    bridge_threshold: float = 0.60,
    nms_kernel: int = 3,
    use_amp: bool = True,
) -> Dict:
    """
    Full tiled inference on one image.

    Returns:
        dict with junction_points, bridge_points, heatmaps, timing
    """
    model.eval()
    t0 = time.time()

    h, w = image.shape[:2]
    blend = gaussian_blend_mask(tile_size)

    tiled = TiledInferenceDataset(image, pipe_mask, skeleton, tile_size, overlap)
    logger.info("Image %dx%d → %d tiles", w, h, len(tiled))

    prob_acc = np.zeros((2, h, w), dtype=np.float32)
    weight_acc = np.zeros((h, w), dtype=np.float32)

    batch_tensors = []
    batch_positions = []

    for i in tqdm(range(len(tiled)), desc="  Inference", leave=False):
        tensor, (ty, tx) = tiled.get_tile_tensor(i)
        batch_tensors.append(tensor)
        batch_positions.append((ty, tx))

        if len(batch_tensors) == batch_size or i == len(tiled) - 1:
            batch_t = torch.stack(batch_tensors).to(device)

            with torch.amp.autocast("cuda", enabled=use_amp):
                logits, _ = model(batch_t)
                hm = torch.sigmoid(logits)

            hm_np = hm.cpu().numpy()

            for j, (ty, tx) in enumerate(batch_positions):
                ts = tile_size
                tile_h = min(ts, h - ty)
                tile_w = min(ts, w - tx)
                prob_acc[:, ty:ty+tile_h, tx:tx+tile_w] += (
                    hm_np[j, :, :tile_h, :tile_w] * blend[:tile_h, :tile_w]
                )
                weight_acc[ty:ty+tile_h, tx:tx+tile_w] += blend[:tile_h, :tile_w]

            batch_tensors.clear()
            batch_positions.clear()

    weight_acc = np.maximum(weight_acc, 1e-8)
    pred_hm = prob_acc / weight_acc  # [2, H, W]

    # Extract points
    j_peaks = extract_local_maxima(pred_hm[0], junction_threshold, nms_kernel)
    b_peaks = extract_local_maxima(pred_hm[1], bridge_threshold, nms_kernel)

    junction_points = [{"x": int(x), "y": int(y), "confidence": round(float(c), 4)}
                       for x, y, c in sorted(j_peaks, key=lambda p: -p[2])]
    bridge_points = [{"x": int(x), "y": int(y), "confidence": round(float(c), 4)}
                     for x, y, c in sorted(b_peaks, key=lambda p: -p[2])]

    elapsed = time.time() - t0

    return {
        "junction_points": junction_points,
        "bridge_points": bridge_points,
        "heatmaps": pred_hm,
        "n_tiles": len(tiled),
        "time_sec": elapsed,
    }


def main():
    parser = argparse.ArgumentParser(description="Junction/bridge inference")
    parser.add_argument("--checkpoint", type=str, required=True)
    parser.add_argument("--image", type=str, required=True)
    parser.add_argument("--pipe-mask", type=str, required=True)
    parser.add_argument("--skeleton", type=str, default=None, help="If not provided, generated from pipe mask")
    parser.add_argument("--output-dir", type=str, required=True)
    parser.add_argument("--junction-threshold", type=float, default=None, help="Override junction threshold")
    parser.add_argument("--bridge-threshold", type=float, default=None, help="Override bridge threshold")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--device", type=str, default="cuda")
    parser.add_argument("--visualize", action="store_true")
    parser.add_argument("--square-size", type=int, default=15)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    device = torch.device(args.device if torch.cuda.is_available() else "cpu")

    # Load checkpoint
    ckpt = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
    cfg_dict = ckpt.get("config", {})
    cfg = Config()
    for k, v in cfg_dict.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)

    # Load model
    model = JunctionSegModel(cfg).to(device)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()
    logger.info("Model loaded from epoch %d", ckpt.get("epoch", -1))

    # Thresholds: CLI override > threshold_analysis.json > config defaults
    j_thr = args.junction_threshold or cfg.val_threshold_junction
    b_thr = args.bridge_threshold or cfg.val_threshold_bridge

    # Try loading optimized thresholds
    thr_path = Path(args.checkpoint).parent / "threshold_analysis" / "threshold_analysis.json"
    if thr_path.exists() and not args.junction_threshold and not args.bridge_threshold:
        with open(thr_path) as f:
            thr_data = json.load(f)
        j_thr = thr_data["recommended"]["junction_threshold"]
        b_thr = thr_data["recommended"]["bridge_threshold"]
        logger.info("Using optimized thresholds from %s", thr_path)

    logger.info("Thresholds: junction=%.2f, bridge=%.2f", j_thr, b_thr)

    # Load image
    img_bgr = cv2.imread(args.image)
    if img_bgr is None:
        raise FileNotFoundError(f"Cannot read image: {args.image}")
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    h, w = img_rgb.shape[:2]
    logger.info("Image: %s (%dx%d)", args.image, w, h)

    # Load pipe mask
    pipe_mask = cv2.imread(args.pipe_mask, cv2.IMREAD_GRAYSCALE)
    if pipe_mask is None:
        raise FileNotFoundError(f"Cannot read pipe mask: {args.pipe_mask}")

    # Load or generate skeleton
    if args.skeleton:
        skeleton = cv2.imread(args.skeleton, cv2.IMREAD_GRAYSCALE)
        if skeleton is None:
            raise FileNotFoundError(f"Cannot read skeleton: {args.skeleton}")
    else:
        logger.info("Generating skeleton from pipe mask...")
        skeleton = skeletonize_mask(pipe_mask)

    # Run inference
    result = run_inference(
        model, img_rgb, pipe_mask, skeleton, device,
        tile_size=cfg.tile_size,
        overlap=cfg.val_tile_overlap,
        batch_size=args.batch_size,
        junction_threshold=j_thr,
        bridge_threshold=b_thr,
        nms_kernel=cfg.nms_kernel,
        use_amp=cfg.amp,
    )

    junctions = result["junction_points"]
    bridges = result["bridge_points"]

    logger.info("Found %d junctions, %d bridges in %.1fs (%d tiles)",
                len(junctions), len(bridges), result["time_sec"], result["n_tiles"])

    # Save binary masks
    junction_mask = create_binary_mask(h, w, junctions, args.square_size)
    bridge_mask = create_binary_mask(h, w, bridges, args.square_size)

    cv2.imwrite(str(output_dir / "junction_mask.png"), junction_mask)
    cv2.imwrite(str(output_dir / "bridge_mask.png"), bridge_mask)

    # Save points JSON
    points_data = {
        "image": args.image,
        "width": w,
        "height": h,
        "junction_threshold": j_thr,
        "bridge_threshold": b_thr,
        "junctions": junctions,
        "bridges": bridges,
        "n_tiles": result["n_tiles"],
        "time_sec": round(result["time_sec"], 2),
    }
    with open(output_dir / "points.json", "w") as f:
        json.dump(points_data, f, indent=2)

    # Visualization
    if args.visualize:
        vis = create_visualization(img_rgb, skeleton, junctions, bridges, args.square_size)
        vis_bgr = cv2.cvtColor(vis, cv2.COLOR_RGB2BGR)
        cv2.imwrite(str(output_dir / "visualization.png"), vis_bgr)
        logger.info("Visualization saved")

    logger.info("Output: %s", output_dir)
    logger.info("  junction_mask.png: %d points", len(junctions))
    logger.info("  bridge_mask.png:   %d points", len(bridges))
    logger.info("  points.json")


if __name__ == "__main__":
    main()

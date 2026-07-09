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

# --- Наблюдаемость (Волна 3: §8.4 batch3): под-под-шаги COMPUTE tiled-инференса --
# inference.py исполняется и в worker'е (app на PYTHONPATH), и standalone-CLI
# (`python -m junction_segmentation.inference`). Слой obs импортится опционально
# (как engine.py): в CLI → no-op, инференс не ломается.
try:
    from app.core.obs import step as _obs_step
    from app.core.errors import InferenceError, GpuOutOfMemoryError, PipelineError
except Exception:  # standalone junction_segmentation: app не на PYTHONPATH
    from contextlib import contextmanager

    @contextmanager
    def _obs_step(_name, _logger, **_fields):
        yield

    class PipelineError(Exception):
        pass

    class InferenceError(PipelineError):
        pass

    class GpuOutOfMemoryError(InferenceError):
        pass


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

    with _obs_step("tiling", logger):
        h, w = image.shape[:2]
        blend = gaussian_blend_mask(tile_size)

        tiled = TiledInferenceDataset(image, pipe_mask, skeleton, tile_size, overlap)
        logger.info("Image %dx%d → %d tiles", w, h, len(tiled))

    with _obs_step("inference", logger):
        prob_acc = np.zeros((2, h, w), dtype=np.float32)
        weight_acc = np.zeros((h, w), dtype=np.float32)

        batch_tensors = []
        batch_positions = []

        try:
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
        except PipelineError:
            raise
        except Exception as exc:
            if "out of memory" in str(exc).lower():
                raise GpuOutOfMemoryError(
                    "GPU OOM during junction inference", step="inference", cause=exc
                ) from exc
            raise InferenceError(str(exc), step="inference", cause=exc) from exc

        weight_acc = np.maximum(weight_acc, 1e-8)
        pred_hm = prob_acc / weight_acc  # [2, H, W]

    # Extract points
    with _obs_step("extract_points", logger):
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
    parser = argparse.ArgumentParser(description="Junction/bridge inference (single or batch)")
    parser.add_argument("--checkpoint", type=str, required=True)
    parser.add_argument("--image", type=str, required=True,
                        help="Path to a single image file OR a directory of images for batch mode")
    parser.add_argument("--pipe-mask", type=str, required=True,
                        help="Path to a single pipe mask file OR a directory of pipe masks (matched by stem)")
    parser.add_argument("--skeleton", type=str, default=None,
                        help="Path to a single skeleton OR a directory of skeletons. "
                             "If not provided, skeleton is generated from pipe mask.")
    parser.add_argument("--output-dir", type=str, required=True,
                        help="Output directory. In batch mode each image gets its own subdirectory.")
    parser.add_argument("--junction-threshold", type=float, default=None)
    parser.add_argument("--bridge-threshold", type=float, default=None)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--device", type=str, default="cuda")
    parser.add_argument("--visualize", action="store_true")
    parser.add_argument("--square-size", type=int, default=15)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    device = torch.device(args.device if torch.cuda.is_available() else "cpu")

    # ── Load checkpoint ────────────────────────────────────────────────────────
    ckpt = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
    cfg_dict = ckpt.get("config", {})
    cfg = Config()
    for k, v in cfg_dict.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)

    # ── Load model ─────────────────────────────────────────────────────────────
    model = JunctionSegModel(cfg).to(device)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()
    logger.info("Model loaded from epoch %d", ckpt.get("epoch", -1))

    # ── Thresholds: CLI > threshold_analysis.json > config defaults ────────────
    j_thr = args.junction_threshold or cfg.val_threshold_junction
    b_thr = args.bridge_threshold or cfg.val_threshold_bridge

    thr_path = Path(args.checkpoint).parent / "threshold_analysis" / "threshold_analysis.json"
    if thr_path.exists() and not args.junction_threshold and not args.bridge_threshold:
        with open(thr_path) as f:
            thr_data = json.load(f)
        j_thr = thr_data["recommended"]["junction_threshold"]
        b_thr = thr_data["recommended"]["bridge_threshold"]
        logger.info("Using optimized thresholds from %s", thr_path)

    logger.info("Thresholds: junction=%.2f, bridge=%.2f", j_thr, b_thr)

    # ── Resolve image list ─────────────────────────────────────────────────────
    image_path = Path(args.image)
    pipe_mask_path = Path(args.pipe_mask)
    skeleton_path = Path(args.skeleton) if args.skeleton else None

    if image_path.is_dir():
        image_files = sorted(
            list(image_path.glob("*.png")) +
            list(image_path.glob("*.jpg")) +
            list(image_path.glob("*.jpeg"))
        )
        if not image_files:
            raise FileNotFoundError(f"No PNG/JPG images found in directory: {image_path}")
        batch_mode = True
        logger.info("Batch mode: %d images found in %s", len(image_files), image_path)
    elif image_path.is_file():
        image_files = [image_path]
        batch_mode = False
        logger.info("Single mode: %s", image_path)
    else:
        raise FileNotFoundError(f"--image path does not exist: {image_path}")

    # ── Process each image ─────────────────────────────────────────────────────
    total_j = 0
    total_b = 0
    failed = []

    for img_file in image_files:
        stem = img_file.stem
        logger.info("── Processing: %s", stem)

        # Load image
        img_bgr = cv2.imread(str(img_file))
        if img_bgr is None:
            logger.warning("Cannot read image: %s — skipping", img_file)
            failed.append(stem)
            continue
        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        h, w = img_rgb.shape[:2]
        logger.info("  Image size: %dx%d", w, h)

        # Load pipe mask
        if pipe_mask_path.is_dir():
            mask_file = pipe_mask_path / f"{stem}.png"
        else:
            mask_file = pipe_mask_path

        pipe_mask = cv2.imread(str(mask_file), cv2.IMREAD_GRAYSCALE)
        if pipe_mask is None:
            logger.warning("Cannot read pipe mask: %s — skipping", mask_file)
            failed.append(stem)
            continue

        # Load or generate skeleton
        if skeleton_path is not None:
            if skeleton_path.is_dir():
                skel_file = skeleton_path / f"{stem}.png"
            else:
                skel_file = skeleton_path

            skeleton = cv2.imread(str(skel_file), cv2.IMREAD_GRAYSCALE)
            if skeleton is None:
                logger.warning("Cannot read skeleton: %s — generating from pipe mask", skel_file)
                skeleton = skeletonize_mask(pipe_mask)
        else:
            logger.info("  Generating skeleton from pipe mask...")
            skeleton = skeletonize_mask(pipe_mask)

        # Output directory: batch → subdir per image, single → flat output_dir
        if batch_mode:
            img_out = output_dir / stem
            img_out.mkdir(parents=True, exist_ok=True)
        else:
            img_out = output_dir

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
        total_j += len(junctions)
        total_b += len(bridges)

        logger.info("  Found %d junctions, %d bridges in %.1fs (%d tiles)",
                    len(junctions), len(bridges), result["time_sec"], result["n_tiles"])

        # Save binary masks
        junction_mask = create_binary_mask(h, w, junctions, args.square_size)
        bridge_mask = create_binary_mask(h, w, bridges, args.square_size)
        cv2.imwrite(str(img_out / "junction_mask.png"), junction_mask)
        cv2.imwrite(str(img_out / "bridge_mask.png"), bridge_mask)

        # Save points JSON
        points_data = {
            "image": str(img_file),
            "width": w,
            "height": h,
            "junction_threshold": j_thr,
            "bridge_threshold": b_thr,
            "junctions": junctions,
            "bridges": bridges,
            "n_tiles": result["n_tiles"],
            "time_sec": round(result["time_sec"], 2),
        }
        with open(img_out / "points.json", "w") as f:
            json.dump(points_data, f, indent=2)

        # Visualization
        if args.visualize:
            vis = create_visualization(img_rgb, skeleton, junctions, bridges, args.square_size)
            vis_bgr = cv2.cvtColor(vis, cv2.COLOR_RGB2BGR)
            cv2.imwrite(str(img_out / "visualization.png"), vis_bgr)

        logger.info("  Saved → %s", img_out)

    # ── Summary ────────────────────────────────────────────────────────────────
    processed = len(image_files) - len(failed)
    logger.info("══════════════════════════════════════")
    logger.info("Done: %d/%d images processed", processed, len(image_files))
    logger.info("Total junctions: %d  bridges: %d", total_j, total_b)
    if failed:
        logger.warning("Skipped (%d): %s", len(failed), ", ".join(failed))
    logger.info("Output: %s", output_dir)


if __name__ == "__main__":
    main()
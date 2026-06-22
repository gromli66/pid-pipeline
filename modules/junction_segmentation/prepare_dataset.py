"""
prepare_dataset.py — Convert mask-based annotations to the point-based format
expected by junction_segmentation training code.

Does NOT touch model/loss/training pipeline — only data conversion.

Output layout (consumed by existing dataset.py / train.py as-is):
    <out_dir>/images/<schema>.png
    <out_dir>/pipe_masks/<schema>.png
    <out_dir>/skeletons/<schema>.png       — Zhang-Suen from pipe mask
    <out_dir>/annotations/<schema>.json    — {junctions, bridges, width, height}
    <out_dir>/dataset_meta.json            — {split: {train, val}, ...}

Point extraction:
  For each connected component, classify SINGLE vs MERGED:
    * SINGLE: contour has exactly 4 corners AND bbox is ~square AND ~fully filled
              → one point at the centroid (works for ANY single-marker size)
    * MERGED: anything else (corner "mess" — L / T / Z / diagonal shapes)
              → split greedily as 15×15 squares via boxFilter template matching

Layouts:
  nested  : <raw-dir>/<schema>/{original,pipe_mask,junction_mask,bridge_mask}.png
  flat    : <raw-dir>/{images,pipe_masks,junction_masks,bridge_masks}/<schema>.png
  jb      : <raw-dir>/j/<schema>.png + <raw-dir>/b/<schema>.png
            + --orig-dir / --pipe-dir for the remaining two
"""

import argparse
import json
import logging
import random
from pathlib import Path
from typing import Dict, List, Tuple

import cv2
import numpy as np
from tqdm import tqdm

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════
# Core extraction
# ═════════════════════════════════════════════════════════════════════════

def _extract_centers_greedy(comp_mask: np.ndarray, square_size: int,
                            fill_threshold: float = 0.9) -> List[Tuple[int, int]]:
    """
    Greedy template matching for merged components: at each iteration find
    the top-left of the score plateau where a square_size×square_size window
    is fully filled, place a point there, erase that square, repeat.

    NOTE: BORDER_CONSTANT is critical — the default reflective border
    inflates boxFilter scores at component edges and breaks extraction.
    """
    binary = (comp_mask > 127).astype(np.uint8)
    h, w = binary.shape
    work = binary.copy()
    half = square_size // 2
    target = square_size * square_size
    centers: List[Tuple[int, int]] = []
    max_iter = max(1, (h * w) // (square_size * square_size) + 2)  # safety cap

    for _ in range(max_iter):
        ones = work.astype(np.float32)
        count = cv2.boxFilter(ones, cv2.CV_32F, (square_size, square_size),
                              normalize=False, borderType=cv2.BORDER_CONSTANT)
        max_score = float(count.max())
        if max_score < target * fill_threshold:
            break
        plateau = count >= max_score - 0.5
        ys, xs = np.where(plateau)
        idx = int(np.argmin(ys.astype(np.int32) + xs.astype(np.int32)))
        cy, cx = int(ys[idx]), int(xs[idx])
        centers.append((cx, cy))
        work[max(0, cy - half):min(h, cy + half + 1),
             max(0, cx - half):min(w, cx + half + 1)] = 0
    return centers


def extract_points_from_mask(
    mask: np.ndarray,
    merged_square_size: int = 15,
    square_aspect_tol: float = 0.20,
    fill_tol: float = 0.10,
    approx_eps_frac: float = 0.02,
) -> Tuple[List[Tuple[int, int]], Dict[str, int]]:
    """
    Returns (points, summary). Each component → 1 point if it's a clean
    4-corner ~square marker; else split as 15×15 squares.
    """
    binary = (mask > 127).astype(np.uint8)
    n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        binary, connectivity=8
    )

    points: List[Tuple[int, int]] = []
    summary = {"single": 0, "merged": 0, "split_into": 0, "rejected_tiny": 0}

    for i in range(1, n_labels):
        x0 = int(stats[i, cv2.CC_STAT_LEFT])
        y0 = int(stats[i, cv2.CC_STAT_TOP])
        w  = int(stats[i, cv2.CC_STAT_WIDTH])
        h  = int(stats[i, cv2.CC_STAT_HEIGHT])
        area = int(stats[i, cv2.CC_STAT_AREA])

        if area < 4:                       # noise
            summary["rejected_tiny"] += 1
            continue

        comp_mask = (labels[y0:y0+h, x0:x0+w] == i).astype(np.uint8) * 255
        contours, _ = cv2.findContours(comp_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        cnt = max(contours, key=cv2.contourArea)
        perim = cv2.arcLength(cnt, True)
        if perim == 0:
            continue
        n_corners = len(cv2.approxPolyDP(cnt, approx_eps_frac * perim, True))

        is_square_aspect = abs(w - h) <= max(2, square_aspect_tol * max(w, h))
        is_fully_filled  = (area / max(1, w * h)) >= 1.0 - fill_tol

        if n_corners == 4 and is_square_aspect and is_fully_filled:
            cx, cy = centroids[i]
            points.append((int(round(cx)), int(round(cy))))
            summary["single"] += 1
        else:
            sub = _extract_centers_greedy(comp_mask, merged_square_size, 0.9)
            if not sub:
                # fallback: weird shape, no full square fits — take centroid
                cx, cy = centroids[i]
                points.append((int(round(cx)), int(round(cy))))
                summary["single"] += 1
            else:
                for (cx, cy) in sub:
                    points.append((cx + x0, cy + y0))
                summary["merged"] += 1
                summary["split_into"] += len(sub)
    return points, summary


def skeletonize_pipe_mask(pipe_mask: np.ndarray, binarize_threshold: int = 127) -> np.ndarray:
    """Zhang-Suen thinning (cv2.ximgproc) with skimage fallback."""
    binary = (pipe_mask > binarize_threshold).astype(np.uint8) * 255
    try:
        return cv2.ximgproc.thinning(binary, thinningType=cv2.ximgproc.THINNING_ZHANGSUEN)
    except AttributeError:
        from skimage.morphology import skeletonize
        return (skeletonize(binary > 0).astype(np.uint8) * 255)


# ═════════════════════════════════════════════════════════════════════════
# Debug visualization
# ═════════════════════════════════════════════════════════════════════════

def make_debug_vis(image: np.ndarray, junction_pts, bridge_pts) -> np.ndarray:
    vis = image.copy() if image.ndim == 3 else cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    for (x, y) in junction_pts:
        cv2.circle(vis, (x, y), 8, (0, 255, 0), 2)
        cv2.circle(vis, (x, y), 2, (0, 255, 255), -1)
    for (x, y) in bridge_pts:
        cv2.circle(vis, (x, y), 8, (0, 128, 255), 2)
        cv2.circle(vis, (x, y), 2, (0, 255, 255), -1)
    return vis


# ═════════════════════════════════════════════════════════════════════════
# Layout handling
# ═════════════════════════════════════════════════════════════════════════

def find_schemas(raw_dir: Path, layout: str, j_dir: Path) -> List[str]:
    if layout == "nested":
        return sorted(d.name for d in raw_dir.iterdir() if d.is_dir())
    exts = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
    d = (raw_dir / "images") if layout == "flat" else j_dir
    if not d or not d.exists():
        raise FileNotFoundError(f"Expected schema source dir: {d}")
    return sorted(p.stem for p in d.iterdir() if p.suffix.lower() in exts)


def get_paths(raw_dir, schema, layout, orig_dir=None, pipe_dir=None,
              j_dir=None, b_dir=None) -> Dict[str, Path]:
    if layout == "nested":
        base = raw_dir / schema
        return {"original": base / "original.png",
                "pipe":     base / "pipe_mask.png",
                "junction": base / "junction_mask.png",
                "bridge":   base / "bridge_mask.png"}
    if layout == "flat":
        return {"original": raw_dir / "images"         / f"{schema}.png",
                "pipe":     raw_dir / "pipe_masks"     / f"{schema}.png",
                "junction": raw_dir / "junction_masks" / f"{schema}.png",
                "bridge":   raw_dir / "bridge_masks"   / f"{schema}.png"}
    if layout == "jb":
        return {"original": orig_dir / f"{schema}.png",
                "pipe":     pipe_dir / f"{schema}.png",
                "junction": j_dir    / f"{schema}.png",
                "bridge":   b_dir    / f"{schema}.png"}
    raise ValueError(f"Unknown layout: {layout}")


# ═════════════════════════════════════════════════════════════════════════
# Per-schema processing
# ═════════════════════════════════════════════════════════════════════════

def process_schema(schema, raw_dir, out_dir, layout, merged_square_size,
                   orig_dir=None, pipe_dir=None, j_dir=None, b_dir=None,
                   debug_vis_dir=None) -> dict:
    paths = get_paths(raw_dir, schema, layout, orig_dir, pipe_dir, j_dir, b_dir)
    for key, p in paths.items():
        if not p.exists():
            raise FileNotFoundError(f"Schema {schema}: missing {key} at {p}")

    img = cv2.imread(str(paths["original"]))
    if img is None:
        raise IOError(f"Cannot read {paths['original']}")
    h, w = img.shape[:2]

    pipe = cv2.imread(str(paths["pipe"]),     cv2.IMREAD_GRAYSCALE)
    junc = cv2.imread(str(paths["junction"]), cv2.IMREAD_GRAYSCALE)
    brid = cv2.imread(str(paths["bridge"]),   cv2.IMREAD_GRAYSCALE)
    for arr, name in [(pipe, "pipe"), (junc, "junction"), (brid, "bridge")]:
        if arr is None:
            raise IOError(f"Cannot read {name} mask for {schema}")
        if arr.shape != (h, w):
            raise ValueError(f"Schema {schema}: {name} mask shape {arr.shape} != image {(h, w)}")

    j_pts, j_sum = extract_points_from_mask(junc, merged_square_size=merged_square_size)
    b_pts, b_sum = extract_points_from_mask(brid, merged_square_size=merged_square_size)
    skel = skeletonize_pipe_mask(pipe)

    cv2.imwrite(str(out_dir / "images"     / f"{schema}.png"), img)
    cv2.imwrite(str(out_dir / "pipe_masks" / f"{schema}.png"), pipe)
    cv2.imwrite(str(out_dir / "skeletons"  / f"{schema}.png"), skel)

    with open(out_dir / "annotations" / f"{schema}.json", "w") as f:
        json.dump({
            "width": w, "height": h,
            "junctions": [{"x": x, "y": y} for x, y in j_pts],
            "bridges":   [{"x": x, "y": y} for x, y in b_pts],
        }, f, indent=2)

    if debug_vis_dir is not None:
        cv2.imwrite(str(debug_vis_dir / f"{schema}.png"),
                    make_debug_vis(img, j_pts, b_pts))

    return {
        "schema": schema, "width": w, "height": h,
        "n_junctions": len(j_pts), "n_bridges": len(b_pts),
        "j_merged": j_sum["merged"], "j_split_into": j_sum["split_into"],
        "b_merged": b_sum["merged"], "b_split_into": b_sum["split_into"],
    }


# ═════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Convert mask-based annotations → point-based for training.")
    ap.add_argument("--raw-dir", type=str, required=True)
    ap.add_argument("--out-dir", type=str, required=True)
    ap.add_argument("--layout", choices=["nested", "flat", "jb"], default="nested")
    ap.add_argument("--orig-dir", type=str, default=None, help="(layout=jb) RGB originals dir")
    ap.add_argument("--pipe-dir", type=str, default=None, help="(layout=jb) pipe masks dir")
    ap.add_argument("--jdir", type=str, default=None, help="(layout=jb) junction masks dir; default <raw-dir>/j")
    ap.add_argument("--bdir", type=str, default=None, help="(layout=jb) bridge masks dir; default <raw-dir>/b")
    ap.add_argument("--merged-square-size", type=int, default=15)
    ap.add_argument("--val-ratio", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--debug-vis", action="store_true",
                    help="Save per-schema overlays with extracted points (green=junction, orange=bridge)")
    args = ap.parse_args()

    raw_dir  = Path(args.raw_dir)
    out_dir  = Path(args.out_dir)
    orig_dir = Path(args.orig_dir) if args.orig_dir else None
    pipe_dir = Path(args.pipe_dir) if args.pipe_dir else None
    j_dir = Path(args.jdir) if args.jdir else (raw_dir / "j" if args.layout == "jb" else None)
    b_dir = Path(args.bdir) if args.bdir else (raw_dir / "b" if args.layout == "jb" else None)

    if args.layout == "jb" and (orig_dir is None or pipe_dir is None):
        ap.error("--layout jb requires --orig-dir and --pipe-dir")
    if out_dir.exists() and any(out_dir.iterdir()) and not args.overwrite:
        raise FileExistsError(f"{out_dir} exists and not empty; pass --overwrite")

    for sub in ("images", "pipe_masks", "skeletons", "annotations"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)
    debug_vis_dir = (out_dir / "debug_vis") if args.debug_vis else None
    if debug_vis_dir is not None:
        debug_vis_dir.mkdir(parents=True, exist_ok=True)

    schemas = find_schemas(raw_dir, args.layout, j_dir)
    logger.info("Found %d schemas (layout=%s)", len(schemas), args.layout)

    stats: List[dict] = []
    failed: List[Tuple[str, str]] = []
    for schema in tqdm(schemas, desc="Processing"):
        try:
            stats.append(process_schema(
                schema, raw_dir, out_dir, args.layout, args.merged_square_size,
                orig_dir, pipe_dir, j_dir, b_dir, debug_vis_dir,
            ))
        except Exception as e:
            logger.error("Failed %s: %s", schema, e)
            failed.append((schema, str(e)))

    if failed:
        logger.warning("%d schemas failed:", len(failed))
        for s, e in failed:
            logger.warning("  %s: %s", s, e)

    # Deterministic split
    rng = random.Random(args.seed)
    ok = [s["schema"] for s in stats]
    shuffled = ok.copy(); rng.shuffle(shuffled)
    n_val = max(1, int(len(shuffled) * args.val_ratio))
    val_schemas = sorted(shuffled[:n_val])
    train_schemas = sorted(shuffled[n_val:])

    meta = {
        "split": {"train": train_schemas, "val": val_schemas},
        "seed": args.seed, "val_ratio": args.val_ratio,
        "n_train": len(train_schemas), "n_val": len(val_schemas),
        "schemas": {s["schema"]: {"width": s["width"], "height": s["height"],
                                  "n_junctions": s["n_junctions"], "n_bridges": s["n_bridges"]}
                    for s in stats},
    }
    with open(out_dir / "dataset_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    total_j = sum(s["n_junctions"] for s in stats)
    total_b = sum(s["n_bridges"]   for s in stats)
    j_merged = sum(s["j_merged"] for s in stats)
    j_split  = sum(s["j_split_into"] for s in stats)
    b_merged = sum(s["b_merged"] for s in stats)
    b_split  = sum(s["b_split_into"] for s in stats)

    logger.info("=" * 60)
    logger.info("Done. Processed: %d, Failed: %d", len(stats), len(failed))
    logger.info("Split: train=%d, val=%d", len(train_schemas), len(val_schemas))
    logger.info("Junctions: %d points total (%d merged components → %d points)",
                total_j, j_merged, j_split)
    logger.info("Bridges:   %d points total (%d merged components → %d points)",
                total_b, b_merged, b_split)
    if stats:
        logger.info("Avg/schema: j=%.1f, b=%.1f", total_j/len(stats), total_b/len(stats))
    logger.info("Output: %s", out_dir)


if __name__ == "__main__":
    main()

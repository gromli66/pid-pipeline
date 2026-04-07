"""
prepare_junction_dataset.py

Подготовка датасета для обучения junction/bridge segmentation.

Входные данные:
  - images_dir:      RGB оригиналы (.png)
  - pipe_masks_dir:  Validated pipe masks — GT сегментации (.png)
  - annotations:     annotations.json — [{schema, points: [{x, y, class}]}]

Выходная структура:
  output_dir/
  ├── images/              # симлинки или копии RGB
  ├── pipe_masks/          # симлинки или копии validated pipe masks
  ├── skeletons/           # сгенерированные скелеты
  ├── annotations/         # per-schema JSON с точками
  ├── dataset_meta.json    # метаданные: split, статистика
  └── visualizations/      # overlay для проверки разметки (опционально)

Запуск:
  python prepare_junction_dataset.py \
    --images "C:\project\pid\pid\app\data\finetune\images" \
    --pipe-masks "C:\project\pid\pid\app\data_seg_pipe\dataset\masks\pipes" \
    --annotations annotations.json \
    --output ./data/junction_seg \
    --visualize
"""

import argparse
import json
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


# =============================================================================
# Skeleton generation
# =============================================================================

def skeletonize_mask(mask: np.ndarray) -> np.ndarray:
    """
    Скелетизация бинарной маски (Zhang-Suen thinning через OpenCV).

    Args:
        mask: uint8 [H, W], 0 or 255

    Returns:
        skeleton: uint8 [H, W], 0 or 255
    """
    binary = (mask > 127).astype(np.uint8)
    skeleton = cv2.ximgproc.thinning(binary * 255, thinningType=cv2.ximgproc.THINNING_ZHANGSUEN)
    return skeleton


def skeletonize_mask_fallback(mask: np.ndarray) -> np.ndarray:
    """
    Fallback скелетизация через skimage (если нет cv2.ximgproc).
    """
    from skimage.morphology import skeletonize as sk_skeletonize
    binary = mask > 127
    skeleton = sk_skeletonize(binary).astype(np.uint8) * 255
    return skeleton


def safe_skeletonize(mask: np.ndarray) -> np.ndarray:
    """Скелетизация с fallback."""
    try:
        return skeletonize_mask(mask)
    except (AttributeError, cv2.error):
        return skeletonize_mask_fallback(mask)


# =============================================================================
# Visualization
# =============================================================================

def create_overlay(
    image: np.ndarray,
    skeleton: np.ndarray,
    junctions: List[dict],
    bridges: List[dict],
    square_size: int = 15,
    darken: float = 0.35,
) -> np.ndarray:
    """
    Overlay: затемнённый оригинал + skeleton зелёным + junction квадраты + bridge квадраты.
    """
    if image.ndim == 2:
        vis = np.stack([image] * 3, axis=-1).astype(np.float32)
    else:
        vis = image.astype(np.float32)

    vis = (vis * darken).astype(np.uint8)

    # Skeleton → зелёный
    if skeleton is not None:
        skel_mask = skeleton > 127
        vis[skel_mask] = [0, 200, 0]

    half = square_size // 2

    # Junctions → зелёные квадраты
    for p in junctions:
        x, y = p["x"], p["y"]
        y1, y2 = max(0, y - half), min(vis.shape[0], y + half + 1)
        x1, x2 = max(0, x - half), min(vis.shape[1], x + half + 1)
        vis[y1:y2, x1:x2] = [0, 255, 0]

    # Bridges → красные квадраты
    for p in bridges:
        x, y = p["x"], p["y"]
        y1, y2 = max(0, y - half), min(vis.shape[0], y + half + 1)
        x1, x2 = max(0, x - half), min(vis.shape[1], x + half + 1)
        vis[y1:y2, x1:x2] = [255, 0, 0]

    return vis


# =============================================================================
# Train/Val split by schema
# =============================================================================

def split_schemas(
    schemas: List[str],
    train_ratio: float = 0.85,
    seed: int = 42,
) -> Tuple[List[str], List[str]]:
    """
    Разделение по схемам (все тайлы одной схемы в одном split).
    """
    rng = np.random.RandomState(seed)
    indices = rng.permutation(len(schemas))
    n_train = int(len(schemas) * train_ratio)

    train_schemas = [schemas[i] for i in indices[:n_train]]
    val_schemas = [schemas[i] for i in indices[n_train:]]

    return sorted(train_schemas), sorted(val_schemas)


# =============================================================================
# Main
# =============================================================================

def prepare_dataset(
    images_dir: Path,
    pipe_masks_dir: Path,
    annotations_path: Path,
    output_dir: Path,
    visualize: bool = False,
    copy_files: bool = False,
    train_ratio: float = 0.85,
    seed: int = 42,
):
    print("=" * 70)
    print("Junction Segmentation — Dataset Preparation")
    print("=" * 70)
    print(f"  Images:      {images_dir}")
    print(f"  Pipe masks:  {pipe_masks_dir}")
    print(f"  Annotations: {annotations_path}")
    print(f"  Output:      {output_dir}")
    print(f"  Visualize:   {visualize}")
    print()

    # ── 1. Load annotations ──────────────────────────────────────────────
    with open(annotations_path, "r", encoding="utf-8") as f:
        annotations = json.load(f)

    print(f"[1/6] Loaded {len(annotations)} schemas from annotations")

    # Build lookup: schema_name → points
    ann_lookup: Dict[str, List[dict]] = {}
    for entry in annotations:
        schema = entry["schema"]
        points = entry.get("points", [])
        ann_lookup[schema] = points

    # ── 2. Find intersection ─────────────────────────────────────────────
    print(f"[2/6] Finding intersection (image + pipe_mask + annotation)...")

    # Scan available files
    image_files = {p.stem: p for p in images_dir.glob("*.png")}
    mask_files = {p.stem: p for p in pipe_masks_dir.glob("*.png")}

    # Also check jpg/tiff for images
    for ext in ("*.jpg", "*.jpeg", "*.tiff", "*.tif"):
        for p in images_dir.glob(ext):
            if p.stem not in image_files:
                image_files[p.stem] = p

    ann_schemas = set(ann_lookup.keys())
    img_schemas = set(image_files.keys())
    mask_schemas = set(mask_files.keys())

    # Intersection
    valid_schemas = sorted(ann_schemas & img_schemas & mask_schemas)

    # Exclude schemas with 0 points
    non_empty = [s for s in valid_schemas if len(ann_lookup[s]) > 0]
    empty = [s for s in valid_schemas if len(ann_lookup[s]) == 0]

    print(f"  Annotations: {len(ann_schemas)} schemas")
    print(f"  Images:      {len(img_schemas)} files")
    print(f"  Pipe masks:  {len(mask_schemas)} files")
    print(f"  Intersection (all 3): {len(valid_schemas)}")
    print(f"  With points: {len(non_empty)}")
    print(f"  Empty (0 points, skipped): {len(empty)}")

    # Show what's missing
    missing_img = ann_schemas - img_schemas
    missing_mask = ann_schemas - mask_schemas
    if missing_img:
        print(f"\n  ⚠ Missing images ({len(missing_img)}):")
        for s in sorted(missing_img)[:10]:
            print(f"    {s}")
        if len(missing_img) > 10:
            print(f"    ... and {len(missing_img) - 10} more")

    if missing_mask:
        print(f"\n  ⚠ Missing pipe masks ({len(missing_mask)}):")
        for s in sorted(missing_mask)[:10]:
            print(f"    {s}")
        if len(missing_mask) > 10:
            print(f"    ... and {len(missing_mask) - 10} more")

    if len(non_empty) == 0:
        print("\n❌ No valid schemas found. Check paths.")
        return

    schemas = non_empty

    # ── 3. Create output structure ───────────────────────────────────────
    print(f"\n[3/6] Creating output directory: {output_dir}")

    out_images = output_dir / "images"
    out_masks = output_dir / "pipe_masks"
    out_skeletons = output_dir / "skeletons"
    out_annotations = output_dir / "annotations"

    for d in [out_images, out_masks, out_skeletons, out_annotations]:
        d.mkdir(parents=True, exist_ok=True)

    if visualize:
        out_vis = output_dir / "visualizations"
        out_vis.mkdir(parents=True, exist_ok=True)

    # ── 4. Process each schema ───────────────────────────────────────────
    print(f"\n[4/6] Processing {len(schemas)} schemas...")

    stats = {
        "total_schemas": 0,
        "total_junctions": 0,
        "total_bridges": 0,
        "schemas": {},
    }

    t0 = time.time()

    for i, schema in enumerate(schemas):
        img_path = image_files[schema]
        mask_path = mask_files[schema]
        points = ann_lookup[schema]

        # ── Load and validate ──
        image = cv2.imread(str(img_path))
        if image is None:
            print(f"  ⚠ Cannot read image: {img_path}, skipping")
            continue

        pipe_mask = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
        if pipe_mask is None:
            print(f"  ⚠ Cannot read mask: {mask_path}, skipping")
            continue

        h_img, w_img = image.shape[:2]
        h_mask, w_mask = pipe_mask.shape[:2]

        # Check size match
        if (h_img, w_img) != (h_mask, w_mask):
            print(f"  ⚠ Size mismatch {schema}: image {w_img}x{h_img} vs mask {w_mask}x{h_mask}, skipping")
            continue

        # ── Filter points within bounds ──
        junctions = []
        bridges = []
        out_of_bounds = 0

        for p in points:
            x, y = p["x"], p["y"]
            if 0 <= x < w_img and 0 <= y < h_img:
                if p["class"] == "junction":
                    junctions.append({"x": x, "y": y})
                elif p["class"] == "bridge":
                    bridges.append({"x": x, "y": y})
            else:
                out_of_bounds += 1

        if out_of_bounds > 0:
            print(f"  ⚠ {schema}: {out_of_bounds} points out of bounds, filtered")

        # ── Generate skeleton ──
        skeleton = safe_skeletonize(pipe_mask)

        # ── Copy/link files ──
        dst_img = out_images / f"{schema}.png"
        dst_mask = out_masks / f"{schema}.png"
        dst_skel = out_skeletons / f"{schema}.png"

        if copy_files:
            if not dst_img.exists():
                shutil.copy2(img_path, dst_img)
            if not dst_mask.exists():
                shutil.copy2(mask_path, dst_mask)
        else:
            # Symlinks (Windows: might need admin or developer mode)
            try:
                if not dst_img.exists():
                    dst_img.symlink_to(img_path.resolve())
                if not dst_mask.exists():
                    dst_mask.symlink_to(mask_path.resolve())
            except OSError:
                # Fallback to copy on Windows without symlink permissions
                if not dst_img.exists():
                    shutil.copy2(img_path, dst_img)
                if not dst_mask.exists():
                    shutil.copy2(mask_path, dst_mask)

        # Save skeleton
        cv2.imwrite(str(dst_skel), skeleton)

        # ── Save annotation ──
        ann_data = {
            "schema": schema,
            "width": w_img,
            "height": h_img,
            "junctions": junctions,
            "bridges": bridges,
        }
        with open(out_annotations / f"{schema}.json", "w") as f:
            json.dump(ann_data, f, indent=2)

        # ── Visualization ──
        if visualize:
            image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            overlay = create_overlay(image_rgb, skeleton, junctions, bridges)
            overlay_bgr = cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR)
            cv2.imwrite(str(out_vis / f"{schema}.jpg"), overlay_bgr, [cv2.IMWRITE_JPEG_QUALITY, 60])

        # ── Stats ──
        stats["total_schemas"] += 1
        stats["total_junctions"] += len(junctions)
        stats["total_bridges"] += len(bridges)
        stats["schemas"][schema] = {
            "width": w_img,
            "height": h_img,
            "junctions": len(junctions),
            "bridges": len(bridges),
        }

        if (i + 1) % 10 == 0 or i == len(schemas) - 1:
            elapsed = time.time() - t0
            print(f"  [{i+1}/{len(schemas)}] {elapsed:.1f}s — {schema} ({len(junctions)}j, {len(bridges)}b)")

    # ── 5. Train/val split ───────────────────────────────────────────────
    print(f"\n[5/6] Splitting train/val (ratio={train_ratio})...")

    processed_schemas = sorted(stats["schemas"].keys())
    train_schemas, val_schemas = split_schemas(processed_schemas, train_ratio, seed)

    train_junctions = sum(stats["schemas"][s]["junctions"] for s in train_schemas)
    train_bridges = sum(stats["schemas"][s]["bridges"] for s in train_schemas)
    val_junctions = sum(stats["schemas"][s]["junctions"] for s in val_schemas)
    val_bridges = sum(stats["schemas"][s]["bridges"] for s in val_schemas)

    print(f"  Train: {len(train_schemas)} schemas ({train_junctions} junctions, {train_bridges} bridges)")
    print(f"  Val:   {len(val_schemas)} schemas ({val_junctions} junctions, {val_bridges} bridges)")

    # ── 6. Save metadata ─────────────────────────────────────────────────
    print(f"\n[6/6] Saving metadata...")

    meta = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sources": {
            "images_dir": str(images_dir),
            "pipe_masks_dir": str(pipe_masks_dir),
            "annotations": str(annotations_path),
        },
        "stats": {
            "total_schemas": stats["total_schemas"],
            "total_junctions": stats["total_junctions"],
            "total_bridges": stats["total_bridges"],
            "avg_junctions_per_schema": round(stats["total_junctions"] / max(stats["total_schemas"], 1), 1),
            "avg_bridges_per_schema": round(stats["total_bridges"] / max(stats["total_schemas"], 1), 1),
        },
        "split": {
            "seed": seed,
            "train_ratio": train_ratio,
            "train": train_schemas,
            "val": val_schemas,
        },
        "schemas": stats["schemas"],
    }

    meta_path = output_dir / "dataset_meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    # ── Summary ──────────────────────────────────────────────────────────
    elapsed_total = time.time() - t0
    print(f"\n{'=' * 70}")
    print("✅ Dataset prepared!")
    print(f"{'=' * 70}")
    print(f"  Schemas:    {stats['total_schemas']}")
    print(f"  Junctions:  {stats['total_junctions']}")
    print(f"  Bridges:    {stats['total_bridges']}")
    print(f"  Train/Val:  {len(train_schemas)}/{len(val_schemas)}")
    print(f"  Time:       {elapsed_total:.1f}s")
    print(f"\n  Output: {output_dir}")
    print(f"  Meta:   {meta_path}")
    if visualize:
        print(f"  Viz:    {out_vis}")
    print()


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Prepare dataset for junction/bridge segmentation training"
    )
    parser.add_argument(
        "--images",
        type=str,
        required=True,
        help="Path to RGB originals directory",
    )
    parser.add_argument(
        "--pipe-masks",
        type=str,
        required=True,
        help="Path to validated pipe masks directory (GT for pipe segmentation)",
    )
    parser.add_argument(
        "--annotations",
        type=str,
        required=True,
        help="Path to annotations.json",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="./data/junction_seg",
        help="Output directory (default: ./data/junction_seg)",
    )
    parser.add_argument(
        "--visualize",
        action="store_true",
        help="Generate overlay visualizations for verification",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="Copy files instead of symlinks (uses more disk space)",
    )
    parser.add_argument(
        "--train-ratio",
        type=float,
        default=0.85,
        help="Train split ratio (default: 0.85)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for split (default: 42)",
    )

    args = parser.parse_args()

    prepare_dataset(
        images_dir=Path(args.images),
        pipe_masks_dir=Path(args.pipe_masks),
        annotations_path=Path(args.annotations),
        output_dir=Path(args.output),
        visualize=args.visualize,
        copy_files=args.copy,
        train_ratio=args.train_ratio,
        seed=args.seed,
    )


if __name__ == "__main__":
    main()

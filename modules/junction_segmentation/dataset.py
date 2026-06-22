"""
Dataset for junction/bridge segmentation.

Sampling strategy:
  - 60% positive: random crop centered near a random junction/bridge point
  - 40% negative: random crop from a random schema (no points inside)

Input: 5 channels [RGB(3) + pipe_mask(1) + skeleton(1)]
GT: 2 Gaussian heatmaps [junction + bridge], float32 [0..1]
Aux: binary label — does this tile contain any point?
"""

import json
import random
from pathlib import Path
from typing import Dict, List, Tuple

import cv2
import numpy as np
import torch
from torch.utils.data import Dataset

from .config import Config


# ═════════════════════════════════════════════════════════════════════════
# Gaussian heatmap generation
# ═════════════════════════════════════════════════════════════════════════

def gaussian_heatmap(
    height: int,
    width: int,
    points: List[Tuple[int, int]],
    sigma: float = 4.0,
) -> np.ndarray:
    """
    Generate a Gaussian heatmap with peaks at given points.
    CenterNet style: peak = 1.0 at center, Gaussian decay.
    Multiple points: element-wise max (not sum).
    """
    heatmap = np.zeros((height, width), dtype=np.float32)
    if len(points) == 0:
        return heatmap

    radius = int(3 * sigma + 0.5)  # truncate at 3σ

    for px, py in points:
        x0, y0 = int(round(px)), int(round(py))

        y_min = max(0, y0 - radius)
        y_max = min(height, y0 + radius + 1)
        x_min = max(0, x0 - radius)
        x_max = min(width, x0 + radius + 1)

        if y_min >= y_max or x_min >= x_max:
            continue

        Y = np.arange(y_min, y_max, dtype=np.float32)
        X = np.arange(x_min, x_max, dtype=np.float32)
        XX, YY = np.meshgrid(X, Y)

        g = np.exp(-((XX - x0) ** 2 + (YY - y0) ** 2) / (2 * sigma ** 2))
        heatmap[y_min:y_max, x_min:x_max] = np.maximum(
            heatmap[y_min:y_max, x_min:x_max], g
        )

    return heatmap


# ═════════════════════════════════════════════════════════════════════════
# Augmentations (sync geometric for all channels, color for RGB only)
# ═════════════════════════════════════════════════════════════════════════

def augment_geometric(
    images: List[np.ndarray],
    points_list: List[List[Tuple[int, int]]],
    hflip_p: float = 0.5,
    vflip_p: float = 0.5,
    rot90_p: float = 0.5,
) -> Tuple[List[np.ndarray], List[List[Tuple[int, int]]]]:
    """
    Apply synchronized geometric augmentations to images AND point lists.

    Instead of composing lambda transforms (error-prone), we transform
    points directly after each operation using the known geometry.

    Args:
        images: list of arrays [H,W,...] to transform identically
        points_list: list of point-lists, each [(x,y), ...]
        hflip_p, vflip_p, rot90_p: augmentation probabilities

    Returns:
        (augmented_images, augmented_points_list)
    """
    h, w = images[0].shape[:2]

    # Horizontal flip
    if random.random() < hflip_p:
        images = [np.ascontiguousarray(img[:, ::-1]) for img in images]
        points_list = [
            [(w - 1 - px, py) for px, py in pts]
            for pts in points_list
        ]

    # Vertical flip
    if random.random() < vflip_p:
        images = [np.ascontiguousarray(img[::-1, :]) for img in images]
        points_list = [
            [(px, h - 1 - py) for px, py in pts]
            for pts in points_list
        ]

    # Rotation 90° (CCW, k times)
    if random.random() < rot90_p:
        k = random.choice([1, 2, 3])
        images = [np.rot90(img, k).copy() for img in images]

        # np.rot90(arr, 1) single step: pixel at (x, y) → (y, H-1-x)
        # Verified numerically against np.rot90 output.
        cur_h, cur_w = h, w
        new_points_list = [list(pts) for pts in points_list]
        for _ in range(k):
            new_points_list = [
                [(py, cur_h - 1 - px) for px, py in pts]
                for pts in new_points_list
            ]
            cur_h, cur_w = cur_w, cur_h  # dimensions swap on each 90° step

        points_list = new_points_list

    return images, points_list


def augment_color(rgb: np.ndarray, cfg: Config) -> np.ndarray:
    """
    Color augmentations applied ONLY to RGB channels.

    Order emulates a real scan/print pipeline:
      brightness → contrast → gamma   (sensor electronics / paper aging)
        → gaussian noise              (sensor noise)
        → JPEG compression            (storage artifacts)

    Each effect is independent (own probability) so the model sees
    a roughly uniform mix of: clean tiles, single-effect tiles,
    and multi-effect tiles.
    """
    # Brightness — multiplicative
    if random.random() < cfg.aug_brightness:
        factor = 1.0 + random.uniform(-cfg.aug_brightness_limit, cfg.aug_brightness_limit)
        rgb = np.clip(rgb.astype(np.float32) * factor, 0, 255).astype(np.uint8)

    # Contrast — scaling around per-channel mean
    # (per-channel rather than global: handles slight color casts on aged paper)
    if random.random() < cfg.aug_contrast:
        factor = 1.0 + random.uniform(-cfg.aug_contrast_limit, cfg.aug_contrast_limit)
        mean = rgb.mean(axis=(0, 1), keepdims=True)
        rgb_f = (rgb.astype(np.float32) - mean) * factor + mean
        rgb = np.clip(rgb_f, 0, 255).astype(np.uint8)

    # Gamma correction via LUT (fast, exact for uint8)
    # gamma > 1 → darker (over-copied), gamma < 1 → lighter (faded)
    if random.random() < cfg.aug_gamma:
        gamma = random.uniform(cfg.aug_gamma_min, cfg.aug_gamma_max)
        table = (np.linspace(0, 1, 256) ** gamma * 255).astype(np.uint8)
        rgb = cv2.LUT(rgb, table)

    # Gaussian noise (sensor noise)
    if random.random() < cfg.aug_noise:
        noise = np.random.normal(0, cfg.aug_noise_var, rgb.shape).astype(np.float32)
        rgb = np.clip(rgb.astype(np.float32) + noise, 0, 255).astype(np.uint8)

    # JPEG compression artifacts
    # cv2 expects BGR for correct YCrCb chroma subsampling → swap explicitly
    if random.random() < cfg.aug_jpeg:
        q = random.randint(cfg.aug_jpeg_quality_min, cfg.aug_jpeg_quality_max)
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, q])
        if ok:
            bgr_dec = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            rgb = cv2.cvtColor(bgr_dec, cv2.COLOR_BGR2RGB)

    return rgb


# ═════════════════════════════════════════════════════════════════════════
# Schema data cache
# ═════════════════════════════════════════════════════════════════════════

class SchemaCache:
    """LRU cache for loaded schema images (per-worker safe)."""

    def __init__(self, data_dir: Path, max_size: int = 12):
        self.data_dir = data_dir
        self.max_size = max_size
        self._cache: Dict[str, Tuple] = {}
        self._order: List[str] = []

    def get(self, schema: str):
        if schema in self._cache:
            return self._cache[schema]

        img = cv2.imread(str(self.data_dir / "images" / f"{schema}.png"))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB) if img is not None else None
        mask = cv2.imread(str(self.data_dir / "pipe_masks" / f"{schema}.png"), cv2.IMREAD_GRAYSCALE)
        skel = cv2.imread(str(self.data_dir / "skeletons" / f"{schema}.png"), cv2.IMREAD_GRAYSCALE)

        if img is None or mask is None or skel is None:
            raise FileNotFoundError(f"Missing files for schema {schema}")

        data = (img, mask, skel)
        self._cache[schema] = data
        self._order.append(schema)

        if len(self._order) > self.max_size:
            evict = self._order.pop(0)
            self._cache.pop(evict, None)

        return data


# ═════════════════════════════════════════════════════════════════════════
# Dataset
# ═════════════════════════════════════════════════════════════════════════

class JunctionDataset(Dataset):
    """
    Training dataset: random crops with balanced positive/negative sampling.
    """

    def __init__(self, schemas: List[str], data_dir: str, cfg: Config, is_train: bool = True):
        self.data_dir = Path(data_dir)
        self.cfg = cfg
        self.is_train = is_train
        self.tile_size = cfg.tile_size

        # Load annotations
        self.annotations: Dict[str, dict] = {}
        for schema in schemas:
            ann_path = self.data_dir / "annotations" / f"{schema}.json"
            with open(ann_path) as f:
                self.annotations[schema] = json.load(f)

        self.schemas = list(self.annotations.keys())

        # Build positive sample index: (schema, x, y, class)
        self.positives = []
        for schema, ann in self.annotations.items():
            for p in ann["junctions"]:
                self.positives.append((schema, p["x"], p["y"], "junction"))
            for p in ann["bridges"]:
                self.positives.append((schema, p["x"], p["y"], "bridge"))

        # Image cache
        self._cache = SchemaCache(self.data_dir, max_size=12)

        # ImageNet normalization for RGB
        self.rgb_mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        self.rgb_std = np.array([0.229, 0.224, 0.225], dtype=np.float32)

    def __len__(self):
        if self.is_train:
            return self.cfg.epoch_tiles
        return len(self.positives)

    def __getitem__(self, idx):
        if self.is_train:
            return self._get_train_item()
        else:
            return self._get_val_item(idx)

    def _get_train_item(self):
        if random.random() < self.cfg.positive_ratio and self.positives:
            return self._sample_positive()
        else:
            return self._sample_negative()

    def _sample_positive(self):
        schema, px, py, _ = random.choice(self.positives)
        ann = self.annotations[schema]
        jitter = self.cfg.jitter_px
        cx = px + random.randint(-jitter, jitter)
        cy = py + random.randint(-jitter, jitter)
        return self._extract_tile(schema, cx, cy, ann)

    def _sample_negative(self):
        schema = random.choice(self.schemas)
        ann = self.annotations[schema]
        half = self.tile_size // 2
        cx = random.randint(half, max(half, ann["width"] - half - 1))
        cy = random.randint(half, max(half, ann["height"] - half - 1))
        return self._extract_tile(schema, cx, cy, ann)

    def _get_val_item(self, idx):
        schema, px, py, _ = self.positives[idx % len(self.positives)]
        ann = self.annotations[schema]
        return self._extract_tile(schema, px, py, ann)

    def _extract_tile(self, schema, cx, cy, ann):
        ts = self.tile_size
        half = ts // 2
        w_img, h_img = ann["width"], ann["height"]

        # Clamp crop to image bounds
        x1 = max(0, min(cx - half, w_img - ts))
        y1 = max(0, min(cy - half, h_img - ts))
        x2 = x1 + ts
        y2 = y1 + ts

        # Load data
        img, mask, skel = self._cache.get(schema)

        # Crop
        rgb_crop = img[y1:y2, x1:x2].copy()
        mask_crop = mask[y1:y2, x1:x2].copy()
        skel_crop = skel[y1:y2, x1:x2].copy()

        # Points within this crop (local coordinates)
        junctions_local = [
            (p["x"] - x1, p["y"] - y1)
            for p in ann["junctions"]
            if 0 <= p["x"] - x1 < ts and 0 <= p["y"] - y1 < ts
        ]
        bridges_local = [
            (p["x"] - x1, p["y"] - y1)
            for p in ann["bridges"]
            if 0 <= p["x"] - x1 < ts and 0 <= p["y"] - y1 < ts
        ]

        # ── Augmentations ────────────────────────────────────────────
        if self.is_train:
            arrays, (junctions_local, bridges_local) = augment_geometric(
                [rgb_crop, mask_crop, skel_crop],
                [junctions_local, bridges_local],
                self.cfg.aug_hflip, self.cfg.aug_vflip, self.cfg.aug_rotate90,
            )
            rgb_crop, mask_crop, skel_crop = arrays
            rgb_crop = augment_color(rgb_crop, self.cfg)

        # ── Build 5-channel input tensor ─────────────────────────────
        h_tile, w_tile = rgb_crop.shape[:2]

        rgb_f = rgb_crop.astype(np.float32) / 255.0
        rgb_f = (rgb_f - self.rgb_mean) / self.rgb_std

        mask_f = mask_crop.astype(np.float32) / 255.0
        skel_f = skel_crop.astype(np.float32) / 255.0

        input_5ch = np.concatenate([
            rgb_f,
            mask_f[:, :, np.newaxis],
            skel_f[:, :, np.newaxis],
        ], axis=2).transpose(2, 0, 1).astype(np.float32)

        # ── Generate GT heatmaps ─────────────────────────────────────
        hm_junction = gaussian_heatmap(h_tile, w_tile, junctions_local, self.cfg.sigma)
        hm_bridge = gaussian_heatmap(h_tile, w_tile, bridges_local, self.cfg.sigma)
        heatmap = np.stack([hm_junction, hm_bridge], axis=0).astype(np.float32)

        has_positive = len(junctions_local) + len(bridges_local) > 0

        return {
            "input": torch.from_numpy(input_5ch),
            "heatmap": torch.from_numpy(heatmap),
            "has_positive": torch.tensor(1.0 if has_positive else 0.0, dtype=torch.float32),
        }


# ═════════════════════════════════════════════════════════════════════════
# Full-image tiled inference dataset (for validation)
# ═════════════════════════════════════════════════════════════════════════

class TiledInferenceDataset:
    """Cuts a single full image into overlapping tiles for inference."""

    def __init__(self, image, pipe_mask, skeleton, tile_size=512, overlap=128):
        self.image = image
        self.pipe_mask = pipe_mask
        self.skeleton = skeleton
        self.tile_size = tile_size
        self.stride = tile_size - overlap
        self.h, self.w = image.shape[:2]
        self.positions = self._compute_positions()
        self.rgb_mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        self.rgb_std = np.array([0.229, 0.224, 0.225], dtype=np.float32)

    def _compute_positions(self):
        ts, s, h, w = self.tile_size, self.stride, self.h, self.w
        positions = set()
        for y in range(0, max(1, h - ts + 1), s):
            for x in range(0, max(1, w - ts + 1), s):
                positions.add((y, x))
        # Ensure right and bottom edges are covered
        if h >= ts:
            for x in range(0, max(1, w - ts + 1), s):
                positions.add((h - ts, x))
        if w >= ts:
            for y in range(0, max(1, h - ts + 1), s):
                positions.add((y, w - ts))
        if h >= ts and w >= ts:
            positions.add((h - ts, w - ts))
        return sorted(positions)

    def get_tile_tensor(self, idx) -> Tuple[torch.Tensor, Tuple[int, int]]:
        y, x = self.positions[idx]
        ts = self.tile_size
        rgb = self.image[y:y+ts, x:x+ts]
        mask = self.pipe_mask[y:y+ts, x:x+ts]
        skel = self.skeleton[y:y+ts, x:x+ts]

        rgb_f = rgb.astype(np.float32) / 255.0
        rgb_f = (rgb_f - self.rgb_mean) / self.rgb_std
        mask_f = mask.astype(np.float32) / 255.0
        skel_f = skel.astype(np.float32) / 255.0

        tensor = np.concatenate([
            rgb_f,
            mask_f[:, :, np.newaxis],
            skel_f[:, :, np.newaxis],
        ], axis=2).transpose(2, 0, 1).astype(np.float32)

        return torch.from_numpy(tensor), (y, x)

    def __len__(self):
        return len(self.positions)

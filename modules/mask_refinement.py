"""
mask_refinement.py — Коррекция маски труб через adaptive dilate из оригинала.

Алгоритм:
  1. Скелетизация маски
  2. Бинаризация оригинала → distance_transform → реальная толщина трубы
  3. Propagate толщину на extension-участки скелета (вне линий)
  4. Adaptive dilate по группам толщин
  5. Clip к реальным линиям чертежа + исходной маске
  6. Вычитание node_mask

Вызывается из worker/tasks/skeleton.py перед skeleton_extension.
"""

import logging
import time

import cv2
import numpy as np
from skimage.morphology import skeletonize

logger = logging.getLogger(__name__)


def refine_pipe_mask(pipe_mask_bin, image_bgr, node_mask_bin=None,
                     min_thickness=2, max_thickness=30):
    """
    Коррекция маски труб: skeleton → adaptive dilate по реальной толщине
    из оригинального изображения.

    Args:
        pipe_mask_bin: бинарная маска труб (uint8, 0/1)
        image_bgr: оригинальное изображение (BGR, uint8)
        node_mask_bin: маска узлов (uint8, 0/1), вычитается из результата.
                       None — не вычитать.
        min_thickness: минимальный диаметр трубы (px)
        max_thickness: максимальный диаметр трубы (px)

    Returns:
        (refined_mask, stats):
            refined_mask: uint8, 0/255
            stats: dict с метриками
    """
    t0 = time.time()
    H, W = pipe_mask_bin.shape

    # ── 1. Скелетизация ──
    skel = skeletonize(pipe_mask_bin > 0).astype(np.uint8)
    skel_ys, skel_xs = np.where(skel > 0)
    if len(skel_ys) == 0:
        logger.warning("Empty skeleton — returning zero mask")
        return np.zeros((H, W), dtype=np.uint8), {"time": 0, "skeleton_px": 0}

    # ── 2. Бинаризация оригинала → DT → карта толщин ──
    gray = np.min(image_bgr, axis=2)
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 51, 10)
    # binary: 255 = фон, 0 = линии
    lines = (binary == 0).astype(np.uint8)
    dt = cv2.distanceTransform(lines, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    # dt[y,x] = расстояние от линии до фона = half-width
    thickness_map = np.clip(dt * 2, 0, max_thickness).astype(np.float32)

    # ── 3. Propagate толщину на extension-участки ──
    skel_thick = thickness_map[skel_ys, skel_xs]
    covered = skel_thick > 0
    fallback = float(np.median(skel_thick[covered])) if np.any(covered) else 6.0

    if not np.all(covered) and np.any(covered):
        from scipy.spatial import cKDTree
        covered_pts = np.column_stack((skel_ys[covered], skel_xs[covered]))
        uncovered_idx = np.where(~covered)[0]
        uncovered_pts = np.column_stack(
            (skel_ys[uncovered_idx], skel_xs[uncovered_idx]))
        tree = cKDTree(covered_pts)
        _, nearest = tree.query(uncovered_pts)
        for i, idx in enumerate(uncovered_idx):
            thickness_map[skel_ys[idx], skel_xs[idx]] = \
                skel_thick[covered][nearest[i]]
    elif not np.any(covered):
        thickness_map[skel_ys, skel_xs] = fallback

    # ── 4. Adaptive dilate ──
    mask = np.zeros((H, W), dtype=np.uint8)
    diameters = thickness_map[skel_ys, skel_xs]
    diameters_int = np.clip(
        np.round(diameters).astype(int), min_thickness, max_thickness)
    diameters_int = diameters_int | 1  # нечётные для симметричного ядра

    for d in np.unique(diameters_int):
        group = (diameters_int == d)
        group_skel = np.zeros((H, W), dtype=np.uint8)
        group_skel[skel_ys[group], skel_xs[group]] = 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (d, d))
        mask = np.maximum(mask, cv2.dilate(group_skel, kernel))

    # ── 5. Clip к реальным линиям + исходной маске ──
    clip_expand = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    lines_expanded = cv2.dilate(lines, clip_expand)
    clip_zone = np.maximum(lines_expanded, pipe_mask_bin)
    before_clip = int(np.sum(mask))
    mask = mask & clip_zone
    after_clip = int(np.sum(mask))

    # ── 6. Вычитаем node_mask ──
    if node_mask_bin is not None:
        mask[node_mask_bin > 0] = 0

    # Closing для сглаживания
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kern)

    elapsed = time.time() - t0
    stats = {
        "time": round(elapsed, 2),
        "skeleton_px": len(skel_ys),
        "original_px": int(np.sum(pipe_mask_bin)),
        "refined_px": int(np.sum(mask)),
        "clipped_px": before_clip - after_clip,
        "median_thickness": round(float(np.median(diameters)), 1),
        "thickness_groups": len(np.unique(diameters_int)),
    }

    return mask * 255, stats

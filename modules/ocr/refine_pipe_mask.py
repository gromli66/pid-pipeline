"""
refine_pipe_mask.py — Уточнение маски труб по графовой структуре P&ID.

Адаптировано для серверного использования (без CLI).

Алгоритм:
  1. Distance transform маски → медианная полуширина трубы (из данных)
  2. Скелетизация маски труб → центральная линия
  3. Разрез скелета по точкам junction/bridge из аннотаций
  4. Dilate скелета по медианной полуширине → уточнённая маска

Все параметры вычисляются из данных.

Экспортирует:
  - load_graph_points(annotations_path, schema_name)
  - refine_pipe_mask(pipe_mask, junctions, bridges)
"""

import json
import time
import cv2
import numpy as np
from pathlib import Path
from skimage.morphology import skeletonize


# ── I/O (кириллические пути) ─────────────────────────────

def load_image_cv2(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def load_mask(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    return (img > 127).astype(np.uint8)


def find_mask(stem, mask_dir):
    mask_dir = Path(mask_dir)
    if not mask_dir.exists():
        return None
    for ext in ['.png', '.jpg', '.jpeg', '.tif', '.tiff']:
        candidate = mask_dir / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    for f in mask_dir.iterdir():
        if f.suffix.lower() in ['.png', '.jpg', '.jpeg', '.tif', '.tiff']:
            if stem in f.stem or f.stem in stem:
                return f
    return None


def ensure_size(mask, H, W):
    if mask is None:
        return None
    if mask.shape[:2] != (H, W):
        return cv2.resize(mask, (W, H), interpolation=cv2.INTER_NEAREST)
    return mask


# ── Загрузка аннотаций ───────────────────────────────────

def load_graph_points(annotations_path, schema_name):
    """
    Загружает junction/bridge точки из annotations.json
    по schema или image_path, содержащему schema_name.

    Возвращает (junctions, bridges) — списки (x, y).
    """
    with open(annotations_path, encoding='utf-8') as f:
        data = json.load(f)

    for item in data:
        img_path = item.get('image_path', '')
        schema = item.get('schema', '')
        if schema_name in img_path or schema_name in schema:
            junctions = []
            bridges = []
            for p in item.get('points', []):
                x, y = int(p['x']), int(p['y'])
                cls = p.get('class', '')
                if cls == 'junction':
                    junctions.append((x, y))
                elif cls == 'bridge':
                    bridges.append((x, y))
            return junctions, bridges

    return [], []


# ── Уточнение маски ──────────────────────────────────────

def refine_pipe_mask(pipe_mask, junctions, bridges):
    """
    Уточняет маску труб через скелетизацию + разрез по графу.

    1. Distance transform → медианная полуширина (из данных)
    2. Скелетизация → центральная линия
    3. Вырезаем junction/bridge точки (разрываем скелет)
    4. Dilate скелета по медианной полуширине

    Все параметры — из данных маски и аннотаций.

    Возвращает: (refined_mask, stats_dict)
    """
    # Distance transform → локальная полуширина
    dist = cv2.distanceTransform(pipe_mask, cv2.DIST_L2, 5)
    vals = dist[dist > 0]
    if len(vals) == 0:
        return np.zeros_like(pipe_mask), {'med_hw': 0, 'skel_px': 0}

    med_hw = float(np.median(vals))

    # Скелетизация
    skel = skeletonize(pipe_mask > 0).astype(np.uint8)
    skel_px = int(np.count_nonzero(skel))

    # Разрез по junction/bridge точкам
    # Радиус разреза — из данных (пропорционален полуширине трубы)
    cut_r = int(med_hw * 2) + 1
    cuts = 0
    for x, y in junctions + bridges:
        cv2.circle(skel, (int(x), int(y)), cut_r, 0, -1)
        cuts += 1

    # CC на разрезанном скелете
    n_cc, _, _, _ = cv2.connectedComponentsWithStats(skel, 8)

    # Dilate по медианной полуширине
    dilation_r = int(np.ceil(med_hw)) + 1
    kern = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * dilation_r + 1, 2 * dilation_r + 1))
    refined = cv2.dilate(skel, kern)

    stats = {
        'med_hw': round(med_hw, 1),
        'skel_px': skel_px,
        'cut_r': cut_r,
        'cuts': cuts,
        'segments': n_cc - 1,
        'dilation_r': dilation_r,
    }

    return refined, stats


# CLI visualization (save_visualization) удалена — server mode.


# CLI (main, save_visualization) удалены — server mode.
# Используются: load_graph_points, refine_pipe_mask, ensure_size, load_mask.
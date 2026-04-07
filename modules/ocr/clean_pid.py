"""
Step 1: Очистка P&ID от коммуникаций (труб + оборудования) для OCR.

Адаптировано из: clean_pid_for_ocr.py

Вход:
  - diagram_dir/original/image.png
  - diagram_dir/segmentation/pipe_mask_validated.png (fallback: pipe_mask.png)
  - diagram_dir/segmentation/node_mask.png
  - diagram_dir/skeleton/skeleton_final.png
  - diagram_dir/graph/graph_validated.json (fallback: graph.json)

Выход:
  - ocr_dir/cleaned_for_ocr.png (цветная)
  - ocr_dir/cleaned_for_ocr_gray.png (серая)
"""

import json
import logging
import math
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from modules.ocr.utils import Image as _  # triggers MAX_IMAGE_PIXELS = None

logger = logging.getLogger(__name__)

# ── Конфигурация ──────────────────────────────────────────
MAX_THICKNESS = 15
THICKNESS_SCAN_RADIUS = 30
THICKNESS_SAMPLES = 20
TEXT_PADDING = 3
TEXT_MIN_AREA = 5
TEXT_MAX_AREA = 30000
SKEL_SEARCH_RADIUS = 15
BINARIZE_THRESHOLD = 200


def _find_skeleton_label(row, col, labels, radius=SKEL_SEARCH_RADIUS):
    r, c = int(round(row)), int(round(col))
    H, W = labels.shape
    for d in range(radius + 1):
        for dr in range(-d, d + 1):
            for dc in range(-d, d + 1):
                if abs(dr) != d and abs(dc) != d:
                    continue
                rr, cc = r + dr, c + dc
                if 0 <= rr < H and 0 <= cc < W and labels[rr, cc] > 0:
                    return labels[rr, cc], d
    return 0, -1


def _measure_thickness(binary, row, col, angle, scan_radius=THICKNESS_SCAN_RADIUS):
    H, W = binary.shape
    perp = angle + math.pi / 2
    dx, dy = math.cos(perp), math.sin(perp)
    count = 0
    for step in range(0, scan_radius):
        rr, cc = int(round(row + dy * step)), int(round(col + dx * step))
        if 0 <= rr < H and 0 <= cc < W and binary[rr, cc] > 0:
            count += 1
        else:
            break
    for step in range(1, scan_radius):
        rr, cc = int(round(row - dy * step)), int(round(col - dx * step))
        if 0 <= rr < H and 0 <= cc < W and binary[rr, cc] > 0:
            count += 1
        else:
            break
    return count


def _load_with_fallback(base: Path, primary: str, fallback: str, grayscale=False):
    """Загрузить файл, при отсутствии — fallback."""
    flag = cv2.IMREAD_GRAYSCALE if grayscale else cv2.IMREAD_UNCHANGED
    for p in [primary, fallback]:
        path = base / p
        if path.exists():
            img = cv2.imread(str(path), flag)
            if img is not None:
                logger.info("Loaded %s", path)
                return img
    raise FileNotFoundError(f"Neither {primary} nor {fallback} found in {base}")


def run_clean(diagram_dir: Path, ocr_dir: Path):
    """
    Очистка P&ID: удаление труб и узлов, сохранение чистого изображения.

    Args:
        diagram_dir: корневая папка диаграммы (содержит original/, segmentation/, ...)
        ocr_dir: папка для выходных файлов OCR
    """
    ocr_dir.mkdir(parents=True, exist_ok=True)

    # ── Загрузка данных ───────────────────────────────────
    orig_gray = cv2.imread(str(diagram_dir / "original" / "image.png"), cv2.IMREAD_GRAYSCALE)
    orig_color = cv2.imread(str(diagram_dir / "original" / "image.png"))
    if orig_gray is None:
        raise FileNotFoundError(f"Original image not found: {diagram_dir / 'original' / 'image.png'}")

    orig_pil = Image.open(str(diagram_dir / "original" / "image.png"))
    dpi = orig_pil.info.get("dpi", (300, 300))
    H, W = orig_gray.shape

    _, binary = cv2.threshold(orig_gray, BINARIZE_THRESHOLD, 255, cv2.THRESH_BINARY_INV)

    # Скелет (с fallback)
    try:
        sk = _load_with_fallback(diagram_dir, "skeleton/skeleton_final.png",
                                 "skeleton/skeleton.png", grayscale=True)
        sk_bin = (sk > 0).astype(np.uint8)
    except FileNotFoundError:
        logger.warning("Skeleton not found, using empty skeleton")
        sk_bin = np.zeros((H, W), dtype=np.uint8)

    # Pipe mask (с fallback)
    try:
        pm = _load_with_fallback(diagram_dir, "segmentation/pipe_mask_validated.png",
                                 "segmentation/pipe_mask.png")
        pm_bin = (pm[:, :, 0] > 0).astype(np.uint8) if pm.ndim == 3 else (pm > 0).astype(np.uint8)
    except FileNotFoundError:
        logger.warning("Pipe mask not found, using empty mask")
        pm_bin = np.zeros((H, W), dtype=np.uint8)

    # Node mask
    try:
        nm = cv2.imread(str(diagram_dir / "segmentation" / "node_mask.png"), cv2.IMREAD_GRAYSCALE)
        nm_bin = (nm > 0).astype(np.uint8) if nm is not None else np.zeros((H, W), dtype=np.uint8)
    except Exception:
        nm_bin = np.zeros((H, W), dtype=np.uint8)

    # Graph (для толщины скелета)
    graph = None
    for gp in ["graph/graph_validated.json", "graph/graph.json"]:
        gpath = diagram_dir / gp
        if gpath.exists():
            with open(gpath) as f:
                graph = json.load(f)
            logger.info("Loaded graph: %s", gpath)
            break

    logger.info("Image: %dx%d, DPI: %s", W, H, dpi)

    # ── Этап 1: Толщина скелета ───────────────────────────
    n_sk, labels_sk, stats_sk, _ = cv2.connectedComponentsWithStats(sk_bin, connectivity=8)
    skel_thickness = {}

    if graph and n_sk > 1:
        skel_to_edges = defaultdict(set)
        edge_angles = {}
        for link in graph.get("links", []):
            eid = link["id"]
            sp, tp = link["source_point"], link["target_point"]
            edge_angles[eid] = math.atan2(tp[0] - sp[0], tp[1] - sp[1])
            for pt in [sp, tp]:
                label, _ = _find_skeleton_label(pt[0], pt[1], labels_sk)
                if label > 0:
                    skel_to_edges[label].add(eid)

        for skel_label in range(1, n_sk):
            ys, xs = np.where(labels_sk == skel_label)
            if len(ys) < 3:
                skel_thickness[skel_label] = 3
                continue
            edges = skel_to_edges.get(skel_label, set())
            angle = edge_angles[list(edges)[0]] if edges else (
                math.pi / 2 if (ys.max() - ys.min()) > (xs.max() - xs.min()) else 0
            )
            n_samples = min(THICKNESS_SAMPLES, len(ys))
            indices = np.linspace(0, len(ys) - 1, n_samples, dtype=int)
            thicknesses = [_measure_thickness(binary, int(ys[i]), int(xs[i]), angle) for i in indices]
            thicknesses = [t for t in thicknesses if t > 0]
            med = int(np.median(thicknesses)) if thicknesses else 3
            skel_thickness[skel_label] = min(max(3, med), MAX_THICKNESS)
    else:
        for sl in range(1, n_sk):
            skel_thickness[sl] = 5  # дефолт

    # ── Этап 2: Реконструкция маски труб ──────────────────
    reconstructed = np.zeros((H, W), dtype=np.uint8)
    for skel_label in range(1, n_sk):
        thickness = skel_thickness.get(skel_label, 5)
        radius = max(1, thickness // 2)
        comp = (labels_sk == skel_label).astype(np.uint8)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
        reconstructed = cv2.bitwise_or(reconstructed, cv2.dilate(comp, kernel))

    combined_pipe = cv2.bitwise_or(reconstructed, pm_bin)

    # ── Этап 3: Маска текста (для информации, не блокирует удаление) ─
    binary_clean = binary.copy()
    binary_clean[combined_pipe > 0] = 0
    binary_clean[nm_bin > 0] = 0

    # ── Этап 4: Финальная маска удаления ──────────────────
    final_removal = cv2.bitwise_or(combined_pipe, nm_bin)

    # ── Этап 5: Очистка + сохранение ─────────────────────
    # Цветная версия
    cleaned_color = orig_color.copy()
    cleaned_color[final_removal > 0] = (255, 255, 255)
    cleaned_rgb = cv2.cvtColor(cleaned_color, cv2.COLOR_BGR2RGB)
    Image.fromarray(cleaned_rgb).save(str(ocr_dir / "cleaned_for_ocr.png"), dpi=dpi)

    # Серая версия
    cleaned_gray = orig_gray.copy()
    cleaned_gray[final_removal > 0] = 255
    Image.fromarray(cleaned_gray).save(str(ocr_dir / "cleaned_for_ocr_gray.png"), dpi=dpi)

    logger.info("Cleaned images saved to %s", ocr_dir)

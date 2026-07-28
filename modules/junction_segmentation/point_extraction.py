"""Извлечение центров перекрёстков/мостов из бинарной маски.

Вынесено из prepare_dataset.py, чтобы код был доступен UI: тот модуль на
верхнем уровне тянет tqdm, которого нет в requirements/ui.txt. Здесь только
cv2 + numpy. prepare_dataset импортирует функции отсюда — единственная
реализация, поведение обучения не меняется.

Классификация компоненты:
  SINGLE — контур ровно из 4 углов, bbox ~квадратный и ~полностью залит
           → одна точка в центроиде (работает для маркера ЛЮБОГО размера);
  MERGED — всё остальное (слипшиеся L/T/Z-образные кляксы)
           → жадное разбиение окнами square_size × square_size.
"""

from typing import Dict, List, Tuple

import cv2
import numpy as np


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
    4-corner ~square marker; else split as merged_square_size squares.

    ВАЖНО для UI: merged_square_size обязан быть ПОСЛЕДНИМ ПРИМЕНЁННЫМ
    размером квадрата, а не дефолтной 15. В квадратах, ужатых до <15, окно 15
    не находится (fill 0.9) → фолбэк на центроид → слипшаяся пара снова даёт
    один центр.
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
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
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
        is_fully_filled = (area / max(1, w * h)) >= 1.0 - fill_tol

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

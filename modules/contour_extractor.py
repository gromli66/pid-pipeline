"""
Contour Extractor for P&ID Nodes
Извлекает контуры символов из изображения для узлов без скинов.
Интегрируется с graph_to_fxml.py — обогащает граф полем segmentation.

Использование:
    from contour_extractor import enrich_graph_with_contours
    enrich_graph_with_contours(graph_data, image_path, pipe_mask_path)
"""

import cv2
import numpy as np
from pathlib import Path
from typing import Optional


# Единственный хардкод — отступ вокруг bbox при кадрировании
PAD = 20

# Категории для которых НЕ нужно извлекать контуры
# (уже имеют скин или не должны рендериться)
SKIP_CONTOUR = {
    'background', 'truba', 'annotation', 'strelka', 'connector',
}

# Классы которые рисуются линиями (skeleton → Line).
# Все остальные без скина → заливка (Polygon).
STROKE_CLASSES = {'drossel', 'voronka'}


class ContourExtractor:
    """
    Извлекает контуры символов из P&ID изображения.
    Все параметры вычисляются автоматически из данных.
    """

    def __init__(self, image_gray: np.ndarray, pipe_mask: np.ndarray):
        self.image = image_gray
        self.pipe_mask = pipe_mask
        self.H, self.W = image_gray.shape
        self._fill_k = 7  # default; override via set_fill_k()

        # Auto-params из данных
        dt = cv2.distanceTransform(pipe_mask, cv2.DIST_L2, 5)
        pipe_pixels = pipe_mask > 128
        self.pipe_thickness = float(np.median(dt[pipe_pixels]) * 2) if np.any(pipe_pixels) else 6.0
        self.close_k = max(2, int(self.pipe_thickness / 3))

    def _compute_fill_k(self, bboxes: list) -> int:
        """Вычисляет fill_holes kernel из статистики bbox."""
        if not bboxes:
            return 7
        min_dims = [min(b[2], b[3]) for b in bboxes]
        med = float(np.median(min_dims))
        return max(3, round(med * 0.15 + 0.5))

    def _get_merged_mask(self, bbox: list):
        """
        Common step: crop → threshold → subtract pipes → select components.
        Returns (merged_mask, x1_offset, y1_offset) or (None, None, None).
        """
        x, y, w, h = [int(v) for v in bbox]

        x1 = max(0, x - PAD)
        y1 = max(0, y - PAD)
        x2 = min(self.W, x + w + PAD)
        y2 = min(self.H, y + h + PAD)

        crop = self.image[y1:y2, x1:x2].copy()
        crop_pipe = self.pipe_mask[y1:y2, x1:x2].copy()

        _, bin_crop = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
        symbol_only = cv2.bitwise_and(bin_crop, cv2.bitwise_not(crop_pipe))

        kernel = np.ones((self.close_k, self.close_k), np.uint8)
        symbol_clean = cv2.morphologyEx(symbol_only, cv2.MORPH_CLOSE, kernel)

        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
            symbol_clean, connectivity=8
        )

        bx_l, by_l = x - x1, y - y1
        margin = max(2, min(int(min(w, h) * 0.08), 5))
        min_area = max(10, int(w * h * 0.005))

        selected = []
        for i in range(1, num_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            if area < min_area:
                continue
            cx_c, cy_c = centroids[i]
            if (cx_c >= bx_l - margin and cx_c <= bx_l + w + margin and
                    cy_c >= by_l - margin and cy_c <= by_l + h + margin):
                selected.append(i)

        if not selected:
            return None, None, None

        merged = np.zeros_like(symbol_clean)
        for i in selected:
            merged[labels == i] = 255

        return merged, x1, y1

    def extract(self, bbox: list) -> Optional[dict]:
        """
        Извлекает контуры символа (для fill-режима).
        Returns dict с 'contours' и 'segmentation', или None.
        """
        merged, x1, y1 = self._get_merged_mask(bbox)
        if merged is None:
            return None

        # Fill interior holes
        merged = self._fill_holes(merged)

        contours, _ = cv2.findContours(
            merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_TC89_L1
        )
        if not contours:
            return None

        global_contours = []
        for cnt in contours:
            pts = [[int(pt[0][0] + x1), int(pt[0][1] + y1)] for pt in cnt]
            if len(pts) >= 3:
                global_contours.append(pts)

        if not global_contours:
            return None

        largest = max(global_contours, key=len)
        flat_seg = []
        for px, py in largest:
            flat_seg.extend([float(px), float(py)])

        return {
            'contours': global_contours,
            'segmentation': flat_seg,
        }

    def extract_lines(self, bbox: list) -> Optional[dict]:
        """
        Извлекает линии символа через топологию скелета (для stroke-режима).

        Суть: skeleton → endpoints (кончики) + junctions (пересечения)
        → каждый endpoint соединяется прямой с ближайшим junction.
        Нет junction → endpoints соединяются через центроид компонента.

        Дроссель >< = два V-компонента, каждый: endpoints → junction/centroid.
        Воронка Y = 3 endpoints → 1 junction = 3 линии.

        Никакого хардкода форм — топология сама определяет геометрию.

        Returns dict с 'lines': [[x1,y1,x2,y2], ...] в глобальных координатах,
        или None.
        """
        from skimage.morphology import skeletonize as sk_thin

        merged, x1, y1 = self._get_merged_mask(bbox)
        if merged is None:
            return None

        # Skeletonize → 1px lines
        skel = (sk_thin(merged > 0) * 255).astype(np.uint8)
        skel_bin = (skel > 0).astype(np.uint8)

        # Endpoints: skeleton pixels with exactly 1 neighbor
        kernel = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], dtype=np.uint8)
        neighbors = cv2.filter2D(skel_bin, -1, kernel) * skel_bin
        ep_all = [(int(xx), int(yy))
                  for yy, xx in zip(*np.where(neighbors == 1))]

        # Connected components of skeleton
        nl, lb, _, _ = cv2.connectedComponentsWithStats(skel_bin, 8)
        num_components = nl - 1

        global_lines = []
        for comp_id in range(1, nl):
            comp_mask = (lb == comp_id)
            comp_eps = [p for p in ep_all if comp_mask[p[1], p[0]]]
            if len(comp_eps) < 2:
                continue

            # Centroid of skeleton component
            ys, xs = np.where(comp_mask)
            cx, cy = int(np.mean(xs)), int(np.mean(ys))

            if num_components >= 2:
                # Multi-component (e.g. drossel ><):
                # each component is a V-shape → 2 farthest endpoints
                comp_eps.sort(
                    key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2,
                    reverse=True
                )
                tips = comp_eps[:2]
            else:
                # Single component (e.g. voronka Y):
                # all endpoints are real branches
                tips = comp_eps

            for ep in tips:
                global_lines.append([
                    ep[0] + x1, ep[1] + y1,
                    cx + x1, cy + y1
                ])

        if not global_lines:
            return None

        return {
            'lines': global_lines,
        }

    def _fill_holes(self, mask: np.ndarray) -> np.ndarray:
        """Fill interior holes per connected component (prevents merging neighbors)."""
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (self._fill_k, self._fill_k))
        nl, lb, _, _ = cv2.connectedComponentsWithStats(mask, 8)
        result = np.zeros_like(mask)

        for i in range(1, nl):
            comp = (lb == i).astype(np.uint8) * 255
            dilated = cv2.dilate(comp, k)
            inv = cv2.bitwise_not(dilated)
            mff = np.zeros((inv.shape[0] + 2, inv.shape[1] + 2), np.uint8)
            flood = inv.copy()
            cv2.floodFill(flood, mff, (0, 0), 128)
            interior = (flood == 255).astype(np.uint8) * 255
            solid = cv2.erode(cv2.bitwise_or(dilated, interior), k)
            result = cv2.bitwise_or(result, solid)

        return result

    def set_fill_k(self, bboxes: list):
        """Установить fill_k из статистики bbox. Вызвать перед extract()."""
        self._fill_k = self._compute_fill_k(bboxes)


def enrich_graph_with_contours(
    graph_data: dict,
    image_path: str,
    pipe_mask_path: str,
    skin_mapped_classes: set = None,
) -> dict:
    """
    Обогащает граф контурами для узлов без скинов.

    Для каждого equipment-узла без скина и без segmentation:
    1. Извлекает контур из изображения
    2. Записывает segmentation (flat) и contour_render_mode в узел

    Args:
        graph_data: граф (модифицируется in-place)
        image_path: путь к оригинальному изображению
        pipe_mask_path: путь к маске труб
        skin_mapped_classes: set имён классов которые УЖЕ имеют скин

    Returns:
        graph_data (тот же объект, модифицированный)
    """
    img_gray = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    pipe_mask = cv2.imread(str(pipe_mask_path), cv2.IMREAD_GRAYSCALE)

    if img_gray is None or pipe_mask is None:
        print(f"WARNING: Cannot load image/mask, skipping contour extraction")
        return graph_data

    extractor = ContourExtractor(img_gray, pipe_mask)

    # Collect all bboxes for auto fill_k
    all_bboxes = []
    target_nodes = []

    for node in graph_data.get('nodes', []):
        if node.get('type') != 'equipment':
            continue
        class_name = node.get('class_name', '')
        if class_name in SKIP_CONTOUR:
            continue
        if skin_mapped_classes and class_name in skin_mapped_classes:
            continue

        bbox = node.get('bbox')
        if not bbox or len(bbox) != 4:
            continue

        # Convert [x1,y1,x2,y2] → [x,y,w,h]
        x1, y1, x2, y2 = bbox
        coco_bbox = [x1, y1, x2 - x1, y2 - y1]
        all_bboxes.append(coco_bbox)
        target_nodes.append((node, coco_bbox))

    if not target_nodes:
        return graph_data

    # Set fill_k from statistics
    extractor.set_fill_k(all_bboxes)

    enriched = 0
    for node, coco_bbox in target_nodes:
        result = extractor.extract(coco_bbox)
        if result:
            node['segmentation'] = result['segmentation']
            node['contours_all'] = result['contours']
            enriched += 1

    print(f"  Contour extraction: {enriched}/{len(target_nodes)} nodes enriched")
    return graph_data

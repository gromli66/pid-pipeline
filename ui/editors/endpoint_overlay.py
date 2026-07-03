"""
Live-эндпоинты маски труб для PolylineMaskEditor (вкладка «Вал. pipe»).

Маркер (красный кружок) = РЕАЛЬНЫЙ разрыв цепи бокс_узла → бокс_узла.
Эндпоинт НЕ показываем, если конец трубы у края кадра, в боксе узла, или это
перекрёсток (degree >= 3).

Учитываются и ещё НЕ вмёрженные в маску полилинии (extra_strokes) — поэтому
точки исчезают сразу после соединения, до бейка маски.
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

try:
    from skimage.morphology import skeletonize, remove_small_objects
    from scipy.ndimage import convolve
    _SKIMAGE_OK = True
except Exception as _imp_exc:  # pragma: no cover
    _SKIMAGE_OK = False
    logger.warning(
        "Эндпоинты труб отключены: не удалось импортировать skimage/scipy (%s). "
        "Установите scikit-image (см. requirements/ui.txt).", _imp_exc
    )

try:
    import cv2
    _CV2_OK = True
except Exception:  # pragma: no cover
    _CV2_OK = False


ENDPOINT_MARGIN_PX = 24
ENDPOINT_DEDUP_PX = 6
ENDPOINT_SPUR_PRUNE_PX = 5
ENDPOINT_MARKER_PX = 9
NODE_TOUCH_PX = 6
IGNORE_IMAGE_BORDER = True
# Сшивать зазоры до ~этого радиуса (px) ПЕРЕД скелетизацией: конец трубы,
# к которому линия подходит близко (±ENDPOINT_GAP_CLOSE_PX), перестаёт быть
# degree-1 концом → маркер исчезает, даже если нет пиксель-в-пиксель стыка.
ENDPOINT_GAP_CLOSE_PX = 6

PIPE_CATEGORIES = ("truba",)
IGNORE_CATEGORIES = ("annotation",)

_NEIGH = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], dtype=np.uint8)


# ===================== ЧИСТОЕ ЯДРО (без Qt) =====================

def _close_gaps(b, px):
    """Морфологическое closing: сшить разрывы шириной до ~px между линиями."""
    k = int(px) * 2 + 1
    if _CV2_OK:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        closed = cv2.morphologyEx(b.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
        return closed > 0
    try:
        from scipy.ndimage import binary_closing
        return binary_closing(b, structure=np.ones((k, k), dtype=bool))
    except Exception:
        return b

def _skeleton_and_neigh(binary, spur_px, close_px=ENDPOINT_GAP_CLOSE_PX):
    if not _SKIMAGE_OK:
        return None, None
    b = binary > 0
    if not b.any():
        return None, None
    # Сшиваем мелкие зазоры, чтобы близко проходящая линия смыкала конец трубы.
    if close_px and close_px > 0:
        b = _close_gaps(b, close_px)
        if not b.any():
            return None, None
    skel = skeletonize(b)
    if spur_px and spur_px > 0:
        skel = remove_small_objects(skel, min_size=int(spur_px), connectivity=2)
    if not skel.any():
        return None, None
    sk = skel.astype(np.uint8)
    neigh = convolve(sk, _NEIGH, mode="constant", cval=0).astype(np.int16)
    return sk, neigh


def _is_spur(sk, neigh, x, y, max_len):
    h, w = sk.shape
    prev = None
    cur = (x, y)
    for step in range(max_len + 1):
        cx, cy = cur
        if step > 0 and neigh[cy, cx] >= 3:
            return True
        nxts = []
        for ny in (cy - 1, cy, cy + 1):
            for nx in (cx - 1, cx, cx + 1):
                if (nx, ny) == (cx, cy):
                    continue
                if 0 <= nx < w and 0 <= ny < h and sk[ny, nx] and (nx, ny) != prev:
                    nxts.append((nx, ny))
        if len(nxts) != 1:
            return False
        prev = cur
        cur = nxts[0]
    return False


def _dedup(pts, r):
    out = []
    r2 = r * r
    for p in pts:
        if all((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 > r2 for q in out):
            out.append(p)
    return out


def compute_endpoints(binary, spur_px=ENDPOINT_SPUR_PRUNE_PX, dedup_px=ENDPOINT_DEDUP_PX,
                      close_px=ENDPOINT_GAP_CLOSE_PX):
    """binary HxW (>0 = труба) -> список (x,y) концов скелета (degree==1, без усов/спеклов).

    close_px: радиус сшивки зазоров перед скелетизацией — конец, к которому линия
    подходит ближе ~close_px, перестаёт считаться разрывом.
    """
    sk, neigh = _skeleton_and_neigh(binary, spur_px, close_px)
    if sk is None:
        return []
    ep = (sk == 1) & (neigh == 1)
    ys, xs = np.where(ep)
    pts = []
    for x, y in zip(xs.tolist(), ys.tolist()):
        if spur_px and spur_px > 0 and _is_spur(sk, neigh, x, y, spur_px):
            continue
        pts.append((x, y))
    if dedup_px and dedup_px > 1:
        pts = _dedup(pts, dedup_px)
    return pts


def _fill_annotation(region, ann):
    seg = ann.get("segmentation")
    if seg and _CV2_OK:
        drawn = False
        for poly in (seg if isinstance(seg, list) else []):
            if isinstance(poly, (list, tuple)) and len(poly) >= 6:
                pts = np.array(poly, dtype=np.float32).reshape(-1, 2).round().astype(np.int32)
                cv2.fillPoly(region, [pts], 1)
                drawn = True
        if drawn:
            return
    bbox = ann.get("bbox")
    if bbox:
        x, y, w, h = bbox
        h_img, w_img = region.shape
        x0 = max(0, int(round(x))); y0 = max(0, int(round(y)))
        x1 = min(w_img, int(round(x + w))); y1 = min(h_img, int(round(y + h)))
        if x1 > x0 and y1 > y0:
            region[y0:y1, x0:x1] = 1


def build_node_region(coco_full_data, img_w, img_h, dilate_px=NODE_TOUCH_PX,
                      pipe_categories=PIPE_CATEGORIES, ignore_categories=IGNORE_CATEGORIES):
    if not coco_full_data:
        return None
    cats = {c.get("id"): str(c.get("name", "")).lower()
            for c in coco_full_data.get("categories", [])}
    if not cats:
        return None
    excl = {n.lower() for n in pipe_categories} | {n.lower() for n in ignore_categories}
    region = np.zeros((img_h, img_w), dtype=np.uint8)
    for ann in coco_full_data.get("annotations", []):
        if cats.get(ann.get("category_id"), "") in excl:
            continue
        _fill_annotation(region, ann)
    if dilate_px and dilate_px > 0 and _CV2_OK and region.any():
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (2 * dilate_px + 1, 2 * dilate_px + 1))
        region = cv2.dilate(region, k)
    return region


# ===================== Qt-ОБЁРТКА =====================

def _qimage_region_to_binary(mask_qimage, x, y, w, h):
    from PySide6.QtGui import QImage
    region = mask_qimage.copy(x, y, w, h).convertToFormat(QImage.Format.Format_ARGB32)
    ww, hh = region.width(), region.height()
    ptr = region.bits()
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape((hh, ww, 4))
    return (arr[:, :, 3] > 0).astype(np.uint8)


def _rasterize_strokes(strokes, cx1, cy1, cw, ch):
    """Растеризует ещё не вмёрженные полилинии [(QPainterPath, width)] в бинарь кропа."""
    from PySide6.QtGui import QImage, QPainter, QPen, QColor
    from PySide6.QtCore import Qt, QRectF
    crop_rect = QRectF(cx1, cy1, cw, ch)
    relevant = [(p, w) for (p, w) in strokes if p.boundingRect().intersects(crop_rect)]
    if not relevant:
        return None
    img = QImage(cw, ch, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    painter.translate(-cx1, -cy1)
    for path, width in relevant:
        pen = QPen(QColor(255, 255, 255, 255), max(1, int(width)))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
    painter.end()
    ptr = img.bits()
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape((ch, cw, 4))
    return (arr[:, :, 3] > 0).astype(np.uint8)


class EndpointOverlay:
    def __init__(self, scene, img_w, img_h, marker_px=ENDPOINT_MARKER_PX,
                 color=(255, 0, 0), zvalue=100):
        from PySide6.QtGui import QColor
        self.scene = scene
        self.w = img_w
        self.h = img_h
        self.marker_px = marker_px
        self.color = QColor(*color)
        self.zvalue = zvalue
        self.visible = True
        self.items = {}
        self.node_region = None

    def set_node_region(self, region):
        self.node_region = region

    def add_node_box(self, x, y, w, h, touch=NODE_TOUCH_PX):
        if self.node_region is None:
            self.node_region = np.zeros((self.h, self.w), dtype=np.uint8)
        x0 = max(0, int(x - touch)); y0 = max(0, int(y - touch))
        x1 = min(self.w, int(x + w + touch)); y1 = min(self.h, int(y + h + touch))
        if x1 > x0 and y1 > y0:
            self.node_region[y0:y1, x0:x1] = 1

    def set_visible(self, b):
        self.visible = b
        for it in self.items.values():
            it.setVisible(b)

    def clear(self):
        for it in self.items.values():
            self.scene.removeItem(it)
        self.items.clear()

    def count(self):
        return len(self.items)

    def _add_marker(self, x, y):
        from PySide6.QtWidgets import QGraphicsEllipseItem
        from PySide6.QtGui import QPen, QBrush, QColor
        r = self.marker_px / 2.0
        it = QGraphicsEllipseItem(-r, -r, 2 * r, 2 * r)
        pen = QPen(self.color)
        pen.setWidthF(1.6)
        pen.setCosmetic(True)
        it.setPen(pen)
        it.setBrush(QBrush(QColor(255, 0, 0, 90)))
        it.setZValue(self.zvalue)
        it.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        it.setPos(x + 0.5, y + 0.5)
        it.setVisible(self.visible)
        self.scene.addItem(it)
        self.items[(x, y)] = it

    def _remove_marker(self, key):
        it = self.items.pop(key, None)
        if it is not None:
            self.scene.removeItem(it)

    def _filtered(self, gx, gy):
        if self.node_region is not None and self.node_region[gy, gx] > 0:
            return True
        if IGNORE_IMAGE_BORDER:
            m = ENDPOINT_MARGIN_PX
            if gx < m or gy < m or gx >= self.w - m or gy >= self.h - m:
                return True
        return False

    def refresh(self, mask_qimage, rect=None, extra_strokes=None):
        if mask_qimage is None:
            return
        if rect is None:
            ix1, iy1, ix2, iy2 = 0, 0, self.w, self.h
        else:
            ix1 = max(0, int(rect[0])); iy1 = max(0, int(rect[1]))
            ix2 = min(self.w, int(rect[2])); iy2 = min(self.h, int(rect[3]))
        if ix2 <= ix1 or iy2 <= iy1:
            return
        m = ENDPOINT_MARGIN_PX
        cx1 = max(0, ix1 - m); cy1 = max(0, iy1 - m)
        cx2 = min(self.w, ix2 + m); cy2 = min(self.h, iy2 + m)
        cw = cx2 - cx1; ch = cy2 - cy1
        crop = _qimage_region_to_binary(mask_qimage, cx1, cy1, cw, ch)
        if extra_strokes:
            stroke_bin = _rasterize_strokes(extra_strokes, cx1, cy1, cw, ch)
            if stroke_bin is not None:
                crop = np.maximum(crop, stroke_bin)
        local = compute_endpoints(crop)
        new_in_zone = set()
        for (lx, ly) in local:
            gx = cx1 + lx
            gy = cy1 + ly
            if not (ix1 <= gx < ix2 and iy1 <= gy < iy2):
                continue
            if self._filtered(gx, gy):
                continue
            new_in_zone.add((gx, gy))
        for key in [k for k in self.items if ix1 <= k[0] < ix2 and iy1 <= k[1] < iy2]:
            if key not in new_in_zone:
                self._remove_marker(key)
        for key in new_in_zone:
            if key not in self.items:
                self._add_marker(*key)

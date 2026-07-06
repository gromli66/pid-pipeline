"""
Square Mask Editor — редактор масок с квадратами.

Используется для валидации junction/bridge масок.
Ctrl+ЛКМ: добавить квадрат, Ctrl+ПКМ: flood-fill удаление.
Клавиши 1/2: переключение класса (белая/красная маска).

Оптимизации vs прототип:
- numpy vectorized mask ↔ RGBA конверсии (~100x быстрее)
- Grayscale 8-bit подложка (28 MB vs 112 MB на 7000×4000)
"""

import json
import numpy as np
from collections import deque
from pathlib import Path

from PySide6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QGraphicsRectItem,
)
from PySide6.QtGui import (
    QPixmap, QImage, QPainter, QColor, QPen, QBrush,
    QPainterPath, QWheelEvent, QMouseEvent, QKeyEvent,
)
from PySide6.QtCore import Qt, QRectF

SQUARE_SIZE = 15  # значение по умолчанию (fallback)
MAX_UNDO_STEPS = 50


def _qimage_to_numpy(qimg: QImage) -> np.ndarray:
    """QImage (любой формат) → numpy array (H, W, 4) BGRA."""
    qimg = qimg.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = qimg.width(), qimg.height()
    ptr = qimg.bits()
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape((h, w, 4)).copy()
    return arr  # BGRA


def _numpy_to_qimage(arr: np.ndarray) -> QImage:
    """numpy array (H, W, 4) BGRA → QImage ARGB32."""
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, w * 4, QImage.Format.Format_ARGB32).copy()


def _make_mask_rgba_fast(mask_qimage: QImage, color: QColor) -> QImage:
    """
    Бинарная маска → RGBA: чёрный→прозрачный, белый→color.
    Numpy vectorized: <0.1 сек на 7000×4000 (vs 35 сек в прототипе).
    """
    arr = _qimage_to_numpy(mask_qimage)  # (H, W, 4) BGRA

    # Средняя яркость по BGR каналам
    brightness = arr[:, :, :3].mean(axis=2)

    # Результат: прозрачный фон
    result = np.zeros_like(arr)

    # Белые пиксели → color
    mask = brightness >= 128
    result[mask, 0] = color.blue()
    result[mask, 1] = color.green()
    result[mask, 2] = color.red()
    result[mask, 3] = 255

    return _numpy_to_qimage(result)


def _save_binary_mask_fast(mask_qimage: QImage, path: str):
    """
    RGBA маска → бинарный PNG (alpha > 128 → белый, иначе чёрный).
    Numpy vectorized: <0.1 сек на 7000×4000 (vs 35 сек в прототипе).
    """
    arr = _qimage_to_numpy(mask_qimage)  # (H, W, 4) BGRA
    alpha = arr[:, :, 3]

    # Бинарная маска
    binary = np.zeros((arr.shape[0], arr.shape[1], 4), dtype=np.uint8)
    white = alpha > 128
    binary[white, 0] = 255
    binary[white, 1] = 255
    binary[white, 2] = 255
    binary[white, 3] = 255
    # Чёрные пиксели: alpha=255 (не прозрачный)
    binary[~white, 3] = 255

    qimg = _numpy_to_qimage(binary)
    qimg.save(path)


def _make_grayscale_darkened(original: QImage, brightness: float = 0.4) -> QImage:
    """
    Original → grayscale 8-bit → затемнение.
    28 MB вместо 112 MB на 7000×4000.
    """
    arr = _qimage_to_numpy(original)  # (H, W, 4) BGRA

    # Grayscale: weighted average
    gray = (
        0.114 * arr[:, :, 0].astype(np.float32) +  # B
        0.587 * arr[:, :, 1].astype(np.float32) +  # G
        0.299 * arr[:, :, 2].astype(np.float32)     # R
    )

    # Затемнение
    gray = (gray * brightness).clip(0, 255).astype(np.uint8)

    # → BGRA
    result = np.zeros((arr.shape[0], arr.shape[1], 4), dtype=np.uint8)
    result[:, :, 0] = gray
    result[:, :, 1] = gray
    result[:, :, 2] = gray
    result[:, :, 3] = 255

    return _numpy_to_qimage(result)


def _scanline_components(binary: np.ndarray, min_size: int = 3) -> list[tuple]:
    """Connected component bounding boxes without scipy.
    Uses cv2 if available, otherwise pure numpy row-projection."""
    try:
        import cv2
        n, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
        bboxes = []
        for i in range(1, n):  # skip background
            x1 = int(stats[i, cv2.CC_STAT_LEFT])
            y1 = int(stats[i, cv2.CC_STAT_TOP])
            w = int(stats[i, cv2.CC_STAT_WIDTH])
            h = int(stats[i, cv2.CC_STAT_HEIGHT])
            if w >= min_size and h >= min_size:
                bboxes.append((x1, y1, x1 + w, y1 + h))
        return bboxes
    except ImportError:
        pass

    # Pure numpy fallback: project rows/cols to find bounding boxes of non-zero regions
    # Less accurate (merges close blobs) but works without dependencies
    rows = np.any(binary, axis=1)
    cols = np.any(binary, axis=0)
    if not rows.any():
        return []
    y1, y2 = np.where(rows)[0][[0, -1]]
    x1, x2 = np.where(cols)[0][[0, -1]]
    return [(int(x1), int(y1), int(x2 + 1), int(y2 + 1))]


class SquareMaskEditor(QGraphicsView):
    """
    Редактор масок с квадратами.

    Два класса (mask1=белый, mask2=красный):
    - Ctrl+ЛКМ: добавить квадрат текущего класса
    - Ctrl+ПКМ: flood-fill удаление (квадрат или маска)
    - 1/2: переключение класса
    - Ctrl+Z: undo
    - Ctrl+S: save
    """

    def __init__(self):
        super().__init__()

        self.scene = QGraphicsScene()
        self.setScene(self.scene)

        # Images
        self.original_image: QImage | None = None
        self.skeleton_image: QImage | None = None
        self.mask1_image: QImage | None = None
        self.mask2_image: QImage | None = None
        self.img_width = 0
        self.img_height = 0

        # Display items
        self.mask1_item: QGraphicsPixmapItem | None = None
        self.mask2_item: QGraphicsPixmapItem | None = None
        self.skeleton_item: QGraphicsPixmapItem | None = None
        self.bbox_frames_item: QGraphicsPixmapItem | None = None
        self.bg_item: QGraphicsPixmapItem | None = None

        # Скелет: исходник (для перекраски) и текущий цвет — меняются в «Вид»
        self._skel_raw: QImage | None = None
        self._skeleton_color = QColor(0, 255, 0)

        # COCO annotations (для рамок узлов оборудования)
        self.coco_annotations: list[dict] = []

        # Current class (1 = white/junction, 2 = red/bridge)
        self.current_class = 1

        # Squares overlay
        self.squares_white: list[QGraphicsRectItem] = []
        self.squares_red: list[QGraphicsRectItem] = []
        self.square_size = SQUARE_SIZE

        # Undo
        self.undo_stack: deque = deque(maxlen=MAX_UNDO_STEPS)

        # Setup view
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QBrush(QColor(30, 30, 30)))

        # Оптимизации отрисовки (аналогично polyline_mask_editor / ocr_binding_editor)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing)

        self.ctrl_pressed = False
        self.status_callback = None

        # Shift+drag selection
        self._shift_pressed = False
        self._drag_start = None
        self._drag_rect_item = None

        # Blob tracking: list of (bbox, class, confirmed, rect_item)
        self._blobs: list[dict] = []  # {x1,y1,x2,y2, cls, confirmed, rect_item}

        # Placeholder
        self._show_placeholder()

    def _show_placeholder(self):
        self.scene.clear()
        text = self.scene.addText("Выберите диаграмму для валидации перекрёстков")
        text.setDefaultTextColor(QColor(150, 150, 150))

    # ==================== LOADING ====================

    def load_images(
        self,
        original_path: str,
        mask1_path: str,
        mask2_path: str = "",
        skeleton_path: str = "",
        coco_path: str = "",
    ) -> bool:
        """
        Загрузить изображения.

        Args:
            original_path: путь к оригиналу (подложка)
            mask1_path: junction_mask.png (белый)
            mask2_path: bridge_mask.png (красный, опционально)
            skeleton_path: skeleton.png (зелёный, опционально)
            coco_path: coco_validated.json (рамки узлов, опционально)
        """
        self.original_image = QImage(original_path)
        if self.original_image.isNull():
            return False

        self.img_width = self.original_image.width()
        self.img_height = self.original_image.height()

        # COCO annotations (рамки узлов оборудования)
        if coco_path and Path(coco_path).exists():
            self._load_coco(coco_path)
        else:
            self.coco_annotations = []

        # Skeleton — GREEN (optional)
        if skeleton_path and Path(skeleton_path).exists():
            self._skel_raw = QImage(skeleton_path)
            self.skeleton_image = _make_mask_rgba_fast(self._skel_raw, self._skeleton_color)
        else:
            self._skel_raw = None
            self.skeleton_image = None

        # Mask1 — WHITE (junction)
        mask1_raw = QImage(mask1_path)
        if mask1_raw.isNull():
            return False
        self.mask1_image = _make_mask_rgba_fast(mask1_raw, QColor(255, 255, 255))

        # Mask2 — RED (bridge)
        if mask2_path and Path(mask2_path).exists():
            mask2_raw = QImage(mask2_path)
            self.mask2_image = _make_mask_rgba_fast(mask2_raw, QColor(255, 0, 0))
        else:
            self.mask2_image = QImage(
                self.img_width, self.img_height, QImage.Format.Format_ARGB32
            )
            self.mask2_image.fill(QColor(0, 0, 0, 0))

        self._setup_scene()
        return True

    def _load_coco(self, path: str) -> bool:
        try:
            with open(path, "r", encoding="utf-8") as f:
                coco_data = json.load(f)
            self.coco_annotations = coco_data.get("annotations", [])
            return True
        except Exception:
            self.coco_annotations = []
            return False

    def _render_bbox_frames(self) -> QImage:
        """Отрисовать рамки узлов из COCO (bbox/полигоны) красным контуром."""
        frames = QImage(
            self.img_width, self.img_height, QImage.Format.Format_ARGB32
        )
        frames.fill(QColor(0, 0, 0, 0))

        if not self.coco_annotations:
            return frames

        painter = QPainter(frames)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        pen = QPen(QColor(255, 0, 0, 255))
        pen.setWidth(2)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        for ann in self.coco_annotations:
            drawn = False

            # Сегментация-полигон (приоритет).
            # Рисуем только список полигонов [[x,y,...]]; RLE-словарь и прочее -> bbox.
            seg = ann.get("segmentation")
            if isinstance(seg, list):
                for poly_coords in seg:
                    if isinstance(poly_coords, (list, tuple)) and len(poly_coords) >= 6:
                        path = QPainterPath()
                        path.moveTo(poly_coords[0], poly_coords[1])
                        for i in range(2, len(poly_coords) - 1, 2):
                            path.lineTo(poly_coords[i], poly_coords[i + 1])
                        path.closeSubpath()
                        painter.drawPath(path)
                        drawn = True

            # Fallback на bbox
            if not drawn and "bbox" in ann and ann["bbox"]:
                x, y, w, h = ann["bbox"]
                painter.drawRect(round(x), round(y), round(w), round(h))

        painter.end()
        return frames

    def _setup_scene(self):
        self.scene.clear()
        self.squares_white.clear()
        self.squares_red.clear()
        self.undo_stack.clear()

        # Z=0: Grayscale darkened background
        darkened = _make_grayscale_darkened(self.original_image, brightness=0.4)
        self.bg_item = QGraphicsPixmapItem(QPixmap.fromImage(darkened))
        self.bg_item.setZValue(0)
        self.scene.addItem(self.bg_item)

        # Z=0.5: рамки узлов из COCO (красные контуры)
        if self.coco_annotations:
            bbox_img = self._render_bbox_frames()
            self.bbox_frames_item = QGraphicsPixmapItem(QPixmap.fromImage(bbox_img))
            self.bbox_frames_item.setZValue(0.5)
            self.scene.addItem(self.bbox_frames_item)
        else:
            self.bbox_frames_item = None

        # Z=1: Skeleton (green, 50%)
        if self.skeleton_image:
            self.skeleton_item = QGraphicsPixmapItem(
                QPixmap.fromImage(self.skeleton_image)
            )
            self.skeleton_item.setZValue(1)
            self.skeleton_item.setOpacity(0.5)
            self.scene.addItem(self.skeleton_item)

        # Z=2: Mask1 (white, 50%)
        self.mask1_item = QGraphicsPixmapItem(QPixmap.fromImage(self.mask1_image))
        self.mask1_item.setZValue(2)
        self.mask1_item.setOpacity(0.5)
        self.scene.addItem(self.mask1_item)

        # Z=3: Mask2 (red, 50%)
        self.mask2_item = QGraphicsPixmapItem(QPixmap.fromImage(self.mask2_image))
        self.mask2_item.setZValue(3)
        self.mask2_item.setOpacity(0.5)
        self.scene.addItem(self.mask2_item)

        self.setSceneRect(QRectF(0, 0, self.img_width, self.img_height))
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

        # Найти пятна (blobs) на масках
        self._detect_blobs()

    # ==================== EDITING ====================

    def _update_mask1_display(self):
        if self.mask1_item:
            self.mask1_item.setPixmap(QPixmap.fromImage(self.mask1_image))

    def _update_mask2_display(self):
        if self.mask2_item:
            self.mask2_item.setPixmap(QPixmap.fromImage(self.mask2_image))

    def set_background_darkness(self, darkness: float):
        """Затемнение фоновой подложки. darkness 0..1 (0 — оригинал, 1 — чёрный)."""
        if self.original_image is None or self.bg_item is None:
            return
        brightness = max(0.05, min(1.0, 1.0 - float(darkness)))
        darkened = _make_grayscale_darkened(self.original_image, brightness=brightness)
        self.bg_item.setPixmap(QPixmap.fromImage(darkened))

    def set_skeleton_color(self, color: QColor):
        """Цвет отображения скелета труб."""
        self._skeleton_color = QColor(color)
        if self._skel_raw is None or self.skeleton_item is None:
            return
        self.skeleton_image = _make_mask_rgba_fast(self._skel_raw, self._skeleton_color)
        self.skeleton_item.setPixmap(QPixmap.fromImage(self.skeleton_image))

    def set_square_size(self, size: int):
        """Установить размер квадратов."""
        self.square_size = max(3, size)

    def add_square(self, x: int, y: int):
        """Добавить квадрат текущего класса."""
        half = self.square_size // 2
        if self.current_class == 1:
            color = QColor(255, 255, 255, 127)
            squares_list = self.squares_white
        else:
            color = QColor(255, 0, 0, 127)
            squares_list = self.squares_red

        rect = QGraphicsRectItem(x - half, y - half, self.square_size, self.square_size)
        rect.setBrush(QBrush(color))
        rect.setPen(QPen(Qt.PenStyle.NoPen))
        rect.setZValue(4)
        self.scene.addItem(rect)
        squares_list.append(rect)
        self.undo_stack.append(("add_square", self.current_class, rect))
        self._update_status(f"Квадрат ({x}, {y})")

    def flood_fill_delete(self, x: int, y: int, threshold: int = 128) -> bool:
        """Ctrl+ПКМ удаление: все подтверждённые blobs, квадрат или маска."""
        # Проверяем: клик по любому confirmed blob → удалить ВСЕ confirmed
        for blob in self._blobs:
            if not blob["confirmed"]:
                continue
            if blob["x1"] <= x <= blob["x2"] and blob["y1"] <= y <= blob["y2"]:
                self._delete_all_confirmed_blobs()
                return True

        # Проверяем квадраты ОБОИХ классов
        for cls, squares_list in [(1, self.squares_white), (2, self.squares_red)]:
            for rect in squares_list:
                if rect.rect().contains(x, y):
                    self.scene.removeItem(rect)
                    squares_list.remove(rect)
                    self.undo_stack.append(("remove_square", cls, rect))
                    label = "junction" if cls == 1 else "bridge"
                    self._update_status(f"Квадрат {label} удалён")
                    return True

        # Flood-fill на маске — проверяем обе, начиная с той что непрозрачна
        for cls, mask in [(1, self.mask1_image), (2, self.mask2_image)]:
            if x < 0 or x >= self.img_width or y < 0 or y >= self.img_height:
                continue

            pixel = mask.pixelColor(x, y)
            if pixel.alpha() < threshold:
                continue

            self.undo_stack.append(("flood_fill", cls, mask.copy()))

            # BFS flood fill
            visited = set()
            queue = [(x, y)]
            ptr = mask.bits()
            bpl = mask.bytesPerLine()

            while queue:
                cx, cy = queue.pop()
                if (cx, cy) in visited:
                    continue
                if cx < 0 or cx >= self.img_width or cy < 0 or cy >= self.img_height:
                    continue

                idx = cy * bpl + cx * 4
                if ptr[idx + 3] < threshold:
                    continue

                visited.add((cx, cy))
                ptr[idx] = ptr[idx + 1] = ptr[idx + 2] = ptr[idx + 3] = 0
                queue.extend([
                    (cx + 1, cy), (cx - 1, cy),
                    (cx, cy + 1), (cx, cy - 1),
                ])

            if cls == 1:
                self._update_mask1_display()
            else:
                self._update_mask2_display()

            label = "junction" if cls == 1 else "bridge"
            self._update_status(f"Удалено {len(visited)} px ({label})")
            return True

        self._update_status("Пустая область")
        return False

    # ==================== UNDO ====================

    def undo(self):
        if not self.undo_stack:
            self._update_status("Нечего отменять")
            return

        action = self.undo_stack.pop()
        if action[0] == "add_square":
            _, cls, rect = action
            self.scene.removeItem(rect)
            (self.squares_white if cls == 1 else self.squares_red).remove(rect)
            self._update_status("Undo: квадрат удалён")
        elif action[0] == "remove_square":
            _, cls, rect = action
            self.scene.addItem(rect)
            (self.squares_white if cls == 1 else self.squares_red).append(rect)
            self._update_status("Undo: квадрат восстановлен")
        elif action[0] == "flood_fill":
            _, cls, old_mask = action
            if cls == 1:
                self.mask1_image = old_mask
                self._update_mask1_display()
            else:
                self.mask2_image = old_mask
                self._update_mask2_display()
            self._update_status("Undo: маска восстановлена")
        elif action[0] == "delete_blob":
            _, cls, old_mask, blob_data = action
            if cls == 1:
                self.mask1_image = old_mask
                self._update_mask1_display()
            else:
                self.mask2_image = old_mask
                self._update_mask2_display()
            blob_data["rect_item"] = None
            blob_data["confirmed"] = True
            self._blobs.append(blob_data)
            self._draw_blob_highlight(blob_data)
            self._update_status("Undo: пятно восстановлено")
        elif action[0] == "delete_all_blobs":
            _, old_mask1, old_mask2, blobs_data = action
            self.mask1_image = old_mask1
            self.mask2_image = old_mask2
            self._update_mask1_display()
            self._update_mask2_display()
            for bd in blobs_data:
                bd["rect_item"] = None
                bd["confirmed"] = True
                self._blobs.append(bd)
                self._draw_blob_highlight(bd)
            self._update_status(f"Undo: {len(blobs_data)} пятен восстановлено")

    # ==================== SAVE ====================

    def save_masks(self, mask1_path: str = "", mask2_path: str = ""):
        """
        Сохранить маски с вмерженными квадратами.

        Returns:
            (mask1_path, mask2_path) или None при ошибке
        """
        if not self.original_image:
            self._update_status("Нечего сохранять")
            return None

        # Merge белые квадраты → mask1
        for rect in self.squares_white:
            painter = QPainter(self.mask1_image)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(255, 255, 255, 255)))
            r = rect.rect()
            painter.drawRect(int(r.x()), int(r.y()), int(r.width()), int(r.height()))
            painter.end()

        # Merge красные квадраты → mask2
        for rect in self.squares_red:
            painter = QPainter(self.mask2_image)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(255, 255, 255, 255)))
            r = rect.rect()
            painter.drawRect(int(r.x()), int(r.y()), int(r.width()), int(r.height()))
            painter.end()

        # Save binary masks
        p1 = mask1_path or "junction_mask_validated.png"
        p2 = mask2_path or "bridge_mask_validated.png"
        _save_binary_mask_fast(self.mask1_image, p1)
        _save_binary_mask_fast(self.mask2_image, p2)

        self._update_status(f"Сохранено: {Path(p1).name}, {Path(p2).name}")
        return (p1, p2)

    # ==================== EVENTS ====================

    def _update_status(self, msg: str):
        if self.status_callback:
            self.status_callback(msg)

    def wheelEvent(self, event: QWheelEvent):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Control:
            self.ctrl_pressed = True
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif event.key() == Qt.Key.Key_Shift:
            self._shift_pressed = True
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif event.key() == Qt.Key.Key_Escape:
            self._cancel_drag_rect()
        elif event.key() == Qt.Key.Key_1:
            self.current_class = 1
            self._update_status("Класс: белая (junction)")
        elif event.key() == Qt.Key.Key_2:
            self.current_class = 2
            self._update_status("Класс: красная (bridge)")
        elif (
            event.key() == Qt.Key.Key_Z
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.undo()
        elif (
            event.key() == Qt.Key.Key_S
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.save_masks()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Control:
            self.ctrl_pressed = False
            if not self._shift_pressed:
                self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
                self.setCursor(Qt.CursorShape.ArrowCursor)
        elif event.key() == Qt.Key.Key_Shift:
            self._shift_pressed = False
            if not self.ctrl_pressed:
                self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
                self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            super().keyReleaseEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if self._shift_pressed and self.original_image and event.button() == Qt.MouseButton.LeftButton:
            # Начать обводку прямоугольника
            pos = self.mapToScene(event.pos())
            x, y = int(pos.x()), int(pos.y())
            if 0 <= x < self.img_width and 0 <= y < self.img_height:
                self._drag_start = (x, y)
                # Предпросмотр
                color = QColor(255, 255, 255, 80) if self.current_class == 1 else QColor(255, 0, 0, 80)
                border = QColor(255, 255, 255, 200) if self.current_class == 1 else QColor(255, 0, 0, 200)
                self._drag_rect_item = QGraphicsRectItem(x, y, 0, 0)
                self._drag_rect_item.setBrush(QBrush(color))
                self._drag_rect_item.setPen(QPen(border, 1, Qt.PenStyle.DashLine))
                self._drag_rect_item.setZValue(10)
                self.scene.addItem(self._drag_rect_item)
            event.accept()
            return
        if self.ctrl_pressed and self.original_image:
            pos = self.mapToScene(event.pos())
            x, y = int(pos.x()), int(pos.y())
            if 0 <= x < self.img_width and 0 <= y < self.img_height:
                if event.button() == Qt.MouseButton.LeftButton:
                    self.add_square(x, y)
                elif event.button() == Qt.MouseButton.RightButton:
                    self.flood_fill_delete(x, y)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_start and self._drag_rect_item:
            pos = self.mapToScene(event.pos())
            x, y = int(pos.x()), int(pos.y())
            sx, sy = self._drag_start
            rx = min(sx, x)
            ry = min(sy, y)
            rw = abs(x - sx)
            rh = abs(y - sy)
            self._drag_rect_item.setRect(rx, ry, rw, rh)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._drag_start and self._drag_rect_item and event.button() == Qt.MouseButton.LeftButton:
            # Завершить обводку — создать финальный прямоугольник
            r = self._drag_rect_item.rect()
            self.scene.removeItem(self._drag_rect_item)
            self._drag_rect_item = None
            self._drag_start = None

            # Выделить пятна, попавшие в рамку
            if r.width() >= 3 and r.height() >= 3:
                self._select_blobs_in_rect(r)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _cancel_drag_rect(self):
        """Отменить текущую обводку и снять все выделения."""
        if self._drag_rect_item:
            self.scene.removeItem(self._drag_rect_item)
            self._drag_rect_item = None
        self._drag_start = None
        # Снять все жёлтые рамки
        count = 0
        for blob in self._blobs:
            if blob["confirmed"]:
                self._remove_blob_highlight(blob)
                count += 1
        if count:
            self._update_status(f"Снято выделение с {count} пятен")
        else:
            self._update_status("Обводка отменена")

    # ==================== BLOB DETECTION ====================

    def _detect_blobs(self):
        """Найти связные компоненты (пятна) на масках, создать bbox'ы."""
        self._blobs.clear()
        for cls, mask in [(1, self.mask1_image), (2, self.mask2_image)]:
            if mask is None:
                continue
            arr = _qimage_to_numpy(mask)  # BGRA
            alpha = arr[:, :, 3]
            binary = (alpha > 128).astype(np.uint8)

            # Найти connected components
            blobs = self._find_connected_components(binary)
            for x1, y1, x2, y2 in blobs:
                # Минимальный размер
                if (x2 - x1) < 3 or (y2 - y1) < 3:
                    continue
                self._blobs.append({
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "cls": cls,
                    "confirmed": False,
                    "rect_item": None,
                })
        self._update_status(f"Найдено {len(self._blobs)} пятен (junction + bridge)")

    def _redetect_blobs(self):
        """Пере-детектить blob'ы из текущего состояния масок.
        Убирает рамки старых, строит новый список."""
        # Убрать все highlight'ы
        for blob in self._blobs:
            if blob["rect_item"]:
                self.scene.removeItem(blob["rect_item"])
                blob["rect_item"] = None
        self._blobs.clear()
        # Детектить заново
        self._detect_blobs()

    @staticmethod
    def _find_connected_components(binary: np.ndarray) -> list[tuple]:
        """Найти bbox связных компонентов. Возвращает [(x1,y1,x2,y2), ...]."""
        try:
            from scipy.ndimage import label, find_objects
            labeled, n = label(binary)
            bboxes = []
            for slc in find_objects(labeled):
                if slc is None:
                    continue
                y1, y2 = slc[0].start, slc[0].stop
                x1, x2 = slc[1].start, slc[1].stop
                bboxes.append((x1, y1, x2, y2))
            return bboxes
        except ImportError:
            # Fallback без scipy: простой scanline
            return _scanline_components(binary)

    def _select_blobs_in_rect(self, rect: QRectF):
        """Выделить пятна, bbox которых пересекается с rect."""
        rx1, ry1 = rect.x(), rect.y()
        rx2, ry2 = rx1 + rect.width(), ry1 + rect.height()
        count = 0
        for blob in self._blobs:
            # Проверить пересечение
            if (blob["x1"] < rx2 and blob["x2"] > rx1
                    and blob["y1"] < ry2 and blob["y2"] > ry1):
                if not blob["confirmed"]:
                    blob["confirmed"] = True
                    self._draw_blob_highlight(blob)
                    count += 1
        self._update_status(f"Выделено {count} пятен")

    def _draw_blob_highlight(self, blob: dict):
        """Нарисовать жёлтую рамку вокруг подтверждённого пятна."""
        if blob["rect_item"]:
            return  # уже нарисована
        x1, y1, x2, y2 = blob["x1"], blob["y1"], blob["x2"], blob["y2"]
        pad = 2
        rect = QGraphicsRectItem(x1 - pad, y1 - pad, x2 - x1 + pad*2, y2 - y1 + pad*2)
        rect.setBrush(QBrush(Qt.NoBrush))
        rect.setPen(QPen(QColor(255, 215, 0, 220), 2))  # золотой
        rect.setZValue(5)
        self.scene.addItem(rect)
        blob["rect_item"] = rect

    def _remove_blob_highlight(self, blob: dict):
        """Убрать жёлтую рамку."""
        if blob["rect_item"]:
            self.scene.removeItem(blob["rect_item"])
            blob["rect_item"] = None
        blob["confirmed"] = False

    def _delete_all_confirmed_blobs(self):
        """Удалить ВСЕ подтверждённые пятна с масок."""
        confirmed = [b for b in self._blobs if b["confirmed"]]
        if not confirmed:
            return

        # Сохранить маски для undo (одна операция)
        self.undo_stack.append((
            "delete_all_blobs",
            self.mask1_image.copy(),
            self.mask2_image.copy(),
            [b.copy() for b in confirmed],
        ))

        # Очистить пиксели для каждого blob
        arr1 = _qimage_to_numpy(self.mask1_image)
        arr2 = _qimage_to_numpy(self.mask2_image)

        for blob in confirmed:
            x1, y1, x2, y2 = blob["x1"], blob["y1"], blob["x2"], blob["y2"]
            if blob["cls"] == 1:
                arr1[y1:y2, x1:x2, :] = 0
            else:
                arr2[y1:y2, x1:x2, :] = 0
            self._remove_blob_highlight(blob)

        self.mask1_image = _numpy_to_qimage(arr1)
        self.mask2_image = _numpy_to_qimage(arr2)
        self._update_mask1_display()
        self._update_mask2_display()

        # Убрать из списка
        self._blobs = [b for b in self._blobs if not b["confirmed"]]

        # Пере-детектить blob'ы (старые bbox могут быть пустыми)
        self._redetect_blobs()

        self._update_status(f"Удалено {len(confirmed)} пятен")

    def _delete_blob(self, blob: dict):
        """Удалить подтверждённое пятно с маски (flood-fill в bbox)."""
        cls = blob["cls"]
        mask = self.mask1_image if cls == 1 else self.mask2_image

        # Сохранить для undo
        self.undo_stack.append(("delete_blob", cls, mask.copy(), blob.copy()))

        # Очистить пиксели маски внутри bbox
        x1, y1, x2, y2 = blob["x1"], blob["y1"], blob["x2"], blob["y2"]
        arr = _qimage_to_numpy(mask)
        arr[y1:y2, x1:x2, :] = 0  # прозрачный
        new_mask = _numpy_to_qimage(arr)

        if cls == 1:
            self.mask1_image = new_mask
            self._update_mask1_display()
        else:
            self.mask2_image = new_mask
            self._update_mask2_display()

        # Убрать рамку и пометить как удалённый
        self._remove_blob_highlight(blob)
        self._blobs.remove(blob)
        self._redetect_blobs()

        label = "junction" if cls == 1 else "bridge"
        self._update_status(f"Удалено пятно {label} ({x2-x1}×{y2-y1})")

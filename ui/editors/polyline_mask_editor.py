"""
Polyline Mask Editor — редактор масок полилиниями.

Используется для валидации pipe_mask (маска труб).
Ctrl+ЛКМ: добавить точку полилинии.
2×ЛКМ / ПКМ / Enter: завершить полилинию.
Escape: отменить текущую полилинию.

Инструменты: Полилиния (рисование) / Ластик (стирание).

Оптимизации vs прототип:
- numpy vectorized mask конверсии
- Grayscale 8-bit подложка
- COCO bbox рисуются через QPainter (быстро)
"""

import json
import numpy as np
from collections import deque
from pathlib import Path
from enum import Enum, auto

from PySide6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QGraphicsPathItem, QGraphicsEllipseItem, QGraphicsRectItem,
)
from PySide6.QtGui import (
    QPixmap, QImage, QPainter, QColor, QPen, QBrush,
    QPainterPath, QWheelEvent, QMouseEvent, QKeyEvent,
)
from PySide6.QtCore import Qt, QRectF, QPointF

MAX_UNDO_STEPS = 50
MASK_TILE_SIZE = 512


class PolylineTool(Enum):
    POLYLINE = auto()
    ERASER = auto()
    ADD_NODE = auto()


def _qimage_to_numpy(qimg: QImage) -> np.ndarray:
    """QImage → numpy (H, W, 4) BGRA."""
    qimg = qimg.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = qimg.width(), qimg.height()
    ptr = qimg.bits()
    return np.frombuffer(ptr, dtype=np.uint8).reshape((h, w, 4)).copy()


def _numpy_to_qimage(arr: np.ndarray) -> QImage:
    """numpy (H, W, 4) BGRA → QImage ARGB32."""
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, w * 4, QImage.Format.Format_ARGB32).copy()


def _make_black_transparent_fast(mask_qimage: QImage) -> QImage:
    """
    Бинарная маска → ARGB: чёрный→прозрачный, белый→белый непрозрачный.
    Numpy: <0.1 сек на 7000×4000.
    """
    arr = _qimage_to_numpy(mask_qimage)
    brightness = arr[:, :, :3].mean(axis=2)

    result = np.zeros_like(arr)
    white = brightness >= 128
    result[white, 0] = 255  # B
    result[white, 1] = 255  # G
    result[white, 2] = 255  # R
    result[white, 3] = 255  # A
    # Чёрные пиксели: alpha=0 (прозрачный)

    return _numpy_to_qimage(result)


def _save_binary_mask_fast(mask_qimage: QImage, path: str):
    """RGBA маска → бинарный PNG (alpha > 128 → белый)."""
    arr = _qimage_to_numpy(mask_qimage)
    alpha = arr[:, :, 3]

    binary = np.zeros((arr.shape[0], arr.shape[1], 4), dtype=np.uint8)
    white = alpha > 128
    binary[white, :3] = 255
    binary[:, :, 3] = 255  # Все пиксели непрозрачные

    qimg = _numpy_to_qimage(binary)
    qimg.save(path)


def _make_mask_colored(mask_qimage: QImage, color: QColor) -> QImage:
    """Бинарная маска → RGBA: белый→color, чёрный→прозрачный."""
    arr = _qimage_to_numpy(mask_qimage)
    brightness = arr[:, :, :3].mean(axis=2)
    result = np.zeros_like(arr)
    mask = brightness >= 128
    result[mask, 0] = color.blue()
    result[mask, 1] = color.green()
    result[mask, 2] = color.red()
    result[mask, 3] = 255
    return _numpy_to_qimage(result)


def _make_grayscale_darkened(original: QImage, brightness: float = 0.4) -> QImage:
    """Original → grayscale darkened. 28 MB vs 112 MB."""
    arr = _qimage_to_numpy(original)
    gray = (
        0.114 * arr[:, :, 0].astype(np.float32)
        + 0.587 * arr[:, :, 1].astype(np.float32)
        + 0.299 * arr[:, :, 2].astype(np.float32)
    )
    gray = (gray * brightness).clip(0, 255).astype(np.uint8)

    result = np.zeros((arr.shape[0], arr.shape[1], 4), dtype=np.uint8)
    result[:, :, 0] = gray
    result[:, :, 1] = gray
    result[:, :, 2] = gray
    result[:, :, 3] = 255

    return _numpy_to_qimage(result)


class PolylineMaskEditor(QGraphicsView):
    """
    Редактор масок полилиниями.

    Инструменты:
    - Полилиния: Ctrl+ЛКМ добавляет точки, завершение 2×ЛКМ/ПКМ/Enter
    - Ластик: Ctrl+ЛКМ зажать и водить, стирает маску

    Слои:
    - Z=0: Grayscale darkened original
    - Z=0.5: COCO bbox frames (красные контуры узлов)
    - Z=1: Маска труб (белая, 90%)
    - Z=2+: Полилинии (зелёные при рисовании, белые завершённые)
    """

    def __init__(self):
        super().__init__()

        self.scene = QGraphicsScene()
        self.setScene(self.scene)

        # Images
        self.original_image: QImage | None = None
        self.mask_image: QImage | None = None
        self.img_width = 0
        self.img_height = 0

        # Display items
        self.original_item: QGraphicsPixmapItem | None = None
        self._mask_tiles: list[list[QGraphicsPixmapItem]] = []  # [row][col] tile grid
        self._mask_tile_rows = 0
        self._mask_tile_cols = 0
        self.bbox_frames_item: QGraphicsPixmapItem | None = None

        # Current tool
        self.current_tool = PolylineTool.POLYLINE
        self.line_width = 3

        # Polyline state
        self.current_polyline_points: list[QPointF] = []
        self.current_path_item: QGraphicsPathItem | None = None
        self.preview_line_item: QGraphicsPathItem | None = None

        # Completed polylines
        self.polylines: list[QGraphicsPathItem] = []

        # Undo
        self.undo_stack: deque = deque(maxlen=MAX_UNDO_STEPS)

        # Eraser
        self.eraser_cursor: QGraphicsEllipseItem | None = None
        self._erasing = False

        # Rectangle erase (Shift+LMB drag)
        self._rect_erasing = False
        self._rect_start: QPointF | None = None
        self._rect_preview = None  # QGraphicsRectItem
        self.shift_pressed = False

        # COCO annotations
        self.coco_annotations: list[dict] = []
        self.coco_full_data: dict | None = None  # Full COCO JSON for saving

        # Add node mode
        self._pending_node_class: dict | None = None  # {"id": N, "name": "..."}
        self._node_drawing = False
        self._node_rect_start: QPointF | None = None
        self._node_rect_preview: QGraphicsRectItem | None = None
        self._added_node_items: list[QGraphicsRectItem] = []  # visual items for added nodes
        self._coco_dirty = False  # True when COCO was modified

        # View setup
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QBrush(QColor(30, 30, 30)))
        self.setMouseTracking(True)

        # Оптимизации отрисовки (аналогично ocr_binding_editor)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing)

        self.ctrl_pressed = False
        self.status_callback = None

        self._show_placeholder()

    def _show_placeholder(self):
        self.scene.clear()
        text = self.scene.addText("Выберите диаграмму для валидации труб")
        text.setDefaultTextColor(QColor(150, 150, 150))

    # ==================== LOADING ====================

    def load_images(
        self,
        original_path: str,
        mask_path: str,
        coco_path: str = "",
        pipe_mask_path: str = "",
    ) -> bool:
        """
        Загрузить изображения.

        Args:
            original_path: путь к оригиналу
            mask_path: skeleton_mask.png (маска труб)
            coco_path: coco_validated.json (для bbox рамок, опционально)
            pipe_mask_path: segmentation_mask.png (маска сегментации, опционально)
        """
        self.original_image = QImage(original_path)
        if self.original_image.isNull():
            return False

        self.img_width = self.original_image.width()
        self.img_height = self.original_image.height()

        # COCO annotations
        if coco_path and Path(coco_path).exists():
            self._load_coco(coco_path)
        else:
            self.coco_annotations = []

        # Pipe mask (segmentation) — полупрозрачный синий слой
        self.pipe_mask_image = None
        if pipe_mask_path and Path(pipe_mask_path).exists():
            pipe_raw = QImage(pipe_mask_path)
            if not pipe_raw.isNull():
                self.pipe_mask_image = _make_mask_colored(pipe_raw, QColor(0, 120, 255))

        # Mask
        if mask_path and Path(mask_path).exists():
            mask_raw = QImage(mask_path)
            if mask_raw.isNull():
                return False
            self.mask_image = _make_black_transparent_fast(mask_raw)
        else:
            self.mask_image = QImage(
                self.img_width, self.img_height, QImage.Format.Format_ARGB32
            )
            self.mask_image.fill(QColor(0, 0, 0, 0))

        self._setup_scene()
        return True

    def _load_coco(self, path: str) -> bool:
        try:
            with open(path, "r", encoding="utf-8") as f:
                coco_data = json.load(f)
            self.coco_full_data = coco_data
            self.coco_annotations = coco_data.get("annotations", [])
            return True
        except Exception:
            self.coco_full_data = None
            self.coco_annotations = []
            return False

    def _render_bbox_frames(self) -> QImage:
        """Render COCO bbox/polygon frames as red outlines."""
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

            # Segmentation polygon (priority)
            if "segmentation" in ann and ann["segmentation"]:
                for poly_coords in ann["segmentation"]:
                    if len(poly_coords) >= 6:
                        path = QPainterPath()
                        path.moveTo(poly_coords[0], poly_coords[1])
                        for i in range(2, len(poly_coords), 2):
                            path.lineTo(poly_coords[i], poly_coords[i + 1])
                        path.closeSubpath()
                        painter.drawPath(path)
                        drawn = True

            # Bbox fallback
            if not drawn and "bbox" in ann and ann["bbox"]:
                x, y, w, h = ann["bbox"]
                painter.drawRect(round(x), round(y), round(w), round(h))

        painter.end()
        return frames

    def _setup_scene(self):
        self.scene.clear()
        self.polylines.clear()
        self.current_polyline_points.clear()
        self.current_path_item = None
        self.preview_line_item = None
        self.undo_stack.clear()

        # Z=0: Grayscale darkened
        darkened = _make_grayscale_darkened(self.original_image, brightness=0.4)
        self.original_item = QGraphicsPixmapItem(QPixmap.fromImage(darkened))
        self.original_item.setZValue(0)
        self.scene.addItem(self.original_item)

        # Z=0.5: COCO bbox frames
        if self.coco_annotations:
            bbox_img = self._render_bbox_frames()
            self.bbox_frames_item = QGraphicsPixmapItem(QPixmap.fromImage(bbox_img))
            self.bbox_frames_item.setZValue(0.5)
            self.scene.addItem(self.bbox_frames_item)
        else:
            self.bbox_frames_item = None

        # Z=1: Mask tiles (тайловая маска для быстрого ластика)
        self._mask_tiles = []
        ts = MASK_TILE_SIZE
        self._mask_tile_rows = (self.img_height + ts - 1) // ts
        self._mask_tile_cols = (self.img_width + ts - 1) // ts
        for row in range(self._mask_tile_rows):
            tile_row = []
            for col in range(self._mask_tile_cols):
                x0, y0 = col * ts, row * ts
                w = min(ts, self.img_width - x0)
                h = min(ts, self.img_height - y0)
                region = self.mask_image.copy(x0, y0, w, h)
                item = QGraphicsPixmapItem(QPixmap.fromImage(region))
                item.setPos(x0, y0)
                item.setZValue(1)
                item.setOpacity(0.5)
                self.scene.addItem(item)
                tile_row.append(item)
            self._mask_tiles.append(tile_row)

        # Eraser cursor
        self.eraser_cursor = QGraphicsEllipseItem()
        self.eraser_cursor.setPen(
            QPen(QColor(255, 100, 100), 1, Qt.PenStyle.DashLine)
        )
        self.eraser_cursor.setBrush(QBrush(QColor(255, 0, 0, 50)))
        self.eraser_cursor.setZValue(100)
        self.eraser_cursor.hide()
        self.scene.addItem(self.eraser_cursor)

        self.setSceneRect(QRectF(0, 0, self.img_width, self.img_height))
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    # ==================== TOOL SWITCHING ====================

    def set_tool(self, tool: PolylineTool):
        if self.current_tool == PolylineTool.POLYLINE and tool != PolylineTool.POLYLINE:
            self._finish_polyline()

        # Cancel node drawing if switching away
        if self.current_tool == PolylineTool.ADD_NODE and tool != PolylineTool.ADD_NODE:
            self._cancel_node_drawing()

        self.current_tool = tool

        if tool == PolylineTool.ERASER:
            if self.eraser_cursor:
                self.eraser_cursor.show()
            self._update_eraser_cursor_size()
        else:
            if self.eraser_cursor:
                self.eraser_cursor.hide()

        tool_names = {
            PolylineTool.POLYLINE: "Полилиния",
            PolylineTool.ERASER: "Ластик",
            PolylineTool.ADD_NODE: "Добавить узел",
        }
        self._update_status(f"Инструмент: {tool_names.get(tool, '?')}")

    def set_line_width(self, width: int):
        self.line_width = width
        self._update_eraser_cursor_size()

        if self.current_path_item:
            pen = self.current_path_item.pen()
            pen.setWidth(width)
            self.current_path_item.setPen(pen)

    def _update_eraser_cursor_size(self):
        if self.eraser_cursor:
            r = self.line_width / 2
            self.eraser_cursor.setRect(-r, -r, self.line_width, self.line_width)

    def update_mask_display(self):
        """Полное обновление всех тайлов маски (для undo/bake/rect-erase)."""
        ts = MASK_TILE_SIZE
        for row in range(self._mask_tile_rows):
            for col in range(self._mask_tile_cols):
                x0, y0 = col * ts, row * ts
                w = min(ts, self.img_width - x0)
                h = min(ts, self.img_height - y0)
                region = self.mask_image.copy(x0, y0, w, h)
                self._mask_tiles[row][col].setPixmap(QPixmap.fromImage(region))

    def _update_mask_region(self, x: int, y: int, radius: int):
        """Обновить только тайлы, задетые областью (x±radius, y±radius)."""
        self._update_mask_rect(x - radius, y - radius, x + radius, y + radius)

    def _update_mask_rect(self, x1: int, y1: int, x2: int, y2: int):
        """Обновить только тайлы, пересекающие прямоугольник (x1,y1)-(x2,y2)."""
        ts = MASK_TILE_SIZE
        col_min = max(0, x1 // ts)
        col_max = min(self._mask_tile_cols - 1, x2 // ts)
        row_min = max(0, y1 // ts)
        row_max = min(self._mask_tile_rows - 1, y2 // ts)
        for row in range(row_min, row_max + 1):
            for col in range(col_min, col_max + 1):
                x0, y0 = col * ts, row * ts
                w = min(ts, self.img_width - x0)
                h = min(ts, self.img_height - y0)
                region = self.mask_image.copy(x0, y0, w, h)
                self._mask_tiles[row][col].setPixmap(QPixmap.fromImage(region))

    # ==================== POLYLINE ====================

    def _add_polyline_point(self, pos: QPointF):
        self.current_polyline_points.append(pos)

        if len(self.current_polyline_points) == 1:
            self.current_path_item = QGraphicsPathItem()
            pen = QPen(QColor(0, 255, 0), self.line_width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            self.current_path_item.setPen(pen)
            self.current_path_item.setZValue(2)
            self.scene.addItem(self.current_path_item)

        self._update_polyline_path()
        self._update_status(
            f"Точка {len(self.current_polyline_points)}. "
            "2x клик / ПКМ / Enter — завершить"
        )

    def _update_polyline_path(self):
        if not self.current_path_item or not self.current_polyline_points:
            return

        path = QPainterPath()
        path.moveTo(self.current_polyline_points[0])
        for pt in self.current_polyline_points[1:]:
            path.lineTo(pt)
        self.current_path_item.setPath(path)

    def _update_preview_line(self, mouse_pos: QPointF):
        if not self.current_polyline_points:
            return

        last_pt = self.current_polyline_points[-1]

        if not self.preview_line_item:
            self.preview_line_item = QGraphicsPathItem()
            pen = QPen(QColor(0, 255, 0, 128), self.line_width)
            pen.setStyle(Qt.PenStyle.DashLine)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            self.preview_line_item.setPen(pen)
            self.preview_line_item.setZValue(3)
            self.scene.addItem(self.preview_line_item)

        path = QPainterPath()
        path.moveTo(last_pt)
        path.lineTo(mouse_pos)
        self.preview_line_item.setPath(path)

    def _finish_polyline(self):
        if self.preview_line_item:
            self.scene.removeItem(self.preview_line_item)
            self.preview_line_item = None

        if not self.current_path_item or len(self.current_polyline_points) < 2:
            if self.current_path_item:
                self.scene.removeItem(self.current_path_item)
                self.current_path_item = None
            self.current_polyline_points.clear()
            self._update_status("Нужно минимум 2 точки")
            return

        # Финальный цвет — белый, полупрозрачный как маска
        pen = QPen(QColor(255, 255, 255, 255), self.line_width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self.current_path_item.setPen(pen)
        self.current_path_item.setOpacity(0.5)  # как маска

        self.polylines.append(self.current_path_item)
        self.undo_stack.append(("polyline", self.current_path_item))

        self._update_status(
            f"Полилиния завершена ({len(self.current_polyline_points)} точек)"
        )

        self.current_path_item = None
        self.current_polyline_points.clear()

    def _cancel_polyline(self):
        if self.preview_line_item:
            self.scene.removeItem(self.preview_line_item)
            self.preview_line_item = None

        if self.current_path_item:
            self.scene.removeItem(self.current_path_item)
            self.current_path_item = None

        self.current_polyline_points.clear()
        self._update_status("Полилиния отменена")

    # ==================== ERASER ====================

    def _bake_polylines_to_mask(self):
        """Merge polylines into mask image before erasing."""
        if not self.polylines:
            return

        count = len(self.polylines)

        painter = QPainter(self.mask_image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        for item in self.polylines:
            pen = QPen(QColor(255, 255, 255, 255), item.pen().width())
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(item.path())

        painter.end()

        for item in self.polylines:
            self.scene.removeItem(item)
        self.polylines.clear()

        # Remove polyline undo entries
        self.undo_stack = deque(
            [a for a in self.undo_stack if a[0] != "polyline"],
            maxlen=MAX_UNDO_STEPS,
        )

        self.update_mask_display()
        self._update_status(f"{count} полилиний объединены с маской")

    def _erase_at(self, pos: QPointF):
        x, y = int(pos.x()), int(pos.y())
        radius = self.line_width // 2

        if not self._erasing:
            if self.polylines:
                self._bake_polylines_to_mask()
            self.undo_stack.append(("erase", self.mask_image.copy()))
            self._erasing = True

        # Стираем на QImage (данные)
        painter = QPainter(self.mask_image)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.setBrush(QBrush(QColor(0, 0, 0, 0)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(x, y), radius, radius)
        painter.end()

        # Обновляем только задетые тайлы (~512×512 вместо 14K×10K)
        self._update_mask_region(x, y, radius + 1)

    def _stop_erasing(self):
        self._erasing = False

    # ==================== RECTANGLE ERASE (Shift+LMB) ====================

    def _start_rect_erase(self, pos: QPointF):
        """Start rectangle selection for erasing."""
        self._rect_start = pos
        self._rect_erasing = True

        # Create preview rectangle
        pen = QPen(QColor(255, 0, 0, 200), 2)
        pen.setStyle(Qt.PenStyle.DashLine)
        brush = QBrush(QColor(255, 0, 0, 40))
        self._rect_preview = self.scene.addRect(
            QRectF(pos, pos), pen, brush,
        )

    def _update_rect_preview(self, pos: QPointF):
        """Update rectangle preview during drag."""
        if not self._rect_preview or not self._rect_start:
            return
        rect = QRectF(self._rect_start, pos).normalized()
        self._rect_preview.setRect(rect)

    def _finish_rect_erase(self, pos: QPointF):
        """Apply rectangle erase to mask."""
        if not self._rect_start or not self.mask_image:
            self._cancel_rect_erase()
            return

        rect = QRectF(self._rect_start, pos).normalized()

        # Clamp to image bounds
        x1 = max(0, int(rect.left()))
        y1 = max(0, int(rect.top()))
        x2 = min(self.img_width, int(rect.right()))
        y2 = min(self.img_height, int(rect.bottom()))

        if x2 - x1 < 2 or y2 - y1 < 2:
            self._cancel_rect_erase()
            return

        # Bake pending polylines first
        if self.polylines:
            self._bake_polylines_to_mask()

        # Save undo
        self.undo_stack.append(("erase", self.mask_image.copy()))

        # Erase rectangle area
        painter = QPainter(self.mask_image)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.setBrush(QBrush(QColor(0, 0, 0, 0)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(x1, y1, x2 - x1, y2 - y1)
        painter.end()

        # Обновить только задетые тайлы
        self._update_mask_rect(x1, y1, x2, y2)
        self._update_status(f"Область {x2-x1}×{y2-y1} px стёрта")

        self._cancel_rect_erase()

    def _cancel_rect_erase(self):
        """Cancel/cleanup rectangle selection."""
        if self._rect_preview:
            self.scene.removeItem(self._rect_preview)
            self._rect_preview = None
        self._rect_start = None
        self._rect_erasing = False

    # ==================== ADD NODE (bbox drawing) ====================

    def set_pending_node_class(self, cls: dict):
        """Set equipment class for next node placement.

        Args:
            cls: {"id": N, "name": "..."} from NodeListDialog
        """
        self._pending_node_class = cls

    def _start_node_drawing(self, pos: QPointF):
        """Start drawing a new node bbox rectangle."""
        if not self._pending_node_class:
            self._update_status("Сначала выберите класс оборудования")
            return
        self._node_drawing = True
        self._node_rect_start = pos

        pen = QPen(QColor(0, 200, 0, 220), 2)
        pen.setStyle(Qt.PenStyle.DashLine)
        brush = QBrush(QColor(0, 200, 0, 40))
        self._node_rect_preview = self.scene.addRect(
            QRectF(pos, pos), pen, brush,
        )
        self._node_rect_preview.setZValue(50)

    def _update_node_preview(self, pos: QPointF):
        """Update node bbox preview during drag."""
        if not self._node_rect_preview or not self._node_rect_start:
            return
        rect = QRectF(self._node_rect_start, pos).normalized()
        self._node_rect_preview.setRect(rect)

    def _finish_node_drawing(self, pos: QPointF):
        """Finish drawing node bbox and add annotation to COCO."""
        if not self._node_rect_start or not self._pending_node_class:
            self._cancel_node_drawing()
            return

        rect = QRectF(self._node_rect_start, pos).normalized()

        # Minimum size check
        if rect.width() < 5 or rect.height() < 5:
            self._cancel_node_drawing()
            self._update_status("Слишком маленький bbox — отменено")
            return

        # Clamp to image
        x1 = max(0, rect.left())
        y1 = max(0, rect.top())
        x2 = min(self.img_width, rect.right())
        y2 = min(self.img_height, rect.bottom())
        w = x2 - x1
        h = y2 - y1

        cls = self._pending_node_class

        # Create COCO annotation
        new_ann_id = max((a.get("id", 0) for a in self.coco_annotations), default=0) + 1
        annotation = {
            "id": new_ann_id,
            "image_id": 1,
            "category_id": cls["id"],
            "segmentation": [],
            "area": round(w * h, 2),
            "bbox": [round(x1, 2), round(y1, 2), round(w, 2), round(h, 2)],
            "iscrowd": 0,
            "attributes": {"occluded": False, "rotation": 0.0},
        }
        self.coco_annotations.append(annotation)
        if self.coco_full_data is not None:
            self.coco_full_data["annotations"] = self.coco_annotations
        self._coco_dirty = True

        # Remove preview, draw permanent green bbox
        if self._node_rect_preview:
            self.scene.removeItem(self._node_rect_preview)
            self._node_rect_preview = None

        pen = QPen(QColor(0, 200, 0, 255), 2)
        brush = QBrush(QColor(0, 200, 0, 30))
        node_item = self.scene.addRect(x1, y1, w, h, pen, brush)
        node_item.setZValue(0.6)
        self._added_node_items.append(node_item)

        # Also update the existing bbox_frames layer (re-render)
        self._refresh_bbox_frames()

        # Undo support
        self.undo_stack.append(("add_node", annotation, node_item))

        self._node_drawing = False
        self._node_rect_start = None
        self._update_status(
            f"Добавлен узел: {cls['name']} ({int(w)}×{int(h)}px) — id={new_ann_id}"
        )

    def _cancel_node_drawing(self):
        """Cancel current node bbox drawing."""
        if self._node_rect_preview:
            self.scene.removeItem(self._node_rect_preview)
            self._node_rect_preview = None
        self._node_drawing = False
        self._node_rect_start = None

    def _refresh_bbox_frames(self):
        """Re-render the bbox frames layer after COCO changes."""
        if self.bbox_frames_item:
            self.scene.removeItem(self.bbox_frames_item)
        bbox_img = self._render_bbox_frames()
        self.bbox_frames_item = QGraphicsPixmapItem(QPixmap.fromImage(bbox_img))
        self.bbox_frames_item.setZValue(0.5)
        self.scene.addItem(self.bbox_frames_item)

    def save_coco(self, output_path: str) -> bool:
        """Save updated COCO JSON to file.

        Returns:
            True on success.
        """
        if not self.coco_full_data:
            return False
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(self.coco_full_data, f, indent=2, ensure_ascii=False)
            return True
        except Exception:
            return False

    @property
    def has_coco_changes(self) -> bool:
        """True if COCO annotations were modified (nodes added/removed)."""
        return self._coco_dirty

    # ==================== UNDO ====================

    def undo(self):
        if not self.undo_stack:
            self._update_status("Нечего отменять")
            return

        action = self.undo_stack.pop()

        if action[0] == "polyline":
            item = action[1]
            self.scene.removeItem(item)
            if item in self.polylines:
                self.polylines.remove(item)
            self._update_status("Undo: полилиния удалена")

        elif action[0] == "erase":
            self.mask_image = action[1]
            self.update_mask_display()
            self._update_status("Undo: ластик отменён")

        elif action[0] == "add_node":
            annotation = action[1]
            node_item = action[2]
            # Remove from scene
            self.scene.removeItem(node_item)
            if node_item in self._added_node_items:
                self._added_node_items.remove(node_item)
            # Remove from COCO
            if annotation in self.coco_annotations:
                self.coco_annotations.remove(annotation)
            if self.coco_full_data is not None:
                self.coco_full_data["annotations"] = self.coco_annotations
            # Re-render bbox frames
            self._refresh_bbox_frames()
            self._coco_dirty = True
            self._update_status("Undo: узел удалён")

    # ==================== SAVE ====================

    def save_mask(self, output_path: str = "") -> str | None:
        """
        Сохранить маску с вмерженными полилиниями.

        Returns:
            путь к сохранённому файлу или None
        """
        if not self.original_image:
            self._update_status("Нечего сохранять")
            return None

        # Merge polylines into mask
        painter = QPainter(self.mask_image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        for item in self.polylines:
            pen = QPen(QColor(255, 255, 255), item.pen().width())
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(item.path())

        painter.end()

        path = output_path or "pipe_mask_validated.png"
        _save_binary_mask_fast(self.mask_image, path)

        self._update_status(f"Сохранено: {Path(path).name}")
        return path

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
            self.shift_pressed = True
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.current_tool == PolylineTool.POLYLINE:
                self._finish_polyline()
        elif event.key() == Qt.Key.Key_Escape:
            if self.current_tool == PolylineTool.POLYLINE:
                self._cancel_polyline()
            elif self.current_tool == PolylineTool.ADD_NODE:
                self._cancel_node_drawing()
        elif (
            event.key() == Qt.Key.Key_Z
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.undo()
        elif (
            event.key() == Qt.Key.Key_S
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.save_mask()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Control:
            self.ctrl_pressed = False
            if not self.shift_pressed:
                self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
                self.setCursor(Qt.CursorShape.ArrowCursor)
        elif event.key() == Qt.Key.Key_Shift:
            self.shift_pressed = False
            self._cancel_rect_erase()
            if not self.ctrl_pressed:
                self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
                self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            super().keyReleaseEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        # Shift+LMB: rectangle erase
        if self.shift_pressed and self.original_image and event.button() == Qt.MouseButton.LeftButton:
            pos = self.mapToScene(event.pos())
            if 0 <= pos.x() < self.img_width and 0 <= pos.y() < self.img_height:
                self._start_rect_erase(pos)
            return

        # ADD_NODE: Ctrl+LMB starts bbox drawing
        if self.ctrl_pressed and self.original_image and self.current_tool == PolylineTool.ADD_NODE:
            pos = self.mapToScene(event.pos())
            if (
                event.button() == Qt.MouseButton.LeftButton
                and 0 <= pos.x() < self.img_width
                and 0 <= pos.y() < self.img_height
            ):
                self._start_node_drawing(pos)
            return

        if self.ctrl_pressed and self.original_image:
            pos = self.mapToScene(event.pos())

            if 0 <= pos.x() < self.img_width and 0 <= pos.y() < self.img_height:
                if event.button() == Qt.MouseButton.LeftButton:
                    if self.current_tool == PolylineTool.POLYLINE:
                        self._add_polyline_point(pos)
                    elif self.current_tool == PolylineTool.ERASER:
                        self._erase_at(pos)
                elif event.button() == Qt.MouseButton.RightButton:
                    if self.current_tool == PolylineTool.POLYLINE:
                        self._finish_polyline()
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if self.ctrl_pressed and self.current_tool == PolylineTool.POLYLINE:
            if event.button() == Qt.MouseButton.LeftButton:
                self._finish_polyline()
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        pos = self.mapToScene(event.pos())

        # Rectangle erase preview
        if self._rect_erasing and self.shift_pressed:
            self._update_rect_preview(pos)

        # Node bbox preview
        if self._node_drawing and self.current_tool == PolylineTool.ADD_NODE:
            self._update_node_preview(pos)

        if self.current_tool == PolylineTool.ERASER and self.eraser_cursor:
            self.eraser_cursor.setPos(pos)

        if self.ctrl_pressed:
            if (
                self.current_tool == PolylineTool.POLYLINE
                and self.current_polyline_points
            ):
                self._update_preview_line(pos)

            if (
                self.current_tool == PolylineTool.ERASER
                and event.buttons() & Qt.MouseButton.LeftButton
            ):
                if 0 <= pos.x() < self.img_width and 0 <= pos.y() < self.img_height:
                    self._erase_at(pos)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._rect_erasing and event.button() == Qt.MouseButton.LeftButton:
            pos = self.mapToScene(event.pos())
            self._finish_rect_erase(pos)
            return
        if self._node_drawing and event.button() == Qt.MouseButton.LeftButton:
            pos = self.mapToScene(event.pos())
            self._finish_node_drawing(pos)
            return
        if self.current_tool == PolylineTool.ERASER:
            self._stop_erasing()
        super().mouseReleaseEvent(event)

"""
Frame Editor — переиспользуемый QGraphicsView-редактор удаления рамки/штампа.

Извлечён из batch-утилиты frame_remover_app.py (QMainWindow/очередь файлов убраны).
Используется вкладкой ui/tabs/frame_tab.py (этап 0 pipeline).

Инструменты:
  🔲 Полигон (внешнее) — обвести внутреннюю часть чертежа; всё СНАРУЖИ → фон.
  ⬛ Бокс (внутреннее)  — прямоугольник; всё ВНУТРИ → фон (штамп/таблица).

Публичное API (для вкладки):
  load_image(path) -> bool          загрузить исходное изображение
  set_tool("polygon"|"box")          выбрать инструмент
  undo()                             отменить последнюю операцию
  save_image(path)                   сохранить очищенное (RGB888, DPI сохраняется)
  has_edits -> bool                  были ли применены правки
  status_callback                    callable(str) для сообщений
"""

import sys
from collections import deque
from pathlib import Path

import numpy as np

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QToolBar, QPushButton, QLabel, QStatusBar,
    QMessageBox, QGraphicsView, QGraphicsScene,
    QGraphicsPixmapItem, QGraphicsRectItem,
)
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import (
    QKeySequence, QAction, QImage, QPixmap, QColor,
    QPainter, QPainterPath, QBrush, QPen,
    QWheelEvent, QMouseEvent, QKeyEvent,
    QImageReader,
)

from ui.editors.polygon_overlay import PolygonVertexOverlay


class BrightPolygonOverlay(PolygonVertexOverlay):
    """PolygonVertexOverlay с крупными яркими точками (красно-жёлтый)."""

    DRAW_POINT_RADIUS = 8
    SNAP_THRESHOLD = 20
    DRAW_LINE_WIDTH = 3

    COLOR_DRAW_POINT = QColor(255, 50, 50)          # красный
    COLOR_DRAW_FILL = QColor(255, 50, 50, 140)
    COLOR_DRAW_LINE = QColor(255, 50, 50, 220)      # жёлтая линия
    COLOR_DRAW_PREVIEW = QColor(255, 50, 50, 120)   # жёлтый пунктир
    COLOR_SNAP_RING = QColor(255, 50, 50, 240)      # красное кольцо

    def add_draw_point(self, x, y):
        """Толстые линии и крупные точки."""
        from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsLineItem

        self._draw_points.append((x, y))

        r = self.DRAW_POINT_RADIUS
        item = QGraphicsEllipseItem(x - r, y - r, r * 2, r * 2)
        item.setPen(QPen(self.COLOR_DRAW_POINT, 3))
        item.setBrush(QBrush(self.COLOR_DRAW_FILL))
        item.setZValue(self.VERTEX_Z)
        self._scene.addItem(item)
        self._draw_point_items.append(item)

        n = len(self._draw_points)
        if n >= 2:
            px, py = self._draw_points[-2]
            line = QGraphicsLineItem(px, py, x, y)
            line.setPen(QPen(self.COLOR_DRAW_LINE, self.DRAW_LINE_WIDTH))
            line.setZValue(self.EDGE_LINE_Z)
            self._scene.addItem(line)
            self._draw_line_items.append(line)

        if n >= 3:
            self._show_snap_ring()

    def update_draw_preview(self, x, y):
        """Толстый пунктир."""
        from PySide6.QtWidgets import QGraphicsLineItem

        if self._phase != "draw" or not self._draw_points:
            return

        lx, ly = self._draw_points[-1]

        if not self._draw_preview_line:
            self._draw_preview_line = QGraphicsLineItem(lx, ly, x, y)
            pen = QPen(self.COLOR_DRAW_PREVIEW, self.DRAW_LINE_WIDTH,
                       Qt.PenStyle.DashLine)
            self._draw_preview_line.setPen(pen)
            self._draw_preview_line.setZValue(self.PREVIEW_Z)
            self._scene.addItem(self._draw_preview_line)
        else:
            self._draw_preview_line.setLine(lx, ly, x, y)

# ── Утилиты ──────────────────────────────────────────────


def _qimg_to_np(qimg: QImage) -> np.ndarray:
    """QImage → numpy (H, W, 4) BGRA."""
    qimg = qimg.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = qimg.width(), qimg.height()
    ptr = qimg.bits()
    return np.frombuffer(ptr, dtype=np.uint8).reshape((h, w, 4)).copy()


def _np_to_qimg(arr: np.ndarray) -> QImage:
    """numpy (H, W, 4) BGRA → QImage ARGB32."""
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, w * 4, QImage.Format.Format_ARGB32).copy()


def _detect_bg_color(qimage: QImage, margin: int = 15) -> QColor:
    """Медиана RGB по краям изображения → цвет фона."""
    arr = _qimg_to_np(qimage)
    h, w = arr.shape[:2]
    m = min(margin, h // 4, w // 4)

    strips = [
        arr[:m, :, :3].reshape(-1, 3),
        arr[h - m:, :, :3].reshape(-1, 3),
        arr[:, :m, :3].reshape(-1, 3),
        arr[:, w - m:, :3].reshape(-1, 3),
    ]
    med = np.median(np.concatenate(strips), axis=0).astype(np.uint8)
    return QColor(int(med[2]), int(med[1]), int(med[0]))


def _build_polygon_mask(
    width: int, height: int, points: list[tuple[float, float]],
) -> np.ndarray:
    """Полигон → numpy bool mask (True = внутри полигона)."""
    img = QImage(width, height, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 255))

    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(255, 255, 255, 255)))

    path = QPainterPath()
    path.moveTo(points[0][0], points[0][1])
    for x, y in points[1:]:
        path.lineTo(x, y)
    path.closeSubpath()
    painter.drawPath(path)
    painter.end()

    arr = _qimg_to_np(img)
    return arr[:, :, 2] > 128  # R channel: white = inside


def _build_rect_mask(
    width: int, height: int, x1: int, y1: int, x2: int, y2: int,
) -> np.ndarray:
    """Прямоугольник → numpy bool mask (True = внутри бокса)."""
    mask = np.zeros((height, width), dtype=bool)
    r1, r2 = max(0, min(y1, y2)), min(height, max(y1, y2))
    c1, c2 = max(0, min(x1, x2)), min(width, max(x1, x2))
    mask[r1:r2, c1:c2] = True
    return mask


MAX_UNDO = 30

# ── Виджет-редактор ──────────────────────────────────────


class FrameRemoverView(QGraphicsView):
    """QGraphicsView для удаления рамок: полигон (внешнее) + бокс (внутреннее)."""

    SCENE_PAD = 0.3  # 30% padding вокруг изображения

    def __init__(self):
        super().__init__()

        self.scene_obj = QGraphicsScene()
        self.setScene(self.scene_obj)

        # Изображение
        self.image: QImage | None = None
        self.img_w = 0
        self.img_h = 0
        self.image_item: QGraphicsPixmapItem | None = None

        # Инструмент: "polygon" | "box"
        self.tool = "polygon"

        # Undo
        self.undo_stack: deque = deque(maxlen=MAX_UNDO)

        # Polygon draw state
        self._poly_overlay: PolygonVertexOverlay | None = None
        self._poly_drawing = False

        # Box draw state
        self._box_drawing = False
        self._box_start = None  # (x, y)
        self._box_preview: QGraphicsRectItem | None = None

        # View setup
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.NoAnchor
        )
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QBrush(QColor(30, 30, 30)))
        self.setMouseTracking(True)
        self.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.SmartViewportUpdate
        )

        self.status_callback = None

    # ── Load / Display ────────────────────────────────────

    def load_image(self, path: str) -> bool:
        # Рендеры P&ID бывают >256 МБ в несжатом виде
        QImageReader.setAllocationLimit(1024)  # 1 GB
        qimg = QImage(path)
        if qimg.isNull():
            return False
        # Сохранить DPI из исходного файла
        self._dpm_x = qimg.dotsPerMeterX()
        self._dpm_y = qimg.dotsPerMeterY()
        self.image = qimg.convertToFormat(QImage.Format.Format_ARGB32)
        self.img_w = self.image.width()
        self.img_h = self.image.height()
        self._rebuild_scene()
        return True

    def _rebuild_scene(self):
        self.scene_obj.clear()
        self.undo_stack.clear()
        self._cleanup_polygon()
        self._cleanup_box()

        self.image_item = QGraphicsPixmapItem(QPixmap.fromImage(self.image))
        self.image_item.setZValue(0)
        self.scene_obj.addItem(self.image_item)

        pad_x = self.img_w * self.SCENE_PAD
        pad_y = self.img_h * self.SCENE_PAD
        self.setSceneRect(QRectF(
            -pad_x, -pad_y,
            self.img_w + pad_x * 2,
            self.img_h + pad_y * 2,
        ))
        self.fitInView(
            QRectF(0, 0, self.img_w, self.img_h),
            Qt.AspectRatioMode.KeepAspectRatio,
        )

    def _refresh_pixmap(self):
        """Обновить отображение после изменения self.image."""
        if self.image_item:
            self.image_item.setPixmap(QPixmap.fromImage(self.image))

    # ── Tool switching ────────────────────────────────────

    def set_tool(self, tool: str):
        # Отменить текущее рисование при смене инструмента
        self._cleanup_polygon()
        self._cleanup_box()
        self.tool = tool
        if tool == "polygon":
            self._start_polygon()
        self._status(f"Инструмент: {'Полигон (внешнее)' if tool == 'polygon' else 'Бокс (внутреннее)'}")

    # ── Polygon (удалить внешнее) ─────────────────────────

    def _start_polygon(self):
        self._cleanup_polygon()
        self._poly_overlay = BrightPolygonOverlay(self.scene_obj, "__frame__")
        self._poly_overlay.show_draw()
        self._poly_drawing = True
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._status(
            "Полигон: ЛКМ — точки, замкнуть на 1-ю / ПКМ / Enter. "
            "Ctrl+Z — отменить точку. Escape — отмена."
        )

    def _cleanup_polygon(self):
        if self._poly_overlay:
            self._poly_overlay.hide()
            self._poly_overlay = None
        self._poly_drawing = False

    def _apply_polygon(self):
        if not self._poly_overlay:
            return
        points = self._poly_overlay.get_draw_points()
        if len(points) < 3:
            self._status("Нужно минимум 3 точки.")
            return

        backup = self.image.copy()
        bg = _detect_bg_color(self.image)
        inside = _build_polygon_mask(self.img_w, self.img_h, points)

        arr = _qimg_to_np(self.image)
        outside = ~inside
        arr[outside, 0] = bg.blue()
        arr[outside, 1] = bg.green()
        arr[outside, 2] = bg.red()
        self.image = _np_to_qimg(arr)
        self.image.setDotsPerMeterX(self._dpm_x)
        self.image.setDotsPerMeterY(self._dpm_y)

        self._refresh_pixmap()
        self.undo_stack.append(backup)
        self._status(
            f"Внешняя рамка удалена. Фон: RGB({bg.red()},{bg.green()},{bg.blue()})"
        )
        self._cleanup_polygon()
        # Вернуть drag mode
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    # ── Box (удалить внутреннее) ──────────────────────────

    def _start_box(self, scene_pos):
        x, y = int(scene_pos.x()), int(scene_pos.y())
        if not (0 <= x < self.img_w and 0 <= y < self.img_h):
            return
        self._box_start = (x, y)
        self._box_drawing = True

        self._box_preview = QGraphicsRectItem()
        self._box_preview.setPen(QPen(QColor(255, 80, 80), 2, Qt.PenStyle.DashLine))
        self._box_preview.setBrush(QBrush(QColor(255, 0, 0, 40)))
        self._box_preview.setZValue(100)
        self.scene_obj.addItem(self._box_preview)

    def _update_box_preview(self, scene_pos):
        if not self._box_drawing or not self._box_start:
            return
        x1, y1 = self._box_start
        x2 = max(0, min(int(scene_pos.x()), self.img_w))
        y2 = max(0, min(int(scene_pos.y()), self.img_h))
        self._box_preview.setRect(QRectF(
            min(x1, x2), min(y1, y2),
            abs(x2 - x1), abs(y2 - y1),
        ))

    def _finish_box(self, scene_pos):
        if not self._box_drawing or not self._box_start:
            return
        x1, y1 = self._box_start
        x2 = max(0, min(int(scene_pos.x()), self.img_w))
        y2 = max(0, min(int(scene_pos.y()), self.img_h))

        # Убрать preview
        self._cleanup_box()

        # Минимальный размер
        if abs(x2 - x1) < 5 or abs(y2 - y1) < 5:
            self._status("Слишком маленький бокс, отменено.")
            return

        backup = self.image.copy()
        bg = _detect_bg_color(self.image)
        inside = _build_rect_mask(self.img_w, self.img_h, x1, y1, x2, y2)

        arr = _qimg_to_np(self.image)
        arr[inside, 0] = bg.blue()
        arr[inside, 1] = bg.green()
        arr[inside, 2] = bg.red()
        self.image = _np_to_qimg(arr)
        self.image.setDotsPerMeterX(self._dpm_x)
        self.image.setDotsPerMeterY(self._dpm_y)

        self._refresh_pixmap()
        self.undo_stack.append(backup)
        self._status(
            f"Внутренний блок удалён ({abs(x2-x1)}×{abs(y2-y1)}px). "
            f"Фон: RGB({bg.red()},{bg.green()},{bg.blue()})"
        )

    def _cleanup_box(self):
        if self._box_preview:
            self.scene_obj.removeItem(self._box_preview)
            self._box_preview = None
        self._box_drawing = False
        self._box_start = None

    # ── Undo ──────────────────────────────────────────────

    def undo(self):
        if not self.undo_stack:
            self._status("Нечего отменять.")
            return
        self.image = self.undo_stack.pop()
        self._refresh_pixmap()
        self._status("Отменено.")

    # ── Save ──────────────────────────────────────────────

    def save_image(self, path: str):
        if self.image:
            # Конвертировать в RGB 24-bit (без alpha)
            rgb = self.image.convertToFormat(QImage.Format.Format_RGB888)
            rgb.setDotsPerMeterX(self._dpm_x)
            rgb.setDotsPerMeterY(self._dpm_y)
            rgb.save(path)

    # ── State ─────────────────────────────────────────────

    @property
    def has_edits(self) -> bool:
        """Были ли применены правки (есть что сохранять)."""
        return len(self.undo_stack) > 0

    # ── Status ────────────────────────────────────────────

    def _status(self, msg: str):
        if self.status_callback:
            self.status_callback(msg)

    # ── Events ────────────────────────────────────────────

    def wheelEvent(self, event: QWheelEvent):
        old_pos = self.mapToScene(event.position().toPoint())
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        new_pos = self.mapToScene(event.position().toPoint())
        delta = old_pos - new_pos
        self.horizontalScrollBar().setValue(
            self.horizontalScrollBar().value() + int(delta.x() * self.transform().m11())
        )
        self.verticalScrollBar().setValue(
            self.verticalScrollBar().value() + int(delta.y() * self.transform().m22())
        )

    def mousePressEvent(self, event: QMouseEvent):
        pos = self.mapToScene(event.position().toPoint())

        # ── Polygon mode ──
        if self.tool == "polygon" and self._poly_drawing and self._poly_overlay:
            if event.button() == Qt.MouseButton.LeftButton:
                if 0 <= pos.x() < self.img_w and 0 <= pos.y() < self.img_h:
                    if self._poly_overlay.is_near_first_point(pos.x(), pos.y()):
                        self._apply_polygon()
                    else:
                        self._poly_overlay.add_draw_point(pos.x(), pos.y())
                        n = self._poly_overlay.draw_point_count
                        self._status(f"Полигон: {n} точек. Замкните / ПКМ / Enter.")
                return
            elif event.button() == Qt.MouseButton.RightButton:
                if self._poly_overlay.draw_point_count >= 3:
                    self._apply_polygon()
                return

        # ── Box mode ──
        if self.tool == "box":
            if event.button() == Qt.MouseButton.LeftButton:
                self._start_box(pos)
                return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if self.tool == "polygon" and self._poly_drawing and self._poly_overlay:
            if event.button() == Qt.MouseButton.LeftButton:
                if self._poly_overlay.draw_point_count >= 3:
                    self._apply_polygon()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        pos = self.mapToScene(event.position().toPoint())

        # Polygon preview
        if self.tool == "polygon" and self._poly_drawing and self._poly_overlay:
            self._poly_overlay.update_draw_preview(pos.x(), pos.y())
            return

        # Box preview
        if self.tool == "box" and self._box_drawing:
            self._update_box_preview(pos)
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self.tool == "box" and self._box_drawing:
            if event.button() == Qt.MouseButton.LeftButton:
                pos = self.mapToScene(event.position().toPoint())
                self._finish_box(pos)
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        # Polygon hotkeys
        if self.tool == "polygon" and self._poly_drawing:
            if event.key() == Qt.Key.Key_Escape:
                self._cleanup_polygon()
                self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
                self.setCursor(Qt.CursorShape.ArrowCursor)
                self._status("Полигон отменён.")
                return
            if (
                event.key() == Qt.Key.Key_Z
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                if self._poly_overlay:
                    self._poly_overlay.undo_last_draw_point()
                    n = self._poly_overlay.draw_point_count
                    self._status(f"Полигон: {n} точек.")
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self._poly_overlay and self._poly_overlay.draw_point_count >= 3:
                    self._apply_polygon()
                return

        # Box hotkeys
        if self.tool == "box" and self._box_drawing:
            if event.key() == Qt.Key.Key_Escape:
                self._cleanup_box()
                self._status("Бокс отменён.")
                return

        # Global undo (вне рисования)
        if (
            event.key() == Qt.Key.Key_Z
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.undo()
            return

        super().keyPressEvent(event)

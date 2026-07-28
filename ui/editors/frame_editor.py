"""
Frame Editor — переиспользуемый QGraphicsView-редактор удаления рамки/штампа.

Извлечён из batch-утилиты frame_remover_app.py (QMainWindow/очередь файлов убраны).
Используется вкладкой ui/tabs/frame_tab.py (этап 0 pipeline).

Инструменты:
  🔲 Полигон (внешнее) — обвести внутреннюю часть чертежа; всё СНАРУЖИ → фон.
  ⬛ Бокс (внутреннее)  — прямоугольник; всё ВНУТРИ → фон (штамп/таблица).
  ✂ Обрезать            — прямоугольник; всё ВНУТРИ остаётся, остальное
                           отрезается (лист уменьшается, DPI сохраняется).

Публичное API (для вкладки):
  load_image(path) -> bool          загрузить исходное изображение
  set_tool("polygon"|"box"|"crop")   выбрать инструмент
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
from PySide6.QtCore import Qt, QRect, QRectF
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
    """QGraphicsView для удаления рамок: полигон (внешнее) + бокс (внутреннее) + обрезка."""

    SCENE_PAD = 0.3  # 30% padding вокруг изображения
    # Зона захвата стороны превью обрезки, экранных px (объектов-ручек нет —
    # hit-test по самому прямоугольнику).
    CROP_HANDLE_PX = 6
    MIN_CROP = 5     # меньше — вырожденная рамка, обрезка отменяется

    def __init__(self):
        super().__init__()

        self.scene_obj = QGraphicsScene()
        self.setScene(self.scene_obj)

        # Изображение
        self.image: QImage | None = None
        self.img_w = 0
        self.img_h = 0
        self.image_item: QGraphicsPixmapItem | None = None

        # Инструмент: "polygon" | "box" | "crop"
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

        # Crop draw state (отдельное состояние, не делится с box)
        self._crop_drawing = False
        self._crop_start = None  # (x, y)
        self._crop_preview: QGraphicsRectItem | None = None
        # Припаркованное превью: протяжка закончилась, обрезка ЕЩЁ НЕ применена.
        # Отдельное поле, а не `_crop_preview is not None`: превью-item живёт и
        # во время протяжки, а _crop_drawing к этому моменту уже сброшен.
        self._crop_rect: tuple | None = None      # (l, t, r, b)
        self._crop_drag_zone: str | None = None   # какая ручка тянется
        self._crop_drag_from = None               # (x, y) на момент захвата
        self._crop_drag_rect: tuple | None = None # rect на момент захвата

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

    def _reset_scene_keep_undo(self):
        """Пересобрать сцену под новый размер self.image, НЕ очищая undo_stack.

        Как _rebuild_scene, но для смены размера внутри сессии (crop / его undo):
        история правок должна пережить пересборку. Cleanup'ы рисования — ДО
        scene.clear(): overlay/preview снимают item'ы через removeItem, после
        clear() это были бы уже удалённые объекты.
        """
        self._cleanup_polygon()
        self._cleanup_box()
        self._cleanup_crop()
        self.scene_obj.clear()

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
        # Рисование сброшено — вернуть пан/курсор (как _apply_polygon)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def _refresh_pixmap(self):
        """Обновить отображение после изменения self.image."""
        if self.image_item:
            self.image_item.setPixmap(QPixmap.fromImage(self.image))

    # ── Tool switching ────────────────────────────────────

    def set_tool(self, tool: str):
        # Отменить текущее рисование при смене инструмента. Припаркованное
        # превью обрезки при этом тоже исчезает — смена инструмента считается
        # отказом от обрезки.
        self._cleanup_polygon()
        self._cleanup_box()
        self._cleanup_crop()
        self.tool = tool
        if tool == "polygon":
            self._start_polygon()
        names = {
            "polygon": "Полигон (внешнее)",
            "box": "Бокс (внутреннее)",
            "crop": "Обрезать (оставить выделенное)",
        }
        self._status(f"Инструмент: {names.get(tool, tool)}")

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

    # ── Crop (обрезать лист: оставить внутреннее) ─────────

    def _start_crop(self, scene_pos):
        x, y = int(scene_pos.x()), int(scene_pos.y())
        if not (0 <= x < self.img_w and 0 <= y < self.img_h):
            return
        self._crop_start = (x, y)
        self._crop_drawing = True

        # Зелёное превью — область, которая ОСТАНЕТСЯ (красное = удаление)
        self._crop_preview = QGraphicsRectItem()
        self._crop_preview.setPen(QPen(QColor(60, 200, 90), 2, Qt.PenStyle.DashLine))
        self._crop_preview.setBrush(QBrush(QColor(60, 200, 90, 40)))
        self._crop_preview.setZValue(100)
        self.scene_obj.addItem(self._crop_preview)

    def _update_crop_preview(self, scene_pos):
        if not self._crop_drawing or not self._crop_start:
            return
        x1, y1 = self._crop_start
        x2 = max(0, min(int(scene_pos.x()), self.img_w))
        y2 = max(0, min(int(scene_pos.y()), self.img_h))
        self._crop_preview.setRect(QRectF(
            min(x1, x2), min(y1, y2),
            abs(x2 - x1), abs(y2 - y1),
        ))

    def _finish_crop(self, scene_pos):
        """Конец протяжки — превью ПАРКУЕТСЯ, обрезка не применяется.

        Раньше отпускание кнопки сразу резало лист. Теперь рамку можно
        поправить (тяга за любую точку стороны / за углы / перенос целиком) и
        применить по Enter, отменить по Esc.
        """
        if not self._crop_drawing or not self._crop_start:
            return
        x1, y1 = self._crop_start
        x2 = max(0, min(int(scene_pos.x()), self.img_w))
        y2 = max(0, min(int(scene_pos.y()), self.img_h))
        self._crop_drawing = False
        self._crop_start = None

        l, t = min(x1, x2), min(y1, y2)
        r, b = max(x1, x2), max(y1, y2)
        # Проверяем вырожденность здесь, а не в _apply_crop: иначе Enter молча
        # ничего не делал бы (пользователь не понял бы, что рамка мала).
        if (r - l) < self.MIN_CROP or (b - t) < self.MIN_CROP:
            self._cleanup_crop()
            self._status("Слишком маленькая область обрезки, отменено.")
            return

        self._crop_rect = (l, t, r, b)
        self._sync_crop_preview()
        self._status(
            f"Область {r-l}×{b-t}px. Тяните за стороны/углы или внутри — "
            "перенести. Enter — обрезать, Esc — отмена."
        )

    # ── Правка припаркованного превью ─────────────────────

    def _sync_crop_preview(self):
        if self._crop_preview is None or self._crop_rect is None:
            return
        l, t, r, b = self._crop_rect
        self._crop_preview.setRect(QRectF(l, t, r - l, b - t))

    def _crop_tolerance(self) -> float:
        """Ширина зоны захвата стороны в координатах сцены (≈CROP_HANDLE_PX
        экранных пикселей при любом зуме)."""
        scale = abs(self.transform().m11()) or 1.0
        return self.CROP_HANDLE_PX / scale

    def _crop_zone_at(self, x: float, y: float) -> str | None:
        """Зона под точкой: 'l','r','t','b','tl','tr','bl','br','move' или None.

        Ручек-объектов нет — hit-test по самому прямоугольнику (углы попадают
        сразу в обе стороны, внутри — перенос рамки).
        """
        if self._crop_rect is None:
            return None
        l, t, r, b = self._crop_rect
        tol = self._crop_tolerance()
        if not (l - tol <= x <= r + tol and t - tol <= y <= b + tol):
            return None
        h = 'l' if abs(x - l) <= tol else ('r' if abs(x - r) <= tol else '')
        v = 't' if abs(y - t) <= tol else ('b' if abs(y - b) <= tol else '')
        zone = v + h                       # 'tl', 'tr', 'bl', 'br'
        if zone:
            return zone
        return 'move' if (l <= x <= r and t <= y <= b) else None

    _CROP_CURSORS = {
        'l': Qt.CursorShape.SizeHorCursor, 'r': Qt.CursorShape.SizeHorCursor,
        't': Qt.CursorShape.SizeVerCursor, 'b': Qt.CursorShape.SizeVerCursor,
        'tl': Qt.CursorShape.SizeFDiagCursor, 'br': Qt.CursorShape.SizeFDiagCursor,
        'tr': Qt.CursorShape.SizeBDiagCursor, 'bl': Qt.CursorShape.SizeBDiagCursor,
        'move': Qt.CursorShape.SizeAllCursor,
    }

    def _update_crop_cursor(self, x: float, y: float):
        zone = self._crop_zone_at(x, y)
        self.setCursor(self._CROP_CURSORS.get(zone, Qt.CursorShape.CrossCursor))

    def _start_crop_drag(self, zone: str, x: float, y: float):
        self._crop_drag_zone = zone
        self._crop_drag_from = (x, y)
        self._crop_drag_rect = self._crop_rect

    def _update_crop_drag(self, x: float, y: float):
        """Пересчитать рамку под текущее положение курсора."""
        if not self._crop_drag_zone or self._crop_drag_rect is None:
            return
        l, t, r, b = self._crop_drag_rect
        dx = int(x - self._crop_drag_from[0])
        dy = int(y - self._crop_drag_from[1])
        zone = self._crop_drag_zone

        if zone == 'move':
            dx = max(-l, min(dx, self.img_w - r))
            dy = max(-t, min(dy, self.img_h - b))
            l, r, t, b = l + dx, r + dx, t + dy, b + dy
        else:
            if 'l' in zone:
                l = max(0, min(l + dx, r - self.MIN_CROP))
            if 'r' in zone:
                r = min(self.img_w, max(r + dx, l + self.MIN_CROP))
            if 't' in zone:
                t = max(0, min(t + dy, b - self.MIN_CROP))
            if 'b' in zone:
                b = min(self.img_h, max(b + dy, t + self.MIN_CROP))

        self._crop_rect = (l, t, r, b)
        self._sync_crop_preview()
        self._status(f"Область {r-l}×{b-t}px. Enter — обрезать, Esc — отмена.")

    def _end_crop_drag(self):
        self._crop_drag_zone = None
        self._crop_drag_from = None
        self._crop_drag_rect = None

    def _commit_crop_preview(self):
        """Enter — применить припаркованное превью."""
        if self._crop_rect is None:
            return
        l, t, r, b = self._crop_rect
        self._cleanup_crop()
        self._apply_crop(l, t, r, b)

    def _apply_crop(self, x1: int, y1: int, x2: int, y2: int):
        """Обрезать self.image по прямоугольнику: внутреннее остаётся.

        Попиксельный QImage.copy(QRect) без пересэмплирования; DPI переносится,
        поэтому дальше по пайплайну (save_image) уходит лист меньшего размера
        в том же качестве.
        """
        l, t = max(0, min(x1, x2)), max(0, min(y1, y2))
        r, b = min(self.img_w, max(x1, x2)), min(self.img_h, max(y1, y2))
        w, h = r - l, b - t

        # Вырожденный прямоугольник
        if w < 5 or h < 5:
            self._status("Слишком маленькая область обрезки, отменено.")
            return

        backup = self.image.copy()
        self.image = self.image.copy(QRect(l, t, w, h))
        self.image.setDotsPerMeterX(self._dpm_x)
        self.image.setDotsPerMeterY(self._dpm_y)
        self.img_w = self.image.width()
        self.img_h = self.image.height()

        self.undo_stack.append(backup)
        # Размер сцены изменился — пересобрать, сохранив историю
        # (заодно вернёт ScrollHandDrag/ArrowCursor)
        self._reset_scene_keep_undo()
        self._status(
            f"Лист обрезан до {w}×{h}px, DPI сохранён. Ctrl+Z — вернуть исходный размер."
        )

    def _cleanup_crop(self):
        if self._crop_preview:
            self.scene_obj.removeItem(self._crop_preview)
            self._crop_preview = None
        self._crop_drawing = False
        self._crop_start = None
        self._crop_rect = None
        self._end_crop_drag()

    # ── Undo ──────────────────────────────────────────────

    def undo(self):
        if not self.undo_stack:
            self._status("Нечего отменять.")
            return
        popped = self.undo_stack.pop()
        size_changed = (
            popped.width() != self.img_w or popped.height() != self.img_h
        )
        self.image = popped
        if size_changed:
            # Отмена crop: восстановить размеры и сцену, undo_stack не трогать
            self.img_w = self.image.width()
            self.img_h = self.image.height()
            self._reset_scene_keep_undo()
            self._status(f"Отменено (размер восстановлен: {self.img_w}×{self.img_h}px).")
        else:
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

        # ── Crop mode ──
        if self.tool == "crop":
            if event.button() == Qt.MouseButton.LeftButton:
                # Припарковано превью и попали в него — правим рамку,
                # а не начинаем новую протяжку.
                zone = self._crop_zone_at(pos.x(), pos.y())
                if zone:
                    self._start_crop_drag(zone, pos.x(), pos.y())
                    return
                # Клик мимо превью — обычная новая протяжка (старое превью
                # заменяется). Панорамирование не подавляем сверх прежнего:
                # средняя/правая кнопка по-прежнему уходят в super().
                self._cleanup_crop()
                self._start_crop(pos)
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

        # Crop preview
        if self.tool == "crop":
            if self._crop_drawing:
                self._update_crop_preview(pos)
                return
            if self._crop_drag_zone:
                self._update_crop_drag(pos.x(), pos.y())
                return
            if self._crop_rect is not None:
                self._update_crop_cursor(pos.x(), pos.y())
                return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self.tool == "box" and self._box_drawing:
            if event.button() == Qt.MouseButton.LeftButton:
                pos = self.mapToScene(event.position().toPoint())
                self._finish_box(pos)
            return
        if self.tool == "crop" and self._crop_drawing:
            if event.button() == Qt.MouseButton.LeftButton:
                pos = self.mapToScene(event.position().toPoint())
                self._finish_crop(pos)
            return
        if self.tool == "crop" and self._crop_drag_zone:
            if event.button() == Qt.MouseButton.LeftButton:
                self._end_crop_drag()
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

        # Crop hotkeys (только пока crop реально живёт — не красть Esc у других)
        if self.tool == "crop" and (self._crop_drawing or self._crop_rect is not None):
            if event.key() == Qt.Key.Key_Escape:
                self._cleanup_crop()
                self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
                self.setCursor(Qt.CursorShape.ArrowCursor)
                self._status("Обрезка отменена.")
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                # Return у crop раньше не был занят вовсе (только у polygon).
                self._commit_crop_preview()
                return
            if (
                event.key() == Qt.Key.Key_Z
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                # Ctrl+Z при живом превью: undo пересобрал бы сцену
                # (_reset_scene_keep_undo → _cleanup_crop) и убил превью-item
                # посреди жеста. Отменяем сначала само превью — применено ещё
                # ничего не было, отменять в изображении нечего. Следующий
                # Ctrl+Z уже отменит правку изображения.
                self._cleanup_crop()
                self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
                self.setCursor(Qt.CursorShape.ArrowCursor)
                self._status("Обрезка отменена (Ctrl+Z ещё раз — отменить правку).")
                return

        # Global undo (вне рисования)
        if (
            event.key() == Qt.Key.Key_Z
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.undo()
            return

        super().keyPressEvent(event)

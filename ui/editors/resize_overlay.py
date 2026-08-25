"""
Resizable Node Overlay — 4 угловых handle для изменения размера bbox узла.

Используется в SimpleGraphEditor и AdvancedGraphEditor для:
- Новых equipment-узлов (показывается сразу после добавления)
- Существующих equipment-узлов (двойной Ctrl+Click)

Ограничения:
- Минимальный размер bbox: 15×15
- Начальный размер: 40×40
- Максимального ограничения нет
"""

from typing import Callable, Optional

from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsScene
from PySide6.QtGui import QColor, QPen, QBrush, QCursor
from PySide6.QtCore import Qt


class ResizableNodeOverlay:
    """4 угловых handle (tl, tr, bl, br) для изменения размера bbox.

    Использование:
        overlay = ResizableNodeOverlay(
            scene=self.scene,
            bbox=[x1, y1, x2, y2],
            on_resize=lambda bbox: ...,   # при каждом движении
            on_commit=lambda: ...,         # при mouseRelease
        )
        overlay.show()

        # В mousePressEvent:
        handle = overlay.find_handle_at(x, y)
        if handle:
            overlay.start_drag(handle)

        # В mouseMoveEvent:
        overlay.drag_to(x, y)

        # В mouseReleaseEvent:
        overlay.end_drag()

        # Cleanup:
        overlay.hide()
    """

    HANDLE_SIZE = 8
    MIN_BBOX_SIZE = 15
    HANDLE_Z = 20

    # Цвета
    HANDLE_PEN_COLOR = QColor("#f1c40f")
    HANDLE_FILL_COLOR = QColor(255, 255, 0, 100)
    HANDLE_ACTIVE_FILL = QColor(255, 255, 0, 200)

    def __init__(self, scene: QGraphicsScene, bbox: list,
                 min_size: int = 15,
                 on_resize: Optional[Callable] = None,
                 on_commit: Optional[Callable] = None,
                 handle_offset: float = 0.0,
                 grab_radius: Optional[float] = None):
        """
        Args:
            scene: QGraphicsScene для добавления handles
            bbox: [x1, y1, x2, y2]
            min_size: Минимальный размер bbox по каждой оси
            on_resize: Callback(new_bbox) при каждом движении handle
            on_commit: Callback() при завершении drag (mouseRelease)
            handle_offset: сдвиг ручки НАРУЖУ по диагонали от угла, px сцены.
                0 — ручка на самом углу (как было). Смещение уводит ручку
                из-под порога клика по узлу (`CLICK_THRESHOLD`), который в
                растровой сцене втрое шире, чем на холсте; drag_to сдвиг
                компенсирует, поэтому угол по-прежнему идёт за курсором.
            grab_radius: радиус захвата, px сцены. None — `HANDLE_SIZE + 4`.

        ⚠ Оверлей ОБЩИЙ с «Ручной правкой»: оба параметра — инстансные и по
        умолчанию дают ровно прежнее поведение, класс-константы не трогаются.
        """
        self._scene = scene
        self._bbox = bbox.copy()
        self._min_size = min_size
        self._on_resize = on_resize
        self._on_commit = on_commit
        self._handle_offset = float(handle_offset)
        self._grab_radius = float(grab_radius) if grab_radius is not None \
            else float(self.HANDLE_SIZE + 4)

        # Handle items
        self._handles: dict[str, QGraphicsRectItem] = {}

        # Drag state
        self._dragging_handle: str | None = None
        self._drag_start_bbox: list | None = None

        # Visible state
        self._visible = False

    # =================================================================
    # Show / Hide
    # =================================================================

    def show(self):
        """Создать и показать 4 corner handles."""
        self.hide()  # Убрать предыдущие если были

        hs = self.HANDLE_SIZE
        pen = QPen(self.HANDLE_PEN_COLOR, 2)
        brush = QBrush(self.HANDLE_FILL_COLOR)

        for corner in ("tl", "tr", "bl", "br"):
            rect = QGraphicsRectItem(0, 0, hs, hs)
            rect.setPen(pen)
            rect.setBrush(brush)
            rect.setZValue(self.HANDLE_Z)
            self._scene.addItem(rect)
            self._handles[corner] = rect

        self._update_handle_positions()
        self._visible = True

    def hide(self):
        """Удалить handles из сцены."""
        for item in self._handles.values():
            self._scene.removeItem(item)
        self._handles.clear()
        self._visible = False
        self._dragging_handle = None

    @property
    def visible(self) -> bool:
        return self._visible

    # =================================================================
    # Handle positions
    # =================================================================

    def _corner_shift(self, corner: str) -> tuple:
        """Сдвиг ручки от угла НАРУЖУ по диагонали: (dx, dy) в px сцены."""
        if not self._handle_offset:
            return (0.0, 0.0)
        d = self._handle_offset / (2 ** 0.5)     # по диагонали, поровну на оси
        sx = -d if corner in ("tl", "bl") else d
        sy = -d if corner in ("tl", "tr") else d
        return (sx, sy)

    def handle_centres(self) -> dict:
        """Центры четырёх ручек в координатах сцены — с учётом смещения."""
        x1, y1, x2, y2 = self._bbox
        base = {"tl": (x1, y1), "tr": (x2, y1),
                "bl": (x1, y2), "br": (x2, y2)}
        out = {}
        for corner, (cx, cy) in base.items():
            sx, sy = self._corner_shift(corner)
            out[corner] = (cx + sx, cy + sy)
        return out

    def _update_handle_positions(self):
        """Обновить позиции 4 handles по текущему bbox."""
        if not self._handles:
            return

        hs = self.HANDLE_SIZE
        half = hs / 2
        positions = {c: (cx - half, cy - half)
                     for c, (cx, cy) in self.handle_centres().items()}

        for corner, (px, py) in positions.items():
            if corner in self._handles:
                self._handles[corner].setRect(px, py, hs, hs)

    # =================================================================
    # Hit testing
    # =================================================================

    def find_handle_at(self, x: float, y: float) -> str | None:
        """Найти handle под координатами.

        Returns:
            'tl' | 'tr' | 'bl' | 'br' | None
        """
        threshold = self._grab_radius   # по умолчанию HANDLE_SIZE + 4

        corners = self.handle_centres()

        best_corner = None
        best_dist = threshold

        for corner, (cx, cy) in corners.items():
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_corner = corner

        return best_corner

    # =================================================================
    # Drag
    # =================================================================

    def start_drag(self, handle_name: str):
        """Начать перетаскивание handle."""
        self._dragging_handle = handle_name
        self._drag_start_bbox = self._bbox.copy()

        # Подсветить активный handle
        if handle_name in self._handles:
            self._handles[handle_name].setBrush(QBrush(self.HANDLE_ACTIVE_FILL))

    def drag_to(self, x: float, y: float):
        """Обновить bbox при движении handle.

        Соблюдает минимальный размер.
        """
        if not self._dragging_handle or not self._drag_start_bbox:
            return

        new_bbox = self._bbox.copy()
        handle = self._dragging_handle
        ms = self._min_size

        # Ручка нарисована СНАРУЖИ угла — снимаем сдвиг, иначе угол прыгнул бы
        # к курсору на всю величину смещения в первый же кадр тяги.
        sx, sy = self._corner_shift(handle)
        x, y = x - sx, y - sy

        # Двигаем соответствующие углы
        if handle == "tl":
            new_bbox[0] = min(x, new_bbox[2] - ms)  # x1
            new_bbox[1] = min(y, new_bbox[3] - ms)  # y1
        elif handle == "tr":
            new_bbox[2] = max(x, new_bbox[0] + ms)  # x2
            new_bbox[1] = min(y, new_bbox[3] - ms)  # y1
        elif handle == "bl":
            new_bbox[0] = min(x, new_bbox[2] - ms)  # x1
            new_bbox[3] = max(y, new_bbox[1] + ms)  # y2
        elif handle == "br":
            new_bbox[2] = max(x, new_bbox[0] + ms)  # x2
            new_bbox[3] = max(y, new_bbox[1] + ms)  # y2

        self._bbox = new_bbox
        self._update_handle_positions()

        if self._on_resize:
            self._on_resize(new_bbox)

    def end_drag(self):
        """Завершить drag → commit."""
        if self._dragging_handle and self._dragging_handle in self._handles:
            # Убрать подсветку
            self._handles[self._dragging_handle].setBrush(QBrush(self.HANDLE_FILL_COLOR))

        self._dragging_handle = None
        self._drag_start_bbox = None

        if self._on_commit:
            self._on_commit()

    @property
    def is_dragging(self) -> bool:
        return self._dragging_handle is not None

    # =================================================================
    # Accessors
    # =================================================================

    @property
    def bbox(self) -> list:
        """Текущий bbox [x1, y1, x2, y2]."""
        return self._bbox.copy()

    def set_bbox(self, bbox: list):
        """Установить bbox и обновить позиции handles."""
        self._bbox = bbox.copy()
        if self._visible:
            self._update_handle_positions()

    @property
    def start_bbox(self) -> list | None:
        """Bbox на момент начала drag (для undo)."""
        return self._drag_start_bbox.copy() if self._drag_start_bbox else None

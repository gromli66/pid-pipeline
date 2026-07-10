"""
Progress Beads — визуальный индикатор прогресса пайплайна P&ID.

Поддерживает горизонтальную и вертикальную ориентацию.
Вертикальная цепочка бусин:  ●
                             │
                             ●
                             │
                             ●
Состояния: выполнено (зелёная), текущий (оранжевая sweep-анимация),
           доступно (белая обводка), недоступно (серая), ошибка (красная).

Может выравнивать бусины по позициям внешних виджетов
(кнопок этапов) через set_anchor_widgets() — по X (гориз.) или Y (вертик.).
"""

from typing import List, Optional
from dataclasses import dataclass
from enum import Enum

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QPointF, QRectF, QTimer
from PySide6.QtGui import (
    QPainter, QColor, QBrush, QPen, QFont, QFontMetrics,
)


class BeadState(Enum):
    """Состояние бусины."""
    UNAVAILABLE = "unavailable"    # Серая — этап недоступен
    AVAILABLE = "available"        # Белая обводка — можно начать
    IN_PROGRESS = "in_progress"    # Оранжевая — выполняется
    COMPLETED = "completed"        # Зелёная — выполнено
    ERROR = "error"                # Красная — ошибка


@dataclass
class BeadInfo:
    """Информация о бусине."""
    label: str
    state: BeadState = BeadState.UNAVAILABLE
    date: str = ""


# Цвета
_COLORS = {
    BeadState.UNAVAILABLE: QColor(100, 100, 100),
    BeadState.AVAILABLE: QColor(180, 180, 180),
    BeadState.IN_PROGRESS: QColor(255, 165, 0),
    BeadState.COMPLETED: QColor(76, 175, 80),
    BeadState.ERROR: QColor(244, 67, 54),
}

_LINE_COLOR = QColor(80, 80, 80)
_LINE_COLOR_DONE = QColor(76, 175, 80, 120)
_TEXT_COLOR = QColor(220, 220, 220)
_DATE_COLOR = QColor(150, 150, 150)


class ProgressBeads(QWidget):
    """
    Виджет прогресс-бусин.

    orientation="vertical" — цепочка сверху вниз (по умолчанию),
    бусины выравниваются по Y-центрам якорных виджетов.
    orientation="horizontal" — цепочка слева направо, по X-центрам.
    """

    LINE_THICKNESS = 3
    PADDING = 12
    LABEL_SPACING = 8
    DATE_SPACING = 2

    def __init__(self, parent=None, orientation: str = "vertical"):
        super().__init__(parent)

        self._beads: List[BeadInfo] = []
        self._anchor_widgets: List[QWidget] = []
        self._sweep_angle = 0
        self._orientation = orientation
        self._radius = 16
        self._show_labels = (orientation == "horizontal")

        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._animate_sweep)
        self._pulse_timer.start(40)

        if orientation == "vertical":
            self.setMinimumWidth(2 * self._radius + 2 * self.PADDING)
        else:
            self.setMinimumHeight(100)

    # === Public API ===

    def set_beads(self, beads: List[BeadInfo]):
        self._beads = beads
        self.update()

    def set_anchor_widgets(self, widgets: List[QWidget]):
        self._anchor_widgets = widgets
        self.update()

    def set_radius(self, r: int):
        """Адаптивный радиус бусины (px)."""
        r = max(6, int(r))
        if r != self._radius:
            self._radius = r
            if self._orientation == "vertical":
                self.setFixedWidth(2 * r + 2 * self.PADDING)
            self.update()

    def set_state(self, index: int, state: BeadState, date: str = ""):
        if 0 <= index < len(self._beads):
            self._beads[index].state = state
            if date:
                self._beads[index].date = date
            self.update()

    def get_state(self, index: int) -> Optional[BeadState]:
        if 0 <= index < len(self._beads):
            return self._beads[index].state
        return None

    # === Animation ===

    def _animate_sweep(self):
        self._sweep_angle = (self._sweep_angle + 3) % 360
        if any(b.state == BeadState.IN_PROGRESS for b in self._beads):
            self.update()

    # === Positioning ===

    def _get_bead_positions(self) -> List[QPointF]:
        n = len(self._beads)
        if n == 0:
            return []
        r = self._radius

        if self._orientation == "vertical":
            x_center = self.width() / 2
            if self._anchor_widgets and len(self._anchor_widgets) == n:
                positions = []
                for widget in self._anchor_widgets:
                    center = widget.mapTo(self.parent(), widget.rect().center())
                    local = self.mapFrom(self.parent(), center)
                    positions.append(QPointF(x_center, local.y()))
                return positions
            margin = r + 16
            available = self.height() - 2 * margin
            step = available / (n - 1) if n > 1 else 0
            return [QPointF(x_center, margin + i * step) for i in range(n)]

        # horizontal
        y_center = self.PADDING + r
        if self._anchor_widgets and len(self._anchor_widgets) == n:
            positions = []
            for widget in self._anchor_widgets:
                center = widget.mapTo(self.parent(), widget.rect().center())
                local = self.mapFrom(self.parent(), center)
                positions.append(QPointF(local.x(), y_center))
            return positions
        margin = r + 30
        available_width = self.width() - 2 * margin
        step = available_width / (n - 1) if n > 1 else 0
        return [QPointF(margin + i * step, y_center) for i in range(n)]

    # === Painting ===

    def paintEvent(self, event):
        if not self._beads:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        r = self._radius
        positions = self._get_bead_positions()
        n = len(self._beads)
        vertical = (self._orientation == "vertical")

        # Линии между бусинами
        for i in range(n - 1):
            p1 = positions[i]
            p2 = positions[i + 1]
            if (self._beads[i].state == BeadState.COMPLETED
                    and self._beads[i + 1].state == BeadState.COMPLETED):
                pen = QPen(_LINE_COLOR_DONE, self.LINE_THICKNESS)
            else:
                pen = QPen(_LINE_COLOR, self.LINE_THICKNESS)
            painter.setPen(pen)
            if vertical:
                painter.drawLine(QPointF(p1.x(), p1.y() + r),
                                 QPointF(p2.x(), p2.y() - r))
            else:
                painter.drawLine(QPointF(p1.x() + r, p1.y()),
                                 QPointF(p2.x() - r, p2.y()))

        # Бусины
        label_font = QFont(self.font().family(), 9)
        date_font = QFont(self.font().family(), 7)
        label_fm = QFontMetrics(label_font)
        date_fm = QFontMetrics(date_font)

        for i, (bead, pos) in enumerate(zip(self._beads, positions)):
            color = QColor(_COLORS[bead.state])

            if bead.state == BeadState.IN_PROGRESS:
                bg_color = QColor(80, 50, 0)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(bg_color))
                painter.drawEllipse(pos, r, r)

                fill_color = QColor(_COLORS[BeadState.IN_PROGRESS])
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(fill_color))
                rect = QRectF(pos.x() - r, pos.y() - r, r * 2, r * 2)
                start = 90 * 16
                span = -int(self._sweep_angle * 16)
                painter.drawPie(rect, start, span)

                painter.setPen(QPen(fill_color.darker(130), 1.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(pos, r, r)
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(color))
                painter.drawEllipse(pos, r, r)

            if bead.state == BeadState.AVAILABLE:
                painter.setPen(QPen(QColor(220, 220, 220), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(pos, r, r)

            if bead.state == BeadState.COMPLETED:
                painter.setPen(QPen(QColor(255, 255, 255), 2.5))
                cx, cy = pos.x(), pos.y()
                k = r / 18.0
                painter.drawLine(QPointF(cx - 6 * k, cy),
                                 QPointF(cx - 2 * k, cy + 5 * k))
                painter.drawLine(QPointF(cx - 2 * k, cy + 5 * k),
                                 QPointF(cx + 7 * k, cy - 4 * k))

            if self._show_labels:
                painter.setFont(label_font)
                painter.setPen(QPen(_TEXT_COLOR))
                text_width = label_fm.horizontalAdvance(bead.label)
                text_x = pos.x() - text_width / 2
                text_y = pos.y() + r + self.LABEL_SPACING + label_fm.ascent()
                painter.drawText(QPointF(text_x, text_y), bead.label)

                if bead.date:
                    painter.setFont(date_font)
                    painter.setPen(QPen(_DATE_COLOR))
                    date_width = date_fm.horizontalAdvance(bead.date)
                    date_x = pos.x() - date_width / 2
                    date_y = text_y + self.DATE_SPACING + date_fm.ascent()
                    painter.drawText(QPointF(date_x, date_y), bead.date)

        painter.end()

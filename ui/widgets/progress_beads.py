"""
Progress Beads — визуальный индикатор прогресса пайплайна P&ID.

Цепочка бусин: ●—●—●—●—●—●—●—●
Состояния: выполнено (зелёная), текущий (оранжевая пульсирующая),
           доступно (белая обводка), недоступно (серая), ошибка (красная).

Поддерживает выравнивание бусин по X-позициям внешних виджетов
(например, кнопок расположенных под бусинами).
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

    Отображает горизонтальную цепочку бусин с подписями.
    Может выравнивать бусины по X-позициям внешних виджетов
    через set_anchor_widgets().
    """

    BEAD_RADIUS = 18
    LINE_THICKNESS = 3
    VERTICAL_PADDING = 12
    LABEL_SPACING = 8
    DATE_SPACING = 2

    def __init__(self, parent=None):
        super().__init__(parent)

        self._beads: List[BeadInfo] = []
        self._anchor_widgets: List[QWidget] = []
        self._sweep_angle = 0  # 0..360 для sweep animation

        # Анимация заполнения для IN_PROGRESS
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._animate_sweep)
        self._pulse_timer.start(40)

        self.setMinimumHeight(100)

    # === Public API ===

    def set_beads(self, beads: List[BeadInfo]):
        """Установить список бусин."""
        self._beads = beads
        self.update()

    def set_anchor_widgets(self, widgets: List[QWidget]):
        self._anchor_widgets = widgets
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
        """Вычислить позиции бусин."""
        n = len(self._beads)
        if n == 0:
            return []

        r = self.BEAD_RADIUS
        y_center = self.VERTICAL_PADDING + r

        # Если есть якорные виджеты — выравниваем по ним
        if self._anchor_widgets and len(self._anchor_widgets) == n:
            positions = []
            for widget in self._anchor_widgets:
                # Центр виджета в координатах ProgressBeads
                center = widget.mapTo(self.parent(), widget.rect().center())
                local = self.mapFrom(self.parent(), center)
                positions.append(QPointF(local.x(), y_center))
            return positions

        # Fallback — равномерное распределение
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

        r = self.BEAD_RADIUS
        positions = self._get_bead_positions()
        n = len(self._beads)

        # Рисуем линии между бусинами
        for i in range(n - 1):
            p1 = positions[i]
            p2 = positions[i + 1]

            if (self._beads[i].state == BeadState.COMPLETED
                    and self._beads[i + 1].state == BeadState.COMPLETED):
                pen = QPen(_LINE_COLOR_DONE, self.LINE_THICKNESS)
            else:
                pen = QPen(_LINE_COLOR, self.LINE_THICKNESS)

            painter.setPen(pen)
            painter.drawLine(
                QPointF(p1.x() + r, p1.y()),
                QPointF(p2.x() - r, p2.y()),
            )

        # Рисуем бусины
        label_font = QFont("Segoe UI", 9)
        date_font = QFont("Segoe UI", 7)
        label_fm = QFontMetrics(label_font)
        date_fm = QFontMetrics(date_font)

        for i, (bead, pos) in enumerate(zip(self._beads, positions)):
            color = QColor(_COLORS[bead.state])

            if bead.state == BeadState.IN_PROGRESS:
                # Фон: тёмно-оранжевый круг
                bg_color = QColor(80, 50, 0)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(bg_color))
                painter.drawEllipse(pos, r, r)

                # Заполнение: sweep (заливка сектором)
                fill_color = QColor(_COLORS[BeadState.IN_PROGRESS])
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(fill_color))
                rect = QRectF(pos.x() - r, pos.y() - r, r * 2, r * 2)
                # drawPie: startAngle, spanAngle в 1/16 градуса
                start = 90 * 16  # начало сверху
                span = -int(self._sweep_angle * 16)  # по часовой
                painter.drawPie(rect, start, span)

                # Обводка
                painter.setPen(QPen(fill_color.darker(130), 1.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(pos, r, r)
            else:
                # Обычная заливка
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(color))
                painter.drawEllipse(pos, r, r)

            # Обводка для AVAILABLE
            if bead.state == BeadState.AVAILABLE:
                painter.setPen(QPen(QColor(220, 220, 220), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(pos, r, r)

            # Галочка для COMPLETED
            if bead.state == BeadState.COMPLETED:
                painter.setPen(QPen(QColor(255, 255, 255), 2.5))
                cx, cy = pos.x(), pos.y()
                painter.drawLine(
                    QPointF(cx - 6, cy),
                    QPointF(cx - 2, cy + 5),
                )
                painter.drawLine(
                    QPointF(cx - 2, cy + 5),
                    QPointF(cx + 7, cy - 4),
                )

            # Подпись снизу
            painter.setFont(label_font)
            painter.setPen(QPen(_TEXT_COLOR))
            text_width = label_fm.horizontalAdvance(bead.label)
            text_x = pos.x() - text_width / 2
            text_y = pos.y() + r + self.LABEL_SPACING + label_fm.ascent()
            painter.drawText(QPointF(text_x, text_y), bead.label)

            # Дата под подписью
            if bead.date:
                painter.setFont(date_font)
                painter.setPen(QPen(_DATE_COLOR))
                date_width = date_fm.horizontalAdvance(bead.date)
                date_x = pos.x() - date_width / 2
                date_y = text_y + self.DATE_SPACING + date_fm.ascent()
                painter.drawText(QPointF(date_x, date_y), bead.date)

        painter.end()

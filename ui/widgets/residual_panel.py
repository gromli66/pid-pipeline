"""Панель «Очаги» — список остаточных дефектов раскладки (Э12).

Левая шторка вкладки «Ручная правка» (механика — как у ObjectResizePanel).
Панель «глупая»: список строк ей передаёт вкладка через set_spots(), клик по
строке дёргает колбэк on_jump(index) — переход-зум делает редактор
(ResidualLayerMixin.focus_residual).
"""

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRect
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout,
)

PANEL_WIDTH = 300


class ResidualPanel(QFrame):
    """Левая шторка со списком очагов остатка."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("residualPanel")
        self.setStyleSheet(
            "#residualPanel { background: #2b2b2b; border-right: 1px solid #444; }"
            "QLabel { color: #ddd; font-size: 12px; }"
            "QListWidget { background: #3a3a3a; color: #eee; border: 1px solid #555;"
            " border-radius: 3px; font-size: 11px; }"
            "QListWidget::item { padding: 4px; }"
            "QListWidget::item:selected { background: #16a085; color: #fff; }"
        )

        self.on_jump = None        # (index: int) -> None
        self.on_visibility = None  # (shown: bool) -> None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Остаток раскладки")
        title.setStyleSheet("color: #fff; font-weight: bold; font-size: 13px;")
        header.addWidget(title)
        header.addStretch()
        btn_close = QPushButton("✕")
        btn_close.setFixedSize(22, 22)
        btn_close.setStyleSheet(
            "QPushButton { border: none; color: #aaa; }"
            "QPushButton:hover { color: #fff; }"
        )
        btn_close.clicked.connect(self.hide_panel)
        header.addWidget(btn_close)
        root.addLayout(header)

        hint = QLabel("Клик по строке — переход к очагу на холсте")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 10px;")
        root.addWidget(hint)

        self._list = QListWidget()
        self._list.itemClicked.connect(self._jump)
        root.addWidget(self._list, stretch=1)

        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._shown = False
        self._top = 0
        self._height = None
        self.hide()

    # ───────────────────────── данные ─────────────────────────

    def set_spots(self, rows):
        """rows: [(номер, вид, описание)] из ResidualLayerMixin.residual_spots()."""
        self._list.clear()
        for num, kind, descr in rows:
            QListWidgetItem(f"№{num} · {kind}\n{descr}", self._list)

    def _jump(self, item):
        if callable(self.on_jump):
            self.on_jump(self._list.row(item))

    # ─────────────────── геометрия и показ (как у ObjectResizePanel) ───────

    def set_bounds(self, top: int, height: int):
        self._top = max(0, int(top))
        self._height = int(height) if (height and height > 50) else None

    def _rect(self, shown: bool) -> QRect:
        p = self.parentWidget()
        full_h = p.height() if p else 600
        top = self._top
        h = self._height if self._height else max(50, full_h - top)
        x = 0 if shown else -PANEL_WIDTH
        return QRect(x, top, PANEL_WIDTH, h)

    def show_panel(self):
        try:
            self._anim.finished.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.setGeometry(self._rect(False))
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(self._rect(False))
        self._anim.setEndValue(self._rect(True))
        self._anim.start()
        self._shown = True
        if callable(self.on_visibility):
            self.on_visibility(True)

    def hide_panel(self):
        self._anim.stop()
        self._anim.setStartValue(self.geometry())
        self._anim.setEndValue(self._rect(False))
        try:
            self._anim.finished.disconnect()
        except (TypeError, RuntimeError):
            pass
        self._anim.finished.connect(self.hide)
        self._anim.start()
        self._shown = False
        if callable(self.on_visibility):
            self.on_visibility(False)

    @property
    def is_shown(self) -> bool:
        return self._shown

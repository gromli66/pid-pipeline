"""
Панель оформления вкладки — выезжающая слева «шторка» с настройками вида.

Состав:
- AppearancePanel — сам виджет-шторка (анимированный выезд от левого края).
- AppearanceMixin — подмешивается во вкладку: создаёт панель и кнопку-тоггл ⚙,
  хранит/читает настройки по диаграмме (UISettings, ключ = uid).

Фаза 1: единственный общий регулятор — «Затемнение фона». Цвета/прозрачность
для конкретных вкладок добавляются переопределением _build_appearance_controls.
"""

from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QPushButton,
    QColorDialog,
)
from PySide6.QtGui import QColor
from PySide6.QtCore import Qt, QPropertyAnimation, QRect, QEasingCurve

from ui.services.ui_settings import UISettings

PANEL_WIDTH = 280


class AppearancePanel(QFrame):
    """Левая шторка с контролами оформления."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("appearancePanel")
        self.setStyleSheet(
            "#appearancePanel { background: #2b2b2b; border-right: 1px solid #444; }"
            "QLabel { color: #ddd; font-size: 12px; }"
        )

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(12, 10, 12, 10)
        self._root.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Оформление")
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
        self._root.addLayout(header)

        self._content = QVBoxLayout()
        self._content.setSpacing(12)
        self._root.addLayout(self._content)
        self._root.addStretch()

        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._shown = False
        # Вертикальные границы (область редактора, без тулбара и нижней полосы)
        self._top = 0
        self._height = None  # None → до низа родителя
        self.hide()

    def set_bounds(self, top: int, height: int):
        """Ограничить панель по вертикали областью редактора."""
        self._top = max(0, int(top))
        self._height = int(height) if (height and height > 50) else None

    # ---- построение контролов ----

    def add_slider(self, label: str, minimum: int, maximum: int,
                   value: int, on_change, suffix: str = "%") -> QSlider:
        """Добавить подписанный ползунок. on_change(v:int) вызывается при изменении."""
        box = QVBoxLayout()
        box.setSpacing(2)
        lbl = QLabel(f"{label}: {value}{suffix}")
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(value)

        def _changed(v: int):
            lbl.setText(f"{label}: {v}{suffix}")
            on_change(v)

        slider.valueChanged.connect(_changed)
        box.addWidget(lbl)
        box.addWidget(slider)
        self._content.addLayout(box)
        return slider

    def add_color(self, label: str, initial: QColor, on_change) -> QPushButton:
        """Добавить строку выбора цвета (swatch + QColorDialog)."""
        box = QHBoxLayout()
        lbl = QLabel(label)
        btn = QPushButton()
        btn.setFixedSize(44, 22)
        cur = {"c": QColor(initial)}

        def _swatch(c: QColor):
            btn.setStyleSheet(
                f"background: {c.name()}; border: 1px solid #888; border-radius: 3px;"
            )

        def _pick():
            c = QColorDialog.getColor(cur["c"], self, label)
            if c.isValid():
                cur["c"] = c
                _swatch(c)
                on_change(c)

        _swatch(cur["c"])
        btn.clicked.connect(_pick)
        box.addWidget(lbl)
        box.addStretch()
        box.addWidget(btn)
        self._content.addLayout(box)
        return btn

    def add_reset_button(self, on_reset):
        """Добавить внизу кнопку «Сбросить к исходным» (один раз)."""
        if getattr(self, "_reset_btn", None) is not None:
            return
        self._reset_btn = QPushButton("Сбросить к исходным")
        self._reset_btn.setStyleSheet(
            "QPushButton { color: #ddd; background: #444; border-radius: 3px; padding: 5px; }"
            "QPushButton:hover { background: #555; }"
        )
        self._reset_btn.clicked.connect(on_reset)
        self._root.addWidget(self._reset_btn)

    def clear_content(self):
        """Удалить все контролы (для пересборки после сброса)."""
        self._clear_layout(self._content)

    def _clear_layout(self, lay):
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            else:
                child = item.layout()
                if child is not None:
                    self._clear_layout(child)

    # ---- показ/скрытие с анимацией ----

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

    def toggle(self):
        if self._shown:
            self.hide_panel()
        else:
            self.show_panel()


class AppearanceMixin:
    """Подмешивается во вкладку (QWidget). Требует self.uid.

    Потомок переопределяет:
      _appearance_editor() -> редактор с сеттерами оформления (или None)
      _build_appearance_controls(panel) -> наполнить панель контролами
    и вызывает self.apply_saved_appearance() после готовности редактора.
    """

    _appearance_panel = None

    def toggle_appearance_panel(self):
        if self._appearance_panel is None:
            self._appearance_panel = AppearancePanel(self)
            self._build_appearance_controls(self._appearance_panel)
            self._appearance_panel.add_reset_button(self.reset_appearance)
        self._update_panel_bounds()
        self._appearance_panel.toggle()

    def reset_appearance(self):
        """Очистить настройки оформления диаграммы и вернуть исходный вид."""
        UISettings.instance().clear_appearance(self.uid)
        self.apply_default_appearance()
        if self._appearance_panel is not None:
            self._appearance_panel.clear_content()
            self._build_appearance_controls(self._appearance_panel)

    def apply_default_appearance(self):
        """Вернуть редактору исходные значения оформления (переопределяется)."""
        ed = self._appearance_editor()
        if ed is not None and hasattr(ed, "set_background_darkness"):
            ed.set_background_darkness(0.60)

    def _update_panel_bounds(self):
        """Ограничить панель по вертикали областью редактора (ниже тулбара,
        выше нижней полосы)."""
        if self._appearance_panel is None:
            return
        ed = self._appearance_editor()
        if ed is None:
            return
        try:
            geo = ed.geometry()  # координаты редактора внутри вкладки
            self._appearance_panel.set_bounds(geo.top(), geo.height())
        except Exception:
            pass

    # ---- хелперы для потомков ----

    def _appearance_editor(self):
        return None

    def _build_appearance_controls(self, panel: AppearancePanel):
        self._add_bg_darkness_slider(panel)

    def _add_bg_darkness_slider(self, panel: AppearancePanel):
        s = UISettings.instance()
        cur = int(round(s.get_appearance(self.uid, "bg_darkness", 60.0)))

        def on_change(v: int):
            s.set_appearance(self.uid, "bg_darkness", float(v))
            ed = self._appearance_editor()
            if ed is not None and hasattr(ed, "set_background_darkness"):
                ed.set_background_darkness(v / 100.0)

        panel.add_slider("Затемнение фона", 0, 95, cur, on_change)

    def _add_color_setting(self, panel: AppearancePanel, label: str, key: str,
                           default_color: QColor, apply_fn):
        s = UISettings.instance()
        cur = self._saved_color(key, default_color)

        def on_change(c: QColor):
            s.set_appearance(self.uid, key, c.name())
            apply_fn(c)

        panel.add_color(label, cur, on_change)

    def _add_pct_setting(self, panel: AppearancePanel, label: str, key: str,
                         default_pct: float, apply_fn, lo: int = 0, hi: int = 100):
        s = UISettings.instance()
        cur = int(round(self._saved_pct(key, default_pct)))

        def on_change(v: int):
            s.set_appearance(self.uid, key, float(v))
            apply_fn(v)

        panel.add_slider(label, lo, hi, cur, on_change)

    def _saved_color(self, key: str, default_color: QColor) -> QColor:
        raw = UISettings.instance().get_appearance(self.uid, key, default_color.name())
        c = QColor(raw)
        return c if c.isValid() else QColor(default_color)

    def _saved_pct(self, key: str, default_pct: float) -> float:
        return float(UISettings.instance().get_appearance(self.uid, key, float(default_pct)))

    def _apply_saved_color(self, key: str, default_color: QColor, apply_fn):
        """Применить сохранённый цвет, только если пользователь его задавал."""
        if UISettings.instance().has_appearance(self.uid, key):
            apply_fn(self._saved_color(key, default_color))

    def _apply_saved_pct(self, key: str, default_pct: float, apply_fn):
        """Применить сохранённый % , только если пользователь его задавал."""
        if UISettings.instance().has_appearance(self.uid, key):
            apply_fn(self._saved_pct(key, default_pct))

    def apply_saved_appearance(self):
        """Применить сохранённые настройки оформления к редактору при загрузке."""
        ed = self._appearance_editor()
        if ed is None:
            return
        s = UISettings.instance()
        if hasattr(ed, "set_background_darkness"):
            d = s.get_appearance(self.uid, "bg_darkness", 60.0)
            ed.set_background_darkness(float(d) / 100.0)

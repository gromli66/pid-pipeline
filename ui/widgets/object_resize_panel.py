"""
Панель «Размер объектов» — выезжающая слева шторка для массового изменения
размеров узлов одного класса (вкладка финального редактора графа).

Состав:
- выбор класса (присутствующего на схеме);
- кнопки «Выбрать все» / «Выбрать 1» (стартовый набор);
- контролы размера, зависящие от геометрии текущего набора:
    • боксы   → поля Ширина / Высота (по умолчанию — медиана по набору);
    • полигоны → бегунок масштаба (форма сохраняется);
    • смешанный набор → блок с фильтрами «оставить только боксы / полигоны»;
- кнопка «Применить к выбранным».

Панель «глупая»: всё состояние и отрисовка жёлтых рамок живут в редакторе,
панель лишь дёргает колбэки (on_*) и отображает то, что ей передали через
set_classes() / set_state().
"""

from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QSpinBox, QSlider, QWidget,
)
from PySide6.QtCore import Qt, QPropertyAnimation, QRect, QEasingCurve

PANEL_WIDTH = 280


class ObjectResizePanel(QFrame):
    """Левая шторка изменения размеров объектов класса."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("objectResizePanel")
        self.setStyleSheet(
            "#objectResizePanel { background: #2b2b2b; border-right: 1px solid #444; }"
            "QLabel { color: #ddd; font-size: 12px; }"
            "QComboBox, QSpinBox { background: #3a3a3a; color: #eee; border: 1px solid #555;"
            " border-radius: 3px; padding: 2px 4px; }"
            # выпадающий список класса — тёмный фон и читаемый текст
            "QComboBox QAbstractItemView { background: #3a3a3a; color: #eee;"
            " border: 1px solid #555; selection-background-color: #16a085;"
            " selection-color: #fff; outline: 0; }"
        )

        # колбэки (назначаются извне, обычно вкладкой)
        self.on_class_changed = None    # (name: str) -> None
        self.on_select_all = None       # () -> None
        self.on_select_one = None       # () -> None
        self.on_filter = None           # (kind: 'box'|'poly') -> None
        self.on_apply = None            # (width:int|None, height:int|None, scale:float|None) -> None
        self.on_preview = None          # (width, height, scale) -> None — живое превью

        self._populating = False
        self._updating = False          # подавляет превью при программном set_state

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(10)

        # ── заголовок ──
        header = QHBoxLayout()
        title = QLabel("Размер объектов")
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

        # ── выбор класса ──
        root.addWidget(self._lbl("Класс:"))
        self._class_combo = QComboBox()
        self._class_combo.currentTextChanged.connect(self._class_changed)
        root.addWidget(self._class_combo)

        # ── выбрать все / 1 ──
        sel_row = QHBoxLayout()
        self._btn_all = QPushButton("Выбрать все")
        self._btn_all.clicked.connect(lambda: self._cb(self.on_select_all))
        self._btn_one = QPushButton("Выбрать 1")
        self._btn_one.clicked.connect(lambda: self._cb(self.on_select_one))
        for b in (self._btn_all, self._btn_one):
            b.setStyleSheet(
                "QPushButton { color: #ddd; background: #444; border-radius: 3px; padding: 5px; }"
                "QPushButton:hover { background: #555; }"
            )
            sel_row.addWidget(b)
        root.addLayout(sel_row)

        hint = QLabel("Ctrl+ЛКМ — добавить · Ctrl+ПКМ — убрать · Shift+рамка — добавить группу")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 10px;")
        root.addWidget(hint)

        # ── статус набора ──
        self._info = QLabel("Набор: —")
        self._info.setWordWrap(True)
        self._info.setStyleSheet("color: #f1c40f; font-size: 11px;")
        root.addWidget(self._info)

        # ── контролы: боксы (Ш/В) ──
        self._box_ctrl = QWidget()
        bl = QVBoxLayout(self._box_ctrl)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        wr = QHBoxLayout()
        wr.addWidget(self._lbl("Ширина:"))
        self._spin_w = QSpinBox()
        self._spin_w.setRange(1, 100000)
        self._spin_w.setSuffix(" px")
        self._spin_w.valueChanged.connect(self._size_preview)
        wr.addWidget(self._spin_w)
        bl.addLayout(wr)
        hr = QHBoxLayout()
        hr.addWidget(self._lbl("Высота:"))
        self._spin_h = QSpinBox()
        self._spin_h.setRange(1, 100000)
        self._spin_h.setSuffix(" px")
        self._spin_h.valueChanged.connect(self._size_preview)
        hr.addWidget(self._spin_h)
        bl.addLayout(hr)
        root.addWidget(self._box_ctrl)

        # ── контролы: полигоны (масштаб) ──
        self._poly_ctrl = QWidget()
        pl = QVBoxLayout(self._poly_ctrl)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(4)
        self._scale_lbl = QLabel("Масштаб: 100%")
        pl.addWidget(self._scale_lbl)
        self._scale = QSlider(Qt.Orientation.Horizontal)
        self._scale.setRange(20, 400)   # 20%..400%
        self._scale.setValue(100)
        self._scale.valueChanged.connect(self._scale_preview)
        pl.addWidget(self._scale)
        root.addWidget(self._poly_ctrl)

        # ── контролы: смешанный набор ──
        self._mixed_ctrl = QWidget()
        ml = QVBoxLayout(self._mixed_ctrl)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(6)
        warn = QLabel("В наборе и боксы, и полигоны. Изменять можно только один "
                      "тип за раз — оставьте что-то одно:")
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #e67e22; font-size: 11px;")
        ml.addWidget(warn)
        self._btn_only_box = QPushButton("Оставить только боксы")
        self._btn_only_box.clicked.connect(lambda: self._cb(self.on_filter, "box"))
        self._btn_only_poly = QPushButton("Оставить только полигоны")
        self._btn_only_poly.clicked.connect(lambda: self._cb(self.on_filter, "poly"))
        for b in (self._btn_only_box, self._btn_only_poly):
            b.setStyleSheet(
                "QPushButton { color: #ddd; background: #444; border-radius: 3px; padding: 5px; }"
                "QPushButton:hover { background: #555; }"
            )
            ml.addWidget(b)
        root.addWidget(self._mixed_ctrl)

        root.addStretch()

        # ── применить ──
        self._apply_btn = QPushButton("Применить к выбранным")
        self._apply_btn.setStyleSheet(
            "QPushButton { background-color: #FF9800; color: white; font-weight: bold;"
            " border-radius: 3px; padding: 7px; }"
            "QPushButton:hover { background-color: #F57C00; }"
            "QPushButton:disabled { background-color: #5a4a30; color: #aaa; }"
        )
        self._apply_btn.clicked.connect(self._apply)
        root.addWidget(self._apply_btn)

        # ── анимация выезда ──
        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._shown = False
        self._top = 0
        self._height = None
        self.hide()

    # ───────────────────────── helpers ─────────────────────────

    @staticmethod
    def _lbl(text: str) -> QLabel:
        return QLabel(text)

    @staticmethod
    def _cb(fn, *args):
        if callable(fn):
            fn(*args)

    def _class_changed(self, name: str):
        if self._populating:
            return
        if name and callable(self.on_class_changed):
            self.on_class_changed(name)

    def _size_preview(self, _v=None):
        """Живое превью размеров боксов (Ширина/Высота)."""
        if self._updating or not callable(self.on_preview):
            return
        self.on_preview(int(self._spin_w.value()), int(self._spin_h.value()), None)

    def _scale_preview(self, v: int):
        """Живое превью масштаба полигонов."""
        self._scale_lbl.setText(f"Масштаб: {v}%")
        if self._updating or not callable(self.on_preview):
            return
        self.on_preview(None, None, v / 100.0)

    def _apply(self):
        if not callable(self.on_apply):
            return
        if self._poly_ctrl.isVisible():
            self.on_apply(None, None, self._scale.value() / 100.0)
        elif self._box_ctrl.isVisible():
            self.on_apply(int(self._spin_w.value()), int(self._spin_h.value()), None)

    # ───────────────────── обновление состояния ─────────────────────

    def set_classes(self, names, current=None):
        """Заполнить выпадающий список классов."""
        self._populating = True
        self._class_combo.clear()
        self._class_combo.addItems(list(names))
        if current and current in names:
            self._class_combo.setCurrentText(current)
        self._populating = False

    def set_state(self, kind: str, count: int,
                  median_w=None, median_h=None):
        """Обновить контролы под геометрию набора.

        kind: 'box' | 'poly' | 'mixed' | 'empty'
        """
        is_box = (kind == "box")
        is_poly = (kind == "poly")
        is_mixed = (kind == "mixed")

        self._box_ctrl.setVisible(is_box)
        self._poly_ctrl.setVisible(is_poly)
        self._mixed_ctrl.setVisible(is_mixed)
        self._apply_btn.setEnabled(is_box or is_poly)

        # Программное обновление контролов не должно запускать превью.
        self._updating = True
        if is_box:
            if median_w:
                self._spin_w.setValue(int(median_w))
            if median_h:
                self._spin_h.setValue(int(median_h))
            self._info.setText(f"Набор: {count} экз. · боксы")
        elif is_poly:
            self._scale.setValue(100)
            self._scale_lbl.setText("Масштаб: 100%")
            self._info.setText(f"Набор: {count} экз. · полигоны")
        elif is_mixed:
            self._info.setText(f"Набор: {count} экз. · смешанный")
        else:
            self._info.setText("Набор: пусто — кликните по экземплярам")
        self._updating = False

    # ───────────────────── показ/скрытие (анимация) ─────────────────────

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

    @property
    def is_shown(self) -> bool:
        return self._shown

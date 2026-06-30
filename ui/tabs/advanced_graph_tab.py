
"""
Advanced Graph Tab — вкладка продвинутого редактора графа P&ID.

Инструменты SimpleGraphTab + routing, optimise, drag, multi-select,
waypoints, batch delete, auto-fix, perp stats.

Используется на второй фазе двухфазного флоу val_graph:
  SimpleGraphTab → ✅ → AdvancedGraphTab → ✅ → complete_graph_validation()
"""

import logging

from PySide6.QtWidgets import (
    QHBoxLayout, QVBoxLayout, QWidget, QPushButton, QLabel,
    QSpinBox, QMenu, QButtonGroup,
)
from PySide6.QtCore import Slot, Qt
from PySide6.QtGui import QColor, QPixmap, QIcon

from ui.services.api_client import APIClient
from ui.editors.advanced_graph_editor import AdvancedGraphEditor
from ui.editors.base_graph_editor import BaseGraphEditor
from ui.tabs.simple_graph_tab import SimpleGraphTab

logger = logging.getLogger(__name__)


class AdvancedGraphTab(SimpleGraphTab):
    """Вкладка продвинутого редактора графа.

    Наследует SimpleGraphTab (добавляет к его тулбару).
    Редактор: AdvancedGraphEditor.
    """

    def __init__(
        self,
        diagram_uid: str,
        diagram_name: str,
        api_client: APIClient,
        parent=None,
    ):
        super().__init__(diagram_uid, diagram_name, api_client, parent)

    # =================================================================
    # BaseGraphTab interface
    # =================================================================

    def _create_editor(self) -> BaseGraphEditor:
        return AdvancedGraphEditor()

    def _setup_toolbar(self, toolbar: QHBoxLayout):
        # Сначала Simple-кнопки
        super()._setup_toolbar(toolbar)

        self._add_separator(toolbar)

        # --- optimize_edge ---
        self.btn_optimize_edge = QPushButton("Оптимизировать")
        self.btn_optimize_edge.setCheckable(True)
        self.btn_optimize_edge.setToolTip(
            "Выравнивание одного ребра под прямой угол.\n"
            "Ctrl+ЛКМ по оранжевому (неперпендикулярному) ребру — выровнять его под 90°.\n"
            "Повторное нажатие кнопки или Esc — выйти из режима."
        )
        self.btn_optimize_edge.setStyleSheet(
            "QPushButton:checked { background-color: #9C27B0; color: white; }"
        )
        self.btn_optimize_edge.clicked.connect(lambda: self._set_mode("optimize_edge"))
        self.mode_group.addButton(self.btn_optimize_edge)
        toolbar.addWidget(self.btn_optimize_edge)

        # --- optimize_all (не переключатель) ---
        btn_optimize_all = QPushButton("Оптимизировать все")
        btn_optimize_all.setToolTip(
            "Выровнять под прямой угол сразу все неперпендикулярные рёбра."
        )
        btn_optimize_all.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_optimize_all.clicked.connect(self._optimize_all_edges)
        toolbar.addWidget(btn_optimize_all)

        self._add_separator(toolbar)

        # --- edit_waypoint ---
        self.btn_waypoints = QPushButton("Точки изгиба")
        self.btn_waypoints.setCheckable(True)
        self.btn_waypoints.setToolTip(
            "Изломы ребра (точки изгиба трубы).\n"
            "Ctrl+ЛКМ по сегменту ребра — добавить точку изгиба.\n"
            "Ctrl+ЛКМ по точке и тянуть — двигать её (примагничивание к сетке).\n"
            "Маркеры точек видны только в этом режиме."
        )
        self.btn_waypoints.setStyleSheet(
            "QPushButton:checked { background-color: #00BCD4; color: white; }"
        )
        self.btn_waypoints.clicked.connect(lambda: self._set_mode("edit_waypoint"))
        self.mode_group.addButton(self.btn_waypoints)
        toolbar.addWidget(self.btn_waypoints)

        self._add_separator(toolbar)

        # --- auto_fix (не переключатель) ---
        btn_auto_fix = QPushButton("Авто-выравнивание")
        btn_auto_fix.setToolTip(
            "Автоматически выровнять цепочки узлов по горизонтали и вертикали "
            "и спрямить рёбра.\nCtrl+Z — отменить."
        )
        btn_auto_fix.setStyleSheet(
            "QPushButton { background-color: #FF9800; color: white; font-weight: bold; }"
            "QPushButton:hover { background-color: #F57C00; }"
        )
        btn_auto_fix.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_auto_fix.clicked.connect(self._auto_fix)
        toolbar.addWidget(btn_auto_fix)

        self._add_separator(toolbar)

        # --- Размер объектов (режим, открывает левую панель) ---
        self.btn_resize_objects = QPushButton("Размер объектов")
        self.btn_resize_objects.setCheckable(True)
        self.btn_resize_objects.setToolTip(
            "Массовое изменение размеров объектов одного класса.\n"
            "Слева открывается панель: выбор класса, набор экземпляров, "
            "ширина/высота (боксы) или масштаб (полигоны).\n"
            "Ctrl+ЛКМ — добавить экземпляр в набор, Ctrl+ПКМ — убрать, "
            "Shift+рамка — добавить группу."
        )
        self.btn_resize_objects.setStyleSheet(
            "QPushButton:checked { background-color: #16a085; color: white; }"
        )
        self.btn_resize_objects.clicked.connect(lambda: self._set_mode("resize_objects"))
        self.mode_group.addButton(self.btn_resize_objects)
        toolbar.addWidget(self.btn_resize_objects)

        self._add_separator(toolbar)

        # --- Режимы отображения/правки (взаимоисключающие) ---
        self.regime_group = QButtonGroup(self)
        self.regime_group.setExclusive(True)

        self.btn_regime_ocr = QPushButton("ОКР привязка")
        self.btn_regime_ocr.setCheckable(True)
        self.btn_regime_ocr.setChecked(True)
        self.btn_regime_ocr.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_regime_ocr.setToolTip(
            "Режим «ОКР привязка» (по умолчанию).\n"
            "Подсветка: зелёный/красный узел — есть/нет KKS, красное ребро — нет "
            "диаметра, KKS-подписи и подсказки.\n"
            "Двойной клик: по оборудованию — правка KKS, по ребру — правка диаметра.\n"
            "Перпендикулярность и кисть цвет/размер в этом режиме скрыты."
        )
        self.btn_regime_ocr.setStyleSheet(
            "QPushButton:checked { background-color: #2ecc71; color: white; }"
        )
        self.btn_regime_ocr.clicked.connect(lambda: self._set_regime("ocr"))
        self.regime_group.addButton(self.btn_regime_ocr)
        toolbar.addWidget(self.btn_regime_ocr)

        self.btn_regime_perp = QPushButton("Перпендикулярность")
        self.btn_regime_perp.setCheckable(True)
        self.btn_regime_perp.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_regime_perp.setToolTip(
            "Режим «Перпендикулярность».\n"
            "Подсветка: оранжевые неперпендикулярные рёбра + утолщение, "
            "метка перпендикулярности.\n"
            "Кнопки «Оптимизировать» / «Оптимизировать все» выравнивают рёбра под 90°.\n"
            "Подсветка KKS/диаметра и двойной клик отключены."
        )
        self.btn_regime_perp.setStyleSheet(
            "QPushButton:checked { background-color: #e67e22; color: white; }"
        )
        self.btn_regime_perp.clicked.connect(lambda: self._set_regime("perp"))
        self.regime_group.addButton(self.btn_regime_perp)
        toolbar.addWidget(self.btn_regime_perp)

        self.btn_regime_style = QPushButton("Размер и цвет")
        self.btn_regime_style.setCheckable(True)
        self.btn_regime_style.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_regime_style.setToolTip(
            "Режим «Размер и цвет».\n"
            "Рёбра рисуются своим цветом/размером (по умолчанию белые → чёрные в FXML).\n"
            "Снизу появляются выбор цвета (палитра) и размер.\n"
            "Ctrl+ЛКМ — применить; Shift+протяжка — обвести только рёбра; "
            "Ctrl+ПКМ — убрать из обводки; Ctrl+колесо — размер."
        )
        self.btn_regime_style.setStyleSheet(
            "QPushButton:checked { background-color: #3498db; color: white; }"
        )
        self.btn_regime_style.clicked.connect(lambda: self._set_regime("style"))
        self.regime_group.addButton(self.btn_regime_style)
        toolbar.addWidget(self.btn_regime_style)

        # --- perp stats (добавится после stretch из Base) ---
        # Создаём здесь, Base добавит stretch + stats_label + sep
        # Поэтому perp_stats_label добавляем через _on_editor_ready
        self.perp_stats_label = QLabel("Перпендикулярность: —")
        self.perp_stats_label.setStyleSheet("color: #aaa; font-size: 11px;")
        self.perp_stats_label.setVisible(False)  # видна только в режиме «Перпендикулярность»
        toolbar.addWidget(self.perp_stats_label)

    def _get_mode_button_map(self) -> dict:
        btn_map = super()._get_mode_button_map()
        btn_map.update({
            "optimize_edge": self.btn_optimize_edge,
            "edit_waypoint": self.btn_waypoints,
            "edit_edge_color": self.btn_edge_color,
            "edit_edge_size": self.btn_edge_size,
            "resize_objects": self.btn_resize_objects,
        })
        return btn_map

    # =================================================================
    # Панель «Размер объектов»
    # =================================================================

    def _ensure_resize_panel(self):
        if getattr(self, "_resize_panel", None) is None:
            from ui.widgets.object_resize_panel import ObjectResizePanel
            p = ObjectResizePanel(self)
            p.on_class_changed = lambda name: self._editor and self._editor.set_resize_class(name)
            p.on_select_all = lambda: self._editor and self._editor.resize_select_all()
            p.on_select_one = lambda: self._editor and self._editor.resize_select_one_mode()
            p.on_filter = lambda kind: self._editor and self._editor.resize_filter(kind)
            p.on_preview = lambda w, h, s: self._editor and self._editor.preview_resize(w, h, s)
            p.on_apply = lambda w, h, s: self._editor and self._editor.apply_resize(w, h, s)
            self._resize_panel = p
        return self._resize_panel

    def _update_resize_panel_bounds(self):
        p = getattr(self, "_resize_panel", None)
        if p is None or self._editor is None:
            return
        try:
            geo = self._editor.geometry()
            p.set_bounds(geo.top(), geo.height())
        except Exception:
            pass

    def _show_resize_panel(self, visible: bool):
        p = self._ensure_resize_panel()
        self._update_resize_panel_bounds()
        if visible:
            p.show_panel()
        else:
            p.hide_panel()

    def _resize_classes_cb(self, names, current):
        self._ensure_resize_panel().set_classes(names, current)

    def _resize_state_cb(self, kind, count, mw, mh):
        self._ensure_resize_panel().set_state(kind, count, mw, mh)

    # =================================================================
    # Второй ряд тулбара: изменение ребра (цвет / размер)
    # =================================================================

    # Пресеты палитры цветов рёбер.
    _EDGE_PALETTE = [
        ("#e74c3c", "Красный"), ("#e67e22", "Оранжевый"),
        ("#f1c40f", "Жёлтый"),  ("#2ecc71", "Зелёный"),
        ("#1abc9c", "Бирюзовый"), ("#3498db", "Синий"),
        ("#9b59b6", "Фиолетовый"), ("#34495e", "Тёмно-синий"),
        ("#7f8c8d", "Серый"), ("#333333", "Тёмный"),
        ("#000000", "Чёрный"), ("#ffffff", "Белый"),
    ]

    def _setup_secondary_toolbar(self, layout: QVBoxLayout):
        self._size_sync = False
        self._current_edge_color = self._EDGE_PALETTE[0][0]

        # Контейнер второго ряда — виден только в режиме «Размер и цвет».
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(8, 0, 8, 4)
        row.setSpacing(8)

        title = QLabel("Изменение ребра:")
        title.setStyleSheet("color: #aaa; font-weight: bold;")
        title.setToolTip(
            "Отдельный режим правки рёбер схемы.\n"
            "Обводка: Shift+протяжка — обвести группу рёбер.\n"
            "Ctrl+ЛКМ по обведённому — применить ко всем; по необведённому — "
            "только к этому ребру.\n"
            "Ctrl+ПКМ по обведённому — убрать ребро из обводки. Esc — сброс."
        )
        row.addWidget(title)

        # --- Цвет ---
        self.btn_edge_color = QPushButton("🎨 Цвет")
        self.btn_edge_color.setCheckable(True)
        self.btn_edge_color.setToolTip(
            "Режим изменения цвета ребра.\n"
            "Ctrl+ЛКМ по ребру — покрасить в текущий цвет палитры.\n"
            "Обведённые рёбра (Shift+протяжка) красятся все сразу.\n"
            "Цвет выбирается в палитре справа и сохраняется в FXML."
        )
        self.btn_edge_color.setStyleSheet(
            "QPushButton:checked { background-color: #E91E63; color: white; }"
        )
        self.btn_edge_color.clicked.connect(lambda: self._set_mode("edit_edge_color"))
        self.mode_group.addButton(self.btn_edge_color)
        row.addWidget(self.btn_edge_color)

        # Образец цвета + скрытая палитра (поповер)
        self.btn_edge_swatch = QPushButton()
        self.btn_edge_swatch.setFixedSize(26, 24)
        self.btn_edge_swatch.setToolTip(
            "Текущий цвет ребра. Нажмите — открыть палитру (пресеты)."
        )
        self._edge_palette_menu = QMenu(self)
        for hexc, name in self._EDGE_PALETTE:
            act = self._edge_palette_menu.addAction(self._color_icon(hexc), name)
            act.triggered.connect(lambda checked=False, c=hexc: self._on_edge_color_selected(c))
        self.btn_edge_swatch.setMenu(self._edge_palette_menu)
        row.addWidget(self.btn_edge_swatch)
        self._update_edge_swatch(self._current_edge_color)

        sep = QLabel(" | ")
        sep.setStyleSheet("color: #666;")
        row.addWidget(sep)

        # --- Размер ---
        self.btn_edge_size = QPushButton("📏 Размер")
        self.btn_edge_size.setCheckable(True)
        self.btn_edge_size.setToolTip(
            "Режим изменения размера (толщины) ребра.\n"
            "Ctrl+ЛКМ по ребру — задать текущий размер.\n"
            "Ctrl+колесо в редакторе — менять размер. Обведённым — всем сразу.\n"
            "Размер виден в редакторе и записывается в FXML."
        )
        self.btn_edge_size.setStyleSheet(
            "QPushButton:checked { background-color: #795548; color: white; }"
        )
        self.btn_edge_size.clicked.connect(lambda: self._set_mode("edit_edge_size"))
        self.mode_group.addButton(self.btn_edge_size)
        row.addWidget(self.btn_edge_size)

        size_lbl = QLabel("размер:")
        size_lbl.setStyleSheet("color: #aaa;")
        row.addWidget(size_lbl)
        self.spin_edge_size = QSpinBox()
        self.spin_edge_size.setRange(1, 40)
        self.spin_edge_size.setValue(4)
        self.spin_edge_size.setMaximumWidth(60)
        self.spin_edge_size.setToolTip(
            "Размер (толщина) ребра числом.\n"
            "Можно менять здесь или Ctrl+колесом в редакторе."
        )
        self.spin_edge_size.valueChanged.connect(self._on_edge_size_spin)
        row.addWidget(self.spin_edge_size)

        row.addStretch()
        layout.addWidget(container)
        self._edge_style_row = container
        self._edge_style_row.setVisible(False)  # включается режимом «Размер и цвет»

    # =================================================================
    # Переключение режима отображения/правки
    # =================================================================

    def _set_regime(self, regime: str):
        """Клик по кнопке режима → переключить редактор и UI."""
        if self._editor and hasattr(self._editor, "set_display_regime"):
            self._editor.set_display_regime(regime)
        self._apply_regime_ui(regime)

    def _on_regime_changed(self, regime: str):
        """Callback из редактора → синхронизировать кнопки и UI."""
        btn = {
            "ocr": self.btn_regime_ocr,
            "perp": self.btn_regime_perp,
            "style": self.btn_regime_style,
        }.get(regime)
        if btn:
            btn.setChecked(True)
        self._apply_regime_ui(regime)

    def _apply_regime_ui(self, regime: str):
        """Показать/спрятать элементы под активный режим."""
        is_style = (regime == "style")
        if hasattr(self, "_edge_style_row"):
            self._edge_style_row.setVisible(is_style)
        if hasattr(self, "perp_stats_label"):
            self.perp_stats_label.setVisible(regime == "perp")
        if is_style:
            # Авто-вход в подрежим «Цвет» для удобства
            self.btn_edge_color.setChecked(True)
            self._set_mode("edit_edge_color")

    @staticmethod
    def _color_icon(hexc: str) -> QIcon:
        pm = QPixmap(16, 16)
        pm.fill(QColor(hexc))
        return QIcon(pm)

    def _update_edge_swatch(self, hexc: str):
        """Отрисовать образец текущего цвета на кнопке."""
        self._current_edge_color = hexc
        border = "#000" if hexc.lower() in ("#ffffff", "#fff") else "#222"
        self.btn_edge_swatch.setStyleSheet(
            f"QPushButton {{ background-color: {hexc}; border: 1px solid {border}; "
            f"border-radius: 3px; }}"
        )

    @Slot()
    def _on_edge_color_selected(self, hexc: str):
        """Выбран цвет в палитре → установить кисть + включить режим цвета."""
        self._update_edge_swatch(hexc)
        if self._editor and hasattr(self._editor, "set_edge_brush_color"):
            self._editor.set_edge_brush_color(QColor(hexc))
        # Активировать режим цвета для удобства
        self.btn_edge_color.setChecked(True)
        self._set_mode("edit_edge_color")
        self.status_label.setText(f"Цвет кисти рёбер: {hexc}")

    @Slot(int)
    def _on_edge_size_spin(self, value: int):
        """Спинбокс размера → передать в редактор (без петли обратного вызова)."""
        if self._size_sync:
            return
        if self._editor and hasattr(self._editor, "edge_brush_size"):
            self._editor.edge_brush_size = max(1, int(value))

    def _on_editor_size_changed(self, size: int):
        """Callback из редактора (Ctrl+колесо) → обновить спинбокс."""
        self._size_sync = True
        self.spin_edge_size.setValue(int(size))
        self._size_sync = False

    # =================================================================
    # Editor-ready hook
    # =================================================================

    def _on_editor_ready(self):
        """После загрузки — обновить perp stats + config dir для KKS."""
        self._update_perp_stats()
        self._sync_editor_config_dir()
        # Инициализировать кисть изменения ребра (цвет/размер)
        if self._editor and hasattr(self._editor, "set_edge_brush_color"):
            self._editor.set_edge_brush_color(QColor(self._current_edge_color))
            self._editor.edge_brush_size = self.spin_edge_size.value()
            self._editor.edge_size_callback = self._on_editor_size_changed
        # Подключить режимы отображения/правки (по умолчанию — «ОКР привязка»)
        if self._editor and hasattr(self._editor, "set_display_regime"):
            self._editor.regime_callback = self._on_regime_changed
            self._editor.set_display_regime("ocr")
        self._apply_regime_ui("ocr")
        # Колбэки панели «Размер объектов»
        if self._editor is not None and hasattr(self._editor, "resize_panel_show_cb"):
            self._editor.resize_panel_show_cb = self._show_resize_panel
            self._editor.resize_panel_classes_cb = self._resize_classes_cb
            self._editor.resize_panel_state_cb = self._resize_state_cb

    # =================================================================
    # Оформление: + цвета рёбер по стадиям
    # =================================================================

    def _build_appearance_controls(self, panel):
        from PySide6.QtGui import QColor
        super()._build_appearance_controls(panel)
        self._add_color_setting(
            panel, "Ребро без диаметра", "edge_no_diam_color", QColor(255, 60, 40),
            lambda c: self._editor and self._editor.set_edge_no_diameter_color(c),
        )
        self._add_color_setting(
            panel, "Неперпенд. ребро", "edge_bad_color", QColor("#e67e22"),
            lambda c: self._editor and self._editor.set_edge_bad_color(c),
        )

    def apply_saved_appearance(self):
        super().apply_saved_appearance()
        ed = self._editor
        if ed is None:
            return
        from PySide6.QtGui import QColor
        if hasattr(ed, "set_edge_no_diameter_color"):
            self._apply_saved_color("edge_no_diam_color", QColor(255, 60, 40),
                                    ed.set_edge_no_diameter_color)
            self._apply_saved_color("edge_bad_color", QColor("#e67e22"),
                                    ed.set_edge_bad_color)

    def apply_default_appearance(self):
        super().apply_default_appearance()
        ed = self._editor
        if ed is None:
            return
        from PySide6.QtGui import QColor
        if hasattr(ed, "set_edge_no_diameter_color"):
            ed.set_edge_no_diameter_color(QColor(255, 60, 40, 180))
            ed.set_edge_bad_color(QColor("#e67e22"))

    def set_project_code(self, project_code: str):
        """Установить код проекта + передать config dir в editor."""
        super().set_project_code(project_code)
        self._sync_editor_config_dir()

    def _sync_editor_config_dir(self):
        """Передать путь к configs в editor для KKS нормализации (B6.5)."""
        if self._editor and hasattr(self._editor, '_project_config_dir') and self._project_code:
            from pathlib import Path
            cfg_dir = Path("configs/projects") / self._project_code
            if cfg_dir.is_dir():
                self._editor._project_config_dir = str(cfg_dir)

    # =================================================================
    # Stats override
    # =================================================================

    def _update_stats(self, stats: dict):
        """Обновить основные + перп статистику."""
        super()._update_stats(stats)
        self._update_perp_stats()

    def _update_perp_stats(self):
        """Обновить метку перпендикулярности рёбер."""
        if self._editor and hasattr(self._editor, "get_perpendicularity_stats"):
            s = self._editor.get_perpendicularity_stats()
            self.perp_stats_label.setText(
                f"Перпендикулярность: {s['good']}/{s['total']} ({s['avg_score']:.0%})"
            )

    # =================================================================
    # Advanced tools
    # =================================================================

    @Slot()
    def _optimize_all_edges(self):
        """Оптимизировать все неперпендикулярные рёбра."""
        if self._editor and hasattr(self._editor, "optimize_all_edges"):
            count = self._editor.optimize_all_edges()
            self._update_perp_stats()
            self.status_label.setText(f"Оптимизировано {count} рёбер")
            self._editor.setFocus()  # вернуть фокус — чтобы Ctrl+Z работал

    @Slot()
    def _batch_delete(self):
        """Удалить все выделенные узлы и рёбра."""
        if self._editor and hasattr(self._editor, "batch_delete"):
            self._editor.batch_delete()

    @Slot()
    def _auto_fix(self):
        """Запустить Auto-Fix."""
        if self._editor and hasattr(self._editor, "auto_fix"):
            try:
                self._editor.auto_fix()
                # Вернуть фокус редактору, иначе Ctrl+Z не дойдёт и не отменит Auto-Fix
                self._editor.setFocus()
            except Exception as exc:
                import traceback
                traceback.print_exc()
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(self, "Auto-Fix Error", str(exc))

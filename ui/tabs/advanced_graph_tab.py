"""
Advanced Graph Tab — вкладка продвинутого редактора графа P&ID.

Инструменты SimpleGraphTab + routing, optimise, drag, multi-select,
waypoints, batch delete, auto-fix, perp stats.

Используется на второй фазе двухфазного флоу val_graph:
  SimpleGraphTab → ✅ → AdvancedGraphTab → ✅ → complete_graph_validation()
"""

import logging

from PySide6.QtWidgets import (
    QHBoxLayout, QPushButton, QLabel, QCheckBox,
)
from PySide6.QtCore import Slot, Qt

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

        # --- галочка подсветки привязки OCR ---
        self.chk_ocr_highlight = QCheckBox("Подсветка привязки OCR")
        self.chk_ocr_highlight.setChecked(True)
        self.chk_ocr_highlight.setToolTip(
            "Подсветка узлов и рёбер по привязке OCR:\n"
            "зелёный/красный узел — есть/нет KKS, красное ребро — нет диаметра, "
            "плюс KKS-подписи.\nСнимите галочку, чтобы показать нейтральные цвета графа."
        )
        self.chk_ocr_highlight.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.chk_ocr_highlight.toggled.connect(self._on_toggle_ocr_highlight)
        toolbar.addWidget(self.chk_ocr_highlight)

        # --- perp stats (добавится после stretch из Base) ---
        # Создаём здесь, Base добавит stretch + stats_label + sep
        # Поэтому perp_stats_label добавляем через _on_editor_ready
        self.perp_stats_label = QLabel("Перпендикулярность: —")
        self.perp_stats_label.setStyleSheet("color: #aaa; font-size: 11px;")
        toolbar.addWidget(self.perp_stats_label)

    def _get_mode_button_map(self) -> dict:
        btn_map = super()._get_mode_button_map()
        btn_map.update({
            "optimize_edge": self.btn_optimize_edge,
            "edit_waypoint": self.btn_waypoints,
        })
        return btn_map

    # =================================================================
    # Editor-ready hook
    # =================================================================

    def _on_editor_ready(self):
        """После загрузки — обновить perp stats + config dir для KKS."""
        self._update_perp_stats()
        self._sync_editor_config_dir()
        # Применить состояние галочки подсветки к редактору
        if self._editor and hasattr(self._editor, "set_ocr_highlight"):
            self._editor.set_ocr_highlight(self.chk_ocr_highlight.isChecked())

    @Slot(bool)
    def _on_toggle_ocr_highlight(self, checked: bool):
        """Галочка «Подсветка привязки OCR» → вкл/выкл подсветку в редакторе."""
        if self._editor and hasattr(self._editor, "set_ocr_highlight"):
            self._editor.set_ocr_highlight(checked)

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

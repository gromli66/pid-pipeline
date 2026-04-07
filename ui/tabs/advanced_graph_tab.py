"""
Advanced Graph Tab — вкладка продвинутого редактора графа P&ID.

Инструменты SimpleGraphTab + routing, optimise, drag, multi-select,
waypoints, batch delete, auto-fix, perp stats.

Используется на второй фазе двухфазного флоу val_graph:
  SimpleGraphTab → ✅ → AdvancedGraphTab → ✅ → complete_graph_validation()
"""

import logging

from PySide6.QtWidgets import (
    QHBoxLayout, QPushButton, QLabel,
)
from PySide6.QtCore import Slot

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
        self.btn_optimize_edge = QPushButton("📐 Оптимизировать")
        self.btn_optimize_edge.setCheckable(True)
        self.btn_optimize_edge.setToolTip(
            "Клик на оранжевое ребро → оптимизировать перпендикулярность"
        )
        self.btn_optimize_edge.setStyleSheet(
            "QPushButton:checked { background-color: #9C27B0; color: white; }"
        )
        self.btn_optimize_edge.clicked.connect(lambda: self._set_mode("optimize_edge"))
        self.mode_group.addButton(self.btn_optimize_edge)
        toolbar.addWidget(self.btn_optimize_edge)

        # --- optimize_all (не переключатель) ---
        btn_optimize_all = QPushButton("📐 Все")
        btn_optimize_all.setToolTip("Оптимизировать все неперпендикулярные рёбра")
        btn_optimize_all.clicked.connect(self._optimize_all_edges)
        toolbar.addWidget(btn_optimize_all)

        self._add_separator(toolbar)

        # --- edit_waypoint ---
        self.btn_waypoints = QPushButton("◆ Waypoints")
        self.btn_waypoints.setCheckable(True)
        self.btn_waypoints.setToolTip(
            "Ctrl+Click на waypoint — перетащить (snap к сетке).\n"
            "Ctrl+Click на сегмент ребра — добавить waypoint.\n"
            "Маркеры видны только в этом режиме."
        )
        self.btn_waypoints.setStyleSheet(
            "QPushButton:checked { background-color: #00BCD4; color: white; }"
        )
        self.btn_waypoints.clicked.connect(lambda: self._set_mode("edit_waypoint"))
        self.mode_group.addButton(self.btn_waypoints)
        toolbar.addWidget(self.btn_waypoints)

        self._add_separator(toolbar)

        # --- auto_fix (не переключатель) ---
        btn_auto_fix = QPushButton("⚡ Auto-Fix")
        btn_auto_fix.setToolTip(
            "Выровнять коннекторы по осям трубопроводов.\n"
            "Enter — применить, Escape — отмена."
        )
        btn_auto_fix.setStyleSheet(
            "QPushButton { background-color: #FF9800; color: white; font-weight: bold; }"
            "QPushButton:hover { background-color: #F57C00; }"
        )
        btn_auto_fix.clicked.connect(self._auto_fix)
        toolbar.addWidget(btn_auto_fix)

        # --- perp stats (добавится после stretch из Base) ---
        # Создаём здесь, Base добавит stretch + stats_label + sep
        # Поэтому perp_stats_label добавляем через _on_editor_ready
        self.perp_stats_label = QLabel("⊥: —")
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
                f"⊥: {s['good']}/{s['total']} ({s['avg_score']:.0%})"
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
            except Exception as exc:
                import traceback
                traceback.print_exc()
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(self, "Auto-Fix Error", str(exc))

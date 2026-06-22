"""
Simple Graph Tab — вкладка простого редактора графа P&ID.

Инструменты: добавить/удалить ребро, добавить/удалить коннектор,
добавить оборудование из списка классов проекта.

Используется на первой фазе двухфазного флоу val_graph:
  SimpleGraphTab → ✅ → AdvancedGraphTab → ✅ → complete_graph_validation()
"""

import logging
from typing import Optional

from PySide6.QtWidgets import (
    QHBoxLayout, QPushButton, QMessageBox, QButtonGroup,
)
from PySide6.QtCore import Slot

from ui.services.api_client import APIClient, APIError
from ui.editors.simple_graph_editor import SimpleGraphEditor
from ui.editors.base_graph_editor import BaseGraphEditor
from ui.tabs.base_graph_tab import BaseGraphTab

logger = logging.getLogger(__name__)


class SimpleGraphTab(BaseGraphTab):
    """Вкладка простого редактора графа.

    Режимы: add_edge, delete_edge, add_connector, delete_node, add_node_from_list.
    Рёбра: point-to-point (без L-route).
    """

    def __init__(
        self,
        diagram_uid: str,
        diagram_name: str,
        api_client: APIClient,
        parent=None,
    ):
        self._project_code: Optional[str] = None
        super().__init__(diagram_uid, diagram_name, api_client, parent)

    def set_project_code(self, project_code: str):
        """Установить код проекта для загрузки классов оборудования."""
        self._project_code = project_code

    # =================================================================
    # BaseGraphTab interface
    # =================================================================

    def _create_editor(self) -> BaseGraphEditor:
        return SimpleGraphEditor()

    def _setup_toolbar(self, toolbar: QHBoxLayout):
        self.mode_group = QButtonGroup(self)

        # --- add_edge ---
        self.btn_add_edge = QPushButton("Добавить ребро")
        self.btn_add_edge.setCheckable(True)
        self.btn_add_edge.setToolTip(
            "Добавить ребро — соединение между двумя узлами.\n"
            "Ctrl+ЛКМ по центроиду первого узла, затем Ctrl+ЛКМ по центроиду второго "
            "— ребро создаётся.\n"
            "Esc — сбросить выбор. Ctrl+ПКМ — удалить ребро или узел под курсором.\n"
            "Навигация: ЛКМ — двигать схему, колесо мыши — масштаб."
        )
        self.btn_add_edge.setStyleSheet(
            "QPushButton:checked { background-color: #4CAF50; color: white; }"
        )
        self.btn_add_edge.clicked.connect(lambda: self._set_mode("add_edge"))
        self.mode_group.addButton(self.btn_add_edge)
        toolbar.addWidget(self.btn_add_edge)

        # --- add_connector ---
        self.btn_add_connector = QPushButton("Добавить перекрёсток")
        self.btn_add_connector.setCheckable(True)
        self.btn_add_connector.setToolTip(
            "Добавить перекрёсток — точку соединения/разветвления труб.\n"
            "Ctrl+ЛКМ по ребру — вставить перекрёсток в это место (ребро делится надвое).\n"
            "Ctrl+ЛКМ по свободному месту — отдельный (изолированный) перекрёсток.\n"
            "Ctrl+ПКМ — удалить узел или ребро под курсором."
        )
        self.btn_add_connector.setStyleSheet(
            "QPushButton:checked { background-color: #FF9800; color: white; }"
        )
        self.btn_add_connector.clicked.connect(lambda: self._set_mode("add_connector"))
        self.mode_group.addButton(self.btn_add_connector)
        toolbar.addWidget(self.btn_add_connector)

        self._add_separator(toolbar)

        # --- add_node_from_list (не toggleable — открывает диалог) ---
        self.btn_add_node = QPushButton("Добавить узел")
        self.btn_add_node.setCheckable(True)
        self.btn_add_node.setToolTip(
            "Добавить узел оборудования из списка классов проекта.\n"
            "Выберите класс, затем Ctrl+ЛКМ с протяжкой — обведите рамкой область узла. "
            "Слишком маленькая рамка — отмена.\n"
            "Класс остаётся выбранным: можно обвести несколько узлов подряд.\n"
            "Ctrl+ПКМ — удалить узел под курсором. Esc — выйти из режима."
        )
        self.btn_add_node.setStyleSheet(
            "QPushButton:checked { background-color: #2196F3; color: white; }"
        )
        self.btn_add_node.clicked.connect(self._on_add_node_from_list)
        self.mode_group.addButton(self.btn_add_node)
        toolbar.addWidget(self.btn_add_node)

    def _get_mode_button_map(self) -> dict:
        return {
            "add_edge": self.btn_add_edge,
            "add_connector": self.btn_add_connector,
            "add_node_from_list": self.btn_add_node,
        }

    # =================================================================
    # Add node from list
    # =================================================================

    @Slot()
    def _on_add_node_from_list(self):
        """Открыть диалог выбора класса → установить pending_node_class."""
        if not self._editor:
            self.btn_add_node.setChecked(False)
            return

        # Загрузить классы проекта
        classes = self._load_project_classes()
        if classes is None:
            self.btn_add_node.setChecked(False)
            return

        # Открыть диалог
        from ui.editors.node_list_dialog import NodeListDialog
        dlg = NodeListDialog(classes, self)
        result = dlg.exec()

        if result:
            selected = dlg.get_selected_class()
        else:
            selected = None

        if selected and self._editor and hasattr(self._editor, "set_pending_node_class"):
            self._editor.set_pending_node_class(selected)
            self._set_mode("add_node_from_list")
            self.status_label.setText(
                f"Ctrl+ЛКМ с протяжкой — обведите узел: {selected['name']}"
            )
        else:
            # Пользователь отменил — вернуться в idle
            self.btn_add_node.setChecked(False)
            self._set_mode("idle")

    def _load_project_classes(self) -> Optional[list]:
        """Загрузить классы из API. Возвращает list[dict] или None при ошибке."""
        if not self._project_code:
            # Fallback: получить из API
            try:
                diagram = self.api_client.get_diagram(self.uid)
                self._project_code = diagram.project_code
                logger.info("project_code получен из API: %s", self._project_code)
            except Exception as exc:
                logger.error("Не удалось получить project_code: %s", exc)
                self._project_code = "thermohydraulics"  # последний fallback

        try:
            result = self.api_client.get_project_classes(self._project_code)
            return result.get("classes", [])
        except APIError as exc:
            logger.error("Failed to load project classes: %s", exc)
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось загрузить классы проекта:\n{exc}"
            )
            return None

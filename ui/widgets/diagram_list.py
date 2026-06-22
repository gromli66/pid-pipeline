"""
Diagram List Widget — таблица диаграмм с фильтрами.

5 колонок: Файл, Проект, Статус, Дата, Удалить.
Двойной клик → diagram_selected(uid, filename).
"""

import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, List

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QPushButton, QLabel,
    QFileDialog, QMessageBox, QHeaderView, QComboBox,
    QFrame, QLineEdit, QMenu,
)
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QCursor

from ui.services.api_client import APIClient, DiagramInfo, DiagramStatus, APIError
from ui.services.status_provider import StatusProvider

logger = logging.getLogger(__name__)


# =====================================================================
# Константы отображения статусов
# =====================================================================

STATUS_COLORS = {
    DiagramStatus.UPLOADED: "#9E9E9E",
    DiagramStatus.DETECTING: "#2196F3",
    DiagramStatus.DETECTED: "#FF9800",
    DiagramStatus.VALIDATING_BBOX: "#FF9800",
    DiagramStatus.VALIDATED_BBOX: "#4CAF50",
    DiagramStatus.SEGMENTING: "#2196F3",
    DiagramStatus.SKELETONIZING: "#2196F3",
    DiagramStatus.SKELETONIZED: "#4CAF50",
    DiagramStatus.VALIDATING_MASKS: "#FF9800",
    DiagramStatus.VALIDATED_MASKS: "#4CAF50",
    DiagramStatus.SKELETONIZING_FINAL: "#2196F3",
    DiagramStatus.SKELETONIZED_FINAL: "#4CAF50",
    DiagramStatus.DETECTING_JUNCTIONS: "#2196F3",
    DiagramStatus.DETECTED_JUNCTIONS: "#4CAF50",
    DiagramStatus.VALIDATING_JUNCTIONS: "#FF9800",
    DiagramStatus.VALIDATED_JUNCTIONS: "#4CAF50",
    DiagramStatus.BUILDING_GRAPH: "#2196F3",
    DiagramStatus.BUILT: "#4CAF50",
    DiagramStatus.VALIDATING_GRAPH: "#FF9800",
    DiagramStatus.VALIDATED_GRAPH: "#4CAF50",
    DiagramStatus.OCR_PROCESSING: "#2196F3",
    DiagramStatus.OCR_COMPLETED: "#4CAF50",
    DiagramStatus.OCR_BOUND: "#4CAF50",
    DiagramStatus.GENERATING_FXML: "#2196F3",
    DiagramStatus.COMPLETED: "#8BC34A",
    DiagramStatus.ERROR: "#F44336",
}

STATUS_LABELS = {
    DiagramStatus.UPLOADED: "Загружено",
    DiagramStatus.CLEANING_FRAME: "🖼️ Очистка рамки",
    DiagramStatus.FRAME_CLEANED: "✓ Рамка очищена",
    DiagramStatus.DETECTING: "⏳ Детекция...",
    DiagramStatus.DETECTED: "🔍 Детекция завершена",
    DiagramStatus.VALIDATING_BBOX: "🏷️ Валидация bbox",
    DiagramStatus.VALIDATED_BBOX: "✓ Bbox валидированы",
    DiagramStatus.SEGMENTING: "⏳ Сегментация...",
    DiagramStatus.SKELETONIZING: "⏳ Скелетизация...",
    DiagramStatus.SKELETONIZED: "✓ Скелетизировано",
    DiagramStatus.VALIDATING_MASKS: "🏷️ Валидация масок",
    DiagramStatus.VALIDATED_MASKS: "✓ Маски валидированы",
    DiagramStatus.SKELETONIZING_FINAL: "⏳ Финальная скелетизация...",
    DiagramStatus.SKELETONIZED_FINAL: "✓ Финальный скелет",
    DiagramStatus.DETECTING_JUNCTIONS: "⏳ Детекция перекрёстков...",
    DiagramStatus.DETECTED_JUNCTIONS: "✓ Перекрёстки найдены",
    DiagramStatus.VALIDATING_JUNCTIONS: "🏷️ Валидация перекрёстков",
    DiagramStatus.VALIDATED_JUNCTIONS: "✓ Перекрёстки валидированы",
    DiagramStatus.BUILDING_GRAPH: "⏳ Построение графа...",
    DiagramStatus.BUILT: "✓ Граф построен",
    DiagramStatus.VALIDATING_GRAPH: "🏷️ Валидация графа",
    DiagramStatus.VALIDATED_GRAPH: "✓ Граф валидирован",
    DiagramStatus.OCR_PROCESSING: "⏳ OCR...",
    DiagramStatus.OCR_COMPLETED: "✓ OCR завершён",
    DiagramStatus.OCR_BOUND: "✓ OCR привязан",
    DiagramStatus.GENERATING_FXML: "⏳ Генерация FXML...",
    DiagramStatus.COMPLETED: "✅ Завершено",
    DiagramStatus.ERROR: "✗ Ошибка",
}

STATUS_ORDER = {s: i for i, s in enumerate(DiagramStatus)}


class DiagramListWidget(QWidget):
    """
    Таблица диаграмм.

    Signals:
        diagram_selected(uid, filename): двойной клик по диаграмме
        status_message(str, int): сообщение для статусбара
        show_progress(str): показать прогресс
        hide_progress(): скрыть прогресс
    """

    diagram_selected = Signal(str, str)  # uid, filename
    status_message = Signal(str, int)
    show_progress = Signal(str)
    hide_progress = Signal()

    COL_FILE = 0
    COL_PROJECT = 1
    COL_STATUS = 2
    COL_DATE = 3
    COL_DELETE = 4

    def __init__(
        self,
        api_client: APIClient,
        status_provider: StatusProvider,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)

        self.api_client = api_client
        self.status_provider = status_provider

        self._diagrams: List[DiagramInfo] = []
        self._projects: List[dict] = []

        self._sort_column = self.COL_DATE
        self._sort_ascending = False

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # === Фильтры ===
        filter_frame = QFrame()
        filter_layout = QHBoxLayout(filter_frame)
        filter_layout.setContentsMargins(0, 0, 0, 10)

        filter_layout.addWidget(QLabel("🔍"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск по имени файла...")
        self.search_input.setMaximumWidth(300)
        self.search_input.textChanged.connect(self._apply_filters)
        filter_layout.addWidget(self.search_input)

        filter_layout.addSpacing(20)

        filter_layout.addWidget(QLabel("Проект:"))
        self.project_filter = QComboBox()
        self.project_filter.addItem("Все", None)
        self.project_filter.setMinimumWidth(150)
        self.project_filter.currentIndexChanged.connect(self._apply_filters)
        filter_layout.addWidget(self.project_filter)

        filter_layout.addSpacing(10)

        filter_layout.addWidget(QLabel("Статус:"))
        self.status_filter = QComboBox()
        self.status_filter.addItem("Все", None)
        self.status_filter.addItem("Загружено", DiagramStatus.UPLOADED)
        self.status_filter.addItem("В процессе", DiagramStatus.DETECTING)
        self.status_filter.addItem("Завершено", DiagramStatus.COMPLETED)
        self.status_filter.addItem("Ошибка", DiagramStatus.ERROR)
        self.status_filter.setMinimumWidth(150)
        self.status_filter.currentIndexChanged.connect(self._apply_filters)
        filter_layout.addWidget(self.status_filter)

        filter_layout.addStretch()

        # === Автосохранение: toggle + интервал ===
        from ui.services.ui_settings import UISettings
        self._ui_settings = UISettings.instance()

        self._btn_autosave = QPushButton()
        self._btn_autosave.setFixedHeight(28)
        self._btn_autosave.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._btn_autosave.clicked.connect(self._toggle_autosave)
        filter_layout.addWidget(self._btn_autosave)

        self._btn_autosave_interval = QPushButton()
        self._btn_autosave_interval.setFixedHeight(28)
        self._btn_autosave_interval.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._interval_menu = QMenu(self)
        for sec in (30, 60, 120, 300):
            label = f"{sec} сек"
            action = self._interval_menu.addAction(label)
            action.setData(sec)
            action.triggered.connect(lambda checked, s=sec: self._set_autosave_interval(s))
        self._btn_autosave_interval.setMenu(self._interval_menu)
        filter_layout.addWidget(self._btn_autosave_interval)

        self._update_autosave_ui()

        layout.addWidget(filter_frame)

        # === Таблица ===
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels([
            "Файл", "Проект", "Статус", "Дата", "🗑️",
        ])

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_FILE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_PROJECT, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.COL_STATUS, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.COL_DATE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.COL_DELETE, QHeaderView.ResizeMode.Fixed)

        self.table.setColumnWidth(self.COL_PROJECT, 140)
        self.table.setColumnWidth(self.COL_STATUS, 200)
        self.table.setColumnWidth(self.COL_DATE, 100)
        self.table.setColumnWidth(self.COL_DELETE, 40)

        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)

        header.sectionClicked.connect(self._on_header_clicked)

        # Двойной клик → открыть workspace
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        layout.addWidget(self.table)

    # === Public API ===

    def set_projects(self, projects: List[dict]):
        self.project_filter.blockSignals(True)
        current = self.project_filter.currentData()
        self.project_filter.clear()
        self.project_filter.addItem("Все", None)
        for proj in projects:
            self.project_filter.addItem(proj["name"], proj["code"])
        for i in range(self.project_filter.count()):
            if self.project_filter.itemData(i) == current:
                self.project_filter.setCurrentIndex(i)
                break
        self.project_filter.blockSignals(False)

    @Slot()
    def load_diagrams(self):
        """Загрузить список диаграмм из API."""
        try:
            self._diagrams = self.api_client.list_diagrams()
            self._apply_filters()
            self.status_message.emit(
                f"Загружено {len(self._diagrams)} диаграмм", 3000,
            )
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось загрузить список:\n{exc.message}",
            )

    # === Фильтры и сортировка ===

    @Slot()
    def _apply_filters(self):
        search_text = self.search_input.text().lower().strip()
        project_code = self.project_filter.currentData()
        status_val = self.status_filter.currentData()

        filtered = []
        for d in self._diagrams:
            if search_text and search_text not in d.filename.lower():
                continue
            if project_code and d.project_code != project_code:
                continue
            if status_val:
                if status_val == DiagramStatus.DETECTING:
                    # "В процессе" — все processing статусы
                    if not self._is_processing(d.status):
                        continue
                elif d.status != status_val:
                    continue
            filtered.append(d)

        filtered = self._sort_diagrams(filtered)
        self._update_table(filtered)

    def _is_processing(self, status: DiagramStatus) -> bool:
        return status in {
            DiagramStatus.DETECTING,
            DiagramStatus.SEGMENTING,
            DiagramStatus.SKELETONIZING,
            DiagramStatus.SKELETONIZING_FINAL,
            DiagramStatus.DETECTING_JUNCTIONS,
            DiagramStatus.BUILDING_GRAPH,
            DiagramStatus.OCR_PROCESSING,
            DiagramStatus.GENERATING_FXML,
        }

    def _sort_diagrams(self, diagrams: list) -> list:
        reverse = not self._sort_ascending
        key_map = {
            self.COL_FILE: lambda d: d.filename.lower(),
            self.COL_PROJECT: lambda d: d.project_code,
            self.COL_STATUS: lambda d: STATUS_ORDER.get(d.status, 50),
            self.COL_DATE: lambda d: d.created_at or "",
        }
        key_fn = key_map.get(self._sort_column, key_map[self.COL_DATE])
        return sorted(diagrams, key=key_fn, reverse=reverse)

    @Slot(int)
    def _on_header_clicked(self, column: int):
        if column in (self.COL_FILE, self.COL_PROJECT, self.COL_STATUS, self.COL_DATE):
            if self._sort_column == column:
                self._sort_ascending = not self._sort_ascending
            else:
                self._sort_column = column
                self._sort_ascending = True
            self._apply_filters()

    # === Таблица ===

    def _update_table(self, diagrams: list):
        self.table.setRowCount(len(diagrams))

        for row, diagram in enumerate(diagrams):
            # Файл
            item = QTableWidgetItem(diagram.filename)
            item.setData(Qt.ItemDataRole.UserRole, diagram.uid)
            self.table.setItem(row, self.COL_FILE, item)

            # Проект
            project_name = diagram.project_code
            for p in self._projects:
                if p["code"] == diagram.project_code:
                    project_name = p["name"]
                    break
            self.table.setItem(
                row, self.COL_PROJECT, QTableWidgetItem(project_name),
            )

            # Статус
            status_text = STATUS_LABELS.get(diagram.status, diagram.status.value)
            status_item = QTableWidgetItem(status_text)
            status_color = STATUS_COLORS.get(diagram.status, "#AAAAAA")
            status_item.setForeground(QColor(status_color))
            if diagram.error_message:
                status_item.setToolTip(diagram.error_message)
            self.table.setItem(row, self.COL_STATUS, status_item)

            # Дата
            date_str = ""
            if diagram.created_at:
                try:
                    dt = datetime.fromisoformat(
                        diagram.created_at.replace("Z", "+00:00"),
                    )
                    date_str = dt.strftime("%d.%m.%y")
                except Exception:
                    date_str = diagram.created_at[:10]
            self.table.setItem(row, self.COL_DATE, QTableWidgetItem(date_str))

            # Удалить
            btn_delete = QPushButton("🗑️")
            btn_delete.setFixedWidth(30)
            btn_delete.setToolTip("Удалить диаграмму")
            btn_delete.clicked.connect(
                lambda checked, uid=diagram.uid, name=diagram.filename:
                    self._delete_diagram(uid, name),
            )
            self.table.setCellWidget(row, self.COL_DELETE, btn_delete)

            # Авто-watch для processing статусов
            if self._is_processing(diagram.status):
                self.status_provider.watch(diagram.uid)

    # === Действия ===

    @Slot(int, int)
    def _on_cell_double_clicked(self, row: int, column: int):
        item = self.table.item(row, self.COL_FILE)
        if item:
            uid = item.data(Qt.ItemDataRole.UserRole)
            filename = item.text()
            self.diagram_selected.emit(uid, filename)

    def _delete_diagram(self, uid: str, name: str):
        reply = QMessageBox.question(
            self,
            "Подтверждение удаления",
            f"Удалить '{name}'?\n\nВсе связанные файлы будут удалены.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self.show_progress.emit("Удаление...")
            self.api_client.delete_diagram(uid)
            self.hide_progress.emit()
            self.status_message.emit(f"Удалено: {name}", 3000)
            self.load_diagrams()
        except APIError as exc:
            self.hide_progress.emit()
            QMessageBox.warning(
                self, "Ошибка", f"Не удалось удалить:\n{exc.message}",
            )

    # =================================================================
    # Autosave UI
    # =================================================================

    def _toggle_autosave(self):
        self._ui_settings.autosave_enabled = not self._ui_settings.autosave_enabled
        self._update_autosave_ui()

    def _set_autosave_interval(self, sec: int):
        self._ui_settings.autosave_interval_sec = sec
        self._update_autosave_ui()

    def _update_autosave_ui(self):
        enabled = self._ui_settings.autosave_enabled
        interval = self._ui_settings.autosave_interval_sec

        if enabled:
            self._btn_autosave.setText("💾 Автосохранение: ВКЛ")
            self._btn_autosave.setStyleSheet(
                "QPushButton { background: #388E3C; color: white; "
                "border-radius: 4px; padding: 2px 8px; font-size: 11px; }"
                "QPushButton:hover { background: #43A047; }"
            )
        else:
            self._btn_autosave.setText("💾 Автосохранение: ВЫКЛ")
            self._btn_autosave.setStyleSheet(
                "QPushButton { background: #666; color: #ccc; "
                "border-radius: 4px; padding: 2px 8px; font-size: 11px; }"
                "QPushButton:hover { background: #777; }"
            )

        self._btn_autosave_interval.setText(f"⚙️ {interval}с")
        self._btn_autosave_interval.setStyleSheet(
            "QPushButton { background: #444; color: white; "
            "border-radius: 4px; padding: 2px 8px; font-size: 11px; }"
            "QPushButton:hover { background: #555; }"
            "QPushButton::menu-indicator { image: none; }"
        )

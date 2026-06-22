"""
Node List Dialog — диалог выбора класса оборудования из проекта.

Используется при добавлении equipment-узла в SimpleGraphEditor и AdvancedGraphEditor.
Источник данных: API GET /api/projects/{code}/classes → thermohydraulics.yaml
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem,
    QLineEdit, QPushButton, QLabel,
)
from PySide6.QtCore import Qt


# Технические классы, не показываемые в диалоге
# "unknow" НЕ скрываем — это валидный класс-заглушка, его можно ставить вручную
# (например, неопознанное оборудование, с последующей переклассификацией по KKS).
_SKIP_CLASSES = {"annotation", "background", "truba", "strelka"}


class NodeListDialog(QDialog):
    """Диалог выбора класса оборудования из проекта.

    Содержит поиск (QLineEdit) и список (QListWidget) с фильтрацией.

    Использование:
        dialog = NodeListDialog(classes_list, parent)
        if dialog.exec() == QDialog.Accepted:
            cls = dialog.get_selected_class()
            # cls = {"id": 1, "name": "armatura_ruchn"}
    """

    def __init__(self, classes: list[dict], parent=None):
        """
        Args:
            classes: Список из API: [{"id": 1, "name": "armatura_ruchn"}, ...]
        """
        super().__init__(parent)
        self.setWindowTitle("Добавить узел оборудования")
        self.setMinimumSize(350, 500)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        self._classes = classes
        self._selected_class: dict | None = None

        self._setup_ui()
        self._populate()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # ── Заголовок ──
        header = QLabel("Выберите класс оборудования:")
        header.setStyleSheet("font-weight: bold; font-size: 13px; margin-bottom: 4px;")
        layout.addWidget(header)

        # ── Поиск ──
        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍 Поиск по имени...")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter)
        layout.addWidget(self._search)

        # ── Список ──
        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.itemDoubleClicked.connect(self.accept)
        self._list.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self._list)

        # ── Инфо-лейбл ──
        self._info_label = QLabel("")
        self._info_label.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self._info_label)

        # ── Кнопки ──
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        btn_cancel = QPushButton("Отмена")
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(btn_cancel)

        self._btn_add = QPushButton("Добавить")
        self._btn_add.setEnabled(False)
        self._btn_add.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; "
            "font-weight: bold; padding: 6px 16px; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #9E9E9E; }"
            "QPushButton:hover:!disabled { background-color: #45a049; }"
        )
        self._btn_add.clicked.connect(self.accept)
        btn_layout.addWidget(self._btn_add)

        layout.addLayout(btn_layout)

    def _populate(self):
        """Заполнить список классами, исключая технические."""
        self._list.clear()
        count = 0
        for cls in self._classes:
            name = cls.get("name", "")
            if name in _SKIP_CLASSES:
                continue

            item = QListWidgetItem(f'{name}  (id={cls["id"]})')
            item.setData(Qt.ItemDataRole.UserRole, cls)
            self._list.addItem(item)
            count += 1

        self._info_label.setText(f"{count} классов доступно")

    def _filter(self, text: str):
        """Фильтрация списка по тексту поиска."""
        text_lower = text.lower().strip()
        visible_count = 0
        for i in range(self._list.count()):
            item = self._list.item(i)
            matches = text_lower in item.text().lower()
            item.setHidden(not matches)
            if matches:
                visible_count += 1
        self._info_label.setText(f"{visible_count} совпадений")

    def _on_selection_changed(self, current, previous):
        """Активировать кнопку «Добавить» при выборе."""
        self._btn_add.setEnabled(current is not None)

    def get_selected_class(self) -> dict | None:
        """Получить выбранный класс.

        Returns:
            {"id": N, "name": "..."} или None.
        """
        item = self._list.currentItem()
        if item:
            return item.data(Qt.ItemDataRole.UserRole)
        return None

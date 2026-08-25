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

# Ключ сортировки берём из общего модуля, а не дублируем: разойдись он с
# `create_labels_from_config`, порядок в палитре и в CVAT стал бы разным.
from app.services.class_display import sort_key as _sort_key


# Технические классы, не показываемые в диалоге
# "unknow" НЕ скрываем — это валидный класс-заглушка, его можно ставить вручную
# (например, неопознанное оборудование, с последующей переклассификацией по KKS).
_SKIP_CLASSES = {"annotation", "background", "truba", "strelka"}


class NodeListDialog(QDialog):
    """Диалог выбора класса оборудования из проекта.

    Содержит поиск (QLineEdit) и список (QListWidget) с фильтрацией.

    Пункты показываются под русскими названиями (`display_name` из API) и
    отсортированы по ним; в данные узла при этом уходит английское `name`.

    Использование:
        dialog = NodeListDialog(classes_list, parent)
        if dialog.exec() == QDialog.Accepted:
            cls = dialog.get_selected_class()
            # cls = {"id": 1, "name": "armatura_ruchn", "display_name": "Арматура ручная"}
    """

    def __init__(self, classes: list[dict], parent=None):
        """
        Args:
            classes: Список из API: [{"id": 1, "name": "armatura_ruchn",
                     "display_name": "Арматура ручная"}, ...]. `display_name`
                     может отсутствовать (старый сервер) — тогда показываем `name`.
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

    @staticmethod
    def _label(cls: dict) -> str:
        """Что показать человеку: русское название, если API его прислал."""
        return cls.get("display_name") or cls.get("name", "")

    def _populate(self):
        """Заполнить список классами по алфавиту, исключая технические."""
        self._list.clear()
        shown = [c for c in self._classes if c.get("name", "") not in _SKIP_CLASSES]
        shown.sort(key=lambda c: _sort_key(self._label(c)))

        for cls in shown:
            item = QListWidgetItem(f'{self._label(cls)}  (id={cls["id"]})')
            # В UserRole кладём ИСХОДНЫЙ dict с английским `name`: именно он
            # уходит в class_name узла графа. Показываем русское — пишем англ.
            item.setData(Qt.ItemDataRole.UserRole, cls)
            self._list.addItem(item)

        self._info_label.setText(f"{len(shown)} классов доступно")

    def _filter(self, text: str):
        """Фильтрация по русскому названию, английскому имени и «id=».

        Ищем по данным из UserRole, а не по тексту пункта: английское имя из
        текста ушло, но искать по нему привычно и удобно при отладке.
        """
        needle = text.lower().strip()
        visible_count = 0
        for i in range(self._list.count()):
            item = self._list.item(i)
            cls = item.data(Qt.ItemDataRole.UserRole) or {}
            haystack = " ".join((
                item.text(),
                cls.get("name", ""),
                f'id={cls.get("id", "")}',
            )).lower()
            matches = needle in haystack
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
            Исходный dict из API ({"id": N, "name": "...", ...}) или None.
            `name` здесь ВСЕГДА английское — оно уходит в class_name узла.
        """
        item = self._list.currentItem()
        if item:
            return item.data(Qt.ItemDataRole.UserRole)
        return None

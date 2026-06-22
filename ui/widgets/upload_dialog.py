"""
Upload Dialog - диалог загрузки новой диаграммы.

Поддерживает изображения (PNG/JPG/TIFF) и PDF. Для PDF можно выбрать страницу;
если страниц несколько — показывается предупреждение. Рендер PDF→PNG @300 DPI
выполняется на сервере при загрузке.
"""

from pathlib import Path
from typing import List

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout,
    QComboBox, QLineEdit, QPushButton, QFileDialog,
    QSpinBox, QLabel, QHBoxLayout, QWidget,
)

_PDF_EXT = ".pdf"


class UploadDialog(QDialog):
    """Диалог выбора файла, проекта (и страницы для PDF) для загрузки."""

    def __init__(self, projects: List[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Загрузить диаграмму")
        self.setMinimumWidth(420)

        layout = QFormLayout(self)

        # Файл
        self.file_input = QLineEdit()
        self.file_input.setReadOnly(True)
        self.file_input.setPlaceholderText("Выберите файл...")

        btn_browse = QPushButton("📁")
        btn_browse.setFixedWidth(30)
        btn_browse.clicked.connect(self._browse_file)

        file_widget = QWidget()
        file_layout = QHBoxLayout(file_widget)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.addWidget(self.file_input)
        file_layout.addWidget(btn_browse)

        layout.addRow("Файл:", file_widget)

        # Проект
        self.project_combo = QComboBox()
        for proj in projects:
            self.project_combo.addItem(proj["name"], proj["code"])
        layout.addRow("Проект:", self.project_combo)

        # Страница PDF (скрыто по умолчанию, показывается при выборе PDF)
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setMaximum(1)
        self.page_label = QLabel("Страница PDF:")
        layout.addRow(self.page_label, self.page_spin)

        self.warn_label = QLabel("")
        self.warn_label.setStyleSheet("color: #E65100;")
        self.warn_label.setWordWrap(True)
        layout.addRow("", self.warn_label)

        self._set_pdf_controls_visible(False)

        # Кнопки
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _set_pdf_controls_visible(self, visible: bool):
        self.page_label.setVisible(visible)
        self.page_spin.setVisible(visible)
        self.warn_label.setVisible(visible)

    def _browse_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Выберите файл", "",
            "P&ID файлы (*.png *.jpg *.jpeg *.tiff *.tif *.pdf)"
        )
        if not file_path:
            return
        self.file_input.setText(file_path)

        if Path(file_path).suffix.lower() == _PDF_EXT:
            self._configure_pdf(file_path)
        else:
            self._set_pdf_controls_visible(False)
            self.page_spin.setValue(1)

    def _configure_pdf(self, file_path: str):
        """Показать выбор страницы для PDF; число страниц — через PyMuPDF."""
        n_pages = self._pdf_page_count(file_path)
        self._set_pdf_controls_visible(True)
        if n_pages and n_pages > 0:
            self.page_spin.setMaximum(n_pages)
            self.page_spin.setValue(1)
            if n_pages > 1:
                self.warn_label.setText(
                    f"⚠ В PDF {n_pages} страниц. Будет загружена выбранная страница "
                    f"(по умолчанию — первая)."
                )
            else:
                self.warn_label.setText("")
        else:
            # Не удалось прочитать число страниц — ручной ввод
            self.page_spin.setMaximum(9999)
            self.page_spin.setValue(1)
            self.warn_label.setText(
                "Не удалось определить число страниц PDF. Укажите номер вручную."
            )

    @staticmethod
    def _pdf_page_count(file_path: str) -> int:
        try:
            import fitz  # PyMuPDF
            with fitz.open(file_path) as doc:
                return doc.page_count
        except Exception:
            return 0

    def get_values(self) -> tuple:
        """Вернуть (file_path, project_code, page)."""
        page = self.page_spin.value() if self.page_spin.isVisible() else 1
        return Path(self.file_input.text()), self.project_combo.currentData(), page

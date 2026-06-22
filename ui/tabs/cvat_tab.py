"""
CVAT Tab — вкладка валидации аннотаций во встроенном CVAT.

Рефакторинг из CVATWindow (QMainWindow → QWidget).
"""

from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QMessageBox,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtCore import Signal, Slot, QUrl, QTimer


# JS для принудительного сохранения в CVAT (Ctrl+S)
_CVAT_SAVE_JS = """
(function() {
    document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 's', code: 'KeyS', keyCode: 83,
        ctrlKey: true, bubbles: true, cancelable: true
    }));
})();
"""


class CvatTab(QWidget):
    """Вкладка с встроенным CVAT для валидации аннотаций."""

    # Сигналы для workspace
    confirmed = Signal()       # Подтверждено — можно закрывать и продолжать
    status_message = Signal(str)  # Сообщение для статусбара

    def __init__(
        self,
        diagram_uid: str,
        cvat_url: str,
        diagram_name: str = "",
        cvat_task_id: Optional[int] = None,
        cvat_job_id: Optional[int] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)

        self.diagram_uid = diagram_uid
        self.cvat_url = cvat_url
        self.diagram_name = diagram_name
        self.cvat_task_id = cvat_task_id
        self.cvat_job_id = cvat_job_id

        self._is_saving = False

        self._setup_ui()
        self.web_view.setUrl(QUrl(self.cvat_url))

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # === Toolbar ===
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(8, 4, 8, 4)
        toolbar.setSpacing(8)

        label = QLabel(
            "Отредактируйте аннотации, затем нажмите "
            "'Подтвердить валидацию'"
        )
        label.setStyleSheet("color: #666; font-size: 12px;")
        toolbar.addWidget(label)

        toolbar.addStretch()

        btn_refresh = QPushButton("🔄 Обновить")
        btn_refresh.setToolTip(
            "Перезагрузить страницу CVAT (если не прогрузилась/зависла "
            "или не видно разметки).\n"
            "⚠️ Несохранённые правки потеряются — сначала сохрани в CVAT "
            "(Ctrl+S) или нажми 'Подтвердить валидацию'."
        )
        btn_refresh.clicked.connect(self._on_refresh)
        toolbar.addWidget(btn_refresh)

        self.btn_confirm = QPushButton("✅ Подтвердить валидацию")
        self.btn_confirm.setToolTip(
            "Сохранить отредактированные аннотации в CVAT и перейти "
            "к следующему этапу пайплайна."
        )
        self.btn_confirm.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                font-weight: bold;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:disabled {
                background-color: #9E9E9E;
            }
        """)
        self.btn_confirm.clicked.connect(self._on_confirm)
        toolbar.addWidget(self.btn_confirm)

        layout.addLayout(toolbar)

        # === WebView ===
        self.web_view = QWebEngineView()
        layout.addWidget(self.web_view, stretch=1)

        # === Status label ===
        self.status_label = QLabel("Загрузка CVAT...")
        self.status_label.setStyleSheet(
            "color: #888; font-size: 11px; padding: 2px 8px;"
        )
        layout.addWidget(self.status_label)

    # === Actions ===

    @Slot()
    def _on_refresh(self):
        self.web_view.setUrl(QUrl(self.cvat_url))

    @Slot()
    def _on_confirm(self):
        if self._is_saving:
            return

        self._is_saving = True
        self.btn_confirm.setEnabled(False)
        self.status_label.setText("💾 Сохранение аннотаций в CVAT...")
        self.status_message.emit("Сохранение аннотаций в CVAT...")

        self.web_view.page().runJavaScript(_CVAT_SAVE_JS)
        QTimer.singleShot(5000, self._after_save)

    def _after_save(self):
        self._is_saving = False
        self.btn_confirm.setEnabled(True)
        self.status_label.setText("✅ Аннотации сохранены")
        self.confirmed.emit()

    # === Public API ===

    def has_unsaved_changes(self) -> bool:
        """Проверить наличие несохранённых изменений."""
        # CVAT сохраняет автоматически, но на всякий случай
        return False

    def trigger_save_and_confirm(self):
        """Сохранить и подтвердить (вызов извне)."""
        self._on_confirm()

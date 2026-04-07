"""
CVAT Window - окно для валидации аннотаций в встроенном CVAT.

Чистый QWebEngineView без кастомных инъекций.
Единственный JS — Ctrl+S для принудительного сохранения перед экспортом.
"""

from typing import Optional

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout,
    QPushButton, QLabel, QToolBar, QStatusBar,
    QMessageBox,
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


class CVATWindow(QMainWindow):
    """Окно с встроенным CVAT для валидации аннотаций."""

    # Аннотации сохранены в CVAT, можно скачивать экспорт
    validation_confirmed = Signal(str)
    # Окно закрыто без сохранения
    window_closed = Signal(str)

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

        # Флаги состояния
        self._is_saving = False
        self._close_after_save = False

        self.setWindowTitle(f"CVAT Валидация - {diagram_name or diagram_uid[:8]}")
        self.setMinimumSize(1200, 800)

        self._setup_ui()
        self.web_view.setUrl(QUrl(self.cvat_url))

    def _setup_ui(self):
        """Настройка UI."""
        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # === Toolbar ===
        toolbar = QToolBar("CVAT Actions")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        label = QLabel(
            "  Отредактируйте аннотации, затем нажмите "
            "'Подтвердить валидацию'  "
        )
        label.setStyleSheet("color: #666; font-size: 12px;")
        toolbar.addWidget(label)

        toolbar.addSeparator()

        btn_refresh = QPushButton("🔄 Обновить")
        btn_refresh.clicked.connect(self._on_refresh)
        toolbar.addWidget(btn_refresh)

        toolbar.addSeparator()

        self.btn_confirm = QPushButton("✅ Подтвердить валидацию")
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

        # === WebView ===
        self.web_view = QWebEngineView()
        layout.addWidget(self.web_view)

        # === Status Bar ===
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)
        self.statusbar.showMessage("Загрузка CVAT...")

    # -----------------------------------------------------------------
    # Actions
    # -----------------------------------------------------------------

    @Slot()
    def _on_refresh(self):
        """Обновить страницу."""
        self.web_view.setUrl(QUrl(self.cvat_url))

    @Slot()
    def _on_confirm(self):
        """
        Подтвердить валидацию.

        Flow: Ctrl+S → ждём 5 сек → emit validation_confirmed.
        При вызове из кнопки — _close_after_save = True (закрыть после).
        """
        if self._is_saving:
            return

        self._is_saving = True
        # Кнопка в окне всегда закрывает после сохранения
        self._close_after_save = True
        self.btn_confirm.setEnabled(False)
        self.statusbar.showMessage("💾 Сохранение аннотаций в CVAT...")

        self._trigger_save_in_cvat()
        QTimer.singleShot(5000, self._after_save)

    def _trigger_save_in_cvat(self):
        """Инжектить Ctrl+S в CVAT для принудительного сохранения."""
        self.web_view.page().runJavaScript(_CVAT_SAVE_JS)

    def _after_save(self):
        """Вызывается через 5 сек после Ctrl+S."""
        self._is_saving = False
        self.btn_confirm.setEnabled(True)

        self.validation_confirmed.emit(self.diagram_uid)

        if self._close_after_save:
            self.statusbar.showMessage("✅ Сохранено. Закрытие...")
            # close() вызовет closeEvent, но _close_after_save уже True
            # — сразу примем закрытие
            self.close()
        else:
            self.statusbar.showMessage("✅ Аннотации сохранены", 5000)

    # -----------------------------------------------------------------
    # Public API (для вызова из MainWindow)
    # -----------------------------------------------------------------

    def trigger_save_and_confirm(self):
        """
        Сохранить аннотации и emit validation_confirmed.

        Вызывается из MainWindow когда кнопка "Валидация" нажата
        в таблице при открытом CVAT окне. После сохранения окно
        закроется автоматически.
        """
        if self._is_saving:
            return

        self._is_saving = True
        self._close_after_save = True
        self.btn_confirm.setEnabled(False)
        self.statusbar.showMessage("💾 Сохранение аннотаций в CVAT...")

        self._trigger_save_in_cvat()
        QTimer.singleShot(5000, self._after_save)

    # -----------------------------------------------------------------
    # Close handling
    # -----------------------------------------------------------------

    def closeEvent(self, event):
        """Обработка закрытия окна."""
        # Если уже в процессе сохранения — не мешать
        if self._is_saving:
            if self._close_after_save:
                # Сохранение завершилось, можно закрывать
                self.window_closed.emit(self.diagram_uid)
                event.accept()
            else:
                event.ignore()
            return

        # Если close вызван программно после save — просто закрываем
        if self._close_after_save:
            self.window_closed.emit(self.diagram_uid)
            event.accept()
            return

        # Спрашиваем пользователя
        reply = QMessageBox.question(
            self,
            "Закрытие CVAT",
            "Сохранить аннотации перед закрытием?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )

        if reply == QMessageBox.StandardButton.Save:
            # Сохранить → Ctrl+S → 5 сек → emit → close
            event.ignore()
            self._is_saving = True
            self._close_after_save = True
            self.btn_confirm.setEnabled(False)
            self.statusbar.showMessage("💾 Сохранение аннотаций в CVAT...")
            self._trigger_save_in_cvat()
            QTimer.singleShot(5000, self._after_save)

        elif reply == QMessageBox.StandardButton.Discard:
            # Закрыть без сохранения
            self.window_closed.emit(self.diagram_uid)
            event.accept()

        else:
            # Отмена — остаться
            event.ignore()

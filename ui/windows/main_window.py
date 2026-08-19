"""
Main Window — главное окно P&ID Pipeline.

Два уровня навигации:
- Page 0: Список диаграмм (DiagramListWidget)
- Page 1: Рабочее пространство диаграммы (DiagramWorkspace)
"""

import logging
from typing import Optional

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout,
    QLabel, QMessageBox, QStatusBar, QToolBar,
    QProgressBar, QFrame, QHBoxLayout,
    QStackedWidget, QDialog,
)
from PySide6.QtCore import Qt, Slot, QTimer
from PySide6.QtGui import QAction

from ui.services.api_client import APIClient, APIError, DiagramStatus
from ui.services.progress_model import substep_status_line
from ui.services.status_provider import StatusProvider
from ui.widgets.diagram_list import DiagramListWidget
from ui.widgets.diagram_workspace import DiagramWorkspace
from ui.widgets.upload_dialog import UploadDialog
from ui._version import __version__ as APP_VERSION

logger = logging.getLogger(__name__)

# Базовый заголовок окна: в собранном клиенте показываем версию-дату,
# из исходников (dev) — исполняемый git-коммит: «какой код я гоняю»
# проверяется взглядом на заголовок, а не верой (запрос заказчика после
# путаницы с перезапусками во время правок).
APP_TITLE = "P&ID Pipeline" if APP_VERSION == "dev" else f"P&ID Pipeline {APP_VERSION}"
if APP_VERSION == "dev":
    try:
        import subprocess
        from pathlib import Path as _Path
        _r = subprocess.run(
            ["git", "log", "-1", "--format=%h %cd", "--date=format:%d.%m %H:%M"],
            cwd=_Path(__file__).resolve().parents[2],
            capture_output=True, text=True, timeout=3)
        if _r.returncode == 0 and _r.stdout.strip():
            APP_TITLE = f"P&ID Pipeline [dev {_r.stdout.strip()}]"
    except (OSError, subprocess.SubprocessError):
        pass  # без git заголовок остаётся прежним


class MainWindow(QMainWindow):
    """Главное окно приложения."""

    def __init__(self):
        super().__init__()

        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(1200, 700)

        # Shared services
        import os
        self.api_client = APIClient(os.environ.get("PID_API_URL", "http://localhost:8000"))
        self.status_provider = StatusProvider(self.api_client, parent=self)
        self.status_provider.status_updated.connect(self._on_status_updated)
        self.status_provider.stages_updated.connect(self._on_stages_updated)
        self.status_provider.error_occurred.connect(self._on_status_error)

        # State
        self._projects: list = []

        # UI
        self._setup_ui()
        self._setup_toolbar()
        self._setup_statusbar()

        # Load data
        QTimer.singleShot(100, self._load_initial_data)

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # === Progress bar ===
        self.progress_frame = QFrame()
        progress_layout = QHBoxLayout(self.progress_frame)
        progress_layout.setContentsMargins(10, 5, 10, 5)

        self.progress_label = QLabel("Обработка...")
        progress_layout.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setMaximumWidth(200)
        progress_layout.addWidget(self.progress_bar)

        progress_layout.addStretch()
        self.progress_frame.hide()
        layout.addWidget(self.progress_frame)

        # === Stacked Widget ===
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        # Page 0: Diagram List
        self.diagram_list = DiagramListWidget(
            api_client=self.api_client,
            status_provider=self.status_provider,
        )
        self.diagram_list.diagram_selected.connect(self._on_diagram_selected)
        self.diagram_list.status_message.connect(self._on_status_message)
        self.diagram_list.show_progress.connect(self._show_progress)
        self.diagram_list.hide_progress.connect(self._hide_progress)
        self.stack.addWidget(self.diagram_list)

        # Page 1: Diagram Workspace
        self.workspace = DiagramWorkspace(
            api_client=self.api_client,
            status_provider=self.status_provider,
        )
        self.workspace.back_requested.connect(self._on_back_to_list)
        self.workspace.status_message.connect(self._on_status_message)
        self.stack.addWidget(self.workspace)

    def _setup_toolbar(self):
        self.toolbar = QToolBar("Main")
        self.toolbar.setMovable(False)
        self.addToolBar(self.toolbar)

        self.action_upload = QAction("📁 Загрузить", self)
        self.action_upload.triggered.connect(self._on_upload_clicked)
        self.toolbar.addAction(self.action_upload)

        self.toolbar.addSeparator()

        self.action_refresh = QAction("🔄 Обновить", self)
        self.action_refresh.triggered.connect(self._on_refresh)
        self.toolbar.addAction(self.action_refresh)

    def _setup_statusbar(self):
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)

        # Подстадия бегущей авто-стадии открытой диаграммы (Волна B, current_step):
        # «Выделение труб · прогон модели» — слева от значка API.
        self.substep_label = QLabel()
        self.substep_label.setStyleSheet("color: gray;")
        self.statusbar.addPermanentWidget(self.substep_label)

        self.connection_label = QLabel()
        self.statusbar.addPermanentWidget(self.connection_label)

        self._check_connection()

    # === Navigation ===

    @Slot(str, str)
    def _on_diagram_selected(self, uid: str, name: str):
        """Двойной клик по диаграмме → открыть workspace."""
        self.workspace.load_diagram(uid, name)
        self.stack.setCurrentIndex(1)
        self.action_upload.setVisible(False)
        self.substep_label.clear()  # не показывать подстадию прошлой диаграммы
        self.setWindowTitle(f"{APP_TITLE} — {name}")

    @Slot()
    def _on_back_to_list(self):
        """Вернуться в список диаграмм."""
        self.stack.setCurrentIndex(0)
        self.action_upload.setVisible(True)
        self.substep_label.clear()
        self.setWindowTitle(APP_TITLE)
        self.diagram_list.load_diagrams()

    # === Connection ===

    def _check_connection(self):
        try:
            if self.api_client.health_check():
                self.connection_label.setText("🟢 API")
                self.connection_label.setStyleSheet("color: green;")
            else:
                self.connection_label.setText("🔴 API недоступен")
                self.connection_label.setStyleSheet("color: red;")
        except Exception:
            self.connection_label.setText("🔴 API недоступен")
            self.connection_label.setStyleSheet("color: red;")

    # === Progress ===

    @Slot(str)
    def _show_progress(self, message: str):
        # Ad-hoc операции (загрузка и т.п.) — неопределённый «бегунок».
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText(message)
        self.progress_frame.show()

    @Slot()
    def _hide_progress(self):
        self.progress_frame.hide()

    # === Data loading ===

    def _load_initial_data(self):
        self._load_projects()
        self.diagram_list.load_diagrams()

    def _load_projects(self):
        try:
            self._projects = self.api_client.list_projects()
        except APIError:
            self._projects = [
                {"code": "thermohydraulics", "name": "Термогидравлика"},
            ]
        self.diagram_list.set_projects(self._projects)

    # === Upload ===

    @Slot()
    def _on_upload_clicked(self):
        if not self._projects:
            self._projects = [
                {"code": "thermohydraulics", "name": "Термогидравлика"},
            ]

        dialog = UploadDialog(self._projects, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        file_path, project_code, pdf_page = dialog.get_values()
        if not file_path or not str(file_path):
            QMessageBox.warning(self, "Ошибка", "Файл не выбран")
            return

        try:
            self._show_progress("Загрузка файла...")
            info = self.api_client.upload_diagram(file_path, project_code, page=pdf_page)
            self._hide_progress()
            self.statusbar.showMessage(f"Загружено: {info.filename}", 3000)
            self.diagram_list.load_diagrams()
        except APIError as exc:
            self._hide_progress()
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось загрузить:\n{exc.message}",
            )

    # === Refresh ===

    @Slot()
    def _on_refresh(self):
        if self.stack.currentIndex() == 0:
            self.diagram_list.load_diagrams()
        else:
            self.workspace._refresh_status()

    # === Callbacks ===

    @Slot(str, int)
    def _on_status_message(self, message: str, timeout_ms: int = 3000):
        self.statusbar.showMessage(message, timeout_ms)

    @Slot(str, object)
    def _on_status_updated(self, uid: str, status_info):
        if self.stack.currentIndex() == 0:
            self.diagram_list.load_diagrams()

    @Slot(str, object)
    def _on_stages_updated(self, uid: str, stages):
        """Подстадия бегущей авто-стадии → статусбар у значка API (Волна B).

        Только для диаграммы, открытой в workspace; ручные стадии/нет бегущей →
        пустая строка (label очищается).
        """
        if self.stack.currentIndex() != 1 or uid != getattr(self.workspace, "_uid", None):
            return
        self.substep_label.setText(substep_status_line(stages))

    @Slot(str, str)
    def _on_status_error(self, uid: str, error_message: str):
        self.statusbar.showMessage(f"Ошибка: {error_message}", 5000)

    # === Закрытие клиента ===

    def closeEvent(self, event):
        """Спросить о несохранённом, погасить фон, оставить след в логе.

        Обработчика не было вовсе, и крестик окна убивал процесс мимо всего:
        правки открытой вкладки уходили молча (вопрос задавал только путь
        «← Назад» — `DiagramWorkspace._close_active_tab`), `cleanup()` не
        звался, а о конце сеанса в логе клиента не оставалось ни строки —
        в собранном `.exe` (`console=False`) файл лога единственный след,
        `sys.stdout` там `None`.

        Это же единственная точка, где накопленное клиентом успевает уйти:
        отсюда пункт 10.15 (активное время оператора) шлёт последний батч.
        """
        if not self._confirm_unsaved():
            logger.info("Закрытие клиента отменено — несохранённые изменения")
            event.ignore()
            return

        self.workspace.cleanup()
        logger.info("Клиент закрывается")
        event.accept()

    def _confirm_unsaved(self) -> bool:
        """Вопрос об открытой вкладке с правками. `False` — не закрывать.

        Вопрос и цепочка сохранения — те же, что у «← Назад»: у вкладок один
        контракт `has_unsaved_changes()` плюс один из `_save_graph` /
        `_save_masks` / `_save_mask` (у вкладки привязки OCR `_save_graph` —
        псевдоним). Вкладки без единого из них (сейчас таких нет) закрытию не
        мешают: тупик «клиент не закрывается» дороже несохранённой вкладки,
        о которой оператора спросили.
        """
        tab = self.workspace._active_tab
        if tab is None or not hasattr(tab, 'has_unsaved_changes'):
            return True
        if not tab.has_unsaved_changes():
            return True

        reply = QMessageBox.question(
            self, "Выход",
            "Есть несохранённые изменения. Сохранить перед выходом?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return False
        if reply == QMessageBox.StandardButton.No:
            logger.warning("Выход без сохранения вкладки %s — выбор оператора",
                           type(tab).__name__)
            return True

        for name in ('_save_graph', '_save_masks', '_save_mask'):
            save = getattr(tab, name, None)
            if save is None:
                continue
            if save():
                return True
            # Об ошибке говорит сама вкладка (диалог или своя строка статуса);
            # здесь — почему окно осталось на экране.
            logger.warning("Сохранение %s.%s не удалось — клиент не закрыт",
                           type(tab).__name__, name)
            self.statusbar.showMessage(
                "Не удалось сохранить — клиент остался открытым", 5000)
            return False

        logger.warning("Вкладка %s не умеет сохраняться — выход без сохранения",
                       type(tab).__name__)
        return True

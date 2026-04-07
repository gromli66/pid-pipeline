"""
Mask Validation Window — окно валидации масок (Phase 4).

Содержит 2 вкладки:
- Junction/Bridge: SquareMaskEditor (рисование квадратами)
- Pipe: PolylineMaskEditor (рисование полилиниями + ластик)

Загружает артефакты из API, сохраняет валидированные маски обратно.
"""

import logging
import tempfile
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QToolBar, QPushButton, QLabel,
    QStatusBar, QMessageBox, QSlider,
    QApplication,
)
from PySide6.QtCore import Qt, Signal, Slot, QThread, QObject

from ui.services.api_client import APIClient, APIError

logger = logging.getLogger(__name__)


class ArtifactDownloader(QObject):
    """Фоновая загрузка артефактов из API."""

    finished = Signal(dict)
    error = Signal(str)
    progress = Signal(str)

    def __init__(self, api_client: APIClient, uid: str, temp_dir: Path):
        super().__init__()
        self.api_client = api_client
        self.uid = uid
        self.temp_dir = temp_dir

    def run(self):
        artifacts = {}
        required = [
            ("original_image", "original.png"),
            ("skeleton", "skeleton.png"),
            ("skeleton_mask", "skeleton_mask.png"),
        ]
        optional = [
            ("coco_validated", "coco_validated.json"),
        ]

        try:
            for art_type, filename in required:
                self.progress.emit(f"Загрузка {art_type}...")
                dest = self.temp_dir / filename
                self.api_client.download_artifact(self.uid, art_type, dest)
                artifacts[art_type] = dest

            for art_type, filename in optional:
                try:
                    self.progress.emit(f"Загрузка {art_type}...")
                    dest = self.temp_dir / filename
                    self.api_client.download_artifact(self.uid, art_type, dest)
                    artifacts[art_type] = dest
                except APIError:
                    logger.info("Optional artifact %s not available", art_type)

            self.finished.emit(artifacts)

        except Exception as exc:
            self.error.emit(str(exc))


class MaskValidationWindow(QMainWindow):
    """
    Окно валидации масок.

    Открывается для конкретной диаграммы из главного окна.
    Содержит 2 вкладки: Junction/Bridge и Pipe.
    """

    validation_completed = Signal(str)  # uid
    window_closed = Signal(str)  # uid

    def __init__(
        self,
        diagram_uid: str,
        diagram_name: str,
        api_client: APIClient,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)

        self.uid = diagram_uid
        self.diagram_name = diagram_name
        self.api_client = api_client

        self._temp_dir_obj = tempfile.TemporaryDirectory(prefix="pid_masks_")
        self.temp_dir = Path(self._temp_dir_obj.name)

        # State
        self._artifacts: dict = {}
        self._junction_editor = None
        self._pipe_editor = None
        self._junction_saved = False
        self._pipe_saved = False
        self._junction_undo_baseline = 0
        self._pipe_undo_baseline = 0

        self.setWindowTitle(f"Валидация масок — {diagram_name}")
        self.setMinimumSize(1200, 800)
        self.showMaximized()

        self._setup_ui()
        self._setup_statusbar()
        self._download_artifacts()

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        # === Toolbar ===
        self.toolbar = QToolBar("Mask Validation")
        self.toolbar.setMovable(False)
        self.addToolBar(self.toolbar)

        # --- Junction tools ---
        self.junction_tools = QWidget()
        jt_layout = QHBoxLayout(self.junction_tools)
        jt_layout.setContentsMargins(4, 0, 4, 0)
        jt_layout.setSpacing(4)

        self.btn_class1 = QPushButton("⬜ Junction (1)")
        self.btn_class1.setCheckable(True)
        self.btn_class1.setChecked(True)
        self.btn_class1.setStyleSheet(
            "QPushButton:checked { background-color: #4CAF50; color: white; }"
        )
        self.btn_class1.clicked.connect(lambda: self._set_junction_class(1))
        jt_layout.addWidget(self.btn_class1)

        self.btn_class2 = QPushButton("🟥 Bridge (2)")
        self.btn_class2.setCheckable(True)
        self.btn_class2.setStyleSheet(
            "QPushButton:checked { background-color: #F44336; color: white; }"
        )
        self.btn_class2.clicked.connect(lambda: self._set_junction_class(2))
        jt_layout.addWidget(self.btn_class2)

        jt_layout.addWidget(QLabel(" Размер:"))
        self.square_slider = QSlider(Qt.Horizontal)
        self.square_slider.setRange(3, 15)
        self.square_slider.setValue(15)
        self.square_slider.setMaximumWidth(120)
        self.square_slider.valueChanged.connect(self._on_square_size_changed)
        jt_layout.addWidget(self.square_slider)
        self.square_label = QLabel("15px")
        jt_layout.addWidget(self.square_label)

        jt_layout.addWidget(
            QLabel("  |  Ctrl+LMB: квадрат, Ctrl+RMB: удалить, 1/2: класс")
        )

        self.toolbar.addWidget(self.junction_tools)

        # --- Pipe tools ---
        self.pipe_tools = QWidget()
        pt_layout = QHBoxLayout(self.pipe_tools)
        pt_layout.setContentsMargins(4, 0, 4, 0)
        pt_layout.setSpacing(4)

        self.btn_polyline = QPushButton("✏️ Полилиния")
        self.btn_polyline.setCheckable(True)
        self.btn_polyline.setChecked(True)
        self.btn_polyline.setStyleSheet(
            "QPushButton:checked { background-color: #4CAF50; color: white; }"
        )
        self.btn_polyline.clicked.connect(lambda: self._set_pipe_tool("polyline"))
        pt_layout.addWidget(self.btn_polyline)

        self.btn_eraser = QPushButton("🧹 Ластик")
        self.btn_eraser.setCheckable(True)
        self.btn_eraser.setStyleSheet(
            "QPushButton:checked { background-color: #FF9800; color: white; }"
        )
        self.btn_eraser.clicked.connect(lambda: self._set_pipe_tool("eraser"))
        pt_layout.addWidget(self.btn_eraser)

        pt_layout.addWidget(QLabel(" Ширина:"))
        self.width_slider = QSlider(Qt.Horizontal)
        self.width_slider.setRange(2, 12)
        self.width_slider.setValue(4)
        self.width_slider.setMaximumWidth(120)
        self.width_slider.valueChanged.connect(self._on_width_changed)
        pt_layout.addWidget(self.width_slider)
        self.width_label = QLabel("4px")
        pt_layout.addWidget(self.width_label)

        pt_layout.addWidget(
            QLabel("  |  Ctrl+LMB: точки, Enter: завершить, E: ластик")
        )

        self.toolbar.addWidget(self.pipe_tools)
        self.pipe_tools.hide()

        # --- Common tools ---
        self.toolbar.addSeparator()

        btn_undo = QPushButton("↩ Undo (Ctrl+Z)")
        btn_undo.clicked.connect(self._undo)
        self.toolbar.addWidget(btn_undo)

        self.toolbar.addSeparator()

        btn_save = QPushButton("💾 Сохранить вкладку")
        btn_save.clicked.connect(self._save_current_tab)
        self.toolbar.addWidget(btn_save)

        btn_complete = QPushButton("✅ Завершить валидацию")
        btn_complete.setStyleSheet(
            "background-color: #4CAF50; color: white; font-weight: bold; padding: 4px 12px;"
        )
        btn_complete.clicked.connect(self._complete_validation)
        self.toolbar.addWidget(btn_complete)

        # === Tabs ===
        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs)

        # Placeholder
        self.loading_label = QLabel("⏳ Загрузка артефактов...")
        self.loading_label.setAlignment(Qt.AlignCenter)
        self.loading_label.setStyleSheet("font-size: 18px; color: #666;")
        layout.addWidget(self.loading_label)
        self.tabs.hide()

    def _setup_statusbar(self):
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)

    # === Download ===

    def _download_artifacts(self):
        self._download_thread = QThread()
        self._downloader = ArtifactDownloader(
            self.api_client, self.uid, self.temp_dir
        )
        self._downloader.moveToThread(self._download_thread)

        self._download_thread.started.connect(self._downloader.run)
        self._downloader.finished.connect(self._on_artifacts_downloaded)
        self._downloader.error.connect(self._on_download_error)
        self._downloader.progress.connect(
            lambda msg: self.statusbar.showMessage(msg)
        )

        self._download_thread.start()

    @Slot(dict)
    def _on_artifacts_downloaded(self, artifacts: dict):
        self._artifacts = artifacts
        self._download_thread.quit()
        self._download_thread.wait()

        self.loading_label.hide()
        self.tabs.show()

        try:
            self._init_editors()
            self.statusbar.showMessage("Артефакты загружены", 3000)
        except Exception as exc:
            logger.error("Failed to init editors: %s", exc, exc_info=True)
            QMessageBox.critical(
                self, "Ошибка",
                f"Не удалось инициализировать редакторы:\n{exc}"
            )

    @Slot(str)
    def _on_download_error(self, error_msg: str):
        self._download_thread.quit()
        self._download_thread.wait()

        self.loading_label.setText(f"❌ Ошибка загрузки: {error_msg}")
        QMessageBox.critical(
            self, "Ошибка",
            f"Не удалось загрузить артефакты:\n{error_msg}"
        )

    # === Init editors ===

    def _init_editors(self):
        """Создать редакторы после загрузки артефактов."""
        # Lazy imports
        from ui.editors.square_mask_editor import SquareMaskEditor
        from ui.editors.polyline_mask_editor import PolylineMaskEditor

        a = self._artifacts

        # Tab 1: Junction/Bridge — только если маски были загружены
        # (В новом pipeline junction маски создаются ПОСЛЕ валидации масок,
        #  поэтому на этапе mask validation их ещё нет)
        if "junction_mask" in a and "bridge_mask" in a:
            self._junction_editor = SquareMaskEditor()
            self._junction_editor.load_images(
                original_path=str(a["original_image"]),
                mask1_path=str(a["junction_mask"]),
                mask2_path=str(a["bridge_mask"]),
                skeleton_path=str(a["skeleton"]),
            )
            self.tabs.addTab(self._junction_editor, "🔲 Junction / Bridge")
            self._junction_undo_baseline = len(self._junction_editor.undo_stack)

        # Tab: Pipe
        self._pipe_editor = PolylineMaskEditor()
        self._pipe_editor.load_images(
            original_path=str(a["original_image"]),
            mask_path=str(a["skeleton_mask"]),
            coco_path=str(a.get("coco_validated", "")),
        )
        self.tabs.addTab(self._pipe_editor, "🔗 Pipe Mask")
        self._pipe_undo_baseline = len(self._pipe_editor.undo_stack)

        self._on_tab_changed(0)

    # === Tab switching ===

    @Slot(int)
    def _on_tab_changed(self, index: int):
        is_junction = self.tabs.currentWidget() is self._junction_editor
        self.junction_tools.setVisible(is_junction)
        self.pipe_tools.setVisible(not is_junction)

    # === Junction tools ===

    def _set_junction_class(self, cls: int):
        """Переключить класс: 1=junction (белая), 2=bridge (красная)."""
        self.btn_class1.setChecked(cls == 1)
        self.btn_class2.setChecked(cls == 2)
        if self._junction_editor:
            self._junction_editor.current_class = cls
            label = "junction (белая)" if cls == 1 else "bridge (красная)"
            self.statusbar.showMessage(f"Класс: {label}", 2000)

    # === Pipe tools ===

    def _set_pipe_tool(self, tool: str):
        self.btn_polyline.setChecked(tool == "polyline")
        self.btn_eraser.setChecked(tool == "eraser")
        if self._pipe_editor:
            from ui.editors.polyline_mask_editor import PolylineTool
            if tool == "polyline":
                self._pipe_editor.set_tool(PolylineTool.POLYLINE)
            else:
                self._pipe_editor.set_tool(PolylineTool.ERASER)

    @Slot(int)
    def _on_width_changed(self, value: int):
        self.width_label.setText(f"{value}px")
        if self._pipe_editor:
            self._pipe_editor.set_line_width(value)

    def _on_square_size_changed(self, value: int):
        self.square_label.setText(f"{value}px")
        if self._junction_editor:
            self._junction_editor.set_square_size(value)

    # === Undo ===

    @Slot()
    def _undo(self):
        current_widget = self.tabs.currentWidget()
        if current_widget is self._junction_editor and self._junction_editor:
            self._junction_editor.undo()
        elif current_widget is self._pipe_editor and self._pipe_editor:
            self._pipe_editor.undo()

    # === Change detection ===

    def _has_unsaved_changes(self) -> bool:
        junction_changed = False
        pipe_changed = False

        if self._junction_editor and not self._junction_saved:
            junction_changed = (
                len(self._junction_editor.undo_stack) > self._junction_undo_baseline
            )

        if self._pipe_editor and not self._pipe_saved:
            pipe_changed = (
                len(self._pipe_editor.undo_stack) > self._pipe_undo_baseline
            )

        return junction_changed or pipe_changed

    # === Save ===

    @Slot()
    def _save_current_tab(self):
        current_widget = self.tabs.currentWidget()
        if current_widget is self._junction_editor:
            self._save_junction_masks()
        elif current_widget is self._pipe_editor:
            self._save_pipe_mask()

    def _save_junction_masks(self):
        if not self._junction_editor:
            return

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)

            junction_path = self.temp_dir / "junction_mask_validated.png"
            bridge_path = self.temp_dir / "bridge_mask_validated.png"
            self._junction_editor.save_masks(
                str(junction_path), str(bridge_path)
            )

            self.statusbar.showMessage("Загрузка junction mask...")
            self.api_client.upload_validated_mask(
                self.uid, "junction_mask_validated", junction_path
            )

            self.statusbar.showMessage("Загрузка bridge mask...")
            self.api_client.upload_validated_mask(
                self.uid, "bridge_mask_validated", bridge_path
            )

            self._junction_saved = True
            self._junction_undo_baseline = len(
                self._junction_editor.undo_stack
            )
            self.statusbar.showMessage(
                "✅ Junction/Bridge маски сохранены", 5000
            )

        except Exception as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось сохранить junction маски:\n{exc}"
            )
        finally:
            QApplication.restoreOverrideCursor()

    def _save_pipe_mask(self):
        if not self._pipe_editor:
            return

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)

            pipe_path = self.temp_dir / "pipe_mask_validated.png"
            self._pipe_editor.save_mask(str(pipe_path))

            self.statusbar.showMessage("Загрузка pipe mask...")
            self.api_client.upload_validated_mask(
                self.uid, "pipe_mask_validated", pipe_path
            )

            self._pipe_saved = True
            self._pipe_undo_baseline = len(self._pipe_editor.undo_stack)
            self.statusbar.showMessage("✅ Pipe маска сохранена", 5000)

        except Exception as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось сохранить pipe маску:\n{exc}"
            )
        finally:
            QApplication.restoreOverrideCursor()

    # === Complete ===

    @Slot()
    def _complete_validation(self):
        if not self._junction_saved:
            reply = QMessageBox.question(
                self, "Сохранение",
                "Junction/Bridge маски не сохранены. Сохранить?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if reply == QMessageBox.Cancel:
                return
            if reply == QMessageBox.Yes:
                self._save_junction_masks()
                if not self._junction_saved:
                    return

        if not self._pipe_saved:
            reply = QMessageBox.question(
                self, "Сохранение",
                "Pipe маска не сохранена. Сохранить?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if reply == QMessageBox.Cancel:
                return
            if reply == QMessageBox.Yes:
                self._save_pipe_mask()
                if not self._pipe_saved:
                    return

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            result = self.api_client.complete_mask_validation(self.uid)
            QApplication.restoreOverrideCursor()

            task_id = result.get("task_id")
            msg = "Валидация масок завершена!"
            if task_id:
                msg += f"\nСкелетизация запущена (task: {task_id[:8]}...)"

            QMessageBox.information(self, "Готово", msg)
            self.validation_completed.emit(self.uid)
            self.close()

        except APIError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось завершить валидацию:\n{exc.message}"
            )

    # === Close ===

    def closeEvent(self, event):
        """Спросить о сохранении при закрытии если есть изменения."""
        if self._has_unsaved_changes():
            reply = QMessageBox.question(
                self, "Закрытие",
                "Есть несохранённые изменения. Сохранить перед закрытием?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            )
            if reply == QMessageBox.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.Save:
                self._save_junction_masks()
                self._save_pipe_mask()

        self.window_closed.emit(self.uid)

        try:
            self._temp_dir_obj.cleanup()
        except Exception:
            pass

        super().closeEvent(event)

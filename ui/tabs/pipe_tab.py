"""
Pipe Validation Tab — вкладка валидации маски труб.

Рефакторинг pipe-части из MaskValidationWindow.
"""

import logging
import tempfile
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QMessageBox, QSlider, QApplication,
)
from PySide6.QtCore import Signal, Slot, Qt, QThread, QObject

from ui.services.api_client import APIClient, APIError

logger = logging.getLogger(__name__)


class _PipeArtifactDownloader(QObject):
    """Загрузчик артефактов для pipe tab (параллельный)."""
    finished = Signal(dict)
    error = Signal(str)
    progress = Signal(str)

    def __init__(self, api_client: APIClient, uid: str, temp_dir: Path):
        super().__init__()
        self.api_client = api_client
        self.uid = uid
        self.temp_dir = temp_dir

    def _download_one(self, art_type: str, filename: str, required: bool):
        """Скачать один артефакт. Возвращает (art_type, path) или None."""
        dest = self.temp_dir / filename
        try:
            self.api_client.download_artifact(self.uid, art_type, dest)
            return (art_type, dest)
        except (APIError, Exception):
            if required:
                raise
            return None

    def _dl_mask(self):
        """pipe_mask_validated → fallback skeleton_mask."""
        dest = self.temp_dir / "mask.png"
        try:
            self.api_client.download_artifact(self.uid, "pipe_mask_validated", dest)
            return ("pipe_mask_validated", dest)
        except (APIError, Exception):
            self.api_client.download_artifact(self.uid, "skeleton_mask", dest)
            return ("skeleton_mask", dest)

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        try:
            self.progress.emit("Загрузка артефактов...")
            artifacts = {}

            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(self._download_one, "original_image", "original.png", True),
                    pool.submit(self._dl_mask),
                    pool.submit(self._download_one, "coco_validated", "coco_validated.json", False),
                    pool.submit(self._download_one, "segmentation_mask", "segmentation_mask.png", False),
                ]
                for future in as_completed(futures):
                    result = future.result()  # raises if required failed
                    if result:
                        art_type, dest = result
                        artifacts[art_type] = dest
                        self.progress.emit(f"Загружен {art_type}")

            self.finished.emit(artifacts)
        except Exception as exc:
            self.error.emit(str(exc))


class PipeTab(QWidget):
    """Вкладка валидации pipe маски."""

    confirmed = Signal()          # Подтверждено
    status_message = Signal(str)  # Сообщение для статусбара

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

        self._temp_dir_obj = tempfile.TemporaryDirectory(prefix="pid_pipe_")
        self.temp_dir = Path(self._temp_dir_obj.name)

        self._editor = None
        self._saved = False
        self._confirmed = False
        self._undo_baseline = 0
        self._project_code: Optional[str] = None

        self._setup_ui()
        self._download_artifacts()

    def set_project_code(self, project_code: str):
        """Set project code for loading equipment classes."""
        self._project_code = project_code

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # === Toolbar ===
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(8, 4, 8, 4)
        toolbar.setSpacing(8)

        self.btn_polyline = QPushButton("✏️ Полилиния")
        self.btn_polyline.setCheckable(True)
        self.btn_polyline.setChecked(True)
        self.btn_polyline.setStyleSheet(
            "QPushButton:checked { background-color: #4CAF50; color: white; }"
        )
        self.btn_polyline.clicked.connect(lambda: self._set_tool("polyline"))
        toolbar.addWidget(self.btn_polyline)

        self.btn_eraser = QPushButton("🧹 Ластик")
        self.btn_eraser.setCheckable(True)
        self.btn_eraser.setStyleSheet(
            "QPushButton:checked { background-color: #FF9800; color: white; }"
        )
        self.btn_eraser.clicked.connect(lambda: self._set_tool("eraser"))
        toolbar.addWidget(self.btn_eraser)

        self.btn_add_node = QPushButton("📦 Добавить узел")
        self.btn_add_node.setCheckable(True)
        self.btn_add_node.setToolTip(
            "Выбрать класс оборудования и нарисовать bbox на схеме (Ctrl+LMB drag)"
        )
        self.btn_add_node.setStyleSheet(
            "QPushButton:checked { background-color: #2196F3; color: white; }"
        )
        self.btn_add_node.clicked.connect(self._on_add_node_clicked)
        toolbar.addWidget(self.btn_add_node)

        toolbar.addWidget(QLabel(" Ширина:"))
        self.width_slider = QSlider(Qt.Horizontal)
        self.width_slider.setRange(2, 12)
        self.width_slider.setValue(4)
        self.width_slider.setMaximumWidth(120)
        self.width_slider.valueChanged.connect(self._on_width_changed)
        toolbar.addWidget(self.width_slider)
        self.width_label = QLabel("4px")
        toolbar.addWidget(self.width_label)

        toolbar.addWidget(
            QLabel("  |  Ctrl+LMB: точки/bbox, Enter: завершить, E: ластик")
        )

        toolbar.addStretch()

        btn_undo = QPushButton("↩ Undo")
        btn_undo.clicked.connect(self._undo)
        toolbar.addWidget(btn_undo)

        btn_save = QPushButton("💾 Сохранить")
        btn_save.clicked.connect(self._save_mask)
        toolbar.addWidget(btn_save)

        self.btn_confirm = QPushButton("✅ Подтвердить")
        self.btn_confirm.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                font-weight: bold;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #45a049; }
            QPushButton:disabled { background-color: #9E9E9E; }
        """)
        self.btn_confirm.clicked.connect(self._on_confirm)
        toolbar.addWidget(self.btn_confirm)

        layout.addLayout(toolbar)

        # === Editor placeholder ===
        self.loading_label = QLabel("⏳ Загрузка артефактов...")
        self.loading_label.setAlignment(Qt.AlignCenter)
        self.loading_label.setStyleSheet("font-size: 18px; color: #666;")
        layout.addWidget(self.loading_label)

        self._editor_layout = layout

        # === Status ===
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(
            "color: #888; font-size: 11px; padding: 2px 8px;"
        )
        layout.addWidget(self.status_label)

    # === Download ===

    def _download_artifacts(self):
        self._download_thread = QThread()
        self._downloader = _PipeArtifactDownloader(
            self.api_client, self.uid, self.temp_dir
        )
        self._downloader.moveToThread(self._download_thread)
        self._download_thread.started.connect(self._downloader.run)
        self._downloader.finished.connect(self._on_downloaded)
        self._downloader.error.connect(self._on_download_error)
        self._downloader.progress.connect(
            lambda msg: self.status_label.setText(msg)
        )
        self._download_thread.start()

    @Slot(dict)
    def _on_downloaded(self, artifacts: dict):
        self._download_thread.quit()
        self._download_thread.wait()
        self.loading_label.hide()

        try:
            from ui.editors.polyline_mask_editor import PolylineMaskEditor

            self._editor = PolylineMaskEditor()
            self._editor.status_callback = lambda msg: self.status_label.setText(msg)

            # pipe_mask_validated (если ранее сохранена) → fallback skeleton_mask
            mask_path = artifacts.get("pipe_mask_validated") or artifacts.get("skeleton_mask")

            self._editor.load_images(
                original_path=str(artifacts["original_image"]),
                mask_path=str(mask_path),
                coco_path=str(artifacts.get("coco_validated", "")),
                pipe_mask_path=str(artifacts.get("segmentation_mask", "")),
            )
            self._editor_layout.insertWidget(
                self._editor_layout.count() - 1, self._editor
            )
            self._undo_baseline = len(self._editor.undo_stack)
            self.status_label.setText("Артефакты загружены")
        except Exception as exc:
            logger.error("Failed to init pipe editor: %s", exc, exc_info=True)
            QMessageBox.critical(
                self, "Ошибка",
                f"Не удалось инициализировать редактор:\n{exc}"
            )

    @Slot(str)
    def _on_download_error(self, error_msg: str):
        self._download_thread.quit()
        self._download_thread.wait()
        self.loading_label.setText(f"❌ Ошибка: {error_msg}")

    # === Tools ===

    def _set_tool(self, tool: str):
        from ui.editors.polyline_mask_editor import PolylineTool
        self.btn_polyline.setChecked(tool == "polyline")
        self.btn_eraser.setChecked(tool == "eraser")
        self.btn_add_node.setChecked(tool == "add_node")
        if self._editor:
            if tool == "polyline":
                self._editor.set_tool(PolylineTool.POLYLINE)
            elif tool == "eraser":
                self._editor.set_tool(PolylineTool.ERASER)
            elif tool == "add_node":
                self._editor.set_tool(PolylineTool.ADD_NODE)

    def _on_add_node_clicked(self):
        """Open class selection dialog, then switch to ADD_NODE tool."""
        if not self._editor:
            self.btn_add_node.setChecked(False)
            return

        classes = self._load_project_classes()
        if classes is None:
            self.btn_add_node.setChecked(False)
            return

        from ui.editors.node_list_dialog import NodeListDialog
        dlg = NodeListDialog(classes, self)
        result = dlg.exec()

        if result:
            selected = dlg.get_selected_class()
        else:
            selected = None

        if selected and self._editor:
            self._editor.set_pending_node_class(selected)
            self._set_tool("add_node")
            self.status_label.setText(
                f"Ctrl+LMB drag для добавления: {selected['name']}"
            )
        else:
            self.btn_add_node.setChecked(False)
            self._set_tool("polyline")

    def _load_project_classes(self) -> Optional[list]:
        """Load project classes from API."""
        if not self._project_code:
            try:
                diagram = self.api_client.get_diagram(self.uid)
                self._project_code = diagram.project_code
                logger.info("project_code from API: %s", self._project_code)
            except Exception as exc:
                logger.error("Failed to get project_code: %s", exc)
                self._project_code = "thermohydraulics"

        try:
            result = self.api_client.get_project_classes(self._project_code)
            return result.get("classes", [])
        except Exception as exc:
            logger.error("Failed to load project classes: %s", exc)
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось загрузить классы проекта:\n{exc}"
            )
            return None

    def _on_width_changed(self, value: int):
        self.width_label.setText(f"{value}px")
        if self._editor:
            self._editor.set_line_width(value)

    def _undo(self):
        if self._editor:
            self._editor.undo()

    # === Save & Confirm ===

    def has_unsaved_changes(self) -> bool:
        if self._editor and not self._saved:
            return len(self._editor.undo_stack) > self._undo_baseline
        return False

    def _save_mask(self) -> bool:
        """Сохранить маску + обновлённый COCO на сервер. Возвращает True при успехе."""
        if not self._editor:
            return False

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)

            # 1. Save pipe mask
            pipe_path = self.temp_dir / "pipe_mask_validated.png"
            self._editor.save_mask(str(pipe_path))

            self.status_label.setText("Загрузка pipe mask...")
            self.api_client.upload_validated_mask(
                self.uid, "pipe_mask_validated", pipe_path
            )

            # 2. Save updated COCO + regenerate node_mask (if nodes were added)
            if self._editor.has_coco_changes:
                coco_path = self.temp_dir / "coco_validated.json"
                if self._editor.save_coco(str(coco_path)):
                    self.status_label.setText("Загрузка обновлённых узлов...")
                    self.api_client.upload_updated_nodes(self.uid, coco_path)
                    logger.info("COCO + node_mask updated on server")

            self._saved = True
            self._undo_baseline = len(self._editor.undo_stack)
            self.status_label.setText("✅ Pipe маска сохранена")
            return True

        except Exception as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось сохранить:\n{exc}"
            )
            return False
        finally:
            QApplication.restoreOverrideCursor()

    @Slot()
    def _on_confirm(self):
        """Подтвердить: сохранить + emit confirmed."""
        logger.info("Pipe _on_confirm called, editor=%s", self._editor is not None)
        if self._save_mask():
            self._confirmed = True
            logger.info("Pipe mask saved, emitting confirmed")
            self.status_message.emit("✅ Pipe маска подтверждена")
            self.confirmed.emit()
        else:
            logger.warning("Pipe _save_mask returned False")

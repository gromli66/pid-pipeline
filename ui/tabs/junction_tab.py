"""
Junction/Bridge Validation Tab — вкладка валидации масок перекрёстков и мостов.

Рефакторинг junction-части из MaskValidationWindow.
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


class _JunctionArtifactDownloader(QObject):
    """Загрузчик артефактов для junction tab (параллельный)."""
    finished = Signal(dict)
    error = Signal(str)
    progress = Signal(str)

    def __init__(self, api_client: APIClient, uid: str, temp_dir: Path):
        super().__init__()
        self.api_client = api_client
        self.uid = uid
        self.temp_dir = temp_dir

    def _dl(self, art_type: str, dest: Path) -> bool:
        """Скачать артефакт, вернуть True при успехе."""
        try:
            self.api_client.download_artifact(self.uid, art_type, dest)
            return True
        except (APIError, Exception):
            return False

    def _dl_original(self):
        dest = self.temp_dir / "original.png"
        self.api_client.download_artifact(self.uid, "original_image", dest)
        return ("original_image", dest)

    def _dl_junction_mask(self):
        dest = self.temp_dir / "junction_mask.png"
        if not self._dl("junction_mask_validated", dest):
            self.api_client.download_artifact(self.uid, "junction_mask", dest)
        return ("junction_mask", dest)

    def _dl_bridge_mask(self):
        dest = self.temp_dir / "bridge_mask.png"
        if self._dl("bridge_mask_validated", dest):
            return ("bridge_mask", dest)
        if self._dl("bridge_mask", dest):
            return ("bridge_mask", dest)
        return None

    def _dl_skeleton(self):
        dest = self.temp_dir / "skeleton.png"
        if self._dl("skeleton_final", dest):
            return ("skeleton", dest)
        if self._dl("skeleton", dest):
            return ("skeleton", dest)
        return None

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        try:
            self.progress.emit("Загрузка артефактов...")
            artifacts = {}

            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(self._dl_original),
                    pool.submit(self._dl_junction_mask),
                    pool.submit(self._dl_bridge_mask),
                    pool.submit(self._dl_skeleton),
                ]
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        art_type, dest = result
                        artifacts[art_type] = dest
                        self.progress.emit(f"Загружен {art_type}")

            self.finished.emit(artifacts)
        except Exception as exc:
            self.error.emit(str(exc))


class JunctionTab(QWidget):
    """Вкладка валидации junction/bridge масок."""

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

        self._temp_dir_obj = tempfile.TemporaryDirectory(prefix="pid_junction_")
        self.temp_dir = Path(self._temp_dir_obj.name)

        self._editor = None
        self._saved = False
        self._confirmed = False
        self._undo_baseline = 0

        self._setup_ui()
        self._download_artifacts()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # === Toolbar ===
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(8, 4, 8, 4)
        toolbar.setSpacing(8)

        self.btn_class1 = QPushButton("⬜ Junction (1)")
        self.btn_class1.setCheckable(True)
        self.btn_class1.setChecked(True)
        self.btn_class1.setStyleSheet(
            "QPushButton:checked { background-color: #4CAF50; color: white; }"
        )
        self.btn_class1.clicked.connect(lambda: self._set_class(1))
        toolbar.addWidget(self.btn_class1)

        self.btn_class2 = QPushButton("🟥 Bridge (2)")
        self.btn_class2.setCheckable(True)
        self.btn_class2.setStyleSheet(
            "QPushButton:checked { background-color: #F44336; color: white; }"
        )
        self.btn_class2.clicked.connect(lambda: self._set_class(2))
        toolbar.addWidget(self.btn_class2)

        toolbar.addWidget(QLabel(" Размер:"))
        self.square_slider = QSlider(Qt.Horizontal)
        self.square_slider.setRange(3, 15)
        self.square_slider.setValue(15)
        self.square_slider.setMaximumWidth(120)
        self.square_slider.valueChanged.connect(self._on_square_size_changed)
        toolbar.addWidget(self.square_slider)
        self.square_label = QLabel("15px")
        toolbar.addWidget(self.square_label)

        toolbar.addWidget(
            QLabel("  |  Ctrl+ЛКМ: квадрат | Shift+drag: обводка | Ctrl+ПКМ: удалить | 1/2: класс | Esc: отмена")
        )

        toolbar.addStretch()

        btn_undo = QPushButton("↩ Undo")
        btn_undo.clicked.connect(self._undo)
        toolbar.addWidget(btn_undo)

        btn_save = QPushButton("💾 Сохранить")
        btn_save.clicked.connect(self._save_masks)
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

        # Editor будет добавлен сюда после загрузки
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
        self._downloader = _JunctionArtifactDownloader(
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
            from ui.editors.square_mask_editor import SquareMaskEditor

            self._editor = SquareMaskEditor()
            self._editor.status_callback = lambda msg: self.status_label.setText(msg)
            self._editor.load_images(
                original_path=str(artifacts["original_image"]),
                mask1_path=str(artifacts["junction_mask"]),
                mask2_path=str(artifacts.get("bridge_mask", "")),
                skeleton_path=str(artifacts.get("skeleton", "")),
            )
            # Вставляем перед status_label (последний виджет)
            self._editor_layout.insertWidget(
                self._editor_layout.count() - 1, self._editor
            )
            self._undo_baseline = len(self._editor.undo_stack)
            self.status_label.setText("Артефакты загружены")
        except Exception as exc:
            logger.error("Failed to init junction editor: %s", exc, exc_info=True)
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

    def _set_class(self, cls: int):
        self.btn_class1.setChecked(cls == 1)
        self.btn_class2.setChecked(cls == 2)
        if self._editor:
            self._editor.current_class = cls

    def _on_square_size_changed(self, value: int):
        self.square_label.setText(f"{value}px")
        if self._editor:
            self._editor.set_square_size(value)

    def _undo(self):
        if self._editor:
            self._editor.undo()

    # === Save & Confirm ===

    def has_unsaved_changes(self) -> bool:
        if self._editor and not self._saved:
            return len(self._editor.undo_stack) > self._undo_baseline
        return False

    def _save_masks(self) -> bool:
        """Сохранить маски на сервер. Возвращает True при успехе."""
        if not self._editor:
            return False

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)

            junction_path = self.temp_dir / "junction_mask_validated.png"
            bridge_path = self.temp_dir / "bridge_mask_validated.png"
            self._editor.save_masks(str(junction_path), str(bridge_path))

            self.status_label.setText("Загрузка junction mask...")
            self.api_client.upload_validated_mask(
                self.uid, "junction_mask_validated", junction_path
            )

            self.status_label.setText("Загрузка bridge mask...")
            self.api_client.upload_validated_mask(
                self.uid, "bridge_mask_validated", bridge_path
            )

            self._saved = True
            self._undo_baseline = len(self._editor.undo_stack)
            self.status_label.setText("✅ Junction/Bridge маски сохранены")
            return True

        except Exception as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось сохранить junction маски:\n{exc}"
            )
            return False
        finally:
            QApplication.restoreOverrideCursor()

    @Slot()
    def _on_confirm(self):
        """Подтвердить: сохранить + emit confirmed."""
        logger.info("Junction _on_confirm called, editor=%s", self._editor is not None)
        if self._save_masks():
            self._confirmed = True
            logger.info("Junction masks saved, emitting confirmed")
            self.status_message.emit("✅ Junction/Bridge маски подтверждены")
            self.confirmed.emit()
        else:
            logger.warning("Junction _save_masks returned False")

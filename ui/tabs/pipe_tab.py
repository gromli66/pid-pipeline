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
from PySide6.QtCore import Signal, Slot, Qt, QThread

from ui.services.api_client import APIClient
from ui.services.artifact_downloader import (
    ArtifactDownloader, Job, artifact, one,
)
from ui.widgets.appearance_panel import AppearanceMixin
from ui.widgets.toolbar_buttons import (
    make_undo_button, make_save_button, make_confirm_button,
)

logger = logging.getLogger(__name__)


#: Что вкладка тянет с сервера. У маски ключ = сработавший кандидат:
#: `_on_downloaded` читает pipe_mask_validated, потом skeleton_mask.
#: ⛔ Маска сегментации — `pipe_mask` (выход этапа сегментации). Раньше здесь
#: стоял `segmentation_mask`, которого нет в `ArtifactType`: эндпоинт проверяет
#: тип ДО поиска артефакта (`app/api/diagrams.py:369-376`) и отвечал 400,
#: а необязательное задание его глотало — медианная толщина труб не считалась
#: никогда. Тип обязан быть значением `ArtifactType`.
_ARTIFACTS = (
    one(artifact("original_image", "original.png"), required=True),
    Job((artifact("pipe_mask_validated", "mask.png"),
         artifact("skeleton_mask", "mask.png")), required=True),
    one(artifact("coco_validated", "coco_validated.json")),
    one(artifact("pipe_mask", "pipe_mask.png")),
)


class PipeTab(AppearanceMixin, QWidget):
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

    def _appearance_editor(self):
        return self._editor

    def _build_appearance_controls(self, panel):
        from PySide6.QtGui import QColor
        self._add_bg_darkness_slider(panel)
        self._add_color_setting(
            panel, "Цвет маски", "mask_color", QColor(255, 255, 255),
            lambda c: self._editor and self._editor.set_mask_color(c),
        )
        self._add_pct_setting(
            panel, "Яркость маски", "mask_brightness", 100.0,
            lambda v: self._editor and self._editor.set_mask_brightness(v), 20, 100,
        )
        self._add_pct_setting(
            panel, "Прозрачность маски", "mask_opacity", 50.0,
            lambda v: self._editor and self._editor.set_mask_opacity(v), 0, 100,
        )

    def apply_saved_appearance(self):
        super().apply_saved_appearance()
        ed = self._editor
        if ed is None:
            return
        from PySide6.QtGui import QColor
        self._apply_saved_color("mask_color", QColor(255, 255, 255), ed.set_mask_color)
        self._apply_saved_pct("mask_brightness", 100.0, ed.set_mask_brightness)
        self._apply_saved_pct("mask_opacity", 50.0, ed.set_mask_opacity)

    def apply_default_appearance(self):
        super().apply_default_appearance()
        ed = self._editor
        if ed is None:
            return
        from PySide6.QtGui import QColor
        ed.set_mask_color(QColor(255, 255, 255))
        ed.set_mask_brightness(100.0)
        ed.set_mask_opacity(50.0)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # === Toolbar ===
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(8, 4, 8, 4)
        toolbar.setSpacing(8)

        # Undo слева (единая); redo нет — редактор маски его не поддерживает
        self.btn_undo = make_undo_button(self._undo)
        toolbar.addWidget(self.btn_undo)

        self.btn_polyline = QPushButton("Нарисовать линию")
        self.btn_polyline.setCheckable(True)
        self.btn_polyline.setChecked(True)
        self.btn_polyline.setToolTip(
            "Рисование линии трубы.\n"
            "Ctrl+ЛКМ — ставить точки, Enter / ПКМ / двойной клик — завершить, "
            "Esc — отменить.\nCtrl+колесо — менять толщину."
        )
        self.btn_polyline.setStyleSheet(
            "QPushButton:checked { background-color: #4CAF50; color: white; }"
        )
        self.btn_polyline.clicked.connect(lambda: self._set_tool("polyline"))
        toolbar.addWidget(self.btn_polyline)

        self.btn_eraser = QPushButton("Ластик")
        self.btn_eraser.setCheckable(True)
        self.btn_eraser.setToolTip(
            "Стирание маски труб.\n"
            "Ctrl+ЛКМ — стирать кистью, Shift+ЛКМ — стереть прямоугольником.\n"
            "Ctrl+колесо — менять размер кисти."
        )
        self.btn_eraser.setStyleSheet(
            "QPushButton:checked { background-color: #FF9800; color: white; }"
        )
        self.btn_eraser.clicked.connect(lambda: self._set_tool("eraser"))
        toolbar.addWidget(self.btn_eraser)

        self.btn_add_node = QPushButton("Добавить узел")
        self.btn_add_node.setCheckable(True)
        self.btn_add_node.setToolTip(
            "Добавление узла оборудования.\n"
            "Выберите класс, затем Ctrl+ЛКМ с протяжкой — нарисовать bbox на схеме."
        )
        self.btn_add_node.setStyleSheet(
            "QPushButton:checked { background-color: #2196F3; color: white; }"
        )
        self.btn_add_node.clicked.connect(self._on_add_node_clicked)
        toolbar.addWidget(self.btn_add_node)

        self.btn_endpoints = QPushButton("🔴 Эндпоинты")
        self.btn_endpoints.setCheckable(True)
        self.btn_endpoints.setChecked(True)
        self.btn_endpoints.setToolTip(
            "Показ концов труб — разрывов цепи узел→узел.\n"
            "Нажмите, чтобы скрыть или показать маркеры."
        )
        self.btn_endpoints.setStyleSheet(
            "QPushButton:checked { background-color: #E53935; color: white; }"
        )
        self.btn_endpoints.toggled.connect(self._on_toggle_endpoints)
        toolbar.addWidget(self.btn_endpoints)

        self.width_title = QLabel(" Ширина:")
        self.width_title.setToolTip(
            "Толщина линии и размер кисти ластика.\n"
            "По умолчанию — медианная толщина труб на схеме.\n"
            "Также меняется через Ctrl+колесо."
        )
        toolbar.addWidget(self.width_title)
        self.width_slider = QSlider(Qt.Horizontal)
        self.width_slider.setRange(2, 12)
        self.width_slider.setValue(4)
        self.width_slider.setMaximumWidth(120)
        self.width_slider.setToolTip("Толщина линии / размер кисти (Ctrl+колесо)")
        self.width_slider.valueChanged.connect(self._on_width_changed)
        toolbar.addWidget(self.width_slider)
        self.width_label = QLabel("4px")
        toolbar.addWidget(self.width_label)

        toolbar.addStretch()

        self.btn_save = make_save_button(self._save_mask, "Сохранить маску и узлы (Ctrl+S)")
        toolbar.addWidget(self.btn_save)

        self.btn_confirm = make_confirm_button(
            self._on_confirm,
            tooltip="Сохранить и подтвердить валидацию маски труб.\nЗапускает следующий этап.",
        )
        toolbar.addWidget(self.btn_confirm)

        layout.addLayout(toolbar)

        # === Editor placeholder ===
        self.loading_label = QLabel("Загрузка артефактов...")
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
        self._downloader = ArtifactDownloader(
            self.api_client, self.uid, self.temp_dir, _ARTIFACTS
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
            self._editor.width_changed_callback = self._on_editor_width_changed

            # pipe_mask_validated (если ранее сохранена) → fallback skeleton_mask
            mask_path = artifacts.get("pipe_mask_validated") or artifacts.get("skeleton_mask")

            self._editor.load_images(
                original_path=str(artifacts["original_image"]),
                mask_path=str(mask_path),
                coco_path=str(artifacts.get("coco_validated", "")),
                pipe_mask_path=str(artifacts.get("pipe_mask", "")),
            )
            self._editor_layout.insertWidget(
                self._editor_layout.count() - 1, self._editor
            )

            # Стартовая ширина = медианная толщина труб на схеме (clamp к слайдеру)
            median = self._editor.median_thickness
            lo, hi = self.width_slider.minimum(), self.width_slider.maximum()
            start_w = max(lo, min(hi, int(median))) if median else self.width_slider.value()
            self.width_slider.blockSignals(True)
            self.width_slider.setValue(start_w)
            self.width_slider.blockSignals(False)
            self.width_label.setText(f"{start_w}px")
            self._editor.set_line_width(start_w)

            self._undo_baseline = len(self._editor.undo_stack)
            self.apply_saved_appearance()
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
        self.loading_label.setText(f"Ошибка: {error_msg}")

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

    def _on_toggle_endpoints(self, checked: bool):
        if self._editor is not None:
            self._editor.set_endpoints_visible(checked)

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

    def _on_editor_width_changed(self, value: int):
        """Ширина изменена из редактора (Ctrl+колесо) → синхронизировать слайдер."""
        self.width_slider.blockSignals(True)
        self.width_slider.setValue(value)
        self.width_slider.blockSignals(False)
        self.width_label.setText(f"{value}px")

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
            self.status_label.setText("Pipe маска сохранена")
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
            self.status_message.emit("Pipe маска подтверждена")
            self.confirmed.emit()
        else:
            logger.warning("Pipe _save_mask returned False")
            self.status_label.setText("Не удалось сохранить — изменения не подтверждены")

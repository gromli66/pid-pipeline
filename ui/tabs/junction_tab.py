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
    QPushButton, QLabel, QMessageBox, QSlider, QApplication, QSpinBox,
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


#: Что вкладка тянет с сервера: правленое оператором → модельное.
#: Центры квадратов необязательны — на старом сервере типов ещё нет, и вкладка
#: от этого падать не должна (доопределятся из масок экстрактором).
_ARTIFACTS = (
    one(artifact("original_image", "original.png"), required=True),
    Job((artifact("junction_mask_validated", "junction_mask.png",
                  key="junction_mask"),
         artifact("junction_mask", "junction_mask.png")), required=True),
    Job((artifact("bridge_mask_validated", "bridge_mask.png", key="bridge_mask"),
         artifact("bridge_mask", "bridge_mask.png"))),
    Job((artifact("skeleton_final", "skeleton.png", key="skeleton"),
         artifact("skeleton", "skeleton.png"))),
    one(artifact("coco_validated", "coco_validated.json")),
    Job((artifact("junction_points_validated", "points.json", key="points"),
         artifact("junction_points", "points.json", key="points"))),
)


class JunctionTab(AppearanceMixin, QWidget):
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

        # Undo слева (единая кнопка); redo нет — редактор масок его не поддерживает
        self.btn_undo = make_undo_button(self._undo)
        toolbar.addWidget(self.btn_undo)

        self.btn_class1 = QPushButton("⬜ Перекрёсток")
        self.btn_class1.setCheckable(True)
        self.btn_class1.setChecked(True)
        self.btn_class1.setToolTip(
            "Перекрёсток — разветвление или поворот труб (белая маска).\n"
            "Ctrl+ЛКМ — поставить квадрат, Ctrl+ПКМ — удалить.\n"
            "Shift+протяжка — обвести и выделить пятна, Esc — снять выделение.\n"
            "Горячая клавиша: 1."
        )
        self.btn_class1.setStyleSheet(
            "QPushButton { border: 2px solid transparent; border-radius: 6px; padding: 4px 10px; }"
            "QPushButton:checked { background-color: #FFFFFF; color: #1b1b1b; "
            "border: 2px solid #555; }"
        )
        self.btn_class1.clicked.connect(lambda: self._set_class(1))
        toolbar.addWidget(self.btn_class1)

        self.btn_class2 = QPushButton("🟥 Мост")
        self.btn_class2.setCheckable(True)
        self.btn_class2.setToolTip(
            "Мост — труба проходит над другой трубой без соединения (красная маска).\n"
            "Ctrl+ЛКМ — поставить квадрат, Ctrl+ПКМ — удалить.\n"
            "Shift+протяжка — обвести и выделить пятна, Esc — снять выделение.\n"
            "Горячая клавиша: 2."
        )
        self.btn_class2.setStyleSheet(
            "QPushButton { border: 2px solid transparent; border-radius: 6px; padding: 4px 10px; }"
            "QPushButton:checked { background-color: #F44336; color: white; "
            "border: 2px solid #555; }"
        )
        self.btn_class2.clicked.connect(lambda: self._set_class(2))
        toolbar.addWidget(self.btn_class2)

        self.size_title = QLabel(" Размер:")
        self.size_title.setToolTip("Размер квадрата-кисти (px) для текущего класса")
        toolbar.addWidget(self.size_title)
        self.square_slider = QSlider(Qt.Horizontal)
        self.square_slider.setRange(3, 15)
        self.square_slider.setValue(15)
        self.square_slider.setMaximumWidth(120)
        self.square_slider.setToolTip("Размер квадрата-кисти (px)")
        self.square_slider.valueChanged.connect(self._on_square_size_changed)
        toolbar.addWidget(self.square_slider)
        self.square_label = QLabel("15px")
        toolbar.addWidget(self.square_label)

        sep = QLabel(" | ")
        sep.setStyleSheet("color: #666;")
        toolbar.addWidget(sep)

        # Реальный размер СУЩЕСТВУЮЩИХ перекрёстков/мостов — не кисть.
        # Потолок кисти (15) здесь не наследуется: «расширение» может требовать
        # больше. Связка с растеризацией конвейера — configs/projects/*/
        # thermohydraulics.yaml → junction.square_size (сейчас 15).
        toolbar.addWidget(QLabel(" Размер объектов:"))
        self.spin_obj_size = QSpinBox()
        self.spin_obj_size.setRange(3, 100)
        self.spin_obj_size.setValue(15)
        self.spin_obj_size.setSuffix(" px")
        self.spin_obj_size.setToolTip(
            "Реальный размер существующих перекрёстков/мостов.\n"
            "Сжатие/расширение относительно центра квадрата."
        )
        toolbar.addWidget(self.spin_obj_size)

        self.btn_apply_size = QPushButton("Применить размер")
        self.btn_apply_size.setToolTip(
            "Есть выделение (Shift+протяжка) — меняются выделенные пятна.\n"
            "Выделения нет — все пятна текущего класса (с подтверждением)."
        )
        # Кнопка не должна забирать фокус клавиатуры: Ctrl+Z обрабатывает
        # keyPressEvent самого редактора, и с фокусом на кнопке отмена
        # операции размера не срабатывала бы.
        self.btn_apply_size.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_apply_size.clicked.connect(self._apply_object_size)
        toolbar.addWidget(self.btn_apply_size)

        toolbar.addStretch()

        self.btn_save = make_save_button(
            self._save_masks, "Сохранить маски перекрёстков и мостов (Ctrl+S)")
        toolbar.addWidget(self.btn_save)

        self.btn_confirm = make_confirm_button(
            self._on_confirm,
            tooltip="Сохранить и подтвердить валидацию.\nЗапускает построение графа схемы.",
        )
        toolbar.addWidget(self.btn_confirm)

        layout.addLayout(toolbar)

        # === Editor placeholder ===
        self.loading_label = QLabel("Загрузка артефактов...")
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

    def _appearance_editor(self):
        return self._editor

    def _build_appearance_controls(self, panel):
        from PySide6.QtGui import QColor
        self._add_bg_darkness_slider(panel)
        self._add_color_setting(
            panel, "Цвет скелета", "skeleton_color", QColor(0, 255, 0),
            lambda c: self._editor and self._editor.set_skeleton_color(c),
        )

    def apply_saved_appearance(self):
        super().apply_saved_appearance()
        ed = self._editor
        if ed is None:
            return
        from PySide6.QtGui import QColor
        self._apply_saved_color("skeleton_color", QColor(0, 255, 0), ed.set_skeleton_color)

    def apply_default_appearance(self):
        super().apply_default_appearance()
        ed = self._editor
        if ed is None:
            return
        from PySide6.QtGui import QColor
        ed.set_skeleton_color(QColor(0, 255, 0))

    @Slot(dict)
    def _on_downloaded(self, artifacts: dict):
        self._download_thread.quit()
        self._download_thread.wait()
        self.loading_label.hide()

        try:
            from ui.editors.square_mask_editor import SquareMaskEditor

            self._editor = SquareMaskEditor()
            self._editor.status_callback = lambda msg: self.status_label.setText(msg)
            # Ctrl+S в редакторе раньше звал save_masks() без путей — PNG падали
            # в CWD процесса и на сервер не уходили. Теперь шорткат идёт сюда.
            self._editor.save_requested_callback = self._save_masks
            self._editor.load_images(
                original_path=str(artifacts["original_image"]),
                mask1_path=str(artifacts["junction_mask"]),
                mask2_path=str(artifacts.get("bridge_mask", "")),
                skeleton_path=str(artifacts.get("skeleton", "")),
                coco_path=str(artifacts.get("coco_validated", "")),
            )
            self._load_points(artifacts.get("points"))
            # Вставляем перед status_label (последний виджет)
            self._editor_layout.insertWidget(
                self._editor_layout.count() - 1, self._editor
            )
            self._undo_baseline = len(self._editor.undo_stack)
            self.apply_saved_appearance()
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
        self.loading_label.setText(f"Ошибка: {error_msg}")

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

    def _load_points(self, path) -> None:
        """Центры квадратов: файл → редактор; без файла — доопределить из масок."""
        import json

        if self._editor is None:
            return
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._editor.load_points(json.load(f))
            except (OSError, ValueError) as exc:
                logger.warning("points.json не прочитан (%s) — центры из масок", exc)
        self._editor.ensure_points()
        # Спинбокс показывает последний применённый размер (из points-файла).
        sizes = getattr(self._editor, "_applied_size", {})
        if sizes:
            self.spin_obj_size.setValue(int(max(sizes.values())))

    @Slot()
    def _apply_object_size(self):
        """Изменить реальный размер существующих перекрёстков/мостов."""
        if not self._editor:
            return
        size = int(self.spin_obj_size.value())
        has_selection = any(b["confirmed"] for b in self._editor._blobs)
        if not has_selection:
            label = "перекрёстков" if self._editor.current_class == 1 else "мостов"
            answer = QMessageBox.question(
                self, "Изменить размер",
                f"Выделения нет — размер {size}px будет применён ко ВСЕМ "
                f"{label} на схеме.\n\nПродолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            changed = self._editor.apply_square_size(size)
        finally:
            QApplication.restoreOverrideCursor()
        # Вернуть фокус редактору: иначе он остаётся на спинбоксе (там Ctrl+Z
        # отменяет ввод текста), и отмена операции не работает.
        self._editor.setFocus(Qt.FocusReason.OtherFocusReason)
        if changed:
            self._saved = False

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

            self._upload_points()

            self._saved = True
            self._undo_baseline = len(self._editor.undo_stack)
            self.status_label.setText("Маски перекрёстков и мостов сохранены")
            return True

        except Exception as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось сохранить junction маски:\n{exc}"
            )
            return False
        finally:
            QApplication.restoreOverrideCursor()

    def _upload_points(self):
        """Отправить центры квадратов рядом с масками.

        Без этого правленые центры умирают вместе с вкладкой: при переоткрытии
        доопределение пошло бы из устаревшего points.json, а экстрактор с
        дефолтным окном 15 не находит окна в квадратах, ужатых до <15 — фича
        ломала бы собственный фундамент.
        Старый сервер типа не знает — не роняем сохранение масок из-за этого.
        """
        import json

        try:
            path = self.temp_dir / "points_validated.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._editor.export_points(), f, ensure_ascii=False)
            self.status_label.setText("Загрузка центров...")
            self.api_client.upload_validated_mask(
                self.uid, "junction_points_validated", path
            )
        except Exception as exc:
            logger.warning("Центры не сохранены на сервер: %s", exc)

    @Slot()
    def _on_confirm(self):
        """Подтвердить: сохранить + emit confirmed."""
        logger.info("Junction _on_confirm called, editor=%s", self._editor is not None)
        if self._save_masks():
            self._confirmed = True
            logger.info("Junction masks saved, emitting confirmed")
            self.status_message.emit("Маски перекрёстков и мостов подтверждены")
            self.confirmed.emit()
        else:
            logger.warning("Junction _save_masks returned False")

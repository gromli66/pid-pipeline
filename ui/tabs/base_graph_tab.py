"""
Base Graph Tab — базовый класс вкладок редактора графа P&ID.

Template method: скачивание артефактов, сохранение, подтверждение.
Потомки: SimpleGraphTab, AdvancedGraphTab.

Принцип: Base НЕ обращается к атрибутам потомков напрямую.
Вся расширяемость — через _create_editor() и _setup_toolbar().
"""

import json
import logging
import struct
import tempfile
from abc import abstractmethod
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QMessageBox, QApplication,
)
from PySide6.QtCore import Signal, Slot, Qt, QThread, QObject

from ui.services.api_client import APIClient, APIError
from ui.editors.base_graph_editor import BaseGraphEditor
from ui.widgets.appearance_panel import AppearanceMixin
from ui.widgets.toolbar_buttons import (
    make_undo_button, make_redo_button, make_save_button, make_confirm_button,
)

logger = logging.getLogger(__name__)


def _png_size(path: Path):
    """(height, width) PNG из заголовка, без Qt. None если не PNG."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", head[16:24])
            return (h, w)
    except OSError:
        pass
    return None


def _pretransform_to_canvas(graph_path: Path, image_path: Path, out_path: Path) -> bool:
    """WYSIWYG: перевести граф в холст 1920x1080 (фикс-размеры + declust).

    Идемпотентно (уже-1920 граф не трогается). Пишет out_path. True при успехе.
    """
    from modules.graph.core.pretransform import pretransform
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    g, transform, stats = pretransform(graph, image_hw=_png_size(image_path))
    out_path.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    logger.info("pre-transform → холст 1920x1080: %s", stats)
    return True


class _GraphArtifactDownloader(QObject):
    """Фоновый загрузчик артефактов для graph tab."""

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

            # Граф: предпочитаем validated (сохранённый), fallback на json (оригинальный)
            graph_dest = self.temp_dir / "graph.json"
            try:
                self.progress.emit("Загрузка graph_validated...")
                self.api_client.download_artifact(self.uid, "graph_validated", graph_dest)
                artifacts["graph_json"] = graph_dest
                logger.info("Loaded graph_validated")
            except (APIError, Exception):
                self.progress.emit("Загрузка graph_json...")
                self.api_client.download_artifact(self.uid, "graph_json", graph_dest)
                artifacts["graph_json"] = graph_dest
                logger.info("Loaded graph_json (no validated version)")

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


class BaseGraphTab(AppearanceMixin, QWidget):
    """Базовый класс вкладки редактора графа P&ID.

    Template method:
      - скачивание артефактов через _GraphArtifactDownloader
      - _setup_ui() → toolbar (из _setup_toolbar) + loading + status
      - _save_graph() → editor.save_graph + upload_validated_graph
      - _on_confirm() → безусловный save + emit confirmed
      - has_unsaved_changes → undo_mgr.revision != _saved_revision

    Потомки обязаны реализовать:
      _create_editor() → BaseGraphEditor
      _setup_toolbar(toolbar: QHBoxLayout)
    """

    confirmed = Signal()           # Пользователь подтвердил
    status_message = Signal(str)   # Сообщение для статусбара workspace

    @staticmethod
    def _add_separator(toolbar: QHBoxLayout):
        """Добавить визуальный разделитель в toolbar."""
        sep = QLabel(" | ")
        sep.setStyleSheet("color: #666;")
        toolbar.addWidget(sep)

    def __init__(
        self,
        diagram_uid: str,
        diagram_name: str,
        api_client: APIClient,
        parent: Optional[QWidget] = None,
    ):
        if type(self) is BaseGraphTab:
            raise TypeError(
                "BaseGraphTab is abstract, use SimpleGraphTab or AdvancedGraphTab"
            )
        super().__init__(parent)

        self.uid = diagram_uid
        self.diagram_name = diagram_name
        self.api_client = api_client

        self._temp_dir_obj = tempfile.TemporaryDirectory(prefix="pid_graph_")
        self.temp_dir = Path(self._temp_dir_obj.name)

        self._editor: Optional[BaseGraphEditor] = None
        # undo_mgr.revision на момент последнего успешного save
        self._saved_revision: int = 0

        self._setup_ui()
        self._download_artifacts()

    # =================================================================
    # Abstract interface
    # =================================================================

    @abstractmethod
    def _create_editor(self) -> BaseGraphEditor:
        """Создать конкретный экземпляр редактора."""
        ...

    @abstractmethod
    def _setup_toolbar(self, toolbar: QHBoxLayout) -> None:
        """Наполнить toolbar кнопками режимов и инструментов."""
        ...

    def _setup_secondary_toolbar(self, layout: QVBoxLayout) -> None:
        """Опциональный второй ряд тулбара. По умолчанию ничего не добавляет.

        Потомки могут переопределить и добавить второй QHBoxLayout в layout
        (он встаёт сразу под основным рядом кнопок).
        """
        return

    # =================================================================
    # UI Setup
    # =================================================================

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # === Toolbar (одна строка, авто-подгонка шрифта/кнопок под ширину) ===
        toolbar_container = QWidget()
        toolbar = QHBoxLayout(toolbar_container)
        toolbar.setContentsMargins(8, 4, 8, 4)
        toolbar.setSpacing(8)

        # --- Undo / Redo (единые; справа от ⚙ после инжекта Назад/⚙) ---
        self.btn_undo = make_undo_button(self._undo)
        toolbar.addWidget(self.btn_undo)
        self.btn_redo = make_redo_button(self._redo)
        toolbar.addWidget(self.btn_redo)

        # Потомок заполняет toolbar
        self._setup_toolbar(toolbar)

        toolbar.addStretch()

        # --- Save (единая) рядом с Подтвердить ---
        self.btn_save = make_save_button(self._save_graph, "Сохранить граф на сервер (Ctrl+S)")
        toolbar.addWidget(self.btn_save)

        # --- Confirm (единая) ---
        self.btn_confirm = make_confirm_button(
            self._on_confirm,
            tooltip="Сохранить и подтвердить граф.\nПереход к следующему этапу.",
        )
        toolbar.addWidget(self.btn_confirm)

        layout.addWidget(toolbar_container)
        from ui.widgets.responsive_toolbar import install_responsive_toolbar
        self._toolbar_responsive = install_responsive_toolbar(toolbar_container)

        # Опциональный второй ряд тулбара (потомки могут наполнить)
        self._setup_secondary_toolbar(layout)

        # === Loading placeholder ===
        self.loading_label = QLabel("Загрузка артефактов...")
        self.loading_label.setAlignment(Qt.AlignCenter)
        self.loading_label.setStyleSheet("font-size: 18px; color: #666;")
        layout.addWidget(self.loading_label)

        # Сохраняем ссылку для вставки редактора
        self._editor_layout = layout

        # === Status ===
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(
            "color: #888; font-size: 11px; padding: 2px 8px;"
        )
        layout.addWidget(self.status_label)

    # =================================================================
    # Download
    # =================================================================

    def _download_artifacts(self):
        self._download_thread = QThread()
        self._downloader = _GraphArtifactDownloader(
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
            editor = self._create_editor()
            editor.status_callback = lambda msg: self.status_label.setText(msg)
            editor.stats_callback = self._update_stats
            editor.mode_callback = self._on_mode_changed
            # WYSIWYG: pre-transform графа в холст 1920x1080 перед загрузкой.
            # При неудаче — грузим как есть (граф в исходных координатах).
            graph_for_editor = artifacts["graph_json"]
            try:
                canvas_graph = self.temp_dir / "graph_1920.json"
                if _pretransform_to_canvas(
                    Path(artifacts["graph_json"]),
                    Path(artifacts["original_image"]),
                    canvas_graph,
                ):
                    graph_for_editor = canvas_graph
            except Exception as exc:
                logger.warning("pre-transform пропущен, гружу граф как есть: %s", exc)

            editor.load_data(
                image_path=str(artifacts["original_image"]),
                graph_path=str(graph_for_editor),
                coco_path=str(artifacts.get("coco_validated", "")),
            )
            # Вставить редактор перед status_label (последний виджет)
            self._editor_layout.insertWidget(
                self._editor_layout.count() - 1, editor
            )
            self._editor = editor  # присвоить только после успеха
            self.status_label.setText("Граф загружен")
            self.apply_saved_appearance()
            self._on_editor_ready()
        except Exception as exc:
            logger.error("Failed to init graph editor: %s", exc, exc_info=True)
            QMessageBox.critical(
                self, "Ошибка",
                f"Не удалось инициализировать редактор графа:\n{exc}"
            )

    def _appearance_editor(self):
        return self._editor

    def _build_appearance_controls(self, panel):
        from PySide6.QtGui import QColor
        self._add_bg_darkness_slider(panel)
        self._add_color_setting(
            panel, "Цвет рёбер", "edge_color", QColor(255, 255, 255),
            lambda c: self._editor and self._editor.set_edge_color(c),
        )

    def apply_saved_appearance(self):
        super().apply_saved_appearance()
        ed = self._editor
        if ed is None:
            return
        from PySide6.QtGui import QColor
        if hasattr(ed, "set_edge_color"):
            self._apply_saved_color("edge_color", QColor(255, 255, 255), ed.set_edge_color)

    def apply_default_appearance(self):
        super().apply_default_appearance()
        ed = self._editor
        if ed is None:
            return
        from PySide6.QtGui import QColor
        if hasattr(ed, "set_edge_color"):
            ed.set_edge_color(QColor(255, 255, 255, 150))

    @Slot(str)
    def _on_download_error(self, error_msg: str):
        self._download_thread.quit()
        self._download_thread.wait()
        self.loading_label.setText(f"Ошибка: {error_msg}")

    def _on_editor_ready(self):
        """Хук: вызывается сразу после успешной загрузки редактора.

        Потомки могут переопределить для дополнительной инициализации.
        """

    # =================================================================
    # Stats callbacks
    # =================================================================

    def _update_stats(self, stats: dict):
        """Callback от редактора. Счётчик в toolbar убран — оставлено для совместимости."""
        return

    # =================================================================
    # Common mode/tool helpers
    # =================================================================

    def _set_mode(self, mode: str):
        """Установить режим редактора по строковому ключу."""
        if self._editor:
            self._editor.set_mode(mode)

    def _on_mode_changed(self, mode: str):
        """Callback от editor при смене режима — синхронизировать кнопки toolbar.

        Снимает checked со всех кнопок mode_group.
        Потомки переопределяют _get_mode_button_map() для автоматической активации.
        """
        if not hasattr(self, 'mode_group'):
            return
        btn_map = self._get_mode_button_map()
        # Снимаем exclusive чтобы можно было uncheck все
        self.mode_group.setExclusive(False)
        for btn in self.mode_group.buttons():
            btn.setChecked(False)
        # Активируем нужную кнопку если есть маппинг
        target_btn = btn_map.get(mode)
        if target_btn:
            target_btn.setChecked(True)
        self.mode_group.setExclusive(True)

    def _get_mode_button_map(self) -> dict:
        """Маппинг mode_name → QPushButton. Потомки переопределяют."""
        return {}

    def _undo(self):
        if self._editor:
            self._editor.undo()

    def _redo(self):
        if self._editor:
            self._editor.redo()

    # =================================================================
    # Save & Confirm
    # =================================================================

    def has_unsaved_changes(self) -> bool:
        """True если есть несохранённые изменения после последнего save."""
        if self._editor:
            return self._editor.undo_mgr.revision != self._saved_revision
        return False

    def _save_graph(self) -> bool:
        """Сохранить граф на сервер. Возвращает True при успехе."""
        if not self._editor:
            return False

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)

            graph_path = self.temp_dir / "graph_validated.json"
            if not self._editor.save_graph(str(graph_path)):
                self.status_label.setText("Не удалось сохранить локально")
                return False

            self.status_label.setText("Загрузка графа на сервер...")
            self.api_client.upload_validated_graph(self.uid, graph_path)

            self._saved_revision = self._editor.undo_mgr.revision
            self.status_label.setText("Граф сохранён")
            return True

        except Exception as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось сохранить граф:\n{exc}"
            )
            return False
        finally:
            QApplication.restoreOverrideCursor()

    @Slot()
    def _on_confirm(self):
        """Подтвердить: безусловно сохранить + emit confirmed.

        Save не гейтится дырти-флагом: он может ложно давать «нет изменений»,
        а без свежего graph_validated сервер сгенерирует FXML из устаревшего
        графа. Лишний POST дёшев — страхует от потери правок.
        """
        if self.has_unsaved_changes():
            reply = QMessageBox.question(
                self, "Сохранение",
                "Несохранённые изменения будут сохранены. Продолжить?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        if not self._save_graph():
            return

        self.status_message.emit("Граф подтверждён")
        self.confirmed.emit()

    # =================================================================
    # Cleanup
    # =================================================================

    def cleanup(self):
        """Остановить фоновую загрузку и освободить ресурсы."""
        if hasattr(self, "_download_thread") and self._download_thread.isRunning():
            self._download_thread.quit()
            self._download_thread.wait(3000)
        if hasattr(self, "_temp_dir_obj"):
            self._temp_dir_obj.cleanup()

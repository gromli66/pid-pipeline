"""
Graph Validation Window — окно валидации графа P&ID.

Standalone QMainWindow для валидации графа.
Использует AdvancedGraphEditor (строковые режимы, UndoManager).

Загружает graph_json и original_image из API, позволяет редактировать,
сохраняет graph_validated обратно.
"""

import logging
import tempfile
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QToolBar, QPushButton, QLabel,
    QStatusBar, QMessageBox, QFileDialog,
    QApplication, QButtonGroup,
)
from PySide6.QtCore import Qt, Signal, Slot, QThread, QObject

from ui.editors.advanced_graph_editor import AdvancedGraphEditor
from ui.services.api_client import APIClient, APIError

logger = logging.getLogger(__name__)


# =====================================================================
# Background downloader (по аналогии с MaskValidationWindow)
# =====================================================================

class GraphArtifactDownloader(QObject):
    """Фоновая загрузка артефактов для валидации графа."""

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
        # graph: prefer validated, fallback to json
        graph_prefer_validated = True
        optional = [
            ("coco_validated", "coco_validated.json"),
        ]

        try:
            for art_type, filename in required:
                self.progress.emit(f"Загрузка {art_type}...")
                dest = self.temp_dir / filename
                self.api_client.download_artifact(self.uid, art_type, dest)
                artifacts[art_type] = dest

            # Граф: prefer validated
            graph_dest = self.temp_dir / "graph.json"
            try:
                self.progress.emit("Загрузка graph_validated...")
                self.api_client.download_artifact(self.uid, "graph_validated", graph_dest)
                artifacts["graph_json"] = graph_dest
            except Exception:
                self.progress.emit("Загрузка graph_json...")
                self.api_client.download_artifact(self.uid, "graph_json", graph_dest)
                artifacts["graph_json"] = graph_dest

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


# =====================================================================
# GraphValidationWindow
# =====================================================================

class GraphValidationWindow(QMainWindow):
    """
    Окно валидации графа P&ID.

    Открывается для конкретной диаграммы из главного окна.
    Содержит тулбар с режимами редактирования и AdvancedGraphEditor.

    Режимы — строковые ключи:
        "add_edge", "delete_edge", "add_connector",
        "delete_node", "optimize_edge", "drag_node"

    Сигналы:
        validation_completed(uid) — валидация завершена, граф сохранён
        window_closed(uid) — окно закрыто
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

        self._temp_dir_obj = tempfile.TemporaryDirectory(prefix="pid_graph_")
        self.temp_dir = Path(self._temp_dir_obj.name)

        # State
        self._artifacts: dict = {}
        self._saved_stack_depth: int = 0

        self.setWindowTitle(f"Валидация графа — {diagram_name}")
        self.setMinimumSize(1200, 800)
        self.showMaximized()

        self._setup_ui()
        self._setup_statusbar()
        self._download_artifacts()

    # === UI Setup ===

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        # === Toolbar ===
        self.toolbar = QToolBar("Graph Validation")
        self.toolbar.setMovable(False)
        self.addToolBar(self.toolbar)

        # --- Mode buttons (строковые ключи) ---
        self.btn_add_edge = QPushButton("➕ Ребро")
        self.btn_add_edge.setCheckable(True)
        self.btn_add_edge.clicked.connect(lambda: self._set_mode("add_edge"))
        self.toolbar.addWidget(self.btn_add_edge)

        self.btn_del_edge = QPushButton("➖ Ребро")
        self.btn_del_edge.setCheckable(True)
        self.btn_del_edge.clicked.connect(lambda: self._set_mode("delete_edge"))
        self.toolbar.addWidget(self.btn_del_edge)

        self.btn_add_connector = QPushButton("⊕ Коннектор")
        self.btn_add_connector.setCheckable(True)
        self.btn_add_connector.clicked.connect(lambda: self._set_mode("add_connector"))
        self.toolbar.addWidget(self.btn_add_connector)

        self.btn_del_node = QPushButton("⊖ Узел")
        self.btn_del_node.setCheckable(True)
        self.btn_del_node.clicked.connect(lambda: self._set_mode("delete_node"))
        self.toolbar.addWidget(self.btn_del_node)

        self.toolbar.addSeparator()

        # --- Perpendicularity optimization ---
        self.btn_optimize_edge = QPushButton("📐 Оптимизировать")
        self.btn_optimize_edge.setCheckable(True)
        self.btn_optimize_edge.setToolTip(
            "Клик на оранжевое ребро → оптимизировать перпендикулярность"
        )
        self.btn_optimize_edge.clicked.connect(
            lambda: self._set_mode("optimize_edge")
        )
        self.toolbar.addWidget(self.btn_optimize_edge)

        btn_optimize_all = QPushButton("📐 Все")
        btn_optimize_all.setToolTip("Оптимизировать все неперпендикулярные рёбра")
        btn_optimize_all.clicked.connect(self._optimize_all_edges)
        self.toolbar.addWidget(btn_optimize_all)

        self.toolbar.addSeparator()

        # --- Drag ---
        self.btn_drag_node = QPushButton("✋ Двигать")
        self.btn_drag_node.setCheckable(True)
        self.btn_drag_node.setToolTip(
            "Перетаскивание узлов. Рёбра станут белыми когда выровняются."
        )
        self.btn_drag_node.clicked.connect(lambda: self._set_mode("drag_node"))
        self.toolbar.addWidget(self.btn_drag_node)

        # Exclusive button group
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.btn_add_edge)
        self.mode_group.addButton(self.btn_del_edge)
        self.mode_group.addButton(self.btn_add_connector)
        self.mode_group.addButton(self.btn_del_node)
        self.mode_group.addButton(self.btn_optimize_edge)
        self.mode_group.addButton(self.btn_drag_node)

        self.toolbar.addSeparator()

        # --- Actions ---
        btn_undo = QPushButton("↩ Undo (Ctrl+Z)")
        btn_undo.clicked.connect(self._undo)
        self.toolbar.addWidget(btn_undo)

        self.toolbar.addSeparator()

        btn_save = QPushButton("💾 Сохранить")
        btn_save.clicked.connect(self._save_graph)
        self.toolbar.addWidget(btn_save)

        btn_complete = QPushButton("✅ Завершить валидацию")
        btn_complete.setStyleSheet(
            "background-color: #4CAF50; color: white; "
            "font-weight: bold; padding: 4px 12px;"
        )
        btn_complete.clicked.connect(self._complete_validation)
        self.toolbar.addWidget(btn_complete)

        # === Editor (AdvancedGraphEditor) ===
        self.graph_editor = AdvancedGraphEditor()
        self.graph_editor.status_callback = self._update_status
        self.graph_editor.stats_callback = self._update_stats
        self.graph_editor.mode_callback = self._on_mode_changed
        layout.addWidget(self.graph_editor)

        # === Stats bar ===
        stats_layout = QHBoxLayout()

        self.graph_stats_label = QLabel(
            "Nodes: — | Edges: — | Connected: — | Isolated: —"
        )
        stats_layout.addWidget(self.graph_stats_label)

        self.perp_stats_label = QLabel("⊥: —")
        stats_layout.addWidget(self.perp_stats_label)

        stats_layout.addStretch()

        layout.addLayout(stats_layout)

        # === Status ===
        self.graph_status = QLabel(
            "Ctrl+клик: действие | Escape: сброс | Колесо: зум"
        )
        layout.addWidget(self.graph_status)

        # Placeholder while loading
        self.loading_label = QLabel("⏳ Загрузка артефактов...")
        self.loading_label.setAlignment(Qt.AlignCenter)

    def _setup_statusbar(self):
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)

    # === Artifact download ===

    def _download_artifacts(self):
        """Запустить фоновую загрузку артефактов."""
        self._thread = QThread()
        self._downloader = GraphArtifactDownloader(
            self.api_client, self.uid, self.temp_dir
        )
        self._downloader.moveToThread(self._thread)

        self._thread.started.connect(self._downloader.run)
        self._downloader.finished.connect(self._on_artifacts_loaded)
        self._downloader.error.connect(self._on_download_error)
        self._downloader.progress.connect(
            lambda msg: self.statusbar.showMessage(msg)
        )

        self._downloader.finished.connect(self._thread.quit)
        self._downloader.error.connect(self._thread.quit)

        self._thread.start()

    @Slot(dict)
    def _on_artifacts_loaded(self, artifacts: dict):
        """Артефакты загружены — инициализируем редактор."""
        self._artifacts = artifacts
        self.statusbar.showMessage("Артефакты загружены", 3000)

        image_path = str(artifacts["original_image"])
        graph_path = str(artifacts["graph_json"])
        coco_path = str(artifacts.get("coco_validated", ""))

        if self.graph_editor.load_data(image_path, graph_path, coco_path):
            self._saved_stack_depth = self.graph_editor.undo_mgr.stack_depth
            stats = self.graph_editor.model.compute_statistics()
            self.statusbar.showMessage(
                f"Загружено: {self.diagram_name} | "
                f"{stats['total_nodes']} узлов, {stats['total_edges']} рёбер",
                5000,
            )
        else:
            QMessageBox.warning(
                self, "Ошибка",
                "Не удалось загрузить граф. Проверьте артефакты.",
            )

    @Slot(str)
    def _on_download_error(self, error_msg: str):
        """Ошибка загрузки артефактов."""
        self.statusbar.showMessage(f"Ошибка: {error_msg}")
        QMessageBox.warning(
            self, "Ошибка загрузки",
            f"Не удалось загрузить артефакты:\n{error_msg}",
        )

    # === Load from files (for standalone/debug use) ===

    def load_from_files(self):
        """Загрузить граф из файлов (без API)."""
        image_path, _ = QFileDialog.getOpenFileName(
            self, "Изображение P&ID", "", "Images (*.png *.jpg)"
        )
        if not image_path:
            return

        graph_path, _ = QFileDialog.getOpenFileName(
            self, "Граф (JSON)", "", "JSON (*.json)"
        )
        if not graph_path:
            return

        coco_path, _ = QFileDialog.getOpenFileName(
            self, "COCO аннотации (опционально)", "", "JSON (*.json)"
        )

        if self.graph_editor.load_data(
            image_path, graph_path, coco_path or ""
        ):
            self._saved_stack_depth = self.graph_editor.undo_mgr.stack_depth
            stats = self.graph_editor.model.compute_statistics()
            self.statusbar.showMessage(
                f"Загружено: {Path(image_path).name} | "
                f"{stats['total_nodes']} узлов, {stats['total_edges']} рёбер",
                5000,
            )
        else:
            self.statusbar.showMessage("Ошибка загрузки")

    # === Mode switching (строковые ключи) ===

    _MODE_BUTTONS = {
        "add_edge": "btn_add_edge",
        "delete_edge": "btn_del_edge",
        "add_connector": "btn_add_connector",
        "delete_node": "btn_del_node",
        "optimize_edge": "btn_optimize_edge",
        "drag_node": "btn_drag_node",
    }

    def _set_mode(self, mode: str):
        """Переключить режим редактирования."""
        self.graph_editor.set_mode(mode)

    def _on_mode_changed(self, mode: str):
        """Синхронизировать кнопки toolbar при смене режима."""
        for mode_key, btn_attr in self._MODE_BUTTONS.items():
            btn = getattr(self, btn_attr, None)
            if btn:
                btn.setChecked(mode_key == mode)

    # === Optimization ===

    def _optimize_all_edges(self):
        """Оптимизировать все неперпендикулярные рёбра."""
        count = self.graph_editor.optimize_all_edges()
        self._update_perp_stats()
        self.statusbar.showMessage(f"Оптимизировано {count} рёбер", 5000)

    def _update_perp_stats(self):
        """Обновить статистику перпендикулярности."""
        stats = self.graph_editor.get_perpendicularity_stats()
        self.perp_stats_label.setText(
            f"⊥: {stats['good']}/{stats['total']} ({stats['avg_score']:.0%})"
        )

    # === Status callbacks ===

    def _update_status(self, msg: str):
        """Callback от AdvancedGraphEditor."""
        self.graph_status.setText(msg)

    def _update_stats(self, stats: dict):
        """Callback от AdvancedGraphEditor."""
        self.graph_stats_label.setText(
            f"Nodes: {stats['total_nodes']} | "
            f"Edges: {stats['total_edges']} | "
            f"Connected: {stats['connected']} | "
            f"Isolated: {stats['isolated']}"
        )
        self._update_perp_stats()

    # === Undo ===

    @Slot()
    def _undo(self):
        self.graph_editor.undo()

    # === Change detection ===

    def _has_unsaved_changes(self) -> bool:
        return self.graph_editor.undo_mgr.stack_depth != self._saved_stack_depth

    # === Save ===

    @Slot()
    def _save_graph(self):
        """Сохранить валидированный граф на сервер."""
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)

            # Сохраняем во временный файл
            graph_path = self.temp_dir / "graph_validated.json"
            if not self.graph_editor.save_graph(str(graph_path)):
                QApplication.restoreOverrideCursor()
                return

            # Загружаем на сервер
            self.statusbar.showMessage("Загрузка графа на сервер...")
            self.api_client.upload_validated_graph(self.uid, graph_path)

            self._saved_stack_depth = self.graph_editor.undo_mgr.stack_depth
            self.statusbar.showMessage("✅ Граф сохранён", 5000)

        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось сохранить граф:\n{exc.message}",
            )
        except Exception as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось сохранить граф:\n{exc}",
            )
        finally:
            QApplication.restoreOverrideCursor()

    # === Complete ===

    @Slot()
    def _complete_validation(self):
        """Завершить валидацию графа."""
        if self._has_unsaved_changes():
            reply = QMessageBox.question(
                self, "Сохранение",
                "Граф не сохранён. Сохранить перед завершением?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if reply == QMessageBox.Cancel:
                return
            if reply == QMessageBox.Yes:
                self._save_graph()
                if self._has_unsaved_changes():
                    return  # Сохранение не удалось

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            result = self.api_client.complete_graph_validation(self.uid)
            QApplication.restoreOverrideCursor()

            task_id = result.get("task_id")
            msg = "Валидация графа завершена!"
            if task_id:
                msg += f"\nГенерация FXML запущена (task: {task_id[:8]}...)"

            QMessageBox.information(self, "Готово", msg)
            self.validation_completed.emit(self.uid)
            self.close()

        except APIError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось завершить валидацию:\n{exc.message}",
            )

    # === Close ===

    def closeEvent(self, event):
        """Спросить о сохранении при закрытии если есть изменения."""
        if self._has_unsaved_changes():
            reply = QMessageBox.question(
                self, "Закрытие",
                "Есть несохранённые изменения графа. Сохранить перед закрытием?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            )
            if reply == QMessageBox.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.Save:
                self._save_graph()

        self.window_closed.emit(self.uid)

        try:
            self._temp_dir_obj.cleanup()
        except Exception:
            pass

        super().closeEvent(event)

"""
Тест редакторов масок — запускает оба редактора на данных реальной диаграммы.

Использование:
    python test_editors.py <diagram_uid>

Пример:
    python test_editors.py a816c12f-5c8d-4d45-8754-82658cfa69eb

Скачивает артефакты через API и открывает 2 вкладки:
- Валидация перекрёстков (SquareMaskEditor)
- Валидация труб (PolylineMaskEditor)
"""

import sys
import tempfile
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QVBoxLayout, QHBoxLayout,
    QWidget, QPushButton, QLabel, QSlider, QButtonGroup,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QImageReader

# Увеличиваем лимит для больших P&ID
QImageReader.setAllocationLimit(1024)

# Editors
from square_mask_editor import SquareMaskEditor
from polyline_mask_editor import PolylineMaskEditor, PolylineTool


class TestWindow(QMainWindow):
    def __init__(
        self,
        diagram_uid: str,
        api_base: str = "http://localhost:8000",
        local_dir: str | None = None,
    ):
        super().__init__()

        self.diagram_uid = diagram_uid
        self.api_base = api_base
        self.local_dir = Path(local_dir) if local_dir else None
        self.tmp_dir = self.local_dir or Path(tempfile.mkdtemp(prefix="pid_test_"))

        self.setWindowTitle(f"Test Editors — {diagram_uid[:8]}")
        self.setMinimumSize(1400, 900)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(5, 5, 5, 5)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self.statusBar().showMessage("Загрузка...")

        # Setup tabs
        self._download_and_setup()

    def _download_artifact(self, artifact_type: str, filename: str) -> Path:
        """Скачать артефакт через API."""
        import httpx

        dest = self.tmp_dir / filename
        if dest.exists():
            return dest

        url = f"{self.api_base}/api/diagrams/{self.diagram_uid}/download/{artifact_type}"
        try:
            response = httpx.get(url, timeout=120.0)
            response.raise_for_status()
            dest.write_bytes(response.content)
            return dest
        except Exception as e:
            print(f"Ошибка скачивания {artifact_type}: {e}")
            return Path("")

    def _get_file(self, artifact_type: str, filename: str) -> Path:
        """Получить файл: из локальной папки или скачать через API."""
        dest = self.tmp_dir / filename

        # Local mode — файл уже на месте
        if self.local_dir and dest.exists():
            return dest

        # API mode — скачать
        if not self.local_dir:
            return self._download_artifact(artifact_type, filename)

        return dest  # local_dir mode but file missing

    def _download_and_setup(self):
        """Загрузить все артефакты и настроить вкладки."""
        mode = f"local: {self.local_dir}" if self.local_dir else f"API: {self.api_base}"
        print(f"📥 Загрузка ({mode}) ...")

        original = self._get_file("original_image", "original.png")
        junction_mask = self._get_file("junction_mask", "junction_mask.png")
        bridge_mask = self._get_file("bridge_mask", "bridge_mask.png")
        skeleton = self._get_file("skeleton", "skeleton.png")
        skeleton_mask = self._get_file("skeleton_mask", "skeleton_mask.png")
        coco = self._get_file("coco_validated", "coco_validated.json")

        if not original.exists():
            self.statusBar().showMessage("Ошибка: не удалось скачать original")
            return

        # Tab 1: Junction validation
        self._setup_junction_tab(original, junction_mask, bridge_mask, skeleton)

        # Tab 2: Pipe validation
        self._setup_pipe_tab(original, skeleton_mask, coco)

        self.statusBar().showMessage(
            f"Загружено: {self.diagram_uid[:8]} | tmp: {self.tmp_dir}"
        )

    # ==================== TAB 1: Junction ====================

    def _setup_junction_tab(
        self, original: Path, junction_mask: Path, bridge_mask: Path, skeleton: Path
    ):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)

        # Toolbar
        toolbar = QHBoxLayout()

        self.btn_sq_white = QPushButton("⬜ Junction (1)")
        self.btn_sq_white.setCheckable(True)
        self.btn_sq_white.setChecked(True)
        self.btn_sq_white.clicked.connect(lambda: self._set_square_class(1))
        toolbar.addWidget(self.btn_sq_white)

        self.btn_sq_red = QPushButton("🟥 Bridge (2)")
        self.btn_sq_red.setCheckable(True)
        self.btn_sq_red.clicked.connect(lambda: self._set_square_class(2))
        toolbar.addWidget(self.btn_sq_red)

        sq_group = QButtonGroup(self)
        sq_group.addButton(self.btn_sq_white)
        sq_group.addButton(self.btn_sq_red)

        toolbar.addStretch()

        btn_undo = QPushButton("↶ Undo")
        btn_undo.clicked.connect(lambda: self.square_editor.undo())
        toolbar.addWidget(btn_undo)

        btn_save = QPushButton("💾 Сохранить")
        btn_save.clicked.connect(self._save_junction_masks)
        toolbar.addWidget(btn_save)

        layout.addLayout(toolbar)

        # Editor
        self.square_editor = SquareMaskEditor()
        self.square_editor.status_callback = self._update_junction_status
        layout.addWidget(self.square_editor)

        # Status
        self.junction_status = QLabel(
            "Ctrl+ЛКМ: добавить квадрат | Ctrl+ПКМ: удалить | 1/2: класс | Колесо: зум"
        )
        layout.addWidget(self.junction_status)

        self.tabs.addTab(tab, "🎨 Валидация перекрёстков")

        # Load
        if junction_mask.exists():
            ok = self.square_editor.load_images(
                str(original),
                str(junction_mask),
                str(bridge_mask) if bridge_mask.exists() else "",
                str(skeleton) if skeleton.exists() else "",
            )
            if ok:
                print(f"✅ Junction editor loaded ({original.name})")
            else:
                print("❌ Junction editor load failed")

    def _set_square_class(self, cls: int):
        self.square_editor.current_class = cls
        self.btn_sq_white.setChecked(cls == 1)
        self.btn_sq_red.setChecked(cls == 2)

    def _update_junction_status(self, msg: str):
        self.junction_status.setText(msg)

    def _save_junction_masks(self):
        p1 = str(self.tmp_dir / "junction_mask_validated.png")
        p2 = str(self.tmp_dir / "bridge_mask_validated.png")
        result = self.square_editor.save_masks(p1, p2)
        if result:
            self.statusBar().showMessage(f"Сохранено в {self.tmp_dir}")

    # ==================== TAB 2: Pipe ====================

    def _setup_pipe_tab(self, original: Path, skeleton_mask: Path, coco: Path):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)

        # Toolbar
        toolbar = QHBoxLayout()

        self.btn_polyline = QPushButton("✏️ Полилиния")
        self.btn_polyline.setCheckable(True)
        self.btn_polyline.setChecked(True)
        self.btn_polyline.clicked.connect(
            lambda: self._set_polyline_tool(PolylineTool.POLYLINE)
        )
        toolbar.addWidget(self.btn_polyline)

        self.btn_eraser = QPushButton("🧹 Ластик")
        self.btn_eraser.setCheckable(True)
        self.btn_eraser.clicked.connect(
            lambda: self._set_polyline_tool(PolylineTool.ERASER)
        )
        toolbar.addWidget(self.btn_eraser)

        poly_group = QButtonGroup(self)
        poly_group.addButton(self.btn_polyline)
        poly_group.addButton(self.btn_eraser)

        toolbar.addSpacing(20)

        toolbar.addWidget(QLabel("Толщина:"))
        self.width_slider = QSlider(Qt.Orientation.Horizontal)
        self.width_slider.setRange(1, 50)
        self.width_slider.setValue(3)
        self.width_slider.setFixedWidth(150)
        self.width_slider.valueChanged.connect(self._on_width_changed)
        toolbar.addWidget(self.width_slider)

        self.width_label = QLabel("3px")
        self.width_label.setFixedWidth(40)
        toolbar.addWidget(self.width_label)

        toolbar.addStretch()

        btn_undo = QPushButton("↶ Undo")
        btn_undo.clicked.connect(lambda: self.polyline_editor.undo())
        toolbar.addWidget(btn_undo)

        btn_save = QPushButton("💾 Сохранить")
        btn_save.clicked.connect(self._save_pipe_mask)
        toolbar.addWidget(btn_save)

        layout.addLayout(toolbar)

        # Editor
        self.polyline_editor = PolylineMaskEditor()
        self.polyline_editor.status_callback = self._update_pipe_status
        layout.addWidget(self.polyline_editor)

        # Status
        self.pipe_status = QLabel(
            "Ctrl+ЛКМ: точка | 2×ЛКМ/ПКМ/Enter: завершить | Escape: отмена | Колесо: зум"
        )
        layout.addWidget(self.pipe_status)

        self.tabs.addTab(tab, "✏️ Валидация труб")

        # Load
        if skeleton_mask.exists():
            ok = self.polyline_editor.load_images(
                str(original),
                str(skeleton_mask),
                str(coco) if coco.exists() else "",
            )
            if ok:
                coco_info = (
                    f" + {len(self.polyline_editor.coco_annotations)} bbox"
                    if self.polyline_editor.coco_annotations
                    else ""
                )
                print(f"✅ Pipe editor loaded ({original.name}{coco_info})")
            else:
                print("❌ Pipe editor load failed")

    def _set_polyline_tool(self, tool: PolylineTool):
        self.polyline_editor.set_tool(tool)
        self.btn_polyline.setChecked(tool == PolylineTool.POLYLINE)
        self.btn_eraser.setChecked(tool == PolylineTool.ERASER)

    def _on_width_changed(self, value: int):
        self.width_label.setText(f"{value}px")
        self.polyline_editor.set_line_width(value)

    def _update_pipe_status(self, msg: str):
        self.pipe_status.setText(msg)

    def _save_pipe_mask(self):
        path = str(self.tmp_dir / "pipe_mask_validated.png")
        result = self.polyline_editor.save_mask(path)
        if result:
            self.statusBar().showMessage(f"Сохранено в {self.tmp_dir}")


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python test_editors.py <uid>                        # download via API")
        print("  python test_editors.py <uid> --local <folder>       # use local files")
        print()
        print("Local folder must contain:")
        print("  original.png, junction_mask.png, bridge_mask.png,")
        print("  skeleton.png, skeleton_mask.png, coco_validated.json")
        sys.exit(1)

    uid = sys.argv[1]
    local_dir = None
    api_base = "http://localhost:8000"

    if "--local" in sys.argv:
        idx = sys.argv.index("--local")
        if idx + 1 < len(sys.argv):
            local_dir = sys.argv[idx + 1]
    elif len(sys.argv) > 2 and not sys.argv[2].startswith("--"):
        api_base = sys.argv[2]

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QMainWindow, QWidget { background-color: #2b2b2b; color: #fff; }
        QPushButton {
            background: #3c3c3c;
            border: 1px solid #555;
            padding: 6px 12px;
            border-radius: 4px;
        }
        QPushButton:hover { background: #4a4a4a; }
        QPushButton:checked { background: #0d6efd; }
        QTabWidget::pane { border: 1px solid #555; }
        QTabBar::tab {
            background: #3c3c3c;
            padding: 8px 16px;
            margin-right: 2px;
        }
        QTabBar::tab:selected { background: #0d6efd; }
        QLabel { padding: 4px; }
        QSlider::groove:horizontal {
            height: 6px; background: #555; border-radius: 3px;
        }
        QSlider::handle:horizontal {
            background: #0d6efd; width: 16px; margin: -5px 0; border-radius: 8px;
        }
    """)

    window = TestWindow(uid, api_base, local_dir=local_dir)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

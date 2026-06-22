"""
FrameTab — вкладка очистки рамки/штампа (этап 0 pipeline).

Оператор обводит полигоном внутреннюю часть чертежа (всё снаружи → фон) и боксом
убирает штамп/таблицу. Очищенное изображение сохраняется неразрушающе как
канонический original/image.png (сырой бэкап → image_raw.png); это изображение читают все
последующие этапы. Исходный PDF (если был) сохраняется как original/source.pdf.

Toolbar (первый QHBoxLayout — в него DiagramWorkspace вставляет «← Назад»):
  Полигон | Бокс | Undo | 💾 Сохранить и продолжить

Сигналы:
  confirmed()           — этап завершён (save+complete); вкладку закрыть
  status_message(str)   — текст для статусбара
"""

import logging
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QMessageBox,
)

from ui.editors.frame_editor import FrameRemoverView
from ui.services.api_client import APIError

logger = logging.getLogger(__name__)


class FrameTab(QWidget):
    confirmed = Signal()
    status_message = Signal(str)

    def __init__(self, diagram_uid, diagram_name, api_client, parent=None):
        super().__init__(parent)
        self._uid = diagram_uid
        self._diagram_name = diagram_name
        self.api_client = api_client
        self._confirmed = False
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="frame_"))

        self._build_ui()
        self._load_original()

    # ── UI ────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Toolbar — ПЕРВЫЙ layout-элемент (сюда воркспейс врезает «← Назад»)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(6, 6, 6, 6)
        toolbar.setSpacing(6)

        self.btn_polygon = QPushButton("🔲 Полигон (внешнее)")
        self.btn_polygon.setToolTip(
            "Обвести внутреннюю часть чертежа — всё снаружи полигона "
            "зальётся фоном (убирает внешнюю рамку).\n"
            "ЛКМ — ставить точки, замкнуть на 1-ю точку / ПКМ / Enter."
        )
        self.btn_polygon.setCheckable(True)
        self.btn_polygon.setChecked(True)
        self.btn_polygon.setStyleSheet(
            "QPushButton:checked { background-color: #E91E63; color: white; }"
        )
        self.btn_polygon.clicked.connect(lambda: self._set_tool("polygon"))
        toolbar.addWidget(self.btn_polygon)

        self.btn_box = QPushButton("⬛ Бокс (внутреннее)")
        self.btn_box.setToolTip(
            "Прямоугольник по штампу/таблице — всё внутри бокса "
            "зальётся фоном.\n"
            "Рисовать зажатой ЛКМ. Боксов можно поставить несколько."
        )
        self.btn_box.setCheckable(True)
        self.btn_box.setStyleSheet(
            "QPushButton:checked { background-color: #FF9800; color: white; }"
        )
        self.btn_box.clicked.connect(lambda: self._set_tool("box"))
        toolbar.addWidget(self.btn_box)

        btn_undo = QPushButton("↩ Undo")
        btn_undo.setToolTip(
            "Отменить последнюю операцию (заливку полигона или бокса).\n"
            "Также Ctrl+Z. История — до 30 шагов."
        )
        btn_undo.clicked.connect(self._on_undo)
        toolbar.addWidget(btn_undo)

        toolbar.addStretch(1)

        btn_save = QPushButton("💾 Сохранить и продолжить")
        btn_save.setToolTip(
            "Сохранить очищенное изображение и перейти к детекции."
        )
        btn_save.setStyleSheet(
            "background-color: #4CAF50; color: white; font-weight: bold; padding: 4px 12px;"
        )
        btn_save.clicked.connect(self._on_save)
        toolbar.addWidget(btn_save)

        root.addLayout(toolbar)

        self.editor = FrameRemoverView()
        self.editor.status_callback = self._on_editor_status
        root.addWidget(self.editor, stretch=1)

    # ── Загрузка исходника ────────────────────────────────
    def _load_original(self):
        try:
            dest = self._tmp_dir / "image.png"
            self.api_client.download_artifact(str(self._uid), "original_image", dest)
            if not self.editor.load_image(str(dest)):
                raise RuntimeError("Не удалось открыть изображение")
            self.editor.set_tool("polygon")
            self.status_message.emit("Обведите внутреннюю часть чертежа полигоном")
        except (APIError, Exception) as exc:
            QMessageBox.warning(self, "Ошибка", f"Не удалось загрузить изображение:\n{exc}")

    # ── Инструменты ───────────────────────────────────────
    def _set_tool(self, tool: str):
        self.btn_polygon.setChecked(tool == "polygon")
        self.btn_box.setChecked(tool == "box")
        self.editor.set_tool(tool)

    def _on_undo(self):
        self.editor.undo()

    def _on_editor_status(self, msg: str):
        self.status_message.emit(msg)

    # ── Сохранение / пропуск ──────────────────────────────
    @Slot()
    def _on_save(self):
        try:
            clean_path = self._tmp_dir / "cleaned.png"
            self.editor.save_image(str(clean_path))
            self.api_client.save_cleaned_image(str(self._uid), clean_path)
            self.api_client.complete_frame_removal(str(self._uid))
            self._confirmed = True
            self.status_message.emit("✅ Рамка очищена → детекция доступна")
            self.confirmed.emit()
        except (APIError, Exception) as exc:
            QMessageBox.warning(self, "Ошибка", f"Не удалось сохранить:\n{exc}")


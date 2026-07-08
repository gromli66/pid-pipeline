"""
Error Report Dialog — окно отчёта об ошибке упавшего этапа (Волна 2).

Показывает phase / step / code / сообщение + traceback (моноширинный, read-only).
Кнопки: «Копировать» (в буфер), «Сохранить» (в .txt), «Перезапустить», «Закрыть».

Источник — строка ProcessingStage из /stages (dict с полями
stage_type/status/error_message/error_traceback/error_code/failed_step/…).
"""

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPlainTextEdit, QPushButton, QFileDialog,
)


class ErrorReportDialog(QDialog):
    """Отчёт об ошибке этапа с копированием/сохранением traceback."""

    def __init__(
        self,
        stage: dict,
        parent=None,
        phase_label: Optional[str] = None,
        diagram_name: str = "",
    ):
        super().__init__(parent)
        self._stage = stage or {}
        self._diagram_name = diagram_name

        stage_type = self._stage.get("stage_type") or "—"
        self._phase = phase_label or stage_type
        self._step = self._stage.get("failed_step") or "—"
        self._code = self._stage.get("error_code") or "—"
        self._message = self._stage.get("error_message") or "Этап завершился с ошибкой."
        self._traceback = self._stage.get("error_traceback") or ""

        self._retry = False

        self.setWindowTitle(f"Ошибка этапа «{self._phase}»")
        self.setMinimumSize(720, 480)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)

        # Шапка: фаза / шаг / код.
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow("Фаза:", QLabel(self._phase))
        form.addRow("Под-шаг:", QLabel(self._step))
        code_label = QLabel(self._code)
        code_label.setStyleSheet("font-weight: bold; color: #d32f2f;")
        form.addRow("Код:", code_label)
        root.addLayout(form)

        # Сообщение.
        msg = QLabel(self._message)
        msg.setWordWrap(True)
        msg.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(msg)

        # Traceback (моноширинный, read-only).
        root.addWidget(QLabel("Traceback:"))
        self._tb_view = QPlainTextEdit()
        self._tb_view.setReadOnly(True)
        self._tb_view.setPlainText(self._traceback or "(traceback отсутствует)")
        self._tb_view.setFont(QFont("Consolas", 9))
        self._tb_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        root.addWidget(self._tb_view, stretch=1)

        # Кнопки.
        buttons = QHBoxLayout()
        btn_copy = QPushButton("📋 Копировать")
        btn_copy.clicked.connect(self._on_copy)
        btn_save = QPushButton("💾 Сохранить…")
        btn_save.clicked.connect(self._on_save)
        buttons.addWidget(btn_copy)
        buttons.addWidget(btn_save)
        buttons.addStretch()

        btn_retry = QPushButton("🔄 Перезапустить")
        btn_retry.clicked.connect(self._on_retry)
        btn_close = QPushButton("Закрыть")
        btn_close.clicked.connect(self.reject)
        buttons.addWidget(btn_retry)
        buttons.addWidget(btn_close)
        root.addLayout(buttons)

    def report_text(self) -> str:
        """Полный текст отчёта (для буфера/файла)."""
        lines = [
            f"Диаграмма: {self._diagram_name}" if self._diagram_name else "",
            f"Фаза:      {self._phase}",
            f"Под-шаг:   {self._step}",
            f"Код:       {self._code}",
            f"Сообщение: {self._message}",
            "",
            "Traceback:",
            self._traceback or "(отсутствует)",
        ]
        return "\n".join(x for x in lines if x != "" or True).strip() + "\n"

    def _on_copy(self):
        QGuiApplication.clipboard().setText(self.report_text())

    def _on_save(self):
        safe_name = (self._diagram_name or "diagram").replace(" ", "_")
        default = f"error_{safe_name}_{self._stage.get('stage_type') or 'stage'}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить отчёт об ошибке", default, "Текст (*.txt)"
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.report_text())

    def _on_retry(self):
        self._retry = True
        self.accept()

    def exec_retry(self) -> bool:
        """Показать окно; вернуть True, если пользователь выбрал «Перезапустить»."""
        self.exec()
        return self._retry

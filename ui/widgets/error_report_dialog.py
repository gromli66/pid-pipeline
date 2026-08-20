"""
Error Report Dialog — окно отчёта об ошибке (Волна 2 + пункт ДН3 дороги).

Показывает phase / step / code / сообщение + traceback (моноширинный, read-only),
ХОЛСТ на момент отказа и ЗАМЕТКУ оператора.
Кнопки: «Копировать» (в буфер), «Сохранить», «Перезапустить», «Закрыть».

Два источника:

* строка ProcessingStage из /stages (dict с полями
  stage_type/status/error_message/error_traceback/error_code/failed_step/…) —
  отказ этапа на сервере;
* ``report_exception()`` — падение в самом клиенте, где такой строки нет вовсе.

Зачем холст и заметка (ДН3). Отказ инструмента редактора оставлял оператору одну
строку в ``QMessageBox``, а трассировку печатал в ``sys.stderr``, которого
в собранном ``.exe`` нет (``console=False``, замер 1.10 §41.9) — единственный след
отказа терялся целиком (замер §96: прирост файла лога 0 байт). Теперь отказ идёт
в лог клиента (``ui/services/client_logging.py``), а оператор одной кнопкой
собирает отчёт, по которому видно и ЧТО упало, и НА ЧЁМ: холст к разработчику
иначе не попадает — растр и граф лежат на сервере, а состояние экрана в момент
отказа не лежит нигде.
"""

import logging
import traceback
import zipfile
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QBuffer, QIODevice
from PySide6.QtGui import QFont, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPlainTextEdit, QPushButton, QFileDialog,
)

logger = logging.getLogger(__name__)

# Имена внутри архива отчёта: их называет и сам отчёт, и служба поддержки.
REPORT_NAME = "report.txt"
CANVAS_NAME = "canvas.png"


class ErrorReportDialog(QDialog):
    """Отчёт об ошибке этапа с копированием/сохранением traceback."""

    def __init__(
        self,
        stage: dict,
        parent=None,
        phase_label: Optional[str] = None,
        diagram_name: str = "",
        canvas: Optional[QPixmap] = None,
        allow_retry: bool = True,
    ):
        super().__init__(parent)
        self._stage = stage or {}
        self._diagram_name = diagram_name
        # Пустой снимок в отчёте хуже отсутствующего: он читается как «холст
        # был чист», то есть врёт о состоянии, ради которого отчёт и собирают.
        self._canvas = canvas if canvas is not None and not canvas.isNull() else None
        self._allow_retry = allow_retry

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

        # Холст на момент отказа + заметка оператора (ДН3). Первое показывает,
        # НА ЧЁМ упало, второе — что оператор делал; в трассировке нет ни того,
        # ни другого, а восстановить это потом уже не по чему.
        attach = QHBoxLayout()
        if self._canvas is not None:
            preview = QLabel()
            preview.setPixmap(self._canvas.scaled(
                320, 180,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            preview.setToolTip(
                f"Холст {self._canvas.width()}×{self._canvas.height()} px — "
                f"уйдёт в отчёт файлом {CANVAS_NAME}"
            )
            attach.addWidget(preview)
        note_box = QVBoxLayout()
        note_box.addWidget(QLabel("Заметка оператора:"))
        self._note_view = QPlainTextEdit()
        self._note_view.setPlaceholderText(
            "Что делали перед ошибкой, что ожидали увидеть…"
        )
        self._note_view.setMaximumHeight(96)
        note_box.addWidget(self._note_view)
        attach.addLayout(note_box)
        root.addLayout(attach)

        # Кнопки.
        buttons = QHBoxLayout()
        btn_copy = QPushButton("📋 Копировать")
        btn_copy.clicked.connect(self._on_copy)
        btn_save = QPushButton("💾 Сохранить…")
        btn_save.clicked.connect(self._on_save)
        buttons.addWidget(btn_copy)
        buttons.addWidget(btn_save)
        buttons.addStretch()

        # Перезапустить умеет только тот, кто отчёт открыл: у падения ВНУТРИ
        # клиента перезапускать нечего, и мёртвая кнопка была бы враньём.
        if self._allow_retry:
            btn_retry = QPushButton("🔄 Перезапустить")
            btn_retry.clicked.connect(self._on_retry)
            buttons.addWidget(btn_retry)
        btn_close = QPushButton("Закрыть")
        btn_close.clicked.connect(self.reject)
        buttons.addWidget(btn_close)
        root.addLayout(buttons)

    def note(self) -> str:
        """Заметка оператора без обрамляющих пробелов."""
        return self._note_view.toPlainText().strip()

    def report_text(self) -> str:
        """Полный текст отчёта (для буфера/файла)."""
        lines = [
            f"Диаграмма: {self._diagram_name}" if self._diagram_name else "",
            f"Фаза:      {self._phase}",
            f"Под-шаг:   {self._step}",
            f"Код:       {self._code}",
            f"Сообщение: {self._message}",
        ]
        if self._canvas is not None:
            lines.append(
                f"Холст:     {CANVAS_NAME} "
                f"({self._canvas.width()}×{self._canvas.height()} px, в архиве)"
            )
        lines += [
            "",
            "Заметка оператора:",
            self.note() or "(не заполнена)",
            "",
            "Traceback:",
            self._traceback or "(отсутствует)",
        ]
        return "\n".join(x for x in lines if x != "" or True).strip() + "\n"

    def _on_copy(self):
        QGuiApplication.clipboard().setText(self.report_text())

    def _on_save(self):
        safe_name = (self._diagram_name or "diagram").replace(" ", "_")
        stem = f"error_{safe_name}_{self._stage.get('stage_type') or 'stage'}"
        # Холст в .txt не положить: с ним отчёт — архив «текст + картинка».
        suffix, mask = ((".zip", "Отчёт (*.zip)") if self._canvas is not None
                        else (".txt", "Текст (*.txt)"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить отчёт об ошибке", stem + suffix, mask
        )
        if path:
            self.save_report(Path(path))

    def save_report(self, path: Path) -> Path:
        """Записать отчёт: с холстом — архив ``report.txt`` + ``canvas.png``.

        Д3: удачная запись видна в логе клиента — по ней поддержка знает,
        что отчёт у оператора на руках, и спрашивает именно его.
        """
        if self._canvas is None:
            path.write_text(self.report_text(), encoding="utf-8")
        else:
            buf = QBuffer()
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            self._canvas.save(buf, "PNG")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(REPORT_NAME, self.report_text())
                archive.writestr(CANVAS_NAME, bytes(buf.data()))
        logger.info("Отчёт оператора сохранён: %s", path)
        return path

    def _on_retry(self):
        self._retry = True
        self.accept()

    def exec_retry(self) -> bool:
        """Показать окно; вернуть True, если пользователь выбрал «Перезапустить»."""
        self.exec()
        return self._retry


def report_exception(
    parent,
    phase: str,
    exc: BaseException,
    canvas: Optional[QPixmap] = None,
    diagram_name: str = "",
) -> ErrorReportDialog:
    """Показать отчёт о падении В КЛИЕНТЕ — строки ProcessingStage тут нет.

    Собирает из исключения то же, что сервер кладёт в стадию: код — класс
    исключения, сообщение — его текст, traceback — полный, а не одна строка,
    которой до ДН3 всё и заканчивалось.
    """
    stage = {
        "stage_type": phase,
        "error_code": type(exc).__name__,
        "error_message": str(exc) or type(exc).__name__,
        "error_traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
    }
    dialog = ErrorReportDialog(
        stage,
        parent=parent,
        phase_label=phase,
        diagram_name=diagram_name,
        canvas=canvas,
        allow_retry=False,
    )
    dialog.exec()
    return dialog

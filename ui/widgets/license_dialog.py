"""Диалог «Лицензия САПФИР»: видит ли клиент ключ и готов ли конвертер.

Открывается кнопкой в панели главного окна — до начала работы, а не на
экспорте. Локальные проверки показываются сразу, серверная догружается в
отдельном потоке: она ходит по сети и на неотзывчивом сервере ждёт таймаут.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.services.prtx_license import Report, diagnose_local
from ui.services.thread_lifetime import hand_over


class _ServerProbe(QObject):
    """Проверка сервера в отдельном потоке (образец — PrtxWorker)."""

    done = Signal(object)   # Report

    def __init__(self, api_client):
        super().__init__()
        self.api_client = api_client

    def run(self):
        from ui.services.prtx_license import diagnose
        try:
            self.done.emit(diagnose(self.api_client))
        except Exception as exc:  # noqa: BLE001 — диалог не должен падать
            report = diagnose_local()
            report.add("Конвертер на сервере", False, str(exc),
                       "Не удалось опросить сервер.")
            self.done.emit(report)


class LicenseDialog(QDialog):
    """Отчёт о лицензии с подсказками, что чинить."""

    def __init__(self, parent, api_client=None):
        super().__init__(parent)
        self.api_client = api_client
        self._thread = None
        self._probe = None

        self.setWindowTitle("Лицензия САПФИР")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)

        self._verdict = QLabel()
        self._verdict.setWordWrap(True)
        self._verdict.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._verdict)

        self._body = QLabel()
        self._body.setWordWrap(True)
        self._body.setTextFormat(Qt.TextFormat.RichText)
        self._body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._body)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        self._recheck = QPushButton("Проверить снова")
        self._recheck.clicked.connect(self.refresh)
        buttons.addButton(self._recheck, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self):
        report = diagnose_local()
        # локальная часть готова сразу; сервер — только если ключ на месте
        pending = self.api_client is not None and report.ok
        self._render(report, pending=pending)
        if pending:
            self._start_probe()

    def _start_probe(self):
        if self._thread is not None and self._thread.isRunning():
            return
        self._recheck.setEnabled(False)
        self._thread = QThread()
        self._probe = _ServerProbe(self.api_client)
        self._probe.moveToThread(self._thread)
        self._thread.started.connect(self._probe.run)
        self._probe.done.connect(self._on_probe_done)
        self._probe.done.connect(self._thread.quit)
        # `closeEvent` гасит пробу с потолком 3 с, но приходит он не на
        # всяком пути разрушения диалога — а сама проба непрерываема
        # (один HTTP). Дверь ниже закрывает обе дыры (пункт 1-50).
        hand_over(self, self._thread, self._probe,
                  name="проверка лицензии")
        self._thread.start()

    def _on_probe_done(self, report):
        self._recheck.setEnabled(True)
        self._render(report, pending=False)

    def _render(self, report: Report, pending: bool):
        if pending:
            head = "⏳ <b>Ключ на месте. Проверяю сервер…</b>"
        elif report.ok:
            head = "✅ <b>Лицензия в порядке — расчётная схема соберётся.</b>"
        else:
            head = "❌ <b>Расчётную схему собрать не получится.</b>"
        self._verdict.setText(head)

        rows = []
        for check in report.checks:
            mark = "✔" if check.ok else "✖"
            line = f"{mark} <b>{check.title}</b>"
            if check.detail:
                line += f"<br>&nbsp;&nbsp;&nbsp;<code>{_esc(check.detail)}</code>"
            if check.hint and not check.ok:
                line += f"<br>&nbsp;&nbsp;&nbsp;<i>{_esc(check.hint)}</i>"
            rows.append(line)
        if pending:
            rows.append("⏳ <b>Конвертер на сервере</b><br>&nbsp;&nbsp;&nbsp;опрашиваю…")
        # хвост: подсказка про остальную программу
        rows.append(
            "<br><i>Без ключа остальная программа работает как обычно — "
            "не собирается только расчётная схема .prtx.</i>"
        )
        self._body.setText("<br><br>".join(rows))

    def closeEvent(self, event):
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(3000)
        super().closeEvent(event)


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def show_license_dialog(parent: QWidget, api_client=None):
    LicenseDialog(parent, api_client).exec()

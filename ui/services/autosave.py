"""
AutoSaveService — единый сервис автосохранения на QTimer.

Запускается при открытии вкладки, останавливается при закрытии.
Интервал и вкл/выкл — из UISettings.
"""

import logging
from datetime import datetime
from typing import Optional, Callable

from PySide6.QtCore import QTimer, QObject

from ui.services.ui_settings import UISettings

logger = logging.getLogger(__name__)

# Маппинг: имя класса вкладки → имя метода сохранения
_SAVE_METHODS = {
    "PipeTab": "_save_mask",
    "JunctionTab": "_save_masks",
    "SimpleGraphTab": "_save_graph",
    "AdvancedGraphTab": "_save_graph",
    "ContourTab": "_save_graph",
    "OcrBindingTab": "_save_binding",
}


class AutoSaveService(QObject):
    """Автосохранение для активной вкладки редактора."""

    def __init__(
        self,
        status_callback: Optional[Callable[[str], None]] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._settings = UISettings.instance()
        self._status_callback = status_callback
        self._tab = None

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, tab_widget):
        """Запустить автосохранение для конкретной вкладки."""
        self._tab = tab_widget
        self._restart_timer()

    def stop(self):
        """Остановить автосохранение."""
        self._timer.stop()
        self._tab = None

    def reset_timer(self):
        """Сбросить таймер (вызывается после ручного сохранения)."""
        if self._timer.isActive():
            self._restart_timer()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _restart_timer(self):
        """(Пере)запустить таймер с текущим интервалом из настроек."""
        interval_ms = self._settings.autosave_interval_sec * 1000
        self._timer.start(interval_ms)

    def _on_tick(self):
        """Вызывается по таймеру."""
        if not self._settings.autosave_enabled:
            return
        if self._tab is None:
            return
        if not hasattr(self._tab, 'has_unsaved_changes'):
            return
        if not self._tab.has_unsaved_changes():
            return

        success = self._call_save(self._tab)
        if success:
            ts = datetime.now().strftime("%H:%M:%S")
            msg = f"💾 Автосохранено в {ts}"
            logger.info("Autosave OK for %s", type(self._tab).__name__)
            # Обновить status_label на вкладке
            if hasattr(self._tab, 'status_label'):
                self._tab.status_label.setText(msg)
            if self._status_callback:
                self._status_callback(msg)
        else:
            logger.warning("Autosave failed for %s", type(self._tab).__name__)
            if hasattr(self._tab, 'status_label'):
                self._tab.status_label.setText("⚠️ Автосохранение не удалось")

    def _call_save(self, tab) -> bool:
        """Вызвать метод сохранения вкладки."""
        class_name = type(tab).__name__
        method_name = _SAVE_METHODS.get(class_name)
        if method_name and hasattr(tab, method_name):
            try:
                result = getattr(tab, method_name)()
                return bool(result)
            except Exception as exc:
                logger.warning("Autosave exception in %s.%s: %s",
                               class_name, method_name, exc)
                return False
        return False

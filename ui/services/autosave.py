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
        # Буфер помечен несвежим гейтом пересборки (блок 5): граф на сервере
        # пересобран, и узлов, к которым привязаны правки вкладки, больше нет.
        # Раньше тик молча ретраил каждые 120 с и, когда статус минует гейт,
        # заливал СТАРОЕ поколение поверх нового — окно «после BUILT».
        if self._announce_stale_buffer():
            return
        if not hasattr(self._tab, 'has_unsaved_changes'):
            return
        if not self._tab.has_unsaved_changes():
            return

        success = self._call_save(self._tab)
        # Вкладка, у которой на время тика отняли право спрашивать, отвечает
        # СТРОКОЙ (пункт 1-38). Её строка точнее общей и в успехе (граф
        # записан, контуры — нет), и в отказе (запрет 1-33 вслепую не снимают).
        refusal = getattr(self._tab, 'save_refusal', "")
        if success:
            ts = datetime.now().strftime("%H:%M:%S")
            msg = refusal or f"💾 Автосохранено в {ts}"
            logger.info("Autosave OK for %s", type(self._tab).__name__)
            # Обновить status_label на вкладке
            if hasattr(self._tab, 'status_label'):
                self._tab.status_label.setText(msg)
            if self._status_callback:
                self._status_callback(msg)
        else:
            logger.warning("Autosave failed for %s", type(self._tab).__name__)
            if hasattr(self._tab, 'status_label'):
                self._tab.status_label.setText(
                    refusal or "⚠️ Автосохранение не удалось")
            # Отказ, помеченный гейтом пересборки, повторять нечем — метка
            # липкая, и следующий тик отличался бы только лишним запросом.
            self._announce_stale_buffer()

    def _announce_stale_buffer(self) -> bool:
        """Буфер вкладки несвежий → сказать и ОСТАНОВИТЬСЯ. True, если так."""
        reason = getattr(self._tab, 'save_blocked_reason', "")
        if not reason:
            return False
        logger.warning("Автосохранение остановлено для %s: %s",
                       type(self._tab).__name__, reason)
        if hasattr(self._tab, 'status_label'):
            self._tab.status_label.setText(reason)
        if self._status_callback:
            self._status_callback(reason)
        self.stop()
        return True

    def _call_save(self, tab) -> bool:
        """Вызвать метод сохранения вкладки."""
        class_name = type(tab).__name__
        method_name = _SAVE_METHODS.get(class_name)
        if method_name and hasattr(tab, method_name):
            try:
                # Тик таймера — не жест оператора: диалоги на этом пути
                # запрещены. Лечить их здесь нечем — они сидят у самих методов
                # сохранения, поэтому сервис только отнимает у вкладки право
                # спрашивать, а отказывается и объясняется она сама (1-38).
                with tab.non_interactive_save():
                    result = getattr(tab, method_name)()
                return bool(result)
            except Exception as exc:
                logger.warning("Autosave exception in %s.%s: %s",
                               class_name, method_name, exc)
                return False
        return False

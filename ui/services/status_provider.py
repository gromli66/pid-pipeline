"""
Status Provider — получение обновлений статуса диаграмм.

MVP: HTTP Polling каждые 2 секунды.
"""

import logging
from typing import Optional, Set

from PySide6.QtCore import QObject, Signal, QTimer, Slot

from ui.services.api_client import APIClient, DiagramStatusInfo, DiagramStatus, APIError

logger = logging.getLogger(__name__)


class StatusProvider(QObject):
    """
    Провайдер обновлений статуса диаграмм через polling.

    Signals:
        status_updated(uid, status_info): Статус диаграммы изменился
        error_occurred(uid, message): Ошибка при опросе
    """

    status_updated = Signal(str, object)  # uid, DiagramStatusInfo
    stages_updated = Signal(str, object)  # uid, list[stage dict] — для прогресса/ETA
    error_occurred = Signal(str, str)     # uid, error_message

    # Статусы, после которых polling останавливается.
    # Это статусы, требующие действия пользователя или финальные.
    #
    # НЕ финальные (после них автозапуск следующего этапа):
    #   SEGMENTING → auto skeletonize → SKELETONIZED
    #   VALIDATED_MASKS → auto skeletonize_simple → detect_junctions → DETECTED_JUNCTIONS
    #   VALIDATED_GRAPH → auto task_generate_fxml → COMPLETED
    _FINAL_STATUSES = frozenset({
        DiagramStatus.DETECTED,
        DiagramStatus.VALIDATED_BBOX,
        DiagramStatus.SKELETONIZED,
        DiagramStatus.DETECTED_JUNCTIONS,
        DiagramStatus.BUILT,
        DiagramStatus.VALIDATED_GRAPH,      # ждёт оператора (контуры)
        DiagramStatus.CONTOURS_EXTRACTED,   # SAM2 done (parallel)
        DiagramStatus.CONTOURS_VALIDATED,   # ждёт оператора (привязка)
        DiagramStatus.OCR_COMPLETED,
        DiagramStatus.OCR_BOUND,
        DiagramStatus.COMPLETED,
        DiagramStatus.ERROR,
    })

    def __init__(
        self,
        api_client: APIClient,
        poll_interval_ms: int = 2000,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)

        self.api_client = api_client
        self.poll_interval_ms = poll_interval_ms

        self._watched_uids: Set[str] = set()
        self._last_status: dict[str, DiagramStatus] = {}

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)

    def watch(self, uid: str) -> None:
        """Начать отслеживание диаграммы."""
        self._watched_uids.add(uid)
        # Сбросить кеш чтобы первый poll гарантированно emitил
        self._last_status.pop(uid, None)

        if not self._timer.isActive():
            self._timer.start(self.poll_interval_ms)

    def unwatch(self, uid: str) -> None:
        """Остановить отслеживание диаграммы."""
        self._watched_uids.discard(uid)
        self._last_status.pop(uid, None)

        if not self._watched_uids:
            self._timer.stop()

    def unwatch_all(self) -> None:
        """Остановить отслеживание всех диаграмм."""
        self._watched_uids.clear()
        self._last_status.clear()
        self._timer.stop()

    def is_watching(self, uid: str) -> bool:
        return uid in self._watched_uids

    @Slot()
    def _poll(self) -> None:
        """Опросить статусы отслеживаемых диаграмм."""
        for uid in list(self._watched_uids):
            try:
                status_info = self.api_client.get_status(uid)

                # Стадии — для детерминированного прогресс-бара / окна ошибки.
                # Эмитим каждый опрос: ETA пересчитывается по elapsed (динамично).
                stages = self.api_client.get_stages(uid)
                self.stages_updated.emit(uid, stages)
                busy = any((s.get("status") or "").lower() in
                           ("running", "pending") for s in (stages or []))

                last = self._last_status.get(uid)
                if last != status_info.status:
                    logger.info(
                        "Status %s: %s → %s",
                        uid[:8], last, status_info.status.value,
                    )
                    self._last_status[uid] = status_info.status
                    self.status_updated.emit(uid, status_info)

                    # НЕ БРОСАЕМ ОПРОС, ПОКА ЕСТЬ БЕГУЩАЯ СТАДИЯ. Статус и
                    # стадии — разные оси: `OCR_BOUND` числится финальным
                    # («ждёт оператора»), а под ним в это время считается
                    # раскладка. Первый же опрос снимал слежение, кнопка
                    # «Ручной правки» оставалась заглушенной той стадией,
                    # которая давно завершилась, и оператор ждал вечно
                    # (замер 8d517a35: опрос в 13:16:07 -> unwatch, раскладка
                    # закончилась в 13:16:13, узнать об этом было некому).
                    if status_info.status in self._FINAL_STATUSES and not busy:
                        self.unwatch(uid)

            except APIError as exc:
                self.error_occurred.emit(uid, exc.message)

    def force_update(self, uid: str) -> Optional[DiagramStatusInfo]:
        """Принудительно обновить статус."""
        try:
            status_info = self.api_client.get_status(uid)
            self._last_status[uid] = status_info.status
            self.status_updated.emit(uid, status_info)
            self.stages_updated.emit(uid, self.api_client.get_stages(uid))
            return status_info
        except APIError as exc:
            self.error_occurred.emit(uid, exc.message)
            return None

"""Фоновое OCR-распознавание вручную добавленных боксов.

Один воркер на две вкладки (`AdvancedGraphTab` и `OcrBindingTab`); до пункта 0.5
классы были двумя копиями, расходившимися на четыре строки. Не блокирует UI:
кладётся в отдельный QThread, результат приходит сигналом.
"""

import logging

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


class RecognizeWorker(QObject):
    """`api_client.recognize_boxes(uid, boxes)` в фоне."""

    finished = Signal(list)   # list[dict]: [{"text": ..., "confidence": ...}, ...]
    error = Signal(str)

    def __init__(self, api_client, uid: str, boxes: list):
        super().__init__()
        self.api_client = api_client
        self.uid = uid
        self.boxes = boxes    # list[[x0,y0,x1,y1], ...]

    def run(self):
        try:
            resp = self.api_client.recognize_boxes(self.uid, self.boxes)
            # Ответ не-словарём (заглушка, чужой прокси) — не падать, а вернуть
            # пусто: страховка из ocr_binding-версии, в графовой её не было.
            results = resp.get("results", []) if isinstance(resp, dict) else []
            self.finished.emit(results)
        except Exception as exc:  # noqa: BLE001 — любой сбой уходит во вкладку
            self.error.emit(str(exc))

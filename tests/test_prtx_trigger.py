# -*- coding: utf-8 -*-
"""Когда клиент запускает автосборку .prtx (ветка exp/prtx-convert).

Замок против ровно той ошибки, что была в первой редакции: триггер висел на
`GENERATING_FXML`, а этот статус опрос клиента (2 с) почти никогда не видит —
генерация FXML занимает 0.1 с (замер по 75 стадиям `fxml_generation`, max 1.3 с).
На авто-пути после «Проверки схемы» клиент видит `VALIDATED_GRAPH → COMPLETED`,
и сборка не запускалась бы вовсе.

Второй замок (2026-08-23): этап «Экспорт» формирует ОБА файла, но на диск
оператора здесь не сохраняется ничего — файлы забирает `ExportDialog`. И
пересборка ТОЛЬКО чертежа (`_prtx_skip_next`) не тянет за собой расчётную схему.

Тест гоняет НЕ виджет, а голый `_apply_status` на заглушке: без QApplication и
без сети, зато ровно ту логику взвода, что стоит в бою.
"""
import pytest

pytest.importorskip("PySide6")

from ui.services.api_client import DiagramStatus as S           # noqa: E402
from ui.widgets.diagram_workspace import DiagramWorkspace       # noqa: E402


class _Stub:
    """Минимум, который трогает `_apply_status` на нужных ветках."""

    _apply_status = DiagramWorkspace._apply_status

    def __init__(self):
        self._uid = "u"
        self._last_status = S.UPLOADED          # как выставляет load_diagram
        self._prtx_armed = False
        self._prtx_skip_next = False
        self._stage_errors = {}
        self._ocr_notified = True               # ветку OCR-опроса не трогаем
        self._action_buttons = {}
        self.prtx_calls = 0
        self.btn_error_retry = type("B", (), {"setVisible": lambda *a: None})()
        self.beads = type("Bd", (), {"set_state": lambda *a: None})()

    _update_beads = _update_buttons = _update_gif = lambda *a, **k: None
    _start_ocr_poll = _stop_ocr_poll = lambda *a: None
    _apply_error_status = lambda *a, **k: None
    # Ещё один художник кнопок, которого `_apply_status` зовёт на ветках
    # параллельного OCR (`ocr_binding` зелёная или жёлтая по тому, пройден
    # ли этап). Заглушка гоняет НАСТОЯЩИЙ `_apply_status`, поэтому обязана
    # знать всё, что он трогает у себя, — сторож ниже это и проверяет.
    _light_binding_button = lambda *a, **k: None

    def _start_prtx_conversion(self):
        self.prtx_calls += 1


def _play(statuses, **pre):
    w = _Stub()
    for k, v in pre.items():
        setattr(w, k, v)
    for st in statuses:
        w._apply_status(st)
    return w


def test_заглушка_переживает_каждый_статус():
    """Сторож самой заглушки: `_apply_status` гоняется по ПОЛНОМУ набору статусов.

    Клетки ниже берут три-четыре статуса каждая, и ветки параллельного OCR
    (`building_graph`…`contours_validated`) достаются лишь двум из семи. Из-за
    этого новый вызов внутри `_apply_status` ронял ровно две клетки, а не файл
    целиком, — и точечный прогон по `tests/ui` его не видел вовсе
    (доработка №5 блока 3, 2026-08-25).

    Заглушка неизбежно повторяет поверхность настоящего виджета; пусть она
    ломается на ПЕРЕБОРЕ, с понятным сообщением, а не на выборке.
    """
    w = _Stub()
    for st in S:
        w._apply_status(st)


def test_открытие_готовой_схемы_не_пересобирает():
    """Иначе .prtx собирался бы заново при каждом открытии готовой диаграммы."""
    w = _play([S.COMPLETED, S.COMPLETED])
    assert w.prtx_calls == 0


def test_авто_путь_собирает_даже_когда_generating_fxml_не_увиден():
    w = _play([S.VALIDATING_GRAPH, S.VALIDATED_GRAPH, S.COMPLETED])
    assert w.prtx_calls == 1


def test_путь_после_контуров():
    w = _play([S.OCR_BOUND, S.COMPLETED])
    assert w.prtx_calls == 1


def test_повторный_опрос_completed_не_собирает_второй_раз():
    w = _play([S.VALIDATED_GRAPH, S.COMPLETED, S.COMPLETED, S.COMPLETED])
    assert w.prtx_calls == 1


def test_пересборка_только_чертежа_не_трогает_расчётную_схему():
    """Кнопка «Пересобрать чертёж FXML» стоит секунды, расчётная схема — минуты
    счёта и повторное чтение ключа лицензии. Для неё в диалоге своя кнопка."""
    w = _play([S.GENERATING_FXML, S.COMPLETED], _prtx_skip_next=True)
    assert w.prtx_calls == 0
    assert w._prtx_skip_next is False       # разовый, следующий прогон соберёт


def test_подавление_разовое():
    """После пересборки чертежа обычный авто-путь снова собирает схему."""
    w = _play([S.GENERATING_FXML, S.COMPLETED], _prtx_skip_next=True)
    w._apply_status(S.GENERATING_FXML)
    w._apply_status(S.COMPLETED)
    assert w.prtx_calls == 1


def test_подавление_снимается_ошибкой_этапа():
    """Прогон кончился ошибкой — флаг не должен дожить до следующего прогона
    и съесть уже честную автосборку."""
    w = _play([S.GENERATING_FXML, S.ERROR], _prtx_skip_next=True)
    assert w._prtx_skip_next is False

    w._apply_status(S.GENERATING_FXML)
    w._apply_status(S.COMPLETED)
    assert w.prtx_calls == 1

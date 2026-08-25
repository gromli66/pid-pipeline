# -*- coding: utf-8 -*-
"""Блок 5, пункт 5.3 — кнопки модалок графовых вкладок по-русски.

Текст диалогов русский, а кнопки рисовались стандартные. Замер этой сессии
на боевом PySide6 (`QMessageBox` с флагами `Ok` и `Yes|Cancel`):

    ['OK']
    ['Cancel', '&Yes']

То есть переводить нечего ровно у тех модалок, где кнопка одна: «OK» и в
русском интерфейсе «OK». Английскими остаются `Cancel` и `&Yes` — они у
вопросов, и вопросов у графовых вкладок два: запрет слепой перезаписи
(`BlindOverwriteGuard._confirm_blind_overwrite`) и «Подтвердить»
(`BaseGraphTab._on_confirm`). Оба сведены в одну дверь `_ask_yes_cancel`,
чтобы не разошлись, как разошлись две двери вопроса о несохранённом (1-18).

`QTranslator` в проекте не установлен, поэтому подписи задаются своими
кнопками — прецедент — диалог конфликта диаметров
(`ocr_binding_editor.py:2265-2267`).

⚠ Утверждается НАШЕ решение (какие подписи выбрал код), а не поведение Qt:
сторож, проверяющий свойства библиотеки, остаётся зелёным при снятии того,
что он стережёт (`PROTOCOL`, замер 1-19).

⚠ `exec()` подменён: настоящий модальный цикл подвесил бы набор вместо
падения (`PROTOCOL §5`, четвёртый исход зонда).
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                    # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtWidgets import (QApplication, QMessageBox,        # noqa: E402
                               QWidget)

from ui.tabs.blind_overwrite import BlindOverwriteGuard          # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Tab(BlindOverwriteGuard, QWidget):
    """Носитель двери — тот же расклад, что у графовых вкладок."""


@pytest.fixture
def asked(qapp, monkeypatch):
    """Журнал подписей + чем ответил оператор. Возврата ждать некому."""
    seen = {"texts": [], "click": None}

    def _exec(self):
        seen["texts"] = [b.text() for b in self.buttons()]
        for btn in self.buttons():
            if btn.text() == seen["click"]:
                btn.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", _exec)
    return seen


def test_the_question_is_asked_with_russian_buttons(asked):
    """Ни одной латинской кнопки: оператор видит «Да» и «Отмена»."""
    tab = _Tab()
    asked["click"] = "Отмена"

    tab._ask_yes_cancel("Сохранение", "Продолжить?")

    assert sorted(asked["texts"]) == ["Да", "Отмена"], (
        f"кнопки не переведены: {asked['texts']}")
    tab.deleteLater()


def test_yes_and_cancel_answer_differently(asked):
    """Обе полярности: «Да» пропускает запись, «Отмена» — нет."""
    tab = _Tab()

    asked["click"] = "Да"
    assert tab._ask_yes_cancel("Сохранение", "Продолжить?") is True

    asked["click"] = "Отмена"
    assert tab._ask_yes_cancel("Сохранение", "Продолжить?") is False
    tab.deleteLater()

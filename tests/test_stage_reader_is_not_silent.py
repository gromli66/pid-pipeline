# -*- coding: utf-8 -*-
"""Чтение стадий не молчит об отказе сервера (пункт 1-13, нога 1.16 дороги).

Находка ноги 1.12, адресованная сюда архитектором: голое `except Exception:
stages = []` в `_apply_error_status` даёт спутать «стадий нет» с «сервер отказал»,
а ветки поведения у них разные — вторая уводит в фолбэк, где до правки 1.12
у оператора было 0 кнопок из 13.

Что нашлось при проверке адреса (шаг 2, замер по коду). Подмену делает НЕ тот
`except`, на который указывала находка: `APIClient.get_stages` ловит `APIError`
САМ и возвращает `[]`, то есть до внешнего перехвата отказ сервера не доходит
вовсе — внешний ловит только настоящие поломки чтения. Поэтому лечится и то,
и другое, но в разных местах и по-разному:

  * `get_stages` — называет причину и всё равно отдаёт `[]` (на пустой список
    опираются `status_provider` и модель прогресса; менять контракт незачем);
  * `_apply_error_status` — перехват остаётся широким СОЗНАТЕЛЬНО (без окна
    оператор не увидит ничего), но перестаёт быть молчаливым.

Qt здесь не поднимается: метод зовётся несвязанным, на заглушке. Живой виджет
этому тесту ничего не добавил бы, а мусор после себя оставил бы соседям
(замер 1-36: чужие 2000 виджетов превращают 3.6 с набора в 201.9 с).
"""
import logging

import pytest

from ui.services.api_client import APIClient, APIError

UID = "b7d1c2e3-9999-8888-7777-666655554444"


class _Boom(RuntimeError):
    """Поломка чтения, которую `get_stages` НЕ ловит (не `APIError`)."""


# ── слой 1: сам читатель ─────────────────────────────────────────────────

def test_get_stages_names_the_refusal(monkeypatch, caplog):
    """Сервер отказал — в логе есть причина, наружу по-прежнему пустой список."""
    client = APIClient.__new__(APIClient)

    def _request(*args, **kwargs):
        raise APIError("Connection failed after 2 attempts: [Errno 111] refused")

    monkeypatch.setattr(client, "_request", _request, raising=False)

    with caplog.at_level(logging.WARNING, logger="ui.services.api_client"):
        stages = client.get_stages(UID)

    assert stages == [], "контракт читателей сломан: они ждут список"
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1, "отказ сервера остался молчаливым"
    assert UID in messages[0], "по строке не понять, о какой схеме речь"
    assert "Errno 111" in messages[0], "причина отказа не доехала до лога"


def test_get_stages_is_silent_when_there_are_no_stages(monkeypatch, caplog):
    """Стадий действительно нет — жаловаться не на что.

    Порог заперт с двух сторон: без этого теста «лог всегда» выглядел бы так же
    правильно, и различать два исхода снова было бы нечем.
    """
    client = APIClient.__new__(APIClient)
    monkeypatch.setattr(client, "_request", lambda *a, **kw: {"stages": []},
                        raising=False)

    with caplog.at_level(logging.WARNING, logger="ui.services.api_client"):
        assert client.get_stages(UID) == []

    assert caplog.records == []


# ── слой 2: читатель внутри вкладки ──────────────────────────────────────

class _StubWorkspace:
    """Поверхность `_apply_error_status`, которой хватает фолбэку."""

    def __init__(self, api_client):
        self.api_client = api_client
        self._uid = UID
        self._stage_errors = None
        self.calls = []

    def _update_beads(self, status):
        self.calls.append(("beads", status))

    def _update_buttons(self, status, error_stage=None):
        self.calls.append(("buttons", status, error_stage))

    def _update_gif(self, status):
        self.calls.append(("gif", status))

    def _offer_error_retry(self, error_stage, error_message):
        self.calls.append(("retry", error_stage, error_message))


class _BrokenClient:
    def get_stages(self, uid):
        raise _Boom("сломался разбор ответа")


class _EmptyClient:
    def get_stages(self, uid):
        return []


def _apply(stub):
    """Позвать метод несвязанным — без QWidget и без QApplication."""
    from ui.widgets.diagram_workspace import DiagramWorkspace

    return DiagramWorkspace._apply_error_status(stub, "building_graph", "boom")


def test_broken_stage_read_is_logged_and_falls_back(caplog):
    """Поломка чтения: фолбэк отрабатывает, но в логе о ней сказано."""
    stub = _StubWorkspace(_BrokenClient())

    with caplog.at_level(logging.WARNING, logger="ui.widgets.diagram_workspace"):
        _apply(stub)

    assert [c[0] for c in stub.calls] == ["beads", "buttons", "gif", "retry"], \
        "фолбэк перестал отрабатывать — оператор остался без окна"
    assert stub.calls[-1] == ("retry", "building_graph", "boom")

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1, "поломка чтения стадий осталась молчаливой"
    assert "_Boom" in messages[0], "класс поломки не назван"
    assert "сломался разбор ответа" in messages[0]


def test_empty_stage_list_is_not_reported_as_a_break(caplog):
    """Стадий нет — тот же фолбэк, но без жалобы: это не поломка."""
    stub = _StubWorkspace(_EmptyClient())

    with caplog.at_level(logging.WARNING, logger="ui.widgets.diagram_workspace"):
        _apply(stub)

    assert [c[0] for c in stub.calls] == ["beads", "buttons", "gif", "retry"]
    assert caplog.records == []


def test_stage_errors_are_reset_before_the_read():
    """`_stage_errors` очищается до чтения — иначе прошлый отказ пережил бы новый.

    Утверждается РАЗНИЦА, а не совпадение: словарь заполняется заведомо чужим
    значением, и после вызова его там быть не должно.
    """
    stub = _StubWorkspace(_BrokenClient())
    stub._stage_errors = {"graph": {"status": "failed"}}

    _apply(stub)

    assert stub._stage_errors == {}


@pytest.mark.parametrize("client", [_BrokenClient(), _EmptyClient()])
def test_fallback_path_is_identical_for_both_causes(client):
    """Ветка поведения у обеих причин одна — различает их только лог.

    Это и есть суть находки: разные события, одинаковый результат. Тест
    фиксирует, что правка НЕ развела их поведение (развод — чужой пункт),
    а только сделала различимыми в журнале.
    """
    stub = _StubWorkspace(client)
    _apply(stub)
    assert [c[0] for c in stub.calls] == ["beads", "buttons", "gif", "retry"]

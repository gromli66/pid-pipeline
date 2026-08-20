# -*- coding: utf-8 -*-
"""Политика повторов клиента: что `APIClient._request` повторяет, а что нет.

Пункт 1-19 дороги. Зона без покрытия (`PROTOCOL §3`), поэтому решётка написана
ДО правки и фиксирует ТЕКУЩЕЕ поведение: каждая клетка — абсолютное число
попыток, а не «столько же, сколько раньше».

Почему это вообще пункт. `_request` ловит `httpx.RequestError` и повторяет
запрос ЛЮБЫМ методом. Таймаут — подкласс `RequestError`, дефолт клиента 60 с,
а боевой отказ `send_task` наступает на 64-й (`docs/STATUS_MACHINE.md §5`,
замер §87д): один клик оператора собирает на сервере до `max_retries + 1`
конкурентных обработчиков одного эндпоинта. Замер §104е/§107.5 показал, чем
это кончается — второй обработчик восстанавливал исходный тупик.

Мутирующих вызовов у клиента **40** (36 POST + 3 PUT + 1 DELETE), и повторяются
ВСЕ 40: `retries=0` стоит только у `GET /health`. Полная таблица — в
`MEASUREMENTS.md §118`. Но решётка ниже нарочно НЕ перечисляет эндпоинты:
вопрос задаётся состоянию («было ли соединение установлено»), а не перечню,
поэтому новый POST защищён автоматически.
"""
from __future__ import annotations

import inspect

import httpx
import pytest

from ui.services.api_client import APIClient, APIError

# Классы отказов httpx, каждый со своей стороной границы.
#
#   Connect*/Pool* — соединение НЕ установлено, сервер запроса не видел:
#                    дубль невозможен по построению, повтор безопасен всем.
#   Read*/Write*/Protocol — запрос МОГ быть доставлен и обработан; для POST
#                    повтор здесь и есть «дубль эффекта».
#
# ⚠ `ReadError` попал во вторую группу ЗАМЕРОМ, а не рассуждением: обрыв
# keep-alive (сервер закрыл соединение) даёт именно его, а не `ConnectError`
# (WinError 10053, замер §118). Раскладка «по интуиции» отнесла бы его к первой.
NEVER_DELIVERED = ["ConnectError", "ConnectTimeout", "PoolTimeout"]
MAYBE_DELIVERED = ["ReadTimeout", "ReadError", "WriteError", "RemoteProtocolError"]
ALL_FAILURES = NEVER_DELIVERED + MAYBE_DELIVERED

IDEMPOTENT_METHODS = ["GET", "PUT", "DELETE"]
ALL_METHODS = ["POST"] + IDEMPOTENT_METHODS

# Абсолютные числа, не выражения от `max_retries`: тест, вычисляющий свой вход
# из проверяемой константы, останется зелёным при любом её значении
# (`PROTOCOL §3`, поймано инъекцией в 0.4).
ATTEMPTS_WITH_RETRY = 4      # max_retries=3 + первая попытка
ATTEMPTS_WITHOUT = 1


def _raiser(name: str):
    """Транспорт, который всегда падает нужным классом, и счётчик попыток."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        raise getattr(httpx, name)("проба {}".format(name), request=request)

    return handler, seen


def _client(transport: httpx.MockTransport, monkeypatch, **kw) -> APIClient:
    """`APIClient`, у которого транспорт переживает пересоздание пула.

    `_request` пересоздаёт `self._client` между попытками, поэтому подменять
    экземпляр бесполезно — подменяется фабрика. Восстановление за monkeypatch.
    """
    real = httpx.Client

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    kw.setdefault("retry_delay", 0.0)      # backoff здесь не проверяется
    return APIClient(base_url="http://probe.invalid", **kw)


@pytest.mark.parametrize("method", ALL_METHODS)
@pytest.mark.parametrize("failure", ALL_FAILURES)
def test_how_many_times_a_failing_request_reaches_the_transport(
    method, failure, monkeypatch
):
    """Решётка 4 метода × 7 классов отказа = 28 клеток, все с числом.

    Полное множество методов взято не выборкой: `GET`/`POST`/`PUT`/`DELETE` —
    это ВСЕ методы, которыми клиент ходит (снято разбором вызовов
    `self._request(`/`self._request_raw(`, команда в `MEASUREMENTS §118`).
    """
    handler, seen = _raiser(failure)
    client = _client(httpx.MockTransport(handler), monkeypatch)
    try:
        with pytest.raises(APIError):
            client._request(method, "/api/probe")
    finally:
        client.close()

    assert len(seen) == ATTEMPTS_WITH_RETRY, (
        "{} × {}: попыток {}, ожидалось {}".format(
            method, failure, len(seen), ATTEMPTS_WITH_RETRY)
    )


def test_the_client_retries_post_after_the_operators_previous_click(monkeypatch):
    """⛔ Сценарий ПОСЛЕ чужого действия оператора, не на свежем клиенте.

    Тест на СВЕЖЕМ объекте не проверяет взаимодействие с предысторией
    (`PROTOCOL §3`), а у этого клиента предыстория несущая: `_request`
    ПЕРЕСОЗДАЁТ пул после каждой неудачной попытки. Поэтому сначала оператор
    жмёт «Сохранить маску» и роняет POST четырежды — пул пересоздан трижды, —
    и только ПОТОМ, тем же клиентом, жмёт «Завершить».

    Утверждается РАЗНИЦА между двумя кликами, а не совпадение с состоянием
    «до»: числа у них обязаны быть разными после правки и одинаковыми до неё.
    """
    calls = {"save": [], "complete": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/masks/upload"):
            calls["save"].append(1)
            raise httpx.ConnectError("API ещё не поднят", request=request)
        calls["complete"].append(1)
        raise httpx.ReadTimeout("сервер отвечает на 64-й", request=request)

    client = _client(httpx.MockTransport(handler), monkeypatch)
    try:
        with pytest.raises(APIError):
            client._request("POST", "/api/validation/u/masks/upload")
        with pytest.raises(APIError):
            client._request("POST", "/api/validation/u/masks/complete")
    finally:
        client.close()

    assert len(calls["save"]) == ATTEMPTS_WITH_RETRY
    assert len(calls["complete"]) == ATTEMPTS_WITH_RETRY


def test_the_live_battle_profile_by_the_letter(monkeypatch):
    """Боевой профиль буквой: клиент сдаётся на 60-й, сервер отвечает на 64-й.

    Числа абсолютные и обе стороны заперты: 60 лежит в клиенте, 64 — в
    `docs/STATUS_MACHINE.md §5`. Сервер здесь не «медленный вообще», а
    медленнее клиентского терпения ровно так, как на бою.
    """
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        raise httpx.ReadTimeout("60 с вышли", request=request)

    client = _client(httpx.MockTransport(handler), monkeypatch, timeout=60.0)
    try:
        with pytest.raises(APIError):
            client._request("POST", "/api/validation/u/masks/complete")
    finally:
        client.close()

    assert len(seen) == ATTEMPTS_WITH_RETRY


def test_the_defaults_of_the_client_are_the_ones_the_border_was_measured_on():
    """Дефолты, на которых сняты замеры §104е/§107.5 — абсолютными числами."""
    signature = inspect.signature(APIClient.__init__)
    assert signature.parameters["timeout"].default == 60.0
    assert signature.parameters["max_retries"].default == 3
    assert signature.parameters["retry_delay"].default == 1.0


def test_health_check_is_the_only_call_that_opts_out_of_retries():
    """`retries=0` в клиенте ровно один — у `GET /health`.

    Не «я посмотрел и вроде один», а счёт по файлу: заведут второй — скажет.
    """
    import re
    from pathlib import Path

    src = Path(inspect.getfile(APIClient)).read_text(encoding="utf-8")
    assert len(re.findall(r"retries=0", src)) == 1
    assert len(re.findall(r"retries=1", src)) == 3

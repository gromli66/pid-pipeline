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
ATTEMPTS_WITHOUT = 1         # отказ сразу, повтор за оператором

# Решётка ожиданий выписана ПОЭЛЕМЕНТНО, а не вычислена из той же логики,
# что и код: агрегатное правило («POST не повторяется, кроме…») выглядит
# верным ровно до тех пор, пока множество однородно (`PROTOCOL §3`, замер
# 1-6 третий возврат). Здесь у каждой из 28 клеток стоит своё число.
EXPECTED_ATTEMPTS = {
    ("POST", "ConnectError"): ATTEMPTS_WITH_RETRY,
    ("POST", "ConnectTimeout"): ATTEMPTS_WITH_RETRY,
    ("POST", "PoolTimeout"): ATTEMPTS_WITH_RETRY,
    ("POST", "ReadTimeout"): ATTEMPTS_WITHOUT,
    ("POST", "ReadError"): ATTEMPTS_WITHOUT,
    ("POST", "WriteError"): ATTEMPTS_WITHOUT,
    ("POST", "RemoteProtocolError"): ATTEMPTS_WITHOUT,
    ("GET", "ConnectError"): ATTEMPTS_WITH_RETRY,
    ("GET", "ConnectTimeout"): ATTEMPTS_WITH_RETRY,
    ("GET", "PoolTimeout"): ATTEMPTS_WITH_RETRY,
    ("GET", "ReadTimeout"): ATTEMPTS_WITH_RETRY,
    ("GET", "ReadError"): ATTEMPTS_WITH_RETRY,
    ("GET", "WriteError"): ATTEMPTS_WITH_RETRY,
    ("GET", "RemoteProtocolError"): ATTEMPTS_WITH_RETRY,
    ("PUT", "ConnectError"): ATTEMPTS_WITH_RETRY,
    ("PUT", "ConnectTimeout"): ATTEMPTS_WITH_RETRY,
    ("PUT", "PoolTimeout"): ATTEMPTS_WITH_RETRY,
    ("PUT", "ReadTimeout"): ATTEMPTS_WITH_RETRY,
    ("PUT", "ReadError"): ATTEMPTS_WITH_RETRY,
    ("PUT", "WriteError"): ATTEMPTS_WITH_RETRY,
    ("PUT", "RemoteProtocolError"): ATTEMPTS_WITH_RETRY,
    ("DELETE", "ConnectError"): ATTEMPTS_WITH_RETRY,
    ("DELETE", "ConnectTimeout"): ATTEMPTS_WITH_RETRY,
    ("DELETE", "PoolTimeout"): ATTEMPTS_WITH_RETRY,
    ("DELETE", "ReadTimeout"): ATTEMPTS_WITH_RETRY,
    ("DELETE", "ReadError"): ATTEMPTS_WITH_RETRY,
    ("DELETE", "WriteError"): ATTEMPTS_WITH_RETRY,
    ("DELETE", "RemoteProtocolError"): ATTEMPTS_WITH_RETRY,
}


def test_the_grid_covers_every_cell_of_the_product():
    """Решётка полна: добавят метод или класс отказа — скажет, а не смолчит.

    Без этого сторожа новая строка в `ALL_METHODS`/`ALL_FAILURES` дала бы
    `KeyError` внутри одной клетки, а выпавшая — прошла бы незамеченной.
    """
    from itertools import product

    assert len(EXPECTED_ATTEMPTS) == 28
    assert set(EXPECTED_ATTEMPTS) == set(product(ALL_METHODS, ALL_FAILURES))
    # Порог заперт с двух сторон: в решётке есть обе стороны границы.
    assert ATTEMPTS_WITHOUT in EXPECTED_ATTEMPTS.values()
    assert ATTEMPTS_WITH_RETRY in EXPECTED_ATTEMPTS.values()


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

    expected = EXPECTED_ATTEMPTS[(method, failure)]
    assert len(seen) == expected, (
        "{} × {}: попыток {}, ожидалось {}".format(
            method, failure, len(seen), expected)
    )


def test_the_second_click_does_not_duplicate_after_the_first_one_failed(monkeypatch):
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

    assert len(calls["save"]) == ATTEMPTS_WITH_RETRY, (
        "полезный повтор «API ещё не поднят» обязан пережить правку"
    )
    assert len(calls["complete"]) == ATTEMPTS_WITHOUT, (
        "второй клик по уже пожившему клиенту продублировал эффект"
    )
    # ⛔ Утверждается РАЗНИЦА, а не совпадение с состоянием «до»
    # (`PROTOCOL §3`): до правки оба числа были 4, и равенство было зелёным
    # ровно потому, что дефект жив.
    assert len(calls["save"]) != len(calls["complete"])


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
        with pytest.raises(APIError) as failure:
            client._request("POST", "/api/validation/u/masks/complete")
    finally:
        client.close()

    assert len(seen) == ATTEMPTS_WITHOUT, (
        "один клик оператора снова собирает конкурентные обработчики"
    )
    # Сообщение называет ФАКТИЧЕСКОЕ число попыток: «after 4 attempts» после
    # единственной было бы неправдой ровно там, где разбирают инцидент.
    assert "after 1 attempts" in str(failure.value)


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


def test_an_endpoint_the_client_does_not_even_have_is_protected_too(monkeypatch):
    """⭐ Лечение не держится на перечне «таких POST ровно 36».

    Адрес выдуман — такого эндпоинта в клиенте нет и не было. Защита всё
    равно работает, потому что вопрос задан состоянию («соединение было?»),
    а не списку. Значит POST, который заведут завтра, защищён с рождения,
    и ошибка в числе 36 перестаёт быть дефектом (`PROTOCOL §3`, замеры 1-41
    и 1-6 доработка 4).
    """
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        raise httpx.ReadTimeout("сервер думает", request=request)

    client = _client(httpx.MockTransport(handler), monkeypatch)
    try:
        with pytest.raises(APIError):
            client._request("POST", "/api/чего-нибудь/нового/{uid}/start")
    finally:
        client.close()

    assert len(seen) == ATTEMPTS_WITHOUT


def test_the_refusal_to_retry_leaves_a_trace_with_the_address(monkeypatch, caplog):
    """Д2: у неповторённого запроса остаётся след, и в нём адрес с uid.

    Иначе «клиент не дошёл» и «клиент сдался нарочно» в логе неразличимы —
    а разбирают инцидент именно по логу.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("60 с вышли", request=request)

    client = _client(httpx.MockTransport(handler), monkeypatch)
    uid = "5f2c1a7e-0000-4000-8000-000000000001"
    try:
        with caplog.at_level("WARNING", logger="ui.services.api_client"):
            with pytest.raises(APIError):
                client._request("POST", f"/api/validation/{uid}/masks/complete")
    finally:
        client.close()

    trace = [r.getMessage() for r in caplog.records]
    assert any(uid in line and "ReadTimeout" in line for line in trace), trace
    assert any("POST" in line for line in trace), trace


def test_a_successful_call_says_nothing(monkeypatch, caplog):
    """Порог с другой стороны: удачный POST молчит.

    Без этого сторож «след есть» был бы зелён и у болтливого клиента,
    который пишет предупреждение на каждый успешный запрос.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "started"})

    client = _client(httpx.MockTransport(handler), monkeypatch)
    try:
        with caplog.at_level("WARNING", logger="ui.services.api_client"):
            assert client._request("POST", "/api/graph/u/build") == {"status": "started"}
    finally:
        client.close()

    assert [r.getMessage() for r in caplog.records] == []

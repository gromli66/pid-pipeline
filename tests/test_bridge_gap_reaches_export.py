# -*- coding: utf-8 -*-
"""Ширина разрыва мостов доезжает от регулятора «Ручной правки» до выгрузки.

Дефект, ради которого набор написан (замер 2026-08-26). Оператор поставил
разрыв 7, а выгруженный лист совпал БАЙТ В БАЙТ с генерацией на дефолте
`BRIDGE_GAP_STROKE_FACTOR = 3.0` (85313 байт, разрывы 6 px вместо 14).
Терялось не значение — терялся путь: авто-сборка FXML после «Подтвердить»
(`complete_graph_validation`) звала задачу голым uid, а `bridge_gap` читала
только кнопка «Пересобрать чертёж» (`/api/graph/{uid}/generate-fxml`).

Здесь судятся две ноги пути, каждая ИСПОЛНЕНИЕМ, а не грепом:
  • клиент → HTTP: запрос уходит с параметром (httpx.MockTransport);
  • точка вызова: «Подтвердить» берёт ту же настройку диаграммы, что и
    «Пересобрать чертёж» — это чтение исходника, потому что метод живёт на
    Qt-виджете и поднимать окно ради одной строки дороже, чем она стоит.
Серверная нога (эндпоинт → kwargs задачи) заперта в
`tests/test_validation_dispatch_failure_gate.py`, где уже стоит поддельная
`AsyncSession` и журнал `send_task`.
"""
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = (REPO / "ui" / "widgets" / "diagram_workspace.py").read_text(encoding="utf-8")

UID = "ae30deb4-1b54-44d1-9212-3bababc4c9a4"


def _method_body(marker: str) -> str:
    """Тело метода от его `def` до следующего `def` того же уровня."""
    idx = WORKSPACE.index(marker)
    nxt = WORKSPACE.index(chr(10) + "    def ", idx + 1)
    return WORKSPACE[idx:nxt]


def _client_with_spy():
    """APIClient поверх MockTransport: запросы никуда не уходят, но видны."""
    from ui.services.api_client import APIClient

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "validated_graph", "task_id": "t"})

    api = APIClient(base_url="http://test")
    api._client = httpx.Client(base_url="http://test",
                               transport=httpx.MockTransport(handler))
    return api, seen


def test_confirmation_carries_the_gap_to_the_server():
    api, seen = _client_with_spy()
    api.complete_graph_validation(UID, bridge_gap=7.0)
    assert len(seen) == 1
    assert seen[0].url.path == f"/api/validation/{UID}/graph/complete"
    assert seen[0].url.params.get("bridge_gap") == "7.0"


def test_without_the_setting_the_request_is_as_before():
    """Настройки нет — параметра нет, конвертер остаётся на своём дефолте."""
    api, seen = _client_with_spy()
    api.complete_graph_validation(UID)
    assert "bridge_gap" not in seen[0].url.params


def test_rebuild_button_sends_the_same_parameter():
    api, seen = _client_with_spy()
    api.generate_fxml(UID, bridge_gap=7.0)
    assert seen[0].url.params.get("bridge_gap") == "7.0"


def test_confirm_reads_the_setting_of_this_diagram():
    """«Подтвердить» берёт ту же настройку, что и «Пересобрать чертёж»."""
    body = _method_body("def _on_graph_confirmed")
    assert "complete_graph_validation(" in body
    assert "bridge_gap=self._bridge_gap_setting()" in body

    helper = _method_body("def _bridge_gap_setting")
    assert 'get_appearance(self._uid, "bridge_gap_factor"' in helper

    rebuild = _method_body("def _regenerate_fxml")
    assert "bridge_gap = self._bridge_gap_setting()" in rebuild

def test_server_reads_the_gap_from_the_query():
    """FastAPI объявляет `bridge_gap` параметром ЗАПРОСА — без `Query(...)`.

    Голый дефолт `None` выбран нарочно (докстрока эндпоинта: с `Query(...)`
    прямой вызов корутины получал бы дефолтом объект `Query`, и он уезжал бы
    в Celery). Цена выбора — этот сторож: перестань параметр читаться из
    query, и клиентская нога била бы в пустоту молча.
    """
    from app.api.validation import router

    routes = [r for r in router.routes
              if getattr(r, "path", "").endswith("/graph/complete")]
    assert len(routes) == 1, [getattr(r, "path", "") for r in routes]
    assert "bridge_gap" in {p.name for p in routes[0].dependant.query_params}


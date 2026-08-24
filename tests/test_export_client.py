# -*- coding: utf-8 -*-
"""Клиентская половина этапа «Экспорт»: скачивание и ветвление кнопки.

Замки на решения 2026-08-23:
* этап формирует ОБА файла сам, поэтому кнопка на готовой схеме ничего не
  перезапускает, а открывает диалог «скачать / пересобрать»;
* чертёж всегда 1:1 с холстом — размер листа клиент не передаёт вовсе;
* скачивание переживает обрыв связи (раньше один разрыв на готовом 80-КБ .prtx
  ронял весь экспорт).
"""
import os

import httpx
import pytest

pytest.importorskip("PySide6")

import ui.services.api_client as api_mod                               # noqa: E402
from ui.services.api_client import APIClient, APIError                 # noqa: E402
from ui.services.api_client import DiagramStatus as S                  # noqa: E402
from ui.widgets import diagram_workspace as dw                         # noqa: E402


# =====================================================================
# Скачивание файлов: ретраи
# =====================================================================

class _FlakyTransport:
    """Роняет первые `fail_times` запросов, дальше отвечает `response`."""

    def __init__(self, fail_times, response):
        self.fail_times = fail_times
        self.response = response
        self.attempts = 0

    def request(self, method, endpoint, **kwargs):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise httpx.ConnectError("WinError 10054")
        return self.response

    def close(self):
        pass


def _wire(monkeypatch, transport, max_retries=3):
    """Клиент, у которого и первый, и пересозданный транспорт — наш.

    Между попытками код пересоздаёт httpx.Client (лечит connection reset),
    поэтому без подмены фабрики вторая попытка уходила бы в реальную сеть.
    """
    api = APIClient(base_url="http://x", retry_delay=0.0, max_retries=max_retries)
    api.close()
    api._client = transport
    monkeypatch.setattr(api_mod.httpx, "Client", lambda **kw: transport)
    return api


class _Response:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.text = ""

    def json(self):
        return self._payload


def test_скачивание_переживает_обрыв_связи(monkeypatch):
    """Файл на сервере есть — рвать весь экспорт из-за одного reset нельзя."""
    transport = _FlakyTransport(2, _Response(content=b"PRTX"))
    api = _wire(monkeypatch, transport)

    assert api._request_raw("GET", "/x").content == b"PRTX"
    assert transport.attempts == 3


def test_скачивание_сдаётся_после_всех_попыток(monkeypatch):
    transport = _FlakyTransport(99, _Response())
    api = _wire(monkeypatch, transport, max_retries=1)

    with pytest.raises(APIError):
        api._request_raw("GET", "/x")
    assert transport.attempts == 2


def test_ответ_с_ошибкой_не_ретраится(monkeypatch):
    """404 «артефакта нет» повторять нечего — придёт таким же."""
    transport = _FlakyTransport(0, _Response(404, {"detail": "not found"}))
    api = _wire(monkeypatch, transport)

    with pytest.raises(APIError) as exc:
        api._request_raw("GET", "/x")

    assert exc.value.status_code == 404
    assert transport.attempts == 1


# =====================================================================
# Кнопка «Экспорт»
# =====================================================================

class _Api:
    def __init__(self):
        self.fxml_calls = []
        self.downloads = []

    def generate_fxml(self, uid, **kwargs):
        self.fxml_calls.append(kwargs)

    def download_artifact(self, uid, artifact_type, dest_path):
        self.downloads.append((artifact_type, str(dest_path)))


class _Signal:
    def __init__(self):
        self.messages = []

    def emit(self, text, timeout=0):
        self.messages.append(text)


class _Stub:
    """Ровно те методы кнопки «Экспорт», что проверяем."""

    _start_fxml = dw.DiagramWorkspace._start_fxml
    _regenerate_fxml = dw.DiagramWorkspace._regenerate_fxml
    _download_export = dw.DiagramWorkspace._download_export

    def __init__(self, status=S.COMPLETED):
        self._uid = "u"
        self._last_status = status
        self._prtx_skip_next = False
        self.api_client = _Api()
        self.status_provider = type("SP", (), {"watch": lambda *a: None})()
        self.status_message = _Signal()
        self.dialog_opened = 0

    def _open_export_dialog(self):
        self.dialog_opened += 1

    def _refresh_status(self):
        pass


@pytest.fixture(autouse=True)
def _no_qt(monkeypatch):
    """Диалоги и курсор ожидания — заглушками: QApplication в тестах нет."""
    monkeypatch.setattr(dw, "QApplication", type("A", (), {
        "setOverrideCursor": staticmethod(lambda *a: None),
        "restoreOverrideCursor": staticmethod(lambda *a: None)}))
    monkeypatch.setattr(dw, "QMessageBox", type("M", (), {
        "information": staticmethod(lambda *a, **k: None),
        "warning": staticmethod(lambda *a, **k: None)}))


@pytest.fixture(autouse=True)
def _no_ui_settings(monkeypatch):
    """Настройки редактора в тестах не читаем — они лезут на диск."""
    import ui.services.ui_settings as us
    monkeypatch.setattr(us, "UISettings", type("U", (), {
        "instance": staticmethod(
            lambda: type("I", (), {"get_appearance": lambda *a, **k: None})())}))


def test_готовая_схема_открывает_диалог_и_ничего_не_пересобирает():
    """Оба файла уже сформированы этапом — кнопке остаётся их отдать."""
    w = _Stub(S.COMPLETED)
    w._start_fxml()

    assert w.dialog_opened == 1
    assert w.api_client.fxml_calls == []


def test_недоехавший_авто_путь_формирует_оба():
    w = _Stub(S.GENERATING_FXML)
    w._start_fxml()

    assert w.dialog_opened == 0
    assert len(w.api_client.fxml_calls) == 1
    assert w._prtx_skip_next is False, "автосборку .prtx подавлять здесь нечем"


def test_размер_листа_клиент_не_передаёт():
    """Чертёж всегда 1:1 с холстом: page_size ушёл из UI вместе с выбором."""
    w = _Stub(S.GENERATING_FXML)
    w._start_fxml()

    assert "page_size" not in w.api_client.fxml_calls[0]


def test_пересборка_только_чертежа_взводит_подавление():
    w = _Stub(S.COMPLETED)
    w._regenerate_fxml(with_prtx=False)

    assert w._prtx_skip_next is True
    assert len(w.api_client.fxml_calls) == 1


def test_расчётная_схема_ложится_рядом_с_чертежом(tmp_path):
    """Имя с точками внутри резать нельзя — «1. Схема…» уезжала в «1.prtx»."""
    w = _Stub()
    fxml = os.path.join(str(tmp_path), "1. Схема отборов пара.fxml")

    w._download_export(fxml, want_fxml=True, want_prtx=True)

    assert w.api_client.downloads == [
        ("fxml", fxml),
        ("prtx", os.path.join(str(tmp_path), "1. Схема отборов пара.prtx")),
    ]


def test_скачивается_только_отмеченное(tmp_path):
    w = _Stub()
    w._download_export(os.path.join(str(tmp_path), "d.fxml"),
                       want_fxml=False, want_prtx=True)

    assert [t for t, _ in w.api_client.downloads] == ["prtx"]


def test_провал_одного_файла_не_прячет_второй(tmp_path):
    """Раньше первая же ошибка обрывала экспорт целиком."""
    w = _Stub()

    def _boom(uid, artifact_type, dest_path):
        w.api_client.downloads.append((artifact_type, str(dest_path)))
        if artifact_type == "prtx":
            raise APIError("Artifact 'prtx' not found", 404)

    w.api_client.download_artifact = _boom
    w._download_export(os.path.join(str(tmp_path), "d.fxml"),
                       want_fxml=True, want_prtx=True)

    assert [t for t, _ in w.api_client.downloads] == ["fxml", "prtx"]

# -*- coding: utf-8 -*-
"""Диагностика лицензии САПФИР — `ui/services/prtx_license.py`.

Зачем. Про неверно прописанный ключ оператор узнавал в конце пайплайна, на
экспорте. Диагностика вынесена вперёд (кнопка «🔑 Лицензия САПФИР»), и её
ценность целиком в том, НАЗЫВАЕТ ли она причину. Поэтому тесты проверяют не
только «ок / не ок», но и то, что в отчёте есть подсказка, а в ней — путь или
действие, по которому оператор поймёт, что чинить.

Разбираются четыре живые ошибки набора: кавычки вокруг пути (client.cfg берёт
значение как есть), путь к файлу вместо папки, несуществующая папка и чужой
файл вместо ключа.
"""
import pytest

pytest.importorskip("PySide6")

from ui.services import prtx_license as pl                      # noqa: E402
from ui.services.prtx_converter import LICENSE_KEY_FILE          # noqa: E402

KEY = b"XB" + b"A" * 27          # 29 печатаемых ascii — как настоящий


def _home_with_key(tmp_path, name="home", data=KEY):
    home = tmp_path / name
    home.mkdir(exist_ok=True)
    (home / LICENSE_KEY_FILE).write_bytes(data)
    return home


def _fail(report):
    """Первая провалившаяся проверка — она и показывается оператору."""
    assert not report.ok, "ожидался провал, а отчёт чистый"
    return report.first_problem


def test_key_in_profile_is_ok(monkeypatch, tmp_path):
    monkeypatch.delenv("PRTX_LICENSE_HOME", raising=False)
    monkeypatch.setattr(pl.Path, "home", classmethod(lambda cls: _home_with_key(tmp_path)))
    report = pl.diagnose_local()
    assert report.ok
    assert report.first_problem is None


def test_key_by_env_is_ok(monkeypatch, tmp_path):
    home = _home_with_key(tmp_path, "Иван Петров")
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(home))
    assert pl.diagnose_local().ok


def test_quotes_in_path_are_named(monkeypatch, tmp_path):
    """client.cfg не снимает кавычки — путь уезжает в движок вместе с ними."""
    monkeypatch.setenv("PRTX_LICENSE_HOME", f'"{tmp_path}"')
    problem = _fail(pl.diagnose_local())
    assert "авычк" in problem.title
    assert "PRTX_LICENSE_HOME=" in problem.hint      # показан правильный вид


def test_path_to_file_instead_of_folder(monkeypatch, tmp_path):
    """Частая ошибка: указали сам ключ, а нужна папка с ним."""
    home = _home_with_key(tmp_path)
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(home / LICENSE_KEY_FILE))
    problem = _fail(pl.diagnose_local())
    assert "файл" in problem.title.lower()
    assert str(home) in problem.hint                 # подсказан путь без имени файла


def test_missing_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(tmp_path / "нет-такой"))
    problem = _fail(pl.diagnose_local())
    assert "апка" in problem.title
    assert "нет-такой" in problem.detail


def test_missing_key_points_to_profile_copy(monkeypatch, tmp_path):
    """Ключ есть в профиле, а ищем не там — прямо это и говорим."""
    profile = _home_with_key(tmp_path, "профиль")
    empty = tmp_path / "пусто"
    empty.mkdir()
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(empty))
    monkeypatch.setattr(pl.Path, "home", classmethod(lambda cls: profile))

    problem = _fail(pl.diagnose_local())
    assert "Файл ключа" == problem.title
    assert "PRTX_LICENSE_HOME" in problem.hint       # советуем убрать строку


def test_empty_key(monkeypatch, tmp_path):
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(_home_with_key(tmp_path, data=b"")))
    problem = _fail(pl.diagnose_local())
    assert "пуст" in problem.detail


def test_foreign_file_instead_of_key(monkeypatch, tmp_path):
    """Подсунули не тот файл — ключ это ascii-строка, а не бинарь."""
    monkeypatch.setenv("PRTX_LICENSE_HOME",
                       str(_home_with_key(tmp_path, data=b"%PDF-1.4\x00\x01\x02\xff")))
    problem = _fail(pl.diagnose_local())
    assert "не похоже на ключ" in problem.detail
    assert LICENSE_KEY_FILE in problem.hint


def test_key_never_appears_in_report(monkeypatch, tmp_path):
    """Отчёт показывается на экране — самого ключа в нём быть не должно."""
    secret = b"SECRET-KEY-VALUE-0123456789AB"
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(_home_with_key(tmp_path, data=secret)))
    text = "\n".join(f"{c.title} {c.detail} {c.hint}" for c in pl.diagnose_local().checks)
    assert secret.decode() not in text


# --- серверная половина -------------------------------------------------

class FakeApi:
    def __init__(self, state=None, exc=None):
        self._state, self._exc = state, exc

    def prtx_health(self):
        if self._exc:
            raise self._exc
        return self._state


def test_server_ready(monkeypatch, tmp_path):
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(_home_with_key(tmp_path)))
    report = pl.diagnose(FakeApi({"ok": True, "busy": False}))
    assert report.ok
    assert report.checks[-1].title == "Конвертер на сервере"


def test_server_busy_is_still_ok(monkeypatch, tmp_path):
    """Занят чужим прогоном — это не отказ, просто придётся подождать."""
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(_home_with_key(tmp_path)))
    report = pl.diagnose(FakeApi({"ok": True, "busy": True}))
    assert report.ok
    assert "считает" in report.checks[-1].detail


def test_server_unreachable(monkeypatch, tmp_path):
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(_home_with_key(tmp_path)))
    problem = _fail(pl.diagnose(FakeApi(exc=RuntimeError("connection refused"))))
    assert problem.title == "Конвертер на сервере"
    assert "connection refused" in problem.detail


def test_local_problem_short_circuits_server(monkeypatch, tmp_path):
    """Нет ключа — сервер не опрашиваем, чинить надо у себя."""
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(tmp_path / "нет"))

    class Boom(FakeApi):
        def prtx_health(self):
            raise AssertionError("сервер опрошен, хотя ключа нет")

    report = pl.diagnose(Boom())
    assert not report.ok
    # строки про сервер в отчёте вообще нет — до него не дошли
    assert all(c.title != "Конвертер на сервере" for c in report.checks)


def test_old_server_without_health_endpoint(monkeypatch, tmp_path):
    """Сервер старее клиента (404 на проверке) — это не отказ лицензии."""
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(_home_with_key(tmp_path)))

    class Old:
        def prtx_health(self):
            exc = RuntimeError("Not Found")
            exc.status_code = 404
            raise exc

    report = pl.diagnose(Old())
    assert report.ok, "старый сервер не должен выглядеть как сломанная лицензия"
    assert "сервер старее" in report.checks[-1].detail

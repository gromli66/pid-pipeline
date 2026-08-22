# -*- coding: utf-8 -*-
"""Лицензия САПФИР на стороне клиента: где взять ключ и что без него.

Замок против двух измеренных вещей:

1. Без ключа движок не падает, а показывает модальное окно и висит до своего
   300-секундного дедлайна (замер 2026-08-22 в контейнере: ровно 5 минут, потом
   `TIMEOUT waiting for editor`). Сервис считает схемы по одной, поэтому такой
   прогон держит очередь всех операторов. Значит наличие ключа проверяет
   клиент — до того, как запрос уйдёт на сервер.
2. Ключ лежит в профиле КОНКРЕТНОГО пользователя Windows. Если САПФИР
   активирован под другой учёткой, каталог задаётся `PRTX_LICENSE_HOME`.

Сама сборка живёт на сервере (docker/prtx), клиент только отдаёт ключ на один
прогон — поэтому здесь же заперто, что без ключа в сеть не ходят вовсе.
"""
import pytest

pytest.importorskip("PySide6")

from ui.services import prtx_converter as pc                    # noqa: E402


def _home_with_key(tmp_path, name="home", data=b"KEY"):
    home = tmp_path / name
    home.mkdir(exist_ok=True)
    (home / pc.LICENSE_KEY_FILE).write_bytes(data)
    return home


class FakeApi:
    """Поверхность api_client, которой пользуется PrtxWorker."""

    def __init__(self, prtx=b"PRTX", states=None):
        self.built = []
        self.downloaded = []
        self.polls = 0
        self._prtx = prtx
        #: что отдаёт /prtx/status по порядку; последнее значение повторяется
        self._states = list(states or [{"state": "done"}])

    def build_prtx(self, uid, license_key):
        self.built.append((uid, license_key))
        return {"status": "building"}

    def prtx_status(self, uid):
        self.polls += 1
        return self._states[min(self.polls - 1, len(self._states) - 1)]

    def download_artifact(self, uid, artifact_type, dest_path):
        self.downloaded.append((uid, artifact_type))
        dest_path.write_bytes(self._prtx)
        return dest_path


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Опрос в тестах — без пауз: ждать нечего, сервер поддельный."""
    monkeypatch.setattr(pc, "POLL_SEC", 0)


def _run(worker):
    """Прогнать работника без QThread и собрать, что он проэмитил."""
    seen = {}
    worker.finished.connect(lambda where: seen.setdefault("finished", where))
    worker.error.connect(lambda msg: seen.setdefault("error", msg))
    worker.run()
    return seen


def test_license_home_defaults_to_user_profile(monkeypatch, tmp_path):
    monkeypatch.delenv("PRTX_LICENSE_HOME", raising=False)
    monkeypatch.setattr(pc.Path, "home", classmethod(lambda cls: tmp_path))
    assert pc.license_key_path() == tmp_path / pc.LICENSE_KEY_FILE


def test_license_home_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(tmp_path / "чужой профиль"))
    assert pc.license_home() == tmp_path / "чужой профиль"


def test_read_key_returns_bytes(monkeypatch, tmp_path):
    # ключ бинарный, а профиль бывает кириллическим — обе беды сразу
    home = _home_with_key(tmp_path, "Иван Петров", data=b"\x00\x01\xff key")
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(home))
    assert pc.read_license_key() == b"\x00\x01\xff key"


def test_no_key_names_the_missing_file(monkeypatch, tmp_path):
    (tmp_path / "пусто").mkdir()
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(tmp_path / "пусто"))
    with pytest.raises(pc.PrtxError) as exc:
        pc.read_license_key()
    assert "Лицензия САПФИР не найдена" in str(exc.value)
    assert pc.LICENSE_KEY_FILE in str(exc.value)


def test_empty_key_is_refused(monkeypatch, tmp_path):
    """Пустой файл движок принял бы за отсутствие ключа и ушёл бы в модалку."""
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(_home_with_key(tmp_path, data=b"")))
    with pytest.raises(pc.PrtxError):
        pc.read_license_key()


def test_worker_without_key_never_calls_server(monkeypatch, tmp_path):
    """Без ключа запрос не уходит — иначе сервис 5 минут держал бы очередь."""
    (tmp_path / "пусто").mkdir()
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(tmp_path / "пусто"))
    api = FakeApi()

    seen = _run(pc.PrtxWorker(api, "uid-1", None))

    assert api.built == []
    assert "Лицензия САПФИР не найдена" in seen["error"]
    assert "finished" not in seen


def test_worker_sends_key_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(_home_with_key(tmp_path, data=b"KEY29")))
    api = FakeApi()

    seen = _run(pc.PrtxWorker(api, "uid-2", None))

    assert api.built == [("uid-2", b"KEY29")]
    # без пути экспорта готовый файл никуда не тянем
    assert api.downloaded == []
    assert seen["finished"] == "на сервере"


def test_worker_puts_copy_next_to_fxml(monkeypatch, tmp_path):
    """Оператор сохранил .fxml — рядом должен лечь .prtx, скачанный с сервера."""
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(_home_with_key(tmp_path)))
    export = tmp_path / "экспорт" / "схема.fxml"
    export.parent.mkdir()
    api = FakeApi(prtx=b"PRTX-BODY")

    seen = _run(pc.PrtxWorker(api, "uid-3", export))

    target = export.with_suffix(".prtx")
    assert target.read_bytes() == b"PRTX-BODY"
    assert api.downloaded == [("uid-3", "prtx")]
    assert str(target) in seen["finished"]


def test_worker_reports_server_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(_home_with_key(tmp_path)))

    class Failing(FakeApi):
        def build_prtx(self, uid, license_key):
            raise RuntimeError("сервис конвертера недоступен")

    seen = _run(pc.PrtxWorker(Failing(), "uid-4", None))

    assert "сервис конвертера недоступен" in seen["error"]
    assert "finished" not in seen


# --- имя файла при экспорте --------------------------------------------

@pytest.mark.parametrize("given, expected", [
    # уже .prtx — не трогаем
    (r"C:\out\схема.prtx", r"C:\out\схема.prtx"),
    # рядом с чертежом — заменяем расширение
    (r"C:\out\схема.fxml", r"C:\out\схема.prtx"),
    (r"C:\out\2. Схема Т.С.fxml", r"C:\out\2. Схема Т.С.prtx"),
    # ⚠ имя с точками и без расширения: with_suffix схлопнул бы в «1.prtx»
    (r"C:\out\1. Схема отборов и дренажей турбины 1",
     r"C:\out\1. Схема отборов и дренажей турбины 1.prtx"),
    (r"C:\out\2. Схема основных магистралей и подпитки Т.С",
     r"C:\out\2. Схема основных магистралей и подпитки Т.С.prtx"),
    (r"C:\out\схема", r"C:\out\схема.prtx"),
])
def test_export_target_keeps_dotted_names(given, expected):
    """Оператор не находил файл: путь с точками в имени резался до «1.prtx»."""
    assert str(pc._prtx_target(given)) == expected


def test_worker_waits_for_background_build(monkeypatch, tmp_path):
    """Сборка идёт минутами: клиент опрашивает сервер, а не ждёт в запросе."""
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(_home_with_key(tmp_path)))
    api = FakeApi(states=[{"state": "building"}, {"state": "building"},
                          {"state": "done"}])

    seen = _run(pc.PrtxWorker(api, "uid-5", None))

    assert api.polls == 3
    assert seen["finished"] == "на сервере"


def test_worker_reports_build_error_from_status(monkeypatch, tmp_path):
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(_home_with_key(tmp_path)))
    api = FakeApi(states=[{"state": "error", "error": "движок не принял ключ"}])

    seen = _run(pc.PrtxWorker(api, "uid-6", None))

    assert "движок не принял ключ" in seen["error"]
    assert api.downloaded == []


def test_worker_notices_lost_job(monkeypatch, tmp_path):
    """Сервер перезапустили посреди сборки — не ждём молча до таймаута."""
    monkeypatch.setenv("PRTX_LICENSE_HOME", str(_home_with_key(tmp_path)))
    api = FakeApi(states=[{"state": "idle"}])

    seen = _run(pc.PrtxWorker(api, "uid-7", None))

    assert "потерял задание" in seen["error"]

"""Сборка расчётной схемы САПФИР (.prtx) из валидированного графа.

Считает СЕРВЕР — контейнер `prtx` (docker/prtx) с движком САПФИР. Замер
2026-08-21/22: движок в Linux даёт .prtx, дамп которого совпадает с
windows-прогоном байт-в-байт, так что клиенту держать у себя коробку на
660 МБ незачем. Единственное, чего у сервера нет, — ключ лицензии: он лежит
в профиле оператора, клиент читает его и отправляет вместе с запросом на
один прогон. На сервере ключ не хранится — ни на диске, ни в БД, ни в логах
(см. docker/prtx/server.py).

Клиенту остаётся одна настройка — `PRTX_LICENSE_HOME` в `client.cfg`, и та
нужна, только если САПФИР активирован под другой учётной записью Windows.
Нет ключа — нет сборки: движок без него не падает, а показывает модальное
окно и висит до своего 300-секундного дедлайна, поэтому проверяем сами и до
отправки.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

#: Файл ключа лицензии САПФИР (license4j). Движок читает его из user.home:
#: `LicenseChecker` собирает путь как `System.getProperty("user.home") + File.separator
#: + ".S$lk$.bin"`. Активация САПФИРа кладёт его в профиль пользователя.
LICENSE_KEY_FILE = ".S$lk$.bin"

#: как часто спрашивать сервер о готовности схемы
POLL_SEC = 3
#: сколько всего ждём сборку: замер 2026-08-22 — 2 минуты на схеме со сканом
#: 4.6 МБ, плюс очередь (сервис считает схемы по одной)
BUILD_WAIT_SEC = 900


class PrtxError(RuntimeError):
    """Расчётная схема не собрана."""


def license_home() -> Path:
    """Каталог, в котором лежит ключ лицензии САПФИР.

    По умолчанию — профиль текущего пользователя Windows. Если САПФИР
    активировали под ДРУГОЙ учётной записью, путь задаётся `PRTX_LICENSE_HOME`
    (в `client.cfg`).
    """
    raw = os.environ.get("PRTX_LICENSE_HOME")
    return Path(raw) if raw else Path.home()


def license_key_path() -> Path:
    """Путь к файлу ключа лицензии САПФИР."""
    return license_home() / LICENSE_KEY_FILE


def read_license_key() -> bytes:
    """Прочитать ключ лицензии. Бросает PrtxError, если его нет."""
    key = license_key_path()
    if not key.is_file():
        raise PrtxError(
            f"Лицензия САПФИР не найдена: нет файла {key}. "
            "Если САПФИР активирован под другой учётной записью Windows — "
            "укажите её профиль в PRTX_LICENSE_HOME (client.cfg)"
        )
    data = key.read_bytes()
    if not data:
        raise PrtxError(f"Ключ лицензии пуст: {key}")
    return data


#: расширения чертежа, которые при экспорте схемы заменяются на .prtx
_DRAWING_SUFFIXES = (".fxml", ".xml")


def _prtx_target(path) -> Path:
    """Путь для .prtx рядом с выбранным файлом.

    ⚠ Не `with_suffix`: у диаграмм имена вида «1. Схема отборов … турбины 1»,
    и Python считает суффиксом всё после ПОСЛЕДНЕЙ точки — такой путь
    схлопывался в «1.prtx», файл уезжал не туда, а снаружи это выглядело как
    «экспорт молча ничего не сохранил» (замер 2026-08-22).
    """
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix == ".prtx":
        return target
    if suffix in _DRAWING_SUFFIXES:
        return target.with_suffix(".prtx")
    return target.with_name(target.name + ".prtx")


class PrtxWorker(QObject):
    """Отдать серверу ключ → он соберёт .prtx → положить копию рядом с FXML.

    Живёт в отдельном QThread (образец — `ArtifactDownloader`): сборка на
    сервере занимает десятки секунд и сериализована с другими операторами,
    на GUI-потоке это заморозка окна.
    """

    finished = Signal(str)   # человекочитаемое «куда положили»
    error = Signal(str)
    progress = Signal(str)   # «схема считается, 40 с…» — в статусбар

    def __init__(self, api_client, uid: str, export_path: Path | None):
        super().__init__()
        self.api_client = api_client
        self.uid = uid
        #: путь, куда оператор сохранил .fxml — рядом ляжет .prtx (или None)
        self.export_path = export_path

    def _wait_for_build(self):
        """Дождаться конца фоновой сборки короткими опросами.

        Не ждём в самом запросе сборки: он держал бы соединение минутами, а
        промежуточные узлы такое рвут (замер 2026-08-22). Каждый опрос —
        доли секунды, рвать нечего.
        """
        deadline = time.monotonic() + BUILD_WAIT_SEC
        while time.monotonic() < deadline:
            time.sleep(POLL_SEC)
            state = self.api_client.prtx_status(self.uid)
            kind = state.get("state")
            if kind == "done":
                return
            if kind == "error":
                raise PrtxError(state.get("error") or "сборка не удалась")
            if kind == "idle":
                # Сервер перезапустили посреди сборки — состояние он потерял.
                raise PrtxError(
                    "Сервер потерял задание (перезапуск?) — повторите экспорт")
            self.progress.emit(
                f"⏳ Схема считается на сервере… {int(time.monotonic() - deadline + BUILD_WAIT_SEC)} с")
        raise PrtxError(f"Схема не собралась за {BUILD_WAIT_SEC // 60} мин")

    def run(self):
        try:
            key = read_license_key()
            self.api_client.build_prtx(self.uid, key)
            self._wait_for_build()
            where = ["на сервере"]

            if self.export_path:
                target = _prtx_target(self.export_path)
                tmp = Path(tempfile.mkdtemp(prefix="prtx_"))
                try:
                    self.api_client.download_artifact(self.uid, "prtx", tmp / "diagram.prtx")
                    shutil.copyfile(tmp / "diagram.prtx", target)
                    where.append(str(target))
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)

            self.finished.emit(" и ".join(where))

        except PrtxError as exc:
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 — сеть/диск, в UI уходит текст
            logger.error("PRTX: %s", exc, exc_info=True)
            self.error.emit(str(exc))

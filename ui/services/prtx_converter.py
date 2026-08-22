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
from pathlib import Path

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

#: Файл ключа лицензии САПФИР (license4j). Движок читает его из user.home:
#: `LicenseChecker` собирает путь как `System.getProperty("user.home") + File.separator
#: + ".S$lk$.bin"`. Активация САПФИРа кладёт его в профиль пользователя.
LICENSE_KEY_FILE = ".S$lk$.bin"


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


class PrtxWorker(QObject):
    """Отдать серверу ключ → он соберёт .prtx → положить копию рядом с FXML.

    Живёт в отдельном QThread (образец — `ArtifactDownloader`): сборка на
    сервере занимает десятки секунд и сериализована с другими операторами,
    на GUI-потоке это заморозка окна.
    """

    finished = Signal(str)   # человекочитаемое «куда положили»
    error = Signal(str)

    def __init__(self, api_client, uid: str, export_path: Path | None):
        super().__init__()
        self.api_client = api_client
        self.uid = uid
        #: путь, куда оператор сохранил .fxml — рядом ляжет .prtx (или None)
        self.export_path = export_path

    def run(self):
        try:
            key = read_license_key()
            self.api_client.build_prtx(self.uid, key)
            where = ["на сервере"]

            if self.export_path:
                target = Path(self.export_path).with_suffix(".prtx")
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

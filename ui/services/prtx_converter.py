"""Сборка расчётной схемы САПФИР (.prtx) из валидированного графа.

Почему это делает КЛИЕНТ, а не воркер (замер 2026-08-20). Движок САПФИР
поднимается и в Linux-контейнере (temurin 17 + Xvfb + libXtst), и первый этап
коробки (`py/json2xml.py`, чистый Python) там отрабатывает за 0.3 с. Упирается
всё в лицензию: `ru.get.dcad.license.LicenseChecker` привязан к железу машины
(на Windows в логе `validateLicenseKey: hdd Serial = ...`), в контейнере он
показывает модальное «Чтобы использовать данный продукт, Вы обязаны иметь
действительную лицензию» — EDT уходит в цикл диалога, `Xml2PrtxCli` вылетает
по своему 300-секундному дедлайну с кодом 10 и без файла на выходе. Поэтому
конвертация живёт там, где лицензия — на клиентской Windows-машине, а сервер
только хранит результат (`POST /api/graph/{uid}/prtx/upload`).

Коробка конвертера лежит ВНЕ этого репо (свой git, ветка `rules/unknow-boundary-arrow`):
`C:\\project\\prt_convertor\\converter-box`, переопределяется `PRTX_BOX_DIR`.
Точка входа — `json2prtx.cmd <граф.json> <скан.png|-> <выход.prtx> [all|bound|none]`,
коды возврата: 0 — успех, 1 — аргументы, 2 — вход не разобран, 3 — сбой движка.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

DEFAULT_BOX_DIR = Path(r"C:\project\prt_convertor\converter-box")

#: движок грузит ядро САПФИР и рисует лист — на корпусе 400-узловых схем
#: полный прогон укладывается в ~40 с, запас на холодный старт
CONVERT_TIMEOUT_SEC = 900

#: cmd.exe отдаёт вывод в кодировке консоли; читаем только для лога
_CONSOLE_ENCODING = "cp866"


class PrtxError(RuntimeError):
    """Коробка не собрала .prtx."""


def box_dir() -> Path | None:
    """Каталог коробки конвертера или None, если её нет на этой машине."""
    raw = os.environ.get("PRTX_BOX_DIR")
    candidate = Path(raw) if raw else DEFAULT_BOX_DIR
    return candidate if (candidate / "json2prtx.cmd").is_file() else None


def convert(
    graph_path: Path,
    image_path: Path | None,
    out_path: Path,
    text_mode: str = "all",
) -> None:
    """Собрать .prtx из graph_validated.json. Бросает PrtxError.

    ⚠ Пути передаём коробке ТОЛЬКО ascii-шные (temp-каталог): батники коробки
    ломаются на кириллице в аргументах, а папку экспорта выбирает оператор.
    Копирование в конечное место — уже питоном, см. PrtxWorker.
    """
    box = box_dir()
    if box is None:
        raise PrtxError(
            f"Коробка конвертера не найдена: {os.environ.get('PRTX_BOX_DIR') or DEFAULT_BOX_DIR}"
        )

    cmd = [
        "cmd", "/c", str(box / "json2prtx.cmd"),
        str(graph_path),
        str(image_path) if image_path else "-",
        str(out_path),
        text_mode,
    ]
    logger.info("PRTX: %s", " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=CONVERT_TIMEOUT_SEC,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise PrtxError(f"Конвертация не уложилась в {CONVERT_TIMEOUT_SEC} с") from exc
    except OSError as exc:
        raise PrtxError(f"Не удалось запустить json2prtx.cmd: {exc}") from exc

    output = (proc.stdout + proc.stderr).decode(_CONSOLE_ENCODING, errors="replace")
    if proc.returncode != 0:
        reason = {
            1: "неверные аргументы",
            2: "вход не разобран (json2xml)",
            3: "сбой движка САПФИР (XML → prtx)",
        }.get(proc.returncode, f"код возврата {proc.returncode}")
        logger.error("PRTX failed (%s):\n%s", reason, output[-4000:])
        raise PrtxError(f"Конвертер не собрал схему: {reason}")

    if not out_path.is_file():
        logger.error("PRTX: код 0, но файла нет:\n%s", output[-4000:])
        raise PrtxError("Конвертер отчитался успехом, но файла нет")

    logger.info("PRTX собран: %s (%d байт)", out_path, out_path.stat().st_size)


class PrtxWorker(QObject):
    """Скачать граф со схемой → собрать .prtx → положить в storage и рядом с FXML.

    Живёт в отдельном QThread (образец — `ArtifactDownloader`): движок считает
    десятки секунд, на GUI-потоке это заморозка окна.
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
        tmp = Path(tempfile.mkdtemp(prefix="prtx_"))
        try:
            graph = self.api_client.download_artifact(
                self.uid, "graph_validated", tmp / "graph_validated.json")

            # Скан не обязателен: без него угол насосов берётся по трубам
            image = tmp / "image.png"
            try:
                self.api_client.download_artifact(self.uid, "original_image", image)
            except Exception as exc:  # noqa: BLE001 — скан опционален, причина в лог
                logger.info("PRTX: скан не скачался (%s), считаем без него", exc)
                image = None

            out = tmp / "diagram.prtx"
            convert(Path(graph), image, out)

            where = []
            self.api_client.upload_prtx(self.uid, out)
            where.append("на сервере")

            if self.export_path:
                target = Path(self.export_path).with_suffix(".prtx")
                shutil.copyfile(out, target)
                where.append(str(target))

            self.finished.emit(" и ".join(where))

        except PrtxError as exc:
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 — сеть/диск, в UI уходит текст
            logger.error("PRTX: %s", exc, exc_info=True)
            self.error.emit(str(exc))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

# -*- coding: utf-8 -*-
"""HTTP-обёртка над коробкой конвертера: (граф, скан, ключ) → .prtx.

Только stdlib: образ собран из коробки САПФИР, pip в нём нет.

Ключ лицензии сервис НЕ ХРАНИТ. Он приезжает в теле запроса, кладётся в
каталог на tmpfs (`/dev/shm`), уезжает движку как `-Duser.home` и удаляется в
`finally`. На диск не попадает, между запросами не живёт, в лог не пишется.
Замер 2026-08-21: движок в user.home ничего не создаёт, каталогу хватает
права на чтение.

Прогон сериализован замком: движок пишет своё состояние в общий
`engine/SETTINGS` (Logs, EventLog.log, s3.ini), два параллельных прогона в
одном контейнере передрались бы за него.
"""
import base64
import binascii
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("prtx")

PORT = int(os.environ.get("PRTX_PORT", "8081"))
#: полный прогон на 400-узловой схеме — десятки секунд, запас на холодный старт
TIMEOUT_SEC = int(os.environ.get("PRTX_TIMEOUT_SEC", "900"))
#: скан бывает многомегабайтным, но не безразмерным
MAX_BODY = int(os.environ.get("PRTX_MAX_BODY", str(64 * 1024 * 1024)))
LICENSE_KEY_FILE = ".S$lk$.bin"

_lock = threading.Lock()


class BuildError(Exception):
    """Не собрали .prtx. `code` — HTTP-статус ответа."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


#: Движок пишет свой журнал в каталог настроек и НЕ подчищает его: замер
#: 2026-08-22 — 4.7 МБ за один прогон, то есть полгига на сотню схем прямо в
#: слое контейнера. Чистим после УСПЕШНОГО прогона; после сбоя оставляем —
#: там причина, и её же кладёт в свой лог сервис.
_ENGINE_LOGS = Path("/opt/box/engine/SETTINGS")


def _sweep_engine_logs() -> None:
    try:
        for path in list((_ENGINE_LOGS / "Logs").glob("*")) + \
                list(_ENGINE_LOGS.glob("EventLog.log*")):
            if path.is_file():
                path.unlink()
    except OSError as exc:
        logger.warning("журнал движка не подчистился: %s", exc)


def _write_license(tmp_root: Path, key: bytes) -> Path:
    """Положить ключ на tmpfs и вернуть каталог для `-Duser.home`."""
    home = Path(tempfile.mkdtemp(prefix="lic_", dir=str(tmp_root)))
    home.chmod(0o700)
    (home / LICENSE_KEY_FILE).write_bytes(key)
    return home


def build(graph: bytes, image: bytes | None, key: bytes, text_mode: str) -> bytes:
    """Собрать .prtx. Бросает BuildError."""
    # /dev/shm — RAM: ключ не касается диска даже на время прогона
    tmp_root = Path("/dev/shm") if Path("/dev/shm").is_dir() else Path(tempfile.gettempdir())
    work = Path(tempfile.mkdtemp(prefix="prtx_", dir=str(tmp_root)))
    try:
        graph_path = work / "graph_validated.json"
        graph_path.write_bytes(graph)

        image_path = work / "image.png"
        if image:
            image_path.write_bytes(image)

        out = work / "diagram.prtx"
        lic_home = _write_license(work, key)

        cmd = [
            "/usr/local/bin/json2prtx",
            str(graph_path),
            str(image_path) if image else "-",
            str(out),
            text_mode,
        ]
        env = dict(os.environ, PRTX_LIC_HOME=str(lic_home))

        with _lock:
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, timeout=TIMEOUT_SEC, env=env)
            except subprocess.TimeoutExpired as exc:
                raise BuildError(
                    f"Конвертация не уложилась в {TIMEOUT_SEC} с", 504) from exc

        if proc.returncode != 0:
            output = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
            reason, code = {
                1: ("неверные аргументы", 500),
                2: ("вход не разобран (json2xml)", 422),
                3: ("сбой движка САПФИР (XML → prtx)", 500),
                4: ("ключ лицензии не доехал до движка", 500),
            }.get(proc.returncode, (f"код возврата {proc.returncode}", 500))
            # Непринятый ключ выглядит не как ошибка, а как молчание: движок
            # уходит в модальное окно лицензии и сдаётся по своему 300-секундному
            # дедлайну (замер 2026-08-22 — ровно 5 минут). Не выдавать это за
            # «сбой движка»: оператору надо чинить ключ, а не схему.
            if "TIMEOUT waiting for editor" in output:
                reason, code = (
                    "движок не принял ключ лицензии (или не поднялся) — "
                    "прогон снят по дедлайну 300 с", 403)
            logger.error("прогон провален (%s):\n%s", reason, output[-4000:])
            raise BuildError(f"Конвертер не собрал схему: {reason}", code)

        if not out.is_file():
            raise BuildError("Конвертер отчитался успехом, но файла нет", 500)
        _sweep_engine_logs()
        return out.read_bytes()
    finally:
        # вместе с каталогом уходит и ключ
        shutil.rmtree(work, ignore_errors=True)


def _decode(payload: dict, field: str, required: bool = True) -> bytes | None:
    raw = payload.get(field)
    if raw is None:
        if required:
            raise BuildError(f"нет обязательного поля '{field}'")
        return None
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise BuildError(f"поле '{field}' — не base64") from exc


class Handler(BaseHTTPRequestHandler):
    server_version = "prtx/1.0"

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — имя диктует BaseHTTPRequestHandler
        if self.path == "/health":
            self._reply(200, {"ok": True, "busy": _lock.locked()})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/build":
            self._reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise BuildError("пустое тело запроса")
            if length > MAX_BODY:
                raise BuildError(f"тело больше {MAX_BODY} байт", 413)

            try:
                payload = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BuildError("тело — не JSON") from exc

            graph = _decode(payload, "graph")
            key = _decode(payload, "license")
            image = _decode(payload, "image", required=False)
            text_mode = payload.get("text_mode") or "all"
            if text_mode not in ("all", "bound", "none"):
                raise BuildError(f"неизвестный text_mode: {text_mode}")

            logger.info(
                "сборка: граф %d Б, скан %s, текст %s",
                len(graph), f"{len(image)} Б" if image else "нет", text_mode)
            prtx = build(graph, image, key, text_mode)
            logger.info("готово: %d Б", len(prtx))
            self._reply(200, {
                "prtx": base64.b64encode(prtx).decode("ascii"),
                "size": len(prtx),
            })
        except BuildError as exc:
            self._reply(exc.code, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 — сервис не должен падать целиком
            logger.exception("необработанный сбой")
            self._reply(500, {"error": f"внутренний сбой: {exc}"})

    def log_message(self, fmt, *args):
        logger.info("%s %s", self.address_string(), fmt % args)


if __name__ == "__main__":
    logger.info("prtx-сервис слушает :%d (таймаут %d с)", PORT, TIMEOUT_SEC)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

# -*- coding: utf-8 -*-
"""redelivery_bench.py — передоставка задачи брокером (пункт 1.2 · Б10 дороги).

Зачем. При `task_acks_late=True` подтверждение уходит брокеру только по
завершении задачи, а Redis держит выданное сообщение невидимым ровно
`visibility_timeout` секунд. Опция не была задана нигде → работал дефолт kombu
**3600 с** при `time_limit=5400` у детекции: 90-минутная задача гарантированно
доживала до 60-й минуты неподтверждённой, и брокер выдавал её ВТОРОЙ раз.
Сегментация и OCR стоят ровно на границе (3600 = 3600).

Утверждение проверяемо только на живом брокере: чтением кода видны предпосылки,
а не сам факт повторной выдачи. Стенд сжимает боевую связку 3600 < 5400 в 720
раз и смотрит, сколько раз задача пришла воркеру.

| нога | условия | ожидание | что доказывает |
|---|---|---|---|
| A | `visibility_timeout` < длительности | доставок ≥ 2 | дубль воспроизводится — «как на бою сегодня» |
| B | `visibility_timeout` > длительности | ровно 1 | порог заперт с двух сторон: стенд не красит всегда |
| C | боевой `celery_app`, воркер не нужен | 7200 на КАНАЛЕ и > самого длинного `time_limit` | опция доезжает до kombu, а не лежит в конфиге |

Нога C красная до правки (канал отдавал 3600) и зелёная после — это и есть зонд
гейта. Ноги A и B — зонд друг другу.

Безопасность. Стенд ходит в **отдельную базу Redis 15** и свою очередь
`probe_redelivery`; боевых очередей локального стека (`default`/`gpu`/`ocr`/
`sam2` в базе 0) он не касается и отказывается работать, если база не 15.
Задача-заглушка зарегистрирована ПОД ИМЕНЕМ боевой детекции
(`worker.tasks.detection.task_detect_yolo`), тело — `sleep`: гейт пункта звучит
«в логе Celery нет повторного `task_detect_yolo` с тем же uid», и лог стенда
читается буквально. Ничего из боевого кода детекции при этом не исполняется.

⛔ Гонять ногу A надо в БОЕВОЙ среде — Linux + prefork, то есть в контейнере:

    docker exec -w /app pid_worker python -X utf8 tools/redelivery_bench.py --check

Замер 2026-08-18: там дубль воспроизводится стабильно (3 из 3, приходил через
137.8 / 137.7 / 81.6 с), а на Windows с пулом `threads` — как повезёт (2 из 5:
17.9 и 104.9 с, потом три прогона подряд по 150-300 с без единой передоставки,
при том что сообщение всё это время висело в `unacked` с просроченным окном).
Восстановлением просроченных занимается периодический скан kombu
(`maybe_restore_messages` раз в 10 с, тело `restore_visible` — каждый десятый
вызов), и на Windows он до задачи не доходит. Дефект от этого не исчезает: бой
— это Linux и prefork. Поэтому нога A на Windows не «проваливается», а честно
отдаёт «судить нечем» (exit 2) и называет команду для контейнера.

Redis нужен живой, поэтому в CI стенда нет — там `--check` тоже отдаёт 2.
Инварианты конфига без брокера закрыты
`tests/test_worker/test_broker_visibility.py`, они в CI.

Запуск (из корня репо, локальный стек поднят):
    python -X utf8 tools/redelivery_bench.py --check      # A + B + C
    python -X utf8 tools/redelivery_bench.py --leg A      # только воспроизведение дубля
    python -X utf8 tools/redelivery_bench.py --leg C      # только конфиг (брокер не нужен)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from celery import Celery

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:                    # запуск и как скрипт, и как `celery -A`
    sys.path.insert(0, str(REPO))

PROBE_DB = 15                                    # боевые очереди живут в базе 0
PROBE_QUEUE = "probe_redelivery"
TASK_NAME = "worker.tasks.detection.task_detect_yolo"
DEFAULT_BROKER = "redis://localhost:6380/0"

# Длительности ног. Опорное число — период скана «просроченных» у kombu: воркер
# зовёт `maybe_restore_messages` каждые 10 с, а тело `restore_visible`
# отрабатывает каждый десятый вызов, то есть скан идёт примерно раз в 100 с.
# Поэтому задача обязана пережить хотя бы один скан с запасом.
LEG_A_VT, LEG_A_SLEEP = 5, 150
LEG_A_ATTEMPTS = 2                               # каждая попытка — свежая фаза скана
LEG_B_VT, LEG_B_SLEEP = 200, 120
LEG_TAIL = 25                                    # ожидание сверх длительности задачи
READY_TIMEOUT = 90                               # старт воркера стенда


def probe_broker_url() -> str:
    """Боевой URL брокера с подменённым номером базы на 15."""
    url = (os.getenv("CELERY_BROKER_URL") or DEFAULT_BROKER).rstrip("/")
    return "{}/{}".format(re.sub(r"/[0-9]+$", "", url), PROBE_DB)


# --------------------------------------------------------------------------- #
# Приложение стенда. Импортируется и родителем, и воркером (celery -A ...).
# --------------------------------------------------------------------------- #

# ⛔ Переменная окружения `CELERY_BROKER_URL` СИЛЬНЕЕ аргумента конструктора: в
# контейнере она задана как redis://redis:6379/0, и воркер стенда, запущенный
# там через `celery -A tools.redelivery_bench`, сел на БОЕВУЮ базу 0 (замерено
# 2026-08-18 по строке «Connected to redis://redis:6379/0» в его логе). Поэтому
# окружение пиннится здесь, до создания приложения, и любой запуск стенда —
# хоть свой, хоть руками — гарантированно уходит в базу 15.
os.environ["CELERY_BROKER_URL"] = probe_broker_url()
os.environ["CELERY_RESULT_BACKEND"] = probe_broker_url()

app = Celery("redelivery_bench", broker=probe_broker_url(),
             backend=probe_broker_url())
app.conf.update(
    # Те же три настройки, что делают дубль возможным в бою (celery_app.py:53,54,66).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue=PROBE_QUEUE,
    broker_transport_options={"visibility_timeout": int(os.getenv("PROBE_VT", "5"))},
)


@app.task(name=TASK_NAME, bind=True)
def probe_task(self, uid: str) -> str:
    """Заглушка под именем боевой детекции: записать факт доставки и поспать."""
    path = Path(os.environ["PROBE_LOG"])
    record = {"uid": uid, "task_id": self.request.id, "pid": os.getpid(),
              "at": time.time()}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    time.sleep(float(os.getenv("PROBE_SLEEP", "150")))
    return "ok"


# --------------------------------------------------------------------------- #
# Числа боевого конфига (нога C). Тест импортирует их отсюда, а не копирует.
# --------------------------------------------------------------------------- #

def task_time_limits() -> dict[str, int]:
    """{celery-имя задачи -> time_limit} по декораторам `worker/tasks/*.py`."""
    out: dict[str, int] = {}
    for path in sorted((REPO / "worker" / "tasks").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        consts = dict(re.findall(r"^([A-Z][A-Z0-9_]*)\s*=\s*([0-9]+)", src, re.M))
        for dec in re.finditer(r"@celery_app\.task\(", src):
            head = src[dec.start():src.index("\ndef ", dec.start())]
            name = re.search(r'name="([^"]+)"', head)
            limit = re.search(r"[^_]\btime_limit=(\w+)", head)
            if not (name and limit):
                continue
            raw = limit.group(1)
            out[name.group(1)] = int(raw) if raw.isdigit() else int(consts[raw])
    return out


def live_channel_visibility_timeout() -> int | None:
    """`visibility_timeout` на канале боевого `celery_app` (None — брокер молчит)."""
    from kombu.exceptions import OperationalError

    from worker.celery_app import celery_app
    try:
        return int(celery_app.connection().default_channel.visibility_timeout)
    except (OSError, OperationalError):
        return None


# --------------------------------------------------------------------------- #
# Ноги A и B: живой воркер на живом брокере
# --------------------------------------------------------------------------- #

def _flush_probe_db() -> None:
    import redis

    url = probe_broker_url()
    if not url.endswith("/{}".format(PROBE_DB)):
        raise SystemExit("стенд отказывается работать вне базы {}: {}".format(PROBE_DB, url))
    client = redis.Redis.from_url(url)
    client.flushdb()
    client.close()


def _default_pool() -> str:
    """prefork — как в боевом контейнере; на Windows он не работает."""
    return "threads" if sys.platform == "win32" else "prefork"


def _deliveries(log: Path, uid: str) -> list[dict]:
    if not log.exists():
        return []
    rows = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row["uid"] == uid:
                rows.append(row)
    return sorted(rows, key=lambda r: r["at"])


def run_leg(vt: int, sleep_s: int, expect_duplicate: bool, pool: str,
            keep_logs: bool = False) -> dict:
    """Поднять воркер с заданным `visibility_timeout`, выдать одну задачу."""
    _flush_probe_db()
    workdir = Path(tempfile.mkdtemp(prefix="redelivery_bench_"))
    delivery_log = workdir / "deliveries.jsonl"
    celery_log = workdir / "celery.log"
    uid = uuid.uuid4().hex[:8]

    env = dict(os.environ)
    env.update(PROBE_VT=str(vt), PROBE_SLEEP=str(sleep_s),
               PROBE_LOG=str(delivery_log), PYTHONPATH=str(REPO),
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

    cmd = [sys.executable, "-X", "utf8", "-m", "celery", "-A",
           "tools.redelivery_bench", "worker", "-Q", PROBE_QUEUE,
           "-c", "2", "-P", pool, "--loglevel=INFO", "-n", "probe@%h"]

    started = time.time()
    sent = started
    with celery_log.open("w", encoding="utf-8") as sink:
        worker = subprocess.Popen(cmd, cwd=str(REPO), env=env, stdout=sink,
                                  stderr=subprocess.STDOUT)
        try:
            producer = Celery("redelivery_bench_producer",
                              broker=probe_broker_url(), backend=probe_broker_url())
            deadline = time.time() + READY_TIMEOUT
            while not producer.control.ping(timeout=1.0):
                if worker.poll() is not None:
                    raise SystemExit("воркер стенда умер на старте, лог: {}".format(celery_log))
                if time.time() > deadline:
                    raise SystemExit("воркер стенда не поднялся за {} с, лог: {}".format(
                        READY_TIMEOUT, celery_log))

            sent = time.time()
            producer.send_task(TASK_NAME, args=[uid], queue=PROBE_QUEUE)

            deadline = sent + sleep_s + LEG_TAIL
            while time.time() < deadline:
                if expect_duplicate and len(_deliveries(delivery_log, uid)) >= 2:
                    break                      # доказано, ждать до конца незачем
                time.sleep(2)
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=30)
            except subprocess.TimeoutExpired:
                worker.kill()

    rows = _deliveries(delivery_log, uid)
    _flush_probe_db()
    return {
        "uid": uid,
        "visibility_timeout": vt,
        "sleep": sleep_s,
        "pool": pool,
        "deliveries": len(rows),
        "task_ids": sorted({r["task_id"] for r in rows}),
        "first_delay": round(rows[0]["at"] - sent, 1) if rows else None,
        "dup_delay": round(rows[1]["at"] - rows[0]["at"], 1) if len(rows) > 1 else None,
        "elapsed": round(time.time() - started, 1),
        "celery_log": str(celery_log) if (keep_logs or len(rows) > 1) else None,
    }


# --------------------------------------------------------------------------- #
# Отчёт и приёмка
# --------------------------------------------------------------------------- #

def leg_config() -> dict:
    """Нога C: числа боевого конфига. Брокер нужен только для канала."""
    from worker.celery_app import celery_app

    limits = task_time_limits()
    top = max(limits.items(), key=lambda kv: kv[1]) if limits else (None, 0)
    return {
        "conf": celery_app.conf.broker_transport_options.get("visibility_timeout"),
        "channel": live_channel_visibility_timeout(),
        "task_time_limit": celery_app.conf.task_time_limit,
        "limits_found": len(limits),
        "longest_task": top[0],
        "longest_limit": top[1],
    }


def verdict(legs: dict) -> tuple[int, list[str]]:
    """0 — приёмка пройдена, 1 — провал, 2 — судить нечем (среда не воспроизводит)."""
    lines, bad, unknown = [], 0, 0
    a, b, c = legs.get("A"), legs.get("B"), legs.get("C")

    if a is not None:
        head = ("A: visibility_timeout={} c < задачи {} c → доставок {} (нужно ≥2), "
                "дубль через {} c, попыток {}").format(
                    a["visibility_timeout"], a["sleep"], a["deliveries"],
                    a["dup_delay"], a.get("attempts", 1))
        if a["deliveries"] >= 2:
            lines.append("OK     " + head)
        elif a["pool"] != "prefork":
            unknown += 1
            lines.append("НЕЧЕМ  " + head)
            lines.append("       периодический скан kombu на пуле «{}» не сработал; "
                         "бой — это Linux и prefork, гонять там:".format(a["pool"]))
            lines.append("       docker exec -w /app pid_worker python -X utf8 "
                         "tools/redelivery_bench.py --check")
        else:
            bad += 1
            lines.append("ПРОВАЛ " + head)
    if b is not None:
        ok = b["deliveries"] == 1
        bad += 0 if ok else 1
        lines.append(
            "{} B: visibility_timeout={} c > задачи {} c → доставок {} "
            "(нужно ровно 1)".format("OK    " if ok else "ПРОВАЛ",
                                     b["visibility_timeout"], b["sleep"], b["deliveries"]))
    if c is not None:
        checks = (
            (c["conf"] is not None and c["channel"] == c["conf"],
             "C: канал брокера отдаёт {} при {} в конфиге".format(c["channel"], c["conf"])),
            (c["limits_found"] >= 12,
             "C: в worker/tasks найдено {} задач с time_limit (нужно ≥12)".format(
                 c["limits_found"])),
            ((c["conf"] or 0) > c["longest_limit"],
             "C: {} против самого длинного time_limit {} ({})".format(
                 c["conf"], c["longest_limit"], c["longest_task"])),
            ((c["conf"] or 0) > c["task_time_limit"],
             "C: {} против глобального task_time_limit {}".format(
                 c["conf"], c["task_time_limit"])),
        )
        for ok, text in checks:
            bad += 0 if ok else 1
            lines.append("{} {}".format("OK    " if ok else "ПРОВАЛ", text))

    lines.append("\nнарушений критериев приёмки: {}{}".format(
        bad, " · судить нечем: {}".format(unknown) if unknown else ""))
    return (1 if bad else (2 if unknown else 0)), lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Б10: передоставка задачи брокером (visibility_timeout)")
    parser.add_argument("--leg", choices=["A", "B", "C"], default=None,
                        help="прогнать одну ногу вместо всех")
    parser.add_argument("--pool", default=_default_pool(),
                        help="пул воркера стенда (по умолчанию prefork, на Windows threads)")
    parser.add_argument("--attempts", type=int, default=LEG_A_ATTEMPTS,
                        help="сколько раз пробовать поймать дубль в ноге A")
    parser.add_argument("--keep-logs", action="store_true",
                        help="не скрывать путь к логу Celery при отсутствии дубля")
    parser.add_argument("--check", action="store_true",
                        help="вердикт по критериям приёмки, exit 1 при нарушении")
    args = parser.parse_args(argv)

    wanted = [args.leg] if args.leg else ["A", "B", "C"]
    print("брокер стенда: {} · очередь: {} · задача: {}".format(
        probe_broker_url(), PROBE_QUEUE, TASK_NAME))

    legs: dict[str, dict] = {}
    if "C" in wanted:
        legs["C"] = leg_config()
        c = legs["C"]
        print("\nконфиг: broker_transport_options.visibility_timeout={} · "
              "на канале {} · task_time_limit={}".format(
                  c["conf"], c["channel"], c["task_time_limit"]))
        print("        самый длинный time_limit: {} ({}), задач с лимитом {}".format(
            c["longest_limit"], c["longest_task"], c["limits_found"]))
    for leg in ("A", "B"):
        if leg not in wanted:
            continue
        vt, sleep_s = (LEG_A_VT, LEG_A_SLEEP) if leg == "A" else (LEG_B_VT, LEG_B_SLEEP)
        attempts = args.attempts if leg == "A" else 1
        for attempt in range(1, attempts + 1):
            print("\nнога {}{}: visibility_timeout={} c, задача {} c, пул {} — идёт…".format(
                leg, " (попытка {} из {})".format(attempt, attempts) if attempts > 1 else "",
                vt, sleep_s, args.pool))
            legs[leg] = run_leg(vt, sleep_s, expect_duplicate=(leg == "A"),
                                pool=args.pool, keep_logs=args.keep_logs)
            legs[leg]["attempts"] = attempt
            if leg != "A" or legs[leg]["deliveries"] >= 2:
                break
        row = legs[leg]
        print("        uid={} доставок={} task_id={} первая через {} c, "
              "дубль через {} c, всего {} c".format(
                  row["uid"], row["deliveries"], row["task_ids"], row["first_delay"],
                  row["dup_delay"], row["elapsed"]))
        if row["celery_log"]:
            print("        лог Celery: {}".format(row["celery_log"]))

    if args.check:
        code, lines = verdict(legs)
        print()
        for line in lines:
            print(line)
        return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

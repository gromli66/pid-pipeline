# -*- coding: utf-8 -*-
"""flower_gate.py — гейт сервиса Flower (пункт 3.x1 дороги).

Зачем. Flower ставится, чтобы дубли задач (пункт 1.2 · Б10) было видно глазами.
Но сам Flower — обычный потребитель kombu, и **своим окном невидимости он может
отменить фикс 1.2**: `restore_visible` режет по окну СКАНИРУЮЩЕГО канала
(`kombu/transport/redis.py:415`, `ceil = time() - self.visibility_timeout`),
а ключ `unacked_index` — ОДИН на всю базу (`:640`), не по очередям. Канал,
поднятый на голом URL, берёт дефолт **3600** (`:644`) и возвращает в очередь
сообщения, которые боевой воркер держит под окном **7200**. Отсюда требование:
Flower поднимается с нашим приложением (`-A worker.celery_app`), тогда окна
совпадают.

| нога | что проверяет | ожидание | что доказывает |
|---|---|---|---|
| A | сканер с КОРОТКИМ окном против чужого unacked | сообщение восстановлено | механизм реален — так Flower на голом URL отменял бы 1.2 |
| B | сканер с окном ЖЕРТВЫ (парность) | не восстановлено | порог заперт с двух сторон: стенд не красит всегда |
| C | боевой контейнер Flower | окно **7200** на канале и > самого длинного `time_limit` | `-A worker.celery_app` доехал до kombu, а не лежит в тексте |
| D | Flower показывает очереди и задачи | зондовая задача видна | собственно польза сервиса |

Ноги A и B — зонд друг другу: одна и та же машинерия, разница только в окне
сканера. Нога A красная (то есть «дефект воспроизводится») — это норма, она
описывает мир БЕЗ правки; провал ноги A означает, что стенд ничего не меряет.

⚠ Границы ноги A. Она доказывает **механизм**, а не частоту: срабатывание
`maybe_restore_messages` в стенде вызывается явно, потому что у блокирующего
потребителя kombu зовёт его только на пустом опросе (`redis.py:606`), а
регулярно — лишь на событийном цикле (`redis.py:1382`, раз в 10 с). Гейт
запирает инвариант «ни один канал на брокере не несёт окно короче воркерского»,
а не «Flower восстанавливает каждые N секунд».

Безопасность. Ноги A и B ходят в **отдельную базу Redis 15** и свои очереди
`probe_flower_*`; при иной базе стенд отказывается работать. Нога D кладёт
задачу в БОЕВУЮ очередь (иначе Flower её не увидит — он смотрит базу 0), но
задача — встроенная `celery.accumulate`: чистая функция, `ignore_result=True`,
ни БД, ни storage, ни статусов. Очередь и брокер параметризованы.

| исход | код | когда |
|---|---|---|
| доказано | 0 | все запрошенные ноги отработали как ожидалось |
| опровергнуто | 1 | Flower не поднят / не показывает / несёт чужое окно; порог не заперт |
| судить нечем | 2 | нет брокера, нет воркера на очереди зонда, нет docker/контейнера |

⛔ «Flower не поднят» — это КРАСНЫЙ исход, а не «судить нечем»: ровно это гейт
и обязан ловить.

⛔ `CELERY_BROKER_URL` сильнее аргумента `Celery(broker=...)` (замер 1.2) —
поэтому зонд пинит окружение сам и печатает адрес, на который сел.

Запуск (из корня репо, локальный стек поднят):
    python -X utf8 tools/flower_gate.py --check
    python -X utf8 tools/flower_gate.py --leg A          # только механизм дефекта
    python -X utf8 tools/flower_gate.py --leg D --queue default
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_BROKER = "redis://localhost:6380/0"      # порт хоста из docker-compose.yml
DEFAULT_FLOWER = "http://127.0.0.1:5555"         # loopback: у Flower нет аутентификации
DEFAULT_QUEUE = "default"                        # слушает pid_worker (-Q default,gpu,sam2)
DEFAULT_CONTAINER = "pid_flower"
PROBE_TASK = "celery.accumulate"                 # встроенная чистая задача celery
TASK_WAIT_S = 30                                 # событие долетает за ~1 с, запас на CI

BENCH_DB = 15                                    # боевые очереди живут в базе 0
BENCH_QUEUE = "probe_flower_held"                # очередь «жертвы»
BENCH_SCAN_QUEUE = "probe_flower_scan"           # очередь сканера — заведомо другая
VICTIM_WINDOW = 60                               # окно «воркера» в сжатии (бой: 7200)
SHORT_WINDOW = 5                                 # окно голого URL в сжатии (бой: 3600)
HELD_FOR_S = 8                                   # держим unacked дольше короткого окна
EXPECTED_WINDOW = 7200                           # то же число, что в worker/celery_app.py

PROVEN, REFUTED, NO_VERDICT = 0, 1, 2


def _get_json(url: str, timeout: float = 10.0):
    """GET с разбором JSON. Возвращает (данные, None) или (None, причина)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None, f"HTTP {resp.status}"
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return None, f"нет ответа: {exc}"
    except json.JSONDecodeError as exc:
        return None, f"не JSON: {exc}"


def _bench_url(broker: str) -> str:
    """Тот же сервер Redis, но база стенда: боевые очереди не трогаем."""
    head, _, _ = broker.rpartition("/")
    return f"{head}/{BENCH_DB}"


# --- ноги A и B: чужой unacked против окна сканера ---------------------------

def _leg_restore(broker: str, scanner_window: int) -> bool | None:
    """Держим сообщение под окном VICTIM_WINDOW и даём сканеру просканировать.

    Возвращает True, если сканер вернул сообщение в очередь, False — если нет,
    None — если стенда нет (нет Redis).
    """
    import redis
    from kombu import Connection, Consumer, Producer, Queue

    url = _bench_url(broker)
    if not url.endswith(f"/{BENCH_DB}"):             # страховка от боевой базы
        print(f"[судить нечем] стенд обязан идти в базу {BENCH_DB}, а не в {url}")
        return None
    try:
        client = redis.Redis.from_url(url)
        client.ping()
    except Exception as exc:                          # noqa: BLE001 — любой отказ сети = нет стенда
        print(f"[судить нечем] брокер недоступен: {exc}")
        return None

    held = Queue(BENCH_QUEUE, routing_key=BENCH_QUEUE)
    scanned = Queue(BENCH_SCAN_QUEUE, routing_key=BENCH_SCAN_QUEUE)
    for key in (BENCH_QUEUE, BENCH_SCAN_QUEUE, "unacked", "unacked_index"):
        client.delete(key)

    victim = Connection(url, transport_options={"visibility_timeout": VICTIM_WINDOW})
    scanner = Connection(url, transport_options={"visibility_timeout": scanner_window})
    try:
        vch = victim.channel()
        Producer(vch).publish({"probe": "held"}, routing_key=held.name, declare=[held])
        taken = []

        def _take(body, message):                 # ack НЕ шлём: сообщение остаётся unacked
            taken.append(message)

        Consumer(vch, [held], accept=["json"], callbacks=[_take]).consume()
        victim.drain_events(timeout=5)
        if len(taken) != 1 or client.zcard("unacked_index") != 1:
            print("[судить нечем] сообщение не встало в unacked — стенд не собрался")
            return None
        print(f"[стенд] жертва держит 1 сообщение под окном {VICTIM_WINDOW} с")

        time.sleep(HELD_FOR_S)                        # старше короткого окна, младше окна жертвы

        sch = scanner.channel()
        Consumer(sch, [scanned], accept=["json"], callbacks=[_ignore]).consume()
        # Триггер зовём явно: у блокирующего потребителя kombu доходит до него
        # только на пустом опросе (redis.py:606) — ждать этого случая недетерминированно.
        scanner.transport.cycle.maybe_restore_messages()
        restored = client.llen(held.name) == 1
        print(f"[стенд] сканер с окном {scanner_window} с: "
              f"в очереди {client.llen(held.name)}, в unacked {client.zcard('unacked_index')}")
        return restored
    finally:
        for conn in (victim, scanner):
            try:
                conn.release()
            except Exception:                          # noqa: BLE001 — закрытие не должно валить гейт
                pass
        for key in (BENCH_QUEUE, BENCH_SCAN_QUEUE, "unacked", "unacked_index"):
            client.delete(key)


def _ignore(body, message):
    """Сканеру содержимое не нужно — ему нужна активная очередь."""


def leg_a(broker: str) -> int:
    print("[нога A] сканер с коротким окном против чужого unacked")
    restored = _leg_restore(broker, SHORT_WINDOW)
    if restored is None:
        return NO_VERDICT
    if restored:
        print(f"[ДОКАЗАНО] окно {SHORT_WINDOW} с вернуло сообщение, которое держат "
              f"под окном {VICTIM_WINDOW} с — механизм реален")
        return PROVEN
    print("[ОПРОВЕРГНУТО] короткое окно ничего не вернуло — стенд ничего не меряет")
    return REFUTED


def leg_b(broker: str) -> int:
    print("[нога B] сканер с окном жертвы (парность окон)")
    restored = _leg_restore(broker, VICTIM_WINDOW)
    if restored is None:
        return NO_VERDICT
    if restored:
        print("[ОПРОВЕРГНУТО] равные окна всё равно вернули сообщение — порог не заперт")
        return REFUTED
    print(f"[ДОКАЗАНО] при равных окнах ({VICTIM_WINDOW} с) сообщение осталось у жертвы")
    return PROVEN


# --- нога C: боевой контейнер Flower ----------------------------------------

CONTAINER_SNIPPET = (
    "from worker.celery_app import celery_app;"
    "from tools.redelivery_bench import task_time_limits;"
    "print(celery_app.connection().default_channel.visibility_timeout,"
    "      max(task_time_limits().values()))"
)


def _container_cmdline(container: str) -> tuple[str | None, str]:
    """Полная командная строка контейнера (работает и для остановленного)."""
    try:
        done = subprocess.run(
            ["docker", "inspect", "-f", '{{.Path}} {{join .Args " "}}', container],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"docker недоступен: {exc}"
    if done.returncode != 0:
        return None, (done.stderr or done.stdout).strip()
    return done.stdout.strip(), ""


def leg_c(container: str) -> int:
    print(f"[нога C] окно невидимости в контейнере {container}")
    cmdline, why = _container_cmdline(container)
    if cmdline is None:
        print(f"[судить нечем] {why}")
        return NO_VERDICT
    print(f"[контейнер] команда: {cmdline}")
    if "-A worker.celery_app" not in cmdline:
        print("[ОПРОВЕРГНУТО] Flower поднят МИМО приложения проекта — его канал возьмёт "
              "дефолтное окно kombu и будет возвращать чужие unacked (пункт 1.2)")
        return REFUTED
    try:
        done = subprocess.run(
            ["docker", "exec", "-w", "/app", container, "python", "-c", CONTAINER_SNIPPET],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[судить нечем] docker недоступен: {exc}")
        return NO_VERDICT
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["без вывода"]
        if "No such container" in done.stderr or "is not running" in done.stderr:
            print(f"[судить нечем] контейнера {container} нет: {tail[0]}")
            return NO_VERDICT
        print(f"[ОПРОВЕРГНУТО] контейнер не поднимает приложение проекта: {tail[0]}")
        return REFUTED
    window, longest = (int(x) for x in done.stdout.split())
    print(f"[контейнер] окно {window} с, самый длинный time_limit {longest} с")
    if window != EXPECTED_WINDOW:
        print(f"[ОПРОВЕРГНУТО] окно {window} вместо {EXPECTED_WINDOW}: Flower поднят "
              f"мимо worker.celery_app и режет чужие unacked")
        return REFUTED
    if window <= longest:
        print(f"[ОПРОВЕРГНУТО] окно {window} не длиннее самой длинной задачи {longest}")
        return REFUTED
    print("[ДОКАЗАНО] Flower несёт то же окно, что воркер, и переживает самую длинную задачу")
    return PROVEN


# --- нога D: Flower показывает очереди и задачи ------------------------------

def _live_queues(app) -> tuple[set[str] | None, str]:
    """Очереди, о которых сообщили сами воркеры (мимо Flower)."""
    from kombu.exceptions import OperationalError   # мёртвый брокер приходит отсюда

    try:
        active = app.control.inspect(timeout=3.0).active_queues()
    except (OperationalError, OSError) as exc:
        return None, f"брокер недоступен: {exc}"
    if not active:
        return set(), "ни один воркер не ответил"
    names = {q["name"] for queues in active.values() for q in queues}
    return names, f"воркеров {len(active)}, очередей {len(names)}"


def leg_d(broker: str, flower_url: str, queue: str) -> int:
    print(f"[нога D] Flower {flower_url}, очередь зонда {queue!r}")
    os.environ["CELERY_BROKER_URL"] = broker      # env сильнее аргумента (замер 1.2)
    from celery import Celery

    app = Celery(broker=broker)

    live, note = _live_queues(app)
    if live is None:
        print(f"[судить нечем] {note}")
        return NO_VERDICT
    print(f"[зонд] воркеры: {note}; очереди {sorted(live) or '—'}")
    if queue not in live:
        print(f"[судить нечем] очередь {queue!r} никто не слушает — зонд некуда класть")
        return NO_VERDICT

    data, err = _get_json(f"{flower_url}/api/queues/length")
    if err:
        print(f"[ОПРОВЕРГНУТО] Flower не отвечает на /api/queues/length — {err}")
        return REFUTED
    shown = {q["name"] for q in data.get("active_queues", [])}
    print(f"[Flower] очереди: {sorted(shown) or '—'}")
    missing = live - shown
    if missing:
        print(f"[ОПРОВЕРГНУТО] Flower не показывает боевые очереди: {sorted(missing)}")
        return REFUTED

    task_id = str(uuid.uuid4())
    marker = f"flower_probe:{task_id[:8]}"
    app.send_task(PROBE_TASK, args=[marker], queue=queue,
                  task_id=task_id, ignore_result=True)
    print(f"[зонд] отправлена {PROBE_TASK} id={task_id} marker={marker}")

    deadline = time.monotonic() + TASK_WAIT_S
    last = "Flower о задаче ничего не знает"
    while time.monotonic() < deadline:
        time.sleep(1.0)
        info, err = _get_json(f"{flower_url}/api/task/info/{task_id}")
        if err:
            last = f"/api/task/info — {err}"
            continue
        state, worker = info.get("state"), info.get("worker")
        last = f"state={state} worker={worker}"
        if state == "SUCCESS":
            print(f"[Flower] задача {task_id}: name={info.get('name')} "
                  f"worker={worker} result={info.get('result')}")
            print("[ДОКАЗАНО] Flower показывает и очереди, и задачи")
            return PROVEN
        if state in ("FAILURE", "REVOKED"):
            break
    print(f"[ОПРОВЕРГНУТО] зондовая задача не видна в Flower за {TASK_WAIT_S} с — {last}")
    return REFUTED


LEGS = ("A", "B", "C", "D")


def run(legs, broker: str, flower_url: str, queue: str, container: str) -> int:
    print(f"[гейт] брокер {broker}, стенд {_bench_url(broker)}, ноги {','.join(legs)}")
    results = {}
    for leg in legs:
        if leg == "A":
            results[leg] = leg_a(broker)
        elif leg == "B":
            results[leg] = leg_b(broker)
        elif leg == "C":
            results[leg] = leg_c(container)
        else:
            results[leg] = leg_d(broker, flower_url, queue)
        print()
    verdicts = {PROVEN: "доказано", REFUTED: "ОПРОВЕРГНУТО", NO_VERDICT: "судить нечем"}
    print("[итог] " + ", ".join(f"{leg}: {verdicts[code]}" for leg, code in results.items()))
    if REFUTED in results.values():
        return REFUTED
    if NO_VERDICT in results.values():
        return NO_VERDICT
    return PROVEN


def main() -> int:
    parser = argparse.ArgumentParser(description="Гейт сервиса Flower (пункт 3.x1)")
    parser.add_argument("--check", action="store_true", help="прогнать все ноги")
    parser.add_argument("--leg", choices=LEGS, action="append", help="только эти ноги")
    parser.add_argument("--broker", default=DEFAULT_BROKER, help=f"по умолчанию {DEFAULT_BROKER}")
    parser.add_argument("--flower-url", default=DEFAULT_FLOWER, help=f"по умолчанию {DEFAULT_FLOWER}")
    parser.add_argument("--queue", default=DEFAULT_QUEUE,
                        help=f"очередь зонда ноги D, по умолчанию {DEFAULT_QUEUE}")
    parser.add_argument("--container", default=DEFAULT_CONTAINER,
                        help=f"контейнер Flower для ноги C, по умолчанию {DEFAULT_CONTAINER}")
    args = parser.parse_args()
    if not (args.check or args.leg):
        parser.print_help()
        return NO_VERDICT
    return run(args.leg or list(LEGS), args.broker,
               args.flower_url.rstrip("/"), args.queue, args.container)


if __name__ == "__main__":
    sys.exit(main())

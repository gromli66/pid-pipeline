# -*- coding: utf-8 -*-
"""flower_gate.py — зонд мониторинга Celery через Flower (пункт 3.x1 дороги).

Зачем. Пункт 1.2 (Б10) чинил передоставку задачи брокером; сам дефект был виден
только специальным стендом. Flower ставится ради того, чтобы такие вещи было
видно ГЛАЗАМИ: очереди, живые воркеры, задачи с их uuid и состоянием.

Гейт пункта звучит «Flower показывает очереди/задачи» — утверждение о рантайме,
чтением кода недоказуемое. Зонд доказывает его насквозь: кладёт в боевую очередь
безобидную задачу и требует увидеть её В FLOWER, а не в брокере.

Задача-зонд — встроенная `celery.accumulate` (чистая функция, возвращает свои же
аргументы): она зарегистрирована на боевом воркере, ничего не пишет ни в БД, ни
в storage, ни в статус диаграммы. `ignore_result=True` — осадка в бэкенде
результатов не остаётся.

| исход | код | когда |
|---|---|---|
| доказано | 0 | Flower отдал список очередей и показал зондовую задачу |
| опровергнуто | 1 | брокер и воркер живы, а Flower не отвечает / очередей не знает / задачи не видит |
| судить нечем | 2 | нет брокера или ни один воркер не слушает очередь зонда — стенда нет |

⛔ «Flower не поднят» — это КРАСНЫЙ исход (1), а не «судить нечем»: ровно это
гейт и обязан ловить. Проверено инъекцией: `docker stop pid_flower` → exit 1,
`docker start pid_flower` → exit 0. Исход 2 оставлен под отсутствие самого
стенда (нет Redis / нет воркеров), когда зонд физически некуда положить.

Живость брокера и воркеров зонд выясняет НАПРЯМУЮ (через celery), а не через
Flower — иначе «судить нечем» и «опровергнуто» смешались бы в один ответ.

⛔ `CELERY_BROKER_URL` сильнее аргумента `Celery(broker=...)` (замер 1.2) —
поэтому зонд пинит окружение сам и печатает адрес, на который сел.

Запуск (из корня репо, локальный стек поднят):
    python -X utf8 tools/flower_gate.py --check
    python -X utf8 tools/flower_gate.py --check --flower-url http://127.0.0.1:5555
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_BROKER = "redis://localhost:6380/0"      # порт хоста из docker-compose.yml
DEFAULT_FLOWER = "http://127.0.0.1:5555"         # loopback: у Flower нет аутентификации
PROBE_TASK = "celery.accumulate"                 # встроенная чистая задача celery
PROBE_QUEUE = "default"                          # слушает pid_worker (-Q default,gpu,sam2)
TASK_WAIT_S = 30                                 # событие долетает за ~1 с, запас на CI

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


def check(broker: str, flower_url: str) -> int:
    os.environ["CELERY_BROKER_URL"] = broker      # env сильнее аргумента (замер 1.2)
    from celery import Celery

    app = Celery(broker=broker)
    print(f"[зонд] брокер {broker}, Flower {flower_url}")

    # 1. Есть ли вообще стенд: брокер + воркер на очереди зонда.
    live, note = _live_queues(app)
    if live is None:
        print(f"[судить нечем] {note}")
        return NO_VERDICT
    print(f"[зонд] воркеры: {note}; очереди {sorted(live) or '—'}")
    if PROBE_QUEUE not in live:
        print(f"[судить нечем] очередь {PROBE_QUEUE!r} никто не слушает — зонд некуда класть")
        return NO_VERDICT

    # 2. Очереди в Flower. Отсюда и ниже отказ — красный: это и есть предмет гейта.
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

    # 3. Задача. Кладём в боевую очередь и требуем увидеть её именно в Flower.
    task_id = str(uuid.uuid4())
    marker = f"flower_probe:{task_id[:8]}"
    app.send_task(PROBE_TASK, args=[marker], queue=PROBE_QUEUE,
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Зонд Flower (пункт 3.x1)")
    parser.add_argument("--check", action="store_true", help="прогнать проверку целиком")
    parser.add_argument("--broker", default=DEFAULT_BROKER, help=f"по умолчанию {DEFAULT_BROKER}")
    parser.add_argument("--flower-url", default=DEFAULT_FLOWER, help=f"по умолчанию {DEFAULT_FLOWER}")
    args = parser.parse_args()
    if not args.check:
        parser.print_help()
        return NO_VERDICT
    return check(args.broker, args.flower_url.rstrip("/"))


if __name__ == "__main__":
    sys.exit(main())

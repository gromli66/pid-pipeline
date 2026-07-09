# План: вывод логов и прогресса в клиент (pid-pipeline)

Цель — две связанные задачи:
1. **Не лазить на сервер за логами** — оператор видит логи воркеров прямо в клиенте.
2. **Клиент видит, что процесс идёт** — живой прогресс/heartbeat, а не только смену «бусин» стадий.

---

## 1. Что есть сейчас

- **Сервер:** FastAPI (`api`) + Celery (`worker`, `worker_ocr`) + Redis (брокер и result-backend) + Postgres + CVAT.
- **Логи:** `app/core/logging.py` → stdlib logging в **stdout**. Воркеры местами пишут `print("[DETECT] ...")`. Всё уходит только в `docker logs` → отсюда и необходимость SSH.
- **Статус для клиента:** `ProcessingStage` в Postgres (status/timing/`error_message`/`error_traceback`/`metrics_json`).
- **Клиент:** PySide6, `StatusProvider` **опрашивает** `/api/diagrams/{uid}/status` каждые 2 c, есть `get_stages()` и анимированные «бусины» стадий (`ui/widgets/progress_beads.py`).
- **Чего нет:** ни SSE, ни WebSocket, ни `update_state`/progress. Клиент знает «detection: running», но не видит ни строк лога, ни под-прогресса («тайл 45/120»).

**Вывод:** каркас статусов уже есть, не хватает **потока строк лога** и **под-прогресса**, привязанных к `diagram_uid`.

---

## 2. Как это делают в индустрии

**Транспорт (server → client):**

| Вариант | Когда | Для нашей задачи |
|---|---|---|
| **SSE** (Server-Sent Events, `text/event-stream`) | Однонаправленный поток server→client: логи, прогресс, нотификации | **Рекомендуется.** Идёт по уже открытому порту 8000, авто-reconnect с `Last-Event-ID`, встроенный keepalive, проще WebSocket |
| WebSocket | Нужна двусторонняя связь (чат, коллаборация) | Избыточно — нам не нужен канал client→server |
| Long-polling | Фолбэк, где нет SSE/WS | Уже есть polling статуса; оставить как деградацию |

Отраслевой дефолт прямо для логов/прогресса: **SSE**. Классический стек «FastAPI + Celery + Redis» гоняет прогресс так: воркер **публикует** события в Redis (pub/sub + короткий буфер), FastAPI отдаёт их клиенту через **SSE**, на фронте — авто-reconnect и закрытие по `request.is_disconnected()`. Плюс — **структурные логи** (JSON с полями `ts/level/stage/diagram_uid/msg`), которые одинаково годятся и для стрима в клиент, и для будущей централизации (Loki/ELK).

---

## 3. Целевая архитектура

```
worker (Celery task)
   │  logger.info(...)  ← с контекстом diagram_uid + stage
   ▼
RedisLogHandler ──publish──►  Redis  ┌─ pub/sub  logs:{uid}      (live)
                              └─ list logs:hist:{uid} (кап 1000, TTL 48ч)
                                        │
FastAPI  GET /diagrams/{uid}/logs/stream (SSE) ── подписка + реплей истории
         GET /diagrams/{uid}/logs?tail=N        (история, фолбэк/polling)
                                        │
PySide6 клиент: QThread-consumer ─► сигнал ─► Log-console (dock) + прогресс/heartbeat
```

Ключевая идея: **один логгер-хендлер в воркере** тегирует записи `diagram_uid`/`stage` и публикует их в Redis. Тогда в клиент попадают даже логи из глубоких модулей (`yolo_detector`, `ocr`, …) **без правки самих модулей**.

**Почему Redis, а не новая таблица в Postgres:** операционные логи эфемерны, объём большой, а pub/sub бесплатно раздаёт их нескольким клиентам сразу. Важное о падениях уже персистится в `ProcessingStage` (`error_message`/`traceback`). Если нужен долгий аудит — добавить таблицу `stage_logs` опционально (см. §7).

---

## 4. Серверная часть — изменения

**Новые файлы**
- `app/core/log_stream.py` — `RedisLogHandler` (publish + `rpush`/`ltrim`/`expire`).
- `worker/utils/log_context.py` — `contextvars` + `logging.Filter`, функция `bind(diagram_uid=..., stage=...)`.
- `app/api/logs.py` — эндпойнты истории и SSE-стрима.

**Правки**
- `worker/celery_app.py` — на старте воркер-процесса (сигнал `worker_process_init`) навесить `RedisLogHandler` + `ContextFilter` на root-логгер.
- `worker/tasks/*.py` — в начале задачи `bind(diagram_uid=..., stage="detection")`; заменить `print(...)` на `logger.*` (можно постепенно).
- `app/main.py` — `app.include_router(logs.router)`.
- `requirements/api.txt` — `sse-starlette`; клиент — `httpx-sse` (или ручной парсинг).

**Хендлер (набросок):**
```python
# app/core/log_stream.py
import json, logging
from redis import Redis
from app.config import settings

_redis = Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)
def _chan(uid): return f"logs:{uid}"
def _hist(uid): return f"logs:hist:{uid}"

class RedisLogHandler(logging.Handler):
    def emit(self, record):
        uid = getattr(record, "diagram_uid", None)
        if not uid:
            return
        entry = json.dumps({
            "ts": record.created,
            "level": record.levelname,
            "stage": getattr(record, "stage", None),
            "logger": record.name,
            "msg": record.getMessage(),
        })
        p = _redis.pipeline()
        p.publish(_chan(uid), entry)
        p.rpush(_hist(uid), entry); p.ltrim(_hist(uid), -1000, -1)
        p.expire(_hist(uid), 172800)          # 48 ч
        p.execute()
```

**Контекст (минимум правок в задачах):**
```python
# worker/utils/log_context.py
import contextvars, logging
_ctx = contextvars.ContextVar("logctx", default={})
def bind(**kw): _ctx.set({**_ctx.get(), **kw})
class ContextFilter(logging.Filter):
    def filter(self, record):
        for k, v in _ctx.get().items(): setattr(record, k, v)
        return True
```

**SSE-эндпойнт (реплей истории → live, с keepalive):**
```python
# app/api/logs.py
import json
from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse
from redis.asyncio import Redis
from app.config import settings

router = APIRouter(prefix="/api/diagrams", tags=["logs"])

@router.get("/{uid}/logs")
async def get_logs(uid: str, tail: int = 200):
    r = Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)
    items = await r.lrange(f"logs:hist:{uid}", -tail, -1); await r.aclose()
    return {"logs": [json.loads(i) for i in items]}

@router.get("/{uid}/logs/stream")
async def stream_logs(uid: str, request: Request):
    r = Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)
    async def gen():
        for i in await r.lrange(f"logs:hist:{uid}", -200, -1):
            yield {"event": "log", "data": i}
        ps = r.pubsub(); await ps.subscribe(f"logs:{uid}")
        try:
            while not await request.is_disconnected():
                m = await ps.get_message(ignore_subscribe_messages=True, timeout=15)
                yield {"event": "log", "data": m["data"]} if m else {"event": "ping", "data": "{}"}
        finally:
            await ps.unsubscribe(f"logs:{uid}"); await r.aclose()
    return EventSourceResponse(gen())
```

**Под-прогресс (для «живого» индикатора):** там, где есть счётчики (тайлы YOLO, батчи OCR), слать типизированное событие в тот же канал, например `logger.info("progress", extra={"pct": 37, "note": "tile 45/120"})`, либо отдельным `event: progress`. Клиент рисует % и «последняя активность N c назад».

---

## 5. Клиентская часть — изменения

**Новые файлы**
- `ui/services/log_stream_client.py` — `LogStreamWorker(QThread)`: стримит SSE через `httpx.stream`, парсит `event:`/`data:`, шлёт Qt-сигналы; авто-reconnect (2 c) + `Last-Event-ID`.
- `ui/widgets/log_console.py` — dock-панель: read-only `QPlainTextEdit` (цвет по уровню, `maximumBlockCount` как ring-buffer), тулбар: фильтр по уровню, пауза автоскролла, очистить, «сохранить в файл».

**Правки**
- `ui/services/api_client.py` — `get_logs(uid, tail)` (история/фолбэк).
- `ui/windows/main_window.py` — dock снизу; при выборе диаграммы `start(uid)` стрима, при закрытии/смене — `stop()`.
- Связать `event: progress` → существующие «бусины» + новый heartbeat («работает… последняя активность N c назад»).

**Consumer (набросок):**
```python
# ui/services/log_stream_client.py
import json, httpx
from PySide6.QtCore import QThread, Signal

class LogStreamWorker(QThread):
    line = Signal(dict); state = Signal(str)
    def __init__(self, base_url, uid):
        super().__init__(); self._url = f"{base_url}/api/diagrams/{uid}/logs/stream"; self._stop = False
    def run(self):
        while not self._stop:
            try:
                self.state.emit("connected"); ev = "message"
                with httpx.stream("GET", self._url, timeout=None) as r:
                    for raw in r.iter_lines():
                        if self._stop: break
                        if raw.startswith("event:"): ev = raw[6:].strip()
                        elif raw.startswith("data:") and ev == "log":
                            self.line.emit(json.loads(raw[5:].strip()))
            except Exception:
                self.state.emit("reconnecting"); self.msleep(2000)
    def stop(self): self._stop = True; self.wait(3000)
```
> Важно: стрим — **в отдельном потоке** (не в UI и не в существующем `QTimer`-polling), иначе окно подвиснет. Апдейт виджета — только через сигналы.

---

## 6. Фазы внедрения (по возрастанию сложности)

**Фаза 0 — «не лазить на сервер», быстрый результат (~0.5 дня).**
`RedisLogHandler` + `bind()` в воркере, `GET /logs?tail=N`, панель в клиенте, которая тянет логи на существующем 2-секундном polling. Уже закрывает пункт 1 минимальными правками, без нового транспорта.

**Фаза 1 — live-стриминг (~1–2 дня).**
SSE-эндпойнт + `LogStreamWorker` в клиенте. Реплей истории при подключении, авто-reconnect. Логи идут в реальном времени.

**Фаза 2 — прогресс/liveness (~1–2 дня).**
Типизированные `progress`-события (%, под-шаги, heartbeat), привязка к «бусинам» + прогресс-бар + индикатор «последняя активность». Замена `print(...)` на `logger.*` с контекстом.

**Фаза 3 — полировка/ops.**
Фильтр по уровню, «сохранить/скачать лог стадии», подсветка ошибок с переходом, версия/уровень в заголовке. Опционально — JSON-логи для будущей централизации (Loki/Grafana).

---

## 7. Нюансы и риски

- **Несколько клиентов + один сервер:** Redis pub/sub раздаёт одному каналу нескольким SSE-подписчикам — работает из коробки.
- **Объём логов:** `LTRIM` кап (≈1000 строк/диаграмму) + `maximumBlockCount` в UI; не слать `DEBUG` глубоких библиотек (уже приглушены в `logging.py`).
- **Keepalive/подвисания:** SSE-`ping` раз в 15 c + `request.is_disconnected()`, чтобы не копить «мёртвые» генераторы.
- **Потоки в Qt:** только сигналы из QThread; никаких прямых обращений к виджетам.
- **Безопасность:** порт 8000 открыт наружу, CORS `*`, аутентификации у API нет — стрим логов её наследует. Если логи чувствительны, это повод добавить токен/ограничить сеть (отдельная задача).
- **Долгий аудит (опционально):** если логи нужны и после TTL — добавить таблицу `stage_logs(diagram_uid, ts, level, stage, msg)` (append-only) и писать в неё из того же хендлера; миграция alembic по образцу существующих.

---

## 8. Решения, которые стоит принять

1. **Хранилище логов:** Redis-буфер (рекоменд., эфемерно) vs. + таблица `stage_logs` (долгий аудит).
2. **Что стримить:** зеркалить весь лог воркера vs. только курируемые события прогресса + WARNING/ERROR (чище для оператора).
3. **Глубина реплея** при подключении (по умолчанию 200 строк).

Дефолт: Redis-буфер + зеркалирование INFO и выше, реплей 200. Меняется тривиально.

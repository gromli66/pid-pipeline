# TROUBLESHOOTING.md

**Аудитория:** DEV / OPS
**Версия:** 1.2
**Обновлено:** 2026-08-18
**Связанные документы:** DEPLOYMENT.md, DEV_SETUP.md, CONFIG_REFERENCE.md

---

## Оглавление

1. [cp1251 / кодировка на Windows](#1-cp1251--кодировка-на-windows)
2. [GPU OOM](#2-gpu-oom)
3. [Worker зависает](#3-worker-зависает)
4. [Артефакт не найден](#4-артефакт-не-найден)
5. [Статус не переключается](#5-статус-не-переключается)
6. [UI не видит backend](#6-ui-не-видит-backend)
7. [Autosave fails](#7-autosave-fails)
8. [PySide6 RuntimeWarning](#8-pyside6-runtimewarning)
9. [Alembic миграция: COMMIT перед ALTER TYPE](#9-alembic-миграция-commit-перед-alter-type)
10. [CVAT: task не создаётся](#10-cvat-task-не-создаётся)
11. [Surya / PaddleOCR ошибки](#11-surya--paddleocr-ошибки)
12. [SAM2 контуры: low confidence](#12-sam2-контуры-low-confidence)
13. [CVAT: мигание / медленный рендеринг](#13-cvat-мигание--медленный-рендеринг)
14. [Клиент завис или закрылся — где лог](#14-клиент-завис-или-закрылся--где-лог)

---

## 1. cp1251 / кодировка на Windows

**Симптом:** `UnicodeDecodeError: 'charmap' codec can't decode byte` или `UnicodeEncodeError` при работе с файлами, содержащими кириллицу в путях или содержимом.

**Причина:** Windows default encoding — cp1251/cp1252, Python ожидает UTF-8.

**Решение:**

Установить переменную окружения:

```bash
set PYTHONIOENCODING=utf-8
```

Все файловые операции в проекте используют `encoding="utf-8"` явно. При добавлении нового кода — всегда указывать encoding:

```python
with open(path, "r", encoding="utf-8") as f:
    data = f.read()
```

Все идентификаторы в коде — ASCII-only (исторически из-за этой проблемы). Кириллица только в строковых литералах (comments, docstrings, UI labels).

---

## 2. GPU OOM

**Симптом:** `CUDA out of memory`, `RuntimeError: CUDA error: out of memory`.

**Причины и решения:**

**Слишком большой batch_size.** Уменьшить в project YAML:

```yaml
segmentation:
  batch_size: 2  # было 4
junction_seg:
  batch_size: 4  # было 8
```

**Два worker'а конкурируют за GPU.** Установить `concurrency=1` для обоих:

```bash
# В Dockerfile.worker CMD:
--concurrency=1
```

Или разделить GPU (см. DEPLOYMENT.md §8):

```yaml
worker:
  environment:
    - NVIDIA_VISIBLE_DEVICES=0
worker_ocr:
  environment:
    - NVIDIA_VISIBLE_DEVICES=1
```

**Утечка памяти между задачами.** Добавить очистку:

```python
import torch
torch.cuda.empty_cache()
import gc
gc.collect()
```

**Surya OCR на 8 GB VRAM.** Уменьшить batch size в env:

```yaml
worker_ocr:
  environment:
    - RECOGNITION_BATCH_SIZE=32   # было 64
    - DETECTOR_BATCH_SIZE=2       # было 4
```

---

## 3. Worker зависает

**Симптом:** задача в статусе `STARTED` бесконечно, worker не берёт новые задачи.

**Причины и решения:**

**Задача превысила timeout.** Celery `CELERY_TASK_TIME_LIMIT=3600` (1 час). Для крупных схем может быть мало. Увеличить в `.env` или docker-compose.

**soft_time_limit не настроен.** Без soft limit задача убивается жёстко (SIGKILL) без cleanup. Рекомендуется добавить `soft_time_limit` в задачи:

```python
@celery_app.task(soft_time_limit=3000, time_limit=3600)
def run_detection(...):
    ...
```

**acks_late + visibility_timeout.** Если worker умирает до ACK, задача возвращается в очередь. По умолчанию `visibility_timeout=3600` в Redis backend. Если задача дольше — Redis считает её потерянной и ре-доставляет.

**Диагностика:**

```bash
# Активные задачи
docker exec pid_worker celery -A worker.celery_app inspect active

# Зарезервированные
docker exec pid_worker celery -A worker.celery_app inspect reserved

# Purge очереди (экстренная мера)
docker exec pid_worker celery -A worker.celery_app purge
```

---

## 4. Артефакт не найден

**Симптом:** `FileNotFoundError` при чтении артефакта, или downstream-этап не находит входные данные.

**Причины и решения:**

**Неправильный путь.** Все артефакты хранятся в `STORAGE_PATH/{diagram_uid}/{stage}/`. Проверить:

```bash
ls -la storage/diagrams/{uid}/detection/
ls -la storage/diagrams/{uid}/segmentation/
```

**Артефакт не создан предыдущим этапом.** Проверить статус диаграммы:

```sql
SELECT status, error_message, error_stage FROM diagrams WHERE uid = '{uid}';
```

**Docker volume mismatch.** Worker и API должны видеть одинаковый `STORAGE_PATH`. Проверить bind mount в docker-compose:

```yaml
volumes:
  - ./storage:/storage  # одинаковый для api, worker, worker_ocr
```

---

## 5. Статус не переключается

**Симптом:** диаграмма "застряла" в статусе, следующий этап не запускается.

**Причины и решения:**

**Idempotency check.** Каждый worker-task проверяет текущий статус перед обработкой. Если статус неожиданный — задача скипается. Проверить:

```sql
SELECT status, error_message, error_stage FROM diagrams WHERE uid = '{uid}';
```

**error_stage не сброшен.** После ошибки диаграмма в `error` с `error_stage`. Сбросить вручную:

```sql
UPDATE diagrams 
SET status = 'detected', error_message = NULL, error_stage = NULL 
WHERE uid = '{uid}';
```

Или через reset.py (отредактировать UUID и target status).

**safe_dispatch не вызван.** Проверить логи worker'а — `safe_dispatch` логирует отправку следующей задачи. Если dispatch не произошёл — ошибка в коде задачи (exception до dispatch).

---

## 6. UI не видит backend

**Симптом:** UI показывает "Connection error", "timeout", или пустой список диаграмм.

**Причины и решения:**

**API не запущен.** Проверить:

```bash
curl http://localhost:8000/health
docker-compose ps api
```

**Неправильный порт.** UI по умолчанию подключается к `http://localhost:8000`. Если API на другом порту — проверить `API_PORT` в `.env`.

**CORS.** FastAPI defaults не ограничивают CORS при локальной разработке. Если UI работает на другом хосте — может потребоваться middleware:

```python
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"], ...)
```

**Docker network isolation.** UI на хосте обращается к `localhost:8000`. Порт API должен быть проброшен в docker-compose (`ports: - "8000:8000"`).

---

## 7. Autosave fails

**Симптом:** autosave не срабатывает или сохраняет не все данные.

**Причина:** `_SAVE_METHODS` маппинг в `ui/services/autosave.py` не содержит метод save для текущего типа вкладки.

**Решение:** при добавлении нового типа вкладки с редактированием — добавить соответствующий метод в `_SAVE_METHODS` dict. Каждая вкладка с `has_unsaved_changes() == True` должна иметь зарегистрированный save-метод.

Параметры autosave настраиваются через `UISettings`: `autosave/enabled` (default true), `autosave/interval_sec` (default 120). См. CONFIG_REFERENCE.md §5.

---

## 8. PySide6 RuntimeWarning

**Симптом:** `RuntimeWarning: Failed to disconnect signal` при закрытии вкладки или переключении режима.

**Причина:** попытка `disconnect()` сигнала, который не был подключён или уже отключён.

**Решение:** оборачивать disconnect в try/except:

```python
try:
    self.signal.disconnect(self.slot)
except (RuntimeError, TypeError):
    pass
```

Или проверять подключение через `isSignalConnected()` (PySide6 не всегда поддерживает). Ворнинг безвреден — не влияет на функциональность.

---

## 9. Alembic миграция: COMMIT перед ALTER TYPE

**Симптом:** `InternalError: ALTER TYPE ... cannot run inside a transaction block` при миграции PostgreSQL с изменением ENUM.

**Причина:** PostgreSQL не позволяет `ALTER TYPE ... ADD VALUE` внутри транзакции.

**Решение:** в файле миграции добавить `COMMIT` перед ALTER:

```python
def upgrade():
    # Завершить текущую транзакцию
    op.execute("COMMIT")
    # Теперь можно менять ENUM
    op.execute("ALTER TYPE diagramstatus ADD VALUE 'new_status'")
    # Начать новую транзакцию для остальных операций
    op.execute("BEGIN")
```

Если Alembic version рассинхронизировался — использовать `fix_stamp.py` для ручной установки версии.

---

## 10. CVAT: task не создаётся

**Симптом:** `POST /cvat/{uid}/create-task` возвращает 500 или timeout.

**Причины и решения:**

**CVAT недоступен.** Проверить:

```bash
curl http://localhost:8080/api/server/about
docker-compose ps cvat_server
```

**Неправильный токен.** `CVAT_TOKEN` в docker-compose должен совпадать с реальным токеном CVAT-пользователя. Получить новый:

```bash
docker exec -it cvat_server bash -ic 'python3 ~/manage.py drf_create_token admin'
```

**Изображение слишком большое.** CVAT timeout при загрузке крупных изображений (>50 MB). `CVATClient.timeout` по умолчанию 120 секунд, для upload — `timeout * 2`. Увеличить при необходимости. Создание task на больших схемах ускоряет `use_cache=true` (ленивые чанки) в `CVATClient.create_task()`; см. CVAT.md §5.

**Job не создаётся.** `_wait_for_job()` ждёт до 30 секунд. Если CVAT worker_chunks перегружен — увеличить `max_attempts`.

---

## 11. Surya / PaddleOCR ошибки

**Симптом:** OCR worker падает при инициализации или на первом изображении.

**Причины и решения:**

**Модели не скачаны.** При первом запуске Surya скачивает модели в `HF_HOME`. Если нет доступа к интернету из контейнера — предварительно скачать на хосте и смонтировать:

```yaml
volumes:
  - ./models/ocr/datalab_cache:/root/.cache/datalab
  - ./models/ocr/hf_cache:/models/ocr/hf_cache
```

**Конфликт torch версий.** worker и worker_ocr используют разные версии PyTorch/CUDA (12.4 vs 12.6). Не запускать OCR-задачи в основном worker'е.

**transformers версия.** Surya 0.17.1 требует `transformers>=4.46.0,<4.52.0`. Конфликт с другими зависимостями — причина отдельного Dockerfile для OCR.

**PaddleOCR CPU.** `Dockerfile.paddle` использует PaddlePaddle CPU. Если PaddleOCR не стартует — проверить, что модель `PP-OCRv5_server_rec` скачана при сборке образа.

---

## 12. SAM2 контуры: low confidence

**Симптом:** большинство контуров получают `status: "manual_review"` вместо `"auto"`.

**Причины и решения:**

**Низкий порог.** `contour_extraction.confidence_threshold` в project YAML (default 0.85). Понижение до 0.80 увеличит auto-accept, но может снизить качество.

**Snap-параметры слишком агрессивны.** Если `snap_dp_eps` слишком велик — полигоны упрощаются чрезмерно и теряют форму:

```yaml
contour_extraction:
  snap_dp_eps: 0.10    # было 0.15 — менее агрессивное упрощение
  snap_threshold: 0.06  # было 0.08
```

**skip_classes неполный.** Убедиться, что все не-оборудованные классы в `skip_classes`:

```yaml
skip_classes:
  - background
  - truba
  - annotation
  - strelka
  - connector
```

**Визуальная диагностика:** `python visualize_contours.py` — зелёные (auto), оранжевые (review), красные (empty). См. SCRIPTS.md §2.

---

## 13. CVAT: мигание / медленный рендеринг

**Симптом:** встроенный CVAT (`QWebEngineView` в `CvatTab`) мигает / даёт чёрные вспышки при перерисовке, либо тормозит pan/zoom на схемах.

**Причина:** на Windows дефолтный GPU-бэкенд QtWebEngine — ANGLE/D3D11, его swap-chain даёт мигание. Полное отключение GPU-композитинга убирает мигание, но переводит композитинг на CPU → тормоза.

**Решение:** в `ui/main.py` (до создания `QApplication`) выставлены `AA_ShareOpenGLContexts` + нативный desktop-OpenGL-бэкенд — мигание уходит, аппаратное ускорение сохраняется. Бэкенд переопределяется переменной окружения `PID_GL_BACKEND` без правки кода:

```bash
set PID_GL_BACKEND=desktop    # по умолчанию: нативный OpenGL — быстро и без мигания (NVIDIA/AMD)
set PID_GL_BACKEND=gles       # ANGLE — если desktop GL артефачит; аппаратное ускорение сохраняется
set PID_GL_BACKEND=software   # софт-рендер — универсально, но медленно (крайний случай)
set PID_GL_BACKEND=auto       # дефолт ОС (на Windows вернёт мигание)
python -m ui.main
```

Лесенка подбора: `desktop` → если мигает `gles` → если всё ещё мигает `software`. `AA_ShareOpenGLContexts` ставится всегда и безопасен на всех ОС. Десктоп-UI не в Docker — настройка задаётся на каждой машине индивидуально, на универсальность серверного стека не влияет.

---

## 14. Клиент завис или закрылся — где лог

**Симптом:** окно клиента замерло, кнопка перестала отвечать или программа закрылась сама. На экране — ничего.

**Причина:** необработанное исключение в слоте Qt. Оно не роняет процесс: PySide6 отдаёт его в `sys.excepthook`, а UI остаётся в полусостоянии.

**Где смотреть:**

```
%LOCALAPPDATA%\PID-Client\logs\client.log        # Windows
~/.local/state/pid-client/logs/client.log          # Linux
```

Первая строка каждого запуска называет путь файла — если сомневаетесь, что читаете нужный. Падение пишется уровнем `CRITICAL` с полной трассировкой; в строке есть `uid=` открытой диаграммы — по нему лог клиента сшивается с логом воркера на сервере.

**Настройки** (переменные окружения; для собранного клиента — строки `KEY=VALUE` в `client.cfg` рядом с `.exe`):

| Переменная | Что делает | Default |
|---|---|---|
| `PID_LOG_DIR` | каталог лога | `%LOCALAPPDATA%\PID-Client\logs` |
| `PID_LOG_LEVEL` | уровень (`DEBUG` для разбора жалобы) | `INFO` |

Ротация: 5 МБ × 5 файлов (`client.log.1` … `client.log.5`), то есть история переживает несколько сеансов.

**Каталог недоступен** (нет прав, отвалился сетевой профиль): клиент стартует без файла, в консоли — предупреждение «Лог в файл недоступен». В собранном `.exe` консоли нет (`console=False`), поэтому проверять надо задав `PID_LOG_DIR` в записываемый каталог.

Устройство приёмника — `UI_GUIDE.md §11.5`. Логи **сервера**, показанные в клиенте, — отдельная тема (`docs/observability/PLAN_client_logs.md`).

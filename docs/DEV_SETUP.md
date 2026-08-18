# DEV_SETUP.md

**Аудитория:** DEV
**Версия:** 1.1
**Обновлено:** 2026-04-09
**Связанные документы:** DEPLOYMENT.md, CONFIG_REFERENCE.md, CODING_GUIDE.md

---

## Оглавление

1. [Архитектура dev-среды](#1-архитектура-dev-среды)
2. [Требования](#2-требования)
3. [Backend в Docker](#3-backend-в-docker)
4. [Локальная среда Python](#4-локальная-среда-python)
5. [Запуск UI](#5-запуск-ui)
6. [Запуск и отладка worker'ов](#6-запуск-и-отладка-workerов)
7. [Миграции БД](#7-миграции-бд)
8. [IDE setup](#8-ide-setup)
9. [Типичные проблемы](#9-типичные-проблемы)
10. [Прогон конвейера end-to-end локально](#10-прогон-конвейера-end-to-end-локально)

---

## 1. Архитектура dev-среды

Рекомендуемая конфигурация: инфраструктура (PostgreSQL, Redis, CVAT) в Docker, код (UI, worker, API) — локально. Это даёт быструю итерацию без пересборки образов.

```
┌─────────────────────────────────────────────┐
│  Docker                                     │
│  ┌──────────┐  ┌───────┐  ┌──────────────┐ │
│  │ postgres │  │ redis │  │ CVAT (full)  │ │
│  │ :5433    │  │ :6380 │  │ :8080        │ │
│  └──────────┘  └───────┘  └──────────────┘ │
└─────────────────────────────────────────────┘
         ▲            ▲            ▲
         │            │            │
┌────────┴────────────┴────────────┴──────────┐
│  Локально (venv Python 3.11)                │
│  ┌─────┐  ┌────────┐  ┌────────────┐       │
│  │ UI  │  │ worker │  │ API (opt.) │       │
│  └─────┘  └────────┘  └────────────┘       │
└─────────────────────────────────────────────┘
```

UI подключается к API (Docker или локальный) по `http://localhost:8000`. Worker'ы подключаются к Redis `localhost:6380` и PostgreSQL `localhost:5433`.

---

## 2. Требования

| Компонент | Версия | Примечание |
|-----------|--------|-----------|
| Python | 3.11.x | Строго 3.11 (совместимость с torch, PySide6) |
| Docker + Compose | 24.0+ / v2.20+ | Для инфраструктуры |
| NVIDIA Driver | 535+ | Для GPU-задач worker'а |
| CUDA Toolkit | 12.4+ | Для локального PyTorch |
| Git | 2.30+ | — |
| OS | Windows 10/11, Ubuntu 22.04+ | Windows — основная dev-платформа для UI |

---

## 3. Backend в Docker

Для разработки достаточно поднять инфраструктуру без P&ID API/worker'ов:

```bash
# Поднять только инфраструктуру
docker-compose up -d postgres redis

# Если нужен CVAT
docker-compose up -d postgres redis cvat_db cvat_redis_inmem cvat_redis_ondisk \
  cvat_server cvat_worker_import cvat_worker_export cvat_worker_annotation \
  cvat_worker_chunks cvat_ui cvat_opa traefik
```

Или поднять всё и остановить то, что запускаете локально:

```bash
docker-compose up -d
docker-compose stop worker worker_ocr  # запускаем локально
```

Проверка:

```bash
# PostgreSQL
psql -h localhost -p 5433 -U pid_user -d pid_pipeline

# Redis
redis-cli -p 6380 ping
```

---

## 4. Локальная среда Python

### 4.1 Создание virtual environment

```bash
# Windows
python -m venv .venv311
.venv311\Scripts\activate

# Linux/Mac
python3.11 -m venv .venv311
source .venv311/bin/activate
```

### 4.2 Установка зависимостей

Файлы requirements в `requirements/`:

| Файл | Содержимое | Когда нужен |
|------|-----------|-------------|
| `base.txt` | SQLAlchemy, Pydantic, httpx, Pillow, alembic | Всегда |
| `api.txt` | FastAPI, uvicorn, celery (включает `base.txt` через `-r base.txt`) | Для локального API |
| `worker.txt` | Celery, ultralytics, sahi, smp, albumentations, OpenCV, SAM2 (включает `base.txt` через `-r base.txt`) | Для локального worker'а |
| `ui.txt` | PySide6, httpx, OpenCV, qasync, numpy | Для UI |
| `dev.txt` | pytest, ruff, mypy | Для тестов и линтинга — **но одного его мало, см. ниже** |

Файлы `api.txt` и `worker.txt` используют директиву pip `-r base.txt` для включения базовых зависимостей. При установке `pip install -r requirements/worker.txt` зависимости из `base.txt` устанавливаются автоматически. Файлы `ui.txt` и `dev.txt` — самостоятельные, без `-r` включения.

```bash
# Минимум для UI-разработки
pip install -r requirements/ui.txt
pip install -r requirements/dev.txt

# Для worker-разработки (GPU)
pip install -r requirements/worker.txt
pip install -r requirements/dev.txt

# Для прогона тестов — нужен и api.txt (пункт 0.0, 2026-08-14)
pip install -r requirements/api.txt -r requirements/ui.txt -r requirements/dev.txt
```

⛔ **Ни один из двух первых сценариев не ставит `fastapi`**, а `tests/observability/` его
импортирует — по документу сбор тестов падал с `ModuleNotFoundError: fastapi` и прогон
прерывался целиком. Для тестов ставится **`api.txt` + `ui.txt` + `dev.txt`** (`api.txt`
подтягивает `base.txt`). Проверка среды:

```bash
python -c "import fastapi, pytest, PySide6"
```

`worker.txt` для сбора тестов не нужен: задачи воркера импортируются внутри тестов, не на
уровне модуля.

### 4.3 PyTorch на Windows

PyTorch не указан в requirements (устанавливается отдельно из-за platform-specific индексов):

```bash
# Windows + CUDA 12.4
pip install torch==2.6.0+cu124 torchvision==0.21.0+cu124 --index-url https://download.pytorch.org/whl/cu124
```

### 4.4 PySide6 на Windows

PySide6 из `ui.txt` устанавливается через pip без дополнительных шагов на Windows. На Linux может потребоваться установка WebEngine отдельно:

```bash
# Linux — раскомментировать в ui.txt:
pip install PySide6-WebEngine>=6.6.0
```

### 4.5 .env для локальной разработки

Создать `.env` в корне проекта с параметрами для подключения к Docker-инфраструктуре:

```bash
DATABASE_URL=postgresql://pid_user:changeme@localhost:5433/pid_pipeline
CELERY_BROKER_URL=redis://localhost:6380/0
CELERY_RESULT_BACKEND=redis://localhost:6380/0
CVAT_URL=http://localhost:8080
STORAGE_PATH=./storage/diagrams
PROJECTS_CONFIG_DIR=./configs/projects
LOG_LEVEL=DEBUG
```

Порты `5433` (PostgreSQL) и `6380` (Redis) — порты хоста из docker-compose, смещённые от стандартных чтобы не конфликтовать с локальными инстансами.

---

## 5. Запуск UI

```bash
# Из корня проекта, с активированным venv
python -m ui.main
```

UI (PySide6) подключается к API по `http://localhost:8000`. Если API запущен в Docker — убедиться, что контейнер `pid_api` поднят. Если API запущен локально:

```bash
# В отдельном терминале
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Настройки UI (autosave, интервалы) хранятся в `QSettings` — реестр Windows (`HKCU\Software\PID\P&ID Pipeline`) или `~/.config/PID/` на Linux. См. CONFIG_REFERENCE.md §5.

`StatusProvider` опрашивает API каждые 2 секунды (`poll_interval_ms=2000`). При отладке UI этот интервал может влиять на задержку отображения статусов — при необходимости настраивается в конструкторе `StatusProvider`.

---

## 6. Запуск и отладка worker'ов

### 6.1 Локальный запуск worker'а

```bash
# Основной worker (detection, segmentation, skeleton, junction, contour)
celery -A worker.celery_app worker --loglevel=debug --concurrency=1 --pool=prefork -Q default,gpu,sam2

# OCR worker
celery -A worker.celery_app worker --loglevel=debug --concurrency=1 --pool=prefork -Q ocr
```

`--concurrency=1` рекомендуется для отладки — одна задача за раз, предсказуемое использование VRAM.

### 6.2 Отладка конкретной задачи

Для отладки без Celery — вызвать задачу напрямую:

```python
# debug_task.py
import os
os.environ["DATABASE_URL"] = "postgresql://pid_user:changeme@localhost:5433/pid_pipeline"
os.environ["STORAGE_PATH"] = "./storage/diagrams"
os.environ["PROJECTS_CONFIG_DIR"] = "./configs/projects"

from worker.tasks.detection import run_detection
result = run_detection(diagram_id=1, project_code="thermohydraulics")
print(result)
```

Запускать с `PYTHONPATH=.`:

```bash
PYTHONPATH=. python debug_task.py
```

### 6.3 Отладка в IDE

Для отладки worker-задач в PyCharm/VS Code — запустить debug_task.py с breakpoints. Для отладки Celery worker — настроить launch config с `celery` как module (см. §8).

---

## 7. Миграции БД

### Применить миграции

```bash
# Если API в Docker
docker exec -it pid_api alembic upgrade head

# Если локально
alembic upgrade head
```

### Создать новую миграцию

```bash
# Автогенерация из изменений моделей
alembic revision --autogenerate -m "add_column_xyz"

# Проверить сгенерированный файл
cat alembic/versions/<hash>_add_column_xyz.py

# Применить
alembic upgrade head
```

Alembic использует `DATABASE_URL` из переменной окружения (приоритет) или из `alembic.ini` (fallback: `postgresql://pid_user:changeme@localhost:5433/pid_pipeline`). Модели импортируются из `app.models`: `Diagram`, `Artifact`, `ProcessingStage`, `Project`.

---

## 8. IDE setup

### 8.1 VS Code

Рекомендуемые расширения: Python (ms-python), Pylance, Ruff, YAML, Docker.

`.vscode/settings.json`:

```json
{
    "python.defaultInterpreterPath": "${workspaceFolder}/.venv311/Scripts/python",
    "python.analysis.typeCheckingMode": "basic",
    "python.analysis.extraPaths": ["${workspaceFolder}/modules"],
    "ruff.enable": true,
    "[python]": {
        "editor.defaultFormatter": "charliermarsh.ruff",
        "editor.formatOnSave": true
    }
}
```

`.vscode/launch.json` (отладка worker-задачи):

```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "UI",
            "type": "debugpy",
            "request": "launch",
            "module": "ui.main",
            "env": {"PYTHONPATH": "${workspaceFolder}"}
        },
        {
            "name": "API",
            "type": "debugpy",
            "request": "launch",
            "module": "uvicorn",
            "args": ["app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"],
            "env": {"PYTHONPATH": "${workspaceFolder}"}
        },
        {
            "name": "Debug Task",
            "type": "debugpy",
            "request": "launch",
            "program": "${workspaceFolder}/debug_task.py",
            "env": {"PYTHONPATH": "${workspaceFolder}"}
        }
    ]
}
```

### 8.2 PyCharm

Настройка интерпретатора: File → Settings → Project → Python Interpreter → Add → Existing → `.venv311/Scripts/python.exe`.

Добавить `modules/` в Source Roots: правый клик → Mark Directory as → Sources Root. Это обеспечит корректное разрешение импортов (`from modules.xxx import ...`).

Run Configuration для UI: Script path = `ui/main.py`, Working directory = корень проекта.

---

## 9. Типичные проблемы

### PySide6 не импортируется на Linux

Установить системные зависимости Qt:

```bash
sudo apt-get install libgl1-mesa-glx libegl1 libxkbcommon0 libdbus-1-3
```

### torch.cuda.is_available() == False

Проверить совместимость версий: NVIDIA Driver → CUDA Toolkit → PyTorch. На Windows — убедиться, что установлен PyTorch с `+cu124`, а не CPU-версия.

```bash
python -c "import torch; print(torch.version.cuda, torch.cuda.is_available())"
```

### Порт 5433/6380 занят

Docker-compose использует нестандартные порты (5433 для PostgreSQL, 6380 для Redis) чтобы не конфликтовать с локальными инстансами. Если порты заняты — изменить в `docker-compose.yml`:

```yaml
ports:
  - "5434:5432"  # другой порт
```

И обновить `DATABASE_URL` в `.env`.

### Ошибки encoding (cp1251) на Windows

Все скрипты проекта используют ASCII-only идентификаторы. Если возникают проблемы с кодировкой — установить переменную:

```bash
set PYTHONIOENCODING=utf-8
```

### Import ошибки modules/

Убедиться, что `PYTHONPATH` включает корень проекта и `modules/`:

```bash
# Windows
set PYTHONPATH=.;modules

# Linux
export PYTHONPATH=.:modules
```

Или в IDE — добавить `modules/` как Source Root.

### Celery worker не видит задачи

Проверить подключение к Redis и название очередей:

```bash
# Проверить Redis
redis-cli -p 6380 ping

# Проверить регистрацию задач
celery -A worker.celery_app inspect registered
```

Убедиться, что worker запущен с правильными очередями: `-Q default,gpu,sam2` для основного, `-Q ocr` для OCR.

---

## 10. Прогон конвейера end-to-end локально

Стенд `tools/e2e_local.py` (пункт 0.12 дороги рефакторинга) проводит ОДНУ
диаграмму через все этапы без клиента: он дёргает те же REST-эндпоинты, что и
`ui/services/api_client.py`, а ручные этапы закрывает авто-приёмкой, которая в
API уже есть (`STATUS_MACHINE.md §6`). Правок оператора при этом ноль — прогон
измеряет машину.

Нужен полный P&ID-стек (это **5 сервисов из 15** в `docker-compose.yml`,
остальные — CVAT; CVAT нужен: этап приёмки bbox идёт через него):

```bash
docker compose up -d postgres redis api worker worker_ocr cvat_db \
  cvat_redis_inmem cvat_redis_ondisk cvat_server cvat_opa cvat_ui traefik
```

```bash
# Готов ли стек: /health, проект, очереди воркеров, картинка. exit 1 — не готов
python -X utf8 tools/e2e_local.py --check

# Полный прогон: загрузить картинку и провести до completed
python -X utf8 tools/e2e_local.py --run --report e2e.json

# Продолжить прогон после таймаута/обрыва (в том числе с промежуточного статуса)
python -X utf8 tools/e2e_local.py --resume <uid>
```

Прогон создаёт **новую** диаграмму в локальной БД и каталог в
`storage/diagrams/` — это данные разработчика, в git не попадает ничего.

### Цепочка, по которой идёт стенд

| шаг | что дёргает | ждёт статус |
|---|---|---|
| `frame` | `POST /api/frame/{uid}/skip` | `frame_cleaned` |
| `detection` | `POST /api/detection/{uid}/detect` | `detected` |
| `cvat_open` | `create-task` + `open-validation` | `validating_bbox` |
| `cvat_fetch` | `POST /api/cvat/{uid}/fetch-annotations` | `validated_bbox` |
| `segmentation` | `POST /api/segmentation/{uid}/segment` | `skeletonized` |
| `masks_start` / `masks_done` | `masks/start`, `masks/complete` | `detected_junctions` |
| `junctions_start` / `junctions_done` | `junctions/start`, `junctions/complete` | `built` |
| `graph_start` / `graph_done` | `graph/start`, `graph/save`, `graph/complete-simple` | `validated_graph` |
| `contours` | `contours/extract` → ждёт `has_auto` → `contours/complete` | `contours_validated` |
| `ocr_bind` | ждёт `has_ocr_result` → `binding/save`, `binding/apply` | `ocr_bound` |
| `fxml` | `POST /api/validation/{uid}/graph/complete` | `completed` |

⚠ Цепочка снята с кода 2026-08-18 и в трёх местах расходится с
`STATUS_MACHINE.md`/`API.md` (там она описана по состоянию на 04-09):

- детекция стартует с `frame_cleaned`, а не с `uploaded` (`app/api/detection.py:40`);
- SAM2-контуры веером после перекрёстков **не запускаются**
  (`app/api/validation.py:666-669`) — их считает вкладка контуров по требованию;
- `complete-simple` больше не копирует `graph.json` в validated молча
  (`app/api/validation.py:728`) — граф обязан быть сохранён явно.

Числа прогона (13 стадий, состав артефактов, время) — `MEASUREMENTS.md §34`.

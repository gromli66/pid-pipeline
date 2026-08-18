# DEPLOYMENT.md

**Аудитория:** OPS / DEV
**Версия:** 1.2
**Обновлено:** 2026-06-17
**Связанные документы:** ARCHITECTURE.md, CONFIG_REFERENCE.md, DEV_SETUP.md

---

## Оглавление

1. [Требования](#1-требования)
2. [Docker Compose — обзор сервисов](#2-docker-compose--обзор-сервисов)
3. [Первый запуск](#3-первый-запуск)
4. [Сервисы P&ID Pipeline](#4-сервисы-pid-pipeline)
5. [Сервисы CVAT](#5-сервисы-cvat)
6. [Volumes и хранилище](#6-volumes-и-хранилище)
7. [Сети](#7-сети)
8. [GPU](#8-gpu)
9. [Обновление](#9-обновление)
10. [Мониторинг](#10-мониторинг)
11. [Backup и восстановление](#11-backup-и-восстановление)
12. [Security](#12-security)
13. [Performance tuning](#13-performance-tuning)

---

## 1. Требования

**Хост-система:**

| Компонент | Минимум | Рекомендация |
|-----------|---------|-------------|
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04+ / Windows WSL2 |
| Docker | 24.0+ | Последний stable |
| Docker Compose | v2.20+ (plugin) | Последний stable |
| NVIDIA Driver | 535+ | 550+ |
| nvidia-container-toolkit | 1.14+ | Последний stable |
| CUDA (на хосте) | Не требуется (внутри контейнеров) | — |
| RAM | 16 GB | 32 GB |
| GPU VRAM | 8 GB | 12+ GB |
| Диск | 50 GB свободных | SSD, 100+ GB |

**Проверка GPU перед запуском:**

```bash
# Драйвер установлен?
nvidia-smi

# nvidia-container-toolkit работает?
docker run --rm --gpus all nvidia/cuda:12.4.0-runtime-ubuntu22.04 nvidia-smi
```

---

## 2. Docker Compose — обзор сервисов

Файл: `docker-compose.yml`. Содержит две группы сервисов: P&ID Pipeline и CVAT.

| Сервис | Image / Build | Порт (хост) | GPU | Сеть |
|--------|--------------|-------------|-----|------|
| `postgres` | postgres:15-alpine | 5433 | — | pid_network |
| `redis` | redis:7-alpine | 6380 | — | pid_network |
| `api` | Dockerfile.api | 8000 | — | pid_network, cvat |
| `worker` | Dockerfile.worker | — | ✅ 1 GPU | pid_network, cvat |
| `worker_ocr` | Dockerfile.worker_ocr | — | ✅ 1 GPU | pid_network |
| `flower` | mher/flower:2.0 | 5555 (**только 127.0.0.1**) | — | pid_network |
| `cvat_db` | postgres:15-alpine | — | — | cvat |
| `cvat_redis_inmem` | redis:7.2-alpine | — | — | cvat |
| `cvat_redis_ondisk` | apache/kvrocks:latest | — | — | cvat |
| `cvat_server` | cvat/server:v2.25.0 | — | — | cvat |
| `cvat_worker_import` | cvat/server:v2.25.0 | — | — | cvat |
| `cvat_worker_export` | cvat/server:v2.25.0 | — | — | cvat |
| `cvat_worker_chunks` | cvat/server:v2.25.0 | — | — | cvat |
| `cvat_ui` | cvat/ui:v2.25.0 | — | — | cvat |
| `cvat_opa` | openpolicyagent/opa:0.63.0 | — | — | cvat |
| `traefik` | traefik:v2.11 | 8080 | — | cvat |

Точки доступа: P&ID API — `http://localhost:8000/docs`, CVAT UI — `http://localhost:8080`.

---

## 3. Первый запуск

### 3.1 Клонирование и настройка .env

```bash
git clone <repo_url> pid-pipeline
cd pid-pipeline
cp .env.example .env
```

Отредактировать `.env` — как минимум изменить пароли:

```bash
DB_PASSWORD=<strong_password>
CVAT_SUPERUSER_PASSWORD=<strong_password>
```

Полный список переменных — см. CONFIG_REFERENCE.md §2.

### 3.2 Размещение моделей

ML-модели не входят в репозиторий. Разместить вручную:

```
models/
├── yolo/
│   ├── best.pt              # YOLOv8m (основная модель)
│   └── best_pakh.pt         # YOLOv8m (Пакш)
├── segmentation/
│   ├── best.pth             # UNet++ Model A
│   └── best_dual.pth        # UNet++ Model B (DualHead)
├── junction_cnn/
│   └── best.pth             # CenterNet junction/bridge
├── sam2/
│   ├── sam2_hiera_small.pt  # SAM2 базовые веса
│   └── sam2_pid_best.pth    # SAM2 fine-tuned чекпоинт
└── ocr/
    ├── datalab_cache/        # Surya модели (скачиваются при первом запуске)
    └── hf_cache/             # HuggingFace cache
```

Пути к весам настраиваются в project YAML (см. CONFIG_REFERENCE.md §3).

### 3.3 Сборка и запуск

```bash
# Сборка образов
docker-compose build

# Запуск всех сервисов
docker-compose up -d

# Проверка статуса
docker-compose ps
```

### 3.4 Миграции БД

```bash
docker exec -it pid_api alembic upgrade head
```

Alembic берёт `DATABASE_URL` из переменной окружения (env.py переопределяет URL из alembic.ini). Модели: `Diagram`, `Artifact`, `ProcessingStage`, `Project`.

### 3.5 Создание суперпользователя CVAT

```bash
docker exec -it cvat_server bash -ic 'python3 ~/manage.py createsuperuser'
```

### 3.6 Проверка

```bash
# API healthcheck
curl http://localhost:8000/health

# CVAT доступен
curl http://localhost:8080/api/server/about

# Worker'ы подключены к Redis
docker logs pid_worker --tail 20
docker logs pid_worker_ocr --tail 20
```

---

## 4. Сервисы P&ID Pipeline

### 4.1 postgres

PostgreSQL 15 Alpine. Хранит метаданные схем, статусы обработки, артефакты.

| Параметр | Значение |
|----------|---------|
| Container | `pid_postgres` |
| Порт хоста | `5433` → 5432 внутри |
| Volume | `pid_postgres_data` |
| Healthcheck | `pg_isready` каждые 5с |
| Env | `DB_NAME`, `DB_USER`, `DB_PASSWORD` из `.env` |

### 4.2 redis

Redis 7 Alpine с AOF persistence. Celery broker + result backend.

| Параметр | Значение |
|----------|---------|
| Container | `pid_redis` |
| Порт хоста | `6380` → 6379 внутри |
| Volume | `pid_redis_data` |
| Healthcheck | `redis-cli ping` каждые 5с |

### 4.3 api

FastAPI backend. Dockerfile.api на базе `python:3.11-slim`.

| Параметр | Значение |
|----------|---------|
| Container | `pid_api` |
| Порт хоста | `${API_PORT:-8000}` → 8000 внутри |
| Base image | `python:3.11-slim` |
| Requirements | `base.txt` + `api.txt` |
| Healthcheck | `curl http://localhost:8000/health` каждые 30с |
| CMD | `uvicorn app.main:app --host 0.0.0.0 --port 8000` |

Volumes (bind mounts для hot-reload в dev):

| Хост | Контейнер | Режим |
|------|-----------|-------|
| `./app` | `/app/app` | rw |
| `./worker` | `/app/worker` | rw |
| `./configs` | `/app/configs` | rw |
| `./storage` | `/storage` | rw |
| `./alembic` | `/app/alembic` | rw |
| `./alembic.ini` | `/app/alembic.ini` | rw |
| `cvat_data` | `/home/django/data` | ro |

### 4.4 worker

Celery worker для GPU-задач: детекция (YOLO+SAHI), сегментация (UNet++), скелетизация, junction classification (CenterNet), контурная экстракция (SAM2).

| Параметр | Значение |
|----------|---------|
| Container | `pid_worker` |
| Base image | `nvidia/cuda:12.4.0-runtime-ubuntu22.04` |
| Python | 3.11 (deadsnakes PPA) |
| PyTorch | `2.6.0+cu124` |
| Requirements | `base.txt` + `worker.txt` |
| Queues | `default`, `gpu`, `sam2` |
| Concurrency | 2 (prefork) |
| GPU | 1 GPU (nvidia driver) |
| PYTHONPATH | `/app:/app/modules` |

Специфичные env-переменные: `YOLO_WEIGHTS`, `YOLO_DEVICE`, `HF_HOME`.

### 4.5 worker_ocr

Отдельный Celery worker для OCR (Surya 0.17.1). Изолирован от основного worker из-за разных версий PyTorch и конфликтов зависимостей.

| Параметр | Значение |
|----------|---------|
| Container | `pid_worker_ocr` |
| Base image | `nvidia/cuda:12.6.3-runtime-ubuntu22.04` |
| Python | 3.11 (deadsnakes PPA) |
| PyTorch | latest+cu126 |
| Surya | 0.17.1 |
| Queue | `ocr` (только) |
| Concurrency | 1 (одна GPU-задача за раз) |
| GPU | 1 GPU |

Специфичные env-переменные: `HF_HOME`, `RECOGNITION_BATCH_SIZE` (default 64), `DETECTOR_BATCH_SIZE` (default 4), `SKIP_SURYA_CROPS`.

Volume для кэша моделей Surya: `./models/ocr/datalab_cache` → `/root/.cache/datalab`.

### 4.6 paddle (опционально)

PaddleOCR HTTP-сервис для recognition. CPU-only, без CUDA/torch. Не включён в основной `docker-compose.yml` — запускается отдельно при необходимости.

| Параметр | Значение |
|----------|---------|
| Base image | `python:3.11-slim` |
| Порт | 8010 |
| PaddlePaddle | 3.0.0 (CPU) |
| PaddleOCR | 3.0.0 |
| Model | PP-OCRv5_server_rec |
| CMD | `uvicorn paddle_service:app --host 0.0.0.0 --port 8010 --workers 1` |

---

## 5. Сервисы CVAT

CVAT v2.25.0 развёрнут как часть единого docker-compose. Все сервисы CVAT в сети `cvat`, P&ID Pipeline подключается к ней через сервисы `api` и `worker`.

| Сервис | Назначение |
|--------|-----------|
| `cvat_db` | PostgreSQL для CVAT (отдельная от P&ID) |
| `cvat_redis_inmem` | Redis in-memory (задачи, кэш) |
| `cvat_redis_ondisk` | Kvrocks (персистентное хранилище) |
| `cvat_server` | Django backend (API + admin) |
| `cvat_worker_import` | Импорт задач/данных |
| `cvat_worker_export` | Экспорт аннотаций |
| `cvat_worker_chunks` | Нарезка изображений на чанки |
| `cvat_ui` | React frontend |
| `cvat_opa` | Open Policy Agent (авторизация) |
| `traefik` | Reverse proxy (маршрутизация /api/ → server, остальное → ui) |

CVAT доступен через traefik на порту 8080. API pipeline подключается к `cvat_server:8080` напрямую через внутреннюю сеть `cvat` (без traefik). UI (браузер пользователя) обращается к CVAT через `traefik:8080` (reverse proxy маршрутизирует `/api/` → `cvat_server`, остальное → `cvat_ui`).

**Оптимизация стека.** Относительно эталонного compose CVAT набор сервисов урезан под роль «разметчик + экспорт»: убраны сервисы аналитики (clickhouse, vector, grafana), воркеры webhooks/quality/analytics и `cvat_worker_annotation`. Очередь `worker.annotation` обслуживает только авто-аннотацию (serverless/Nuclio); она отключена (`CVAT_SERVERLESS_ENABLED=false`, Nuclio нет), ручная разметка bbox сохраняется напрямую через REST, импорт/экспорт — через свои воркеры. Воркерам `import`/`export`/`chunks` задан `NUMPROCS: 1` — для одиночной разметки параллелизм не нужен, это снижает потребление RAM.

---

## 6. Volumes и хранилище

### Named volumes (Docker-managed)

| Volume | Сервис | Содержимое |
|--------|--------|-----------|
| `pid_postgres_data` | postgres | Данные PostgreSQL P&ID |
| `pid_redis_data` | redis | AOF persistence Redis |
| `pid_flower_data` | flower | История задач Flower (`--persistent`) |
| `cvat_db` | cvat_db | Данные PostgreSQL CVAT |
| `cvat_redis_inmem` | cvat_redis_inmem | Redis CVAT (in-memory) |
| `cvat_redis_ondisk` | cvat_redis_ondisk | Kvrocks CVAT |
| `cvat_data` | cvat_server + api/worker | Данные CVAT (изображения, задачи). Шарится: api/worker монтируют ro |
| `cvat_keys` | cvat_server | Ключи CVAT |
| `cvat_logs` | cvat_server | Логи CVAT |

### Bind mounts (из рабочей директории)

| Хост | Контейнер | Кто использует |
|------|-----------|---------------|
| `./storage` | `/storage` | api, worker, worker_ocr — артефакты pipeline |
| `./configs` | `/app/configs` | api, worker, worker_ocr — project YAML |
| `./models/*` | `/models/*` | worker (ro), worker_ocr — ML-чекпоинты |
| `./app`, `./worker`, `./modules` | `/app/*` | все P&ID сервисы — код (bind mount для dev) |

**Структура storage:**

Артефакты хранятся во вложенной структуре по этапам (подробное описание — см. [ARCHITECTURE.md §5](ARCHITECTURE.md#5-storage-layout)):

```
storage/diagrams/
└── {uid}/
    ├── original/image.png              # Загруженная схема
    ├── detection/coco_predicted.json    # Результат детекции
    ├── segmentation/pipe_mask.png       # Маска труб
    ├── skeleton/skeleton.png            # Скелет
    ├── graph/graph.json                 # Граф (узлы + рёбра)
    ├── contours/contours_auto.json      # SAM2-контуры
    ├── ocr/ocr_result.json              # OCR-результаты
    └── fxml/diagram.fxml                # Финальный FXML
```

---

## 7. Сети

| Сеть | Driver | Участники |
|------|--------|-----------|
| `pid_network` | bridge | postgres, redis, api, worker, worker_ocr |
| `cvat` | bridge | Все CVAT-сервисы + api, worker (для доступа к CVAT API) |

Сервис `api` и `worker` подключены к обеим сетям — они обращаются и к P&ID-инфраструктуре (postgres, redis), и к CVAT (cvat_server).

---

## 8. GPU

### Распределение GPU между worker'ами

Оба worker'а (`worker`, `worker_ocr`) резервируют GPU через Docker device reservation:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

На машине с одной GPU оба worker'а делят её. `worker_ocr` имеет `concurrency=1` — выполняет одну OCR-задачу за раз. `worker` имеет `concurrency=2`, но GPU-задачи (детекция, сегментация, SAM2) выполняются последовательно из-за ограничений VRAM.

### Разделение на multi-GPU

Для выделения конкретных GPU отдельным worker'ам — переопределить `NVIDIA_VISIBLE_DEVICES`:

```yaml
# worker — только GPU 0
worker:
  environment:
    - NVIDIA_VISIBLE_DEVICES=0

# worker_ocr — только GPU 1
worker_ocr:
  environment:
    - NVIDIA_VISIBLE_DEVICES=1
```

### Версии CUDA

| Worker | Base image | CUDA | PyTorch |
|--------|-----------|------|---------|
| `worker` | nvidia/cuda:12.4.0-runtime | 12.4 | 2.6.0+cu124 |
| `worker_ocr` | nvidia/cuda:12.6.3-runtime | 12.6 | latest+cu126 |

Разные версии CUDA — это корректно: каждый контейнер содержит свой CUDA runtime. Хост-драйвер должен поддерживать обе версии (driver 535+ поддерживает CUDA 12.x).

---

## 9. Обновление

### 9.1 Обновление кода

```bash
git pull origin main
docker-compose build api worker worker_ocr
docker-compose up -d api worker worker_ocr
```

Bind mounts (`./app`, `./worker`, `./modules`) обеспечивают обновление кода без пересборки образов в dev-режиме. Для production — пересборка обязательна.

### 9.2 Обновление моделей

Модели подключены через bind mount `./models:/models:ro`. Замена файла на хосте + рестарт worker'а:

```bash
# Заменить файл модели
cp new_best.pt models/yolo/best.pt

# Рестарт worker'а (модели загружаются при старте)
docker-compose restart worker
```

### 9.3 Миграции БД

```bash
docker exec -it pid_api alembic upgrade head
```

При добавлении новых миграций — сначала `alembic upgrade head`, затем рестарт сервисов.

### 9.4 Обновление CVAT

Изменить версию в `docker-compose.yml` (все cvat-сервисы):

```yaml
image: cvat/server:v2.26.0  # новая версия
```

```bash
docker-compose pull cvat_server cvat_ui cvat_worker_import cvat_worker_export cvat_worker_chunks
docker-compose up -d
```

---

## 10. Мониторинг

### 10.1 Логи

```bash
# Все сервисы
docker-compose logs -f

# Конкретный сервис
docker-compose logs -f worker
docker-compose logs -f worker_ocr

# Последние N строк
docker-compose logs --tail 100 api
```

Уровень логирования управляется переменной `LOG_LEVEL` в `.env` (по умолчанию `INFO`).

### 10.2 Healthcheck

API имеет встроенный healthcheck: `GET /health`. Docker проверяет каждые 30 секунд.

PostgreSQL и Redis также имеют healthcheck'и (`pg_isready`, `redis-cli ping`) с интервалом 5 секунд. Worker'ы зависят от них через `depends_on: condition: service_healthy`.

### 10.3 Celery мониторинг

Состояние очередей и worker'ов:

```bash
# Активные worker'ы
docker exec pid_worker celery -A worker.celery_app inspect active

# Зарезервированные задачи
docker exec pid_worker celery -A worker.celery_app inspect reserved

# Статистика
docker exec pid_worker celery -A worker.celery_app inspect stats
```

#### Flower — web-мониторинг Celery

Сервис `flower` в `docker-compose.yml` (образ `mher/flower:2.0`, контейнер `pid_flower`).
Открывается на `http://127.0.0.1:5555`: вкладка **Broker** — глубина очередей
(`default`, `gpu`, `ocr`, `sam2`), **Workers** — живые воркеры и их активные задачи,
**Tasks** — история задач с `uuid`, состоянием и временем.

```bash
docker compose up -d --no-deps flower     # поднять, не трогая остальной стек
docker logs pid_flower --tail 20
```

Зачем: **повторная выдача задачи брокером видна глазами** — та самая задача приходит
воркеру второй раз (дефект закрыт в `worker/celery_app.py` опцией
`visibility_timeout=7200`, но следить за ним больше неоткуда).

⛔ **Порт публикуется только на loopback.** У Flower по умолчанию нет аутентификации,
а в UI есть управление: отозвать задачу, снять воркера. С сервера смотреть через
SSH-туннель (`ssh -L 5555:127.0.0.1:5555 …`); публиковать наружу — только добавив
`--basic-auth=user:pass`. По той же причине включена `FLOWER_UNAUTHENTICATED_API=true`:
экспозиции она не добавляет (UI на том же порту и так открыт), но без неё `/api/*`
не отвечает.

События задач Flower включает воркерам сам, периодической командой `enable_events`
(`worker_send_task_events` в конфиге Celery не задан — при остановленном Flower
воркеры событий не шлют, и это нормально: смотреть их некому).

Проверка, что мониторинг действительно работает (кладёт в очередь `default` безобидную
встроенную `celery.accumulate` и требует увидеть её в Flower):

```bash
python -X utf8 tools/flower_gate.py --check
```

Коды выхода: `0` — Flower показывает очереди и задачи; `1` — не показывает (не поднят,
не отвечает, задачи не видит); `2` — судить нечем (нет брокера или очередь `default`
никто не слушает).

### 10.4 GPU мониторинг

```bash
# На хосте
nvidia-smi -l 5  # обновление каждые 5 секунд

# Внутри контейнера
docker exec pid_worker nvidia-smi
```

---

## 11. Backup и восстановление

### 11.1 PostgreSQL (P&ID)

```bash
# Backup
docker exec pid_postgres pg_dump -U pid_user pid_pipeline > backup_$(date +%Y%m%d).sql

# Restore
docker exec -i pid_postgres psql -U pid_user pid_pipeline < backup_20260409.sql
```

### 11.2 PostgreSQL (CVAT)

```bash
# Backup
docker exec cvat_db pg_dump -U root cvat > cvat_backup_$(date +%Y%m%d).sql

# Restore
docker exec -i cvat_db psql -U root cvat < cvat_backup_20260409.sql
```

### 11.3 Storage (артефакты)

```bash
# Backup
rsync -av ./storage/ /backup/pid_storage/

# Restore
rsync -av /backup/pid_storage/ ./storage/
```

### 11.4 Полный backup (volumes)

```bash
# Остановить сервисы
docker-compose down

# Backup всех volumes
for vol in pid_postgres_data pid_redis_data cvat_db cvat_data; do
  docker run --rm -v ${vol}:/data -v /backup:/backup alpine \
    tar czf /backup/${vol}_$(date +%Y%m%d).tar.gz -C /data .
done

# Backup storage и models
tar czf /backup/storage_$(date +%Y%m%d).tar.gz -C . storage/
tar czf /backup/models_$(date +%Y%m%d).tar.gz -C . models/
```

---

## 12. Security

### Текущее состояние

| Аспект | Статус | Описание |
|--------|--------|----------|
| Аутентификация API | Нет | P&ID API без auth (внутренняя сеть) |
| Аутентификация CVAT | Есть | Django superuser + token |
| CORS | Не настроен | FastAPI defaults |
| TLS/HTTPS | Нет | Traefik на HTTP (порт 8080) |
| Worker non-root | Нет | TODO в Dockerfile.worker (проблема с правами volumes) |
| Secrets в .env | Plain text | Пароли в `.env` файле |

### Рекомендации для production

Ограничить доступ к портам 8000 (API) и 8080 (CVAT) файрволом — только из внутренней сети. Сменить пароли по умолчанию: `DB_PASSWORD`, `CVAT_SUPERUSER_PASSWORD`. При необходимости внешнего доступа — настроить TLS на traefik и добавить middleware авторизации в FastAPI.

---

## 13. Performance tuning

### Worker concurrency

| Worker | Default | Параметр | Влияние |
|--------|---------|----------|---------|
| `worker` | 2 | `--concurrency=N` в CMD Dockerfile.worker | Больше параллельных задач, но больше VRAM |
| `worker_ocr` | 1 | `--concurrency=N` в CMD Dockerfile.worker_ocr | Surya потребляет ~4-6 GB VRAM, >1 рискованно на 8 GB |

### Batch sizes

| Параметр | Где | Default | Описание |
|----------|-----|---------|----------|
| `segmentation.batch_size` | project YAML | 4 | Тайлы UNet++ за раз |
| `junction_seg.batch_size` | project YAML | 8 | Тайлы CenterNet за раз |
| `RECOGNITION_BATCH_SIZE` | env worker_ocr | 64 | Кропы Surya recognition за раз |
| `DETECTOR_BATCH_SIZE` | env worker_ocr | 4 | Тайлы Surya detection за раз |

На GPU с 8 GB VRAM — defaults оптимальны. На 12+ GB можно увеличить batch_size для ускорения.

### Celery task timeout

`CELERY_TASK_TIME_LIMIT` (default 3600 секунд = 1 час). Для крупных схем (>10000×7000 px) может потребоваться увеличение.

### Redis memory

Redis используется как broker + result backend. При большом количестве задач в очереди — настроить `maxmemory` в Redis:

```yaml
redis:
  command: redis-server --appendonly yes --maxmemory 512mb --maxmemory-policy allkeys-lru
```

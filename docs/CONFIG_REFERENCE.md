# CONFIG_REFERENCE.md

**Аудитория:** DEV / OPS
**Версия:** 1.1
**Обновлено:** 2026-04-09
**Связанные документы:** ARCHITECTURE.md, MODELS.md, OCR_PIPELINE.md, WORKER_TASKS.md, DEPLOYMENT.md

---

## Оглавление

1. [Обзор системы конфигурации](#1-обзор-системы-конфигурации)
2. [Environment Variables](#2-environment-variables)
3. [Project YAML](#3-project-yaml)
   - 3.1 project / cvat
   - 3.2 classes
   - 3.3 detection
   - 3.4 segmentation
   - 3.5 skeleton
   - 3.6 junction_seg
   - 3.7 contour_extraction
   - 3.8 ocr
   - 3.9 text_recognition (legacy)
4. [Domain Profiles](#4-domain-profiles)
   - 4.1 Архитектура domain profile
   - 4.2 KKS профиль
   - 4.3 Кириллический профиль
   - 4.4 Переключение профиля
5. [UI Settings](#5-ui-settings)
6. [Dataclass-маппинг](#6-dataclass-маппинг)

---

## 1. Обзор системы конфигурации

Конфигурация P&ID Pipeline состоит из четырёх уровней, каждый из которых отвечает за свою область:

```
.env / ENV vars          ← инфраструктура (БД, Redis, CVAT, пути)
  └─ app/config.py         ← Settings (pydantic-settings), загрузка из .env
      └─ project YAML      ← параметры pipeline по проекту (модели, пороги, тайлинг)
          └─ domain profile ← OCR: regex, classify, grouping, binding (отдельный YAML)

ui/services/ui_settings.py ← настройки UI (QSettings, хранятся в реестре / ~/.config)
```

**Механизм загрузки.** Класс `ProjectLoader` (`app/services/project_loader.py`) при старте сканирует `PROJECTS_CONFIG_DIR` (по умолчанию `./configs/projects`). Для каждого проекта ищет YAML двумя способами: подпапка `configs/projects/{code}/{code}.yaml` или плоский файл `configs/projects/{code}.yaml`. Результат парсится в дерево dataclass'ов (`ProjectConfig` → `DetectionConfig`, `SegmentationConfig` и т.д.) и кэшируется через `lru_cache`.

**Файлы конфигурации проекта "Термогидравлика":**

| Файл | Назначение |
|------|-----------|
| `configs/projects/thermohydraulics/thermohydraulics.yaml` | Основной конфиг проекта: классы, модели, пороги всех этапов |
| `configs/projects/thermohydraulics/domain_profile_kks.yaml` | OCR domain profile для KKS-кодов (Росатом) |
| `configs/projects/thermohydraulics/domain_profile_cyrillic.yaml` | OCR domain profile для кириллических кодов (ТЭЦ/Котельные) |
| `app/config.py` | Env-переменные инфраструктуры |
| `ui/services/ui_settings.py` | Настройки UI-приложения |

**Проект "ТЭЦ Котельная" (tec_boiler):**

| Файл | Назначение |
|------|-----------|
| `configs/projects/tec_boiler/domain_profile.yaml` | Domain profile для ТЭЦ-котельных схем |

Проект `tec_boiler` использует ту же структуру конфигурации, что и `thermohydraulics`. `ProjectLoader` автоматически обнаруживает все проекты в `PROJECTS_CONFIG_DIR`.

---

## 2. Environment Variables

Загружаются из `.env` файла через `pydantic-settings` (`app/config.py` → класс `Settings`). Все переменные `case_sensitive=True`, лишние переменные в `.env` игнорируются (`extra="ignore"`).

| Переменная | Тип | Default | Описание |
|-----------|-----|---------|----------|
| `DATABASE_URL` | str | `postgresql://pid_user:changeme@localhost:5433/pid_pipeline` | PostgreSQL connection string |
| `CELERY_BROKER_URL` | str | `redis://localhost:6380/0` | Redis URL для Celery broker |
| `CELERY_RESULT_BACKEND` | str | `redis://localhost:6380/0` | Redis URL для Celery result backend |
| `CELERY_TASK_TIME_LIMIT` | int | `3600` | Таймаут задачи Celery (секунды) |
| `CVAT_URL` | str | `http://localhost:8080` | URL CVAT API (для серверных вызовов) |
| `CVAT_BROWSER_URL` | str | `http://localhost:8080` | URL CVAT для открытия в браузере пользователя |
| `CVAT_TOKEN` | str \| None | `None` | Токен авторизации CVAT (опционально) |
| `STORAGE_PATH` | str | `./storage/diagrams` | Корневой путь хранилища артефактов (схемы, маски, JSON) |
| `PROJECTS_CONFIG_DIR` | str | `./configs/projects` | Директория с YAML-конфигами проектов |
| `API_HOST` | str | `0.0.0.0` | Хост FastAPI сервера |
| `API_PORT` | int | `8000` | Порт FastAPI сервера |
| `DEBUG` | bool | `False` | Режим отладки |
| `LOG_LEVEL` | str | `DEBUG` | Уровень логирования (DEBUG, INFO, WARNING, ERROR) |

Глобальный singleton доступен через `from app.config import settings`.

### Переменные из `.env.example` (вспомогательные)

Следующие переменные используются в `.env.example` и `docker-compose.yml`, но **не** входят в класс `Settings` (Pydantic). Они подставляются через shell-interpolation в `docker-compose.yml` или используются напрямую в Docker environment:

| Переменная | Тип | Default | Описание |
|-----------|-----|---------|----------|
| `DB_HOST` | str | `postgres` | Хост PostgreSQL (используется в `DATABASE_URL` через `${DB_HOST}`) |
| `DB_PORT` | int | `5432` | Порт PostgreSQL |
| `DB_USER` | str | `pid_user` | Пользователь БД |
| `DB_PASSWORD` | str | — | Пароль БД |
| `DB_NAME` | str | `pid_pipeline` | Имя базы данных |
| `REDIS_HOST` | str | `redis` | Хост Redis |
| `REDIS_PORT` | int | `6379` | Порт Redis |
| `REDIS_DB` | int | `0` | Номер Redis DB |
| `CVAT_PROJECT_ID` | int | `1` | ID проекта в CVAT (для начальной настройки) |
| `CVAT_NETWORK_NAME` | str | `cvat_cvat` | Имя Docker network для CVAT |
| `YOLO_WEIGHTS` | str | `/models/yolo/best.pt` | Путь к весам YOLO (env для Docker) |
| `JUNCTION_WEIGHTS` | str | `/models/junction/best.pth` | Путь к весам Junction CNN (env для Docker) |
| `CELERY_CONCURRENCY` | int | `2` | Число worker-процессов Celery |
| `API_DEBUG` | bool | `true` | Режим отладки для Docker. **Примечание:** класс `Settings` использует поле `DEBUG` (не `API_DEBUG`). В `.env.example` используется `API_DEBUG` — это **не** то же самое поле. При использовании `Settings` следует задавать `DEBUG=true` |
| `STATUS_POLL_INTERVAL` | int | `2000` | Интервал polling статуса (мс), используется UI |
| `STATUS_PROVIDER` | str | `polling` | Провайдер статуса (для UI) |

---

## 3. Project YAML

Основной файл: `configs/projects/thermohydraulics/thermohydraulics.yaml`. Парсится в `ProjectConfig` dataclass через `ProjectLoader._parse_yaml()`.

### 3.1 `project` / `cvat`

```yaml
project:
  name: "Термогидравлика"
  code: "thermohydraulics"

cvat:
  project_name: "P&ID Термогидравлика"
```

| Параметр | Тип | Описание |
|----------|-----|----------|
| `project.name` | str | Человекочитаемое название проекта |
| `project.code` | str | Уникальный код проекта (используется в путях, API, БД) |
| `cvat.project_name` | str | Имя проекта в CVAT (для автоматического создания) |

### 3.2 `classes`

Реестр классов оборудования. ID 1-based (соответствуют `category_id` в CVAT).

```yaml
classes:
  - {id: 1, name: "armatura_ruchn"}
  - {id: 2, name: "klapan_obratn"}
  # ... всего 40 классов
  - {id: 38, name: "unknow"}
  - {id: 39, name: "strelka"}
  - {id: 40, name: "background"}
```

Парсится в список `ClassInfo(id, name)`. Полный перечень — 40 классов, от `armatura_ruchn` (1) до `background` (40). Специальные классы: `annotation` (35) — текстовые аннотации, `output` (36) — выходы со схемы, `truba` (37) — трубы (используются для сегментации, а не детекции как оборудование), `unknow` (38) — нераспознанное оборудование (кандидат на реклассификацию по KKS-коду), `strelka` (39) — стрелки направления потока, `background` (40) — фон.

### 3.3 `detection`

Мульти-модельная детекция YOLOv8m + SAHI. Поддерживает несколько моделей с переключением через `default_model`.

```yaml
detection:
  default_model: "thermo"
  models:
    thermo:
      name: "Термогидравлика"
      weights: "/models/yolo/best.pt"
      num_classes: 36
      confidence: 0.8
      description: "Основная модель (термогидравлика)"
      class_mapping: *class_mapping
    paksh:
      name: "Пакш"
      weights: "/models/yolo/best_pakh.pt"
      # ... аналогичная структура
```

| Параметр | Тип | Default | Описание |
|----------|-----|---------|----------|
| `default_model` | str | `"default"` | ID модели по умолчанию |
| `models.{id}.name` | str | `""` | Название для UI |
| `models.{id}.weights` | str | `""` | Путь к весам (внутри Docker: `/models/...`) |
| `models.{id}.num_classes` | int | `36` | Количество YOLO-классов |
| `models.{id}.confidence` | float | `0.8` | Глобальный порог confidence |
| `models.{id}.class_mapping` | dict | `{}` | YOLO class_id → CVAT category_id (1-based) |
| `models.{id}.description` | str | `""` | Описание для UI tooltip |
| `models.{id}.per_class_confidence` | dict | `{}` | Пороги confidence per-class (имя класса → float) |
| `models.{id}.sahi_slice_size` | int | `1280` | Размер тайла SAHI (px) |
| `models.{id}.sahi_overlap_ratio` | float | `0.25` | Перекрытие тайлов SAHI |

**class_mapping.** Маппинг YOLO class_id (0-based, после reverse_reindex) в CVAT category_id (1-based). Для 36 классов: class_id 0–33 → category_id 1–34 (прямое +1), class_id 35 → category_id 36 (output), class_id 38 → category_id 39 (strelka). Классы 34, 36, 37 (annotation, truba, background) отсутствуют — они не детектируются YOLO.

**Legacy-совместимость.** Если вместо `detection.models` в YAML есть секция `yolo`, `ProjectLoader` автоматически конвертирует её в `detection.models.default`.

### 3.4 `segmentation`

UNet++ ensemble из двух моделей для сегментации труб.

```yaml
segmentation:
  weights: "/models/segmentation/best.pth"        # Model A: plain UNet++
  weights_b: "/models/segmentation/best_dual.pth"  # Model B: DualHeadModel
  dual_head_a: false
  dual_head_b: true
  ensemble_strategy: "or"
  # ...
```

| Параметр | Тип | Default | Описание |
|----------|-----|---------|----------|
| `weights` | str | `""` | Чекпоинт Model A (plain UNet++) |
| `weights_b` | str | `""` | Чекпоинт Model B (DualHeadModel) |
| `dual_head_a` | bool | `false` | Model A — DualHeadModel? |
| `dual_head_b` | bool | `true` | Model B — DualHeadModel? |
| `ensemble_strategy` | str | `"or"` | Стратегия ensemble: `"or"` / `"and"` / `"mean"` / `"weighted_mean"`. OR даёт +4% recall |
| `tile_size` | int | `1024` | Размер тайла (px) |
| `overlap` | int | `128` | Перекрытие тайлов (px) |
| `batch_size` | int | `4` | Batch size инференса |
| `threshold` | float | `0.5` | Порог бинаризации предсказания |
| `use_tta` | bool | `true` | Test-Time Augmentation (flip-аугментации) |
| `binarize` | bool | `true` | Бинаризовать выходную маску |
| `binarize_method` | str | `"adaptive"` | Метод бинаризации: `"adaptive"` / `"global"` |
| `postprocess` | bool | `true` | Включить постобработку |
| `pipe_categories` | list | `["truba"]` | Категории, формирующие pipe mask |
| `ignore_categories` | list | `["annotation"]` | Категории, исключаемые из node mask |

**postprocess_config** — вложенная структура постобработки сегментации:

| Параметр | Тип | Default | Описание |
|----------|-----|---------|----------|
| `skeleton_gap_fill.enabled` | bool | `true` | Заполнение разрывов скелета |
| `skeleton_gap_fill.max_gap` | int | `40` | Макс. длина заполняемого разрыва (px) |
| `skeleton_gap_fill.direction_tolerance` | int | `20` | Допуск направления (градусы) |
| `skeleton_gap_fill.verify_path` | bool | `true` | Верификация пути через маску |
| `remove_border_frame.enabled` | bool | `true` | Удаление рамки чертежа |
| `remove_border_frame.margin` | int | `30` | Отступ от края (px) |
| `fill_holes.enabled` | bool | `true` | Заполнение дыр в маске |
| `fill_holes.max_hole_size` | int | `500` | Макс. площадь заполняемой дыры (px²) |

### 3.5 `skeleton`

Скелетизация труб и генерация маски из скелета. Параметры передаются как dict в `skeleton_extension.process_single_image()` через метод `SkeletonConfig.to_process_config()`.

| Параметр | Тип | Default | Описание |
|----------|-----|---------|----------|
| `simple_mode` | bool | `false` | Упрощённый режим (без trim/directed/BFS). `false` = полный режим |
| `node_boundary_expansion` | int | `1` | Расширение границ узлов (px) |
| `trim_length` | int | `10` | Длина обрезки концов скелета (px) |
| `trim_protection` | int | `15` | Минимальная длина ветки, защищённой от trim (px) |
| `mask_width` | int | `8` | Ширина маски для extend (px) |
| `extend_radius` | int | `5` | Радиус поиска при extension скелета к узлам (px) |
| `direction_trace_length` | int | `5` | Длина трассировки для определения направления (px) |
| `max_line_length` | int | `600` | Макс. длина ребра графа (px). Dataclass fallback = 1000, но YAML проекта задаёт 600. **Приоритет:** значение из YAML всегда переопределяет dataclass default |
| `endpoint_search_radius` | int | `5` | Радиус поиска endpoint'ов (px) |
| `endpoint_line_white_tolerance` | int | `2` | Допуск белых пикселей при поиске endpoint (px) |
| `skeleton_search_radius` | int | `5` | Радиус поиска пикселей скелета (px) |
| `skeleton_line_white_tolerance` | int | `2` | Допуск белых пикселей на скелете (px) |
| `bfs_max_depth` | int | `1000` | Макс. глубина BFS при трассировке (в dataclass default=2000) |
| `bfs_iterations` | int | `1` | Количество итераций BFS |
| `bfs_mask_tolerance` | int | `5` | Допуск маски при BFS (px) |
| `remove_orphans` | bool | `false` | Удалять orphan-ветки |
| `orphan_trim_length` | int | `5` | Длина обрезки orphan'ов (px) |
| `orphan_touch_distance` | int | `3` | Дистанция "касания" orphan до основного скелета (px) |

**Параметры генерации маски** (skeleton → mask):

| Параметр | Тип | Default | Описание |
|----------|-----|---------|----------|
| `mask_adaptive` | bool | `true` | Адаптивная толщина из реальной ширины труб |
| `mask_min_thickness` | int | `2` | Минимальный диаметр (px) |
| `mask_max_thickness` | int | `40` | Максимальный диаметр (px) |
| `mask_thickness` | int | `4` | Fallback толщина для фиксированного режима (в dataclass default=12) |
| `mask_prune_spurs` | int | `5` | Длина шпор для удаления (0 = отключить) |
| `mask_smooth_size` | int | `5` | Ядро closing-сглаживания (0 = отключить) |

### 3.6 `junction_seg`

CenterNet-модель для классификации junction (T/X/L-соединений) и bridge (пересечений без соединения).

| Параметр | Тип | Default | Описание |
|----------|-----|---------|----------|
| `weights` | str | `""` | Путь к чекпоинту CenterNet |
| `tile_size` | int | `512` | Размер тайла (px) |
| `overlap` | int | `128` | Перекрытие тайлов (px) |
| `batch_size` | int | `8` | Batch size |
| `junction_threshold` | float | `0.40` | Порог heatmap для junction (в dataclass default=0.55) |
| `bridge_threshold` | float | `0.45` | Порог heatmap для bridge (в dataclass default=0.60) |
| `nms_kernel` | int | `3` | Ядро NMS (px) |
| `square_size` | int | `15` | Размер квадрата junction-области (px) |

### 3.7 `contour_extraction`

SAM2-модель для извлечения контуров оборудования. Работает параллельно с graph/OCR этапами (Phase 7b). См. MODELS.md для деталей архитектуры.

```yaml
contour_extraction:
  enabled: true
  checkpoint: "/models/sam2/sam2_pid_best.pth"
  base_weights: "/models/sam2/sam2_hiera_small.pt"
  confidence_threshold: 0.85
  snap_dp_eps: 0.15
  snap_threshold: 0.08
  snap_min_edge: 0.03
  skip_classes:
    - background
    - truba
    - annotation
    - strelka
    - connector
```

| Параметр | Тип | Default | Описание |
|----------|-----|---------|----------|
| `enabled` | bool | `false` | Включить контурную экстракцию |
| `checkpoint` | str | `""` | Путь к fine-tuned SAM2 чекпоинту |
| `base_weights` | str | `""` | Путь к базовым весам SAM2 (Hiera Small) |
| `confidence_threshold` | float | `0.85` | Минимальный confidence для принятия контура |
| `snap_dp_eps` | float | `0.15` | Douglas-Peucker epsilon как доля периметра bbox |
| `snap_threshold` | float | `0.08` | Порог "smart snap" для H/V-привязки рёбер (доля стороны bbox) |
| `snap_min_edge` | float | `0.03` | Минимальная длина ребра полигона после snap (доля периметра) |
| `skip_classes` | list | `[]` | Классы, для которых контуры не извлекаются |

### 3.8 `ocr`

Параметры OCR pipeline (3-проходный Surya OCR + PaddleOCR). Секция `ocr` в project YAML управляет выбором domain profile и тайлингом.

```yaml
ocr:
  domain_profile_path: "configs/projects/thermohydraulics/domain_profile_kks.yaml"
  profile_path: "configs/projects/thermohydraulics/ocr_profile.py"   # legacy 1.x
  profile_class: "KKSProfile"                                        # legacy 1.x
  no_protection: true
  tile2_size: 1536
  tile2_overlap: 256
  tile3_size: 2560
  tile3_overlap: 384
```

| Параметр | Тип | Default | Описание |
|----------|-----|---------|----------|
| `domain_profile_path` | str \| None | `None` | Путь к domain profile YAML (v2.0, приоритет над всем) |
| `profile_path` | str \| None | `None` | Legacy 1.x: путь к Python-файлу профиля |
| `profile_class` | str | `"KKSProfile"` | Legacy 1.x: имя класса в Python-профиле |
| `no_protection` | bool | `true` | Отключить protection padding вокруг текста |
| `tile2_size` | int | `1536` | Размер тайла второго прохода OCR (px) |
| `tile2_overlap` | int | `256` | Перекрытие тайлов второго прохода (px) |
| `tile3_size` | int | `2560` | Размер тайла третьего прохода OCR (px) |
| `tile3_overlap` | int | `384` | Перекрытие тайлов третьего прохода (px) |

**Приоритет загрузки профиля:** `domain_profile_path` (v2.0) → `profile_path` (v1.x .py) → auto-detect `domain_profile.yaml` рядом с project YAML.

### 3.9 `text_recognition` (legacy)

Секция `text_recognition` внутри project YAML — legacy-механизм привязки OCR-результатов к графу. В v2.0 эта логика перенесена в domain profile (секции `code_types` и `binding`). Если `domain_profile_path` задан, `text_recognition` игнорируется.

```yaml
text_recognition:
  kks:
    patterns:
      - name: "standard_kks"
        regex: "..."
    binding:
      target: "equipment"
      method: "nearest_bbox"
      max_distance: 150
  pipe_diameter:
    ocr_corrections:
      - pattern: "^0([yYvVуУ])"
        replace: "D\\1"
    patterns:
      - name: "dy_dn_format"
        regex: "..."
    binding:
      target: "edge"
      method: "nearest_edge_boundary"
      max_distance: 150
```

---

## 4. Domain Profiles

### 4.1 Архитектура domain profile

Domain profile — единый YAML-файл, заменивший legacy-набор из 4+ файлов (ocr_profile.py, kks_config.yaml, class_to_kks_config.yaml и секцию text_recognition). Содержит полную конфигурацию OCR-пайплайна: от regex-паттернов до правил привязки к графу.

Секции domain profile:

| Секция | Назначение |
|--------|-----------|
| `meta` | Метаданные: имя, версия, языки, направление нормализации |
| `code_types` | Типы кодов (equipment_code, pipeline_code, diameter) с match_patterns и binding |
| `units` | Список юнитов/систем (подставляются в `%%UNITS%%` placeholder в regex) |
| `patterns` | Regex-паттерны для classify engine (full_code, head_code, tail_code, diameter и т.д.) |
| `classify` | Правила классификации OCR-текста → категория (FULL, HEAD, TAIL, DN, MULTI, FRAG, OTHER) |
| `grouping` | Пространственная сборка блоков (head→tail merge, orphan attach, emit) |
| `noise` | Фильтрация шума (даты, boilerplate, instrument tags) |
| `char_normalization` | Посимвольная замена визуально похожих символов (кириллица↔латиница) |
| `ocr_corrections` | Исправление типичных OCR-ошибок (O→0, кириллица→латиница в digit позициях) |
| `postprocess` | Хуки пост-обработки (split_multi, promote_from_secondary, merge_secondary) |
| `binding` | Привязка кодов к графу: unit_to_classes, class_rules, reclassify |

### 4.2 KKS профиль (`domain_profile_kks.yaml`)

**Назначение:** OCR-профиль для схем Росатома. Коды оборудования — Kraftwerk-Kennzeichensystem (KKS).

**Структура KKS-кода:**

```
{block}{system}{fn}{unit}{num}{suffix}
  │       │     │     │     │     └─ опциональная буква (A, B, ...)
  │       │     │     │     └─ счётчик (2-4 цифры): 017, 201, 002
  │       │     │     └─ агрегат (2 буквы): AA, AP, CT, BR ...
  │       │     └─ функциональный номер (2-3 цифры): 10, 30, 03
  │       └─ система (2-3 буквы): LFN, LAB, LAC ...
  └─ блок (0-2 цифры): 10, 2, 50

Примеры: 10LFN10 AA017, 10LAB30AA201, 2LAC03CT002
```

**meta:**

| Параметр | Значение | Описание |
|----------|---------|----------|
| `name` | `"KKS (Росатом)"` | Человекочитаемое имя |
| `version` | `"2.0"` | Версия формата |
| `languages` | `["en", "ru"]` | Поддерживаемые языки |
| `normalization_direction` | `"cyr_to_lat"` | Нормализация: кириллица → латиница (А→A, С→C) |
| `secondary_script` | `"cyrillic"` | Вторичный скрипт (описания, не коды) |

**units.** Список допустимых 2-буквенных юнитов KKS: AA, AC, AH, AP, AT, AX, BB, BR, BP, BQ, CF, CG, CL, CM, CP, CQ, CR, CT, CY, GF, GH. Подставляются в regex через placeholder `%%UNITS%%`.

**patterns.** Regex-паттерны с ролями:

| Паттерн | Роль | Что ищет |
|---------|------|----------|
| `full_code` | full | Полный KKS-код: блок + система + fn + unit + num |
| `head_code` | head | Головная часть: блок + система + fn |
| `tail_code` | tail | Хвостовая часть: unit + num |
| `diameter` | standalone | Начало строки: Dy/DN/Dv + цифра |
| `diameter_inline` | standalone | Dy/DN внутри строки |
| `fragment_head` | fragment | Неполный head: блок + система + 0-1 цифра |
| `head_loose` | head | Ослабленный head (без word boundary слева) |

**classify.rules.** Цепочка правил (if/elif логика, декларативно):

```
full_code match_count >= 2  → MULTI
full_code matches           → FULL
head_code matches (без tail):
  head_code count >= 2      → MULTI
  + diameter                → HEAD_DN
  иначе                    → HEAD
tail_code matches           → TAIL
diameter match_only         → DN
fragment_head match_only    → FRAG
иначе                      → OTHER
```

Роли для grouping engine: full → [FULL, FULL_BR, MULTI], head → [HEAD, HEAD_DN], tail → [TAIL], standalone → [DN], fragment → [FRAG].

**grouping.passes.** Последовательные проходы пространственной сборки:

| Проход | Source | Target | Действие | Параметры |
|--------|--------|--------|----------|-----------|
| `fragment_completion` | FRAG | HEAD, FULL, TAIL, OTHER | merge + reclassify | max_distance=2.0, direction=any |
| `full_codes` | FULL, FULL_BR | — | emit | — |
| `head_to_tail` | HEAD, HEAD_DN | TAIL | merge + emit | max_distance=3.0, below_only, x_overlap≥0.25, chain (chain_max=2.0, head_between_check) |
| `orphan_tail` | TAIL | HEAD, FULL | attach | max_distance=3.0, any direction |
| `diameters` | DN | — | emit | — |
| `multi_codes` | MULTI | — | emit | — |
| `remaining_heads` | HEAD | — | emit | — |

Расстояния в `max_distance` — в единицах высоты bbox блока (не пиксели).

**noise.rules:**

| Правило | Что фильтрует |
|---------|--------------|
| `instrument_tag` | Коды КИП: FI, LI, TI, PI, LISAC, PISA, FIDA |
| `date` | Даты: 01.01.2024 |
| `company_boilerplate` | Юридический boilerplate: "shall not be disclosed", "Проектная документ..." |
| `file_extension` | Расширения файлов: .pdf, .dwg, .doc |

Пороги шума: `min_length_without_target=2`, `max_length_text_only=25`, `max_word_count_no_target=4`, `max_special_char_ratio_short=0.5`, `short_text_max_length=5`.

**char_normalization.** Направление: `cyr_to_lat`. Заменяет визуально похожие кириллические символы латинскими: А→A, В→B, С→C, Е→E, Н→H, К→K, М→M, О→O, Р→P, Т→T, У→Y, Х→X (+ строчные).

**ocr_corrections:**

| Секция | Назначение |
|--------|-----------|
| `cyrillic_to_latin` | Буквенные позиции: А→A, В→B, С→C и т.д. (полный набор upper+lower) |
| `digit_corrections` | Цифровые позиции: O→0, о→0, О→0, I→1, l→1 |
| `symbol_fixes` | Спецсимволы: (→C, [→C, )→D, ]→J, \|→I |
| `unit_blacklist` | Юниты-исключения (совпадают с диаметрами): DY, DV, DN, DН |
| `known_blocks` | Известные блоки для коррекции "0"→"10": [10, 50] |

**binding.** Привязка распознанных кодов к элементам графа:

| Параметр | Значение | Описание |
|----------|---------|----------|
| `node_max_distance` | `30.0` | Макс. расстояние привязки к узлу (px) |
| `edge_max_distance` | `150.0` | Макс. расстояние привязки к ребру (px) |
| `edge_binding_units` | `["BR"]` | Юниты, привязываемые к рёбрам (трубопроводы) |

**unit_to_classes** — маппинг юнит → допустимые классы оборудования. Примеры: AA → арматура, клапаны, регуляторы, дросселя; AP → насосы; AC → теплообменники, сепараторы, деаэраторы; CF/CG/CL/CM/CP/CQ/CR/CT/CY → датчики; BR → [] (привязка к ребру, не к узлу).

**class_rules** — для каждого класса оборудования определяет: `kks_target` (node/edge/none/reclassify) и `expected_units` (допустимые юниты). Класс `unknow` имеет `kks_target: "reclassify"` с маппингом unit → целевой класс (AA→armatura_electro, AP→nasos, AC→teploobmen и т.д.).

### 4.3 Кириллический профиль (`domain_profile_cyrillic.yaml`)

**Назначение:** OCR-профиль для схем ТЭЦ/Котельных с кириллическими кодами.

**Структура кода:**

```
{блок}-{система}-{номер}{суффикс}
  │        │        │       └─ опционально: К, Р, А, Б, Л, Н, СК
  │        │        └─ 1-4 цифры: 585, 601, 32
  │        └─ 2 кириллические буквы: ПД, ПО, ПП, ВП, КТ ...
  └─ буква + цифра: Т1, Е1, Г1 (может отсутствовать)

Примеры: Т1-ПД-585, ПД-601Р, Е1-ПД-32Л, Т1-ПО-45СК
Диаметры: ⌀50, Ø125, ø20
```

**Ключевые отличия от KKS-профиля:**

| Аспект | KKS | Кириллический |
|--------|-----|--------------|
| Направление нормализации | `cyr_to_lat` (А→A) | `lat_to_cyr` (T→Т) + греческий→кирилл. |
| Разделитель | без разделителя | дефис `-` (+ юникод-варианты) |
| Юниты (системы) | 2 латинские буквы (AA, AP, CT) | 2 кириллические буквы (ПД, ПО, ВП) |
| unit_to_classes | unit определяет тип оборудования | все системы → все классы |
| Формат диаметра | Dy/DN + число | ⌀/Ø + число |
| OCR corrections | cyrillic_to_latin, digit, symbol fixes | block_fixes (71→Т1, [1→Т1 и т.д.) |
| Classify preprocess | нет | clean_html, normalize, block_fixes |
| Classify patterns | single pattern rules | combined `[full_code, bare_code]` с `total_count` |
| Postprocess split | `head_tail_join` | `inline_split` |
| promote_from_secondary | enabled | disabled |

**systems.** 18 кириллических систем: ПД, ПО, ПП, ПР, ВП, КТ, НК, ТК, ВД, НД, КД, ДП, ДР, РД, СК, КН, ПВ, ОК.

**binding.** В кириллическом профиле код системы (ПД, ПО, ...) обозначает функциональную систему станции, а не тип оборудования. Поэтому `unit_to_classes` маппит каждую систему на все типы оборудования (через YAML-якорь `&all_eq`). `edge_binding_units` пуст — трубопроводных кодов (аналог BR) в кириллических схемах нет.

### 4.4 Переключение профиля

Переключение между KKS и кириллическим профилем — одна строка в `thermohydraulics.yaml`:

```yaml
ocr:
  # KKS (Росатом):
  domain_profile_path: "configs/projects/thermohydraulics/domain_profile_kks.yaml"
  # Кириллический (ТЭЦ):
# domain_profile_path: "configs/projects/thermohydraulics/domain_profile_cyrillic.yaml"
```

После изменения — рестарт OCR-воркера:

```bash
docker-compose restart worker_ocr
```

---

## 5. UI Settings

Файл: `ui/services/ui_settings.py`. Класс `UISettings` — singleton-обёртка над `QSettings` (PySide6). Хранилище: реестр Windows (`HKCU\Software\PID\P&ID Pipeline`) или `~/.config/PID/P&ID Pipeline.conf` на Linux.

| Параметр | Тип | Default | Описание |
|----------|-----|---------|----------|
| `autosave/enabled` | bool | `true` | Автосохранение редактора включено |
| `autosave/interval_sec` | int | `120` | Интервал автосохранения (секунды) |

Доступ: `UISettings.instance().autosave_enabled`, `UISettings.instance().autosave_interval_sec`.

**Примечание:** `QSettings` может содержать дополнительные ключи (window geometry, last project и др.), которые управляются Qt автоматически. Здесь документированы только программно используемые параметры pipeline.

---

## 6. Dataclass-маппинг

Каждая секция project YAML парсится в соответствующий dataclass из `app/services/project_loader.py`:

| YAML-секция | Dataclass | Ключевые потребители |
|------------|-----------|---------------------|
| `project` + `cvat` + `classes` | `ProjectConfig` | Все worker'ы, API, UI |
| `detection.models.{id}` | `DetectionModelConfig` | `worker_detection` (YOLO + SAHI) |
| `detection` | `DetectionConfig` | `ProjectConfig.yolo` (backward compat), UI model selector |
| `segmentation` | `SegmentationConfig` | `worker_segmentation` (UNet++ ensemble) |
| `skeleton` | `SkeletonConfig` | `worker_skeleton` (через `.to_process_config()`) |
| `junction_seg` | `JunctionSegConfig` | `worker_junction` (CenterNet) |
| `contour_extraction` | `ContourExtractionConfig` | `worker_contour` (SAM2) |
| `ocr` | `OcrConfig` | `worker_ocr` (Surya + PaddleOCR) |
| `classes[i]` | `ClassInfo` | Маппинг class_id ↔ name |

**ProjectConfig** агрегирует все вышеперечисленные dataclass'ы и предоставляет convenience-методы: `yolo` (property → default DetectionModelConfig), `num_classes`, `get_cvat_category_id(yolo_class_id)`, `get_class_name(cvat_category_id)`.

**ProjectLoader** — загрузчик с кэшированием. Ключевые методы: `load(project_code)` → `ProjectConfig | None`, `load_all()` → `List[ProjectConfig]`, `list_codes()` → `List[str]`. Глобальный singleton: `get_project_loader()`.

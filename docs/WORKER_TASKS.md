# WORKER_TASKS.md — Celery Tasks, Routing, Dispatch

**Аудитория:** DEV / OPS
**Версия:** 1.3
**Обновлено:** 2026-08-18
**Связанные документы:** ARCHITECTURE.md, STATUS_MACHINE.md, DB_SCHEMA.md, CONFIG_REFERENCE.md

---

## Оглавление

1. [Celery конфигурация](#1-celery-конфигурация)
2. [Dispatch и auto-chain](#2-dispatch-и-auto-chain)
3. [Общий протокол task'а](#3-общий-протокол-taskа)
4. [Task reference](#4-task-reference)
5. [db_helpers — утилиты worker'а](#5-db_helpers--утилиты-workerа)
6. [Error handling](#6-error-handling)

---

## 1. Celery конфигурация

**Файл:** `worker/celery_app.py`

### Broker и backend

Оба — Redis (один инстанс, разные назначения):

```
CELERY_BROKER_URL     = redis://localhost:6380/0   (env override)
CELERY_RESULT_BACKEND = redis://localhost:6380/0   (env override)
```

### Очереди

| Queue | Назначение | Worker container | GPU |
|-------|-----------|-----------------|-----|
| `default` + `gpu` + `sam2` | Detection (YOLO), segmentation (UNet++), junction (CenterNet), skeleton, graph, SAM2 contours | `pid_worker` | да |
| `ocr` | Surya OCR (3-pass tiling) | `pid_worker_ocr` | да |

**Примечание:** Контейнер `pid_worker` обслуживает три очереди (`-Q default,gpu,sam2`). Отдельных контейнеров для `default` и `gpu` нет — все задачи выполняются одним GPU-worker'ом.

### Task routing

```python
task_routes = {
    "worker.tasks.detection.*":    {"queue": "gpu"},
    "worker.tasks.segmentation.*": {"queue": "gpu"},
    "worker.tasks.skeleton.*":     {"queue": "default"},
    "worker.tasks.junction.*":     {"queue": "gpu"},
    "worker.tasks.graph.*":        {"queue": "default"},
    "worker.tasks.ocr.*":          {"queue": "ocr"},
    "worker.tasks.contours.*":     {"queue": "sam2"},
    "worker.tasks.layout.*":       {"queue": "default"},
}
```

**Важно:** API dispatch (`async_safe_dispatch()`) передаёт `queue=` явно, что **переопределяет** routing из `task_routes`. В частности, `task_build_graph` маршрутизируется в `default` через `task_routes`, но API всегда отправляет его в `queue="gpu"`. Фактически routing `graph.* → default` не используется — API-код является единственной точкой dispatch для graph tasks.

### Глобальные настройки

| Параметр | Значение | Комментарий |
|----------|----------|-------------|
| `task_serializer` | `json` | Все данные — JSON-сериализуемые |
| `task_time_limit` | 3600 (1 час) | Глобальный hard limit |
| `task_soft_time_limit` | 3300 (55 мин) | Глобальный soft limit |
| `broker_transport_options.visibility_timeout` | 7200 (2 часа) | Окно невидимости выданного сообщения у Redis: до его истечения брокер не выдаёт задачу второй раз |
| `task_acks_late` | `True` | Подтверждение после выполнения (не теряем task при crash) |
| `task_reject_on_worker_lost` | `True` | Возврат в очередь при гибели worker'а |
| `worker_prefetch_multiplier` | 1 | Для GPU: не берём лишних задач |
| `worker_concurrency` | 2 | По умолчанию; GPU worker'ы обычно 1 |

**Правило про `visibility_timeout` (пункт 1.2 · Б10).** Окно обязано быть **длиннее самого
длинного `time_limit`** (сейчас 5400 у детекции). При `task_acks_late=True` подтверждение уходит
брокеру только по завершении задачи, поэтому окно короче задачи означает гарантированную
повторную выдачу — две одинаковые задачи по одной диаграмме. Дефолт kombu — 3600, то есть был
в 1.5 раза короче детекции и ровно на границе у сегментации и OCR. Заводя задачу с бо́льшим
лимитом, поднимать и окно: инвариант заперт `tests/test_worker/test_broker_visibility.py`,
живая проверка на брокере — `tools/redelivery_bench.py --check`.

⛔ **Окно у флота — это минимум по живым потребителям, а не по конфигу.** Ключи `unacked`,
`unacked_index` и `unacked_mutex` у kombu **общие на всю базу Redis** (без суффикса очереди), а
восстановление режет просроченные по окну того канала, который сканирует. Отсюда два правила
эксплуатации:

- **менять окно → перезапускать ВСЕ воркер-контейнеры разом** (`pid_worker` и `pid_worker_ocr`;
  конфиг читается один раз при импорте). Частичный рестарт оставляет в флоте потребителя со
  старым окном 3600 в памяти — и он вернёт Б10 на 3601-й секунде, сколько бы ни стояло в файле;
- **новый потребитель брокера поднимается только на `worker.celery_app`** (или с теми же
  `broker_transport_options`). Служебный контейнер, поднятый на голом URL брокера — например
  `celery --broker=redis://redis:6379/0 flower`, — получает дефолт kombu 3600, и его цикл
  восстановления молча возвращает дубли всему флоту.

---

## 2. Dispatch и auto-chain

### Dispatch chain

Pipeline состоит из двух веток (auto-chain и manual UI confirmation):

```
UPLOAD
  └─ API dispatch ──────────────────────┐
                                        ▼
  detection ──auto──► segmentation ──auto──► skeleton
                                              │
                                    ┌─ STOP (UI: mask validation) ─┐
                                    ▼                               │
                               skeleton_simple ◄── UI confirm ─────┘
                                    │
                                    ▼ (auto-chain)
                              junction_detection
                                    │
                          ┌─ STOP (UI: junction validation) ─┐
                          ▼                                   │
                    API confirm  ◄── UI confirm ──────────────┘
                     │        │        │
                     ▼        ▼        ▼
              graph_build  contours   OCR     ← параллельно (все три)
                     │
           ┌─ STOP (UI: graph validation) ─┐
           ▼                                │
     API confirm  ◄── UI confirm ───────────┘
           │
           ▼
       FXML gen
           │
           ▼
        COMPLETED
```

**Auto-chain** (task чейнит следующий через `.delay()`):

- `segmentation` → `skeleton` (автоматически: `task_skeletonize.delay()`)

**Manual stops** (UI confirmation через API endpoint):

- `SKELETONIZED` → UI валидация масок → API `complete_mask_validation` → `task_skeletonize_simple`
- `DETECTED_JUNCTIONS` → UI валидация junction → API `complete_junction_validation` → `task_build_graph` + `task_extract_contours` + `task_run_ocr` (все три параллельно)
- `BUILT` → UI валидация графа → API `complete_graph_validation` → `task_generate_fxml`
- Закрытие этапа контуров (`POST /api/contours/{uid}/complete`, а также возврат после контуров и откат) → `app/services/layout_dispatch.py::dispatch_layout` → `task_run_layout` (§4.10; `DiagramStatus` не меняет — раскладка считается, пока оператор проходит «Привязку подписей»)

### Два механизма dispatch

| Контекст | Функция | Описание |
|----------|---------|----------|
| Worker (sync) | `safe_dispatch()` | Откатывает статус при ошибке dispatch |
| API (async) | `async_safe_dispatch()` | `asyncio.to_thread()` обёртка, не блокирует event loop; при ошибке — не трогает статус (пользователь retry через UI) |

---

## 3. Общий протокол task'а

Каждый task следует единому шаблону:

```python
@celery_app.task(bind=True, name="worker.tasks.X.task_Y",
                 max_retries=2, time_limit=1800, soft_time_limit=1740,
                 acks_late=True)
def task_Y(self, diagram_uid: str, project_code: str = "thermohydraulics"):
    db = SessionLocal()
    stage = None
    try:
        # 1. check_deleted() — прервать если диаграмма удалена
```

**Примечание:** Параметр `project_code` с default `"thermohydraulics"` используется в detection, segmentation, skeleton, junction и graph tasks. Задачи `task_run_ocr` и `task_extract_contours` принимают только `diagram_uid` и загружают `project_code` из БД через `diagram.project_code`.
        # 2. Idempotency — если статус уже дальше по pipeline → skip
        # 3. Status guard — если статус не тот (не DOING_X / ERROR) → skip
        # 4. start_stage() — создать ProcessingStage (RUNNING)
        # 5. Загрузка артефактов из storage
        # 6. Основная логика (inference / processing)
        # 7. Сохранение артефактов: upsert_artifact()
        # 8. Обновление статуса диаграммы
        # 9. complete_stage() с метриками
        # 10. db.commit()
        # 11. Auto-chain: next_task.delay() (если есть)
    except SoftTimeLimitExceeded:
        fail_stage(stage, "timed out")
        set_diagram_error(db, diagram_uid, "timed out", "stage_name")
        raise
    except Exception as exc:
        if self.request.retries < self.max_retries:
            fail_stage(stage, str(exc))
            raise self.retry(exc=exc)
        fail_stage(stage, str(exc))
        set_diagram_error(db, diagram_uid, str(exc), "stage_name")
        raise
    finally:
        db.close()
```

**Ключевые паттерны:**

- **check_deleted** — в начале каждого task'а; если `is_deleted=True` → return без ошибки
- **Idempotency** — проверка текущего статуса: если диаграмма уже прошла этот этап (статус дальше по pipeline), task пропускается
- **Status guard** — task работает только если статус = ожидаемый (`DOING_X`) или `ERROR` (retry)
- **ProcessingStage** — создаётся в начале, заполняется метриками в конце; при ошибке — `fail_stage()`
- **Retry** — при исключении: если retries < max_retries → `self.retry()` (без ERROR в БД); при исчерпании попыток → `set_diagram_error()`

---

## 4. Task reference

### 4.1 task_detect_yolo

**Файл:** `worker/tasks/detection.py`
**Celery name:** `worker.tasks.detection.task_detect_yolo`
**Queue:** `gpu`

| Параметр | Значение |
|----------|----------|
| `max_retries` | 2 |
| `time_limit` | 5400 (90 мин) |
| `soft_time_limit` | 5340 (89 мин) |
| Аргументы | `diagram_uid`, `project_code="thermohydraulics"`, `model_id=None` |

> Лимит поднят с 1800/1740 до 5400/5340 (`worker/tasks/detection.py:56-57`, коммит `a1e0e14`
> от 2026-06-22): ансамбль из 3 моделей с SAHI на CPU в получас не укладывается. Это
> перекрывает глобальный `task_time_limit` 3600 из §1 — здесь так и задумано.

**Статус:** `DETECTING` → `DETECTED`

**Логика:**

1. Загрузка project config и модели YOLO
2. `NodeDetector.detect()` с SAHI (tiled inference)
3. Per-class confidence фильтрация (из project config)
4. `resolve_overlaps()` — NMS для перекрывающихся детекций
5. Сохранение `yolo_predicted.txt`
6. Создание CVAT task + job + импорт аннотаций (non-fatal при ошибке CVAT)
7. `upsert_artifact(YOLO_PREDICTED)`

**Input артефакты:** `original/image.png`
**Output артефакты:** `detection/yolo_predicted.txt` (YOLO_PREDICTED)
**Auto-chain:** нет (DETECTED — manual stop для CVAT валидации)

### 4.2 task_segment_pipes

**Файл:** `worker/tasks/segmentation.py`
**Celery name:** `worker.tasks.segmentation.task_segment_pipes`
**Queue:** `gpu`

| Параметр | Значение |
|----------|----------|
| `max_retries` | 2 |
| `time_limit` | 3600 (60 мин) |
| `soft_time_limit` | 3540 (59 мин) |
| Аргументы | `diagram_uid`, `project_code` |

**Статус:** `SEGMENTING` → `SKELETONIZING`

**Логика:**

1. Генерация `node_mask` из `coco_validated.json` — все категории кроме `truba` и `annotation` (polygon → mask, RLE → mask, bbox fallback)
2. `EnsembleInference` — два UNet++ checkpoint'а (`weights_a` + `weights_b`), tiled inference с TTA
3. Сохранение `node_mask.png`, `pipe_mask.png`, `segmentation_overlay.png` (визуализация)
4. `upsert_artifact()` для NODE_MASK, PIPE_MASK, SEGMENTATION_OVERLAY
5. GPU memory cleanup: `engine.free_memory()` + `torch.cuda.empty_cache()`

**Input артефакты:** `original/image.png`, `detection/coco_validated.json`
**Output артефакты:** `segmentation/node_mask.png`, `segmentation/pipe_mask.png`, `segmentation/segmentation_overlay.png`
**Auto-chain:** `task_skeletonize.delay(diagram_uid, project_code)`

### 4.3 task_skeletonize

**Файл:** `worker/tasks/skeleton.py`
**Celery name:** `worker.tasks.skeleton.task_skeletonize`
**Queue:** `default`

| Параметр | Значение |
|----------|----------|
| `max_retries` | 2 |
| `time_limit` | 1800 (30 мин) |
| `soft_time_limit` | 1740 (29 мин) |
| Аргументы | `diagram_uid`, `project_code` |

**Статус:** `SKELETONIZING` → `SKELETONIZED`

**Логика:**

1. Skeleton Extension: skeletonize + directed lines + BFS + mask generation
2. Сохранение skeleton и mask артефактов

**Input артефакты:** `segmentation/pipe_mask.png`, `segmentation/node_mask.png`, `original/image.png`
**Output артефакты:** skeleton и mask файлы в `segmentation/`
**Auto-chain:** нет (`SKELETONIZED` — manual stop для UI валидации масок)

### 4.4 task_skeletonize_simple

**Файл:** `worker/tasks/skeleton.py`
**Celery name:** `worker.tasks.skeleton.task_skeletonize_simple`
**Queue:** `default`

| Параметр | Значение |
|----------|----------|
| `max_retries` | 1 |
| `time_limit` | 600 (10 мин) |
| `soft_time_limit` | 540 (9 мин) |
| Аргументы | `diagram_uid`, `project_code` |

**Статус:** `VALIDATED_MASKS` / `SKELETONIZING_FINAL` → `SKELETONIZED_FINAL`

**Логика:**

1. Скелетизация валидированной маски (`pipe_mask_validated.png`) через `skeleton_extension` в `simple_mode`
2. Этапы simple_mode: skeletonize → remove under nodes → protection mask → directed lines → BFS → remove orphans

**Input артефакты:** `segmentation/pipe_mask_validated.png`, `segmentation/node_mask.png`, `original/image.png`
**Output артефакты:** `skeleton/skeleton_final.png` (SKELETON_FINAL)
**Auto-chain:** `task_detect_junctions` через `send_task()` → queue: `gpu`

### 4.5 task_detect_junctions

**Файл:** `worker/tasks/junction.py`
**Celery name:** `worker.tasks.junction.task_detect_junctions`
**Queue:** `gpu`

| Параметр | Значение |
|----------|----------|
| `max_retries` | 2 |
| `time_limit` | 1200 (20 мин) |
| `soft_time_limit` | 1140 (19 мин) |
| Аргументы | `diagram_uid`, `project_code` |

**Статус:** `SKELETONIZED_FINAL` / `DETECTING_JUNCTIONS` → `DETECTED_JUNCTIONS`

**Логика:**

1. CenterNet inference (tiled + NMS)
2. Классификация: junction vs bridge
3. Сохранение junction/bridge JSON + визуализация

**Input артефакты:** `skeleton/skeleton_final.png`, `original/image.png`, `segmentation/node_mask.png`
**Output артефакты:** junction/bridge JSON файлы
**Auto-chain:** нет (`DETECTED_JUNCTIONS` — manual stop для UI валидации)

### 4.6 task_build_graph

**Файл:** `worker/tasks/graph.py`
**Celery name:** `worker.tasks.graph.task_build_graph`
**Queue:** `gpu` (API dispatch override; Celery routing = `default`, но API всегда передаёт `queue="gpu"`)

| Параметр | Значение |
|----------|----------|
| `max_retries` | 2 |
| `time_limit` | 1800 (30 мин) |
| `soft_time_limit` | 1740 (29 мин) |
| Аргументы | `diagram_uid` |

**Статус:** `BUILDING_GRAPH` / `VALIDATED_JUNCTIONS` → `BUILT`

**Логика:**

1. Graph builder pipeline: skeleton → labeled nodes → connectors → edges → waypoints → L-routing (см. MODULES.md)
2. Сохранение `graph.json` + визуализации

**Input артефакты:** `skeleton/skeleton_final.png`, `original/image.png`, `detection/coco_validated.json`, junction/bridge данные
**Output артефакты:** `graph/graph.json` (GRAPH_JSON), визуализации
**Auto-chain:** нет (`BUILT` — manual stop для UI валидации графа)

### 4.7 task_run_ocr

**Файл:** `worker/tasks/ocr.py`
**Celery name:** `worker.tasks.ocr.task_run_ocr`
**Queue:** `ocr`

| Параметр | Значение |
|----------|----------|
| `max_retries` | 1 |
| `time_limit` | 3600 (60 мин) |
| `soft_time_limit` | 3300 (55 мин) |
| Аргументы | `diagram_uid` |

**Статус:** Параллельная ветка — task устанавливает `OCR_PROCESSING` в начале и `OCR_COMPLETED` в конце, но эти статусы могут быть перезаписаны основной веткой (graph), поскольку `DiagramStatus` — единственное поле. Готовность OCR определяется по наличию артефакта `OCR_RESULT`, а не по значению `DiagramStatus`

**Логика:**

1. Загрузка OCR профиля из project config (динамический импорт)
2. Surya OCR 3-pass tiling (1x, tile2, tile3), merge, dedup
3. Сохранение OCR результата

**Input артефакты:** `original/image.png`, `segmentation/pipe_mask.png`
**Output артефакты:** `ocr/ocr_result.json` (OCR_RESULT)
**Auto-chain:** нет
**Особенность:** запускается параллельно с `task_build_graph` после `complete_junction_validation`

### 4.8 task_extract_contours

**Файл:** `worker/tasks/contours.py`
**Celery name:** `worker.tasks.contours.task_extract_contours`
**Queue:** `sam2`

| Параметр | Значение |
|----------|----------|
| `max_retries` | 1 |
| `time_limit` | 600 (10 мин) |
| `soft_time_limit` | 540 (9 мин) |
| Аргументы | `diagram_uid` |

**Статус:** не меняет `DiagramStatus` (idempotency по наличию артефакта CONTOURS_AUTO)

**Логика:**

1. Проверка `contour_extraction.enabled` в project config — если отключено, skip
2. Загрузка image + COCO + graph
3. Фильтрация eligible annotations (equipment с ann_idx, исключая skip_classes)
4. SAM2 inference (Hiera Small + LoRA, DT-max prompt, 6-channel input)
5. Smart snap постпроцессинг (Douglas-Peucker + H/V snapping)
6. Сохранение `contours_auto.json`

**Input артефакты:** `original/image.png`, `detection/coco_validated.json`, `graph/graph.json` или `graph/graph_validated.json`
**Output артефакты:** `contours/contours_auto.json` (CONTOURS_AUTO)
**Auto-chain:** нет

### 4.9 task_generate_fxml

**Файл:** `worker/tasks/graph.py`
**Celery name:** `worker.tasks.graph.task_generate_fxml`
**Queue:** `default`

| Параметр | Значение |
|----------|----------|
| `max_retries` | 1 |
| `time_limit` | 300 (5 мин) |
| `soft_time_limit` | 270 (4.5 мин) |
| Аргументы | `diagram_uid`, `page_size=None`, `bridge_gap=None` |

**Статус:** `VALIDATED_GRAPH` / `GENERATING_FXML` → `COMPLETED`

**Логика:**

1. Выбор входа: `graph_canvas.json` (холст «Ручной правки» — приоритет, если свеж по `canvas_state`) → `graph_validated.json` → `graph.json`. На холст зеркалируется текст из `graph_validated` (`text_import`) — холст считался до OCR.
2. Enrich контурами — **только для не-canvas входа**: `contours_validated.json` (только `polygon_validated`, IoU bbox > 0.5, equipment-узлы), fallback — legacy `contour_extractor`. Для холста пропускается целиком: контуры влиты при построении холста (`modules/graph/core/contours_merge.py`), а влив в пикселях растра против холста 1920x1080 давал бы IoU≈0.
3. Конвертация: `is_canvas` (есть `graph.canvas_transform`, либо `image_size == [1080, 1920]`) → `generate_canvas_fxml()` (`modules/canvas_to_fxml.py`, 1:1 без масштаба и без `fxml_standardize`); иначе → `generate_fxml()` (`modules/graph_to_fxml.py`; при `page_size='1920x1080'` — плюс `tools/fxml_standardize.py`). Детали различий — DATA_FORMATS.md §6.
4. Сохранение `fxml/diagram.fxml`

**Input артефакты:** `graph/graph_canvas.json` (приоритет) / `graph/graph_validated.json` / `graph/graph.json`; `contours/contours_validated.json` (не-canvas путь)
**Output артефакты:** `fxml/diagram.fxml` (FXML)
**Auto-chain:** нет (COMPLETED — финальный статус)

---

### 4.10 task_run_layout

**Файл:** `worker/tasks/layout.py`
**Celery name:** `worker.tasks.layout.task_run_layout`
**Queue:** `default`

| Параметр | Значение |
|----------|----------|
| `max_retries` | 0 (повтор не нужен — диспетчер поставит задачу заново) |
| `time_limit` | 1800 (30 мин; CPU-only, лимит с запасом — обязан быть перемерян на боевом железе) |
| `soft_time_limit` | 1500 (25 мин) |
| Аргументы | `diagram_uid`, `stage_id=None`, `dispatch_sha=None` |

**Статус:** `DiagramStatus` **не меняется** — операция идёт внутри одного этапа; прогресс отражает `ProcessingStage` типа `LAYOUT` (клиент читает `GET /{uid}/stages`).

**Логика:**

1. Загрузка `graph_validated.json`, sha-проекция входа (`canvas_state.graph_projection_sha`).
2. Влив выбранных SAM2-контуров (`modules/graph/core/contours_merge.py`): `polygon_validated` из `contours_validated.json` → `node["segmentation"]` (IoU bbox > 0.5, equipment-узлы), ещё в координатах растра. Строго в deepcopy графа: sha-проекция включает `segmentation`, а штамп (шаг 5) обязан считаться от исходного `graph_validated` — иначе холст рождается «устаревшим».
3. `to_canvas` → раскладка (`modules/graph/core/layout.py`).
4. Две защиты перед записью: (а) `graph_validated` перечитывается — sha сменился → результат выброшен (`stale_input`); (б) `operator_saved` на существующем холсте → результат выброшен (`operator_saved`).
5. `canvas_state.stamp` (от исходного `graph_validated`) + `stamp_contours` (метка `contours_merged_sha`), атомарная запись (`tmp + os.replace`).

Запускается диспетчером `app/services/layout_dispatch.py` при закрытии этапа контуров; диспетчер владеет идемпотентностью и revoke: не ставит задачу, если холст свеж и по `source_sha`, и по `contours_merged_sha`, и перезапускает раскладку, если контуры переиграны (`contours_are_stale`).

**Input артефакты:** `graph/graph_validated.json`; `contours/contours_validated.json` (опционально)
**Output артефакты:** `graph/graph_canvas.json` (GRAPH_CANVAS)
**Auto-chain:** нет

---

## 5. db_helpers — утилиты worker'а

**Файл:** `worker/utils/db_helpers.py`

| Функция | Назначение |
|---------|-----------|
| `set_diagram_error(db, uid, message, stage)` | Поставить `DiagramStatus.ERROR` + `error_message` + `error_stage`. Единая реализация для всех tasks (вместо дублирования `_set_error`) |
| `check_deleted(db, uid)` | Вернуть `True` если диаграмма не найдена или `is_deleted=True`. Вызывается в начале каждого task'а |
| `upsert_artifact(db, uid, type, path, storage_base, mime)` | Создать или обновить артефакт в БД. Безопасен при retry — не создаёт дубли (ищет по `diagram_uid + artifact_type`) |
| `safe_dispatch(db, diagram, task_name, args, fallback_status)` | Отправить Celery task из worker'а. При ошибке: откатывает статус на `fallback_status` (или ERROR) |
| `start_stage(db, uid, stage_type, celery_task_id)` | Создать `ProcessingStage` запись, пометить RUNNING. Считает `attempt` автоматически |
| `complete_stage(stage, metrics)` | Пометить stage как COMPLETED с метриками (dict) |
| `fail_stage(stage, error, tb)` | Пометить stage как FAILED с error message и traceback |

### ProcessingStage tracking

Каждый запуск task'а создаёт запись в `processing_stages`:

```python
stage = start_stage(db, diagram_uid, StageType.DETECTION, celery_task_id=self.request.id)
# ... processing ...
complete_stage(stage, {"detection_count": 42, "model": "yolov8m"})
# или при ошибке:
fail_stage(stage, "Out of memory", traceback.format_exc())
```

`attempt` считается автоматически (количество существующих записей + 1 для данного `diagram_uid + stage_type`).

---

## 6. Error handling

### Retry flow

```
Exception → retries < max_retries?
  ├─ YES → fail_stage() + self.retry(exc=exc)
  │         (ProcessingStage=FAILED, DiagramStatus не меняется)
  │         Celery ставит task обратно в очередь через default_retry_delay (60с)
  │
  └─ NO  → fail_stage() + set_diagram_error()
            (ProcessingStage=FAILED, DiagramStatus=ERROR)
            Task завершается с исключением
```

### SoftTimeLimitExceeded

Обрабатывается отдельно — без retry (timeout обычно детерминированный, retry не поможет):

```python
except SoftTimeLimitExceeded:
    fail_stage(stage, "timed out")
    set_diagram_error(db, uid, "timed out", "stage_name")
    raise  # Celery запишет как FAILURE
```

### Idempotency

Каждый task проверяет в начале:

1. **check_deleted()** — диаграмма существует и не удалена
2. **Status forward** — если диаграмма уже дальше по pipeline (статус > ожидаемого), task пропускается с `{"status": "already_completed"}`
3. **Status guard** — если статус не совпадает с ожидаемым (не `DOING_X` и не `ERROR`), task пропускается с `{"status": "skipped"}`
4. **Артефакт exists** (OCR, contours) — если артефакт уже создан, skip

Это гарантирует безопасность при повторной отправке задач (retry, manual re-dispatch через UI).

### GPU memory cleanup

GPU-задачи (segmentation, contours) выполняют cleanup после inference:

```python
engine.free_memory()
del engine
if device == "cuda":
    torch.cuda.empty_cache()
```

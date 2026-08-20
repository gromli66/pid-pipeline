# API.md — Справочник REST API

**Аудитория:** DEV
**Версия:** 1.1
**Обновлено:** 2026-04-09
**Связанные документы:** [ARCHITECTURE.md](ARCHITECTURE.md), [STATUS_MACHINE.md](STATUS_MACHINE.md), [DATA_FORMATS.md](DATA_FORMATS.md)

---

## Содержание

1. [Общее](#1-общее)
2. [Projects](#2-projects)
3. [Diagrams](#3-diagrams)
4. [Detection](#4-detection)
5. [CVAT](#5-cvat)
6. [Segmentation & Skeleton & Junction](#6-segmentation--skeleton--junction)
7. [Validation (masks, junctions, graph)](#7-validation)
8. [Graph](#8-graph)
9. [Contours](#9-contours)
10. [OCR](#10-ocr)
11. [Rollback](#11-rollback)

---

## 1. Общее

**Base URL:** `http://localhost:8000`
**Swagger UI:** `http://localhost:8000/docs`
**Аутентификация:** Отсутствует (внутренний сервис, desktop UI).
**CORS:** `allow_origins=["*"]`, `allow_credentials=False`.

### Health check

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | Проверка API, PostgreSQL, Redis/Celery |

```bash
curl http://localhost:8000/health
# {"status": "healthy", "checks": {"api": "healthy", "database": "healthy", "redis": "healthy"}}
```

### Общие статус-коды

| Код | Значение |
|-----|----------|
| 200 | OK |
| 400 | Невалидный запрос / недопустимый статус |
| 404 | Ресурс не найден |
| 409 | Конфликт (дубликат имени файла) |
| 413 | Файл слишком большой (>200MB) |
| 503 | Worker недоступен |

---

## 2. Projects

**Prefix:** `/api/projects`

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/` | Список проектов (синхронизация YAML → БД) |
| GET | `/summary` | Краткий список для UI (код, имя, кол-во диаграмм) |
| GET | `/{project_code}` | Проект по коду |
| GET | `/{project_code}/config` | Конфигурация проекта из YAML |
| GET | `/{project_code}/classes` | Список классов оборудования |
| GET | `/{project_code}/detection-models` | Доступные модели детекции |

```bash
# Список проектов
curl http://localhost:8000/api/projects/summary

# Классы проекта
curl http://localhost:8000/api/projects/thermohydraulics/classes

# Модели детекции
curl http://localhost:8000/api/projects/thermohydraulics/detection-models
```

---

## 3. Diagrams

**Prefix:** `/api/diagrams`

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/upload` | Загрузить диаграмму (multipart: file + project_code) |
| GET | `/` | Список диаграмм (query: skip, limit, project_code, status) |
| GET | `/{uid}` | Диаграмма по UID |
| GET | `/{uid}/status` | Статус для polling (легковесный) |
| GET | `/{uid}/download/{artifact_type}` | Скачать артефакт (FileResponse) |
| DELETE | `/{uid}` | Soft delete диаграммы |
| POST | `/{uid}/retry` | Повтор операции после ошибки |
| POST | `/{uid}/reupload-original` | Перезагрузить оригинальное изображение |

### Upload

```bash
curl -X POST http://localhost:8000/api/diagrams/upload \
  -F "file=@diagram_001.png" \
  -F "project_code=thermohydraulics"
# {"uid": "a1b2c3d4-...", "number": 1, "project_code": "thermohydraulics", "status": "uploaded"}
```

Ограничения: файл ≤ 200MB, допустимые типы: `image/png`, `image/jpeg`, `image/tiff`. Имя файла уникально в рамках проекта (409 при дубликате).

### Status polling

```bash
curl http://localhost:8000/api/diagrams/{uid}/status
# {"status": "detecting", "error_message": null, "error_stage": null, "detection_count": null, ...}
```

### Download артефакта

```bash
curl -o graph.json http://localhost:8000/api/diagrams/{uid}/download/graph_json
curl -o pipe_mask.png http://localhost:8000/api/diagrams/{uid}/download/pipe_mask
```

`artifact_type` — любое значение из `ArtifactType` enum (см. [ARCHITECTURE.md §6](ARCHITECTURE.md#6-артефакты-pipeline)).

### Retry после ошибки

```bash
curl -X POST http://localhost:8000/api/diagrams/{uid}/retry
# {"status": "validated_bbox", "message": "Status reset to validated_bbox"}
```

**Precondition:** `status == error` (иначе 400; диаграммы нет — 404).
**Transition:** `error → <статус перед упавшим этапом>` по карте `error_stage → статус`;
`error_message`/`error_stage` снимаются, артефакты НЕ удаляются. Полная карта, её дефолт
и условие, при котором клиент вообще сюда ходит, —
[STATUS_MACHINE.md §5](STATUS_MACHINE.md#выход-из-тупика-post-apidiagramsuidretry).

---

## 4. Detection

**Prefix:** `/api/detection`

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/{uid}/detect` | Запустить YOLO детекцию |
| POST | `/{uid}/retry` | Retry детекции после ошибки |

### Start detection

```bash
curl -X POST "http://localhost:8000/api/detection/{uid}/detect"
# С выбором модели:
curl -X POST "http://localhost:8000/api/detection/{uid}/detect?model_id=yolov8m_v2"
```

| Параметр | Тип | Описание |
|----------|-----|----------|
| `uid` (path) | UUID | UID диаграммы |
| `model_id` (query) | string, optional | ID модели детекции (default из конфига проекта) |

**Precondition:** `status == frame_cleaned` или `error + error_stage == detecting`.
**Transition:** `frame_cleaned → detecting`, `error → detecting` (повтор после падения:
сюда ведёт красная кнопка «🔄 Поиск элементов» — `/{uid}/retry` из UI не вызывается).

**Отправка задачи не удалась (брокер лёг): 503**, а состояние возвращается таким, каким было
до вызова, — вместе с `error_message`/`error_stage`. Диаграмма не остаётся в `detecting`,
поэтому повтор после подъёма брокера проходит. Правило общее для всех эндпоинтов запуска
этапов, см. [STATUS_MACHINE.md §5](STATUS_MACHINE.md#5-error-handling).

---

## 5. CVAT

**Prefix:** `/api/cvat`

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/{uid}/create-task` | Создать CVAT task и загрузить изображение + аннотации |
| POST | `/{uid}/open-validation` | Открыть валидацию: `detected → validating_bbox` |
| POST | `/{uid}/fetch-annotations` | Скачать аннотации из CVAT → `coco_validated.json` |
| GET | `/{uid}/cvat-url` | Получить URL CVAT для открытия в WebView |
| POST | `/{uid}/retry-fetch` | Повторить загрузку аннотаций |

### Типичный flow

1. `POST /{uid}/create-task` — создаёт CVAT task, загружает изображение и COCO predicted annotations.
2. Оператор редактирует bbox в CVAT WebView.
3. `POST /{uid}/fetch-annotations` — скачивает аннотации из CVAT → `coco_validated.json`, переводит в `validated_bbox`.

---

## 6. Segmentation & Skeleton & Junction

### Segmentation

**Prefix:** `/api/segmentation`

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/{uid}/segment` | Запустить цепочку: segmentation → skeleton #1 |

**Precondition:** `status == validated_bbox` (полный запуск) или `status == error` (smart retry — определяет `error_stage` и перезапускает с нужного шага: `segmenting` / `skeletonizing` / `detecting_junctions`).
**Transition:** → `segmenting` (полный запуск и все стадии, кроме двух ниже), → `skeletonizing`
(`error_stage == skeletonizing`), → `detecting_junctions` (`error_stage == detecting_junctions`).
`error` пускается с ЛЮБЫМ `error_stage`: гейт судит только статус.

### Skeleton

**Prefix:** `/api/skeleton`

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/{uid}/skeletonize` | Ручной запуск/retry скелетизации |

**Precondition:** `status ∈ {segmenting, validated_masks}` или `error + error_stage == skeletonizing`.
**Transition:** → `skeletonizing`. ⚠ Из клиента не вызывается ни разу: метод
`api_client.start_skeletonization` есть, вызывающих у него нет. Это единственный эндпоинт,
принимающий `validated_masks`.

### Junction

**Prefix:** `/api/junction`

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/{uid}/detect-junctions` | Ручной запуск/retry CenterNet |

**Precondition:** `status == skeletonized_final` или `error + error_stage == detecting_junctions`.
**Transition:** → `detecting_junctions`. ⚠ У клиента нет даже метода для этого пути: детекция
перекрёстков доезжает авто-цепочкой из `task_skeletonize_simple` и через `app/api/validation.py`.

**Все три эндпоинта: отправка задачи не удалась (брокер лёг) — 503**, состояние возвращается
таким, каким было до вызова, вместе с `error_message`/`error_stage`; в `*ING`-статусе диаграмма
не остаётся. См. [STATUS_MACHINE.md §5](STATUS_MACHINE.md#5-error-handling).

---

## 7. Validation

**Prefix:** `/api/validation`

### Mask validation (pipe)

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/{uid}/masks/start` | `skeletonized → validating_masks` |
| POST | `/{uid}/masks/upload` | Загрузить валидированную маску (multipart: mask_type + file) |
| POST | `/{uid}/masks/complete` | Завершить → `validated_masks`, auto-dispatch skeleton #2 |
| POST | `/{uid}/nodes/update` | Обновить COCO + перегенерировать node_mask |

**mask_type:** `pipe_mask_validated`, `junction_mask_validated`, `bridge_mask_validated`, `junction_points_validated`.

`junction_points_validated` — не маска, а JSON с центрами квадратов
(`junction/points_validated.json`, формат — см. [DATA_FORMATS.md](DATA_FORMATS.md));
едет тем же эндпоинтом, но с `Content-Type: application/json` (для остальных
типов ожидается `image/png`).

### Junction validation

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/{uid}/junctions/start` | `detected_junctions → validating_junctions` |
| POST | `/{uid}/junctions/complete` | Завершить → `validated_junctions`, auto-dispatch graph + OCR |

**SAM2-контуры отсюда НЕ запускаются** (давно): `contour_task_id` в ответе всегда `null`,
контуры считаются поточечно из своей вкладки через `POST /api/contours/{uid}/extract`.

### Graph validation

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/{uid}/graph/start` | `built → validating_graph` |
| POST | `/{uid}/graph/save` | Загрузить `graph_validated.json` |
| POST | `/{uid}/graph/complete-simple` | Завершить simple-валидацию → `validated_graph`, auto-dispatch OCR |
| POST | `/{uid}/graph/complete` | Завершить полную валидацию → auto-dispatch FXML |

**Разница complete-simple vs complete:** `complete-simple` запускает OCR (flow: граф → simple val → OCR → binding → advanced editor → FXML). `complete` запускает FXML напрямую (финальный шаг после advanced editor).

**Примечание:** OCR обычно уже запущен параллельно с graph build из `complete_junction_validation` (см. [§7 Validation](#7-validation)). Dispatch OCR из `complete-simple` — **idempotent safety net**: task проверяет наличие артефакта `OCR_RESULT` и пропускает выполнение, если OCR уже завершён.

### Отказ отправки задачи — 503 у всех четырёх `complete`

`masks/complete`, `junctions/complete`, `graph/complete-simple`, `graph/complete` ставят
задачу ПОСЛЕ коммита статуса. Брокер лёг между двумя действиями — эндпоинт **возвращает
состояние, каким оно было до вызова** (статус и оба поля ошибки), и отвечает **503**;
`event=dispatch_failed` в логе несёт `uid` и точку возврата. Точка возврата лежит внутри
`Precondition` того же эндпоинта, поэтому повтор после подъёма брокера проходит.
Модель отказа, классы исключений и заявленные границы — [STATUS_MACHINE.md §5](STATUS_MACHINE.md).

⚠ **`task_id: null` в ответе 200 значит ровно одно — «цепочка уже ушла вперёд»** (идемпотентный
повтор). Раньше тем же ответом отвечал и мёртвый брокер, и различить их клиенту было нечем.

⚠ **Исключение — параллельный OCR в `junctions/complete`:** его отказ НЕ откатывает ничего
(сборка графа уже в брокере) — он назван в `message` и в `ocr_task_id: null`, а восстановление
даёт safety net из `complete-simple` выше.

---

## 8. Graph

**Prefix:** `/api/graph`

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/{uid}/build` | Запустить построение графа |
| GET | `/{uid}/result` | Результат: node_count, edge_count, артефакты |
| POST | `/{uid}/generate-fxml` | Запустить генерацию FXML |

### Build Graph

**Precondition:** `status` ∈ {`validated_junctions`, `built`, `error`} — нормальный путь
и оба повтора. **Transition:** → `building_graph`.

Особые исходы:

| исход | ответ | что со статусом |
|-------|-------|-----------------|
| `status == building_graph` | 200, `already in progress` | не меняется, задача не отправляется повторно |
| нет артефакта `SKELETON_FINAL` | 200, `skeletonizing` | не меняется; автоматически запускается `task_skeletonize_simple` |
| отправка задачи не удалась (брокер лёг) | 503 | **возвращается таким, каким был до вызова** — вместе с `error_message`/`error_stage` |

Откат на 503 восстанавливает пред-вызовное состояние целиком, а не сваливает диаграмму
в фиксированный статус: работа не начиналась, и любая другая точка отката вывела бы
её из `Precondition` этого же эндпоинта — повтор отвечал бы 400 навсегда.

### Generate FXML

```bash
curl -X POST "http://localhost:8000/api/graph/{uid}/generate-fxml?page_size=A3"
```

| Параметр | Тип | Описание |
|----------|-----|----------|
| `page_size` (query) | string, optional | `A0`/`A1`/`A2`/`A3`/`A4` (landscape) или null (оригинальные пиксели) |

---

## 9. Contours

**Prefix:** `/api/contours`

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/{uid}/status` | Статус контуров: `has_auto`, `has_validated`, `stats` |
| GET | `/{uid}/auto` | Скачать `contours_auto.json` |
| GET | `/{uid}/validated` | Скачать `contours_validated.json` |
| PUT | `/{uid}/validated` | Загрузить `contours_validated.json` |
| POST | `/{uid}/auto-accept` | Auto-accept: `polygon_auto → polygon_validated` для всех |
| POST | `/{uid}/complete` | Завершить валидацию → `contours_validated` |

---

## 10. OCR

**Prefix:** `/api/ocr`

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/{uid}/start` | Запустить/перезапустить OCR |
| GET | `/{uid}/status` | Статус OCR: `has_ocr_result`, `has_binding` |
| GET | `/{uid}/result` | Скачать `ocr_result.json` |
| PUT | `/{uid}/result` | Обновить `ocr_result.json` |
| POST | `/{uid}/binding/save` | Сохранить `ocr_binding.json` |
| GET | `/{uid}/binding` | Скачать `ocr_binding.json` |
| POST | `/{uid}/binding/apply` | Применить binding к графу (записать kks/diameter в graph_validated) |
| POST | `/{uid}/validation/save` | Сохранить `ocr_validation.json` |
| GET | `/{uid}/validation` | Скачать `ocr_validation.json` |

### Start OCR

```bash
curl -X POST http://localhost:8000/api/ocr/{uid}/start
```

OCR работает параллельно с графом. Не меняет `DiagramStatus` — готовность определяется наличием артефакта `OCR_RESULT`.

### Apply binding

```bash
curl -X POST http://localhost:8000/api/ocr/{uid}/binding/apply
```

Читает `ocr_binding.json` и записывает `kks_full` в узлы, `diameter_text`/`diameter_value` в рёбра `graph_validated.json`.

---

## 11. Rollback

**Prefix:** `/api/diagrams`

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/{uid}/rollback` | Откатить диаграмму до указанного этапа |

```bash
curl -X POST "http://localhost:8000/api/diagrams/{uid}/rollback?target_status=detected&preserve_ocr=true&preserve_contours=true"
```

| Параметр | Тип | Описание |
|----------|-----|----------|
| `target_status` (query) | string, required | Целевой статус (из `_STAGE_ORDER`) |
| `preserve_ocr` (query) | bool, default false | Сохранить OCR артефакты |
| `preserve_contours` (query) | bool, default false | Сохранить contour артефакты |

Удаляет из БД артефакты всех этапов после target. Устанавливает `status = target`, очищает `error_message` и `error_stage`. Подробнее — см. [STATUS_MACHINE.md §4](STATUS_MACHINE.md#4-rollback-system).

---

## Новые термины для GLOSSARY.md

| Термин | Описание |
|--------|----------|
| **smart retry** | Логика в `start_segmentation()`: по `error_stage` определяет, с какого шага перезапустить цепочку |
| **advisory lock** | `pg_advisory_xact_lock` в PostgreSQL — предотвращает race condition при генерации номера диаграммы |
| **complete-simple vs complete** | Два endpoint завершения валидации графа: simple → OCR, complete → FXML |

---

## Реестр документации (обновление)

| # | Документ | Статус | Версия | Чат |
|---|---------|--------|--------|-----|
| 3 | `ARCHITECTURE.md` | ✅ Done | 1.0 | 2 |
| 4 | `STATUS_MACHINE.md` | ✅ Done | 1.0 | 3 |
| 5 | `DB_SCHEMA.md` | ✅ Done | 1.0 | 3 |
| 6 | `DATA_FORMATS.md` | ✅ Done | 1.0 | 4 |
| 7 | `API.md` | ✅ Done | 1.0 | 5 |

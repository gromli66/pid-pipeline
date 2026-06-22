# CVAT.md

**Аудитория:** DEV / ALL
**Версия:** 1.1
**Обновлено:** 2026-06-17
**Связанные документы:** ARCHITECTURE.md, STATUS_MACHINE.md, API.md, DEPLOYMENT.md

---

## Оглавление

1. [Что такое CVAT и зачем](#1-что-такое-cvat-и-зачем)
2. [Архитектура интеграции](#2-архитектура-интеграции)
3. [Жизненный цикл](#3-жизненный-цикл)
4. [API endpoints](#4-api-endpoints)
5. [CVAT Client](#5-cvat-client)
6. [Экспорт в CVAT](#6-экспорт-в-cvat)
7. [Импорт из CVAT](#7-импорт-из-cvat)
8. [UI: CvatTab](#8-ui-cvattab)
9. [Форматы данных](#9-форматы-данных)

---

## 1. Что такое CVAT и зачем

CVAT (Computer Vision Annotation Tool) — open-source инструмент для разметки изображений. В P&ID Pipeline используется как **визуальный редактор для валидации bbox-детекций**: после автоматической детекции узлов (YOLO+SAHI) пользователь может исправить ошибки — удалить ложные детекции, добавить пропущенные, скорректировать bbox — в удобном web-интерфейсе.

Роль CVAT в pipeline: этап между `detected` и `validated_bbox` (см. STATUS_MACHINE.md). Без CVAT pipeline продолжит обработку с автоматическими детекциями, но качество downstream-этапов (граф, OCR-привязка) зависит от точности bbox.

---

## 2. Архитектура интеграции

```
┌─────────────┐      ┌──────────────────┐      ┌────────────┐
│ P&ID API    │─────▶│  CVATClient      │─────▶│ CVAT API   │
│ (app/api/   │      │  (cvat_client.py)│      │ (v2.25.0)  │
│  cvat.py)   │      └──────────────────┘      └────────────┘
└──────┬──────┘                                      │
       │                                             │
       │      ┌──────────────────┐                   │
       │      │  CVATExporter    │                   │
       │      │  (cvat_export.py)│                   │
       │      └──────────────────┘                   │
       │                                             │
┌──────▼──────┐                              ┌───────▼──────┐
│ UI:CvatTab  │◀─────── QWebEngineView ─────▶│ CVAT UI      │
│(cvat_tab.py)│                              │ (port 8080)  │
└─────────────┘                              └──────────────┘
```

Файлы:

| Файл | Назначение |
|------|-----------|
| `app/services/cvat_client.py` | HTTP-клиент для CVAT API v2 (синхронный, httpx) |
| `app/services/cvat_export.py` | Конвертация детекций в YOLO 1.1 / COCO 1.0 для импорта в CVAT |
| `app/api/cvat.py` | FastAPI endpoints: create-task, open-validation, fetch-annotations |
| `ui/tabs/cvat_tab.py` | PySide6 вкладка со встроенным CVAT (QWebEngineView) |

---

## 3. Жизненный цикл

Полный цикл валидации bbox через CVAT:

```
detected ──▶ create-task ──▶ open-validation ──▶ CVAT (редактирование)
                                                       │
validated_bbox ◀── fetch-annotations ◀── confirm ◀─────┘
```

**Шаг 1: Создание CVAT task** (`POST /cvat/{uid}/create-task`). Автоматически при переходе в `detected`. Создаёт project в CVAT (если нет), task с изображением схемы, импортирует YOLO-аннотации из `yolo_predicted.txt`.

**Шаг 2: Открытие валидации** (`POST /cvat/{uid}/open-validation`). Переводит статус `detected` → `validating_bbox`. Возвращает URL CVAT для открытия в браузере.

**Шаг 3: Редактирование в CVAT.** Пользователь правит bbox в CVAT UI (встроенном в CvatTab или в отдельном браузере).

**Шаг 4: Получение аннотаций** (`POST /cvat/{uid}/fetch-annotations`). Экспортирует аннотации из CVAT в COCO 1.0, парсит, сохраняет `coco_validated.json` и `yolo_validated.txt`, переводит статус → `validated_bbox`.

---

## 4. API endpoints

Все endpoints в роутере `app/api/cvat.py`, префикс `/cvat`.

| Endpoint | Метод | Описание |
|----------|-------|----------|
| `/{uid}/create-task` | POST | Создать CVAT task + импортировать детекции |
| `/{uid}/open-validation` | POST | Перевести в validating_bbox, вернуть URL |
| `/{uid}/fetch-annotations` | POST | Скачать аннотации из CVAT → validated_bbox |
| `/{uid}/cvat-url` | GET | Получить URL CVAT task |
| `/{uid}/retry-fetch` | POST | Повторить fetch после ошибки (error → validating_bbox) |

**create-task** — идемпотентен: если `cvat_task_id` уже есть, возвращает существующий. Создаёт project через `get_or_create_project()` (по `cvat_project_name` из конфига). Labels из `project_config.classes`.

**fetch-annotations** — долгая операция (экспорт из CVAT может занять до 2 минут). Выполняется через `asyncio.to_thread()` вне DB-транзакции. При ошибке — статус → `error`, `error_stage = "fetching_annotations"`.

---

## 5. CVAT Client

Файл: `app/services/cvat_client.py`. Класс `CVATClient` — синхронный HTTP-клиент для CVAT API v2 на базе httpx с persistent connection pooling.

Подключение: `CVAT_URL` (для серверных API-запросов, используется в `CVATClient`). `CVAT_BROWSER_URL` (для формирования URL, открываемых в UI) — используется как в API-слое (`app/api/cvat.py`, функция `_get_cvat_browser_url()`), так и в `CVATClient` для task URL. Авторизация: `CVAT_TOKEN` (приоритет) или login по username/password.

Ключевые методы:

| Метод | Описание |
|-------|----------|
| `get_or_create_project(name, labels)` | Найти project по имени или создать новый |
| `create_task(project_id, name, image_path)` | Создать task + загрузить изображение + дождаться job |
| `import_annotations(task_id, zip_path, format)` | Импортировать аннотации (YOLO 1.1 ZIP) |
| `export_annotations(task_id, format, output)` | Экспортировать аннотации (COCO 1.0 ZIP) |
| `get_task_url(task_id, job_id)` | URL для браузера |

Singleton: `get_cvat_client()` — модульный singleton с переиспользованием соединений.

**Загрузка изображения (большие цветные схемы).** `create_task()` отправляет оригинал одним файлом в `POST /api/tasks/{id}/data` с параметрами `image_quality=70` и `use_cache=true`. `use_cache` включает ленивую генерацию чанков (по запросу, а не вся пирамида сразу при создании task) — для схем ~15000×7000 это резко ускоряет создание задачи. `image_quality` (0–100) задаёт качество JPEG-перекодирования для просмотра в редакторе: 70 — компромисс в пользу скорости; для более чёткого мелкого текста/линий поднять до 90–95 (цена — крупнее чанки, дольше первая загрузка).

---

## 6. Экспорт в CVAT

Файл: `app/services/cvat_export.py`. Класс `CVATExporter` — конвертация детекций pipeline в форматы CVAT.

**Поддерживаемые форматы:**

**YOLO 1.1** (основной для импорта аннотаций). ZIP-архив со структурой:

```
obj.data          # classes=40, paths
obj.names         # имена классов (по строке)
train.txt         # data/obj_train_data/{image}
obj_train_data/
  {image_stem}.txt  # class_id x_center y_center width height
```

**COCO 1.0** (используется при экспорте из CVAT). ZIP-архив со структурой:

```
annotations/
  instances_default.json  # стандартный COCO JSON
```

**Class mapping.** `CVATExporter` принимает `class_mapping: Dict[int, int]` для трансляции YOLO class_id → CVAT class_id. Создание из конфига: `create_exporter_from_config(project_config)`.

---

## 7. Импорт из CVAT

Функция `_fetch_cvat_annotations_sync()` в `app/api/cvat.py`:

1. `CVATClient.export_annotations(task_id, format="COCO 1.0")` → ZIP
2. Распаковка → поиск JSON
3. `parse_coco_annotations(coco_json)` → список аннотаций + category_map
4. Сохранение `coco_validated.json` (полный COCO) и `yolo_validated.txt` (YOLO формат)
5. Создание артефактов `COCO_VALIDATED` и `YOLO_VALIDATED` в БД

**parse_coco_annotations** конвертирует COCO bbox `[x, y, w, h]` (абсолютные) в YOLO normalized `[x_center, y_center, width, height]`. Category_id: COCO может быть как 0-based, так и 1-based (зависит от источника/экспортёра CVAT) → YOLO 0-based. Код использует `cat_map` без жёсткой проверки базы индексации.

---

## 8. UI: CvatTab

Файл: `ui/tabs/cvat_tab.py`. Вкладка со встроенным CVAT через `QWebEngineView`.

Компоненты: toolbar (кнопки "Обновить" и "Подтвердить валидацию"), WebView с загруженным URL CVAT task, status label.

**Процесс подтверждения.** По нажатию "Подтвердить валидацию" — CvatTab инжектирует JavaScript для симуляции Ctrl+S в CVAT (принудительное сохранение), ждёт 5 секунд, эмитит сигнал `confirmed`. Workspace затем вызывает `fetch-annotations` через API.

Сигналы: `confirmed` — валидация подтверждена, `status_message(str)` — для статусбара.

**Рендеринг встроенного редактора.** `QWebEngineView` чувствителен к GPU-композитингу: на Windows дефолтный бэкенд ANGLE/D3D11 даёт мигание. В точке входа `ui/main.py` (до создания `QApplication`) выставляются `AA_ShareOpenGLContexts` и нативный desktop-OpenGL-бэкенд — это убирает мигание, сохраняя аппаратное ускорение. Бэкенд переопределяется переменной окружения `PID_GL_BACKEND` (`desktop`/`gles`/`software`), без правки кода — см. TROUBLESHOOTING.md §13.

---

## 9. Форматы данных

### Артефакты pipeline → CVAT (экспорт)

| Артефакт | Путь | Формат | Описание |
|----------|------|--------|----------|
| `yolo_predicted.txt` | `{uid}/detection/` | YOLO txt | Автодетекции для импорта в CVAT |

### CVAT → артефакты pipeline (импорт)

| Артефакт | Путь | Формат | Описание |
|----------|------|--------|----------|
| `coco_validated.json` | `{uid}/detection/` | COCO JSON | Валидированные bbox (полный COCO) |
| `yolo_validated.txt` | `{uid}/detection/` | YOLO txt | Валидированные bbox (YOLO формат) |

### YOLO txt формат

```
class_id x_center y_center width height
```

Координаты нормализованы 0-1. `class_id` 0-based (YOLO). Один объект — одна строка.

### COCO JSON (ключевые поля)

```
images[0].width, images[0].height  — размеры изображения
categories[i].id (1-based), categories[i].name
annotations[i].bbox = [x, y, w, h]  — абсолютные координаты
annotations[i].category_id (1-based)
```

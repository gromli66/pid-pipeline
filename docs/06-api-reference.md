# 6. API Reference

## 6.1 Обзор

**Base URL:** `http://localhost:8000`

**Документация:**
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

**Формат:** JSON

---

## 6.2 Health Endpoints

### GET /health

Проверка здоровья API и всех зависимостей.

**Response:**
```json
{
    "status": "healthy",
    "checks": {
        "api": "healthy",
        "database": "healthy",
        "redis": "healthy"
    }
}
```

**Возможные значения status:** `healthy` (всё работает), `degraded` (одна или более зависимостей недоступна).

**Возможные значения checks:** `healthy`, `unhealthy: <причина>`, `no workers` (Redis доступен, но Celery workers не запущены).

---

### GET /

Корневой endpoint.

```json
{"name": "P&ID Pipeline API", "version": "1.0.0", "docs": "/docs"}
```

---

## 6.3 Projects API

### GET /api/projects/

Получить список всех проектов.

**Response:**
```json
{
    "items": [
        {
            "code": "thermohydraulics",
            "name": "Термогидравлика",
            "cvat_project_id": null,
            "cvat_project_name": "P&ID Термогидравлика",
            "config_path": "configs/projects/thermohydraulics.yaml",
            "created_at": "2026-02-04T12:00:00",
            "updated_at": "2026-02-04T12:00:00"
        }
    ],
    "total": 1
}
```

### GET /api/projects/summary

Краткий список для выбора в UI.

### GET /api/projects/{project_code}

Получить проект по коду.

### GET /api/projects/{project_code}/config

Получить конфигурацию из YAML.

### GET /api/projects/{project_code}/classes

Получить список классов проекта.

---

## 6.4 Diagrams API

### POST /api/diagrams/upload

Загрузить новую диаграмму.

**Request:** `multipart/form-data`
- `file` — изображение (PNG, JPEG, TIFF), **макс. 200 MB**
- `project_code` — код проекта (обязательно)

**Валидация:**
- Chunked проверка размера файла (без загрузки всего в RAM)
- Sanitization имени файла (path traversal, спецсимволы)
- Advisory lock на генерацию номера диаграммы (race condition protection)

**Response:** `200 OK`
```json
{
    "uid": "550e8400-e29b-41d4-a716-446655440000",
    "number": 1,
    "project_code": "thermohydraulics",
    "status": "uploaded",
    "filename": "scheme_001.png"
}
```

**Errors:** `400` (неизвестный проект), `413` (файл > 200 MB)

---

### GET /api/diagrams/

Получить список диаграмм.

**Query Parameters:**

| Параметр | Тип | Default | Описание |
|----------|-----|---------|----------|
| skip | int | 0 | Пропустить N записей |
| limit | int | 50 | Максимум записей (1-100) |
| project_code | string | - | Фильтр по проекту |
| status | string | - | Фильтр по статусу |

---

### GET /api/diagrams/{uid}

Получить информацию о диаграмме.

---

### GET /api/diagrams/{uid}/status

Получить статус (для polling).

**Response:**
```json
{
    "status": "detecting",
    "error_message": null,
    "error_stage": null,
    "cvat_task_id": null,
    "cvat_job_id": null,
    "detection_count": null,
    "updated_at": "2026-02-09T12:05:00"
}
```

---

### GET /api/diagrams/{uid}/download/{artifact_type}

Скачать артефакт диаграммы.

**Path Parameters:**
- `uid` — UUID диаграммы
- `artifact_type` — тип артефакта (см. таблицу)

**Доступные artifact_type:**

| Значение | Описание | Формат |
|----------|----------|--------|
| `original_image` | Оригинальное изображение | PNG/JPEG/TIFF |
| `yolo_predicted` | YOLO предсказания (до валидации) | TXT |
| `yolo_validated` | YOLO после валидации в CVAT | TXT |
| `coco_predicted` | COCO предсказания | JSON |
| `coco_validated` | COCO после валидации | JSON |
| `node_mask` | Маска узлов (после сегментации) | PNG |
| `pipe_mask` | Маска труб (после сегментации) | PNG |
| `skeleton` | Скелет (после скелетизации) | PNG |

**Response:** Файл (`application/octet-stream`) с `Content-Disposition: attachment`.

**Errors:** `400` (неизвестный тип), `404` (диаграмма или артефакт не найдены)

---

### POST /api/diagrams/{uid}/reupload-original

Перезагрузить оригинальное изображение (если потеряно).

**Request:** `multipart/form-data`
- `file` — новое изображение

---

### DELETE /api/diagrams/{uid}

Удалить диаграмму и все файлы.

---

## 6.5 Detection API

### POST /api/detection/{uid}/detect

Запустить YOLO детекцию.

**Precondition:** `status == "uploaded"`

**Response:**
```json
{
    "status": "detecting",
    "task_id": "abc123-celery-task-id",
    "uid": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Celery task:**
- Retry: 3 попытки с exponential backoff
- Soft time limit: 29 минут
- Idempotency: пропускает если уже `detected`

---

## 6.6 CVAT API

### POST /api/cvat/{uid}/open-validation

Открыть валидацию в CVAT. Переводит статус в `validating_bbox`.

**Response:**
```json
{
    "status": "validating_bbox",
    "cvat_url": "http://localhost:8080/tasks/123/jobs/456"
}
```

### POST /api/cvat/{uid}/create-task

Создать CVAT task для диаграммы (если ещё нет).

### POST /api/cvat/{uid}/fetch-annotations

Получить валидированные аннотации из CVAT. Переводит статус в `validated_bbox`.

**Response:**
```json
{
    "annotation_count": 42,
    "coco_path": "storage/diagrams/{uid}/detection/coco_validated.json",
    "yolo_path": "storage/diagrams/{uid}/detection/yolo_validated.txt"
}
```

### GET /api/cvat/{uid}/cvat-url

Получить URL CVAT задачи.

---

## 6.7 Segmentation API

### POST /api/segmentation/{uid}/segment

Запустить сегментацию U2-Net++.

**Precondition:** `status == "validated_bbox"`

**Статус:** Реализован endpoint, Celery task ожидает интеграции ML модуля.

---

## 6.8 Остальные API (Phase 3-5)

| Endpoint | Phase | Статус |
|----------|-------|--------|
| POST /api/skeleton/{uid}/skeletonize | Phase 3 | Endpoint есть, task TBD |
| POST /api/junction/{uid}/classify | Phase 4 | TBD |
| POST /api/graph/{uid}/build | Phase 5 | TBD |
| POST /api/graph/{uid}/generate-fxml | Phase 5 | TBD |

---

## 6.9 Коды ошибок

| Код | Описание |
|-----|----------|
| 200 | Успешно |
| 400 | Некорректный запрос / Неизвестный проект / Неизвестный тип артефакта |
| 404 | Диаграмма, проект или артефакт не найден |
| 413 | Файл слишком большой (> 200 MB) |
| 422 | Ошибка валидации данных |
| 500 | Внутренняя ошибка сервера |

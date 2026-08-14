# TESTING.md

**Аудитория:** DEV
**Версия:** 1.0
**Обновлено:** 2026-04-09
**Связанные документы:** CODING_GUIDE.md, DEV_SETUP.md

---

## Оглавление

1. [Обзор](#1-обзор)
2. [Запуск тестов](#2-запуск-тестов)
3. [conftest.py и fixtures](#3-conftestpy-и-fixtures)
4. [Тестовые файлы](#4-тестовые-файлы)
5. [Покрытие](#5-покрытие)
6. [Как добавить тест](#6-как-добавить-тест)

---

## 1. Обзор

Тесты используют **pytest** и расположены в `tests/`. Основные категории: smoke-тесты импортов (worker), unit-тесты рефакторинга (модели, storage, dispatch), интеграционные тесты OCR pipeline (domain profiles, binding, classify), тесты UI-логики (graph flow, validation window) без запуска Qt Application.

Все тесты работают **без Docker, GPU и реальной БД** — используют SQLite in-memory и MagicMock.

---

## 2. Запуск тестов

```bash
# Все тесты
pytest tests/ -v

# Конкретный файл
pytest tests/test_refactoring.py -v

# Конкретный тест
pytest tests/test_refactoring.py::TestUpsertArtifact -v

# С выводом print/stdout
pytest tests/ -v -s

# Остановиться на первом сбое
pytest tests/ -v -x

# Только smoke-тесты импортов
pytest tests/test_worker/ -v
```

Для тестов OCR phase0 (Surya + PaddleOCR) — запуск внутри контейнера `worker_ocr`:

```bash
docker run --rm --gpus all \
  -v ./tests:/app/tests \
  -v ./models/ocr:/models/ocr \
  -e HF_HOME=/models/ocr/hf_cache \
  pid_worker_ocr python /app/tests/test_ocr_phase0.py
```

---

## 3. conftest.py и fixtures

Файл: `tests/conftest.py`. Выполняет критическую подготовку **до любого импорта app-модулей**:

**Env-переменные.** Устанавливает тестовые значения `DATABASE_URL=sqlite:///test.db`, `STORAGE_PATH`, `PROJECTS_CONFIG_DIR`, `CELERY_BROKER_URL` до импорта `app.config`.

**Patching async engine.** Подменяет `sqlalchemy.ext.asyncio.create_async_engine` и `async_sessionmaker` на `MagicMock` — это предотвращает крэш при импорте `app.db.session` (SQLite не поддерживает asyncpg).

**Sync engine.** Создаёт `SQLite:///:memory:` engine и `sessionmaker`, патчит `app.db.session.engine` и `SessionLocal` — тесты используют синхронный SQLite вместо PostgreSQL.

Порядок инициализации критичен: env vars → patch async → import app → patch sync engine.

---

## 4. Тестовые файлы

### test_refactoring.py (714 строк)

Основной набор тестов рефакторинга pipeline. Покрывает:

| Тема | Что тестируется |
|------|----------------|
| 1.1 UniqueConstraint | `upsert_artifact` — insert + update через unique constraint |
| 1.2 StorageService | Async-методы хранилища |
| 1.3 Composite unique | `Diagram(project_code, number)` — уникальность |
| 1.4 Storage paths | Unified path helpers |
| 1.5 safe_dispatch | Диспетчеризация Celery задач |
| 3.1 find_original_image | Поиск файла изображения по расширениям |
| 3.3 is_deleted | Soft-delete проверка |
| 3.4 Timezone-aware | Timestamps с timezone |
| 3.8 Reupload cleanup | Очистка при повторной загрузке |
| 4.3 set_diagram_error | Установка ошибки в БД |
| 4.5 ProjectLoader TTL | Кэш с TTL |
| 4.6 DEFAULT_PROJECT_CODE | Код проекта по умолчанию |

### test_refactoring_v2.py (382 строки)

Верификация рефакторинга OCR + Binding. Проверяет: парсинг `domain_profile.yaml`, `ConfigDrivenProfile`, classify/is_noise/is_target/category_role, split_inline, `DomainBindingConfig`, `UnifiedMatcher` (KKS + диаметры), `BindingValidator` (unit↔class).

### test_b5_fixes.py (171 строка)

Тесты фиксов B5: sorted edge_key (`"B|A"` → `"A|B"`), exclude connectors из auto_bind, новый OCR-формат (B5.0). Self-contained — логика извлечена как pure functions, без PySide6.

### test_ocr_phase0.py (308 строк)

Smoke-тесты совместимости OCR-стека. Запускается внутри Docker-контейнера с GPU. Проверяет: torch+CUDA, Surya API (FoundationPredictor/RecognitionPredictor/DetectionPredictor), Surya OCR на белом изображении + кириллица, PaddlePaddle+PaddleX, PaddleOCR API, torch+paddle совместимость, наличие шрифтов.

### test_cyrillic_binding.py (370 строк)

Тесты binding с кириллическим профилем. Фаза 4: загрузка `DomainBindingConfig` + `UnifiedMatcher` (распознавание кодов Т1-ПД-585, ПД-601Р). Фаза 5: интеграция `UnifiedBinder` с фейковыми данными графа.

### test_cyrillic_load.py (330 строк)

Загрузка `domain_profile_cyrillic.yaml`. Проверяет: meta, patterns compilation, classify на всех типах кодов (FULL, HEAD, TAIL, DN, MULTI, OTHER), нормализация lat→cyr, grouping passes, noise engine, has_any_target_pattern, split_inline, has_secondary_script, загрузка через ProjectLoader.

### test_phase7_cyrillic.py (196 строк)

Сравнение `ConfigDrivenProfile(yaml)` с legacy `PidCyrillicProfile(python)`. Верифицирует, что YAML-профиль даёт идентичные результаты classify/noise/split для набора тестовых строк.

### test_stage7_graph_flow.py (514 строк)

Тесты Stage 7 UI: `GraphValidationWindow` (режимы, unsaved changes, undo stack) и `DiagramWorkspace` (two-phase graph flow: SimpleGraphTab → AdvancedGraphTab). Все тесты через MagicMock без Qt Application.

### test_worker/test_imports.py (110 строк)

Smoke-тесты: все worker-задачи и утилиты импортируются без ошибок. Ловит broken imports после рефакторинга (переименование, удаление, circular imports). Проверяет: `set_diagram_error`, `check_deleted`, `upsert_artifact`, `safe_dispatch` и другие callable'ы.

---

## 5. Покрытие

### Покрыто

| Область | Файлы |
|---------|-------|
| Worker imports (smoke) | `test_worker/test_imports.py` |
| DB models + upsert | `test_refactoring.py` |
| StorageService | `test_refactoring.py` |
| safe_dispatch | `test_refactoring.py` |
| ProjectLoader | `test_refactoring.py` |
| OCR domain profiles (KKS) | `test_refactoring_v2.py` |
| OCR domain profiles (кириллический) | `test_cyrillic_load.py`, `test_phase7_cyrillic.py` |
| Binding (KKS + кириллический) | `test_refactoring_v2.py`, `test_cyrillic_binding.py` |
| OCR stack compatibility (GPU) | `test_ocr_phase0.py` |
| UI graph flow (mock) | `test_stage7_graph_flow.py` |
| B5 edge_key / connectors / OCR format | `test_b5_fixes.py` |

### Не покрыто (требует Docker/GPU или реального pipeline)

| Область | Причина |
|---------|---------|
| End-to-end pipeline (upload → FXML) | Требует полный Docker stack |
| YOLO detection accuracy | Требует GPU + модели |
| Segmentation / skeleton / junction | Требует GPU + модели |
| SAM2 contour extraction | Требует GPU + модели |
| CVAT integration | Требует CVAT server |
| API endpoints (FastAPI) | Нет async test fixtures |
| UI rendering (PySide6) | Нет QApplication в CI |

---

## 6. Как добавить тест

### Для нового модуля app/

1. Создать `tests/test_{module}.py`
2. conftest.py обеспечит SQLite и patched engine автоматически
3. Импортировать тестируемый модуль — env vars и patches уже применены

```python
"""Tests for app/services/my_service.py."""
import pytest
from app.services.my_service import MyService

def test_basic():
    svc = MyService()
    result = svc.process("input")
    assert result == "expected"
```

### Для нового worker task

Добавить smoke-import в `tests/test_worker/test_imports.py`:

```python
def test_my_new_task(self):
    from worker.tasks.my_task import run_my_task
    assert callable(run_my_task)
```

### Для OCR-профиля

Следовать паттерну `test_cyrillic_load.py`: загрузить YAML, создать `ConfigDrivenProfile`, проверить classify/noise/split на наборе тестовых строк с ожидаемыми результатами.

### Для UI-логики без Qt

⛔ **Не подменять `sys.modules` на уровне модуля.** Подмена без восстановления травит **всю
сессию, начиная с коллекции**: следующие файлы по алфавиту видят заглушку вместо настоящего
модуля. Проект наступал на это трижды — `test_ocr_phase0.py` (лечили `collect_ignore`),
`sys.modules.setdefault` в observability (аудит 2026-07-09, C3: 13 упавших тестов
`test_direction_nodes.py`), `_stub_qt()` в `test_stage7_graph_flow.py:163` (2026-08-14:
`ImportError: cannot import name 'QTextLayout'` во всём `tests/ui/`).

Канон — фикстура с `monkeypatch.setitem` (функциональный скоуп → авто-восстановление);
образцы: `tests/observability/test_graph_errors.py:40-51`, `test_postprocess_log_level.py:12,41`,
`test_contours_errors.py:47-55`.

```python
@pytest.fixture
def widget_mod(monkeypatch):
    for _m in ("PySide6", "PySide6.QtCore", "PySide6.QtWidgets", "PySide6.QtGui"):
        monkeypatch.setitem(sys.modules, _m, MagicMock())
    sys.modules.pop("ui.widgets.my_widget", None)      # свежий импорт под заглушками
    mod = importlib.import_module("ui.widgets.my_widget")
    yield mod
    sys.modules.pop("ui.widgets.my_widget", None)      # ноль протечки
```

Логику методов тестировать через `patch`, виджеты — `MagicMock`.

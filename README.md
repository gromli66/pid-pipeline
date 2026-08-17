# P&ID Pipeline

**Аудитория:** ALL
**Версия:** 1.0
**Обновлено:** 2026-04-09

Система автоматической оцифровки технологических схем (Piping and Instrumentation Diagrams) для объектов атомной и тепловой энергетики.

## Что делает

Принимает растровое изображение P&ID-схемы и проходит 9 этапов обработки:

1. **Детекция узлов** — YOLOv8m + SAHI (36 классов оборудования)
2. **Валидация bbox** — CVAT-интеграция для ручной коррекции детекций
3. **Сегментация труб** — UNet++ ensemble (EfficientNet-B3, 4-канальный вход)
4. **Скелетизация** — морфологическое утончение масок труб
5. **Детекция перекрёстков** — UNet++ heatmap, CenterNet-style (EfficientNet-B2, 5-канальный вход: RGB + маска труб + скелет; классы junction/bridge)
6. **Валидация масок** — ручная коррекция pipe/junction масок
7. **Построение графа** — топологический граф из скелета + узлов (NetworkX)
8. **OCR + привязка** — Surya + PaddleOCR, привязка KKS и диаметров к узлам/рёбрам
9. **Экспорт FXML** — генерация JavaFX Markup для САПР

На выходе — структурированный граф с KKS-кодами, диаметрами труб и контурами оборудования.

## Стек

- **Backend:** FastAPI, PostgreSQL, Redis, Celery (3 очереди: gpu, ocr, sam2)
- **ML:** PyTorch, Ultralytics YOLO, SMP (UNet++), SAM2, Surya, PaddleOCR
- **UI:** PySide6 (desktop), QWebEngineView (CVAT-интеграция)
- **Инфраструктура:** Docker Compose, Alembic

## Быстрый старт
Подробнее: [DEPLOY_README.md](https://github.com/gromli66/pid-pipeline/blob/deploy/DEPLOY_README.md).

## Структура проекта

```
pid_pipeline/
├── app/                        # FastAPI backend
│   ├── api/                    # Endpoints (diagrams, detection, segmentation, ...)
│   ├── models/                 # SQLAlchemy models (Diagram, Artifact, ProcessingStage)
│   ├── schemas/                # Pydantic schemas
│   └── services/               # Business logic (storage, cvat_client, cvat_export)
├── modules/                    # ML-модули
│   ├── yolo_detector/          # YOLOv8m + SAHI детекция
│   ├── pipe_segmentation/      # UNet++ ensemble сегментация труб
│   ├── skeleton_extension/     # Скелетизация и утончение
│   ├── junction_segmentation/  # UNet++ heatmap (CenterNet-style), junction/bridge
│   ├── graph/                  # Построение топологического графа
│   ├── ocr/                    # Surya + PaddleOCR (3-pass tiling, classify/noise engine)
│   ├── binding/                # Привязка текста к узлам/рёбрам
│   ├── text_binding/           # Привязка диаметров
│   ├── kks_binding/            # Привязка KKS-кодов
│   ├── ocr_validation/         # Валидация OCR-результатов
│   ├── sam2_contour.py         # SAM2 контурная сегментация (Hiera Small + LoRA)
│   ├── contour_extractor.py    # Извлечение контуров из масок
│   ├── mask_refinement.py      # Уточнение масок
│   └── graph_to_fxml.py        # Генерация FXML из графа
├── worker/                     # Celery workers
│   └── tasks/                  # Task definitions (detection, segmentation, ...)
├── ui/                         # PySide6 desktop UI
│   ├── editors/                # Графические редакторы (base, simple, advanced, contour, ocr_binding)
│   ├── tabs/                   # Вкладки (pipe, junction, graph, contour, ocr_binding, cvat)
│   ├── widgets/                # Виджеты (diagram_list, progress_beads, upload_dialog)
│   ├── windows/                # Окна валидации (graph, mask, cvat)
│   └── services/               # Сервисы (api_client, status_provider, autosave)
├── config/                     # Конфигурация (classes.yaml, loader)
├── configs/projects/           # Per-project configs (thermohydraulics)
├── tests/                      # Тесты (pytest)
├── scripts/                    # CLI-утилиты
├── training/                   # Скрипты обучения моделей
├── augmentation/               # Аугментации (copy-paste, rotation)
└── docs/                       # Документация (21 документ + ADR)
```

## Pipeline: DiagramStatus (29 значений)

```
UPLOADED → DETECTING → DETECTED → VALIDATING_BBOX → VALIDATED_BBOX
→ SEGMENTING → SKELETONIZING → SKELETONIZED
→ VALIDATING_MASKS → VALIDATED_MASKS
→ SKELETONIZING_FINAL → SKELETONIZED_FINAL
→ DETECTING_JUNCTIONS → DETECTED_JUNCTIONS
→ VALIDATING_JUNCTIONS → VALIDATED_JUNCTIONS
→ BUILDING_GRAPH → BUILT → VALIDATING_GRAPH → VALIDATED_GRAPH
→ EXTRACTING_CONTOURS → CONTOURS_EXTRACTED → CONTOURS_VALIDATED
→ OCR_PROCESSING → OCR_COMPLETED → OCR_BOUND
→ GENERATING_FXML → COMPLETED
+ ERROR (из любого состояния)
```

Подробнее: [STATUS_MACHINE.md](docs/STATUS_MACHINE.md).

## Документация

| Документ | Описание |
|----------|----------|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Pipeline stages, Docker topology, storage layout, data flow |
| [STATUS_MACHINE.md](docs/STATUS_MACHINE.md) | 29 значений DiagramStatus, переходы, rollback, error recovery |
| [DB_SCHEMA.md](docs/DB_SCHEMA.md) | Таблицы, enums, миграции |
| [API.md](docs/API.md) | Все REST endpoints с примерами |
| [DATA_FORMATS.md](docs/DATA_FORMATS.md) | JSON schemas: COCO, graph node-link, contours, OCR, FXML |
| [CODING_GUIDE.md](docs/CODING_GUIDE.md) | Паттерны (Command, ModeHandler, Overlay), координаты, чеклисты |
| [WORKER_TASKS.md](docs/WORKER_TASKS.md) | Celery routing, dispatch chains, idempotency |
| [MODULES.md](docs/MODULES.md) | Graph builder, segmentation, skeleton, junction, binding |
| [OCR_PIPELINE.md](docs/OCR_PIPELINE.md) | Surya 3-pass, domain profiles, classify/noise engine, KKS |
| [UI_GUIDE.md](docs/UI_GUIDE.md) | Вкладки, редакторы, виджеты, windows |
| [MODELS.md](docs/MODELS.md) | ML-модели: архитектура, обучение, метрики |
| [CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) | Все YAML-параметры: тип, default, влияние |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Docker, GPU, volumes, миграции |
| [DEV_SETUP.md](docs/DEV_SETUP.md) | Python 3.11, venv, PySide6, подключение к backend |
| [CVAT.md](docs/CVAT.md) | CVAT-интеграция: экспорт/импорт, синхронизация |
| [SCRIPTS.md](docs/SCRIPTS.md) | CLI-утилиты, debug, визуализаторы |
| [TESTING.md](docs/TESTING.md) | Тесты, покрытие, как запускать |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Частые ошибки: cp1251, GPU OOM, статус завис |
| [GLOSSARY.md](docs/GLOSSARY.md) | Термины: P&ID, KKS, FXML, SAM2, SAHI и др. |
| [DOCUMENTATION_RULES.md](docs/DOCUMENTATION_RULES.md) | Мета-правила документации |
| [ADR/](docs/ADR/) | 6 Architecture Decision Records (SAM2, LoRA r=8, 6ch input, smart snap и др.) |
| [AUTO_LAYOUT.md](docs/AUTO_LAYOUT.md) | Раскладка графа на холсте: где живёт R&D, что нужно для запуска, чего нет в git |
| [AUTO_LAYOUT_E2E_TESTPLAN.md](docs/AUTO_LAYOUT_E2E_TESTPLAN.md) | Проверки авто-раскладки, требующие поднятого стека: e2e и отказные сценарии |
| [NAPRAVLENIE.md](docs/NAPRAVLENIE.md) | Класс `napravlenie` (стрелка на трубе): состояние фичи, цепочка direction-классификации, поля в артефактах |
| [NAPRAVLENIE_E2E_CHECKLIST.md](docs/NAPRAVLENIE_E2E_CHECKLIST.md) | Сквозная приёмка `napravlenie` на поднятом стеке (пункт ВН1б) |

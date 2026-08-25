# GLOSSARY.md — Глоссарий терминов P&ID Pipeline

**Обновлено:** 2026-08-25
**Правило:** пополняется в конце каждого чата документации.

---

## Домен

| Термин | Описание |
|--------|----------|
| **P&ID** | Piping and Instrumentation Diagram — технологическая схема трубопроводов и КИПиА |
| **KKS** | Kraftwerk-Kennzeichensystem — система маркировки оборудования АЭС (Rosatom) |
| **FXML** | JavaFX Markup Language — формат выхода pipeline, используется в САПР |
| **COCO** | Common Objects in Context — формат аннотаций (bbox + segmentation) |
| **domain profile** | YAML-конфигурация распознавания текста для конкретного объекта (KKS Rosatom, кириллический и т.д.). Задаёт regex-паттерны, словари, правила классификации OCR-блоков. Файл: `configs/projects/*/domain_profile_*.yaml` |

## Pipeline

| Термин | Описание |
|--------|----------|
| **Бусина (bead)** | Визуальный индикатор этапа pipeline в UI (зелёный/жёлтый/серый/оранжевый) |
| **Артефакт (artifact)** | Файл, создаваемый на этапе pipeline (image, JSON, mask) |
| **Worker** | Celery worker — фоновый процесс для GPU-задач |
| **Queue** | Celery queue — gpu, ocr, sam2 |
| **DiagramStatus** | Enum из 29 значений (от `UPLOADED` до `COMPLETED` + `ERROR`), определяющий текущее состояние диаграммы в pipeline. См. STATUS_MACHINE.md |
| **safe_dispatch** | Функция безопасного запуска Celery task с async-bridge — оборачивает `task.delay()` в проверку статуса и обработку ошибок. См. WORKER_TASKS.md |
| **skip_classes** | Набор классов (`connector`, `voronka`, `annotation`, `truba`, `background`), которые не получают стандартный скин при генерации FXML |

## Данные и координаты

| Термин | Описание |
|--------|----------|
| **centroid** | Центр узла, формат `[y, x]` (row, col) |
| **bbox** | Bounding box, формат `[x1, y1, x2, y2]` в graph; `[x, y, w, h]` в COCO |
| **segmentation** | Полигон контура, flat list `[x1, y1, x2, y2, ...]` |
| **ann_idx / ann_id** | ID COCO annotation — ключ связи между graph node и contour data |
| **class_id / class_name** | Канонический класс оборудования: `class_id = category_id - 1`, `class_name` — английское имя из `classes:` в YAML проекта. Единственное имя класса внутри системы, в артефактах и в конфигах |
| **display_labels / отображаемое имя класса** | Блок YAML `display_labels` (en → ru) и построенное по нему название, видимое человеку: метки в CVAT и списки классов в клиенте. Никогда не попадает в артефакты и не заменяет `class_name`. Модуль `app/services/class_display.py`; см. CONFIG_REFERENCE.md §3.2.1 |
| **денормализация меток** | Приведение категорий из выгрузки CVAT к каноническим именам и id (`denormalize_coco_labels`, `app/api/cvat.py`). Нужна потому, что CVAT нумерует `category_id` позицией метки в проекте, а не её id. См. CVAT.md §7 |
| **source_point / target_point** | Точки соединения ребра с узлами, формат `[y, x]` |
| **waypoints** | Промежуточные точки ребра графа (формат `[[y1,x1], [y2,x2], ...]`), определяющие L-маршрут. Используются при рендеринге polyline и экспорте FXML. В холсте «Ручной правки» — кэш последнего расчёта маршрута, не намерение оператора (любой жест вправе перестроить) |
| **пин входа (pin_source / pin_target)** | Ключ ребра холста «Ручной правки» (модель «пин на ребре», 2026-08-03): `{'dx','dy'}` — локальное смещение **(x, y)** конца ребра от центроида узла; единственное персистентное намерение оператора, переживает undo/save/resize. Пин на коннекторе запрещён. API: `modules/graph/core/ports.py`; заменил легаси `_manual_route`/`_auto_route`/`node['_ports']` (до 2026-08-03) |
| **node-link format** | JSON-формат представления графа: `{"nodes": [...], "edges": [...]}`. Каждый node содержит id, class_name, centroid, bbox; каждый edge — source, target, source_point, target_point, waypoints |
| **contours_all** | Поле узла графа, содержащее множественные полигоны из contour_extractor (для drossel/voronka). Используется при генерации FXML как массив `<Polygon>` элементов |
| **auto / manual_review** | Статус контура SAM2: `auto` — принят автоматически (confidence ≥ threshold), `manual_review` — требует ручной проверки оператором |

## Модели

| Термин | Описание |
|--------|----------|
| **SAHI** | Slicing Aided Hyper Inference — тайловый inference для YOLO |
| **SAM2** | Segment Anything Model 2 (Meta) — модель сегментации с prompt |
| **LoRA** | Low-Rank Adaptation — метод fine-tuning с малым числом параметров |
| **Hiera** | Hierarchical Vision Transformer — backbone SAM2 |
| **DT-max** | Distance Transform Maximum — точка максимально внутри маски (prompt для SAM2) |
| **Smart snap** | Douglas-Peucker + H/V edge snapping — регуляризация полигонов контуров. Параметры: `dp_eps_pct`, `snap_threshold`, `min_edge_pct` (относительные, масштабируются по размеру bbox) |
| **UNet++** | Nested U-Net — архитектура для сегментации труб/узлов |
| **CenterNet** | Anchor-free детектор объектов — используется для детекции junction points. В отличие от YOLO, предсказывает центры объектов через heatmap |
| **Surya** | OCR-модель для детекции текстовых строк (3-pass tiling: 1×, tile2, tile3). Используется на первом этапе OCR pipeline |
| **PaddleOCR** | OCR-модель (PaddlePaddle) для распознавания текста. Используется как второй recognizer совместно с Surya |

## UI и паттерны

| Термин | Описание |
|--------|----------|
| **Command** | Паттерн undo/redo — каждая мутация обёрнута в объект с execute()/undo() |
| **ModeHandler** | Обработчик режима редактирования (on_press/move/release) |
| **Overlay** | Набор QGraphicsItems поверх сцены (vertex handles, resize handles) |
| **BaseGraphTab** | Базовый класс вкладки — download, save, confirm template |
| **BaseGraphEditor** | Базовый класс редактора — rendering, zoom, hit testing, mode system |
| **GraphDataModel** | Модель данных графа (`ui/editors/graph_data.py`). Proxy-обёртки над nodes/edges dict, snapshot/restore для undo, CRUD-операции, фабрики узлов и рёбер |
| **edge_routing** | Модуль `ui/editors/edge_routing.py` (544 строки) — алгоритмы маршрутизации рёбер: L-route, perpendicular connection, waypoint computation |
| **autofix** | Модуль `ui/editors/autofix_chains.py` (767 строк) — автоматическая коррекция графа: выравнивание узлов, перпендикуляризация рёбер, цепочки коннекторов |

## Инфраструктура

| Термин | Описание |
|--------|----------|
| **Rollback** | Откат статуса диаграммы к предыдущему этапу с удалением артефактов |
| **preserve_ocr / preserve_contours** | Флаги rollback — не удалять OCR/contour артефакты при откате графа |
| **Idempotency** | Проверка в начале task — если диаграмма уже дальше по pipeline, пропустить |
| **ProcessingStage** | Запись в БД о выполнении этапа (начало, конец, время, ошибка) |

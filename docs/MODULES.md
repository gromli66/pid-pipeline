# MODULES.md — Внутренняя логика модулей

**Аудитория:** DEV / ML
**Версия:** 1.1
**Обновлено:** 2026-04-09
**Связанные документы:** WORKER_TASKS.md, ARCHITECTURE.md, CONFIG_REFERENCE.md

---

## Оглавление

1. [Graph Builder](#1-graph-builder)
2. [Pipe Segmentation](#2-pipe-segmentation)
3. [Skeleton Extension](#3-skeleton-extension)
4. [Junction Segmentation](#4-junction-segmentation)
5. [Mask Refinement](#5-mask-refinement)
6. [Contour Extractor (legacy)](#6-contour-extractor-legacy)
7. [OCR Validation](#7-ocr-validation)

---

## 1. Graph Builder

**Файлы:** `modules/graph/core/builder.py`, `nodes.py`, `tracing.py`, `export_json.py`, `config.py`, `visualize.py`, `bridge_preprocessing.py`, `utils.py`

`GraphBuilder` — основной класс, превращающий бинарные маски (equipment, connections, bridges, skeleton) в граф P&ID.

**Файлы по функциям:**

| Файл | Назначение |
|------|-----------|
| `builder.py` | Главный класс `GraphBuilder`, метод `build()` |
| `nodes.py` | `extract_nodes`, `update_node_degrees`, `prepare_tracing_data`, `assign_node_classes`, `filter_isolated_connectors`, `load_coco_annotations` |
| `tracing.py` | `trace_edges_v3` — трассировка рёбер по скелету |
| `export_json.py` | Экспорт в NetworkX node-link JSON |
| `config.py` | Конфигурация параметров Graph Builder |
| `visualize.py` | Визуализация графа поверх изображения |
| `bridge_preprocessing.py` | Валидация и preprocessing мостов |
| `utils.py` | Утилиты: `load_binary_mask` и др. |

### Pipeline (`build()`)

```
Input masks:
  equipment_mask   — equipment (YOLO bbox + COCO segmentation)
  connection_mask  — connectors (tee, elbow, reducer, ...)
  bridge_mask      — мосты (труба проходит поверх)
  skeleton         — skeleton_final из skeleton_extension
  coco_path        — COCO JSON с аннотациями (segmentation/bbox)
  original_image   — для визуализации (опционально)

[1/7] Загрузка масок
  ├── load_binary_mask() (из utils.py) для каждой маски
  └── turn_mask (опционально) объединяется с connection_mask

[2/7] Bridge preprocessing
  ├── preprocess_bridges() → valid/invalid bridges, routing info
  ├── Invalid bridges → конвертируются в connector nodes
  └── unified_nodes_mask = equipment | updated_connections

[3/7] Компонентная разметка (раздельная!)
  ├── extract_nodes() (из nodes.py) — внутри:
  │     ndimage.label(equipment_mask) → labeled_equipment, num_equipment
  │     ndimage.label(connections_mask) → labeled_connectors, num_connectors
  └── Connectors получают offset: label_id + num_equipment

[4/7] Подготовка данных для трассировки
  └── prepare_tracing_data() (из nodes.py) → skeleton_cleaned, contact_map, bridge_contact_map

[5/7] Трассировка рёбер (v3 — динамические узлы)
  ├── trace_edges_v3() → edges, nodes
  ├── Обход скелета: endpoint → path → contact с узлом/мостом/endpoint
  ├── Динамическое создание connector'ов на точках ветвления
  └── compute_edge_statistics() для каждого ребра

[6/7] Фильтрация коротких шпор
  └── Удаление terminal edges с length < min_spur_length

[7/7] Фильтрация изолированных connector'ов
  └── filter_isolated_connectors() (из nodes.py) → удаление degree-0 connectors

Output → result dict → save() / export_graph_to_json()
```

### Ключевые параметры

| Параметр | Default | Описание |
|----------|---------|----------|
| `min_spur_length` | 5 | Минимальная длина концевого ребра (px) |
| `max_path_length` | 10000 | Максимальная длина пути трассировки (px) |
| `node_dilation` | 1 | Дилатация узлов для поиска контактов (px) |
| `json_format` | `"node-link"` | Формат JSON экспорта (NetworkX node-link) |
| `include_paths` | `False` | Включать pixel paths в JSON |

### Bridge preprocessing

**Файл:** `modules/graph/core/bridge_preprocessing.py`

Мост (bridge) — место, где труба проходит *поверх* другой трубы без соединения. Preprocessing определяет: какие мосты корректные (2 точки контакта со скелетом, routing path прямой) и какие нужно переклассифицировать.

Результат: `valid_bridges_mask` (мосты с routing) + `invalid_bridges_mask` (→ становятся connector'ами) + `bridge_routing` dict (bridge_id → пара точек для трассировки «через»).

### Экспорт JSON

**Файл:** `modules/graph/core/export_json.py`

Формат: NetworkX node-link (`{"directed": false, "graph": {...}, "nodes": [...], "links": [...]}`). Каждый node содержит: id, type, centroid `[y, x]`, bbox `[x1, y1, x2, y2]`, segmentation (flat), class_id, class_name, area, degree, ann_idx. Каждый edge: id, source, target, source_point `[y, x]`, target_point `[y, x]`, waypoints, length, is_terminal.

---

## 2. Pipe Segmentation

**Файлы:** `modules/pipe_segmentation/inference/ensemble.py`, `postprocessing.py`, `engine.py`, `modules/pipe_segmentation/model/architecture.py`

### Архитектура модели

`DualHeadModel` — обёртка над UNet++ (segmentation_model_pytorch) с двумя головами: **mask** (основная segmentation head) и **skeleton** (дополнительная 1×1 conv). При инференсе возвращает только маску (backward compatible). При обучении — `(mask_logits, skeleton_logits)`.

Инициализация skeleton head: Xavier uniform, zero bias. Используется общий encoder + decoder из smp.

### Ensemble inference

`EnsembleInference` — загружает два checkpoint'а (model A + model B), прогоняет оба через `TiledInference`, комбинирует результаты.

**Стратегии комбинирования:**

| Strategy | Формула | Когда |
|----------|---------|-------|
| `or` | `A \| B` (побитовое ИЛИ) | Default — максимальный recall |
| `and` | `A & B` | Максимальная precision |
| `mean` | `(prob_A + prob_B) / 2 > threshold` | Баланс |
| `weighted_mean` | `w_a * prob_A + w_b * prob_B > threshold` | Кастомные веса |

**Pipeline:**

1. Model A inference (tiled, без постобработки)
2. Model B inference (tiled, без постобработки)
3. Combine по стратегии
4. Постобработка (если `postprocess=True`)
5. GPU cleanup: `free_memory()` → удаление моделей + `torch.cuda.empty_cache()`

### Постобработка

`post_process_mask()` — pipeline v3 (без агрессивной морфологии):

1. **remove_small_components** — удаление мелких компонент (шум)
2. **remove_drawing_frame** — удаление рамки чертежа (connected components + morphological analysis: поиск компонент, касающихся краёв изображения в пределах margin, с длиной bbox > 50% стороны)
3. **smart_skeleton_connect** — скелетное соединение разрывов: скелетизация → поиск endpoints → направленное соединение пар (проверка: трассировка по тёмным пикселям, совпадение направлений, штраф за нарушение)
4. **Минимальная морфология** — closing/opening (только если kernel > 0)
5. **fill_mask_holes** — заполнение дыр внутри замкнутых контуров (max_hole_size)

---

## 3. Skeleton Extension

**Файлы:** `modules/skeleton_extension/core.py` (~1600 строк), `processing.py`, `mask_generation.py`, `visualization.py`

### Entry point

`process_single_image()` — обрабатывает одно изображение. Два режима:

| Режим | Когда | Описание |
|-------|-------|----------|
| **Full mode** | Первая скелетизация (`task_skeletonize`) | Полный pipeline с protection mask |
| **Simple mode** | После UI валидации (`task_skeletonize_simple`) | Упрощённый pipeline для `pipe_mask_validated` |

### Full mode pipeline

```
Input: prediction (pipe_mask), original, nodes_mask
                     ↓
skeletonize(prediction) → binary skeleton
                     ↓
[1] remove_skeleton_under_nodes — удалить внутри узлов, контур сохранить
                     ↓
[2] remove_skeleton_around_nodes — удалить в 1px вокруг (expansion)
                     ↓
[3] create_endpoint_protection_mask — trim 10px + build 8px-wide mask
                     ↓
[4] connect_with_directed_lines — направленные линии
    Приоритет целей: node_contour → endpoint → other_skeleton
    • Трассировка по тёмным пикселям оригинала (pipe_mask)
    • KDTree для быстрого поиска endpoints и скелета
    • Только к ЧУЖОЙ компоненте (connected components)
                     ↓
[5] bfs_connect_endpoints — BFS для оставшихся
    • BFS по реальной трубе (pipe_mask)
    • Допуск BFS_MASK_TOLERANCE белых пикселей за путь
    • Приоритет: node > endpoint > skeleton
                     ↓
[6] remove_orphan_components — удаление неподключённых компонент
                     ↓
skeleton_to_mask() — утолщение скелета в маску
```

### Simple mode pipeline

Для валидированной маски (после UI):

```
Input: pipe_mask_validated, original, nodes_mask
                     ↓
skeletonize → remove_under_nodes_simple → create_simple_protection_mask
→ connect_with_directed_lines → bfs_connect_endpoints → remove_orphan_components
```

Отличия от full: `remove_skeleton_under_nodes_simple` (менее агрессивный), `create_simple_protection_mask` (продление endpoints до контура узлов через extend_radius).

### Visualization

**Файл:** `modules/skeleton_extension/visualization.py` — визуализация результатов скелетизации (overlay скелета, endpoints, connections на оригинальном изображении).

### Mask Generation

`skeleton_to_mask()` — превращает скелет обратно в маску труб:

1. **Pruning** — удаление коротких шпор (артефакты скелетизации)
2. **Утолщение** — адаптивное (из реальной ширины труб через Distance Transform) или фиксированное (fallback)
3. **Clip** — маска не выходит за пределы реальных труб (по pipe_mask)
4. **Smoothing** — morphological closing

Адаптивная толщина: бинаризация оригинала → Distance Transform → `thickness_map` (half-width × 2) → `_propagate_thickness` (KDTree для extension-участков) → `_adaptive_dilate` (per-pixel dilation по thickness_map).

### Ключевые параметры (из project config)

| Параметр | Default | Описание |
|----------|---------|----------|
| `node_boundary_expansion` | 1 | Расширение узлов при поиске контактов |
| `trim_length` | 10 | Длина trim от endpoints |
| `trim_protection` | 15 | Длина защиты от trim |
| `mask_width` | 8 | Ширина protection mask |
| `direction_trace_length` | 5 | Длина трассировки для определения направления |
| `max_line_length` | 600 | Максимальная длина направленной линии |
| `endpoint_search_radius` | 5 | Радиус поиска endpoint'ов |
| `skeleton_search_radius` | 5 | Радиус поиска скелета |
| `bfs_max_depth` | 1000 | Максимальная глубина BFS |
| `bfs_iterations` | 1 | Количество проходов BFS |
| `bfs_mask_tolerance` | 5 | Допуск белых пикселей в BFS пути |
| `simple_mode` | `False` | Упрощённый режим (для validated mask) |

---

## 4. Junction Segmentation

**Файлы:** `modules/junction_segmentation/model.py`, `inference.py`, `batch_inference.py`, `config.py`, `dataset.py`, `loss.py`, `metrics.py`, `find_thresholds.py`, `prepare_junction_dataset.py`

### Архитектура

`JunctionSegModel` — UNet++ (smp) с двумя каналами выхода (junction heatmap + bridge heatmap), опционально с Skeleton Attention Gate и Aux Head.

| Компонент | Описание |
|-----------|----------|
| **Encoder** | из smp (e.g. EfficientNet) |
| **Decoder** | UNet++ decoder (вложенные skip connections) |
| **Seg head** | 2-channel output → sigmoid → heatmaps |
| **SkeletonAttentionGate** | Skeleton dilation через `F.max_pool2d` (kernel=dilation_px) → soft gate: `attention * 0.9 + 0.1` → multiplication на encoder features |
| **AuxHead** | Бинарная классификация (есть junction / нет) из bottleneck features |

**CenterNet bias init:** `sigmoid(-4.6) ≈ 0.01` — критически важно для стабильного обучения с focal loss. Без инициализации: `sigmoid(0) = 0.5` → initial loss ~60000. Подробнее об архитектуре и обучении — см. [MODELS.md §4](MODELS.md#4-junctionbridge-centernet-junction-segmentation).

### Inference pipeline

`run_inference()`:

1. **Tiled inference** — tile_size=512, overlap=128, Gaussian blending
2. **Accumulation** — weighted sum по тайлам → нормализация
3. **NMS** — max pooling kernel для подавления дубликатов
4. **Peak extraction** — по порогам (см. ниже)
5. **Output** — списки `{x, y, confidence}` для junctions и bridges

**Пороги inference (cascade приоритетов):**

| Источник | Junction | Bridge | Когда применяется |
|----------|----------|--------|-------------------|
| `config.py` defaults | 0.4 | 0.5 | Baseline — если ничего не указано |
| `inference.py` CLI defaults | 0.55 | 0.60 | При вызове `run_inference()` без аргументов |
| `threshold_analysis.json` | по данным | по данным | Если файл найден и CLI не переопределяет |
| CLI аргументы | любые | любые | Наивысший приоритет |

Каскад в коде: CLI args (если заданы) → `threshold_analysis.json` (если файл существует) → `config.py` defaults.

**Рекомендованные production-пороги** (из threshold analysis): junction=0.40, bridge=0.60 (баланс precision ≥ 95%).

**Input:** image (RGB), pipe_mask, skeleton (3 канала → stack)
**Output:** `junction_points`, `bridge_points`, heatmaps `[2, H, W]`

---

## 5. Mask Refinement

**Файл:** `modules/mask_refinement.py`

`refine_pipe_mask()` — коррекция маски труб через skeleton → adaptive dilate.

**Pipeline:**

1. **Skeletonize** маску труб
2. **Бинаризация** оригинала → Distance Transform → `thickness_map` (реальная ширина труб)
3. **Propagate** толщину на extension-участки (KDTree nearest neighbor)
4. **Adaptive dilate** — per-pixel dilation по thickness_map
5. **Subtract** node_mask (если передан) — трубы не заходят внутрь узлов

Результат — refined mask с корректной шириной труб, даже на участках, где оригинальная сегментация была неточной.

### Другие реализации уточнения масок

В кодовой базе три разных подхода к уточнению масок труб:

| Файл | Подход | Контекст использования |
|------|--------|----------------------|
| `modules/mask_refinement.py` | Skeleton + adaptive dilate по distance transform | Основной: коррекция ширины труб |
| `modules/ocr/refine_pipe_mask.py` | Графовая структура: distance transform → скелетизация → разрез по junction/bridge → dilate по медианной полуширине | Серверный: уточнение маски по аннотациям графа |
| `refine_pipe_mask_perp()` в `modules/ocr/pipeline.py` | Перпендикулярное сечение по junction/bridge точкам | OCR preprocessing: уточнение перед вычитанием масок |

---

## 6. Contour Extractor (legacy)

**Файл:** `modules/contour_extractor.py`

`ContourExtractor` — извлечение контуров equipment из изображения (pre-SAM2, legacy). Используется через `enrich_graph_with_contours()` для узлов без SAM2 polygon.

**Метод `extract(bbox)`:**

1. Crop по bbox + padding
2. Otsu threshold → бинаризация
3. Subtract pipe_mask → только символы оборудования
4. Morphological closing (kernel из pipe thickness)
5. Connected components → выбор компонент внутри bbox
6. `cv2.findContours()` → полигон
7. Simplify (Douglas-Peucker, если реализовано; в текущем коде возвращается raw контур без упрощения)

**Метод `extract_lines(bbox)`:** альтернативный метод — извлечение отрезков через Hough Lines (для символов с прямыми линиями: вентили, задвижки).

**Auto-параметры:** `pipe_thickness` (из Distance Transform pipe_mask), `close_k` (из pipe_thickness / 3), `fill_k` (из медианы min-размеров bbox'ов).

`enrich_graph_with_contours()` — обогащает граф: для каждого equipment-узла без segmentation вызывает `extract()`, записывает flat polygon в `node["segmentation"]` и `contour_render_mode`.

---

## 7. OCR Validation

**Файлы:** `modules/ocr_validation/classifier.py`, `modules/ocr_validation/result.py`

`OcrBlockClassifier` — двухпроходный классификатор OCR-блоков для UI-валидации результатов OCR.

### Алгоритм

Для каждого блока:

1. **DiameterMatcher** — поиск диаметра в тексте, вычитание из строки
2. **KksMatcher** — поиск KKS в оставшемся тексте
3. Тип блока определяется по найденному; цвет — по качеству match

Порядок важен: вычитание диаметра решает проблему составных строк (напр. `"10LBG50 Dy25 AA401"`), где regex без вычитания ложно находит unit=Dy в KKS.

### Типы и цвета

| Тип (`BlockType`) | Описание |
|-------------------|----------|
| KKS | Найден полный KKS-код |
| DIAMETER | Найден диаметр (Dv/DN/Ø) |
| KKS_DIAMETER | Найдены оба |
| OTHER | Ни KKS, ни диаметр |

| Качество (`MatchQuality`) | Цвет (`ValidationColor`) |
|---------------------------|--------------------------|
| EXACT | GREEN |
| CORRECTED | YELLOW |
| NONE | ORANGE |

### Результат

Dataclass `BlockClassification` содержит: block_type, kks_match (KksMatch или None), diameter_match (DiameterMatch или None), color, details. `ValidationReport` — агрегированный отчёт по всем блокам.

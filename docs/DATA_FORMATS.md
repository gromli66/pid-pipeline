# DATA_FORMATS.md — Форматы данных P&ID Pipeline

**Аудитория:** DEV / ML
**Версия:** 1.1
**Обновлено:** 2026-07-30
**Связанные документы:** [ARCHITECTURE.md](ARCHITECTURE.md), [STATUS_MACHINE.md](STATUS_MACHINE.md), [MODULES.md](MODULES.md)

---

## Содержание

1. [Координатные системы](#1-координатные-системы)
2. [COCO format](#2-coco-format)
3. [Graph format](#3-graph-format)
4. [Contours format](#4-contours-format)
5. [OCR format](#5-ocr-format)
6. [FXML output](#6-fxml-output)
7. [Image артефакты](#7-image-артефакты)

---

## 1. Координатные системы

В pipeline используются **четыре разных формата координат**. Несоблюдение формата — частый источник багов.

| Формат | Порядок | Используется в | Пример |
|--------|---------|----------------|--------|
| **centroid** | `[y, x]` (row, col) | graph nodes, skeleton | `[450, 320]` |
| **bbox (graph)** | `[x1, y1, x2, y2]` | graph nodes | `[100, 200, 300, 400]` |
| **bbox (COCO)** | `[x, y, w, h]` | COCO annotations | `[100, 200, 200, 200]` |
| **segmentation** | flat `[x1, y1, x2, y2, ...]` | graph nodes, contours | `[100, 200, 150, 210, ...]` |
| **source_point / target_point** | `[y, x]` (row, col) | graph edges | `[450, 320]` |
| **waypoints** | `[[y, x], [y, x], ...]` | graph edges | `[[100, 200], [100, 300]]` |

**Важно:** `centroid`, `source_point`, `target_point`, `waypoints` — формат `[y, x]` (row, col). `bbox` и `segmentation` — формат `[x, ...]` (col first). Это историческое соглашение, унаследованное от OpenCV (row-first) и COCO (col-first).

---

## 2. COCO format

Файлы: `coco_predicted.json` (после YOLO), `coco_validated.json` (после CVAT). Стандартный COCO формат.

### Структура

```json
{
  "images": [
    {
      "id": 1,
      "file_name": "image.png",
      "width": 6000,
      "height": 4200
    }
  ],
  "categories": [
    {"id": 0, "name": "armatura_ruchn"},
    {"id": 1, "name": "armatura_electro"},
    {"id": 2, "name": "nasos"},
    {"id": 3, "name": "truba"},
    {"id": 4, "name": "annotation"}
  ],
  "annotations": [
    {
      "id": 1,
      "image_id": 1,
      "category_id": 0,
      "bbox": [1200, 850, 180, 160],
      "area": 28800,
      "segmentation": [[1200, 850, 1380, 850, 1380, 1010, 1200, 1010]],
      "iscrowd": 0,
      "score": 0.92
    }
  ]
}
```

### Поля annotation

| Поле | Тип | Обязательное | Описание |
|------|-----|-------------|----------|
| `id` | int | ✅ | Уникальный ID аннотации (ann_id). Ключ связи с graph node и contour |
| `image_id` | int | ✅ | Ссылка на `images[].id` |
| `category_id` | int | ✅ | Ссылка на `categories[].id` |
| `bbox` | `[x, y, w, h]` | ✅ | Bounding box в формате COCO |
| `area` | float | ✅ | Площадь аннотации (пикселей) |
| `segmentation` | `[[x1, y1, ...]]` | ✅ | Список полигонов (обычно один) |
| `iscrowd` | int | ✅ | Всегда `0` |
| `score` | float | — | Confidence YOLO (только в `coco_predicted.json`) |

**Особые категории:** `truba` (трубопровод) и `annotation` (текстовые подписи) исключаются при генерации `node_mask` и при SAM2 inference — они не являются узлами оборудования.

---

## 3. Graph format

Файлы: `graph.json` (автоматический), `graph_validated.json` (после валидации). Формат: NetworkX node-link.

### Верхний уровень

```json
{
  "directed": false,
  "multigraph": false,
  "graph": {},
  "nodes": [ ... ],
  "links": [ ... ]
}
```

### Node (узел)

```json
{
  "id": "node_5",
  "type": "equipment",
  "centroid": [1250, 3400],
  "area": 28800,
  "bbox": [3310, 1170, 3490, 1330],
  "degree": 2,
  "class_id": 0,
  "class_name": "armatura_ruchn",
  "yolo_idx": 5,
  "ann_idx": 42,
  "segmentation": [3310, 1170, 3490, 1170, 3490, 1330, 3310, 1330],
  "kks_full": "10LAA01AA101"
}
```

| Поле | Тип | Обязательное | Описание |
|------|-----|-------------|----------|
| `id` | string | ✅ | Уникальный ID (`node_N` или `junction_N`) |
| `type` | string | ✅ | `"equipment"`, `"junction"`, `"bridge"` |
| `centroid` | `[y, x]` | ✅ | Центр узла (row, col) |
| `area` | int | ✅ | Площадь в пикселях |
| `bbox` | `[x1, y1, x2, y2]` | ✅ | Bounding box (graph format, НЕ COCO) |
| `degree` | int | ✅ | Степень узла (количество рёбер) |
| `class_id` | int | — | ID категории из COCO |
| `class_name` | string | — | Имя категории из COCO |
| `yolo_idx` | int | — | Индекс в предсказаниях YOLO |
| `ann_idx` | int | — | COCO annotation ID (ключ связи с contours) |
| `segmentation` | `[x1, y1, ...]` | — | Полигон контура (flat list, из SAM2 или legacy) |
| `kks_full` | string | — | Полный KKS-код оборудования (после OCR binding) |

### Link (ребро)

```json
{
  "id": "edge_12",
  "source": "node_5",
  "target": "node_8",
  "source_point": [1250, 3490],
  "target_point": [1400, 3490],
  "length": 150.0,
  "is_terminal": false,
  "color": null,
  "straight_line_distance": 150.0,
  "waypoints": [[1250, 3490], [1400, 3490]],
  "diameter_text": "Dv50",
  "diameter_value": 50
}
```

| Поле | Тип | Обязательное | Описание |
|------|-----|-------------|----------|
| `id` | string | ✅ | Уникальный ID ребра |
| `source` | string | ✅ | ID узла-источника |
| `target` | string | ✅ | ID узла-цели (null для terminal → ребро пропускается в export) |
| `source_point` | `[y, x]` | — | Точка контакта с source узлом (row, col) |
| `target_point` | `[y, x]` | — | Точка контакта с target узлом (row, col) |
| `length` | float | ✅ | Длина ребра (пиксели) |
| `is_terminal` | bool | ✅ | Терминальное ребро (конец трубопровода) |
| `color` | string/null | — | Цвет (для bridge-рёбер) |
| `straight_line_distance` | float | — | Расстояние по прямой |
| `waypoints` | `[[y, x], ...]` | — | Промежуточные точки (для L-routing в UI) |
| `diameter_text` | string | — | Текст диаметра (`"Dv50"`, `"Dn100"`) — после OCR binding |
| `diameter_value` | float | — | Числовое значение диаметра (для FXML strokeWidth) |

---

## 4. Contours format

Файлы: `contours_auto.json` (SAM2), `contours_validated.json` (после редактирования). Генерируется `worker/tasks/contours.py`.

### Верхний уровень

```json
{
  "version": "1.0",
  "diagram_uid": "a1b2c3d4-...",
  "nodes": [ ... ],
  "stats": {
    "total": 134,
    "eligible": 134,
    "auto": 130,
    "manual_review": 4,
    "skipped": 0,
    "mean_confidence": 0.96
  }
}
```

### Node (контур)

```json
{
  "ann_id": 42,
  "category_id": 0,
  "class_name": "armatura_ruchn",
  "bbox": [1200, 850, 180, 160],
  "polygon_auto": [1205, 855, 1375, 855, 1378, 1005, ...],
  "polygon_validated": null,
  "confidence": 0.97,
  "status": "auto",
  "was_edited": false,
  "n_points": 12,
  "K": 0.85
}
```

| Поле | Тип | Обязательное | Описание |
|------|-----|-------------|----------|
| `ann_id` | int | ✅ | COCO annotation ID — ключ связи с graph node |
| `category_id` | int | ✅ | COCO category ID |
| `class_name` | string | ✅ | Имя класса |
| `bbox` | `[x, y, w, h]` | ✅ | COCO-формат bbox |
| `polygon_auto` | `[x1, y1, ...]` | ✅ | Автоматический полигон (SAM2 + smart snap), flat list |
| `polygon_validated` | `[x1, y1, ...]` / null | ✅ | Полигон после ручной правки (null если не редактировался) |
| `confidence` | float | ✅ | SAM2 confidence (0..1) |
| `status` | string | ✅ | `"auto"` (confidence ≥ threshold) или `"manual_review"` |
| `was_edited` | bool | ✅ | Был ли отредактирован оператором |
| `n_points` | int | — | Количество вершин полигона |
| `K` | float | — | Adaptive K — коэффициент кропа (зависит от размера bbox) |

### Stats

| Поле | Тип | Описание |
|------|-----|----------|
| `total` | int | Общее число обработанных аннотаций |
| `eligible` | int | Число аннотаций, подходящих для SAM2 (без skip_classes) |
| `auto` | int | Автоматически принятых (confidence ≥ threshold) |
| `manual_review` | int | Требующих ручной проверки |
| `skipped` | int | Пропущенных (skip_classes) |
| `mean_confidence` | float | Средняя confidence |

### Связь с графом

При генерации FXML (`task_generate_fxml`) контуры вливаются в граф по `ann_idx`: для каждого узла графа с `ann_idx` ищется контур с совпадающим `ann_id`, и `polygon_validated` (или `polygon_auto`) записывается в `node["segmentation"]`. Если прямого совпадения нет — используется IoU bounding box.

---

## 5. OCR format

### ocr_result.json

Результат `run_ocr_pipeline()`. Содержит два списка блоков: `target` (основные: KKS, диаметры) и `secondary` (прочие тексты).

```json
{
  "target": [
    {
      "text": "10LAA01AA101",
      "bbox": [1200, 850, 1380, 880],
      "confidence": 0.95,
      "class": "kks",
      "source": "surya_pass1"
    },
    {
      "text": "Dv50",
      "bbox": [2100, 900, 2200, 930],
      "confidence": 0.88,
      "class": "diameter",
      "source": "surya_pass2"
    }
  ],
  "secondary": [
    {
      "text": "Линия подпитки",
      "bbox": [3000, 100, 3400, 130],
      "confidence": 0.91,
      "class": "other",
      "source": "surya_pass1"
    }
  ]
}
```

| Поле | Тип | Описание |
|------|-----|----------|
| `text` | string | Распознанный текст |
| `bbox` | `[x1, y1, x2, y2]` | Bounding box текста |
| `confidence` | float | Confidence распознавания |
| `class` | string | Классификация: `"kks"`, `"diameter"`, `"other"`, `"noise"` |
| `source` | string | Источник: `"surya_pass1"`, `"surya_pass2"`, `"surya_pass3"`, `"paddle"` |

### ocr_binding.json

Результат привязки текста к узлам/рёбрам графа (автоматической или ручной).

```json
{
  "node_bindings": [
    {
      "node_id": "node_5",
      "kks_full": "10LAA01AA101",
      "kks_block": "10",
      "kks_system": "LAA",
      "kks_fn": "01",
      "kks_unit": "AA101",
      "confidence": 0.95,
      "source": "auto"
    }
  ],
  "edge_bindings": [
    {
      "edge_id": "edge_12",
      "diameter_text": "Dv50",
      "diameter_value": 50,
      "confidence": 0.88,
      "source": "auto"
    }
  ]
}
```

### ocr_validation.json

Результат ручной валидации OCR оператором. Формат аналогичен `ocr_binding.json` с добавлением полей `validated_by`, `validated_at`, правок оператора.

---

## 6. FXML output

Файл: `output/diagram.fxml`. Генерируется `modules/graph_to_fxml.py` из `graph_validated.json`.

FXML — XML-формат JavaFX, используемый в САПР. Содержит визуальные элементы: контролы оборудования (со скинами), полигоны (для элементов без скинов), линии соединений.

### Маппинг graph → FXML

4 ветки выбора FXML-элемента для каждого узла графа (проверяются последовательно):

| Graph node | FXML элемент | Критерий |
|-----------|-------------|----------|
| `class_name` в `CLASS_NAME_TO_SKIN` | `<ValveControl>`, `<PumpControl>`, `<HeaterControl>` и т.д. | Скин найден |
| `node["contours_all"]` присутствует | Множественные `<Polygon>` из contour_extractor | Для drossel/voronka — несколько полигонов на один узел |
| `node["segmentation"]` присутствует | `<Polygon>` из `node["segmentation"]` | Один полигон контура |
| Нет скина, нет segmentation | `<Rectangle>` по bbox | Fallback |

`SKIP_CLASS_NAMES` = `{'connector', 'voronka', 'annotation', 'truba', 'background'}` — классы, для которых `get_skin_info()` возвращает `None`. Это не «элементы без скина», а структурные / служебные узлы графа (коннекторы, аннотации, трубы-как-узлы). Для них branching идёт по `contours_all` → `segmentation` → `Rectangle`.

### Атрибуты контрола

| Атрибут | Источник |
|---------|---------|
| `layoutX`, `layoutY` | `bbox[0]`, `bbox[1]` (масштабированные) |
| `prefWidth`, `prefHeight` | `bbox[2]-bbox[0]`, `bbox[3]-bbox[1]` |
| `skinType` | `CLASS_NAME_TO_SKIN[class_name][1]` |
| `kks` | `node["kks_full"]` |
| `kksVisible` | `"true"` если `kks_full` присутствует |
| `kksFontSize` | Рассчитывается как `min(w, h) * 0.35` с ограничениями |
| `orientation` | `"HORIZONTAL"` / `"VERTICAL"` по соотношению сторон |

### Линии (рёбра)

Каждое ребро → `<Line>` или `<Polyline>` (если есть waypoints). Атрибуты: `startX/Y`, `endX/Y`, `strokeWidth` (масштабируется по `diameter_value`), `stroke="#333333"`.

### Масштабирование

При указании page_size (A3, A4 и т.д.) координаты масштабируются из пиксельных в миллиметры: `graph_scale = target_mm / image_pixels`.

---

## 7. Image артефакты

Все маски — PNG, одноканальные (grayscale), бинарные (0 или 255). Размер совпадает с оригинальным изображением.

| Файл | Формат | Содержимое |
|------|--------|-----------|
| `original/image.png` | PNG (RGB) | Исходный скан P&ID |
| `segmentation/node_mask.png` | PNG (grayscale, binary) | Маска узлов оборудования: белый = оборудование, чёрный = фон. Генерируется из COCO annotations (исключая `truba` и `annotation`) |
| `segmentation/pipe_mask.png` | PNG (grayscale, binary) | Маска трубопроводов: белый = труба. UNet++ ensemble (OR strategy) |
| `segmentation/pipe_mask_validated.png` | PNG (grayscale, binary) | Pipe mask после коррекции оператором |
| `segmentation/pipe_mask_refined.png` | PNG (grayscale, binary) | Pipe mask после adaptive dilate correction (OCR preprocessing) |
| `skeleton/skeleton.png` | PNG (grayscale, binary) | Начальный скелет (skeletonize из pipe_mask + node_mask) |
| `skeleton/skeleton_mask.png` | PNG (grayscale, binary) | Маска, восстановленная из скелета (для сравнения в UI) |
| `skeleton/skeleton_final.png` | PNG (grayscale, binary) | Финальный скелет (из validated масок) |
| `junction/junction_mask.png` | PNG (grayscale, binary) | Маска перекрёстков (CenterNet) |
| `junction/bridge_mask.png` | PNG (grayscale, binary) | Маска мостов (CenterNet) |
| `junction/junction_mask_validated.png` | PNG (grayscale, binary) | Перекрёстки после валидации |
| `junction/bridge_mask_validated.png` | PNG (grayscale, binary) | Мосты после валидации |
| `junction/points.json` | JSON | Центры перекрёстков/мостов от модели (см. ниже) |
| `junction/points_validated.json` | JSON | Центры после правки оператором (см. ниже) |
| `ocr/ocr_cleaned.png` | PNG (RGB) | Очищенное изображение для OCR (удалены pipe/node маски) |
| `detection/detection_overlay.png` | PNG (RGB) | Визуализация bbox поверх оригинала (debug) |
| `segmentation/segmentation_overlay.png` | PNG (RGB) | Визуализация масок поверх оригинала (debug) |
| `graph/graph_overlay.png` | PNG (RGB) | Визуализация графа поверх оригинала (debug) |

---

## Центры перекрёстков и мостов (`junction/points*.json`)

Два файла одного назначения — координаты центров квадратов, которыми
растеризуются перекрёстки и мосты.

**`points.json` — вариант воркера** (`worker/tasks/junction.py`,
`ArtifactType.JUNCTION_POINTS`). Точки — плоские пары `[x, y]`:

```json
{
  "junctions": [[1520, 880], [1533, 880]],
  "bridges": [[402, 1190]],
  "junction_threshold": 0.5,
  "bridge_threshold": 0.5,
  "n_tiles": 42,
  "time_sec": 18.7
}
```

Ключей `width` / `height` здесь **нет** (в отличие от CLI-варианта из
`modules/junction_segmentation/prepare_dataset.py`, где аннотации датасета их
содержат) — полагаться можно только на `junctions` / `bridges`.

**`points_validated.json` — вариант UI** (вкладка «Проверка узлов»,
`ArtifactType.JUNCTION_POINTS_VALIDATED`). Точки — объекты, у каждой хранится
последний применённый к ней размер квадрата:

```json
{
  "junctions": [{"x": 1520, "y": 880, "size": 9}],
  "bridges": [{"x": 402, "y": 1190, "size": 15}]
}
```

`size` обязателен: без него при следующем открытии вкладки экстрактор центров
работал бы с дефолтным окном 15 px и не нашёл бы ни одного окна в квадратах,
ужатых до меньшего размера — слипшаяся пара снова свернулась бы в один центр.
Редактор понимает оба формата (`SquareMaskEditor.load_points`).

---

## Остаток авто-раскладки (`graph/residual_defects.json`)

Артефакт `ArtifactType.RESIDUAL_DEFECTS`. Пишет задача раскладки
(`worker/tasks/layout.py` → `modules/graph/core/layout/residual.py`) рядом с
холстом `graph_canvas.json`; вкладка «Ручная правка» подсвечивает очаги
маркерами и панелью «Очаги». Производен от холста: при откате удаляется
вместе с ним (`app/api/rollback.py`).

```json
{
  "floor": 6.0,
  "invisible_edges": [
    {"edge_id": "e12", "nodes": ["n3", "n7"], "gap": 2.4, "point": [811.0, 402.5]}
  ],
  "box_on_magi": [
    {"node_id": "n9", "point": [520.0, 331.0]}
  ],
  "overlaps": [
    {"nodes": ["n2", "n5"], "area_px2": 84.0, "point": [1010.0, 640.0]}
  ],
  "total": 3,
  "canvas_sha": "5f1c…"
}
```

Пояснения:

- `point` — координаты очага `[x, y]` в системе холста 1920x1080.
- `invisible_edges` — рёбра с зазором форм меньше `floor` (труба невидима);
  `gap` — фактический зазор в px.
- `box_on_magi` — боксы, лежащие на чужих нарисованных магистралях.
- `overlaps` — нелегальные наложения пар блоков (наложения, унаследованные из
  детекции, легальны и в остаток не входят); `area_px2` — площадь пересечения.
- `total` — сумма очагов всех трёх видов.
- `canvas_sha` — проекция холста, для которого посчитан остаток; вкладка
  сверяет её с загруженным холстом и устаревший остаток не подсвечивает.

---

## Новые термины для GLOSSARY.md

| Термин | Описание |
|--------|----------|
| **node-link format** | Формат экспорта графа NetworkX: верхний уровень `{nodes: [], links: []}` |
| **smart snap** | Регуляризация полигонов SAM2: Douglas-Peucker simplification + выравнивание H/V рёбер |
| **skip_classes** | Классы COCO-аннотаций, исключаемые из SAM2 inference (настраивается в project config) |
| **domain profile** | YAML-конфигурация OCR для конкретного домена: regex паттерны, правила классификации, привязки |
| **auto / manual_review** | Статусы контура: `auto` = confidence ≥ threshold (принят автоматически), `manual_review` = требует ручной проверки |

---

## Реестр документации (обновление)

| # | Документ | Статус | Версия | Чат |
|---|---------|--------|--------|-----|
| 3 | `ARCHITECTURE.md` | ✅ Done | 1.0 | 2 |
| 4 | `STATUS_MACHINE.md` | ✅ Done | 1.0 | 3 |
| 5 | `DB_SCHEMA.md` | ✅ Done | 1.0 | 3 |
| 6 | `DATA_FORMATS.md` | ✅ Done | 1.0 | 4 |

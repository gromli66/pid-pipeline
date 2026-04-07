# Pipe Segmentation — сегментация труб на P&ID схемах

Модуль бинарной семантической сегментации трубопроводов на технологических схемах (P&ID / Piping and Instrumentation Diagrams). Принимает сканы чертежей высокого разрешения, выделяет трубы как сплошные линии, включая корректную обработку пунктирных линий.

---

## Содержание

1. [Обзор архитектуры](#обзор-архитектуры)
2. [Требования](#требования)
3. [Структура проекта](#структура-проекта)
4. [Быстрый старт](#быстрый-старт)
5. [Подготовка данных](#1-подготовка-данных-prepare)
6. [Обучение модели](#2-обучение-модели-train)
7. [Fine-tuning](#3-fine-tuning-finetune)
8. [Тестирование](#4-тестирование-test)
9. [Инференс](#5-инференс-infer)
10. [Конфигурация](#конфигурация)
11. [Модель](#модель)
12. [Loss-функции](#loss-функции)
13. [Аугментации](#аугментации)
14. [Постобработка](#постобработка)
15. [Обработка пунктира](#обработка-пунктирных-линий)
16. [FAQ и troubleshooting](#faq-и-troubleshooting)

---

## Обзор архитектуры

```
Входное изображение (скан P&ID, ~5000×3500 px, 300 DPI)
    │
    ▼
Бинаризация (adaptive threshold) ─── Превращает в чёрно-белое
    │
    ▼
Тайлинг (1024×1024 px, overlap 128) ─── Нарезка на фрагменты
    │
    ▼
UNet++ / EfficientNet-B3 ─── 4-канальный вход: RGB + маска узлов
    │                         1-канальный выход: вероятность "труба"
    ▼
Сборка с блендингом ─── Гауссов блендинг на границах тайлов
    │
    ▼
Постобработка ─── Направленное закрытие + skeleton gap filling
    │                + fill holes + удаление мелких компонент
    ▼
Бинарная маска труб (0/255)
```

Ключевые особенности:

- **4-канальный вход**: RGB-изображение (3 канала) + бинарная маска узлов оборудования (1 канал). 4-й канал помогает модели отличать трубы от рамок, штампов и таблиц.
- **UltimateLoss**: Dice + Focal (OHEM) + clDice — комбинация для overlap, hard examples и сохранения топологии линий.
- **Обработка пунктира**: двусторонний подход — аугментация при обучении + постобработка при инференсе.
- **SCSE attention** в decoder для улучшения деталей.

---

## Требования

```bash
# Python 3.9+
pip install torch torchvision
pip install segmentation-models-pytorch
pip install albumentations
pip install opencv-python
pip install scikit-image
pip install tqdm click pyyaml
pip install matplotlib
```

GPU: рекомендуется NVIDIA с ≥8 GB VRAM (RTX 4070 Laptop и выше).

---

## Структура проекта

```
pipe_segmentation/
├── __main__.py              # Entry point: python -m pipe_segmentation
├── cli.py                   # CLI команды (click)
├── config/
│   ├── default.yaml         # Полная конфигурация с комментариями
│   ├── defaults.py          # Константы по умолчанию
│   ├── config_loader.py     # Загрузка и мерж YAML + CLI
│   └── schemas.py           # Валидация конфига
├── data/
│   ├── augmentations.py     # Аугментации (вкл. DashedLine, RealisticDashed)
│   ├── coco_parser.py       # Извлечение масок из COCO JSON
│   ├── coco_to_masks.py     # Утилиты конвертации
│   ├── dataset.py           # PyTorch Dataset (4-канальный вход)
│   └── tiling.py            # Тайлинг + split + гауссов блендинг
├── inference/
│   ├── engine.py            # TiledInference — инференс на полных изображениях
│   ├── postprocessing.py    # Морфология + directional closing + gap filling
│   ├── preprocessing.py     # Бинаризация + нормализация для инференса
│   └── tta.py               # Test-Time Augmentation (flip, rotate)
├── model/
│   ├── architecture.py      # UNet++ создание, 4-канальная инициализация
│   ├── losses.py            # Dice + Focal(OHEM) + clDice = UltimateLoss
│   └── metrics.py           # Dice, IoU, Precision, Recall, F1
├── training/
│   ├── trainer.py           # Training loop (AMP, grad accum, early stop)
│   └── callbacks.py         # EarlyStopping, CSVLogger, Checkpointing
└── utils/
    ├── io.py                # Загрузка/сохранение, бинаризация с валидацией
    └── visualization.py     # Overlay, графики обучения
```

---

## Быстрый старт

```bash
# 1. Подготовка данных (COCO JSON → тайлы)
python -m pipe_segmentation prepare --config my_config.yaml

# 2. Обучение
python -m pipe_segmentation train --config my_config.yaml

# 3. Инференс на новых изображениях
python -m pipe_segmentation infer --config my_config.yaml \
    --images ./new_schemes \
    --checkpoint ./result_seg_pipe/experiments/checkpoints/best.pth
```

Все параметры в конфиге. CLI-аргументы переопределяют значения из конфига.

---

## 1. Подготовка данных (prepare)

Команда `prepare` выполняет полный pipeline подготовки: извлечение масок → бинаризация → split → тайлинг.

### Источники масок

**Вариант A: COCO JSON** (рекомендуется)
```yaml
paths:
  coco_annotations: "./data/raw/annotations.json"
```
Модуль автоматически извлечёт маски труб (категория `truba`) и маски узлов (все остальные категории кроме `annotation`).

**Вариант B: Готовые маски**
```yaml
paths:
  masks:
    pipes: "./data/masks/pipes"    # Бинарные PNG: белый=труба, чёрный=фон
    nodes: "./data/masks/nodes"    # Бинарные PNG: белый=узел, чёрный=фон
```

### CLI

```bash
python -m pipe_segmentation prepare --config my_config.yaml

# С переопределением путей:
python -m pipe_segmentation prepare --config my_config.yaml \
    --images ./other/images \
    --coco ./other/annotations.json \
    --output ./other/dataset \
    --tile-size 1024 \
    --overlap 128 \
    --split "0.7/0.15/0.15"
```

### Опции

| Параметр | Описание | По умолчанию |
|----------|----------|-------------|
| `--config` | YAML конфиг | default.yaml |
| `--images` | Папка с изображениями | из конфига |
| `--coco` | COCO JSON | из конфига |
| `--pipe-masks` | Папка с масками труб | из конфига |
| `--node-masks` | Папка с масками узлов | из конфига |
| `--output` | Выходная папка | из конфига |
| `--tile-size` | Размер тайла (px) | 1024 |
| `--overlap` | Перекрытие (px) | 128 |
| `--split` | Пропорции "train/val/test" | "0.7/0.15/0.15" |
| `--test-files` | Файл со списком тестовых изображений | — |
| `--binarize/--no-binarize` | Бинаризация | --binarize |
| `--seed` | Random seed | 42 |

### Результат

```
dataset_dir/
├── train/
│   ├── images/          # RGB тайлы 1024×1024
│   ├── pipe_masks/      # Маски труб (GT)
│   └── node_masks/      # Маски узлов (4-й канал)
├── val/
│   └── ...
├── test/
│   └── ...
├── tiling_results.json  # Статистика
└── prepare_config.json  # Конфиг для воспроизводимости
```

---

## 2. Обучение модели (train)

### CLI

```bash
python -m pipe_segmentation train --config my_config.yaml

# С переопределениями:
python -m pipe_segmentation train --config my_config.yaml \
    --data ./dataset \
    --epochs 100 \
    --batch-size 4 \
    --patience 20 \
    --device cuda
```

### Опции

| Параметр | Описание | По умолчанию |
|----------|----------|-------------|
| `--data` | Папка с dataset (после prepare) | из конфига |
| `--output` | Папка для результатов | из конфига |
| `--epochs` | Количество эпох | 50 |
| `--batch-size` | Размер батча | 2 |
| `--encoder-lr` | LR для encoder | 1e-5 |
| `--decoder-lr` | LR для decoder | 1e-4 |
| `--patience` | Early stopping patience | 15 |
| `--device` | cuda / cpu | cuda |
| `--seed` | Random seed | 42 |

### Что происходит внутри

1. Загрузка тайлов → PyTorch DataLoader
2. Создание UNet++ с 4-канальным входом (ImageNet pretrained + 4-й канал zeros+noise)
3. Аугментации: геометрические + DashedLine + RealisticDashedLine + noise/blur
4. Loss: Dice(1.0) + Focal-OHEM(1.0) + clDice(0.5)
5. Optimizer: AdamW с разными LR для encoder (1e-5) и decoder (1e-4)
6. Scheduler: CosineAnnealing или ReduceLROnPlateau (из конфига)
7. AMP (mixed precision) + gradient accumulation
8. Early stopping по val_dice
9. Сохранение лучшего и периодических чекпоинтов

### Результат

```
experiments_dir/
├── checkpoints/
│   ├── best.pth          # Лучшая модель по val_dice
│   └── epoch_10.pth      # Периодические чекпоинты
├── plots/
│   └── training_curves.png
├── training_stats.csv     # Метрики по эпохам
├── history.json
└── training_config.json
```

### Рекомендации по железу

| GPU | batch_size | accumulation_steps | Эффективный batch |
|-----|-----------|-------------------|-------------------|
| RTX 4070 Laptop (8 GB) | 2 | 4 | 8 |
| RTX 4090 (24 GB) | 8 | 1 | 8 |
| A100 (40 GB) | 16 | 1 | 16 |

---

## 3. Fine-tuning (finetune)

Fine-tuning автоматически выполняет `prepare` под капотом — передавайте ОРИГИНАЛЬНЫЕ изображения и маски, а не тайлы.

### CLI

```bash
# С готовыми масками
python -m pipe_segmentation finetune --config my_config.yaml \
    --images ./new_data/images \
    --pipe-masks ./new_data/masks/pipes \
    --checkpoint ./best_model.pth

# С COCO разметкой
python -m pipe_segmentation finetune --config my_config.yaml \
    --images ./new_data/images \
    --coco ./new_data/annotations.json \
    --checkpoint ./best_model.pth \
    --epochs 20 \
    --lr 1e-5
```

### Опции

| Параметр | Описание | По умолчанию |
|----------|----------|-------------|
| `--checkpoint` | Путь к модели для дообучения | обязательный |
| `--images` | Папка с ОРИГИНАЛЬНЫМИ изображениями | из конфига |
| `--pipe-masks` | Маски труб | из конфига |
| `--coco` | COCO JSON | из конфига |
| `--epochs` | Количество эпох | 20 |
| `--lr` | Learning rate (единый) | 1e-5 |
| `--freeze-encoder` | Заморозить encoder на N эпох | 5 |
| `--split` | Пропорции "train/val" | "0.8/0.2" |

---

## 4. Тестирование (test)

Тестирование работает на **ОРИГИНАЛЬНЫХ** изображениях полного разрешения. Тайлинг выполняется автоматически внутри (как SAHI).

### CLI

```bash
# С GT масками
python -m pipe_segmentation test --config my_config.yaml \
    --images ./test/images \
    --pipe-masks ./test/masks/pipes \
    --checkpoint ./best.pth

# С COCO GT
python -m pipe_segmentation test --config my_config.yaml \
    --images ./test/images \
    --coco ./test/annotations.json \
    --checkpoint ./best.pth \
    --tta \
    --postprocess
```

### Опции

| Параметр | Описание | По умолчанию |
|----------|----------|-------------|
| `--checkpoint` | Путь к модели | обязательный |
| `--images` | Папка с ОРИГИНАЛЬНЫМИ тестовыми изображениями | из конфига |
| `--pipe-masks` | GT маски труб | из конфига |
| `--coco` | COCO JSON с GT | из конфига |
| `--node-masks` | Маски узлов (4-й канал) | из конфига |
| `--threshold` | Порог бинаризации | 0.5 |
| `--tta/--no-tta` | Test-Time Augmentation | --tta |
| `--postprocess/--no-postprocess` | Постобработка | --postprocess |

### Результат

```
output/
├── masks/             # Предсказанные маски
├── overlays/          # Overlay визуализации
├── test_results.csv   # Метрики по каждому изображению
└── test_summary.json  # Средние метрики
```

### Выводимые метрики

- **Dice** — основная метрика, overlap между prediction и GT
- **IoU** — Intersection over Union
- **Precision** — доля правильных среди предсказанных
- **Recall** — доля найденных среди существующих
- **F1** — гармоническое среднее Precision и Recall

---

## 5. Инференс (infer)

Инференс на новых изображениях **без GT**. Тайлинг автоматический.

### CLI

```bash
# С COCO разметкой узлов (рекомендуется)
python -m pipe_segmentation infer --config my_config.yaml \
    --images ./new_schemes \
    --coco ./annotations.json \
    --checkpoint ./best.pth \
    --save-overlay

# С готовыми масками узлов
python -m pipe_segmentation infer --config my_config.yaml \
    --images ./new_schemes \
    --node-masks ./masks/nodes \
    --checkpoint ./best.pth

# Без масок узлов (4-й канал = нули, качество ниже)
python -m pipe_segmentation infer --config my_config.yaml \
    --images ./new_schemes \
    --checkpoint ./best.pth

# Один файл
python -m pipe_segmentation infer --config my_config.yaml \
    --images ./scheme.png \
    --checkpoint ./best.pth
```

### Опции

| Параметр | Описание | По умолчанию |
|----------|----------|-------------|
| `--checkpoint` | Путь к модели | обязательный |
| `--images` | Папка или файл | из конфига |
| `--coco` | COCO JSON (извлечёт маски узлов) | — |
| `--node-masks` | Готовые маски узлов | из конфига |
| `--output` | Папка для результатов | из конфига |
| `--threshold` | Порог | 0.5 |
| `--tta/--no-tta` | TTA | --tta |
| `--postprocess/--no-postprocess` | Постобработка | --postprocess |
| `--save-overlay/--no-save-overlay` | Сохранять overlay | --no-save-overlay |

### Результат

```
output/
├── masks/             # Бинарные маски (0/255)
├── node_masks/        # Извлечённые маски узлов (если --coco)
├── overlays/          # Overlay (если --save-overlay)
└── inference_log.csv  # Лог: файл, время, coverage
```

---

## Конфигурация

Полный конфиг — `config/default.yaml`. Создайте копию и отредактируйте:

```bash
cp config/default.yaml my_config.yaml
```

### Ключевые секции

**Бинаризация** — превращает скан в чёрно-белое:
```yaml
preprocessing:
  binarization:
    enabled: true
    method: "adaptive"      # adaptive | otsu | fixed
    adaptive:
      block_size: 31        # Размер окна (нечётное, меньше = сохраняет пунктир)
      c: 7                  # Порог (меньше = менее агрессивно)
```

**Тайлинг** — нарезка на фрагменты для GPU:
```yaml
tiling:
  tile_size: 1024           # Размер тайла
  overlap: 128              # Перекрытие (12.5%)
  filtering:
    min_pipe_pixels: 100    # Мин. пикселей труб для сохранения тайла
```

**Loss** — комбинированная функция потерь:
```yaml
loss:
  weights:
    dice: 1.0               # Overlap
    focal: 1.0              # Hard examples
    cldice: 0.5             # Topology (непрерывность линий)
  ohem:
    enabled: true
    top_k_ratio: 0.5        # Loss по 50% самых сложных пикселей
```

**Постобработка** — соединение разрывов:
```yaml
inference:
  postprocessing:
    directional_closing:
      enabled: true
      kernel_length: 20       # Длина линейного ядра
    skeleton_gap_fill:
      enabled: true
      max_gap: 80             # Макс. зазор для соединения (px)
      direction_tolerance: 30  # Допуск направления (°)
    fill_holes:
      enabled: true
      max_hole_size: 500
```

---

## Модель

### Архитектура

**UNet++** (Nested U-Net) с encoder **EfficientNet-B3** pretrained на ImageNet.

- Вход: `[B, 4, 1024, 1024]` — RGB (3 канала) + маска узлов (1 канал)
- Выход: `[B, 1, 1024, 1024]` — вероятность "труба" для каждого пикселя
- Decoder: SCSE attention (Spatial + Channel Squeeze-and-Excitation)
- Параметров: ~12M, ~48 MB (float32)

### 4-й канал (маска узлов)

При создании модели:
1. Создаётся UNet++ с 3 каналами (загружаются ImageNet веса)
2. Создаётся UNet++ с 4 каналами (random init)
3. Копируются все веса кроме первого conv
4. 4-й канал инициализируется нулями + малый шум (0.01 std)

Это гарантирует что при отсутствии маски узлов (все нули) модель не получает ложный сигнал.

### Нормализация

- RGB: ImageNet normalization `(x - mean) / std`
- Node mask: `(x - 0.05) / 0.2` — масштабирование к диапазону RGB каналов

Нормализация **одинаковая** при обучении (`dataset.py`) и инференсе (`preprocessing.py`).

---

## Loss-функции

### UltimateLoss = Dice + Focal(OHEM) + clDice

```
Total = 1.0 × Dice + 1.0 × Focal_OHEM + 0.5 × clDice
```

**DiceLoss** — основная метрика для сегментации. Работает с дисбалансом классов (1-3% труб, 97-99% фон). Поощряет непрерывные линии.

**FocalLoss + OHEM** — фокусируется на сложных пикселях (границы труб, пунктир, перекрёстки). OHEM (Online Hard Example Mining) считает loss только по 50% самых сложных пикселей — сплошные трубы с высоким confidence перестают доминировать в gradient.

**clDice (Centerline Dice)** — специальный loss для линейных структур. Выполняет мягкую скелетонизацию (5 итераций min-pooling) и считает Dice по скелетам. Штрафует за разрывы в центральной линии трубы — главная защита от потери пунктира.

### pos_weight

Автоматически вычисляется из данных: `neg_pixels / pos_pixels`, clamp [1, 50]. При coverage 2% труб → pos_weight ≈ 49.

---

## Аугментации

Применяются **только при обучении** (train и finetune). Validation и inference — только нормализация.

### Pipeline аугментаций

```
1. DashedLineAugmentation (p=0.25)
   └─ Стирает куски трубы → модель учится достраивать
2. RealisticDashedLineAugmentation (p=0.35)
   └─ Рисует пунктир на изображении, маска остаётся сплошной
   └─ Модель учится: "вижу пунктир → это труба"
3. RandomRotate90 (p=1.0) + Flip (p=0.5)
4. Rotate ±10° (p=0.2)
5. ElasticTransform (p=0.15)
6. GridDistortion (p=0.1)
7. CoarseDropout (p=0.25)
8. BrightnessContrast (p=0.5) + Gamma (p=0.3) + CLAHE (p=0.2)
9. GaussNoise (p=0.25)
10. GaussianBlur / MotionBlur (p=0.15)
11. Normalize (ImageNet) + ToTensor
```

### RealisticDashedLineAugmentation (новое)

Ключевая аугментация для обработки пунктира. Алгоритм:

1. Берёт случайные 10-40% связных компонент труб
2. Скелетонизирует каждую компоненту
3. Стирает оригинальную трубу (заливает белым)
4. Рисует пунктирную линию по скелету: тире 15-40px, зазор 10-30px
5. **Маска остаётся сплошной** — модель учится что пунктир = труба

---

## Постобработка

Pipeline постобработки при инференсе:

```
1. Directional closing
   └─ Линейные ядра (20px) под 0°/45°/90°/135°
   └─ Соединяет мелкие разрывы вдоль направления трубы
   └─ Не соединяет параллельные линии перпендикулярно

2. Skeleton gap filling
   └─ Скелетонизация → поиск endpoints
   └─ Определение направления в каждом endpoint
   └─ Поиск ближайшего endpoint в том же направлении (±30°)
   └─ Соединение линией если расстояние ≤ 80px

3. Morphological closing (5×5 ellipse)
4. Morphological opening (3×3 ellipse)

5. Fill holes
   └─ Заполнение дыр ≤ 500px внутри контуров труб

6. Remove small objects
   └─ Удаление компонент < 100px
```

### Настройка max_gap

Если пунктир на ваших чертежах имеет зазоры больше 80px — увеличьте:

```yaml
inference:
  postprocessing:
    skeleton_gap_fill:
      max_gap: 120           # Увеличить для крупного пунктира
      direction_tolerance: 40 # Можно расширить допуск
```

---

## Обработка пунктирных линий

Проблема: на P&ID пунктирные линии обозначают трубы (импульсные, вспомогательные), но модель может их терять. Решение — трёхуровневый подход.

### Уровень 1: Бинаризация (сохранение)

Смягчённые параметры адаптивной бинаризации (`block_size=31, c=7`) лучше сохраняют короткие штрихи пунктира по сравнению с агрессивными (`51, 10`).

### Уровень 2: Обучение (распознавание)

- **RealisticDashedLineAugmentation** — модель видит пунктирные линии при обучении и учится классифицировать их как трубы
- **DashedLineAugmentation** — модель учится достраивать пропуски в сплошных линиях
- **clDice loss** — штрафует за разрывы в центральной линии

### Уровень 3: Постобработка (восстановление)

- **Directional closing** — соединяет мелкие зазоры (до 20px)
- **Skeleton gap filling** — соединяет крупные зазоры (до 80px) с учётом направления трубы

---

## FAQ и troubleshooting

### Модель даёт false positives на рамках и штампах

Убедитесь что маска узлов (`node_masks`) покрывает зоны штампа. Или добавьте категорию "рамка" в COCO разметку — она попадёт в node_mask и модель научится игнорировать эти области.

### Dice < 0.85

Проверьте:
- Бинаризация: визуально проверьте что трубы видны после `binarize_to_black_white()`
- Маски: убедитесь что маски строго бинарные (0 и 255), а не grayscale
- LR: encoder_lr должен быть в 10× меньше decoder_lr
- Данных мало: включите все аугментации, попробуйте finetune с freeze_encoder=5

### Пунктир не распознаётся

1. Проверьте что пунктирные трубы размечены классом `truba` в COCO
2. Проверьте бинаризацию — уменьшите `block_size` и `c`
3. Увеличьте `skeleton_gap_fill.max_gap`
4. Включите TTA (`--tta`)

### Out of Memory

Уменьшите `batch_size` и увеличьте `accumulation_steps` пропорционально:
```yaml
training:
  batch_size: 1
  accumulation_steps: 8    # Эффективный batch = 8
```

### Windows: num_workers > 0 падает

Оставьте `num_workers: 0` в конфиге. Это ограничение Windows multiprocessing.

### Как добавить новые данные без переобучения с нуля

Используйте `finetune`:
```bash
python -m pipe_segmentation finetune --config my_config.yaml \
    --images ./new_images \
    --coco ./new_annotations.json \
    --checkpoint ./best.pth \
    --epochs 20 --lr 1e-5 --freeze-encoder 5
```

### Как объединить несколько COCO JSON

Используйте скрипт `merge_coco.py`:
```bash
python merge_coco.py ./папка_с_json merged.json
```

---

## Версия

**v2.0** — 23 исправления и улучшения:
- Смягчённая бинаризация для пунктира
- RealisticDashedLineAugmentation
- Directional closing + skeleton gap filling (до 80px)
- OHEM в Focal Loss
- SCSE attention в decoder
- clDice: вес 0.5, 5 итераций
- Гауссов блендинг тайлов
- TTA по умолчанию
- Настраиваемый scheduler
- Нормализация node-маски
- Обработка изображений меньше tile_size

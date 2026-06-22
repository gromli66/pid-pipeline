# MODELS.md — ML-модели P&ID Pipeline

**Аудитория:** ML  
**Версия:** 1.1  
**Обновлено:** 2026-04-09  
**Связанные документы:** [ARCHITECTURE.md](ARCHITECTURE.md), [WORKER_TASKS.md](WORKER_TASKS.md), [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md), [MODULES.md](MODULES.md), [OCR_PIPELINE.md](OCR_PIPELINE.md)

---

## Оглавление

- [1. Обзор моделей](#1-обзор-моделей)
- [2. YOLOv8m + SAHI (Node Detection)](#2-yolov8m--sahi-node-detection)
- [3. UNet++ Ensemble (Pipe Segmentation)](#3-unet-ensemble-pipe-segmentation)
- [4. Junction/Bridge CenterNet (Junction Segmentation)](#4-junctionbridge-centernet-junction-segmentation)
- [5. SAM2 Hiera Small + LoRA (Contour Extraction)](#5-sam2-hiera-small--lora-contour-extraction)
- [6. Surya + PaddleOCR (Text Recognition)](#6-surya--paddleocr-text-recognition)

---

## 1. Обзор моделей

Pipeline использует пять моделей, выполняемых последовательно (кроме SAM2, который работает параллельно с graph/OCR):

| # | Модель | Задача | Архитектура | Input | Checkpoint | GPU RAM |
|---|--------|--------|-------------|-------|------------|---------|
| 1 | YOLOv8m + SAHI | Детекция узлов оборудования | YOLOv8 medium | BGR 1280px tiles | `best.pt` (~50 MB) | ~4 GB |
| 2 | UNet++ ensemble | Сегментация труб | EfficientNet-B3 + UNet++ | 4ch 1024px tiles | `best_model.pth` (~47 MB) × 2 | ~3 GB |
| 3 | CenterNet junction | Детекция junction/bridge точек | EfficientNet-B2 + UNet++ | 5ch 512px tiles | `best.pth` (~30 MB) | ~2 GB |
| 4 | SAM2 + LoRA | Извлечение контуров символов | Hiera Small + LoRA r=8 | 6ch 1024px crops | `sam2_pid_best.pth` (~5 MB LoRA) + base (~150 MB) | ~4 GB |
| 5 | Surya + PaddleOCR | Распознавание текста | Transformer (Surya) + CRNN (Paddle) | RGB crops | Предобученные | ~2 GB |

Все модели запускаются на GPU через Celery workers. YOLO, UNet++, Junction и SAM2 работают в очереди `gpu` (или `sam2` для контуров). OCR-модели — в очереди `ocr`. Подробнее о workers и очередях — см. [WORKER_TASKS.md](WORKER_TASKS.md).

---

## 2. YOLOv8m + SAHI (Node Detection)

**Файлы:** `modules/yolo_detector/detector.py`, `modules/yolo_detector/config.py`, `training/trainer.py`, `pipelines/train_pipeline.py`, `pipelines/finetune_pipeline.py`, `stage2_finetune.py`

### 2.1 Архитектура и inference

Модель — стандартный YOLOv8m (medium) из пакета `ultralytics`. Inference выполняется через SAHI (Slicing Aided Hyper Inference) — изображение нарезается на тайлы с перекрытием, детекции со всех тайлов объединяются и фильтруются NMS.

Ключевые параметры inference:

| Параметр | Тип | Default | Описание |
|----------|-----|---------|----------|
| `confidence` | float | 0.8 | Порог уверенности детекции |
| `iou_threshold` | float | 0.5 | Порог IoU для NMS |
| `sahi_slice_size` | int | 1280 | Размер тайла SAHI (px) |
| `sahi_overlap_ratio` | float | 0.25 | Перекрытие между тайлами |
| `class_agnostic_nms` | bool | False | Cross-class NMS для дубликатов на границах тайлов |
| `apply_preprocessing` | bool | False | Применять preprocessing при inference |

Класс `NodeDetector` использует ленивую загрузку модели — YOLO и SAHI инициализируются при первом вызове `detect()`.

Выход `detect()` — список dict с ключами: `class_id`, `class_name`, `x_center`, `y_center`, `width`, `height` (нормализованные), `confidence`, `bbox` ([x1, y1, x2, y2] абсолютные). Доступны конверторы `detections_to_yolo()` и `detections_to_coco()`.

Постобработка `resolve_overlaps()` — разрешение взаимных перекрытий после SAHI + NMS: при mutual overlap ≥ 70% одинаковые классы — оставляем больший бокс, разные классы — оставляем с большим confidence.

### 2.2 Preprocessing

Preprocessing воспроизводит пайплайн обучения и применяется к данным перед детекцией (опционально):

NLM (шумоподавление, h=10) → CLAHE (контраст, clip=2.0, grid 8×8) → Otsu (бинаризация) → Erode (утолщение линий, kernel 2×2, 1 итерация)

Параметры зафиксированы как атрибуты класса `NodeDetector` и не настраиваются в runtime.

> **Примечание к коду:** В `detector.py` результат erode хранится в переменной `dilated` — misleading naming. Операция корректна (erode на бинарном изображении утолщает линии), но имя переменной вводит в заблуждение.

Модуль `data/preprocessing.py` реализует два режима бинаризации для подготовки данных: `full` (NLM → CLAHE → Otsu → Dilate, для обучения) и `otsu` (только Otsu, для быстрой обработки). Для больших изображений (> 8000px) автоматически уменьшается `searchWindowSize` NLM.

### 2.3 Классы

Модель обучена на **36 классов** оборудования P&ID (от `armatura_ruchn` до `strelka`). Полный маппинг — в `modules/yolo_detector/config.py`.

При обучении классы 34 (annotation), 36 (truba), 37 (unknown) удалены из датасета. Классы 35 (output) и 38 (strelka) переиндексированы в 34 и 35. Обратный маппинг `REVERSE_REINDEX` ({34→35, 35→38}) применяется при inference для совместимости с исходной разметкой.

### 2.4 Dataset и подготовка данных

Подготовка данных выполняется пайплайном `TrainPipeline` (обучение с нуля) или `FinetunePipeline` (адаптация).

**Бинаризация.** Все схемы проходят preprocessing (§2.2). Метод `full` для обучения, `otsu` для инференса.

**Тайлинг** (`data/tiling.py`). P&ID схемы (~4900×3500 px при 300 DPI) нарезаются на тайлы 1280×1280 px с overlap 25%. Объекты, видимые менее чем на 60%, отбрасываются (`min_visible_ratio=0.6`). Для каждого тайла создаётся forbidden-маска (трубы + аннотации) — области, куда нельзя вставлять объекты при CPA.

**Стратифицированный split** (`data/splitting.py`). Двухэтапное разбиение: сначала схемы на Test / TrainVal (предотвращает утечку данных между тайлами одной схемы), затем тайлы на Train / Val. Для редких классов (≤ 3 схемы) все схемы остаются в TrainVal. Для 4–5 схем — минимум 1 в Test.

**Copy-Paste аугментация** (`augmentation/copy_paste.py`). Компенсирует дисбаланс классов (от 3 до 3000 примеров на класс). Вырезает объекты из тренировочных тайлов и вставляет на другие тайлы с проверкой forbidden-масок. Правила вставки: 70% полностью видимые, 20% частично видимые, 10% с перекрытием. При вставке — поворот (0/90/180/270°), яркость (±15), erode/dilate линий.

**Rotation аугментация** (`augmentation/rotation.py`). Поворот на 90°, 180°, 270°. Три режима: `all` (4× данных), `random_1` (2×), `random_2` (3×). В finetune pipeline: old-схемы — `all`, new-схемы — `random_1` для баланса ~35:65 (old:new).

### 2.5 Обучение

Три сценария обучения:

**TrainPipeline** (`pipelines/train_pipeline.py`) — полный цикл с нуля. Оркестрирует все шаги: preprocessing → split → tiling → CPA → rotation → YOLO train. Test фиксируется навсегда при первом запуске.

**FinetunePipeline** (`pipelines/finetune_pipeline.py`) — адаптация к новому стилю схем. Раздельно обрабатывает old и new схемы (тайлинг, CPA, rotation с разными режимами), затем объединяет для обучения.

**Stage 2 finetune** (`stage2_finetune.py`) — дообучение только на новых данных после Stage 1 (old+new). lr=0.0001, 20–30 эпох.

Обучение через класс `YOLOTrainer` (`training/trainer.py`):

| Параметр | Train | Finetune |
|----------|-------|----------|
| Base model | `yolov8m.pt` | `best.pt` от Stage 1 |
| Epochs | 100 | 50 |
| Batch size | 4 | 4 |
| Image size | 1280 | 1280 |
| Optimizer | AdamW | AdamW |
| lr0 | 0.001 | 0.0001 |
| lrf | 0.01 | 0.01 |
| Patience | 25 | 25 |
| box loss weight | 7.5 | 7.5 |

Встроенные аугментации YOLO отключены (аугментации применены в пайплайне). `box=7.5` — важна точная локализация оборудования (совпадает с default ultralytics, оставлено явно для фиксации).

### 2.6 Метрики

Лучший результат (epoch 59, 73 эпохи обучения):

| Метрика | Значение |
|---------|----------|
| mAP@50 | **0.954** |
| mAP@50-95 | **0.812** |
| Precision | 0.971 |
| Recall | 0.926 |

Динамика обучения: mAP@50 выходит на плато ~0.94+ к эпохе 40, финальные эпохи 72–73 показывают деградацию (вероятно, corrupted batch или нестабильность — `cls_loss=inf`).

### 2.7 Развёртывание

Checkpoint `best.pt` монтируется в Docker volume. Путь указывается в YAML-конфиге проекта (секция `detection.weights`). Worker `detection` загружает модель при первом вызове (ленивая загрузка). SAHI slice_size адаптируется под размер входного изображения.

---

## 3. UNet++ Ensemble (Pipe Segmentation)

**Файлы:**

*Model:* `modules/pipe_segmentation/model/architecture.py`, `modules/pipe_segmentation/model/losses.py`, `modules/pipe_segmentation/model/metrics.py`

*Training:* `modules/pipe_segmentation/training/trainer.py`, `modules/pipe_segmentation/training/callbacks.py`

*Inference:* `modules/pipe_segmentation/inference/engine.py` (TiledInference), `modules/pipe_segmentation/inference/ensemble.py`, `modules/pipe_segmentation/inference/preprocessing.py`, `modules/pipe_segmentation/inference/postprocessing.py`, `modules/pipe_segmentation/inference/tta.py` (Test-Time Augmentation)

*Data:* `modules/pipe_segmentation/data/augmentations.py`, `modules/pipe_segmentation/data/tiling.py`, `modules/pipe_segmentation/data/dataset.py`, `modules/pipe_segmentation/data/coco_parser.py`, `modules/pipe_segmentation/data/coco_to_masks.py`

*Config:* `modules/pipe_segmentation/config/defaults.py`, `modules/pipe_segmentation/config/config_loader.py`, `modules/pipe_segmentation/config/schemas.py`

*CLI:* `modules/pipe_segmentation/cli.py`

### 3.1 Архитектура

**Base model:** UNet++ (Nested U-Net) через `segmentation_models_pytorch`. Encoder — EfficientNet-B3 с pretrained ImageNet весами. Вход — 4 канала: RGB (3ch) + node_mask (1ch). Выход — 1 класс (binary: труба / не труба).

**4-канальный вход.** Создание модели: сначала загружаются ImageNet веса для 3ch, затем первый conv расширяется до 4ch. 4-й канал инициализируется нулями + малый шум N(0, 0.01) (не mean(RGB) — это давало фантомный сигнал на пустой маске).

> **Примечание:** Docstring `create_model()` в `architecture.py` до сих пор содержит устаревшее описание «4-й канал = mean(RGB weights)». Это legacy-текст до FIX 5.1. Реальная логика — zeros + noise (строки 244-245).

**DualHeadModel** — обёртка для multi-task learning. Добавляет вторую segmentation head (1×1 Conv2d) для предсказания скелета труб. При `training` возвращает `(mask_logits, skeleton_logits)`, при `eval` — только `mask_logits` (backward compatible). Skeleton head инициализируется Xavier.

**Differential LR.** Encoder (pretrained) обучается с меньшим lr, decoder (random init) — с большим. Функция `get_parameter_groups()` создаёт две группы для optimizer.

**Freeze/Unfreeze.** `freeze_encoder()` / `unfreeze_encoder()` — замораживание encoder на первые N эпох при finetune.

### 3.2 Loss: UltimateLoss v3

Комбинированный loss из трёх компонент:

```
Total = dice_weight × Dice + focal_weight × Focal(OHEM) + clce_weight × clCE
```

> **Именование:** В формулах и документации параметр назывался `topo_weight`. В коде `UltimateLoss` поле хранится как `self.topo_weight`, но конструктор принимает `clce_weight=...`. В `defaults.py` ключ — `clce_weight`. Используйте `clce_weight` при настройке.

**DiceLoss** — стандартный Dice с smooth=1.0.

**FocalLossOHEM** — Focal Loss (alpha=0.25, gamma=2.0) + OHEM: берутся только top 50% hardest pixels. `pos_weight` вычисляется автоматически из данных (clamp ≤ 50).

**ClCELoss** (Centerline Cross-Entropy, MICCAI 2024) — topology-preserving loss. Заменил clDice (CVPR 2021) в v3. BCE на soft-скелетах предсказания и GT. Два члена: sensitivity (pred должен покрывать скелет GT) + precision (GT должен покрывать скелет pred). Soft-скелетонизация — 10 итераций морфологической эрозии через max_pool (было 5, увеличено для зазоров пунктира).

**MultiTaskLoss** (для DualHead) — оборачивает UltimateLoss + SkeletonBCELoss (pos_weight=20):

```
Total = mask_loss + skeleton_weight × skeleton_BCE
```

Конфигурация loss (из `defaults.py`):

| Параметр | Значение в defaults.py | Примечание |
|----------|------------------------|------------|
| `dice_weight` | 1.0 | |
| `focal_weight` | 1.0 | |
| `clce_weight` | 0.7 | ⚠️ Конструктор `UltimateLoss` имеет default=0.5; при запуске через Trainer используется значение из defaults.py (0.7) |
| `focal_alpha` | 0.25 | |
| `focal_gamma` | 2.0 | |
| `cldice_iters` | 10 | |
| `ohem_top_k_ratio` | 0.5 | |
| `skeleton_weight` | 0.5 | |
| `skeleton_pos_weight` | 20.0 | |

### 3.3 Метрики

Доступные метрики (`modules/pipe_segmentation/model/metrics.py`): Dice, IoU, PixelAccuracy, Precision, Recall, F1. Класс `MetricsTracker` аккумулирует метрики по батчам. Функция `calculate_metrics()` — полный расчёт для одного изображения (numpy).

Лучший результат (epoch 9 из 24, early stopping):

| Метрика | Значение |
|---------|----------|
| **Val Dice** | **0.866** |
| Val IoU | 0.788 |
| Val Precision | 0.835 |
| Val Recall | 0.922 |
| Val F1 | 0.877 |

Динамика: Dice быстро растёт до ~0.86 к epoch 8–9, затем медленно колеблется. Recall стабильно высокий (0.92+), Precision — узкое место. Final epoch 24 показал деградацию (Dice 0.788) — early stopping сработал корректно.

### 3.4 Dataset и подготовка данных

**4-канальный вход:** каналы 0–2 — RGB (бинаризованное изображение), канал 3 — маска узлов (node_mask, binary 0/255). Нормализация: RGB по ImageNet статистикам, node_mask — mean=0.05, std=0.2.

**Тайлинг** (`modules/pipe_segmentation/data/tiling.py`): tile_size=1024, overlap=128 (stride=896). Фильтрация пустых тайлов: min_pipe_pixels=100, empty_threshold=0.90 (>90% белого — отбросить). Бинаризация: метод `adaptive` (block_size=31, C=7).

**Аугментации** (`modules/pipe_segmentation/data/augmentations.py`): геометрические (flip, rotate90, shift-scale-rotate, elastic), цветовые (brightness/contrast, gamma), шумовые (gaussian noise), blur (motion, gaussian), pipe-specific: `RealisticDashedLineAugmentation` (имитация пунктира: dash 10–50px, gap 8–35px, thickness 1–3px, fraction 15–50% труб), `CoarseDropout` (max 8 holes 32×32px), dashed_line gap augmentation (dilate_radius=4). Быстрая скелетонизация через `cv2.ximgproc.thinning` (10–30× быстрее skimage).

### 3.5 Обучение

Класс `Trainer` (`modules/pipe_segmentation/training/trainer.py`):

| Параметр | Train | Finetune |
|----------|-------|----------|
| Epochs | 50 | 20 |
| Batch size | 2 | 2 |
| Accumulation steps | 4 (effective batch 8) | 4 |
| Encoder LR | 1e-5 | 1e-5 |
| Decoder LR | 1e-4 | 1e-5 |
| Weight decay | 1e-4 | 1e-5 |
| Patience | 15 | 10 |
| Gradient clip | 1.0 | 1.0 |
| Scheduler | CosineAnnealing | CosineAnnealing |
| AMP | True | True |
| Freeze encoder | — | 0 epochs (default); рекомендуется 5 epochs |

> **Freeze encoder при finetune:** Default в коде (`DEFAULT_FINETUNE_FREEZE_ENCODER`) = 0, т.е. encoder не замораживается. Рекомендуемое значение — 5 epochs (передать `--freeze-encoder-epochs 5` или задать в конфиге).

Тренер поддерживает gradient accumulation, mixed precision (AMP), differential LR (encoder/decoder), early stopping по val_dice, CSV logging, checkpoint callback (`training/callbacks.py`, best model по val_dice), графики обучения.

### 3.6 Inference

**TiledInference** (`inference/engine.py`) — нарезка на тайлы 1024px с overlap 128px, inference побатчно, сшивка предсказаний.

**TTA** (`inference/tta.py`, Test-Time Augmentation) — flip (horizontal + vertical), усреднение предсказаний.

**Ensemble OR** (`modules/pipe_segmentation/inference/ensemble.py`) — загружаются два чекпоинта (например, plain UNet++ + DualHeadModel), каждый прогоняется через TiledInference, результаты комбинируются стратегией OR: пиксель = труба если хотя бы одна модель так считает. Даёт +4% Recall при том же Precision. Поддерживаемые стратегии: `or`, `and`, `mean`, `weighted_mean`.

**Постобработка:** remove_small_objects (< 100px), skeleton_gap_fill (max_gap=40px, direction_tolerance=20°), fill_holes (max_hole_size=500px), remove_border_frame (margin=30px).

### 3.7 Развёртывание

Два checkpoint монтируются в Docker volume. Пути — в YAML-конфиге (секция `segmentation`). Worker `segmentation` загружает `EnsembleInference` при первом вызове. Threshold = 0.5 по умолчанию.

---

## 4. Junction/Bridge CenterNet (Junction Segmentation)

**Файлы:**

*Model:* `modules/junction_segmentation/model.py`, `modules/junction_segmentation/loss.py` (CenterNetFocalLoss), `modules/junction_segmentation/config.py`

*Training:* `modules/junction_segmentation/train.py`, `modules/junction_segmentation/dataset.py` (TiledInferenceDataset, JunctionDataset)

*Inference:* `modules/junction_segmentation/inference.py`, `modules/junction_segmentation/batch_inference.py`, `modules/junction_segmentation/metrics.py` (extract_local_maxima, F1 evaluation)

*Утилиты:* `modules/junction_segmentation/find_thresholds.py` (оптимизация порогов), `modules/junction_segmentation/prepare_junction_dataset.py` (подготовка данных)

### 4.1 Архитектура

**JunctionSegModel** — UNet++ с EfficientNet-B2 encoder, 5-канальным входом и 2-канальным выходом (junction heatmap + bridge heatmap).

**Каналы входа (5ch):** RGB (3ch) + pipe_mask (1ch) + skeleton (1ch).

**Skeleton Attention Gate.** Dilated skeleton (dilation 20px через max_pool) → soft multiplication на decoder features. Мягкий gate: `attention * 0.9 + 0.1` (background не обнуляется полностью).

**AuxHead.** Tile-level binary classifier: "содержит ли тайл хотя бы одну точку?" AdaptiveAvgPool → Linear(128) → ReLU → Dropout(0.3) → Linear(1). Помогает модели быстрее обучиться отличать пустые тайлы.

**CenterNet init.** Bias seg head = −4.6 → sigmoid ≈ 0.01 → начальный loss ≈ 5 (стандартный приём из CenterNet/CornerNet). Без этого: sigmoid(0) = 0.5 → начальный loss ≈ 60 000.

**forward()** возвращает raw logits (без sigmoid) — для численной стабильности loss через `F.logsigmoid`. **predict()** — с sigmoid, для inference.

### 4.2 Loss

CenterNet focal loss (`loss.py`, alpha=2.0, beta=4.0): для каждого канала (junction, bridge) отдельно. Aux loss — BCE на tile-level prediction (weight 0.1). Total = junction_loss + bridge_loss + 0.1 × aux_loss.

### 4.3 Метрики

Метрики (`metrics.py`) вычисляются по-point: предсказанные пики (NMS на heatmap) сопоставляются с GT точками в радиусе `match_radius=15px`. Для каждого класса (junction, bridge): TP, FP, FN → Precision, Recall, F1. Итоговая метрика — `val_f1_combined` = среднее F1 junction и F1 bridge.

Результаты threshold analysis (`find_thresholds.py`, на валидации, 2751 junction + 439 bridge GT):

| Класс | Threshold | Precision | Recall | F1 |
|-------|-----------|-----------|--------|----|
| Junction (best F1) | **0.35** | 0.945 | 0.969 | **0.957** |
| Junction (P≥95%) | 0.40 | 0.953 | 0.959 | 0.956 |
| Bridge (best F1) | **0.35** | 0.906 | 0.941 | **0.923** |
| Bridge (P≥95%) | 0.60 | 0.954 | 0.800 | 0.870 |

Рекомендованные пороги для production: junction=0.40, bridge=0.60 (баланс precision ≥ 95%).

### 4.4 Dataset и подготовка данных

**5-канальный вход:** RGB + pipe_mask + skeleton. Pipe_mask и skeleton генерируются из pipe segmentation. Класс `JunctionDataset` (`dataset.py`) реализует загрузку и препроцессинг данных, `TiledInferenceDataset` — нарезку на тайлы при inference.

**GT heatmaps.** Gaussian spot с sigma=4.0 в позиции каждой GT точки. Два канала: junction (0) и bridge (1).

**Тайлинг:** tile_size=512, positive_ratio=0.6 (60% тайлов содержат хотя бы одну точку), epoch_tiles=2000 (виртуальные тайлы/эпоху), jitter_px=100 (случайный сдвиг центра тайла).

**Аугментации:** HFlip(0.5), VFlip(0.5), Rotate90(0.5), Brightness(0.3, limit=0.2), GaussNoise(0.2, var=10).

**Подготовка данных:** Скрипт `prepare_junction_dataset.py` генерирует GT-аннотации и тайлы из размеченных данных.

**Init from pipe_seg.** Encoder может инициализироваться из весов pipe segmentation (4ch → 5ch: RGB и pipe_mask копируются, skeleton = pipe_mask × 0.5).

### 4.5 Обучение

| Параметр | Значение |
|----------|----------|
| Encoder | EfficientNet-B2 (ImageNet) |
| Epochs | 120 |
| Batch size | 16 |
| Optimizer | AdamW (lr=1e-4, wd=1e-4) |
| Scheduler | CosineAnnealing (eta_min=1e-6) |
| Warmup | 3 epochs (linear) |
| AMP | True |
| Patience | 40 |
| Monitor | val_f1_combined |

Скрипт: `python -m junction_segmentation.train --data-dir ./data/junction_seg`. Поддерживает `--resume` для продолжения обучения. TensorBoard логирование.

### 4.6 Inference

Tiled inference с gaussian blend: тайлы 512px, overlap 128px. Каждый тайл взвешивается гауссовским окном (sigma = 0.25 × tile_size) для плавной сшивки. Batch inference: тайлы собираются в батч, прогоняются через `model.forward()` → sigmoid → gaussian blend accumulation.

NMS на итоговых heatmaps (kernel 3×3) → пики с confidence > threshold → список точек (x, y, confidence, class).

### 4.7 Развёртывание

Checkpoint `best.pth` монтируется в volume. Worker `junction` загружает модель. Thresholds (junction=0.40, bridge=0.60) — в YAML-конфиге проекта (секция `junction_seg`).

---

## 5. SAM2 Hiera Small + LoRA (Contour Extraction)

**Файлы:** `modules/sam2_contour.py`, `scripts/download_sam2_base.py`

### 5.1 Архитектура

**Base model:** SAM2 (Segment Anything Model 2, Meta) с backbone Hiera Small. Загружается из HuggingFace (`facebook/sam2.1-hiera-small`).

**LoRA** (Low-Rank Adaptation). Production-значение: r=8, alpha=16 (из checkpoint config, `cfg.get('lora_r', 8)`). Добавляется ко всем `nn.Linear` с `in_features ≥ 64`. Класс `LoRALayer`: `forward(x) = original(x) + (x @ A^T @ B^T) * scale`, где `scale = alpha / r`. Только `lora_A`, `lora_B` сохраняются в checkpoint.

> **Примечание:** Сигнатуры `LoRALayer.__init__` и `_apply_lora()` имеют default `r=16`. При вызове без явного указания r получится r=16, а не r=8. Production-значение r=8 приходит из checkpoint config при загрузке модели через `_load_sam2_model()`.

**6-канальный вход.** Patch embedding расширяется с 3ch до 6ch: pretrained RGB веса копируются в ch 0–2, ch 3–5 инициализируются нулями.

**Checkpoint формат.** Содержит trainable параметры (LoRA + patch_embed + mask decoder и др.). При загрузке: base SAM2 скачивается из HuggingFace → LoRA inject → `load_state_dict(strict=False)` — загружается всё, что есть в checkpoint, несовпадающие ключи игнорируются.

**Критические заметки по inference:**
- Использовать `forward_image()`, НЕ `image_encoder()` — последний пропускает FPN features, необходимые для mask decoder
- `sam_mask_decoder` требует `high_res_features` и `repeat_image=False`
- Возвращает 4 значения: `(logits, iou_pred, _, _)`

### 5.2 6-канальный вход

| Канал | Содержание | Источник |
|-------|------------|----------|
| 0–2 | RGB | Crop из исходного P&ID изображения |
| 3 | Rough mask | Полигон или bbox целевого объекта (dilated 4px) |
| 4 | Pipe mask | Маска труб из pipe segmentation |
| 5 | Other nodes | Маски соседних объектов (из COCO annotations) |

Rough mask (ch3) строится функцией `_build_rough_mask()`: если доступен полигон — `cv2.fillPoly`, иначе — bbox с padding 4px + морфологическая дилатация.

Other nodes mask (ch5) строится функцией `_build_other_nodes_mask()`: рендерит все детекции, попадающие в crop, кроме целевого объекта (фильтр по IoU > 0.5).

### 5.3 Smart crop (adaptive K)

Функция `compute_smart_crop()` вычисляет квадратный crop с адаптивным padding:

| Размер объекта | K (множитель) | Логика |
|---------------|---------------|--------|
| max_side × 2 ≤ 1024 | 2.0 | Маленький объект — полный контекст |
| max_side ≤ 1024 | 1024 / max_side | Средний — crop ~1024px |
| max_side > 1024 | 1.2 | Большой — сохранить разрешение |

Crop сдвигается (не обрезается) чтобы не выходить за границы изображения.

### 5.4 Smart snap

Функция `smart_snap()` конвертирует пиксельную маску SAM2 в регуляризованный полигон:

1. Найти наибольший контур (`cv2.findContours`)
2. Douglas-Peucker simplification (eps = `dp_eps_pct` × 1% периметра, default 0.15)
3. H/V edge snap: если отклонение поперечной оси < `snap_threshold` × длину ребра (default 0.08), ребро выравнивается на горизонталь/вертикаль
4. 3 итеративных прохода snap для пропагации
5. Удаление коллинеарных точек (threshold 1.5px)

Параметры smart snap:

| Параметр | Тип | Default | Описание |
|----------|-----|---------|----------|
| `snap_dp_eps` | float | 0.15 | Douglas-Peucker epsilon как % периметра |
| `snap_threshold` | float | 0.08 | Max deviation ratio для H/V snap |
| `snap_min_edge` | float | 0.03 | Min edge length как % от min(H,W) |

### 5.5 Ensemble (v7 + v8)

Класс `ContourExtractor` поддерживает опциональный ensemble из двух чекпоинтов (v7 — frozen decoder, v8 — unfrozen decoder):

| Условие | Стратегия | Описание |
|---------|-----------|----------|
| Оба confident (> 0.98) | Average | Усреднение probability maps → threshold 0.5 |
| Иначе | Intersection | Пересечение бинарных масок (консервативно) |

Confidence = max(conf_v7, conf_v8).

### 5.6 Dataset

Обучающие данные — 915 training samples. Формат: COCO annotations с bbox + rough polygon. Для каждого sample генерируется 6-канальный crop с adaptive K sizing.

### 5.7 Обучение

Обучение через fine-tune SAM2 base: только LoRA параметры + patch_embed trainable, base weights frozen. Эксперименты показали: LoRA r=8 — оптимальный компромисс (r=4/8/16/32 в пределах шума 1.3%, bottleneck — количество данных). RGB-only vs 6-channel: все auxiliary каналы в пределах 1.3% noise от RGB-only, но 6ch используется для production (контекст полезен на edge cases). Подробнее — см. ADR/ (001, 002, 003).

### 5.8 Метрики

Production результаты (17 verified схем):

| Метрика | Значение |
|---------|----------|
| Auto-accept rate | **134/136** (98.5%) |
| Mean confidence | **0.96** |
| Status threshold | 0.85 (ниже → `manual_review`) |

`status = 'auto'` если `confidence ≥ 0.85`, иначе `'manual_review'`.

### 5.9 Развёртывание

**Base weights.** Скачать заранее: `python scripts/download_sam2_base.py` → `models/sam2/sam2_hiera_small.pt` (~150 MB). В Docker: `TRANSFORMERS_OFFLINE=1` блокирует runtime загрузки.

**LoRA checkpoint.** Монтируется в volume. Путь — в YAML-конфиге (секция `contour_extraction.checkpoint`).

**Отдельный worker.** SAM2 работает в отдельной Celery queue `sam2` — изоляция GPU памяти от основного `gpu` worker. См. ADR/004.

---

## 6. Surya + PaddleOCR (Text Recognition)

Подробное описание OCR pipeline — в [OCR_PIPELINE.md](OCR_PIPELINE.md).

Кратко: двухэтапное распознавание текста на P&ID схемах.

**Surya** — transformer-based layout detection + recognition. Используется для первичной детекции текстовых блоков и распознавания.

**PaddleOCR** — CRNN-based recognition. Используется как fallback / second opinion для повышения точности.

Результаты OCR проходят через domain-specific валидацию: KKS pattern matching, пост-обработка (нормализация символов, коррекция частых ошибок), привязка текста к ближайшим узлам оборудования.

Модели предобученные, fine-tune не требуется. Работают в отдельном worker (queue `ocr`).

---

## Новые термины для GLOSSARY.md

| Термин | Описание |
|--------|----------|
| **CPA** | Copy-Paste Augmentation — вставка объектов из одних тайлов на другие для компенсации дисбаланса классов |
| **clCE** | Centerline Cross-Entropy (MICCAI 2024) — topology-preserving loss на soft-скелетах |
| **clDice** | Centerline Dice (CVPR 2021) — предшественник clCE, Dice на soft-скелетах |
| **OHEM** | Online Hard Example Mining — обучение на top-K% сложных пикселей |
| **DualHeadModel** | UNet++ с двумя выходами: mask + skeleton для multi-task learning |
| **AuxHead** | Вспомогательный tile-level classifier ("есть ли объект на тайле?") |
| **CenterNet init** | Инициализация bias=−4.6 в seg head для стабильного начала обучения |
| **Adaptive K** | Адаптивный множитель crop размера в зависимости от размера объекта (SAM2) |
| **Gaussian blend** | Взвешивание предсказаний тайлов гауссовским окном при сшивке |
| **Forbidden mask** | Маска областей, куда запрещена вставка объектов при CPA (трубы + аннотации) |

---

## Обновлённый статус реестра

| # | Документ | Статус | Версия | Чат |
|---|---------|--------|--------|-----|
| 13 | `MODELS.md` | ✅ Done | 1.1 | 8 |

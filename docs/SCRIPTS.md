# SCRIPTS.md

**Аудитория:** DEV / ML
**Версия:** 1.1
**Обновлено:** 2026-04-09
**Связанные документы:** MODELS.md, DEPLOYMENT.md, DEV_SETUP.md

---

## Оглавление

1. [cli.py — Node Detection CLI](#1-clipy--node-detection-cli)
2. [visualize_contours.py — визуализация SAM2 контуров](#2-visualize_contourspy--визуализация-sam2-контуров)
3. [enhance_pid.py — улучшение сканов](#3-enhance_pidpy--улучшение-сканов)
4. [magic_wand_editor.py — Magic Wand контурный редактор](#4-magic_wand_editorpy--magic-wand-контурный-редактор)
5. [debug_detection.py — отладка детекции](#5-debug_detectionpy--отладка-детекции)
6. [reset.py — сброс статуса диаграммы](#6-resetpy--сброс-статуса-диаграммы)
7. [fix_stamp.py — фикс Alembic stamp](#7-fix_stamppy--фикс-alembic-stamp)
8. [scripts/download_sam2_base.py — скачивание базовых весов SAM2](#8-scriptsdownload_sam2_basepy--скачивание-базовых-весов-sam2)
9. [polygon_editor.py — редактор полигонов unknow-узлов](#9-polygon_editorpy--редактор-полигонов-unknow-узлов)
10. [scripts/init_db.py — инициализация БД](#10-scriptsinit_dbpy--инициализация-бд)
11. [return_learning.py — возобновление обучения YOLO](#11-return_learningpy--возобновление-обучения-yolo)

---

## 1. cli.py — Node Detection CLI

Основной CLI для обучения, тестирования и инференса YOLOv8 детектора узлов. Модуль `pid_node_detection`.

### Команды

| Команда | Описание |
|---------|----------|
| `train` | Полный пайплайн обучения (подготовка данных + YOLO train) |
| `finetune` | Дообучение на новых данных (transfer learning) |
| `test` | Оценка модели на test-наборе (метрики + визуализации) |
| `inference` | Детекция на новых изображениях (batch или single) |
| `stats` | Статистика датасета (распределение классов, редкие классы) |

### Использование

```bash
# Обучение
python -m pid_node_detection train --config config.yaml --name baseline

# Тестирование
python -m pid_node_detection test --config config.yaml --weights best.pt

# Инференс (одно изображение)
python -m pid_node_detection inference --weights best.pt --input scheme.png --output ./predictions

# Инференс (директория)
python -m pid_node_detection inference --weights best.pt --input ./images --output ./predictions --confidence 0.8

# Дообучение
python -m pid_node_detection finetune --config config.yaml --weights best.pt \
    --new-images ./new_data/images --new-labels ./new_data/labels

# Статистика датасета
python -m pid_node_detection stats --labels ./dataset/train/labels --output stats.csv
```

### Аргументы inference

| Аргумент | Тип | Default | Описание |
|----------|-----|---------|----------|
| `--weights` / `-w` | str | обязательный | Путь к весам YOLO |
| `--input` / `-i` | str | обязательный | Изображение или директория |
| `--output` / `-o` | str | обязательный | Директория для результатов |
| `--confidence` | float | `0.8` | Порог confidence |
| `--device` | str | `cuda` | Устройство: cuda/cpu |
| `--no-grayscale` | flag | — | Не конвертировать в grayscale (по умолчанию конвертация включена) |
| `--visualize` | flag | — | Сохранить визуализации детекций |

Препроцессинг (`binarize_for_yolo method="full"`): NLM → CLAHE → Otsu → Dilate → 3ch BGR. Идентичен train-time preprocessing.

---

## 2. visualize_contours.py — визуализация SAM2 контуров

Визуализация полигонов из `contours_auto.json` на оригинальном изображении. Цвета по статусу: зелёный (auto-accept), оранжевый (manual_review), красный (пустой полигон).

### Использование

```bash
# Defaults (hardcoded base path)
python visualize_contours.py

# Указать файлы
python visualize_contours.py --contours storage/diagrams/{uid}/contours/contours_auto.json \
                              --image storage/diagrams/{uid}/original/image.png

# С опциями
python visualize_contours.py --contours contours.json --image image.png \
    --output result.png --no-bbox --no-vertices --scale 0.5
```

### Аргументы

| Аргумент | Тип | Default | Описание |
|----------|-----|---------|----------|
| `--contours` | Path | hardcoded | Путь к `contours_auto.json` |
| `--image` | Path | hardcoded | Путь к оригинальному изображению |
| `--output` | Path | `contours_vis.png` рядом с JSON | Выходной файл |
| `--no-bbox` | flag | — | Скрыть bounding boxes |
| `--no-vertices` | flag | — | Скрыть точки вершин полигонов |
| `--no-labels` | flag | — | Скрыть подписи (класс, confidence, n_points) |
| `--scale` | float | `1.0` | Масштаб выходного изображения |

### Выход

Изображение с наложенными полигонами (полупрозрачная заливка + контур), bbox, вершинами (магента) и подписями. Статистика в консоли: auto/review/empty.

---

## 3. enhance_pid.py — улучшение сканов

Пакетная обработка сканов P&ID чертежей: шумоподавление, отбеливание фона, усиление линий, удаление пыли и рамки ГОСТ.

### Использование

```bash
# Один файл
python enhance_pid.py scan.png result.png

# Директория
python enhance_pid.py ./input ./output

# Параллельная обработка (4 процесса)
python enhance_pid.py ./input ./output -w 4

# Без шумоподавления (быстрее)
python enhance_pid.py ./input ./output --no-denoise

# Без удаления рамки
python enhance_pid.py ./input ./output --no-frame
```

### Аргументы

| Аргумент | Тип | Default | Описание |
|----------|-----|---------|----------|
| `input` | str | обязательный | Файл или директория с изображениями |
| `output` | str | обязательный | Файл или директория для результатов |
| `-w` / `--workers` | int | `1` | Количество параллельных процессов |
| `--no-denoise` | flag | — | Отключить NLMeans шумоподавление (ускоряет в ~3x) |
| `--no-frame` | flag | — | Не удалять рамку ГОСТ |

### Этапы обработки

1. **detect_lines** — детекция линий чертежа через локальный контраст (fast_blur σ=50). Результат — маска линий, защищающая их на последующих этапах.
2. **denoise_background** — NLMeans шумоподавление только на фоне (линии защищены маской).
3. **whiten_background** (1-й вызов) — шумовые пиксели фона → белый. Критерий: пиксель >220 яркости и контраст с локальным фоном <3.
4. **enhance_lines** — усиление линий через LAB-пространство: понижение L-канала, усиление A/B-каналов по levels-маске.
5. **whiten_background** (2-й вызов) — повторная очистка фона после enhance_lines (enhance может создать новые артефакты).
6. **remove_dust** — морфологическое открытие: объекты <25 px² удаляются как пыль.
7. **remove_frame** — удаление рамки ГОСТ: геометрический поиск от краёв по плотности тёмных пикселей в строках/столбцах.

Поддерживаемые форматы: PNG, TIFF, BMP, JPG.

---

## 4. magic_wand_editor.py — Magic Wand контурный редактор

Интерактивный инструмент для создания контуров unknow-узлов. Два режима выделения, определяемых автоматически по цвету пикселя.

### Использование

```bash
python magic_wand_editor.py --image storage/.../original/image.png \
                             --graph storage/.../graph/graph.json
```

### Режимы работы

**Клик на белую линию** — connected component белых пикселей → fill holes → контур. Выделяет замкнутый контур символа по его белым линиям.

**Клик на чёрную область** — flood fill замкнутой внутренней области → контур. Выделяет внутренность символа.

**Shift+click** — добавить ещё один компонент (собрать контур из нескольких частей).

**Right-click** — убрать компонент.

Интерфейс: matplotlib TkAgg с кнопками и слайдерами. Результат — полигон в flat-формате `[x1, y1, x2, y2, ...]`.

---

## 5. debug_detection.py — отладка детекции

Скрипт для ручной тайловой детекции с подсчётом классов. Используется для отладки проблем с конкретными классами (например, `datchik`).

### Использование

```bash
python debug_detection.py
```

Пути к весам и изображению hardcoded в скрипте — редактировать перед запуском. Параметры: `tile_size=1280`, `overlap=0.25`, `conf=0.3` (значения приведены как пример текущего состояния; все параметры hardcoded и могут меняться при редактировании скрипта).

Выход: список детекций по тайлам с confidence, итоговый подсчёт по классам.

---

## 6. reset.py — сброс статуса диаграммы

Утилита для ручного сброса статуса конкретной диаграммы в БД. Используется при отладке для повторного запуска этапов pipeline.

### Использование

```bash
# Отредактировать UUID и target status в скрипте
DATABASE_URL=postgresql://pid_user:changeme@localhost:5433/pid_pipeline python reset.py
```

Скрипт hardcoded: обновляет `diagrams.status`, сбрасывает `error_message` и `error_stage` для конкретного UUID. Редактировать перед каждым использованием.

---

## 7. fix_stamp.py — фикс Alembic stamp

Принудительная установка версии Alembic в БД. Используется при рассинхронизации `alembic_version` с реальным состоянием миграций.

### Использование

```bash
python fix_stamp.py
```

Hardcoded: connection string (`localhost:5433`), target version (`d4e5f6a7b8c9`). Редактировать перед использованием.

---

## 8. scripts/download_sam2_base.py — скачивание базовых весов SAM2

Скачивание SAM2 Hiera Small base weights из Facebook CDN. Запустить однократно перед первым `docker-compose up` — контейнер worker ожидает наличие весов в `models/sam2/`.

### Использование

```bash
python scripts/download_sam2_base.py
```

Скачивает `sam2.1_hiera_small.pt` (~150 MB) в `models/sam2/sam2_hiera_small.pt`. Идемпотентен: если файл уже существует, выводит размер и завершается.

| Параметр | Значение |
|----------|---------|
| URL | `https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt` |
| Destination | `models/sam2/sam2_hiera_small.pt` |

---

## 9. polygon_editor.py — редактор полигонов unknow-узлов

Standalone matplotlib-инструмент для ручного редактирования контуров unknow-узлов P&ID. Показывает инвертированное изображение (чёрные линии на белом фоне), загружает CVAT-полигон как начальную форму.

### Использование

```bash
python polygon_editor.py --image <path.png> --graph <graph.json> --output approved_contours.json
```

### Аргументы

| Аргумент | Тип | Default | Описание |
|----------|-----|---------|----------|
| `--image` | Path | обязательный | Путь к оригинальному изображению |
| `--graph` | Path | обязательный | Путь к `graph.json` (загрузка узлов и полигонов) |
| `--output` | Path | `approved_contours.json` | Выходной файл с результатами |

### Управление

| Действие | Описание |
|----------|----------|
| Левая кнопка (drag) | Перетаскивание вершины полигона |
| Double-click на ребро | Добавить новую вершину |
| Right-click на вершину | Удалить вершину |
| Кнопка **Approve** | Принять текущий полигон |
| Кнопка **Skip** | Пропустить узел |
| Кнопка **Reset** | Сбросить к исходному полигону |
| Кнопка **Export** | Экспортировать все approved контуры в JSON |

Интерфейс: matplotlib TkAgg. Итерирует по unknow-узлам из графа. Результат — JSON с approved контурами (flat polygon `[x1, y1, x2, y2, ...]`).

### Размер: 393 строки.

---

## 10. scripts/init_db.py — инициализация БД

Инициализация базы данных: создание всех таблиц (включая projects) по моделям SQLAlchemy.

### Использование

```bash
python scripts/init_db.py
```

Использует `DATABASE_URL` из `app.config.settings`. Опционально поддерживает drop существующих таблиц перед созданием (см. исходный код).

### Размер: ~50 строк.

---

## 11. return_learning.py — возобновление обучения YOLO

Вспомогательный скрипт для возобновления прерванного обучения YOLOv8. Пути к модели и данным **hardcoded** — редактировать перед каждым использованием.

### Использование

```bash
python return_learning.py
```

Внутри: загружает YOLOv8m и вызывает `model.train()` с hardcoded параметрами (data yaml, epochs=100, batch=4, imgsz=1280, lr0=0.001, patience=25).

> **Примечание:** Для возобновления прерванного обучения (resume) также можно использовать однострочник из комментария в файле: `YOLO('last.pt').train(resume=True)`.

### Размер: ~20 строк.

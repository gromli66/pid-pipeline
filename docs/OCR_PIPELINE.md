# OCR_PIPELINE.md — OCR и Text Binding Pipeline

**Аудитория:** DEV / ML  
**Версия:** 1.1  
**Обновлено:** 2026-04-09  
**Связанные документы:** WORKER_TASKS.md, MODULES.md, CONFIG_REFERENCE.md, DATA_FORMATS.md

---

## Содержание

1. [Обзор](#1-обзор)
2. [3-проходный Surya Pipeline](#2-3-проходный-surya-pipeline)
3. [Semantic Regroup](#3-semantic-regroup)
4. [Domain Profiles](#4-domain-profiles)
5. [Classify Engine](#5-classify-engine)
6. [Noise Engine](#6-noise-engine)
7. [PaddleOCR Recognition](#7-paddleocr-recognition)
8. [Recluster (legacy path)](#8-recluster-legacy-path)
9. [Text Binding](#9-text-binding)
10. [Постобработка (Hooks)](#10-постобработка-hooks)
11. [Выходной формат](#11-выходной-формат)
12. [Конфигурация](#12-конфигурация)

---

## 1. Обзор

OCR pipeline извлекает текст с P&ID диаграмм и привязывает его к элементам графа. Это две логически независимые задачи: распознавание текста (OCR) и привязка (binding).

**Место в pipeline.** Celery task `task_run_ocr` запускается **параллельно** с `task_build_graph` после завершения junction validation. Task не меняет `DiagramStatus` — готовность определяется по наличию артефакта `OCR_RESULT`. Работает в очереди `ocr` (отдельный worker с GPU).

**Два OCR-движка:**

- **Surya OCR** — основной. Выполняет и detection (поиск текстовых блоков), и recognition (распознавание символов). Работает в 3 итерации с разным масштабом тайлинга.
- **PaddleOCR** — вспомогательный. Только recognition на кропах блоков, полученных из recluster. Работает через HTTP-сервис в отдельном контейнере `paddle_service`.

**Общий поток данных:**

```
original_image + pipe_mask + node_mask + junction/bridge points
  → clean_image (вычитание масок)
  → 3 итерации Surya OCR
  → semantic_regroup (classify + spatial grouping)
  → постобработка (split_multi, promote, merge)
  → ocr_result.json {target[], secondary[], stats{}}
  → binding engine (target → nodes/edges графа)
```

Файлы: `modules/ocr/pipeline.py` (главная функция `run_ocr_pipeline`), `worker/tasks/ocr.py` (Celery task).

---

## 2. 3-проходный Surya Pipeline

Точка входа — `run_ocr_pipeline()` в `modules/ocr/pipeline.py`. Принимает изображение, маски, точки junction/bridge и domain profile. Возвращает dict с target- и secondary-блоками.

### 2.1 Подготовка изображения

Перед OCR из изображения вычитаются маски труб и узлов, чтобы Surya не путал графические элементы с текстом.

**Уточнение маски труб** (`refine_pipe_mask_perp`). Если есть `pipe_mask` и `junction_points`, маска уточняется через перпендикулярное сечение:

1. Скелетизация маски труб → центральная линия.
2. Разрез скелета в точках junction/bridge (радиус = `2 × median_halfwidth + 1`).
3. Удаление коротких сегментов (< `min_segment_length` px).
4. Для каждого сегмента: fit line → сэмплирование перпендикулярного сечения → измерение ширины → построение ориентированного прямоугольника.
5. Восстановление зон junction/bridge.

Это даёт точную маску с правильной шириной каждого участка трубы, в отличие от исходной маски из UNet++ ensemble.

> **Альтернативная реализация:** `modules/ocr/refine_pipe_mask.py` — отдельный модуль уточнения маски по графовой структуре (distance transform → скелетизация → разрез по junction/bridge → dilate по медианной полуширине). Адаптирован для серверного использования без CLI. Отличается от `refine_pipe_mask_perp` подходом (median halfwidth vs перпендикулярное сечение). См. также `modules/mask_refinement.py` (adaptive dilate) в [MODULES.md §5](MODULES.md#5-mask-refinement).

**Очистка** (`clean_image` из `clean_and_ocr.py`). Строит `removal_mask = (pipes ∪ nodes) − text_protection` и заменяет пиксели на белый. Опционально (если `no_protection=False`) перед вычитанием запускает Surya detector для построения `text_protection_mask` — зон, где текст перекрывается с масками.

**Дочистка краёв** (`cleanup_removal_edges`). После вычитания маски на границе удалённых зон остаются тёмные пиксели — они стираются dilate + threshold.

### 2.2 Мультимасштабный тайлинг

Каждая итерация использует свой масштаб тайлов для захвата текста разного размера:

| Итерация | tile_size | overlap | Назначение |
|----------|-----------|---------|------------|
| 1 | 2048 | 256 | Базовый масштаб — основная масса текста |
| 2 | 1536 | 256 | Zoom-in — мелкий текст (DN, номера) |
| 3 | 2560 | 384 | Zoom-out — больше контекста для длинных строк |

Если изображение меньше `tile_threshold` (4096 px по большей стороне), тайлинг не используется — OCR запускается на полном изображении.

Функция `ocr_tiled` из `clean_and_ocr.py` нарезает изображение на тайлы с перекрытием, запускает Surya на каждом, затем `merge_overlapping` склеивает результаты по IoU и containment.

### 2.3 Цикл итераций

Итерации работают по принципу «найди → сотри → ищи дальше»:

```
Iteration 1:
  cleaned_1 (masks removed) → Surya OCR → detections_1
  → semantic_regroup → target_1 (confirmed targets)

Erase target_1 from cleaned_1 → cleaned_2

Iteration 2 (zoom-in):
  cleaned_2 → Surya OCR → detections_2
  → semantic_regroup → target_2 (new targets)

Erase (target_1 + target_2) from cleaned_1 → cleaned_3
Restore node_mask regions in cleaned_3

Iteration 3 (zoom-out):
  cleaned_3 → Surya OCR → detections_3
  → semantic_regroup + postprocess → target_3
  + collect secondary (кириллические блоки)
```

**Стирание текста** (`erase_text_in_boxes`). Для каждого target-блока: вырезает ROI с отступом `shrink_px=7`, бинаризует, dilate маску текста, заменяет тёмные пиксели на белый. Затем `denoise_erase_artifacts` удаляет мелкие connected components (≤ 15 px², параметр `max_area=15`).

**Восстановление узлов** перед итерацией 3: `cleaned_3[node_mask > 0] = orig[node_mask > 0]` — возвращает пиксели оригинала в зонах узлов, чтобы Surya мог прочитать текст внутри/рядом с оборудованием.

### 2.4 Финальная сборка

После трёх итераций:

1. `base_target` = `target_1 + target_2 + target_3`, каждый блок получает `source` ("iter1"/"iter2"/"iter3") и `confidence` (lookup из raw detections).
2. `postprocess_split_multi` — разбивка блоков с несколькими кодами (см. §10).
3. `postprocess_promote_from_secondary` — извлечение целевых кодов из кириллических блоков.
4. `collect_secondary_from_raw` — сбор блоков вторичного языка из сырых детекций iter3.

Вспомогательные метрики: `line_pitch` (медианная высота строки, вычисляется из multiline-блоков iter1) и `all_med_h` (медианная высота всех блоков) — используются как fallback для merge/grouping.

**Debug output.** Если pipeline создаёт `output_dir`, промежуточные результаты каждого этапа сохраняются в `debug/01_iter1_raw.json` … `debug/11_final_target.json` через `_dump_debug()`.

---

## 3. Semantic Regroup

Файл: `modules/ocr/evaluate.py`, функция `semantic_regroup()`.

Получает сырые OCR-блоки (text + bbox) и domain profile. Возвращает перегруппированные блоки с флагом `is_target`.

### 3.1 Дедупликация тайловых границ

`dedup_blocks()` — удаляет дубли, возникающие на границах тайлов. Два блока считаются дублями, если у них одинаковый текст и центры находятся ближе чем `med_h` по вертикали и `2×med_h` по горизонтали. Из пары сохраняется блок с большим confidence.

### 3.2 Построение signal

Для каждого блока:

1. Проверка `profile.is_noise()` — шумовые блоки отбрасываются.
2. Если блок содержит `<br>` (multiline) — вызывается `_process_multiline()`.
3. Иначе — `_process_singleline()`.

Каждый блок в signal получает `cls` — результат `profile.classify()`.

**Multiline processing** (`_process_multiline`). Разбивает блок по `<br>`, классифицирует каждую строку. Логика ветвления:

- **≥2 HEAD + ≥2 TAIL** — `cluster_split_bbox` (AgglomerativeClustering) разделяет bbox на N кластеров по connected components бинаризованного изображения. Каждая строка привязывается к ближайшему кластеру.
- **≥1 HEAD + TAIL + standalone** — разделение на non-standalone часть и standalone (DN), каждая часть получает свой sub-bbox.
- **≥1 HEAD + TAIL** — объединение в один блок с combined text.
- **Остальные** — каждая строка получает свой sub-bbox.

**Singleline processing** (`_process_singleline`). Вызывает `profile.split_inline()` — если разбивается на >1 токенов (например, `"AA001 10LCS68"`), каждый токен получает пропорциональную долю bbox.

### 3.3 Title block detection

`detect_tb()` — определяет область штампа (title block) по плотности длинных нецелевых текстовых блоков в углах изображения. Делит изображение на сетку 4×4, находит ячейку с наибольшей плотностью в нижней половине. Блоки из этой зоны без target-паттернов удаляются.

### 3.4 Spatial grouping

Пространственная группировка управляется `profile.get_grouping_passes()` — списком `GroupingPass` из YAML. Каждый pass определяет:

| Поле | Описание |
|------|----------|
| `source` | Категории-источники (HEAD, FRAG, ...) |
| `target` | Категории-цели для поиска соседей (TAIL, DN, ...) |
| `action` | emit, merge_and_emit, attach_to_existing, merge_text_and_reclassify |
| `max_distance` | Макс. расстояние в единицах `med_h` |
| `direction` | any, below_only, above_only |
| `x_overlap_min` | Мин. горизонтальное перекрытие (доля) |
| `chain` | Разрешить цепочку из 2 TAIL |

Поиск соседей использует пространственный grid (ячейка = `max(2×med_h, 30)` px) для ускорения.

**Действия passes:**

- `emit` — блок выдаётся как есть.
- `merge_and_emit` — source + nearest target → объединённый bbox и текст через ` | `.
- `attach_to_existing` — source присоединяется к уже выданному результату, расширяя bbox.
- `merge_text_and_reclassify` (`_pass_frag_complete`) — фрагмент (FRAG) склеивается с соседом, результат переклассифицируется; если classify даёт head/full — текст и cls обновляются.

После passes: оставшиеся блоки с target-паттерном добавляются. Каждый результат помечается `is_target = profile.is_target(text)`.

---

## 4. Domain Profiles

Файл: `modules/ocr/domain_profile.py`.

Domain profile инкапсулирует всю доменную логику OCR: как классифицировать текст, что считать шумом, что является целевым кодом, как группировать блоки. Pipeline и semantic regroup — полностью универсальные, вся специфика в профиле.

### 4.1 BaseDomainProfile

Абстрактный базовый класс. Загружает YAML-конфиг (паттерны, units, noise regex, char normalization, grouping passes). Определяет контракт:

| Метод | Назначение |
|-------|-----------|
| `classify(text)` | Текст → категория (FULL, HEAD, TAIL, DN, MULTI, FRAG, OTHER, ...) |
| `is_noise(text)` | Шумовой блок? Вызывается ДО classify. Целевые блоки не должны быть шумом |
| `is_target(text)` | Финальное решение: блок целевой? Вызывается ПОСЛЕ группировки |
| `has_any_target_pattern(text)` | Есть хотя бы один целевой паттерн? Защита от noise-фильтра |
| `split_inline(text)` | Разбивка строки на токены: `"AA001 10LCS68"` → `["AA001", "10LCS68"]` |
| `category_role(category)` | Категория → роль: head, tail, full, standalone, fragment, other |

Хуки постобработки (переопределяемые, по умолчанию no-op): `postprocess_split_multi`, `postprocess_promote_from_secondary`, `postprocess_merge_secondary`, `postprocess_extract_target_from_merged`, `collect_secondary_from_raw`.

### 4.2 ConfigDrivenProfile

Универсальный профиль — **вся логика из YAML**, ноль Python. Заменяет `KKSProfile`, `PidCyrillicProfile` и любые будущие профили. Новый проект = только YAML-файл.

Делегирует:
- `classify()` → `ClassifyEngine` (см. §5)
- `is_noise()` → `NoiseEngine` (см. §6)
- постобработку → хуки из `modules/ocr/hooks.py` (см. §10)

Предобработка текста (`_preprocess_text`): опционально очищает HTML-теги, нормализует символы (кириллица→латиница), применяет block fixes из `ocr_corrections`.

### 4.3 KKSProfile (legacy)

Файл: `modules/ocr/profiles/kks_rosatom.py`. Python-реализация для KKS Росатом. Паттерны и units загружает из `kks_rosatom.yaml`, сложная логика (classify, is_noise, split, постобработка) — в Python-методах.

Категории classify: FULL, HEAD, TAIL, DN, MULTI, FRAG, HEAD_DN, OTHER.

### 4.4 Как создать новый профиль

Для нового домена достаточно одного YAML-файла:

1. Создать `configs/projects/<project>/domain_profile.yaml` по структуре (см. §12).
2. Определить секции: meta, patterns, units, classify (rules + roles), noise (rules + thresholds), grouping (passes), postprocess.
3. В project YAML указать `ocr.domain_profile_path`.
4. Pipeline автоматически создаст `ConfigDrivenProfile`.

Если доменная логика не укладывается в YAML-правила, можно наследовать `BaseDomainProfile` и реализовать абстрактные методы в Python.

### 4.5 ISA S5.1 Instrument Tag Profile

**Файлы:** `modules/ocr/profiles/isa_s5.py`, `configs/projects/.../isa_s5.yaml`

Готовый профиль для распознавания инструментальных тегов по стандарту ISA S5.1. Реализован как Python-профиль (наследует `BaseDomainProfile`). Поддерживает паттерны вида: первая буква = измеряемая величина (T, P, F, L, ...), последующие = функция (I, C, R, A, ...), номер контура.

Использование: указать `profile_path` → `modules/ocr/profiles/isa_s5.py:ISAS5Profile` и `isa_s5.yaml` в project YAML.

### 4.6 Ключевые dataclass-ы

| Dataclass | Описание |
|-----------|----------|
| `CompiledPattern` | Скомпилированный regex с name, role (full/head/tail/standalone/fragment), min_length |
| `CodeMatch` | Результат разбора кода: full, block, system, fn, unit, num, suffix, confidence, code_type |
| `DiamMatch` | Результат разбора диаметра: prefix (Dy/DN/Ø), diameter (int), suffix |
| `GroupingPass` | Один проход группировки: source, target, action, max_distance, direction, chain |

### 4.7 Загрузка профиля

Функция `load_profile(profile_spec)` поддерживает два формата спецификации:

- **File-path (CLI):** `"path/to/profile.py:ClassName"` или `"path/to/profile.py:ClassName:config.yaml"`
- **Module-path (server):** `"modules.ocr.profiles.kks_rosatom:KKSProfile"`

В Celery task загрузка идёт по приоритету: `domain_profile_path` (v2.0, ConfigDrivenProfile) → `profile_path` (v1.x, file import) → `profile_module` (legacy, module import).

---

## 5. Classify Engine

Файл: `modules/ocr/classify_engine.py`, класс `ClassifyEngine`.

Универсальный движок классификации текста, управляемый YAML-правилами (`classify.rules` в domain profile). Заменяет Python if/elif цепочки в legacy-профилях.

### 5.1 Правила

Правила выполняются сверху вниз, первое совпадение возвращает результат. Каждое правило:

```yaml
classify:
  rules:
    - pattern: "full_code"
      condition: "match_count >= 2"
      result: "MULTI"
    - pattern: "full_code"
      condition: "matches"
      result: "FULL"
    - pattern: "head_code"
      condition: "matches"
      exclude_pattern: "tail_code"
      sub_rules:
        - pattern: "diameter"
          condition: "matches"
          result: "HEAD_DN"
        - result: "HEAD"
    - pattern: "tail_code"
      condition: "match_only"
      result: "TAIL"
    - result: "OTHER"
```

### 5.2 Условия (conditions)

| Condition | Описание |
|-----------|----------|
| `matches` | Паттерн найден где-то в тексте (`search`) |
| `match_only` | Паттерн покрывает весь текст (`match` на stripped) |
| `match_count >= N` | Число non-overlapping matches ≥ N |
| `total_count >= N` | Суммарное число matches нескольких паттернов (`patterns: [a, b]`) ≥ N, без overlap |

`exclude_pattern` — правило не срабатывает, если указанный паттерн тоже матчит (полный match на stripped текст).

`sub_rules` — вложенные правила, выполняются если родительское правило совпало.

### 5.3 Category → Role маппинг

Определяется в `classify.roles` YAML:

```yaml
classify:
  roles:
    head: [HEAD, HEAD_DN]
    tail: [TAIL]
    full: [FULL, MULTI]
    standalone: [DN, LINE]
    fragment: [FRAG]
```

Роли используются semantic regroup для ветвления в multiline processing и spatial grouping.

---

## 6. Noise Engine

Файл: `modules/ocr/noise_engine.py`, класс `NoiseEngine`.

Фильтрует шум — артефакты OCR, штамповые надписи, мусорные символы. Вызывается **до** classify.

### 6.1 Порядок проверок

| # | Проверка | Результат |
|---|----------|-----------|
| 1 | Пустой текст | noise |
| 2 | Содержит `<math>` | noise |
| 3 | Один неалфанумерик | noise |
| 4 | Нет ни букв, ни цифр | noise |
| 5 | `has_any_target_pattern()` = true | **защита**: не noise |
| 6 | Короткий (≤ `min_length_without_target`) без target | noise |
| 7 | Совпадает noise regex из YAML | noise (если нет target) |
| 8 | Длинный (> `max_length_text_only`) без target, больше букв чем цифр | noise |
| 9 | Много слов (≥ `max_word_count_no_target`) без target | noise |
| 10 | Много спецсимволов (> `max_special_char_ratio_short`) в коротком тексте (≤ `short_text_max_length`) | noise |

Ключевой принцип: **блоки с целевыми паттернами никогда не являются шумом** (правило 5 защищает от ложных срабатываний).

### 6.2 Конфигурация

Пороги задаются в `noise.thresholds` YAML:

| Параметр | Тип | Default | Описание |
|----------|-----|---------|----------|
| `min_length_without_target` | int | 2 | Мин. длина текста без target |
| `max_length_text_only` | int | 25 | Макс. длина нецелевого текста |
| `max_word_count_no_target` | int | 4 | Макс. число слов без target |
| `max_special_char_ratio_short` | float | 0.5 | Макс. доля спецсимволов в коротком тексте |
| `short_text_max_length` | int | 5 | Порог «короткого текста» |

Noise regex определяются в `noise.rules`:

```yaml
noise:
  rules:
    - name: "date_stamp"
      regex: '^\d{2}\.\d{2}\.\d{4}$'
    - name: "revision_mark"
      regex: '^[Рр]ев\.\s*\d+'
      flags: "IGNORECASE"
```

---

## 7. PaddleOCR Recognition

Файл: `modules/ocr/paddle_recognize.py`.

PaddleOCR используется как дополнительный recognition-движок на кропах блоков из recluster. Работает через HTTP-сервис в отдельном контейнере (`paddle_service:8010`), чтобы избежать конфликтов PyTorch/CUDA с основным worker.

### 7.1 Архитектура

```
worker_ocr → HTTP POST → paddle_service:8010
               /recognize       (один кроп)
               /recognize-batch (батч кропов)
               /health          (healthcheck)
```

Environment variables: `PADDLE_SERVICE_URL` (default `http://paddle_service:8010`), `PADDLE_RECOGNIZE_TIMEOUT` (default 30s).

### 7.2 Горизонтальные кропы

Отправляются батчами по `BATCH_SIZE=10` на `/recognize-batch`. Каждый кроп — PNG-файл из `crops/` директории.

### 7.3 Вертикальные кропы

Кроп считается вертикальным, если `height/width > 1.5` (определяется по имени файла `{idx}_{x1}_{y1}_{x2}_{y2}.png` или через PIL).

Для вертикальных кропов — 3 варианта: оригинал, поворот +90°, поворот −90°. Каждый вариант отправляется на `/recognize`. Выбирается лучший по confidence среди непустых результатов, не являющихся мусором (`is_garbage_text`).

### 7.4 Фильтрация мусора

`is_garbage_text()` из `modules/ocr/utils.py`: текст считается мусором, если длина < 2 символов или содержит только спецсимволы (`|-_/\.,;:!@#$%^&*()[]{}<>`).

---

## 8. Recluster (legacy path)

Файл: `modules/ocr/recluster.py`.

Legacy-модуль рекластеризации OCR-блоков. Используется в связке с PaddleOCR для дополнительного прохода recognition. В основном pipeline (3-проходный Surya) рекластеризация встроена в `semantic_regroup`.

### 8.1 Pipeline рекластеризации

1. **KKS horizontal split** — разделение широких блоков, где Surya склеил два кода в одну строку (`"10LAB30 10LAB30"` → 2 блока). Используется KKS-regex + список known unit codes.

2. **Vertical `<br>` split** — разделение многострочных блоков на атомарные строки.

3. **Union-Find merge** — объединение атомарных строк в целевые блоки по 4 нормализованным пространственным критериям (вертикальный/горизонтальный gap, размер, overlap).

4. **Дедупликация** перекрывающихся блоков.

5. **Фильтрация мусора**.

### 8.2 OCR-коррекция

При классификации токенов применяется таблица замен кириллица→латиница для визуально похожих символов: `А→A`, `В→B`, `С→C`, `Е→E`, `К→K`, `М→M`, `Н→H`, `О→O`, `Р→P`, `Т→T`, `У→Y`, `Х→X`.

### 8.3 Метрики

На 63 страницах (15 115 целевых блоков): F1@IoU>0.5 = 0.858 (Recall=0.825, Precision=0.895).

---

## 9. Text Binding

Привязка распознанного текста к элементам графа. Два типа привязки: код оборудования → узел (node), диаметр трубы → ребро (edge).

### 9.1 UnifiedBinder (v2.0)

Файл: `modules/binding/engine.py`, класс `UnifiedBinder`.

Единый binding engine, заменяющий legacy `KksBinder` + `TextBinder`. Конфигурация — из `domain_profile.yaml` через `DomainBindingConfig`.

**Алгоритм:**

**Phase 1: Equipment code → node.** Для каждого OCR-блока: `UnifiedMatcher.match_equipment(text)` → если match и unit не в `edge_binding_units` → поиск ближайшего equipment node (расстояние bbox→bbox < `node_max_distance`). Учитывается `expected_classes` для unit (из `unit_to_classes` конфига).

**Phase 2: Diameter → edge.** `UnifiedMatcher.match_diameter(text)` → поиск ближайшего ребра. Расстояние вычисляется от bbox до polyline ребра (`bbox_to_edge_distance_ex`). Приоритет: on-segment > off-segment, при равном расстоянии — совпадение ориентации (горизонтальный текст → горизонтальное ребро).

Если несколько OCR-блоков претендуют на одно ребро — выбирается ближайший.

### 9.2 UnifiedMatcher

Файл: `modules/binding/matcher.py`.

Цепочка распознавания: `raw text → regex match → OCR corrections → regex retry → compact text → regex retry`.

OCR-коррекции:
- Кириллица→латиница (визуально похожие: `А→A`, `В→B`, ...)
- Symbol fixes (специфичные для домена)
- Digit corrections (`O→0` и т.п.)
- Known blocks — словарь известных блоков для быстрого match

Пробует все `code_types` из конфига: сначала `equipment_code`, затем `diameter`.

### 9.3 Валидация и reclassify

Файл: `modules/binding/validator.py`, класс `BindingValidator`.

После привязки проверяет допустимость unit для class_name узла:

- Если unit ∈ `expected_units` для class_name → valid.
- Если class_name = `"unknow"` → reclassify по таблице `reclassify_rules` (unit→new_class).
- Если class_name имеет `kks_target = "none"` → узел не участвует в привязке (connector, text, ...).

### 9.4 Геометрия привязки

Файл: `modules/binding/geometry.py`.

Координаты:
- OCR bbox: `[x1, y1, x2, y2]`
- `source_point` / `target_point`: `[y, x]` (!)
- waypoints: `[[y, x], ...]`

Расстояние bbox→edge: для каждой точки на границе bbox (n_per_side=3 на сторону) вычисляется расстояние до каждого отрезка polyline ребра, берётся минимум. Функция `bbox_to_edge_distance_ex` дополнительно возвращает флаг `on_segment` — проекция точки попадает на отрезок (не на продолжение).

### 9.5 Legacy: KksBinder + TextBinder

Файлы: `modules/kks_binding/binder.py`, `modules/text_binding/binder.py`.

Исходные отдельные модули привязки. `KksBinder` — KKS код → equipment node. `TextBinder` — диаметр → edge. Заменены `UnifiedBinder`, но сохранены для обратной совместимости.

`TextBinder` дополнительно реализует **propagation** — если ребро получило диаметр, соседние рёбра без диаметра наследуют его (правило потока). Результат: `PropagatedDiameter`.

### 9.6 Результат привязки

`BindingReport`:

| Поле | Тип | Описание |
|------|-----|----------|
| `node_bindings` | list[NodeBinding] | Привязки кодов к узлам |
| `edge_bindings` | list[EdgeBinding] | Привязки диаметров к рёбрам |
| `total_ocr_blocks` | int | Всего OCR-блоков |
| `equipment_matched` | int | Блоков с equipment-кодом |
| `diameter_matched` | int | Блоков с диаметром |
| `nodes_bound` | int | Узлов с привязанным кодом |
| `edges_bound` | int | Рёбер с привязанным диаметром |

---

## 10. Постобработка (Hooks)

Файл: `modules/ocr/hooks.py`.

Хуки — чистые функции `(blocks, profile, **kwargs) → blocks`. Подключаются из `domain_profile.yaml` через секцию `postprocess`. Один хук может использоваться разными проектами.

### 10.1 split_multi

Разбивка target-блоков, содержащих несколько кодов.

**Стратегия `head_tail_join`** (KKS):
- 1 HEAD + N TAIL → N блоков. Каждый TAIL проверяется: `HEAD + TAIL` должен давать валидный FULL по regex. Если да — joined текст, sub-bbox по высоте.
- M HEAD (≥2) → M блоков, каждая группа HEAD + следующие до след. HEAD.

**Стратегия `inline_split`** (кириллический):
- Вызывает `profile.split_inline()`, разбивает bbox пропорционально.

Реестр: `SPLIT_MULTI_STRATEGIES = {"head_tail_join": ..., "inline_split": ...}`.

### 10.2 promote_from_secondary

`promote_from_secondary_generic()` — извлекает HEAD-паттерны из secondary (кириллических) блоков после нормализации. Проверяет: HEAD длина ≥ `min_head_length` (default 6), HEAD не дублирует уже найденные target-ы. Результат — новые target-блоки с `source="promoted_from_secondary"`.

### 10.3 merge_secondary

`merge_secondary_union_find()` — Union-Find слияние близких secondary-блоков (кириллических). Два блока объединяются, если вертикальный gap < `fallback_med_h × max_vgap_factor` и горизонтальный gap < `fallback_med_h × max_hgap_factor`. Merged-блок получает `cyrillic_merged=N` и `_members` (для дальнейшей обработки).

### 10.4 extract_target_from_merged

`extract_target_from_merged_cluster()` — извлекает целевые коды из merged кириллических блоков. Если merged-блок содержит HEAD-паттерн, `cluster_split_bbox` (AgglomerativeClustering) делит bbox на кластеры: KKS-members получают отдельные sub-bbox с `is_target=True`, кириллические members объединяются в один secondary-блок.

### 10.5 collect_secondary_from_raw

Метод `ConfigDrivenProfile.collect_secondary_from_raw()` — собирает блоки вторичного скрипта из сырых OCR-детекций. Фильтрация: `has_secondary_script()` + `not is_noise()`. Затем `merge_secondary`. В финальном списке — только блоки с ≥2 кириллических слов (≥2 символов каждое).

---

## 11. Выходной формат

### 11.1 ocr_result.json

```json
{
  "profile": "KKS (Росатом)",
  "target": [
    {
      "bbox": [120, 340, 280, 370],
      "text": "10LCS59AA001",
      "source": "iter1",
      "confidence": 0.95
    }
  ],
  "secondary": [
    {
      "bbox": [120, 375, 280, 400],
      "text": "Клапан регулирующий"
    }
  ],
  "stats": {
    "iter1_target": 45,
    "iter2_target": 12,
    "iter3_target": 5,
    "total_target": 67,
    "total_secondary": 34,
    "time_total_sec": 142.3
  }
}
```

**target** — целевые блоки (KKS-коды, диаметры). Поля:

| Поле | Тип | Описание |
|------|-----|----------|
| `bbox` | list[int] | `[x1, y1, x2, y2]` — координаты в пикселях оригинального изображения |
| `text` | string | Распознанный текст. Multiline parts соединены через ` \| ` |
| `source` | string | Источник: `"iter1"`, `"iter2"`, `"iter3"`, `"promoted_from_secondary"` |
| `confidence` | float | Confidence из Surya OCR (0.0–1.0) |

**secondary** — блоки вторичного скрипта (кириллические описания оборудования). Содержат только bbox и text.

**stats** — статистика по итерациям и общее время.

### 11.2 Debug-артефакты

При `save_intermediate=True` в `output_dir` сохраняются:
- `{stem}_cleaned_1.png` — изображение после вычитания масок
- `debug/01_iter1_raw.json` … `debug/11_final_target.json` — промежуточные результаты каждого этапа

---

### 11.3 OCR Validation (UI)

Модуль `modules/ocr_validation/` (`classifier.py`, `result.py`) реализует `OcrBlockClassifier` — двухпроходный классификатор OCR-блоков для UI-валидации результатов. Алгоритм: DiameterMatcher → вычитание из текста → KksMatcher → тип и цвет блока. Используется в UI для визуальной валидации OCR оператором. Подробнее — см. [MODULES.md §7](MODULES.md#7-ocr-validation).

---

## 12. Конфигурация

### 12.1 Параметры OCR в project YAML

Секция `ocr` в `configs/projects/<project>/<project>.yaml`:

| Параметр | Тип | Default | Описание |
|----------|-----|---------|----------|
| `domain_profile_path` | string | — | Путь к domain_profile.yaml (v2.0, приоритетный) |
| `profile_path` | string | — | Путь к Python-профилю (v1.x) |
| `profile_module` | string | — | Module path для import (legacy) |
| `profile_class` | string | — | Имя класса в Python-профиле |
| `no_protection` | bool | true | Не строить text protection mask |
| `tile2_size` | int | 1536 | Размер тайла итерации 2 |
| `tile2_overlap` | int | 256 | Overlap тайлов итерации 2 |
| `tile3_size` | int | 2560 | Размер тайла итерации 3 |
| `tile3_overlap` | int | 384 | Overlap тайлов итерации 3 |

> **Примечание:** Параметры итерации 1 (`tile1_size=2048`, `tile1_overlap=256`) зафиксированы в `CONFIG` внутри `clean_and_ocr.py` и не конфигурируются через project YAML.

### 12.2 Структура domain_profile.yaml

Полный конфиг профиля объединяет OCR и binding в одном файле:

```yaml
meta:
  name: "KKS (Росатом)"
  version: "2.0"
  languages: ["en", "ru"]
  normalization_direction: "cyr_to_lat"
  secondary_script: "cyrillic"

code_types:
  equipment_code:
    structure: "{block}{system}{fn}{unit}{num}{suffix}"
    fields: { ... }
    binding: { target: "node", method: "nearest_node", max_distance: 200 }
    match_patterns: [ ... ]
  diameter:
    structure: "{prefix}{value}{suffix}"
    binding: { target: "edge", method: "nearest_edge_boundary", max_distance: 150 }

units: ["AA", "AC", "AH", ...]    # подставляются в %%UNITS%%

patterns:
  full_code: { regex: "...", role: "full" }
  head_code: { regex: "...", role: "head" }
  tail_code: { regex: "...", role: "tail" }
  diameter:  { regex: "...", role: "standalone" }

char_normalization:
  enabled: true
  table: { "А": "A", "В": "B", ... }

classify:
  preprocess: { clean_html: true, normalize: true, block_fixes: true }
  rules: [ ... ]
  roles:
    head: [HEAD, HEAD_DN]
    tail: [TAIL]
    full: [FULL, MULTI]
    standalone: [DN]

noise:
  rules: [ { name: "...", regex: "..." } ]
  thresholds:
    min_length_without_target: 2
    max_length_text_only: 25
    max_word_count_no_target: 4

grouping:
  passes:
    - name: "full_emit"
      source: [FULL, MULTI]
      action: "emit"
    - name: "head_tail_merge"
      source: [HEAD, HEAD_DN]
      target: [TAIL]
      action: "merge_and_emit"
      max_distance: 3.0
      direction: "below_only"

postprocess:
  split_multi: { enabled: true, strategy: "head_tail_join" }
  promote_from_secondary: { enabled: true, min_head_length: 6 }
  merge_secondary: { enabled: true, max_vgap_factor: 1.0 }
  extract_target_from_merged: { enabled: true }

binding:
  class_rules: { ... }
  unit_to_classes: { "AA": ["valve"], "BB": ["pump"], ... }

ocr_corrections:
  cyr_to_lat: { "А": "A", ... }
  block_fixes: { "71": "Т1", "[1": "Т1" }
```

### 12.3 Загрузка профиля в task

Приоритет в `task_run_ocr`:

1. `domain_profile_path` → `ConfigDrivenProfile(yaml_path=...)` — рекомендуемый путь.
2. `profile_path` → `importlib.util.spec_from_file_location(...)` → `ProfileClass()`.
3. `profile_module` → `importlib.import_module(...)` → `ProfileClass()`.

### 12.4 Celery task параметры

| Параметр | Значение |
|----------|----------|
| `time_limit` | 3600 (60 мин) |
| `soft_time_limit` | 3300 (55 мин) |
| `max_retries` | 1 |
| `default_retry_delay` | 60 сек |
| `acks_late` | true |
| Очередь | `ocr` |

### 12.5 Входные артефакты

| Артефакт | Путь | Обязательный |
|----------|------|--------------|
| Оригинальное изображение | `{diagram_dir}/original/image.png` | Да |
| Маска труб | `{diagram_dir}/segmentation/pipe_mask_refined.png` (fallback: `_validated.png`, `pipe_mask.png`) | Нет |
| Маска узлов | `{diagram_dir}/segmentation/node_mask.png` | Нет |
| Junction/bridge points | `{diagram_dir}/junction/points.json` (`ArtifactType.JUNCTION_POINTS`) | Нет |

### 12.6 Выходной артефакт

| Артефакт | Путь | ArtifactType |
|----------|------|-------------|
| Результат OCR | `{diagram_dir}/ocr/ocr_result.json` | `OCR_RESULT` |

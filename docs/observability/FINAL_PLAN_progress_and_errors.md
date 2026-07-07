# Финальный план: прогресс-бар + ETA и видимые ошибки в клиенте

Ветка `deploy`. Учтён суженный объём: в клиенте нужны только **(1) прогресс с описанием и ETA** и **(2) любая ошибка с трейсбеком** (чтобы переслать разработчику). Полный лайв-лог не нужен. Логи завершённых диаграмм — чистить.

---

## 0. Главный вывод

Стриминг (SSE/WebSocket/Redis pub/sub) для этой задачи **не нужен**. Почти вся инфраструктура уже есть:

- `GET /api/diagrams/{uid}/stages` уже возвращает по каждой стадии: `status` (pending/running/completed/**failed**/skipped), `started_at`, `completed_at`, `duration_seconds`, `error_message`, **`error_traceback`**. (проверено в `app/api/diagrams.py:287` + `schemas/diagram.py:73`).
- Клиентский `api_client.get_stages()` уже тянет эти поля (`ui/services/api_client.py:317`).
- `start_stage()` сразу пишет `started_at` и `status=RUNNING` (`db_helpers.py`, `stage.start()` + `db.flush()`) → **есть якорь для ETA**.
- `fail_stage(stage, error, tb)` сохраняет `error[:2000]` и `traceback[:10000]` → **полный трейсбек уже персистится** для каждой стадии, которая корректно фейлится.
- В `main_window` уже есть `QProgressBar` (`self.progress_bar`, сейчас крутилка `setRange(0,0)`) и `progress_label` (`ui/windows/main_window.py:67-81`).
- В `diagram_workspace` уже есть маппинг `error_stage → красная retry-кнопка` и заготовка `self._stage_errors = {}` (`:615, :883, :953`).

**Значит основной объём — клиентская логика поверх текущего 2-сек. polling + точечные серверные фиксы «слепых зон» ошибок.** Никакой новой транспортной подсистемы.

---

## 1. Архитектура решения (data flow)

```
Worker (Celery)                    Postgres                     Client (Qt, polling 2с)
  start_stage() → RUNNING, started_at ─┐
  ... работа ...                        ├─►  processing_stages  ──►  GET /stages ──► ProgressModel
  complete_stage(duration) / fail_stage(tb)                     │                      ├─ overall %  → QProgressBar
                                                                │                      ├─ ETA        → label
  (новое) любая «слепая» ошибка → тоже создаёт/фейлит stage ────┘                      └─ FAILED-стадии → ErrorReport
```

Ничего не пушим; клиент раз в 2с читает `/stages`, считает прогресс и показывает ошибки. Между опросами бар доезжает по локальным часам (elapsed от `started_at`).

---

## 2. Часть A — Прогресс-бар с описанием и ETA

### A1. Общий прогресс пайплайна (overall %)
Канонический порядок «майлстоунов» уже задан в `app/api/rollback.py::_STAGE_ORDER` (15 стабильных точек: UPLOADED→DETECTED→VALIDATED_BBOX→SKELETONIZED→…→COMPLETED). Overall % = (индекс текущего майлстоуна / N) с добавкой доли внутри активной стадии.

Три типа состояний (из `docs/STATUS_MACHINE.md`):
- **Worker-стадии** (`detecting, segmenting, skeletonizing, skeletonizing_final, detecting_junctions, building_graph, extracting_contours, ocr_processing, generating_fxml`) → показываем **% + ETA**.
- **`*ed`-состояния** («detected», «built» …) → мгновенные, бар на границе сегмента.
- **Ручные стадии** (`validating_bbox/masks/junctions/graph`, `ocr_bound`, `contours_validated`) → **не проценты**, а надпись «Ожидает оператора: …» (спиннер/пауза бара).

### A2. Внутри активной стадии — ETA по историческому времени
Данные уже есть: `started_at` (когда началась) и `duration_seconds` прошлых прогонов той же `stage_type`.

```
elapsed        = now - started_at
typical        = median(duration_seconds  по прошлым COMPLETED стадиям этого stage_type)
percent_stage  = min(elapsed / typical, 0.99)     # 0.99 — не «застревать» на 100%
eta_seconds    = max(typical - elapsed, 0)
```
- **Источник медианы:** новый лёгкий эндпойнт `GET /api/stats/stage-durations` — один SQL `GROUP BY stage_type` c `percentile_cont(0.5)`; клиент запрашивает раз за сессию и кэширует.
- **Первый прогон / пустая история:** сид-дефолты в клиентском конфиге (грубые типичные времена на CPU-сервере), затем медиана самокалибруется.
- **Точность (refinement):** время detection/segmentation/OCR растёт с размером картинки. Нормировать: `typical = median(duration/megapixels) × megapixels_этой_диаграммы` (есть `image_width/height`, `detection_count`). Включить, если плоская медиана окажется неточной.

### A3. Описание стадии
Статическая карта `stage_type → человекочитаемый текст` («Детекция символов…», «Сегментация труб…», «Распознавание текста (OCR)…»). Плюс мелкий счётчик из уже логируемых финальных метрик, где он есть (`detection_count`, `n_tiles`, `junction_count`) — опционально.

### A4. Параллельная фаза (после `validated_junctions`)
graph + contours + OCR идут **параллельно**, а `DiagramStatus` отражает только graph. Но в `/stages` есть отдельные строки `GRAPH_BUILDING`, `CONTOUR_EXTRACTION`, `OCR` → для общего бара берём max(elapsed/typical) из трёх активных и описание «Граф + контуры + OCR (параллельно)».

### A5. Клиентские изменения (прогресс)
- `ui/windows/main_window.py`: `progress_bar.setRange(0,100)`, `setValue(percent)`; label = «Стадия K/N: <описание> — осталось ~MM:SS».
- `ui/services/status_provider.py`: добавить опрос `/stages` (не только `/status`) для активной диаграммы; эмитить `progress_updated(uid, ProgressInfo)`.
- Новое `ui/services/progress_model.py`: чистая логика A1–A4 (тестируемая без Qt).
- Опрос: оставить 2с; между тиками бар анимировать по локальным часам (QTimer) от `started_at`, чтобы двигался плавно.

### A6. Серверные изменения (прогресс)
- Новый `app/api/stats.py`: `GET /api/stats/stage-durations` (медиана/‑по‑мегапикселю на stage_type). Зарегистрировать в `app/main.py`.
- Больше ничего: `started_at`/`duration_seconds` уже пишутся.
- (Опц., фаза 2) реальный под-прогресс для 2–3 самых длинных стадий (detection SAHI, OCR Surya, segmentation) — писать `stage.metrics_json={"progress":k/total}` каждые N шагов; клиент предпочитает его медиане, если есть. Требует правки циклов в модулях (см. AUDIT). Делать только если ETA недостаточно точен.

---

## 3. Часть B — Любая ошибка видна в клиенте и отправляема

### B1. Принцип
Клиент показывает **любую `ProcessingStage` со `status=failed`** (и опц. `warning`), читая `error_message` + `error_traceback` из уже существующего `/stages`. Данные уже текут — надо (1) чтобы КАЖДЫЙ сбой создавал/фейлил stage, (2) показать это в UI.

### B2. Серверные фиксы слепых зон (чтобы «любая» = действительно любая)
Из аудита — места, где ошибка сейчас НЕ доходит до stage/трейсбека:

| Слепая зона | Файл (ориентир) | Сейчас | Фикс |
|---|---|---|---|
| **CVAT (гл. проблема)** | `worker/tasks/detection.py:307-322` | `except: print("non-fatal")`, стадия COMPLETED | создать `ProcessingStage(CVAT)` и `fail_stage(tb)` при сбое (не блокируя пайплайн — `diagram.status` остаётся DETECTED); `logger.error(exc_info=True)` |
| CVAT-клиент без логов | `app/services/cvat_client.py` | нет `import logging` | добавить logger на каждый сбойный ответ (код/тело/попытка) |
| CVAT из UI | `app/api/cvat.py` | только HTTPException | `logger.error(exc_info=True)` перед подъёмом |
| Мягкая деградация | `skeleton.py:227,551,602,700`, `ocr.py:192` | `except: logger.warning(); continue` | те, что влияют на качество → писать `ProcessingStage` со `status=warning` (видно клиенту, не блокирует) |
| Frame removal | `app/api/frame.py` | нет logger, нет stage | обернуть в `start_stage/fail_stage` + logger |
| Upload/PDF-render | `app/api/diagrams.py`, `pdf_render.py` | частично | гарантировать `fail_stage`/error при сбое рендера |
| print вместо logger | `detection.py`, `modules/graph/core/builder.py` | контекст в stdout без uid | заменить на `logger` c `diagram_uid` (для «что было перед ошибкой») |

> Глубокие исключения модулей (GPU OOM в segmentation/tta/SAM2/Surya) уже всплывают в `except Exception` задачи → `fail_stage(traceback)`. Они УЖЕ покрыты — трейсбек в БД. Отдельно чинить не нужно, только (опц.) добавить контекст «какой тайл/объект».

### B3. Клиентская часть (показ + отправка)
- Новый `ui/widgets/error_report_dialog.py`: показывает `error_message` крупно + `error_traceback` в моноширинном поле + контекст-шапку: `diagram_uid`, `project_code`, `stage_type`, `detection_model`, размер картинки, **версия клиента** (`ui/_version.py`), время. Кнопки:
  - **Копировать отчёт** (в буфер),
  - **Сохранить .txt** (готовый файл, чтобы переслать),
  - **Отправить разработчику** — вариант выбрать (см. открытые вопросы): `mailto:` с телом / выгрузка файла / POST на endpoint.
- `ui/widgets/diagram_workspace.py`: `_apply_error_status()` → помимо красной retry-кнопки, по клику открывать `error_report_dialog`; заполнять `self._stage_errors` из `get_stages()` (там уже трейсбек). Показывать и `warning`-стадии (жёлтый значок, не блокирует).
- `api_client.get_stages()` — уже есть, ничего не менять.

### B4. (Опц., фаза 2) «Контекст-бандл» последних строк лога при ошибке
Если трейсбека мало (нужно, что печатали SAHI/Surya перед падением): в воркере держать **in-memory `deque(maxlen≈200)`** последних строк текущей задачи (через `logging.Handler` + tee `stdout/stderr`), и при `fail_stage` дописывать хвост в `error_traceback`/отдельный файл `storage/diagrams/{uid}/error_report.txt`. Только при ошибке, без Redis/SSE.
Подводный камень (заземлено): воркер `--pool=prefork` → хендлер/deque инициализировать в сигнале **`worker_process_init`** (после fork), Redis/файловые ресурсы не шарить через fork.

---

## 4. Часть C — Эфемерность и очистка

Что реально растёт на сервере и как чистить:

1. **Docker-логи контейнеров** (`worker`/`worker_ocr`/`api`) — сейчас json-file **без ротации** (в `docker-compose.yml` нет `logging:`), растут неограниченно. Это главный источник. → добавить в compose каждому сервису:
   ```yaml
   logging: { driver: json-file, options: { max-size: "20m", max-file: "5" } }
   ```
2. **`processing_stages` строки** — крошечные (сотни байт). Нужны для ETA-медиан → **не удалять** на успехе. На COMPLETED трейсбеков нет по определению.
3. **Error-бандлы/отчёты** (если включим B4): удалять `storage/diagrams/{uid}/error_report.txt` **при переходе в COMPLETED**, оставлять при ERROR. Прямо реализует «прошло до финала → почистить».
4. **Опц. scheduled purge**: раз в сутки чистить `error_traceback` у стадий старше N дней и осиротевшие бандлы (можно через существующий механизм задач/cron).

> Файлы-артефакты (маски/изображения в `storage/diagrams`) — это выход пайплайна, не «логи»; их чистка (если нужна) — отдельная тема, вне этого плана.

---

## 5. Подтверждённые факты и подводные камни (из заземления)

- `api` = один `uvicorn` без `--workers` (`Dockerfile.api:40`). Для polling это неважно (запросы короткие). SSE не вводим — проблема одного event-loop неактуальна.
- Celery `--pool=prefork` (2 и 1). Важно **только** если делаем B4 (deque/логгер в воркере) → init в `worker_process_init`.
- `started_at` пишется сразу при старте стадии → ETA-якорь готов.
- `/stages` уже отдаёт всё нужное (трейсбек, тайминги) — нулевые изменения для чтения ошибок и таймингов.
- Клиент пересобирается на каждое изменение → новые зависимости не проблема (но здесь они, скорее всего, и не нужны — только stdlib+Qt).

---

## 6. Что ещё под вопросом / проверить в реализации

- **Точность ETA на первом прогоне CPU-сервера** — нужны сид-дефолты; проверить на 1–2 реальных прогонах, включать ли нормировку по мегапикселям.
- **Параллельная фаза** (graph/contours/OCR) — согласовать формулу общего % (предложено max из трёх).
- **Ручные стадии в общем %** — как их «весить» (предложено: фиксированные границы сегментов, внутри — «ожидает оператора»).
- **Тест без GPU**: UI прогресса/ошибок можно валидировать, вставив фейковые `ProcessingStage` (RUNNING со `started_at`, FAILED с `error_traceback`) прямо в БД — прогонять весь пайплайн не нужно.
- **`requirements/ui.txt` / версия Qt** — свериться при добавлении диалога (не блокирует).

---

## 7. Этапы внедрения

**Фаза 1 — Ошибки (закрывает «не лазить на сервер», highest value):**
1. Фиксы слепых зон B2 (CVAT в первую очередь: stage+fail+logger; frame; мягкие деградации → warning-stage).
2. `error_report_dialog` + показ FAILED/WARNING стадий из `/stages`, кнопки Копировать/Сохранить.
3. Docker log-ротация (C1).
→ Результат: любая ошибка (включая CVAT) видна в клиенте с трейсбеком и выгружаема тебе.

**Фаза 2 — Прогресс + ETA:**
4. `GET /api/stats/stage-durations` + `progress_model.py`.
5. Детерминированный `progress_bar` + описание + ETA; опрос `/stages`; сид-дефолты.
6. Ручные стадии = «ожидает оператора»; параллельная фаза.
→ Результат: бар с описанием и «сколько осталось».

**Фаза 3 — Полировка (опц.):**
7. Контекст-бандл логов при ошибке (B4).
8. Реальный под-прогресс для detection/OCR/segmentation, если ETA неточен.
9. «Отправить разработчику» (выбранный канал), scheduled purge.

---

## 8. Решения, которые нужны от тебя

1. **CVAT-ошибка**: блокирующая (ERROR + retry-кнопка, пайплайн стоп) или **неблокирующая** (жёлтый warning, детекция засчитана, пайплайн идёт)? — по умолчанию предлагаю неблокирующую warning-стадию + видимый трейсбек.
2. **«Отправить разработчику»**: (а) сохранить .txt и переслать вручную; (б) `mailto:` с телом; (в) кнопка → POST на твой endpoint/бот (авто). Что предпочитаешь?
3. **Контекст-бандл** последних строк лога при ошибке — нужен, или трейсбека из `fail_stage` достаточно?
4. **ETA-точность**: стартуем с плоской медианы, или сразу нормировка по размеру картинки?
5. **Warning-стадии** (мягкие деградации skeleton/ocr) — показывать оператору или молчать (log-only)?

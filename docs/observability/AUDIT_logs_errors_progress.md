# Аудит: хватает ли логов для (1) всех ошибок и (2) шкалы прогресса

Проверено по коду ветки `deploy` (внутренности модулей, не только задачи-обёртки). Цитаты — из реального кода.

## Короткий вердикт

- **Ошибки в целом:** фатальные ошибки стадий — фиксируются (почти каждая задача ловит `except Exception` → `fail_stage()` + `set_diagram_error()`, это ложится в Postgres). Но «ВСЕ ошибки» — **нет**: есть системные слепые зоны.
- **CVAT (твой главный интерес):** **не отслеживается вообще**. Ошибки CVAT в детекции глушатся как «non-fatal» (только `print`, без logger, без пометки стадии/диаграммы как ошибка). Сам `cvat_client.py` и `app/api/cvat.py` **не логируют ничего**. Это — дыра №1.
- **Прогресс:** текущих логов на **детерминированную** шкала % не хватает нигде, кроме частично junction. Реально сейчас можно только indeterminate («идёт…»). Для процентов нужна доработка ~5 горячих циклов.
- **Источники (worker / worker_ocr / api):** worker и worker_ocr покрывают почти все стадии, **но** часть кода пишет через `print()` (детекция, graph-builder, skeleton_extension) — значит «чистый» capture только через logging их потеряет, а capture stdout контейнера их поймает, но без привязки к diagram_uid. Контейнер **api — самое слабое звено**: именно там прячутся ошибки CVAT и frame, и там логов почти нет.

---

## Матрица по стадиям

| Стадия | Контейнер | Ошибки → стадия FAILED? | Синк | diagram_uid | Прогресс сейчас |
|---|---|---|---|---|---|
| FRAME_REMOVAL | api | ❌ **нет stage-трекинга вообще** | только HTTPException | ❌ | ручной, N/A |
| DETECTION | worker | ✅ (кроме CVAT) | **print()** | частично (в тексте) | ❌ indeterminate (SAHI `verbose=0`) |
| **CVAT (внутри detection + api)** | worker + api | ❌ **глушится «non-fatal»** | print / ничего | ❌ | — |
| DIRECTION | worker | ✅ | logger | ✅ | determinate только на выходе |
| SEGMENTATION | worker | ✅ | logger | ✅ | ⚠️ известно total тайлов, батчи не логируются |
| SKELETONIZATION (+FINAL) | worker | ✅, но 4× `warning+continue` | logger (+ модуль через print) | ✅ | ❌ indeterminate (6 этапов через print) |
| JUNCTION | worker | ✅ | logger + **tqdm по тайлам** | ✅ (в задаче) | ✅ **determinate возможен** (есть счётчик тайлов) |
| CONTOUR/SAM2 | worker | ✅, но `predict_batch` без обёртки | logger | ✅ | ❌ per-object прогресс не логируется |
| OCR | **worker_ocr** | ✅ (worker-слой), Surya ❌ | logger | ✅ | ❌ Surya одним батчем, без промежутка |
| GRAPH_BUILDING | worker | ✅ (задача), builder внутри ❌ | logger (**builder — print()**) | ✅ в задаче, ❌ в builder | determinate только на выходе |
| FXML_GENERATION | worker | ✅ (частично, parse/generate без try) | logger | ✅ | determinate только на выходе |

---

## CVAT — разбор дыры №1

**1. `cvat_client.py` не логирует ничего.** Нет даже `import logging`. Все методы — на `response.raise_for_status()`; при сбое летит `httpx.HTTPStatusError`/`TimeoutError` без единой строки лога. Слепые ветки:
- `create_task()` — POST-ошибка, таймаут загрузки картинки;
- `_wait_for_job()` → `raise TimeoutError` (job не создан за 30 попыток);
- `import_annotations()` — 400/403 при отклонении ZIP;
- `export_annotations()` — таймаут экспорта, `status_code not in (200,201,202)`, пустой ZIP (`< 50 байт`);
- connection refused / socket timeout — как generic `Exception`.

**2. В `detection.py` весь блок CVAT завёрнут в «проглатывающий» try/except** (строки 254–322):
```python
        except Exception as cvat_exc:
            # CVAT ошибки не фатальны - детекция выполнена
            print(f"[WARN] CVAT error (non-fatal): {cvat_exc}")
            print(traceback.format_exc())

        # ===== 8. Обновляем диаграмму в БД =====
        diagram.status = DiagramStatus.DETECTED          # ← всё равно DETECTED
        diagram.cvat_task_id = cvat_task_id              # ← None, если CVAT упал
        complete_stage(stage, {...})                      # ← стадия COMPLETED
```
Итог: если CVAT недоступен/таймаут/отклонил аннотации — **диаграмма помечается DETECTED, стадия COMPLETED, `cvat_task_id=None`**, а единственный след — `print` в stdout контейнера `worker` без `diagram_uid`. Ни logger, ни `set_diagram_error`, ни FAILED-стадии. В UI это «успех».

**3. `app/api/cvat.py` тоже без logger.** Ошибки эндпойнтов (`create_cvat_task_endpoint`, `fetch_cvat_annotations`) уходят только как `HTTPException(detail=...)` клиенту; в контейнере `api` записи нет (кроме дефолтного uvicorn).

**Чтобы «отслеживать ВСЕ ошибки CVAT», нужно:**
1. добавить `logger` в `cvat_client.py` (каждый сбойный ответ: код, тело, попытка) и в `app/api/cvat.py`;
2. перестать глушить CVAT как «non-fatal» — как минимум помечать стадию `partial`/писать `diagram.error_stage="cvat"` + `logger.error(exc_info=True)`, чтобы это было видно клиенту;
3. дифференцировать типы (`TimeoutError` job vs export vs connection) — сейчас всё сводится к одному `Exception`.

---

## Слепые зоны ошибок (сводно)

1. **CVAT целиком** (см. выше) — приоритет 1.
2. **`print()` вместо logger** в: `detection.py`, `modules/graph/core/builder.py`, `modules/skeleton_extension/processing.py`. При «чистом» capture через logging эти строки не попадут в клиент; ошибки внутри `builder.build()`/трассировки рёбер — не видны в Celery-логах.
3. **`except Exception: logger.warning(...); continue`** — «мягкая» деградация, в UI выглядит как успех: `skeleton.py` (~227 background-mask, ~551 mask-refinement, ~602, ~700 auto-dispatch junction), `ocr.py` (~192 OCR→graph merge), graph (пропущенные COCO/оригинал).
4. **Необёрнутые тяжёлые вызовы** — GPU OOM / NaN всплывают как generic Exception без контекста «какой тайл/объект/батч»: `pipe_segmentation/inference/engine.py` (батч-цикл), `tta.py`, `sam2_contour.predict_batch`, Surya-инференс (`recognize_surya.py`), Paddle (`paddle_service._recognize_image` — ошибки не логируются).
5. **FRAME_REMOVAL** (`app/api/frame.py`): нет logger, нет `start_stage/fail_stage`. Ошибки `storage.save_file()`/commit не пишутся и не помечают диаграмму — она может «зависнуть» в промежуточном статусе без следа.

---

## Прогресс — что реально можно сейчас и что дописать

**Сейчас достаточно только для indeterminate** (спиннер + heartbeat) практически везде. Причины:
- DETECTION: SAHI зовётся с `verbose=0`, ансамбль из 3 моделей и WBF — без счётчиков.
- SEGMENTATION: `len(positions)` (тайлы) известно и логируется в конце, но индекс батча в `engine.py` не эмитится.
- SKELETONIZATION: 6 внутренних этапов через `print`, без счётчика; наружу — один `bool success`.
- CONTOUR/SAM2: `predict_batch()` не логирует по объектам.
- OCR: Surya получает все боксы одним батчем — промежутка нет.
- **JUNCTION — единственная, где детерминированность близко:** есть `for i in tqdm(range(len(tiled)))` в `run_inference()`. Но tqdm пишет в stderr, не в logger → в контейнере это не поток для клиента. Достаточно заменить/дополнить на `logger.info("tile %d/%d")`.

**Минимальная доработка для % (по одному циклу на стадию):**
- detection: обернуть SAHI callback'ом или логировать модель `idx/3` в ансамбле;
- segmentation: `logger.info` каждые N батчей в `engine.py`;
- junction: `logger.info` вместо/вместе с tqdm;
- contours: счётчик по `raw_results` после `predict_batch`;
- ocr: разбить Surya на под-батчи и логировать `k/total`.

После этого шкала считается тривиально: `% = current/total` из строк лога.

---

## Ответ на «хватит ли логов worker / api / worker_ocr / остальных»

- **worker** — здесь живёт большинство стадий (detection, direction, segmentation, skeleton, junction, contours, graph, fxml). Ловит их stdout — поймаешь и `logger`, и `print`. Но: без `diagram_uid` в `print`-строках их не разнести по диаграммам при параллельной обработке.
- **worker_ocr** — OCR (logger + `diagram_uid`, хороший источник). Дыра — внутренние ошибки Surya/Paddle не обёрнуты.
- **api** — frame + эндпойнты CVAT. **Почти не логирует.** Именно тут теряются ошибки CVAT-вызовов из UI и ошибки frame. Это надо чинить кодом, capture не поможет.
- **остальные** (postgres/redis/cvat_server/traefik) — для твоей задачи не нужны, кроме `cvat_server`: если понадобится глубоко дебажить сам CVAT, его логи — в контейнере `cvat_server` (отдельно от твоего pipeline).

**Вывод:** capture stdout `worker`+`worker_ocr`+`api` — это самый полный «сырой» источник (ловит и print, и logger), но как есть он: не тегирован `diagram_uid`, содержит CVAT-ошибки под ярлыком «non-fatal», и в нём нет счётчиков прогресса, потому что их пока не эмитят. То есть «скопировать существующие логи» ≠ получить то, что нужно клиенту.

---

## Могу ли я это сделать / нужен ли рантайм-захват логов UI

- **Да, это делаю я** — задача разбивается на: (а) унификация синка (`print`→`logger` с `diagram_uid`-контекстом или мост stdout→logging), (б) починка CVAT-логирования и снятие «глушилки», (в) добавление 5 счётчиков прогресса, (г) стрим в клиент (Redis→SSE из прошлого плана).
- **Рантайм-захват логов во время работы UI — это не замена, а дополнение.** Статический аудит (этот документ) даёт полную карту веток кода; один инструментированный прогон нужен, чтобы (1) увидеть, какие строки реально срабатывают, и (2) поймать «болтовню» сторонних библиотек (SAHI/Surya/Paddle/httpx/ultralytics), которую по коду не перечислить. Т.е. сначала правим код-источник, потом — контрольный прогон для верификации.

**Рекомендованный порядок:** Фаза 0 — унификация логов + CVAT-логирование (снимает «не лазить на сервер» и закрывает дыру CVAT) → Фаза 1 — SSE-стрим в клиент → Фаза 2 — счётчики прогресса и детерминированные шкалы.

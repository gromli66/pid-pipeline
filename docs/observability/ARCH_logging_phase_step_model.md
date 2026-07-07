# Архитектура логов: канонический цикл «фаза → под-шаг»

Цель — задать **один** повторяемый жизненный цикл фазы и логировать границы под-шагов единообразно во всём пайплайне. Тогда любая проблема (как текущая на подтверждении CVAT) локализуется по под-шагу автоматически, и в будущем не приходится дописывать логи реактивно.

Твоя интуиция («загрузка артефактов → сама фаза → подтверждение → создание артефактов») — верна. Ниже она обобщена: под-шаги «загрузка входов» и «создание артефактов» — общие для всех фаз, а «сама фаза» и «подтверждение» — это два **архетипа** фаз.

---

## 1. Два архетипа фаз

**A. Автоматическая фаза** (worker, Celery): detection, direction, segmentation, skeletonization(×2), junction, graph, contours, ocr, fxml.
Под-шаги (упорядоченно):
`LOAD_INPUTS` → [`LOAD_MODEL`] → `COMPUTE` → `POSTPROCESS` → `PERSIST_ARTIFACTS` → `DISPATCH_NEXT`

**B. Фаза подтверждения** (api + оператор): cvat_validation(bbox), mask_validation, junction_validation, graph_validation, ocr_binding, contour_validation, frame_removal.
Под-шаги:
`OPEN` (подготовить/выдать оператору) → `AWAIT_OPERATOR` → `CONFIRM` (принять/забрать) → `PERSIST_VALIDATED` → `DISPATCH_NEXT`

> Твой кейс: подтверждение CVAT = архетип B, под-шаги **`CONFIRM`** (`export_annotations`) и **`PERSIST_VALIDATED`** (сохранить `coco_validated`/`yolo_validated`). Сейчас у них ноль инструментирования — отсюда «непонятно, где встало».

Общие «книжные концовки» (bookends) обоих архетипов — `LOAD_INPUTS` и `PERSIST_*`. Это и есть «загрузка артефактов в фазу» и «создание артефактов фазы» из твоей формулировки. detection дополнительно имеет `EXPORT_TO_CVAT` (мост в фазу B).

Полный набор под-шагов (enum `Step`):
`LOAD_INPUTS, LOAD_MODEL, COMPUTE, POSTPROCESS, PERSIST_ARTIFACTS, EXPORT_TO_CVAT, OPEN, AWAIT_OPERATOR, CONFIRM, PERSIST_VALIDATED, DISPATCH_NEXT`

---

## 2. Стабильная схема лог-события

Каждая строка лога несёт фиксированный набор полей (инъекция через `logging.Filter` + `contextvars`, задаётся один раз в начале фазы):

| Поле | Смысл |
|---|---|
| `ts`, `level`, `logger` | время, уровень, имя логгера = **где** (модуль:строка) |
| `uid` | diagram_uid — грепаемость по диаграмме |
| `phase` | StageType (detecting, cvat_validation, …) |
| `step` | под-шаг (load_inputs, compute, confirm, …) |
| `attempt`, `task_id` | попытка + celery_task_id (для worker) |
| `event` | `start` / `end` / `error` / `progress` |
| `duration_ms` | на `end` — длительность под-шага |
| `code` | на `error` — доменный код ошибки (§4) |
| свободные поля | `artifact`, `path`, `size`, `count`, `model`, `device`, `http_status`, … |

Стабильность набора полей важнее формата: сегодня грепаешь глазами, завтра включаешь JSON-сток и это уезжает в Loki/ELK **без переинструментирования**.

---

## 3. Примитив инструментирования (один helper на весь код)

Ключ к единообразию — не расставлять логи руками, а оборачивать под-шаг контекст-менеджером. Новый `app/core/obs.py`:

```python
import time, logging, contextvars
from contextlib import contextmanager
from pathlib import Path
from app.core.errors import PipelineError, ArtifactMissingError, ArtifactWriteError

_ctx = contextvars.ContextVar("ctx", default={})
def bind(**kw): _ctx.set({**_ctx.get(), **kw})     # вызывается в начале фазы

@contextmanager
def step(name, logger, **fields):
    t0 = time.perf_counter()
    logger.info("step.start", extra={"step": name, "event": "start", **fields})
    try:
        yield
    except PipelineError as e:
        e.step = e.step or name
        logger.error("step.error", extra={"step": name, "event": "error", "code": e.code}, exc_info=True)
        raise
    except Exception as e:
        logger.error("step.error", extra={"step": name, "event": "error", "code": "unexpected"}, exc_info=True)
        raise PipelineError(str(e), stage=_ctx.get().get("phase"), cause=e) from e   # типизируем неожиданное
    else:
        logger.info("step.end", extra={"step": name, "event": "end",
                                       "duration_ms": round((time.perf_counter()-t0)*1000)})
```

Обёртки артефактного I/O (общие «концовки» из твоей мысли):

```python
@contextmanager
def load_artifact(kind, path, logger):
    with step("load_inputs", logger, artifact=kind, path=str(path)):
        if not Path(path).exists():
            raise ArtifactMissingError(f"{kind} not found: {path}")
        yield path

def persist_artifact(db, uid, kind, path, base, logger):
    with step("persist_artifacts", logger, artifact=kind, path=str(path)):
        try:
            return upsert_artifact(db, uid, kind, path, base)
        except Exception as e:
            raise ArtifactWriteError(f"failed to persist {kind}: {path}", cause=e) from e
```

Применение в задаче/эндпойнте становится декларативным и одинаковым везде:

```python
bind(uid=uid, phase="segmenting", attempt=attempt, task_id=self.request.id)
with load_artifact("PIPE_MASK", pipe_mask_path, log): ...
with step("load_model", log, weights=weights, device=device): model = load(...)
with step("compute", log, tiles=n): prob = engine.predict(...)
with step("postprocess", log): mask = postprocess(prob)
persist_artifact(db, uid, "PIPE_MASK", out_path, base, log)
```

CVAT-подтверждение (твой кейс) — так же:
```python
bind(uid=uid, phase="cvat_validation")
with step("confirm", log, cvat_task_id=tid):          # export_annotations
    zip_path = cvat.export_annotations(tid, "COCO 1.0")   # внутри бросает CVATExportError/CVATTimeoutError
with step("persist_validated", log, artifact="COCO_VALIDATED"):
    save_validated(...)
```

---

## 4. Как ошибки привязываются к под-шагу (связка с иерархией)

- `step()` при неожиданном исключении оборачивает его в `PipelineError(step, phase, from exc)` → в логе `event=error step=... code=...` + полный трейсбек (`exc_info=True`).
- Доменные ошибки (`CVATExportError`, `InferenceError`, `ArtifactMissingError`, …) уже несут `code` и `stage`; `step()` дописывает `step`.
- В `fail_stage`/`set_diagram_error` пишем структурно: `error_code = exc.code`, **`failed_step = exc.step`**, `error_stage = exc.stage`, `error_traceback = tb`.
- Клиентский отчёт и `/stages` отдают `phase + step + code` → «где встало» видно и в UI, и в логах.

Итог: `phase=cvat_validation step=confirm code=cvat_export_timeout` — самоописательно, без гадания.

---

## 5. Что durable, что ephemeral (важно, чтобы не переусложнить)

- **Под-шаги — это слой ЛОГОВ и атрибуции ошибок, не новые строки в БД.** НЕ заводим таблицу `processing_substages`: это противоречит твоему «чистить завершённые» и добавляет запись в горячих циклах.
- **`processing_stages` остаётся 1 строка на фазу.** На неё добавляем `error_code` (решено) + `failed_step` (мелкая колонка) — только заполняются при ошибке.
- Полная детализация под-шагов живёт в логах (эфемерно, чистится на успехе, как договорились). На FAILED — сводка (failed_step + code + traceback) уезжает в `processing_stages` и в клиент.

---

## 6. Формат

- **Сейчас:** человекочитаемый `key=value` в консоль (дружелюбно для `docker logs | grep`). Стабильные поля (§2) гарантируются фильтром, не форматом.
- **На будущее:** второй JSON-сток (тот же набор полей) включается флагом — и логи готовы к Loki/Grafana/ELK без переписывания. Рекомендую заложить схему полей сразу, JSON-сток — опционально позже.

---

## 7. Маппинг «фаза → применимые под-шаги»

| Фаза | Архетип | Под-шаги |
|---|---|---|
| upload | A' (api) | LOAD_INPUTS(файл) → COMPUTE(pdf render/validate) → PERSIST_ARTIFACTS(original) |
| detection | A | LOAD_INPUTS → LOAD_MODEL → COMPUTE(SAHI×ensemble) → POSTPROCESS(WBF/filters) → PERSIST_ARTIFACTS → **EXPORT_TO_CVAT** → DISPATCH |
| cvat_validation | **B** | OPEN(open_cvat) → AWAIT_OPERATOR → **CONFIRM(export)** → **PERSIST_VALIDATED** → DISPATCH |
| segmentation / skeleton×2 / junction / graph / contours / ocr / fxml | A | LOAD_INPUTS → [LOAD_MODEL] → COMPUTE → POSTPROCESS → PERSIST_ARTIFACTS → DISPATCH |
| mask/junction/graph/contour/ocr validation, frame_removal | B | OPEN → AWAIT_OPERATOR → CONFIRM → PERSIST_VALIDATED → DISPATCH |

---

## 8. Порядок внедрения (ложится на уже принятые решения)

1. Фундамент: `app/core/errors.py` (иерархия) + `app/core/obs.py` (`bind`/`step`/`artifact I/O`) + `contextvars`-фильтр и формат + миграция `error_code` + `failed_step` + `celery_task_id` в `/stages` + stdout-мост.
2. **Первой реальной фазой — cvat_validation (твоя боль):** обернуть `CONFIRM`(`export_annotations`) и `PERSIST_VALIDATED` в `step()`, ввести `CVATExportError/CVATTimeoutError/CVATRequestError`, залогировать http-статус/тело/попытку.
3. Дальше по волнам (поэтапно, но все — как решили): detection+CVAT push → segmentation/skeleton/graph → ocr/junction/contours/fxml → upload/frame.
4. Каждую фазу приводим к **шаблону** (§3) — это и есть гарант единообразия.

---

## 9. Решения (зафиксировано)

1. **`failed_step` — отдельной колонкой** ✅ (queryable). В `processing_stages`: `failed_step String(32) nullable`, заполняется из `exc.step` в `fail_stage`.
2. **Схема полей сейчас, JSON-сток позже** ✅. Стабильный набор полей (§2) вводим сразу через фильтр; console `key=value` сейчас, JSON-handler включается флагом когда понадобится Loki/ELK.
3. **`COMPUTE` бьём на под-под-шаги** ✅. Для длинных фаз: detection → `tiling`/`inference`/`fusion`; ocr → `text_detect`/`recognize`/`postfilter`; segmentation → `tiling`/`inference`/`tta`/`stitch`. Каждый под-под-шаг = свой `step()` с duration и complexity-полями (см. §10).

---

## 10. Наблюдаемость тайминга и сложности (CPU-сервер)

На CPU всё медленнее и чувствительно к размеру входа — поэтому логируем не только «сколько заняло», но и «почему столько».

- **Duration на каждом под-шаге** — уже даёт `step()` (`event=end duration_ms`). С дроблением `COMPUTE` (§9.3) видно, где именно время: tiling vs inference vs fusion.
- **Complexity-поля на `start` под-шага** — чтобы медленный шаг был объясним: `megapixels` (из `image_width×height`), `tiles`, `objects`/`detection_count`, `batch`, `nodes`/`edges`. Пример строки: `step=inference event=start tiles=486 megapixels=105`.
- **Slow-step WARNING по бюджету.** Бюджет = исторический p95 из `processing_stages`, **нормированный по сложности** (ms/мегапиксель или ms/тайл), либо статический дефолт для первого прогона. Если `duration > k×бюджет` → `WARNING step.slow ratio=3.2`. Самокалибруется по мере прогонов.
- **Complexity WARNING до `compute`.** Если вход аномально большой (мегапиксели/объекты выше порога) → `WARNING` заранее: «ожидается долгий прогон». 
- **Про потоки CPU — пока только наблюдаем, не трогаем.** Сейчас `torch` не ограничен (нет `set_num_threads`/`OMP_NUM_THREADS`), и при `concurrency=2` на CPU две задачи оверсабскрайбят ядра. **Решено НЕ менять параллелизм** (работает, жалоб нет). Только логируем фактическое число потоков и `active_running`, чтобы «медленно под нагрузкой» было видно в цифрах. Менять concurrency/потоки/воркеры — отдельно и позже, по этим цифрам.

---

## 11. Многопользовательскость: текущее состояние + что логировать

**Прямой ответ на «все ли этапы многопользовательские»: изолированно — да, по пропускной способности — нет.**

- **Изоляция — да.** Всё scoped по `diagram_uid`, общего мутабельного состояния нет, явных локов нет (`get_project_loader` — read-only `@lru_cache`; `get_cvat_client` — синглтон-клиент). Несколько пользователей работают безопасно, данные не путаются.
- **Пропускная способность — жёсткий потолок.** Один `pid_worker` `--concurrency=2 -Q default,gpu,sam2` → **максимум 2 тяжёлых задачи одновременно на всю систему** (detection/segmentation/skeleton/junction/graph/contours конкурируют за эти 2 слота). Один `pid_worker_ocr` `--concurrency=1` → **OCR строго по одному**. Реплик нет. Больше пользователей → задачи копятся в Redis-очереди, растёт ожидание. OCR — самое узкое место.
- **CPU усугубляет:** без лимита потоков две параллельные задачи оверсабскрайбят ядра (§10) → каждая ещё медленнее под нагрузкой.
- **API:** один uvicorn worker (async, для polling ок), но подтверждение CVAT идёт через `asyncio.to_thread` + общий синглтон-клиент — много одновременных подтверждений могут исчерпать дефолтный тред-пул и конкурировать за один клиент.
- **БД:** async 5+10, sync 5+10 соединений — умеренно; много поллящих клиентов добавляют нагрузку, но не главный лимит.
- **Идентификации пользователя НЕТ вообще** (ни `user_id`/`owner`, ни auth, ни заголовков). → «проблемы **по конкретному пользователю**» атрибутировать сейчас нельзя, видно только агрегатную конкуренцию.

**Что логировать, чтобы видеть проблемы нагрузки:**
- `queue_wait_ms` — от диспатча (API) до `start_stage.started_at`. Растёт → бэклог/нехватка воркеров.
- `active_running` — сколько стадий в статусе RUNNING на момент старта задачи (один запрос). Показывает загрузку.
- `queue_depth` — `LLEN` очередей Redis (celery inspect) периодически / на старте.
- `db_pool_wait` — лог при ожидании/overflow пула.
- `cvat_inflight` — сколько экспортов CVAT одновременно (тред-пул).
- **Для атрибуции по пользователю** — ввести `client_id` (из `client.cfg` или заголовка) в контекст логов (и опц. в `Diagram`). Это предпосылка к «проблемам по пользователям»; иначе — только агрегат.

> Замечание: если нужна реальная многопользовательская пропускная способность на CPU — это уже про масштаб (реплики воркеров/очередей, лимит потоков), отдельно от логов. Логи здесь дают видимость проблемы; решение — инфраструктурное.

---

## 12. Ретеншн логов (успех vs ошибка)

Принцип: **успешный прогон не оставляет почти ничего долговременного; ошибка оставляет ограниченный структурный диагностик, который сам вычищается через N дней.**

- **Многословные логи под-шагов — эфемерны.** Уходят в docker `json-file` с ротацией (`max-size: 20m, max-file: 5`) → на успехе просто устаревают и вытесняются за часы/сутки. Это и есть ответ на «зачем хранить, если без ошибок» — не храним, самоистекают.
- **`processing_stages` — оставляем** (крошечные строки). Нужны для ETA-медиан и бюджетов тайминга (§10). На успехе трейсбека нет.
- **На ошибке — durable, но ограниченно:** `failed_step + error_code + error_traceback` в строке стадии + опц. «бандл» последних N строк лога в `storage/diagrams/{uid}/error_report.txt`.
- **Очистка:** бандл удаляется при успешном ре-ране этой диаграммы или переходе в `completed`; scheduled purge раз в сутки чистит `error_traceback` и бандлы старше N дней (конфиг, напр. 14–30 дней).
- Итог: чистый прогон → только крошечная строка тайминга; ошибка → структурный диагностик на N дней; безграничного роста нет ни там, ни там.

---

## 13. Решения (финально)

- **Порог slow-step WARNING = ×3** от бюджета; бюджеты снимаем с первых 1–2 прогонов на CPU.
- **Ретеншн ошибок = 30 дней**; «бандл» последних строк при ошибке — **включаем**. Успешные прогоны не храним.
- **`client_id` — НЕ вводим** (нагрузку видим агрегатно).
- **Параллелизм (concurrency/потоки/воркеры) — НЕ трогаем.** Работает, жалоб нет. Только собираем статистику (§14).

---

## 14. Сбор статистики за неделю (перед решением по параллелизму)

Дать пользователям поработать ~неделю, накопить цифры и уже предметно решать, нужно ли двигать параллелизм и куда. Почти всё уже пишется в `processing_stages` (`started_at/completed_at/duration_seconds/status/attempt`) — добавить минимум.

Что собираем:
- **CPU-baseline по стадиям** — p50/p95/max длительности каждой стадии (реальные времена на CPU против старых GPU), из `duration_seconds`.
- **Ожидание в очереди `queue_wait`** — ключевой сигнал «мало воркеров». Нужен один малый доп: создавать строку стадии `PENDING` в момент **диспатча** (в API), воркер переводит в `RUNNING` → `queue_wait = started_at − created_at`. Бонус: «в очереди» становится видно и клиенту в прогрессе.
- **Пиковая одновременность** (`active_running` во времени) и **глубина очередей Redis** — упираемся ли в 2 слота.
- **Близость к таймаутам** — стадии, чья длительность подходит к celery `soft_time_limit` (он откалиброван под GPU!). Прямой предвестник «упадёт» на CPU — ловим ДО реального падения.
- **Отказы** — частота по стадии/`error_code`, повторы (`attempt`).
- **Корреляция сложности** — длительность vs мегапиксели/число объектов, чтобы прогнозировать.

Как: всё копится в `processing_stages` (строки не чистим — крошечные). В конце недели — простой SQL-отчёт (p50/p95/max, wait, failure-rate, timeout-proximity, пиковая одновременность), можно оформить scheduled-задачей «еженедельная сводка». По этим цифрам возвращаемся к параллелизму.

# RUNBOOK: логирование, ошибки, прогресс — план работ по волнам

Единый источник правды для внедрения. Один чат = одна волна. Документ самодостаточен: новый чат стартует, прочитав его.

---

## 0. Как пользоваться (для каждого нового чата)

1. В начале чата вставить **kickoff-промпт** волны (см. §7) + сказать «делаем Волну N».
2. Claude: (а) грепом строит line-level карту вставок этой волны, (б) пишет код дифами, (в) пишет тесты, (г) даёт команды на прогон. Ты ревьюишь и гоняешь.
3. Опорные документы (в этой же папке), если нужен контекст: `ARCH_logging_phase_step_model.md` (архитектура), `PLAN_serverside_debug_and_errors.md` (дебаг/иерархия), `PLAYBOOK_implementation_and_testing.md` (как тестируем), `FINAL_PLAN_progress_and_errors.md` (клиент).

**Принципы работы (из твоего CLAUDE.md — обязательны):** Think before coding (assumptions вслух, спорное — спросить), Simplicity first (минимум кода; листовые типы ошибок добавляем по мере надобности, не наперёд), Surgical changes (каждая изменённая строка → к цели волны; чужой код не трогаем), Goal-driven (у каждой волны — критерий готовности + тесты, чтобы чат мог зациклиться на проверке сам).

### 0.1 Доступы и привязки (в начале каждого чата-волны)

- **Привяжи папку репозитория** (подключение/выбор папки в начале чата) → чат получает Read/Edit на **реальные файлы** и правит их дифами напрямую. Без привязки чат работает с read-only клоном из GitHub и отдаёт дифы, а ты применяешь их руками (медленнее).
- **Держи планы в репо.** Закоммить эти документы в `docs/observability/` (RUNBOOK, ARCH, PLAN_serverside, PLAYBOOK). Тогда любой новый чат читает их сам из репо — не нужно переприкреплять.
- **Доступы, которые чату НЕ даём:** боевой сервер, твой Docker/Postgres/Redis/CVAT/GPU. Их касаешься только ты (запуск, миграции на стенде, smoke, деплой). Чат работает с кодом и чистыми тестами.
- **Коннекторы не нужны** (Slack/Jira/облака ни при чём). Нужны только: доступ к папке репо + песочница для грепа/чистых тестов (есть по умолчанию).
- **⚠️ Git-команды (add/commit/checkout/branch) выполняет ТОЛЬКО пользователь в своём терминале.** Запись в `.git/` через смонтированную папку падает (index.lock, «Operation not permitted») и портит индекс. Чат правит **файлы кода** (Read/Write/Edit — это работает), а весь git — пользователь. Восстановление после сбоя индекса: `del .git\index.lock` + `del .git\index` + `git reset`.

### 0.2 Протокол незапланированного (что делать, если всплыло то, что мы не обсуждали)

Обязателен для каждого чата (следует твоему CLAUDE.md: не решать молча, вскрывать, спрашивать).

Когда чат находит что-то вне обсуждённого — **не менять молча и не расширять объём волны.** Классифицировать:

1. **Мелкий блокер**, без которого волну не закрыть, и решение очевидно → сделать, но **явно отметить** в пояснении к дифу.
2. **Смежный баг/техдолг**, не нужный для этой волны → **не трогать**; занести в parking lot (§9) и спросить: сейчас или отдельной волной.
3. **Пробел дизайна** (новая категория ошибок, стадия со странной структурой, конфликт с зафиксированным решением) → **остановиться**, показать варианты с плюсами/минусами, спросить; после решения — **обновить RUNBOOK/ARCH** (живой источник правды).
4. **Неоднозначность** (несколько валидных трактовок) → показать их, **не выбирать молча**.

Правило: объём волны не растёт без твоего «ок». Любое новое решение записывается в RUNBOOK, чтобы будущие чаты его наследовали.

---

## 1. Контекст (рекап)

Система: FastAPI (`api`) + Celery (`worker`, `worker_ocr`) + Redis + Postgres + CVAT, тонкий Qt-клиент. Пайплайн из ~14 стадий (см. `docs/STATUS_MACHINE.md`). Репо: `github.com/gromli66/pid-pipeline`, ветка `deploy`. Рабочая ветка внедрения: **`feat/observability`**.

Целевая архитектура: каждая фаза = канон под-шагов (`LOAD_INPUTS → [LOAD_MODEL] → COMPUTE(дробим) → POSTPROCESS → PERSIST_ARTIFACTS → DISPATCH` для авто; `OPEN → AWAIT_OPERATOR → CONFIRM → PERSIST_VALIDATED → DISPATCH` для подтверждений), обёрнутых примитивом `step()`; доменная иерархия ошибок; корреляция `uid/phase/step/task_id` в каждой строке; `error_code`+`failed_step` в БД; прогресс+ошибки в клиенте; ретеншн: успех не храним, ошибки 30 дней.

**Зафиксированные решения (не пересматриваем):**
- `failed_step` — отдельной колонкой; `error_code` — отдельной колонкой.
- Стабильная схема полей логов сейчас; JSON-сток — позже (флагом).
- `COMPUTE` длинных стадий бьём на под-под-шаги (detection: tiling/inference/fusion; ocr: text_detect/recognize/postfilter; segmentation: tiling/inference/tta/stitch).
- Порог slow-step WARNING = ×3 от бюджета; бюджеты — с первых 1–2 прогонов.
- Ретеншн ошибок = 30 дней, «бандл» последних строк включаем; успешные прогоны не храним.
- `client_id` не вводим (нагрузка — агрегатно).
- **Параллелизм НЕ трогаем** (concurrency/потоки/воркеры). Только собираем метрики для решения через неделю.
- **Гранулярность инструментовки (решено 2026-07-08):** скелет-канон под-шагов — единообразно на всех стадиях; глубину `COMPUTE` дробим на под-под-шаги *по боли* (реальные ошибки) **и проактивно для всего, что заведомо медленно на CPU** (иначе на CPU-стенде стадия выглядит зависшей). Граница под-шага — на каждом I/O-стыке (вход/выход артефакта, запись в БД, внешний вызов) и вокруг длинных циклов; чистые in-memory преобразования отдельными шагами не оборачиваем. Две оси независимы: **логи** дробим смело; **типы ошибок** (`error_code`) — только по факту нужды, не наперёд. CVAT доминирует в жалобах во многом из-за экспозиции (единственный интерактивный + сетевой этап), но авто-стадии падают невидимо (пользователь видит лишь статус `error`) — их инструментовка по плану всё равно нужна.

---

## 2. Конвенции репо (проверено)

- **Тесты:** `pytest tests/ -v` — без Docker/GPU/БД (SQLite in-memory + MagicMock, `tests/conftest.py` ставит env и патчит async engine). Новые тесты кладём в `tests/`. Полезное: `pytest tests/ -x` (стоп на первом сбое), `-s` (видеть stdout).
- **Dev-среда:** инфра в Docker (`postgres:5433`, `redis:6380`, `cvat:8080`), код локально (venv Python **3.11**). Миграции: `alembic upgrade head` (внутри `pid_api` или локально).
- **Стиль:** surgical-диффы; не рефакторить соседний код; совпадать с существующим стилем; минимум абстракций.

---

## 3. Роли (кто что делает на каждой волне)

**Claude (в чате):**
- строит карту вставок (грепом по коду волны),
- пишет код **дифами по файлам** (по одному, чтобы ревьюить),
- пишет юнит- и fault-тесты в `tests/`,
- пишет alembic-миграции,
- обновляет затронутые доки (`docs/…`),
- даёт точные команды на прогон и smoke.

**Ты (пользователь):**
- ревьюишь дифы (принять/поправить),
- гоняешь `pytest tests/ -v` у себя,
- применяешь миграции на **стенде** (не сразу на бой),
- прогоняешь smoke: 1 маленькая диаграмма через затронутую стадию на CPU-стенде,
- присылаешь реальный текст ошибок, когда просят (особенно CVAT),
- мержишь ветку и выкатываешь, когда доволен.

Каждая волна ниже помечает **[нужен ты]** там, где без тебя не продвинуться.

---

## 4. Definition of Done (общий чек для КАЖДОЙ волны)

- [ ] `print(` в затронутом модуле → 0 (или явно через stdout-мост).
- [ ] нет `except:`/`except Exception` без `logger.*(exc_info=True)`; голых `pass` не осталось.
- [ ] каждый `raise` в пайплайн-коде — типизированный (`PipelineError`-потомок).
- [ ] у стадии есть баннер на старте (входы/размеры/модель) и `duration` на конце каждого под-шага.
- [ ] `/stages` по тестовой диаграмме отдаёт `error_code`+`failed_step`+`error_traceback`.
- [ ] новые тесты зелёные + существующие не сломаны (`pytest tests/ -v`).
- [ ] дифф surgical (каждая строка → к цели волны).
- [ ] поведение пайплайна не изменилось (те же сбои падают так же, просто теперь записаны).

---

## 5. Волны

### Волна 0 — Фундамент  (без неё остальные не лягут правильно)
**Цель/DoD:** примитивы есть и покрыты тестами; поведение пайплайна не изменилось; корреляция работает.
**Файлы:**
- `app/core/errors.py` *(new)* — `PipelineError(message, *, stage, diagram_uid, cause)` + `.code`/`.step`; семейства-заглушки (ArtifactMissingError, ModelLoadError, InferenceError→GpuOutOfMemoryError; по стадиям; CVATError-семейство). **Минимально — листья добавляем в своих волнах.**
- `app/core/obs.py` *(new)* — `bind(**ctx)`, `@contextmanager step(name, logger, **fields)`, `load_artifact`, `persist_artifact` (см. код-скетч в `ARCH_…md §3`).
- `app/core/logging.py` — `ContextFilter` (инъекция uid/phase/step/attempt/task_id), `LOG_FORMAT` с этими полями, уровни либ (`LOG_LEVEL` — только наши; `LOG_LEVEL_LIBS`=WARNING; `LOG_OVERRIDES`).
- `app/models/stage.py` — `+ error_code: str|None`, `+ failed_step: str|None`.
- `worker/utils/db_helpers.py` — `fail_stage` пишет `error_code=getattr(exc,'code',type(exc).__name__)`, `failed_step=getattr(exc,'step',None)`; `set_diagram_error` аналогично.
- `app/schemas/diagram.py` — `ProcessingStageResponse` `+ celery_task_id, + error_code, + failed_step`.
- `app/api/diagrams.py` — вернуть новые поля в `/stages`.
- `alembic/versions/xxxx_add_error_code_failed_step.py` *(new)*.
- `docker-compose.yml` — каждому сервису `logging: {driver: json-file, options: {max-size: 20m, max-file: 5}}`.
- `worker/celery_app.py` — сигнал `worker_process_init`: навесить ContextFilter/handler (после fork); включить stdout→logging мост.
**Тесты:** `tests/test_obs.py` (step логирует start/end/duration; при исключении оборачивает+логирует+пробрасывает типизированное), `tests/test_errors.py` (коды/стадии), `tests/test_db_helpers_errorcode.py` (fail_stage пишет code/failed_step). Прогон: `pytest tests/test_obs.py tests/test_errors.py tests/test_db_helpers_errorcode.py -v`.
**[нужен ты]:** применить миграцию на стенде (`alembic upgrade head`); `pytest tests/ -v` (все зелёные, старые не сломаны).

### Волна 1 — CVAT  (текущая боль; эталон для остальных)
**Цель/DoD:** любой сбой CVAT (в detection **и** в подтверждении) → типизированная ошибка + строка лога (операция, http-статус, тело-срез, попытка, тайминг) + запись в stage (`code/failed_step/traceback`), видно в `/stages`. Поведение то же: detection по-прежнему не валит пайплайн из-за CVAT, но теперь это **видимая warning-стадия**, а подтверждение отдаёт внятную типизированную ошибку.
**Файлы:**
- `app/services/cvat_client.py` — обернуть `login/create_task/_wait_for_job/import_annotations/export_annotations` в `CVATConnectionError/CVATTimeoutError/CVATRequestError/CVATImportError/CVATExportError` + `logger`.
- `app/api/cvat.py` — `fetch_cvat_annotations`/`create_cvat_task_endpoint`: `logger.error(exc_info=True)`, типизированные ошибки, `step("confirm")`/`step("persist_validated")`.
- `worker/tasks/detection.py:254-322` — `except Exception as cvat_exc` → `except CVATError`; писать warning-stage + `logger`; не-CVAT наверх.
- `app/core/errors.py` — добить листья CVAT если нужно.
**Тесты:** `tests/test_cvat_errors.py` — мок httpx: `500→CVATRequestError`, таймаут→`CVATTimeoutError`, пустой zip→`CVATExportError`, нет json→`CVATExportError`; assert тип + запись stage-полей + строка лога.
**[нужен ты]:** прислать реальный текст ошибки подтверждения (сверить, что ветка совпала); smoke — одно подтверждение CVAT на стенде, глянуть логи `api` (`docker logs pid_api | grep <uid>`).

**Расширение объёма (решено 2026-07-08, по §0.2):** реальные ошибки клиентов — ДВЕ семьи, не только CVAT-транспорт:
- **A. Состояние** — `Cannot fetch annotations: status is '…', expected 'validating_bbox'`: не сбой CVAT, а неверный статус диаграммы. Причина: клиентский «возврат на проверку» не делал реального отката (rollback→reopen), статус висел на `skeletonizing`. Введён `StageStateError` (`stage_state_invalid`) + warning-лог на предусловии `confirm`.
- **B. CVAT-транспорт** — `Export … 400 not finished` / таймаут / 5xx: типизированы `CVATExportError`/… (сделано, см. ниже).
- Новый `POST /api/cvat/{uid}/reopen-bbox-validation` — «жёсткий стоп»: revoke бегущей стадии (по `celery_task_id`) → сброс артефактов после `detected` → статус `validating_bbox` → переоткрытие ТОЙ ЖЕ CVAT-job. Решение с пользователем: **жёсткий стоп, ручную разметку в CVAT не теряем** (таск не пересоздаётся).
  - Механика (проверено по коду): `rollback` откатывает на любой **стабильный** этап, включая `detected` (не только на рамку — частая путаница); `open-validation` переоткрывает ТУ ЖЕ CVAT-job по `cvat_task_id`, а `rollback` сам CVAT-таск не трогает → поэтому ручная разметка сохраняется. `validating_bbox` — транзитный, целью отката быть не может (только `detected` + переоткрытие).

**Сделано (2026-07-08, код закрыт):**
- `errors.py` — CVAT-листья (`CVATConnectionError/Timeout/Request/Import/Export`) + `StageStateError`.
- `app/api/cvat.py` — `POST /api/cvat/{uid}/reopen-bbox-validation` (жёсткий стоп) + `StageStateError`-warning на предусловии `confirm` + `obs.bind`/`step("confirm")` вокруг экспорта.
- `app/services/cvat_client.py` — `_cvat_op`/`_cvat_call`: httpx → CVAT-типы + строка лога (op/http_status/тело-срез/тайминг); обёрнуты `login`/`get_or_create_project`/`create_task`/`_wait_for_job`/`import`/`export`.
- `worker/tasks/detection.py` — `except CVATError` (CVAT non-fatal + warning-лог + `error_code` на стадии; не-CVAT наверх).
- `app/core/obs.py` — `code` в сообщении `step.error` (виден в консоли без чтения traceback).
- `tests/observability/test_cvat_errors.py` — типы + httpx-мок (`500→Request`, `timeout→Timeout`, `connect→Connection`, `wrap→Import/Export`, доменный пропуск). `pytest tests/observability -v` = **27 зелёных**.
- Смоук на стенде: reopen (разметка в CVAT сохранена), confirm в неверном статусе → `StageStateError`, CVAT down → `step=confirm code=cvat_export`.

**Осталось по Волне 1 (вернулись закрыть — решено 2026-07-08):**
- **Дыра DoD «видно в /stages»:** интерактивные CVAT-эндпоинты (`create-task`, `fetch/confirm`) НЕ пишут строку `ProcessingStage` → CVAT-сбои не доходят до `/stages` и до окна ошибки клиента. Закрыть по **Варианту A** (§9 #8): одна строка `cvat_validation` на CVAT-фазу + `fail_stage` с `error_code`/`failed_step`; под-шаги различаем полем `failed_step`. Обязательный болевой под-шаг сразу — `upload_media` (CVAT отвергает большой файл, реальная жалоба).
  - **Реализация (решено 2026-07-08):** строку `cvat_validation` пишем прямо через **async-сессию** в эндпоинте (маленький async-хелпер) — воркерные `start_stage`/`fail_stage` синхронные, их НЕ переиспользуем.
  - **`failed_step` — точность:** `create_task` обернуть по двум фазам — оболочка (`POST /tasks`) vs загрузка медиа (`POST /tasks/{id}/data`), чтобы отличать `upload_media` (большой файл) от общего сбоя создания; `fetch` — общий шаг.
- `step("persist_validated")` в `fetch` — отложено осознанно; можно добить заодно.
- ✅ Клиентская кнопка «Проверка элементов» → `reopen-bbox-validation` + `cvat_url` — сделано в Волне 2 (§9 #4).

**Тесты наблюдаемости — `tests/observability/` (отдельный прогон: `pytest tests/observability -v`, сейчас 27 зелёных):**
- `test_errors.py` — иерархия `PipelineError`, стабильные `code`, корреляционные поля (`stage`/`step`/`diagram_uid`/`cause`).
- `test_obs.py` — `step()`: лог start/end + `duration_ms`; сбой → типизированная ошибка с проставленным `step`; `load_artifact`.
- `test_logging.py` — `ContextFilter` (инъекция uid/phase/step/attempt/task_id; дефолт `-`), парсер `LOG_OVERRIDES`.
- `test_db_helpers_errorcode.py` — `fail_stage(exc=…)` пишет `error_code`/`failed_step` (типизир. exc → code/step; обычный → имя типа; без exc → NULL).
- `test_cvat_errors.py` — типы CVAT + `StageStateError`; httpx-мок `_cvat_op` (`500→Request`, `timeout→Timeout`, `connect→Connection`, `wrap→Import/Export`, доменный пропуск).
> Изолированы намеренно: старую `tests/` не трогаем, полный `pytest tests/` чинится отдельно на `pr0/fix-test-infra` (§9).

### Волна 2 — Клиент: прогресс + окно ошибки  (на фикстурах, без пайплайна)
**Цель/DoD:** прогресс-бар детерминированный (фаза + под-шаг + ETA); при FAILED-стадии — окно с `error_traceback`/`phase`/`step`/`code` + «Копировать/Сохранить».
**Файлы:** `ui/services/status_provider.py` (+опрос `/stages`), `ui/windows/main_window.py` (`progress_bar` determinate), `ui/services/progress_model.py` *(new)*, `ui/widgets/error_report_dialog.py` *(new)*, `ui/widgets/diagram_workspace.py` (открыть отчёт из `_apply_error_status`), `app/api/stats.py` *(new — `/api/stats/stage-durations`)*, малое поле `current_step` на стадии (чтобы показывать под-шаг).
**Тесты:** `tests/test_progress_model.py` (проценты/ETA из фейковых строк стадий). Клиент — визуально на фикстурных строках stage (RUNNING/FAILED), пайплайн не гоняем.
**[нужен ты]:** пересобрать клиент; визуально проверить бар и окно ошибки на 1–2 диаграммах.

### Волны 3+ — остальные стадии  (по шаблону Волны 1)
Порядок: detection (compute→tiling/inference/fusion) → segmentation/skeleton/graph → ocr/junction/contours/fxml → upload/frame.
Каждая: карта вставок → `step()`+типы+узкие except → fault-тесты → smoke. Немые `except: pass` (`segmentation.py:91/131/135`, `graph.py:246`) и `print`-модули (`graph/core` 128, `skeleton_extension` 185, `pipe_segmentation` 281) закрываются в своих волнах.

### Волна финальная — недельная сводка
`app/api/stats.py` или scheduled-задача: SQL по `processing_stages` → p50/p95/max по стадиям, `queue_wait`, пик одновременности, близость к `soft_time_limit`, failure-rate. По цифрам возвращаемся к параллелизму.

---

## 6. Безопасность выкатки

Аддитивно и поведение-сохраняюще. Ветка `feat/observability` → стенд (локальный docker-compose или копия) → бой. Волна за волной, каждая обратима. Мост `print→logging` закрывает переход (не мигрированные `print` уже тегируются). На бой — только после того, как ты доволен smoke на стенде.

---

## 7. Kickoff-промпты для новых чатов (копипаст)

> Перед вставкой: **привяжи папку репо** (§0.1). Каждый промпт подразумевает «соблюдай §0.2 — на незапланированном не меняй молча, занеси в §9, спроси».

**Волна 0:**
> Работаем по `RUNBOOK_execution_plan.md`, Волна 0 (Фундамент). Репо gromli66/pid-pipeline, ветка deploy, рабочая ветка feat/observability. Построй карту вставок, дай дифы по файлам из §5-Волна0 по одному, напиши тесты в tests/. Соблюдай CLAUDE.md (surgical/simplicity). В конце — команды pytest и что применить мне.

**Волна 1 (CVAT):**
> Работаем по `RUNBOOK_execution_plan.md`, Волна 1 (CVAT). Фундамент (Волна 0) уже в ветке feat/observability. Сначала грепни cvat_client.py / api/cvat.py / detection.py:254-322 и покажи карту вставок, потом дифы + tests/test_cvat_errors.py. Вот реальный текст моей ошибки подтверждения: «<ВСТАВИТЬ>».

**Любая стадийная волна N:**
> Работаем по `RUNBOOK_execution_plan.md`, Волна N (<стадия>). Фундамент есть. Грепни задачу стадии + её модули на except/raise/print, покажи карту вставок, размапь на step()+типы ошибок, дай дифы + fault-тесты. Не трогай параллелизм. Соблюдай Definition of Done (§4).

---

## 8. Трекинг прогресса волн

| Волна | Статус | Ветка/PR | Дата |
|---|---|---|---|
| 0 Фундамент | ✅ готово (смоук на стенде) | feat/observability | 2026-07-08 |
| 1 CVAT | ✅ ядро готово; **переоткрыта для закрытия DoD-дыры** — `create-task`/`fetch` не пишут строку в `/stages` (§9 #8, вариант A, порядок — §8.4) | feat/observability | 2026-07-08 |
| 2 Клиент | ✅ проверено визуально: окно ошибки (FAILED), прогресс-заливка в кнопке (RUNNING; верхний бар убран, §9 #9), reopen-диалог «Проверка элементов». Отложено осознанно: `stats`-эндпоинт, `current_step` | feat/observability | 2026-07-08 |
| 3 detection | ⬜ | | |
| 3 segmentation/skeleton/graph | ⬜ | | |
| 3 ocr/junction/contours/fxml | ⬜ | | |
| 3 upload/frame | ⬜ | | |
| Fin недельная сводка | ⬜ | | |

(обновляй статусы по мере закрытия — так новый чат сразу видит, где мы)

### 8.1 Волна 0 (Фундамент) — закрыто 2026-07-08

**Сделано (ветка `feat/observability`):**
- `app/core/errors.py` — иерархия `PipelineError` (Artifact*/ModelLoad/Inference→GpuOOM/CVATError-стаб).
- `app/core/obs.py` — `bind`/`step`/`load_artifact`/`persist_artifact`.
- `app/core/logging.py` — `ContextFilter` (uid/phase/step/attempt/task_id), корреляционный `LOG_FORMAT`, `LOG_LEVEL`/`LOG_LEVEL_LIBS`/`LOG_OVERRIDES`.
- `app/models/stage.py` + `alembic/versions/0007_add_error_code_failed_step.py` — колонки `error_code`/`failed_step` (миграция применена на стенде).
- `worker/utils/db_helpers.py` — `fail_stage(exc=…)` пишет `error_code`/`failed_step`.
- `app/schemas/diagram.py` + `app/api/diagrams.py` — `celery_task_id`/`error_code`/`failed_step` в `/stages`.
- `worker/celery_app.py` — логи воркера через сигнал Celery `setup_logging` + мост `stdout→logging` (после fork).
- `docker-compose.yml` — `json-file` ротация (20m×5) на 5 сервисах P&ID.
- Тесты: `tests/observability/` (errors/obs/db_helpers/logging) — 19 зелёных; смоук на стенде пройден.

**Решения (наследуются волнами):**
- `error_code`/`failed_step` — только на `ProcessingStage`, НЕ на `Diagram`.
- `fail_stage` умеет писать код/шаг, но call-sites `exc=` в Волне 0 НЕ проводим — каждая стадийная волна проводит свои.
- Ротация docker-логов — только 5 сервисов P&ID; CVAT-стек (10) не трогаем.
- Логи воркера держатся на сигнале Celery `setup_logging` (иначе Celery хайджекает root и наш формат в задачах теряется).
- Тесты волн живут в `tests/observability/`, гоняются отдельно (`pytest tests/observability -v`); старую `tests/` не трогаем.

### 8.2 Волна 1 — тест-сессия + фикс reopen (2026-07-08)

Ручная тест-сессия по `TEST_SESSION_wave0-1.md` (стенд Windows/PowerShell, `curl.exe`, docker-compose). Журнал прогонов — §7 того файла.

**Проверено вживую (всё зелёное):** корреляц. формат логов + мост `worker.stdout` + Celery-логи в нашем формате; `/stages` отдаёт `celery_task_id`/`error_code`/`failed_step` (`null` на успехе); ротация `json-file 20m×5`; полный прогон клиента до FXML (10 стадий `completed`, регрессий нет); reopen сценарий A (downstream сброшен, разметка CVAT цела, confirm после проходит); состояние-ошибки `confirm`/`reopen rejected` (HTTP 400/409 + лог по `uid`); CVAT-down → `code=cvat_export` (типизировано).

**Найдено и исправлено:**
- **Баг DoD «жёсткий стоп» (§9 #5):** `reopen-bbox-validation` не ревокал бегущую авто-стадию. Причина: `start_stage` коммитил RUNNING-строку только `flush()`; reopen читает стадии в ОТДЕЛЬНОЙ сессии → незакоммиченную строку не видит (dirty-read не даёт ни один уровень изоляции) → `revoked_tasks=0`, задача добегает и перетирает статус (гонка, уезжало в `skeletonized`). **Фикс (Вариант 1):** `start_stage` `flush()`→`commit()` (RUNNING видна сразу + бонусом видна в `/stages` вживую) + регресс-тест `tests/observability/test_start_stage_commit.py`. Перепроверено: `revoked 0→1`, статус остаётся `validating_bbox`; `pytest tests/observability` = 29 passed.
- **Наблюдаемость `rejected`-логов:** в `confirm rejected`/`reopen rejected` вынесены `code`/`from_status` в текст сообщения (были только в `extra` — не видны в `docker logs`; JSON-сток по-прежнему позже).

**Решения / отложено:**
- Предохранитель статуса в телах задач (Вариант 2: перед финальным commit проверять, что статус ещё «свой») — **отложен**; добавить, если поймаем остаточную микро-гонку на стыке стадий.
- CVAT-транспорт-типы уже сделаны в Волне 1 (`code=cvat_export`) → прозу `TEST_SESSION §0/§5` (была «pending») поправил.
- `/status` без поля `status` (§9 #6) — оказалось артефактом копипаста, не баг.

### 8.3 Волна 2 (Клиент) — код готов, ждёт визуальной проверки (2026-07-08)

Только клиент, без нагрузки на сервер. Проверка — на фикстурных строках stage (RUNNING/FAILED), пайплайн не гоняли.

**Сделано (ветка `feat/observability`):**
- `ui/services/progress_model.py` *(new)* — чистый Python (без Qt): `compute_progress(stages)` → `state/percent/phase_label/running_stage/eta_seconds`. Прогресс по-этапный (14 фаз канона), взвешен по бюджетам; `fxml_generation=completed` → 100%. ETA динамическая: (бюджет бегущей − elapsed) + бюджеты оставшихся авто-стадий, ×калибровка (медиана факт/бюджет по завершённым стадиям ЭТОЙ диаграммы, кламп 0.5–2.0). Ручные/await-стадии (`cvat_validation`/`mask_validation`/`graph_validation` — budget=None) → без ETA.
- `ui/widgets/error_report_dialog.py` *(new)* — `QDialog` из строки FAILED-стадии: phase / step(`failed_step`) / code(`error_code`) / message + traceback (моно, read-only) + Копировать/Сохранить/Перезапустить.
- `ui/services/status_provider.py` — сигнал `stages_updated(uid, list)`; `_poll`/`force_update` эмитят `/stages` каждый опрос (ETA тикает).
- `ui/windows/main_window.py` — `progress_bar` determinate из `stages_updated` («фаза · ~ETA»); busy-«бегунок» (`setRange(0,0)`) оставлен для upload.
- `ui/widgets/diagram_workspace.py` — `_apply_error_status` хранит полную строку стадии; `_show_stage_error_dialog` → `ErrorReportDialog`; `_open_cvat` трёхветочный (DETECTED→open-validation / VALIDATING_BBOX→get-url / иначе→`reopen_bbox_validation` c подтверждением) — закрывает §9 #4; `_on_button_click` не гоняет `cvat` через общий rollback.
- `ui/services/api_client.py` — `reopen_bbox_validation(uid)`.
- Тест: `tests/observability/test_progress_model.py` — **11 зелёных** (headless: грузит модуль по пути, минуя `ui/services/__init__.py`/Qt).

**Решения (наследуются волнами):**
- **ETA без сервера:** `app/api/stats.py` (`/api/stats/stage-durations`) из §5-Волна2 **НЕ делаем** (решение пользователя: «динамично, без нагрузки на сервер»). Источник — статические бюджеты в `progress_model` + клиентская калибровка по факт-длительностям завершённых стадий. Бюджеты перекрываются параметром `budgets=` (сюда позже можно подать реальные p50, если захотим).
- **Под-шаг в UI отложен:** поля `current_step` на стадии нет → прогресс по-этапный, не под-шаговый. Реальная потребность пользователя — **под-шаг в ЛОГАХ** стадий (detection «SAHI загружено» и молчит) → §9 #7 (worker, Волна 3).
- `progress_model.py` — БЕЗ `from __future__ import annotations` (иначе `@dataclass` при загрузке по пути падает на резолве строковых аннотаций через `sys.modules`).
- Тесты Волны 2 — в `tests/observability/` (путь `tests/observability/test_progress_model.py`, не `tests/` как в §5), как Волны 0–1.

**Осталось по Волне 2 [нужен ты]:** пересобрать клиент; визуально: determinate-бар на RUNNING, окно ошибки на FAILED (1–2 диаграммы), кнопка «Проверка элементов» с позднего этапа (жёсткий стоп + разметка CVAT цела). Опц.: `app/api/stats.py` / `current_step` — если решишь добить позже.

**Правка направления (2026-07-08, в закрытии Волны 2):** верхний глобальный determinate-бар **убираем** — он один на окно и при нескольких бегущих диаграммах мельтешил (§9 #9). Прогресс переносим В активную кнопку-стадию: заливка (по времени — `elapsed/бюджет` + калибровка) + `N%` прямо в кнопке бегущего этапа; на завершении кнопка зеленеет. Per-diagram по построению (живёт в workspace = одна диаграмма) и per-stage. Ручные стадии (без бюджета) — прежний «крутящийся» бид. Честная заливка по факту работы — когда воркер начнёт репортить под-шаги (`current_step`, Волна 3); пока — оценка по времени. Upload-«бегунок» (busy) остаётся.

### 8.4 Порядок дальше (решено 2026-07-08)

Сначала закрываем начатое, потом расширяем (нашли недоделанное — возвращаемся сразу, не копим).

1. **Волна 2 — закрыть визуалкой** [нужен ты]: пересобрать клиент, проверить determinate-бар (RUNNING), окно ошибки (FAILED), кнопку «Проверка элементов» с позднего этапа. Код + 41 тест зелёные; `stats`-эндпоинт и `current_step` осознанно не делаем.
2. **Волна 1 — закрыть DoD-дыру:** `create-task`/`fetch` → строка `cvat_validation` + `fail_stage` (**Вариант A**, §9 #8); обязательный под-шаг `upload_media`; опц. `step("persist_validated")`.
3. **После — расширение:** CVAT-углубление (тонкая карта отказов: login/project · task-shell · media-upload · wait-job · open/reopen · export[request→ready→download→parse] · import · state; отдельные `error_code` — по боли) + авто-стадии Волн 3+. Инкрементально: по боли + проактивно медленное-на-CPU. Идея отдельной «Волны 1.5» распущена — нужная часть ушла в п.2.

---

## 9. Parking lot (найденное вне объёма — по §0.2)

Сюда чат заносит всё, что всплыло, но не входит в текущую волну. Ты решаешь: взять отдельной волной, сделать сейчас, или отклонить.

| # | Что нашли (файл/место) | Тип (баг/долг/пробел дизайна) | Предложение | Решение |
|---|---|---|---|---|
| 1 | Celery хайджекает root-логгер → логи задач не в нашем формате (смоук Волны 0) | баг инфры | сигнал `setup_logging` вместо голого `worker_process_init` | ✅ решено в Волне 0 (`celery_app.py`) |
| 2 | `pytest tests/` рушится на сборке (`cv2`/`torch`/`celery` нет в anaconda) + устаревшие тесты (`test_refactoring*`, `test_stage7_graph_flow`, cp1251 в `.read_text`) | тех-долг тест-инфры | изолировали тесты волн в `tests/observability/`; полная чистка — на `pr0/fix-test-infra` (importorskip + xfail) | ⬜ отложено (pr0) |
| 3 | Индекс git на смонтированной папке ловит `index.lock`/«удаления» (сценарий §0.1) | инфра-риск | восстановление: `del .git\index.lock` + `del .git\index` + `git reset` | ✅ подтверждено, задокументировано (§0.1) |
| 4 | Кнопка «Проверка элементов» в клиенте не зовёт бэкенд-откат — переоткрывает CVAT визуально (источник ошибки `status is skeletonizing`) | баг флоу (клиент) | подключить кнопку к `POST /reopen-bbox-validation` + открыть возвращённый `cvat_url` | ✅ Волна 2: `_open_cvat` трёхветочный → `reopen_bbox_validation`+открыть `cvat_url`; общий rollback для cvat убран (`feat/observability`) |
| 5 | `reopen-bbox-validation` НЕ останавливал бегущую авто-стадию: `start_stage` коммитил RUNNING-строку только `flush`, reopen (др. сессия) её не видел → `revoked_tasks=0`, чейн добегал и корраптил статус (тест-сессия §4-B: reopen посреди сегментации → статус уезжал в `skeletonized`) | баг (Волна 1, DoD «жёсткий стоп») | `start_stage`: `flush`→`commit` (RUNNING видна сразу, бонус — видна в `/stages` вживую) + `tests/observability/test_start_stage_commit.py` | ✅ исправлено в сессии 2026-07-08 (`feat/observability`, Вариант 1); предохранитель статуса в телах задач (Вариант 2) — отложен |
| 6 | Показалось, что `GET /{uid}/status` не отдаёт поле `status` | ложная тревога (артефакт копипаста) | повтор `/status` вернул `status` (`error`) корректно; код/enum/сериализация в порядке — механизма дропа нет | ✅ закрыто, не баг |
| 7 | detection пишет «SAHI загружено» и молчит — под-под-шаги (tiling/inference/fusion) не логируются, стадия выглядит зависшей (всплыло в Волне 2 при обсуждении под-шага) | долг обсёрвабилити (worker) | обернуть под-под-шаги detection в `obs.step()` — тогда в логах видно движение | ⬜ Волна 3 (detection); пользователь запросил в Волне 2 — делать точечно сейчас или в Волне 3 |
| 8 | Сбой `create-task` (CVAT отверг большой файл — Pillow) виден плохо: причина (`body`) была только в `extra` → не в `docker logs`; эндпоинт синхронный, без `obs.bind` и без строки `ProcessingStage` → новое окно ошибки Волны 2 его не ловит | долг обсёрвабилити (CVAT) | вынести `op/http_status/body` в текст лога и в исключение; `obs.bind(uid)` в `create-task`; (остаток) сделать `create-task` трекаемой стадией | 🔶 частично 2026-07-08 (`feat/observability`): `_cvat_op` → `op/http_status/body` в тексте лога **и** в исключении (видно в `docker logs` и в клиентском окне); `create-task` получил `obs.bind(uid)`. → **решено: Вариант A** (одна строка `cvat_validation` + `fail_stage`, под-шаги через `failed_step`), закрываем в рамках Волны 1 (§5, §8.4). ⬜ остаток: реализовать `ProcessingStage`+`fail_stage` для `create-task`/`fetch` (болевой под-шаг `upload_media`) |
| 9 | Верхний determinate-бар — один на окно, не мульти-диаграммный: при N бегущих `_on_stages_updated` перерисовывал его по каждой → мельтешение, не подписано какая диаграмма | UX-дыра (клиент, Волна 2) | убрать верхний бар; прогресс — в активную кнопку-стадию (per-diagram + per-stage заливка), кнопка зеленеет на завершении | ✅ сделано 2026-07-08 (`feat/observability`): заливка `N%` в кнопке бегущего этапа + верхний бар убран; проверено визуально на фикстурах |

Правило: пункт отсюда либо становится своей мини-волной, либо явно закрывается как «не делаем». Молча не растворяется.

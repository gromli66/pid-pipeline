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
- **⚠️ Git-команды (add/commit/checkout/branch) выполняет ТОЛЬКО пользователь в своём терминале.** Запись в `.git/` через смонтированную папку падает (index.lock, «Operation not permitted») и портит индекс. Чат правит **файлы кода** (Read/Write/Edit — это работает), а весь git — пользователь. Восстановление после сбоя индекса: `del .git\index.lock` + `del .git\index` + `git reset`. **CRLF-фантом (2026-07-09):** `git` из песочницы/смонтированной папки врёт про `modified` (напр. 23 «изменённых» против 10 у Windows-`git`) и даёт фантомный дифф (`obs.py −18`) — из-за `.gitattributes eol=lf` при незаданном `core.autocrlf`; это окончания строк, не реальные правки/усечение. **Источник правды — Windows-`git status`/`diff`**; сверять и коммитить только из него.

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
- Ретеншн: ошибки 30 дней («бандл» последних строк включаем). «Успешные прогоны не храним» = не храним **verbose-payload** успеха (`error_traceback`, крупный `metrics_json`, лог-бандлы); **тонкий тайминг** успешных стадий (`stage_type`+`duration_seconds`+`status`+таймстампы) держим в том же 30-дн окне — оно же окно бюджетов прогресса (§5 «бюджеты из БД», решено 2026-07-08). Конфликт «не храним успех» vs «усредняем длительности» — мнимый (разные объёмы).
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

### Волна — бюджеты прогресса из БД (CPU-ETA)  (решено 2026-07-08 по §0.2; разворот §8.3)
**Проблема:** прогресс/ETA клиента (`ui/services/progress_model.py`) на статических бюджетах `_DEFAULT_BUDGETS` (detection 60c, segmentation 90c…), засеянных на GPU/dev-масштабе. Бой — CPU, тяжёлый inference в 10–50× медленнее → заливка бегущей фазы упирается в кап `0.95` и замирает («выглядит зависшей», §52); самокалибровка закламплена `0.5–2.0` — разрыв ×10 не тянет.
**Решение:** лёгкий `app/api/stats.py` → `/api/stats/stage-durations`: один агрегат `percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_seconds) FILTER (WHERE status='completed') GROUP BY stage_type` (поле `duration_seconds` в `processing_stages` уже есть — миграции нет). Клиент дёргает ПРИ ЗАГРУЗКЕ (кэш на сессию / 5–10 мин) и кладёт p50 в `progress_model(budgets=…)` (параметр уже поддержан). Само чинит GPU/CPU: длительности — с того железа, где реально крутится бой. Частый `/stages`-поллинг не трогаем.
**Параметры (решено):**
- окно **30 дней** = окно ретеншна ошибок (свежесть: текущий CPU-сервер + текущий код, без GPU/до-опт перекоса); пересмотреть на 14 д, если тайминги часто плывут после перф-правок.
- фоллбэк `< N=5` наблюдений в окне → статический сид; **`_DEFAULT_BUDGETS` пере-засеять в CPU-масштаб** (из первого реального p50 боя) — иначе холодный старт / новая стадия воспроизводят §52.
- исключить ручные/await-стадии (`cvat_validation`, `mask_validation`, `graph_validation` — человеческое время); только `completed`. `upload` — busy-бегунок, бюджет не используется (в агрегате безвреден).
- p50 на лету (мс по индексу `stage_type`); материализация/t-digest — не нужны.
**Ограничение:** p50 по `stage_type` без учёта размера картинки → мажет на выбросах (detection/segmentation/ocr масштабируются с разрешением). Приемлемо (p50 — центр, кратно лучше GPU-статики); бакетить по мегапикселям — позже, если больно.
**Тесты:** p50-запрос на фикстурных строках стадий (исключения / только `completed` / окно); клиент — визуально бар на CPU-длительностях.
**[нужен ты]:** снять первый CPU-p50 с боя для сидов; визуалка бара.
**Наблюдение (2026-07-08, GPU-стенд, из `dur=` Волны 4):** статические `_DEFAULT_BUDGETS` (`progress_model.py`: detection 60с, segmentation 90с, skeleton 20с, junction 30с) ≈ **×4–6 от реальных** длительностей стенда (detection/segmentation ~14с, skeleton ~3.3с, junction ~5.8с) → клиентский ETA/бюджет мажет ~×5. Подтверждает разворот: тянуть p50 из БД, не хардкодить. NB: это **GPU**-числа стенда — НЕ CPU-сид для боя (его снимать на бою, там реал наоборот *медленнее* бюджета).

**Статус:** ✅ код + тесты, эндпоинт на стенде (GPU-p50) — закрытие §8.8 (2026-07-09).

### Волна финальная — недельная сводка
Тот же `/api/stats/stage-durations` расширяем (или scheduled-задача): p50/p95/max по стадиям, `queue_wait`, пик одновременности, близость к `soft_time_limit`, failure-rate. Один эндпоинт — две цели (бюджеты + сводка). По цифрам возвращаемся к параллелизму.

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
| 1 CVAT | ✅ готово; DoD-дыра закрыта — `create-task`/`fetch` пишут `cvat_validation` в `/stages` (async-хелпер, Вариант A); болевой `upload_media` ловится опросом task-status (большой файл = асинхронный отказ CVAT). Клиент — Вариант A. Смоук на стенде зелёный (§8.5) | feat/observability | 2026-07-08 |
| 2 Клиент | ✅ проверено визуально: окно ошибки (FAILED), прогресс-заливка в кнопке (RUNNING; верхний бар убран, §9 #9), reopen-диалог «Проверка элементов». Отложено осознанно: `stats`-эндпоинт, `current_step`. Добивка 2026-07-09: per-stage заливка graph/ocr + cp1251-лог (§8.11, §9 #15/#17) | feat/observability | 2026-07-09 |
| 3 detection | ✅ smoke (§8.6, uid 6e7144d5) | feat/observability | 2026-07-08 |
| 3 segmentation | ✅ smoke (uid 6e7144d5); baseline+tiling/inference/stitch видны; +фикс протечки контекста (§9 #11) | feat/observability | 2026-07-08 |
| 3 skeleton/graph | ✅ код+тесты (9 новых: skeleton 5 / graph 4; `pytest tests/observability`); [нужен ты] smoke на стенде + `docker restart pid_worker` | feat/observability | 2026-07-09 |
| 3 ocr/junction/contours/fxml | ⬜ | | |
| 3 upload/frame | ⬜ | | |
| Бюджеты из БД (CPU-ETA) | ✅ код+тесты (13 новых; `pytest tests/observability`=75); эндпоинт на стенде (GPU-p50); [нужен ты] клиент-ребилд+визуалка, деплой `app/` на бой (§8.8) | feat/observability | 2026-07-09 |
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
- **ETA без сервера — РАЗВЁРНУТО 2026-07-08 (§0.2):** `app/api/stats.py` (`/api/stats/stage-durations`) **делаем** — отдельной мини-волной (§5 «бюджеты из БД»). Причина разворота: статические бюджеты засеяны на GPU-масштабе → на CPU-бою заливка бегущей фазы улетает в кап и замирает (§52), калибровка ×2 разрыв ×10 не тянет. Разовый агрегат при загрузке клиента — это не «нагрузка на сервер» (то возражение было про частый поллинг `/stages`, его не трогаем). Клиентская калибровка и `budgets=`-переопределение остаются; источник бюджетов — p50 из БД по CPU-серверу, не статика/GPU.
- **Под-шаг в UI отложен:** поля `current_step` на стадии нет → прогресс по-этапный, не под-шаговый. Реальная потребность пользователя — **под-шаг в ЛОГАХ** стадий (detection «SAHI загружено» и молчит) → §9 #7 (worker, Волна 3).
- `progress_model.py` — БЕЗ `from __future__ import annotations` (иначе `@dataclass` при загрузке по пути падает на резолве строковых аннотаций через `sys.modules`).
- Тесты Волны 2 — в `tests/observability/` (путь `tests/observability/test_progress_model.py`, не `tests/` как в §5), как Волны 0–1.

**Осталось по Волне 2 [нужен ты]:** пересобрать клиент; визуально: determinate-бар на RUNNING, окно ошибки на FAILED (1–2 диаграммы), кнопка «Проверка элементов» с позднего этапа (жёсткий стоп + разметка CVAT цела). Опц.: `app/api/stats.py` / `current_step` — если решишь добить позже.

**Правка направления (2026-07-08, в закрытии Волны 2):** верхний глобальный determinate-бар **убираем** — он один на окно и при нескольких бегущих диаграммах мельтешил (§9 #9). Прогресс переносим В активную кнопку-стадию: заливка (по времени — `elapsed/бюджет` + калибровка) + `N%` прямо в кнопке бегущего этапа; на завершении кнопка зеленеет. Per-diagram по построению (живёт в workspace = одна диаграмма) и per-stage. Ручные стадии (без бюджета) — прежний «крутящийся» бид. Честная заливка по факту работы — когда воркер начнёт репортить под-шаги (`current_step`, Волна 3); пока — оценка по времени. Upload-«бегунок» (busy) остаётся.

### 8.4 Порядок дальше (решено 2026-07-08)

Сначала закрываем начатое, потом расширяем (нашли недоделанное — возвращаемся сразу, не копим).

1. **Волна 2 — закрыть визуалкой** [нужен ты]: пересобрать клиент, проверить determinate-бар (RUNNING), окно ошибки (FAILED), кнопку «Проверка элементов» с позднего этапа. Код + 41 тест зелёные; `stats`-эндпоинт и `current_step` осознанно не делаем.
2. **Волна 1 — закрыть DoD-дыру:** ✅ сделано (§8.5) — `create-task`/`fetch` пишут строку `cvat_validation` + `fail_stage` (Вариант A, async-хелпер); двухфазный `create_task` (`task_shell`/`upload_media`); болевой `upload_media` — опросом task-status после `/data` (Вариант b). `step("persist_validated")` — не делали (осознанно).
3. **После — расширение:** CVAT-углубление (тонкая карта отказов: login/project · task-shell · media-upload · wait-job · open/reopen · export[request→ready→download→parse] · import · state; отдельные `error_code` — по боли) + авто-стадии Волн 3+. Инкрементально: по боли + проактивно медленное-на-CPU. Идея отдельной «Волны 1.5» распущена — нужная часть ушла в п.2.

### 8.5 Волна 1 — закрытие DoD-дыры (2026-07-08)

Интерактивные CVAT-эндпоинты теперь пишут строку `cvat_validation` → CVAT-сбои видны в `/stages` и в окне ошибки клиента.

**Сделано (`feat/observability`):**
- `app/api/cvat.py` — async-хелперы `_start_cvat_stage` (RUNNING-строка `cvat_validation`, `commit` сразу — видна в `/stages` во время долгой операции, как worker `start_stage`) и `_fail_cvat_stage` (`error_code`/`failed_step`/traceback, без commit — коммитит вызывающий). Проводка: `create-task` (start→`complete`/`fail` c `default_step=create_task`), `fetch` (start→`complete`/`fail` c `default_step=confirm`). Воркерные sync `start_stage`/`fail_stage` НЕ переиспользуются.
- `app/services/cvat_client.py` — `_cvat_op` принимает `step=` (доезжает до `failed_step`); `create_task` разбит на две фазы (`task_shell`=`POST /tasks`, `upload_media`=`POST /data`); `_wait_for_data` после `/data` опрашивает `GET /api/tasks/{id}/status` и при `state=Failed` поднимает `CVATRequestError step=upload_media` с причиной CVAT (последняя строка traceback).
- `tests/observability/test_cvat_stage_rows.py` — `_cvat_op(step)`, двухфазность `create_task`, `_fail_cvat_stage`, `_wait_for_data` (8 тестов).

**Находка (§0.2, изменила план):** большой файл CVAT отвергает **асинхронно** — `POST /data`→202, Pillow-бомба падает в фоновом rq-воркере, видно в task-status как `state=Failed`. Исходное допущение «синхронный отказ `/data`» не годилось (сбой всплывал слепым таймаутом `_wait_for_job` → `failed_step=create_task`/`cvat_timeout`). Поэтому добавлен опрос статуса (Вариант b). Смоук на стенде: `failed_step=upload_media`, msg `PIL.Image.DecompressionBombError: Image size (200000000 pixels) exceeds limit of 178956970 pixels…`.

**Решения (наследуются волнами):**
- **Клиент — Вариант A:** `fetch`-сбой уже зажигает богатое окно ошибки (статус→ERROR → поллинг → `_apply_error_status` → строка `cvat_validation` по `_STAGE_TYPE_TO_KEY["cvat_validation"]="cvat"`). `create-task`-сбой показывается inline-сообщением «Не удалось открыть CVAT: <причина>» + строкой в `/stages`; богатое окно для него НЕ делаем (синхронное действие; боль §9 #8 = невидимая причина — закрыта).
- `create-task` НЕ ставит `diagram.status=ERROR` (пишет только строку стадии; диаграмма остаётся в своём статусе, retry не ломается). `fetch` свой ERROR-откат сохранил.
- Наблюдаемость only — сам отказ большого файла НЕ чиним (DoD «поведение не изменилось»). Фикс (даунскейл/лимит Pillow/тайлинг) — при необходимости отдельным пунктом §9.
- **Гоча деплоя:** `uvicorn` в `pid_api` без `--reload` → правки кода подхватываются только `docker restart pid_api` (воркерный код — `docker restart pid_worker`). Код бинд-маунтится (`./app:/app/app`), пересборка образа не нужна.

---

### 8.6 Волна 3 — detection (2026-07-08)

Под-под-шаги COMPUTE детекции теперь видны в логах (закрывает §9 #7). Инструментированы и задача, и модуль ансамбля — по §0.2 (пользователь: «как лучше» → Вариант A + минимальный объём `print`).

**Сделано и проверено на стенде (`feat/observability`, smoke 2026-07-08 ✅):**
- `worker/tasks/detection.py` — `obs.bind(uid/phase/task_id/attempt)` + баннер (входы/размеры/модель/tiles/device); канон под-шагов через `obs.step()`: `load_inputs`/`load_model`/`compute`/`postprocess`/`persist_artifacts`; ~23 `print`→`logger`; `raise` типизированы (`ConfigError`/`ArtifactMissingError`/`ArtifactWriteError`; диаграмма не найдена → `PipelineError`). Внешний `except` — `logger.error(exc_info=True)` + `fail_stage(exc=exc)` → `error_code`/`failed_step` доезжают до `/stages`. `except CVATError` (Волна 1) не тронут.
- `modules/yolo_detector/ensemble.py` (**Вариант A**) — под-под-шаги внутри `detect()`: `step("inference", model=tileN)` на каждую модель ансамбля + `step("fusion")` вокруг WBF/NMS. Сбой инференса → `InferenceError`, неизвестная стратегия слияния → `ConfigError` (оба со `step`). `tiling`+`inference` не разделяются — SAHI тайлит внутри одного `get_sliced_prediction`. `print` детектора не мигрировали — на stdout→logging мост Волны 0.
- `app/core/errors.py` — лист `ConfigError` (`config_invalid`) для конфига проекта/модели (листья по волнам, §5-Волна0).
- `tests/observability/test_detection_errors.py` — fault-тесты: лист `ConfigError` + типизация под-под-шагов ансамбля (`inference`/`fusion`/unknown-strategy, границы `step` в логах). cv2 замокан (§9 #2), numpy реальный; sahi/ensemble_boxes не нужны.

**Решения (наследуются волнами 3+):** COMPUTE-под-под-шаги остальных авто-стадий (segmentation/skeleton/graph/ocr) так же живут в модулях → инструментируем **модуль** (Вариант A), не только задачу; связность `modules→app.core.obs` — норма (модули исполняются в worker'е, app на PYTHONPATH). `print` в затронутых модулях — на мост, точечно мигрируем по боли (две оси §1). Параллелизм не тронут.

**Smoke (стенд, GPU, 2026-07-08, uid `6e7144d5`):** диаграмма 4964×3509 → в логах баннер + под-шаги `load_inputs/load_model/compute/postprocess/persist_artifacts`; внутри `compute` — 3× `step=inference` (≈5/2/1 c по моделям) + `step=fusion`; `detection: completed count=97`, пайплайн ушёл дальше (поведение сохранено). Длительность под-шага (`duration_ms`) пишется в structured-extra `step.end`, но в текстовый `LOG_FORMAT` не выведена — видна по таймстампам start/end; инлайн в текст отложен до JSON-стока/правки формата Волны 0 (§9 #10).

**Гоча деплоя (напоминание):** `uvicorn`/worker без `--reload` → после правок `docker restart pid_worker` (и `pid_api` при правках `app/`). Код бинд-маунтится, пересборка не нужна.

---

### 8.7 Волна 4 — segmentation (2026-07-08, ✅ smoke passed)

Под-под-шаги COMPUTE сегментации видны в логах; raise'ы типизированы; немые `except: pass` закрыты. По образцу Волны 3 (Вариант A) — инструментированы и задача, и движок tiled-инференса.

**Сделано (`feat/observability`):**
- `worker/tasks/segmentation.py` — `obs.bind(uid/phase=segmenting/task_id/attempt)` + баннер (image/wh/model/tile/overlap/device); канон под-шагов `obs.step()`: `load_inputs`/`load_model`/`compute`/`persist_artifacts`. Raise'ы типизированы: project config → `ConfigError`, диаграмма не найдена → `PipelineError`, нет/битый образ → `ArtifactMissingError`, нет чекпоинта → `ModelLoadError`. Внешний `except` → `logger.error(exc_info=True)` + `fail_stage(exc=exc)` (`error_code`/`failed_step` доезжают до `/stages`); `SoftTimeLimitExceeded` → +traceback. `get_logger` вместо `logging.getLogger`.
- Немые `except: pass` (node_mask helpers `segmentation.py:91/131/135`) → `logger.warning(exc_info=True)` + прежний bbox/zeros-fallback; overlay-viz `except` → `exc_info=True` (best-effort, широкий тип осознанно).
- `modules/pipe_segmentation/inference/engine.py` (**Вариант A**) — под-под-шаги внутри `TiledInference.predict()`: `step("tiling")` (бинаризация+позиции) / `step("inference")` (батч-цикл) / `step("stitch")` (нормализация+бинаризация+постобработка). Сбой инференса → `InferenceError`, OOM → `GpuOutOfMemoryError` (оба `step=inference`).
- `tests/observability/test_segmentation_errors.py` — fault-тесты движка (типизация под-под-шагов, границы `tiling/inference/stitch` в логах, passthrough `PipelineError`, коды листьев). Изоляция cv2/torch/tqdm + pipe-листья (§9 #2).
- `app/core/errors.py` — без изменений (переиспользуем листья Волн 0/3).

**Решения (§0.2 — наследуются волнами):**
- **TTA свёрнута в `inference`:** канон §1 называет tiling/inference/tta/stitch, но TTA идёт покадрово внутри `_run_inference` (в батч-цикле) — чистой последовательной границы нет; отдельный `step` на батч дал бы десятки строк start/end (против §1 «чистые in-memory не оборачиваем»). Логируем `tiling/inference/stitch`; TTA — часть `inference`. Зеркалит detection («tiling+inference не разделяются — SAHI внутри одного вызова», §8.6).
- **`engine.py` — optional-import obs:** движок импортится и standalone-CLI (`python -m pipe_segmentation infer/test`, где `app` не на PYTHONPATH). `from app.core…` обёрнут в `try/except` → no-op `step` + локальные заглушки ошибок; в worker'е (app на PYTHONPATH) работает реальный `obs`. Отличие от `ensemble.py` (top-level import): `yolo_detector` не standalone-пакет, `pipe_segmentation` — да (свой `__main__`/`cli.py`).
- **281 `print` в `pipe_segmentation` — НЕ мигрируем:** все в тренировке/CLI (`cli.py` 121, `architecture.py` 36, `trainer.py` 28, `coco_to_masks` 26, `tiling` 17 в `ImageTiler`…); рантайм-путь инференса (`engine/tta/postprocessing/preprocessing`) и task-файл — **0 `print`**. На stdout→logging мост Волны 0 (как `print` детектора, §8.6). DoD «print→0» на рантайме выполнен.

**Smoke (стенд, GPU, 2026-07-08, uid `6e7144d5`, 4964×3509):** баннер + `step=load_inputs/load_model/compute/persist_artifacts`, внутри `compute` — `step=tiling/inference/stitch` (inference ≈7 c по таймстампам); `Segmentation done: 24 tiles, 7.8s, coverage 1.1%`; пайплайн ушёл дальше (skeleton→junction), поведение сохранено. Побочно всплыла и закрыта протечка контекста (§9 #11): `obs.bind` тёк в неинструментированные стадии → сброс в `task_prerun` (`obs.reset()`); skeleton/junction теперь честный `-`, segmentation/detection — без изменений.

**Гоча деплоя (напоминание):** воркерный код бинд-маунтится → после правок `docker restart pid_worker` (пересборка не нужна).

---

## 9. Parking lot (найденное вне объёма — по §0.2)

Сюда чат заносит всё, что всплыло, но не входит в текущую волну. Ты решаешь: взять отдельной волной, сделать сейчас, или отклонить.

| # | Что нашли (файл/место) | Тип (баг/долг/пробел дизайна) | Предложение | Решение |
|---|---|---|---|---|
| 1 | Celery хайджекает root-логгер → логи задач не в нашем формате (смоук Волны 0) | баг инфры | сигнал `setup_logging` вместо голого `worker_process_init` | ✅ решено в Волне 0 (`celery_app.py`) |
| 2 | `pytest tests/` рушится на сборке (`cv2`/`torch`/`celery` нет в anaconda) + устаревшие тесты (`test_refactoring*`, `test_stage7_graph_flow`, cp1251 в `.read_text`) | тех-долг тест-инфры | изолировали тесты волн в `tests/observability/`; полная чистка — на `pr0/fix-test-infra` (importorskip + xfail) | ⬜ отложено (pr0) |
| 3 | Индекс git на смонтированной папке ловит `index.lock`/«удаления» (сценарий §0.1) | инфра-риск | восстановление: `del .git\index.lock` + `del .git\index` + `git reset` | ✅ подтверждено, задокументировано (§0.1). Рецидив 2026-07-09 на пуше §8.11 — снят удалением ТОЛЬКО `.git\index.lock` (индекс цел; `del .git\index`+`reset` не понадобились). Тогда же всплыл **CRLF-фантом**: `git` из песочницы даёт лишние `modified`/дифф из-за `eol=lf`+нет `autocrlf` (`obs.py −18` оказался ложным) → источник правды Windows-`git` |
| 4 | Кнопка «Проверка элементов» в клиенте не зовёт бэкенд-откат — переоткрывает CVAT визуально (источник ошибки `status is skeletonizing`) | баг флоу (клиент) | подключить кнопку к `POST /reopen-bbox-validation` + открыть возвращённый `cvat_url` | ✅ Волна 2: `_open_cvat` трёхветочный → `reopen_bbox_validation`+открыть `cvat_url`; общий rollback для cvat убран (`feat/observability`) |
| 5 | `reopen-bbox-validation` НЕ останавливал бегущую авто-стадию: `start_stage` коммитил RUNNING-строку только `flush`, reopen (др. сессия) её не видел → `revoked_tasks=0`, чейн добегал и корраптил статус (тест-сессия §4-B: reopen посреди сегментации → статус уезжал в `skeletonized`) | баг (Волна 1, DoD «жёсткий стоп») | `start_stage`: `flush`→`commit` (RUNNING видна сразу, бонус — видна в `/stages` вживую) + `tests/observability/test_start_stage_commit.py` | ✅ исправлено в сессии 2026-07-08 (`feat/observability`, Вариант 1); предохранитель статуса в телах задач (Вариант 2) — отложен |
| 6 | Показалось, что `GET /{uid}/status` не отдаёт поле `status` | ложная тревога (артефакт копипаста) | повтор `/status` вернул `status` (`error`) корректно; код/enum/сериализация в порядке — механизма дропа нет | ✅ закрыто, не баг |
| 7 | detection пишет «SAHI загружено» и молчит — под-под-шаги (tiling/inference/fusion) не логируются, стадия выглядит зависшей (всплыло в Волне 2 при обсуждении под-шага) | долг обсёрвабилити (worker) | обернуть под-под-шаги detection в `obs.step()` — тогда в логах видно движение | ✅ Волна 3 (§8.6, smoke 2026-07-08): в логах баннер + `step=load_inputs/load_model/compute/postprocess/persist_artifacts`, внутри `compute` — 3× `step=inference` (по модели) + `step=fusion`. Вариант A; связность `modules→app.core.obs` принята |
| 8 | Сбой `create-task` (CVAT отверг большой файл — Pillow) виден плохо: причина (`body`) была только в `extra` → не в `docker logs`; эндпоинт синхронный, без `obs.bind` и без строки `ProcessingStage` → новое окно ошибки Волны 2 его не ловит | долг обсёрвабилити (CVAT) | вынести `op/http_status/body` в текст лога и в исключение; `obs.bind(uid)` в `create-task`; (остаток) сделать `create-task` трекаемой стадией | ✅ закрыто 2026-07-08 (`feat/observability`, §8.5): `create-task`/`fetch` пишут строку `cvat_validation` (async-хелпер, Вариант A), под-шаги через `failed_step`. **Находка:** большой файл CVAT отвергает АСИНХРОННО (`POST /data`→202, Pillow падает в фоне) → всплывал слепым таймаутом `_wait_for_job`. Реализован Вариант b: после `/data` опрос `GET /tasks/{id}/status`, при `state=Failed` → `CVATRequestError step=upload_media` с причиной CVAT. Смоук: `failed_step=upload_media`, `DecompressionBombError` |
| 9 | Верхний determinate-бар — один на окно, не мульти-диаграммный: при N бегущих `_on_stages_updated` перерисовывал его по каждой → мельтешение, не подписано какая диаграмма | UX-дыра (клиент, Волна 2) | убрать верхний бар; прогресс — в активную кнопку-стадию (per-diagram + per-stage заливка), кнопка зеленеет на завершении | ✅ сделано 2026-07-08 (`feat/observability`): заливка `N%` в кнопке бегущего этапа + верхний бар убран; проверено визуально на фикстурах |
| 10 | `duration_ms` под-шага пишется в structured-extra `step.end`, но не в текстовом `LOG_FORMAT` — в `docker logs` длительность видна только по таймстампам start/end (всплыло на smoke Волны 3) | долг обсёрвабилити (формат логов) | опц. `dur=%(duration_ms)s` в `LOG_FORMAT` (Волна 0, общая схема) или ms в текст сообщения `step.end`; иначе ждать JSON-стока | ✅ решено 2026-07-08 (`feat/observability`, Волна 4): `dur=%(duration_ms)s` в `LOG_FORMAT` + дефолт `-` в `ContextFilter` (записи без под-шага). NB: это про длительность в ТЕКСТЕ лога; ETA-бюджеты клиента — отдельный путь (`duration_seconds` из БД, §5 «бюджеты из БД»), не логи |
| 11 | `obs.bind` протекал между задачами: `contextvars` в prefork-воркере не обнулялся → неинструментированная стадия (skeleton/junction/direction) логировалась с `uid/phase/task` предыдущей задачи (смоук Волны 4: skeleton = `phase=detecting` + task детекции; под нагрузкой/мультидиаграммно мог бы взять ЧУЖОЙ uid) | баг обсёрвабилити (Волна 0) | сброс контекста в сигнале `task_prerun` (`obs.reset()`) — каждая задача стартует с чистого; инструментированные биндят поверх | ✅ решено 2026-07-08 (`feat/observability`): `obs.reset()` + `@task_prerun.connect` в `celery_app.py`. Неинструментир. стадии теперь честный `-` до своих волн (поведение инструментированных не изменилось) |
| 12 | `torch.cuda.amp.autocast()` deprecated (FutureWarning, `engine.py:224`, смоук Волны 4) | долг (пред-существующий, не-obs) | → `torch.amp.autocast('cuda')`, поведение то же | ✅ фикс 2026-07-08 (`feat/observability`, точечно — согласовано) |
| 13 | `[POSTPROCESS]`-логи модуля `postprocessing` идут на `WARNING`, хотя информационные (смоук Волны 4: `[POSTPROCESS] TOTAL: 0.49s` как warning — шум) | долг обсёрвабилити (уровень логов) | снизить до `INFO`/`DEBUG` в волне skeleton/graph (там же трогаем модули сегментации) | ✅ закрыто 2026-07-09 (`feat/observability`, §8.12): 9 `[POSTPROCESS]` тайминг-логов `warning`→`info`; заодно 5 `[SKELETON_CONNECT]` в том же `postprocessing.py` (#18, по решению пользователя). +regress-тест `test_postprocess_log_level.py`. INFO, не DEBUG — тайминги под-шагов видны в штатных логах (DoD §4) |
| 14 | `skeleton_extension` — под-под-шаги COMPUTE (Вариант A) не инструментированы (§8.9): skeletonization ≈5.7с/GPU (не кандидат на дробление), модуль печатает `[SKEL_EXT] stage_N` на stdout→logging мост, `processing.py:396` уже делает `traceback.print_exc()` | долг обсёрвабилити (worker) | взять, если на CPU-бою skeleton «выглядит зависшей» несмотря на `[SKEL_EXT]`-тайминги | ⬜ отложено осознанно (§8.9) |
| 15 | Клиентская заливка `%` не работала у `graph_building` и `ocr` (отдельная мини-волна Волны 2) | баг UX (клиент) | диагностика логами → точечный фикс | ✅ закрыто 2026-07-09 (`feat/observability`, §8.11). Логи ОПРОВЕРГЛИ гипотезу «опрос на паузе»: в `building_graph` опрос жив, но OCR бежит ПАРАЛЛЕЛЬНО и `compute_progress` брал ПОСЛЕДНЮЮ бегущую (`ocr`) → заливка уходила на кнопку OCR, `graph` не заливался; после `built` (гейт→`unwatch`) подача `stages_updated` вставала → OCR замерзал. Фикс: `ProgressState.stage_percents` (% на КАЖДУЮ бегущую авто-стадию), foreground=самая ранняя; `_on_stages_updated` льёт каждую кнопку своим %, снятие — точечный `_restyle_button` (не `_update_buttons`, регрессия §8.10); OCR тикает после `built` через `_ocr_poll_timer`. +2 теста |
| 16 | Перф graph: `builder.save()` безусловно рендерил matplotlib-оверлей (~54с из 72.8с), задача `unlink`'ала его при `save_visualizations=False` | долг перф (вне obs) | рендер оверлея только под флагом | ✅ сделано 2026-07-09 (§8.10): `graph_building` при выкл. флаге ~72.8с→~18с, вывод идентичен |
| 17 | Windows-клиент: stdout=cp1251 роняет StreamHandler на `→`/emoji в логах (`UnicodeEncodeError`) → строка ТЕРЯЕТСЯ (напр. `logger.info("Refresh: %s → %s")`, `"Status %s: %s → %s"`, `✅/🔍/📊`-сообщения); всплыло на диагностике §8.11 | долг обсёрвабилити (Windows) | `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")` в `ui/main.py` до `basicConfig` | ✅ закрыто 2026-07-09 (`feat/observability`, §8.11): reconfigure utf-8+replace; кириллица и раньше проходила (в cp1251), падали только не-cp1251 символы |
| 18 | `[SKELETON_CONNECT]` тайминг-логи (skeletonize/find_endpoints/trace/pair_matching/TOTAL) в том же `postprocessing.py` — тоже на `WARNING`, тот же шум (найдено при #13, §0.2) | долг обсёрвабилити (уровень логов) | понизить до `INFO` вместе с #13 (один файл, один дефект, один путь вызова `post_process_mask`) | ✅ закрыто 2026-07-09 (`feat/observability`, §8.12): 5 логов `warning`→`info`, включено в #13 по решению пользователя |

Правило: пункт отсюда либо становится своей мини-волной, либо явно закрывается как «не делаем». Молча не растворяется.


---

### 8.8 Волна — бюджеты прогресса из БД (CPU-ETA) — закрытие (2026-07-09)

Клиентский прогресс/ETA берёт бюджеты стадий из БД того сервера, где крутится `pid_api` (p50 реальных длительностей), а не из статических GPU-сидов `_DEFAULT_BUDGETS` (§52). Аддитивно, поведение пайплайна не изменено, миграции нет (`duration_seconds` уже был).

**Сделано (`feat/observability`):**
- `app/api/stats.py` *(new)* — `GET /api/stats/stage-durations`. Тонкий `SELECT` (completed / окно 30д / `duration_seconds` not null — дешёвый скоуп-префильтр) → чистый `compute_stage_budgets(rows)` делает исключения / окно / медиану / фоллбэк `<N=5`. Отдаёт `{"budgets": {stage_type: p50_сек}, "window_days": 30, "min_observations": 5}`.
- `app/main.py` — регистрация роутера (`prefix="/api/stats"`).
- `ui/services/api_client.py` — `get_stage_durations()` с TTL-кэшем (`_STAGE_DURATIONS_TTL_SEC=600`, на инстанс = на сессию); ошибка/недоступность → `{}` без исключения, кэш не отравляется (ретрай на след. поле).
- `ui/widgets/diagram_workspace.py` — точка `_on_stages_updated`: `compute_progress(stages, budgets=self.api_client.get_stage_durations())`.
- `tests/observability/test_stage_durations.py` *(new)* — 13 тестов `compute_stage_budgets` на фикстурных строках.
- НЕ тронуты: `progress_model.py` (параметр `budgets=` уже был; мерж `_DEFAULT_BUDGETS`←p50), `status_provider.py`, частый `/stages`-поллинг, параллелизм.

**Решения (§0.2 — были развилки, спросил у пользователя):**
- **p50 = `SELECT`+медиана в чистом хелпере, НЕ `percentile_cont` в SQL.** Тест-харнес — SQLite + async-движок замокан (`tests/conftest.py`): `percentile_cont … WITHIN GROUP … FILTER` там не исполняется и async-эндпоинт не тестируется вовсе → буквальный SQL §8.3 нельзя покрыть тестом «p50 на фикстурах» §5. `statistics.median` для чётного N интерполирует середину = ровно `percentile_cont(0.5)`; поведение идентично, перф не важен (1 агрегат на сессию, тонкие строки за 30д). Бизнес-отбор — в хелпере (единый источник правды, покрыт тестами), `SELECT WHERE` — только скоуп-префильтр.
- **Исключения — 4 стадии, а не 3 из §5.** `+frame_removal` к `cvat_validation`/`mask_validation`/`graph_validation`: все четыре — `budget=None` (ручные) в `progress_model`. Иначе `merged.update(budgets)` перекрыл бы `None`→число и показал ETA/заливку на ручном шаге. §5 просто недосчитал `frame_removal`.
- **Фоллбэк — на клиенте, сервер сида не знает.** Сервер отдаёт только стадии с `≥5` наблюдений; отсутствующие клиент добирает из своего `_DEFAULT_BUDGETS` штатным мержем. Server-side сид не нужен.
- **Вайринг — TTL-кэш на `APIClient`, НЕ eager в `main_window`.** Меньше правок (точка вызова `compute_progress` уехала в `diagram_workspace`, §9 #9), бюджеты живут рядом с клиентом, что их тянет, и пустой ответ при недоступном API само-восстанавливается на след. поле (eager-once так не умеет).

**Инсайды/находки:**
- **Само-адаптация под сервер.** Эндпоинт читает БД своего `pid_api` → на GPU-стенде даёт GPU-p50, на CPU-бою тот же код даст CPU-p50; клиент берёт бюджеты оттуда, куда указывает `PID_API_URL`. Для «точно на любом сервере» пере-засев НЕ нужен — это уже живой p50. `_DEFAULT_BUDGETS` теперь — только пол для холодного старта / стадий с `<5` наблюдений; на сервере с данными живой p50 всегда перекрывает сид (`merged.update`).
- **`direction_classification` (в §9 parking lot):** эндпоинт её отдаёт (реальная авто-стадия цепочки сегментации, ~0.69c на стенде), но её нет в клиентском `_PIPELINE`/`_STAGE_LABELS`/`_DEFAULT_BUDGETS` → `compute_progress` её игнорит (лишний ключ безвреден). Учитывать в баре — отдельной правкой клиентского пайплайна, если понадобится.

**Инцидент инструментария (важно для будущих правок .md/кода с кириллицей):** Edit/Write несколько раз **усекали хвост файла** после региона правки, обрываясь на многобайтном символе (кириллица/emoji): `ui/services/api_client.py` (−46 строк), `ui/widgets/diagram_workspace.py`, `app/main.py` (затёрт `return` рут-эндпоинта). Дополнительно **этот RUNBOOK в рабочем дереве оказался усечён на §8.5** (292 стр. против 367 в HEAD — терялись хвост §8.5 + весь §8.6 detection). Всё поймано `py_compile`+`git diff`; файлы пересобраны из `git HEAD` с повторным наложением правок байт-точной записью (`cat`/py-heredoc), RUNBOOK восстановлен из HEAD перед дозаписью. Итоговые дифы — только целевые изменения, удалений нет. **Вывод:** правки крупных кириллических файлов — через `git`-safe запись/append, не Edit/Write. Мелочь: в тесте even-median ожидал 25.0, факт 35.0 (арифметика в ассерте) — поймано верификацией, поправлено.

**Верификация:**
- `pytest tests/observability` = **75 passed** (вкл. 13 новых), старые не сломаны.
- Эндпоинт на стенде: `200`, ручные стадии отсутствуют, p50 правдоподобны.
- Смоук клиентского пути (headless, без GUI): `[1]` живой fetch → `[2]` исключения → `[3]` кэш на сессию → `[4]` p50 протекает в ETA (detection: сервер 19.6c→ETA 210c vs статика 60c→345c) → `[5]` НЕГАТИВ: мёртвый API → `{}`, прогресс на сиде (ETA 345c), без падения, кэш чист. `SMOKE OK`. (скрипт был временный в `_scratch/`, удалён.)
- `py_compile` всех правленых файлов; дифы surgical.

**Наблюдение — GPU-p50 стенда (2026-07-09, это НЕ CPU-сид для боя):** detection 19.6 · segmentation 12.8 · skeletonization 5.7 · final_skeletonization 5.2 · junction_classification 9.3 · contour_extraction 24.8 · graph_building 46.3 · ocr 86.5 · fxml_generation 0.07 · direction_classification 0.69 (сек). Ручные исключены.

**[нужен ты]:** пересобрать клиент (`ui/`) + визуалка заливки кнопки-стадии на 1–2 диаграммах; при выкатке на бой — задеплоить `app/` + `docker restart pid_api` (эндпоинт сам даст CPU-p50); опц. пере-засев `_DEFAULT_BUDGETS` под CPU из первого боевого p50 (только холодный старт / стадии с `<5` наблюдений).

---

### 8.9 Волна 3 — skeleton/graph (2026-07-09)

Инструментированы стадии SKELETONIZATION / FINAL_SKELETONIZATION / GRAPH_BUILDING по образцу Волн 3–4 (Вариант A). FXML_GENERATION (тот же `graph.py`) — НЕ трогали: это под-волна «ocr/junction/contours/fxml» (границы модулей чистые: `build_graph`→`builder.py`, `generate_fxml`→`graph_to_fxml.py`).

**Сделано (`feat/observability`):**
- `worker/tasks/skeleton.py` — обе задачи: `obs.bind(uid/phase=skeletonizing[_simple]/task_id/attempt)` + `get_logger`; канон `obs.step()`: `load_inputs`/`compute`/`persist_artifacts` (task1 без отдельного `postprocess` — `skeleton_to_mask` — в persist по факту, §1). Raise'ы типизированы: project config → `ConfigError`, диаграмма → `PipelineError`, входные маски/образ → `ArtifactMissingError`, сбой скелета (falsy-возврат / нет файла / нечитаем) → **`SkeletonizationError`** (`step=compute`). Немые `except OSError: pass` (temp-cleanup) → `logger.debug(exc_info=True)`; `except` bg-mask/refine/dispatch → `+exc_info=True`. Внешний `except`/`SoftTimeLimit` → `exc_info=True` + `fail_stage(..., exc=exc)` (`error_code`/`failed_step` доезжают до `/stages`).
- `worker/tasks/graph.py` — только `task_build_graph`: `obs.bind(phase=building_graph)` + канон `compute`/`persist_artifacts` (+ `step=load_inputs` на raise'ах входов); raise'ы → `PipelineError`/`ArtifactMissingError`. **Немой `except Exception: pass` (`graph.py:246`, флаг ТЗ) → `logger.warning(exc_info=True)`** (флаг `save_visualizations` остаётся `False`). Внешний `except` → `exc=exc`+`exc_info`.
- `modules/graph/core/builder.py` (**Вариант A**, optional-import obs как `engine.py`) — под-под-шаги COMPUTE в `build()`: `load_masks` / `bridge_preprocess` / `prepare_tracing` / **`trace_edges`** (доминанта; graph — самая медленная авто-стадия ≈46с/GPU → на CPU кратно дольше). Broad-`except` дампа сырого графа (`:349`, был `print`) → `logger.warning(exc_info=True)`.
- `app/core/errors.py` — лист `SkeletonizationError` (`skeletonization_failed`), by-need (как `ConfigError` в детекции; граф своего листа не заводит — сбои `builder.build()` типизирует `obs.step`).
- `tests/observability/test_skeleton_errors.py` (5) + `test_graph_errors.py` (4) — листья/иерархия, `fail_stage` довозит `error_code`/`failed_step`, обёртка/passthrough `obs.step`, границы под-под-шагов `builder.build()` (start/end+`duration_ms`). Изоляция §9 #2: cv2/scipy + graph.core-под-модули замоканы; scipy — через `monkeypatch.setitem` (не течёт в сессию).

**Решения (§0.2 — спросил у пользователя, наследуются):**
- **skeleton_extension — глубина «минимум» (решено с пользователем):** модуль (`processing.py`/`core.py` 1605 стр.) НЕ инструментируем под-под-шагами. Причина: skeletonization ≈5.7с/GPU (2-я по скорости авто-стадия) — не кандидат на проактивное дробление (§1); модуль уже печатает `[SKEL_EXT] stage_N` тайминги на stdout→logging мост; риск усечения кириллицы (§8.8). Задачный `step=compute` даёт `failed_step`+`duration`; `processing.py:396` уже делает `traceback.print_exc()` (не немой глотатель) → причина в логах. **Отложено → §9 #14.**
- **fxml отложен:** `generate_fxml`/FXML_GENERATION — в свою под-волну; `except`-блоки `graph.py` (генерация fxml) не трогаем.
- **`print`-модули (graph/core 128, skeleton_extension 185) — на stdout→logging мост** (DoD §4 «или явно через stdout-мост»), как §8.6/§8.7. Задачные файлы — 0 `print`. Wholesale-миграция не делается (surgical, §3).
- **Гранулярность skeleton-задач:** `load_inputs`/`compute`/`persist` (без `load_model` — модели нет; без отдельного `postprocess`).

**[нужен ты]:** `pytest tests/observability -v` (ожидаемо 75 старых + 9 новых); smoke на стенде — 1 диаграмма через skeleton→final_skeleton→graph, в логах баннер + `step=load_inputs/compute/persist_artifacts`, внутри graph-`compute` — `step=load_masks/bridge_preprocess/prepare_tracing/trace_edges`; `/stages` при искусственном сбое отдаёт `error_code`/`failed_step`. Деплой: `docker restart pid_worker` (worker+modules+app.core.errors), при желании и `pid_api` (errors.py под `app/`; `/stages` читает строкой — не обязателен).

**§9 (parking lot) — добавить:**
- **#13 (POSTPROCESS WARNING→INFO, `pipe_segmentation/postprocessing`):** остаётся отложенным — это segmentation-модуль, в этой волне его не трогали.
- **#14 (новый):** skeleton_extension — под-под-шаги COMPUTE (Вариант A) отложены осознанно (см. решение выше). Взять, если на CPU-бою skeleton «выглядит зависшей» несмотря на `[SKEL_EXT]`-тайминги на мосту.

---

### 8.10 Мини-волна — клиентский прогресс кнопки-стадии (2026-07-09, §0.2)

Всплыло на смоуке Волны 3 (пользователь): кнопка-стадия «Построение графа» залипала на ~18% и прыгала к 100%, пока уже бежал OCR; другие стадии — норма. Вне skeleton/graph (клиент, Волна 2 / «бюджеты из БД») — по §0.2 взято отдельной мини-волной с согласия.

**Причина (по коду):** `ui/widgets/diagram_workspace.py::_on_stages_updated` при смене бегущей авто-стадии (graph→ocr) заливал НОВУЮ кнопку, но НЕ сбрасывал предыдущую — завершённый `graph_building` висел со старым текстом/заливкой «· 18%» до следующей полной перерисовки (`_update_buttons`), т.е. до смены статуса. Усугубляет `_open_tab` (намеренная пауза опроса `status_provider.unwatch` на время открытой вкладки — против мерцания CVAT/WebEngine, §comment): `graph_building` — самая длинная авто-стадия (смоук: **72.8с**), часто бежит при открытой вкладке валидации → её кнопка успевает получить лишь один ранний опрос (~18%) и застыть.

**Фикс (surgical):** `_on_stages_updated` — при `prev_key != key` сбрасываем заливку предыдущей кнопки (`_update_buttons(self._last_status)`) перед заливкой новой; ветку «нет бегущей» сохранили. `progress_model.py` НЕ тронут (расчёт корректен — на OCR он и отдаёт `running_stage=ocr`). Тест — визуальный (Qt-виджет, как клиентские правки Волны 2; headless не гоняется без QApplication).
**[нужен ты]:** клиент-ребилд + визуалка перехода graph→ocr (кнопка graph зеленеет, ocr заливается; «· 18%» не залипает).

**§9 (parking lot) — добавить:**
- **#15 (клиент, прогресс — ОТДЕЛЬНАЯ мини-волна Волны 2, новый чат):** заливка `%` есть у detection/segmentation/skeleton, НЕТ у `graph_building` и `ocr`. Диагноз: кнопка заливается только на приходящем `stages_updated`; graph/ocr бегут ПОСЛЕ ручных гейтов (валидация узлов / привязка), где опрос `status_provider` на паузе (`_open_tab`→`unwatch` + остановка на `_FINAL_STATUSES`: DETECTED_JUNCTIONS/BUILT/CONTOURS_*/OCR_*), а первый авто-чейн идёт при активном опросе → у него `%` есть. Маппинг `_STAGE_TYPE_TO_KEY` корректен, `StageType` совпадает с `_PIPELINE`. Пробный сброс предыдущей кнопки через `_update_buttons` дал регрессию (ресетит текст ВСЕХ кнопок → стёр % и graph, и ocr) → **ОТКАЧЕНО к оригиналу**. Варианты (нужна живая итерация — клиент из venv, Claude не гоняет): (a) debug в `_on_stages_updated` → тикает ли опрос во время graph/ocr; (b) локальный QTimer заливки по elapsed, независимо от сетевого опроса; (c) сузить `_FINAL_STATUSES` до истинных гейтов+терминалов. **Kickoff:** «Волна 2 добивка — клиентский прогресс graph/ocr, RUNBOOK §9 #15».
- **#16 (перф graph, вне obs):** ✅ сделано 2026-07-09 (решено с пользователем «убрать рендеринг»). Было: `builder.save()` БЕЗУСЛОВНО рендерил matplotlib-оверлей (`plot_graph_overlay`, ~54с из 72.8с стадии), а задача `unlink`'ала его при `save_visualizations=False` → рендер впустую. Фикс: `save(save_visualization=...)` — `plot_graph_overlay` только под флагом; `graph.py` грузит `_save_vis` ДО `save`, передаёт флаг, мёртвый `unlink` убран. Вывод идентичен (оверлей сохраняется ровно когда `save_visualizations=True`, как раньше; артефакт `GRAPH_OVERLAY` отдаётся `/api/graph`), `graph_building` при выкл. флаге ~72.8с→~18с. По образцу segmentation/junction. Всплыло через obs-инструментовку Волны 3.

---

### 8.11 Мини-волна — клиентская заливка graph/ocr (per-stage %) (2026-07-09, §9 #15)

Добивка Волны 2 (§9 #15, отдельный чат). Заливка `%` кнопки-стадии не работала у `graph_building` и `ocr`. Взято с живой итерацией (клиент из venv), т.к. GUI не гоняется headless.

**Диагностика (Вариант a — временные `[WAVE2-DBG]`-логи в `_poll`/`watch`/`unwatch`/`_on_stages_updated`, сняты по закрытии):** прогон одной диаграммы через узлы→graph→ocr. Логи ОПРОВЕРГЛИ гипотезу §9 #15 («опрос `status_provider` на паузе во время graph/ocr»):
- В фазе `building_graph` опрос ЖИВ (`final=False will_unwatch=False`, `stages_updated` каждые 2с).
- НО `running_rows=['graph_building','ocr']` — OCR бежит ПАРАЛЛЕЛЬНО графу, и `compute_progress` перезаписывал `running_stage` последней бегущей → `ocr` (дальше по `_PIPELINE`). Заливка (`stage_percent`) уходила на кнопку **ocr**, `graph` не заливался вовсе.
- На `built` (гейт): `will_unwatch=True` → `unwatch` (таймер стоп). OCR ещё бежал в фоне (артефакт +18с), но `stages_updated` больше не тикал → заливка OCR замерзала (~31%) до перекраски ocr-поллером в зелёную.

**Сделано (`feat/observability`):**
- `ui/services/progress_model.py` — `ProgressState.stage_percents: {stage_type: %}` для КАЖДОЙ бегущей авто-стадии; `running_stage`/ETA/`stage_percent`/label теперь = самая РАННЯЯ бегущая (foreground), а не последняя. Параллельные стадии считаются в `done_w` и `stage_percents`, но foreground не перебивают.
- `ui/widgets/diagram_workspace.py` — `_on_stages_updated` льёт КАЖДУЮ бегущую кнопку своим % (graph и ocr одновременно); снятие заливки с переставшей бежать — новый `_restyle_button(key, status)` по ОДНОЙ кнопке (НЕ `_update_buttons` — тот трёт все → регрессия §8.10 / §9 #15). В `_check_ocr_artifact` (`_ocr_poll_timer`, 3с) добавлена подача заливки OCR после `built`, пока основной опрос на паузе; на готовности OCR — сброс текста кнопки.
- `ui/main.py` — cp1251-фикс логов (§9 #17): `stdout/stderr.reconfigure(utf-8, errors=replace)` до `basicConfig`.
- `tests/observability/test_progress_model.py` — +2 теста: параллельные `graph_building`+`ocr` (у каждой свой %, foreground=graph); одиночная стадия по-прежнему в `stage_percents` (обратная совместимость).
- НЕ тронуты: `status_provider.py` (вернулся к HEAD после снятия debug), `_update_buttons`, `progress_model` ETA/калибровка, параллелизм.

**Решения (§0.2 — спросил у пользователя):**
- **Per-stage %, а не единый foreground.** Пользователь: у каждой параллельной стадии — своя процентовка. Поэтому `stage_percents` (все бегущие), а не только foreground; каждая кнопка заливается своим %.
- **cp1251-лог — чиним заодно** (не отдельной волной): аддитивный reconfigure, поведение не меняет.
- **Инструмент правок:** крупные кириллические файлы правились UTF-8-safe записью (py-heredoc), НЕ Edit/Write — по уроку §8.8 (усечение хвоста на многобайтном символе).

**Верификация:**
- `pytest tests/observability/test_progress_model.py` = **15 passed** (13 старых + 2 новых); полный `tests/observability` ожидаемо **77**.
- `py_compile` всех правленых файлов; хвосты целы; `status_provider.py` — `git diff` пуст (debug снят подчистую).
- Визуальная проверка на клиенте (пользователь, uid `6e7144d5`): graph и ocr заливаются каждый своим %, OCR тикает после `built` до зелёной, cp1251-спам в консоли ушёл.

**Наблюдение:** OCR стартует ПАРАЛЛЕЛЬНО `building_graph` (не после гейтов графа, как подразумевал исходный §9 #15) и финиширует вскоре после `built`. Поэтому «честная» заливка OCR — в основном под foreground графа + короткий хвост через `_ocr_poll_timer`.

---

### 8.12 Мини-волна — POSTPROCESS/SKELETON_CONNECT WARNING→INFO (2026-07-09, §9 #13 + #18)

Закрытие §9 #13 (+ смежное #18, найдено по §0.2 в том же файле). Информационные тайминг-логи постобработки сегментации сыпались на `WARNING` (смоук Волны 4: `[POSTPROCESS] TOTAL: 0.49s` как warning — шум в `docker logs`). Понижены до `INFO`. Только уровень; текст/формат/аргументы/поведение не тронуты.

**Сделано (`feat/observability`):**
- `modules/pipe_segmentation/inference/postprocessing.py` — 14 `logger.warning` → `logger.info`: 9 в `post_process_mask` (`Start`, `1_remove_small`…`6_final_cleanup`, `TOTAL`) + 5 в `smart_skeleton_connect` (`skeletonize`/`find_endpoints`/`trace_directions+thickness`/`pair_matching`/`TOTAL`). Правка байт-safe (`sed`, не Edit/Write — §8.8); EOL=LF сохранён; диф ровно 14 строк (только токен уровня).
- `tests/observability/test_postprocess_log_level.py` *(new, 2 теста)* — регресс уровня: тайминги обеих функций на INFO, ни одной WARNING. Изоляция §9 #2 (cv2/torch/tqdm + `postprocessing.py` грузится напрямую через `importlib` из файла (в обход `inference.__init__`), БЕЗ заглушек `sys.modules["pipe_segmentation.inference.*"]`; cv2 — общий MagicMock; хелперы/скелетонизация — monkeypatch).

**Решения (§0.2 — спросил у пользователя):**
- **[SKELETON_CONNECT] — заодно (#18), а не отдельной волной.** Тот же файл, тот же дефект (информационный тайминг на WARNING), тот же путь вызова (внутри `post_process_mask`). Держать половину шума смысла нет.
- **INFO, не DEBUG.** Пункт #13 предлагал «INFO/DEBUG». Выбран INFO: тайминги под-шагов должны быть видны в штатных логах (весь смысл обсёрвабилити; DoD §4 «duration на конце каждого под-шага»), а не прятаться под DEBUG.

**Верификация:**
- `pytest tests/observability/test_postprocess_log_level.py` = **2 passed**; негативный контроль: на откате к `warning` оба падают (тест реально ловит регресс).
- `py_compile` OK; `logger.warning` в файле — 0, `logger.info` — 14; `print`/`except`/`raise` — 0 (DoD §4 чист без правок).
- Существующие тесты уровень этих логов нигде не проверяют (`post_process_mask` в `test_segmentation_errors` замокан) → регресса нет.

**Находка (тест-инфра, к §9 #2):** первая версия теста подменяла `pipe_segmentation.inference.engine` в `sys.modules` на уровне модуля → протечка в сессию pytest уронила `test_segmentation_errors` (4 fail «DID NOT RAISE»: он ждёт настоящий engine). Исправлено — целевой модуль грузится через `importlib` под приватным именем, `sys.modules` пакета не трогается (проверено: после импорта теста `pipe_segmentation.inference.*` отсутствуют). Побочно: `inference/__init__` импортит `run_inference`, которого нет в `engine.py` → `pipe_segmentation.inference` собирается лишь при замоканном engine (фрагильность тест-инфры — в скоуп §9 #2).

**[нужен ты]:** `pytest tests/observability -v` у себя (ожидаемо +2 к прошлому счёту); опц. smoke — 1 диаграмма через сегментацию, в `docker logs` `[POSTPROCESS]`/`[SKELETON_CONNECT]` теперь INFO. Деплой: `docker restart pid_worker`. Коммит — ты (§0.1).

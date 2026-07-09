# AUDIT: предмержевой разбор feat/observability → deploy (2026-07-09)

Полный разбор перед мержем: состояние git, проверка ветки тестами и построчным ревью (3 независимых прохода по зонам: worker / core+api / modules+ui), план работ. Код не менялся — только анализ.

---

## 1. Резюме

Ветка достигает заявленной цели (канон под-шагов, типизированные ошибки, error_code/failed_step в БД, прогресс/окно ошибок в клиенте; 124/124 obs-теста зелёные; инвариант «поведение ML-модулей не меняется» подтверждён `diff -w`). Но мержить прямо сейчас нельзя:

- **1 CRITICAL-регрессия**: `obs.step()` перехватывает `SoftTimeLimitExceeded` → таймауты всех 8 worker-задач уходят в retry вместо терминального фейла (§5 C1).
- **2 бага ветки**: в compose уехали volume-строки `pil_unbomb.pth` без самого файла (C2); загрязнение `sys.modules` из `test_graph_errors.py` валит 13 тестов `test_direction_nodes.py` в полном прогоне (C3).
- Локальный `deploy` отстал от `origin/deploy` на 2 коммита (Pillow-хотфикс) → **fast-forward уже невозможен**, нужен обычный merge. Симуляция мержа: **конфликтов нет**.
- Плюс 11 RISK-находок (стрельнут при определённых условиях) и список MINOR — большинство можно после мержа (§5, §7).

Рекомендуемый порядок: Волна A (блокеры) → тесты → коммит → мерж в deploy → Волна B (current_step в клиент) → Волна C (остальное).

---

## 2. Git-состояние (чинится в твоём Windows-терминале)

| Что | Диагноз | Действие |
|---|---|---|
| В индексе staged-удаление всего `worker/` + странный rename `worker/__init__.py → ''` | Известная поломка индекса от записи в .git из песочницы (RUNBOOK §0.1) | `del .git\index.lock` (если есть), `del .git\index`, `git reset`; проверить `git status` |
| 12 файлов «modified» (obs.py, logging.py, cvat.py…) | Фантом песочницы (CRLF/стейл-чтения). Реальные файлы на диске сверены с HEAD — целые и идентичные | Ничего. Источник правды — Windows-git |
| `deploy` локальный | Отстаёт от `origin/deploy` на ce683c8 (pil_unbomb.pth) + 5a31331 (volumes) | `git checkout deploy && git pull --ff-only` |
| `feat/observability` | 10 локальных непушнутых коммитов | `git push origin feat/observability` |
| Untracked-мусор: `PLAN_*.md` (3 шт. в корне), `skins_raw/`, `skins_catalog.fxml`, `wave*.log`, `diag_status.txt`, `.~lock.*.xlsx#`, `tools/cvat_export_probe.py`, `docs/AUDIT_manual_edit_routing.md`, `docs/PLAN_routing_patch.md`, `docs/audit_model_2026-07.py`, `_scratch/` | В мерж не попадает (не отслеживается) | По желанию: разобрать/добавить в .gitignore |

---

## 3. Мерж: как правильно

**Почему не fast-forward.** ff возможен, только когда целевая ветка — строгий предок. После Pillow-хотфикса `origin/deploy` и `feat` разошлись (общий предок d4f0bb3) → git сделает обычный мерж-коммит. Разница, о которой спрашивал:
- `--ff-only`: просто передвинуть указатель без мерж-коммита — **уже неприменимо** (история разошлась).
- `--no-ff`: принудительный мерж-коммит даже там, где возможен ff. В нашем случае мерж-коммит будет и так; флаги не нужны.

**Симуляция** (клон в песочнице, репозиторий не трогался): merge `origin/deploy` × `feat` — **0 конфликтов**. docker-compose объединился корректно (logging-якоря feat + pil_unbomb-вольюмы deploy; volume-строки совпали байт-в-байт, потому что в feat они уехали случайно — см. C2). Итоговый дифф мержа против feat — только файл `cvat_patch/pil_unbomb.pth`.

**Процедура (после Волны A и коммита, всё в твоём терминале):**

```bat
git fetch origin
:: 1. починка индекса — только если git status показывает удаление worker/
del .git\index.lock
del .git\index
git reset
:: 2. обновить deploy
git checkout deploy
git pull --ff-only origin deploy
:: 3. мерж (конфликтов не ожидается)
git merge feat/observability
:: 4. прогнать тесты на результате мержа, затем
git push origin deploy
git push origin feat/observability
```

**Деплой-чеклист (сервер):**
1. `git pull` на deploy.
2. **`alembic upgrade head` ДО перезапуска воркеров** — модель теперь селектит `error_code`/`failed_step`; воркер на немигрированной базе уронит каждую стадию (UndefinedColumn) (R11).
3. `docker compose build api worker worker_ocr && docker compose up -d` (сигнатуры/очереди задач не менялись — in-flight задачи совместимы).
4. Пересобрать клиент (PySide6).
5. Смоук: одна диаграмма конец-в-конец, проверить `/stages`, заливку кнопок, окно ошибки.

---

## 4. Как проверялась ветка

- Чистые снапшоты `git archive` (мимо глючного маунта): **tests/observability 124/124 passed**.
- Полный прогон: feat 225 passed / 35 failed vs deploy 114 passed / 22 failed. Все +13 новых падений = `test_direction_nodes.py`, причина — C3 (при одиночном прогоне проходят). Остальные падения идентичны deploy (нет torch/PySide6 в песочнице + legacy `test_ocr_phase0.py` с `sys.exit` на импорте — предсуществует).
- Случайный порядок (pytest-randomly, 3 сида): стабильно, других загрязнений нет.
- Построчное ревью всех рантайм-диффов тремя независимыми проходами; спорные гипотезы проверялись исполнением (обёртка SoftTimeLimit — на живом celery; aware-datetime TypeError — воспроизведён; торч 2.6.0 из Dockerfile.worker совместим с `torch.amp.autocast('cuda')` и `torch.cuda.OutOfMemoryError`).
- Смоуки на стенде (GPU) — по записям RUNBOOK §8, пройдены тобой ранее.

---

## 5. Находки

### CRITICAL

**C1. `obs.step()` глотает `SoftTimeLimitExceeded`** — app/core/obs.py:58-69.
`SoftTimeLimitExceeded` наследует `Exception`; `step()` оборачивает его в `PipelineError` → задачные `except SoftTimeLimitExceeded` (detection.py:420, segmentation.py:480, skeleton.py:399/790, graph.py:342/718, junction.py:313, ocr.py:218, contours.py:304, direction.py:245) — мёртвый код. Таймаут уходит в generic-ветку с `self.retry`: до 3× полного времени на заведомо тухнущей задаче, искажённое сообщение об ошибке, фантомные RUNNING-строки на ретраях. Подтверждено на живом celery. **Фикс**: passthrough в `obs.step` до `except Exception` (ленивый импорт `celery.exceptions.SoftTimeLimitExceeded`) + regress-тест. Заодно passthrough для `BaseException`-семейства не нужен (он и так не ловится), а вот `HTTPException` — см. R8.

### Баги ветки (не рантайм-код)

**C2. Volume-строки `pil_unbomb.pth` без файла** — docker-compose.yml (уехало в коммит 06e0a27), файл есть только в origin/deploy (ce683c8).
На чистом клоне feat `docker compose up` создаст на месте файла **каталог**; site.py молча игнорирует каталог-«.pth» → Pillow-bomb-лимит возвращается для больших схем (тот самый отказ upload_media). Затронуты cvat_server, cvat_worker_import, cvat_worker_export, cvat_worker_chunks. **Фикс**: закоммитить файл в feat (117 байт, ASCII) — или полагаться на порядок «сначала pull deploy, потом merge» (мерж лечит). Рекомендую закоммитить: ветка самодостаточна.

**C3. Загрязнение `sys.modules` из test_graph_errors.py** — tests/observability/test_graph_errors.py:28-31.
`sys.modules.setdefault("cv2", MagicMock())` + 7 подмодулей graph на уровне модуля, без снятия. Загрязнение происходит на этапе КОЛЛЕКЦИИ pytest (импорт модуля), поэтому порядок прогона не спасает: 13 тестов `test_direction_nodes.py` получают MagicMock вместо реального `direction_nodes`. **Фикс**: паттерн изоляции как в test_junction_errors.py (импорт под заглушками + pop из sys.modules на teardown, «ноль протечки»).

### RISK (может стрельнуть при условиях)

| # | Где | Что | Минимальный фикс |
|---|---|---|---|
| R1 | worker/tasks/detection.py:382 | Сужение `except Exception` → `except CVATError`: не-httpx сбои CVAT-блока (парсинг ответа, `det["class_id"]`, tempfile/zip) раньше были non-fatal print — теперь фатальны + при ретраях `create_task` **плодит дубликаты CVAT-задач**, GPU-пересчёт ×3 | Обсудить: вернуть широкий except вокруг не-сетевой части или try внутри |
| R2 | retry-ветки 9 задач (detection.py:442, segmentation.py:495, skeleton.py:413/805, graph.py:357/732, junction.py:327, ocr.py:234, contours.py:322, direction.py:259) | `fail_stage(...)` без `db.commit()` перед `raise self.retry` → на каждый ретраенный аттемпт **вечная RUNNING-строка** в БД + error_code/traceback не сохраняются для нефинальных попыток | `db.commit()` сразу после `fail_stage` |
| R3 | worker/utils/db_helpers.py:186-190 × app/api/cvat.py:424-436 | Гонка start_stage(commit)↔reopen сужена, не закрыта: окно до commit + обратная гонка (complete перетирает SKIPPED) | Зафиксировать как известное ограничение или re-check статуса перед финальным commit |
| R4 | app/api/cvat.py:308-372, frame_stage_helpers.py:49-60 | CVAT/frame-стадии: RUNNING навсегда при CancelledError/kill процесса; двойной `/start` frame → 2 RUNNING-строки, закрывается только новейшая | Закрывать ВСЕ RUNNING-строки типа в complete/skip |
| R5 | app/api/cvat.py:366-372, frame.py:246-249 | Упал `db.commit()` в try → except коммитит «грязную» сессию → PendingRollbackError, стадия остаётся RUNNING | `await db.rollback()` перед fail-записью |
| R6 | app/models/stage.py:123-137, db_helpers.py:207-208, cvat.py:68 | `error_code`/`failed_step` не обрезаются под String(64)/String(32); у SQLAlchemyError есть свой `.code` (None/"e3q8") → мусор вместо имени типа | `str(...)[:64]/[:32]` + `isinstance(code, str)` |
| R7 | worker/celery_app.py:104-136 | Латентная рекурсия: повторный `setup_logging()` после подмены stdout посадит хендлер на мост → лог→stdout→лог | Guard от повторной инициализации поверх моста |
| R8 | app/core/obs.py:58 | `HTTPException` внутри `obs.step` превратится в PipelineError (404→500). Сейчас все raise до step-блоков — ловушка на будущее | Passthrough HTTPException рядом с C1-фиксом |
| R9 | ui/services/progress_model.py:98-135,160,188; api_client.py:339-357,442-450 | (а) aware-ISO (`+00:00`) → TypeError в вычитании с naive utcnow (латентно, сервер шлёт naive); (б) elapsed от часов клиента → рассинхрон часов = заливка 0%/95%; (в) нет негативного кэша stage-durations → на старом API лишний HTTP каждые 2 с; (г) reopen POST с retries=3 → возможен повторный revoke при таймауте | (а) replace(tzinfo=None); (б) отдавать `now` сервера в /stages; (в) кэшировать {} на 60 с; (г) retries=0 |
| R10 | ui/services/status_provider.py:104 + app/api/diagrams.py:325-340 | 2 синхронных HTTP/тик/диаграмму в GUI-потоке (при недоступном сервере — заморозки на sleep 1-2-4 с); /stages каждый тик тащит `error_traceback` всех попыток (килобайты) | Позже: QThread/объединённый ответ; traceback — отдельным эндпоинтом по клику |
| R11 | деплой | Миграция 0007 обязана пройти ДО старта нового воркера/api (UndefinedColumn на каждой стадии) | Пункт чеклиста §3 |

### MINOR / SMELL (не блокирует; кандидаты в Волну C)

- Канон под-шагов расходится: direction — загрузка модели внутри `compute` (сбой атрибутируется не тому шагу); junction — запись артефактов в `postprocess` вместо `persist_artifacts`; graph build — `load_inputs` без step-обёртки; `dispatch` нигде не оформлен шагом. Важно выровнять ДО current_step (Волна B), чтобы клиент показывал осмысленные имена.
- STL-ветки зовут `fail_stage` без `exc=` → `error_code=NULL` для таймаутов (код `timeout` был бы полезнее).
- ui/widgets/error_report_dialog.py:106 — `if x != "" or True` всегда истинно (фильтр мёртв, спасает .strip()).
- ETA: для параллельной стадии в remaining добавляется полный бюджет вместо остатка; `percent=100` при failed+fxml_done; format_eta без часов.
- app/api/frame.py:211-223 — повторный `/skip` неидемпотентен (плодит строки); SKIPPED-строкам пишется `error_message="stopped: reopened…"` — не ошибка в поле ошибки.
- cvat_client: `CVATTimeoutError` из `_wait_for_job` без `step=`; `upload_images_to_task` не обёрнут `_cvat_op`; `_wait_for_data` использует deprecated `GET /tasks/{id}/status` (перепроверить при апгрейде CVAT>2.25).
- Ротация docker-логов не применена к cvat_*/traefik; LOG_LEVEL default DEBUG теперь и в worker (объём, купируется ротацией).
- segmentation.py:99,139-147 — warning с exc_info в пер-аннотационном цикле (спам при систематически битых данных).
- graph: убран `unlink()` старого graph_graph.png → возможен stale-файл; скип matplotlib-рендера (~54 с) — недекларированное (полезное) изменение.
- detection.py:392-394 — error_code/error_message на стадии, которая завершится COMPLETED (задумано «не фатально», зафиксировать контракт).
- ensemble.py:48-50 — единственный модуль с безусловным `from app.core...` (в чистом образе без volume-mount `./app` упадёт импорт; остальные модули деградируют молча).
- junction OOM ловится по подстроке без `torch.cuda.OutOfMemoryError` (несогласовано с engine).
- Мёртвые импорты: PipelineError (contours:31, direction:36), OcrError (ocr:25), `import logging` в 6 задачах.
- `datetime.utcnow()` deprecated (3.12); `_restyle_button` не воспроизводит ERROR-ветку (латентно); setStyleSheet каждые 2 с без чека изменения значения.

### Проверено-ОК (для уверенности)

Инвариант ML-модулей (`diff -w` чист: skeleton_extension/engine/builder/junction/ocr — реиндент+обёртки); autocast-фикс эквивалентен, torch 2.6.0 совместим; `binarize=False` — рефакторинг (так было и раньше); в ocr потоков нет → contextvars безопасны; сигнатуры/имена/очереди задач не менялись; alembic-цепочка одна голова, 0007 metadata-only; logging без дубликации хендлеров, ContextFilter дефолтит поля (`dur=` не падает на чужих записях); contextvars в async-API не текут между запросами; stats: медиана в Python (SQLite/PG), пустая таблица → {}; /stages обратносовместим со старым клиентом; cvat_client: ретраи/таймауты/ветка 400 not-finished сохранены, `_wait_for_data` без вечного цикла (cap 60 с); мост stdout→log в текущем порядке инициализации безопасен, fileno/isatty делегированы; cp1251-фикс клиента корректен; obs.step на границах фаз — накладные ничтожны.

---

## 6. Дизайн Волны B — current_step («подстадия» в клиент)

Цель пользователя: на CPU-стенде видно, что медленная авто-стадия жива. Поле `current_step` осознанно отложено в Волне 2 RUNBOOK — реализуем:

1. **БД**: миграция 0008 — `current_step VARCHAR(64) NULL` в processing_stages (nullable, metadata-only, как 0007).
2. **obs.py**: необязательный «репортер» шага в контексте (`_ctx`): `obs.step()` на старте шага зовёт его под try/except (сбой репортера никогда не роняет пайплайн); `obs.reset()` в task_prerun его чистит.
3. **worker**: после `start_stage(...)` задача привязывает репортер; запись — **отдельной короткой сессией** `UPDATE processing_stages SET current_step=:name WHERE id=:id` + commit (не трогает грязную транзакцию задачи — урок R2); на complete/fail — NULL. Под-под-шаги модулей (inference/tiling/trace_edges…) репортятся автоматически — они уже идут через obs.step.
4. **API**: `current_step` в ProcessingStageResponse (/stages) — лишних запросов нет.
5. **Клиент**: подпись в статусбаре рядом с индикатором `🟢 API` для открытой диаграммы: «Выделение труб · inference». Показывать только для авто-стадий (бюджет ≠ None); ручные — ничего. Обновление — существующим поллингом 2 с. Чистая функция в progress_model (тестируемая headless).
6. **Тесты**: obs-репортер (вызов/очистка/глотание сбоев); db-хелпер; /stages поле; клиентская функция.
7. RUNBOOK: запись решения (§9 + волна), по протоколу §0.2.

Предпосылка: выровнять канон-имена шагов (MINOR №1), иначе в статусбаре будут кривые атрибуции.

---

## 7. План работ

**Волна A — блокеры до мержа** (≈ один заход):
- A1. C1: passthrough SoftTimeLimitExceeded в obs.step + regress-тест.
- A2. C2: закоммитить `cvat_patch/pil_unbomb.pth` в feat.
- A3. C3: изоляция sys.modules в test_graph_errors.py → полный прогон без новых падений vs deploy.
- A4. R2: `db.commit()` после fail_stage в retry-ветках (9 мест) + тест.
- A5. R6: обрезка/страховка error_code/failed_step.
- A6. R1 — **нужно твоё решение**: вернуть широкий except в CVAT-блоке detection (как в deploy) или оставить фатальность? Рекомендация: вернуть non-fatal для не-CVATError (поведение deploy), CVATError оставить типизированным.
- DoD: obs-тесты + новые зелёные; полный прогон = deploy-бейзлайн; дифф-ревью правок.

**Мерж** (§3) — после Волны A.

**Волна B — current_step** (§6). Можно до мержа (в feat) — рекомендую, это часть цели ветки.

**Волна C — после мержа, по выбору**: R3 (решение по гонке), R4 janitor RUNNING-строк, R5 rollback-fix, R7 guard, R8 HTTPException passthrough, R9а-г клиентские фиксы, R10 поллинг/traceback, канон-выравнивание + MINOR-мелочь.

---

## 8. Границы анализа

Живой стенд (Docker/GPU/CVAT/Qt) не запускался — reopen/stats интеграционно и UI-поведение проверены кодом и юнитами; внутренности незатронутых зависимостей (text_detect_yolo, recognize_surya, trace_edges_v3, bfs_connect_endpoints) — только границы вызовов. Смоуки стенда — по твоим записям RUNBOOK §8.

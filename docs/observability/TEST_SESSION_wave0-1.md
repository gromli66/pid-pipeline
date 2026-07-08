# TEST SESSION: наблюдаемость (Волна 0 + Волна 1-reopen)

Чек-лист ручной тест-сессии по внедрённым изменениям. Идём сверху вниз, отмечаем.

> **Главный принцип:** Волна 0 — фундамент, **поведение пайплайна НЕ меняли**. Поэтому
> задача теста — (а) убедиться, что ничего не сломалось (регрессия), и (б) что новая
> наблюдаемость реально видна. Reopen (Волна 1) — единственное новое поведение.

---

## 0. Что покрыто этой сессией / что НЕТ

**Покрыто:**
- корреляционный формат логов + `ContextFilter`, ротация docker-логов, мост `stdout→logging`;
- колонки `error_code`/`failed_step`, поля `celery_task_id`/`error_code`/`failed_step` в `/stages`;
- `reopen-bbox-validation` (жёсткий возврат на проверку) + понятная ошибка подтверждения в неверном статусе.

**НЕ покрыто (осознанно, будет позже):**
- ~~CVAT-транспортные типы (export/timeout/5xx)~~ → **уже сделаны в Волне 1** (типизированы + лог `code=cvat_*`; проверено §5.3). Осталась generic-обёртка только в теле HTTP-ответа (500) — эндпоинт CVAT-тип на HTTP-статус не мапит (осознанно).
- клиентская кнопка «Проверка элементов» → reopen → **Волна 2 / точечно** (§9). Пока reopen только через API.
- реальные `uid/phase/step` в логах СТАДИЙ (не reopen/confirm) → появятся, когда инструментируем задачи (стадийные волны). Сейчас там `-`.

---

## 1. Подготовка
- [ ] `docker compose ps` — `api`/`worker`/`worker_ocr`/`postgres`/`redis` = Up (healthy).
- [ ] `docker exec pid_api alembic current` → `0007_add_error_code_failed_step (head)`.
- Логи: `docker logs pid_api -f`, `docker logs pid_worker -f`. Греп по диаграмме: `... | Select-String "<uid>"`.
- Формат строки теперь: `… | uid=… phase=… step=… | сообщение`.

---

## 2. Регрессия — нормальный прогон из клиента (ничего не сломалось)
Прогнать 1–2 диаграммы полный цикл через клиент, как обычно:
- [ ] Загрузка → детекция → открыть CVAT → разметить → подтвердить → сегментация → скелет → … → FXML.
- [ ] Каждый этап проходит как раньше; клиент ведёт себя идентично.
- [ ] Прогресс-бусины, окна валидации — без изменений.

**Ожидание: разницы в UX нет.** Любое иное поведение = регрессия, фиксируем (файл/шаг/что ждали).

---

## 3. Наблюдаемость — что нового видно (backend)
Во время/после прогона:
- [ ] `docker logs pid_worker | Select-String "uid="` — строки задач в нашем формате (пока `uid=-`, задачи ещё не биндят — это ок).
- [ ] `docker logs pid_worker | Select-String "worker.stdout"` — `print()` из стадий тегированы.
- [ ] `curl.exe http://localhost:8000/api/diagrams/<uid>/stages` — у стадий есть `celery_task_id`, `error_code`, `failed_step` (на успехе `null`).
- [ ] Ротация: `docker inspect pid_worker --format "{{json .HostConfig.LogConfig}}"` → `json-file`, 20m×5.

---

## 4. Reopen — жёсткий возврат на проверку (сейчас через API)
> Ручка: `POST http://localhost:8000/api/cvat/<uid>/reopen-bbox-validation`. Кнопка в клиенте — позже.

**Сценарий A — стадия завершена (обычный кейс):**
- [ ] Взять диаграмму, ушедшую за bbox (seg/skeleton прошли).
- [ ] POST reopen → в ответе `status: validating_bbox`, `cvat_url` на ТОТ ЖЕ таск, `deleted_artifacts > 0`, `revoked_tasks: 0`.
- [ ] Открыть `cvat_url` → **разметка на месте** (таск не пересоздан).
- [ ] Логи: `reopen_validation` `step.start`/`step.end` с реальным `uid` и `duration_ms`.
- [ ] Доразметить в CVAT → `POST /api/cvat/<uid>/fetch-annotations` (подтвердить) → проходит (статус был `validating_bbox`), пайплайн пошёл заново.

**Сценарий B — жёсткий стоп (стадия ещё бежит):**
- [ ] Запустить диаграмму, поймать момент, когда сегментация/скелет РАБОТАЕТ.
- [ ] POST reopen → `revoked_tasks > 0`.
- [ ] Проверить, что задача реально остановилась: в `docker logs pid_worker` нет продолжения этой стадии; статус диаграммы = `validating_bbox` и НЕ перескочил обратно (нет гонки).

---

## 5. Как ломать (fault-инъекции) — ожидаемое
- [x] **Подтверждение в неверном статусе** (исходный баг): довести до `skeletonizing` → POST `fetch-annotations` → HTTP 400, внятный текст «Нельзя подтвердить: диаграмма в статусе '…', ожидается 'validating_bbox'…»; в логах `confirm rejected` с `uid`+`from_status`+`code=stage_state_invalid`. ✅ репро через `validated_bbox`: `HTTP 400`, лог `code=stage_state_invalid from_status=validated_bbox`.
- [x] **Reopen из раннего статуса** (`uploaded`/`detected`) → HTTP 409 «Нельзя переоткрыть…»; лог `reopen rejected`. ✅ на `validating_bbox` (тот же reject-путь): `HTTP 409` + лог.
- [x] **CVAT недоступен**: `docker stop cvat_server` → подтвердить → `HTTP 500`. Лог **уже типизирован**: `step=confirm code=cvat_export` + `cvat.error` (op/http_status/тело-срез). Тело HTTP-ответа — generic (`Failed to fetch annotations: …`): эндпоинт CVAT-тип на HTTP-статус не мапит (осознанно). (не забыть `docker start cvat_server`.)
- [x] **Export не готов** (`400 ... not been finished yet`): тип `CVATExportError`/`cvat_export` **уже есть** (тот же путь, что §5.3). Гонку экспорта детерминированно живьём не воспроизвести — покрыто юнит-тестом `test_cvat_errors.py`.

---

## 6. Чек-лист приёмки
- [x] Полный прогон из клиента — без регрессий. *(fe4832f8 → FXML, 10 стадий `completed`)*
- [x] Reopen: сброс downstream + разметка CVAT сохранена + подтверждение после проходит. *(§4-A)*
- [x] Жёсткий стоп реально останавливает бегущую стадию (revoked>0, задача не дописывает). *(после фикса §9 #5: `revoked=1`, статус держится)*
- [x] Ошибки состояния — внятные + залогированы по `uid` (`confirm rejected` / `reopen rejected`). *(§5.1–5.2)*
- [x] `/stages` отдаёт новые поля; логи в новом формате; ротация настроена. *(§3)*

---

## 7. Заметки по сессии (заполнять по ходу)

> Сессия 2026-07-08 (`feat/observability`). Стенд: Windows/PowerShell, `curl.exe`, docker-compose.

| Что делали | Ожидали | Получили | Баг? |
|---|---|---|---|
| §1–§3: сервисы, миграция, формат логов, `/stages`, ротация | Up; `0007…(head)`; `uid=… phase=… step=…`; поля `celery_task_id/error_code/failed_step`; `json-file 20m×5` | всё ✓; Celery-логи в нашем формате; `worker.stdout` теги от `print()` | нет |
| §2 полный прогон из клиента (`fe4832f8`) до FXML | UX как раньше, без регрессий | все 10 стадий `completed`, `error_code=null`; FXML сгенерён | нет |
| §4-A reopen с завершённой стадии | `validating_bbox`, тот же CVAT-таск, `deleted>0`, `revoked=0`, разметка цела, confirm после проходит | всё ✓ (`deleted_artifacts:20`, разметка на месте, confirm→`validated_bbox`) | нет |
| §4-B reopen на бегущей сегментации | `revoked>0`, стадия убита, статус не уезжает | `revoked_tasks:0`, сегментация дожила до `succeeded`, статус уехал в `skeletonized` | **ДА** → §9 #5 |
| §4-B повтор после фикса (`start_stage` flush→commit) | `revoked≥1`, статус остаётся `validating_bbox` | `revoked_tasks:1`, статус `validating_bbox`; `pytest tests/observability` = 29 passed | нет (исправлено) |
| §5.1 confirm в неверном статусе | HTTP 400 + `confirm rejected` с `code`/`from_status` | `HTTP 400`; лог `code=stage_state_invalid from_status=validated_bbox` | нет |
| §5.2 reopen из невозвратного статуса | HTTP 409 + `reopen rejected` | `HTTP 409`; лог `reopen rejected … code=stage_state_invalid` | нет |
| §5.3 CVAT недоступен (`docker stop cvat_server`) | ошибка + типизированный лог | `HTTP 500`; лог `step.error code=cvat_export` + `cvat.error` (уже типизировано — §0/§5 «pending» устарели) | нет |
| §5.4 export не готов (`400 not finished`) | `CVATExportError` | не воспроизводили (гонка); тип `cvat_export` покрыт `test_cvat_errors.py` + §5.3 | н/д |

---

## 8. Итог сессии (2026-07-08)

**Вердикт: пройдено.** §1–§6 зелёные; наблюдаемость Волн 0–1 подтверждена вживую на стенде.

**Что проверили:** корреляционный формат логов + мост `worker.stdout` + Celery-логи в нашем формате; `/stages` (`celery_task_id`/`error_code`/`failed_step`, `null` на успехе); ротация `json-file 20m×5`; полный прогон из клиента до FXML без регрессий (10 стадий `completed`); reopen сценарии A/B; состояние-ошибки (`confirm`/`reopen rejected`, HTTP 400/409); CVAT-транспорт-ошибки (`code=cvat_export`).

**Что нашли и почему починили:**
- **Баг (§9 #5): reopen не останавливал бегущую стадию.** `start_stage` коммитил RUNNING-строку только `flush()` → reopen читает стадии в ОТДЕЛЬНОЙ сессии и незакоммиченную строку не видит → `revoked_tasks=0`, чейн добегает и перетирает статус (гонка: уезжал в `skeletonized`). Это ломало DoD Волны 1 «жёсткий стоп». **Фикс (Вариант 1):** `flush()`→`commit()` в `start_stage` + регресс-тест `test_start_stage_commit.py`. Перепроверено: `revoked 0→1`, статус держится; `pytest tests/observability` = 29 passed.
- **Наблюдаемость (правка по ходу):** в `confirm`/`reopen rejected` вынес `code`/`from_status` в текст лог-строки (были только в `extra`, невидимы в `docker logs`).
- **Находка №2:** CVAT-транспорт уже типизирован (`code=cvat_export`); §0/§5 «pending» устарели — поправлено.
- **#6 (`/status` без `status`)** — ложная тревога (артефакт копипаста), закрыто.

**Отложено:** предохранитель статуса в телах задач (Вариант 2) — если поймаем остаточную микро-гонку на стыке стадий; клиентская кнопка → Волна 2 (§9 #4).

**Код-дельта (`feat/observability`):** `worker/utils/db_helpers.py` (фикс), `app/api/cvat.py` (лог-строки `rejected`), `tests/observability/test_start_stage_commit.py` *(new)*.

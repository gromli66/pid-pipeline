# План: серверная дебаг-дичность логов + доменные ошибки

Сценарий: клиент прислал ошибку → ты лезешь на сервер и должен **сразу** понять на каком этапе и где проблема. Плюс ответ на вопрос про доменные исключения, иерархию и «слишком крупную сетку `except`».

---

## 0. Почему сейчас нельзя быстро локализовать (факты)

- **Корреляции нет** — ни `contextvar`, ни `logging.Filter`, ни `extra=` (проверено). В строках лога нет `uid`/`stage`/`task_id`. При `worker --concurrency=2` две диаграммы пишут вперемешку → нельзя грепнуть по диаграмме.
- **`/stages` не отдаёт `celery_task_id`** (хранится, но не в `ProcessingStageResponse`) → клиент не может дать тебе якорь для грепа.
- **Детализация заперта в `print()`** в самых сложных стадиях: `modules/graph/core` print=128/logger=0, `skeleton_extension` 185/0, `pipe_segmentation` 281/27, `yolo_detector` 4/0. `print` без времени/уровня/модуля/uid.
- **Нет доменных типов ошибок** — всё встроенные (`FileNotFoundError`×45, `ValueError`×43, `RuntimeError`×13, `Exception`×3). По типу нельзя понять этап/категорию; `except` не может быть точечным.
- **Крупная и немая сетка**: `segmentation.py:91,131,135` и `graph.py:246` — `except Exception: pass` вообще без лога. В task-boundary трейсбек — на `logger.debug` → при INFO его в логах нет.

Вывод: сначала — фундамент (типы ошибок + корреляция + вывод трейсбека), потом — точечное расширение по стадиям. Без фундамента расширять логи бессмысленно.

---

## 1. Доменные ошибки и иерархия (ответ на вопрос)

Сейчас: `raise ValueError(...)`, `raise RuntimeError(...)`, `raise Exception(...)` — тип ничего не говорит. Предлагаю ввести иерархию в новом `app/core/errors.py`:

```python
class PipelineError(Exception):
    """База: несёт этап, uid, код, причину."""
    stage = "unknown"; code = "pipeline_error"
    def __init__(self, message, *, stage=None, diagram_uid=None, cause=None):
        super().__init__(message)
        self.stage = stage or self.stage
        self.diagram_uid = diagram_uid
        if cause: self.__cause__ = cause

# входные данные
class ArtifactMissingError(PipelineError): code = "artifact_missing"
class InvalidImageError(PipelineError):    code = "invalid_image"
# модель/инференс
class ModelLoadError(PipelineError):       code = "model_load"
class InferenceError(PipelineError):       code = "inference"
class GpuOutOfMemoryError(InferenceError): code = "gpu_oom"
# по стадиям
class DetectionError(PipelineError):     stage = "detecting"
class SegmentationError(PipelineError):  stage = "segmenting"
class SkeletonizationError(PipelineError): stage = "skeletonizing"
class JunctionError(PipelineError):      stage = "detecting_junctions"
class GraphBuildError(PipelineError):    stage = "building_graph"
class ContourError(PipelineError):       stage = "extracting_contours"
class OcrError(PipelineError):           stage = "ocr"
class FxmlError(PipelineError):          stage = "generating_fxml"
class FrameRemovalError(PipelineError):  stage = "frame_removal"
# CVAT (отдельное семейство — главная слепая зона)
class CVATError(PipelineError):          stage = "cvat"; code = "cvat_error"
class CVATConnectionError(CVATError):    code = "cvat_connection"
class CVATTimeoutError(CVATError):       code = "cvat_timeout"
class CVATRequestError(CVATError):       code = "cvat_request"   # 4xx/5xx
class CVATImportError(CVATError):        code = "cvat_import"
class CVATExportError(CVATError):        code = "cvat_export"
```

Что это даёт для дебага:
- **Тип = этап + категория.** `GpuOutOfMemoryError` в логе сразу говорит и что (OOM), и где (InferenceError→стадия).
- **`raise ... from exc`** сохраняет причину (cause chain) — видно и наш контекст, и оригинальное исключение библиотеки.
- **Структурный `error_code`.** `fail_stage`/`set_diagram_error` пишут `code = getattr(exc, "code", type(exc).__name__)` и `stage = getattr(exc, "stage", ...)`. Тогда `error_stage` перестаёт зависеть от строкового совпадения, а клиент показывает категорию и решает retry vs report.
- **Точечный `except`.** Можно ловить `except CVATError` или `except InferenceError`, а не «всё подряд».

Минимальная схема применения: **бросать типизированное в точке отказа с `from exc`**, ловить узко рядом с причиной только ожидаемое/recoverable, а один широкий safety-net оставить на границе задачи (он и так есть). Рекомендую добавить колонку `error_code` в `processing_stages` (мелкая alembic-миграция) — тогда категория едет в клиент и грепается в логах.

---

## 2. Где сетка слишком крупная → сделать точечной

| Место | Сейчас | Проблема | Точечно |
|---|---|---|---|
| `detection.py:307` (CVAT) | `except Exception as cvat_exc:` → `print("non-fatal")` | ловит и сбой CVAT, и наши баги; глушит; в stdout | `except CVATError:` → warning-stage + `logger.warning(exc_info=True)`; не-`CVATError` **не ловить** (пусть падает в safety-net) |
| `segmentation.py:91` | `except Exception: pass` | **немой** глотатель, возвращает нулевую маску | `except (KeyError, ValueError, cv2.error):` + `logger.warning(...)`; неожиданное — наверх |
| `segmentation.py:131/135` | `except Exception: pass` (decode→bbox fallback) | **немой** | узкий except на ошибку декода RLE/полигона + WARNING с uid |
| `graph.py:246` | `except Exception: pass` (чтение флага проекта) | **немой** | `except AttributeError:` + `logger.debug` |
| `graph.py:536/564` | `except Exception: warning "non-fatal"` | широкий демоут (прячет баги merge) | `except (ContourError, FileNotFoundError, json.JSONDecodeError):`; прочее — наверх |
| `skeleton.py:227/551/602/700` | `except Exception: warning; continue` | широкий демоут, скрывает деградацию качества | узкий except на ожидаемое (`cv2.error`/`FileNotFoundError`) → **warning-stage**; прочее — наверх |
| task-boundary во всех задачах | `except Exception as exc:` → `fail_stage`+`set_error`, `logger.debug(traceback)` | сам catch ОК (последний рубеж), но **трейсбек на DEBUG** — при INFO его нет в логах | оставить широким, но `logger.error("...", exc_info=True)` + записать `error_code` |
| `cvat_client.py` (все методы) | утекают `httpx.HTTPStatusError`/`TimeoutError`/`Exception` | тип не говорит, что за сбой (auth? import? export? сеть?) | оборачивать: 4xx/5xx→`CVATRequestError`, timeout→`CVATTimeoutError`, connect→`CVATConnectionError`, пустой zip→`CVATExportError` |

Принцип: **точечно — у причины (узкий `except` + типизированный `raise from`), широко — только один safety-net на границе задачи, который логирует `exc_info=True` и пишет `error_code`.** Голых `except: pass` без `logger.warning(exc_info=True)` быть не должно — это главные «немые» баги.

---

## 3. Кросс-сквозная корреляция и формат (делается один раз, работает во всех контейнерах)

- **Контекст в каждой строке.** `logging.Filter` + `contextvars`, инъекция `uid`, `stage`, `attempt`, `task_id`. Обновить `LOG_FORMAT` в `app/core/logging.py`:
  `%(asctime)s | %(levelname)-8s | uid=%(uid)s stage=%(stage)s try=%(attempt)s | %(name)s | %(message)s`.
  Бинд в начале задачи; для `--pool=prefork` инициализировать хендлер/фильтр в сигнале `worker_process_init` (после fork).
- **`print()` → `logger`.** В `graph/core` (128), `skeleton_extension` (185), `pipe_segmentation` (281), `yolo_detector` (4). Быстрый промежуточный шаг: мост `stdout→logging` на время задачи (перехват + тег), затем миграция на именованные логгеры (важно: имя логгера = `модуль:строка` → сразу видно «где»).
- **Сторонние логгеры — развязать уровни, точечно (см. §7.4).** Сейчас приглушены только httpx/httpcore/uvicorn.access/PIL. Развязка:
  - `LOG_LEVEL` — управляет **только нашими** логгерами (`app.*`, `worker.*`, `modules.*`); его бампаешь в DEBUG, чтобы видеть свой пер-тайл/пер-узел детейл (он тегированный и коррелированный — безопасно).
  - `LOG_LEVEL_LIBS` (default WARNING) — базовый потолок для шумного семейства (`ultralytics`, `torch`, `sahi`, `surya`, `paddle`, `transformers`).
  - `LOG_OVERRIDES="sahi=DEBUG,surya=INFO"` — точечно поднять **один** логгер, не разворачивая весь firehose (дебажишь тайлинг → включаешь только `sahi`). Применяется последним в `setup_logging`.
  - (лучший вариант) **пер-задачный тумблер**: kwarg задачи `debug_libs=["sahi"]` (или флаг на диаграмме) поднимает уровень только на время ЭТОГО прогона и сбрасывает в `finally`. `--pool=prefork` делает это безопасным (в дочернем процессе одновременно одна задача) → можно перезапустить одну упавшую диаграмму с детальным логом библиотеки, не трогая прод и не рестартя контейнер.
  - SAHI `verbose=0` в detection → связать с уровнем логгера `sahi`.
- **`celery_task_id` наружу.** Добавить в `ProcessingStageResponse` (+ в клиентский отчёт об ошибке). Тогда ты грепаешь `docker logs pid_worker | grep <task_id>`.
- **Баннер стадии.** Одна INFO-строка в начале каждой стадии со всеми входами (пути артефактов, размеры картинки, `model_id`, device, ключевые параметры/пороги) и одна в конце (duration + метрики). Единообразно во всех задачах — это то, что при заходе в логи сразу отвечает «с чем стадия работала».

---

## 4. Какие этапы расширить по логам (ранжировано)

**Tier 1 — почти слепые, расширять в первую очередь:**

| Этап | Контейнер | Сейчас | Добавить для дебага |
|---|---|---|---|
| **CVAT** | worker + api | 0 логов | пер-операция (create_project/create_task/wait_job/import/export): http-статус, кусок тела, попытка, тайминг, task/job id; типизированные ошибки (§1) |
| **GRAPH** | worker | 128 print / 0 logger | logger+uid; счётчики по фазам (bridge preproc, extract_nodes, trace_edges), прогресс трассировки, отказ идентификации узла (DEBUG); тип `GraphBuildError` |
| **SKELETON** (обе) | worker | 185 print / 0 logger | logger+uid по 6 внутр. шагам; явные fallback'и (bg-mask/refine/dispatch) с причиной; вместо `bool success` — детальный результат |
| **SEGMENTATION** | worker | 281 print / 27 logger | баннер (веса/тайлы/device); прогресс батчей (DEBUG); **WARN на пустые positions / нулевую coverage** (немой silent-success); OOM с индексом тайла |
| **DETECTION** | worker | 4/0 модуль + 26 print задача | logger+uid; старт/конец каждой модели ансамбля + count; SAHI-тайлинг на DEBUG; WBF in/out; тип `DetectionError` |
| **FRAME / UPLOAD** | api | нет logger, нет stage | logger + start_stage/fail_stage; путь/размер файла, параметры PDF-рендера; ошибки save/commit |

**Tier 2 — частично есть, добить контекст:**

| Этап | Контейнер | Добавить |
|---|---|---|
| **OCR** | worker_ocr | обернуть Surya/Paddle в try/except+logger; границы батчей распознавания; причины отброса блоков (DEBUG); баннер модель/device/версии |
| **JUNCTION** | worker | баннер (веса/tile grid); обернуть `run_inference` → OOM с индексом тайла |
| **CONTOURS/SAM2** | worker | прогресс per-object (DEBUG); обернуть `predict_batch` → какой объект упал |
| **DIRECTION** | worker | причины `crop=None`; пер-элементные отказы predict; баннер |
| **FXML** | worker | обернуть JSON parse/generate/write в типизированные ошибки; баннер (путь graph.json, counts) |

---

## 5. Карта «этап → контейнер» (где смотреть) и золотой путь дебага

**Где какие логи:**
- **api**: upload, frame_removal, эндпойнты CVAT (fetch/create), validation, rollback.
- **worker**: detection (+создание CVAT-таски), direction, segmentation, skeleton (обе), junction, graph, contours, fxml.
- **worker_ocr**: ocr.
- **cvat_server**: внутренности самого CVAT (если дебажишь CVAT, а не наш вызов).

**Золотой путь после изменений:**
1. Клиент шлёт отчёт: `uid`, `stage`, `attempt`, `celery_task_id`, `error_code`, контейнер, версия клиента, трейсбек.
2. Ты: `docker logs <контейнер> 2>&1 | grep <uid>` (или task_id) → видишь **баннер стадии** (входы/параметры), прогресс, и **строку отказа с `exc_info` и error_code**.
3. Сразу ясно: какой этап (тег/контейнер), где (имя логгера = модуль/строка), почему (тип ошибки + cause chain + предшествующие строки).

---

## 6. Порядок внедрения

1. **`app/core/errors.py` + корреляция + формат** (фундамент; трейсбек на ERROR `exc_info=True`; `celery_task_id` в `/stages`). Сразу делает любые логи атрибутируемыми.
2. **Сузить сетку** (§2) — начиная с немых `except: pass` (segmentation 91/131/135, graph 246) и CVAT.
3. **CVAT-логирование + типы** (Tier 1, highest value для дебага).
4. **print→logger** в graph/skeleton/segmentation/detection + баннеры стадий.
5. **Сторонние логгеры по уровням** + под-детализация на DEBUG.
6. Tier 2 добивка.

---

## 7. Решения (зафиксировано)

1. **`error_code` — колонкой** в `processing_stages` ✅. Мелкая alembic-миграция (`error_code String(64) nullable`), пишется в `fail_stage`/`set_diagram_error` как `getattr(exc, "code", type(exc).__name__)`, добавляется в `ProcessingStageResponse` и в клиентский отчёт.
2. **Миграция типов — поэтапно, но полная** ✅. Порядок волн: (1) CVAT + инференс/OOM + входные артефакты; (2) graph/skeleton/segmentation/ocr/fxml/direction; (3) добить оставшиеся `ValueError/RuntimeError/Exception` по всему пайплайну. На каждой волне `raise <DomainError>(...) from exc` в точке отказа + сужение соответствующего `except`.
3. **`print→logger` — мост сейчас** ✅. На старте задачи включать `stdout/stderr → logging`-мост (перехват + тег `uid/stage`), чтобы 600 print сразу стали атрибутируемыми; затем помодульно переписывать на именованные логгеры (имя логгера = «где»). Мост снимается по мере миграции.
4. **Сторонние либы — развязанные уровни, точечно** ✅ (детали в §3): `LOG_LEVEL` только для наших логгеров; `LOG_LEVEL_LIBS`=WARNING по умолчанию; `LOG_OVERRIDES` для поднятия одного логгера; пер-задачный `debug_libs=[...]` со сбросом в `finally`. Глобальный `LOG_LEVEL=DEBUG` на бою **не используем** (firehose от torch/surya, забивает диск, бьёт по перфу горячих циклов, всё-или-ничего).

Готово к реализации. Первая волна (фундамент): `app/core/errors.py` + `error_code`-миграция + корреляция (`contextvars`-фильтр, формат) + `celery_task_id` в `/stages` + stdout-мост + трейсбек на ERROR `exc_info=True`.

"""
Progress Model — детерминированный прогресс пайплайна из строк ProcessingStage.

Чистый Python (без Qt) — тестируется headless (без QApplication).
Источник данных: APIClient.get_stages(uid) / GET /api/diagrams/{uid}/stages.

Прогресс — по-этапный (канон пайплайна), взвешенный по бюджетам стадий.
ETA — динамическая: считается на клиенте из elapsed текущей стадии + бюджетов
оставшихся; калибруется по фактическим длительностям уже завершённых стадий
ЭТОЙ ЖЕ диаграммы. Никаких обращений к серверу сверх опроса /stages, который
клиент и так делает (без нагрузки на сервер).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


# Канонический порядок авто-пайплайна (значения ProcessingStage.stage_type).
# Совпадает с _STAGE_TYPE_TO_KEY/_STAGE_DONE_STATUS в diagram_workspace.
# Три стадии-призрака (`upload`, `mask_validation`, `graph_validation`) убраны:
# строк `ProcessingStage` с такими типами не заводит НИКТО (`StageType.UPLOAD`/
# `MASK_VALIDATION`/`GRAPH_VALIDATION` не встречаются ни в `app/`, ни в
# `worker/`), а вес в знаменателе они держали — процент структурно не доходил
# до 100 (замер 0.9). `direction_classification` наоборот добавлена: строку она
# заводит (`worker/tasks/direction.py`), а в каноне её не было.
# Порядок пары «финал скелета / перекрёстки» — как в конвейере: финальную
# скелетизацию диспетчит `task_skeletonize_simple`, и только она зовёт
# `task_detect_junctions` (`worker/tasks/skeleton.py`). Обратный порядок врал ETA.
_PIPELINE = [
    "frame_removal",
    "detection",
    "cvat_validation",
    "direction_classification",
    "segmentation",
    "skeletonization",
    "final_skeletonization",
    "junction_classification",
    "graph_building",
    "contour_extraction",
    "layout",
    "ocr",
    "fxml_generation",
]

# Человекочитаемые подписи фаз (для лейбла прогресс-бара).
_STAGE_LABELS = {
    "frame_removal": "Очистка рамки",
    "detection": "Поиск элементов",
    "cvat_validation": "Проверка элементов",
    "direction_classification": "Классификация направления",
    "segmentation": "Выделение труб",
    "skeletonization": "Скелетизация",
    "final_skeletonization": "Скелетизация (финал)",
    "junction_classification": "Проверка узлов",
    "graph_building": "Сборка схемы",
    "contour_extraction": "Контуры элемента",
    "layout": "Раскладка схемы",
    "ocr": "Распознавание текста",
    "fxml_generation": "Экспорт",
}

# Русские подписи под-шагов (Волна B): current_step из /stages → статусбар.
# Неизвестное имя показываем как есть (тех-имя, как в логах воркера).
_STEP_LABELS = {
    # канон задач
    "load_inputs": "чтение входов",
    "load_model": "загрузка модели",
    "compute": "вычисление",
    "postprocess": "постобработка",
    "persist_artifacts": "сохранение результатов",
    # под-под-шаги COMPUTE (модули)
    "tiling": "нарезка на тайлы",
    "inference": "прогон модели",
    "stitch": "сшивка результата",
    "fusion": "слияние детекций",
    "extract_points": "извлечение точек",
    "skeletonize": "скелетизация",
    "bfs": "соединение концов",
    "load_masks": "чтение масок",
    "bridge_preprocess": "обработка мостов",
    "prepare_tracing": "подготовка трассировки",
    "trace_edges": "трассировка линий",
    "text_detect": "поиск текста",
    "recognize": "распознавание",
    "postfilter": "фильтрация",
}


def substep_status_line(stages) -> str:
    """Строка статусбара «Стадия · под-шаг» (Волна B, current_step).

    Берётся самая ранняя по пайплайну БЕГУЩАЯ АВТО-стадия с заполненным
    ``current_step`` (ручные пропускаем — там ждём оператора; авто без
    current_step — ещё не отрепортила или старый сервер). Нет такой → "".
    Чистый Python — тестируется headless.
    """
    latest = {}
    for s in stages or []:
        st = s.get("stage_type")
        if st:
            latest[st] = s
    for st in _PIPELINE:
        s = latest.get(st)
        if _status_of(s) != "running":
            continue
        if _DEFAULT_BUDGETS.get(st) is None:
            continue  # ручная стадия
        step = s.get("current_step")
        if step:
            return f"{_STAGE_LABELS.get(st, st)} · {_STEP_LABELS.get(step, step)}"
    return ""


# Дефолтные бюджеты стадий (сек) — ПРОВИЗОРНЫЕ, тюнятся с первых прогонов.
# None = ручная/await-стадия (оператор): в ETA не учитываем, в проценте — номинал.
_DEFAULT_BUDGETS = {
    "frame_removal": None,
    "detection": 60,
    "cvat_validation": None,
    # Сид направления: на dev-прогонах стадия занимала 0.3 и 0.8 с
    # (MEASUREMENTS §34.5, §35.2), запас на CPU-only — тот же множитель, что у
    # соседей. Число провизорное: с шестого наблюдения его перекрывает p50
    # сервера (`app/api/stats.py`).
    "direction_classification": 5,
    "segmentation": 90,
    "skeletonization": 20,
    "final_skeletonization": 20,
    "junction_classification": 30,
    "graph_building": 25,
    "contour_extraction": 40,
    # ПРОВИЗОРНО. Замер на dev-CPU: 936 узлов — 72 с. Боевой CPU-only
    # ожидаемо в 2-5 раз медленнее, модели время-от-размера нет.
    # Перемерять по docs/AUTO_LAYOUT_E2E_TESTPLAN.md §5.
    "layout": 180,
    "ocr": 45,
    "fxml_generation": 15,
}

# Номинальный вес ручной стадии в проценте (у неё budget=None).
_MANUAL_WEIGHT = 15.0
# Доля веса бегущей ручной стадии (не знаем elapsed-бюджета → фиксируем середину).
_MANUAL_RUNNING_FRAC = 0.5
# Потолок вклада бегущей авто-стадии, пока не пришёл её completed.
_RUNNING_CAP = 0.95
# Границы калибровочного коэффициента ETA.
_CAL_MIN, _CAL_MAX = 0.5, 2.0


@dataclass
class ProgressState:
    """Снимок прогресса для UI."""
    state: str                      # "idle" | "running" | "failed" | "completed"
    percent: int                    # 0..100
    phase_label: str                # подпись текущей фазы
    running_stage: Optional[str]    # stage_type бегущей стадии (или None)
    eta_seconds: Optional[int]      # None — неизвестно / ждёт оператора
    stage_percent: Optional[int] = None  # % своей бегущей авто-стадии; None у ручной/нет бегущей
    failed_stage: Optional[str] = None
    stage_percents: Optional[dict] = None  # {stage_type: %} КАЖДОЙ бегущей авто-стадии (параллельно-безоп.)


def _parse_dt(value) -> Optional[datetime]:
    """ISO-строка (или datetime) → naive datetime; мусор → None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.fromisoformat(s.split(".")[0])
        except ValueError:
            return None


def _weight(stage_type: str, budgets: dict) -> float:
    """Вес стадии в проценте: бюджет сек, ручная (None) → номинал."""
    b = budgets.get(stage_type, None)
    return float(b) if b else _MANUAL_WEIGHT


def _status_of(stage_row) -> str:
    return (stage_row.get("status") or "").lower() if stage_row else "pending"


def _calibration(latest: dict, budgets: dict) -> float:
    """Коэффициент скорости этой машины по завершённым авто-стадиям.

    Медиана (факт/бюджет) по completed-стадиям с известным бюджетом; клампится
    в [0.5, 2.0]. Нет данных → 1.0. Делает ETA динамичной без сервера.
    """
    ratios = []
    for st, s in latest.items():
        if _status_of(s) != "completed":
            continue
        b = budgets.get(st)
        actual = s.get("duration_seconds")
        if b and actual and actual > 0:
            ratios.append(actual / b)
    if not ratios:
        return 1.0
    ratios.sort()
    mid = len(ratios) // 2
    median = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2
    return max(_CAL_MIN, min(_CAL_MAX, median))


def compute_progress(stages, *, now: Optional[datetime] = None,
                     budgets: Optional[dict] = None) -> ProgressState:
    """Построить ProgressState из списка строк ProcessingStage (как из /stages).

    stages: list[dict] с полями stage_type/status/started_at/duration_seconds.
    now: точка отсчёта для elapsed/ETA (по умолчанию utcnow) — параметр для тестов.
    budgets: перекрытие бюджетов (напр. с будущего /api/stats/stage-durations).
    """
    merged = dict(_DEFAULT_BUDGETS)
    if budgets:
        merged.update(budgets)
    if now is None:
        now = datetime.utcnow()

    # Последняя попытка по каждому stage_type.
    latest: dict = {}
    for s in stages or []:
        st = s.get("stage_type")
        if st:
            latest[st] = s

    total_w = 0.0
    done_w = 0.0
    running_stage: Optional[str] = None
    running_elapsed = 0.0
    running_frac = 0.0
    running_is_auto = False
    running_percents: dict = {}
    failed_stage: Optional[str] = None

    for st in _PIPELINE:
        w = _weight(st, merged)
        total_w += w
        s = latest.get(st)
        status = _status_of(s)

        if status == "completed":
            done_w += w
        elif status == "running":
            started = _parse_dt(s.get("started_at"))
            elapsed = (now - started).total_seconds() if started else 0.0
            _elapsed = elapsed if elapsed > 0 else 0.0
            b = merged.get(st)
            if b:
                frac = min(max(_elapsed / b, 0.0), _RUNNING_CAP)
                running_percents[st] = int(round(frac * 100))
                _is_auto = True
            else:
                frac = _MANUAL_RUNNING_FRAC
                _is_auto = False
            done_w += w * frac
            # foreground = САМАЯ РАННЯЯ бегущая (для label/ETA/одиночного stage_percent).
            # Параллельные (напр. OCR при graph_building) идут в done_w и в
            # running_percents (каждая льёт СВОЮ кнопку), но foreground не перебивают —
            # иначе кнопка graph не заливалась бы (§9 #15).
            if running_stage is None:
                running_stage = st
                running_elapsed = _elapsed
                running_frac = frac
                running_is_auto = _is_auto
        elif status == "failed":
            # Самый дальний по пайплайну упавший этап.
            failed_stage = st
        # skipped / pending → вклад 0

    percent = int(round(100.0 * done_w / total_w)) if total_w else 0

    fxml_done = _status_of(latest.get("fxml_generation")) == "completed"
    if fxml_done:
        percent = 100
    percent = max(0, min(100, percent))

    # Состояние.
    if failed_stage is not None:
        state = "failed"
    elif fxml_done:
        state = "completed"
    elif running_stage is not None:
        state = "running"
    else:
        state = "idle"

    # ETA — только для бегущей авто-стадии.
    eta_seconds: Optional[int] = None
    if running_stage is not None and merged.get(running_stage):
        k = _calibration(latest, merged)
        remaining = max(merged[running_stage] - running_elapsed, 0.0)
        idx = _PIPELINE.index(running_stage)
        for st in _PIPELINE[idx + 1:]:
            if _status_of(latest.get(st)) == "completed":
                continue
            b = merged.get(st)
            if b:
                remaining += b
        eta_seconds = int(round(k * remaining))

    # Подпись фазы.
    if state == "running":
        phase_label = _STAGE_LABELS.get(running_stage, running_stage)
    elif state == "failed":
        phase_label = f"Ошибка — {_STAGE_LABELS.get(failed_stage, failed_stage)}"
    elif state == "completed":
        phase_label = "Готово"
    else:
        phase_label = "Ожидание"

    stage_percent = (
        int(round(running_frac * 100))
        if running_stage is not None and running_is_auto else None
    )

    return ProgressState(
        state=state,
        percent=percent,
        phase_label=phase_label,
        running_stage=running_stage,
        eta_seconds=eta_seconds,
        stage_percent=stage_percent,
        failed_stage=failed_stage,
        stage_percents=running_percents,
    )


def format_eta(seconds: Optional[int]) -> str:
    """ETA сек → короткая подпись: '~1 мин 20 с' / '~15 с' / '' если None."""
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"~{seconds} с"
    minutes, sec = divmod(seconds, 60)
    if sec:
        return f"~{minutes} мин {sec} с"
    return f"~{minutes} мин"

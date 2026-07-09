"""
Доменная иерархия ошибок пайплайна (Волна 0 — фундамент).

Базовый ``PipelineError`` несёт корреляцию (``stage`` / ``diagram_uid`` / ``step``)
и стабильный ``code``, который позже пишется в ``processing_stages.error_code`` и
в строку лога. Листовые типы добавляются в своих волнах по мере надобности
(см. RUNBOOK §5) — здесь только минимальный скелет семейств.
"""

from typing import Optional


class PipelineError(Exception):
    """Базовая ошибка пайплайна.

    Attributes:
        code: стабильный доменный код (пишется в ``error_code`` и в лог).
        message: человекочитаемое сообщение.
        stage: StageType/фаза, где произошёл сбой.
        diagram_uid: диаграмма — для грепаемости по логам.
        step: под-шаг; проставляется ``obs.step()`` при всплытии.
        cause: исходное исключение (при оборачивании неожиданного).
    """

    code: str = "pipeline_error"

    def __init__(
        self,
        message: str,
        *,
        stage: Optional[str] = None,
        diagram_uid: Optional[str] = None,
        step: Optional[str] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.diagram_uid = diagram_uid
        self.step = step
        self.cause = cause


# --- Артефактный I/O ---------------------------------------------------------

class ArtifactMissingError(PipelineError):
    """Ожидаемый входной артефакт не найден на диске."""

    code = "artifact_missing"


class ArtifactWriteError(PipelineError):
    """Не удалось сохранить артефакт фазы."""

    code = "artifact_write_failed"


# --- Модель / инференс -------------------------------------------------------

class ModelLoadError(PipelineError):
    """Не удалось загрузить веса/модель."""

    code = "model_load_failed"


class InferenceError(PipelineError):
    """Сбой во время инференса модели."""

    code = "inference_failed"


class GpuOutOfMemoryError(InferenceError):
    """OOM на устройстве во время инференса."""

    code = "gpu_oom"


# --- Конфигурация проекта/модели (Волна 3) -----------------------------------

class ConfigError(PipelineError):
    """Неверная/отсутствующая конфигурация проекта или модели.

    Пример (detection): проект не найден, или модель детекции не `ensemble`,
    или неизвестная стратегия слияния ансамбля. Ожидаемое предусловие — сбой
    видимый и типизированный, а не сырой ``ValueError``.
    """

    code = "config_invalid"


# --- Скелетизация (Волна 3 — skeleton/graph) ---------------------------------

class SkeletonizationError(PipelineError):
    """Сбой вычисления скелета в COMPUTE-под-шаге.

    Поднимается, когда вход есть, но `skeleton_extension` вернул неуспех, либо
    скелет не создан/нечитаем. Отличается от `ArtifactMissingError` (нет ВХОДА):
    здесь вход валиден, но вычисление не дало корректного выхода. Граф своего
    листа не заводит — внутренние сбои `builder.build()` типизирует `obs.step`.
    """

    code = "skeletonization_failed"


# --- CVAT (Волна 1) ----------------------------------------------------------

class CVATError(PipelineError):
    """Базовая ошибка интеграции с CVAT."""

    code = "cvat_error"


class CVATConnectionError(CVATError):
    """Не удалось соединиться с CVAT."""

    code = "cvat_connection"


class CVATTimeoutError(CVATError):
    """Таймаут при обращении к CVAT."""

    code = "cvat_timeout"


class CVATRequestError(CVATError):
    """CVAT вернул ошибочный HTTP-статус (4xx/5xx)."""

    code = "cvat_request"


class CVATImportError(CVATError):
    """Сбой импорта аннотаций в CVAT."""

    code = "cvat_import"


class CVATExportError(CVATError):
    """Сбой экспорта/скачивания аннотаций из CVAT."""

    code = "cvat_export"


# --- Состояние пайплайна -----------------------------------------------------

class StageStateError(PipelineError):
    """Операция запрошена в неверном статусе диаграммы (нарушено предусловие).

    Пример: подтверждение bbox, когда статус уже `skeletonizing`, а не
    `validating_bbox`. Ожидаемая ситуация — логируется как warning, без traceback.
    """

    code = "stage_state_invalid"

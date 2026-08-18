"""Фоновая загрузка артефактов диаграммы для вкладок клиента.

Один класс на все вкладки: чем именно грузить, задаётся списком заданий
(`Job`), а не отдельным загрузчиком на вкладку. До пункта 0.5 копий было
четыре (graph / junction / pipe / ocr), и правка в загрузке — новый артефакт,
фолбэк, обработка сбоя — делалась четыре раза.

Живёт в отдельном QThread: вкладка создаёт загрузчик, перекладывает его в поток
и слушает `finished` / `error` / `progress` (образец — `BaseGraphTab._download_artifacts`).

Контракт заданий:
  * `Job.fetches` — цепочка кандидатов, берётся первый скачавшийся
    (`*_validated` → обычный артефакт);
  * `Job.required` — ни один не скачался → вкладка получает `error`;
  * `Job.swallow` — что глотать у необязательного задания. ⛔ У графовой вкладки
    это `(APIError,)`, а не «всё»: тихо потерянный `graph_canvas` означает
    «холст пересобран заново, прежние правки не сохранятся», тихо потерянный
    `contours_validated` — выброшенные правки оператора;
  * `Job.failure_key` — не-404 `APIError` кладёт в артефакты `{failure_key: True}`
    («состояние неизвестно»), чтобы вкладка не спутала сбой сети с «артефакта нет».
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from PySide6.QtCore import QObject, Signal

from ui.services.api_client import APIError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fetch:
    """Один кандидат: чем тянуть, куда класть, под каким ключом отдать вкладке."""

    filename: str
    key: str
    #: тип артефакта для `APIClient.download_artifact`
    art_type: str | None = None
    #: имя метода `APIClient(uid, dest)` вместо download_artifact (OCR-эндпоинты)
    method: str | None = None

    @property
    def source(self) -> str:
        """Как этот кандидат назвать в логе."""
        return self.art_type or self.method or self.key


@dataclass(frozen=True)
class Job:
    """Одно задание загрузчика: цепочка кандидатов и политика на их отказ."""

    fetches: tuple[Fetch, ...]
    required: bool = False
    swallow: tuple[type[BaseException], ...] = (Exception,)
    failure_key: str | None = None


def artifact(art_type: str, filename: str, key: str | None = None) -> Fetch:
    """Кандидат «обычный артефакт»; по умолчанию ключ = тип артефакта."""
    return Fetch(filename=filename, key=key or art_type, art_type=art_type)


def endpoint(method: str, filename: str, key: str) -> Fetch:
    """Кандидат «свой метод APIClient(uid, dest)»."""
    return Fetch(filename=filename, key=key, method=method)


def one(fetch: Fetch, **kwargs) -> Job:
    """Задание из одного кандидата."""
    return Job(fetches=(fetch,), **kwargs)


class ArtifactDownloader(QObject):
    """Скачивает список заданий параллельно и отдаёт словарь «ключ → путь»."""

    finished = Signal(dict)
    error = Signal(str)
    progress = Signal(str)

    def __init__(self, api_client, uid: str, temp_dir: Path,
                 jobs: Sequence[Job]):
        super().__init__()
        self.api_client = api_client
        self.uid = uid
        self.temp_dir = Path(temp_dir)
        self.jobs = tuple(jobs)

    # -- один кандидат ----------------------------------------------------

    def _fetch(self, f: Fetch) -> Path:
        dest = self.temp_dir / f.filename
        if f.method:
            getattr(self.api_client, f.method)(self.uid, dest)
        else:
            self.api_client.download_artifact(self.uid, f.art_type, dest)
        return dest

    # -- одно задание -----------------------------------------------------

    def _run_job(self, job: Job) -> tuple[dict, str | None]:
        """Вернуть (что положить в артефакты, ключ для строки прогресса)."""
        last: BaseException | None = None
        for i, f in enumerate(job.fetches):
            try:
                dest = self._fetch(f)
            except Exception as exc:  # noqa: BLE001 — следующий кандидат цепочки
                last = exc
                if i + 1 < len(job.fetches):
                    logger.info("артефакт %s недоступен (%s), пробую следующий",
                                f.source, exc)
                    continue
                break
            logger.info("артефакт %s загружен как %s", f.source, f.key)
            return {f.key: dest}, f.key

        assert last is not None
        if job.required:
            raise last
        if not isinstance(last, job.swallow):
            raise last
        if job.failure_key and isinstance(last, APIError) and last.status_code != 404:
            # 404 = артефакта легитимно нет; всё прочее (сеть, 5xx) —
            # «неизвестно». Разница критична: спутать сбой с «артефакта нет»
            # = объявить холст устаревшим и выбросить правки оператора.
            logger.warning("%s не скачался (%s) — состояние неизвестно",
                           job.fetches[-1].source, last)
            return {job.failure_key: True}, None
        logger.info("необязательный артефакт %s недоступен (%s)",
                    job.fetches[-1].source, last)
        return {}, None

    # -- прогон -----------------------------------------------------------

    def run(self):
        try:
            self.progress.emit("Загрузка артефактов...")
            artifacts: dict[str, Any] = {}
            with ThreadPoolExecutor(max_workers=max(1, len(self.jobs))) as pool:
                futures = [pool.submit(self._run_job, job) for job in self.jobs]
                for future in as_completed(futures):
                    found, done_key = future.result()
                    artifacts.update(found)
                    if done_key:
                        self.progress.emit(f"Загружен {done_key}")
            self.finished.emit(artifacts)
        except Exception as exc:  # noqa: BLE001 — любой сбой уходит во вкладку
            self.error.emit(str(exc))

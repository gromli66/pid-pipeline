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
    и у вкладки привязки (пункт 1.x10) это `(APIError,)`, а не «всё»: тихо
    потерянный `graph_canvas` означает «холст пересобран заново, прежние правки
    не сохранятся», тихо потерянный `contours_validated` — выброшенные правки
    оператора, тихо потерянный `ocr_binding` — привязки, которые вкладка тут же
    затрёт пустотой при первом сохранении. Граница по `APIError` не случайна:
    сетевые сбои и коды HTTP завёрнуты в него, а запись скачанного в файл лежит
    ЗА обёрткой, поэтому не-`APIError` = «на диск не легло». С пункта 1.x12
    это правило стоит у всех четырёх вкладок. ⚠ Политика копируется ВМЕСТЕ
    с заданием и теряется молча — новое необязательное задание обязано
    получить её явно;
  * `Job.failure_key` — не-404 `APIError` кладёт в артефакты `{failure_key: True}`
    («состояние неизвестно»), чтобы вкладка не спутала сбой сети с «артефакта нет».
    То же и для ЦЕПОЧКИ (пункт 1.23): фолбэк, взятый после не-404 отказа
    предпочтённого кандидата, взят вслепую — предпочтённый артефакт мог лежать
    на сервере. Флаг приезжает вместе с фолбэком, а не вместо него.
  * `Job.silent_ok` — явная запись «глотаем осознанно» с причиной, ВМЕСТО
    `failure_key` (пункт 1-41). Задание, способное смолчать об отказе, обязано
    нести одно из двух: иначе оно не строится (`Job.__post_init__`). Это и есть
    замок семьи — раньше он был перечнем, а перечень пять раз оказывался неполон.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from PySide6.QtCore import QObject, Signal

from ui.services.api_client import APIError
from ui.services.thread_lifetime import Stopped

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
    """Одно задание загрузчика: цепочка кандидатов и политика на их отказ.

    ⛔ **Задание, способное СМОЛЧАТЬ об отказе, обязано объявить, что с этим
    молчанием делать** (пункт 1-41). Пять поколений семьи чинили один и тот же
    дефект в разных вкладках, и каждое опиралось на перечень «таких мест ровно
    N», снятый грепом; каждое следующее находило, что перечень был неполон.
    Поэтому политика спрашивается у САМОГО задания, а не у перечня: либо
    `failure_key` (вкладка узнаёт об отказе и запирает слепую запись), либо
    `silent_ok` — явная запись «глотаем осознанно» с причиной. Ни того, ни
    другого — задание не строится. Ошибка в N перестала быть дефектом:
    новое задание попадает под то же требование само.
    """

    fetches: tuple[Fetch, ...]
    required: bool = False
    swallow: tuple[type[BaseException], ...] = (Exception,)
    failure_key: str | None = None
    #: почему молчание об отказе законно — ставится ВМЕСТО `failure_key`
    silent_ok: str | None = None

    @property
    def can_be_silent(self) -> bool:
        """Может ли отказ не дойти до вкладки.

        Две формы молчания, и обе одинаково дороги: взятый фолбэк (цепочка
        длиннее одного кандидата — оператору открывают НЕ ЕГО работу) и
        проглоченный отказ необязательного задания. Обязательное задание
        из одного кандидата поднимает отказ наверх — смолчать не может.
        """
        return len(self.fetches) > 1 or (not self.required and bool(self.swallow))

    def __post_init__(self):
        if self.failure_key and self.silent_ok:
            raise ValueError(
                f"задание {self.fetches[0].source}: `failure_key` и `silent_ok` "
                "вместе — противоречие, а не усиление")
        if self.can_be_silent and not (self.failure_key or self.silent_ok):
            raise ValueError(
                f"задание {self.fetches[0].source} способно смолчать об отказе "
                "и не объявило политику: нужен либо `failure_key`, либо "
                "`silent_ok='почему молчание законно'` (пункт 1-41)")


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
        #: чем кончился прогон. Вкладке важен КЛАСС: `APIError` = отказ сервера
        #: (может пройти со следующей попытки), не-`APIError` = скачанное не
        #: легло на локальный диск, повторять нечего.
        self.last_error: BaseException | None = None
        #: Просьба остановиться — от `ui/services/thread_lifetime.py`
        #: (пункт 1-50). Вкладку разрушают «← Назад», а качать дальше некому
        #: и некуда. Флаг снимает ОСТАВШИЕСЯ задания; текущий HTTP-запрос
        #: дорабатывает сам — прервать его отсюда нельзя, и это названная
        #: граница, а не недосмотр.
        self._stop = threading.Event()

    def stop(self):
        """Не начинать новых загрузок: владелец разрушен или клиент уходит."""
        self._stop.set()

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
        #: предпочтённый кандидат отказал НЕ по 404 — он мог быть на месте
        preferred_unknown = False
        for i, f in enumerate(job.fetches):
            if self._stop.is_set():
                raise Stopped("загрузка прекращена — вкладки больше нет")
            try:
                dest = self._fetch(f)
            except Exception as exc:  # noqa: BLE001 — следующий кандидат цепочки
                last = exc
                if i + 1 < len(job.fetches):
                    logger.info("артефакт %s недоступен (%s), пробую следующий",
                                f.source, exc)
                    if not (isinstance(exc, APIError) and exc.status_code == 404):
                        preferred_unknown = True
                    continue
                break
            logger.info("артефакт %s загружен как %s", f.source, f.key)
            if preferred_unknown and job.failure_key:
                # Фолбэк взят вслепую: 404 = предпочтённого артефакта законно
                # нет, всё прочее = он мог быть. Молча подменить его = открыть
                # оператору чужую работу и затереть его собственную при записи.
                logger.warning("взят фолбэк %s — состояние предпочтённого "
                               "артефакта неизвестно (%s)", f.key, last)
                return {f.key: dest, job.failure_key: True}, f.key
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
        except Stopped as exc:
            # Ни `finished`, ни `error`: получателя нет — вкладку разрушили,
            # и поток обязан просто уйти (пункт 1-50).
            logger.info("загрузка %s прекращена: %s", self.uid[:8], exc)
            # ⛔ Но унести СЕБЯ работник обязан всё равно, и сам, в СВОЁМ
            # потоке (инвариант 1-46, замер §119а): на боевом пути это делают
            # связи `finished`/`error` → `deleteLater`, а здесь их нет —
            # иначе объект остался бы сиротой в кончившемся потоке, где
            # `deleteLater()` уже не доставляется вовсе (§119б).
            self.deleteLater()
        except Exception as exc:  # noqa: BLE001 — любой сбой уходит во вкладку
            self.last_error = exc
            self.error.emit(str(exc))

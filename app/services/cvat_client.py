"""
CVAT Client - взаимодействие с CVAT API.

Синхронная версия с persistent connection для Celery workers и API.
"""

import functools
import httpx
import time
from contextlib import contextmanager
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

from app.config import settings
from app.core.logging import get_logger
from app.core.errors import (
    CVATConnectionError,
    CVATError,
    CVATExportError,
    CVATImportError,
    CVATRequestError,
    CVATTimeoutError,
)

logger = get_logger(__name__)


@contextmanager
def _cvat_op(op: str, *, wrap: Optional[type] = None, step: Optional[str] = None, **fields):
    """httpx-ошибки CVAT → доменные CVAT-типы + строка лога.

    Доменные (CVATError) пропускаем как есть; `wrap` переопределяет тип для
    конкретной операции (import/export); не-httpx/не-CVAT — наверх (это баги).
    `step` (если задан) проставляется на исключение → доезжает до `failed_step`
    строки `cvat_validation` (напр. `upload_media` vs `task_shell`, RUNBOOK §8.4).
    """
    t0 = time.perf_counter()
    try:
        yield
    except CVATError:
        raise
    except httpx.HTTPStatusError as exc:
        cls = wrap or CVATRequestError
        status = exc.response.status_code
        body = exc.response.text[:200]
        # op/status/body — в ТЕКСТ сообщения (видно в docker logs) и в extra (для JSON-стока).
        logger.error(
            f"cvat.error op={op} → HTTP {status} body={body!r}",
            extra={"op": op, "event": "error", "code": cls.code,
                   "http_status": status, "body": exc.response.text[:500],
                   "duration_ms": round((time.perf_counter() - t0) * 1000), **fields},
            exc_info=True)
        # Причина CVAT (body) — и в исключении → доходит до клиента через HTTPException.
        raise cls(f"CVAT {op} → HTTP {status}: {body}", cause=exc, step=step) from exc
    except httpx.TimeoutException as exc:
        cls = wrap or CVATTimeoutError
        logger.error(f"cvat.error op={op} → timeout",
                     extra={"op": op, "event": "error", "code": cls.code, **fields}, exc_info=True)
        raise cls(f"CVAT {op}: timeout", cause=exc, step=step) from exc
    except httpx.RequestError as exc:
        cls = wrap or CVATConnectionError
        logger.error(f"cvat.error op={op} → {exc}",
                     extra={"op": op, "event": "error", "code": cls.code, **fields}, exc_info=True)
        raise cls(f"CVAT {op}: {exc}", cause=exc, step=step) from exc


def _cvat_call(op: str, *, wrap: Optional[type] = None):
    """Декоратор: обернуть сетевой метод клиента в _cvat_op."""
    def deco(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            with _cvat_op(op, wrap=wrap):
                return fn(*args, **kwargs)
        return inner
    return deco


@dataclass
class CVATLabel:
    """Метка для CVAT проекта."""
    name: str
    color: Optional[str] = None
    attributes: Optional[List[dict]] = None


class CVATClient:
    """Синхронный клиент для CVAT API v2 с persistent connection."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 120.0,
    ):
        self.base_url = (base_url or settings.CVAT_URL).rstrip("/")
        self.token = token or getattr(settings, 'CVAT_TOKEN', None)
        self.timeout = timeout
        self._session_token: Optional[str] = None
        self._csrf_token: Optional[str] = None

        # Persistent client с connection pooling
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
            ),
        )

    def close(self):
        """Закрыть HTTP соединения."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _get_headers(self, for_download: bool = False) -> Dict[str, str]:
        """Получить заголовки для запросов."""
        headers = {
            "Host": "localhost",
        }

        if for_download:
            headers["Accept"] = "*/*"
        else:
            headers["Accept"] = "application/vnd.cvat+json"

        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        elif self._session_token:
            headers["Authorization"] = f"Token {self._session_token}"

        if self._csrf_token:
            headers["X-CSRFToken"] = self._csrf_token

        return headers

    @_cvat_call("login")
    def login(self, username: str, password: str) -> str:
        """Авторизация в CVAT."""
        response = self._client.post(
            "/api/auth/login",
            headers={"Accept": "application/vnd.cvat+json", "Content-Type": "application/json"},
            json={"username": username, "password": password},
        )
        response.raise_for_status()

        data = response.json()
        self._session_token = data.get("key")

        if "csrftoken" in response.cookies:
            self._csrf_token = response.cookies["csrftoken"]

        return self._session_token

    def get_projects(self) -> List[dict]:
        """Получить список проектов."""
        response = self._client.get(
            "/api/projects",
            headers=self._get_headers(),
        )
        response.raise_for_status()
        return response.json().get("results", [])

    def get_project_by_name(self, name: str) -> Optional[dict]:
        """Найти проект по имени."""
        response = self._client.get(
            "/api/projects",
            headers=self._get_headers(),
            params={"search": name},
        )
        response.raise_for_status()

        results = response.json().get("results", [])
        for project in results:
            if project.get("name") == name:
                return project
        return None

    def create_project(
        self,
        name: str,
        labels: List[CVATLabel],
    ) -> int:
        """Создать проект в CVAT."""
        labels_data = []
        for label in labels:
            label_dict = {"name": label.name}
            if label.color:
                label_dict["color"] = label.color
            if label.attributes:
                label_dict["attributes"] = label.attributes
            labels_data.append(label_dict)

        response = self._client.post(
            "/api/projects",
            headers={**self._get_headers(), "Content-Type": "application/json"},
            json={
                "name": name,
                "labels": labels_data,
            },
        )
        response.raise_for_status()

        return response.json()["id"]

    def get_project_labels(self, project_id: int) -> List[dict]:
        """Получить все лейблы проекта (с учётом пагинации)."""
        labels: List[dict] = []
        page = 1
        while True:
            response = self._client.get(
                "/api/labels",
                headers=self._get_headers(),
                params={"project_id": project_id, "page": page, "page_size": 100},
            )
            response.raise_for_status()
            data = response.json()
            labels.extend(data.get("results", []))
            if not data.get("next"):
                break
            page += 1
        return labels

    def ensure_project_labels(
        self,
        project_id: int,
        labels: List[CVATLabel],
    ) -> int:
        """
        Добавить в существующий проект недостающие лейблы (по имени).

        Существующие лейблы не трогаются, ничего не удаляется. Возвращает
        количество добавленных лейблов.
        """
        existing_names = {lbl.get("name") for lbl in self.get_project_labels(project_id)}
        missing = [lbl for lbl in labels if lbl.name not in existing_names]
        if not missing:
            return 0

        labels_data = []
        for label in missing:
            label_dict = {"name": label.name}
            if label.color:
                label_dict["color"] = label.color
            if label.attributes:
                label_dict["attributes"] = label.attributes
            labels_data.append(label_dict)

        # CVAT v2: PATCH проекта с labels без "id" → добавляет новые лейблы
        response = self._client.patch(
            f"/api/projects/{project_id}",
            headers={**self._get_headers(), "Content-Type": "application/json"},
            json={"labels": labels_data},
        )
        response.raise_for_status()
        return len(missing)

    @_cvat_call("get_or_create_project")
    def get_or_create_project(
        self,
        name: str,
        labels: List[CVATLabel],
    ) -> int:
        """
        Получить существующий проект или создать новый.

        Если проект уже существует — синхронизирует лейблы: добавляет
        недостающие (например, новые классы детекции), не трогая существующие.
        """
        existing = self.get_project_by_name(name)
        if existing:
            project_id = existing["id"]
            try:
                added = self.ensure_project_labels(project_id, labels)
                if added:
                    print(f"[CVAT] Added {added} missing label(s) to project {project_id}")
            except Exception as exc:
                print(f"[CVAT][WARN] Could not sync labels for project {project_id}: {exc}")
            return project_id
        return self.create_project(name, labels)

    def create_task(
        self,
        project_id: int,
        name: str,
        image_path: Path,
    ) -> Tuple[int, int]:
        """Создать task и загрузить изображение.

        Две фазы обёрнуты РАЗДЕЛЬНО (RUNBOOK §8.4): оболочка задачи (POST /tasks)
        vs загрузка медиа (POST /tasks/{id}/data). Так `failed_step` строки
        `cvat_validation` отличает `upload_media` (CVAT отвергает большой файл —
        реальная жалоба) от общего сбоя создания.
        """
        image_path = Path(image_path)

        # 1. Создаём оболочку task (POST /tasks)
        with _cvat_op("create_task", step="task_shell"):
            response = self._client.post(
                "/api/tasks",
                headers={**self._get_headers(), "Content-Type": "application/json"},
                json={
                    "name": name,
                    "project_id": project_id,
                },
            )
            response.raise_for_status()
            task_id = response.json()["id"]

        # 2. Загружаем изображение (POST /tasks/{id}/data, увеличенный timeout).
        #    Болевой под-шаг upload_media: большой файл → Pillow/CVAT может отвергнуть.
        with _cvat_op("upload_media", step="upload_media"):
            headers = self._get_headers()
            with open(image_path, "rb") as f:
                files = {"client_files[0]": (image_path.name, f, "image/png")}
                response = self._client.post(
                    f"/api/tasks/{task_id}/data",
                    headers=headers,
                    files=files,
                    # use_cache=true — чанки генерируются лениво, по запросу,
                    # а не все сразу при создании задачи. Для больших цветных схем
                    # (~15000x7000) это резко ускоряет создание task.
                    data={"image_quality": 70, "use_cache": "true"},
                    timeout=self.timeout * 2,
                )
                response.raise_for_status()

            # CVAT принимает /data (202) и обрабатывает медиа в фоне; отказ
            # (большой файл → Pillow) виден как state=Failed → ловим как upload_media
            # с причиной CVAT, иначе он всплыл бы слепым таймаутом job (§9 #8, вар. b).
            self._wait_for_data(task_id)

        # 3. Ждём создания job (свой _cvat_call-wrapper)
        job_id = self._wait_for_job(task_id)

        return task_id, job_id

    def _wait_for_data(
        self,
        task_id: int,
        max_attempts: int = 60,
        delay: float = 1.0,
    ) -> None:
        """Дождаться завершения фоновой обработки медиа после POST /data.

        CVAT принимает /data (202) и обрабатывает изображение в rq-воркере;
        отказ (например, большой файл → Pillow) виден в
        ``GET /api/tasks/{id}/status`` как ``state=Failed``. Ловим его как
        ``upload_media`` с причиной от CVAT (последняя строка traceback), иначе
        сбой всплыл бы слепым таймаутом ``_wait_for_job`` (RUNBOOK §9 #8, b).
        Вызывается ВНУТРИ ``_cvat_op("upload_media")`` — httpx-ошибки опроса тоже
        атрибутируются на upload_media.
        """
        for _ in range(max_attempts):
            response = self._client.get(
                f"/api/tasks/{task_id}/status",
                headers=self._get_headers(),
            )
            response.raise_for_status()
            data = response.json()
            state = data.get("state")
            if state == "Failed":
                msg = (data.get("message") or "").strip()
                reason = msg.splitlines()[-1] if msg else "unknown"
                raise CVATRequestError(f"CVAT rejected media: {reason}", step="upload_media")
            if state == "Finished":
                return
            time.sleep(delay)
        # Терминального состояния не дождались — не роняем ложно, пусть решает _wait_for_job.

    @_cvat_call("wait_for_job")
    def _wait_for_job(
        self,
        task_id: int,
        max_attempts: int = 30,
        delay: float = 1.0,
    ) -> int:
        """Дождаться создания job для task."""
        for _ in range(max_attempts):
            response = self._client.get(
                "/api/jobs",
                headers=self._get_headers(),
                params={"task_id": task_id},
            )
            response.raise_for_status()

            jobs = response.json().get("results", [])
            if jobs:
                return jobs[0]["id"]

            time.sleep(delay)

        raise CVATTimeoutError(f"Job for task {task_id} not created after {max_attempts} attempts")

    @_cvat_call("import_annotations", wrap=CVATImportError)
    def import_annotations(
        self,
        task_id: int,
        annotations_path: Path,
        format_name: str = "YOLO 1.1",
    ) -> None:
        """Импортировать аннотации в task."""
        annotations_path = Path(annotations_path)

        headers = self._get_headers()

        with open(annotations_path, "rb") as f:
            files = {"annotation_file": (annotations_path.name, f, "application/zip")}
            response = self._client.put(
                f"/api/tasks/{task_id}/annotations",
                headers=headers,
                files=files,
                params={"format": format_name},
                timeout=self.timeout * 2,
            )
            response.raise_for_status()

    @_cvat_call("export_annotations", wrap=CVATExportError)
    def export_annotations(
        self,
        task_id: int,
        format_name: str = "COCO 1.0",
        output_path: Optional[Path] = None,
    ) -> Path:
        """
        Экспортировать аннотации из task.

        CVAT API v2 (проверено на v2.25.0):
        1. GET /api/tasks/{id}/annotations?format=...                  — запустить экспорт (202)
        2. GET /api/tasks/{id}/annotations?format=...&action=download  — скачать

        Используем /annotations (только разметка), а НЕ /dataset: картинка
        пайплайну не нужна (оригинал лежит в storage/{uid}/original/), а на
        больших схемах (~15000x7000) упаковка изображения резко замедляет
        экспорт и грузит cvat_worker_export.

        Пока экспорт готовится, CVAT 2.25 отвечает 400 "Dataset export has not
        been finished yet" (в старых версиях был 202) — это НЕ ошибка,
        продолжаем ждать.
        """
        headers = self._get_headers()

        # Шаг 1: Запросить экспорт (GET без action)
        response = self._client.get(
            f"/api/tasks/{task_id}/annotations",
            headers=headers,
            params={"format": format_name},
        )

        # 202 = экспорт запущен, 200/201 = уже готов
        if response.status_code not in (200, 201, 202):
            raise CVATExportError(f"Failed to request export: {response.status_code} {response.text[:200]}")

        # Шаг 2: Ждём готовности и скачиваем
        max_attempts = 60
        for attempt in range(max_attempts):
            time.sleep(2)

            download_headers = self._get_headers(for_download=True)

            response = self._client.get(
                f"/api/tasks/{task_id}/annotations",
                headers=download_headers,
                params={"format": format_name, "action": "download"},
                follow_redirects=True,
                timeout=self.timeout * 5,
            )

            if response.status_code == 200:
                content_type = response.headers.get("content-type", "")
                if "application/zip" in content_type or "application/octet-stream" in content_type or len(response.content) > 100:
                    break
            elif response.status_code == 202:
                # Старое поведение CVAT: экспорт ещё готовится
                continue
            elif response.status_code == 400 and "not been finished" in response.text.lower():
                # CVAT 2.25: экспорт ещё готовится — это не ошибка, ждём дальше
                continue
            else:
                raise CVATExportError(f"Export download failed: {response.status_code} {response.text[:200]}")
        else:
            raise CVATExportError(f"Export not ready after {max_attempts} attempts")

        if len(response.content) < 50:
            raise CVATExportError(f"Export returned empty: {len(response.content)} bytes")

        if output_path is None:
            output_path = Path(f"task_{task_id}_annotations.zip")

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(response.content)

        return output_path

    def get_task_status(self, task_id: int) -> str:
        """Получить статус task."""
        response = self._client.get(
            f"/api/tasks/{task_id}",
            headers=self._get_headers(),
        )
        response.raise_for_status()
        return response.json().get("status", "unknown")

    def get_job_url(self, job_id: int) -> str:
        """Получить URL для открытия job в браузере."""
        browser_url = getattr(settings, 'CVAT_BROWSER_URL', None) or "http://localhost:8080"
        return f"{browser_url}/tasks/{job_id}/jobs/{job_id}"

    def get_task_url(self, task_id: int, job_id: int) -> str:
        """Получить URL для открытия task в браузере."""
        browser_url = getattr(settings, 'CVAT_BROWSER_URL', None) or "http://localhost:8080"
        return f"{browser_url}/tasks/{task_id}/jobs/{job_id}"


# Module-level singleton
_cvat_client: Optional[CVATClient] = None


def get_cvat_client() -> CVATClient:
    """Получить singleton CVATClient с переиспользованием соединений."""
    global _cvat_client
    if _cvat_client is None:
        _cvat_client = CVATClient()
    return _cvat_client


def create_labels_from_config(project_config) -> List[CVATLabel]:
    """Метки проекта CVAT: отображаемые названия в алфавитном порядке.

    Единственный источник списка меток — зовут и API (`app/api/cvat.py`), и
    воркер (`worker/tasks/detection.py`). Раньше каждый строил свой список
    инлайном, и расхождение копий не ловил ни один тест.

    Порядок здесь — это порядок СОЗДАНИЯ меток в CVAT, а он же и порядок показа:
    CVAT метки не сортирует (проверено на v2.25.0 — ни в API, ни в UI-бандле),
    так что алфавит в интерфейсе разметчика получается только отсюда.

    Побочный эффект порядка: CVAT нумерует `category_id` в выгрузке позицией
    метки, поэтому на возврате обязательна `denormalize_coco_labels`
    (`app/api/cvat.py`) — без неё `category_id - 1` даст чужой класс.

    Цвет берётся из блока `class_colors` (ключ — английское имя класса). Без него
    цвет метки выбирает сам CVAT, и раскраска классов у разметчика меняется от
    проекта к проекту. Цвет действует при СОЗДАНИИ метки: уже существующие метки
    ни `create_project`, ни `ensure_project_labels` не перекрашивают.
    """
    from app.services.class_display import display_name, display_order

    return [
        CVATLabel(
            name=display_name(project_config, cls.name),
            color=project_config.class_colors.get(cls.name),
        )
        for cls in display_order(project_config)
    ]

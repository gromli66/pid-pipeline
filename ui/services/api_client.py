"""
API Client - HTTP клиент для взаимодействия с backend.

Persistent connection + retry при потере связи.
"""

import httpx
import time
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class DiagramStatus(str, Enum):
    """Статусы диаграммы (зеркало backend)."""
    UPLOADED = "uploaded"
    CLEANING_FRAME = "cleaning_frame"
    FRAME_CLEANED = "frame_cleaned"
    DETECTING = "detecting"
    DETECTED = "detected"
    VALIDATING_BBOX = "validating_bbox"
    VALIDATED_BBOX = "validated_bbox"
    SEGMENTING = "segmenting"
    SKELETONIZING = "skeletonizing"
    SKELETONIZED = "skeletonized"
    VALIDATING_MASKS = "validating_masks"
    VALIDATED_MASKS = "validated_masks"
    SKELETONIZING_FINAL = "skeletonizing_final"
    SKELETONIZED_FINAL = "skeletonized_final"
    DETECTING_JUNCTIONS = "detecting_junctions"
    DETECTED_JUNCTIONS = "detected_junctions"
    VALIDATING_JUNCTIONS = "validating_junctions"
    VALIDATED_JUNCTIONS = "validated_junctions"
    BUILDING_GRAPH = "building_graph"
    BUILT = "built"
    VALIDATING_GRAPH = "validating_graph"
    VALIDATED_GRAPH = "validated_graph"
    EXTRACTING_CONTOURS = "extracting_contours"
    CONTOURS_EXTRACTED = "contours_extracted"
    CONTOURS_VALIDATED = "contours_validated"
    OCR_PROCESSING = "ocr_processing"
    OCR_COMPLETED = "ocr_completed"
    OCR_BOUND = "ocr_bound"
    GENERATING_FXML = "generating_fxml"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class DiagramInfo:
    """Информация о диаграмме."""
    uid: str
    number: int
    project_code: str
    status: DiagramStatus
    filename: str
    detection_count: Optional[int] = None
    validated_detection_count: Optional[int] = None
    cvat_task_id: Optional[int] = None
    cvat_job_id: Optional[int] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class DiagramStatusInfo:
    """Статус диаграммы."""
    status: DiagramStatus
    error_message: Optional[str] = None
    error_stage: Optional[str] = None
    cvat_task_id: Optional[int] = None
    cvat_job_id: Optional[int] = None
    detection_count: Optional[int] = None
    updated_at: Optional[str] = None


class APIError(Exception):
    """Ошибка API."""
    def __init__(self, message: str, status_code: int = 0):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class APIClient:
    """
    HTTP клиент для P&ID Pipeline API.

    Persistent connection с автоматическим retry при потере связи.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        timeout: float = 60.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

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

    def _request(
        self,
        method: str,
        endpoint: str,
        retries: Optional[int] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Выполнить HTTP запрос с retry при потере соединения."""
        max_retries = retries if retries is not None else self.max_retries

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                response = self._client.request(method, endpoint, **kwargs)

                if response.status_code >= 400:
                    try:
                        error_data = response.json()
                        message = error_data.get("detail", str(error_data))
                    except Exception:
                        message = response.text or f"HTTP {response.status_code}"
                    raise APIError(message, response.status_code)

                return response.json()

            except httpx.RequestError as exc:
                last_error = exc
                if attempt < max_retries:
                    delay = self.retry_delay * (2 ** attempt)  # exponential backoff
                    logger.warning(
                        f"Connection error (attempt {attempt + 1}/{max_retries + 1}), "
                        f"retrying in {delay:.1f}s: {exc}"
                    )
                    time.sleep(delay)
                    # Пересоздаём client при connection reset
                    try:
                        self._client.close()
                    except Exception:
                        pass
                    self._client = httpx.Client(
                        base_url=self.base_url,
                        timeout=self.timeout,
                        limits=httpx.Limits(
                            max_connections=10,
                            max_keepalive_connections=5,
                        ),
                    )

        raise APIError(f"Connection failed after {max_retries + 1} attempts: {last_error}")

    def _request_raw(
        self,
        method: str,
        endpoint: str,
        **kwargs,
    ) -> httpx.Response:
        """Выполнить HTTP запрос и вернуть raw response (для скачивания файлов)."""
        try:
            response = self._client.request(method, endpoint, **kwargs)

            if response.status_code >= 400:
                try:
                    error_data = response.json()
                    message = error_data.get("detail", str(error_data))
                except Exception:
                    message = response.text or f"HTTP {response.status_code}"
                raise APIError(message, response.status_code)

            return response

        except httpx.RequestError as exc:
            raise APIError(f"Connection error: {exc}")

    # === Health ===

    def health_check(self) -> bool:
        """Проверить доступность API."""
        try:
            result = self._request("GET", "/health", retries=0)
            return result.get("status") in ("healthy", "degraded")
        except (APIError, Exception):
            return False

    # === Diagrams ===

    _MIME_BY_EXT = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }

    def upload_diagram(
        self,
        file_path: Path,
        project_code: str = "thermohydraulics",
        page: int = 1,
    ) -> DiagramInfo:
        """Загрузить диаграмму (PDF рендерится на сервере; page — страница PDF, 1-based)."""
        file_path = Path(file_path)
        mime = self._MIME_BY_EXT.get(file_path.suffix.lower(), "application/octet-stream")

        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, mime)}
            data = {"project_code": project_code, "page": str(page)}

            result = self._request(
                "POST",
                "/api/diagrams/upload",
                files=files,
                data=data,
                timeout=180.0,
            )

        return DiagramInfo(
            uid=result["uid"],
            number=result["number"],
            project_code=result["project_code"],
            status=DiagramStatus(result["status"]),
            filename=result["filename"],
        )

    def list_diagrams(
        self,
        project_code: Optional[str] = None,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[DiagramInfo]:
        """Получить список диаграмм."""
        params = {"skip": skip, "limit": limit}
        if project_code:
            params["project_code"] = project_code
        if status:
            params["status"] = status

        result = self._request("GET", "/api/diagrams/", params=params)

        # API возвращает {"items": [...], "total": ...}
        items = result.get("items", []) if isinstance(result, dict) else result

        diagrams = []
        for item in items:
            diagrams.append(DiagramInfo(
                uid=item["uid"],
                number=item["number"],
                project_code=item["project_code"],
                status=DiagramStatus(item["status"]),
                filename=item["original_filename"],
                detection_count=item.get("detection_count"),
                cvat_task_id=item.get("cvat_task_id"),
                cvat_job_id=item.get("cvat_job_id"),
                error_message=item.get("error_message"),
                created_at=item.get("created_at"),
                updated_at=item.get("updated_at"),
            ))

        return diagrams

    def get_diagram(self, uid: str) -> DiagramInfo:
        """Получить информацию о диаграмме."""
        result = self._request("GET", f"/api/diagrams/{uid}")

        return DiagramInfo(
            uid=result["uid"],
            number=result["number"],
            project_code=result["project_code"],
            status=DiagramStatus(result["status"]),
            filename=result["original_filename"],
            detection_count=result.get("detection_count"),
            validated_detection_count=result.get("validated_detection_count"),
            cvat_task_id=result.get("cvat_task_id"),
            cvat_job_id=result.get("cvat_job_id"),
            error_message=result.get("error_message"),
            created_at=result.get("created_at"),
            updated_at=result.get("updated_at"),
        )

    def get_status(self, uid: str) -> DiagramStatusInfo:
        """Получить статус диаграммы."""
        result = self._request("GET", f"/api/diagrams/{uid}/status", retries=1)

        return DiagramStatusInfo(
            status=DiagramStatus(result["status"]),
            error_message=result.get("error_message"),
            error_stage=result.get("error_stage"),
            cvat_task_id=result.get("cvat_task_id"),
            cvat_job_id=result.get("cvat_job_id"),
            detection_count=result.get("detection_count"),
            updated_at=result.get("updated_at"),
        )

    def get_stages(self, uid: str) -> list:
        """Список этапов обработки (ProcessingStage) — для по-этапной изоляции ошибок.

        Каждый элемент: stage_type, status (pending/running/completed/failed/skipped),
        attempt, error_message, error_traceback, started_at, completed_at, duration_seconds.
        """
        try:
            result = self._request("GET", f"/api/diagrams/{uid}/stages", retries=1)
            return result.get("stages", [])
        except APIError:
            return []

    def delete_diagram(self, uid: str) -> bool:
        """Удалить диаграмму."""
        self._request("DELETE", f"/api/diagrams/{uid}")
        return True

    # === Download ===

    def download_artifact(self, uid: str, artifact_type: str, dest_path: Path) -> Path:
        """
        Скачать артефакт через API (не обращается к filesystem напрямую).

        Args:
            uid: UUID диаграммы
            artifact_type: Тип артефакта (original_image, yolo_predicted, etc.)
            dest_path: Путь для сохранения файла

        Returns:
            Path к сохранённому файлу
        """
        response = self._request_raw(
            "GET",
            f"/api/diagrams/{uid}/download/{artifact_type}",
            timeout=120.0,
        )

        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(response.content)

        return dest_path

    # === Frame removal (Phase 0) ===

    def start_frame_removal(self, uid: str) -> Dict[str, Any]:
        """Начать очистку рамки (UPLOADED → CLEANING_FRAME)."""
        return self._request("POST", f"/api/frame/{uid}/start")

    def save_cleaned_image(self, uid: str, file_path: Path) -> Dict[str, Any]:
        """Загрузить очищенный PNG (становится каноническим original/image.png)."""
        file_path = Path(file_path)
        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, "image/png")}
            return self._request(
                "POST", f"/api/frame/{uid}/save", files=files, timeout=120.0
            )

    def complete_frame_removal(self, uid: str) -> Dict[str, Any]:
        """Завершить очистку рамки (→ FRAME_CLEANED)."""
        return self._request("POST", f"/api/frame/{uid}/complete")

    def skip_frame_removal(self, uid: str) -> Dict[str, Any]:
        """Пропустить очистку («рамки нет», → FRAME_CLEANED)."""
        return self._request("POST", f"/api/frame/{uid}/skip")

    # === Detection ===

    def start_detection(self, uid: str, model_id: str = None) -> Dict[str, Any]:
        """Запустить YOLO детекцию."""
        params = {}
        if model_id:
            params["model_id"] = model_id
        return self._request("POST", f"/api/detection/{uid}/detect", params=params)

    def get_detection_models(self, project_code: str) -> Dict[str, Any]:
        """Получить список доступных моделей детекции."""
        return self._request("GET", f"/api/projects/{project_code}/detection-models")

    # === CVAT ===

    def open_cvat_validation(self, uid: str) -> Dict[str, Any]:
        """Открыть валидацию в CVAT."""
        return self._request("POST", f"/api/cvat/{uid}/open-validation")

    def fetch_cvat_annotations(self, uid: str) -> Dict[str, Any]:
        """Получить валидированные аннотации из CVAT."""
        return self._request("POST", f"/api/cvat/{uid}/fetch-annotations")

    def get_cvat_url(self, uid: str) -> str:
        """Получить URL CVAT задачи."""
        result = self._request("GET", f"/api/cvat/{uid}/cvat-url")
        return result.get("cvat_url", "")

    def reopen_bbox_validation(self, uid: str) -> Dict[str, Any]:
        """Жёсткий возврат к проверке bbox с позднего этапа (§9 #4).

        Стоп текущей стадии (revoke) + сброс артефактов после `detected` +
        статус `validating_bbox` + переоткрытие ТОЙ ЖЕ CVAT-job (ручная разметка
        сохраняется). Возвращает {status, cvat_url, revoked_tasks, deleted_artifacts}.
        """
        return self._request("POST", f"/api/cvat/{uid}/reopen-bbox-validation")

    def create_cvat_task(self, uid: str) -> Dict[str, Any]:
        """Создать CVAT task для диаграммы."""
        return self._request("POST", f"/api/cvat/{uid}/create-task")

    # === Segmentation / Skeleton ===

    def start_segmentation(self, uid: str) -> Dict[str, Any]:
        """Запустить сегментацию."""
        return self._request("POST", f"/api/segmentation/{uid}/segment")

    def start_skeletonization(self, uid: str) -> Dict[str, Any]:
        """Запустить скелетизацию."""
        return self._request("POST", f"/api/skeleton/{uid}/skeletonize")

    # === Mask Validation (Phase 4) ===

    def start_mask_validation(self, uid: str) -> Dict[str, Any]:
        """Начать валидацию масок (SKELETONIZED → VALIDATING_MASKS)."""
        return self._request("POST", f"/api/validation/{uid}/masks/start")

    def upload_validated_mask(
        self, uid: str, mask_type: str, file_path: Path
    ) -> Dict[str, Any]:
        """
        Загрузить валидированную маску.

        Args:
            uid: UUID диаграммы
            mask_type: junction_mask_validated | bridge_mask_validated | pipe_mask_validated
            file_path: путь к PNG файлу
        """
        file_path = Path(file_path)
        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, "image/png")}
            data = {"mask_type": mask_type}
            return self._request(
                "POST",
                f"/api/validation/{uid}/masks/upload",
                files=files,
                data=data,
                timeout=120.0,
            )

    def upload_updated_nodes(
        self, uid: str, file_path: Path
    ) -> Dict[str, Any]:
        """
        Upload updated coco_validated.json and regenerate node_mask.

        Used when user adds/removes equipment nodes during pipe mask validation.

        Args:
            uid: UUID диаграммы
            file_path: путь к обновлённому coco_validated.json
        """
        file_path = Path(file_path)
        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, "application/json")}
            return self._request(
                "POST",
                f"/api/validation/{uid}/nodes/update",
                files=files,
                timeout=120.0,
            )

    def complete_mask_validation(self, uid: str) -> Dict[str, Any]:
        """
        Завершить валидацию масок.

        Проверяет наличие pipe mask, переводит в VALIDATED_MASKS,
        автоматически запускает task_skeletonize_simple → task_detect_junctions.
        """
        return self._request("POST", f"/api/validation/{uid}/masks/complete")

    # === Junction Validation ===

    def start_junction_validation(self, uid: str) -> Dict[str, Any]:
        """Начать валидацию перекрёстков (DETECTED_JUNCTIONS → VALIDATING_JUNCTIONS)."""
        return self._request("POST", f"/api/validation/{uid}/junctions/start")

    def complete_junction_validation(self, uid: str) -> Dict[str, Any]:
        """
        Завершить валидацию перекрёстков.

        Копирует junction/bridge маски как validated, переводит в VALIDATED_JUNCTIONS,
        автоматически запускает task_build_graph.
        """
        return self._request("POST", f"/api/validation/{uid}/junctions/complete")

    # === Graph ===

    def build_graph(self, uid: str) -> Dict[str, Any]:
        """
        Запустить построение графа.

        Проверяет наличие SKELETON_FINAL, переводит в BUILDING_GRAPH,
        запускает task_build_graph через Celery.
        """
        return self._request("POST", f"/api/graph/{uid}/build")

    def get_graph_result(self, uid: str) -> Dict[str, Any]:
        """Получить результат построения графа (node_count, edge_count, artifacts)."""
        return self._request("GET", f"/api/graph/{uid}/result")

    # === Graph Validation (Phase 5) ===

    def start_graph_validation(self, uid: str) -> Dict[str, Any]:
        """Начать валидацию графа (BUILT → VALIDATING_GRAPH)."""
        return self._request("POST", f"/api/validation/{uid}/graph/start")

    def upload_validated_graph(self, uid: str, file_path: Path) -> Dict[str, Any]:
        """
        Загрузить валидированный граф.

        Args:
            uid: UUID диаграммы
            file_path: путь к JSON файлу графа
        """
        file_path = Path(file_path)
        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, "application/json")}
            return self._request(
                "POST",
                f"/api/validation/{uid}/graph/save",
                files=files,
                timeout=120.0,
            )

    def complete_graph_validation(self, uid: str) -> Dict[str, Any]:
        """
        Завершить валидацию графа.

        Проверяет наличие graph_validated, переводит в VALIDATED_GRAPH,
        автоматически запускает генерацию FXML.
        """
        return self._request("POST", f"/api/validation/{uid}/graph/complete")

    def generate_fxml(self, uid: str, page_size: str = None, bridge_gap: float = None) -> Dict[str, Any]:
        """
        Запустить генерацию FXML из валидированного графа.

        Args:
            uid: UUID диаграммы
            page_size: 'A4'..'A0', '1920x1080' (экран) или None (оригинал)

        VALIDATED_GRAPH → GENERATING_FXML → COMPLETED.
        """
        params = {}
        if page_size:
            params["page_size"] = page_size
        if bridge_gap is not None:
            params["bridge_gap"] = bridge_gap
        return self._request("POST", f"/api/graph/{uid}/generate-fxml", params=params)

    # === OCR ===

    def complete_simple_graph_validation(self, uid: str) -> Dict[str, Any]:
        """
        Завершить Simple-валидацию графа → запустить OCR.

        VALIDATING_GRAPH → VALIDATED_GRAPH → auto-start OCR.
        """
        return self._request("POST", f"/api/validation/{uid}/graph/complete-simple")

    def start_ocr(self, uid: str) -> Dict[str, Any]:
        """Ручной запуск/retry OCR."""
        return self._request("POST", f"/api/ocr/{uid}/start")

    def get_ocr_status(self, uid: str) -> Dict[str, Any]:
        """Получить статус OCR."""
        return self._request("GET", f"/api/ocr/{uid}/status")

    def download_ocr_result(self, uid: str, dest: Path) -> Path:
        """Скачать OCR результат."""
        response = self._request_raw("GET", f"/api/ocr/{uid}/result", timeout=60.0)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(response.content)
        return dest

    def upload_ocr_result(self, uid: str, path: Path) -> Dict[str, Any]:
        """Обновить OCR результат (после слияния блоков)."""
        path = Path(path)
        with open(path, "rb") as f:
            files = {"file": (path.name, f, "application/json")}
            return self._request("PUT", f"/api/ocr/{uid}/result", files=files)

    def save_ocr_binding(self, uid: str, path: Path) -> Dict[str, Any]:
        """Сохранить привязки OCR → граф."""
        path = Path(path)
        with open(path, "rb") as f:
            files = {"file": (path.name, f, "application/json")}
            return self._request("POST", f"/api/ocr/{uid}/binding/save", files=files)

    def download_ocr_binding(self, uid: str, dest: Path) -> Path:
        """Скачать привязки OCR."""
        response = self._request_raw("GET", f"/api/ocr/{uid}/binding", timeout=60.0)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(response.content)
        return dest

    def apply_ocr_binding(self, uid: str) -> Dict[str, Any]:
        """Применить привязки к графу."""
        return self._request("POST", f"/api/ocr/{uid}/binding/apply")

    def recognize_boxes(self, uid: str, boxes: list) -> Dict[str, Any]:
        """П3: распознать вручную добавленные боксы (батчем). boxes: [[x0,y0,x1,y1], ...]."""
        return self._request("POST", f"/api/ocr/{uid}/recognize",
                             json={"boxes": boxes}, timeout=200.0)

    def save_ocr_validation(self, uid: str, path: Path) -> Dict[str, Any]:
        """Сохранить результаты валидации OCR-блоков."""
        path = Path(path)
        with open(path, "rb") as f:
            files = {"file": (path.name, f, "application/json")}
            return self._request("POST", f"/api/ocr/{uid}/validation/save", files=files)

    def download_ocr_validation(self, uid: str, dest: Path) -> Path:
        """Скачать результаты валидации OCR."""
        response = self._request_raw("GET", f"/api/ocr/{uid}/validation", timeout=60.0)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(response.content)
        return dest

    def rollback_diagram(self, uid: str, target_status: str,
                         preserve_ocr: bool = False,
                         preserve_contours: bool = False) -> Dict[str, Any]:
        """Откатить диаграмму до указанного этапа."""
        url = f"/api/diagrams/{uid}/rollback?target_status={target_status}"
        if preserve_ocr:
            url += "&preserve_ocr=true"
        if preserve_contours:
            url += "&preserve_contours=true"
        return self._request("POST", url)

    # === Contours ===

    def get_contours_status(self, uid: str) -> Dict[str, Any]:
        """Проверить наличие контуров и статистику."""
        return self._request("GET", f"/api/contours/{uid}/status")

    def extract_contours(self, uid: str, ann_ids=None) -> Dict[str, Any]:
        """Запустить распознавание контуров по требованию.

        ann_ids: список COCO id выбранных элементов (None = все подходящие).
        Возвращает {"status": "started", "task_id": ...}.
        """
        return self._request("POST", f"/api/contours/{uid}/extract", json={"ann_ids": ann_ids})

    def download_contours_auto(self, uid: str, dest: Path) -> Path:
        """Скачать contours_auto.json."""
        response = self._request_raw("GET", f"/api/contours/{uid}/auto", timeout=60.0)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(response.content)
        return dest

    def download_contours_validated(self, uid: str, dest: Path) -> Path:
        """Скачать contours_validated.json."""
        response = self._request_raw("GET", f"/api/contours/{uid}/validated", timeout=60.0)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(response.content)
        return dest

    def upload_contours_validated(self, uid: str, path: Path) -> Dict[str, Any]:
        """Загрузить contours_validated.json."""
        path = Path(path)
        with open(path, "rb") as f:
            files = {"file": (path.name, f, "application/json")}
            return self._request("PUT", f"/api/contours/{uid}/validated", files=files)

    def upload_contours_training(self, uid: str, path: Path) -> Dict[str, Any]:
        """Загрузить contours_training.json (approved polygons for SAM2 fine-tuning)."""
        path = Path(path)
        with open(path, "rb") as f:
            files = {"file": (path.name, f, "application/json")}
            return self._request("PUT", f"/api/contours/{uid}/training", files=files)

    def auto_accept_contours(self, uid: str) -> Dict[str, Any]:
        """Auto-accept всех контуров."""
        return self._request("POST", f"/api/contours/{uid}/auto-accept")

    def complete_contour_validation(self, uid: str) -> Dict[str, Any]:
        """Завершить валидацию контуров."""
        return self._request("POST", f"/api/contours/{uid}/complete")

    # === Projects ===

    def list_projects(self) -> List[Dict[str, Any]]:
        """Получить список проектов."""
        try:
            result = self._request("GET", "/api/projects/")
            return result.get("items", result) if isinstance(result, dict) else result
        except APIError:
            # Fallback если API проектов не работает
            return [{"code": "thermohydraulics", "name": "Термогидравлика"}]

    def get_project_classes(self, project_code: str) -> Dict[str, Any]:
        """Получить список классов проекта.

        Returns:
            {"project_code": "...", "num_classes": N, "classes": [{"id": N, "name": "..."}, ...]}
        """
        return self._request("GET", f"/api/projects/{project_code}/classes")

    # === Operations ===

    def retry_operation(self, uid: str) -> Dict[str, Any]:
        """Повторить операцию после ошибки."""
        return self._request("POST", f"/api/diagrams/{uid}/retry")

    def reupload_original(self, uid: str, file_path: Path) -> Dict[str, Any]:
        """Перезагрузить оригинальное изображение."""
        file_path = Path(file_path)

        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, "image/png")}

            return self._request(
                "POST",
                f"/api/diagrams/{uid}/reupload-original",
                files=files,
            )

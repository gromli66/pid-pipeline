"""
Ensemble Detector — ансамблевая YOLO-детекция для P&ID Pipeline.

Объединяет предсказания нескольких YOLO моделей, обученных на разных
размерах тайлов (например, 640, 1280, 2048). Каждая модель запускается
через SAHI со своим slice_size, результаты сливаются через WBF
(Weighted Boxes Fusion), NMS или Soft-NMS.

ИДЕЯ:
-----
Разные tile_size дают модели разный "масштаб зрения":
- Мелкие тайлы (640): лучше мелкие объекты
- Средние тайлы (1280): сбалансированный вариант (baseline)
- Крупные тайлы (2048): больше контекста, лучше крупные объекты, меньше FP

ИСПОЛЬЗОВАНИЕ:
-------------
    from modules.yolo_detector import EnsembleDetector

    detector = EnsembleDetector(
        models=[
            {"weights": "exp_640/best.pt",  "tile_size": 640,  "weight": 1.0},
            {"weights": "exp_1280/best.pt", "tile_size": 1280, "weight": 1.0},
            {"weights": "exp_2048/best.pt", "tile_size": 2048, "weight": 1.0},
        ],
        merge_strategy="wbf",
        confidence_threshold=0.5,
    )
    detections = detector.detect(Path("scheme.png"))

Формат детекций на выходе идентичен NodeDetector.detect()
(нормализованные координаты), поэтому вся постобработка
(per_class_confidence, resolve_overlaps, CVAT-экспорт) работает без изменений.
"""

import numpy as np
import cv2
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from modules.yolo_detector.detector import NodeDetector
from modules.yolo_detector.preprocessing import binarize_for_yolo
from modules.yolo_detector.config import CLASS_NAMES, REVERSE_REINDEX

# Наблюдаемость (Волна 3, §9 #7): под-под-шаги COMPUTE ансамбля видимы в логах.
# `obs.step` — тонкий лог-примитив без ML-зависимостей; связность modules→app.core
# осознанная (детектор всегда исполняется внутри worker'а, app на PYTHONPATH).
from app.core.logging import get_logger
from app.core.obs import step
from app.core.errors import ConfigError, InferenceError, PipelineError

logger = get_logger(__name__)


class EnsembleDetector:
    """
    Ансамблевый детектор, объединяющий несколько YOLO моделей.

    Каждая модель обучена на тайлах своего размера и запускается
    через SAHI с соответствующим slice_size.

    Attributes:
        models_config: Конфигурации моделей [{weights, tile_size, weight}]
        merge_strategy: Стратегия объединения ("wbf", "nms", "soft_nms")
        iou_threshold: Порог IoU для слияния боксов
        confidence_threshold: Порог уверенности после слияния
        skip_box_thr: Порог для WBF (боксы ниже отбрасываются)
    """

    def __init__(
        self,
        models: List[Dict],
        merge_strategy: str = "wbf",
        iou_threshold: float = 0.5,
        confidence_threshold: float = 0.5,
        skip_box_thr: float = 0.01,
        device: str = "cuda",
        class_names: Optional[Dict[int, str]] = None,
        reverse_reindex: Optional[Dict[int, int]] = None,
        per_class_weights: Optional[Dict[int, Dict[str, float]]] = None,
    ):
        """
        Args:
            models: Список моделей, каждая — словарь:
                - weights (str|Path): путь к весам
                - tile_size (int): размер тайла, на котором обучена модель
                - weight (float): вес модели в ансамбле (default 1.0)
                - confidence (float): порог conf этой модели (default общий)
                - sahi_overlap (float): overlap для SAHI (default 0.25)
            merge_strategy: "wbf" | "nms" | "soft_nms"
            iou_threshold: Порог IoU для слияния
            confidence_threshold: Порог уверенности после слияния
            skip_box_thr: Минимальный confidence для WBF
            device: Устройство для инференса ("cuda", "cpu")
            class_names: Маппинг id → имя класса (default: из config)
            reverse_reindex: Обратная переиндексация классов (default: из config)
            per_class_weights: Адаптивный ансамбль — per-class веса моделей.
                {class_id: {model_label: weight}}, где model_label = "tile{tile_size}".
                class_id — в финальной нумерации модели (ДО reverse_reindex).
                None/пусто → обычный WBF с равными весами (текущее поведение).
                Активируется только при merge_strategy="wbf".
        """
        self.models_config = []
        for m in models:
            self.models_config.append({
                "weights": Path(m["weights"]),
                "tile_size": int(m.get("tile_size", 1280)),
                "weight": float(m.get("weight", 1.0)),
                "confidence": float(m.get("confidence", confidence_threshold)),
                "sahi_overlap": float(m.get("sahi_overlap", 0.25)),
            })

        self.merge_strategy = merge_strategy
        self.iou_threshold = iou_threshold
        self.confidence_threshold = confidence_threshold
        self.skip_box_thr = skip_box_thr
        self.device = device
        self.class_names = class_names if class_names is not None else CLASS_NAMES
        self.reverse_reindex = (
            reverse_reindex if reverse_reindex is not None else REVERSE_REINDEX
        )
        self.per_class_weights = per_class_weights or {}
        # Метки моделей для адаптивных весов: "tile640", "tile1280", ...
        self.model_labels = [f"tile{cfg['tile_size']}" for cfg in self.models_config]

        # Ленивая загрузка детекторов
        self._detectors = None

    def _load_detectors(self) -> None:
        """Инициализировать детекторы всех моделей."""
        if self._detectors is not None:
            return

        self._detectors = []
        for cfg in self.models_config:
            detector = NodeDetector(
                weights=cfg["weights"],
                confidence=cfg["confidence"],
                iou_threshold=self.iou_threshold,
                device=self.device,
                use_sahi=True,
                sahi_slice_size=cfg["tile_size"],
                sahi_overlap_ratio=cfg["sahi_overlap"],
                apply_preprocessing=False,  # бинаризация делается один раз в detect()
                class_names=self.class_names,
                reverse_reindex=self.reverse_reindex,
            )
            self._detectors.append((detector, cfg))

    # ------------------------------------------------------------------
    # Конвертация детекций <-> массивы для ensemble_boxes
    # ------------------------------------------------------------------

    def _detections_to_arrays(
        self,
        all_model_detections: List[Tuple[List[Dict], float]],
    ) -> Tuple[List[np.ndarray], List[np.ndarray], List[np.ndarray], List[float]]:
        """
        Конвертировать детекции в формат для WBF/NMS.

        Returns:
            boxes_list: [array(N,4)] — нормализованные [x1, y1, x2, y2]
            scores_list: [array(N,)]
            labels_list: [array(N,)]
            weights: веса моделей
        """
        boxes_list, scores_list, labels_list, weights = [], [], [], []

        for detections, model_weight in all_model_detections:
            if not detections:
                boxes_list.append(np.empty((0, 4), dtype=np.float32))
                scores_list.append(np.empty(0, dtype=np.float32))
                labels_list.append(np.empty(0, dtype=np.int32))
                weights.append(model_weight)
                continue

            boxes, scores, labels = [], [], []
            for det in detections:
                xc, yc = det["x_center"], det["y_center"]
                w, h = det["width"], det["height"]
                boxes.append([
                    max(0.0, xc - w / 2),
                    max(0.0, yc - h / 2),
                    min(1.0, xc + w / 2),
                    min(1.0, yc + h / 2),
                ])
                scores.append(det.get("confidence", 1.0))
                labels.append(det["class_id"])

            boxes_list.append(np.array(boxes, dtype=np.float32))
            scores_list.append(np.array(scores, dtype=np.float32))
            labels_list.append(np.array(labels, dtype=np.int32))
            weights.append(model_weight)

        return boxes_list, scores_list, labels_list, weights

    def _arrays_to_detections(
        self,
        boxes: np.ndarray,
        scores: np.ndarray,
        labels: np.ndarray,
    ) -> List[Dict]:
        """Конвертировать массивы обратно в список детекций."""
        detections = []
        for i in range(len(boxes)):
            if scores[i] < self.confidence_threshold:
                continue

            x1, y1, x2, y2 = boxes[i]
            class_id = int(labels[i])
            detections.append({
                "class_id": class_id,
                "class_name": self.class_names.get(class_id, f"class_{class_id}"),
                "x_center": float((x1 + x2) / 2),
                "y_center": float((y1 + y2) / 2),
                "width": float(x2 - x1),
                "height": float(y2 - y1),
                "confidence": float(scores[i]),
            })
        return detections

    # ------------------------------------------------------------------
    # Стратегии слияния
    # ------------------------------------------------------------------

    def _merge_wbf(self, boxes_list, scores_list, labels_list, weights) -> List[Dict]:
        """
        Weighted Boxes Fusion: усредняет координаты боксов с весами
        вместо выбора одного лучшего — точнее локализация.
        """
        from ensemble_boxes import weighted_boxes_fusion

        if all(len(b) == 0 for b in boxes_list):
            return []

        fused_boxes, fused_scores, fused_labels = weighted_boxes_fusion(
            boxes_list,
            scores_list,
            labels_list,
            weights=weights,
            iou_thr=self.iou_threshold,
            skip_box_thr=self.skip_box_thr,
            conf_type="avg",
        )
        return self._arrays_to_detections(fused_boxes, fused_scores, fused_labels)

    def _merge_wbf_adaptive(self, boxes_list, scores_list, labels_list, weights) -> List[Dict]:
        """
        Адаптивный WBF: для каждого класса своё слияние с per-class весами моделей.

        Запускает WBF отдельно по каждому классу, подставляя веса из
        self.per_class_weights[class_id] = {model_label: weight}. Классы без
        записи используют общие веса моделей (`weights`). Эквивалентно обычному
        WBF, если все per-class веса равны общим.
        """
        from ensemble_boxes import weighted_boxes_fusion

        if all(len(b) == 0 for b in boxes_list):
            return []

        # Все классы, присутствующие в предсказаниях
        present_classes = set()
        for labels in labels_list:
            present_classes.update(int(c) for c in labels.tolist())

        agg_boxes, agg_scores, agg_labels = [], [], []

        for cls_id in present_classes:
            # Веса моделей для этого класса (default — общие веса)
            if cls_id in self.per_class_weights:
                cls_weights = [
                    float(self.per_class_weights[cls_id].get(label, w))
                    for label, w in zip(self.model_labels, weights)
                ]
            else:
                cls_weights = list(weights)

            # Боксы только этого класса из каждой модели
            cls_boxes, cls_scores, cls_labels = [], [], []
            for boxes, scores, labels in zip(boxes_list, scores_list, labels_list):
                mask = labels == cls_id
                if mask.any():
                    cls_boxes.append(boxes[mask])
                    cls_scores.append(scores[mask])
                    cls_labels.append(labels[mask])
                else:
                    cls_boxes.append(np.empty((0, 4), dtype=np.float32))
                    cls_scores.append(np.empty(0, dtype=np.float32))
                    cls_labels.append(np.empty(0, dtype=np.int32))

            if all(len(b) == 0 for b in cls_boxes):
                continue

            fb, fs, fl = weighted_boxes_fusion(
                cls_boxes, cls_scores, cls_labels,
                weights=cls_weights,
                iou_thr=self.iou_threshold,
                skip_box_thr=self.skip_box_thr,
                conf_type="avg",
            )
            agg_boxes.append(fb)
            agg_scores.append(fs)
            agg_labels.append(fl)

        if not agg_boxes:
            return []

        return self._arrays_to_detections(
            np.concatenate(agg_boxes, axis=0),
            np.concatenate(agg_scores, axis=0),
            np.concatenate(agg_labels, axis=0),
        )

    def _merge_nms(self, boxes_list, scores_list, labels_list, weights) -> List[Dict]:
        """NMS: из перекрывающихся боксов остаётся бокс с max confidence."""
        from ensemble_boxes import nms

        if all(len(b) == 0 for b in boxes_list):
            return []

        weighted_scores = [s * w for s, w in zip(scores_list, weights)]
        merged_boxes, merged_scores, merged_labels = nms(
            boxes_list, weighted_scores, labels_list, iou_thr=self.iou_threshold
        )
        return self._arrays_to_detections(merged_boxes, merged_scores, merged_labels)

    def _merge_soft_nms(self, boxes_list, scores_list, labels_list, weights) -> List[Dict]:
        """Soft-NMS: вместо удаления перекрывающихся боксов снижает их confidence."""
        from ensemble_boxes import soft_nms

        if all(len(b) == 0 for b in boxes_list):
            return []

        weighted_scores = [s * w for s, w in zip(scores_list, weights)]
        merged_boxes, merged_scores, merged_labels = soft_nms(
            boxes_list,
            weighted_scores,
            labels_list,
            iou_thr=self.iou_threshold,
            sigma=0.5,
            thresh=self.skip_box_thr,
        )
        return self._arrays_to_detections(merged_boxes, merged_scores, merged_labels)

    # ------------------------------------------------------------------
    # Детекция
    # ------------------------------------------------------------------

    def _apply_reverse_reindex(self, detections: List[Dict]) -> List[Dict]:
        """Обратная переиндексация классов (34→35 output, 35→38 strelka)."""
        if not self.reverse_reindex:
            return detections

        for det in detections:
            class_id = det["class_id"]
            if class_id in self.reverse_reindex:
                new_id = self.reverse_reindex[class_id]
                det["class_id"] = new_id
                det["class_name"] = self.class_names.get(new_id, f"class_{new_id}")
        return detections

    def detect(
        self,
        image: Union[str, Path, np.ndarray],
        apply_grayscale: bool = True,
        apply_reverse_mapping: bool = True,
        return_per_model: bool = False,
    ) -> Union[List[Dict], Tuple[List[Dict], List[List[Dict]]]]:
        """
        Ансамблевая детекция на одном изображении.

        1. Бинаризация изображения (один раз для всех моделей)
        2. Каждая модель делает детекцию через SAHI со своим tile_size
        3. Результаты объединяются через WBF/NMS/Soft-NMS
        4. Финальный порог confidence + обратная переиндексация

        Args:
            image: Путь к изображению или numpy array (BGR)
            apply_grayscale: Бинаризовать изображение (как при обучении)
            apply_reverse_mapping: Применять обратную переиндексацию классов
            return_per_model: Также вернуть детекции каждой модели отдельно

        Returns:
            Список объединённых детекций (нормализованные координаты);
            при return_per_model=True — (объединённые, [по моделям]).
        """
        self._load_detectors()

        # Подготовить изображение ОДИН раз (не 3 раза на модель)
        if isinstance(image, (str, Path)):
            if apply_grayscale:
                img = binarize_for_yolo(Path(image), method="full")
            else:
                img = cv2.imread(str(image))
                if img is None:
                    raise ValueError(f"Не удалось загрузить: {image}")
        else:
            img = binarize_for_yolo(image) if apply_grayscale else image

        # Собрать детекции от всех моделей
        all_model_detections = []
        per_model_results = []
        for detector, cfg in self._detectors:
            # Под-под-шаг COMPUTE: инференс одной модели ансамбля (SAHI tiling+
            # inference внутри одного вызова get_sliced_prediction). Логируем
            # границы → на CPU видно движение по моделям (RUNBOOK §9 #7);
            # неожиданный сбой → InferenceError с проставленным step.
            with step("inference", logger, model=f"tile{cfg['tile_size']}",
                      tile=cfg["tile_size"], weight=cfg["weight"]):
                try:
                    dets = detector.detect(
                        img,
                        return_absolute=False,
                        apply_reverse_mapping=False,  # reverse mapping после слияния
                    )
                except PipelineError:
                    raise
                except Exception as exc:
                    raise InferenceError(
                        f"inference failed (tile={cfg['tile_size']})",
                        step="inference", cause=exc,
                    ) from exc
            all_model_detections.append((dets, cfg["weight"]))
            per_model_results.append(dets)

        # Слияние
        boxes_list, scores_list, labels_list, weights = self._detections_to_arrays(
            all_model_detections
        )

        # Под-под-шаг COMPUTE: слияние предсказаний моделей (WBF/NMS/Soft-NMS).
        with step("fusion", logger, strategy=self.merge_strategy,
                  n_models=len(self._detectors)):
            merge_fn = {
                "wbf": self._merge_wbf,
                "nms": self._merge_nms,
                "soft_nms": self._merge_soft_nms,
            }
            if self.merge_strategy not in merge_fn:
                raise ConfigError(
                    f"Неизвестная стратегия: {self.merge_strategy}. "
                    f"Доступные: {list(merge_fn.keys())}",
                    step="fusion",
                )

            # Адаптивный per-class WBF, если заданы per-class веса (только для wbf)
            if self.merge_strategy == "wbf" and self.per_class_weights:
                merged = self._merge_wbf_adaptive(
                    boxes_list, scores_list, labels_list, weights
                )
            else:
                merged = merge_fn[self.merge_strategy](
                    boxes_list, scores_list, labels_list, weights
                )

        # Добавить bbox в абсолютных координатах
        # (его проставляет одиночный NodeDetector; нужен resolve_overlaps и др.)
        img_height, img_width = img.shape[:2]
        for det in merged:
            x1 = (det["x_center"] - det["width"] / 2) * img_width
            y1 = (det["y_center"] - det["height"] / 2) * img_height
            x2 = (det["x_center"] + det["width"] / 2) * img_width
            y2 = (det["y_center"] + det["height"] / 2) * img_height
            det["bbox"] = [float(x1), float(y1), float(x2), float(y2)]

        # Обратная переиндексация
        if apply_reverse_mapping:
            merged = self._apply_reverse_reindex(merged)
            per_model_results = [
                self._apply_reverse_reindex(dets) for dets in per_model_results
            ]

        if return_per_model:
            return merged, per_model_results
        return merged

    def get_model_info(self) -> List[Dict]:
        """Информация о моделях в ансамбле (для логов/отладки)."""
        return [
            {
                "weights": str(cfg["weights"]),
                "tile_size": cfg["tile_size"],
                "weight": cfg["weight"],
                "confidence": cfg["confidence"],
                "sahi_overlap": cfg["sahi_overlap"],
            }
            for cfg in self.models_config
        ]

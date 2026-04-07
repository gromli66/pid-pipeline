"""
Step 2: Surya OCR detection + recognition на полном изображении.

Адаптировано из: 07_surya_all_2.py

Тайлинг для изображений >4096px, дедупликация по IoU + containment.

Вход: cleaned_for_ocr.png
Выход: surya_raw.json — [{bbox, text, confidence, source:"surya"}]
"""

import json
import logging
from pathlib import Path
from typing import List, Dict

from PIL import Image

from modules.ocr.utils import bbox_area, iou, containment

logger = logging.getLogger(__name__)

# ── Конфигурация ──────────────────────────────────────────
TILE_SIZE = 2048
TILE_OVERLAP = 256
TILE_THRESHOLD = 4096
IOU_THRESHOLD = 0.5
CONTAINMENT_THRESHOLD = 0.6

Image.MAX_IMAGE_PIXELS = None


def load_surya_models():
    """
    Загрузить Surya модели.

    Returns:
        (foundation, rec_predictor, det_predictor)
    """
    from surya.foundation import FoundationPredictor
    from surya.recognition import RecognitionPredictor
    from surya.detection import DetectionPredictor

    logger.info("Loading Surya models...")
    foundation = FoundationPredictor()
    rec_predictor = RecognitionPredictor(foundation)
    det_predictor = DetectionPredictor()
    logger.info("Surya models loaded")
    return foundation, rec_predictor, det_predictor


def _tile_image(img: Image.Image) -> List[Dict]:
    """Нарезает PIL Image на тайлы с перекрытием."""
    w, h = img.size
    tiles = []
    for y in range(0, h, TILE_SIZE - TILE_OVERLAP):
        for x in range(0, w, TILE_SIZE - TILE_OVERLAP):
            x2 = min(x + TILE_SIZE, w)
            y2 = min(y + TILE_SIZE, h)
            if (x2 - x) < 100 or (y2 - y) < 100:
                continue
            tile = img.crop((x, y, x2, y2))
            tiles.append({"image": tile, "offset_x": x, "offset_y": y})
    return tiles


def _merge_overlapping(detections: List[Dict]) -> List[Dict]:
    """Дедупликация: оставляем больший бокс при IoU/containment overlap."""
    sorted_dets = sorted(detections, key=lambda d: bbox_area(d["bbox"]), reverse=True)
    remove = set()

    for i in range(len(sorted_dets)):
        if i in remove:
            continue
        for j in range(i + 1, len(sorted_dets)):
            if j in remove:
                continue
            if containment(sorted_dets[j]["bbox"], sorted_dets[i]["bbox"]) > CONTAINMENT_THRESHOLD:
                remove.add(j)
                continue
            if iou(sorted_dets[i]["bbox"], sorted_dets[j]["bbox"]) > IOU_THRESHOLD:
                remove.add(j)

    return [d for i, d in enumerate(sorted_dets) if i not in remove]


def _ocr_single(img: Image.Image, rec_predictor, det_predictor) -> List[Dict]:
    """OCR на одном изображении без тайлинга."""
    preds = rec_predictor([img], det_predictor=det_predictor)
    return [
        {
            "text": line.text,
            "bbox": [round(c) for c in line.bbox],
            "confidence": round(line.confidence, 3),
            "source": "surya",
        }
        for line in preds[0].text_lines
    ]


def _ocr_tiled(img: Image.Image, rec_predictor, det_predictor) -> List[Dict]:
    """OCR с тайлингом + дедупликация."""
    tiles = _tile_image(img)
    logger.info("Tiling: %d tiles (%dpx, overlap=%dpx)", len(tiles), TILE_SIZE, TILE_OVERLAP)

    all_detections = []
    for i, tile in enumerate(tiles):
        preds = rec_predictor([tile["image"]], det_predictor=det_predictor)
        ox, oy = tile["offset_x"], tile["offset_y"]

        for line in preds[0].text_lines:
            all_detections.append({
                "text": line.text,
                "bbox": [
                    round(line.bbox[0] + ox),
                    round(line.bbox[1] + oy),
                    round(line.bbox[2] + ox),
                    round(line.bbox[3] + oy),
                ],
                "confidence": round(line.confidence, 3),
                "source": "surya",
            })

        if (i + 1) % 5 == 0 or (i + 1) == len(tiles):
            logger.info("Tile %d/%d, detections: %d", i + 1, len(tiles), len(all_detections))

    before = len(all_detections)
    detections = _merge_overlapping(all_detections)
    logger.info("Merge overlapping: %d → %d", before, len(detections))
    return detections


def run_detect(cleaned_path: Path, ocr_dir: Path, det_predictor, rec_predictor):
    """
    Запустить Surya detection+recognition на полном изображении.

    Args:
        cleaned_path: путь к cleaned_for_ocr.png
        ocr_dir: папка для выходных файлов
        det_predictor: Surya DetectionPredictor
        rec_predictor: Surya RecognitionPredictor
    """
    img = Image.open(str(cleaned_path)).convert("RGB")
    w, h = img.size
    logger.info("Image: %dx%d", w, h)

    if max(w, h) > TILE_THRESHOLD:
        detections = _ocr_tiled(img, rec_predictor, det_predictor)
    else:
        detections = _ocr_single(img, rec_predictor, det_predictor)

    # Дополнительный merge даже для не-тайловых
    before = len(detections)
    detections = _merge_overlapping(detections)
    if before != len(detections):
        logger.info("Post-merge: %d → %d", before, len(detections))

    # Сохраняем
    out_path = ocr_dir / "surya_raw.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(detections, f, ensure_ascii=False, indent=2)

    logger.info("Surya raw: %d detections → %s", len(detections), out_path)

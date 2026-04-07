"""
Step 4a: Surya Recognition на кропах.

Адаптировано из: recognize_blocks.py

Вход: original/image.png + reclustered_blocks.json + Surya модели
Выход: surya_crops.json — блоки с текстом от Surya

Модели переиспользуются от Step 2 (передаются извне).

Вертикальные кропы (height/width > 1.5) распознаются в 3 вариантах:
  - оригинал
  - поворот +90° (по часовой)
  - поворот −90° (против часовой)
Выбирается результат с лучшим confidence.
"""

import html
import json
import logging
import re
from pathlib import Path
from typing import List, Dict, Tuple

from PIL import Image

from modules.ocr.crop_saver import crop_with_pad, pad_to_min_size

logger = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = None

BATCH_SIZE = 8
CONTAINMENT_THRESH = 0.7
IOU_THRESH = 0.5

# Порог aspect ratio для определения вертикального кропа
VERTICAL_ASPECT_THRESHOLD = 1.5


def _clean_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    return text.strip()


def _containment(inner, outer):
    ix1, iy1 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix2, iy2 = min(inner[2], outer[2]), min(inner[3], outer[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    return inter / area if area > 0 else 0.0


def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _deduplicate_lines(lines: List[Dict]) -> List[Dict]:
    """Дедупликация строк внутри одного кропа."""
    if len(lines) <= 1:
        return lines
    sorted_lines = sorted(lines, key=lambda l: (l["bbox"][2] - l["bbox"][0]) * (l["bbox"][3] - l["bbox"][1]), reverse=True)
    kept = []
    for line in sorted_lines:
        is_dup = any(
            _containment(line["bbox"], ex["bbox"]) > CONTAINMENT_THRESH or
            _iou(line["bbox"], ex["bbox"]) > IOU_THRESH
            for ex in kept
        )
        if not is_dup:
            kept.append(line)
    return kept


def _merge_lines(lines: List[Dict]) -> Tuple[str, float, int]:
    """Мердж строк → один текст + min confidence."""
    if not lines:
        return "", 0.0, 0
    sorted_lines = sorted(lines, key=lambda l: l["bbox"][1])
    texts = [_clean_text(l["text"]) for l in sorted_lines if _clean_text(l["text"])]
    confs = [l["confidence"] for l in sorted_lines]
    return " ".join(texts), min(confs) if confs else 0.0, len(sorted_lines)


def _is_vertical_crop(crop: Image.Image) -> bool:
    """Кроп вертикальный? (высота значительно больше ширины)."""
    w, h = crop.size
    if w <= 0:
        return False
    return h / w > VERTICAL_ASPECT_THRESHOLD


def _recognize_single(crop: Image.Image, det_predictor, rec_predictor) -> List[Dict]:
    """Распознать один кроп, вернуть список строк."""
    preds = rec_predictor([crop], det_predictor=det_predictor)
    return [
        {
            "text": line.text,
            "bbox": [round(c) for c in line.bbox],
            "confidence": round(line.confidence, 3),
        }
        for line in preds[0].text_lines
    ]


def _process_lines(lines: List[Dict]) -> Tuple[str, float, int]:
    """Дедупликация + мердж строк → (text, confidence, n_lines)."""
    lines = _deduplicate_lines(lines)
    return _merge_lines(lines)


def _recognize_with_rotation(
    crop: Image.Image, det_predictor, rec_predictor
) -> Tuple[List[Dict], str]:
    """
    Для вертикального кропа: распознать оригинал, +90°, −90°.
    Выбрать лучший по confidence. Вернуть (lines, orientation).

    Для горизонтального кропа: обычное распознавание.

    Returns:
        (lines, orientation) где orientation: "original" | "cw90" | "ccw90"
    """
    if not _is_vertical_crop(crop):
        lines = _recognize_single(crop, det_predictor, rec_predictor)
        return lines, "original"

    # Три варианта
    variants = []

    # 1. Оригинал
    lines_orig = _recognize_single(crop, det_predictor, rec_predictor)
    text_orig, conf_orig, n_orig = _process_lines(lines_orig)
    variants.append((lines_orig, conf_orig, text_orig, "original"))

    # 2. Поворот по часовой (+90°) → Image.ROTATE_270 (PIL convention)
    crop_cw = crop.transpose(Image.ROTATE_270)
    lines_cw = _recognize_single(crop_cw, det_predictor, rec_predictor)
    text_cw, conf_cw, n_cw = _process_lines(lines_cw)
    variants.append((lines_cw, conf_cw, text_cw, "cw90"))

    # 3. Поворот против часовой (−90°) → Image.ROTATE_90
    crop_ccw = crop.transpose(Image.ROTATE_90)
    lines_ccw = _recognize_single(crop_ccw, det_predictor, rec_predictor)
    text_ccw, conf_ccw, n_ccw = _process_lines(lines_ccw)
    variants.append((lines_ccw, conf_ccw, text_ccw, "ccw90"))

    # Выбираем лучший: наибольший confidence среди вариантов с текстом
    best = variants[0]
    for v in variants[1:]:
        v_lines, v_conf, v_text, v_orient = v
        b_lines, b_conf, b_text, b_orient = best
        # Предпочитаем вариант с текстом и более высоким confidence
        if v_text and (not b_text or v_conf > b_conf):
            best = v

    return best[0], best[3]


def run_recognize(original_path: str, blocks_json_path: str, ocr_dir: str,
                  det_predictor, rec_predictor):
    """
    Surya recognition на кропах из reclustered_blocks.

    Args:
        original_path: путь к original/image.png
        blocks_json_path: путь к reclustered_blocks.json
        ocr_dir: папка OCR
        det_predictor: Surya DetectionPredictor
        rec_predictor: Surya RecognitionPredictor
    """
    image = Image.open(original_path).convert("RGB")
    with open(blocks_json_path, encoding="utf-8") as f:
        blocks = json.load(f)

    logger.info("Recognizing %d blocks with Surya...", len(blocks))

    # Подготовка кропов
    crops = []
    for block in blocks:
        crop, _ = crop_with_pad(image, block["bbox"])
        crop = pad_to_min_size(crop)
        crops.append(crop)

    # Определить вертикальные и горизонтальные кропы
    vertical_indices = {i for i, c in enumerate(crops) if _is_vertical_crop(c)}
    horizontal_indices = [i for i in range(len(crops)) if i not in vertical_indices]

    if vertical_indices:
        logger.info(
            "Vertical crops: %d/%d (will try rotations)",
            len(vertical_indices), len(crops),
        )

    # ── Батчевое распознавание горизонтальных кропов ──
    all_raw = [None] * len(crops)
    batch_size = BATCH_SIZE

    h_crops = [crops[i] for i in horizontal_indices]
    for batch_start in range(0, len(h_crops), batch_size):
        batch_end = min(batch_start + batch_size, len(h_crops))
        batch = h_crops[batch_start:batch_end]

        preds = rec_predictor(batch, det_predictor=det_predictor)

        for j, pred in enumerate(preds):
            orig_idx = horizontal_indices[batch_start + j]
            lines = [
                {
                    "text": line.text,
                    "bbox": [round(c) for c in line.bbox],
                    "confidence": round(line.confidence, 3),
                }
                for line in pred.text_lines
            ]
            all_raw[orig_idx] = lines

        done = min(batch_end, len(h_crops))
        if done % 50 == 0 or done == len(h_crops):
            logger.info("Surya recognize (horizontal): %d/%d", done, len(h_crops))

    # ── Вертикальные кропы с поворотом (по одному) ──
    rotation_stats = {"original": 0, "cw90": 0, "ccw90": 0}
    for count, idx in enumerate(sorted(vertical_indices)):
        lines, orientation = _recognize_with_rotation(
            crops[idx], det_predictor, rec_predictor
        )
        all_raw[idx] = lines
        rotation_stats[orientation] += 1

        if (count + 1) % 20 == 0 or (count + 1) == len(vertical_indices):
            logger.info(
                "Surya recognize (vertical): %d/%d",
                count + 1, len(vertical_indices),
            )

    if vertical_indices:
        logger.info(
            "Rotation results: orig=%d, cw90=%d, ccw90=%d",
            rotation_stats["original"], rotation_stats["cw90"], rotation_stats["ccw90"],
        )

    # Обработка результатов
    results = []
    for block, lines in zip(blocks, all_raw):
        lines = lines or []
        lines = _deduplicate_lines(lines)
        text, confidence, n_lines = _merge_lines(lines)

        results.append({
            "bbox": block["bbox"],
            "text": text,
            "confidence": round(confidence, 3),
            "source": "surya_crops",
            "origin": block.get("origin", "original"),
            "lines_found": n_lines,
        })

    # Сохранение
    out_path = Path(ocr_dir) / "surya_crops.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    with_text = sum(1 for r in results if r["text"])
    logger.info("Surya crops: %d/%d with text → %s", with_text, len(results), out_path)

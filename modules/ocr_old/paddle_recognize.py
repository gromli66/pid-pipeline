"""
Step 4b: PaddleOCR Recognition на кропах через HTTP сервис.

Вместо импорта paddle в worker_ocr (зависает из-за torch/CUDA конфликта),
отправляет кропы на отдельный paddle_service контейнер через HTTP.

Вертикальные кропы (height/width > 1.5) отправляются в 3 вариантах:
  - оригинал, +90°, −90°. Выбирается лучший по confidence.

Вход: reclustered_blocks.json + crops/
Выход: paddle_crops.json
"""

import io
import json
import logging
import os
from pathlib import Path

import httpx
from PIL import Image

from modules.ocr.utils import is_garbage_text

logger = logging.getLogger(__name__)

PADDLE_SERVICE_URL = os.environ.get("PADDLE_SERVICE_URL", "http://paddle_service:8010")
RECOGNIZE_TIMEOUT = float(os.environ.get("PADDLE_RECOGNIZE_TIMEOUT", "30"))
BATCH_SIZE = 10  # кропов за один HTTP запрос

# Порог aspect ratio для определения вертикального кропа
VERTICAL_ASPECT_THRESHOLD = 1.5


def _is_vertical_file(crop_file: Path) -> bool:
    """Определить по имени файла (xxxx_x1_y1_x2_y2.png) вертикальный ли кроп."""
    try:
        parts = crop_file.stem.split("_")
        if len(parts) >= 5:
            x1, y1, x2, y2 = int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
            w, h = x2 - x1, y2 - y1
            return w > 0 and h / w > VERTICAL_ASPECT_THRESHOLD
    except (ValueError, IndexError):
        pass
    # Fallback: открыть файл
    try:
        img = Image.open(crop_file)
        w, h = img.size
        return w > 0 and h / w > VERTICAL_ASPECT_THRESHOLD
    except Exception:
        return False


def _recognize_vertical_crop(crop_file: Path, base_url: str) -> dict:
    """
    Распознать вертикальный кроп в 3 вариантах (оригинал, +90°, −90°).
    Выбрать лучший по confidence.

    Returns:
        {"text": ..., "confidence": ...}
    """
    img = Image.open(crop_file).convert("RGB")

    variants = []
    for label, rotation in [
        ("original", None),
        ("cw90", Image.ROTATE_270),
        ("ccw90", Image.ROTATE_90),
    ]:
        variant = img.transpose(rotation) if rotation else img

        # Сериализовать в PNG bytes
        buf = io.BytesIO()
        variant.save(buf, format="PNG")
        buf.seek(0)

        try:
            resp = httpx.post(
                f"{base_url}/recognize",
                files={"file": (f"{crop_file.stem}_{label}.png", buf, "image/png")},
                timeout=RECOGNIZE_TIMEOUT,
            )
            resp.raise_for_status()
            result = resp.json()
        except Exception as e:
            logger.warning("Paddle vertical %s failed: %s", label, e)
            result = {"text": "", "confidence": 0.0}

        text = result.get("text", "").strip()
        conf = result.get("confidence", 0.0)
        variants.append((text, conf, label))

    # Выбираем лучший: наибольший confidence среди вариантов с непустым текстом
    best_text, best_conf, best_label = "", 0.0, "original"
    for text, conf, label in variants:
        if text and not is_garbage_text(text):
            if not best_text or conf > best_conf:
                best_text, best_conf, best_label = text, conf, label

    return {"text": best_text, "confidence": best_conf}


def run_paddle_recognize(blocks_json_path: str, crops_dir: str, output_path: str):
    """
    PaddleOCR recognition на кропах через HTTP сервис.

    Args:
        blocks_json_path: путь к reclustered_blocks.json
        crops_dir: папка с кропами
        output_path: путь для выходного JSON (paddle_crops.json)
    """
    with open(blocks_json_path, encoding="utf-8") as f:
        blocks = json.load(f)

    crops_path = Path(crops_dir)

    # Проверить доступность сервиса
    try:
        resp = httpx.get(f"{PADDLE_SERVICE_URL}/health", timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"Paddle service unhealthy: {resp.status_code}")
        logger.info("Paddle service healthy: %s", resp.json())
    except Exception as e:
        raise RuntimeError(f"Paddle service unavailable at {PADDLE_SERVICE_URL}: {e}")

    # Построить маппинг кропов
    crop_files = sorted(crops_path.glob("*.png"))
    crop_map = {}
    for cf in crop_files:
        idx = int(cf.stem.split("_")[0])
        crop_map[idx] = cf

    results = []
    recognized = 0

    # Разделить на вертикальные и горизонтальные
    vertical_indices = set()
    horizontal_batch_indices = []
    horizontal_batch_files = []

    for i, block in enumerate(blocks):
        crop_file = crop_map.get(i)
        if crop_file is None or not crop_file.exists():
            results.append({
                "bbox": block["bbox"],
                "text": "",
                "confidence": 0.0,
                "source": "paddle",
                "origin": block.get("origin", "original"),
            })
            continue

        if _is_vertical_file(crop_file):
            vertical_indices.add(i)
        else:
            horizontal_batch_indices.append((i, block))
            horizontal_batch_files.append(crop_file)

    if vertical_indices:
        logger.info(
            "Paddle: %d vertical crops (will try rotations), %d horizontal",
            len(vertical_indices), len(horizontal_batch_indices),
        )

    # ── Горизонтальные: батчами ──
    h_results = {}
    for batch_start in range(0, len(horizontal_batch_files), BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, len(horizontal_batch_files))
        batch_f = horizontal_batch_files[batch_start:batch_end]
        batch_i = horizontal_batch_indices[batch_start:batch_end]

        batch_results = _send_batch(batch_f, PADDLE_SERVICE_URL)
        for (idx, blk), res in zip(batch_i, batch_results):
            text = res.get("text", "").strip()
            conf = res.get("confidence", 0.0)
            is_fp = is_garbage_text(text) if text else True

            h_results[idx] = {
                "bbox": blk["bbox"],
                "text": "" if is_fp else text,
                "confidence": 0.0 if is_fp else round(conf, 3),
                "source": "paddle",
                "origin": blk.get("origin", "original"),
            }
            if text and not is_fp:
                recognized += 1

        done = min(batch_end, len(horizontal_batch_files))
        if done % 50 == 0 or done == len(horizontal_batch_files):
            logger.info("Paddle (horizontal): %d/%d", done, len(horizontal_batch_files))

    # ── Вертикальные: по одному с поворотами ──
    v_results = {}
    for count, idx in enumerate(sorted(vertical_indices)):
        crop_file = crop_map[idx]
        block = blocks[idx]
        res = _recognize_vertical_crop(crop_file, PADDLE_SERVICE_URL)

        text = res.get("text", "").strip()
        conf = res.get("confidence", 0.0)
        is_fp = is_garbage_text(text) if text else True

        v_results[idx] = {
            "bbox": block["bbox"],
            "text": "" if is_fp else text,
            "confidence": 0.0 if is_fp else round(conf, 3),
            "source": "paddle",
            "origin": block.get("origin", "original"),
        }
        if text and not is_fp:
            recognized += 1

        if (count + 1) % 20 == 0 or (count + 1) == len(vertical_indices):
            logger.info("Paddle (vertical): %d/%d", count + 1, len(vertical_indices))

    # ── Собрать results в правильном порядке ──
    final_results = []
    for i, block in enumerate(blocks):
        if i in h_results:
            final_results.append(h_results[i])
        elif i in v_results:
            final_results.append(v_results[i])
        elif len(final_results) <= i:
            # Уже добавлен в results (missing crop_file)
            pass

    # Merge: results (пустые для missing crops) + final_results
    # Проще пересобрать заново
    ordered = []
    empty_idx = 0
    for i in range(len(blocks)):
        if i in h_results:
            ordered.append(h_results[i])
        elif i in v_results:
            ordered.append(v_results[i])
        else:
            # Это был блок без crop-файла — найдём его в results
            ordered.append({
                "bbox": blocks[i]["bbox"],
                "text": "",
                "confidence": 0.0,
                "source": "paddle",
                "origin": blocks[i].get("origin", "original"),
            })

    # Сохранение
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)

    logger.info("Paddle crops: %d/%d recognized → %s", recognized, len(ordered), output_path)


def _send_batch(crop_files: list, base_url: str) -> list:
    """Отправить батч кропов на paddle_service и получить результаты."""
    files = []
    for cf in crop_files:
        files.append(("files", (cf.name, open(cf, "rb"), "image/png")))

    try:
        resp = httpx.post(
            f"{base_url}/recognize-batch",
            files=files,
            timeout=RECOGNIZE_TIMEOUT * len(crop_files),
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("Paddle batch failed: %s", e)
        return [{"text": "", "confidence": 0.0} for _ in crop_files]
    finally:
        # Закрыть файлы
        for _, (_, f, _) in files:
            f.close()

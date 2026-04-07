"""
Step 4b: PaddleOCR Recognition на кропах через HTTP сервис.

Вместо импорта paddle в worker_ocr (зависает из-за torch/CUDA конфликта),
отправляет кропы на отдельный paddle_service контейнер через HTTP.

Вход: reclustered_blocks.json + crops/
Выход: paddle_crops.json
"""

import json
import logging
import os
from pathlib import Path

import httpx

from modules.ocr.utils import is_garbage_text

logger = logging.getLogger(__name__)

PADDLE_SERVICE_URL = os.environ.get("PADDLE_SERVICE_URL", "http://paddle_service:8010")
RECOGNIZE_TIMEOUT = float(os.environ.get("PADDLE_RECOGNIZE_TIMEOUT", "30"))
BATCH_SIZE = 10  # кропов за один HTTP запрос


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

    # Обработка батчами
    batch_indices = []
    batch_files = []

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

        batch_indices.append((i, block))
        batch_files.append(crop_file)

        # Отправить батч когда набрался
        if len(batch_files) >= BATCH_SIZE:
            batch_results = _send_batch(batch_files, PADDLE_SERVICE_URL)
            for (idx, blk), res in zip(batch_indices, batch_results):
                text = res.get("text", "").strip()
                conf = res.get("confidence", 0.0)
                is_fp = is_garbage_text(text) if text else True

                results.append({
                    "bbox": blk["bbox"],
                    "text": "" if is_fp else text,
                    "confidence": 0.0 if is_fp else round(conf, 3),
                    "source": "paddle",
                    "origin": blk.get("origin", "original"),
                })
                if text and not is_fp:
                    recognized += 1

            logger.info("Paddle: %d/%d crops processed", len(results), len(blocks))
            batch_indices = []
            batch_files = []

    # Последний батч
    if batch_files:
        batch_results = _send_batch(batch_files, PADDLE_SERVICE_URL)
        for (idx, blk), res in zip(batch_indices, batch_results):
            text = res.get("text", "").strip()
            conf = res.get("confidence", 0.0)
            is_fp = is_garbage_text(text) if text else True

            results.append({
                "bbox": blk["bbox"],
                "text": "" if is_fp else text,
                "confidence": 0.0 if is_fp else round(conf, 3),
                "source": "paddle",
                "origin": blk.get("origin", "original"),
            })
            if text and not is_fp:
                recognized += 1

    # Сохранение
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    logger.info("Paddle crops: %d/%d recognized → %s", recognized, len(results), output_path)


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

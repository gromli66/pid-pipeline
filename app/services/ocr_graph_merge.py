"""
Слияние результата OCR в общий граф-JSON.

Чистая схема: текст-блоки живут в графе (`graph["text_blocks"]`), привязки —
в `graph["bindings"]`. Сырой `ocr_result.json` остаётся как есть (вывод OCR),
а этот модуль переносит его блоки в граф в точке схождения параллельных веток
(конец OCR-таска и/или создание graph_validated — «кто закончил последним»).

Идемпотентно: если в графе уже есть непустой `text_blocks`, повторное слияние
пропускается (не затирает ручные правки / повторные прогоны). Миграция старых
диаграмм не делается — только новые прогоны, где OCR идёт через этот код.
"""

import json
import logging
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]


def ocr_blocks_to_text_blocks(ocr_result: dict) -> list[dict]:
    """Преобразовать блоки ocr_result (target + secondary) в text_blocks графа."""
    blocks: list[dict] = []
    idx = 0
    # Бинд-блоками считаются только target (как в «бусине»); secondary —
    # прочие подписи, в общий слой привязки не берём.
    for group in ("target",):
        for b in (ocr_result.get(group) or []):
            bbox = b.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            idx += 1
            blocks.append({
                "id": f"block_{idx}",
                "bbox": [float(v) for v in bbox],
                "text": b.get("text", "") or "",
                "confidence": float(b.get("confidence", 0) or 0),
                "source": b.get("source") or b.get("class") or group,
                "merged_into": None,
            })
    return blocks


def merge_ocr_result_into_graph(
    graph_path: PathLike,
    ocr_result_path: PathLike,
    force: bool = False,
) -> int:
    """Слить блоки из ocr_result.json в graph["text_blocks"].

    Возвращает число перенесённых блоков (0 — если нечего сливать или пропуск).
    Идемпотентно: пропускает, если в графе уже есть text_blocks (кроме force).
    """
    graph_path = Path(graph_path)
    ocr_result_path = Path(ocr_result_path)
    if not graph_path.exists() or not ocr_result_path.exists():
        return 0

    try:
        with open(graph_path, encoding="utf-8") as f:
            graph = json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning("merge_ocr: не удалось прочитать граф %s: %s", graph_path, exc)
        return 0

    if graph.get("text_blocks") and not force:
        return 0  # уже слито / есть ручные правки — не трогаем

    try:
        with open(ocr_result_path, encoding="utf-8") as f:
            ocr = json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning("merge_ocr: не удалось прочитать OCR %s: %s", ocr_result_path, exc)
        return 0

    tblocks = ocr_blocks_to_text_blocks(ocr)
    graph["text_blocks"] = tblocks
    graph.setdefault("bindings", [])

    try:
        with open(graph_path, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning("merge_ocr: не удалось записать граф %s: %s", graph_path, exc)
        return 0

    logger.info("merge_ocr: перенесено %d блоков в %s", len(tblocks), graph_path.name)
    return len(tblocks)

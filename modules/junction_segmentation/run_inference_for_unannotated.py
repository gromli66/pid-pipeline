"""
run_inference_for_unannotated.py
================================

Специальный скрипт для итеративной доразметки датасета.

Назначение
----------
Прогнать обученную модель на тех схемах, которые ещё НЕ отвалидированы вручную
через `run_junction_editor.py`, и положить предсказанные маски junction/bridge
во вход редактора (`validated\\jb\\junction\\` и `\\bridge\\`). Дальше человек
открывает редактор, подчищает FP/FN и сохраняет в `_validated`-папки.

Тренируется → инферится на остатке → подчищается → дотренировывается. Loop.

Запуск
------
python -m junction_segmentation.run_inference_for_unannotated

Все пути жёстко прописаны в константах ниже — менять при необходимости там.

Логика
------
1. Прочитать progress.json — там список stem'ов уже отвалидированных схем.
2. Перебрать IMAGES_DIR — найти схемы без записи в progress.
3. Для каждой неотвалидированной:
   - Проверить наличие оригинала и pipe-маски.
   - Сгенерировать скелет (Zhang-Suen).
   - Прогнать модель через run_inference() с recall-first порогами.
   - Сохранить квадратные маски junction/bridge в JUNCTION_OUT / BRIDGE_OUT.
4. В конце вывести итог: обработано / пропущено / общее число точек.

Файлы в JUNCTION_OUT / BRIDGE_OUT для УЖЕ отвалидированных схем НЕ трогаются.
Скрипт безопасно перезапускать — он не перезаписывает то, чего нет в его списке.
"""

import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

import cv2
import numpy as np
import torch
from tqdm import tqdm

# Relative imports from the same package
from .config import Config
from .inference import (
    create_binary_mask,
    run_inference,
    skeletonize_mask,
)
from .model import JunctionSegModel


# ═══════════════════════════════════════════════════════════════════════════
# ПУТИ И НАСТРОЙКИ — менять здесь
# ═══════════════════════════════════════════════════════════════════════════

# Чекпоинт обученной модели
CHECKPOINT = Path(
    r"C:\project\pid\pid\app\junction_new\runs\junction_seg_14.05.2026\best.pth"
)

# Входы
IMAGES_DIR = Path(
    r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated\renders"
)
PIPE_MASKS_DIR = Path(
    r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated"
)

# Файл прогресса валидации (тот же, что использует run_junction_editor.py)
PROGRESS_FILE = Path(
    r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated\jb\progress.json"
)

# Выходы — это входы для редактора
JUNCTION_OUT = Path(
    r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated\jb\junction"
)
BRIDGE_OUT = Path(
    r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated\jb\bridge"
)
SKELETON_OUT = Path(
    r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated\jb\skeletons"
)

# Пороги модели — recall-first (для доразметки лучше иметь немного FP чем FN,
# FP видны сразу и удаляются одним кликом, FN надо искать глазами).
# Подобраны по threshold_analysis на val:
#   junction=0.20 → P=88.9% R=97.7% (пропустим ~2% точек)
#   bridge=0.30   → P=90.3% R=99.0% (пропустим ~1% мостов)
JUNCTION_THRESHOLD = 0.20
BRIDGE_THRESHOLD = 0.30

# Размер квадратов в выходной маске — должен совпадать с форматом разметки
SQUARE_SIZE = 15

# Тайлинг (читается из чекпоинта, эти значения служат запасным дефолтом)
BATCH_SIZE = 8
DEVICE = "cuda"

# Поддерживаемые расширения оригиналов
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}

# Отладка: если задан список stem'ов — обрабатывать только их (для теста на 5
# схемах перед запуском на всех). None или [] = все неотвалидированные.
SUBSET_STEMS: Optional[List[str]] = None

# Dry-run: только показать что собирается обработать, без запуска модели
DRY_RUN = False


# ═══════════════════════════════════════════════════════════════════════════
# Утилиты
# ═══════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_progress() -> Set[str]:
    """Прочитать stem'ы уже отвалидированных схем из progress.json."""
    if not PROGRESS_FILE.exists():
        logger.warning("progress.json не найден: %s — считаем что отвалидировано 0", PROGRESS_FILE)
        return set()
    try:
        import json
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        return set(data.get("validated", []))
    except Exception as exc:
        logger.error("Не удалось прочитать progress.json: %s", exc)
        sys.exit(1)


def find_pipe_mask(stem: str) -> Optional[Path]:
    """Найти pipe-маску по stem'у в PIPE_MASKS_DIR."""
    for ext in IMAGE_EXTENSIONS:
        candidate = PIPE_MASKS_DIR / (stem + ext)
        if candidate.exists():
            return candidate
    return None


def find_image(stem: str) -> Optional[Path]:
    """Найти оригинал по stem'у в IMAGES_DIR."""
    for ext in IMAGE_EXTENSIONS:
        candidate = IMAGES_DIR / (stem + ext)
        if candidate.exists():
            return candidate
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Основная процедура
# ═══════════════════════════════════════════════════════════════════════════

def main():
    # ── Проверка путей ────────────────────────────────────────────────
    for name, p in [
        ("CHECKPOINT", CHECKPOINT),
        ("IMAGES_DIR", IMAGES_DIR),
        ("PIPE_MASKS_DIR", PIPE_MASKS_DIR),
    ]:
        if not p.exists():
            logger.error("%s не существует: %s", name, p)
            sys.exit(1)

    JUNCTION_OUT.mkdir(parents=True, exist_ok=True)
    BRIDGE_OUT.mkdir(parents=True, exist_ok=True)
    SKELETON_OUT.mkdir(parents=True, exist_ok=True)

    # ── Список схем к обработке ───────────────────────────────────────
    validated = load_progress()
    logger.info("В progress.json отвалидировано: %d схем", len(validated))

    all_images = sorted(
        p for p in IMAGES_DIR.iterdir()
        if p.suffix.lower() in IMAGE_EXTENSIONS
    )
    logger.info("Всего изображений в renders: %d", len(all_images))

    pending = [p for p in all_images if p.stem not in validated]
    logger.info("Не отвалидировано: %d", len(pending))

    if SUBSET_STEMS:
        subset = set(SUBSET_STEMS)
        pending = [p for p in pending if p.stem in subset]
        logger.info("После применения SUBSET_STEMS осталось: %d", len(pending))

    if not pending:
        logger.info("Нечего обрабатывать. Выход.")
        return

    # ── Dry-run: только показать ─────────────────────────────────────
    if DRY_RUN:
        logger.info("DRY_RUN=True — модель не запускаем")
        missing_pipe = []
        for p in pending:
            if find_pipe_mask(p.stem) is None:
                missing_pipe.append(p.stem)
        logger.info("Будет обработано: %d", len(pending) - len(missing_pipe))
        logger.info("Пропущено (нет pipe-маски): %d", len(missing_pipe))
        if missing_pipe:
            logger.info("Примеры пропущенных:")
            for stem in missing_pipe[:10]:
                logger.info("  - %s", stem)
        logger.info("Куда положатся:")
        logger.info("  Junction → %s", JUNCTION_OUT)
        logger.info("  Bridge   → %s", BRIDGE_OUT)
        logger.info("  Skeleton → %s", SKELETON_OUT)
        logger.info("Пороги: junction=%.2f, bridge=%.2f", JUNCTION_THRESHOLD, BRIDGE_THRESHOLD)
        return

    # ── Загрузка модели ───────────────────────────────────────────────
    device = torch.device(DEVICE if torch.cuda.is_available() else "cpu")
    logger.info("Device: %s", device)

    ckpt = torch.load(str(CHECKPOINT), map_location="cpu", weights_only=False)
    cfg_dict = ckpt.get("config", {})
    cfg = Config()
    for k, v in cfg_dict.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)

    model = JunctionSegModel(cfg).to(device)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()
    logger.info("Модель загружена (эпоха %d)", ckpt.get("epoch", -1))
    logger.info("Пороги: junction=%.2f, bridge=%.2f", JUNCTION_THRESHOLD, BRIDGE_THRESHOLD)

    # ── Обработка ─────────────────────────────────────────────────────
    n_processed = 0
    n_skipped = 0
    skipped_reasons: Dict[str, List[str]] = {"no_pipe_mask": [], "read_error": []}
    total_j = 0
    total_b = 0
    t_start = time.time()

    pbar = tqdm(pending, desc="Inference", unit="schema")
    for img_path in pbar:
        stem = img_path.stem
        pbar.set_postfix_str(stem[:30])

        # Pipe-маска обязательна
        pipe_path = find_pipe_mask(stem)
        if pipe_path is None:
            n_skipped += 1
            skipped_reasons["no_pipe_mask"].append(stem)
            continue

        # Читаем оригинал
        img_bgr = cv2.imread(str(img_path))
        if img_bgr is None:
            logger.warning("Не удалось прочитать оригинал: %s", img_path)
            n_skipped += 1
            skipped_reasons["read_error"].append(stem)
            continue
        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        h, w = img_rgb.shape[:2]

        # Читаем pipe-маску
        pipe_mask = cv2.imread(str(pipe_path), cv2.IMREAD_GRAYSCALE)
        if pipe_mask is None:
            logger.warning("Не удалось прочитать pipe-маску: %s", pipe_path)
            n_skipped += 1
            skipped_reasons["read_error"].append(stem)
            continue

        if pipe_mask.shape != (h, w):
            logger.warning(
                "Размер pipe-маски %s != размер оригинала %s — пропуск (%s)",
                pipe_mask.shape, (h, w), stem,
            )
            n_skipped += 1
            skipped_reasons["read_error"].append(stem)
            continue

        # Скелет — на лету
        try:
            skeleton = skeletonize_mask(pipe_mask)
        except Exception as exc:
            logger.warning("Skeletonize failed для %s: %s", stem, exc)
            n_skipped += 1
            skipped_reasons["read_error"].append(stem)
            continue

        # Inference
        try:
            result = run_inference(
                model, img_rgb, pipe_mask, skeleton, device,
                tile_size=cfg.tile_size,
                overlap=cfg.val_tile_overlap,
                batch_size=BATCH_SIZE,
                junction_threshold=JUNCTION_THRESHOLD,
                bridge_threshold=BRIDGE_THRESHOLD,
                nms_kernel=cfg.nms_kernel,
                use_amp=cfg.amp,
            )
        except Exception as exc:
            logger.error("Inference failed для %s: %s", stem, exc)
            n_skipped += 1
            skipped_reasons["read_error"].append(stem)
            continue

        j_points = result["junction_points"]
        b_points = result["bridge_points"]

        # Создание масок (квадраты SQUARE_SIZE на местах точек)
        j_mask = create_binary_mask(h, w, j_points, SQUARE_SIZE)
        b_mask = create_binary_mask(h, w, b_points, SQUARE_SIZE)

        # Сохранение (расширение всегда .png — формат, ожидаемый редактором)
        cv2.imwrite(str(JUNCTION_OUT / f"{stem}.png"), j_mask)
        cv2.imwrite(str(BRIDGE_OUT / f"{stem}.png"), b_mask)
        cv2.imwrite(str(SKELETON_OUT / f"{stem}.png"), skeleton)

        n_processed += 1
        total_j += len(j_points)
        total_b += len(b_points)

    pbar.close()

    # ── Итоги ─────────────────────────────────────────────────────────
    elapsed = time.time() - t_start
    logger.info("=" * 60)
    logger.info("ГОТОВО за %.0fс (%.1f мин)", elapsed, elapsed / 60)
    logger.info("Обработано:                  %d", n_processed)
    logger.info("Пропущено (нет pipe-маски):  %d", len(skipped_reasons["no_pipe_mask"]))
    logger.info("Пропущено (ошибка чтения):   %d", len(skipped_reasons["read_error"]))

    if n_processed > 0:
        logger.info("Среднее junction'ов на схему: %.1f", total_j / n_processed)
        logger.info("Среднее bridge'ев  на схему: %.1f", total_b / n_processed)
        logger.info("Всего точек: junction=%d, bridge=%d", total_j, total_b)

    if skipped_reasons["no_pipe_mask"]:
        logger.warning("Схемы без pipe-маски (первые 10):")
        for stem in skipped_reasons["no_pipe_mask"][:10]:
            logger.warning("  - %s", stem)
        if len(skipped_reasons["no_pipe_mask"]) > 10:
            logger.warning("  ... и ещё %d", len(skipped_reasons["no_pipe_mask"]) - 10)

    logger.info("=" * 60)
    logger.info("Маски сохранены:")
    logger.info("  Junction → %s", JUNCTION_OUT)
    logger.info("  Bridge   → %s", BRIDGE_OUT)
    logger.info("  Skeleton → %s", SKELETON_OUT)
    logger.info("Дальше: запусти run_junction_editor.py и подчищай FP/FN.")


if __name__ == "__main__":
    main()

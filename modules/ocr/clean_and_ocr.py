"""
clean_and_ocr.py — Очистка P&ID (вычитание масок труб + узлов) с защитой текста → Surya OCR.

Адаптировано для серверного использования (без CLI).
Вызывается из modules/ocr/pipeline.py.

Двухпроходная схема:
  Pass 1 — Детекция текста на оригинале:
    a) Surya text detector → bbox'ы текстовых строк → protection zone
    b) Connected components (бинаризация − трубы − узлы) → мелкие символы
    c) Объединение → text_protection_mask

  Pass 2 — Очистка + OCR:
    a) removal_mask = (трубы ∪ узлы) − text_protection
    b) Замена removal_mask на белый
    c) Surya OCR на очищенном (с тайлингом)

Маски — бинарные .png (белый = объект).
"""

import json
import time
import cv2
import numpy as np
from PIL import Image
from pathlib import Path
from collections import defaultdict
from scipy.ndimage import gaussian_filter1d

# ── Конфигурация (dict — без проблем с global) ──────────

CONFIG = {
    # Surya OCR
    "languages": ["en", "ru"],
    "tile_size": 2048,
    "tile_overlap": 256,
    "tile_threshold": 4096,
    "iou_threshold": 0.5,
    "containment_threshold": 0.6,

    # Dilate масок оборудования
    "pipe_dilate": 3,
    "node_dilate": 2,

    # Защита текста — connected components
    "binarize_threshold": 200,
    "text_min_area": 8,
    "text_max_area": 25000,
    "text_min_solidity": 0.15,
    "text_max_aspect": 20.0,

    # Защита текста — padding
    "text_bbox_padding": 5,
    "text_cc_padding": 3,

    # Уточнение маски труб (thinning + run-length)
    "refine_pipes": True,           # включить уточнение
    "junction_radius": 3,           # px — радиус удаления вокруг junction point
    "min_segment_length": 25,       # px — мин. длина сегмента (короче = артефакт)
    "thickness_samples": 30,        # точек сэмплирования на сегмент
    "max_thickness": 20,            # px — cap
    "min_thickness": 2,             # px — floor
    "hist_sigma": 1.0,              # сглаживание гистограммы толщин
    "opening_size": 3,              # px — morphological opening
}


# ── Утилиты: I/O ────────────────────────────────────────

def load_image_cv2(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def load_mask(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    return (img > 127).astype(np.uint8)


def find_mask(stem, mask_dir):
    mask_dir = Path(mask_dir)
    if not mask_dir.exists():
        return None
    for ext in ['.png', '.jpg', '.jpeg', '.tif', '.tiff']:
        candidate = mask_dir / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    for f in mask_dir.iterdir():
        if f.suffix.lower() in ['.png', '.jpg', '.jpeg', '.tif', '.tiff']:
            if stem in f.stem or f.stem in stem:
                return f
    return None


def ensure_size(mask, H, W):
    if mask is None:
        return None
    if mask.shape[:2] != (H, W):
        return cv2.resize(mask, (W, H), interpolation=cv2.INTER_NEAREST)
    return mask


def dilate_mask(mask, px):
    if px <= 0 or mask is None:
        return mask
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * px + 1, 2 * px + 1))
    return cv2.dilate(mask, kernel)


# ── Утилиты: дедупликация ────────────────────────────────

def bbox_area(bbox):
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    return inter / (bbox_area(a) + bbox_area(b) - inter + 1e-6)


def containment(inner, outer):
    x1, y1 = max(inner[0], outer[0]), max(inner[1], outer[1])
    x2, y2 = min(inner[2], outer[2]), min(inner[3], outer[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area = bbox_area(inner)
    return inter / area if area > 0 else 0.0


def merge_overlapping(detections):
    thresh_iou = CONFIG["iou_threshold"]
    thresh_cont = CONFIG["containment_threshold"]
    sorted_dets = sorted(detections, key=lambda d: bbox_area(d['bbox']), reverse=True)
    remove = set()
    for i in range(len(sorted_dets)):
        if i in remove:
            continue
        for j in range(i + 1, len(sorted_dets)):
            if j in remove:
                continue
            if containment(sorted_dets[j]['bbox'], sorted_dets[i]['bbox']) > thresh_cont:
                remove.add(j)
            elif iou(sorted_dets[i]['bbox'], sorted_dets[j]['bbox']) > thresh_iou:
                remove.add(j)
    return [d for i, d in enumerate(sorted_dets) if i not in remove]


# ── Тайлинг ──────────────────────────────────────────────

def tile_image(img):
    ts = CONFIG["tile_size"]
    ov = CONFIG["tile_overlap"]
    w, h = img.size
    tiles = []
    for y in range(0, h, ts - ov):
        for x in range(0, w, ts - ov):
            x2, y2 = min(x + ts, w), min(y + ts, h)
            if (x2 - x) < 100 or (y2 - y) < 100:
                continue
            tiles.append({"image": img.crop((x, y, x2, y2)), "ox": x, "oy": y})
    return tiles


# ── Pass 1: Детекция текста (защита) ─────────────────────

def detect_text_surya(img_pil, det_predictor):
    """Surya detector → список bbox'ов текстовых строк."""
    preds = det_predictor([img_pil])
    bboxes = []
    for bbox_obj in preds[0].bboxes:
        bboxes.append([round(c) for c in bbox_obj.bbox])
    return bboxes


def detect_text_cc(binary, equipment_mask, H, W):
    """
    Connected components — мелкие символы, которые Surya мог пропустить.
    Вычитаем оборудование из бинаризации, фильтруем по форме.

    Vectorized: фильтрация по stats + LUT (один проход O(H×W)).
    """
    clean = binary.copy()
    if equipment_mask is not None:
        clean[equipment_mask > 0] = 0

    n_comp, labels, stats, _ = cv2.connectedComponentsWithStats(clean, connectivity=8)

    # Vectorized фильтрация (skip background=0)
    areas = stats[1:, cv2.CC_STAT_AREA].astype(np.float64)
    widths = stats[1:, cv2.CC_STAT_WIDTH].astype(np.float64)
    heights = stats[1:, cv2.CC_STAT_HEIGHT].astype(np.float64)

    area_ok = (areas >= CONFIG["text_min_area"]) & (areas <= CONFIG["text_max_area"])
    max_dim = np.maximum(widths, heights)
    min_dim = np.maximum(np.minimum(widths, heights), 1e-6)
    aspect_ok = (max_dim / min_dim) <= CONFIG["text_max_aspect"]
    solidity_ok = (areas / (widths * heights + 1e-6)) >= CONFIG["text_min_solidity"]

    valid = area_ok & aspect_ok & solidity_ok
    valid_labels = np.where(valid)[0] + 1  # +1: skip background
    count = len(valid_labels)

    # LUT: один проход вместо N сравнений
    if count > 0:
        lut = np.zeros(n_comp, dtype=np.uint8)
        lut[valid_labels] = 1
        text_mask = lut[labels]
    else:
        text_mask = np.zeros((H, W), dtype=np.uint8)

    return text_mask, count


def build_text_protection(img_pil, orig_gray, pipe_mask, node_mask, det_predictor, H, W):
    """
    Pass 1: Строит маску защиты текста.

    Два источника:
      a) Surya detector → bbox с padding
      b) Connected components → character-level

    Объединение — надёжная защита от «съедания» текста.
    """
    protection = np.zeros((H, W), dtype=np.uint8)

    # --- a) Surya detector ---
    print("    [1a] Surya text detection...")
    t0 = time.time()

    if max(W, H) > CONFIG["tile_threshold"]:
        tiles = tile_image(img_pil)
        all_bboxes = []
        for tile in tiles:
            bboxes = detect_text_surya(tile["image"], det_predictor)
            ox, oy = tile["ox"], tile["oy"]
            for bb in bboxes:
                all_bboxes.append([bb[0] + ox, bb[1] + oy, bb[2] + ox, bb[3] + oy])
        surya_bboxes = all_bboxes
    else:
        surya_bboxes = detect_text_surya(img_pil, det_predictor)

    pad = CONFIG["text_bbox_padding"]
    for bb in surya_bboxes:
        x1 = max(0, int(bb[0]) - pad)
        y1 = max(0, int(bb[1]) - pad)
        x2 = min(W, int(bb[2]) + pad)
        y2 = min(H, int(bb[3]) + pad)
        protection[y1:y2, x1:x2] = 1

    print(f"         Surya bbox: {len(surya_bboxes)}, {time.time() - t0:.1f}с")

    # --- b) Connected components ---
    print("    [1b] Connected components...")
    _, binary = cv2.threshold(orig_gray, CONFIG["binarize_threshold"], 255, cv2.THRESH_BINARY_INV)

    equipment_mask = np.zeros((H, W), dtype=np.uint8)
    if pipe_mask is not None:
        equipment_mask = cv2.bitwise_or(equipment_mask, pipe_mask)
    if node_mask is not None:
        equipment_mask = cv2.bitwise_or(equipment_mask, node_mask)

    cc_mask, cc_count = detect_text_cc(binary, equipment_mask, H, W)
    cc_padded = dilate_mask(cc_mask, CONFIG["text_cc_padding"])
    protection = cv2.bitwise_or(protection, cc_padded)

    total_px = np.count_nonzero(protection)
    print(f"         CC компонент: {cc_count}")
    print(f"         Защита: {total_px:,} px ({total_px / (H * W) * 100:.1f}%)")

    return protection


# ── Уточнение маски труб (thinning + run-length) ────────

def compute_distance_transform(binary):
    """
    Distance Transform на бинаризации оригинала.

    dt[r, c] = расстояние до ближайшего фонового пикселя (L2).
    На скелете трубы: dt ≈ половина реальной толщины.

    Robust к касающемуся тексту: даже если текст прилегает с одной стороны,
    с другой стороны трубы есть фон → dt = расстояние до ближнего края
    = half-thickness. Run-length этого не может — считает всё подряд.

    На 14000×10000: ~1с (C++ внутри OpenCV), 560 MB float32.
    """
    return cv2.distanceTransform(
        (binary > 0).astype(np.uint8), cv2.DIST_L2, 5)


def _find_junctions(skeleton):
    """Junction points — пиксели скелета с >=3 соседями."""
    kernel = np.array([[1, 1, 1],
                       [1, 0, 1],
                       [1, 1, 1]], dtype=np.uint8)
    neighbor_count = cv2.filter2D(skeleton, cv2.CV_16U, kernel)
    return ((skeleton > 0) & (neighbor_count >= 3)).astype(np.uint8)


def _smoothed_mode(values):
    """Mode через np.bincount + gaussian_filter1d."""
    arr = np.asarray(values, dtype=np.int32)
    arr = arr[arr > 0]
    if len(arr) == 0:
        return CONFIG["min_thickness"]
    hist = np.bincount(arr).astype(np.float64)
    sigma = CONFIG["hist_sigma"]
    if sigma > 0 and len(hist) > 3:
        hist = gaussian_filter1d(hist, sigma=sigma)
    mode_val = int(np.argmax(hist[1:]) + 1) if len(hist) > 1 else 1
    return max(CONFIG["min_thickness"], min(mode_val, CONFIG["max_thickness"]))


def _segment_thickness(dt_values):
    """
    Толщина из DT: 25th percentile вместо mode.

    На чистых участках DT ≈ half-thickness трубы (маленькая).
    Где маска раздута (текст) DT большая.
    25th percentile отсекает раздутые места.
    """
    vals = np.round(2.0 * dt_values).astype(np.int32)
    vals = vals[vals > 0]
    if len(vals) == 0:
        return CONFIG["min_thickness"]
    q25 = int(np.percentile(vals, 25))
    return max(CONFIG["min_thickness"], min(q25, CONFIG["max_thickness"]))


def _draw_segment_rect(canvas, seg_ys, seg_xs, thickness):
    """
    Рисует прямоугольник вдоль сегмента скелета (fitLine).
    Гарантирует ровные прямые края — труба = прямая линия.
    """
    if len(seg_ys) < 2:
        return

    pts = np.column_stack((seg_xs, seg_ys)).astype(np.float32)

    # fitLine → direction vector
    vx, vy, cx, cy = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).flatten()

    # Длина сегмента: проекция всех точек на direction
    proj = (pts[:, 0] - cx) * vx + (pts[:, 1] - cy) * vy
    p_min, p_max = proj.min(), proj.max()

    # 4 угла прямоугольника
    half_w = thickness / 2.0
    # Нормаль к direction
    nx, ny = -vy, vx

    # Центры концов
    x1, y1 = cx + vx * p_min, cy + vy * p_min
    x2, y2 = cx + vx * p_max, cy + vy * p_max

    corners = np.array([
        [x1 + nx * half_w, y1 + ny * half_w],
        [x1 - nx * half_w, y1 - ny * half_w],
        [x2 - nx * half_w, y2 - ny * half_w],
        [x2 + nx * half_w, y2 + ny * half_w],
    ], dtype=np.int32)

    cv2.fillConvexPoly(canvas, corners, 1)


def refine_pipe_mask(pipe_mask, binary, H, W):
    """
    Уточнение маски труб:
      1. Thinning (Zhang-Suen) → скелет
      2. Junction removal → сегменты
      3. DT → толщина (25th percentile, robust к раздутым местам)
      4. fitLine → прямоугольники вдоль оси (ровные прямые края)
      5. Junction fill + opening

    Ключевое отличие от dilate:
      - dilate ELLIPSE даёт скруглённые края и следует за смещённым скелетом
      - fitLine + rectangle даёт ровную прямую полосу → текст не захватывается
      - 25th percentile DT берёт толщину с чистых участков, игнорируя раздутые

    Returns:
        refined: uint8, уточнённая маска труб
    """
    pm = ensure_size(pipe_mask, H, W)

    # 1. Thinning
    t0 = time.time()
    skeleton = cv2.ximgproc.thinning(
        pm * 255, thinningType=cv2.ximgproc.THINNING_ZHANGSUEN)
    skeleton = (skeleton > 0).astype(np.uint8)
    print(f"    Skeleton: {np.count_nonzero(skeleton):,}px, {time.time()-t0:.1f}с")

    # 2. Junction removal → сегменты
    junctions = _find_junctions(skeleton)
    r = CONFIG["junction_radius"]
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*r+1, 2*r+1))
    junction_mask = cv2.dilate(junctions, kern)

    skeleton_cut = skeleton.copy()
    skeleton_cut[junction_mask > 0] = 0

    n_seg, labels_seg, stats_seg, _ = cv2.connectedComponentsWithStats(
        skeleton_cut, connectivity=8)
    print(f"    Raw segments: {n_seg - 1}, junctions: {np.count_nonzero(junctions):,}")

    # Фильтрация коротких
    min_len = CONFIG["min_segment_length"]
    areas = stats_seg[1:, cv2.CC_STAT_AREA]
    short_labels = np.where(areas < min_len)[0] + 1
    if len(short_labels) > 0:
        lut_kill = np.ones(n_seg, dtype=np.uint8)
        lut_kill[short_labels] = 0
        skeleton_cut *= lut_kill[labels_seg]
        n_seg, labels_seg, stats_seg, _ = cv2.connectedComponentsWithStats(
            skeleton_cut, connectivity=8)
    print(f"    Segments after filter (>={min_len}px): {n_seg - 1}")

    # 3. DT → толщина (25th percentile)
    t0 = time.time()
    dt = compute_distance_transform(binary)
    print(f"    Distance Transform: {time.time()-t0:.1f}с")

    # Pixel index
    ys_all, xs_all = np.where(labels_seg > 0)
    labs_all = labels_seg[ys_all, xs_all]
    order = np.argsort(labs_all, kind='mergesort')
    ys_sorted = ys_all[order]
    xs_sorted = xs_all[order]
    labs_sorted = labs_all[order]
    split_idx = np.searchsorted(labs_sorted, np.arange(1, n_seg + 1))

    n_samples = CONFIG["thickness_samples"]

    # 4. Рисуем прямоугольники вдоль fitLine
    refined = np.zeros((H, W), dtype=np.uint8)
    thickness_stats = []

    for seg_label in range(1, n_seg):
        lo = split_idx[seg_label - 1]
        hi = split_idx[seg_label] if seg_label < len(split_idx) else len(labs_sorted)
        seg_ys = ys_sorted[lo:hi]
        seg_xs = xs_sorted[lo:hi]

        if len(seg_ys) < 3:
            continue

        # Сэмплы DT
        k = min(n_samples, len(seg_ys))
        idx = np.linspace(0, len(seg_ys) - 1, k, dtype=int)
        dt_values = dt[seg_ys[idx], seg_xs[idx]]

        thickness = _segment_thickness(dt_values)
        thickness_stats.append(thickness)

        # Рисуем прямоугольник
        _draw_segment_rect(refined, seg_ys, seg_xs, thickness)

    del dt

    if thickness_stats:
        unique_t = sorted(set(thickness_stats))
        print(f"    Thickness range: {min(thickness_stats)}-{max(thickness_stats)}px, "
              f"unique: {unique_t}")

    # 5. Junction fill (медианная толщина)
    if thickness_stats:
        median_t = int(np.median(thickness_stats))
        junction_skel = skeleton & junction_mask
        if np.count_nonzero(junction_skel) > 0:
            r = max(1, median_t // 2)
            kern = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (2*r+1, 2*r+1))
            refined = cv2.bitwise_or(refined, cv2.dilate(junction_skel, kern))

    # 6. Opening
    open_sz = CONFIG["opening_size"]
    if open_sz > 1:
        kern_open = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (open_sz, open_sz))
        refined = cv2.morphologyEx(refined, cv2.MORPH_OPEN, kern_open)

    print(f"    Refined pipe: {np.count_nonzero(refined):,}px "
          f"(was {np.count_nonzero(pm):,}px)")

    return refined



# ── Pass 2: Очистка ──────────────────────────────────────

def clean_image(orig_color, pipe_mask, node_mask, text_protection, H, W,
                orig_gray=None):
    """
    Узлы удаляются полностью (вся маска как есть).
    Трубы: если refine_pipes=True — уточнённая маска (thinning + run-length mode),
           иначе — dilate как раньше.
    text_protection вычитается только из труб.

    orig_gray: нужен для refine_pipes (бинаризация → run-length).
    """
    # Бинаризация (нужна для refine_pipes)
    binary = None
    if orig_gray is not None:
        _, binary = cv2.threshold(
            orig_gray, CONFIG["binarize_threshold"], 255,
            cv2.THRESH_BINARY_INV)

    # Трубы
    pipes = np.zeros((H, W), dtype=np.uint8)
    if pipe_mask is not None:
        if CONFIG["refine_pipes"] and binary is not None:
            print("  Уточнение маски труб (thinning + run-length)...")
            t0 = time.time()
            pipes = refine_pipe_mask(pipe_mask, binary, H, W)
            print(f"  Уточнение труб: {time.time()-t0:.1f}с")
        else:
            pm = ensure_size(pipe_mask, H, W)
            pm = dilate_mask(pm, CONFIG["pipe_dilate"])
            pipes = cv2.bitwise_or(pipes, pm)

    # Узлы — удаляем всю маску как есть
    nodes = np.zeros((H, W), dtype=np.uint8)
    if node_mask is not None:
        nm = ensure_size(node_mask, H, W)
        nm = dilate_mask(nm, CONFIG["node_dilate"])
        nodes = cv2.bitwise_or(nodes, nm)

    equipment = cv2.bitwise_or(pipes, nodes)
    equipment_px = np.count_nonzero(equipment)

    # text_protection вычитается ТОЛЬКО из труб
    pipes_to_remove = pipes.copy()
    if text_protection is not None:
        pipes_to_remove[text_protection > 0] = 0

    # Финал: узлы всегда + трубы (с вычетом защиты)
    removal = cv2.bitwise_or(pipes_to_remove, nodes)

    removal_px = np.count_nonzero(removal)
    saved_px = equipment_px - removal_px
    total = H * W

    cleaned = orig_color.copy()
    cleaned[removal > 0] = (255, 255, 255)

    stats = {
        "equipment_px": equipment_px,
        "removal_px": removal_px,
        "saved_by_protection_px": saved_px,
        "equipment_pct": round(equipment_px / total * 100, 1),
        "removal_pct": round(removal_px / total * 100, 1),
        "saved_pct": round(saved_px / total * 100, 1),
    }

    print(f"  Оборудование: {equipment_px:,} px ({stats['equipment_pct']}%)")
    print(f"  Удалено:      {removal_px:,} px ({stats['removal_pct']}%)")
    print(f"  Спасено:      {saved_px:,} px ({stats['saved_pct']}%) — текст защищён")

    return cleaned, removal, stats


# ── OCR ──────────────────────────────────────────────────

def ocr_single(img_pil, rec_predictor, det_predictor):
    preds = rec_predictor([img_pil], det_predictor=det_predictor)
    return [
        {
            "text": line.text,
            "bbox": [round(c) for c in line.bbox],
            "confidence": round(line.confidence, 3),
        }
        for line in preds[0].text_lines
    ]


def ocr_tiled(img_pil, rec_predictor, det_predictor):
    tiles = tile_image(img_pil)
    print(f"  Тайлов: {len(tiles)} ({CONFIG['tile_size']}px, overlap={CONFIG['tile_overlap']}px)")

    all_dets = []
    for i, tile in enumerate(tiles):
        preds = rec_predictor([tile["image"]], det_predictor=det_predictor)
        ox, oy = tile["ox"], tile["oy"]
        for line in preds[0].text_lines:
            all_dets.append({
                "text": line.text,
                "bbox": [
                    round(line.bbox[0] + ox), round(line.bbox[1] + oy),
                    round(line.bbox[2] + ox), round(line.bbox[3] + oy),
                ],
                "confidence": round(line.confidence, 3),
            })
        if (i + 1) % 5 == 0 or (i + 1) == len(tiles):
            print(f"    тайл {i + 1}/{len(tiles)}, детекций: {len(all_dets)}")

    before = len(all_dets)
    result = merge_overlapping(all_dets)
    print(f"  Дедупликация: {before} → {len(result)}")
    return result


# CLI-визуализации (save_ocr_viz, save_protection_viz, get_font) удалены — server mode
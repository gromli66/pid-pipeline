"""
pipeline.py -- Многоитерационный OCR-пайплайн для P&ID.

Адаптировано из pipeline_two_pass.py для серверного использования.
Вызывается из worker/tasks/ocr.py.

Универсальный: доменная логика подключается через profile.
Итерации, очистка, OCR, уточнение масок — универсальны.
Классификация, группировка, пост-обработка — из профиля.

Мультимасштабная стратегия тайлинга:
  Итерация 1: tile=2048, overlap=256  (базовый масштаб)
  Итерация 2: tile=1536, overlap=256  (zoom in — мелкий текст DN, номера)
  Итерация 3: tile=2560, overlap=384  (zoom out — больше контекста)

API:
  from modules.ocr.pipeline import run_ocr_pipeline
  result = run_ocr_pipeline(image_path, pipe_mask_path, ...)
"""

import json
import logging
import time
import cv2
import numpy as np
from PIL import Image
from pathlib import Path

from modules.ocr.clean_and_ocr import (
    CONFIG,
    load_image_cv2, load_mask, ensure_size, dilate_mask,
    build_text_protection, clean_image,
    ocr_tiled, ocr_single, merge_overlapping,
)


def _dump_debug(output_dir: Path, stage: str, blocks: list, label: str = ""):
    """Сохранить промежуточный результат в output_dir/debug/."""
    debug_dir = output_dir / "debug"
    debug_dir.mkdir(exist_ok=True)
    path = debug_dir / f"{stage}.json"
    # Безопасная сериализация numpy типов
    import numpy as np

    class _Enc(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, (np.integer,)):
                return int(o)
            if isinstance(o, (np.floating,)):
                return float(o)
            if isinstance(o, np.ndarray):
                return o.tolist()
            return super().default(o)

    data = {
        "stage": stage,
        "label": label,
        "count": len(blocks),
        "blocks": blocks,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, cls=_Enc)
    logger.info(f"[debug] {stage}: {len(blocks)} blocks → {path.name}")

from modules.ocr.evaluate import semantic_regroup, cluster_split_bbox

from modules.ocr.domain_profile import BaseDomainProfile

import re as _re

logger = logging.getLogger(__name__)


class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


# ── Уточнение маски труб: перпендикулярное сечение ─
# (полностью универсальное — не зависит от домена)

def _measure_perp_width(gray, vx, vy, cx, cy, t_val, scan_r,
                        thresh, W, H):
    nx, ny = -vy, vx
    pcx = cx + vx * t_val
    pcy = cy + vy * t_val
    offs = np.arange(-scan_r, scan_r + 1)
    sxx = (pcx + nx * offs).astype(int)
    syy = (pcy + ny * offs).astype(int)
    vld = (sxx >= 0) & (sxx < W) & (syy >= 0) & (syy < H)
    if np.sum(vld) < 3:
        return 0, 0.0
    gv = gray[syy[vld], sxx[vld]]
    dark = gv < thresh
    if not np.any(dark):
        return 0, 0.0
    dd = np.diff(np.concatenate([[0], dark.astype(np.int8), [0]]))
    starts = np.where(dd == 1)[0]
    ends = np.where(dd == -1)[0]
    center = np.searchsorted(np.where(vld)[0], scan_r)
    for s, e in zip(starts, ends):
        if s <= center + 2 and e >= center - 2:
            width = e - s
            valid_indices = np.where(vld)[0]
            run_center = (valid_indices[s] + valid_indices[min(e - 1, len(valid_indices) - 1)]) / 2.0
            offset = run_center - scan_r
            return width, offset
    return 0, 0.0


def refine_pipe_mask_perp(pipe_mask, gray, junctions, bridges, H, W):
    from skimage.morphology import skeletonize
    pm = ensure_size(pipe_mask, H, W)
    skel = skeletonize(pm > 0).astype(np.uint8)
    k3 = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], dtype=np.uint8)
    nc = cv2.filter2D(skel.astype(np.uint16), cv2.CV_16U, k3)
    auto_j = ((skel > 0) & (nc >= 3)).astype(np.uint8)
    r_auto = CONFIG["junction_radius"]
    kj = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*r_auto+1, 2*r_auto+1))
    junc_mask = cv2.dilate(auto_j, kj)
    dt_mask = cv2.distanceTransform(pm, cv2.DIST_L2, 5)
    vals = dt_mask[dt_mask > 0]
    med_hw = float(np.median(vals)) if len(vals) > 0 else 5.0
    cut_r = int(med_hw * 2) + 1
    for x, y in junctions + bridges:
        cv2.circle(junc_mask, (int(x), int(y)), cut_r, 1, -1)
    skel_cut = skel.copy()
    skel_cut[junc_mask > 0] = 0
    n_seg, labels, stats, _ = cv2.connectedComponentsWithStats(skel_cut, 8)
    min_len = CONFIG["min_segment_length"]
    areas = stats[1:, cv2.CC_STAT_AREA]
    short = np.where(areas < min_len)[0] + 1
    if len(short) > 0:
        lut = np.ones(n_seg, dtype=np.uint8)
        lut[short] = 0
        skel_cut *= lut[labels]
        n_seg, labels, stats, _ = cv2.connectedComponentsWithStats(skel_cut, 8)
    logger.info(f"Segments: {n_seg - 1}, "
                f"junctions: {np.count_nonzero(auto_j):,} auto + "
                f"{len(junctions)} ann + {len(bridges)} bridge")
    ya, xa = np.where(labels > 0)
    la = labels[ya, xa]
    del labels
    o = np.argsort(la, kind='mergesort')
    ys, xs, ls = ya[o], xa[o], la[o]
    del ya, xa, la, o
    si = np.searchsorted(ls, np.arange(1, n_seg + 1))
    thresh = CONFIG["binarize_threshold"]
    scan_r = CONFIG["max_thickness"] + 5
    n_samples = CONFIG["thickness_samples"]
    refined = np.zeros((H, W), dtype=np.uint8)
    thicknesses = []
    for sl in range(1, n_seg):
        lo = si[sl - 1]
        hi = si[sl] if sl < len(si) else len(ls)
        seg_ys, seg_xs = ys[lo:hi], xs[lo:hi]
        if len(seg_ys) < 3:
            continue
        pts = np.column_stack((seg_xs, seg_ys)).astype(np.float32)
        vx, vy, cx, cy = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).flatten()
        proj = (pts[:, 0] - cx) * vx + (pts[:, 1] - cy) * vy
        p_min, p_max = float(proj.min()), float(proj.max())
        k = min(n_samples, max(3, int(p_max - p_min)))
        t_vals = np.linspace(p_min, p_max, k)
        widths = []
        offsets = []
        for tv in t_vals:
            w, off = _measure_perp_width(gray, vx, vy, cx, cy, tv, scan_r, thresh, W, H)
            if w > 0:
                widths.append(w)
                offsets.append(off)
        if not widths:
            continue
        med_offset = float(np.median(offsets))
        nx_, ny_ = -vy, vx
        cx_corr = cx + nx_ * med_offset
        cy_corr = cy + ny_ * med_offset
        perp_t = int(np.percentile(widths, 10))
        mask_vals = dt_mask[seg_ys, seg_xs]
        mask_vals_pos = mask_vals[mask_vals > 0]
        if len(mask_vals_pos) > 0:
            mask_t = int(np.round(2.0 * np.median(mask_vals_pos)))
            t = max(CONFIG["min_thickness"], min(perp_t, mask_t, CONFIG["max_thickness"]))
        else:
            t = max(CONFIG["min_thickness"], min(perp_t, CONFIG["max_thickness"]))
        thicknesses.append(t)
        hw = t / 2.0
        extend = hw
        x1, y1 = cx_corr + vx * (p_min - extend), cy_corr + vy * (p_min - extend)
        x2, y2 = cx_corr + vx * (p_max + extend), cy_corr + vy * (p_max + extend)
        corners = np.array([
            [x1 + nx_ * hw, y1 + ny_ * hw], [x1 - nx_ * hw, y1 - ny_ * hw],
            [x2 - nx_ * hw, y2 - ny_ * hw], [x2 + nx_ * hw, y2 + ny_ * hw],
        ], dtype=np.int32)
        cv2.fillConvexPoly(refined, corners, 1)
    if thicknesses:
        unique_t = sorted(set(thicknesses))
        logger.info(f"Thickness: {min(thicknesses)}-{max(thicknesses)}px, unique: {unique_t}")
    del dt_mask
    open_sz = CONFIG["opening_size"]
    if open_sz > 1:
        k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (open_sz, open_sz))
        refined = cv2.morphologyEx(refined, cv2.MORPH_OPEN, k_open)

    # Восстанавливаем зоны junction/bridge
    for x, y in junctions + bridges:
        x1c = max(0, x - cut_r); y1c = max(0, y - cut_r)
        x2c = min(W, x + cut_r + 1); y2c = min(H, y + cut_r + 1)
        refined[y1c:y2c, x1c:x2c] = 1
    return refined


# ── Стирание текста / очистка артефактов (универсальные) ──

def cleanup_removal_edges(cleaned_bgr, removal_mask, orig_gray, thresh=None):
    if thresh is None:
        thresh = CONFIG["binarize_threshold"]
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    border = cv2.dilate(removal_mask, kern) - removal_mask
    dark_border = (border > 0) & (orig_gray < thresh)
    cleaned_bgr[dark_border] = (255, 255, 255)
    n_cleaned = int(np.count_nonzero(dark_border))
    return cleaned_bgr, n_cleaned


def erase_text_in_boxes(image_bgr, target_boxes, shrink_px=7,
                        binarize_thresh=200, dilate_px=2):
    result = image_bgr.copy()
    H, W = result.shape[:2]
    gray = cv2.cvtColor(result, cv2.COLOR_BGR2GRAY)
    erased_count = 0
    kernel = None
    if dilate_px > 0:
        ks = 2 * dilate_px + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ks, ks))
    for box in target_boxes:
        bb = box['bbox']
        x1 = max(0, int(bb[0]) + shrink_px); y1 = max(0, int(bb[1]) + shrink_px)
        x2 = min(W, int(bb[2]) - shrink_px); y2 = min(H, int(bb[3]) - shrink_px)
        if x2 <= x1 or y2 <= y1:
            continue
        roi_gray = gray[y1:y2, x1:x2]
        _, text_mask = cv2.threshold(roi_gray, binarize_thresh, 255, cv2.THRESH_BINARY_INV)
        if kernel is not None:
            text_mask = cv2.dilate(text_mask, kernel)
        roi_color = result[y1:y2, x1:x2]
        roi_color[text_mask > 0] = (255, 255, 255)
        erased_count += 1
    return result, erased_count


def denoise_erase_artifacts(image_bgr, max_area=15, binarize_thresh=200):
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, binarize_thresh, 255, cv2.THRESH_BINARY_INV)
    n_cc, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    areas = stats[1:, cv2.CC_STAT_AREA]
    small_labels = np.where(areas < max_area)[0] + 1
    removed = len(small_labels)
    if removed > 0:
        lut = np.zeros(n_cc, dtype=np.uint8)
        lut[small_labels] = 1
        remove_mask = lut[labels]
        image_bgr[remove_mask > 0] = (255, 255, 255)
    return image_bgr, removed


# ── OCR обёртки (универсальные) ──────────────────

def run_ocr(img_pil, rec_predictor, det_predictor, W, H):
    if max(W, H) > CONFIG["tile_threshold"]:
        detections = ocr_tiled(img_pil, rec_predictor, det_predictor)
    else:
        detections = ocr_single(img_pil, rec_predictor, det_predictor)
    before = len(detections)
    detections = merge_overlapping(detections)
    if before != len(detections):
        logger.info(f"Post-merge: {before} → {len(detections)}")
    return detections


def compute_line_pitch(detections):
    pitches = []
    for b in detections:
        text = b.get('text', '')
        if '<br>' in text:
            n_lines = len([l for l in text.split('<br>') if l.strip()])
            if n_lines >= 2:
                h = b['bbox'][3] - b['bbox'][1]
                if h > 0:
                    pitches.append(h / n_lines)
    if len(pitches) < 3:
        return None
    return float(np.median(pitches))


# ── Дедупликация (универсальная) ─

def _bbox_area(bb):
    return max(0, bb[2]-bb[0]) * max(0, bb[3]-bb[1])

def _iou(a, b):
    x1 = max(a[0],b[0]); y1 = max(a[1],b[1])
    x2 = min(a[2],b[2]); y2 = min(a[3],b[3])
    inter = max(0, x2-x1) * max(0, y2-y1)
    u = _bbox_area(a) + _bbox_area(b) - inter
    return inter / u if u > 0 else 0

def _normalize_text_for_dedup(text):
    return _re.sub(r'[^A-Za-z0-9]', '', text).upper()


def lookup_confidence(block, detections):
    bb = block['bbox']; best_conf = 0.0; best_iou = 0.0
    for d in detections:
        db = d['bbox']
        x1 = max(bb[0], db[0]); y1 = max(bb[1], db[1])
        x2 = min(bb[2], db[2]); y2 = min(bb[3], db[3])
        inter = max(0, x2-x1) * max(0, y2-y1)
        a1 = max(1, (bb[2]-bb[0]) * (bb[3]-bb[1]))
        a2 = max(1, (db[2]-db[0]) * (db[3]-db[1]))
        iou_val = inter / (a1 + a2 - inter + 1e-6)
        cont = inter / min(a1, a2) if min(a1, a2) > 0 else 0
        score = max(iou_val, cont)
        if score > best_iou:
            best_iou = score; best_conf = d.get('confidence', 0)
    return best_conf


def dedup_rotated(blocks, iou_thresh=0.3):
    if len(blocks) <= 1:
        return blocks
    remove = set()
    for i in range(len(blocks)):
        if i in remove: continue
        bi = blocks[i]['bbox']; ti = _normalize_text_for_dedup(blocks[i].get('text',''))
        ci = blocks[i].get('confidence', 0)
        for j in range(i + 1, len(blocks)):
            if j in remove: continue
            bj = blocks[j]['bbox']; tj = _normalize_text_for_dedup(blocks[j].get('text',''))
            cj = blocks[j].get('confidence', 0)
            is_dup = (_iou(bi, bj) > iou_thresh or (ti and tj and ti == tj))
            if is_dup:
                remove.add(j if ci >= cj else i)
                if i in remove: break
    return [b for i, b in enumerate(blocks) if i not in remove]


def _block_group(block, profile):
    text = block.get('text', '')
    cls = profile.classify(text)
    role = profile.category_role(cls)
    if role == 'standalone':
        return 'standalone'
    if role in ('full', 'head'):
        return 'target'
    return 'other'


def filter_new_only(candidates, base, profile, iou_thresh=0.15):
    base_target_texts = set()
    for b in base:
        if _block_group(b, profile) == 'target':
            t = _normalize_text_for_dedup(b.get('text', ''))
            if t: base_target_texts.add(t)
    result = []
    for cand in candidates:
        cand_group = _block_group(cand, profile)
        ct = _normalize_text_for_dedup(cand.get('text', ''))
        same_group_overlap = False
        for b in base:
            if _iou(cand['bbox'], b['bbox']) > iou_thresh:
                if _block_group(b, profile) == cand_group:
                    same_group_overlap = True; break
        if same_group_overlap: continue
        if cand_group == 'target' and ct and ct in base_target_texts: continue
        result.append(cand)
    return result


# ── Главная функция pipeline ─────────────────────

def run_ocr_pipeline(
    image_path: Path,
    pipe_mask_path: Path | None,
    node_mask_path: Path | None,
    junction_points: list[tuple[int, int]],
    bridge_points: list[tuple[int, int]],
    profile: BaseDomainProfile,
    output_dir: Path,
    *,
    rec_predictor=None,
    det_predictor=None,
    no_protection: bool = True,
    save_intermediate: bool = False,
    shrink_px: int = 7,
    erase_thresh: int = 200,
    erase_dilate: int = 2,
    tile2_size: int = 1536,
    tile2_overlap: int = 256,
    tile3_size: int = 2560,
    tile3_overlap: int = 384,
) -> dict:
    """
    Запустить 3-проходный OCR pipeline для одного изображения.

    Args:
        image_path: путь к оригинальному изображению
        pipe_mask_path: путь к маске труб (или None)
        node_mask_path: путь к маске узлов (или None)
        junction_points: список (x, y) точек junctions
        bridge_points: список (x, y) точек bridges
        profile: экземпляр BaseDomainProfile
        output_dir: директория для результатов
        rec_predictor: Surya RecognitionPredictor (если None — загрузит)
        det_predictor: Surya DetectionPredictor (если None — загрузит)
        no_protection: True = не строить text protection mask
        save_intermediate: True = сохранять cleaned_*.png
        shrink_px: отступ при стирании текста
        erase_thresh: порог бинаризации для стирания
        erase_dilate: dilate при стирании
        tile2_size: размер тайла итерации 2
        tile2_overlap: overlap тайлов итерации 2
        tile3_size: размер тайла итерации 3
        tile3_overlap: overlap тайлов итерации 3

    Returns:
        {
            "profile": "KKS (Росатом)",
            "target": [{"bbox": [...], "text": "...", "source": "iter1", "confidence": 0.95}, ...],
            "secondary": [{"bbox": [...], "text": "..."}, ...],
            "stats": {"iter1_target": N, ..., "time_total_sec": T}
        }
    """
    t_total = time.time()

    # ── Настройки CONFIG (локальные, не мутируем глобально) ──
    CONFIG["languages"] = profile.languages
    CONFIG["pipe_dilate"] = 0
    CONFIG["refine_pipes"] = False

    # ── Загрузка моделей Surya если не переданы ──
    if rec_predictor is None or det_predictor is None:
        logger.info("Loading Surya models...")
        from surya.foundation import FoundationPredictor
        from surya.recognition import RecognitionPredictor
        from surya.detection import DetectionPredictor
        foundation = FoundationPredictor()
        rec_predictor = RecognitionPredictor(foundation)
        det_predictor = DetectionPredictor()
        logger.info("Surya models loaded.")

    # ── Загрузка изображения ──
    image_path = Path(image_path)
    orig = load_image_cv2(image_path)
    if orig is None:
        raise ValueError(f"Cannot load image: {image_path}")

    H, W = orig.shape[:2]
    orig_gray = cv2.cvtColor(orig, cv2.COLOR_BGR2GRAY)
    logger.info(f"Image: {image_path.name}, {W}x{H}")

    # ── Маски ──
    pipe_mask = None
    if pipe_mask_path and Path(pipe_mask_path).exists():
        pipe_mask = ensure_size(load_mask(str(pipe_mask_path)), H, W)

    node_mask = None
    if node_mask_path and Path(node_mask_path).exists():
        node_mask = ensure_size(load_mask(str(node_mask_path)), H, W)

    has_masks = pipe_mask is not None or node_mask is not None

    # Уточнение маски труб
    if pipe_mask is not None and junction_points:
        pipe_mask = refine_pipe_mask_perp(
            pipe_mask, orig_gray,
            list(junction_points), list(bridge_points),
            H, W)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = image_path.stem

    # ══════════ ИТЕРАЦИЯ 1 ══════════
    logger.info("── Iteration 1 ──")
    text_protection = None
    if has_masks and not no_protection:
        logger.info("[1] Text detection for protection...")
        img_pil_orig = Image.fromarray(cv2.cvtColor(orig, cv2.COLOR_BGR2RGB))
        text_protection = build_text_protection(
            img_pil_orig, orig_gray, pipe_mask, node_mask, det_predictor, H, W)
        if node_mask is not None and text_protection is not None:
            text_protection[node_mask > 0] = 0

    if has_masks:
        logger.info("[1] Cleaning (pipes + nodes)...")
        cleaned_1, removal_mask, clean_stats = clean_image(
            orig, pipe_mask, node_mask, text_protection, H, W, orig_gray=orig_gray)
        cleaned_1, edge_cleaned = cleanup_removal_edges(cleaned_1, removal_mask, orig_gray)
        if edge_cleaned > 0:
            logger.info(f"[1] Edge cleanup: {edge_cleaned:,}px")
    else:
        cleaned_1 = orig.copy()

    cleaned_1_path = output_dir / f"{stem}_cleaned_1.png"
    if save_intermediate:
        cv2.imencode('.png', cleaned_1)[1].tofile(str(cleaned_1_path))

    logger.info("[1] Surya OCR...")
    t1 = time.time()
    img_pil_1 = Image.fromarray(cv2.cvtColor(cleaned_1, cv2.COLOR_BGR2RGB))
    detections_1 = run_ocr(img_pil_1, rec_predictor, det_predictor, W, H)
    logger.info(f"[1] Blocks: {len(detections_1)}, OCR: {time.time()-t1:.1f}s")
    _dump_debug(output_dir, "01_iter1_raw", detections_1, "Surya raw detections iter1")

    # Classify iter 1
    # semantic_regroup needs image_path for cluster_split_bbox
    tmp_cleaned_1 = output_dir / f"{stem}_tmp_c1.png"
    cv2.imencode('.png', cleaned_1)[1].tofile(str(tmp_cleaned_1))

    blocks_iter1 = semantic_regroup(detections_1, profile,
                                    image_path=str(tmp_cleaned_1), debug=True)
    target_1 = [b for b in blocks_iter1 if b.get('is_target', False)]
    logger.info(f"[1] → {len(target_1)} target")
    _dump_debug(output_dir, "02_iter1_regroup", blocks_iter1, "After semantic_regroup (all)")
    _dump_debug(output_dir, "03_iter1_target", target_1, "iter1 target only")

    line_pitch = compute_line_pitch(detections_1)
    all_heights = [b['bbox'][3]-b['bbox'][1] for b in detections_1 if b['bbox'][3]>b['bbox'][1]]
    all_med_h = float(np.median(all_heights)) if all_heights else None

    # ══════════ СТИРАНИЕ target_1 → cleaned_2 ══════════
    logger.info(f"── Erasing target_1 ({len(target_1)} boxes) ──")
    cleaned_2, n1 = erase_text_in_boxes(
        cleaned_1, target_1, shrink_px=shrink_px,
        binarize_thresh=erase_thresh, dilate_px=erase_dilate)
    cleaned_2, dn1 = denoise_erase_artifacts(cleaned_2)

    # ══════════ ИТЕРАЦИЯ 2 ══════════
    logger.info(f"── Iteration 2 (tile={tile2_size}, overlap={tile2_overlap}) — zoom in ──")
    orig_tile_size = CONFIG["tile_size"]
    orig_tile_overlap = CONFIG["tile_overlap"]
    CONFIG["tile_size"] = tile2_size
    CONFIG["tile_overlap"] = tile2_overlap

    t2 = time.time()
    img_pil_2 = Image.fromarray(cv2.cvtColor(cleaned_2, cv2.COLOR_BGR2RGB))
    detections_2 = run_ocr(img_pil_2, rec_predictor, det_predictor, W, H)
    CONFIG["tile_size"] = orig_tile_size
    CONFIG["tile_overlap"] = orig_tile_overlap
    logger.info(f"[2] Blocks: {len(detections_2)}, OCR: {time.time()-t2:.1f}s")
    _dump_debug(output_dir, "04_iter2_raw", detections_2, "Surya raw detections iter2")

    tmp_cleaned_2 = output_dir / f"{stem}_tmp_c2.png"
    cv2.imencode('.png', cleaned_2)[1].tofile(str(tmp_cleaned_2))

    blocks_iter2 = semantic_regroup(detections_2, profile,
                                    image_path=str(tmp_cleaned_2), debug=True)
    target_2 = [b for b in blocks_iter2 if b.get('is_target', False)]
    logger.info(f"[2] → {len(target_2)} target")
    _dump_debug(output_dir, "05_iter2_target", target_2, "iter2 target only")

    # ══════════ ПОДГОТОВКА ИТЕРАЦИИ 3 ══════════
    logger.info("── Preparing iter 3 ──")
    cleaned_3 = cleaned_1.copy()
    all_confirmed = target_1 + target_2
    cleaned_3, n_erased = erase_text_in_boxes(
        cleaned_3, all_confirmed, shrink_px=shrink_px,
        binarize_thresh=erase_thresh, dilate_px=erase_dilate)
    cleaned_3, dn3 = denoise_erase_artifacts(cleaned_3)

    if node_mask is not None:
        cleaned_3[node_mask > 0] = orig[node_mask > 0]

    # ══════════ ИТЕРАЦИЯ 3 ══════════
    logger.info(f"── Iteration 3 (tile={tile3_size}, overlap={tile3_overlap}) — zoom out ──")
    CONFIG["tile_size"] = tile3_size
    CONFIG["tile_overlap"] = tile3_overlap

    t3 = time.time()
    img_pil_3 = Image.fromarray(cv2.cvtColor(cleaned_3, cv2.COLOR_BGR2RGB))
    detections_3 = run_ocr(img_pil_3, rec_predictor, det_predictor, W, H)
    CONFIG["tile_size"] = orig_tile_size
    CONFIG["tile_overlap"] = orig_tile_overlap
    logger.info(f"[3] Blocks: {len(detections_3)}, OCR: {time.time()-t3:.1f}s")
    _dump_debug(output_dir, "06_iter3_raw", detections_3, "Surya raw detections iter3")

    tmp_cleaned_3 = output_dir / f"{stem}_tmp_c3.png"
    cv2.imencode('.png', cleaned_3)[1].tofile(str(tmp_cleaned_3))

    blocks_iter3 = semantic_regroup(detections_3, profile,
                                    image_path=str(tmp_cleaned_3), debug=True)
    _dump_debug(output_dir, "07_iter3_regroup", blocks_iter3, "After semantic_regroup iter3")

    # Пост-обработка через профиль
    blocks_iter3 = profile.postprocess_merge_secondary(
        blocks_iter3, max_vgap=line_pitch, max_hgap=line_pitch,
        fallback_med_h=all_med_h)

    blocks_iter3 = profile.postprocess_extract_target_from_merged(
        blocks_iter3, image_path=str(tmp_cleaned_3))

    target_3 = [b for b in blocks_iter3 if b.get('is_target', False)]
    logger.info(f"[3] → {len(target_3)} target")
    _dump_debug(output_dir, "08_iter3_target", target_3, "iter3 target after postprocess")

    # Secondary script blocks
    all_secondary = profile.collect_secondary_from_raw(
        detections_3, line_pitch=line_pitch, fallback_med_h=all_med_h)
    logger.info(f"[3] Secondary: {len(all_secondary)}")

    # ══════════ ФИНАЛ ══════════
    elapsed_total = time.time() - t_total

    base_target = []
    for b in target_1:
        base_target.append({'bbox': b['bbox'], 'text': b['text'], 'source': 'iter1',
            'confidence': lookup_confidence(b, detections_1)})
    for b in target_2:
        base_target.append({'bbox': b['bbox'], 'text': b['text'], 'source': 'iter2',
            'confidence': lookup_confidence(b, detections_2)})
    for b in target_3:
        base_target.append({'bbox': b['bbox'], 'text': b['text'], 'source': 'iter3',
            'confidence': lookup_confidence(b, detections_3)})

    # Iter 4 (rotated) — отключена
    all_target = base_target
    _dump_debug(output_dir, "09_base_target", base_target, "Merged iter1+iter2+iter3 before postprocess")

    # Пост-обработка через профиль
    all_target = profile.postprocess_split_multi(all_target)
    _dump_debug(output_dir, "10_after_split_multi", all_target, "After postprocess_split_multi")

    promoted, n_promoted = profile.postprocess_promote_from_secondary(
        all_secondary, all_target)
    if n_promoted > 0:
        all_target.extend(promoted)
    _dump_debug(output_dir, "11_final_target", all_target, "Final target (after promote)")

    logger.info(f"── Final ──")
    logger.info(f"Base (iter1-3): {len(base_target)}")
    logger.info(f"Total target: {len(all_target)}")
    logger.info(f"Secondary: {len(all_secondary)}")
    logger.info(f"Time: {elapsed_total:.1f}s")

    # Cleanup tmp files
    for tmp in [tmp_cleaned_1, tmp_cleaned_2, tmp_cleaned_3]:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    if not save_intermediate:
        try:
            cleaned_1_path.unlink(missing_ok=True)
        except OSError:
            pass

    final_data = {
        "profile": profile.name,
        "target": [{'bbox': b['bbox'], 'text': b['text'],
                    'source': b.get('source', ''),
                    'confidence': round(b.get('confidence', 0), 3)}
                   for b in all_target],
        "secondary": [{'bbox': b['bbox'], 'text': b['text']}
                      for b in all_secondary],
        "stats": {
            "iter1_target": len(target_1),
            "iter2_target": len(target_2),
            "iter3_target": len(target_3),
            "total_target": len(all_target),
            "total_secondary": len(all_secondary),
            "time_total_sec": round(elapsed_total, 1),
        },
    }

    return final_data

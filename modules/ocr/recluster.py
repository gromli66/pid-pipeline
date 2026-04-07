"""
Рекластеризация OCR-блоков для P&ID диаграмм. v4

Пайплайн:
  1. KKS horizontal split — разделение широких блоков, где Surya склеил
     два KKS-кода / компонента в одну строку ("10LAB30 10LAB30" → 2 блока).
     Regex на тексте + конфиг KKS unit codes (PAKSII-PMM-01.1.01.01).
  2. Vertical <br> split — разделение многострочных блоков на атомарные строки.
  3. Union-Find merge — объединение атомарных строк в целевые блоки
     по 4 нормализованным пространственным критериям.
  4. Дедупликация перекрывающихся блоков.
  5. Фильтрация мусора.

Изменения v4 (vs v3):
  - KKS regex split вместо CC cluster split → +1.1% F1, 0 регрессий
  - Union-Find merge с нормализованными порогами вместо абсолютных
  - Изображение больше НЕ нужно для split (только для scale check / визуализации)
  - OCR-коррекция кириллица→латиница при классификации токенов

Метрики (63 страницы, 15 115 целевых):
  F1@IoU>0.5 = 0.858  (Recall=0.825, Precision=0.895)

Использование из task:
  from modules.ocr.recluster import run_recluster
  run_recluster(surya_json, cleaned_path, original_path, output_dir)
"""

import re
import json
import logging
import argparse
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)


# ============================================================
# Data structures (совместимость с crop_saver, merge_results)
# ============================================================
@dataclass
class TextBlock:
    bbox: List[int]          # [x1, y1, x2, y2]
    text: str = ""
    confidence: float = 1.0
    source: str = "surya"
    origin: str = "original"  # original / split / merged
    parent_id: int = -1       # id родительского блока (для split)

    @property
    def x1(self): return self.bbox[0]
    @property
    def y1(self): return self.bbox[1]
    @property
    def x2(self): return self.bbox[2]
    @property
    def y2(self): return self.bbox[3]
    @property
    def w(self): return self.bbox[2] - self.bbox[0]
    @property
    def h(self): return self.bbox[3] - self.bbox[1]
    @property
    def cx(self): return (self.bbox[0] + self.bbox[2]) / 2
    @property
    def cy(self): return (self.bbox[1] + self.bbox[3]) / 2
    @property
    def area(self): return self.w * self.h


# ============================================================
# KKS Configuration (PAKSII-PMM-01.1.01.01)
# ============================================================

KNOWN_UNIT_CODES = frozenset({
    "AA", "AC", "AH", "AP", "AT",
    "BB", "BR", "BP", "BQ",
    "CF", "CG", "CL", "CM", "CP", "CQ", "CR", "CT", "CY",
    "GF", "GH",
})

# Cyrillic → Latin (визуально похожие символы, частые ошибки OCR)
_CYR2LAT = {
    "А": "A", "В": "B", "С": "C", "Е": "E", "К": "K", "М": "M",
    "Н": "H", "О": "O", "Р": "P", "Т": "T", "У": "Y", "Х": "X",
    "а": "A", "в": "B", "с": "C", "е": "E", "к": "K", "м": "M",
    "н": "H", "о": "O", "р": "P", "т": "T", "у": "Y", "х": "X",
}

# Full KKS: Block(1-2d) + System(2-3L) + FN(2-3d) + Unit(2L) + Num(2-4d) + Suffix?
_FULL_KKS_RE = re.compile(
    r"\d{1,2}[A-Za-z]{2,3}\d{2,3}[A-Za-z]{2}\d{2,4}[A-Za-z]?"
)

# First part: Block + System + FN  (10LAB30, 10PCD04)
_KKS_FIRST_RE = re.compile(r"^\d{1,2}[A-Za-z]{2,4}\d{1,3}$")

# Component: KnownUnit + Number  (AA001, CP501, BB01)
_UNIT_ALT = "|".join(sorted(KNOWN_UNIT_CODES, key=len, reverse=True))
_KKS_COMP_RE = re.compile(rf"^(?:{_UNIT_ALT})\d{{1,4}}[A-Za-z]?$")


# ============================================================
# Merge parameters (grid search, 63 pages)
# ============================================================

DEFAULT_MERGE_PARAMS = {
    "v_gap_thresh": 0.20,       # макс. верт. зазор (норм. по средней высоте)
    "h_overlap_thresh": 0.90,   # мин. гориз. перекрытие (от мин. ширины)
    "center_thresh": 0.30,      # макс. смещение центров (от макс. ширины)
    "h_gap_thresh": 0.10,       # макс. гориз. зазор (норм. по средней высоте)
    "v_gap_min": -0.5,          # допуск перекрытия по вертикали
    "neighbor_window": 30,      # кол-во соседей при поиске
    "y_cutoff_factor": 3.0,     # множитель для y-отсечки
}


# ============================================================
# Этап 1: KKS Horizontal Split
# ============================================================

def _fix_ocr(text: str) -> str:
    """Normalize cyrillic look-alikes → latin for KKS matching."""
    return "".join(_CYR2LAT.get(ch, ch) for ch in text)


# Designation pattern: Dy25, Dy250, Jv5, Tv5, Ду25 etc.
_DESIGNATION_RE = re.compile(
    r"^(?:[DdДд][yуYУ]|[JjЈ][vвV]|[Tt][yуYУ])\s?\d+$", re.IGNORECASE
)


def _classify_token(token: str) -> str:
    """
    Classify a whitespace-separated token.

    Returns:
        'FULL'  — полный KKS (10LCM01BB01) → НЕ сплитить
        'KKS1'  — первая часть KKS (10LAB30)
        'COMP'  — компонент с известным unit code (AA001)
        'DY'    — обозначение диаметра/типа (Dy25, Jv5, Tv5)
        'OTHER' — русский текст и т.д.
    """
    fixed = _fix_ocr(token.upper())
    if _FULL_KKS_RE.match(fixed):
        return "FULL"
    if _KKS_FIRST_RE.match(fixed):
        return "KKS1"
    if _KKS_COMP_RE.match(fixed):
        return "COMP"
    if _DESIGNATION_RE.match(_fix_ocr(token)):
        return "DY"
    return "OTHER"


def _should_split_line(text_line: str) -> Optional[str]:
    """
    Определить, нужно ли горизонтально разрезать строку.

    SPLIT:
      KKS1 KKS1       → "10LAB30 10LAB30"
      COMP COMP        → "AA405 AA406"
      ...KKS1 KKS1..  → "BP001 10LAB03 10LAB03" (по границе пары)
      COMP DY          → "AA101 Dy10"
      DY COMP          → "Dy20 AA401"
      KKS1 DY          → "10LAB03 Dy25"
      COMP COMP DY     → "AA402 AA401 Dy25"

    KEEP:
      KKS1 OTHER       → "10MAA02 слово"
      KKS1 COMP        → "10LCS59 AA401" (одна KKS-запись)
      FULL ...          → полный KKS
      OTHER OTHER       → "Отбор проб"
      OTHER DY          → "текст Dy25" (нет KKS-контекста)
    """
    parts = text_line.split()
    if len(parts) < 2:
        return None

    types = [_classify_token(p) for p in parts]

    if "FULL" in types:
        return None

    # All same KKS type
    if all(t == "KKS1" for t in types) and len(types) >= 2:
        return "kks1_repeat"
    if all(t == "COMP" for t in types) and len(types) >= 2:
        return "comp_repeat"

    # KKS1+KKS1 pair in mixed context
    for i in range(len(types) - 1):
        if types[i] == "KKS1" and types[i + 1] == "KKS1":
            return "kks1_pair"

    # DY mixed with KKS1/COMP — split at boundary
    has_dy = "DY" in types
    has_kks = any(t in ("KKS1", "COMP") for t in types)
    if has_dy and has_kks:
        return "dy_mixed"

    return None


def _find_split_x(text_line: str, x1: float, x2: float,
                   reason: str) -> Optional[Tuple[float, str, str]]:
    """
    Найти X-координату разреза и тексты левой/правой частей.
    Returns (split_x, left_text, right_text) или None.
    """
    parts = text_line.split()
    types = [_classify_token(p) for p in parts]
    total_len = len(text_line)
    if total_len == 0:
        return None

    target_idx = None

    if reason in ("kks1_repeat", "comp_repeat"):
        best_gap, best_i = 0, -1
        pos = 0
        for i in range(len(parts) - 1):
            pos = text_line.index(parts[i], pos) + len(parts[i])
            npos = text_line.index(parts[i + 1], pos)
            if npos - pos > best_gap:
                best_gap = npos - pos
                best_i = i
            pos = npos
        target_idx = best_i

    elif reason == "kks1_pair":
        for i in range(len(types) - 1):
            if types[i] == "KKS1" and types[i + 1] == "KKS1":
                target_idx = i
                break

    elif reason == "dy_mixed":
        # Find boundary between DY and non-DY tokens
        # DY at the end: "AA101 Dy10" → split before first DY
        # DY at the start: "Dy20 AA401" → split after last DY
        # DY in middle: unlikely but handle as split before DY
        first_dy = next(i for i, t in enumerate(types) if t == "DY")
        last_dy = len(types) - 1 - next(i for i, t in enumerate(reversed(types)) if t == "DY")

        if first_dy > 0:
            # DY not at start → split before first DY
            target_idx = first_dy - 1
        elif last_dy < len(types) - 1:
            # DY at start → split after last DY
            target_idx = last_dy
        else:
            return None

    if target_idx is None or target_idx < 0:
        return None

    # Вычислить позицию пробела
    pos = 0
    for i in range(target_idx):
        pos = text_line.index(parts[i], pos) + len(parts[i])
        pos = text_line.index(parts[i + 1], pos)
    end_left = text_line.index(parts[target_idx], pos) + len(parts[target_idx])
    start_right = text_line.index(parts[target_idx + 1], end_left)
    gap_mid = (end_left + start_right) / 2.0 / total_len

    if gap_mid < 0.15 or gap_mid > 0.85:
        return None

    left_text = " ".join(parts[:target_idx + 1])
    right_text = " ".join(parts[target_idx + 1:])

    return x1 + (x2 - x1) * gap_mid, left_text, right_text


def _kks_split_block(block: TextBlock, block_idx: int) -> List[TextBlock]:
    """
    KKS horizontal split + vertical <br> split для одного блока.
    Возвращает список атомарных TextBlock.
    """
    text = block.text
    x1, y1, x2, y2 = block.bbox
    lines = text.split("<br>")
    n_lines = len(lines)

    # Однострочный блок
    if n_lines <= 1:
        reason = _should_split_line(text)
        if reason:
            split_result = _find_split_x(text, x1, x2, reason)
            if split_result is not None:
                sx, left_text, right_text = split_result
                return [
                    TextBlock(bbox=[x1, y1, int(sx), y2], text=left_text,
                              confidence=block.confidence, source=block.source,
                              origin="split", parent_id=block_idx),
                    TextBlock(bbox=[int(sx), y1, x2, y2], text=right_text,
                              confidence=block.confidence, source=block.source,
                              origin="split", parent_id=block_idx),
                ]
        return [block]

    # Многострочный: vertical split, каждую строку проверяем на horizontal
    line_h = (y2 - y1) / n_lines
    result = []

    for i, line in enumerate(lines):
        ly1 = int(y1 + i * line_h)
        ly2 = int(y1 + (i + 1) * line_h)

        reason = _should_split_line(line)
        if reason:
            split_result = _find_split_x(line, x1, x2, reason)
            if split_result is not None:
                sx, left_text, right_text = split_result
                result.append(TextBlock(
                    bbox=[x1, ly1, int(sx), ly2], text=left_text,
                    confidence=block.confidence, source=block.source,
                    origin="split", parent_id=block_idx))
                result.append(TextBlock(
                    bbox=[int(sx), ly1, x2, ly2], text=right_text,
                    confidence=block.confidence, source=block.source,
                    origin="split", parent_id=block_idx))
                continue

        result.append(TextBlock(
            bbox=[x1, ly1, x2, ly2], text=line,
            confidence=block.confidence, source=block.source,
            origin="split", parent_id=block_idx))

    return result


# ============================================================
# Этап 1b: Post-merge KKS vertical split
# ============================================================


def _line_type(line: str) -> str:
    """Classify a text line as KKS1, COMP, DY, or OTHER."""
    tokens = line.strip().split()
    if not tokens:
        return "OTHER"
    # Pure designation line (Dy25, Jv5, etc.) — all tokens are designations
    if all(_DESIGNATION_RE.match(_fix_ocr(t)) for t in tokens):
        return "DY"
    for t in tokens:
        if _KKS_FIRST_RE.match(_fix_ocr(t.upper())):
            return "KKS1"
    for t in tokens:
        if _KKS_COMP_RE.match(_fix_ocr(t.upper())):
            return "COMP"
    return "OTHER"


def _post_merge_kks_split(blocks: List[TextBlock]) -> List[TextBlock]:
    """
    Post-merge step: разбить блоки по KKS-границам и Dy-обозначениям.

    Обрабатывает два паттерна:

    1) ≥2 KKS-пар столбиком (требует ≥4 строк):
        10GHA30    ← KKS pair 1
        AA106
        10GHA30    ← KKS pair 2
        AX007

    2) KKS + Dy в одном блоке (требует ≥3 строк):
        10LAB03    ← KKS pair
        AA601
        Dy25       ← designation (отдельный блок)
    """
    result: List[TextBlock] = []
    split_count = 0

    for b in blocks:
        if not b.text or "<br>" not in b.text:
            result.append(b)
            continue

        lines = b.text.split("<br>")
        if len(lines) < 3:
            result.append(b)
            continue

        ltypes = [_line_type(l) for l in lines]

        # ── Find KKS pairs: KKS1 at line i, COMP within next 1-2 lines ──
        pairs: List[Tuple[int, int]] = []
        used_comp: set = set()
        for i, lt in enumerate(ltypes):
            if lt != "KKS1":
                continue
            for j in range(i + 1, min(i + 3, len(lines))):
                if ltypes[j] == "COMP" and j not in used_comp:
                    pairs.append((i, j))
                    used_comp.add(j)
                    break

        # ── Find Dy lines ──
        dy_lines = {i for i, lt in enumerate(ltypes) if lt == "DY"}

        # ── Determine split points ──
        split_at: set = set()

        # Between consecutive KKS pairs
        if len(pairs) >= 2:
            for pi in range(len(pairs) - 1):
                end_current = pairs[pi][1]
                start_next = pairs[pi + 1][0]
                if start_next > end_current:
                    split_at.add(start_next)

        # Before/after each Dy line (separate it from KKS groups)
        if dy_lines and pairs:
            for dy_i in dy_lines:
                # Split before Dy if previous line is not Dy
                if dy_i > 0 and dy_i - 1 not in dy_lines:
                    split_at.add(dy_i)
                # Split after Dy if next line is not Dy
                if dy_i < len(lines) - 1 and dy_i + 1 not in dy_lines:
                    split_at.add(dy_i + 1)

        if not split_at:
            result.append(b)
            continue

        # ── Split ──
        x1, y1, x2, y2 = b.bbox
        total_lines = len(lines)
        line_h = (y2 - y1) / total_lines

        boundaries = sorted({0} | split_at | {total_lines})
        split_count += 1

        for k in range(len(boundaries) - 1):
            s, e = boundaries[k], boundaries[k + 1]
            if s >= e:
                continue
            sy1 = int(y1 + s * line_h)
            sy2 = int(y1 + e * line_h)
            group_text = "<br>".join(lines[s:e])
            result.append(TextBlock(
                bbox=[x1, sy1, x2, sy2],
                text=group_text,
                confidence=b.confidence,
                source=b.source,
                origin="split",
            ))

    if split_count:
        logger.info("Post-merge KKS/Dy split: %d blocks split → %d total",
                     split_count, len(result))
    return result


# ============================================================
# Этап 2+3: Union-Find Merge
# ============================================================

def _get_kks1_codes(text: str) -> frozenset:
    """Extract set of distinct KKS1 codes from atom text."""
    if not text:
        return frozenset()
    codes = set()
    for t in text.split():
        fixed = _fix_ocr(t.upper())
        if _KKS_FIRST_RE.match(fixed):
            codes.add(fixed)
    return frozenset(codes)


class _UnionFind:
    """Union-Find with KKS1 guard: block merge if cluster would have >1 distinct KKS1 code."""
    __slots__ = ("p", "rank", "codes")

    def __init__(self, n: int, kks1_sets: List[frozenset]):
        self.p = list(range(n))
        self.rank = [0] * n
        self.codes = list(kks1_sets)

    def find(self, x: int) -> int:
        r = x
        while self.p[r] != r:
            r = self.p[r]
        while self.p[x] != r:
            self.p[x], x = r, self.p[x]
        return r

    def union(self, x: int, y: int) -> bool:
        """Union two elements. Returns False if blocked by KKS guard."""
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return True

        combined = self.codes[rx] | self.codes[ry]
        if len(combined) > 1:
            return False  # would create cluster with >1 distinct KKS1

        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.p[ry] = rx
        self.codes[rx] = combined
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1
        return True


def _split_and_merge(blocks: List[TextBlock],
                     params: Optional[Dict] = None) -> List[TextBlock]:
    """
    Core algorithm: KKS split → vertical split → Union-Find merge.
    """
    if params is None:
        params = DEFAULT_MERGE_PARAMS

    # ── Split ──
    atomic: List[TextBlock] = []
    for idx, block in enumerate(blocks):
        atomic.extend(_kks_split_block(block, idx))

    n = len(atomic)
    if n == 0:
        return []

    # ── Merge ──
    bboxes = [a.bbox for a in atomic]
    cy = np.array([(b[1] + b[3]) / 2.0 for b in bboxes])
    sorted_idx = np.argsort(cy)
    kks1_sets = [_get_kks1_codes(a.text) for a in atomic]
    uf = _UnionFind(n, kks1_sets)

    nn = params.get("neighbor_window", 30)
    y_cut = params.get("y_cutoff_factor", 3.0)
    vt = params["v_gap_thresh"]
    v_min = params["v_gap_min"]
    hot = params["h_overlap_thresh"]
    ct = params["center_thresh"]
    hgt = params["h_gap_thresh"]

    for ii in range(n):
        i = sorted_idx[ii]
        bi = bboxes[i]
        hi = bi[3] - bi[1]
        wi = bi[2] - bi[0]
        max_dy = max(hi, 1.0) * y_cut

        for jj in range(ii + 1, min(ii + nn, n)):
            j = sorted_idx[jj]
            bj = bboxes[j]

            if cy[j] - cy[i] > max_dy:
                break

            wj = bj[2] - bj[0]
            hj = bj[3] - bj[1]
            min_w = min(wi, wj)
            if min_w <= 0:
                continue

            # 1. Horizontal overlap > 90%
            h_overlap = max(0.0, min(bi[2], bj[2]) - max(bi[0], bj[0]))
            if h_overlap / min_w < hot:
                continue

            # 2. Vertical gap normalized
            avg_h = (hi + hj) / 2.0
            if avg_h <= 0:
                continue
            v_gap = (bj[1] - bi[3]) / avg_h if cy[j] >= cy[i] else (bi[1] - bj[3]) / avg_h
            if v_gap > vt or v_gap < v_min:
                continue

            # 3. Center alignment
            max_w = max(wi, wj)
            if max_w <= 0:
                continue
            if abs((bi[0] + bi[2]) / 2.0 - (bj[0] + bj[2]) / 2.0) / max_w > ct:
                continue

            # 4. Horizontal gap
            h_gap = max(0.0, max(bi[0], bj[0]) - min(bi[2], bj[2]))
            if h_gap / avg_h > hgt:
                continue

            uf.union(i, j)

    # ── Collect clusters ──
    clusters: Dict[int, List[int]] = defaultdict(list)
    for i in range(n):
        clusters[uf.find(i)].append(i)

    result: List[TextBlock] = []
    for idxs in clusters.values():
        rx1 = min(bboxes[i][0] for i in idxs)
        ry1 = min(bboxes[i][1] for i in idxs)
        rx2 = max(bboxes[i][2] for i in idxs)
        ry2 = max(bboxes[i][3] for i in idxs)

        origin = "merged" if len(idxs) > 1 else atomic[idxs[0]].origin

        # Собрать текст (для downstream: merge_results match)
        texts = [atomic[i].text for i in sorted(idxs, key=lambda i: bboxes[i][1])
                 if atomic[i].text]
        combined_text = "<br>".join(texts) if texts else ""

        conf = min(atomic[i].confidence for i in idxs)

        result.append(TextBlock(
            bbox=[int(rx1), int(ry1), int(rx2), int(ry2)],
            text=combined_text,
            confidence=conf,
            source=atomic[idxs[0]].source,
            origin=origin,
        ))

    return result


# ============================================================
# Этап 4: Дедупликация
# ============================================================
def deduplicate_blocks(blocks: List[TextBlock]) -> List[TextBlock]:
    n = len(blocks)
    remove = [False] * n

    for i in range(n):
        if remove[i]:
            continue
        a = blocks[i]
        for j in range(i + 1, n):
            if remove[j]:
                continue
            b = blocks[j]

            ox = max(0, min(a.x2, b.x2) - max(a.x1, b.x1))
            oy = max(0, min(a.y2, b.y2) - max(a.y1, b.y1))
            overlap = ox * oy
            if overlap == 0:
                continue

            min_area = min(a.area, b.area)
            if min_area <= 0:
                continue

            if overlap / min_area > 0.5:
                if a.area < b.area:
                    remove[i] = True
                elif b.area < a.area:
                    remove[j] = True
                elif a.origin == "merged":
                    remove[j] = True
                else:
                    remove[i] = True

    result = [b for i, b in enumerate(blocks) if not remove[i]]
    removed = sum(remove)
    if removed:
        logger.info("Dedup: removed %d → %d total", removed, len(result))
    return result


# ============================================================
# Этап 5: Фильтрация мусора
# ============================================================
def filter_noise(blocks: List[TextBlock],
                 min_area: float = 50,
                 min_dim: float = 5) -> List[TextBlock]:
    result = [b for b in blocks
              if b.area >= min_area and b.w >= min_dim and b.h >= min_dim]
    removed = len(blocks) - len(result)
    if removed:
        logger.info("Filter: removed %d → %d total", removed, len(result))
    return result


# ============================================================
# Визуализация
# ============================================================
def visualize(original: "np.ndarray", blocks: List[TextBlock], output_path: str):
    import cv2
    vis = original.copy()
    for b in blocks:
        cv2.rectangle(vis, (b.x1, b.y1), (b.x2, b.y2), (0, 200, 0), 2)
    cv2.imwrite(output_path, vis)
    logger.info("Saved: %s", output_path)


def visualize_debug(original: "np.ndarray", before: List[TextBlock],
                    after: List[TextBlock], output_path: str):
    import cv2
    H, W = original.shape[:2]
    vis_b = original.copy()
    vis_a = original.copy()

    for b in before:
        cv2.rectangle(vis_b, (b.x1, b.y1), (b.x2, b.y2), (255, 0, 0), 2)

    colors = {"original": (0, 200, 0), "split": (0, 0, 255), "merged": (0, 200, 255)}
    for b in after:
        c = colors.get(b.origin, (0, 200, 0))
        cv2.rectangle(vis_a, (b.x1, b.y1), (b.x2, b.y2), c, 2)

    scale = min(1.0, 2500 / W) if W > 2500 else 1.0
    if scale < 1.0:
        vis_b = cv2.resize(vis_b, None, fx=scale, fy=scale)
        vis_a = cv2.resize(vis_a, None, fx=scale, fy=scale)

    combined = np.hstack([vis_b, vis_a])
    cv2.putText(combined, "BEFORE", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 0), 2)
    cv2.putText(combined, "AFTER", (vis_b.shape[1] + 10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 200, 0), 2)
    cv2.imwrite(output_path, combined)
    logger.info("Saved debug: %s", output_path)


# ============================================================
# I/O
# ============================================================
def load_surya_blocks(path: str) -> List[TextBlock]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [TextBlock(
        bbox=[int(v) for v in item["bbox"]],
        text=item.get("text", ""),
        confidence=item.get("confidence", 1.0),
        source=item.get("source", "surya"),
    ) for item in data]


def save_blocks(blocks: List[TextBlock], path: str):
    data = [{
        "bbox": [int(v) for v in b.bbox],
        "text": b.text,
        "confidence": float(b.confidence),
        "source": b.source,
        "origin": b.origin,
    } for b in blocks]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("Saved %d blocks → %s", len(data), path)


# ============================================================
# Пайплайн
# ============================================================
def run_pipeline(cleaned_path: str, surya_json_path: str,
                 original_path: Optional[str], output_dir: str):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Load
    logger.info("Loading...")
    blocks_before = load_surya_blocks(surya_json_path)
    logger.info("Loaded %d blocks from %s", len(blocks_before), surya_json_path)

    # Scale check: Surya coords vs image
    original = None
    img_h, img_w = 0, 0

    if original_path:
        import cv2
        original = cv2.imread(original_path)
        if original is not None:
            img_h, img_w = original.shape[:2]

    if img_w == 0 and cleaned_path:
        import cv2
        tmp = cv2.imread(cleaned_path, cv2.IMREAD_GRAYSCALE)
        if tmp is not None:
            img_h, img_w = tmp.shape[:2]
            del tmp

    if img_w > 0 and blocks_before:
        max_x = max(b.x2 for b in blocks_before)
        max_y = max(b.y2 for b in blocks_before)
        if max_x > img_w * 1.1 or max_y > img_h * 1.1:
            logger.warning("Surya coords (%dx%d) >> image (%dx%d), scaling...",
                           max_x, max_y, img_w, img_h)
            sx, sy = img_w / max_x, img_h / max_y
            for b in blocks_before:
                b.bbox = [int(b.bbox[0] * sx), int(b.bbox[1] * sy),
                          int(b.bbox[2] * sx), int(b.bbox[3] * sy)]

    logger.info("Image: %dx%d, blocks: %d", img_w, img_h, len(blocks_before))

    # ═══ Core: Split + Merge ═══
    logger.info("=== Split + Merge ===")
    blocks = _split_and_merge(blocks_before)
    logger.info("Split+Merge: %d → %d", len(blocks_before), len(blocks))

    # ═══ Dedup ═══
    blocks = deduplicate_blocks(blocks)

    # ═══ Filter ═══
    blocks = filter_noise(blocks)

    # ═══ Save ═══
    save_blocks(blocks, str(out / "reclustered_blocks.json"))

    # ═══ Visualize ═══
    if original is not None:
        visualize(original, blocks, str(out / "result.png"))
        visualize_debug(original, blocks_before, blocks, str(out / "debug.png"))

    # Stats
    stats = defaultdict(int)
    for b in blocks:
        stats[b.origin] += 1
    logger.info("Result: %d → %d  %s", len(blocks_before), len(blocks), dict(stats))

    return blocks


def run_recluster(surya_json_path: str, cleaned_path: str,
                  original_path: str, output_dir: str):
    """
    Обёртка для вызова из OCR pipeline task.
    Сигнатура совместима с worker/tasks/ocr.py.

    Args:
        surya_json_path: путь к surya_raw.json
        cleaned_path: путь к cleaned_for_ocr_gray.png
        original_path: путь к original/image.png
        output_dir: папка вывода (ocr_dir)
    """
    return run_pipeline(cleaned_path, surya_json_path, original_path, output_dir)


# ============================================================
# CLI
# ============================================================
def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="Рекластеризация OCR P&ID v4")
    p.add_argument("--cleaned", default=None,
                   help="cleaned_for_ocr_gray.png (для scale check)")
    p.add_argument("--surya", required=True,
                   help="surya_raw.json")
    p.add_argument("--original", default=None,
                   help="original/image.png (для визуализации)")
    p.add_argument("--output", default="./ocr_reclustered")
    a = p.parse_args()
    run_pipeline(a.cleaned, a.surya, a.original, a.output)


if __name__ == "__main__":
    main()

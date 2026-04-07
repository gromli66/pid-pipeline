"""
modules/ocr/hooks.py — Библиотека переиспользуемых хуков пост-обработки.

Хуки подключаются из domain_profile.yaml через postprocess секцию.
Один хук используется разными проектами.

Каждый хук — чистая функция: (blocks, profile, **kwargs) → blocks.
"""

import re
import logging
import os
from collections import defaultdict
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# split_multi: разбивка блоков с несколькими кодами
# ═══════════════════════════════════════════════════════════════════════════════

def split_multi_head_tail_join(target_blocks: list, profile) -> list:
    """
    Стратегия "head_tail_join" — KKS-совместимая.

    Разбивает target-блоки с несколькими KKS-кодами:
    Случай 1: 1 HEAD + N TAIL → N блоков (HEAD+TAIL если даёт валидный FULL)
    Случай 2: M HEAD → M блоков
    """
    head_pattern = profile.patterns.get('head_code')
    tail_pattern = profile.patterns.get('tail_code')
    full_pattern = profile.patterns.get('full_code')
    head_loose = profile.patterns.get('head_loose')

    if not head_pattern or not tail_pattern:
        return target_blocks

    def _find_head(text):
        if head_pattern:
            m = head_pattern.regex.search(text)
            if m:
                return m
        if head_loose:
            m = head_loose.regex.search(text)
            if m and len(m.group()) >= 6:
                return m
        return None

    result = []
    for block in target_blocks:
        text = block.get('text', '')
        parts = [p.strip() for p in text.split(' | ') if p.strip()]

        head_positions = []
        tail_positions = []
        for i, p in enumerate(parts):
            head_m = _find_head(p)
            is_tail = tail_pattern.regex.match(p)
            if head_m and not is_tail:
                head_positions.append(i)
            elif is_tail:
                tail_positions.append(i)

        # 1 HEAD + N TAIL
        if len(head_positions) == 1 and len(tail_positions) >= 1:
            hi = head_positions[0]
            head_m = _find_head(parts[hi])
            head_text = head_m.group() if head_m else parts[hi]
            bb = block['bbox']

            valid_joins = []
            orphan_tails = []
            for ti in tail_positions:
                candidate = head_text + parts[ti]
                if full_pattern and full_pattern.regex.search(candidate):
                    valid_joins.append((ti, candidate))
                else:
                    orphan_tails.append(ti)

            if valid_joins:
                n = len(valid_joins)
                h_each = (bb[3] - bb[1]) / max(n, 1)
                for gi, (ti, joined) in enumerate(valid_joins):
                    grp_bb = [bb[0], bb[1] + gi * h_each,
                              bb[2], bb[1] + (gi + 1) * h_each]
                    result.append({**block, 'text': joined, 'bbox': grp_bb})
                for ti in orphan_tails:
                    result.append({**block, 'text': parts[ti]})
                continue

        # 1 HEAD + 0 TAIL
        if len(head_positions) == 1 and len(tail_positions) == 0:
            meaningful = [parts[i] for i in range(len(parts))
                          if i in head_positions or
                          (len(parts[i]) >= 3 and not profile.is_noise(parts[i]))]
            if meaningful:
                result.append({**block, 'text': ' '.join(meaningful)})
                continue

        # M HEAD
        if len(head_positions) >= 2:
            groups = []
            for gi, hi in enumerate(head_positions):
                end = (head_positions[gi + 1]
                       if gi + 1 < len(head_positions)
                       else len(parts))
                group_parts = parts[hi:end]
                if group_parts:
                    head_m = _find_head(group_parts[0])
                    if head_m:
                        group_parts = [head_m.group()] + group_parts[1:]
                    groups.append(group_parts)

            valid_groups = [
                grp for grp in groups
                if (m := _find_head(grp[0]))
                and len(m.group()) >= 6
            ]

            if len(valid_groups) == 0:
                result.append(block)
            elif len(valid_groups) == 1:
                result.append({**block, 'text': ' | '.join(valid_groups[0])})
            else:
                bb = block['bbox']
                h_each = (bb[3] - bb[1]) / len(valid_groups)
                for gi, grp in enumerate(valid_groups):
                    grp_bb = [bb[0], bb[1] + gi * h_each,
                              bb[2], bb[1] + (gi + 1) * h_each]
                    result.append({**block,
                                   'text': ' | '.join(grp),
                                   'bbox': grp_bb})
            continue

        result.append(block)

    return result


def split_multi_inline(target_blocks: list, profile) -> list:
    """
    Стратегия "inline_split" — для кириллических профилей.

    Просто вызывает split_inline и разбивает по bbox.
    """
    result = []
    for block in target_blocks:
        text = block.get('text', '')
        parts = profile.split_inline(text)
        if len(parts) <= 1:
            result.append(block)
            continue
        bbox = block.get('bbox', [0, 0, 0, 0])
        w = bbox[2] - bbox[0]
        n = len(parts)
        for i, part in enumerate(parts):
            sub_bbox = [
                bbox[0] + int(w * i / n), bbox[1],
                bbox[0] + int(w * (i + 1) / n), bbox[3],
            ]
            result.append({
                'bbox': sub_bbox,
                'text': part.strip(),
                'is_target': profile.is_target(part),
            })
    return result


# Реестр стратегий split_multi
SPLIT_MULTI_STRATEGIES = {
    "head_tail_join": split_multi_head_tail_join,
    "inline_split": split_multi_inline,
}


# ═══════════════════════════════════════════════════════════════════════════════
# promote_from_secondary: извлечение кодов из блоков вторичного скрипта
# ═══════════════════════════════════════════════════════════════════════════════

def promote_from_secondary_generic(
    secondary_blocks: list,
    existing_targets: list,
    profile,
    min_head_length: int = 6,
) -> tuple[list, int]:
    """
    Универсальная промоция: ищет HEAD в secondary-блоках после нормализации.
    """
    head_pattern = profile.patterns.get('head_code')
    if not head_pattern:
        return [], 0

    existing_heads = set()
    for g in existing_targets:
        clean = re.sub(r'[^A-Za-z0-9]', '', g.get('text', ''))
        for m in head_pattern.regex.finditer(clean):
            existing_heads.add(m.group().upper())

    new_targets = []
    for block in secondary_blocks:
        text = block.get('text', '')
        clean = re.sub(r'</?[a-z]+>', '', text)

        # Оригинальный текст
        heads_orig = head_pattern.regex.findall(clean)

        # Нормализованный текст
        clean_norm = profile.normalize_text(clean)
        heads_norm = head_pattern.regex.findall(clean_norm)

        # Объединяем (нормализованные приоритетнее)
        seen = set()
        heads = []
        for h in heads_norm:
            norm = re.sub(r'[^A-Za-z0-9]', '', h).upper()
            if norm not in seen:
                seen.add(norm)
                heads.append(h)
        for h in heads_orig:
            norm = re.sub(r'[^A-Za-z0-9]', '', h).upper()
            if norm not in seen:
                seen.add(norm)
                heads.append(h)

        for head in heads:
            norm = re.sub(r'[^A-Za-z0-9]', '', head).upper()
            if len(norm) < min_head_length or norm in existing_heads:
                continue
            existing_heads.add(norm)
            new_targets.append({
                'bbox': list(block['bbox']),
                'text': head,
                'is_target': True,
                'source': 'promoted_from_secondary',
            })

    return new_targets, len(new_targets)


# ═══════════════════════════════════════════════════════════════════════════════
# merge_secondary: Union-Find слияние близких secondary-блоков
# ═══════════════════════════════════════════════════════════════════════════════

def merge_secondary_union_find(
    blocks: list,
    profile,
    max_vgap_factor: float = 1.0,
    max_hgap_factor: float = 1.0,
    fallback_med_h: float = None,
) -> list:
    """Универсальное Union-Find слияние secondary-блоков."""
    if not blocks:
        return blocks

    sec_blocks = []
    other_blocks = []
    for b in blocks:
        text = re.sub(r'</?[a-z]+>', '', b.get('text', ''))
        if profile.has_secondary_script(text):
            sec_blocks.append(b)
        else:
            other_blocks.append(b)

    if len(sec_blocks) <= 1:
        return blocks

    heights = [b['bbox'][3] - b['bbox'][1] for b in sec_blocks
               if b['bbox'][3] > b['bbox'][1]]
    med_h = float(np.median(heights)) if heights else None
    fallback = fallback_med_h if fallback_med_h is not None else med_h
    if fallback is None:
        return blocks

    eff_max_vgap = fallback * max_vgap_factor
    eff_max_hgap = fallback * max_hgap_factor

    n = len(sec_blocks)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        bi = sec_blocks[i]['bbox']
        for j in range(i + 1, n):
            bj = sec_blocks[j]['bbox']
            v_gap = max(0, max(bi[1], bj[1]) - min(bi[3], bj[3]))
            if v_gap > eff_max_vgap:
                continue
            h_gap = max(0, max(bi[0], bj[0]) - min(bi[2], bj[2]))
            if h_gap < eff_max_hgap:
                union(i, j)

    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    result = list(other_blocks)
    for idxs in groups.values():
        members = [sec_blocks[i] for i in idxs]
        members.sort(key=lambda b: (b['bbox'][1], b['bbox'][0]))
        merged_bbox = [
            min(b['bbox'][0] for b in members),
            min(b['bbox'][1] for b in members),
            max(b['bbox'][2] for b in members),
            max(b['bbox'][3] for b in members),
        ]
        texts = [re.sub(r'</?[a-z]+>', '', b.get('text', '')).strip()
                 for b in members]
        merged_text = ' '.join(t for t in texts if t)
        is_tgt = any(b.get('is_target', False) for b in members)
        result.append({
            'bbox': merged_bbox,
            'text': merged_text,
            'is_target': is_tgt,
            'cyrillic_merged': len(members),
            '_members': members,
        })
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# extract_target_from_merged: AgglomerativeClustering для merged-блоков
# ═══════════════════════════════════════════════════════════════════════════════

def extract_target_from_merged_cluster(
    blocks: list,
    profile,
    image_path: Optional[str] = None,
) -> list:
    """
    Извлечь целевые коды из merged secondary-блоков.

    Использует AgglomerativeClustering для разделения bbox.
    """
    from modules.ocr.evaluate import cluster_split_bbox

    head_pattern = profile.patterns.get('head_code')
    full_pattern = profile.patterns.get('full_code')
    if not full_pattern and not head_pattern:
        return blocks

    binary = None
    if image_path and os.path.exists(str(image_path)):
        img_gray = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if img_gray is not None:
            _, binary = cv2.threshold(
                img_gray, 0, 255,
                cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    unit_re_raw = '|'.join(profile.units) if profile.units else ''
    all_bboxes = [b['bbox'] for b in blocks]
    result = []

    def _has_full_kks(text):
        clean = re.sub(r'</?[a-z]+>', '', text)
        if full_pattern and full_pattern.regex.search(clean):
            return True
        head = head_pattern.regex.search(clean) if head_pattern else None
        tail = (re.search(r'\b(' + unit_re_raw + r')\d{2,4}',
                          clean, re.I) if unit_re_raw else None)
        return bool(head and tail)

    for b in blocks:
        members = b.get('_members', None)

        if members is None or len(members) <= 1:
            out = {k: v for k, v in b.items() if k != '_members'}
            result.append(out)
            continue

        merged_text = b.get('text', '')
        if not _has_full_kks(merged_text):
            out = {k: v for k, v in b.items() if k != '_members'}
            result.append(out)
            continue

        kks_members = []
        sec_members = []
        for m in members:
            mt = re.sub(r'</?[a-z]+>', '', m.get('text', ''))
            if ((head_pattern and head_pattern.regex.search(mt))
                    or m.get('is_target', False)):
                kks_members.append(m)
            else:
                sec_members.append(m)

        if not kks_members or not sec_members:
            out = {k: v for k, v in b.items() if k != '_members'}
            result.append(out)
            continue

        n_kks = len(kks_members)
        n_clusters = n_kks + 1
        merged_bb = b['bbox']
        other_bb = [ob for ob in all_bboxes if ob != merged_bb]
        cluster_bbs = cluster_split_bbox(binary, merged_bb, n_clusters, other_bb)

        def _overlap_area(a, c):
            return (max(0, min(a[2], c[2]) - max(a[0], c[0]))
                    * max(0, min(a[3], c[3]) - max(a[1], c[1])))

        used_clusters = set()
        kks_cluster_indices = []
        for km in kks_members:
            kb = km['bbox']
            best_cl, best_ov = -1, -1
            for ci, cbb in enumerate(cluster_bbs):
                if ci in used_clusters:
                    continue
                ov = _overlap_area(kb, list(cbb))
                if ov > best_ov:
                    best_ov = ov
                    best_cl = ci
            if best_cl >= 0:
                kks_cluster_indices.append(best_cl)
                used_clusters.add(best_cl)
            else:
                kks_cluster_indices.append(0)

        cyr_cluster_indices = [ci for ci in range(n_clusters) if ci not in used_clusters]

        for ki, cl_idx in enumerate(kks_cluster_indices):
            if cl_idx < len(cluster_bbs):
                cbb = cluster_bbs[cl_idx]
                result.append({
                    'bbox': list(cbb),
                    'text': re.sub(r'</?[a-z]+>', '', kks_members[ki].get('text', '')).strip(),
                    'is_target': True,
                })

        if cyr_cluster_indices:
            cyr_bboxes = [cluster_bbs[ci] for ci in cyr_cluster_indices if ci < len(cluster_bbs)]
            if cyr_bboxes:
                cyr_bbox = [
                    min(c[0] for c in cyr_bboxes),
                    min(c[1] for c in cyr_bboxes),
                    max(c[2] for c in cyr_bboxes),
                    max(c[3] for c in cyr_bboxes),
                ]
            else:
                cyr_bbox = list(merged_bb)
            cyr_text = ' '.join(
                re.sub(r'</?[a-z]+>', '', m.get('text', '')).strip()
                for m in sec_members)
            result.append({
                'bbox': cyr_bbox,
                'text': cyr_text,
                'is_target': False,
                'cyrillic_merged': len(sec_members),
            })

    return result

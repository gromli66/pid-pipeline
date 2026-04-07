"""
profiles/kks_rosatom.py — KKS-профиль для Росатом P&ID.

Реализует всю доменную логику:
  - classify: FULL, HEAD, TAIL, DN, MULTI, FRAG, HEAD_DN, OTHER
  - is_noise: фильтрация шума (даты, штампы, короткие строки)
  - is_target: полный KKS или HEAD+TAIL или диаметр
  - split_inline: разбивка "AA001 10LCS68" → ["AA001", "10LCS68"]
  - пост-обработка: split_multi, promote_from_cyrillic, merge_cyrillic

Загружает паттерны и units из kks_rosatom.yaml.
Сложная логика — в Python-методах.

Использование:
  from profiles.kks_rosatom import KKSProfile
  profile = KKSProfile(yaml_path='profiles/kks_rosatom.yaml')
  profile.classify('10LCS59')  # → 'HEAD'
"""

import re
import os
import cv2
import numpy as np
from pathlib import Path
from typing import Optional
from collections import defaultdict

from modules.ocr.domain_profile import BaseDomainProfile, GroupingPass


# ── Кириллица ────────────────────────────────────

_CYRILLIC_RE = re.compile(r'[\u0400-\u04FF]')


class KKSProfile(BaseDomainProfile):
    """
    Конкретный профиль для KKS (Kraftwerk-Kennzeichen-System).

    Наследует BaseDomainProfile, загружает YAML для паттернов/units,
    реализует всю доменную логику в Python.
    """

    def __init__(self, yaml_path: Optional[str] = None,
                 config: Optional[dict] = None):
        # Автоматически находим YAML рядом с .py если не указан
        if yaml_path is None and config is None:
            default_yaml = Path(__file__).with_suffix('.yaml')
            if default_yaml.exists():
                yaml_path = str(default_yaml)

        super().__init__(yaml_path=yaml_path, config=config)

        # Кэшируем часто используемые паттерны
        self._full = self.patterns.get('full_code')
        self._head = self.patterns.get('head_code')
        self._tail = self.patterns.get('tail_code')
        self._dn = self.patterns.get('diameter')
        self._dn_inline = self.patterns.get('diameter_inline')
        self._frag = self.patterns.get('fragment_head')
        self._head_loose = self.patterns.get('head_loose')

        # unit_re без экранирования (для inline search)
        self._unit_re_raw = '|'.join(self.units)

    # ── classify ─────────────────────────────────

    def classify(self, text: str) -> str:
        text = text.strip()
        if not text:
            return 'OTHER'

        if self._full and len(self._full.regex.findall(text)) >= 2:
            return 'MULTI'
        if self._full and self._full.regex.search(text):
            return 'FULL'

        heads = self._head.regex.findall(text) if self._head else []
        is_tail_only = (self._tail and self._tail.regex.match(text))

        if heads and not is_tail_only:
            if len(heads) >= 2:
                return 'MULTI'
            if self._dn and self._dn.regex.search(text):
                return 'HEAD_DN'
            return 'HEAD'

        if is_tail_only:
            return 'TAIL'
        if self._dn and self._dn.regex.match(text):
            return 'DN'
        if self._frag and self._frag.regex.match(text):
            return 'FRAG'

        return 'OTHER'

    # ── is_noise ─────────────────────────────────

    def is_noise(self, text_raw: str) -> bool:
        text = re.sub(r'</?b>', '', text_raw).replace('<br>', ' ').strip()
        if not text:
            return True
        if '<math>' in text_raw:
            return True
        if len(text) == 1 and not text[0].isalnum():
            return True

        al = sum(1 for c in text if c.isalpha())
        di = sum(1 for c in text if c.isdigit())
        if al == 0 and di == 0:
            return True

        _k = self.has_any_target_pattern(text)
        if len(text) <= 2 and not _k:
            return True

        # Noise regex из YAML
        if self._check_noise_regexes(text):
            return True

        if len(text) > 25 and not _k and al > di:
            return True
        if len(text.split()) >= 4 and not _k:
            return True

        # Короткие строки (3-5 символов) с высокой долей спецсимволов
        spec = sum(1 for c in text if not c.isalnum() and c != ' ')
        if len(text) <= 5 and spec > len(text) * 0.5 and not _k:
            return True

        return False

    # ── is_target ────────────────────────────────

    def is_target(self, text: str) -> bool:
        text = text.strip()
        has_full = bool(self._full and self._full.regex.search(text))
        has_head = bool(self._head and self._head.regex.search(text))
        has_tail = bool(
            re.search(r'\b(' + self._unit_re_raw + r')\d{2,4}',
                       text, re.I)) if self._unit_re_raw else False
        is_dn = (bool(self._dn and self._dn.regex.match(text.strip()))
                 or bool(re.match(
                     r'^D[NnYy\u0443vVjJ]\s*\d', text.strip(), re.I)))
        return has_full or (has_head and has_tail) or is_dn

    # ── has_any_target_pattern ───────────────────

    def has_any_target_pattern(self, text: str) -> bool:
        if self._head and self._head.regex.search(text):
            return True
        if self._tail and self._tail.regex.match(text.strip()):
            return True
        return False

    # ── category_role ────────────────────────────

    def category_role(self, category: str) -> str:
        _ROLE_MAP = {
            'FULL': 'full', 'FULL_BR': 'full', 'MULTI': 'full',
            'HEAD': 'head', 'HEAD_DN': 'head',
            'TAIL': 'tail',
            'DN': 'standalone',
            'FRAG': 'fragment',
        }
        return _ROLE_MAP.get(category, 'other')

    # ── split_inline ─────────────────────────────

    def split_inline(self, text: str) -> list[str]:
        text = re.sub(r'</?[a-z]+>', '', text).strip()
        tokens = []

        # HEAD patterns
        if self._head:
            for m in self._head.regex.finditer(text):
                tokens.append((m.start(), m.end(), m.group()))

        # TAIL patterns (unit + digits)
        if self._unit_re_raw:
            for m in re.finditer(
                    r'\b(' + self._unit_re_raw + r')(\d{2,4}[A-Z]?)\b',
                    text, re.I):
                if not any(t[0] <= m.start() < t[1] for t in tokens):
                    tokens.append((m.start(), m.end(), m.group()))

        # DN inline
        if self._dn_inline:
            for m in self._dn_inline.regex.finditer(text):
                if not any(t[0] <= m.start() < t[1] for t in tokens):
                    tokens.append((m.start(), m.end(), m.group()))

        tokens.sort(key=lambda t: t[0])
        return ([t[2].strip() for t in tokens]
                if len(tokens) > 1 else [text])

    # ── has_secondary_script ─────────────────────

    def has_secondary_script(self, text: str) -> bool:
        """Кириллица — вторичный скрипт для KKS."""
        return bool(_CYRILLIC_RE.search(text))

    # ── postprocess_split_multi ──────────────────

    def postprocess_split_multi(self, target_blocks: list) -> list:
        """
        Разбивает target-блоки с несколькими KKS-кодами.

        Случай 1: 1 HEAD + N TAIL → N блоков (каждый HEAD+TAIL)
        Случай 2: M HEAD → M блоков (каждый HEAD + свои TAIL)
        """
        if not self._head or not self._tail:
            return target_blocks

        result = []
        for block in target_blocks:
            text = block.get('text', '')
            parts = [p.strip() for p in text.split(' | ') if p.strip()]

            head_positions = []
            tail_positions = []
            for i, p in enumerate(parts):
                head_m = self._find_head_in_text(p)
                is_tail = self._tail.regex.match(p)
                if head_m and not is_tail:
                    head_positions.append(i)
                elif is_tail:
                    tail_positions.append(i)

            # 1 HEAD + N TAIL: join each HEAD+TAIL only if result is valid FULL
            if len(head_positions) == 1 and len(tail_positions) >= 1:
                hi = head_positions[0]
                head_m = self._find_head_in_text(parts[hi])
                head_text = head_m.group() if head_m else parts[hi]
                bb = block['bbox']

                # Проверяем каждый TAIL: даёт ли HEAD+TAIL валидный FULL KKS?
                valid_joins = []
                orphan_tails = []
                for ti in tail_positions:
                    candidate = head_text + parts[ti]
                    if self._full and self._full.regex.search(candidate):
                        valid_joins.append((ti, candidate))
                    else:
                        orphan_tails.append(ti)

                if valid_joins:
                    # Склеенные HEAD+TAIL → отдельные блоки
                    n = len(valid_joins)
                    h_each = (bb[3] - bb[1]) / max(n, 1)
                    for gi, (ti, joined) in enumerate(valid_joins):
                        grp_bb = [bb[0], bb[1] + gi * h_each,
                                  bb[2], bb[1] + (gi + 1) * h_each]
                        result.append({**block, 'text': joined, 'bbox': grp_bb})
                    # Orphan tails (не подходят к HEAD) — оставить как есть
                    for ti in orphan_tails:
                        result.append({**block, 'text': parts[ti]})
                    continue
                # Ни один TAIL не дал валидный FULL → не склеивать, оставить блок как есть
                # (fallthrough to M HEAD or default)

            # 1 HEAD + 0 TAIL → убрать мусор, оставить все значимые части
            if len(head_positions) == 1 and len(tail_positions) == 0:
                # Собрать HEAD + все не-мусорные части
                meaningful = [parts[i] for i in range(len(parts))
                              if i in head_positions or
                              (len(parts[i]) >= 3 and not self.is_noise(parts[i]))]
                if meaningful:
                    result.append({**block, 'text': ' '.join(meaningful)})
                    continue

            # M HEAD
            if len(head_positions) < 2:
                result.append(block)
                continue

            groups = []
            for gi, hi in enumerate(head_positions):
                end = (head_positions[gi + 1]
                       if gi + 1 < len(head_positions)
                       else len(parts))
                group_parts = parts[hi:end]
                if group_parts:
                    head_m = self._find_head_in_text(group_parts[0])
                    if head_m:
                        group_parts = [head_m.group()] + group_parts[1:]
                    groups.append(group_parts)

            valid_groups = [
                grp for grp in groups
                if (m := self._find_head_in_text(grp[0]))
                and len(m.group()) >= 6
            ]

            if len(valid_groups) == 0:
                result.append(block)
            elif len(valid_groups) == 1:
                result.append({**block,
                               'text': ' | '.join(valid_groups[0])})
            else:
                bb = block['bbox']
                h_each = (bb[3] - bb[1]) / len(valid_groups)
                for gi, grp in enumerate(valid_groups):
                    grp_bb = [bb[0], bb[1] + gi * h_each,
                              bb[2], bb[1] + (gi + 1) * h_each]
                    result.append({**block,
                                   'text': ' | '.join(grp),
                                   'bbox': grp_bb})
        return result

    def _find_head_in_text(self, text: str):
        """Ищет HEAD: сначала строгий, потом мягкий паттерн."""
        if self._head:
            m = self._head.regex.search(text)
            if m:
                return m
        if self._head_loose:
            m = self._head_loose.regex.search(text)
            if m and len(m.group()) >= 6:
                return m
        return None

    # ── postprocess_promote_from_secondary ───────

    def postprocess_promote_from_secondary(
            self, secondary_blocks: list,
            existing_targets: list) -> tuple[list, int]:
        """
        Извлекает KKS HEAD из кириллических блоков.

        OCR часто путает кириллические и латинские буквы
        (В→B, С→C и т.д.). Нормализуем перед поиском.
        """
        if not self._head:
            return [], 0

        # Все HEAD из existing targets
        existing_heads = set()
        for g in existing_targets:
            clean = re.sub(r'[^A-Za-z0-9]', '', g.get('text', ''))
            for m in self._head.regex.finditer(clean):
                existing_heads.add(m.group().upper())

        new_targets = []
        for block in secondary_blocks:
            text = block.get('text', '')
            clean = re.sub(r'</?[a-z]+>', '', text)

            # Ищем HEAD в оригинальном тексте
            heads_orig = self._head.regex.findall(clean)

            # Ищем HEAD после нормализации кириллицы
            clean_norm = self.normalize_text(clean)
            heads_norm = self._head.regex.findall(clean_norm)

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
                if len(norm) < 6 or norm in existing_heads:
                    continue
                existing_heads.add(norm)
                new_targets.append({
                    'bbox': list(block['bbox']),
                    'text': head,
                    'is_target': True,
                    'source': 'promoted_from_secondary',
                })

        return new_targets, len(new_targets)

    # ── postprocess_merge_secondary ──────────────

    def postprocess_merge_secondary(
            self, blocks: list,
            max_vgap=None, max_hgap=None,
            fallback_med_h=None) -> list:
        """Склеивает близкие кириллические блоки через Union-Find."""
        if not blocks:
            return blocks

        cyr_blocks = []
        other_blocks = []
        for b in blocks:
            text = re.sub(r'</?[a-z]+>', '', b.get('text', ''))
            if self.has_secondary_script(text):
                cyr_blocks.append(b)
            else:
                other_blocks.append(b)

        if len(cyr_blocks) <= 1:
            return blocks

        heights = [b['bbox'][3] - b['bbox'][1] for b in cyr_blocks
                   if b['bbox'][3] > b['bbox'][1]]
        cyr_med_h = (float(np.median(heights))
                     if heights else None)

        fallback = (fallback_med_h if fallback_med_h is not None
                    else cyr_med_h)
        if fallback is None:
            return blocks

        eff_max_vgap = max_vgap if max_vgap is not None else fallback
        eff_max_hgap = max_hgap if max_hgap is not None else fallback

        # Union-Find
        n = len(cyr_blocks)
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
            bi = cyr_blocks[i]['bbox']
            for j in range(i + 1, n):
                bj = cyr_blocks[j]['bbox']
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
            members = [cyr_blocks[i] for i in idxs]
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

    # ── postprocess_extract_target_from_merged ───

    def postprocess_extract_target_from_merged(
            self, blocks: list,
            image_path: Optional[str] = None) -> list:
        """
        Из merged-кириллических блоков извлекает KKS в отдельные блоки.
        Использует AgglomerativeClustering для точного разделения bbox.
        """
        if not self._full and not self._head:
            return blocks

        binary = None
        if image_path and os.path.exists(str(image_path)):
            img_gray = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            if img_gray is not None:
                _, binary = cv2.threshold(
                    img_gray, 0, 255,
                    cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        all_bboxes = [b['bbox'] for b in blocks]
        result = []

        for b in blocks:
            members = b.get('_members', None)

            if members is None or len(members) <= 1:
                out = {k: v for k, v in b.items() if k != '_members'}
                result.append(out)
                continue

            merged_text = b.get('text', '')
            if not self._has_full_kks_text(merged_text):
                out = {k: v for k, v in b.items() if k != '_members'}
                result.append(out)
                continue

            kks_members = []
            cyr_members = []
            for m in members:
                mt = re.sub(r'</?[a-z]+>', '', m.get('text', ''))
                if ((self._head and self._head.regex.search(mt))
                        or m.get('is_target', False)):
                    kks_members.append(m)
                else:
                    cyr_members.append(m)

            if not kks_members or not cyr_members:
                out = {k: v for k, v in b.items() if k != '_members'}
                result.append(out)
                continue

            # Импорт cluster_split_bbox из evaluate_all
            from modules.ocr.evaluate import cluster_split_bbox

            n_kks = len(kks_members)
            n_clusters = n_kks + 1
            merged_bb = b['bbox']
            other_bb = [ob for ob in all_bboxes if ob != merged_bb]
            cluster_bbs = cluster_split_bbox(
                binary, merged_bb, n_clusters, other_bb)

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

            cyr_cluster_indices = [
                ci for ci in range(n_clusters)
                if ci not in used_clusters]

            for ki, cl_idx in enumerate(kks_cluster_indices):
                if cl_idx < len(cluster_bbs):
                    cbb = cluster_bbs[cl_idx]
                    result.append({
                        'bbox': list(cbb),
                        'text': re.sub(
                            r'</?[a-z]+>', '',
                            kks_members[ki].get('text', '')).strip(),
                        'is_target': True,
                    })

            if cyr_cluster_indices:
                cyr_bboxes = [cluster_bbs[ci]
                              for ci in cyr_cluster_indices
                              if ci < len(cluster_bbs)]
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
                    for m in cyr_members)
                result.append({
                    'bbox': cyr_bbox,
                    'text': cyr_text,
                    'is_target': False,
                    'cyrillic_merged': len(cyr_members),
                })

        return result

    def _has_full_kks_text(self, text: str) -> bool:
        """Проверяет наличие полного KKS в тексте."""
        clean = re.sub(r'</?[a-z]+>', '', text)
        if self._full and self._full.regex.search(clean):
            return True
        head = self._head.regex.search(clean) if self._head else None
        tail = (re.search(r'\b(' + self._unit_re_raw + r')\d{2,4}',
                          clean, re.I) if self._unit_re_raw else None)
        return bool(head and tail)

    # ── collect_secondary_from_raw ───────────────

    def collect_secondary_from_raw(
            self, detections: list,
            line_pitch=None,
            fallback_med_h=None) -> list:
        """
        Собирает кириллические блоки из сырых OCR-детекций,
        минуя TB-фильтр. Фильтрует шум, мержит близкие.
        """
        cyr_candidates = []
        for b in detections:
            text_raw = b.get('text', '')
            text_clean = (re.sub(r'</?[a-z]+>', '', text_raw)
                          .replace('<br>', ' '))
            if not self.has_secondary_script(text_clean):
                continue
            if self.is_noise(text_raw):
                continue
            cyr_candidates.append({
                'bbox': list(b['bbox']),
                'text': text_clean.strip(),
                'is_target': False,
            })

        if not cyr_candidates:
            return []

        merged = self.postprocess_merge_secondary(
            cyr_candidates,
            max_vgap=line_pitch,
            max_hgap=line_pitch,
            fallback_med_h=fallback_med_h)

        result = []
        for b in merged:
            out = {k: v for k, v in b.items() if k != '_members'}
            if (not out.get('is_target', False)
                    and self.has_secondary_script(out.get('text', ''))):
                clean = re.sub(r'</?[a-z]+>', '', out.get('text', ''))
                cyr_words = re.findall(r'[а-яёА-ЯЁ]{2,}', clean)
                if cyr_words:
                    result.append(out)
        return result

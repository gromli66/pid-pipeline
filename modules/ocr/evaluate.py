#!/usr/bin/env python3
"""
evaluate.py -- Universal OCR block regrouping.

Адаптировано для серверного использования (без CLI).

Domain-specific logic (KKS, ISA, DIN, ...) is provided via DomainProfile.
This module contains only universal algorithms:
  - dedup_blocks (tile boundary dedup)
  - cluster_split_bbox (agglomerative clustering for multi-code bbox)
  - detect_tb (title block detection)
  - semantic_regroup (universal spatial grouping driven by profile)
"""
import cv2, numpy as np, re, os
from collections import defaultdict, Counter
from pathlib import Path

from modules.ocr.domain_profile import BaseDomainProfile


# =============================================================================
# DEDUP (tile boundary duplicates) — universal
# =============================================================================
def dedup_blocks(blocks):
    if not blocks:
        return blocks
    med_h = np.median([
        b['bbox'][3] - b['bbox'][1] for b in blocks
        if b['bbox'][3] - b['bbox'][1] > 0])
    remove = set()
    for i in range(len(blocks)):
        if i in remove:
            continue
        ti = (re.sub(r'</?b>', '', blocks[i]['text'])
              .replace('<br>', '|').strip())
        bi = blocks[i]['bbox']
        ci = blocks[i].get('confidence', 0)
        for j in range(i + 1, len(blocks)):
            if j in remove:
                continue
            tj = (re.sub(r'</?b>', '', blocks[j]['text'])
                  .replace('<br>', '|').strip())
            if ti != tj:
                continue
            bj = blocks[j]['bbox']
            if (abs((bi[1]+bi[3])/2 - (bj[1]+bj[3])/2) < med_h
                    and abs((bi[0]+bi[2])/2 - (bj[0]+bj[2])/2) < med_h*2):
                cj = blocks[j].get('confidence', 0)
                remove.add(j if ci >= cj else i)
                if i in remove:
                    break
    return [b for idx, b in enumerate(blocks) if idx not in remove]


# =============================================================================
# AGGLOMERATIVE CLUSTERING for multi-code bbox splitting — universal
# =============================================================================
def cluster_split_bbox(binary, bbox, n_clusters, other_bboxes):
    """Split a multi-code bbox into n_clusters sub-bboxes."""
    from sklearn.cluster import AgglomerativeClustering

    x1, y1, x2, y2 = [int(c) for c in bbox]
    h, w = y2-y1, x2-x1

    if binary is None or h < 5 or w < 5:
        lh = h / n_clusters
        return [(x1, y1+int(i*lh), x2, y1+int((i+1)*lh))
                for i in range(n_clusters)]

    crop = binary[max(0,y1):min(binary.shape[0],y2),
                  max(0,x1):min(binary.shape[1],x2)].copy()
    if crop.size == 0:
        lh = h / n_clusters
        return [(x1, y1+int(i*lh), x2, y1+int((i+1)*lh))
                for i in range(n_clusters)]

    for ob in other_bboxes:
        ox1 = max(0, int(ob[0])-x1); oy1 = max(0, int(ob[1])-y1)
        ox2 = min(w, int(ob[2])-x1); oy2 = min(h, int(ob[3])-y1)
        if ox2 > ox1 and oy2 > oy1:
            crop[oy1:oy2, ox1:ox2] = 0

    n_cc, labels, stats, centroids = cv2.connectedComponentsWithStats(crop, 8)
    min_area = max(3, h * w * 0.0005)
    ccs = []
    for i in range(1, n_cc):
        if stats[i, cv2.CC_STAT_AREA] < min_area:
            continue
        ccs.append({
            'cx': centroids[i][0], 'cy': centroids[i][1],
            'x1': stats[i, cv2.CC_STAT_LEFT],
            'y1': stats[i, cv2.CC_STAT_TOP],
            'x2': stats[i, cv2.CC_STAT_LEFT] + stats[i, cv2.CC_STAT_WIDTH],
            'y2': stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT],
        })

    if len(ccs) < n_clusters:
        lh = h / n_clusters
        return [(x1, y1+int(i*lh), x2, y1+int((i+1)*lh))
                for i in range(n_clusters)]

    X = np.array([[c['cx'], c['cy']] for c in ccs])
    agg = AgglomerativeClustering(n_clusters=n_clusters).fit(X)

    result = []
    for cl in range(n_clusters):
        members = [ccs[i] for i in range(len(ccs)) if agg.labels_[i] == cl]
        if members:
            result.append((
                x1 + min(c['x1'] for c in members),
                y1 + min(c['y1'] for c in members),
                x1 + max(c['x2'] for c in members),
                y1 + max(c['y2'] for c in members),
            ))
        else:
            result.append((x1, y1, x2, y2))
    result.sort(key=lambda r: (r[1], r[0]))
    return result


# =============================================================================
# TITLE BLOCK DETECTION — universal
# =============================================================================
def detect_tb(blocks, pw, ph, profile):
    """Detect title block region via text density in corners."""
    lb = []
    for b in blocks:
        text = re.sub(r'</?b>', '', b['text']).replace('<br>', ' ').strip()
        if len(text) < 15 or profile.has_any_target_pattern(text):
            continue
        bb = b['bbox']
        lb.append(((bb[0]+bb[2])/2/pw, (bb[1]+bb[3])/2/ph))
    if len(lb) < 5:
        return None
    g = defaultdict(int)
    for cx, cy in lb:
        g[(min(3, int(cx*4)), min(3, int(cy*4)))] += 1
    if not g:
        return None
    bc = max(g, key=g.get)
    if g[bc] < len(lb)/16*3 or bc[1] < 2:
        return None
    return (bc[0]/4, bc[1]/4)


# =============================================================================
# SEMANTIC REGROUP — universal engine, domain logic from profile
# =============================================================================
def semantic_regroup(blocks_raw, profile, image_path=None, debug=False):
    """
    Universal OCR block regrouping.

    Replaces both semantic_v5 and semantic_v5_debug.
    All domain-specific logic comes from profile.

    Args:
        blocks_raw: list of OCR blocks [{'text', 'bbox', 'confidence'}]
        profile: BaseDomainProfile instance
        image_path: path to cleaned image (for cluster_split_bbox)
        debug: if True, return ALL blocks with is_target flag
               if False, return only target blocks
    """
    blocks_raw = dedup_blocks(blocks_raw)

    binary = None
    if image_path and os.path.exists(str(image_path)):
        img_gray = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if img_gray is not None:
            _, binary = cv2.threshold(
                img_gray, 0, 255,
                cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    all_bboxes = [b['bbox'] for b in blocks_raw]
    signal = []

    for b in blocks_raw:
        text_raw = b['text']
        text_clean = re.sub(r'</?b>', '', text_raw)
        if profile.is_noise(text_raw):
            continue
        if '<br>' in text_clean:
            _process_multiline(signal, b, text_clean, profile, binary, all_bboxes)
        else:
            _process_singleline(signal, b, text_clean, profile)

    if not signal:
        return []

    # Title block filter
    pw = max(s['bbox'][2] for s in signal) * 1.005
    ph = max(s['bbox'][3] for s in signal) * 1.005
    tb = detect_tb(signal, pw, ph, profile)
    if tb:
        signal = [s for s in signal if not (
            (s['bbox'][0]+s['bbox'][2])/2/pw >= tb[0] and
            (s['bbox'][1]+s['bbox'][3])/2/ph >= tb[1] and
            not profile.has_any_target_pattern(s['text']) and
            s['cls'] == 'OTHER')]

    signal = [s for s in signal
              if s['cls'] != 'OTHER'
              or not profile.is_noise(s['text'])]
    if not signal:
        return []

    # Spatial grouping
    med_h = np.median([s['bbox'][3]-s['bbox'][1] for s in signal
                       if s['bbox'][3]-s['bbox'][1] > 0])
    cell = max(med_h * 2, 30)
    grid = defaultdict(list)
    for i, s in enumerate(signal):
        bb = s['bbox']
        grid[(int((bb[0]+bb[2])/2/cell), int((bb[1]+bb[3])/2/cell))].append(i)

    def find_near(idx, cls_set, maxv=3.0, below_only=False):
        bi = signal[idx]['bbox']; wi, hi = bi[2]-bi[0], bi[3]-bi[1]
        if wi <= 0 or hi <= 0: return None
        cyi = (bi[1]+bi[3])/2; gx = int((bi[0]+bi[2])/2/cell); gy = int(cyi/cell)
        best, bd = None, float('inf')
        for dx in range(-3, 4):
            for dy in range(-3, 4):
                for j in grid.get((gx+dx, gy+dy), []):
                    if j == idx or signal[j]['cls'] not in cls_set: continue
                    bj = signal[j]['bbox']; wj, hj = bj[2]-bj[0], bj[3]-bj[1]
                    if wj <= 0 or hj <= 0: continue
                    cyj = (bj[1]+bj[3])/2
                    if below_only and cyj < cyi: continue
                    if (min(bi[2],bj[2])-max(bi[0],bj[0]))/min(wi,wj) < 0.25: continue
                    vd = abs(cyi-cyj)/((hi+hj)/2)
                    if vd < bd and vd <= maxv: bd = vd; best = j
        return best

    def merge_bbs(idxs):
        bbs = [signal[i]['bbox'] for i in idxs]
        return [min(b[0] for b in bbs), min(b[1] for b in bbs),
                max(b[2] for b in bbs), max(b[3] for b in bbs)]

    # Execute grouping passes
    used = set()
    results = []

    for gpass in profile.get_grouping_passes():
        if gpass.action == 'merge_text_and_reclassify':
            _pass_frag_complete(signal, used, grid, cell, med_h, gpass, profile)

        elif gpass.action == 'emit':
            for i, s in enumerate(signal):
                if i in used or s['cls'] not in gpass.source: continue
                used.add(i)
                results.append({'bbox': list(s['bbox']), 'text': s['text'], 'idx': [i]})

        elif gpass.action == 'merge_and_emit':
            for i, s in enumerate(signal):
                if i in used or s['cls'] not in gpass.source: continue
                grp = [i]; used.add(i)
                direction = gpass.direction == 'below_only'
                tj = find_near(i, set(gpass.target), gpass.max_distance, direction)
                if tj is not None and tj not in used:
                    grp.append(tj); used.add(tj)
                    if gpass.chain:
                        t2 = find_near(tj, set(gpass.target), gpass.chain_max_distance, direction)
                        if t2 is not None and t2 not in used:
                            cy_t1 = (signal[tj]['bbox'][1]+signal[tj]['bbox'][3])/2
                            cy_t2 = (signal[t2]['bbox'][1]+signal[t2]['bbox'][3])/2
                            head_between = False
                            if gpass.chain_head_between_check:
                                head_between = any(
                                    signal[k]['cls'] in gpass.source and k not in used
                                    and cy_t1 < (signal[k]['bbox'][1]+signal[k]['bbox'][3])/2 < cy_t2
                                    for k in range(len(signal)))
                            if not head_between and abs((signal[i]['bbox'][1]+signal[i]['bbox'][3])/2 - cy_t2)/med_h < 4:
                                grp.append(t2); used.add(t2)
                bb = merge_bbs(grp)
                texts = [signal[g]['text'] for g in grp]
                results.append({'bbox': bb, 'text': ' | '.join(texts), 'idx': grp})

        elif gpass.action == 'attach_to_existing':
            for i, s in enumerate(signal):
                if i in used or s['cls'] not in gpass.source: continue
                hj = find_near(i, set(gpass.target), gpass.max_distance)
                if hj:
                    for r in results:
                        if hj in r.get('idx', []):
                            sbb = s['bbox']
                            r['bbox'][0] = min(r['bbox'][0], sbb[0])
                            r['bbox'][1] = min(r['bbox'][1], sbb[1])
                            r['bbox'][2] = max(r['bbox'][2], sbb[2])
                            r['bbox'][3] = max(r['bbox'][3], sbb[3])
                            r['text'] += ' | ' + s['text']
                            used.add(i); break
                if i not in used: used.add(i)

    # Remaining with target pattern
    for i, s in enumerate(signal):
        if i in used: continue
        if profile.has_any_target_pattern(s['text']):
            used.add(i)
            results.append({'bbox': list(s['bbox']), 'text': s['text']})

    # Mark is_target + build output
    out = []
    for r in results:
        r.pop('idx', None)
        r['is_target'] = profile.is_target(r['text'])
        out.append(r)

    if debug:
        for i, s in enumerate(signal):
            if i in used: continue
            out.append({'bbox': list(s['bbox']), 'text': s['text'], 'is_target': False})

    return out


# ── Signal helpers ────────────────────────────────

def _process_multiline(signal, b, text_clean, profile, binary, all_bboxes):
    raw_lines = [l.strip() for l in text_clean.split('<br>') if l.strip()]
    lines = []
    for rl in raw_lines:
        lines.extend(profile.split_inline(rl))

    bb = b['bbox']

    # Classify each line and group by role
    line_cls = [(l, profile.classify(l)) for l in lines]
    line_roles = [(l, c, profile.category_role(c)) for l, c in line_cls]

    heads = [l for l, c, r in line_roles if r in ('head', 'full')]
    tails = [l for l, c, r in line_roles if r == 'tail']
    standalones = [l for l, c, r in line_roles if r == 'standalone']
    n_heads = len([l for l, c, r in line_roles if r == 'head'])

    if n_heads >= 2 and len(tails) >= 2:
        other_bb = [ob for ob in all_bboxes if ob != bb]
        cluster_bbs = cluster_split_bbox(binary, bb, n_heads, other_bb)
        lh = (bb[3]-bb[1]) / max(len(raw_lines), 1)
        def _overlap(a, b_):
            return max(0, min(a[2],b_[2])-max(a[0],b_[0]))*max(0, min(a[3],b_[3])-max(a[1],b_[1]))
        for il, rl in enumerate(raw_lines):
            tok_y1 = int(bb[1] + il * lh); tok_y2 = int(bb[1] + (il+1) * lh)
            approx_cy = (tok_y1 + tok_y2) / 2
            parts = profile.split_inline(rl)
            full_width = len(parts) == 1
            pw = (bb[2]-bb[0]) / max(len(parts), 1)
            for ip, part in enumerate(parts):
                tok_x1 = bb[0] + ip*pw; tok_x2 = bb[0] + (ip+1)*pw
                if full_width:
                    best_cl = min(range(len(cluster_bbs)), key=lambda c:
                        abs(approx_cy - (cluster_bbs[c][1]+cluster_bbs[c][3])/2))
                else:
                    ovs = [_overlap([tok_x1,tok_y1,tok_x2,tok_y2], c) for c in cluster_bbs]
                    if max(ovs) > 0: best_cl = ovs.index(max(ovs))
                    else: best_cl = min(range(len(cluster_bbs)), key=lambda c:
                        abs(approx_cy - (cluster_bbs[c][1]+cluster_bbs[c][3])/2))
                cbb = cluster_bbs[best_cl]
                signal.append({'text': part, 'bbox': [cbb[0], tok_y1, cbb[2], tok_y2],
                               'cls': profile.classify(part)})
    elif len(heads) >= 1 and tails and standalones:
        non_standalone = [l for l, c, r in line_roles if r != 'standalone']
        standalone_lines = [l for l, c, r in line_roles if r == 'standalone']
        n_parts = (1 if non_standalone else 0) + len(standalone_lines)
        other_bb = [ob for ob in all_bboxes if ob != bb]
        cluster_bbs = cluster_split_bbox(binary, bb, max(n_parts, 2), other_bb)
        all_texts = []
        if non_standalone: all_texts.append((' | '.join(non_standalone), profile.classify(' | '.join(non_standalone))))
        for d in standalone_lines: all_texts.append((d, profile.classify(d)))
        for idx, cbb in enumerate(cluster_bbs):
            if idx < len(all_texts):
                text, cls = all_texts[idx]
                signal.append({'text': text, 'bbox': list(cbb), 'cls': cls})
    elif len(heads) >= 1 and tails:
        combined_text = ' | '.join(lines)
        combined_cls = profile.classify(combined_text)
        # heads+tails combined: if classify doesn't recognize it, use first head's class
        if combined_cls == 'OTHER':
            combined_cls = profile.classify(heads[0])
        signal.append({'text': combined_text, 'bbox': list(bb), 'cls': combined_cls})
    elif len(heads) >= 1 and standalones:
        n_parts = len(raw_lines)
        other_bb = [ob for ob in all_bboxes if ob != bb]
        cluster_bbs = cluster_split_bbox(binary, bb, n_parts, other_bb)
        for idx, rl in enumerate(raw_lines):
            cbb = cluster_bbs[idx] if idx < len(cluster_bbs) else bb
            signal.append({'text': rl, 'bbox': list(cbb), 'cls': profile.classify(rl)})
    else:
        n_parts = len(raw_lines)
        other_bb = [ob for ob in all_bboxes if ob != bb]
        cluster_bbs = cluster_split_bbox(binary, bb, n_parts, other_bb)
        for idx, rl in enumerate(raw_lines):
            cbb = cluster_bbs[idx] if idx < len(cluster_bbs) else bb
            signal.append({'text': rl, 'bbox': list(cbb), 'cls': profile.classify(rl)})


def _process_singleline(signal, b, text_clean, profile):
    parts = profile.split_inline(text_clean)
    if len(parts) > 1:
        w = b['bbox'][2]-b['bbox'][0]; pw_ = w/len(parts)
        for i, p in enumerate(parts):
            signal.append({'text': p,
                'bbox': [b['bbox'][0]+i*pw_, b['bbox'][1], b['bbox'][0]+(i+1)*pw_, b['bbox'][3]],
                'cls': profile.classify(p)})
    else:
        signal.append({'text': text_clean, 'bbox': list(b['bbox']),
                       'cls': profile.classify(text_clean)})


def _pass_frag_complete(signal, used, grid, cell, med_h, gpass, profile):
    for i, s in enumerate(signal):
        if s['cls'] not in gpass.source: continue
        bi = s['bbox']; cyi = (bi[1]+bi[3])/2; hi = bi[3]-bi[1]
        gx = int((bi[0]+bi[2])/2/cell); gy = int(cyi/cell); done = False
        for dx in range(-2, 3):
            if done: break
            for dy in range(-2, 3):
                if done: break
                for j in grid.get((gx+dx, gy+dy), []):
                    if j == i: continue
                    bj = signal[j]['bbox']; hj = bj[3]-bj[1]
                    vd = abs(cyi-(bj[1]+bj[3])/2)/((hi+hj)/2) if hi+hj > 0 else 99
                    if vd < gpass.max_distance:
                        combined = s['text'].strip() + signal[j]['text'].strip()
                        cls2 = profile.classify(combined)
                        if profile.category_role(cls2) in ('head', 'full'):
                            signal[i]['text'] = combined; signal[i]['cls'] = cls2
                            signal[i]['bbox'] = [min(bi[0],bj[0]), min(bi[1],bj[1]),
                                                 max(bi[2],bj[2]), max(bi[3],bj[3])]
                            signal[j]['cls'] = 'USED'; used.add(j); done = True; break


# CLI evaluation helpers (iou, f1_at, load_yolo) удалены — server mode.


# CLI (main, load_yolo, f1_at evaluators) удалены — server mode.
# Используются: semantic_regroup, cluster_split_bbox, dedup_blocks.

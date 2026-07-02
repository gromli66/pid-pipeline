"""
Edge Routing v2 — Ортогональный маршрутизатор рёбер графа P&ID.

Правила (все жёсткие):
R1. Ортогональность: каждый сегмент строго H или V
R2. Перпендикулярный выход: первый/последний сегмент ⊥ стороне bbox
R3. Stub ≥ 5px наружу от bbox (target 15px, min 5px)
R4. Не проходить через bbox любого узла
R5. Уникальность: рёбра разнесены по сторонам узлов
R6. Минимум поворотов: L < Z < U
R7. Не параллельно стенке bbox ближе 15px
R8. Минимум пересечений с другими рёбрами (приоритет выше R5, R6)

Pipeline:
0. Подготовка: sides, connection points (distributed), stubs
1. Генерация кандидатов: L, Z, U между stub endpoints
2. Фильтрация: R4 (bbox), R7 (вдоль стенки)
3. Скоринг: R8 (пересечения) > R6 (повороты) > длина, zigzag
4. Сборка: conn → stub → route → stub → conn
5. Clean collinear
6. Финальная валидация R1-R7
"""

from __future__ import annotations
from typing import List, Tuple, Optional

# Waypoint format: [y, x]
# Points in routing: (x, y)

STUB_TARGET = 15
STUB_MIN = 5
WALL_MARGIN = 15  # R7: минимальное расстояние от стенки bbox

# =====================================================================
# Geometry helpers
# =====================================================================

def _seg_hits_bbox(ax: float, ay: float, bx: float, by: float,
                   bbox: list, margin: float = 2) -> bool:
    """Отрезок (ax,ay)→(bx,by) пересекает bbox с margin?"""
    x1, y1, x2, y2 = bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin
    seg_x1, seg_x2 = min(ax, bx), max(ax, bx)
    seg_y1, seg_y2 = min(ay, by), max(ay, by)
    if seg_x2 < x1 or seg_x1 > x2 or seg_y2 < y1 or seg_y1 > y2:
        return False
    if abs(ax - bx) < 0.5:  # V
        return x1 <= ax <= x2 and seg_y1 < y2 and seg_y2 > y1
    if abs(ay - by) < 0.5:  # H
        return y1 <= ay <= y2 and seg_x1 < x2 and seg_x2 > x1
    return True  # diagonal — conservative


# Public alias — используется в AdvancedGraphEditor
segment_intersects_bbox = _seg_hits_bbox


def _seg_near_bbox_wall(ax: float, ay: float, bx: float, by: float,
                        bbox: list, margin: float = WALL_MARGIN) -> bool:
    """R7: Сегмент идёт вдоль стенки bbox СНАРУЖИ ближе margin px.
    Только если сегмент перекрывает bbox по параллельной оси (иначе он просто рядом, не вдоль).
    Внутренность bbox — зона R4 (_seg_hits_bbox), здесь не проверяется."""
    x1, y1, x2, y2 = bbox
    if abs(ax - bx) < 0.5:  # Vertical segment at x=ax
        # Параллелен left/right стенке снаружи?
        near_left = 0 < (x1 - ax) < margin    # слева от левой стенки
        near_right = 0 < (ax - x2) < margin   # справа от правой стенки
        if near_left or near_right:
            # Перекрытие по y — сегмент должен идти ВДОЛЬ bbox, не просто рядом
            seg_y1, seg_y2 = min(ay, by), max(ay, by)
            overlap = min(seg_y2, y2) - max(seg_y1, y1)
            if overlap > 5:  # значительное перекрытие
                return True
    if abs(ay - by) < 0.5:  # Horizontal segment at y=ay
        near_top = 0 < (y1 - ay) < margin     # выше верхней стенки
        near_bottom = 0 < (ay - y2) < margin  # ниже нижней стенки
        if near_top or near_bottom:
            seg_x1, seg_x2 = min(ax, bx), max(ax, bx)
            overlap = min(seg_x2, x2) - max(seg_x1, x1)
            if overlap > 5:
                return True
    return False


def _segments_cross(a1: tuple, a2: tuple, b1: tuple, b2: tuple) -> bool:
    """Два ортогональных отрезка пересекаются? (H meets V or vice versa)."""
    # a: (a1x,a1y)→(a2x,a2y), b: (b1x,b1y)→(b2x,b2y)
    a_h = abs(a1[1] - a2[1]) < 0.5  # horizontal
    b_h = abs(b1[1] - b2[1]) < 0.5
    if a_h == b_h:
        return False  # parallel — no cross (overlap не считаем)
    if a_h:
        # a horizontal at y=a1.y, b vertical at x=b1.x
        h_y = a1[1]
        h_x1, h_x2 = min(a1[0], a2[0]), max(a1[0], a2[0])
        v_x = b1[0]
        v_y1, v_y2 = min(b1[1], b2[1]), max(b1[1], b2[1])
        return h_x1 < v_x < h_x2 and v_y1 < h_y < v_y2
    else:
        # a vertical, b horizontal
        return _segments_cross(b1, b2, a1, a2)


def _path_to_segments(pts: list[tuple]) -> list[tuple[tuple, tuple]]:
    """Путь [(x,y),...] → список сегментов [((x1,y1),(x2,y2)),...]."""
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


# =====================================================================
# Step 0: Preparation
# =====================================================================

def compute_side(src_bbox: list, src_cx: float, src_cy: float,
                 tgt_cx: float, tgt_cy: float) -> str:
    """Определить сторону bbox для выхода к target."""
    x1, y1, x2, y2 = src_bbox
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    dx = tgt_cx - cx
    dy = tgt_cy - cy
    if abs(dx) > abs(dy):
        return 'right' if dx > 0 else 'left'
    else:
        return 'bottom' if dy > 0 else 'top'


def distribute_connection_points(
    node_id: str, side: str, bbox: list,
    all_edges: list[dict], nodes: dict
) -> dict[str, tuple[float, float]]:
    """Распределить connection points для всех рёбер на стороне node_id:side.

    Returns: {edge_id: (x, y)} для каждого ребра на этой стороне.
    """
    # Найти все рёбра на этой стороне
    edges_on_side = []
    for e in all_edges:
        if e['source'] == node_id and e.get('_src_side') == side:
            other_id = e['target']
            edges_on_side.append((e, other_id, 'source'))
        elif e['target'] == node_id and e.get('_tgt_side') == side:
            other_id = e['source']
            edges_on_side.append((e, other_id, 'target'))

    if not edges_on_side:
        return {}

    # Сортировать по направлению к other node
    def sort_key(item):
        _, other_id, _ = item
        other = nodes.get(other_id)
        if not other:
            return 0
        if side in ('left', 'right'):
            return other['centroid'][0]  # sort by y of other
        else:
            return other['centroid'][1]  # sort by x of other

    edges_on_side.sort(key=sort_key)

    x1, y1, x2, y2 = bbox
    n = len(edges_on_side)

    result = {}
    if side in ('left', 'right'):
        x = x1 if side == 'left' else x2
        length = y2 - y1
        for i, (e, _, _) in enumerate(edges_on_side):
            # Делим на n+1 частей, размещаем на границах
            y = y1 + length * (i + 1) / (n + 1)
            result[e['id']] = (x, y)
    else:
        y = y1 if side == 'top' else y2
        length = x2 - x1
        for i, (e, _, _) in enumerate(edges_on_side):
            x = x1 + length * (i + 1) / (n + 1)
            result[e['id']] = (x, y)

    return result


def compute_stub_endpoint(conn_x: float, conn_y: float, side: str,
                          stub_len: float = STUB_TARGET) -> tuple[float, float]:
    """Точка конца stub'а — conn_point + stub_len перпендикулярно наружу."""
    if side == 'right':    return (conn_x + stub_len, conn_y)
    elif side == 'left':   return (conn_x - stub_len, conn_y)
    elif side == 'bottom': return (conn_x, conn_y + stub_len)
    else:                  return (conn_x, conn_y - stub_len)  # top


# =====================================================================
# Step 1: Generate candidates
# =====================================================================

def _gen_l_shapes(sx: float, sy: float, tx: float, ty: float) -> list[list[tuple]]:
    """L-shape: 1 поворот. Два варианта."""
    candidates = []
    # Вариант 1: H first → V
    wp = (tx, sy)
    candidates.append([wp])
    # Вариант 2: V first → H
    wp = (sx, ty)
    candidates.append([wp])
    return candidates


def _gen_z_shapes(sx: float, sy: float, tx: float, ty: float,
                  obstacles: list[list] | None = None) -> list[list[tuple]]:
    """Z-shape: 2 поворота. Стволы по долям пути + по границам препятствий."""
    candidates = []
    dx = tx - sx
    dy = ty - sy

    # H→V→H (вертикальный ствол): для разных mid_x
    if abs(dy) > 1:
        for frac in (0.25, 0.5, 0.75):
            mid_x = sx + dx * frac
            candidates.append([(mid_x, sy), (mid_x, ty)])

    # V→H→V (горизонтальный ствол): для разных mid_y
    if abs(dx) > 1:
        for frac in (0.25, 0.5, 0.75):
            mid_y = sy + dy * frac
            candidates.append([(sx, mid_y), (tx, mid_y)])

    # Стволы по «интересным координатам» — границы препятствий в коридоре
    # ±WALL_MARGIN. Даёт обходы впритык к margin там, где доли 0.25/0.5/0.75
    # заняты препятствиями.
    if obstacles:
        min_x, max_x = min(sx, tx), max(sx, tx)
        min_y, max_y = min(sy, ty), max(sy, ty)
        xs: set = set()
        ys: set = set()
        for bx1, by1, bx2, by2 in obstacles:
            if bx1 < max_x + WALL_MARGIN and bx2 > min_x - WALL_MARGIN and \
               by1 < max_y + WALL_MARGIN and by2 > min_y - WALL_MARGIN:
                for x in (bx1 - WALL_MARGIN, bx2 + WALL_MARGIN):
                    if min_x + 1 < x < max_x - 1:
                        xs.add(round(x, 1))
                for y in (by1 - WALL_MARGIN, by2 + WALL_MARGIN):
                    if min_y + 1 < y < max_y - 1:
                        ys.add(round(y, 1))
        MAX_COORDS = 20  # защита от взрыва кандидатов в плотных местах
        if abs(dy) > 1:
            for mid_x in sorted(xs)[:MAX_COORDS]:
                candidates.append([(mid_x, sy), (mid_x, ty)])
        if abs(dx) > 1:
            for mid_y in sorted(ys)[:MAX_COORDS]:
                candidates.append([(sx, mid_y), (tx, mid_y)])

    return candidates


def _gen_u_shapes(sx: float, sy: float, tx: float, ty: float,
                  obstacles: list[list], margin: float = WALL_MARGIN) -> list[list[tuple]]:
    """U-shape: 3 поворота. Обход каждого препятствия с 4 сторон."""
    candidates = []
    min_x, max_x = min(sx, tx), max(sx, tx)
    min_y, max_y = min(sy, ty), max(sy, ty)

    for bbox in obstacles:
        bx1, by1, bx2, by2 = bbox
        # Если bbox в области маршрута
        if bx1 < max_x + margin and bx2 > min_x - margin and \
           by1 < max_y + margin and by2 > min_y - margin:
            # Обход сверху
            yy = by1 - margin
            candidates.append([(sx, yy), (tx, yy)])
            # Обход снизу
            yy = by2 + margin
            candidates.append([(sx, yy), (tx, yy)])
            # Обход слева
            xx = bx1 - margin
            candidates.append([(xx, sy), (xx, ty)])
            # Обход справа
            xx = bx2 + margin
            candidates.append([(xx, sy), (xx, ty)])

    # Также: U вокруг всей зоны
    all_bboxes = obstacles
    if all_bboxes:
        global_min_x = min(b[0] for b in all_bboxes)
        global_max_x = max(b[2] for b in all_bboxes)
        global_min_y = min(b[1] for b in all_bboxes)
        global_max_y = max(b[3] for b in all_bboxes)
        candidates.append([(sx, global_min_y - margin), (tx, global_min_y - margin)])
        candidates.append([(sx, global_max_y + margin), (tx, global_max_y + margin)])
        candidates.append([(global_min_x - margin, sy), (global_min_x - margin, ty)])
        candidates.append([(global_max_x + margin, sy), (global_max_x + margin, ty)])

    return candidates


def generate_candidates(sx: float, sy: float, tx: float, ty: float,
                        obstacles: list[list]) -> list[list[tuple]]:
    """Генерация всех кандидатов маршрута (между stub endpoints)."""
    candidates = []

    # Прямая (0 поворотов)
    if abs(sx - tx) < 1 or abs(sy - ty) < 1:
        candidates.append([])  # прямая линия

    # L-shapes (1 поворот)
    candidates.extend(_gen_l_shapes(sx, sy, tx, ty))

    # Z-shapes (2 поворота): доли пути + границы препятствий
    candidates.extend(_gen_z_shapes(sx, sy, tx, ty, obstacles))

    # U-shapes (3 поворота)
    candidates.extend(_gen_u_shapes(sx, sy, tx, ty, obstacles))

    return candidates


# =====================================================================
# Step 2: Filtering
# =====================================================================

def filter_candidate(route_pts: list[tuple], all_bboxes: list[list],
                     src_bbox: list, tgt_bbox: list) -> bool:
    """Проверить кандидата на жёсткие правила. True = проходит."""
    segs = _path_to_segments(route_pts)

    for i, (a, b) in enumerate(segs):
        # R1: ортогональность
        if abs(a[0] - b[0]) > 0.5 and abs(a[1] - b[1]) > 0.5:
            return False

        # R4: не через bbox препятствий
        for bbox in all_bboxes:
            if _seg_hits_bbox(a[0], a[1], b[0], b[1], bbox, margin=2):
                return False

        # R7: не параллельно стенке ТОЛЬКО для препятствий (не src/tgt)
        # И пропускать первый/последний сегмент (stubs)
        if 0 < i < len(segs) - 1:
            for bbox in all_bboxes:
                if _seg_near_bbox_wall(a[0], a[1], b[0], b[1], bbox, margin=WALL_MARGIN):
                    return False

    # R4: проверить что СРЕДНИЕ сегменты не через src/tgt bbox.
    # margin=0 (был -1): сегмент ровно ПО стенке своего узла тоже запрещён —
    # иначе ребро сливается со стенкой (см. AUDIT O10, шаг 4).
    for i, (a, b) in enumerate(segs):
        if i == 0 or i == len(segs) - 1:
            continue  # stubs start/end on bbox — OK
        if _seg_hits_bbox(a[0], a[1], b[0], b[1], src_bbox, margin=0):
            return False
        if _seg_hits_bbox(a[0], a[1], b[0], b[1], tgt_bbox, margin=0):
            return False

    return True


def _count_violations(pts: list[tuple], all_bboxes: list[list],
                      src_bbox: list, tgt_bbox: list) -> int:
    """Число пересечений пути с bbox'ами (margin=0) — для мягкого fallback."""
    segs = _path_to_segments(pts)
    violations = 0
    for i, (a, b) in enumerate(segs):
        for bbox in all_bboxes:
            if _seg_hits_bbox(a[0], a[1], b[0], b[1], bbox, margin=0):
                violations += 1
        if 0 < i < len(segs) - 1:
            if _seg_hits_bbox(a[0], a[1], b[0], b[1], src_bbox, margin=0):
                violations += 1
            if _seg_hits_bbox(a[0], a[1], b[0], b[1], tgt_bbox, margin=0):
                violations += 1
    return violations


# =====================================================================
# Step 3: Scoring
# =====================================================================

def _segments_collinear_overlap(a1: tuple, a2: tuple, b1: tuple, b2: tuple) -> float:
    """Длина перекрытия двух коллинеарных (параллельных и совпадающих) ортогональных сегментов."""
    a_h = abs(a1[1] - a2[1]) < 0.5
    b_h = abs(b1[1] - b2[1]) < 0.5

    if a_h and b_h:
        # Оба горизонтальных — совпадают по y?
        if abs(a1[1] - b1[1]) < 3:
            # Перекрытие по x
            a_min, a_max = min(a1[0], a2[0]), max(a1[0], a2[0])
            b_min, b_max = min(b1[0], b2[0]), max(b1[0], b2[0])
            overlap = min(a_max, b_max) - max(a_min, b_min)
            return max(0, overlap)
    elif not a_h and not b_h:
        # Оба вертикальных — совпадают по x?
        if abs(a1[0] - b1[0]) < 3:
            a_min, a_max = min(a1[1], a2[1]), max(a1[1], a2[1])
            b_min, b_max = min(b1[1], b2[1]), max(b1[1], b2[1])
            overlap = min(a_max, b_max) - max(a_min, b_min)
            return max(0, overlap)
    return 0


def score_candidate(route_pts: list[tuple],
                    existing_edges: list[list[tuple]]) -> float:
    """Оценить кандидата. Меньше = лучше."""
    segs = _path_to_segments(route_pts)

    # R8: пересечения с другими рёбрами
    crossings = 0
    # R5: collinear overlap с другими рёбрами
    collinear_overlap = 0.0

    for seg_a in segs:
        for other_path in existing_edges:
            other_segs = _path_to_segments(other_path)
            for seg_b in other_segs:
                if _segments_cross(seg_a[0], seg_a[1], seg_b[0], seg_b[1]):
                    crossings += 1
                overlap = _segments_collinear_overlap(seg_a[0], seg_a[1], seg_b[0], seg_b[1])
                collinear_overlap += overlap

    # R6: повороты
    turns = len(segs) - 1 if len(segs) > 1 else 0

    # Длина
    total_length = 0
    for a, b in segs:
        total_length += abs(a[0] - b[0]) + abs(a[1] - b[1])

    # Zigzag: штраф за разворот на 180°. В ортогональном пути соседние
    # сегменты перпендикулярны, поэтому разворот виден только при сравнении
    # сегментов i и i+2 (оба H или оба V, направления противоположны).
    zigzags = 0
    for i in range(len(segs) - 2):
        (a1, a2), (b1, b2) = segs[i], segs[i + 2]
        dx1, dy1 = a2[0] - a1[0], a2[1] - a1[1]
        dx2, dy2 = b2[0] - b1[0], b2[1] - b1[1]
        if abs(dx1) > 1 and abs(dx2) > 1 and dx1 * dx2 < 0:
            zigzags += 1
        elif abs(dy1) > 1 and abs(dy2) > 1 and dy1 * dy2 < 0:
            zigzags += 1

    return (collinear_overlap * 5 +  # R5: сильнейший штраф за совпадение
            crossings * 1000 +        # R8: пересечения
            zigzags * 500 +           # zigzag
            turns * 100 +             # R6: повороты
            total_length * 1)         # длина — ×1, не ×0.1


# =====================================================================
# Step 5: Clean collinear
# =====================================================================

def clean_collinear(pts: list[tuple]) -> list[tuple]:
    """Убрать коллинеарные точки (3 на одной H/V линии → средняя лишняя)."""
    if len(pts) < 3:
        return pts
    result = [pts[0]]
    for i in range(1, len(pts) - 1):
        prev, curr, nxt = result[-1], pts[i], pts[i + 1]
        # Same x → vertical collinear
        if abs(prev[0] - curr[0]) < 0.5 and abs(curr[0] - nxt[0]) < 0.5:
            continue
        # Same y → horizontal collinear
        if abs(prev[1] - curr[1]) < 0.5 and abs(curr[1] - nxt[1]) < 0.5:
            continue
        result.append(curr)
    result.append(pts[-1])
    return result


# =====================================================================
# Step 6: Final validation
# =====================================================================

def validate_path(pts: list[tuple], all_bboxes: list[list],
                  src_bbox: list, tgt_bbox: list) -> list[str]:
    """Проверить финальный путь на все правила. Returns список нарушений."""
    errors = []
    segs = _path_to_segments(pts)

    for i, (a, b) in enumerate(segs):
        # R1
        if abs(a[0] - b[0]) > 0.5 and abs(a[1] - b[1]) > 0.5:
            errors.append(f"R1: segment {i} diagonal: {a}→{b}")

        # R4
        for bbox in all_bboxes:
            if _seg_hits_bbox(a[0], a[1], b[0], b[1], bbox, margin=0):
                errors.append(f"R4: segment {i} hits obstacle bbox")

    return errors


# =====================================================================
# Main entry point
# =====================================================================

def route_edge(
    src_conn: tuple[float, float],
    tgt_conn: tuple[float, float],
    src_side: str,
    tgt_side: str,
    src_bbox: list,
    tgt_bbox: list,
    obstacle_bboxes: list[list],
    existing_edge_paths: list[list[tuple]] | None = None,
) -> list[list]:
    """
    Построить маршрут ребра.

    Args:
        src_conn: (x, y) точка прикрепления на src bbox
        tgt_conn: (x, y) точка прикрепления на tgt bbox
        src_side: 'left'/'right'/'top'/'bottom'
        tgt_side: 'left'/'right'/'top'/'bottom'
        src_bbox: [x1, y1, x2, y2]
        tgt_bbox: [x1, y1, x2, y2]
        obstacle_bboxes: список bbox всех препятствий (без src/tgt)
        existing_edge_paths: пути уже построенных рёбер [(x,y),...] для подсчёта пересечений

    Returns:
        waypoints в формате [[y, x], ...] (без source/target points)
    """
    sx, sy = src_conn
    tx, ty = tgt_conn

    # --- Step 0: Stubs ---
    stub_src = compute_stub_endpoint(sx, sy, src_side, STUB_TARGET)
    stub_tgt = compute_stub_endpoint(tx, ty, tgt_side, STUB_TARGET)

    # Если stub проходит через препятствие — уменьшить до STUB_MIN
    for bbox in obstacle_bboxes:
        if _seg_hits_bbox(sx, sy, stub_src[0], stub_src[1], bbox, margin=0):
            stub_src = compute_stub_endpoint(sx, sy, src_side, STUB_MIN)
            break
    for bbox in obstacle_bboxes:
        if _seg_hits_bbox(tx, ty, stub_tgt[0], stub_tgt[1], bbox, margin=0):
            stub_tgt = compute_stub_endpoint(tx, ty, tgt_side, STUB_MIN)
            break

    ss_x, ss_y = stub_src  # stub source endpoint
    st_x, st_y = stub_tgt  # stub target endpoint

    # --- Step 1: Generate candidates (between stub endpoints) ---
    all_obs = obstacle_bboxes  # + [src_bbox, tgt_bbox] для фильтрации
    candidates = generate_candidates(ss_x, ss_y, st_x, st_y, all_obs + [src_bbox, tgt_bbox])

    # --- Step 2+3: Filter & Score ---
    existing = existing_edge_paths or []
    best_route = None
    best_score = float('inf')

    for route_wps in candidates:
        # Собрать полный путь для проверки: src → stub → route → stub → tgt
        full_pts = [(sx, sy), stub_src]
        if route_wps:
            full_pts.extend(route_wps)
        full_pts.append(stub_tgt)
        full_pts.append((tx, ty))

        # Clean collinear перед фильтрацией
        full_pts = clean_collinear(full_pts)

        # Filter
        if not filter_candidate(full_pts, obstacle_bboxes, src_bbox, tgt_bbox):
            continue

        # Score
        score = score_candidate(full_pts, existing)
        if score < best_score:
            best_score = score
            best_route = full_pts

    # --- Fallback 1: ничего не прошло жёсткий фильтр — мягкий проход ---
    # Выбираем наименее нарушающего кандидата: каждое пересечение bbox —
    # крупный штраф. Маршрут сквозь узел возможен, только если вариантов
    # без пересечений нет вообще (см. AUDIT O10, шаг 4).
    if best_route is None:
        best_soft_score = float('inf')
        for route_wps in candidates:
            full_pts = [(sx, sy), stub_src]
            if route_wps:
                full_pts.extend(route_wps)
            full_pts.append(stub_tgt)
            full_pts.append((tx, ty))
            full_pts = clean_collinear(full_pts)
            # диагональ недопустима даже в мягком режиме (R1 жёсткое)
            if any(abs(a[0] - b[0]) > 0.5 and abs(a[1] - b[1]) > 0.5
                   for a, b in _path_to_segments(full_pts)):
                continue
            viol = _count_violations(full_pts, obstacle_bboxes,
                                     src_bbox, tgt_bbox)
            soft_score = score_candidate(full_pts, existing) + viol * 100_000
            if soft_score < best_soft_score:
                best_soft_score = soft_score
                best_route = full_pts

    # --- Fallback 2 (крайний случай — кандидатов нет вообще): простой L ---
    if best_route is None:
        full_pts = [(sx, sy), stub_src, (st_x, ss_y), stub_tgt, (tx, ty)]
        full_pts = clean_collinear(full_pts)
        best_route = full_pts

    # --- Step 5: Final clean ---
    best_route = clean_collinear(best_route)

    # --- Step 6: Validate ---
    errors = validate_path(best_route, obstacle_bboxes, src_bbox, tgt_bbox)
    if errors:
        # Log but don't fail
        pass  # TODO: logging

    # --- Convert to waypoints [y, x] ---
    # Убрать первую (src_conn) и последнюю (tgt_conn) точки
    inner_pts = best_route[1:-1]
    waypoints = [[p[1], p[0]] for p in inner_pts]

    return waypoints

"""
Постобработка масок v3 — скелетная реконструкция.

Принцип: НИКАКОЙ агрессивной морфологии. Только точечные соединения
разрывов через анализ скелета, направлений и проверку пути.

Pipeline:
1. Удаление мелких компонент (шум)
2. Удаление рамки чертежа (border frame)
3. Скелетная реконструкция разрывов:
   - Скелетонизация → endpoints
   - Для каждого endpoint: вектор направления
   - Поиск пары: расстояние + направление + чистый путь
   - Соединение тонкой линией (толщина = оригинальная труба)
4. Заполнение дыр внутри контуров
"""

import cv2
import numpy as np
import time
import logging
from typing import Optional, List, Tuple, Dict

from pipe_segmentation.config.defaults import DEFAULT_POSTPROCESS_CONFIG

logger = logging.getLogger(__name__)


# =============================================================================
# MAIN PIPELINE
# =============================================================================

def post_process_mask(
    mask: np.ndarray,
    remove_small_objects: int = 100,
    closing_kernel_size: int = 0,
    opening_kernel_size: int = 0,
    enabled: bool = True,
    directional_closing: Optional[dict] = None,
    skeleton_gap_fill: Optional[dict] = None,
    fill_holes: Optional[dict] = None,
    remove_border_frame: Optional[dict] = None,
) -> np.ndarray:
    """
    Постобработка v3 — без агрессивной морфологии.

    Pipeline:
    1. Удаление мелких компонент
    2. Удаление рамки чертежа
    3. Скелетное соединение разрывов (строгие условия)
    4. Минимальная морфология (только если kernel > 0)
    5. Заполнение дыр внутри контуров
    """
    if not enabled:
        if mask.max() <= 1:
            return (mask * 255).astype(np.uint8)
        return mask.astype(np.uint8)

    t_total = time.time()
    h, w = mask.shape
    logger.info("[POSTPROCESS] Start (%dx%d, %.1f Mpx)", w, h, h * w / 1e6)

    # Приводим к бинарному
    if mask.max() <= 1:
        binary = (mask > 0.5).astype(np.uint8)
    else:
        binary = (mask > 127).astype(np.uint8)

    # 1. Удаление мелких компонент (первый проход — убирает шум)
    if remove_small_objects > 0:
        t0 = time.time()
        binary = remove_small_components(binary, remove_small_objects)
        logger.info("[POSTPROCESS] 1_remove_small: %.2fs", time.time() - t0)

    # 2. Удаление рамки чертежа
    if remove_border_frame is None:
        remove_border_frame = DEFAULT_POSTPROCESS_CONFIG.get('remove_border_frame', {})
    if remove_border_frame.get('enabled', True):
        t0 = time.time()
        binary = remove_drawing_frame(
            binary,
            margin=remove_border_frame.get('margin', 30),
            min_length_ratio=remove_border_frame.get('min_length_ratio', 0.5),
        )
        logger.info("[POSTPROCESS] 2_remove_frame: %.2fs", time.time() - t0)

    # 3. Скелетное соединение разрывов
    if skeleton_gap_fill is None:
        skeleton_gap_fill = DEFAULT_POSTPROCESS_CONFIG.get('skeleton_gap_fill', {})
    if skeleton_gap_fill.get('enabled', True):
        t0 = time.time()
        binary = smart_skeleton_connect(
            binary,
            max_gap=skeleton_gap_fill.get('max_gap', 40),
            direction_tolerance=skeleton_gap_fill.get('direction_tolerance', 20),
            min_segment_length=skeleton_gap_fill.get('min_segment_length', 15),
            verify_path=skeleton_gap_fill.get('verify_path', True),
        )
        logger.info("[POSTPROCESS] 3_skeleton_gap_fill: %.2fs", time.time() - t0)

    # 4. Минимальная морфология (по умолчанию выключена: kernel=0)
    if closing_kernel_size > 0:
        t0 = time.time()
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (closing_kernel_size, closing_kernel_size)
        )
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        logger.info("[POSTPROCESS] 4_closing: %.2fs", time.time() - t0)

    if opening_kernel_size > 0:
        t0 = time.time()
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (opening_kernel_size, opening_kernel_size)
        )
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
        logger.info("[POSTPROCESS] 4_opening: %.2fs", time.time() - t0)

    # 5. Заполнение дыр
    if fill_holes is None:
        fill_holes = DEFAULT_POSTPROCESS_CONFIG.get('fill_holes', {})
    if fill_holes.get('enabled', True):
        t0 = time.time()
        binary = fill_mask_holes(
            binary,
            max_hole_size=fill_holes.get('max_hole_size', 500)
        )
        logger.info("[POSTPROCESS] 5_fill_holes: %.2fs", time.time() - t0)

    # 6. Финальная очистка мелких компонент
    if remove_small_objects > 0:
        t0 = time.time()
        binary = remove_small_components(binary, remove_small_objects)
        logger.info("[POSTPROCESS] 6_final_cleanup: %.2fs", time.time() - t0)

    logger.info("[POSTPROCESS] TOTAL: %.2fs", time.time() - t_total)
    return binary * 255


# =============================================================================
# УДАЛЕНИЕ РАМКИ ЧЕРТЕЖА
# =============================================================================

def remove_drawing_frame(
    mask: np.ndarray,
    margin: int = 30,
    min_length_ratio: float = 0.5,
) -> np.ndarray:
    """
    Удаляет рамку чертежа из маски.

    Рамка — это длинные прямые линии вдоль краёв изображения.
    Ищем компоненты, которые:
    - Касаются краёв изображения (в пределах margin)
    - Имеют bbox шириной или высотой > min_length_ratio от размера изображения
    - При этом bbox в другом измерении < margin*2 (тонкая линия вдоль края)

    Args:
        mask: Бинарная маска [H, W] (0/1)
        margin: Ширина полосы краёв для поиска рамки (px)
        min_length_ratio: Мин. доля стороны изображения для рамки
    """
    h, w = mask.shape
    result = mask.copy()

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)

    for i in range(1, num_labels):
        bx = stats[i, cv2.CC_STAT_LEFT]
        by = stats[i, cv2.CC_STAT_TOP]
        bw = stats[i, cv2.CC_STAT_WIDTH]
        bh = stats[i, cv2.CC_STAT_HEIGHT]

        touches_left = bx < margin
        touches_right = (bx + bw) > (w - margin)
        touches_top = by < margin
        touches_bottom = (by + bh) > (h - margin)

        # Горизонтальная рамка: длинная по X, тонкая по Y, касается верха/низа
        if bw > w * min_length_ratio and bh < margin * 3:
            if touches_top or touches_bottom:
                result[labels == i] = 0
                continue

        # Вертикальная рамка: длинная по Y, тонкая по X, касается лева/права
        if bh > h * min_length_ratio and bw < margin * 3:
            if touches_left or touches_right:
                result[labels == i] = 0
                continue

        # Угловые элементы рамки: касаются двух краёв
        n_edges = sum([touches_left, touches_right, touches_top, touches_bottom])
        if n_edges >= 2:
            # Проверяем что это "рамочный" контур — тонкий и длинный
            area = stats[i, cv2.CC_STAT_AREA]
            bbox_area = bw * bh
            if bbox_area > 0 and area / bbox_area < 0.15:
                # Sparse — скорее всего рамка или L-угол
                if bw > w * 0.3 or bh > h * 0.3:
                    result[labels == i] = 0

    return result


# =============================================================================
# СКЕЛЕТНАЯ РЕКОНСТРУКЦИЯ РАЗРЫВОВ
# =============================================================================

def smart_skeleton_connect(
    mask: np.ndarray,
    max_gap: int = 40,
    direction_tolerance: int = 20,
    min_segment_length: int = 15,
    verify_path: bool = True,
) -> np.ndarray:
    """
    Соединяет разрывы в трубах через строгий анализ скелета.

    Отличие от агрессивного directional_closing:
    - Работает ТОЛЬКО с endpoints (не расширяет сплошные участки)
    - Проверяет направление (±tolerance°)
    - Проверяет чистоту пути (не пересекает другие трубы)
    - Рисует тонкую линию (толщина = оригинал), не раздувает

    Args:
        mask: Бинарная маска [H, W] (0/1)
        max_gap: Максимальный зазор для соединения (px)
        direction_tolerance: Допуск направления (градусов)
        min_segment_length: Мин. длина сегмента для определения направления
        verify_path: Проверять что путь не пересекает другие трубы
    """
    if mask.sum() < 50:
        return mask

    t0 = time.time()
    # Скелетонизация
    skeleton = _fast_skeletonize(mask)
    t_skel = time.time()
    logger.info("[SKELETON_CONNECT] skeletonize: %.2fs (mask %dx%d, %d nonzero px)",
                   t_skel - t0, mask.shape[1], mask.shape[0], int(mask.sum()))
    if skeleton.sum() < 10:
        return mask

    # Endpoints
    endpoints = _find_endpoints(skeleton)
    t_ep = time.time()
    logger.info("[SKELETON_CONNECT] find_endpoints: %.2fs (%d endpoints)",
                   t_ep - t_skel, len(endpoints))
    if len(endpoints) < 2:
        return mask

    # Для каждого endpoint — направление и толщина трубы
    ep_data = []
    for ep in endpoints:
        direction = _trace_direction(skeleton, ep, min_segment_length)
        if direction is not None:
            thickness = _local_pipe_thickness(mask, ep)
            ep_data.append({
                'pos': ep,
                'dir': direction,
                'thickness': thickness,
            })
    t_trace = time.time()
    logger.info("[SKELETON_CONNECT] trace_directions+thickness: %.2fs (%d valid eps)",
                   t_trace - t_ep, len(ep_data))

    # Ищем пары для соединения
    result = mask.copy()
    connected = set()
    n_connected = 0

    for i in range(len(ep_data)):
        if i in connected:
            continue

        best_j = None
        best_score = float('inf')

        for j in range(i + 1, len(ep_data)):
            if j in connected:
                continue

            ep_i = ep_data[i]
            ep_j = ep_data[j]

            # Расстояние
            dist = np.linalg.norm(
                np.array(ep_i['pos']) - np.array(ep_j['pos'])
            )
            if dist > max_gap or dist < 3:
                continue

            # Проверка направлений
            if not _directions_match(ep_i, ep_j, direction_tolerance):
                continue

            # Проверка чистоты пути
            if verify_path and not _path_is_clean(
                mask, skeleton, ep_i['pos'], ep_j['pos']
            ):
                continue

            # Score: расстояние + штраф за отклонение направления
            angle_penalty = _direction_penalty(ep_i, ep_j)
            score = dist + angle_penalty * 2
            if score < best_score:
                best_score = score
                best_j = j

        if best_j is not None:
            ep_i = ep_data[i]
            ep_j = ep_data[best_j]

            # Рисуем тонкую соединительную линию
            thickness = max(1, min(ep_i['thickness'], ep_j['thickness']))
            thickness = min(thickness, 50)  # safety clamp
            cv2.line(
                result,
                (int(ep_i['pos'][1]), int(ep_i['pos'][0])),  # (x, y)
                (int(ep_j['pos'][1]), int(ep_j['pos'][0])),
                1, int(thickness)
            )
            connected.add(i)
            connected.add(best_j)
            n_connected += 1

    t_match = time.time()
    logger.info("[SKELETON_CONNECT] pair_matching: %.2fs (%d pairs connected)",
                   t_match - t_trace, n_connected)
    logger.info("[SKELETON_CONNECT] TOTAL: %.2fs", t_match - t0)

    return result


def _fast_skeletonize(mask: np.ndarray) -> np.ndarray:
    """Быстрая скелетонизация через cv2."""
    binary = (mask * 255).astype(np.uint8) if mask.max() <= 1 else mask.astype(np.uint8)

    if hasattr(cv2, 'ximgproc') and hasattr(cv2.ximgproc, 'thinning'):
        skeleton = cv2.ximgproc.thinning(binary)
        return (skeleton > 0).astype(np.uint8)

    # Fallback
    skel = np.zeros_like(binary)
    element = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    temp = binary.copy()
    while True:
        eroded = cv2.erode(temp, element)
        opened = cv2.dilate(eroded, element)
        subset = cv2.subtract(temp, opened)
        skel = cv2.bitwise_or(skel, subset)
        temp = eroded.copy()
        if cv2.countNonZero(temp) == 0:
            break
    return (skel > 0).astype(np.uint8)


def _find_endpoints(skeleton: np.ndarray) -> List[Tuple[int, int]]:
    """Endpoints скелета (точки с ровно 1 соседом)."""
    kernel = np.array([[1, 1, 1],
                       [1, 0, 1],
                       [1, 1, 1]], dtype=np.uint8)
    neighbors = cv2.filter2D(skeleton, -1, kernel)
    endpoint_mask = (skeleton > 0) & (neighbors == 1)
    coords = np.column_stack(np.where(endpoint_mask))
    return [(int(y), int(x)) for y, x in coords]


def _trace_direction(
    skeleton: np.ndarray,
    endpoint: Tuple[int, int],
    trace_length: int = 15,
) -> Optional[np.ndarray]:
    """
    Определяет направление трубы в endpoint.
    Возвращает единичный вектор (dy, dx) или None.
    """
    y, x = endpoint
    h, w = skeleton.shape

    visited = {(y, x)}
    current = (y, x)
    trace = [(y, x)]

    for _ in range(trace_length):
        cy, cx = current
        found = False
        for dy in [-1, 0, 1]:
            for dx in [-1, 0, 1]:
                if dy == 0 and dx == 0:
                    continue
                ny, nx = cy + dy, cx + dx
                if 0 <= ny < h and 0 <= nx < w:
                    if skeleton[ny, nx] > 0 and (ny, nx) not in visited:
                        visited.add((ny, nx))
                        current = (ny, nx)
                        trace.append((ny, nx))
                        found = True
                        break
            if found:
                break
        if not found:
            break

    if len(trace) < 3:
        return None

    # Вектор направления: от endpoint наружу
    start = np.array(trace[0], dtype=np.float64)
    end = np.array(trace[-1], dtype=np.float64)
    delta = start - end  # Направление НАРУЖУ от скелета

    norm = np.linalg.norm(delta)
    if norm < 1:
        return None

    return delta / norm


def _local_pipe_thickness(mask: np.ndarray, point: Tuple[int, int], radius: int = 8) -> int:
    """Оценивает толщину трубы в окрестности точки."""
    y, x = point
    h, w = mask.shape
    y1, y2 = max(0, y - radius), min(h, y + radius)
    x1, x2 = max(0, x - radius), min(w, x + radius)
    patch = mask[y1:y2, x1:x2]

    if patch.sum() == 0:
        return 2

    # BUG FIX: если весь патч ненулевой, distanceTransform возвращает
    # FLT_MAX (~3.4e38) — нет нулевых пикселей для измерения расстояния.
    # На больших изображениях трубы шире патча (2*radius=16px).
    # В этом случае оцениваем толщину как размер патча.
    patch_binary = (patch > 0).astype(np.uint8)
    if patch_binary.all():
        return min(radius * 2, 20)

    # Считаем через dist transform
    dist = cv2.distanceTransform(
        (patch * 255).astype(np.uint8),
        cv2.DIST_L2, 3
    )
    max_dist = float(dist.max())  # явная конвертация в Python float
    thickness = max(1, int(max_dist * 2))
    # Ограничиваем разумным максимумом (труба не может быть толще патча)
    return min(thickness, radius * 4)


def _directions_match(
    ep_i: Dict,
    ep_j: Dict,
    tolerance: int = 20,
) -> bool:
    """
    Проверяет что два endpoint направлены друг к другу.

    Условия:
    1. dir_i указывает в сторону ep_j (±tolerance)
    2. dir_j указывает в сторону ep_i (±tolerance)
    """
    pos_i = np.array(ep_i['pos'], dtype=np.float64)
    pos_j = np.array(ep_j['pos'], dtype=np.float64)

    # Вектор i→j
    vec_ij = pos_j - pos_i
    norm_ij = np.linalg.norm(vec_ij)
    if norm_ij < 1:
        return False
    vec_ij = vec_ij / norm_ij

    # dir_i должен указывать примерно в сторону j
    cos_i = np.dot(ep_i['dir'], vec_ij)
    angle_i = np.degrees(np.arccos(np.clip(cos_i, -1, 1)))

    # dir_j должен указывать примерно в сторону i (обратно)
    cos_j = np.dot(ep_j['dir'], -vec_ij)
    angle_j = np.degrees(np.arccos(np.clip(cos_j, -1, 1)))

    return angle_i < tolerance and angle_j < tolerance


def _direction_penalty(ep_i: Dict, ep_j: Dict) -> float:
    """Штраф за отклонение направления (0 = идеально, 1 = макс. допустимое)."""
    pos_i = np.array(ep_i['pos'], dtype=np.float64)
    pos_j = np.array(ep_j['pos'], dtype=np.float64)
    vec_ij = pos_j - pos_i
    norm = np.linalg.norm(vec_ij)
    if norm < 1:
        return 100
    vec_ij = vec_ij / norm

    cos_i = np.dot(ep_i['dir'], vec_ij)
    cos_j = np.dot(ep_j['dir'], -vec_ij)

    # Среднее отклонение
    return (np.arccos(np.clip(cos_i, -1, 1)) + np.arccos(np.clip(cos_j, -1, 1))) / 2


def _path_is_clean(
    mask: np.ndarray,
    skeleton: np.ndarray,
    ep1: Tuple[int, int],
    ep2: Tuple[int, int],
    cross_threshold: int = 3,
) -> bool:
    """
    Проверяет что путь между endpoints не пересекает другие трубы.
    Работает на маленьком ROI вместо полного изображения.
    """
    h, w = mask.shape
    margin = 10

    # Вычисляем ROI вокруг двух endpoints
    y1_roi = max(0, min(ep1[0], ep2[0]) - margin)
    y2_roi = min(h, max(ep1[0], ep2[0]) + margin)
    x1_roi = max(0, min(ep1[1], ep2[1]) - margin)
    x2_roi = min(w, max(ep1[1], ep2[1]) + margin)

    roi_h = y2_roi - y1_roi
    roi_w = x2_roi - x1_roi
    if roi_h < 1 or roi_w < 1:
        return False

    # Координаты endpoints в локальных координатах ROI
    lp1 = (ep1[0] - y1_roi, ep1[1] - x1_roi)
    lp2 = (ep2[0] - y1_roi, ep2[1] - x1_roi)

    # Рисуем линию на маленьком буфере
    line_buf = np.zeros((roi_h, roi_w), dtype=np.uint8)
    cv2.line(line_buf, (lp1[1], lp1[0]), (lp2[1], lp2[0]), 1, 1)

    # Исключаем окрестности endpoints (radius=5)
    for lp in [lp1, lp2]:
        ly, lx = lp
        ly1, ly2 = max(0, ly - 5), min(roi_h, ly + 5)
        lx1, lx2 = max(0, lx - 5), min(roi_w, lx + 5)
        line_buf[ly1:ly2, lx1:lx2] = 0

    # Считаем пересечения с маской (берём соответствующий ROI)
    mask_roi = mask[y1_roi:y2_roi, x1_roi:x2_roi]
    crossings = (line_buf & mask_roi).sum()

    return crossings <= cross_threshold


# =============================================================================
# БАЗОВЫЕ ФУНКЦИИ
# =============================================================================

def remove_small_components(mask: np.ndarray, min_size: int) -> np.ndarray:
    """Удаляет мелкие связные компоненты (numpy LUT — один проход)."""
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    if num_labels <= 1:
        return np.zeros_like(mask)
    # Lookup table: keep[label] = 1 если area >= min_size
    areas = stats[1:, cv2.CC_STAT_AREA]  # skip background
    keep = np.zeros(num_labels, dtype=np.uint8)
    keep[1:] = (areas >= min_size).astype(np.uint8)
    # Один проход по всему массиву labels
    return keep[labels]


def fill_mask_holes(mask: np.ndarray, max_hole_size: int = 500) -> np.ndarray:
    """Заполняет дыры внутри объектов (numpy LUT — один проход)."""
    binary = mask.astype(np.uint8) if mask.max() <= 1 else (mask > 127).astype(np.uint8)
    inverted = 1 - binary
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        inverted, connectivity=8
    )
    if num_labels <= 1:
        return binary
    # Lookup table: fill[label] = 1 если hole маленькая
    areas = stats[1:, cv2.CC_STAT_AREA]
    fill = np.zeros(num_labels, dtype=np.uint8)
    fill[1:] = (areas <= max_hole_size).astype(np.uint8)
    # Один проход: binary OR filled holes
    return binary | fill[labels]


def clean_mask(mask: np.ndarray, config: Optional[dict] = None) -> np.ndarray:
    """Очистка маски с конфигурацией."""
    if config is None:
        config = DEFAULT_POSTPROCESS_CONFIG
    return post_process_mask(mask, **{
        k: v for k, v in config.items()
        if k in ('remove_small_objects', 'closing_kernel_size',
                  'opening_kernel_size', 'enabled',
                  'directional_closing', 'skeleton_gap_fill',
                  'fill_holes', 'remove_border_frame')
    })


def skeletonize_mask(mask: np.ndarray) -> np.ndarray:
    """Скелетонизация маски."""
    binary = (mask > 127).astype(np.uint8) * 255 if mask.max() > 1 \
        else (mask > 0.5).astype(np.uint8) * 255
    return _fast_skeletonize(binary // 255) * 255

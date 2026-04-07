"""
Mask Generation - Преобразование скелета в маску

Корректная обработка шпор (артефактов скелетонизации) и сглаживание.
"""

import cv2
import numpy as np


def find_skeleton_endpoints(skeleton):
    """Найти endpoints скелета"""
    kernel = np.array([[1, 1, 1],
                       [1, 10, 1],
                       [1, 1, 1]], dtype=np.uint8)
    neighbors = cv2.filter2D((skeleton > 0).astype(np.uint8), -1, kernel)
    endpoints_mask = (skeleton > 0) & (neighbors == 11)
    return np.column_stack(np.where(endpoints_mask)).tolist()


def trace_to_junction(skeleton, endpoint, max_length):
    """
    Trace от endpoint до junction или max_length.

    Returns:
        path: список точек пути
        stop_type: 'junction' | 'dead_end' | 'max_length'
    """
    h, w = skeleton.shape
    visited = set()
    path = [tuple(endpoint)]
    visited.add(tuple(endpoint))
    current = tuple(endpoint)

    for _ in range(max_length):
        neighbors = []
        for dy in [-1, 0, 1]:
            for dx in [-1, 0, 1]:
                if dy == 0 and dx == 0:
                    continue
                ny, nx = current[0] + dy, current[1] + dx
                if 0 <= ny < h and 0 <= nx < w:
                    if skeleton[ny, nx] > 0 and (ny, nx) not in visited:
                        neighbors.append((ny, nx))

        if len(neighbors) == 0:
            return path, 'dead_end'
        elif len(neighbors) == 1:
            current = neighbors[0]
            path.append(current)
            visited.add(current)
        else:  # >= 2 соседей = junction
            return path, 'junction'

    return path, 'max_length'


def prune_spurs(skeleton, nodes_mask=None, max_spur_length=5,
                node_touch_distance=3, verbose=True):
    """
    Удалить шпоры (короткие ветки от endpoint до junction).

    Шпора = endpoint который:
    - НЕ касается узла (в пределах node_touch_distance)
    - Trace до junction за ≤ max_spur_length пикселей

    Args:
        skeleton: бинарный скелет (0/255 или 0/1)
        nodes_mask: маска узлов (опционально, для защиты endpoints у узлов)
        max_spur_length: максимальная длина шпоры для удаления
        node_touch_distance: расстояние касания узла

    Returns:
        pruned_skeleton: скелет без шпор (255 = скелет)
        stats: словарь со статистикой
    """
    skel_binary = (skeleton > 127).astype(np.uint8) if skeleton.max() > 1 else skeleton.astype(np.uint8)
    pruned = skel_binary.copy()

    # Зона касания узлов
    if nodes_mask is not None:
        nodes_binary = (nodes_mask > 127).astype(np.uint8) if nodes_mask.max() > 1 else nodes_mask.astype(np.uint8)
        kernel_size = node_touch_distance * 2 + 1
        kernel_touch = np.ones((kernel_size, kernel_size), np.uint8)
        nodes_touch = cv2.dilate(nodes_binary, kernel_touch)
    else:
        nodes_touch = np.zeros_like(skel_binary)

    endpoints = find_skeleton_endpoints(skeleton)
    spurs_removed = 0
    pixels_removed = 0
    spur_lengths = []

    for ep in endpoints:
        # Пропускаем endpoints у узлов
        if nodes_touch[ep[0], ep[1]] > 0:
            continue

        path, stop_type = trace_to_junction(skel_binary, ep, max_spur_length + 1)

        # Шпора = короткий путь до junction
        if stop_type == 'junction' and len(path) <= max_spur_length:
            # Удаляем все точки пути КРОМЕ последней (junction)
            for y, x in path[:-1]:
                pruned[y, x] = 0
            spurs_removed += 1
            pixels_removed += len(path) - 1
            spur_lengths.append(len(path) - 1)

    if verbose:
        print(f"✂️ Pruning шпор (max_length={max_spur_length}):")
        print(f"   Удалено шпор: {spurs_removed}")
        print(f"   Удалено пикселей: {pixels_removed}")
        if spur_lengths:
            print(f"   Длины шпор: min={min(spur_lengths)}, max={max(spur_lengths)}, avg={np.mean(spur_lengths):.1f}")

    stats = {
        'spurs_removed': spurs_removed,
        'pixels_removed': pixels_removed,
        'spur_lengths': spur_lengths
    }

    return pruned * 255, stats


def _binarize_original(original_image):
    """
    Бинаризация оригинала: adaptive threshold.

    Параметры (blockSize=51, C=10) идентичны skeleton_extension/processing.py
    для единообразия. min(channels) сохраняет цветные линии.

    Args:
        original_image: оригинальное изображение (BGR или grayscale)

    Returns:
        binary: uint8 (255 = фон/белый, 0 = линии/чёрный)
    """
    if len(original_image.shape) == 3:
        gray = np.min(original_image, axis=2)
    else:
        gray = original_image

    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 51, 10
    )
    return binary


def _build_thickness_map(binary, min_thickness=2, max_thickness=40):
    """
    Карта толщины через distance transform на бинаризации оригинала.

    Работает везде — и внутри pipe_mask, и на extension-участках,
    потому что опирается на реальные чёрные линии чертежа.

    Args:
        binary: бинаризация оригинала (255=фон, 0=линии)
        min_thickness: минимальный диаметр (px)
        max_thickness: максимальный диаметр (px)

    Returns:
        thickness_map: float32 массив, каждый пиксель = диаметр трубы
    """
    # Инвертируем: линии=1, фон=0 (distanceTransform считает от 0)
    lines = ((binary == 0)).astype(np.uint8)

    dist = cv2.distanceTransform(lines, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)

    # radius → diameter, clamp
    diameter_map = np.clip(dist * 2, 0, max_thickness).astype(np.float32)

    return diameter_map


def _propagate_thickness(skeleton_binary, thickness_map, fallback_thickness):
    """
    Для пикселей скелета где thickness_map == 0 (вне реальных линий) —
    наследовать толщину от ближайшего покрытого пикселя скелета.

    Это нужно для extension-участков, которые проходят через белый фон
    между концом трубы и узлом.

    Args:
        skeleton_binary: бинарный скелет (uint8, 0/1)
        thickness_map: карта диаметров (float32)
        fallback_thickness: fallback если вообще нет покрытых пикселей

    Returns:
        per_pixel_thickness: float32 массив толщин для каждого пикселя скелета
    """
    skel_ys, skel_xs = np.where(skeleton_binary > 0)
    if len(skel_ys) == 0:
        return thickness_map

    result = thickness_map.copy()

    # Собираем толщины на скелете
    skel_thicknesses = thickness_map[skel_ys, skel_xs]
    covered_mask = skel_thicknesses > 0

    if not np.any(covered_mask):
        # Ни один пиксель скелета не попал на линию — fallback
        result[skel_ys, skel_xs] = fallback_thickness
        return result

    if np.all(covered_mask):
        # Все покрыты — ничего делать не надо
        return result

    # Координаты покрытых и непокрытых пикселей скелета
    covered_pts = np.column_stack((skel_ys[covered_mask], skel_xs[covered_mask]))
    covered_vals = skel_thicknesses[covered_mask]

    uncovered_idx = np.where(~covered_mask)[0]
    uncovered_pts = np.column_stack((skel_ys[uncovered_idx], skel_xs[uncovered_idx]))

    # Для каждого непокрытого — найти ближайший покрытый (через BallTree или brute)
    # Brute-force — скелет обычно <100k px, O(n*m) приемлемо
    # Оптимизация: используем scipy.spatial.cKDTree если доступен
    try:
        from scipy.spatial import cKDTree
        tree = cKDTree(covered_pts)
        _, nearest_idx = tree.query(uncovered_pts)
        nearest_thicknesses = covered_vals[nearest_idx]
    except ImportError:
        # Fallback без scipy — медиана покрытых
        nearest_thicknesses = np.full(len(uncovered_idx), np.median(covered_vals))

    for i, idx in enumerate(uncovered_idx):
        result[skel_ys[idx], skel_xs[idx]] = nearest_thicknesses[i]

    return result


def _adaptive_dilate(skeleton_binary, thickness_map, min_thickness=2):
    """
    Адаптивное утолщение: группирует пиксели скелета по толщине,
    делает dilate для каждой группы. Значительно быстрее поточечной отрисовки.

    Args:
        skeleton_binary: бинарный скелет (uint8, 0/1)
        thickness_map: карта диаметров (float32)
        min_thickness: минимальный диаметр

    Returns:
        mask: бинарная маска (uint8, 0/1)
        stats: dict
    """
    h, w = skeleton_binary.shape
    mask = np.zeros((h, w), dtype=np.uint8)

    skel_ys, skel_xs = np.where(skeleton_binary > 0)
    if len(skel_ys) == 0:
        return mask, {'method': 'adaptive', 'groups': 0}

    # Квантизация толщин до целых
    diameters = thickness_map[skel_ys, skel_xs]
    diameters_int = np.clip(np.round(diameters).astype(int), min_thickness, None)
    # Нечётные диаметры для симметричного ядра
    diameters_int = diameters_int | 1  # make odd

    unique_diams = np.unique(diameters_int)

    for d in unique_diams:
        # Маска пикселей скелета с этим диаметром
        group_mask = (diameters_int == d)
        group_skel = np.zeros((h, w), dtype=np.uint8)
        group_skel[skel_ys[group_mask], skel_xs[group_mask]] = 1

        # Dilate этой группы
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (d, d))
        dilated = cv2.dilate(group_skel, kernel)
        mask = np.maximum(mask, dilated)

    stats = {
        'method': 'adaptive',
        'groups': len(unique_diams),
        'thickness_range': (int(unique_diams.min()), int(unique_diams.max())),
        'thickness_median': int(np.median(diameters_int)),
    }

    return mask, stats


def skeleton_to_mask(skeleton, nodes_mask=None, thickness=12,
                     prune_spurs_length=5, smooth_size=5, verbose=True,
                     original_image=None, pipe_mask=None,
                     adaptive=True, min_thickness=2, max_thickness=40):
    """
    Преобразовать скелет в маску с корректной обработкой шпор.

    Этапы:
    1. Удаление шпор (pruning) - короткие артефакты скелетонизации
    2. Утолщение — адаптивное (из реальной ширины труб на чертеже)
       или фиксированное (fallback)
    3. Clip — маска не выходит за контуры реальных труб
    4. Сглаживание (closing) - удаление мелких неровностей

    Args:
        skeleton: бинарный скелет (255 = скелет)
        nodes_mask: маска узлов (для защиты endpoints у узлов)
        thickness: толщина маски в пикселях — диаметр (для фиксированного режима)
        prune_spurs_length: максимальная длина шпор для удаления (0 = не удалять)
        smooth_size: размер ядра для closing-сглаживания (0 = не сглаживать)
        verbose: выводить статистику
        original_image: оригинальное изображение (BGR или grayscale) — для adaptive
        pipe_mask: маска труб из сегментации (0/255) — для clip
        adaptive: использовать адаптивную толщину
        min_thickness: минимальный диаметр (px) для adaptive
        max_thickness: максимальный диаметр (px) для adaptive

    Returns:
        mask: финальная маска (255 = маска)
        stats: словарь со статистикой
    """
    use_adaptive = adaptive and original_image is not None

    if verbose:
        mode = "adaptive" if use_adaptive else f"fixed ({thickness}px)"
        print(f"\n🎨 Skeleton → Mask [{mode}]")
        print(f"   Параметры: prune={prune_spurs_length}, smooth={smooth_size}")
        if use_adaptive:
            print(f"   Adaptive: min={min_thickness}, max={max_thickness}")

    skel_binary = (skeleton > 127).astype(np.uint8)
    stats = {'original_pixels': int(np.sum(skel_binary))}

    # 1. Удаление шпор
    if prune_spurs_length > 0:
        pruned, prune_stats = prune_spurs(
            skel_binary * 255, nodes_mask,
            max_spur_length=prune_spurs_length,
            verbose=verbose
        )
        pruned_binary = (pruned > 127).astype(np.uint8)
        stats['pruning'] = prune_stats
    else:
        pruned_binary = skel_binary
        stats['pruning'] = None

    stats['after_prune_pixels'] = int(np.sum(pruned_binary))

    # 2. Утолщение
    if use_adaptive:
        # 2a. Бинаризация оригинала → карта толщин
        binary = _binarize_original(original_image)
        thickness_map = _build_thickness_map(binary, min_thickness, max_thickness)

        # 2b. Propagate толщину на extension-участки (вне линий чертежа)
        thickness_map = _propagate_thickness(
            pruned_binary, thickness_map, fallback_thickness=thickness
        )

        # 2c. Адаптивный dilate
        dilated, adaptive_stats = _adaptive_dilate(
            pruned_binary, thickness_map, min_thickness
        )
        stats['adaptive'] = adaptive_stats

        if verbose:
            print(f"   Adaptive dilate: {adaptive_stats['groups']} groups, "
                  f"range={adaptive_stats.get('thickness_range', '?')}, "
                  f"median={adaptive_stats.get('thickness_median', '?')}px")

        # 2d. Clip: маска не вылезает за контуры реальных труб
        #     Разрешённая зона = pipe_mask ∪ реальные линии чертежа
        lines_binary = (binary == 0).astype(np.uint8)
        # Небольшой dilate линий чтобы компенсировать погрешность бинаризации
        clip_expand = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        lines_expanded = cv2.dilate(lines_binary, clip_expand)

        clip_zone = lines_expanded
        if pipe_mask is not None:
            pm_binary = (pipe_mask > 127).astype(np.uint8) if pipe_mask.max() > 1 else pipe_mask.astype(np.uint8)
            clip_zone = np.maximum(clip_zone, pm_binary)

        before_clip = int(np.sum(dilated))
        dilated = dilated & clip_zone
        after_clip = int(np.sum(dilated))
        stats['clip_removed_pixels'] = before_clip - after_clip

        if verbose and stats['clip_removed_pixels'] > 0:
            print(f"   Clip: removed {stats['clip_removed_pixels']} px outside pipe boundaries")
    else:
        # Фиксированный dilate (старое поведение)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (thickness, thickness))
        dilated = cv2.dilate(pruned_binary, kernel)

    stats['after_dilate_pixels'] = int(np.sum(dilated))

    if verbose and not use_adaptive:
        print(f"   Dilate ({thickness}px): {stats['after_prune_pixels']} → {stats['after_dilate_pixels']} px")

    # 3. Сглаживание (morphological closing)
    if smooth_size > 0:
        smooth_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (smooth_size, smooth_size))
        mask = cv2.morphologyEx(dilated, cv2.MORPH_CLOSE, smooth_kernel)
        stats['after_smooth_pixels'] = int(np.sum(mask))

        if verbose:
            print(f"   Closing ({smooth_size}px): {stats['after_dilate_pixels']} → {stats['after_smooth_pixels']} px")
    else:
        mask = dilated
        stats['after_smooth_pixels'] = stats['after_dilate_pixels']

    return mask * 255, stats


# === CLI ===
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Skeleton to Mask conversion')
    parser.add_argument('skeleton', help='Path to skeleton image')
    parser.add_argument('output', help='Path to output mask')
    parser.add_argument('--nodes', help='Path to nodes mask (optional)')
    parser.add_argument('--original', help='Path to original image (for adaptive thickness)')
    parser.add_argument('--pipe-mask', help='Path to pipe mask (for clip)')
    parser.add_argument('--thickness', type=int, default=4, help='Fallback thickness (default: 4)')
    parser.add_argument('--no-adaptive', action='store_true', help='Disable adaptive thickness')
    parser.add_argument('--min-thickness', type=int, default=2, help='Min adaptive thickness (default: 2)')
    parser.add_argument('--max-thickness', type=int, default=40, help='Max adaptive thickness (default: 40)')
    parser.add_argument('--prune', type=int, default=5, help='Max spur length to prune (default: 5, 0=disable)')
    parser.add_argument('--smooth', type=int, default=5, help='Smoothing kernel size (default: 5, 0=disable)')

    args = parser.parse_args()

    skeleton = cv2.imread(args.skeleton, cv2.IMREAD_GRAYSCALE)
    nodes = cv2.imread(args.nodes, cv2.IMREAD_GRAYSCALE) if args.nodes else None
    original = cv2.imread(args.original) if args.original else None
    pm = cv2.imread(args.pipe_mask, cv2.IMREAD_GRAYSCALE) if args.pipe_mask else None

    mask, stats = skeleton_to_mask(
        skeleton, nodes,
        thickness=args.thickness,
        prune_spurs_length=args.prune,
        smooth_size=args.smooth,
        original_image=original,
        pipe_mask=pm,
        adaptive=not args.no_adaptive,
        min_thickness=args.min_thickness,
        max_thickness=args.max_thickness,
    )

    cv2.imwrite(args.output, mask)
    print(f"\n💾 Маска сохранена: {args.output}")

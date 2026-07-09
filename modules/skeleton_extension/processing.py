"""
Skeleton Extension - Processing

Функция обработки одного изображения из hybrid_pipe_reconstruction_optimized.py
"""

import cv2
import numpy as np
from skimage.morphology import skeletonize
import os
import time

from .core import (
    find_skeleton_endpoints,
    remove_skeleton_under_nodes,
    remove_skeleton_under_nodes_simple,
    remove_skeleton_around_nodes,
    create_endpoint_protection_mask,
    create_simple_protection_mask,
    connect_with_directed_lines,
    create_final_protection_mask,
    bfs_connect_endpoints,
    diagnose_unconnected_endpoints,
    remove_orphan_components,
    trim_orphan_components,
)
from .visualization import (
    visualize_protection_mask,
    visualize_all_lines,
    visualize_bfs_paths,
    visualize_skeleton_contacts,
)


# --- Наблюдаемость (§9 #14: под-под-шаги COMPUTE skeleton_extension) -----------
# processing.py исполняется в worker'е (app на PYTHONPATH) и standalone (CLI).
# Слой obs импортируется опционально: в standalone → no-op, скелетизация не
# ломается (зеркалит engine.py/builder.py, §8.9). Задачный step=compute
# (worker/tasks/skeleton.py) остаётся; здесь — под-под-шаги внутри него.
try:
    from app.core.logging import get_logger
    from app.core.obs import step as _obs_step
    logger = get_logger(__name__)
except Exception:  # standalone: app не на PYTHONPATH
    import logging as _logging
    from contextlib import contextmanager

    logger = _logging.getLogger(__name__)

    @contextmanager
    def _obs_step(_name, _logger, **_fields):
        yield


def process_single_image(original_path, prediction_path, nodes_path, output_path, skeleton_output_path, config):
    """Обработать одно изображение"""
    try:
        # Флаг отладки
        DEBUG = config.get('debug', False)

        print(f"\n{'=' * 70}")
        print(f"📄 Файл: {os.path.basename(original_path)}")
        print(f"{'=' * 70}")
        _t_total = time.time()

        original = cv2.imread(original_path)
        prediction = cv2.imread(prediction_path)
        nodes = cv2.imread(nodes_path)

        if original is None or prediction is None or nodes is None:
            print(f"   ❌ Ошибка загрузки")
            return False

        # Конвертация в grayscale если цветные
        if len(original.shape) == 3:
            # Минимум по каналам - сохраняет ВСЕ цветные линии (зелёные, синие и т.д.)
            # Зелёная линия: R=0, G=255, B=0 → min=0 (чёрный) ✓
            # Белый фон: R=255, G=255, B=255 → min=255 (белый) ✓
            original = np.min(original, axis=2)
        if len(prediction.shape) == 3:
            prediction = cv2.cvtColor(prediction, cv2.COLOR_BGR2GRAY)
        if len(nodes.shape) == 3:
            nodes = cv2.cvtColor(nodes, cv2.COLOR_BGR2GRAY)

        # Бинаризация original: адаптивная (лучше для тонких линий чем Otsu)
        original = cv2.adaptiveThreshold(
            original, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 51, 10
        )

        # Извлекаем параметры из конфига
        NODE_BOUNDARY_EXPANSION = config.get('node_boundary_expansion', 1)
        TRIM_LENGTH = config.get('trim_length', 10)
        TRIM_PROTECTION = config.get('trim_protection', 15)
        MASK_WIDTH = config.get('mask_width', 8)
        BFS_MAX_DEPTH = config.get('bfs_max_depth', 1000)
        BFS_ITERATIONS = config.get('bfs_iterations', 1)
        BFS_MASK_TOLERANCE = config.get('bfs_mask_tolerance', 5)
        DIRECTION_TRACE_LENGTH = config.get('direction_trace_length', 5)
        MAX_LINE_LENGTH = config.get('max_line_length', 600)
        ENDPOINT_SEARCH_RADIUS = config.get('endpoint_search_radius', 5)
        SKELETON_SEARCH_RADIUS = config.get('skeleton_search_radius', 5)

        if DEBUG:
            print("=" * 70)
            print("🚀 SKELETON EXTENSION")
            print("=" * 70)
            print("📋 Параметры:")
            print(f"   DIRECTION_TRACE_LENGTH = {DIRECTION_TRACE_LENGTH}")
            print(f"   MAX_LINE_LENGTH = {MAX_LINE_LENGTH}")
            print(f"   ENDPOINT_SEARCH_RADIUS = {ENDPOINT_SEARCH_RADIUS}")
            print(f"   SKELETON_SEARCH_RADIUS = {SKELETON_SEARCH_RADIUS}")
            print(f"   BFS_MAX_DEPTH = {BFS_MAX_DEPTH}")
            print(f"   BFS_ITERATIONS = {BFS_ITERATIONS}")
            print(f"   BFS_MASK_TOLERANCE = {BFS_MASK_TOLERANCE}")
            print("=" * 70)
            print("📌 Этапы:")
            print("   ЭТАП 1: Удалить скелет ПОД узлами")
            print("   ЭТАП 2: Удалить скелет в 1px ВОКРУГ узлов")
            print("   ЭТАП 3: Защитная маска (15px→8px) + очистка 4px у endpoints")
            print("   ЭТАП 4: Направленные линии (node > endpoint > skeleton)")
            print("   ЭТАП 5: BFS поиск (node > endpoint > skeleton)")
            print("=" * 70)
            print()

        # Скелетонизация
        _t0 = time.time()
        with _obs_step("skeletonize", logger):
            if DEBUG:
                print("🦴 Скелетонизация prediction...")
            prediction_binary = (prediction > 127).astype(np.uint8)
            skeleton = skeletonize(prediction_binary).astype(np.uint8) * 255
        print(f"[SKEL_EXT] skeletonize: {time.time()-_t0:.2f}s")

        # Флаг простого режима
        SIMPLE_MODE = config.get('simple_mode', False)
        EXTEND_RADIUS = config.get('extend_radius', 5)

        if SIMPLE_MODE:
            # === SIMPLE MODE ===
            _t0 = time.time()
            if DEBUG:
                print("\n⚡ SIMPLE MODE: контур узлов сохраняется, endpoints продлеваются")
            skeleton_work, node_contour = remove_skeleton_under_nodes_simple(skeleton, nodes, verbose=DEBUG)
            if DEBUG:
                print("\n⏭️ ЭТАП 2 - Пропущен (simple mode)")
            skeleton_work, protection_mask = create_simple_protection_mask(
                skeleton_work,
                node_contour,
                original,
                mask_width=MASK_WIDTH,
                extend_radius=EXTEND_RADIUS,
                verbose=DEBUG
            )
            print(f"[SKEL_EXT] stages_1-3 (simple): {time.time()-_t0:.2f}s")
        else:
            # === NORMAL MODE ===
            _t0 = time.time()
            skeleton_stage1 = remove_skeleton_under_nodes(skeleton, nodes, verbose=DEBUG)
            skeleton_stage2 = remove_skeleton_around_nodes(skeleton_stage1, nodes, expansion=NODE_BOUNDARY_EXPANSION, verbose=DEBUG)
            skeleton_work, protection_mask = create_endpoint_protection_mask(
                skeleton_stage2,
                original,
                trim_work=TRIM_LENGTH,
                trim_protection=TRIM_PROTECTION,
                mask_width=MASK_WIDTH,
                verbose=DEBUG
            )
            print(f"[SKEL_EXT] stages_1-3 (normal): {time.time()-_t0:.2f}s")

        endpoints_work = find_skeleton_endpoints(skeleton_work)
        if DEBUG:
            print(f"   Endpoints рабочего скелета: {len(endpoints_work)}")

        # Визуализация защитной маски
        if DEBUG:
            viz_mask = visualize_protection_mask(original, skeleton_work, protection_mask, endpoints_work)
            mask_viz_path = output_path.replace('.png', '_stage3_protection_mask.png')
            cv2.imwrite(mask_viz_path, viz_mask)
            print(f"   💾 Визуализация маски: {os.path.basename(mask_viz_path)}")

        # ЭТАП 4: Направленные линии (ИСПРАВЛЕННЫЙ)
        _t0 = time.time()
        connections_mask, info, connected_eps, all_lines = connect_with_directed_lines(
            endpoints_work, skeleton_work, nodes, original, protection_mask, config, verbose=DEBUG
        )
        print(f"[SKEL_EXT] stage_4_directed_lines: {time.time()-_t0:.2f}s ({len(connected_eps)} connected)")

        # ========================================
        # НОВАЯ ВИЗУАЛИЗАЦИЯ ВСЕХ ЛИНИЙ
        # ========================================
        if DEBUG:
            viz_lines = visualize_all_lines(
                original, skeleton_work, nodes, protection_mask,
                endpoints_work, all_lines, connected_eps
            )
            lines_viz_path = output_path.replace('.png', '_stage4_all_lines_debug.png')
            cv2.imwrite(lines_viz_path, viz_lines)
            print(f"   💾 Визуализация линий: {os.path.basename(lines_viz_path)}")

        # Финальный скелет
        skeleton_final = skeleton_work.copy()
        skeleton_final[connections_mask > 0] = 255

        # ========================================
        # ФИНАЛЬНАЯ ЗАЩИТНАЯ МАСКА
        # ========================================
        _t0 = time.time()
        final_protection_mask = create_final_protection_mask(
            skeleton_final, original, endpoints_work, connected_eps,
            mask_width=MASK_WIDTH, clearance_radius=4, verbose=DEBUG
        )
        print(f"[SKEL_EXT] protection_mask_1: {time.time()-_t0:.2f}s")

        # Сохранить финальную защитную маску
        if DEBUG:
            final_mask_path = output_path.replace('.png', '_final_protection_mask.png')
            cv2.imwrite(final_mask_path, final_protection_mask)
            print(f"   💾 Финальная защитная маска: {os.path.basename(final_mask_path)}")

        # Визуализация финальной маски
        remaining_endpoints = [ep for idx, ep in enumerate(endpoints_work) if idx not in connected_eps]
        remaining_ep_indices = [idx for idx, ep in enumerate(endpoints_work) if idx not in connected_eps]

        if DEBUG:
            viz_final_mask = visualize_protection_mask(original, skeleton_final, final_protection_mask, remaining_endpoints)
            final_mask_viz_path = output_path.replace('.png', '_final_protection_mask_viz.png')
            cv2.imwrite(final_mask_viz_path, viz_final_mask)
            print(f"   💾 Визуализация финальной маски: {os.path.basename(final_mask_viz_path)}")

        # ========================================
        # ЭТАП 5: BFS ПОИСК СОЕДИНЕНИЙ
        # ========================================
        _t_bfs = time.time()
        with _obs_step("bfs", logger):
            if len(remaining_endpoints) > 0 and BFS_ITERATIONS > 0:
                # Пересоздаём endpoint_to_component и labels для BFS
                skeleton_binary_for_bfs = (skeleton_final > 127).astype(np.uint8)
                num_components, labels = cv2.connectedComponents(skeleton_binary_for_bfs)

                endpoint_to_component = {}
                for ep_idx, ep in enumerate(endpoints_work):
                    y, x = ep[0], ep[1]
                    # Endpoint может быть не на скелете после обработки, ищем ближайшую точку
                    if labels[y, x] > 0:
                        endpoint_to_component[ep_idx] = labels[y, x]
                    else:
                        # Ищем в окрестности 3x3
                        found = False
                        for dy in [-1, 0, 1]:
                            for dx in [-1, 0, 1]:
                                ny, nx = y + dy, x + dx
                                if 0 <= ny < labels.shape[0] and 0 <= nx < labels.shape[1]:
                                    if labels[ny, nx] > 0:
                                        endpoint_to_component[ep_idx] = labels[ny, nx]
                                        found = True
                                        break
                            if found:
                                break
                        if not found:
                            endpoint_to_component[ep_idx] = 0

                all_connected_eps = connected_eps.copy()

                for iteration in range(BFS_ITERATIONS):
                    if DEBUG:
                        print(f"\n{'=' * 50}")
                        print(f"BFS Итерация {iteration + 1}/{BFS_ITERATIONS}")
                        print(f"{'=' * 50}")

                    # Текущие оставшиеся endpoints
                    current_remaining = [ep for idx, ep in enumerate(endpoints_work) if idx not in all_connected_eps]
                    current_remaining_indices = [idx for idx, ep in enumerate(endpoints_work) if
                                                 idx not in all_connected_eps]

                    if len(current_remaining) == 0:
                        if DEBUG:
                            print("   Все endpoints соединены!")
                        break

                    bfs_mask, bfs_info, newly_connected, all_bfs_paths = bfs_connect_endpoints(
                        current_remaining, current_remaining_indices,
                        skeleton_final, nodes, original, final_protection_mask,
                        endpoint_to_component, endpoints_work,
                        all_connected_eps, labels, config, verbose=DEBUG
                    )

                    # Обновляем скелет
                    skeleton_final[bfs_mask > 0] = 255
                    all_connected_eps.update(newly_connected)

                    # Визуализация BFS
                    if DEBUG:
                        viz_bfs = visualize_bfs_paths(
                            original, skeleton_final, nodes, final_protection_mask,
                            all_bfs_paths, current_remaining, newly_connected
                        )
                        bfs_viz_path = output_path.replace('.png', f'_stage5_bfs_iter{iteration + 1}.png')
                        cv2.imwrite(bfs_viz_path, viz_bfs)
                        print(f"   💾 Визуализация BFS: {os.path.basename(bfs_viz_path)}")

                    if len(newly_connected) == 0:
                        if DEBUG:
                            print("   Нет новых соединений, останавливаем BFS")
                        break

                connected_eps = all_connected_eps

                # Финальная статистика
                final_remaining = len([idx for idx in range(len(endpoints_work)) if idx not in connected_eps])
                print(f"\n📊 Итого:")
                print(f"   Endpoints соединено: {len(connected_eps)}/{len(endpoints_work)}")
                print(f"   Endpoints осталось: {final_remaining}")
                print(f"[SKEL_EXT] stage_5_bfs: {time.time()-_t_bfs:.2f}s")

                # Диагностика несоединённых endpoints (только в debug)
                if DEBUG and final_remaining > 0:
                    unconnected_indices = [idx for idx in range(len(endpoints_work)) if idx not in connected_eps]
                    diagnose_unconnected_endpoints(
                        unconnected_indices, endpoints_work, skeleton_final, nodes, original,
                        final_protection_mask, endpoint_to_component, labels, config
                    )

        # ========================================
        # ЭТАП 6: ОБРАБОТКА ORPHAN КОМПОНЕНТ
        # ========================================
        _t0 = time.time()
        ORPHAN_TRIM_LENGTH = config.get('orphan_trim_length', 0)
        REMOVE_ORPHANS = config.get('remove_orphans', False)
        ORPHAN_TOUCH_DISTANCE = config.get('orphan_touch_distance', 3)

        if ORPHAN_TRIM_LENGTH > 0:
            if DEBUG:
                print(f"\n✂️ ЭТАП 6 - Подрезка orphan компонент ({ORPHAN_TRIM_LENGTH}px)...")

            skeleton_final, trimmed_mask, trim_stats = trim_orphan_components(
                skeleton_final, nodes,
                trim_length=ORPHAN_TRIM_LENGTH,
                touch_distance=ORPHAN_TOUCH_DISTANCE,
                verbose=DEBUG
            )

            if DEBUG and np.sum(trimmed_mask) > 0:
                trimmed_viz_path = output_path.replace('.png', '_trimmed_orphans.png')
                cv2.imwrite(trimmed_viz_path, trimmed_mask)
                print(f"   💾 Подрезанные orphans: {os.path.basename(trimmed_viz_path)}")

        elif REMOVE_ORPHANS:
            if DEBUG:
                print(f"\n🧹 ЭТАП 6 - Удаление orphan компонент...")

            skeleton_final, removed_mask, orphan_stats = remove_orphan_components(
                skeleton_final, nodes,
                touch_distance=ORPHAN_TOUCH_DISTANCE,
                verbose=DEBUG
            )

            if DEBUG and np.sum(removed_mask) > 0:
                removed_viz_path = output_path.replace('.png', '_removed_orphans.png')
                cv2.imwrite(removed_viz_path, removed_mask)
                print(f"   💾 Удалённые orphans: {os.path.basename(removed_viz_path)}")
        print(f"[SKEL_EXT] stage_6_orphans: {time.time()-_t0:.2f}s")

        # ========================================
        # ФИНАЛЬНАЯ РАСШИРЕННАЯ МАСКА (после BFS)
        # ========================================
        _t0 = time.time()
        if DEBUG:
            print(f"\n🛡️ Создание финальной расширенной маски после BFS...")

        final_remaining_endpoints = [ep for idx, ep in enumerate(endpoints_work) if idx not in connected_eps]
        final_remaining_indices = [idx for idx in range(len(endpoints_work)) if idx not in connected_eps]

        final_extended_mask = create_final_protection_mask(
            skeleton_final, original, endpoints_work, connected_eps,
            mask_width=MASK_WIDTH, clearance_radius=4, verbose=DEBUG
        )
        print(f"[SKEL_EXT] protection_mask_2: {time.time()-_t0:.2f}s")

        # Сохранить финальную расширенную маску
        if DEBUG:
            final_extended_mask_path = output_path.replace('.png', '_final_extended_mask.png')
            cv2.imwrite(final_extended_mask_path, final_extended_mask)
            print(f"   💾 Финальная расширенная маска: {os.path.basename(final_extended_mask_path)}")

        # Визуализация финальной расширенной маски
        if DEBUG:
            viz_final_extended = visualize_protection_mask(original, skeleton_final, final_extended_mask,
                                                           final_remaining_endpoints)
            final_extended_viz_path = output_path.replace('.png', '_final_extended_mask_viz.png')
            cv2.imwrite(final_extended_viz_path, viz_final_extended)
            print(f"   💾 Визуализация финальной маски: {os.path.basename(final_extended_viz_path)}")

        # Сохранить финальный скелет
        _t0 = time.time()
        cv2.imwrite(skeleton_output_path, skeleton_final)
        print(f"   💾 Скелет: {os.path.basename(skeleton_output_path)}")

        # Сохранить финальную маску (основной output)
        cv2.imwrite(output_path, final_extended_mask)
        if DEBUG:
            print(f"   💾 Финальная маска: {os.path.basename(output_path)}")

        # Визуализация контактов скелета с узлами
        if DEBUG:
            viz_contacts = visualize_skeleton_contacts(
                original, skeleton_final, nodes, endpoints_work, connected_eps
            )
            contacts_viz_path = output_path.replace('.png', '_skeleton_contacts.png')
            cv2.imwrite(contacts_viz_path, viz_contacts)
            print(f"   💾 Визуализация контактов: {os.path.basename(contacts_viz_path)}")
        print(f"[SKEL_EXT] save_files: {time.time()-_t0:.2f}s")

        print(f"[SKEL_EXT] TOTAL: {time.time()-_t_total:.2f}s")
        print(f"✅ Готово!")
        return True

    except Exception as e:
        print(f"   ❌ Ошибка: {e}")
        logger.error("skeleton_extension: сбой process_single_image: %s", e, exc_info=True)
        return False

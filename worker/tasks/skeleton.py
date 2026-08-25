"""
Skeleton Tasks — скелетизация + генерация маски из скелета.

task_skeletonize:
    Полная скелетизация через skeleton_extension (Phase 2b).
    Этапы: pipe_mask → skeleton_extension.process_single_image() → skeleton_to_mask()
    Останавливается на SKELETONIZED — ждёт UI валидации масок.

task_skeletonize_simple:
    Скелетизация валидированной маски через skeleton_extension simple_mode (Phase 4).
    pipe_mask_validated → skeleton_extension.process_single_image(simple_mode=True)
    → skeleton_final.png
    Auto-chain → task_detect_junctions
"""

import json
import logging
import os
import tempfile
import traceback
from pathlib import Path

import cv2
import numpy as np
from celery.exceptions import SoftTimeLimitExceeded

from worker.celery_app import celery_app
from worker.utils.db_helpers import set_diagram_error, check_deleted, upsert_artifact, start_stage, complete_stage, fail_stage, persist_failed_attempt, make_step_reporter
from app.core import obs
from app.core.errors import (
    ArtifactMissingError,
    ConfigError,
    PipelineError,
    SkeletonizationError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)


def _save_mask_visualization(
    image_path: Path,
    mask_path: Path,
    output_path: Path,
    darken: float = 0.35,
    color: tuple = (0, 255, 0),
    alpha: float = 0.5,
):
    """
    Сохранить визуализацию маски: затемнённый оригинал + полупрозрачная маска.

    Args:
        image_path: путь к оригинальному изображению
        mask_path: путь к маске (binary, 0/255)
        output_path: куда сохранить визуализацию
        darken: коэффициент затемнения оригинала (0.35 = 35% яркости)
        color: цвет маски в BGR (по умолчанию зелёный)
        alpha: прозрачность маски (0.5 = полупрозрачный)
    """
    img = cv2.imread(str(image_path))
    mask = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
    if img is None or mask is None:
        return

    # Затемнить оригинал
    vis = (img.astype(np.float32) * darken).astype(np.uint8)

    # Наложить полупрозрачную маску
    mask_bool = mask > 127
    overlay = vis.copy()
    overlay[mask_bool] = color  # BGR
    vis = cv2.addWeighted(overlay, alpha, vis, 1.0 - alpha, 0)

    cv2.imwrite(str(output_path), vis)


# =============================================================================
# task_skeletonize — полная скелетизация (Phase 2b)
# =============================================================================

@celery_app.task(
    bind=True,
    name="worker.tasks.skeleton.task_skeletonize",
    max_retries=2,
    default_retry_delay=60,
    time_limit=1800,
    soft_time_limit=1740,
    acks_late=True,
)
def task_skeletonize(
    self,
    diagram_uid: str,
    project_code: str = "thermohydraulics",
):
    """
    Полная скелетизация: extension + mask generation.

    Args:
        diagram_uid: UUID диаграммы
        project_code: Код проекта для загрузки конфигурации
    """
    from app.db.session import SessionLocal

    db = SessionLocal()
    stage = None

    # Корреляционный контекст фазы (Волна 3): uid/phase/task_id/attempt → в каждую
    # строку лога через ContextFilter (Волна 0).
    obs.bind(
        uid=str(diagram_uid),
        phase="skeletonizing",
        task_id=self.request.id,
        attempt=self.request.retries,
    )

    try:
        logger.info("Starting skeletonization for %s", diagram_uid)

        from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
        from app.services.project_loader import get_project_loader

        from skeleton_extension.processing import process_single_image
        from skeleton_extension.mask_generation import skeleton_to_mask

        # ===== 1. Project config =====
        project_loader = get_project_loader()
        project_config = project_loader.load(project_code)
        if not project_config:
            raise ConfigError(
                f"Project config '{project_code}' not found", stage="skeletonizing"
            )

        skel_cfg = project_config.skeleton

        # ===== 2. Diagram from DB =====
        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if not diagram:
            raise PipelineError(
                f"Diagram {diagram_uid} not found",
                stage="skeletonizing", diagram_uid=str(diagram_uid),
            )

        if check_deleted(db, diagram_uid):
            logger.info("Diagram %s is deleted, aborting", diagram_uid)
            return {"status": "deleted", "diagram_uid": diagram_uid}

        # Idempotency
        if diagram.status in (
            DiagramStatus.SKELETONIZED,
            DiagramStatus.VALIDATING_MASKS,
            DiagramStatus.VALIDATED_MASKS,
        ):
            logger.info("Diagram %s already past skeletonization, skipping", diagram_uid)
            return {"status": "already_completed", "diagram_uid": diagram_uid}

        if diagram.status not in (DiagramStatus.SKELETONIZING, DiagramStatus.ERROR):
            logger.warning(
                "Diagram %s status is %s, expected SKELETONIZING",
                diagram_uid,
                diagram.status.value,
            )
            return {"status": "skipped", "diagram_uid": diagram_uid}

        # ===== Processing Stage tracking =====
        from app.models.stage import StageType
        stage = start_stage(db, diagram_uid, StageType.SKELETONIZATION, celery_task_id=self.request.id)
        obs.bind_step_sink(make_step_reporter(stage.id))  # current_step → клиент (Волна B)

        # ===== 3. Paths =====
        storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
        diagram_dir = storage_path / str(diagram_uid)

        # Input files
        with obs.step("load_inputs", logger):
            image_path = diagram_dir / "original" / "image.png"
            if not image_path.exists():
                for ext in (".jpg", ".jpeg", ".tiff", ".tif"):
                    alt = image_path.with_suffix(ext)
                    if alt.exists():
                        image_path = alt
                        break
                else:
                    raise ArtifactMissingError(
                        f"Image not found: {image_path}", stage="skeletonizing"
                    )

            pipe_mask_path = diagram_dir / "segmentation" / "pipe_mask.png"
            if not pipe_mask_path.exists():
                raise ArtifactMissingError(
                    f"Pipe mask not found: {pipe_mask_path}", stage="skeletonizing"
                )

            node_mask_path = diagram_dir / "segmentation" / "node_mask.png"
            if not node_mask_path.exists():
                raise ArtifactMissingError(
                    f"Node mask not found: {node_mask_path}", stage="skeletonizing"
                )

        # Output dirs
        skel_dir = diagram_dir / "skeleton"
        skel_dir.mkdir(parents=True, exist_ok=True)

        skeleton_output_path = skel_dir / "skeleton.png"
        skeleton_mask_output_path = skel_dir / "skeleton_mask.png"

        logger.info(
            "Input: image=%s, pipe_mask=%s, node_mask=%s",
            image_path.name,
            pipe_mask_path.name,
            node_mask_path.name,
        )

        # ===== 3c. Визуализация маски (отключена — экономим время) =====

        # ===== 4. Skeleton Extension =====
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            protection_mask_path = tmp.name

        try:
            config = skel_cfg.to_process_config()

            # Build background_node_mask — skeleton extension should not extend to these
            seg_cfg = project_config.segmentation
            ignore_cats = {c.lower() for c in getattr(seg_cfg, 'ignore_categories', [])}
            ignore_cats.discard("annotation")  # annotation already excluded from node_mask

            if ignore_cats:
                coco_path = diagram_dir / "detection" / "coco_validated.json"
                if not coco_path.exists():
                    coco_path = diagram_dir / "detection" / "coco_predicted.json"

                if coco_path.exists():
                    try:
                        import json as _json
                        with open(coco_path, "r", encoding="utf-8") as f:
                            coco_data = _json.load(f)

                        categories = {cat["id"]: cat["name"]
                                      for cat in coco_data.get("categories", [])}
                        exclude_ids = {cid for cid, name in categories.items()
                                       if name.lower() in ignore_cats}

                        if exclude_ids:
                            from worker.tasks.segmentation import _process_annotation
                            node_img = cv2.imread(str(node_mask_path), cv2.IMREAD_GRAYSCALE)
                            _h, _w = node_img.shape[:2]
                            bg_mask = np.zeros((_h, _w), dtype=np.uint8)
                            for ann in coco_data.get("annotations", []):
                                if ann.get("category_id") in exclude_ids:
                                    am = _process_annotation(ann, _h, _w)
                                    bg_mask = np.maximum(bg_mask, am)
                            bg_mask = (bg_mask * 255).astype(np.uint8)
                            config["background_node_mask"] = bg_mask
                            logger.info(
                                "Background node mask: %d px from categories %s",
                                int(np.sum(bg_mask > 0)), ignore_cats,
                            )
                    except Exception as exc:
                        logger.warning("Failed to build background_node_mask: %s", exc, exc_info=True)

            logger.info(
                "Running skeleton extension (simple_mode=%s, bfs_iterations=%d) ...",
                config["simple_mode"],
                config["bfs_iterations"],
            )

            with obs.step("compute", logger):
                success = process_single_image(
                    str(image_path),
                    str(pipe_mask_path),
                    str(node_mask_path),
                    protection_mask_path,
                    str(skeleton_output_path),
                    config,
                )

                if not success:
                    raise SkeletonizationError(
                        "Skeleton extension returned failure",
                        stage="skeletonizing", step="compute",
                    )

        finally:
            try:
                os.unlink(protection_mask_path)
            except OSError:
                logger.debug("temp protection mask cleanup failed", exc_info=True)

        if not skeleton_output_path.exists():
            raise SkeletonizationError(
                f"Skeleton file not created: {skeleton_output_path}",
                stage="skeletonizing", step="compute",
            )

        skeleton_img = cv2.imread(str(skeleton_output_path), cv2.IMREAD_GRAYSCALE)
        if skeleton_img is None:
            raise SkeletonizationError(
                f"Failed to read skeleton: {skeleton_output_path}",
                stage="skeletonizing", step="compute",
            )

        skeleton_pixels = int(np.sum(skeleton_img > 127))
        logger.info("Skeleton created: %d pixels", skeleton_pixels)

        # ===== 5. Mask Generation (skeleton → mask) =====
        node_mask_img = cv2.imread(str(node_mask_path), cv2.IMREAD_GRAYSCALE)

        # Загрузка данных для адаптивной толщины
        original_img = None
        pipe_mask_img = None
        if skel_cfg.mask_adaptive:
            original_img = cv2.imread(str(image_path))
            pipe_mask_img = cv2.imread(str(pipe_mask_path), cv2.IMREAD_GRAYSCALE)
            if original_img is None:
                logger.warning("Cannot read original image for adaptive mask, falling back to fixed")
            else:
                logger.info(
                    "Generating adaptive mask from skeleton "
                    "(fallback=%d, min=%d, max=%d, prune=%d, smooth=%d) ...",
                    skel_cfg.mask_thickness,
                    skel_cfg.mask_min_thickness,
                    skel_cfg.mask_max_thickness,
                    skel_cfg.mask_prune_spurs,
                    skel_cfg.mask_smooth_size,
                )
        if not skel_cfg.mask_adaptive or original_img is None:
            logger.info(
                "Generating fixed mask from skeleton (thickness=%d, prune=%d, smooth=%d) ...",
                skel_cfg.mask_thickness,
                skel_cfg.mask_prune_spurs,
                skel_cfg.mask_smooth_size,
            )

        mask_result, mask_stats = skeleton_to_mask(
            skeleton_img,
            node_mask_img,
            thickness=skel_cfg.mask_thickness,
            prune_spurs_length=skel_cfg.mask_prune_spurs,
            smooth_size=skel_cfg.mask_smooth_size,
            verbose=False,
            original_image=original_img,
            pipe_mask=pipe_mask_img,
            adaptive=skel_cfg.mask_adaptive,
            min_thickness=skel_cfg.mask_min_thickness,
            max_thickness=skel_cfg.mask_max_thickness,
        )

        # Хвост трубы в боксе стрелки втягивается скелетизацией/prune, и бокс
        # остаётся без трубы — вернуть пиксели модельной маски (только внутри
        # боксов napravlenie, текст и прочее не трогается).
        try:
            from mask_refinement import restore_direction_box_tails
            coco_tail_path = diagram_dir / "detection" / "coco_validated.json"
            if coco_tail_path.exists():
                if pipe_mask_img is None:
                    pipe_mask_img = cv2.imread(str(pipe_mask_path), cv2.IMREAD_GRAYSCALE)
                if pipe_mask_img is not None:
                    with open(coco_tail_path, encoding="utf-8") as _f:
                        _coco_tails = json.load(_f)
                    raw_bin = (pipe_mask_img > 127).astype(np.uint8)
                    mask_result, _tails = restore_direction_box_tails(
                        mask_result, raw_bin, _coco_tails)
                    if _tails:
                        logger.info("Direction-box tails restored: %d px", _tails)
        except (OSError, ValueError) as tails_exc:
            logger.warning("direction-box tails restore failed: %s", tails_exc,
                           exc_info=True)

        cv2.imwrite(str(skeleton_mask_output_path), mask_result)

        mask_pixels = int(np.sum(mask_result > 127))
        logger.info("Skeleton mask created: %d pixels", mask_pixels)

        if mask_stats.get("pruning"):
            pruning = mask_stats["pruning"]
            logger.info(
                "Pruning stats: removed %d spurs (%d pixels)",
                pruning.get("spurs_removed", 0),
                pruning.get("pixels_removed", 0),
            )

        if mask_stats.get("adaptive"):
            ad = mask_stats["adaptive"]
            logger.info(
                "Adaptive mask: %d thickness groups, range=%s, median=%spx",
                ad.get("groups", 0),
                ad.get("thickness_range", "?"),
                ad.get("thickness_median", "?"),
            )
        if mask_stats.get("clip_removed_pixels", 0) > 0:
            logger.info(
                "Clip: removed %d pixels outside pipe boundaries",
                mask_stats["clip_removed_pixels"],
            )

        # ===== 6. Артефакты в БД =====
        with obs.step("persist_artifacts", logger):
            for art_type, art_path in [
                (ArtifactType.SKELETON, skeleton_output_path),
                (ArtifactType.SKELETON_MASK, skeleton_mask_output_path),
            ]:
                upsert_artifact(db, diagram_uid, art_type, str(art_path), storage_path, "image/png")

        # ===== 7. Обновление статуса =====
        diagram.status = DiagramStatus.SKELETONIZED
        diagram.error_message = None
        diagram.error_stage = None
        complete_stage(stage, {"skeleton_pixels": skeleton_pixels, "mask_pixels": mask_pixels})
        db.commit()

        logger.info("Skeletonization complete. Ready for mask validation.")

        # НЕ чейним — SKELETONIZED = точка останова для UI валидации масок

        return {
            "status": "success",
            "diagram_uid": diagram_uid,
            "skeleton_pixels": skeleton_pixels,
            "mask_pixels": mask_pixels,
        }

    except SoftTimeLimitExceeded:
        logger.error("Skeletonization timed out for %s", diagram_uid, exc_info=True)
        fail_stage(stage, "Skeletonization timed out (29 min limit)", traceback.format_exc())
        set_diagram_error(db, diagram_uid, "Skeletonization timed out (29 min limit)", "skeletonizing")
        raise

    except Exception as exc:
        # exc_info=True + exc= в fail_stage → error_code/failed_step/traceback
        # доезжают до /stages (DoD §4).
        logger.error("Skeletonization failed for %s: %s", diagram_uid, exc, exc_info=True)

        if self.request.retries < self.max_retries:
            persist_failed_attempt(db, stage, str(exc)[:500], traceback.format_exc(), exc=exc)
            logger.info("Retrying (%d/%d) ...", self.request.retries + 1, self.max_retries)
            raise self.retry(exc=exc)

        fail_stage(stage, str(exc)[:500], traceback.format_exc(), exc=exc)
        set_diagram_error(db, diagram_uid, str(exc)[:500], "skeletonizing")
        raise

    finally:
        db.close()


# =============================================================================
# task_skeletonize_simple — скелетизация через extension simple_mode (Phase 4)
# =============================================================================

@celery_app.task(
    bind=True,
    name="worker.tasks.skeleton.task_skeletonize_simple",
    max_retries=1,
    time_limit=600,
    soft_time_limit=540,
    acks_late=True,
)
def task_skeletonize_simple(
    self,
    diagram_uid: str,
    project_code: str = "thermohydraulics",
):
    """
    Скелетизация валидированной маски труб через skeleton_extension simple_mode.

    pipe_mask_validated + node_mask + original → skeleton_extension (simple_mode=True)
    → skeleton_final.png

    Этапы skeleton_extension simple_mode:
    1. skeletonize(pipe_mask)
    2. remove_skeleton_under_nodes_simple — удалить внутри узлов, контур сохранить
    3. create_simple_protection_mask — продлить endpoints до контура узлов
    4. connect_with_directed_lines — направленные линии к узлам/endpoints/скелетам
    5. bfs_connect_endpoints — BFS поиск оставшихся соединений
    6. remove_orphan_components — удаление неподключённых компонент

    Сохраняет:
    - skeleton/skeleton_final.png (SKELETON_FINAL)
    """
    from app.db.session import SessionLocal

    db = SessionLocal()
    stage = None

    # Корреляционный контекст фазы (Волна 3): uid/phase/task_id/attempt → в каждую
    # строку лога через ContextFilter (Волна 0).
    obs.bind(
        uid=str(diagram_uid),
        phase="skeletonizing_simple",
        task_id=self.request.id,
        attempt=self.request.retries,
    )

    try:
        logger.info("Starting simple skeletonization for %s", diagram_uid)

        from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
        from app.services.project_loader import get_project_loader

        from skeleton_extension.processing import process_single_image

        # ===== 1. Project config =====
        project_loader = get_project_loader()
        project_config = project_loader.load(project_code)
        if not project_config:
            raise ConfigError(
                f"Project config '{project_code}' not found",
                stage="skeletonizing_simple",
            )

        skel_cfg = project_config.skeleton

        # ===== 2. Diagram from DB =====
        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if not diagram:
            raise PipelineError(
                f"Diagram {diagram_uid} not found",
                stage="skeletonizing_simple", diagram_uid=str(diagram_uid),
            )

        if check_deleted(db, diagram_uid):
            logger.info("Diagram %s is deleted, aborting", diagram_uid)
            return {"status": "deleted", "diagram_uid": diagram_uid}

        if diagram.status not in (DiagramStatus.VALIDATED_MASKS, DiagramStatus.SKELETONIZING_FINAL):
            logger.warning(
                "Diagram %s status is %s, expected VALIDATED_MASKS or SKELETONIZING_FINAL",
                diagram_uid,
                diagram.status.value,
            )
            return {"status": "skipped", "diagram_uid": diagram_uid}

        # Set status → SKELETONIZING_FINAL
        diagram.status = DiagramStatus.SKELETONIZING_FINAL

        # ===== Processing Stage tracking =====
        from app.models.stage import StageType
        stage = start_stage(db, diagram_uid, StageType.FINAL_SKELETONIZATION, celery_task_id=self.request.id)
        obs.bind_step_sink(make_step_reporter(stage.id))  # current_step → клиент (Волна B)

        db.commit()

        # ===== 3. Paths =====
        storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
        diagram_dir = storage_path / str(diagram_uid)

        # Input: validated pipe mask
        with obs.step("load_inputs", logger):
            pipe_mask_path = diagram_dir / "segmentation" / "pipe_mask_validated.png"
            if not pipe_mask_path.exists():
                raise ArtifactMissingError(
                    f"Validated pipe mask not found: {pipe_mask_path}",
                    stage="skeletonizing_simple",
                )

            # Node mask (обязательна для skeleton_extension)
            node_mask_path = diagram_dir / "segmentation" / "node_mask.png"
            if not node_mask_path.exists():
                raise ArtifactMissingError(
                    f"Node mask not found: {node_mask_path}",
                    stage="skeletonizing_simple",
                )

            # Original image (нужна для skeleton_extension — adaptive threshold)
            image_path = diagram_dir / "original" / "image.png"
            if not image_path.exists():
                for ext in (".jpg", ".jpeg", ".tiff", ".tif"):
                    alt = image_path.with_suffix(ext)
                    if alt.exists():
                        image_path = alt
                        break
                else:
                    raise ArtifactMissingError(
                        f"Original image not found: {image_path}",
                        stage="skeletonizing_simple",
                    )

        # Output
        skel_dir = diagram_dir / "skeleton"
        skel_dir.mkdir(parents=True, exist_ok=True)
        skeleton_final_path = skel_dir / "skeleton_final.png"

        logger.info(
            "Input: image=%s, pipe_mask=%s, node_mask=%s",
            image_path.name,
            pipe_mask_path.name,
            node_mask_path.name,
        )

        # ===== 3b. Mask Refinement (adaptive dilate) =====
        try:
            from mask_refinement import refine_pipe_mask

            image_bgr = cv2.imread(str(image_path))
            pm_bin = (cv2.imread(str(pipe_mask_path), cv2.IMREAD_GRAYSCALE) > 127).astype(np.uint8)
            nm_bin = (cv2.imread(str(node_mask_path), cv2.IMREAD_GRAYSCALE) > 127).astype(np.uint8)

            refined_mask, refine_stats = refine_pipe_mask(pm_bin, image_bgr, nm_bin)

            # Хвост трубы в боксе стрелки втянут ещё до валидации (skeleton_mask
            # первого этапа), refine его не возвращает — вернуть пиксели
            # модельной маски (только внутри боксов napravlenie).
            try:
                from mask_refinement import restore_direction_box_tails
                raw_tail_path = diagram_dir / "segmentation" / "pipe_mask.png"
                coco_tail_path = diagram_dir / "detection" / "coco_validated.json"
                if raw_tail_path.exists() and coco_tail_path.exists():
                    raw_bin = (cv2.imread(str(raw_tail_path),
                                          cv2.IMREAD_GRAYSCALE) > 127).astype(np.uint8)
                    with open(coco_tail_path, encoding="utf-8") as _f:
                        _coco_tails = json.load(_f)
                    refined_mask, _tails = restore_direction_box_tails(
                        refined_mask, raw_bin, _coco_tails)
                    if _tails:
                        logger.info("Direction-box tails restored: %d px", _tails)
            except (OSError, ValueError) as tails_exc:
                logger.warning("direction-box tails restore failed: %s", tails_exc,
                               exc_info=True)

            refined_path = diagram_dir / "segmentation" / "pipe_mask_refined.png"
            cv2.imwrite(str(refined_path), refined_mask)

            logger.info(
                "Mask refinement: %d -> %d px (%.1fs, median_thickness=%.1f)",
                refine_stats.get("original_px", 0),
                refine_stats.get("refined_px", 0),
                refine_stats.get("time", 0),
                refine_stats.get("median_thickness", 0),
            )

            # Подменяем pipe_mask_path на refined — skeleton_extension работает из неё
            pipe_mask_path = refined_path

            # Сохраняем артефакт в БД
            old_refined = (
                db.query(Artifact)
                .filter(
                    Artifact.diagram_uid == diagram_uid,
                    Artifact.artifact_type == ArtifactType.PIPE_MASK_REFINED,
                )
                .first()
            )
            if old_refined:
                db.delete(old_refined)
                db.flush()

            artifact_refined = Artifact(
                diagram_uid=diagram_uid,
                artifact_type=ArtifactType.PIPE_MASK_REFINED,
                file_path=str(refined_path.relative_to(storage_path)),
                file_size=refined_path.stat().st_size,
                mime_type="image/png",
            )
            db.add(artifact_refined)
            db.flush()

        except Exception as refine_exc:
            logger.warning(
                "Mask refinement failed, using validated mask as-is: %s",
                refine_exc,
                exc_info=True,
            )

        # ===== 4. Skeleton Extension (simple_mode=True) =====
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            protection_mask_path = tmp.name

        try:
            # Строим конфиг из project_config с принудительным simple_mode=True
            config = skel_cfg.to_process_config()
            config["simple_mode"] = True

            # Build background_node_mask — skeleton extension should not extend to these
            seg_cfg = project_config.segmentation
            ignore_cats = {c.lower() for c in getattr(seg_cfg, 'ignore_categories', [])}
            ignore_cats.discard("annotation")

            if ignore_cats:
                coco_path = diagram_dir / "detection" / "coco_validated.json"
                if not coco_path.exists():
                    coco_path = diagram_dir / "detection" / "coco_predicted.json"

                if coco_path.exists():
                    try:
                        import json as _json
                        with open(coco_path, "r", encoding="utf-8") as f:
                            coco_data = _json.load(f)

                        categories = {cat["id"]: cat["name"]
                                      for cat in coco_data.get("categories", [])}
                        exclude_ids = {cid for cid, name in categories.items()
                                       if name.lower() in ignore_cats}

                        if exclude_ids:
                            from worker.tasks.segmentation import _process_annotation
                            node_img = cv2.imread(str(node_mask_path), cv2.IMREAD_GRAYSCALE)
                            _h, _w = node_img.shape[:2]
                            bg_mask = np.zeros((_h, _w), dtype=np.uint8)
                            for ann in coco_data.get("annotations", []):
                                if ann.get("category_id") in exclude_ids:
                                    am = _process_annotation(ann, _h, _w)
                                    bg_mask = np.maximum(bg_mask, am)
                            bg_mask = (bg_mask * 255).astype(np.uint8)
                            config["background_node_mask"] = bg_mask
                            logger.info(
                                "Background node mask (simple): %d px from categories %s",
                                int(np.sum(bg_mask > 0)), ignore_cats,
                            )
                    except Exception as exc:
                        logger.warning("Failed to build background_node_mask: %s", exc, exc_info=True)

            logger.info(
                "Running skeleton extension simple_mode "
                "(extend_radius=%d, bfs_iterations=%d, max_line_length=%d) ...",
                config.get("extend_radius", 5),
                config.get("bfs_iterations", 1),
                config.get("max_line_length", 600),
            )

            with obs.step("compute", logger):
                success = process_single_image(
                    str(image_path),
                    str(pipe_mask_path),
                    str(node_mask_path),
                    protection_mask_path,
                    str(skeleton_final_path),
                    config,
                )

                if not success:
                    raise SkeletonizationError(
                        "Skeleton extension (simple_mode) returned failure",
                        stage="skeletonizing_simple", step="compute",
                    )

        finally:
            try:
                os.unlink(protection_mask_path)
            except OSError:
                logger.debug("temp protection mask cleanup failed", exc_info=True)

        if not skeleton_final_path.exists():
            raise SkeletonizationError(
                f"Skeleton final file not created: {skeleton_final_path}",
                stage="skeletonizing_simple", step="compute",
            )

        skeleton_img = cv2.imread(str(skeleton_final_path), cv2.IMREAD_GRAYSCALE)
        if skeleton_img is None:
            raise SkeletonizationError(
                f"Failed to read skeleton: {skeleton_final_path}",
                stage="skeletonizing_simple", step="compute",
            )

        skeleton_pixels = int(np.sum(skeleton_img > 127))

        # Проверка: скелет должен касаться node_mask
        node_mask_img = cv2.imread(str(node_mask_path), cv2.IMREAD_GRAYSCALE)
        if node_mask_img is not None:
            contact_pixels = int(
                np.sum((skeleton_img > 127) & (node_mask_img > 127))
            )
            logger.info(
                "Skeleton created: %d pixels, %d contact pixels with node_mask",
                skeleton_pixels,
                contact_pixels,
            )
            if contact_pixels == 0:
                logger.warning(
                    "WARNING: skeleton has 0 contact pixels with node_mask! "
                    "Graph building may produce empty graph."
                )
        else:
            logger.info("Skeleton created: %d pixels", skeleton_pixels)

        # ===== 5. Artifact in DB =====
        with obs.step("persist_artifacts", logger):
            # Remove old SKELETON_FINAL if exists
            old = (
                db.query(Artifact)
                .filter(
                    Artifact.diagram_uid == diagram_uid,
                    Artifact.artifact_type == ArtifactType.SKELETON_FINAL,
                )
                .first()
            )
            if old:
                db.delete(old)
                db.flush()

            artifact = Artifact(
                diagram_uid=diagram_uid,
                artifact_type=ArtifactType.SKELETON_FINAL,
                file_path=str(skeleton_final_path.relative_to(storage_path)),
                file_size=skeleton_final_path.stat().st_size,
                mime_type="image/png",
            )
            db.add(artifact)

        # Update status → SKELETONIZED_FINAL (ready for junction detection)
        diagram.status = DiagramStatus.SKELETONIZED_FINAL
        diagram.error_message = None
        diagram.error_stage = None
        complete_stage(stage, {"skeleton_pixels": skeleton_pixels})
        db.commit()

        # Auto-dispatch junction detection
        try:
            from worker.celery_app import celery_app as _celery
            _celery.send_task(
                "worker.tasks.junction.task_detect_junctions",
                args=[diagram_uid, project_code],
                queue="gpu",
            )
            logger.info("Auto-dispatched junction detection for %s", diagram_uid)
        except Exception as dispatch_exc:
            logger.warning(
                "Failed to auto-dispatch junction detection: %s",
                dispatch_exc, exc_info=True,
            )

        logger.info(
            "Simple skeletonization complete for %s: %d px",
            diagram_uid,
            skeleton_pixels,
        )

        return {
            "status": "success",
            "diagram_uid": diagram_uid,
            "skeleton_pixels": skeleton_pixels,
        }

    except SoftTimeLimitExceeded:
        logger.error("Simple skeletonization timed out for %s", diagram_uid, exc_info=True)
        fail_stage(stage, "Simple skeletonization timed out", traceback.format_exc())
        set_diagram_error(db, diagram_uid, "Simple skeletonization timed out", "skeletonizing_simple")
        raise

    except Exception as exc:
        # exc_info=True + exc= в fail_stage → error_code/failed_step/traceback
        # доезжают до /stages (DoD §4).
        logger.error(
            "Simple skeletonization failed for %s: %s", diagram_uid, exc, exc_info=True
        )

        if self.request.retries < self.max_retries:
            persist_failed_attempt(db, stage, str(exc)[:500], traceback.format_exc(), exc=exc)
            raise self.retry(exc=exc)

        fail_stage(stage, str(exc)[:500], traceback.format_exc(), exc=exc)
        set_diagram_error(db, diagram_uid, str(exc)[:500], "skeletonizing_simple")
        raise

    finally:
        db.close()

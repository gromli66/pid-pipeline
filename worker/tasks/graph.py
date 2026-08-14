"""
Graph Tasks — построение графа P&ID (Phase 5).

task_build_graph:
    Полное построение графа через graph_module.GraphBuilder.
    Входы: skeleton_final + node_mask + junction/bridge validated + coco_validated
    Выходы: graph.json + graph_overlay.png
    Auto-chain: вызывается из task_skeletonize_simple (при успехе)

task_generate_fxml:
    Генерация FXML из валидированного графа (Phase 5, Step 2).
    Входы: graph_validated.json (fallback: graph.json)
    Выходы: fxml/diagram.fxml
    Статус: VALIDATED_GRAPH → GENERATING_FXML → COMPLETED
"""

import logging
import os
import traceback
from pathlib import Path

import cv2
import numpy as np
from celery.exceptions import SoftTimeLimitExceeded

from worker.celery_app import celery_app
from worker.utils.db_helpers import set_diagram_error, check_deleted, start_stage, complete_stage, fail_stage, persist_failed_attempt, make_step_reporter
from app.core import obs
from app.core.errors import ArtifactMissingError, PipelineError
from app.core.logging import get_logger

logger = get_logger(__name__)


def _canvas_is_fresh(canvas_path, validated_path, diagram_uid,
                     contours_path=None) -> bool:
    """Можно ли экспортировать этот холст, или он отстал от graph_validated.

    Экспорт брал холст по одному факту существования файла, а файл переживает
    rollback (удаляется только строка артефакта). Плюс с автозапуском раскладки
    холст появляется у КАЖДОЙ диаграммы, так что «существует» перестало значить
    «актуален». Сверка — общим модулем, тем же, что зовёт клиент.

    Холст без метки версии (собран до появления меток) считается свежим: это
    ручная правка оператора, сделанная старым клиентом, и терять её нельзя.

    Контурная свежесть — отдельно (contours_merge): контуры не входят в
    sha-проекцию графа, а холст без свежих контуров экспортировать нельзя —
    FXML молча ушёл бы без выбранных оператором полигонов.
    """
    import json as _json

    from modules.graph.core import canvas_state
    from modules.graph.core.contours_merge import (
        contours_are_stale, load_validated_contours,
    )

    if not validated_path.exists():
        return True          # сверять не с чем — прежнее поведение
    try:
        canvas = _json.loads(canvas_path.read_text(encoding="utf-8"))
        validated = _json.loads(validated_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[%s] холст не читается (%s) — беру graph_validated",
                       diagram_uid, exc)
        return False
    if not canvas_state.read_state(canvas)["layout_version"]:
        logger.info("[%s] холст без метки версии — считаю ручной правкой",
                    diagram_uid)
        return True
    stale, reason = canvas_state.is_stale(canvas, validated)
    if not stale and contours_path is not None:
        stale, reason = contours_are_stale(
            canvas, load_validated_contours(contours_path))
    if stale:
        logger.warning("[%s] холст устарел (%s) — экспорт идёт из "
                       "graph_validated", diagram_uid, reason)
        return False
    return True


def _mirror_text(canvas_graph, validated_path, diagram_uid) -> None:
    """Подмешать подписи и привязки из graph_validated в холст (в памяти).

    Файл холста НЕ трогаем: правки оператора — не дело экспорта. Если текст на
    холсте правился руками (`text_edited`), первоисточником стал сам холст, и
    импорт молча его не затирает.
    """
    import json as _json

    if not validated_path.exists():
        return
    from modules.graph.core import canvas_state, text_import

    if canvas_state.read_state(canvas_graph)["text_edited"]:
        logger.info("[%s] текст правился на холсте — импорт не нужен",
                    diagram_uid)
        return
    try:
        validated = _json.loads(validated_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[%s] graph_validated не читается (%s) — текст в FXML "
                       "берётся как есть", diagram_uid, exc)
        return
    stale, reason = canvas_state.text_is_stale(canvas_graph, validated)
    if not stale:
        return
    try:
        stats = text_import.import_text(canvas_graph, validated)
    except Exception as exc:  # noqa: BLE001 — экспорт важнее подписей
        logger.warning("[%s] импорт текста на холст не удался: %s",
                       diagram_uid, exc, exc_info=True)
        return
    canvas_graph.setdefault("graph", {}).setdefault(
        "canvas_transform", {})["text_imported_sha"] = \
        canvas_state.text_projection_sha(validated)
    logger.info("[%s] текст импортирован в холст перед экспортом (%s): %s",
                diagram_uid, reason, stats)


# =============================================================================
# task_build_graph — построение графа (Phase 5)
# =============================================================================

@celery_app.task(
    bind=True,
    name="worker.tasks.graph.task_build_graph",
    max_retries=2,
    default_retry_delay=60,
    time_limit=1800,
    soft_time_limit=1740,
    acks_late=True,
)
def task_build_graph(self, diagram_uid: str):
    """
    Построение графа P&ID из масок и скелета.

    Использует graph_module.core.builder.GraphBuilder.

    Входные артефакты:
        - SKELETON_FINAL:            skeleton/skeleton_final.png
        - NODE_MASK:                 segmentation/node_mask.png     (equipment)
        - JUNCTION_MASK_VALIDATED:   junction/junction_mask_validated.png (connections)
        - BRIDGE_MASK_VALIDATED:     junction/bridge_mask_validated.png
        - COCO_VALIDATED:            detection/coco_validated.json
        - ORIGINAL_IMAGE:            original/image.png

    Выходные артефакты:
        - GRAPH_JSON:    graph/graph.json
        - GRAPH_OVERLAY: graph/graph_overlay.png

    Статус:  BUILDING_GRAPH → BUILT
    """
    from app.db.session import SessionLocal

    db = SessionLocal()
    stage = None

    # Корреляционный контекст фазы (Волна 3): uid/phase/task_id/attempt → в каждую
    # строку лога (в т.ч. под-под-шаги builder) через ContextFilter (Волна 0).
    obs.bind(
        uid=str(diagram_uid),
        phase="building_graph",
        task_id=self.request.id,
        attempt=self.request.retries,
    )

    try:
        logger.info("Starting graph building for %s", diagram_uid)

        from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
        from modules.graph.core.builder import GraphBuilder

        # ===== 1. Diagram from DB =====
        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if not diagram:
            raise PipelineError(
                f"Diagram {diagram_uid} not found",
                stage="building_graph", diagram_uid=str(diagram_uid),
            )

        if check_deleted(db, diagram_uid):
            logger.info("Diagram %s is deleted, aborting", diagram_uid)
            return {"status": "deleted", "diagram_uid": diagram_uid}

        # Idempotency: если уже дальше — пропускаем
        if diagram.status in (
            DiagramStatus.BUILT,
            DiagramStatus.VALIDATING_GRAPH,
            DiagramStatus.VALIDATED_GRAPH,
            DiagramStatus.GENERATING_FXML,
            DiagramStatus.COMPLETED,
        ):
            logger.info(
                "Diagram %s already past graph building (status=%s), skipping",
                diagram_uid, diagram.status.value,
            )
            return {"status": "already_completed", "diagram_uid": diagram_uid}

        if diagram.status not in (DiagramStatus.BUILDING_GRAPH, DiagramStatus.VALIDATED_JUNCTIONS, DiagramStatus.ERROR):
            logger.warning(
                "Diagram %s status is %s, expected BUILDING_GRAPH or VALIDATED_JUNCTIONS",
                diagram_uid, diagram.status.value,
            )
            return {"status": "skipped", "diagram_uid": diagram_uid}

        # Set BUILDING_GRAPH if not already
        if diagram.status != DiagramStatus.BUILDING_GRAPH:
            diagram.status = DiagramStatus.BUILDING_GRAPH
            db.commit()

        # ===== Processing Stage tracking =====
        from app.models.stage import StageType
        stage = start_stage(db, diagram_uid, StageType.GRAPH_BUILDING, celery_task_id=self.request.id)
        obs.bind_step_sink(make_step_reporter(stage.id))  # current_step → клиент (Волна B)

        # ===== 2. Paths =====
        storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
        diagram_dir = storage_path / str(diagram_uid)

        # --- Input files ---

        # Skeleton final (из task_skeletonize_simple)
        skeleton_final_path = diagram_dir / "skeleton" / "skeleton_final.png"
        if not skeleton_final_path.exists():
            raise ArtifactMissingError(
                f"Skeleton final not found: {skeleton_final_path}. "
                f"task_skeletonize_simple may not have completed.",
                stage="building_graph", step="load_inputs",
            )

        # Equipment mask (node_mask from segmentation)
        node_mask_path = diagram_dir / "segmentation" / "node_mask.png"
        if not node_mask_path.exists():
            raise ArtifactMissingError(
                f"Node mask not found: {node_mask_path}",
                stage="building_graph", step="load_inputs",
            )

        # Junction mask (validated)
        junction_mask_path = diagram_dir / "junction" / "junction_mask_validated.png"
        if not junction_mask_path.exists():
            raise ArtifactMissingError(
                f"Junction mask validated not found: {junction_mask_path}",
                stage="building_graph", step="load_inputs",
            )

        # Bridge mask (validated)
        bridge_mask_path = diagram_dir / "junction" / "bridge_mask_validated.png"
        if not bridge_mask_path.exists():
            raise ArtifactMissingError(
                f"Bridge mask validated not found: {bridge_mask_path}",
                stage="building_graph", step="load_inputs",
            )

        # COCO validated annotations
        coco_validated_path = diagram_dir / "detection" / "coco_validated.json"
        if not coco_validated_path.exists():
            logger.warning(
                "COCO validated not found: %s, building graph without class labels",
                coco_validated_path,
            )
            coco_validated_path = None

        # Original image (optional, for overlay visualization)
        original_image_path = diagram_dir / "original" / "image.png"
        if not original_image_path.exists():
            # Try other extensions
            for ext in (".jpg", ".jpeg", ".tiff", ".tif"):
                alt = original_image_path.with_suffix(ext)
                if alt.exists():
                    original_image_path = alt
                    break
            else:
                logger.warning("Original image not found, overlay will be without background")
                original_image_path = None

        # --- Output dirs ---
        graph_dir = diagram_dir / "graph"
        graph_dir.mkdir(parents=True, exist_ok=True)

        graph_json_path = graph_dir / "graph.json"
        graph_overlay_path = graph_dir / "graph_overlay.png"

        logger.info(
            "Input files:\n"
            "  skeleton_final: %s\n"
            "  node_mask: %s\n"
            "  junction_mask: %s\n"
            "  bridge_mask: %s\n"
            "  coco: %s\n"
            "  original: %s",
            skeleton_final_path,
            node_mask_path,
            junction_mask_path,
            bridge_mask_path,
            coco_validated_path,
            original_image_path,
        )

        # ===== 3. Build graph =====
        builder = GraphBuilder(
            min_spur_length=5,
            max_path_length=10000,
            node_dilation=0,
            dpi=150,
            show_labels=False,
            save_stats=False,
            debug_isolated=False,
            debug_contacts=False,
            json_format="node-link",
            include_paths=False,
            verbose=True,
            debug=False,
        )

        with obs.step("compute", logger):
            result = builder.build(
                equipment_mask_path=str(node_mask_path),
                connection_mask_path=str(junction_mask_path),
                bridge_mask_path=str(bridge_mask_path),
                skeleton_path=str(skeleton_final_path),
                original_image_path=str(original_image_path) if original_image_path else None,
                coco_path=str(coco_validated_path) if coco_validated_path else None,
                image_filename="image.png",  # Имя файла в COCO JSON (storage convention)
            )

        num_nodes = len(result['nodes'])
        num_edges = len(result['edges'])
        elapsed = result.get('elapsed_time', 0)

        logger.info(
            "Graph built: %d nodes, %d edges in %.2f sec",
            num_nodes, num_edges, elapsed,
        )

        # ===== 4. Save results =====
        # save_visualizations по умолчанию выключен → не рендерим дорогой оверлей
        # (matplotlib ~54с на полном разрешении), как segmentation/junction.
        from app.services.project_loader import get_project_loader
        _save_vis = False
        try:
            _proj = get_project_loader().load(diagram.project_code)
            _save_vis = _proj.save_visualizations if _proj else False
        except Exception:
            logger.warning(
                "graph: project config load failed for save_visualizations flag",
                exc_info=True,
            )

        with obs.step("persist_artifacts", logger):
            builder.save(
                result=result,
                output_dir=str(graph_dir),
                graph_path=str(graph_json_path),
                scheme_name="graph",
                save_visualization=_save_vis,
            )

        # builder.save() создаёт {scheme_name}_graph.png → graph_graph.png только
        # при save_visualization=True. Переименовать в наш стандарт.
        auto_viz_path = graph_dir / "graph_graph.png"

        if _save_vis and auto_viz_path.exists():
            auto_viz_path.replace(graph_overlay_path)

        logger.info(
            "Saved: graph.json (%d bytes)%s",
            graph_json_path.stat().st_size if graph_json_path.exists() else 0,
            ", graph_overlay.png" if _save_vis else "",
        )

        # ===== 5. Artifacts in DB =====
        # Remove old artifacts if re-run
        for art_type in (ArtifactType.GRAPH_JSON, ArtifactType.GRAPH_OVERLAY):
            old = (
                db.query(Artifact)
                .filter(
                    Artifact.diagram_uid == diagram_uid,
                    Artifact.artifact_type == art_type,
                )
                .first()
            )
            if old:
                db.delete(old)
                db.flush()

        # GRAPH_JSON
        if graph_json_path.exists():
            artifact_json = Artifact(
                diagram_uid=diagram_uid,
                artifact_type=ArtifactType.GRAPH_JSON,
                file_path=str(graph_json_path.relative_to(storage_path)),
                file_size=graph_json_path.stat().st_size,
                mime_type="application/json",
            )
            db.add(artifact_json)

        # GRAPH_OVERLAY
        if _save_vis and graph_overlay_path.exists():
            artifact_overlay = Artifact(
                diagram_uid=diagram_uid,
                artifact_type=ArtifactType.GRAPH_OVERLAY,
                file_path=str(graph_overlay_path.relative_to(storage_path)),
                file_size=graph_overlay_path.stat().st_size,
                mime_type="image/png",
            )
            db.add(artifact_overlay)

        # ===== 6. Update diagram =====
        diagram.status = DiagramStatus.BUILT
        diagram.node_count = num_nodes
        diagram.edge_count = num_edges
        diagram.error_message = None
        diagram.error_stage = None
        complete_stage(stage, {"num_nodes": num_nodes, "num_edges": num_edges, "elapsed_sec": round(elapsed, 1)})
        db.commit()

        logger.info("Graph building complete for %s → BUILT", diagram_uid)

        return {
            "status": "success",
            "diagram_uid": diagram_uid,
            "num_nodes": num_nodes,
            "num_edges": num_edges,
            "elapsed_time": elapsed,
        }

    except SoftTimeLimitExceeded:
        logger.error("Graph building timed out for %s", diagram_uid, exc_info=True)
        fail_stage(stage, "Graph building timed out (29 min limit)", traceback.format_exc())
        set_diagram_error(db, diagram_uid, "Graph building timed out (29 min limit)", "building_graph")
        raise

    except Exception as exc:
        # exc_info=True + exc= в fail_stage → error_code/failed_step/traceback
        # доезжают до /stages (DoD §4). Под-под-шаг сбоя (load_masks/trace_edges/…)
        # проставляется obs.step() и всплывает в failed_step.
        logger.error("Graph building failed for %s: %s", diagram_uid, exc, exc_info=True)

        if self.request.retries < self.max_retries:
            persist_failed_attempt(db, stage, str(exc)[:500], traceback.format_exc(), exc=exc)
            logger.info("Retrying (%d/%d) ...", self.request.retries + 1, self.max_retries)
            raise self.retry(exc=exc)

        fail_stage(stage, str(exc)[:500], traceback.format_exc(), exc=exc)
        set_diagram_error(db, diagram_uid, str(exc)[:500], "building_graph")
        raise

    finally:
        db.close()


# =============================================================================
# task_generate_fxml — генерация FXML (Phase 5, Step 2)
# =============================================================================

@celery_app.task(
    bind=True,
    name="worker.tasks.graph.task_generate_fxml",
    max_retries=1,
    default_retry_delay=30,
    time_limit=300,
    soft_time_limit=270,
    acks_late=True,
)
def task_generate_fxml(self, diagram_uid: str, page_size: str = None, bridge_gap: float = None):
    """
    Генерация FXML из валидированного графа.

    Входные артефакты:
        - GRAPH_VALIDATED:  graph/graph_validated.json
          (fallback: GRAPH_JSON → graph/graph.json)

    Выходные артефакты:
        - FXML:  fxml/diagram.fxml

    Args:
        diagram_uid: UUID диаграммы
        page_size: 'A4', 'A3', 'A2', 'A1', 'A0';
                   '1920x1080' — экранный лист (стандартизация + фикс скинов);
                   None — пиксельные координаты (оригинал)
        bridge_gap: опциональный фактор разрыва мостов; None — использовать
                    значение по умолчанию generate_fxml (bridge_gap_factor)

    Статус:  GENERATING_FXML → COMPLETED
    """
    from app.db.session import SessionLocal

    db = SessionLocal()
    stage = None

    # Корреляционный контекст фазы (Волна 3): uid/phase/task_id/attempt → в каждую
    # строку лога через ContextFilter (Волна 0).
    obs.bind(
        uid=str(diagram_uid),
        phase="generating_fxml",
        task_id=self.request.id,
        attempt=self.request.retries,
    )

    try:
        logger.info("Starting FXML generation for %s", diagram_uid)

        from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
        from modules.canvas_to_fxml import generate_canvas_fxml
        from modules.graph_to_fxml import generate_fxml

        # ===== 1. Diagram from DB =====
        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if not diagram:
            raise PipelineError(
                f"Diagram {diagram_uid} not found",
                stage="generating_fxml", diagram_uid=str(diagram_uid),
            )

        if check_deleted(db, diagram_uid):
            logger.info("Diagram %s is deleted, aborting", diagram_uid)
            return {"status": "deleted", "diagram_uid": diagram_uid}

        # Idempotency
        if diagram.status == DiagramStatus.COMPLETED:
            logger.info(
                "Diagram %s already COMPLETED, skipping FXML generation",
                diagram_uid,
            )
            return {"status": "already_completed", "diagram_uid": diagram_uid}

        # Допустимые статусы для запуска
        if diagram.status not in (
            DiagramStatus.VALIDATED_GRAPH,
            DiagramStatus.GENERATING_FXML,
            DiagramStatus.ERROR,
        ):
            logger.warning(
                "Diagram %s status is %s, expected VALIDATED_GRAPH or GENERATING_FXML",
                diagram_uid, diagram.status.value,
            )
            return {"status": "skipped", "diagram_uid": diagram_uid}

        # Переводим в GENERATING_FXML
        diagram.status = DiagramStatus.GENERATING_FXML
        diagram.error_message = None
        diagram.error_stage = None

        # ===== Processing Stage tracking =====
        from app.models.stage import StageType
        stage = start_stage(db, diagram_uid, StageType.FXML_GENERATION, celery_task_id=self.request.id)
        obs.bind_step_sink(make_step_reporter(stage.id))  # current_step → клиент (Волна B)

        db.commit()

        # ===== 2. Paths =====
        storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
        diagram_dir = storage_path / str(diagram_uid)

        # --- Input: graph JSON ---
        # Приоритет: graph_validated.json > graph.json
        with obs.step("load_inputs", logger):
            graph_canvas_path = diagram_dir / "graph" / "graph_canvas.json"
            graph_validated_path = diagram_dir / "graph" / "graph_validated.json"
            graph_json_path = diagram_dir / "graph" / "graph.json"

            # WYSIWYG: холст — результат «Ручной правки», последней стадии перед
            # экспортом, поэтому он в приоритете. Но брать его по одному факту
            # существования нельзя: rollback удаляет только строку артефакта, а
            # файл остаётся на диске, и после возврата назад экспорт уносил бы
            # УСТАРЕВШУЮ раскладку. Свежесть сверяем тем же модулем, что и
            # клиент (§3.8 п. 4 плана AUTO_LAYOUT_INTEGRATION.md).
            if graph_canvas_path.exists() and _canvas_is_fresh(
                    graph_canvas_path, graph_validated_path, diagram_uid,
                    contours_path=(diagram_dir / "contours"
                                   / "contours_validated.json")):
                input_graph_path = graph_canvas_path
                logger.info("Using canvas graph (WYSIWYG): %s", input_graph_path)
            elif graph_validated_path.exists():
                input_graph_path = graph_validated_path
                logger.info("Using validated graph: %s", input_graph_path)
            elif graph_json_path.exists():
                input_graph_path = graph_json_path
                logger.info("Using original graph (no validated version): %s", input_graph_path)
            else:
                raise ArtifactMissingError(
                    f"No graph JSON found. Checked:\n"
                    f"  {graph_validated_path}\n"
                    f"  {graph_json_path}",
                    stage="generating_fxml",
                )

            # --- Output dir ---
            fxml_dir = diagram_dir / "fxml"
            fxml_dir.mkdir(parents=True, exist_ok=True)
            output_fxml_path = fxml_dir / "diagram.fxml"

            # ===== 3. Load graph =====
            import json
            with open(input_graph_path, 'r', encoding='utf-8') as f:
                graph_data = json.load(f)

            # Зеркало текста (§3.8 п. 4 плана). Холст — продукт раскладки,
            # текст в нём не первоисточник: задача считает раскладку ДО OCR,
            # когда ни одного text_block ещё не существует. Если оператор
            # вкладку так и не открыл, FXML ушёл бы без единого <Text> и без
            # KKS (KKS берётся из bindings). Клиентский импорт экспорт не
            # чинит — подмешиваем тем же общим модулем.
            if input_graph_path == graph_canvas_path:
                _mirror_text(graph_data, graph_validated_path, diagram_uid)

        nodes_count = len(graph_data.get('nodes', []))
        edges_count = len(graph_data.get('links', []))
        logger.info(
            "Loaded graph: %d nodes, %d edges",
            nodes_count, edges_count,
        )

        # WYSIWYG: холст 1920x1080 узнаём заранее — от этого зависят и enrich
        # контурами (пропускается), и выбор конвертера. Признак —
        # canvas_transform, который кладёт pretransform; размер — фолбэк для
        # графов, сохранённых до его появления.
        _graph_meta = graph_data.get("graph", {}) or {}
        _img_size = _graph_meta.get("image_size")
        is_canvas = bool(_graph_meta.get("canvas_transform")) or (
            _img_size is not None
            and [int(_img_size[0]), int(_img_size[1])] == [1080, 1920]
        )

        # ===== 4. Enrich with contours (SAM2 or legacy fallback) =====
        contours_validated_path = diagram_dir / "contours" / "contours_validated.json"
        contours_auto_path = diagram_dir / "contours" / "contours_auto.json"

        contours_path = None
        if is_canvas:
            # WYSIWYG-холст: enrich пропускается целиком. Контуры уже влиты
            # при построении холста (contours_merge) в его координатах; влив
            # здесь работал бы в растре против холста (IoU≈0), а legacy
            # contour_extractor портил бы узлы полигонами в чужой системе.
            logger.info("WYSIWYG: enrich контурами пропущен — холст уже с ними")
        elif contours_validated_path.exists():
            contours_path = contours_validated_path
            logger.info("Using validated contours: %s", contours_path)
        elif contours_auto_path.exists():
            contours_path = contours_auto_path
            logger.info("Using auto contours (not validated): %s", contours_path)

        if contours_path:
            # Merge SAM2 contour polygons into graph nodes by bbox matching.
            # Реализация одна на оба влива (построение холста и этот, растровый
            # путь) — modules/graph/core/contours_merge: только выбранные
            # оператором polygon_validated, только equipment, IoU > 0.5.
            try:
                from modules.graph.core.contours_merge import (
                    load_validated_contours, merge_validated_contours,
                )
                contour_nodes = load_validated_contours(contours_path)
                merged = merge_validated_contours(graph_data, contour_nodes)
                logger.info(
                    "Merged %d validated contour polygons into graph "
                    "(%d selected by operator); остальные узлы — по bbox",
                    merged, len(contour_nodes),
                )
            except Exception as e:
                logger.warning("Contour merge failed (non-fatal): %s", e, exc_info=True)
        elif not is_canvas:
            # Legacy fallback: old contour_extractor (if no SAM2 contours)
            original_image_path = diagram_dir / "original" / "image.png"
            if not original_image_path.exists():
                for ext in ('.jpg', '.jpeg', '.tif', '.tiff'):
                    alt = original_image_path.with_suffix(ext)
                    if alt.exists():
                        original_image_path = alt
                        break

            pipe_mask_path = diagram_dir / "segmentation" / "pipe_mask_refined.png"
            if not pipe_mask_path.exists():
                pipe_mask_path = diagram_dir / "segmentation" / "pipe_mask_validated.png"
            if not pipe_mask_path.exists():
                pipe_mask_path = diagram_dir / "segmentation" / "pipe_mask.png"

            if original_image_path.exists() and pipe_mask_path.exists():
                try:
                    from modules.contour_extractor import enrich_graph_with_contours
                    from modules.graph_to_fxml import CLASS_NAME_TO_SKIN
                    enrich_graph_with_contours(
                        graph_data,
                        image_path=str(original_image_path),
                        pipe_mask_path=str(pipe_mask_path),
                        skin_mapped_classes=set(CLASS_NAME_TO_SKIN.keys()),
                    )
                except Exception as e:
                    logger.warning("Legacy contour extraction failed (non-fatal): %s", e, exc_info=True)
            else:
                logger.info(
                    "Skipping contour extraction: no SAM2 contours and no image/mask for legacy",
                )

        # ===== 5. Generate FXML =====
        # '1920x1080' — экранный лист: генерируем в пикселях, затем стандартизируем.
        STD_1920 = "1920x1080"
        with obs.step("compute", logger):
            if is_canvas:
                # WYSIWYG: 1:1-сериализатор холста (canvas_to_fxml) — identity,
                # без масштаба и без fxml_standardize (двойной масштаб и
                # повторная посадка горловин ломали паритет с редактором).
                logger.info("WYSIWYG: identity-экспорт холста (canvas_to_fxml)")
                gen_kwargs = {}
                if bridge_gap is not None:
                    gen_kwargs["bridge_gap_factor"] = bridge_gap
                fxml_content = generate_canvas_fxml(graph_data, **gen_kwargs)
            else:
                gen_page_size = None if page_size == STD_1920 else page_size
                page_info = (f" (page: {page_size})" if page_size
                             else " (original pixels)")
                logger.info("Generating FXML%s...", page_info)
                gen_kwargs = {"page_size": gen_page_size}
                if bridge_gap is not None:
                    gen_kwargs["bridge_gap_factor"] = bridge_gap
                fxml_content = generate_fxml(graph_data, **gen_kwargs)

        # Экранный лист 1920x1080: привести к стандарту + убрать смещение скинов,
        # датчиков и невидимые разрывы мостов (tools/fxml_standardize.py). Остальные
        # размеры (оригинал / A4-A0) остаются как есть. Ошибка стандартизации не
        # фатальна — пишем сырой FXML.
        if page_size == STD_1920 and not is_canvas:
            try:
                from pathlib import Path as _Path
                from tools.fxml_standardize import standardize_xml, load_geo
                _geo_path = _Path(__file__).resolve().parents[2] / "tools" / "skin_geometry.json"
                fxml_content = standardize_xml(
                    fxml_content, geo=load_geo(str(_geo_path)), mode="letterbox",
                    pad_top=100.0, pad_bottom=50.0,   # свободные полосы под подписи (фон, не сущность)
                )
                logger.info("FXML standardized to 1920x1080")
            except Exception as exc:
                logger.error(
                    "1920x1080 standardization failed, writing raw FXML: %s",
                    exc, exc_info=True,
                )

        # ===== 6. Save FXML =====
        with obs.step("persist_artifacts", logger):
            with open(output_fxml_path, 'w', encoding='utf-8') as f:
                f.write(fxml_content)

            fxml_size = output_fxml_path.stat().st_size
            logger.info(
                "FXML generated: %s (%d bytes)",
                output_fxml_path, fxml_size,
            )

            # ===== 7. Register artifact =====
            # Удаляем старый FXML артефакт
            old_fxml = (
                db.query(Artifact)
                .filter(
                    Artifact.diagram_uid == diagram_uid,
                    Artifact.artifact_type == ArtifactType.FXML,
                )
                .first()
            )
            if old_fxml:
                db.delete(old_fxml)
                db.flush()

            # Относительный путь для storage
            rel_path = str(output_fxml_path.relative_to(storage_path))

            artifact = Artifact(
                diagram_uid=diagram_uid,
                artifact_type=ArtifactType.FXML,
                file_path=rel_path,
                file_size=fxml_size,
                mime_type="application/xml",
            )
            db.add(artifact)

        # ===== 8. COMPLETED =====
        diagram.status = DiagramStatus.COMPLETED

        # Статистика
        equipment_count = sum(
            1 for n in graph_data.get('nodes', []) if n.get('type') == 'equipment'
        )
        connector_count = sum(
            1 for n in graph_data.get('nodes', []) if n.get('type') == 'connector'
        )

        complete_stage(stage, {
            "fxml_size": fxml_size,
            "equipment_count": equipment_count,
            "connector_count": connector_count,
            "edges_count": edges_count,
        })
        db.commit()

        logger.info(
            "FXML generation completed for %s: "
            "%d equipment, %d connectors, %d edges, %d bytes",
            diagram_uid,
            equipment_count, connector_count, edges_count, fxml_size,
        )

        return {
            "status": "completed",
            "diagram_uid": diagram_uid,
            "fxml_path": rel_path,
            "fxml_size": fxml_size,
            "equipment_count": equipment_count,
            "connector_count": connector_count,
            "edges_count": edges_count,
        }

    except SoftTimeLimitExceeded:
        logger.error("FXML generation timed out for %s", diagram_uid, exc_info=True)
        fail_stage(stage, "FXML generation timed out", traceback.format_exc())
        set_diagram_error(db, diagram_uid, "FXML generation timed out", "generating_fxml")
        raise

    except Exception as exc:
        # exc_info=True + exc= в fail_stage → error_code/failed_step/traceback
        # доезжают до /stages (DoD §4).
        logger.error("FXML generation failed for %s: %s", diagram_uid, exc, exc_info=True)

        if self.request.retries < self.max_retries:
            persist_failed_attempt(db, stage, str(exc)[:500], traceback.format_exc(), exc=exc)
            logger.info("Retrying (%d/%d) ...", self.request.retries + 1, self.max_retries)
            raise self.retry(exc=exc)

        fail_stage(stage, str(exc)[:500], traceback.format_exc(), exc=exc)
        set_diagram_error(db, diagram_uid, str(exc)[:500], "generating_fxml")
        raise

    finally:
        db.close()

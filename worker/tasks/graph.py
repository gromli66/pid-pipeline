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
from worker.utils.db_helpers import set_diagram_error, check_deleted

logger = logging.getLogger(__name__)


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

    try:
        logger.info("Starting graph building for %s", diagram_uid)

        from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
        from modules.graph.core.builder import GraphBuilder

        # ===== 1. Diagram from DB =====
        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if not diagram:
            raise ValueError(f"Diagram {diagram_uid} not found")

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

        # ===== 2. Paths =====
        storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
        diagram_dir = storage_path / str(diagram_uid)

        # --- Input files ---

        # Skeleton final (из task_skeletonize_simple)
        skeleton_final_path = diagram_dir / "skeleton" / "skeleton_final.png"
        if not skeleton_final_path.exists():
            raise FileNotFoundError(
                f"Skeleton final not found: {skeleton_final_path}. "
                f"task_skeletonize_simple may not have completed."
            )

        # Equipment mask (node_mask from segmentation)
        node_mask_path = diagram_dir / "segmentation" / "node_mask.png"
        if not node_mask_path.exists():
            raise FileNotFoundError(f"Node mask not found: {node_mask_path}")

        # Junction mask (validated)
        junction_mask_path = diagram_dir / "junction" / "junction_mask_validated.png"
        if not junction_mask_path.exists():
            raise FileNotFoundError(
                f"Junction mask validated not found: {junction_mask_path}"
            )

        # Bridge mask (validated)
        bridge_mask_path = diagram_dir / "junction" / "bridge_mask_validated.png"
        if not bridge_mask_path.exists():
            raise FileNotFoundError(
                f"Bridge mask validated not found: {bridge_mask_path}"
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
        builder.save(
            result=result,
            output_dir=str(graph_dir),
            graph_path=str(graph_json_path),
            scheme_name="graph",
        )

        # builder.save() creates {scheme_name}_graph.png → graph_graph.png
        # Rename to our standard name
        auto_viz_path = graph_dir / "graph_graph.png"
        if auto_viz_path.exists():
            auto_viz_path.replace(graph_overlay_path)

        logger.info(
            "Saved: graph.json (%d bytes), graph_overlay.png",
            graph_json_path.stat().st_size if graph_json_path.exists() else 0,
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
        if graph_overlay_path.exists():
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
        logger.error("Graph building timed out for %s", diagram_uid)
        set_diagram_error(db, diagram_uid, "Graph building timed out (29 min limit)", "building_graph")
        raise

    except Exception as exc:
        logger.error("Graph building failed for %s: %s", diagram_uid, exc)
        logger.debug(traceback.format_exc())

        if self.request.retries < self.max_retries:
            logger.info("Retrying (%d/%d) ...", self.request.retries + 1, self.max_retries)
            raise self.retry(exc=exc)

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
def task_generate_fxml(self, diagram_uid: str, page_size: str = None):
    """
    Генерация FXML из валидированного графа.

    Входные артефакты:
        - GRAPH_VALIDATED:  graph/graph_validated.json
          (fallback: GRAPH_JSON → graph/graph.json)

    Выходные артефакты:
        - FXML:  fxml/diagram.fxml

    Args:
        diagram_uid: UUID диаграммы
        page_size: 'A4', 'A3', 'A2', 'A1', 'A0' или None (пиксельные координаты)

    Статус:  GENERATING_FXML → COMPLETED
    """
    from app.db.session import SessionLocal

    db = SessionLocal()

    try:
        logger.info("Starting FXML generation for %s", diagram_uid)

        from app.models import Diagram, DiagramStatus, Artifact, ArtifactType
        from modules.graph_to_fxml import generate_fxml

        # ===== 1. Diagram from DB =====
        diagram = db.query(Diagram).filter(Diagram.uid == diagram_uid).first()
        if not diagram:
            raise ValueError(f"Diagram {diagram_uid} not found")

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
        db.commit()

        # ===== 2. Paths =====
        storage_path = Path(os.getenv("STORAGE_PATH", "./storage/diagrams"))
        diagram_dir = storage_path / str(diagram_uid)

        # --- Input: graph JSON ---
        # Приоритет: graph_validated.json > graph.json
        graph_validated_path = diagram_dir / "graph" / "graph_validated.json"
        graph_json_path = diagram_dir / "graph" / "graph.json"

        if graph_validated_path.exists():
            input_graph_path = graph_validated_path
            logger.info("Using validated graph: %s", input_graph_path)
        elif graph_json_path.exists():
            input_graph_path = graph_json_path
            logger.info("Using original graph (no validated version): %s", input_graph_path)
        else:
            raise FileNotFoundError(
                f"No graph JSON found. Checked:\n"
                f"  {graph_validated_path}\n"
                f"  {graph_json_path}"
            )

        # --- Output dir ---
        fxml_dir = diagram_dir / "fxml"
        fxml_dir.mkdir(parents=True, exist_ok=True)
        output_fxml_path = fxml_dir / "diagram.fxml"

        # ===== 3. Load graph =====
        import json
        with open(input_graph_path, 'r', encoding='utf-8') as f:
            graph_data = json.load(f)

        nodes_count = len(graph_data.get('nodes', []))
        edges_count = len(graph_data.get('links', []))
        logger.info(
            "Loaded graph: %d nodes, %d edges",
            nodes_count, edges_count,
        )

        # ===== 4. Enrich with contours =====
        # Извлечь контуры для узлов без скинов (drossel, voronka, etc.)
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
                logger.warning("Contour extraction failed (non-fatal): %s", e)
        else:
            logger.info(
                "Skipping contour extraction: image=%s, mask=%s",
                original_image_path.exists(), pipe_mask_path.exists(),
            )

        # ===== 5. Generate FXML =====
        page_info = f" (page: {page_size})" if page_size else " (original pixels)"
        logger.info("Generating FXML%s...", page_info)
        fxml_content = generate_fxml(graph_data, page_size=page_size)

        # ===== 6. Save FXML =====
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
        db.commit()

        # Статистика
        equipment_count = sum(
            1 for n in graph_data.get('nodes', []) if n.get('type') == 'equipment'
        )
        connector_count = sum(
            1 for n in graph_data.get('nodes', []) if n.get('type') == 'connector'
        )

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
        logger.error("FXML generation timed out for %s", diagram_uid)
        set_diagram_error(db, diagram_uid, "FXML generation timed out", "generating_fxml")
        raise

    except Exception as exc:
        logger.error("FXML generation failed for %s: %s", diagram_uid, exc)
        logger.debug(traceback.format_exc())

        if self.request.retries < self.max_retries:
            logger.info("Retrying (%d/%d) ...", self.request.retries + 1, self.max_retries)
            raise self.retry(exc=exc)

        set_diagram_error(db, diagram_uid, str(exc)[:500], "generating_fxml")
        raise

    finally:
        db.close()

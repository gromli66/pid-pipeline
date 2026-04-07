"""
modules/binding/engine.py — Единый binding engine.

Заменяет: kks_binding/binder.py (KksBinder) + text_binding/binder.py (TextBinder).
Один движок для node-привязки (KKS→equipment) и edge-привязки (диаметры→трубы).
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from modules.ocr.domain_profile import CodeMatch, DiamMatch
from .config import DomainBindingConfig
from .matcher import UnifiedMatcher
from .validator import BindingValidator, ValidationResult

logger = logging.getLogger(__name__)


# ── Результаты привязки ──────────────────────────

@dataclass
class NodeBinding:
    """Привязка кода → node."""
    ocr_block_idx: int
    node_id: str
    node_class: str
    code_match: CodeMatch
    distance: float
    validation: ValidationResult = field(default_factory=lambda: ValidationResult(is_valid=True))


@dataclass
class EdgeBinding:
    """Привязка диаметра → edge."""
    ocr_block_idx: int
    edge_idx: int
    edge_id: str
    edge_key: str             # "source|target"
    diam_match: DiamMatch
    distance: float


@dataclass
class BindingReport:
    """Полный результат привязки."""
    node_bindings: list[NodeBinding] = field(default_factory=list)
    edge_bindings: list[EdgeBinding] = field(default_factory=list)
    # Статистика
    total_ocr_blocks: int = 0
    equipment_matched: int = 0
    diameter_matched: int = 0
    nodes_bound: int = 0
    edges_bound: int = 0
    skipped_edge_unit: int = 0
    skipped_no_target: int = 0
    unit_mismatches: int = 0
    reclassified: int = 0


# ── Engine ───────────────────────────────────────

class UnifiedBinder:
    """
    Единый binding engine.

    Для каждого OCR-блока:
    1. Пробует match_equipment → если match и unit не в edge_binding_units → bind to node
    2. Пробует match_diameter → bind to edge
    3. Если equipment match и unit в edge_binding_units → bind to edge (pipeline KKS)
    """

    def __init__(self, config: DomainBindingConfig):
        self._config = config
        self._matcher = UnifiedMatcher(config)
        self._validator = BindingValidator(config)
        self._edge_units = set(config.edge_binding_units)

    def bind(
        self,
        ocr_blocks: list[dict],
        graph_nodes: list[dict],
        graph_edges: list[dict],
    ) -> BindingReport:
        """
        Привязать все OCR-блоки к элементам графа.

        Args:
            ocr_blocks: [{bbox, text, confidence, ...}]
            graph_nodes: [{id, class_name, bbox, type, ...}]
            graph_edges: [{id, source, target, source_point, target_point, waypoints, ...}]
        """
        report = BindingReport(total_ocr_blocks=len(ocr_blocks))
        equipment_nodes = self._validator.get_bindable_classes(graph_nodes)
        bound_node_ids: set[str] = set()

        # Phase 1: Equipment code → node
        node_candidates: list[tuple[int, CodeMatch]] = []
        diam_candidates: list[tuple[int, DiamMatch]] = []

        for idx, block in enumerate(ocr_blocks):
            text = block.get("text", "").strip()
            if not text:
                continue
            bbox = block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue

            # Try equipment code
            eq = self._matcher.match_equipment(text)
            if eq:
                report.equipment_matched += 1
                if eq.unit in self._edge_units:
                    report.skipped_edge_unit += 1
                    # TODO: edge binding for pipeline codes (BR)
                else:
                    node_candidates.append((idx, eq))
                continue

            # Try diameter
            dm = self._matcher.match_diameter(text)
            if dm:
                report.diameter_matched += 1
                diam_candidates.append((idx, dm))

        # Bind equipment → nearest node
        for idx, eq in node_candidates:
            bbox = ocr_blocks[idx].get("bbox", [0, 0, 0, 0])
            expected_classes = self._validator.get_expected_classes_for_unit(eq.unit)

            best_node = None
            best_dist = self._config.node_max_distance
            best_cls = ""

            for node in equipment_nodes:
                nid = node.get("id", "")
                if nid in bound_node_ids:
                    continue
                node_bbox = node.get("bbox")
                if not node_bbox or len(node_bbox) != 4:
                    continue
                cls_name = node.get("class_name", "")

                dist = _bbox_to_bbox_distance(bbox, node_bbox)
                if dist >= self._config.node_max_distance:
                    continue

                is_expected = cls_name in expected_classes if expected_classes else False
                is_unknow = cls_name == "unknow" and bool(expected_classes)
                if not (is_expected or is_unknow):
                    continue

                if dist < best_dist:
                    best_dist = dist
                    best_node = node
                    best_cls = cls_name

            if best_node is None:
                report.skipped_no_target += 1
                continue

            nid = best_node.get("id", "")
            bound_node_ids.add(nid)

            validation = self._validator.validate(eq.unit, best_cls)
            if not validation.is_valid:
                report.unit_mismatches += 1
            if validation.reclassify_to:
                report.reclassified += 1

            report.node_bindings.append(NodeBinding(
                ocr_block_idx=idx,
                node_id=nid,
                node_class=best_cls,
                code_match=eq,
                distance=round(best_dist, 1),
                validation=validation,
            ))
            report.nodes_bound += 1

        # Phase 2: Diameter → nearest edge
        from modules.binding.geometry import (
            bbox_to_edge_distance_ex,
            bbox_to_edge_distance_weighted,
            edge_orientation,
        )

        edge_best: dict[int, tuple[EdgeBinding, float]] = {}
        for idx, dm in diam_candidates:
            bbox = ocr_blocks[idx].get("bbox", [0, 0, 0, 0])
            bw = bbox[2] - bbox[0]
            bh = bbox[3] - bbox[1]
            bbox_orient = "H" if bw >= bh else "V"

            best_edge_idx = None
            best_dist = self._config.edge_max_distance
            best_edge = None
            best_on_segment = False
            best_orient_match = False

            for edge_idx, edge in enumerate(graph_edges):
                d_raw, on_seg = bbox_to_edge_distance_ex(bbox, edge)
                if d_raw is None:
                    continue
                d_eff = d_raw if on_seg else bbox_to_edge_distance_weighted(bbox, edge)
                if d_eff is None or d_eff >= self._config.edge_max_distance:
                    continue

                orient_match = (edge_orientation(edge) == bbox_orient)

                if on_seg and not best_on_segment:
                    best_dist, best_edge_idx, best_edge = d_eff, edge_idx, edge
                    best_on_segment, best_orient_match = True, orient_match
                elif on_seg == best_on_segment:
                    if d_eff < best_dist:
                        best_dist, best_edge_idx, best_edge = d_eff, edge_idx, edge
                        best_orient_match = orient_match
                    elif abs(d_eff - best_dist) < 5.0 and orient_match and not best_orient_match:
                        best_dist, best_edge_idx, best_edge = d_eff, edge_idx, edge
                        best_orient_match = orient_match

            if best_edge_idx is not None:
                src = best_edge.get("source", "")
                tgt = best_edge.get("target", "")
                binding = EdgeBinding(
                    ocr_block_idx=idx,
                    edge_idx=best_edge_idx,
                    edge_id=best_edge.get("id", ""),
                    edge_key=f"{src}|{tgt}",
                    diam_match=dm,
                    distance=round(best_dist, 1),
                )
                if best_edge_idx not in edge_best or best_dist < edge_best[best_edge_idx][1]:
                    edge_best[best_edge_idx] = (binding, best_dist)

        report.edge_bindings = [b for b, _ in edge_best.values()]
        report.edges_bound = len(report.edge_bindings)

        logger.info(
            "Unified binding: %d OCR → %d equipment, %d diameter → "
            "%d nodes, %d edges bound",
            report.total_ocr_blocks, report.equipment_matched,
            report.diameter_matched, report.nodes_bound, report.edges_bound,
        )
        return report


# ── Geometry helper ──────────────────────────────

def _bbox_to_bbox_distance(bbox_a: list, bbox_b: list) -> float:
    """Минимальное расстояние между границами двух bbox."""
    ax1, ay1, ax2, ay2 = bbox_a
    bx1, by1, bx2, by2 = bbox_b
    dx = max(0, max(ax1, bx1) - min(ax2, bx2)) if ax2 < bx1 or bx2 < ax1 else 0
    dy = max(0, max(ay1, by1) - min(ay2, by2)) if ay2 < by1 or by2 < ay1 else 0
    if ax2 < bx1:
        dx = bx1 - ax2
    elif bx2 < ax1:
        dx = ax1 - bx2
    else:
        dx = 0
    if ay2 < by1:
        dy = by1 - ay2
    elif by2 < ay1:
        dy = ay1 - by2
    else:
        dy = 0
    return math.sqrt(dx * dx + dy * dy)

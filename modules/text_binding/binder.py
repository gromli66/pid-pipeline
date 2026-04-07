"""
TextBinder — автоматическая привязка OCR-диаметров к рёбрам графа.

Алгоритм:
1. Для каждого OCR-блока:
   a. Проверить: bbox рядом с каким-то ребром? (distance < max_distance)
   b. Попробовать diameter regex (raw → corrections → compact)
   c. Если match → привязать к ближайшему ребру
2. Если несколько OCR-блоков претендуют на одно ребро → выбрать ближайший
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from .config import TextRecognitionConfig
from .matcher import DiameterMatcher, DiameterMatch
from .geometry import bbox_to_edge_distance_weighted, bbox_to_edge_distance_ex, edge_midpoint, edge_orientation

logger = logging.getLogger(__name__)


@dataclass
class DiameterBinding:
    """Результат привязки одного диаметра к ребру."""
    ocr_block_idx: int
    edge_idx: int          # индекс в списке edges
    edge_id: str           # id ребра
    edge_key: str          # "source|target"
    text: str              # распознанный текст "Dy50"
    prefix: str            # "Dy"
    diameter: int           # 50
    suffix: str            # ""
    confidence: float      # уверенность match
    distance: float        # расстояние bbox → edge


@dataclass
class BindingReport:
    """Результат auto-bind: список привязок + статистика."""
    bindings: list[DiameterBinding] = field(default_factory=list)
    total_ocr_blocks: int = 0
    diameter_candidates: int = 0
    bound_count: int = 0


@dataclass
class PropagatedDiameter:
    """Диаметр распространённый на ребро (не из OCR, а по правилу потока)."""
    edge_idx: int
    edge_id: str
    edge_key: str
    text: str          # "Dy150"
    prefix: str
    diameter: int
    suffix: str
    source_edge_idx: int   # откуда пришёл диаметр (OCR-привязка)
    confidence: float      # ниже чем у OCR-привязки


@dataclass
class ConflictEdge:
    """Ребро с конфликтом — несколько диаметров претендуют."""
    edge_idx: int
    edge_id: str
    edge_key: str
    candidates: list[int]  # список диаметров-претендентов


@dataclass
class PropagationReport:
    """Результат распространения диаметров."""
    propagated: list[PropagatedDiameter] = field(default_factory=list)
    conflicts: list[ConflictEdge] = field(default_factory=list)
    total_edges: int = 0
    edges_with_ocr_diameter: int = 0
    edges_after_propagation: int = 0


class TextBinder:
    """Автоматическая привязка текста к элементам графа."""

    def __init__(self, config: TextRecognitionConfig):
        self._config = config
        self._diameter_matcher = DiameterMatcher(config.diameter)

    def propagate_diameters(
        self,
        graph_nodes: list[dict],
        graph_edges: list[dict],
        bindings: list[DiameterBinding],
    ) -> PropagationReport:
        """
        Распространить диаметры по трубам.

        Правила:
        1. Диаметр проходит через коннектор (degree=2) без изменений
        2. Диаметр проходит через equipment без изменений (любой degree)
        3. На Т-образном коннекторе (degree=3+) — только в том же направлении (H→H, V→V)
        4. Останавливается если ребро уже имеет ДРУГОЙ диаметр
        5. Переход (perehod) — полный стоп
        """
        report = PropagationReport(total_edges=len(graph_edges))

        # Построить adjacency: node_id → [(edge_idx, other_node_id)]
        node_edges: dict[str, list[tuple[int, str]]] = {}
        for idx, edge in enumerate(graph_edges):
            src, tgt = edge.get("source"), edge.get("target")
            if not src or not tgt:
                continue
            node_edges.setdefault(src, []).append((idx, tgt))
            node_edges.setdefault(tgt, []).append((idx, src))

        # Node type and class lookup
        node_type: dict[str, str] = {}
        node_class: dict[str, str] = {}
        for n in graph_nodes:
            nid = n.get("id", "")
            node_type[nid] = n.get("type", "unknown")
            node_class[nid] = n.get("class_name", "")

        # Диаметр на рёбрах из OCR-привязок
        edge_diameter: dict[int, DiameterBinding] = {}
        for b in bindings:
            edge_diameter[b.edge_idx] = b
        report.edges_with_ocr_diameter = len(edge_diameter)

        # Ориентация рёбер (кэш)
        edge_orient_cache: dict[int, Optional[str]] = {}
        def get_orient(eidx: int) -> Optional[str]:
            if eidx not in edge_orient_cache:
                edge_orient_cache[eidx] = edge_orientation(graph_edges[eidx])
            return edge_orient_cache[eidx]

        # BFS распространение от каждого ребра с диаметром
        # Собираем ВСЕ претенденты на каждое ребро
        edge_candidates: dict[int, set[int]] = {}  # edge_idx → set of diameters
        propagated: dict[int, PropagatedDiameter] = {}

        for seed_binding in bindings:
            seed_idx = seed_binding.edge_idx
            seed_edge = graph_edges[seed_idx]
            seed_orient = get_orient(seed_idx)

            queue: list[tuple[int, Optional[str]]] = [(seed_idx, seed_orient)]
            seen_in_bfs: set[int] = {seed_idx}

            while queue:
                cur_edge_idx, flow_orient = queue.pop(0)
                cur_edge = graph_edges[cur_edge_idx]

                for node_id in (cur_edge.get("source"), cur_edge.get("target")):
                    if not node_id:
                        continue

                    neighbors = node_edges.get(node_id, [])
                    ntype_val = node_type.get(node_id, "unknown")
                    degree = len(neighbors)

                    for next_edge_idx, next_node_id in neighbors:
                        if next_edge_idx == cur_edge_idx:
                            continue
                        if next_edge_idx in seen_in_bfs:
                            continue

                        # Уже имеет OCR-диаметр
                        if next_edge_idx in edge_diameter:
                            existing = edge_diameter[next_edge_idx]
                            if existing.diameter != seed_binding.diameter:
                                continue
                            else:
                                seen_in_bfs.add(next_edge_idx)
                                next_orient = get_orient(next_edge_idx)
                                queue.append((next_edge_idx, next_orient))
                                continue

                        next_orient = get_orient(next_edge_idx)

                        # Переход (perehod) — стоп
                        nclass = node_class.get(node_id, "")
                        if nclass == "perehod":
                            continue

                        # Equipment — проходим свободно при любом degree
                        if ntype_val == "equipment":
                            pass
                        elif degree <= 2:
                            pass
                        else:
                            if flow_orient and next_orient and flow_orient != next_orient:
                                continue

                        # Записать претендента
                        seen_in_bfs.add(next_edge_idx)
                        edge_candidates.setdefault(next_edge_idx, set()).add(seed_binding.diameter)

                        # Если нет конфликта — propagate
                        if next_edge_idx not in edge_diameter:
                            if next_edge_idx not in propagated:
                                next_edge = graph_edges[next_edge_idx]
                                src, tgt = next_edge.get("source", ""), next_edge.get("target", "")
                                propagated[next_edge_idx] = PropagatedDiameter(
                                    edge_idx=next_edge_idx,
                                    edge_id=next_edge.get("id", ""),
                                    edge_key=f"{src}|{tgt}",
                                    text=seed_binding.text,
                                    prefix=seed_binding.prefix,
                                    diameter=seed_binding.diameter,
                                    suffix=seed_binding.suffix,
                                    source_edge_idx=seed_binding.edge_idx,
                                    confidence=seed_binding.confidence * 0.8,
                                )
                            elif propagated[next_edge_idx].diameter != seed_binding.diameter:
                                # Конфликт — другой seed хочет другой диаметр
                                pass  # записан в edge_candidates
                        queue.append((next_edge_idx, next_orient))

        # Найти конфликты — рёбра с >1 претендентом
        conflicts: list[ConflictEdge] = []
        conflict_edge_idxs: set[int] = set()
        for eidx, diams in edge_candidates.items():
            if len(diams) > 1:
                edge = graph_edges[eidx]
                src, tgt = edge.get("source", ""), edge.get("target", "")
                conflicts.append(ConflictEdge(
                    edge_idx=eidx,
                    edge_id=edge.get("id", ""),
                    edge_key=f"{src}|{tgt}",
                    candidates=sorted(diams),
                ))
                conflict_edge_idxs.add(eidx)
                # Убрать из propagated — оператор должен решить
                propagated.pop(eidx, None)

        report.propagated = list(propagated.values())
        report.conflicts = conflicts
        report.edges_after_propagation = len(edge_diameter) + len(propagated)
        logger.info(
            "Diameter propagation: %d OCR-bound + %d propagated = %d / %d edges",
            report.edges_with_ocr_diameter,
            len(propagated),
            report.edges_after_propagation,
            report.total_edges,
        )
        return report

    def bind_diameters(
        self,
        ocr_blocks: list[dict],
        graph_edges: list[dict],
    ) -> BindingReport:
        """
        Привязать диаметры из OCR к рёбрам графа.

        Args:
            ocr_blocks: список OCR-блоков [{bbox, text, confidence, ...}]
            graph_edges: список рёбер [{id, source, target, source_point, target_point, waypoints, ...}]

        Returns:
            BindingReport с привязками
        """
        report = BindingReport(total_ocr_blocks=len(ocr_blocks))
        max_dist = self._config.diameter.max_distance

        # Для каждого OCR-блока: попробовать match + найти ближайшее ребро
        candidates: list[tuple[DiameterBinding, float]] = []  # (binding, distance)

        for block_idx, block in enumerate(ocr_blocks):
            if block.get("merged_into") is not None:
                continue
            text = block.get("text", "").strip()
            if not text:
                continue
            bbox = block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue

            # Попробовать распознать диаметр
            dm = self._diameter_matcher.match(text)
            if dm is None:
                continue

            report.diameter_candidates += 1

            # Ориентация bbox: H если шире чем выше, V если выше чем шире
            bw = bbox[2] - bbox[0]
            bh = bbox[3] - bbox[1]
            bbox_orient = "H" if bw >= bh else "V"

            # Найти ближайшее ребро (приоритет: on_segment > distance,
            # orient_match — только тай-брейкер при близких расстояниях)
            best_edge_idx = None
            best_dist = max_dist
            best_edge = None
            best_on_segment = False
            best_orient_match = False

            for edge_idx, edge in enumerate(graph_edges):
                # Raw distance + on_segment check
                d_raw, on_seg = bbox_to_edge_distance_ex(bbox, edge)
                if d_raw is None:
                    continue

                # on_segment: текст прямо над/под ребром — использовать raw distance
                # off_segment: ребро сбоку — использовать weighted (с penalties)
                if on_seg:
                    d_eff = d_raw
                else:
                    d_eff = bbox_to_edge_distance_weighted(bbox, edge)
                    if d_eff is None:
                        continue

                if d_eff >= max_dist:
                    continue

                # Совпадение ориентации bbox и ребра
                orient_match = (edge_orientation(edge) == bbox_orient)

                # Приоритет: on_segment > distance > orient_match (тай-брейкер)
                if on_seg and not best_on_segment:
                    best_dist, best_edge_idx, best_edge = d_eff, edge_idx, edge
                    best_on_segment, best_orient_match = True, orient_match
                elif on_seg == best_on_segment:
                    if d_eff < best_dist:
                        best_dist, best_edge_idx, best_edge = d_eff, edge_idx, edge
                        best_orient_match = orient_match
                    elif abs(d_eff - best_dist) < 5.0 and orient_match and not best_orient_match:
                        # Тай-брейкер: при почти равных расстояниях (<5px)
                        # предпочесть совпадение ориентации
                        best_dist, best_edge_idx, best_edge = d_eff, edge_idx, edge
                        best_orient_match = orient_match

            if best_edge_idx is not None:
                edge_id = best_edge.get("id", "")
                src, tgt = best_edge.get("source", ""), best_edge.get("target", "")
                binding = DiameterBinding(
                    ocr_block_idx=block_idx,
                    edge_idx=best_edge_idx,
                    edge_id=edge_id,
                    edge_key=f"{src}|{tgt}",
                    text=dm.text,
                    prefix=dm.prefix,
                    diameter=dm.diameter,
                    suffix=dm.suffix,
                    confidence=dm.confidence,
                    distance=round(best_dist, 1),
                )
                candidates.append((binding, best_dist))

        # Разрешить конфликты: если несколько OCR → одно ребро, выбрать ближайший
        edge_best: dict[int, tuple[DiameterBinding, float]] = {}
        for binding, dist in candidates:
            eidx = binding.edge_idx
            if eidx not in edge_best or dist < edge_best[eidx][1]:
                edge_best[eidx] = (binding, dist)

        report.bindings = [b for b, _ in edge_best.values()]
        report.bound_count = len(report.bindings)

        logger.info(
            "Diameter binding: %d OCR blocks → %d candidates → %d bound",
            report.total_ocr_blocks,
            report.diameter_candidates,
            report.bound_count,
        )
        return report

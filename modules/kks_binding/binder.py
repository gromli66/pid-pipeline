"""
KksBinder — привязка KKS к узлам графа.

Правило: 1 confirmed KKS → 1 ближайший equipment node.
BR (трубопровод) — пропускаем, не привязываем.

После привязки проверяем unit ↔ class_name:
- Допустим ли unit для class_name узла?
- Если class_name == "unknow" → reclassify по unit.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from .config import KksConfig, ClassToKksConfig
from modules.text_binding.geometry import bbox_center
from modules.ocr_validation.result import (
    BlockClassification, BlockType, ConfirmStatus,
)

logger = logging.getLogger(__name__)


@dataclass
class KksBinding:
    """Одна привязка KKS → node."""
    ocr_block_idx: int
    node_id: str
    node_class: str
    kks_full: str
    kks_block: str
    kks_system: str
    kks_fn: str
    kks_unit: str
    kks_num: str
    kks_suffix: str
    confidence: float
    distance: float
    unit_valid: bool = True
    validation_msg: str = ""
    reclassify_to: Optional[str] = None


@dataclass
class KksBindingReport:
    """Результат авто-привязки KKS."""
    bindings: list[KksBinding] = field(default_factory=list)
    total_kks: int = 0
    bound: int = 0
    skipped_br: int = 0
    skipped_no_node: int = 0
    unit_mismatches: int = 0
    reclassified: int = 0


class KksBinder:
    """Автоматическая привязка KKS к equipment nodes."""

    def __init__(self, kks_cfg: KksConfig, cls_cfg: ClassToKksConfig):
        self._kks_cfg = kks_cfg
        self._cls_cfg = cls_cfg
        self._max_dist = 30.0  # bbox→bbox, жёсткий порог
        self._edge_units = set(kks_cfg.edge_binding_units)  # {"BR"}

    def bind(
        self,
        classifications: list[BlockClassification],
        ocr_blocks: list[dict],
        graph_nodes: list[dict],
    ) -> KksBindingReport:
        """
        Привязать confirmed KKS-блоки к ближайшим equipment nodes.

        Правила:
        - Только confirmed блоки с type=KKS участвуют
        - BR (трубопровод) пропускается
        - 1 KKS → 1 node (жадный по расстоянию, без дублей)
        - unit ↔ class проверяется после привязки
        - unknow → reclassify по unit
        """
        report = KksBindingReport()

        # Собрать equipment nodes (не connector, kks_target != "none")
        equipment = self._get_bindable_nodes(graph_nodes)

        # Собрать confirmed KKS (без BR)
        kks_blocks = []
        for cl in classifications:
            if cl.block_type != BlockType.KKS:
                continue
            if cl.confirm_status != ConfirmStatus.CONFIRMED:
                continue
            if cl.kks_unit and cl.kks_unit in self._edge_units:
                report.skipped_br += 1
                continue
            kks_blocks.append(cl)

        report.total_kks = len(kks_blocks)

        # Привязка: KKS → ближайший узел ПОДХОДЯЩЕГО класса (по bbox→bbox)
        bound_nodes: set[str] = set()

        for cl in kks_blocks:
            if cl.block_idx >= len(ocr_blocks):
                continue
            ocr_bbox = ocr_blocks[cl.block_idx].get("bbox", [0, 0, 0, 0])

            # Определить допустимые классы для unit
            expected_classes = self._get_expected_classes(cl.kks_unit)

            best_node = None
            best_dist = self._max_dist
            best_cls = ""

            for node in equipment:
                nid = node.get("id", "")
                if nid in bound_nodes:
                    continue
                node_bbox = node.get("bbox")
                if not node_bbox or len(node_bbox) != 4:
                    continue
                cls_name = node.get("class_name", "")

                # Расстояние bbox→bbox (граница OCR → граница узла)
                dist = _bbox_to_bbox_distance(ocr_bbox, node_bbox)
                if dist >= self._max_dist:
                    continue

                # Только совместимые: точное совпадение класса или unknow
                # Если unit неизвестен (нет в unit_to_classes) — не привязывать
                is_expected = cls_name in expected_classes if expected_classes else False
                is_unknow = cls_name == "unknow" and bool(expected_classes)
                if not (is_expected or is_unknow):
                    continue

                if dist < best_dist:
                    best_dist = dist
                    best_node = node
                    best_cls = cls_name

            if best_node is None:
                report.skipped_no_node += 1
                continue

            nid = best_node.get("id", "")
            bound_nodes.add(nid)

            # Проверить unit ↔ class
            unit_valid, msg, reclassify = self._check_unit_class(
                cl.kks_unit, best_cls
            )
            if not unit_valid:
                report.unit_mismatches += 1
            if reclassify:
                report.reclassified += 1

            report.bindings.append(KksBinding(
                ocr_block_idx=cl.block_idx,
                node_id=nid,
                node_class=best_cls,
                kks_full=cl.kks_full or "",
                kks_block=cl.kks_block or "",
                kks_system=cl.kks_system or "",
                kks_fn=cl.kks_fn or "",
                kks_unit=cl.kks_unit or "",
                kks_num=cl.kks_num or "",
                kks_suffix=cl.kks_suffix or "",
                confidence=1.0,
                distance=round(best_dist, 1),
                unit_valid=unit_valid,
                validation_msg=msg,
                reclassify_to=reclassify,
            ))
            report.bound += 1

        logger.info(
            "KKS binding: %d total → %d bound, %d BR skipped, "
            "%d no node, %d unit mismatch, %d reclassified",
            report.total_kks, report.bound, report.skipped_br,
            report.skipped_no_node, report.unit_mismatches,
            report.reclassified,
        )
        return report

    def _get_bindable_nodes(self, graph_nodes: list[dict]) -> list[dict]:
        """Equipment nodes, пригодные для привязки KKS."""
        result = []
        for node in graph_nodes:
            if node.get("type") == "connector":
                continue
            cls_name = node.get("class_name", "")
            rule = self._cls_cfg.class_to_kks.get(cls_name)
            if rule and rule.kks_target == "none":
                continue
            result.append(node)
        return result

    def _get_expected_classes(self, unit: str) -> list[str]:
        """Получить список классов, допустимых для данного unit code."""
        if not unit:
            return []
        return self._cls_cfg.unit_to_classes.get(unit, [])

    def _check_unit_class(
        self, unit: str, class_name: str
    ) -> tuple[bool, str, Optional[str]]:
        """
        unit допустим для class_name?

        Returns:
            (is_valid, message, reclassify_to)
        """
        if not unit or not class_name:
            return True, "", None

        rule = self._cls_cfg.class_to_kks.get(class_name)
        if not rule:
            return True, "", None

        # unknow → reclassify
        if rule.kks_target == "reclassify":
            new_class = (rule.reclassify_rules or {}).get(unit)
            if new_class:
                return True, f"{class_name} → {new_class}", new_class
            return True, "", None

        # Обычная проверка
        if unit in rule.expected_units:
            return True, "", None

        allowed = self._cls_cfg.unit_to_classes.get(unit, [])
        return (
            False,
            f"Unit {unit} не ожидается для {class_name} "
            f"(допустимые классы: {allowed})",
            None,
        )


def _bbox_to_bbox_distance(bbox_a: list, bbox_b: list) -> float:
    """
    Минимальное расстояние между границами двух bbox.

    Возвращает 0 если пересекаются, иначе — минимальное расстояние
    между ближайшими точками границ.
    """
    ax1, ay1, ax2, ay2 = bbox_a[0], bbox_a[1], bbox_a[2], bbox_a[3]
    bx1, by1, bx2, by2 = bbox_b[0], bbox_b[1], bbox_b[2], bbox_b[3]

    # Расстояние по X
    if ax2 < bx1:
        dx = bx1 - ax2
    elif bx2 < ax1:
        dx = ax1 - bx2
    else:
        dx = 0  # пересекаются по X

    # Расстояние по Y
    if ay2 < by1:
        dy = by1 - ay2
    elif by2 < ay1:
        dy = ay1 - by2
    else:
        dy = 0  # пересекаются по Y

    return (dx * dx + dy * dy) ** 0.5

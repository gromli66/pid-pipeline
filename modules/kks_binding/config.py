"""
KKS Config — загрузка kks_config.yaml и class_to_kks_config.yaml.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


# ─── kks_config.yaml ─────────────────────────────────────────────


@dataclass
class KksPattern:
    name: str
    regex: str
    flags: str = ""


@dataclass
class EquipmentUnitInfo:
    code: str               # "AA"
    description: str        # "Valves"
    category: str           # "valve"
    ranges: list[dict] = field(default_factory=list)


@dataclass
class KksConfig:
    """Конфигурация из kks_config.yaml."""
    patterns: list[KksPattern] = field(default_factory=list)
    ocr_corrections_cyr: dict[str, str] = field(default_factory=dict)
    ocr_corrections_digit: dict[str, str] = field(default_factory=dict)
    equipment_units: dict[str, EquipmentUnitInfo] = field(default_factory=dict)
    known_blocks: list[str] = field(default_factory=list)
    binding_max_distance: float = 200.0
    edge_binding_units: list[str] = field(default_factory=list)

    @property
    def known_units(self) -> set[str]:
        """Множество известных unit codes — единственный фильтр KKS."""
        return set(self.equipment_units.keys())

    @classmethod
    def from_yaml(cls, path: str | Path) -> "KksConfig":
        path = Path(path)
        if not path.exists():
            logger.warning("kks_config.yaml not found: %s", path)
            return cls()
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        kks = data.get("kks", {})
        cfg = cls()

        # Patterns
        for p in kks.get("patterns", []):
            cfg.patterns.append(KksPattern(
                name=p.get("name", ""),
                regex=p.get("regex", ""),
                flags=p.get("flags", ""),
            ))

        # OCR corrections
        corr = kks.get("ocr_corrections", {})
        cfg.ocr_corrections_cyr = corr.get("cyrillic_to_latin", {})
        cfg.ocr_corrections_digit = corr.get("digit_corrections", {})

        # Equipment units
        for code, info in kks.get("equipment_units", {}).items():
            cfg.equipment_units[code] = EquipmentUnitInfo(
                code=code,
                description=info.get("description", ""),
                category=info.get("category", ""),
                ranges=info.get("ranges", []),
            )

        # Blocks
        cfg.known_blocks = [b["id"] for b in kks.get("blocks", [])]

        # Binding
        binding = kks.get("binding", {})
        cfg.binding_max_distance = binding.get("max_distance", 200.0)
        cfg.edge_binding_units = binding.get("edge_binding_units", [])

        return cfg


# ─── class_to_kks_config.yaml ────────────────────────────────────


@dataclass
class ClassKksRule:
    description: str
    kks_target: str              # "node" | "none" | "reclassify"
    expected_units: list[str]    # ["AA"]
    reclassify_rules: dict = field(default_factory=dict)


@dataclass
class ClassToKksConfig:
    """Конфигурация из class_to_kks_config.yaml."""
    class_to_kks: dict[str, ClassKksRule] = field(default_factory=dict)
    unit_to_classes: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ClassToKksConfig":
        path = Path(path)
        if not path.exists():
            logger.warning("class_to_kks_config.yaml not found: %s", path)
            return cls()
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        cfg = cls()

        for class_name, info in data.get("class_to_kks", {}).items():
            if not isinstance(info, dict):
                continue
            cfg.class_to_kks[class_name] = ClassKksRule(
                description=info.get("description", ""),
                kks_target=info.get("kks_target", "none"),
                expected_units=info.get("expected_units", []),
                reclassify_rules=info.get("reclassify_rules", {}),
            )

        cfg.unit_to_classes = {
            k: v for k, v in data.get("unit_to_classes", {}).items()
            if isinstance(v, list)
        }
        return cfg

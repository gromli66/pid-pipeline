"""
modules/binding/config.py — Единый конфиг binding из domain_profile.yaml.

Парсит секции: code_types, binding, ocr_corrections из одного YAML.
Заменяет: kks_binding/config.py + text_binding/config.py + kks_config.yaml + class_to_kks_config.yaml.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


# ── Code Type ────────────────────────────────────────────

@dataclass
class CodeTypeField:
    """Одно поле в структуре кода."""
    name: str
    regex: str
    position: str = ""        # "digit" | "letter"
    type: str = "string"      # "string" | "integer"
    optional: bool = False


@dataclass
class CodeTypeBindingRule:
    """Правило привязки для типа кода."""
    target: str = "node"          # "node" | "edge"
    method: str = "nearest_node"  # "nearest_node" | "nearest_edge" | "nearest_edge_boundary"
    max_distance: float = 200.0


@dataclass
class MatchPattern:
    """Скомпилированный regex-паттерн для парсинга кода."""
    name: str
    regex: re.Pattern
    flags_str: str = ""


@dataclass
class OcrCorrectionRule:
    """Замена OCR-ошибок для конкретного code_type."""
    pattern: re.Pattern
    replace: str


@dataclass
class CodeTypeConfig:
    """Полная конфигурация одного типа кода."""
    name: str
    structure: str = ""
    fields: list[CodeTypeField] = field(default_factory=list)
    binding: CodeTypeBindingRule = field(default_factory=CodeTypeBindingRule)
    validation_field: str = ""
    match_patterns: list[MatchPattern] = field(default_factory=list)
    ocr_corrections: list[OcrCorrectionRule] = field(default_factory=list)
    valid_values: list = field(default_factory=list)
    # Наследование: filter по полю (pipeline_code inherits equipment_code)
    inherit: Optional[str] = None
    filter_field: str = ""
    filter_values: list[str] = field(default_factory=list)


# ── Class Rules ──────────────────────────────────────────

@dataclass
class ClassBindingRule:
    """Правило привязки для одного класса детекции."""
    kks_target: str = "none"          # "node" | "edge" | "none" | "reclassify"
    expected_units: list[str] = field(default_factory=list)
    reclassify_rules: dict[str, str] = field(default_factory=dict)


# ── OCR Corrections (global) ────────────────────────────

@dataclass
class GlobalOcrCorrections:
    """Глобальные OCR-коррекции из domain_profile.yaml."""
    cyr_to_lat: dict[str, str] = field(default_factory=dict)
    digit_corrections: dict[str, str] = field(default_factory=dict)
    symbol_fixes: dict[str, str] = field(default_factory=dict)
    unit_blacklist: list[str] = field(default_factory=list)
    known_blocks: list[str] = field(default_factory=list)


# ── Unified Config ───────────────────────────────────────

@dataclass
class DomainBindingConfig:
    """Единый конфиг binding из domain_profile.yaml.

    Объединяет: code_types + binding + ocr_corrections.
    """
    code_types: dict[str, CodeTypeConfig] = field(default_factory=dict)
    ocr_corrections: GlobalOcrCorrections = field(default_factory=GlobalOcrCorrections)

    # Binding
    node_max_distance: float = 30.0
    edge_max_distance: float = 150.0
    unit_to_classes: dict[str, list[str]] = field(default_factory=dict)
    edge_binding_units: list[str] = field(default_factory=list)
    class_rules: dict[str, ClassBindingRule] = field(default_factory=dict)

    def get_bindable_code_types(self, target: str) -> list[CodeTypeConfig]:
        """Получить code_types с данным binding target."""
        return [ct for ct in self.code_types.values()
                if ct.binding.target == target]

    def get_expected_classes(self, unit: str) -> list[str]:
        """Допустимые классы для данного unit code."""
        return self.unit_to_classes.get(unit, [])

    def get_class_rule(self, class_name: str) -> Optional[ClassBindingRule]:
        """Правило привязки для класса."""
        return self.class_rules.get(class_name)

    @classmethod
    def from_yaml(cls, yaml_path: str | Path) -> "DomainBindingConfig":
        """Загрузить из domain_profile.yaml."""
        path = Path(yaml_path)
        if not path.exists():
            logger.warning("domain_profile.yaml not found: %s", path)
            return cls()

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        cfg = cls()
        cfg._parse_code_types(data.get("code_types", {}))
        cfg._parse_ocr_corrections(data.get("ocr_corrections", {}))
        cfg._parse_binding(data.get("binding", {}))
        return cfg

    def _parse_code_types(self, ct_data: dict):
        """Парсинг секции code_types."""
        for name, ct in ct_data.items():
            fields = []
            for fname, fdef in ct.get("fields", {}).items():
                fields.append(CodeTypeField(
                    name=fname,
                    regex=fdef.get("regex", ""),
                    position=fdef.get("position", ""),
                    type=fdef.get("type", "string"),
                    optional=fdef.get("optional", False),
                ))

            binding_data = ct.get("binding", {})
            binding = CodeTypeBindingRule(
                target=binding_data.get("target", "node"),
                method=binding_data.get("method", "nearest_node"),
                max_distance=binding_data.get("max_distance", 200.0),
            )

            patterns = []
            for mp in ct.get("match_patterns", []):
                flags = re.IGNORECASE if "IGNORECASE" in mp.get("flags", "").upper() else 0
                try:
                    compiled = re.compile(mp["regex"], flags)
                    patterns.append(MatchPattern(
                        name=mp.get("name", ""),
                        regex=compiled,
                        flags_str=mp.get("flags", ""),
                    ))
                except re.error as e:
                    logger.error("Bad regex in code_type '%s' pattern '%s': %s",
                                 name, mp.get("name"), e)

            ocr_corr = []
            for oc in ct.get("ocr_corrections", []):
                try:
                    ocr_corr.append(OcrCorrectionRule(
                        pattern=re.compile(oc["pattern"]),
                        replace=oc["replace"],
                    ))
                except re.error as e:
                    logger.error("Bad OCR correction regex in code_type '%s': %s", name, e)

            # Inherit / filter
            inherit = ct.get("inherit")
            filter_data = ct.get("filter", {})
            filter_field = ""
            filter_values = []
            if filter_data:
                filter_field = list(filter_data.keys())[0]
                filter_values = filter_data[filter_field]

            self.code_types[name] = CodeTypeConfig(
                name=name,
                structure=ct.get("structure", ""),
                fields=fields,
                binding=binding,
                validation_field=ct.get("validation_field", ""),
                match_patterns=patterns,
                ocr_corrections=ocr_corr,
                valid_values=ct.get("valid_values", []),
                inherit=inherit,
                filter_field=filter_field,
                filter_values=filter_values,
            )

    def _parse_ocr_corrections(self, data: dict):
        """Парсинг секции ocr_corrections."""
        self.ocr_corrections = GlobalOcrCorrections(
            cyr_to_lat=data.get("cyrillic_to_latin", {}),
            digit_corrections=data.get("digit_corrections", {}),
            symbol_fixes=data.get("symbol_fixes", {}),
            unit_blacklist=data.get("unit_blacklist", []),
            known_blocks=data.get("known_blocks", []),
        )

    def _parse_binding(self, data: dict):
        """Парсинг секции binding."""
        self.node_max_distance = data.get("node_max_distance", 30.0)
        self.edge_max_distance = data.get("edge_max_distance", 150.0)
        self.edge_binding_units = data.get("edge_binding_units", [])

        # unit_to_classes
        for unit, classes in data.get("unit_to_classes", {}).items():
            self.unit_to_classes[unit] = classes if isinstance(classes, list) else []

        # class_rules
        for class_name, rule_data in data.get("class_rules", {}).items():
            if not isinstance(rule_data, dict):
                continue
            self.class_rules[class_name] = ClassBindingRule(
                kks_target=rule_data.get("kks_target", "none"),
                expected_units=rule_data.get("expected_units", []),
                reclassify_rules=rule_data.get("reclassify_rules", {}),
            )

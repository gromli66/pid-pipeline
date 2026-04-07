"""
TextRecognitionConfig — конфигурация привязки текста из project YAML.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class OcrCorrection:
    """Одна замена для OCR-коррекции."""
    pattern: str
    replace: str


@dataclass
class DiameterPattern:
    """Один regex-паттерн для диаметра."""
    name: str
    regex: str
    flags: str = ""
    description: str = ""


@dataclass
class DiameterConfig:
    """Конфигурация распознавания диаметров."""
    patterns: list[DiameterPattern] = field(default_factory=list)
    ocr_corrections: list[OcrCorrection] = field(default_factory=list)
    valid_diameters: list[int] = field(default_factory=list)
    max_distance: float = 100.0
    method: str = "nearest_edge_boundary"
    target: str = "edge"
    edge_rule: str = "node_to_node"


@dataclass
class TextRecognitionConfig:
    """Полная конфигурация text_recognition из project YAML."""
    diameter: DiameterConfig = field(default_factory=DiameterConfig)

    @classmethod
    def from_dict(cls, data: dict) -> "TextRecognitionConfig":
        """Создать из словаря (секция text_recognition из YAML)."""
        cfg = cls()
        if not data:
            return cfg

        # --- pipe_diameter ---
        pd = data.get("pipe_diameter", {})
        if pd:
            # Patterns
            for p in pd.get("patterns", []):
                cfg.diameter.patterns.append(DiameterPattern(
                    name=p.get("name", ""),
                    regex=p.get("regex", ""),
                    flags=p.get("flags", ""),
                    description=p.get("description", ""),
                ))
            # OCR corrections
            for c in pd.get("ocr_corrections", []):
                cfg.diameter.ocr_corrections.append(OcrCorrection(
                    pattern=c.get("pattern", ""),
                    replace=c.get("replace", ""),
                ))
            # Valid diameters
            cfg.diameter.valid_diameters = pd.get("valid_diameters", [])
            # Binding
            binding = pd.get("binding", {})
            cfg.diameter.max_distance = binding.get("max_distance", 100.0)
            cfg.diameter.method = binding.get("method", "nearest_edge_boundary")
            cfg.diameter.target = binding.get("target", "edge")
            cfg.diameter.edge_rule = binding.get("edge_rule", "node_to_node")

        return cfg

    @classmethod
    def from_project_yaml(cls, yaml_path: str | Path) -> "TextRecognitionConfig":
        """Загрузить из project YAML файла."""
        import yaml
        path = Path(yaml_path)
        if not path.exists():
            logger.warning("Project YAML not found: %s, using defaults", path)
            return cls()
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data.get("text_recognition", {}))

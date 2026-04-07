"""
Project Loader - загрузка конфигурации проектов из YAML файлов.
"""

import yaml
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict
from functools import lru_cache

from app.config import settings


@dataclass
class ClassInfo:
    """Информация о классе."""
    id: int
    name: str


@dataclass
class YoloConfig:
    """Конфигурация YOLO модели (legacy, для backward compat)."""
    weights: str
    num_classes: int
    confidence: float
    class_mapping: Dict[int, int]  # YOLO class_id → CVAT category_id


@dataclass
class DetectionModelConfig:
    """Конфигурация одной модели детекции."""
    name: str = ""                      # Human-readable name (для UI)
    type: str = "yolo"                  # Тип модели: "yolo"
    weights: str = ""                   # Путь к весам
    num_classes: int = 36
    confidence: float = 0.8             # Глобальный порог confidence
    class_mapping: Dict[int, int] = field(default_factory=dict)
    description: str = ""               # Описание для UI tooltip
    # TODO: Настроить per-class пороги по результатам валидации.
    #       Ключ — имя класса (str), значение — порог confidence (float).
    #       Классы без записи используют глобальный `confidence`.
    #       Пример: {"strelka": 0.6, "datchik": 0.5}
    per_class_confidence: Dict[str, float] = field(default_factory=dict)
    sahi_slice_size: int = 1280
    sahi_overlap_ratio: float = 0.25


@dataclass
class DetectionConfig:
    """Конфигурация детекции (мульти-модель)."""
    default_model: str = "default"
    models: Dict[str, DetectionModelConfig] = field(default_factory=dict)

    def get_model(self, model_id: Optional[str] = None) -> DetectionModelConfig:
        """Получить конфиг модели по ID (None → default)."""
        key = model_id or self.default_model
        if key not in self.models:
            raise ValueError(
                f"Detection model '{key}' not found. "
                f"Available: {list(self.models.keys())}"
            )
        return self.models[key]

    @property
    def model_choices(self) -> List[tuple]:
        """Список (model_id, name, description) для UI."""
        return [
            (mid, m.name or mid, m.description)
            for mid, m in self.models.items()
        ]


@dataclass
class SegmentationConfig:
    """Конфигурация сегментации труб (ensemble из двух моделей)."""
    # ─── Чекпоинты (оба обязательны) ─────────────────────
    weights: str = ""                 # Чекпоинт A: plain UNet++
    weights_b: str = ""               # Чекпоинт B: DualHeadModel

    # ─── Параметры модели ─────────────────────────────────
    dual_head_a: bool = False         # Model A — DualHeadModel?
    dual_head_b: bool = True          # Model B — DualHeadModel?
    ensemble_strategy: str = "or"     # "or" | "and" | "mean" | "weighted_mean"

    # ─── Тайлинг и инференс ───────────────────────────────
    tile_size: int = 1024
    overlap: int = 128
    batch_size: int = 4
    threshold: float = 0.5
    use_tta: bool = True              # Включён по умолчанию (было False)
    binarize: bool = True
    binarize_method: str = "adaptive"

    # ─── Постобработка ────────────────────────────────────
    postprocess: bool = True
    postprocess_config: dict = field(default_factory=lambda: {})

    # ─── Категории для node_mask ──────────────────────────
    # Эталон (pipe_segmentation): исключаются ТОЛЬКО truba и annotation.
    # background включается в node_mask (skeleton_extension продлевает скелет
    # ко всем узлам, graph_module затем исключает background через EXCLUDED_CLASS_IDS).
    pipe_categories: List[str] = field(
        default_factory=lambda: ["truba"]
    )
    ignore_categories: List[str] = field(
        default_factory=lambda: ["annotation"]
    )


@dataclass
class SkeletonConfig:
    """Конфигурация скелетизации.

    Передаётся как dict в skeleton_extension.process_single_image().
    """
    simple_mode: bool = False
    node_boundary_expansion: int = 1
    trim_length: int = 10
    trim_protection: int = 15
    mask_width: int = 8
    extend_radius: int = 5
    direction_trace_length: int = 5
    max_line_length: int = 1000
    endpoint_search_radius: int = 5
    endpoint_line_white_tolerance: int = 2
    skeleton_search_radius: int = 5
    skeleton_line_white_tolerance: int = 2
    bfs_max_depth: int = 2000
    bfs_iterations: int = 1
    bfs_mask_tolerance: int = 5
    remove_orphans: bool = False
    orphan_trim_length: int = 5
    orphan_touch_distance: int = 3
    # Параметры mask_generation (skeleton → mask)
    mask_thickness: int = 12
    mask_adaptive: bool = True
    mask_min_thickness: int = 2
    mask_max_thickness: int = 40
    mask_prune_spurs: int = 5
    mask_smooth_size: int = 5

    def to_process_config(self) -> dict:
        """Конфиг для skeleton_extension.process_single_image()."""
        return {
            "simple_mode": self.simple_mode,
            "node_boundary_expansion": self.node_boundary_expansion,
            "trim_length": self.trim_length,
            "trim_protection": self.trim_protection,
            "mask_width": self.mask_width,
            "extend_radius": self.extend_radius,
            "direction_trace_length": self.direction_trace_length,
            "max_line_length": self.max_line_length,
            "endpoint_search_radius": self.endpoint_search_radius,
            "endpoint_line_white_tolerance": self.endpoint_line_white_tolerance,
            "skeleton_search_radius": self.skeleton_search_radius,
            "skeleton_line_white_tolerance": self.skeleton_line_white_tolerance,
            "bfs_max_depth": self.bfs_max_depth,
            "bfs_iterations": self.bfs_iterations,
            "bfs_mask_tolerance": self.bfs_mask_tolerance,
            "remove_orphans": self.remove_orphans,
            "orphan_trim_length": self.orphan_trim_length,
            "orphan_touch_distance": self.orphan_touch_distance,
            "debug": False,  # production: без визуализаций
        }


@dataclass
class JunctionSegConfig:
    """Конфигурация CenterNet junction/bridge detection."""
    weights: str = ""
    tile_size: int = 512
    overlap: int = 128
    batch_size: int = 8
    junction_threshold: float = 0.55
    bridge_threshold: float = 0.60
    nms_kernel: int = 3
    square_size: int = 15


@dataclass
class OcrConfig:
    """Конфигурация OCR из project YAML (Phase B)."""
    profile_module: str = ""              # Legacy: importlib module path (пустой = не используется)
    profile_class: str = "KKSProfile"
    profile_path: Optional[str] = None    # 1.x: путь к .py файлу профиля (приоритет над profile_module)
    profile_yaml: Optional[str] = None
    # 2.0: путь к единому domain_profile.yaml (приоритет над всем)
    domain_profile_path: Optional[str] = None
    no_protection: bool = True
    tile2_size: int = 1536
    tile2_overlap: int = 256
    tile3_size: int = 2560
    tile3_overlap: int = 384


@dataclass
class ProjectConfig:
    """Полная конфигурация проекта из YAML."""
    code: str
    name: str
    cvat_project_name: str
    classes: List[ClassInfo]
    detection: DetectionConfig
    segmentation: SegmentationConfig
    skeleton: SkeletonConfig
    junction_seg: JunctionSegConfig
    ocr: OcrConfig
    config_path: str

    @property
    def yolo(self) -> DetectionModelConfig:
        """Backward compat: возвращает default модель детекции."""
        return self.detection.get_model()

    @property
    def num_classes(self) -> int:
        return len(self.classes)

    def get_cvat_category_id(self, yolo_class_id: int) -> int:
        """YOLO class_id → CVAT category_id."""
        return self.yolo.class_mapping.get(yolo_class_id, yolo_class_id + 1)

    def get_class_name(self, cvat_category_id: int) -> Optional[str]:
        """CVAT category_id → class name."""
        for cls in self.classes:
            if cls.id == cvat_category_id:
                return cls.name
        return None


class ProjectLoader:
    """Загрузчик конфигураций проектов из YAML."""

    def __init__(self, configs_dir: Optional[Path] = None):
        self.configs_dir = configs_dir or Path(settings.PROJECTS_CONFIG_DIR)
        self._cache: Dict[str, ProjectConfig] = {}

    def _parse_yaml(self, yaml_path: Path) -> ProjectConfig:
        """Парсинг YAML в ProjectConfig."""
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        project = data.get("project", {})
        cvat = data.get("cvat", {})
        classes_data = data.get("classes", [])
        seg_data = data.get("segmentation", {})
        skel_data = data.get("skeleton", {})
        jseg_data = data.get("junction_seg", {})

        classes = [ClassInfo(id=c["id"], name=c["name"]) for c in classes_data]

        # ─── Detection config (backward compat: yolo → detection.models.default) ───
        detection_data = data.get("detection", {})
        yolo_data = data.get("yolo", {})

        if detection_data and "models" in detection_data:
            # Новый формат: detection.models
            models = {}
            for mid, mdata in detection_data["models"].items():
                models[mid] = DetectionModelConfig(
                    name=mdata.get("name", mid),
                    type=mdata.get("type", "yolo"),
                    weights=mdata.get("weights", ""),
                    num_classes=mdata.get("num_classes", 36),
                    confidence=mdata.get("confidence", 0.8),
                    class_mapping={
                        int(k): int(v)
                        for k, v in mdata.get("class_mapping", {}).items()
                    },
                    description=mdata.get("description", ""),
                    per_class_confidence={
                        str(k): float(v)
                        for k, v in mdata.get("per_class_confidence", {}).items()
                    },
                    sahi_slice_size=mdata.get("sahi_slice_size", 1280),
                    sahi_overlap_ratio=mdata.get("sahi_overlap_ratio", 0.25),
                )
            detection = DetectionConfig(
                default_model=detection_data.get("default_model", "default"),
                models=models,
            )
        elif yolo_data:
            # Legacy формат: yolo → автоконвертация в detection.models.default
            default_model = DetectionModelConfig(
                name=yolo_data.get("name", "YOLO (default)"),
                type="yolo",
                weights=yolo_data.get("weights", ""),
                num_classes=yolo_data.get("num_classes", 36),
                confidence=yolo_data.get("confidence", 0.8),
                class_mapping={
                    int(k): int(v)
                    for k, v in yolo_data.get("class_mapping", {}).items()
                },
                description=yolo_data.get("description", ""),
                per_class_confidence={
                    str(k): float(v)
                    for k, v in yolo_data.get("per_class_confidence", {}).items()
                },
                sahi_slice_size=yolo_data.get("sahi_slice_size", 1280),
                sahi_overlap_ratio=yolo_data.get("sahi_overlap_ratio", 0.25),
            )
            detection = DetectionConfig(
                default_model="default",
                models={"default": default_model},
            )
        else:
            # Нет конфигурации — пустой default
            detection = DetectionConfig(
                default_model="default",
                models={"default": DetectionModelConfig(name="YOLO (default)")},
            )

        segmentation = SegmentationConfig(
            weights=seg_data.get("weights", ""),
            weights_b=seg_data.get("weights_b", ""),
            dual_head_a=seg_data.get("dual_head_a", False),
            dual_head_b=seg_data.get("dual_head_b", True),
            ensemble_strategy=seg_data.get("ensemble_strategy", "or"),
            tile_size=seg_data.get("tile_size", 1024),
            overlap=seg_data.get("overlap", 128),
            batch_size=seg_data.get("batch_size", 4),
            threshold=seg_data.get("threshold", 0.5),
            use_tta=seg_data.get("use_tta", True),
            binarize=seg_data.get("binarize", True),
            binarize_method=seg_data.get("binarize_method", "adaptive"),
            postprocess=seg_data.get("postprocess", True),
            postprocess_config=seg_data.get("postprocess_config", {}),
            pipe_categories=seg_data.get("pipe_categories", ["truba"]),
            ignore_categories=seg_data.get(
                "ignore_categories", ["annotation"]
            ),
        )

        skeleton = SkeletonConfig(
            simple_mode=skel_data.get("simple_mode", False),
            node_boundary_expansion=skel_data.get("node_boundary_expansion", 1),
            trim_length=skel_data.get("trim_length", 10),
            trim_protection=skel_data.get("trim_protection", 15),
            mask_width=skel_data.get("mask_width", 8),
            extend_radius=skel_data.get("extend_radius", 5),
            direction_trace_length=skel_data.get("direction_trace_length", 5),
            max_line_length=skel_data.get("max_line_length", 1000),
            endpoint_search_radius=skel_data.get("endpoint_search_radius", 5),
            endpoint_line_white_tolerance=skel_data.get(
                "endpoint_line_white_tolerance", 2
            ),
            skeleton_search_radius=skel_data.get("skeleton_search_radius", 5),
            skeleton_line_white_tolerance=skel_data.get(
                "skeleton_line_white_tolerance", 2
            ),
            bfs_max_depth=skel_data.get("bfs_max_depth", 2000),
            bfs_iterations=skel_data.get("bfs_iterations", 1),
            bfs_mask_tolerance=skel_data.get("bfs_mask_tolerance", 5),
            remove_orphans=skel_data.get("remove_orphans", False),
            orphan_trim_length=skel_data.get("orphan_trim_length", 5),
            orphan_touch_distance=skel_data.get("orphan_touch_distance", 3),
            mask_thickness=skel_data.get("mask_thickness", 12),
            mask_adaptive=skel_data.get("mask_adaptive", True),
            mask_min_thickness=skel_data.get("mask_min_thickness", 2),
            mask_max_thickness=skel_data.get("mask_max_thickness", 40),
            mask_prune_spurs=skel_data.get("mask_prune_spurs", 5),
            mask_smooth_size=skel_data.get("mask_smooth_size", 5),
        )

        junction_seg = JunctionSegConfig(
            weights=jseg_data.get("weights", ""),
            tile_size=jseg_data.get("tile_size", 512),
            overlap=jseg_data.get("overlap", 128),
            batch_size=jseg_data.get("batch_size", 8),
            junction_threshold=jseg_data.get("junction_threshold", 0.55),
            bridge_threshold=jseg_data.get("bridge_threshold", 0.60),
            nms_kernel=jseg_data.get("nms_kernel", 3),
            square_size=jseg_data.get("square_size", 15),
        )

        ocr_data = data.get("ocr", {})

        # Auto-detect domain_profile.yaml рядом с project YAML
        domain_profile_path = ocr_data.get("domain_profile_path", None)
        if domain_profile_path is None:
            candidate = yaml_path.parent / "domain_profile.yaml"
            if candidate.exists():
                domain_profile_path = str(candidate)

        ocr = OcrConfig(
            profile_module=ocr_data.get("profile_module", ""),
            profile_class=ocr_data.get("profile_class", "KKSProfile"),
            profile_path=ocr_data.get("profile_path", None),
            profile_yaml=ocr_data.get("profile_yaml", None),
            domain_profile_path=domain_profile_path,
            no_protection=ocr_data.get("no_protection", True),
            tile2_size=ocr_data.get("tile2_size", 1536),
            tile2_overlap=ocr_data.get("tile2_overlap", 256),
            tile3_size=ocr_data.get("tile3_size", 2560),
            tile3_overlap=ocr_data.get("tile3_overlap", 384),
        )

        return ProjectConfig(
            code=project.get("code", yaml_path.stem),
            name=project.get("name", yaml_path.stem),
            cvat_project_name=cvat.get(
                "project_name", f"P&ID {project.get('name', '')}"
            ),
            classes=classes,
            detection=detection,
            segmentation=segmentation,
            skeleton=skeleton,
            junction_seg=junction_seg,
            ocr=ocr,
            config_path=str(yaml_path),
        )

    def load(self, project_code: str) -> Optional[ProjectConfig]:
        """Загрузить конфигурацию проекта по коду."""
        if project_code in self._cache:
            return self._cache[project_code]

        for ext in [".yaml", ".yml"]:
            # Подпапка: configs/projects/thermohydraulics/thermohydraulics.yaml
            yaml_path = self.configs_dir / project_code / f"{project_code}{ext}"
            if yaml_path.exists():
                config = self._parse_yaml(yaml_path)
                self._cache[project_code] = config
                return config
            # Плоская: configs/projects/thermohydraulics.yaml
            yaml_path = self.configs_dir / f"{project_code}{ext}"
            if yaml_path.exists():
                config = self._parse_yaml(yaml_path)
                self._cache[project_code] = config
                return config

        return None

    def load_all(self) -> List[ProjectConfig]:
        """Загрузить все конфигурации."""
        configs = []
        if not self.configs_dir.exists():
            return configs

        # Плоские файлы
        for yaml_path in self.configs_dir.glob("*.yaml"):
            try:
                config = self._parse_yaml(yaml_path)
                self._cache[config.code] = config
                configs.append(config)
            except Exception as e:
                print(f"Warning: failed to load {yaml_path}: {e}")

        for yaml_path in self.configs_dir.glob("*.yml"):
            if yaml_path.stem not in self._cache:
                try:
                    config = self._parse_yaml(yaml_path)
                    self._cache[config.code] = config
                    configs.append(config)
                except Exception as e:
                    print(f"Warning: failed to load {yaml_path}: {e}")

        # Подпапки: configs/projects/code/code.yaml
        for subdir in self.configs_dir.iterdir():
            if subdir.is_dir() and subdir.name not in self._cache:
                for ext in [".yaml", ".yml"]:
                    yaml_path = subdir / f"{subdir.name}{ext}"
                    if yaml_path.exists():
                        try:
                            config = self._parse_yaml(yaml_path)
                            self._cache[config.code] = config
                            configs.append(config)
                        except Exception as e:
                            print(f"Warning: failed to load {yaml_path}: {e}")
                        break

        return configs

    def list_codes(self) -> List[str]:
        """Список кодов доступных проектов."""
        codes = set()
        if self.configs_dir.exists():
            for p in self.configs_dir.glob("*.yaml"):
                codes.add(p.stem)
            for p in self.configs_dir.glob("*.yml"):
                codes.add(p.stem)
            # Подпапки
            for subdir in self.configs_dir.iterdir():
                if subdir.is_dir():
                    for ext in [".yaml", ".yml"]:
                        if (subdir / f"{subdir.name}{ext}").exists():
                            codes.add(subdir.name)
                            break
        # Исключить kks_config и class_to_kks_config — это не проекты
        codes.discard("kks_config")
        codes.discard("class_to_kks_config")
        return sorted(codes)


@lru_cache()
def get_project_loader() -> ProjectLoader:
    """Глобальный экземпляр ProjectLoader."""
    return ProjectLoader()

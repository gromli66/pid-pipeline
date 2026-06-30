"""
CVAT Export - конвертация детекций в форматы CVAT.

Поддерживаемые форматы:
- YOLO 1.1 (для импорта аннотаций в CVAT)
- COCO 1.0 (альтернатива)
"""

import json
import zipfile
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass


@dataclass
class Detection:
    """Детекция объекта."""
    class_id: int           # YOLO class_id (0-based)
    x_center: float         # normalized 0-1
    y_center: float         # normalized 0-1
    width: float            # normalized 0-1
    height: float           # normalized 0-1
    confidence: float = 1.0


class CVATExporter:
    """
    Экспортёр детекций в форматы CVAT.
    
    Использование:
        exporter = CVATExporter(
            class_names=["armatura_ruchn", "klapan_obratn", ...],
            class_mapping={0: 0, 1: 1, ..., 34: 35, 35: 38}  # YOLO → CVAT
        )
        
        # Создать YOLO архив для импорта в CVAT
        exporter.export_yolo(
            detections=detections,
            image_filename="scheme.png",
            output_path="export.zip"
        )
    """
    
    # Возможные имена класса "unknown" в конфигах (есть опечатка 'unknow')
    _UNKNOWN_NAMES = ("unknow", "unknown")

    def __init__(
        self,
        class_names: List[str],
        class_mapping: Optional[Dict[int, int]] = None,
    ):
        """
        Args:
            class_names: Список имён классов CVAT (0-indexed)
            class_mapping: YOLO class_id → CVAT class_id (0-based)
                          Если None, используется identity mapping
        """
        self.class_names = class_names
        self.class_mapping = class_mapping or {i: i for i in range(len(class_names))}

    def _unknown_cvat_id(self) -> Optional[int]:
        """CVAT class_id (0-based) класса unknown, или None если его нет."""
        for idx, name in enumerate(self.class_names):
            if name.strip().lower() in self._UNKNOWN_NAMES:
                return idx
        return None
    
    def _map_class_id(self, yolo_class_id: int) -> int:
        """Преобразовать YOLO class_id в CVAT class_id."""
        return self.class_mapping.get(yolo_class_id, yolo_class_id)
    
    def export_yolo(
        self,
        detections: List[Detection],
        image_filename: str,
        output_path: Path,
    ) -> Path:
        """
        Экспортировать детекции в YOLO 1.1 формат для CVAT.
        
        Создаёт ZIP архив со структурой:
            obj.data
            obj.names
            train.txt
            obj_train_data/
                {image_name}.txt
        
        Args:
            detections: Список детекций
            image_filename: Имя файла изображения (например "scheme.png")
            output_path: Путь к выходному ZIP файлу
            
        Returns:
            Путь к созданному ZIP файлу
        """
        output_path = Path(output_path)
        
        # Имя файла без расширения
        image_stem = Path(image_filename).stem
        
        # Генерируем содержимое файлов
        obj_data = self._generate_obj_data()
        obj_names = self._generate_obj_names()
        train_txt = f"data/obj_train_data/{image_filename}\n"
        annotations_txt = self._generate_annotations(detections)
        
        # Создаём ZIP архив
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("obj.data", obj_data)
            zf.writestr("obj.names", obj_names)
            zf.writestr("train.txt", train_txt)
            zf.writestr(f"obj_train_data/{image_stem}.txt", annotations_txt)
        
        return output_path
    
    def _generate_obj_data(self) -> str:
        """Генерировать obj.data."""
        return (
            f"classes = {len(self.class_names)}\n"
            f"train = data/train.txt\n"
            f"names = data/obj.names\n"
            f"backup = backup/\n"
        )
    
    def _generate_obj_names(self) -> str:
        """Генерировать obj.names."""
        return "\n".join(self.class_names) + "\n"
    
    def _generate_annotations(self, detections: List[Detection]) -> str:
        """
        Генерировать файл аннотаций.

        Порядок строк: сначала узлы unknown, затем остальные, отсортированные
        по CVAT class_id (по возрастанию). Внутри одного класса сохраняется
        исходный порядок (стабильная сортировка). Это нужно для удобства
        валидации в CVAT.
        """
        unknown_id = self._unknown_cvat_id()

        # Считаем CVAT class_id один раз, сохраняем исходный индекс для стабильности
        mapped = [
            (self._map_class_id(det.class_id), i, det)
            for i, det in enumerate(detections)
        ]

        def sort_key(item):
            cvat_class_id, orig_idx, _ = item
            is_unknown = unknown_id is not None and cvat_class_id == unknown_id
            # unknown -> группа 0 (первыми), остальные -> группа 1 и сортировка по классу
            return (0 if is_unknown else 1, cvat_class_id, orig_idx)

        mapped.sort(key=sort_key)

        lines = [
            f"{cvat_class_id} {det.x_center:.6f} {det.y_center:.6f} {det.width:.6f} {det.height:.6f}"
            for cvat_class_id, _, det in mapped
        ]
        return "\n".join(lines) + "\n" if lines else ""
    
    def export_coco(
        self,
        detections: List[Detection],
        image_filename: str,
        image_width: int,
        image_height: int,
        output_path: Path,
    ) -> Path:
        """
        Экспортировать детекции в COCO 1.0 формат для CVAT.
        
        Создаёт ZIP архив со структурой:
            annotations/
                instances_default.json
        
        Args:
            detections: Список детекций
            image_filename: Имя файла изображения
            image_width: Ширина изображения в пикселях
            image_height: Высота изображения в пикселях
            output_path: Путь к выходному ZIP файлу
            
        Returns:
            Путь к созданному ZIP файлу
        """
        output_path = Path(output_path)
        
        # Генерируем COCO JSON
        coco_data = self._generate_coco_json(
            detections, image_filename, image_width, image_height
        )
        
        # Создаём ZIP архив
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "annotations/instances_default.json",
                json.dumps(coco_data, ensure_ascii=False)
            )
        
        return output_path
    
    def _generate_coco_json(
        self,
        detections: List[Detection],
        image_filename: str,
        image_width: int,
        image_height: int,
    ) -> dict:
        """Генерировать COCO JSON."""
        # Licenses
        licenses = [{"name": "", "id": 0, "url": ""}]
        
        # Info
        info = {
            "contributor": "",
            "date_created": "",
            "description": "",
            "url": "",
            "version": "",
            "year": ""
        }
        
        # Categories (CVAT использует 1-based id)
        categories = [
            {"id": i + 1, "name": name, "supercategory": ""}
            for i, name in enumerate(self.class_names)
        ]
        
        # Images
        images = [{
            "id": 1,
            "width": image_width,
            "height": image_height,
            "file_name": image_filename,
            "license": 0,
            "flickr_url": "",
            "coco_url": "",
            "date_captured": 0
        }]
        
        # Annotations
        annotations = []
        for i, det in enumerate(detections):
            cvat_class_id = self._map_class_id(det.class_id)
            
            # COCO bbox: [x, y, width, height] в абсолютных координатах
            x = (det.x_center - det.width / 2) * image_width
            y = (det.y_center - det.height / 2) * image_height
            w = det.width * image_width
            h = det.height * image_height
            
            annotations.append({
                "id": i + 1,
                "image_id": 1,
                "category_id": cvat_class_id + 1,  # COCO 1-based
                "segmentation": [],
                "area": w * h,
                "bbox": [x, y, w, h],
                "iscrowd": 0,
                "attributes": {"occluded": False, "rotation": 0.0}
            })
        
        return {
            "licenses": licenses,
            "info": info,
            "categories": categories,
            "images": images,
            "annotations": annotations
        }


def create_exporter_from_config(project_config) -> CVATExporter:
    """
    Создать экспортёр из конфигурации проекта.
    
    Args:
        project_config: ProjectConfig из project_loader
        
    Returns:
        CVATExporter
    """
    # Получаем имена классов
    class_names = [cls.name for cls in project_config.classes]
    
    # Создаём маппинг YOLO → CVAT (0-based)
    # project_config.yolo.class_mapping содержит YOLO → CVAT category_id (1-based)
    # Конвертируем в 0-based для YOLO формата
    class_mapping = {}
    for yolo_id, cvat_category_id in project_config.yolo.class_mapping.items():
        class_mapping[yolo_id] = cvat_category_id - 1  # CVAT 1-based → 0-based
    
    return CVATExporter(
        class_names=class_names,
        class_mapping=class_mapping,
    )


def detections_to_cvat_detections(detections: List[Dict]) -> List[Detection]:
    """
    Конвертировать детекции из NodeDetector в Detection.
    
    Args:
        detections: Список детекций от NodeDetector.detect()
        
    Returns:
        Список Detection
    """
    return [
        Detection(
            class_id=det["class_id"],
            x_center=det["x_center"],
            y_center=det["y_center"],
            width=det["width"],
            height=det["height"],
            confidence=det.get("confidence", 1.0),
        )
        for det in detections
    ]

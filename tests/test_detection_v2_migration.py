"""
Проверка миграции блока детекции на v2 (39 классов).

Покрывает:
1. Согласованность CLASS_NAMES / REVERSE_REINDEX (config.py) ↔ class_mapping и
   список классов в thermohydraulics.yaml ↔ таблица классов графа (nodes.py).
2. resolve_overlaps: подавление вложенного фрагмента ТОГО ЖЕ класса; вложенный
   объект ДРУГОГО класса не трогается.
3. Адаптивный per-class WBF: при пустых per_class_weights поведение не меняется;
   per-class веса корректно подставляются (через стаб ensemble_boxes).

Запуск:  python -m pytest tests/test_detection_v2_migration.py -v
"""

import re
import sys
import types
import ast
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.yolo_detector.config import CLASS_NAMES, REVERSE_REINDEX, NUM_CLASSES
from modules.yolo_detector.detector import resolve_overlaps


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load_yaml_config():
    import yaml
    cfg_path = ROOT / "configs" / "projects" / "thermohydraulics" / "thermohydraulics.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _parse_nodes_class_names():
    """Извлечь CLASS_NAMES из nodes.py без импорта (scipy может отсутствовать)."""
    src = (ROOT / "modules" / "graph" / "core" / "nodes.py").read_text(encoding="utf-8")
    m = re.search(r"CLASS_NAMES\s*=\s*(\{.*?\})", src, re.DOTALL)
    assert m, "CLASS_NAMES не найден в nodes.py"
    return ast.literal_eval(m.group(1))


def _bbox(xc, yc, w, h):
    """Нормализованный центр → детекция с абсолютным bbox (на холсте 1000x1000)."""
    S = 1000.0
    x1 = (xc - w / 2) * S
    y1 = (yc - h / 2) * S
    x2 = (xc + w / 2) * S
    y2 = (yc + h / 2) * S
    return {
        "class_id": 0, "class_name": "x",
        "x_center": xc, "y_center": yc, "width": w, "height": h,
        "confidence": 0.9, "bbox": [x1, y1, x2, y2],
    }


# ---------------------------------------------------------------------------
# 1. Согласованность классов
# ---------------------------------------------------------------------------

def test_num_classes_is_39():
    assert NUM_CLASSES == 39
    assert len(CLASS_NAMES) == 39
    assert CLASS_NAMES[34] == "output"
    assert CLASS_NAMES[35] == "unknow"
    assert CLASS_NAMES[36] == "strelka"
    assert CLASS_NAMES[37] == "napravlenie"
    assert CLASS_NAMES[38] == "vozdushnik"


def test_reverse_reindex_matches_v2():
    assert REVERSE_REINDEX == {34: 35, 35: 37, 36: 38, 37: 40, 38: 41}


def test_class_mapping_consistent_with_yaml():
    """
    Для каждого финального id модели:
      raw = REVERSE_REINDEX.get(id, id)
      cvat_category = class_mapping.get(raw, raw + 1)
      имя класса в yaml[cvat_category] совпадает с CLASS_NAMES[id]
    """
    data = _load_yaml_config()
    classes_by_id = {c["id"]: c["name"] for c in data["classes"]}
    model = data["detection"]["models"]["ensemble_v1"]
    class_mapping = {int(k): int(v) for k, v in model["class_mapping"].items()}

    for final_id, name in CLASS_NAMES.items():
        raw = REVERSE_REINDEX.get(final_id, final_id)
        cvat_cat = class_mapping.get(raw, raw + 1)
        assert cvat_cat in classes_by_id, f"CVAT категория {cvat_cat} отсутствует в classes"
        assert classes_by_id[cvat_cat] == name, (
            f"id={final_id} ({name}): yaml-категория {cvat_cat} = "
            f"'{classes_by_id[cvat_cat]}', ожидалось '{name}'"
        )


def test_yaml_model_num_classes():
    data = _load_yaml_config()
    assert data["detection"]["models"]["ensemble_v1"]["num_classes"] == 39


def test_nodes_class_table_covers_detector_output():
    """nodes.py должен знать все 'сырые' id, которые выдаёт детектор."""
    nodes_names = _parse_nodes_class_names()
    raw_ids_emitted = {REVERSE_REINDEX.get(i, i) for i in CLASS_NAMES}
    for raw in raw_ids_emitted:
        assert raw in nodes_names, f"raw id {raw} отсутствует в nodes.CLASS_NAMES"
    # имена новых классов совпадают
    assert nodes_names[40] == "napravlenie"
    assert nodes_names[41] == "vozdushnik"


# ---------------------------------------------------------------------------
# 2. resolve_overlaps — containment
# ---------------------------------------------------------------------------

def test_containment_same_class_suppressed():
    """Полный бокс + вложенный кусок ТОГО ЖЕ класса → кусок подавляется."""
    full = _bbox(0.5, 0.5, 0.20, 0.20); full["class_id"] = 11   # nasos
    part = _bbox(0.46, 0.46, 0.06, 0.06); part["class_id"] = 11  # кусок (внутри full)
    # containment куска ≈ 1.0 (полностью внутри); взаимного перекрытия нет
    out = resolve_overlaps([full, part], mutual_overlap_threshold=0.7,
                           containment_threshold=0.8)
    assert len(out) == 1
    assert out[0] is full or (out[0]["width"] == full["width"])


def test_containment_diff_class_kept():
    """Вложенный объект ДРУГОГО класса (датчик на оборудовании) НЕ трогаем."""
    equip = _bbox(0.5, 0.5, 0.20, 0.20); equip["class_id"] = 11  # nasos
    sensor = _bbox(0.46, 0.46, 0.05, 0.05); sensor["class_id"] = 33  # datchik внутри
    out = resolve_overlaps([equip, sensor], mutual_overlap_threshold=0.7,
                           containment_threshold=0.8)
    assert len(out) == 2


def test_containment_below_threshold_kept():
    """Слабое вложение (< порога) не срабатывает."""
    big = _bbox(0.5, 0.5, 0.20, 0.20); big["class_id"] = 11
    # кусок наполовину торчит наружу → containment ≈ 0.5 < 0.8
    part = _bbox(0.60, 0.60, 0.10, 0.10); part["class_id"] = 11
    out = resolve_overlaps([big, part], mutual_overlap_threshold=0.7,
                           containment_threshold=0.8)
    assert len(out) == 2


def test_mutual_overlap_same_class_still_works():
    """Старое поведение (взаимное перекрытие) сохраняется."""
    a = _bbox(0.5, 0.5, 0.20, 0.20); a["class_id"] = 11
    b = _bbox(0.51, 0.51, 0.21, 0.21); b["class_id"] = 11  # почти совпадают
    out = resolve_overlaps([a, b])
    assert len(out) == 1


# ---------------------------------------------------------------------------
# 3. Адаптивный per-class WBF
# ---------------------------------------------------------------------------

def _install_wbf_stub(recorder):
    """Стаб ensemble_boxes.weighted_boxes_fusion — записывает переданные веса."""
    mod = types.ModuleType("ensemble_boxes")

    def weighted_boxes_fusion(boxes_list, scores_list, labels_list,
                              weights=None, iou_thr=0.5, skip_box_thr=0.0,
                              conf_type="avg"):
        recorder.append({"weights": list(weights) if weights is not None else None,
                         "labels": [l.tolist() for l in labels_list]})
        # вернуть конкатенацию входа (как будто без слияния)
        b = np.concatenate([x for x in boxes_list if len(x)], axis=0)
        s = np.concatenate([x for x in scores_list if len(x)], axis=0)
        l = np.concatenate([x for x in labels_list if len(x)], axis=0)
        return b, s, l

    mod.weighted_boxes_fusion = weighted_boxes_fusion
    sys.modules["ensemble_boxes"] = mod


def _make_ensemble(per_class_weights=None):
    from modules.yolo_detector.ensemble import EnsembleDetector
    return EnsembleDetector(
        models=[
            {"weights": "a.pt", "tile_size": 640},
            {"weights": "b.pt", "tile_size": 1280},
            {"weights": "c.pt", "tile_size": 2048},
        ],
        confidence_threshold=0.0,
        per_class_weights=per_class_weights,
    )


def test_model_labels_built():
    det = _make_ensemble()
    assert det.model_labels == ["tile640", "tile1280", "tile2048"]


def test_adaptive_uses_per_class_weights():
    recorder = []
    _install_wbf_stub(recorder)
    det = _make_ensemble(per_class_weights={12: {"tile640": 3.0, "tile1280": 0.1,
                                                 "tile2048": 0.1}})
    # две модели нашли класс 12 и класс 0
    boxes_list = [
        np.array([[0.1, 0.1, 0.2, 0.2]], dtype=np.float32),
        np.array([[0.1, 0.1, 0.2, 0.2]], dtype=np.float32),
        np.empty((0, 4), dtype=np.float32),
    ]
    scores_list = [np.array([0.9], np.float32), np.array([0.8], np.float32),
                   np.empty(0, np.float32)]
    labels_list = [np.array([12], np.int32), np.array([0], np.int32),
                   np.empty(0, np.int32)]
    det._merge_wbf_adaptive(boxes_list, scores_list, labels_list, [1.0, 1.0, 1.0])

    # для класса 12 должны примениться кастомные веса.
    # (бокс класса может прийти от любой модели, поэтому сканируем ВСЕ метки вызова)
    weights_for_12 = [r["weights"] for r in recorder
                      if any(12 in lab for lab in r["labels"])]
    assert [3.0, 0.1, 0.1] in weights_for_12
    # для класса 0 — дефолтные общие веса
    weights_for_0 = [r["weights"] for r in recorder
                     if any(0 in lab for lab in r["labels"])]
    assert [1.0, 1.0, 1.0] in weights_for_0


def test_empty_per_class_weights_keeps_standard_path():
    """Пустые per_class_weights → адаптивный путь не активируется."""
    det = _make_ensemble(per_class_weights={})
    assert det.per_class_weights == {}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

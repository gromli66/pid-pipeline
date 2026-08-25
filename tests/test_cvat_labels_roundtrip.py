"""Круг «наши классы → метки CVAT → обратно» не должен терять класс.

CVAT нумерует `category_id` ПОЗИЦИЕЙ метки в проекте (`1 + индекс`, datumaro
coco exporter), а не её id. Пока метки создавались в порядке `classes:`, позиция
совпадала с каноническим id и `category_id - 1` случайно работал. Как только
метки создаются по алфавиту русских названий, совпадение исчезает — и без
`denormalize_coco_labels` объекты молча уезжают в чужой класс.

Фикстура `tests/fixtures/coco/coco_validated_canonical.json` — синтетические
боксы в точной форме выгрузки CVAT (все 42 категории проекта, как их отдаёт
CVAT: он выгружает ВСЕ метки, а не только использованные). Данных заказчика в
ней нет, поэтому тест работает и в CI, где `storage/diagrams/*` отсутствует.
"""
import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

from app.api.cvat import denormalize_coco_labels, parse_coco_annotations
from app.core.errors import CVATLabelMismatchError
from app.services import class_display as cd

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/projects/thermohydraulics/thermohydraulics.yaml"
FIXTURE = ROOT / "tests/fixtures/coco/coco_validated_canonical.json"


@dataclass
class _Class:
    id: int
    name: str


@dataclass
class _Config:
    classes: List[_Class]
    display_labels: Dict[str, str]


@pytest.fixture
def config() -> _Config:
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return _Config(
        classes=[_Class(id=c["id"], name=c["name"]) for c in data["classes"]],
        display_labels=data.get("display_labels") or {},
    )


@pytest.fixture
def canonical() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _as_cvat_would_return(coco: dict, config: _Config) -> dict:
    """Что вернёт CVAT, если метки созданы в алфавитном порядке.

    Повторяет поведение CVAT: имена — отображаемые, порядок — порядок создания
    меток, `category_id` — позиция в этом порядке (1-based).
    """
    out = copy.deepcopy(coco)
    order = cd.display_order(config)
    position = {cls.name: i for i, cls in enumerate(order, start=1)}
    old_name = {c["id"]: c["name"] for c in coco["categories"]}

    out["categories"] = [
        {"id": position[cls.name], "name": cd.display_name(config, cls.name),
         "supercategory": ""}
        for cls in order
    ]
    for ann in out["annotations"]:
        ann["category_id"] = position[old_name[ann["category_id"]]]
    return out


def _class_of(coco: dict) -> List[str]:
    """Имя класса каждой аннотации — то, ради чего весь круг и затевался."""
    by_id = {c["id"]: c["name"] for c in coco["categories"]}
    return [by_id[a["category_id"]] for a in coco["annotations"]]


# --------------------------------------------------------------------------- #
# Главный круг
# --------------------------------------------------------------------------- #

def test_alphabetical_labels_round_trip(config, canonical):
    """Русские метки по алфавиту возвращаются к каноническим именам и id."""
    returned = _as_cvat_would_return(canonical, config)

    # предпосылка теста: класс у каждой аннотации тот же, но назван и занумерован иначе
    assert _class_of(returned) == [
        cd.display_name(config, name) for name in _class_of(canonical)
    ]
    assert [a["category_id"] for a in returned["annotations"]] != [
        a["category_id"] for a in canonical["annotations"]
    ], "симуляция ничего не переставила — тест бы прошёл впустую"

    denormalize_coco_labels(returned, config)

    assert returned["categories"] == canonical["categories"]
    assert [a["category_id"] for a in returned["annotations"]] == [
        a["category_id"] for a in canonical["annotations"]
    ]
    assert _class_of(returned) == _class_of(canonical)


def test_class_ids_survive_to_parse(config, canonical):
    """`parse_coco_annotations` считает `class_id = category_id - 1` — сверяем итог."""
    expected, _ = parse_coco_annotations(copy.deepcopy(canonical))

    returned = _as_cvat_would_return(canonical, config)
    denormalize_coco_labels(returned, config)
    actual, _ = parse_coco_annotations(returned)

    assert [a["class_id"] for a in actual] == [a["class_id"] for a in expected]
    assert [a["class_name"] for a in actual] == [a["class_name"] for a in expected]


def test_old_task_passes_through_unchanged(config, canonical):
    """Задача из старого CVAT-проекта: имена уже англ., порядок исходный.

    Проверено на живом CVAT: метки старого проекта имеют id 1..42 ровно в порядке
    `classes:`, так что для них денормализация обязана быть no-op.
    """
    before = copy.deepcopy(canonical)
    denormalize_coco_labels(canonical, config)
    assert canonical == before


def test_partial_categories_are_accepted(config, canonical):
    """Урезанный набор категорий не должен ломать разбор.

    CVAT выгружает все метки проекта, но полагаться на это незачем: сопоставление
    идёт по имени, а канонический набор восстанавливается из конфига.
    """
    used = {a["category_id"] for a in canonical["annotations"]}
    trimmed = copy.deepcopy(canonical)
    trimmed["categories"] = [c for c in trimmed["categories"] if c["id"] in used]

    expected = _class_of(canonical)
    denormalize_coco_labels(trimmed, config)
    assert trimmed["categories"] == canonical["categories"]
    assert _class_of(trimmed) == expected


# --------------------------------------------------------------------------- #
# Неопознанная метка: останавливаемся, а не портим данные
# --------------------------------------------------------------------------- #

def test_renamed_label_raises_with_context(config, canonical):
    returned = _as_cvat_would_return(canonical, config)
    victim = next(c for c in returned["categories"] if c["name"] == "Арматура ручная")
    victim["name"] = "Арматура ручная (проверено)"
    affected = sum(1 for a in returned["annotations"] if a["category_id"] == victim["id"])
    assert affected, "переименовали неиспользуемый класс — тест бы ничего не проверил"

    with pytest.raises(CVATLabelMismatchError) as exc:
        denormalize_coco_labels(returned, config)

    assert "Арматура ручная (проверено)" in str(exc.value)
    assert str(affected) in str(exc.value)
    assert exc.value.code == "cvat_label_unknown"


def test_extra_label_raises(config, canonical):
    returned = _as_cvat_would_return(canonical, config)
    returned["categories"].append({"id": 999, "name": "Мой класс", "supercategory": ""})

    with pytest.raises(CVATLabelMismatchError, match="Мой класс"):
        denormalize_coco_labels(returned, config)


def test_mismatch_leaves_input_untouched(config, canonical):
    """Данные не должны быть испорчены на полпути — файл пишется только после."""
    returned = _as_cvat_would_return(canonical, config)
    returned["categories"][0]["name"] = "Чужое имя"
    before = copy.deepcopy(returned)

    with pytest.raises(CVATLabelMismatchError):
        denormalize_coco_labels(returned, config)

    assert returned == before


# --------------------------------------------------------------------------- #
# Проект без display_labels: круг обязан работать как раньше
# --------------------------------------------------------------------------- #

def _storage_cocos() -> List[Path]:
    return sorted((ROOT / "storage/diagrams").glob("*/detection/coco_validated.json"))


@pytest.mark.skipif(not _storage_cocos(), reason="локального storage нет")
def test_round_trip_on_real_exports(config):
    """Тот же круг на настоящих выгрузках CVAT, если они есть на машине.

    Фикстура выше синтетическая; здесь проверяется, что реальные файлы не несут
    формы, которую денормализация не переваривает.
    """
    for path in _storage_cocos():
        original = json.loads(path.read_text(encoding="utf-8"))
        returned = _as_cvat_would_return(original, config)
        denormalize_coco_labels(returned, config)

        assert returned["categories"] == original["categories"], path
        assert [a["category_id"] for a in returned["annotations"]] == [
            a["category_id"] for a in original["annotations"]
        ], path


def test_project_without_display_labels(canonical):
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    plain = _Config(
        classes=[_Class(id=c["id"], name=c["name"]) for c in data["classes"]],
        display_labels={},
    )
    before = copy.deepcopy(canonical)
    denormalize_coco_labels(canonical, plain)
    assert canonical == before

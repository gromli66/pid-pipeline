"""Отображаемые названия классов: покрытие, уникальность, алфавит, лимиты CVAT.

Блок `display_labels` — источник имён меток CVAT и списков классов в клиенте.
Ошибка в нём не падает в глаза: метки уедут в CVAT, и обратно вернётся то, чего
конфиг уже не узнаёт. Поэтому проверяем сам блок, а не только код вокруг него.

Реальный конфиг читается ПРЯМЫМ путём: `tests/conftest.py` подменяет
`PROJECTS_CONFIG_DIR` на `/tmp/test_configs`, и через `ProjectLoader` боевой
YAML тестам не виден.
"""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

from app.services import class_display as cd

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/projects/thermohydraulics/thermohydraulics.yaml"


@dataclass
class _Class:
    id: int
    name: str


@dataclass
class _Config:
    """Минимальный двойник ProjectConfig: класс_display работает по утиной типизации."""
    classes: List[_Class]
    display_labels: Dict[str, str]


def _real() -> _Config:
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return _Config(
        classes=[_Class(id=c["id"], name=c["name"]) for c in data["classes"]],
        display_labels=data.get("display_labels") or {},
    )


# --------------------------------------------------------------------------- #
# 1. Боевой конфиг
# --------------------------------------------------------------------------- #

def test_every_class_has_display_name():
    cfg = _real()
    missing = [c.name for c in cfg.classes if c.name not in cfg.display_labels]
    assert not missing, f"без перевода останутся классы: {missing}"


def test_real_config_passes_validation():
    cfg = _real()
    cd.validate([c.name for c in cfg.classes], cfg.display_labels)


def test_display_order_is_alphabetical():
    cfg = _real()
    names = [cd.display_name(cfg, c.name) for c in cd.display_order(cfg)]
    assert names == sorted(names, key=cd.sort_key)
    assert len(names) == len(cfg.classes)


def test_display_order_keeps_canonical_ids():
    """Сортировка меняет порядок показа, но не сам класс."""
    cfg = _real()
    assert {c.id for c in cd.display_order(cfg)} == {c.id for c in cfg.classes}


def test_to_internal_round_trips_every_class():
    cfg = _real()
    back = cd.to_internal(cfg)
    for c in cfg.classes:
        assert back[cd.sort_key(cd.display_name(cfg, c.name))] == c.name


def test_labels_fit_cvat_limit():
    """CVAT молча обрезает имя метки до 64 символов (SafeCharField)."""
    cfg = _real()
    too_long = {k: v for k, v in cfg.display_labels.items() if len(v) > cd.CVAT_LABEL_MAX_LEN}
    assert not too_long, f"CVAT обрежет молча: {too_long}"


def test_display_labels_are_frozen():
    """Замок на названия.

    `ensure_project_labels` (app/services/cvat_client.py) сверяет метки ПО ИМЕНИ
    и умеет только добавлять. Правка названия после того, как CVAT-проект создан,
    приедет туда КАК ЕЩЁ ОДНА метка: старая останется, алфавит сломается, а уже
    размеченные задачи начнут возвращать имя, которого в конфиге больше нет, —
    и возврат аннотаций встанет с `cvat_label_unknown`.

    Красный тест здесь означает: либо название меняют осознанно и вместе с
    ревизией меток в CVAT (тогда обновить отпечаток), либо правку надо откатить.
    """
    labels = _real().display_labels
    blob = json.dumps(labels, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(blob.encode("utf-8")).hexdigest() == (
        "9e0685c6040e097846c5623d997c0153a029027d4694562582e7655eec3ce9d3"
    ), "display_labels изменились — прочитайте докстринг теста"


# --------------------------------------------------------------------------- #
# 2. Проект без блока display_labels — поведение обязано остаться прежним
# --------------------------------------------------------------------------- #

def _plain() -> _Config:
    return _Config(
        classes=[_Class(id=1, name="nasos"), _Class(id=2, name="bak")],
        display_labels={},
    )


def test_without_labels_names_and_order_unchanged():
    cfg = _plain()
    assert [cd.display_name(cfg, c.name) for c in cfg.classes] == ["nasos", "bak"]
    # порядок показа = алфавит по английским именам, канонические id не тронуты
    assert [c.name for c in cd.display_order(cfg)] == ["bak", "nasos"]
    cd.validate([c.name for c in cfg.classes], cfg.display_labels)


# --------------------------------------------------------------------------- #
# 3. validate ловит каждый способ испортить блок
# --------------------------------------------------------------------------- #

def test_validate_rejects_incomplete_coverage():
    with pytest.raises(ValueError, match="нет перевода"):
        cd.validate(["nasos", "bak"], {"nasos": "Насос"})


def test_validate_rejects_unknown_class():
    with pytest.raises(ValueError, match="несуществующих"):
        cd.validate(["nasos"], {"nasos": "Насос", "truba": "Трубопровод"})


def test_validate_rejects_duplicate_display_names():
    with pytest.raises(ValueError, match="одинаковое название"):
        cd.validate(["nasos", "bak"], {"nasos": "Насос", "bak": "насос"})


def test_validate_rejects_clash_with_internal_name():
    """Русское название не должно совпасть с англ. именем другого класса.

    При разборе возврата ветка «имя уже каноническое» проверяется первой и
    перехватила бы такое название раньше ветки перевода.
    """
    with pytest.raises(ValueError, match="совпадает с внутренним"):
        cd.validate(["nasos", "bak"], {"nasos": "bak", "bak": "Бак"})


def test_validate_rejects_too_long_name():
    with pytest.raises(ValueError, match="длиннее"):
        cd.validate(["nasos"], {"nasos": "Н" * (cd.CVAT_LABEL_MAX_LEN + 1)})


def test_sort_key_normalises_case_and_yo():
    assert cd.sort_key("Ёмкость") == cd.sort_key("емкость")

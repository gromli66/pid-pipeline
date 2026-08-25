"""Цвета меток CVAT: формат, покрытие, поведение без блока.

Цвет метки CVAT назначает сам, если при создании проекта его не передали. Так и
вышло с новым русским проектом: раскраска классов у разметчика поменялась. Блок
`class_colors` закрепляет цвет за английским именем класса, а этот тест — за
блоком: ошибка в нём тихо вернёт нас к автоцвету.

Тесты боевого конфига читают YAML ПРЯМЫМ путём: `tests/conftest.py` подменяет
`PROJECTS_CONFIG_DIR`, и через `ProjectLoader` боевой YAML тестам не виден.
"""
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from app.services import class_display as cd

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/projects/thermohydraulics/thermohydraulics.yaml"


def _real() -> tuple:
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return [c["name"] for c in data["classes"]], data.get("class_colors") or {}


# --------------------------------------------------------------------------- #
# 1. Боевой конфиг
# --------------------------------------------------------------------------- #

def test_every_class_has_color():
    names, colors = _real()
    missing = [n for n in names if n not in colors]
    assert not missing, f"CVAT покрасит сам: {missing}"


def test_real_config_passes_validation():
    names, colors = _real()
    cd.validate_colors(names, colors)


def test_colors_are_unique():
    """Два класса одного цвета разметчик не различит на схеме."""
    _, colors = _real()
    dupes = {}
    for name, color in colors.items():
        dupes.setdefault(color, []).append(name)
    repeated = {c: sorted(n) for c, n in dupes.items() if len(n) > 1}
    assert not repeated, f"повторяющиеся цвета: {repeated}"


def test_class_colors_are_frozen():
    """Замок на цвета.

    Значения сняты со старого проекта CVAT «P&ID Термогидравлика» — это и есть
    та раскраска, к которой привык разметчик. Правка здесь означает, что классы
    в CVAT поменяют цвет; уже созданные метки при этом НЕ перекрасятся
    (`ensure_project_labels` их не трогает), так что конфиг и CVAT разъедутся.

    Красный тест: либо цвета меняют осознанно и вместе с ревизией меток в CVAT
    (тогда обновить отпечаток), либо правку надо откатить.
    """
    _, colors = _real()
    blob = json.dumps(colors, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(blob.encode("utf-8")).hexdigest() == (
        "9dc3b7f87e18ccc58c4a2c8082348a400ff252e80e1492ca93f39ce766efec28"
    ), "class_colors изменились — прочитайте докстринг теста"


# --------------------------------------------------------------------------- #
# 2. Проект без блока class_colors — поведение обязано остаться прежним
# --------------------------------------------------------------------------- #

def test_without_colors_validation_passes():
    """Пустой блок = цвет выбирает CVAT, как и до появления блока."""
    cd.validate_colors(["nasos", "bak"], {})


# --------------------------------------------------------------------------- #
# 3. validate_colors ловит каждый способ испортить блок
# --------------------------------------------------------------------------- #

def test_validate_rejects_unknown_class():
    with pytest.raises(ValueError, match="несуществующих"):
        cd.validate_colors(["nasos"], {"nasos": "#ff0000", "truba": "#00ff00"})


def test_validate_rejects_incomplete_coverage():
    with pytest.raises(ValueError, match="нет цвета"):
        cd.validate_colors(["nasos", "bak"], {"nasos": "#ff0000"})


@pytest.mark.parametrize("bad", ["ff0000", "#ff00", "#ff00zz", "#ff0000ff", "red", ""])
def test_validate_rejects_bad_format(bad):
    with pytest.raises(ValueError, match="формате"):
        cd.validate_colors(["nasos"], {"nasos": bad})


def test_validate_accepts_upper_case_hex():
    cd.validate_colors(["nasos"], {"nasos": "#FF00AA"})

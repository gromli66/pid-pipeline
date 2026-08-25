"""Список меток CVAT строится в одном месте, и алфавит берётся оттуда же.

Задачу CVAT создают ДВЕ независимые точки: API (`app/api/cvat.py`) и воркер
(`worker/tasks/detection.py`, основной путь — авто-цепочка после детекции).
До этой правки каждая строила `labels` своим инлайновым list comprehension, и
расхождение копий не ловил ни один тест: воркер мог годами создавать задачи не
теми метками, что API, и заметить это было бы негде.

Порядок здесь — не косметика. CVAT метки не сортирует (проверено на v2.25.0),
поэтому порядок создания = порядок показа разметчику, и он же определяет
`category_id` в выгрузке (CVAT нумерует категории позицией метки).
"""
import re
from pathlib import Path

import pytest

from app.services.class_display import display_name, sort_key
from app.services.cvat_client import CVATLabel, create_labels_from_config
from app.services.project_loader import ProjectLoader

ROOT = Path(__file__).resolve().parents[1]
CALL_SITES = ["app/api/cvat.py", "worker/tasks/detection.py"]

# Инлайновый список меток — ровно то, что этот тест запрещает возвращать.
_INLINE = re.compile(r"CVATLabel\s*\(\s*name\s*=.*?\bfor\b", re.S)


@pytest.fixture(scope="module")
def config():
    return ProjectLoader(ROOT / "configs/projects").load("thermohydraulics")


@pytest.mark.parametrize("path", CALL_SITES)
def test_call_site_uses_shared_helper(path):
    src = (ROOT / path).read_text(encoding="utf-8")
    assert "create_labels_from_config" in src, (
        f"{path} не зовёт общий хелпер — метки снова строятся отдельной копией"
    )


@pytest.mark.parametrize("path", CALL_SITES)
def test_call_site_has_no_inline_label_list(path):
    src = (ROOT / path).read_text(encoding="utf-8")
    assert not _INLINE.search(src), (
        f"{path} строит список CVATLabel инлайном — вернулась вторая копия логики"
    )


def test_helper_returns_every_class_once(config):
    labels = create_labels_from_config(config)
    assert all(isinstance(l, CVATLabel) for l in labels)
    assert len(labels) == len(config.classes)
    assert len({l.name for l in labels}) == len(labels), "дубли имён — CVAT их отвергнет"
    assert {l.name for l in labels} == {
        display_name(config, cls.name) for cls in config.classes
    }


def test_helper_carries_configured_colors(config):
    """Цвет метки берётся из `class_colors`, иначе CVAT красит её сам."""
    by_name = {l.name: l.color for l in create_labels_from_config(config)}
    for cls in config.classes:
        assert by_name[display_name(config, cls.name)] == config.class_colors.get(cls.name)


def test_helper_returns_alphabetical_order(config):
    """Порядок создания меток = алфавит: другого источника сортировки у CVAT нет."""
    names = [l.name for l in create_labels_from_config(config)]
    assert names == sorted(names, key=sort_key)


def test_helper_falls_back_to_english_without_labels(config):
    """Проект без display_labels: прежние имена, порядок — алфавит по ним."""
    from dataclasses import replace

    names = [l.name for l in create_labels_from_config(replace(config, display_labels={}))]
    assert set(names) == {cls.name for cls in config.classes}
    assert names == sorted(names, key=sort_key)

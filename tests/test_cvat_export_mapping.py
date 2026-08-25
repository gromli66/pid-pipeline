"""Заливка предсказаний в CVAT: имя класса в obj.names против позиции в txt.

CVAT сопоставляет аннотации с метками проекта ПО ИМЕНИ
(`cvat/apps/dataset_manager/bindings.py::_get_label_id`), а не по позиции:
из строки `.txt` берётся индекс, по индексу — имя из `obj.names`, по имени —
метка проекта; незнакомое имя CVAT отвергает с ошибкой. Значит проверять надо
одно: по индексу, который мы пишем в txt, в obj.names стоит имя ТОГО класса.

Порядок `class_names` при этом остаётся порядком `classes:` из YAML — он же
индекс, на который ссылается `class_mapping`. Алфавит живёт отдельно, в списке
меток проекта (`create_labels_from_config`), и на этот файл не влияет.

Реальный конфиг читается прямым путём: `tests/conftest.py` подменяет
`PROJECTS_CONFIG_DIR`, и через `ProjectLoader` боевой YAML тестам не виден.
"""
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.cvat_export import CVATExporter, Detection, create_exporter_from_config
from app.services.project_loader import ProjectLoader

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def config():
    return ProjectLoader(ROOT / "configs/projects").load("thermohydraulics")


@pytest.fixture(scope="module")
def exporter(config):
    return create_exporter_from_config(config)


def test_obj_names_follows_yaml_order(config, exporter):
    """Порядок строк obj.names = порядок `classes:`, а не алфавит."""
    assert len(exporter.class_names) == len(config.classes)
    for i, cls in enumerate(config.classes):
        expected = config.display_labels.get(cls.name) or cls.name
        assert exporter.class_names[i] == expected, cls.name


def test_every_yolo_id_points_at_its_own_class(config, exporter):
    """Главная проверка: индекс из class_mapping → правильное имя в obj.names."""
    by_canonical_id = {cls.id: cls for cls in config.classes}

    for yolo_id, cvat_category_id in config.yolo.class_mapping.items():
        idx = exporter.class_mapping[yolo_id]
        assert idx == cvat_category_id - 1, f"yolo_id={yolo_id}: маппинг съехал"

        canonical = by_canonical_id[cvat_category_id]
        expected = config.display_labels.get(canonical.name) or canonical.name
        assert exporter.class_names[idx] == expected, (
            f"yolo_id={yolo_id} → строка {idx} в obj.names даёт "
            f"{exporter.class_names[idx]!r}, а канонический класс — {canonical.name!r}"
        )


def test_class_mapping_unchanged_by_translation(config, exporter):
    """Перевод трогает ЗНАЧЕНИЯ class_names, а не индексы."""
    expected = {y: c - 1 for y, c in config.yolo.class_mapping.items()}
    assert exporter.class_mapping == expected


def test_unknown_class_found_despite_translated_names(config, exporter):
    """Регресс: `_unknown_cvat_id` искал класс строкой 'unknow' в class_names.

    С русскими названиями строковый поиск возвращал None, и объекты unknown
    молча переставали идти первыми в файле аннотаций.
    """
    idx = exporter._unknown_cvat_id()
    assert idx is not None, "класс unknown потерялся"
    assert config.classes[idx].name == "unknow"
    # само имя в obj.names при этом переведено — то есть строкой его не найти
    assert exporter.class_names[idx] not in CVATExporter._UNKNOWN_NAMES


def test_unknown_first_in_annotations(config, exporter):
    """Порядок строк: unknown первыми, затем по возрастанию CVAT class_id."""
    unknown_idx = exporter._unknown_cvat_id()
    yolo_of = {v: k for k, v in exporter.class_mapping.items()}

    dets = [
        Detection(class_id=yolo_of[exporter.class_mapping[12]], x_center=0.5,
                  y_center=0.5, width=0.1, height=0.1),
        Detection(class_id=yolo_of[unknown_idx], x_center=0.2,
                  y_center=0.2, width=0.1, height=0.1),
    ]
    lines = exporter._generate_annotations(dets).strip().split("\n")
    assert lines[0].split()[0] == str(unknown_idx)


def test_yolo_archive_is_self_consistent(config, exporter, tmp_path):
    """Сквозная проверка архива: индекс из txt → имя в obj.names → тот же класс."""
    nasos = next(c for c in config.classes if c.name == "nasos")
    yolo_of = {v: k for k, v in exporter.class_mapping.items()}
    det = Detection(class_id=yolo_of[nasos.id - 1], x_center=0.5, y_center=0.5,
                    width=0.1, height=0.1)

    archive = exporter.export_yolo([det], "image.png", tmp_path / "ann.zip")
    with zipfile.ZipFile(archive) as zf:
        names = zf.read("obj.names").decode("utf-8").splitlines()
        data = zf.read("obj.data").decode("utf-8")
        txt = zf.read("obj_train_data/image.txt").decode("utf-8").strip()

    assert f"classes = {len(config.classes)}" in data
    idx = int(txt.split()[0])
    expected = config.display_labels.get("nasos") or "nasos"
    assert names[idx] == expected


def test_project_without_display_labels_keeps_english(config):
    """Проект без перевода отдаёт в obj.names прежние английские имена."""
    plain = create_exporter_from_config(replace(config, display_labels={}))
    names = [c.name for c in config.classes]
    assert plain.class_names == names
    assert plain._unknown_cvat_id() == names.index("unknow")

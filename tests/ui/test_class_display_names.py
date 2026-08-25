# -*- coding: utf-8 -*-
"""Русские названия классов в клиенте: показываем перевод, пишем английское имя.

Инвариант всего К3 один: человек видит русское, а НАРУЖУ (в `class_name` узла,
в сравнения редактора, в артефакты) уходит внутреннее английское имя. Стоит
где-то показать перевод «насквозь» — узел получит `class_name: "Насос"`, скин
по нему не найдётся, FXML уедет с русским классом, а фильтры по именам
`truba`/`annotation` перестанут срабатывать.

Здесь проверяются ДАННЫЕ: что легло в `Qt.UserRole`, что отдал колбэк панели,
что вернул `get_present_equipment_classes`. Ни одного утверждения про пиксели.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtWidgets import QApplication          # noqa: E402

from app.services.class_display import sort_key      # noqa: E402
from ui.editors.node_list_dialog import NodeListDialog, _SKIP_CLASSES  # noqa: E402
from PySide6.QtWidgets import QWidget                          # noqa: E402
from ui.widgets.object_resize_panel import ObjectResizePanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(qapp):
    """Панель с живым родителем.

    Родителя надо держать ссылкой: уйдёт он под сборщик — Qt снесёт и панель,
    и обращение к её комбобоксу упадёт «C++ object already deleted».
    """
    parent = QWidget()
    yield ObjectResizePanel(parent)
    parent.deleteLater()


# Ответ API в новом виде: name английское, display_name русское.
CLASSES = [
    {"id": 1, "name": "armatura_ruchn", "display_name": "Арматура ручная"},
    {"id": 12, "name": "nasos", "display_name": "Насос"},
    {"id": 21, "name": "bak", "display_name": "Бак"},
    {"id": 37, "name": "truba", "display_name": "Трубопровод"},        # скрытый
    {"id": 38, "name": "unknow", "display_name": "Неизвестный объект"},
]


def _items(dlg):
    from PySide6.QtCore import Qt
    return [
        (dlg._list.item(i).text(), dlg._list.item(i).data(Qt.ItemDataRole.UserRole))
        for i in range(dlg._list.count())
    ]


# --------------------------------------------------------------------------- #
# Палитра вставки узла
# --------------------------------------------------------------------------- #

def test_dialog_shows_russian_names(qapp):
    dlg = NodeListDialog(CLASSES)
    texts = [t for t, _ in _items(dlg)]
    assert any(t.startswith("Арматура ручная") for t in texts)
    assert not any(t.startswith("armatura_ruchn") for t in texts)


def test_dialog_sorted_by_display_name(qapp):
    dlg = NodeListDialog(CLASSES)
    shown = [t.split("  (id=")[0] for t, _ in _items(dlg)]
    assert shown == sorted(shown, key=sort_key)


def test_dialog_keeps_english_name_in_user_role(qapp):
    """Главный инвариант: наружу отдаём исходный dict с англ. `name`."""
    dlg = NodeListDialog(CLASSES)
    for text, data in _items(dlg):
        assert data["name"].isascii(), f"в UserRole уехало русское имя: {data}"
        assert text.startswith(data["display_name"])

    dlg._list.setCurrentRow(0)
    assert dlg.get_selected_class()["name"].isascii()


def test_dialog_still_hides_technical_classes(qapp):
    dlg = NodeListDialog(CLASSES)
    names = {d["name"] for _, d in _items(dlg)}
    assert names.isdisjoint(_SKIP_CLASSES)
    assert "unknow" in names, "unknow скрывать не договаривались"


@pytest.mark.parametrize("needle,expected", [
    ("Насос", "nasos"),          # по русскому
    ("nasos", "nasos"),          # по английскому — привычка и отладка
    ("НАСОС", "nasos"),          # регистр не важен
    ("id=21", "bak"),            # по идентификатору
])
def test_dialog_filter_finds_by_every_key(qapp, needle, expected):
    from PySide6.QtCore import Qt
    dlg = NodeListDialog(CLASSES)
    dlg._filter(needle)
    visible = [
        dlg._list.item(i).data(Qt.ItemDataRole.UserRole)["name"]
        for i in range(dlg._list.count())
        if not dlg._list.item(i).isHidden()
    ]
    assert visible == [expected], f"{needle!r} → {visible}"


def test_dialog_without_display_name_falls_back(qapp):
    """Старый сервер: поля нет — показываем английские имена, как раньше."""
    plain = [{"id": c["id"], "name": c["name"]} for c in CLASSES]
    dlg = NodeListDialog(plain)
    texts = [t.split("  (id=")[0] for t, _ in _items(dlg)]
    assert texts == sorted(texts, key=sort_key)
    assert all(t.isascii() for t in texts)


# --------------------------------------------------------------------------- #
# Панель «Размер объектов»
# --------------------------------------------------------------------------- #

def test_resize_panel_reports_internal_name(panel):
    """Комбобокс показывает русское, а колбэку отдаёт английское имя.

    Это имя редактор сравнивает с `node['class_name']` — подмени его переводом,
    и набор экземпляров окажется пустым.
    """
    seen = []
    panel.on_class_changed = seen.append

    panel.set_classes([("nasos", "Насос"), ("bak", "Бак")], current="nasos")
    assert [panel._class_combo.itemText(i) for i in range(panel._class_combo.count())] \
        == ["Насос", "Бак"]
    assert panel._class_combo.currentData() == "nasos"

    panel._class_combo.setCurrentIndex(1)
    assert seen == ["bak"], f"наружу ушло {seen}"


def test_resize_panel_accepts_plain_strings(panel):
    """Совместимость: список простых строк = имя и подпись совпадают."""
    panel.set_classes(["nasos", "bak"], current="bak")
    assert panel._class_combo.currentData() == "bak"
    assert panel._class_combo.currentText() == "bak"


def test_resize_panel_silent_while_populating(panel):
    """Заполнение списка не должно дёргать колбэк — иначе набор перестроится зря."""
    seen = []
    panel.on_class_changed = seen.append
    panel.set_classes([("nasos", "Насос"), ("bak", "Бак")], current="bak")
    assert seen == []


# --------------------------------------------------------------------------- #
# Редактор: список классов на схеме
# --------------------------------------------------------------------------- #

def _graph_with(*class_names):
    nodes = [
        {"id": f"n{i}", "type": "equipment", "centroid": [100.0 + i * 40, 100.0],
         "bbox": [80.0 + i * 40, 80.0, 120.0 + i * 40, 120.0], "segmentation": None,
         "class_id": i, "class_name": name, "degree": 0}
        for i, name in enumerate(class_names)
    ]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [600, 800]},
            "nodes": nodes, "links": [], "text_blocks": [], "bindings": []}


@pytest.fixture
def editor(qapp, tmp_path):
    import json
    from PySide6.QtGui import QImage, QColor
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    img = QImage(800, 600, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    assert img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(_graph_with("nasos", "bak", "armatura_ruchn")), encoding="utf-8")

    ed = AdvancedGraphEditor()
    assert ed.load_data(str(ip), str(gp))
    yield ed

    # Снос детерминированный: брошенный редактор оставляет отложенные удаления
    # в очереди Qt и роняет ЧУЖОЙ набор, который первым крутит processEvents.
    ed.set_mode("idle")
    ed.scene.clear()
    ed.setParent(None)
    ed.deleteLater()
    qapp.processEvents()


def test_editor_returns_pairs_sorted_by_display(editor):
    editor.class_display_names = {
        "nasos": "Насос", "bak": "Бак", "armatura_ruchn": "Арматура ручная",
    }
    pairs = editor.get_present_equipment_classes()
    assert pairs == [
        ("armatura_ruchn", "Арматура ручная"),
        ("bak", "Бак"),
        ("nasos", "Насос"),
    ]


def test_editor_without_map_shows_internal_names(editor):
    """Карта не загрузилась (сервер не ответил) — работаем как до перевода."""
    pairs = editor.get_present_equipment_classes()
    assert pairs == [("armatura_ruchn", "armatura_ruchn"), ("bak", "bak"), ("nasos", "nasos")]


def test_editor_selection_still_matches_internal_name(editor):
    """Набор экземпляров собирается по class_name узла, а не по переводу."""
    editor.class_display_names = {"nasos": "Насос"}
    editor.set_mode("resize_objects")
    editor.set_resize_class("nasos")
    assert editor._resize_sel == {"n0"}, "перевод просочился в сравнение"
    assert editor.display_class_name("nasos") == "Насос"
    assert editor.display_class_name("bak") == "bak"

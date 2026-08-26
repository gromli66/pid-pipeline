# -*- coding: utf-8 -*-
"""1:1-сериализатор холста -> FXML (canvas_to_fxml).

Проверяется утверждённая спецификация (память fxml-1to1-canvas-export):
identity-геометрия из bbox, приоритет осей, VERTICAL-swap, разворот
REVERSE-классов, поправка талии (13+), датчик горизонтален и БЕЗ /3, KKS из
bindings с константным кеглем, линии без B2-проекции, пунктир, терминальные
рёбра выпадают, мосты рвут линию, авто-датчик шайбы 30x30, оба порядка слоёв.
"""
import xml.etree.ElementTree as ET

from modules.canvas_to_fxml import (
    DEFAULT_LINE_COLOR, KKS_FONT_SIZE, generate_canvas_fxml,
)


def _canvas_graph() -> dict:
    """Синтетический холст 1920x1080 (уже в координатах холста)."""
    nodes = [
        # скиновый, H, 45x45 — identity: layout == bbox
        {"id": "p1", "type": "equipment", "class_name": "nasos", "_axis": "H",
         "centroid": [122.5, 122.5], "bbox": [100.0, 100.0, 145.0, 145.0]},
        # скиновый, V, REVERSE-класс + contact offset (ELECTRIC_VLV)
        {"id": "v1", "type": "equipment", "class_name": "armatura_electro",
         "_axis": "V", "centroid": [221.0, 318.9],
         "bbox": [300.0, 200.0, 337.8, 242.0]},
        # датчик: _axis V, но обязан выйти HORIZONTAL и в родном размере 30x30
        {"id": "d1", "type": "equipment", "class_name": "datchik", "_axis": "V",
         "centroid": [415.0, 515.0], "bbox": [500.0, 400.0, 530.0, 430.0]},
        # полигонный (без скина): Polygon из segmentation холста
        {"id": "b1", "type": "equipment", "class_name": "bak",
         "centroid": [550.0, 750.0], "bbox": [700.0, 500.0, 800.0, 600.0],
         "segmentation": [700.0, 500.0, 800.0, 500.0, 800.0, 600.0,
                          700.0, 600.0]},
        # napravlenie: треугольник по направлению
        {"id": "n1", "type": "equipment", "class_name": "napravlenie",
         "flow_direction": "right",
         "centroid": [310.0, 910.0], "bbox": [900.0, 300.0, 920.0, 320.0]},
        # шайба: скиновая, но БЕЗ _axis (нет в FIXED_SIZES) — ось от рёбер
        {"id": "s1", "type": "equipment", "class_name": "rashodomernaya_shaiba",
         "centroid": [707.5, 1030.0], "bbox": [1000.0, 700.0, 1060.0, 715.0]},
        {"id": "c1", "type": "connector", "centroid": [707.5, 900.0],
         "bbox": None},
    ]
    links = [
        # ручной цвет и размер
        {"id": "e1", "source": "p1", "target": "v1",
         "source_point": [122.5, 145.0], "target_point": [221.0, 300.0],
         "render_color": "#FF0000", "render_width": 3},
        # waypoints + пунктир -> Polyline со стилем
        {"id": "e2", "source": "v1", "target": "b1", "dashed": True,
         "source_point": [242.0, 318.9], "target_point": [500.0, 750.0],
         "waypoints": [[400.0, 318.9], [400.0, 750.0]]},
        # терминальное: target нет — в FXML не попадает
        {"id": "e3", "source": "d1", "target": None,
         "source_point": [415.0, 530.0], "target_point": None},
        # к шайбе слева (задаёт ей горизонтальную ось)
        {"id": "e4", "source": "c1", "target": "s1",
         "source_point": [707.5, 900.0], "target_point": [707.5, 1000.0]},
        # конец на полигонном узле: без B2 конец обязан остаться как в графе
        {"id": "e5", "source": "c1", "target": "b1",
         "source_point": [707.5, 900.0], "target_point": [550.0, 700.0]},
    ]
    return {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [1080, 1920],
                  "canvas_transform": {"s": 1.0, "offx": 0, "offy": 0}},
        "nodes": nodes, "links": links,
        "text_blocks": [
            {"id": 5, "text": "10KAA10AP001",
             "bbox": [140.0, 60.0, 260.0, 90.0]},          # привязан к p1
            {"id": 6, "text": "Свободный", "bbox": [1200.0, 800.0, 1300.0, 830.0]},
            {"id": 7, "text": "слит", "merged_into": 6,
             "bbox": [1200.0, 830.0, 1300.0, 860.0]},
        ],
        "bindings": [
            {"kind": "node", "node_id": "p1", "block_id": 5,
             "text": "10KAA10AP001"},
        ],
    }


def _fxml() -> str:
    return generate_canvas_fxml(_canvas_graph())


def test_valid_xml_and_pane_is_canvas_size():
    xml = _fxml()
    root = ET.fromstring(xml)
    assert root.attrib["prefWidth"] == "1920"
    assert root.attrib["prefHeight"] == "1080"


def test_identity_geometry_horizontal_skin():
    xml = _fxml()
    assert ('<PumpControl fx:id="p1" layoutX="100.0" layoutY="100.0" '
            'prefWidth="45.0" prefHeight="45.0"') in xml
    assert 'orientation="HORIZONTAL"' in xml.split('fx:id="p1"')[1].split('/>')[0]


def test_vertical_swap_reverse_and_contact_offset():
    """V-арматура: swap W/H, VERTICAL_REVERSE, талия ELECTRIC_VLV (0.20)."""
    xml = _fxml()
    v1 = xml.split('fx:id="v1"')[1].split('/>')[0]
    # bbox w=37.8 h=42: layout_x = 300+(37.8-42)/2 = 297.9, минус талия
    # 0.20*37.8 = 7.56 (REVERSE инвертирует знак) -> 290.3
    assert 'layoutX="290.3"' in v1
    assert 'layoutY="202.1"' in v1
    assert 'prefWidth="42.0"' in v1 and 'prefHeight="37.8"' in v1
    assert 'orientation="VERTICAL_REVERSE"' in v1


def test_detector_forced_horizontal_native_size():
    xml = _fxml()
    d1 = xml.split('fx:id="d1"')[1].split('/>')[0]
    assert 'orientation="HORIZONTAL"' in d1
    assert 'prefWidth="30.0"' in d1, "датчик не должен ужиматься /3"
    assert 'kks="fff"' in d1 and 'param="P"' in d1
    assert 'valueVisible="false"' in d1


def test_kks_from_bindings_with_const_font():
    xml = _fxml()
    p1 = xml.split('fx:id="p1"')[1].split('/>')[0]
    assert 'kks="10KAA10AP001"' in p1
    assert f'kksFontSize="{KKS_FONT_SIZE:.1f}"' in p1
    # подпись печатает <Text> в рамке блока, показ на контроле погашен —
    # иначе библиотека скинов нарисует вторую, прямо на боксе
    assert 'kksVisible="false"' in p1
    assert 'text="10KAA10AP001"' in xml
    assert 'text="Свободный"' in xml
    assert 'text="слит"' not in xml


def test_bound_label_printed_in_block_frame():
    """Блок 5 привязан к p1: <Text> встаёт в рамку блока, а не на бокс узла."""
    xml = _fxml()
    hits = [ln for ln in xml.splitlines() if 'text="10KAA10AP001"' in ln]
    assert len(hits) == 1, hits
    # bbox блока [140, 60, 260, 90]: коробка 80 центрируется на рамке
    # (200-40), центр по Y как был — решение Максима 2026-08-26.
    assert 'layoutX="160.0"' in hits[0]
    assert 'layoutY="66.0"' in hits[0]
    assert 'wrappingWidth="80.0"' in hits[0]


def test_free_label_keeps_its_frame_width():
    """80 — только у привязанных; свободный блок печатается как раньше."""
    hits = [ln for ln in _fxml().splitlines() if 'text="Свободный"' in ln]
    assert len(hits) == 1, hits
    # bbox [1200, 800, 1300, 830]: левый край рамки + её ширина
    assert 'layoutX="1200.0"' in hits[0]
    assert 'wrappingWidth="100.0"' in hits[0]


def test_detector_stub_kks_stays_on_control():
    """У заглушки 'fff' привязки нет — печатать нечего, показ гасить нельзя."""
    d1 = _fxml().split('fx:id="d1"')[1].split('/>')[0]
    assert 'kks="fff"' in d1 and 'kksVisible="true"' in d1


def test_polygon_node_and_napravlenie_triangle():
    xml = _fxml()
    b1 = xml.split('fx:id="b1"')[1].split('/>')[0]
    assert 'layoutX="700.0"' in b1 and 'points="' in b1
    n1 = xml.split('fx:id="n1"')[1].split('/>')[0]
    assert 'points="' in n1   # треугольник-стрелка


def test_line_color_default_is_independent_of_editor_settings():
    """Базовый цвет линии — #333333, и настройки редактора на него не влияют.

    Цвет рёбер в шторке «Оформление» (кислотно-зелёный по умолчанию) — только
    отрисовка: он меняет константу редактора и в данные не пишет. В FXML цвет
    берётся из `render_color`, а его ставит ровно один путь — «Линии → Цвет».
    """
    xml = _fxml()
    for eid in ("e2", "e4", "e5"):                   # рёбра без render_color
        assert f'stroke="{DEFAULT_LINE_COLOR}"' in xml.split(f'fx:id="{eid}"')[1].split('/>')[0]
    assert DEFAULT_LINE_COLOR == '#333333'


def test_lines_render_overrides_dash_and_no_b2():
    xml = _fxml()
    e1 = xml.split('fx:id="e1"')[1].split('/>')[0]
    assert 'stroke="#FF0000"' in e1 and 'strokeWidth="3.0"' in e1
    e2 = xml.split('fx:id="e2"')[1].split('/>')[0]
    assert '-fx-stroke-dash-array' in e2
    assert '<Polyline fx:id="e2"' in xml
    # e5 упирается в полигонный узел b1: конец остаётся точкой графа
    e5 = xml.split('fx:id="e5"')[1].split('/>')[0]
    assert 'endX="700.0"' in e5 and 'endY="550.0"' in e5


def test_terminal_edge_skipped():
    assert 'fx:id="e3"' not in _fxml()


def test_flow_detector_generated_native_size():
    xml = _fxml()
    det = xml.split('fx:id="s1_fm_det"')[1].split('/>')[0]
    assert 'prefWidth="30.0"' in det and 'prefHeight="30.0"' in det
    assert 'param="G"' in det
    assert 'fx:id="s1_fm_det_link"' in xml


def test_bridge_cuts_split_edge():
    """Крест двух рёбер без общего узла -> разрыв (— | —), сегменты _b0/_b1."""
    g = {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [1080, 1920], "canvas_transform": {"s": 1.0}},
        "nodes": [
            {"id": "c2", "type": "connector", "centroid": [900.0, 800.0]},
            {"id": "c3", "type": "connector", "centroid": [900.0, 1000.0]},
            {"id": "c4", "type": "connector", "centroid": [850.0, 900.0]},
            {"id": "c5", "type": "connector", "centroid": [950.0, 900.0]},
        ],
        "links": [
            {"id": "e_h", "source": "c2", "target": "c3",
             "source_point": [900.0, 800.0], "target_point": [900.0, 1000.0]},
            {"id": "e_v", "source": "c4", "target": "c5",
             "source_point": [850.0, 900.0], "target_point": [950.0, 900.0]},
        ],
        "text_blocks": [], "bindings": [],
    }
    xml = generate_canvas_fxml(g)
    assert 'fx:id="e_h_b0"' in xml and 'fx:id="e_h_b1"' in xml
    assert 'fx:id="e_v"' in xml   # вертикальная проходит сверху целой


def test_document_order_keeps_canvas_sequence():
    xml = generate_canvas_fxml(_canvas_graph(), node_order='document')
    assert 'canvas document order' in xml
    # порядок узловых элементов повторяет порядок файла: p1 раньше b1, b1 раньше n1
    assert xml.index('fx:id="p1"') < xml.index('fx:id="b1"') < xml.index('fx:id="n1"')

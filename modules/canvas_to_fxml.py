#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""canvas_to_fxml.py — 1:1-экспорт холста «Ручной правки» в FXML.

Вход — граф холста 1920x1080 (`graph/graph_canvas.json`): то, что оператор
видел и правил. Конвертер — СЕРИАЛИЗАТОР, не пересборщик: координаты и размеры
берутся строго из bbox/посаженных концов/waypoints холста, никакого масштаба,
никаких эвристик раскладки. Старый `graph_to_fxml.py` остаётся для пути без
холста (validated в пикселях растра) — он пересобирает сцену заново и для
WYSIWYG не годится.

Что унаследовано от старого конвертера (каталог нюансов, выбор Максима
2026-08-03, см. память fxml-1to1-canvas-export):

  * вся семантика FXML: таблица скинов, каскад скин → contours_all →
    segmentation → Rectangle, треугольник `napravlenie`, KKS только из
    graph.bindings, дефолты датчика (P/кПа/fff), valueVisible="false",
    цвета/толщины/пунктир линий, тексты «System Regular 40», каркас AnchorPane;
  * ориентация: направление классификатора (nasos/шайба) > `_axis` холста >
    стороны подключения рёбер > пропорции bbox. `_axis` есть только у
    FIXED_SIZES-классов; у rashodomernaya_shaiba и perehod его нет — для них
    fallback по подключениям обязателен;
  * развороты: датчики всегда HORIZONTAL; REVERSE_VERTICAL_CLASSES в вертикали
    → VERTICAL_REVERSE; swap W/H на вертикали — контракт OrientationService;
  * поправка талии (SKIN_CONTACT_OFFSET) ПРИМЕНЯЕТСЯ — осознанное отступление
    от пиксельного 1:1: в САПР талия скина обязана лечь на трубу, поэтому
    скины с приводом уезжают поперёк трубы на калиброванную долю;
  * мосты «— | —» ищутся геометрией и режут более толстую трубу;
  * авто-датчик расхода у шайбы без датчика — но в родном размере холста
    (FIXED_SIZES['datchik']), без деления на 3.

Что выкинуто (ломало 1:1): растяжка скина между точками подключения, посадка
конца трубы на контур (B2 — концы уже посажены редактором), ужатие датчика /3,
масштабирование в A0-A4, fxml_standardize, влив контуров на экспорте (контуры
влиты в холст при построении — contours_merge), легаси-маппинг по class_id.

Пины рёбер (pin_source/pin_target) не экспортируются: их произведение — уже
посаженные source_point/target_point.

Порядок слоёв (открытый пункт №35 спецификации): node_order='sections' —
секциями, как у старого конвертера; 'document' — узлы в порядке файла холста.
Линии в обоих случаях первыми (под узлами, как рисует редактор), тексты —
последними. Сравнить глазами в SceneBuilder:
    python -m modules.canvas_to_fxml graph_canvas.json -o a.fxml
    python -m modules.canvas_to_fxml graph_canvas.json -o b.fxml --order document
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape

from modules.graph_to_fxml import (
    BRIDGE_GAP_STROKE_FACTOR,
    CLASS_COLORS,
    CLASS_NAME_TO_SKIN,
    DEFAULT_COLOR,
    DEFAULT_LINE_COLOR,
    DETECTOR_DEFAULT_KKS,
    DETECTOR_DEFAULT_PARAM,
    DETECTOR_DEFAULT_UNIT,
    DIRECTION_ORIENT_CLASSES,
    DIRECTION_TO_ORIENTATION,
    FLOW_DETECTOR_GAP_FACTOR,
    FLOW_DETECTOR_PARAM,
    FLOW_DETECTOR_UNIT,
    FORCE_HORIZONTAL_CONTROLS,
    LINE_STROKE_WIDTH,
    NAPRAVLENIE_CLASS_NAME,
    PANE_BACKGROUND,
    REVERSE_VERTICAL_CLASSES,
    SKIP_CLASS_NAMES,
    SkinGeometry,
    _DIRECTION_TO_AXIS,
    _infer_napravlenie_direction,
    apply_contact_offset,
    build_node_kks_map,
    compute_bridge_cuts,
    generate_fxml_line,
    generate_fxml_polygon,
    generate_fxml_rectangle,
    generate_fxml_text,
    generate_fxml_triangle,
    get_node_connections,
    parse_bbox,
    shaiba_has_detector,
)

logger = logging.getLogger(__name__)

# Кегль KKS-подписи — константа (решение «17−»: формула 0.35*min(w,h) выкинута,
# на фикс-размерах холста она давала разнобой 7..16). Значение согласуется по
# тестовому листу; правится одним числом.
KKS_FONT_SIZE = 10.0

# Размер авто-датчика расхода у шайбы = родной размер датчика на холсте
# (FIXED_SIZES['datchik']). Без деления на 3: на холсте все датчики 30x30,
# и авто-датчик обязан выглядеть как обычный.
def _detector_size() -> tuple:
    from modules.graph.core.pretransform import FIXED_SIZES
    return FIXED_SIZES.get('datchik', (30.0, 30.0))


def _skin_info(node):
    """Скин по class_name. Без легаси-fallback по class_id (решение «2−»):
    на холсте class_name есть у всех узлов."""
    cn = node.get('class_name', '')
    if cn in SKIP_CLASS_NAMES:
        return None
    return CLASS_NAME_TO_SKIN.get(cn)


def _axis_and_orientation(node, connections):
    """Ось ('H'|'V') и orientation для эмита.

    Приоритет: направление классификатора (nasos/шайба) > `_axis` холста >
    стороны подключения рёбер > пропорции bbox. REVERSE-разворот вертикальной
    арматуры — поверх (только графика, ось не меняет).
    """
    cn = node.get('class_name')
    d = node.get('direction') or node.get('flow_direction')
    if cn in DIRECTION_ORIENT_CLASSES and d in DIRECTION_TO_ORIENTATION:
        axis = 'V' if _DIRECTION_TO_AXIS[d] == 'VERTICAL' else 'H'
        return axis, DIRECTION_TO_ORIENTATION[d]

    axis = node.get('_axis')
    if axis not in ('H', 'V'):
        sides = {c.side for c in connections}
        if sides:
            axis = 'H' if ({'LEFT', 'RIGHT'} & sides) else 'V'
        else:
            b = parse_bbox(node.get('bbox'))
            axis = 'H' if (not b or b['width'] >= b['height']) else 'V'

    emit = 'HORIZONTAL' if axis == 'H' else 'VERTICAL'
    if axis == 'V' and cn in REVERSE_VERTICAL_CLASSES:
        emit = 'VERTICAL_REVERSE'
    return axis, emit


def generate_canvas_control(node, node_id: str, connections, kks=None) -> Optional[str]:
    """Контрол со скином: геометрия СТРОГО из bbox холста.

    Вертикаль: swap W/H + пересчёт layout — контракт библиотеки скинов
    (OrientationService применяет rotate(90) к горизонтальной рамке).
    Поправка талии — после, от итоговой ориентации (VERTICAL_REVERSE
    инвертирует знак).
    """
    skin_info = _skin_info(node)
    if not skin_info:
        return None
    b = parse_bbox(node.get('bbox'))
    if not b:
        return None    # аномалия холста — узел уйдёт в Rectangle-fallback

    control_class = skin_info[0]
    skin_type = skin_info[1]
    equipment_type = skin_info[2] if len(skin_info) > 2 else None

    axis, emit_orientation = _axis_and_orientation(node, connections)
    # Датчик всегда горизонтален, независимо от трубы (10+).
    if control_class in FORCE_HORIZONTAL_CONTROLS:
        axis, emit_orientation = 'H', 'HORIZONTAL'

    width, height = b['width'], b['height']
    layout_x, layout_y = b['x1'], b['y1']
    if axis == 'V':
        layout_x = b['x1'] + (width - height) / 2
        layout_y = b['y1'] + (height - width) / 2
        width, height = height, width

    # Талия «бабочки» на трубу (13+) — единственное осознанное отступление
    # от пиксельного 1:1 (см. докстринг модуля).
    layout_x, layout_y = apply_contact_offset(
        skin_type, emit_orientation, layout_x, layout_y, height)

    attrs = [
        f'fx:id="{escape(str(node_id))}"',
        f'layoutX="{max(0.0, layout_x):.1f}"',
        f'layoutY="{max(0.0, layout_y):.1f}"',
        f'prefWidth="{max(1.0, width):.1f}"',
        f'prefHeight="{max(1.0, height):.1f}"',
        f'skinType="{skin_type}"',
        f'orientation="{emit_orientation}"',
    ]
    if equipment_type:
        attrs.append(f'equipmentType="{equipment_type}"')

    if not kks and control_class == 'DetectorControl':
        kks = DETECTOR_DEFAULT_KKS
    if kks:
        esc_kks = escape(str(kks), {chr(34): "&quot;", chr(39): "&apos;"})
        attrs.append(f'kks="{esc_kks}"')
        attrs.append('kksVisible="true"')
        attrs.append(f'kksFontSize="{KKS_FONT_SIZE:.1f}"')
        attrs.append('kksTextOffset="1.0"')

    if control_class == 'DetectorControl':
        attrs.append(f'param="{DETECTOR_DEFAULT_PARAM}"')
        attrs.append(f'unit="{DETECTOR_DEFAULT_UNIT}"')
    if control_class in ('DetectorControl', 'ValveControl'):
        attrs.append('valueVisible="false"')

    class_name = node.get('class_name', '?')
    kks_comment = f' kks={kks}' if kks else ''
    comment = f'<!-- {class_name}{kks_comment} -->'
    return f'        {comment}\n        <{control_class} {" ".join(attrs)} />'


def _contours_all_polygons(node, node_id, kks) -> list:
    """Множественные полигоны узла (contours_all) — как в старом каскаде."""
    class_name = node.get('class_name', 'unknown')
    color = CLASS_COLORS.get(class_name, DEFAULT_COLOR)
    kks_comment = f' kks={kks}' if kks else ''
    out = []
    for idx, contour_pts in enumerate(node.get('contours_all') or []):
        if len(contour_pts) < 3:
            continue
        lx = min(p[0] for p in contour_pts)
        ly = min(p[1] for p in contour_pts)
        rel = []
        for px, py in contour_pts:
            rel.append(px - lx)
            rel.append(py - ly)
        points_str = ",".join(f"{v:.1f}" for v in rel)
        sub_id = f"{node_id}_p{idx}" if idx > 0 else node_id
        attrs = [
            f'fx:id="{escape(str(sub_id))}"',
            f'layoutX="{max(0.0, lx):.1f}"',
            f'layoutY="{max(0.0, ly):.1f}"',
            f'points="{points_str}"',
            f'fill="{color}"',
            'stroke="#333333"',
            'strokeWidth="1.00"',
        ]
        comment = f'<!-- {class_name}{kks_comment} -->'
        out.append(f'        {comment}\n        <Polygon {" ".join(attrs)} />')
    return out


def generate_flow_detectors_canvas(nodes, edges) -> tuple:
    """Авто-датчик расхода у шайбы без датчика (36+), размеры холста.

    Родной размер датчика (30x30), без усреднений и деления на 3; правила
    размещения и поиска существующего датчика — старые (shaiba_has_detector).
    """
    adjacency = {}
    for e in edges:
        adjacency.setdefault(e['source'], []).append(e['target'])
        adjacency.setdefault(e['target'], []).append(e['source'])

    det_w, det_h = _detector_size()
    controls, lines = [], []
    for node_id, node in nodes.items():
        if node.get('class_name') != 'rashodomernaya_shaiba':
            continue
        b = parse_bbox(node.get('bbox'))
        if not b:
            continue
        if shaiba_has_detector(node, nodes, adjacency):
            continue

        cx = (b['x1'] + b['x2']) / 2
        cy = (b['y1'] + b['y2']) / 2
        gap = det_h * FLOW_DETECTOR_GAP_FACTOR

        conns = get_node_connections(node_id, edges, nodes)
        sides = {c.side for c in conns}
        vertical_pipe = bool(sides) and not ({'LEFT', 'RIGHT'} & sides)

        if vertical_pipe:
            det_x, det_y = b['x2'] + gap, cy - det_h / 2
            line_start = (b['x2'], cy)
        else:
            det_x, det_y = cx - det_w / 2, b['y1'] - gap - det_h
            line_start = (cx, b['y1'])

        det_x, det_y = max(0.0, det_x), max(0.0, det_y)
        det_cx, det_cy = det_x + det_w / 2, det_y + det_h / 2

        det_id = f"{node_id}_fm_det"
        comment = f'<!-- auto: датчик расхода для шайбы {node_id} -->'
        attrs = [
            f'fx:id="{escape(det_id)}"',
            f'layoutX="{det_x:.1f}"', f'layoutY="{det_y:.1f}"',
            f'prefWidth="{det_w:.1f}"', f'prefHeight="{det_h:.1f}"',
            'skinType="MINSK"',
            'orientation="HORIZONTAL"',
            'equipmentType="XMA"',
            f'param="{FLOW_DETECTOR_PARAM}"',
            f'unit="{FLOW_DETECTOR_UNIT}"',
            f'kks="{DETECTOR_DEFAULT_KKS}"',
            'kksVisible="true"',
            f'kksFontSize="{KKS_FONT_SIZE:.1f}"',
            'kksTextOffset="1.0"',
            'valueVisible="false"',
        ]
        controls.append(f'        {comment}\n        <DetectorControl {" ".join(attrs)} />')

        line_attrs = [
            f'fx:id="{escape(det_id)}_link"',
            f'startX="{line_start[0]:.1f}"', f'startY="{line_start[1]:.1f}"',
            f'endX="{det_cx:.1f}"', f'endY="{det_cy:.1f}"',
            f'stroke="{DEFAULT_LINE_COLOR}"',
            'strokeWidth="1.00"',
        ]
        lines.append(f'        {comment}\n        <Line {" ".join(line_attrs)} />')

    return controls, lines


def generate_canvas_fxml(graph_data: dict,
                         stroke_width: float = LINE_STROKE_WIDTH,
                         use_diameter: bool = True,
                         bridge_gap_factor: float = BRIDGE_GAP_STROKE_FACTOR,
                         node_order: str = 'sections') -> str:
    """Полный FXML-документ из графа холста. Identity: без масштабов.

    node_order: 'sections' — линии/контролы/прямоугольники/полигоны/тексты
    секциями (как старый конвертер); 'document' — узловые элементы в порядке
    файла холста (для приёмки №35).
    """
    graph_data = copy.deepcopy(graph_data)

    nodes = {n['id']: n for n in graph_data.get('nodes', [])}
    edges = graph_data.get('links', [])

    size = (graph_data.get('graph') or {}).get('image_size') or [1080, 1920]
    pane_h, pane_w = int(size[0]), int(size[1])

    kks_by_node = build_node_kks_map(graph_data)
    bound_block_ids = {
        b.get('block_id') for b in (graph_data.get('bindings') or [])
        if b.get('block_id')
    }

    control_elements, rectangle_elements = [], []
    polygon_elements, document_elements = [], []

    def _put(section_list, fxml):
        section_list.append(fxml)
        document_elements.append(fxml)

    for node_id, node in nodes.items():
        if node.get('type') != 'equipment':
            continue

        kks = kks_by_node.get(node_id)

        # napravlenie → треугольник по направлению потока (приоритет каскада).
        if node.get('class_name') == NAPRAVLENIE_CLASS_NAME or node.get('direction_node'):
            if not (node.get('flow_direction') or node.get('direction')):
                node['direction'] = _infer_napravlenie_direction(
                    node, node_id, edges, nodes)
            tri = generate_fxml_triangle(node, node_id, 1.0, kks=kks,
                                         edges=edges, nodes=nodes)
            if tri:
                _put(polygon_elements, tri)
                continue

        connections = get_node_connections(node_id, edges, nodes)

        fxml = generate_canvas_control(node, node_id, connections, kks=kks)
        if fxml:
            _put(control_elements, fxml)
            continue
        if node.get('contours_all'):
            for p in _contours_all_polygons(node, node_id, kks):
                _put(polygon_elements, p)
            continue
        if node.get('segmentation'):
            p = generate_fxml_polygon(node, node_id, 1.0, kks=kks)
            if p:
                _put(polygon_elements, p)
                continue
        # Fallback: прямоугольник по bbox (или 50x30 вокруг центроида).
        b = parse_bbox(node.get('bbox'))
        if b:
            geometry = SkinGeometry('HORIZONTAL', b['width'], b['height'],
                                    b['x1'], b['y1'])
        else:
            c = node.get('centroid') or [0, 0]
            geometry = SkinGeometry('HORIZONTAL', 50, 30, c[1] - 25, c[0] - 15)
        r = generate_fxml_rectangle(node, geometry, node_id, 1.0, kks=kks)
        if r:
            _put(rectangle_elements, r)

    # Авто-датчики расхода у шайб (36+).
    auto_controls, auto_lines = generate_flow_detectors_canvas(nodes, edges)
    for fxml in auto_controls:
        _put(control_elements, fxml)

    # Мосты «— | —» (27+): geometry-детект, рвётся более толстая труба.
    bridge_cuts = compute_bridge_cuts(
        edges, nodes, base_stroke=stroke_width,
        use_diameter=use_diameter, graph_scale=1.0,
        bridge_gap_factor=bridge_gap_factor,
    )

    line_elements = []
    for edge in edges:
        fxml = generate_fxml_line(edge, nodes, edge['id'], stroke_width,
                                  use_diameter=use_diameter, graph_scale=1.0,
                                  cuts=bridge_cuts.get(edge['id']),
                                  contour_snap=False)
        if fxml:
            line_elements.append(fxml)
    line_elements.extend(auto_lines)

    # Тексты OCR: непривязанные блоки → <Text> (привязанные ушли в kks).
    text_elements = []
    for blk in (graph_data.get('text_blocks') or []):
        if blk.get('merged_into') is not None:
            continue
        if blk.get('id') in bound_block_ids:
            continue
        if not (blk.get('text') or '').strip():
            continue
        t = generate_fxml_text(blk)
        if t:
            text_elements.append(t)

    fxml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '',
        '<?import javafx.scene.layout.*?>',
        '<?import javafx.scene.shape.*?>',
        '<?import javafx.scene.text.*?>',
        '<?import javafx.scene.transform.*?>',
        '<?import ru.get.common.controls.*?>',
        '',
        f'<AnchorPane fx:id="root" xmlns="http://javafx.com/javafx/17" xmlns:fx="http://javafx.com/fxml/1"',
        f'    prefWidth="{pane_w}" prefHeight="{pane_h}"',
        f'    style="-fx-background-color: {PANE_BACKGROUND};">',
        '    <children>',
        '',
        '        <!-- Lines (edges) -->',
    ]
    fxml_lines.extend(line_elements)

    if node_order == 'document':
        fxml_lines.extend(['', '        <!-- Nodes (canvas document order) -->'])
        fxml_lines.extend(document_elements)
    else:
        fxml_lines.extend(['', '        <!-- Equipment with library skins -->'])
        fxml_lines.extend(control_elements)
        fxml_lines.extend(['', '        <!-- Equipment without skins (rectangles) -->'])
        fxml_lines.extend(rectangle_elements)
        fxml_lines.extend(['', '        <!-- Equipment with polygons -->'])
        fxml_lines.extend(polygon_elements)

    fxml_lines.extend(['', '        <!-- OCR text blocks (unbound labels) -->'])
    fxml_lines.extend(text_elements)

    fxml_lines.extend([
        '',
        '    </children>',
        '</AnchorPane>',
    ])
    return '\n'.join(fxml_lines)


def main():
    parser = argparse.ArgumentParser(
        description='1:1 canvas (1920x1080) -> FXML для SceneBuilder')
    parser.add_argument('input', type=Path, help='graph_canvas.json')
    parser.add_argument('-o', '--output', type=Path, default=None)
    parser.add_argument('--order', choices=['sections', 'document'],
                        default='sections', help='порядок слоёв (приёмка №35)')
    args = parser.parse_args()

    graph_data = json.loads(args.input.read_text(encoding='utf-8'))
    fxml = generate_canvas_fxml(graph_data, node_order=args.order)
    out = args.output or args.input.with_suffix('.fxml')
    out.write_text(fxml, encoding='utf-8')
    print(f"OK: {out}")


if __name__ == '__main__':
    main()

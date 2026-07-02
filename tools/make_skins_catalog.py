#!/usr/bin/env python3
"""
Генератор каталога скинов для ручной отрисовки PNG.

Берёт привязки class_name -> (ControlClass, SkinType, [equipmentType]) из
modules.graph_to_fxml.CLASS_NAME_TO_SKIN и раскладывает по сетке один контрол
на каждый класс, с подписью (class_name + skinType). Открываешь результат в
своём FXML-просмотрщике, делаешь скрин, кропаешь каждую ячейку и сохраняешь
как ui/resources/skins/<class_name>.png.

Запуск:
    PYTHONPATH=. python3 tools/make_skins_catalog.py -o skins_catalog.fxml
"""

import argparse
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from modules.graph_to_fxml import CLASS_NAME_TO_SKIN

# Контролы, у которых надо прятать дефолтный value ("0" / "0%").
VALUE_HIDE_CLASSES = {"ValveControl", "DetectorControl"}

# --- параметры сетки ---
COLS = 4
CELL_W = 190
CELL_H = 210
MARGIN = 20
CTRL = 120          # размер контрола (квадрат)
ORIENTATION = "VERTICAL"
BG = "#FFFFFF"


def build_catalog() -> str:
    items = list(CLASS_NAME_TO_SKIN.items())
    rows = (len(items) + COLS - 1) // COLS
    pane_w = MARGIN * 2 + COLS * CELL_W
    pane_h = MARGIN * 2 + rows * CELL_H

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '',
        '<?import javafx.scene.layout.*?>',
        '<?import javafx.scene.shape.*?>',
        '<?import javafx.scene.text.*?>',
        '<?import ru.get.common.controls.*?>',
        '',
        f'<AnchorPane fx:id="root" xmlns="http://javafx.com/javafx/17" '
        f'xmlns:fx="http://javafx.com/fxml/1"',
        f'    prefWidth="{pane_w}" prefHeight="{pane_h}" '
        f'style="-fx-background-color: {BG};">',
        '    <children>',
    ]

    for i, (class_name, spec) in enumerate(items):
        control_class = spec[0]
        skin_type = spec[1]
        equip = spec[2] if len(spec) > 2 else None

        col, row = i % COLS, i // COLS
        cell_x = MARGIN + col * CELL_W
        cell_y = MARGIN + row * CELL_H
        ctrl_x = cell_x + (CELL_W - CTRL) / 2
        ctrl_y = cell_y + 15
        lbl_x = cell_x + 8
        lbl1_y = cell_y + 15 + CTRL + 20
        lbl2_y = lbl1_y + 16

        # рамка ячейки (для удобства кропа)
        out.append(
            f'        <Rectangle layoutX="{cell_x}" layoutY="{cell_y}" '
            f'width="{CELL_W - 10}" height="{CELL_H - 10}" '
            f'fill="TRANSPARENT" stroke="#CCCCCC" strokeWidth="1" />'
        )

        # сам скин-контрол
        attrs = [
            f'layoutX="{ctrl_x:.1f}"',
            f'layoutY="{ctrl_y:.1f}"',
            f'prefWidth="{CTRL}"',
            f'prefHeight="{CTRL}"',
            f'skinType="{skin_type}"',
            f'orientation="{ORIENTATION}"',
        ]
        if equip:
            attrs.append(f'equipmentType="{equip}"')
        if control_class in VALUE_HIDE_CLASSES:
            attrs.append('valueVisible="false"')
        out.append(f'        <{control_class} {" ".join(attrs)} />')

        # подписи: имя класса (= имя файла PNG) и тип скина
        equip_txt = f" / {equip}" if equip else ""
        out.append(
            f'        <Text layoutX="{lbl_x}" layoutY="{lbl1_y}" '
            f'text={quoteattr(class_name)} '
            f'style="-fx-font-size: 12px; -fx-font-weight: bold;" />'
        )
        out.append(
            f'        <Text layoutX="{lbl_x}" layoutY="{lbl2_y}" '
            f'text={quoteattr(skin_type + equip_txt)} '
            f'style="-fx-font-size: 10px; -fx-fill: #777777;" />'
        )

    out.append('    </children>')
    out.append('</AnchorPane>')
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Сгенерировать FXML-каталог скинов")
    ap.add_argument("-o", "--output", type=Path, default=Path("skins_catalog.fxml"))
    args = ap.parse_args()
    args.output.write_text(build_catalog(), encoding="utf-8")
    print(f"Каталог сохранён: {args.output} ({len(CLASS_NAME_TO_SKIN)} скинов)")


if __name__ == "__main__":
    main()

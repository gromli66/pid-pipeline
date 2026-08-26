# -*- coding: utf-8 -*-
"""Один кегль на все подписи FXML + центрирование рамкой (блок 3, mefx-3).

Решение Максима 2026-08-25 №3: кегль подписи фиксированный — **18**, один
на все виды подписи; ⛔ в FXML не должно быть КОЭФФИЦИЕНТОВ. До правки их
было три штуки на четыре числа:

* `_TEXT_FONT_SIZE = 40.0` — кегль `<Text>` (замер §MEFX3: 125 подписей
  корпуса из 158 не влезали в свою рамку);
* `_TEXT_CHAR_W_FACTOR = 0.55` — ОЦЕНКА ширины строки, из неё считался
  `layoutX`; калибрована под System, у другого семейства символ шире,
  и подпись уезжала по X тем сильнее, чем длиннее строка;
* `0.35 * min(w, h)` и `0.4 * min(w, h)` — кегль KKS долей от размера
  бокса: на одном листе `d74eb9f1` это давало 5.1 и 7.2 (замер §MEFX3),
  отсюда жалоба «на одном листе подписи разного размера».

Решение №7 (В4): шрифт фиксируем **только в FXML** — `<Font name="Tahoma"
size="18.0"/>` плюс жирность отдельным inline-стилем.
⛔ **`name` несёт СЕМЕЙСТВО, а не начертание**: полное имя начертания
(«Tahoma Bold») JavaFX молча подменяет на System — баг JDK-8089450. Ровно
на эту грабку набор и запирается: начертание в `name` = зелёный XML и
чужой шрифт на листе, никакой ошибки.

Центрирование — вариант **Б** (решение Максима): координату не вычисляем,
выравнивание отдаёт формат. `textAlignment="CENTER"` эмитился и раньше, но
для однострочного `<Text>` без `wrappingWidth` он мёртв — потому и жила
оценка длины. `layoutX` = край рамки.

⛔ **`wrappingWidth` остался ТОЛЬКО у привязанных подписей** (80 px,
`BOUND_TEXT_WRAPPING`; решение Максима 2026-08-26). У непривязанного блока
атрибута нет вовсе: рамки блоков в разы уже кегля 18, и перенос по ширине
рамки рвал подпись в столбик по одной букве. Значит у свободной подписи
центрирование снова мертво — она идёт одной строкой от левого края рамки,
а замки ниже запирают именно отсутствие атрибута.

⛔ **Граница блока (редтим):** у СКИНОВОЙ KKS-подписи семейство шрифта
атрибутами не задаётся вовсе — в файл уходит только `kksFontSize`. Подпись
ДИАМЕТРА в FXML не печатается вообще; новую эмиссию блок не заводит
(её экранная половина — `tests/ui/test_edge_label_regime.py`).
"""
import json
import os
import re

import pytest

from modules import graph_to_fxml
from modules.canvas_to_fxml import generate_canvas_fxml
from modules.graph.core.pretransform import pretransform
from modules.graph_to_fxml import (
    BOUND_TEXT_WRAPPING, TEXT_STYLES, generate_fxml, generate_fxml_text,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "graph")

#: Кегль решения №3. Число абсолютное: вычислять его из `TEXT_STYLES`
#: значило бы вывести вход из проверяемой константы.
SIZE = 18.0

#: Горизонтальная рамка: 160 x 24 (h < w * 1.3).
HBOX = [100.0, 200.0, 260.0, 224.0]
#: Вертикальная рамка: 24 x 160 (h > w * 1.3 — текст пишется снизу вверх).
VBOX = [100.0, 200.0, 124.0, 360.0]

#: Два узла ОДНОГО листа с ЗАВЕДОМО разным размером — замок порога: при
#: доле от размера бокса их KKS расходились, и «оба по 18» на одинаковых
#: узлах было бы сравнением совпадения с совпадением.
KKS_NODES = ("node_3", "node_6")
#: Их короткие стороны в исходных байтах `d74eb9f1` (замер §MEFX3).
KKS_MIN_SIDES = (27, 108)


def _block(bbox, text="ПГ-1"):
    return {"id": "b1", "bbox": list(bbox), "text": text}


def _attr(xml: str, name: str):
    m = re.search(rf'{name}="([^"]*)"', xml)
    return m.group(1) if m else None


def _fixture(name: str = "d74eb9f1") -> dict:
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def _with_kks(graph: dict) -> dict:
    """Тот же лист плюс привязки KKS на два узла разного размера."""
    graph["bindings"] = [
        {"kind": "node", "node_id": nid, "text": f"KKS-{i}", "block_id": f"kb{i}"}
        for i, nid in enumerate(KKS_NODES)
    ]
    return graph


def _kks_sizes(xml: str) -> set:
    return {float(v) for v in re.findall(r'kksFontSize="([0-9.]+)"', xml)}


# ── замки обстановки ─────────────────────────────────────────────────────

def test_фикстура_несёт_узлы_заведомо_разного_размера():
    """Без этого замка «оба KKS по 18» доказывало бы только своё совпадение."""
    nodes = {n["id"]: n for n in _fixture()["nodes"]}
    sides = tuple(round(min(nodes[nid]["bbox"][2] - nodes[nid]["bbox"][0],
                            nodes[nid]["bbox"][3] - nodes[nid]["bbox"][1]))
                  for nid in KKS_NODES)
    assert sides == KKS_MIN_SIDES
    assert sides[1] > sides[0] * 3


# ── таблица стилей ───────────────────────────────────────────────────────

def test_таблица_несёт_один_кегль_на_все_виды_подписи():
    """Одно число вместо четырёх (40.0 / 10.0 / 0.35·min / 0.4·min)."""
    assert set(TEXT_STYLES) == {"text_block", "kks"}
    assert [s.size for s in TEXT_STYLES.values()] == [SIZE, SIZE]


def test_подписи_диаметра_в_таблице_файла_нет():
    """⛔ Решение Максима по доработке mefx-3.

    Таблица — про то, что уходит В ФАЙЛ. Подпись диаметра в FXML не
    печатается, паритета с файлом у неё нет, кегль у неё свой внутренний
    (`base_graph_editor._create_edge_label`). Запись в таблице означала бы
    обещание, которого выгрузка не выполняет.
    """
    assert "diameter" not in TEXT_STYLES


def test_оценка_ширины_строки_удалена_а_не_поправлена():
    """⛔ Требование: коэффициент УДАЛЯЕТСЯ, перекалибровка запрещена."""
    assert not hasattr(graph_to_fxml, "_TEXT_CHAR_W_FACTOR")


def test_кегль_не_доля_от_размера_бокса():
    """Рамка вдесятеро больше — кегль тот же."""
    small = generate_fxml_text(_block([0.0, 0.0, 16.0, 8.0]))
    big = generate_fxml_text(_block([0.0, 0.0, 1600.0, 800.0]))
    assert _attr(small, "size") == _attr(big, "size") == f"{SIZE:.1f}"


# ── шрифт: семейство в name, начертание обычное ──────────────────────────

def test_семейство_а_не_начертание_в_имени_шрифта():
    """JDK-8089450: «Tahoma Bold» в `name` = молча System на листе."""
    xml = generate_fxml_text(_block(HBOX))
    name = _attr(xml, "name")
    assert name == "Tahoma"
    assert " " not in name, "полное имя начертания JavaFX подменяет на System"


def test_начертание_обычное_у_любой_подписи():
    """Решение Максима 2026-08-26: жирности нет ни у привязанной, ни у свободной."""
    xml = generate_fxml_text(_block(HBOX))
    assert "font-weight" not in xml


def test_вертикальная_подпись_несёт_тот_же_шрифт():
    """Поворот -90° — та же ветка стиля, а не своя копия."""
    xml = generate_fxml_text(_block(VBOX))
    assert '<Font name="Tahoma" size="18.0"/>' in xml
    assert "font-weight" not in xml
    assert '<Rotate angle="-90.0"' in xml


# ── центрирование: рамкой, а не оценкой длины строки ─────────────────────

def test_горизонтальная_свободная_подпись_идёт_от_края_рамки():
    """Свободная подпись: `layoutX` = левый край, `wrappingWidth` не эмитится."""
    xml = generate_fxml_text(_block(HBOX))
    x1, _y1, _x2, _y2 = HBOX
    assert float(_attr(xml, "layoutX")) == pytest.approx(x1, abs=0.05)
    assert _attr(xml, "wrappingWidth") is None
    assert _attr(xml, "textAlignment") == "CENTER"


def test_привязанная_подпись_несёт_коробку_переноса():
    """Единственный носитель `wrappingWidth` — привязанная подпись."""
    xml = generate_fxml_text(_block(HBOX), BOUND_TEXT_WRAPPING)
    x1, _y1, x2, _y2 = HBOX
    assert float(_attr(xml, "wrappingWidth")) == pytest.approx(BOUND_TEXT_WRAPPING, abs=0.05)
    # коробка центрируется на рамке — подпись остаётся там, где привязана
    assert float(_attr(xml, "layoutX")) == pytest.approx(
        (x1 + x2) / 2 - BOUND_TEXT_WRAPPING / 2, abs=0.05)


def test_вертикальная_свободная_подпись_идёт_вверх_от_нижней_грани():
    """Повёрнутая строка идёт вверх от `layoutY`; коробки переноса у неё нет."""
    xml = generate_fxml_text(_block(VBOX))
    x1, _y1, x2, y2 = VBOX
    assert float(_attr(xml, "layoutY")) == pytest.approx(y2, abs=0.05)
    assert _attr(xml, "wrappingWidth") is None
    # толщина строки центрируется по оси рамки — кеглем, не оценкой длины
    assert float(_attr(xml, "layoutX")) == pytest.approx((x1 + x2) / 2 - SIZE / 2, abs=0.05)


@pytest.mark.parametrize("bbox", [HBOX, VBOX])
def test_длина_строки_на_раскладку_не_влияет(bbox):
    """Главное следствие варианта Б: координата больше не зависит от текста.

    При `_TEXT_CHAR_W_FACTOR` подпись из 30 символов уезжала относительно
    подписи из двух на 0.55·18·28 ≈ 277 px.
    """
    short = generate_fxml_text(_block(bbox, "Ф1"))
    long = generate_fxml_text(_block(bbox, "Ф" * 30))
    for a in ("layoutX", "layoutY", "wrappingWidth"):
        assert _attr(short, a) == _attr(long, a), a


# ── KKS: одно число на обоих путях ───────────────────────────────────────

def test_kks_кегль_один_и_тот_же_на_холстовом_пути():
    canvas, _t, _s = pretransform(_with_kks(_fixture()))
    assert _kks_sizes(generate_canvas_fxml(canvas)) == {SIZE}


def test_kks_кегль_один_и_тот_же_на_растровом_пути():
    """Легаси-путь (граф без холста) обязан говорить то же число."""
    assert _kks_sizes(generate_fxml(_with_kks(_fixture()))) == {SIZE}


# ── подпись диаметра в файл не уходит ────────────────────────────────────

#: Текст-блок, изображающий подпись диаметра, и ребро, к которому он привязан.
DN_TEXT = "Dn300"
DN_BLOCK = "dn_block"


def _with_diameter_block(graph: dict, bind: bool) -> dict:
    """Тот же лист плюс блок «Dn300»; `bind` — привязать его к ребру или нет."""
    graph.setdefault("text_blocks", []).append(
        {"id": DN_BLOCK, "bbox": list(HBOX), "text": DN_TEXT})
    if bind:
        e = graph["links"][0]
        graph["bindings"] = [{"kind": "edge", "block_id": DN_BLOCK,
                              "edge_key": f'{e["source"]}|{e["target"]}',
                              "text": DN_TEXT}]
    return graph


def _texts(xml: str) -> list:
    return re.findall(r'<Text [^>]*text="([^"]*)"', xml)


@pytest.mark.parametrize("gen", ["canvas", "raster"])
def test_подпись_диаметра_в_выгрузку_не_печатается(gen):
    """⛔ Решение Максима: в FXML подписи диаметра нет — и новой эмиссии не заводим.

    Держит это не «мы её не пишем», а исключение по привязке: блок, привязанный
    к РЕБРУ, в `<Text>` не идёт (`bound_block_ids`). Замок ниже — про РАЗНИЦУ,
    а не про совпадение: тот же блок БЕЗ привязки печатается, значит тест
    видит именно работу исключения, а не отсутствие блока.
    """
    def render(bind):
        g = _with_diameter_block(_fixture(), bind)
        if gen == "canvas":
            canvas, _t, _s = pretransform(g)
            return generate_canvas_fxml(canvas)
        return generate_fxml(g)

    assert DN_TEXT in _texts(render(bind=False)), "фикстура ниже порога"
    assert DN_TEXT not in _texts(render(bind=True))

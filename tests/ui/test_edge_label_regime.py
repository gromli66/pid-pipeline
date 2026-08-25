# -*- coding: utf-8 -*-
"""Подпись диаметра — как OCR-блок; кегль подписей — из общей таблицы (блок 3, mefx-3).

**3.3 (решение Максима 2026-08-25 по приёмке mefx-2).** Подпись диаметра
создавалась вместе с ребром безусловно и висела во ВСЕХ состояниях
(`base_graph_editor.py:_create_edge_label`), хотя правит её единственный
жест — Ctrl+2ЛКМ в «ОКР привязке» (`advanced_graph_editor.py:6296-6299`),
а в FXML её нет вовсе. Решение: поведение то же, что у блоков текста
ОКР-слоя — видна только в состоянии `'ocr'`. Механизм не свой:
`OcrLayerMixin` уже держит правило «слой виден только в 'ocr'»
(`_refresh_ocr_layer_visibility`), подпись диаметра встаёт в тот же ряд.

**3.1, экранная половина.** Кегль подписи ТЕКСТ-БЛОКА берётся из
`modules.graph_to_fxml.TEXT_STYLES` — той же таблицы, что и у выгрузки: этот
текст в файл уходит, значит паритет ему положен.
⛔ **Подпись диаметра файловой константе НЕ подчиняется** (решение Максима по
доработке mefx-3): в FXML её нет вовсе, паритета с файлом у неё нет, кегль
внутренний и прежний — одинаковый во всех вкладках, включая «Проверку схемы».
⚠ **Общий у экрана и файла ТОЛЬКО кегль** (решение №7): Tahoma в клиент не
бандлим, семейство на экране остаётся прежним (`sans-serif` у диаметра,
`DejaVu Sans` у подписи текст-блока).
⛔ **Множитель `_ocr_vis_scale()` из кегля убран**: кегль в FXML — это em
в координатах ХОЛСТА, ровно тех, в которых живёт сцена, поэтому домножать
его на вписывание растра значит снова расходиться с файлом. Замок ниже
запирает именно это: у фикстуры `_bg_scale` заведомо не 1.0.

⛔ **Фикстура поднята ЗА ПОРОГ.** В исходных байтах `d74eb9f1` нет НИ ОДНОГО
ребра с `diameter_text` (замер §MEFX3), а без него подпись не создаётся
вовсе — «подписей вне 'ocr' ноль» было бы верно и на дефектном коде.
Диаметры дописываются В ПАМЯТИ, их число заперто абсолютным числом.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPointF, Qt              # noqa: E402
from PySide6.QtGui import QImage, QColor, QMouseEvent       # noqa: E402
from PySide6.QtWidgets import QApplication                  # noqa: E402

from modules.graph_to_fxml import TEXT_STYLES               # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "fixtures", "graph")

#: Кегль решения №3 — абсолютное число, не выведенное из таблицы.
SIZE = 18
#: Внутренний кегль подписи диаметра (pt). В FXML она не печатается, поэтому
#: файловому числу не подчиняется — решение Максима по доработке mefx-3.
DIAM_PT = 6
#: Сколько рёбер фикстуры получают диаметр (замок порога).
DIAM_COUNT = 5
#: Живых текст-блоков с текстом в `d74eb9f1` — число абсолютное, фикстура в git.
BLOCK_COUNT = 48
#: Все состояния редактора; подпись обязана быть видна ровно в одном.
REGIMES = ("base", "ocr", "perp", "style")


def _fixture_graph(name: str = "d74eb9f1") -> dict:
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def _diameter_graph() -> dict:
    """`d74eb9f1` плюс диаметры на первых пяти рёбрах."""
    g = _fixture_graph()
    for i, e in enumerate(g["links"][:DIAM_COUNT]):
        e["diameter_value"] = 100.0 + 50 * i
        e["diameter_text"] = f"Dn{int(100 + 50 * i)}"
    return g


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _write(tmp_path, graph: dict):
    img = QImage(200, 150, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    assert img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")
    return str(ip), str(gp)


def _dispose(ed, qapp):
    """Снос детерминированный, а не «пусть соберёт сборщик» (PROTOCOL §5, замер 1-32)."""
    ed.set_mode("idle")
    ed.scene.clear()
    ed.setParent(None)
    ed.deleteLater()
    qapp.processEvents()


@pytest.fixture
def ed(qapp, tmp_path):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    g = _diameter_graph()
    editor = AdvancedGraphEditor()
    editor._canvas_mode = True             # «Ручная правка» — единственный холст
    assert editor.load_data(*_write(tmp_path, g))
    editor.resize(1400, 900)
    yield editor, g
    _dispose(editor, qapp)


def _labels(editor):
    return list(editor.edge_label_items.values())


def _visible_labels(editor):
    return [it for it in _labels(editor) if it.isVisible()]


def _ctrl_dclick(editor, sx, sy):
    """Ctrl+двойной клик в точке СЦЕНЫ — ровно как его отдаёт Qt."""
    vp = QPointF(editor.mapFromScene(QPointF(sx, sy)))
    editor.mouseDoubleClickEvent(QMouseEvent(
        QEvent.Type.MouseButtonDblClick, vp, vp,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier))


# ── замки обстановки ─────────────────────────────────────────────────────

def test_фикстура_поднята_за_порог(ed):
    """В исходных байтах диаметров ноль — без дописи проверять было бы нечего."""
    editor, _g = ed
    assert not [e for e in _fixture_graph()["links"] if e.get("diameter_text")]
    editor.set_display_regime("ocr")
    assert len(_labels(editor)) == DIAM_COUNT


def test_вписывание_растра_не_единица(ed):
    """Замок под «множитель убран»: при `_bg_scale` == 1.0 разницы не видно."""
    editor, _g = ed
    assert editor._bg_scale != pytest.approx(1.0)


# ── 3.3 подпись диаметра видна только в «ОКР привязке» ───────────────────

@pytest.mark.parametrize("regime", REGIMES)
def test_подпись_диаметра_видна_только_в_окр_привязке(ed, regime):
    editor, _g = ed
    editor.set_display_regime(regime)
    seen = len(_visible_labels(editor))
    assert seen == (DIAM_COUNT if regime == "ocr" else 0), regime


def test_подпись_не_удаляется_а_прячется(ed):
    """`_update_edge_path` и `remove_edge_item` работают по `edge_label_items`."""
    editor, _g = ed
    editor.set_display_regime("base")
    assert len(_labels(editor)) == DIAM_COUNT


def test_видимость_переживает_переключение_листа_и_подложки(ed):
    """Хвост блока 1: оба переключателя «Оформления» пересобирают сцену."""
    editor, _g = ed
    editor.set_display_regime("ocr")
    editor.set_light_theme(not editor._light_theme)
    assert len(_visible_labels(editor)) == DIAM_COUNT
    editor.set_background_visible(not editor._bg_visible)
    assert len(_visible_labels(editor)) == DIAM_COUNT
    editor.set_display_regime("base")
    editor.set_light_theme(not editor._light_theme)
    assert _visible_labels(editor) == []


def test_возврат_в_окр_привязку_возвращает_подпись(ed):
    """Перебор ведётся ПОСЛЕ чужого состояния, а не с чистого листа."""
    editor, _g = ed
    for regime in ("base", "perp", "style", "ocr"):
        editor.set_display_regime(regime)
    assert len(_visible_labels(editor)) == DIAM_COUNT


def test_правка_диаметра_по_клику_работает_как_раньше(ed, monkeypatch):
    """Скрытая подпись жеста не отнимает: диалог зовут по ребру, а не по ней."""
    editor, g = ed
    opened = []
    monkeypatch.setattr(editor, "_open_diameter_edit_dialog",
                        lambda key: opened.append(key))
    editor.set_display_regime("ocr")
    e = g["links"][0]
    sy = (e["source_point"][0] + e["target_point"][0]) / 2.0   # [y, x]
    sx = (e["source_point"][1] + e["target_point"][1]) / 2.0
    _ctrl_dclick(editor, sx, sy)
    assert opened == [editor.model.edge_key(e["source"], e["target"])]


# ── 3.1 кегль подписей — из общей таблицы ────────────────────────────────

def test_кегль_подписи_диаметра_внутренний_и_таблице_файла_не_подчиняется(ed):
    """⛔ Решение Максима по доработке mefx-3.

    Подпись диаметра в FXML не печатается (замок —
    `tests/test_fxml_text_style.py`), паритета с файлом у неё нет, поэтому
    файловое число 18 к ней не относится: кегль внутренний, прежний.
    """
    editor, _g = ed
    editor.set_display_regime("ocr")
    assert {it.font().pointSize() for it in _labels(editor)} == {DIAM_PT}
    assert {it.font().pixelSize() for it in _labels(editor)} != {SIZE}
    assert "diameter" not in TEXT_STYLES


def test_кегль_подписи_диаметра_одинаков_и_в_проверке_схемы(qapp, tmp_path):
    """Внутреннее число одно на ВСЕ вкладки, а не только на холст.

    «Проверка схемы» — второй потребитель `_create_edge_label`; состояний
    отображения у неё нет, поэтому подпись там видна всегда, как и была.
    """
    from ui.editors.simple_graph_editor import SimpleGraphEditor

    editor = SimpleGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(*_write(tmp_path, _diameter_graph()))
    editor.resize(1400, 900)
    try:
        labels = _labels(editor)
        assert len(labels) == DIAM_COUNT
        assert {it.font().pointSize() for it in labels} == {DIAM_PT}
        assert len(_visible_labels(editor)) == DIAM_COUNT, \
            "у «Проверки схемы» состояний нет — прятать нечего"
    finally:
        _dispose(editor, qapp)


def test_кегль_подписи_текст_блока_из_общей_таблицы(ed):
    """Тот же кегль, что уйдёт в `<Text>` — и он же не умножен на вписывание."""
    editor, _g = ed
    editor.set_display_regime("ocr")
    labels = [pair["text"] for pair in editor._ocr_block_items.values()
              if pair.get("text") is not None]
    assert len(labels) == BLOCK_COUNT, "фикстура обязана нести текст-блоки"
    assert {it.font().pixelSize() for it in labels} == {SIZE}


def test_семейство_шрифта_на_экране_прежнее(ed):
    """Решение №7: общий у экрана и файла только КЕГЛЬ, Tahoma не бандлим."""
    editor, _g = ed
    editor.set_display_regime("ocr")
    assert {it.font().family() for it in _labels(editor)} == {"sans-serif"}
    blocks = [pair["text"] for pair in editor._ocr_block_items.values()
              if pair.get("text") is not None]
    assert {it.font().family() for it in blocks} == {"DejaVu Sans"}

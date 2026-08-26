# -*- coding: utf-8 -*-
"""Жесты Ду в редакторе привязки — блок 3 линии диаметров.

Дыра, названная красной командой: три возвращённых жеста не были покрыты ничем —
ни прямым вызовом, ни мышью. Здесь закрыты они и соседние отказы того же
вердикта: семантика «одно число = диаметр», снимки undo, обратимость гашения
OCR-бокса, живучесть состояния между листами.

Проверяется НАБЛЮДАЕМОЕ: какие метки появились, что покрашено, что в статусной
строке, сколько снимков в стеке. Ни одного утверждения про внутреннюю кухню.
"""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

PROJECT_YAML = Path("configs/projects/thermohydraulics/thermohydraulics.yaml")

#: Магистраль a—t—b (одна линия) и отвод t—p за `perehod` (стоп).
NODES = [
    {"id": "a", "class_name": "connector", "centroid": [100.0, 0.0]},
    {"id": "t", "class_name": "connector", "centroid": [100.0, 100.0]},
    {"id": "b", "class_name": "connector", "centroid": [100.0, 200.0]},
    {"id": "p", "class_name": "perehod", "centroid": [200.0, 100.0]},
    {"id": "c", "class_name": "connector", "centroid": [300.0, 100.0]},
]
LINKS = [
    {"id": "e1", "source": "a", "target": "t",
     "source_point": [100.0, 0.0], "target_point": [100.0, 100.0], "waypoints": []},
    {"id": "e2", "source": "t", "target": "b",
     "source_point": [100.0, 100.0], "target_point": [100.0, 200.0], "waypoints": []},
    {"id": "e3", "source": "t", "target": "p",
     "source_point": [100.0, 100.0], "target_point": [200.0, 100.0], "waypoints": []},
    {"id": "e4", "source": "p", "target": "c",
     "source_point": [200.0, 100.0], "target_point": [300.0, 100.0], "waypoints": []},
]

DIAM_LABEL = {"bbox": [90.0, 40.0, 130.0, 60.0], "text": "Dy300", "confidence": 0.9}
TAG_LABEL = {"bbox": [90.0, 40.0, 140.0, 60.0], "text": "IITB-56", "confidence": 0.9}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _graph():
    return {"nodes": [dict(n) for n in NODES], "links": [dict(e) for e in LINKS]}


def _editor(qapp, tmp_path, blocks, monkeypatch):
    from ui.editors.ocr_binding_editor import OcrBindingEditor

    img = tmp_path / "raster.png"
    QImage(400, 400, QImage.Format.Format_RGB32).save(str(img))

    ed = OcrBindingEditor()
    ed._project_config_dir = str(PROJECT_YAML.parent)
    ed.load_data(str(img), [dict(b) for b in blocks], _graph(), [])
    return ed


@pytest.fixture
def ed(qapp, tmp_path, monkeypatch):
    e = _editor(qapp, tmp_path, [DIAM_LABEL], monkeypatch)
    yield e
    e.deleteLater()


def _statuses(ed):
    seen = []
    ed.status_message.connect(seen.append)
    return seen


def _painted(ed):
    """Индексы рёбер, получивших Ду (то, что оператор видит покрашенным)."""
    return sorted(ed._diam_by_edge)


# ── Ctrl+drag подписи на ребро ─────────────────────────────────────────────

def test_podpis_du_privyazyvaetsya_bez_voprosov(ed, monkeypatch):
    """«Dy300» — явная подпись диаметра, спрашивать нечего."""
    asked = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: asked.append(1))

    ed._bind_to_edge(0, 0)

    assert asked == []
    assert [(m.edge_id, m.value, m.kind) for m in ed.get_diameter_marks()] == \
        [("e1", 300, "ocr")]
    assert _painted(ed) == [0, 1], "покрашена не вся линия"
    assert ed._bindings == [], "подпись Ду ушла ещё и в текстовые привязки"


def test_nomer_linii_ne_stanovitsya_diametrom_molcha(qapp, tmp_path, monkeypatch):
    """⛔ `IITB-56` — номер линии, а не Ду56.

    Замер редтима: 1616 OCR-блоков корпуса из 2331 содержат ровно одно число,
    и это сплошь номера и позиции. Прежнее правило «одно число = диаметр»
    превращало каждый в Ду молча и отнимало возможность привязать его к ребру
    как текст.
    """
    ed = _editor(qapp, tmp_path, [TAG_LABEL], monkeypatch)
    try:
        asked = []

        def _decline(self):
            asked.append(self.text())
            # «не диаметр» — кнопка с ролью Reject
            for btn in self.buttons():
                if self.buttonRole(btn) == QMessageBox.ButtonRole.RejectRole:
                    self.setProperty("_clicked", btn)
            return 0

        monkeypatch.setattr(QMessageBox, "exec", _decline)
        monkeypatch.setattr(QMessageBox, "clickedButton",
                            lambda self: self.property("_clicked"))

        ed._bind_to_edge(0, 0)

        assert asked, "оператора не спросили"
        assert "IITB-56" in asked[0]
        assert ed.get_diameter_marks() == []
        assert len(ed._bindings) == 1, "обычная привязка текста не состоялась"
        assert ed._bindings[0]["text"] == "IITB-56"
    finally:
        ed.deleteLater()


def test_operator_mozhet_podtverdit_chto_eto_diametr(qapp, tmp_path, monkeypatch):
    """Тот же текст, но оператор говорит «да, 56»."""
    ed = _editor(qapp, tmp_path, [TAG_LABEL], monkeypatch)
    try:
        def _accept(self):
            for btn in self.buttons():
                if self.buttonRole(btn) == QMessageBox.ButtonRole.ActionRole:
                    self.setProperty("_clicked", btn)
                    break
            return 0

        monkeypatch.setattr(QMessageBox, "exec", _accept)
        monkeypatch.setattr(QMessageBox, "clickedButton",
                            lambda self: self.property("_clicked"))

        ed._bind_to_edge(0, 0)
        assert [m.value for m in ed.get_diameter_marks()] == [56]
    finally:
        ed.deleteLater()


# ── Ctrl+2×клик: ввод и правка ─────────────────────────────────────────────

def test_ruchnoi_vvod_krasit_liniyu(ed, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **kw: ("250", True)))
    ed._create_diameter_on_edge(0, 0, 0)

    assert [m.value for m in ed.get_diameter_marks()] == [250]
    assert _painted(ed) == [0, 1]


def test_pravka_metki_perekrashivaet_vsyu_liniyu(ed, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **kw: ("250", True)))
    ed._create_diameter_on_edge(0, 0, 0)

    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **kw: ("300", True)))
    line_idx = ed._diameter_lines.line_for(0)
    ed._edit_diameter_on_line(line_idx, 1)          # клик по ДРУГОМУ ребру линии

    assert [m.value for m in ed.get_diameter_marks()] == [300]
    assert {ed._diam_by_edge[i]["value"] for i in _painted(ed)} == {300}


def test_otmena_dialoga_nichego_ne_menyaet(ed, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **kw: ("", False)))
    ed._create_diameter_on_edge(0, 0, 0)
    assert ed.get_diameter_marks() == []


# ── Ctrl+ПКМ: снять Ду с линии ─────────────────────────────────────────────

def test_snyatie_gasit_vsyu_liniyu(ed, monkeypatch):
    ed._bind_to_edge(0, 0)
    assert _painted(ed) == [0, 1]

    # клик по ВТОРОМУ ребру линии — метка стоит на первом
    assert ed._unbind_diameter_at(1) is True
    assert ed.get_diameter_marks() == []
    assert _painted(ed) == []


def test_snyatie_vozvraschaet_yarkost_ocr_boksa(ed):
    """Гашение до 35% обязано быть обратимым — иначе блок тусклый навсегда."""
    ed._bind_to_edge(0, 0)
    dimmed = [it.opacity() for it in ed._ocr_text_items.values()]
    assert dimmed and max(dimmed) < 0.5

    ed._unbind_diameter_at(0)
    assert min(it.opacity() for it in ed._ocr_text_items.values()) == 1.0


def test_snyatie_na_rebre_bez_du_ne_vrjot(ed):
    assert ed._unbind_diameter_at(2) is False


# ── undo ───────────────────────────────────────────────────────────────────

def test_odin_zhest_odin_snimok(ed, monkeypatch):
    before = len(ed._undo_stack)
    ed._bind_to_edge(0, 0)
    assert len(ed._undo_stack) == before + 1


def test_ctrl_z_vozvraschaet_sostoyanie_do_metki(ed):
    ed._bind_to_edge(0, 0)
    assert ed.get_diameter_marks()

    ed._undo()

    assert ed.get_diameter_marks() == []
    assert _painted(ed) == []


def test_snimok_perezhivaet_metku_datakalssom(ed):
    """`_push_undo` сериализует метки — датаклассы обязаны пережить json."""
    ed._bind_to_edge(0, 0)
    ed._create_diameter_on_edge  # noqa: B018 — просто чтобы жест был не один
    ed._undo()
    ed._bind_to_edge(0, 0)
    marks = ed.get_diameter_marks()
    assert [(m.edge_id, m.value, m.kind) for m in marks] == [("e1", 300, "ocr")]


# ── живучесть состояния ────────────────────────────────────────────────────

def test_povtornaya_zagruzka_ne_ostavlyaet_hvostov(qapp, tmp_path, monkeypatch):
    """Второй лист в том же редакторе: метки, разбиение и items — с нуля.

    Без сброса `scene.clear()` убивал C++-объекты, а перерисовка падала
    `RuntimeError: Internal C++ object already deleted` на ЛЮБОМ жесте.
    """
    ed = _editor(qapp, tmp_path, [DIAM_LABEL], monkeypatch)
    try:
        ed._bind_to_edge(0, 0)
        assert ed.get_diameter_marks()

        img = tmp_path / "raster2.png"
        QImage(400, 400, QImage.Format.Format_RGB32).save(str(img))
        ed.load_data(str(img), [dict(DIAM_LABEL)], _graph(), [])

        assert ed.get_diameter_marks() == []
        assert ed._diameter_label_rects == []
        ed._after_change()          # не должно упасть на мёртвых items
        ed._bind_to_edge(0, 0)      # и жест на новом листе работает
        assert _painted(ed) == [0, 1]
    finally:
        ed.deleteLater()


def test_smena_klassa_uzla_pereschityvaet_liniyu(ed):
    """Отпечаток разбиения обязан видеть классы узлов, а не только рёбра.

    Иначе экран показывал залитую магистраль, а на диск уходила её половина.
    """
    ed._bind_to_edge(0, 0)
    assert _painted(ed) == [0, 1]

    ed._graph_nodes[1]["class_name"] = "perehod"      # узел `t` стал стопом
    ed._rebuild_diameter_lines()
    ed._repropagate_diameters()

    assert _painted(ed) == [0], "линия не перестроилась под новый класс узла"


def test_bez_tablicy_klassov_metka_ne_stavitsya(qapp, tmp_path, monkeypatch):
    """Конфиг не прочитан → метка не ставится и оператор об этом слышит.

    Прежде метка создавалась, статус был зелёный, а `_stamp_diameters` молча
    возвращал 0: работа сеанса испарялась без единого признака.
    """
    ed = _editor(qapp, tmp_path, [DIAM_LABEL], monkeypatch)
    try:
        ed._project_config_dir = None
        ed._diameter_rules = None
        seen = _statuses(ed)

        ed._bind_to_edge(0, 0)

        assert ed.get_diameter_marks() == []
        assert any("не прочитана" in s for s in seen), seen
    finally:
        ed.deleteLater()


# ── конфликт с чужой записью ───────────────────────────────────────────────

def test_vybor_operatora_pobezhdaet_chuzhuyu_zapis(ed, monkeypatch):
    """Старый Ду без нашего источника правится только здесь: правка в «Ручной
    правке» снята, а `apply_marks` чужое не трогает по построению."""
    ed._graph_edges[1]["diameter_value"] = 250
    ed._graph_edges[1]["diameter_text"] = "Ду250"

    ed._bind_to_edge(0, 0)                     # наша метка 300 на ту же линию
    line_idx = ed._diameter_lines.line_for(0)
    assert line_idx in ed._diam_conflicts

    def _pick_300(self):
        for btn in self.buttons():
            if btn.text().endswith("300"):
                self.setProperty("_clicked", btn)
                break
        return 0

    monkeypatch.setattr(QMessageBox, "exec", _pick_300)
    monkeypatch.setattr(QMessageBox, "clickedButton",
                        lambda self: self.property("_clicked"))

    ed._resolve_conflict(line_idx)

    assert ed._diam_conflicts == {}, "конфликт не решился"
    assert {ed._diam_by_edge[i]["value"] for i in _painted(ed)} == {300}

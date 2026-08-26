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


def _answer(monkeypatch, text):
    """Ответить в диалоге кнопкой с таким текстом. Порядок кнопок — за Qt
    (роли), поэтому выбираем по подписи, а не по номеру."""
    def _exec(self):
        self._chosen = next(b for b in self.buttons() if b.text() == text)

    monkeypatch.setattr(QMessageBox, "exec", _exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self._chosen)


def _offered(monkeypatch, seen):
    """Запомнить, что предложили, и ответить первой кнопкой-числом."""
    def _exec(self):
        seen.append([b.text() for b in self.buttons()])
        self._chosen = next(b for b in self.buttons()
                            if b.text().startswith("Ø"))

    monkeypatch.setattr(QMessageBox, "exec", _exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self._chosen)


def test_teg_s_chislom_sprashivaet_diametr_li_eto(qapp, tmp_path, monkeypatch):
    """Подпись без образца «Ду» — оператора спрашивают (разворот 26.08).

    Молчаливый разбор превращал `IITB-56` в Ду56 без единого слова. Вопрос
    вернули: он «чётко определял, что нужно брать».
    """
    ed = _editor(qapp, tmp_path, [TAG_LABEL], monkeypatch)
    try:
        seen = []
        _offered(monkeypatch, seen)

        ed._bind_to_edge(0, 0)

        assert len(seen) == 1, "оператора не спросили"
        assert "Ø 56" in seen[0], seen[0]
        assert "не диаметр" in seen[0], seen[0]
        assert [m.value for m in ed.get_diameter_marks()] == [56]
        assert ed._bindings == []
    finally:
        ed.deleteLater()


def test_otvet_ne_diametr_nichego_ne_privyazyvaet(qapp, tmp_path, monkeypatch):
    """«не диаметр» — к трубе не привязывается ничего, в том числе текстом."""
    ed = _editor(qapp, tmp_path, [TAG_LABEL], monkeypatch)
    try:
        _answer(monkeypatch, "не диаметр")
        seen = _statuses(ed)

        ed._bind_to_edge(0, 0)

        assert ed.get_diameter_marks() == []
        assert ed._bindings == []
        assert seen and "не диаметр" in seen[-1], seen
    finally:
        ed.deleteLater()


def test_neskolko_chisel_predlagayutsya_na_vybor(qapp, tmp_path, monkeypatch):
    """«РОУ.С 25/13» — оператор выбирает, какое из чисел диаметр."""
    label = {"bbox": [90.0, 40.0, 150.0, 60.0], "text": "РОУ.С 25/13",
             "confidence": 0.9}
    ed = _editor(qapp, tmp_path, [label], monkeypatch)
    try:
        _answer(monkeypatch, "Ø 13")

        ed._bind_to_edge(0, 0)

        assert [m.value for m in ed.get_diameter_marks()] == [13]
    finally:
        ed.deleteLater()


def test_tekst_bez_chisel_k_trube_ne_privyazyvaetsya(qapp, tmp_path, monkeypatch):
    """К трубе — или диаметр, или ничего (решение оператора 26.08).

    Раньше текст без числа становился золотой привязкой к ребру, и линия
    выглядела «как с блоками» — оператор читал это как поставленный Ду.
    """
    label = {"bbox": [90.0, 40.0, 150.0, 60.0], "text": "тёплый ящик",
             "confidence": 0.9}
    ed = _editor(qapp, tmp_path, [label], monkeypatch)
    try:
        seen = _statuses(ed)
        ed._bind_to_edge(0, 0)
        assert ed.get_diameter_marks() == []
        assert ed._bindings == [], "текст всё-таки прилип к трубе"
        assert seen and "только Ду" in seen[-1], seen
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

def test_snyatie_edinstvennoi_metki_gasit_liniyu(ed):
    ed._bind_to_edge(0, 0)
    assert _painted(ed) == [0, 1]

    # клик по ВТОРОМУ ребру линии — метка стоит на первом
    assert ed._unbind_diameter_at(1) is True
    assert ed.get_diameter_marks() == []
    assert _painted(ed) == []


def test_snyatie_odnoi_iz_dvuh_metok_pereschityvaet_liniyu(ed):
    """⛔ Решение оператора 26.08: снимается ОДНА метка, а не все на линии.

    Раньше клик сносил все метки линии разом — работа терялась целиком.
    Теперь оставшаяся метка перекрашивает линию своим значением.
    """
    ed._add_diameter_mark(0, 300, "manual", "300")
    ed._add_diameter_mark(1, 300, "manual", "300")
    assert len(ed.get_diameter_marks()) == 2

    ed._unbind_diameter_at(0)          # сняли метку с первого ребра

    marks = ed.get_diameter_marks()
    assert len(marks) == 1 and marks[0].edge_id == "e2"
    assert _painted(ed) == [0, 1], "линия не пересчиталась по оставшейся метке"
    assert {ed._diam_by_edge[i]["value"] for i in _painted(ed)} == {300}


def test_snyatie_metki_menyaet_du_na_ostavsheesya(ed):
    """Две метки с разным Ду: сняли одну — линия берёт значение второй."""
    ed._add_diameter_mark(0, 300, "manual", "300")
    ed._add_diameter_mark(1, 250, "manual", "250")
    line = ed._diameter_lines.line_for(0)
    assert line in ed._diam_conflicts        # пока обе — конфликт

    ed._unbind_diameter_at(0)

    assert ed._diam_conflicts == {}
    assert {ed._diam_by_edge[i]["value"] for i in _painted(ed)} == {250}


def test_privyazka_pryachet_boks_a_otvyazka_vozvraschaet(ed):
    """Решение оператора 26.08: бокс исчезает, остаётся квадрат с числом.

    Скрытие обязано быть обратимым — иначе снятая метка оставляла бы свой блок
    невидимым навсегда, и оператор терял бы число с чертежа.
    """
    ed._bind_to_edge(0, 0)
    assert all(not it.isVisible() for it in ed._ocr_text_items.values())

    ed._unbind_diameter_at(0)
    assert all(it.isVisible() for it in ed._ocr_text_items.values())


def test_boks_ne_pereezzhaet_k_rebru(ed):
    """Он не двигается — поэтому и «возвращается на своё место» сам собой."""
    before = list(ed._ocr_blocks[0]["bbox"])
    ed._bind_to_edge(0, 0)
    assert ed._ocr_blocks[0]["bbox"] == before
    ed._unbind_diameter_at(0)
    assert ed._ocr_blocks[0]["bbox"] == before


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
        assert any("Диаметры выключены" in s for s in seen), seen
        # причина обязана дойти до оператора, а не остаться в логе
        assert any("конфиг" in s.lower() for s in seen), seen
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


# ── блок 6: счётчик покрытия и режим обхода ────────────────────────────────

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402


def _key(ed, key, text=""):
    """Клавиша БОЕВЫМ путём — через `event()`, а не прямым `keyPressEvent`.

    ⛔ Прямой вызов даёт ложный зелёный: Tab, например, Qt разбирает в
    `QWidget.event()` как переход фокуса и до `keyPressEvent` не доводит вовсе.
    Привязка на Tab «работала» в тесте и была мертва в приложении.
    """
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, text)
    QApplication.sendEvent(ed, ev)


def test_pokrytie_schitaet_linii_rebra_i_dlinu(ed):
    """Длина — главное число: top-10% линий держат 54% длины корпуса."""
    c = ed.diameter_coverage()
    assert c["lines_need"] == 3 and c["lines_done"] == 0
    assert c["len_need"] > 0 and c["len_done"] == 0

    ed._bind_to_edge(0, 0)

    c = ed.diameter_coverage()
    assert c["lines_done"] == 1
    assert 0 < c["len_done"] < c["len_need"]


def test_obhod_idet_ot_samoi_dlinnoi_linii(ed):
    """О-2: порядок по длине, а не по номеру."""
    order = ed.lines_without_diameter()
    lengths = [sum(ed._edge_length(i)
                   for i in ed._diameter_lines.edges_of_line[li]) for li in order]
    assert lengths == sorted(lengths, reverse=True)


def test_probel_vstaet_na_liniyu_bez_du(ed):
    _key(ed, Qt.Key.Key_Space, " ")
    assert ed._diam_current_line == ed.lines_without_diameter()[0]


def test_cikl_probel_enter_okno_enter(ed, monkeypatch):
    """Цикл оператора: Пробел — встали на линию, Enter — окно, Enter — поставили.

    Подтверждённый Ду сразу уводит обход дальше: подтверждение и есть шаг
    конвейера (решение оператора 26.08 — разворот прежнего «никуда не прыгать»).
    """
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **kw: ("300", True)))
    _key(ed, Qt.Key.Key_Space, " ")
    first = ed._diam_current_line

    _key(ed, Qt.Key.Key_Return)

    assert [m.value for m in ed.get_diameter_marks()] == [300]
    assert first not in ed.lines_without_diameter()
    assert ed._diam_current_line != first, "подтверждение не шагнуло дальше"
    assert ed._diam_current_line in ed.lines_without_diameter()


def test_enter_bez_podtverzhdeniya_stoit_na_meste(ed, monkeypatch):
    """«Просто энтер — и ничего»: отмена окна не двигает камеру и не ставит Ду."""
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **kw: ("", False)))
    _key(ed, Qt.Key.Key_Space, " ")
    first = ed._diam_current_line

    _key(ed, Qt.Key.Key_Return)

    assert ed.get_diameter_marks() == []
    assert ed._diam_current_line == first, "отменённое окно увело обход"


def test_ne_chislo_ne_dvigaet_obhod(ed, monkeypatch):
    """Набрали не число — линия остаётся под курсором, чтобы поправить набор."""
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **kw: ("Ду", True)))
    _key(ed, Qt.Key.Key_Space, " ")
    first = ed._diam_current_line

    _key(ed, Qt.Key.Key_Return)

    assert ed.get_diameter_marks() == []
    assert ed._diam_current_line == first


def test_okno_zaranee_zapolneno_proshlym_znacheniem(ed, monkeypatch):
    """Ду на листе повторяются — поле подставляет прошлое, хватает Enter."""
    seen = []

    def _dialog(parent, title, label, text="", *a, **kw):
        seen.append(text)
        return (text or "250", True)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(_dialog))
    _key(ed, Qt.Key.Key_Space, " ")
    _key(ed, Qt.Key.Key_Return)          # первое окно пустое -> набрали 250
    _key(ed, Qt.Key.Key_Space, " ")
    _key(ed, Qt.Key.Key_Return)          # второе уже с 250 -> просто Enter

    assert seen[0] == "", "в первом окне что-то уже стояло"
    assert seen[1] == "250", "прошлое значение не подставилось"
    assert sorted(m.value for m in ed.get_diameter_marks()) == [250, 250]


def test_cifry_i_backspace_vkladke_ne_perehvatyvayutsya(ed):
    """Набор идёт в окне ввода — там привычная правка работает сама.

    Раньше цифры копились в строке состояния, а Backspace перехватывался
    редактором. Оператор назвал это неправильным: «только в окне, как с
    текстом обычно».
    """
    _key(ed, Qt.Key.Key_Space, " ")
    assert ed._diam_current_line is not None
    _key(ed, Qt.Key.Key_3, "3")
    _key(ed, Qt.Key.Key_Backspace)
    assert ed.get_diameter_marks() == []



def test_esc_vyhodit_iz_obhoda(ed):
    _key(ed, Qt.Key.Key_Space, " ")
    assert ed._diam_current_line is not None
    _key(ed, Qt.Key.Key_Escape)
    assert ed._diam_current_line is None


def test_kogda_vse_zakryto_obhod_soobschaet_ob_etom(ed):
    seen = _statuses(ed)
    for li in list(ed.lines_without_diameter()):
        group = ed._diameter_lines.edges_of_line[li]
        ed._add_diameter_mark(group[0], 100, "manual", "100")

    assert ed.lines_without_diameter() == []
    assert ed.goto_next_line_without_diameter() is False
    assert any("закрыты" in s for s in seen), seen


def test_cifry_vne_obhoda_ne_perehvatyvayutsya(ed):
    """Пока обход не начат, клавиатура принадлежит остальной вкладке."""
    assert ed._diam_current_line is None
    _key(ed, Qt.Key.Key_3, "3")
    assert ed.get_diameter_marks() == []


def test_tab_ne_perehvatyvaetsya_on_prinadlezhit_fokusu(ed):
    """⛔ Tab остаётся клавишей перехода фокуса.

    Он и не мог бы работать: Qt разбирает его в `QWidget.event()` и до
    `keyPressEvent` не доводит (замер — `sendEvent(Tab)` до обработчика не
    доходит, `Space` и `F3` доходят). Сторож на случай, если кто-то снова
    решит, что «Tab = следующее» — тест на прямом вызове был бы зелёным.
    """
    _key(ed, Qt.Key.Key_Tab)
    assert ed._diam_current_line is None


def test_f3_takzhe_vedet_obhod(ed):
    _key(ed, Qt.Key.Key_F3)
    assert ed._diam_current_line == ed.lines_without_diameter()[0]


def test_bez_pyyaml_vkladka_otkryvaetsya(qapp, tmp_path, monkeypatch):
    """⛔ Клиентское окружение может быть БЕЗ PyYAML — вкладка обязана жить.

    Замер на боевом клиенте (`.venv311`): PySide6 есть, `yaml` нет. Модуль
    правила линии импортировал `yaml` на верхнем уровне, и `load_data` падал
    `ModuleNotFoundError` — вместе со всей привязкой текста, а не только с Ду.
    """
    import builtins

    from ui.editors.ocr_binding_editor import OcrBindingEditor

    real = builtins.__import__

    def no_yaml(name, *a, **kw):
        if name == "yaml" or name.startswith("yaml."):
            raise ModuleNotFoundError("No module named 'yaml'")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_yaml)

    img = tmp_path / "noyaml.png"
    QImage(200, 200, QImage.Format.Format_RGB32).save(str(img))
    ed = OcrBindingEditor()
    try:
        ed._project_config_dir = str(PROJECT_YAML.parent)
        ed.load_data(str(img), [], _graph(), [])      # не должно упасть

        seen = _statuses(ed)
        ed._add_diameter_mark(0, 300, "manual", "300")
        assert ed.get_diameter_marks() == []
        assert any("yaml" in s for s in seen), seen
    finally:
        ed.deleteLater()


def test_cvet_odin_nezavisimo_ot_sposoba_privyazki(ed, monkeypatch):
    """⛔ Требование оператора: у ребра с Ду ОДИН цвет, как бы его ни привязали.

    Перетащил подпись, набрал руками, пришло потоком по линии — на экране это
    одно и то же «ребро с диаметром». Разный цвет по источнику заставлял бы
    оператора помнить, откуда взялось число, а ему важно только «есть Ду».
    Отличается лишь КОНФЛИКТ — но это состояние линии, а не способ привязки.
    """
    def pens(editor):
        from PySide6.QtWidgets import QGraphicsLineItem
        return {(it.pen().color().name(), it.pen().width())
                for it in editor._diameter_items
                if isinstance(it, QGraphicsLineItem)}

    ed._bind_to_edge(0, 0)                       # перетаскиванием подписи
    by_drag = pens(ed)
    ed._unbind_diameter_at(0)

    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **kw: ("300", True)))
    ed._create_diameter_on_edge(0, 0, 0)         # руками
    by_hand = pens(ed)

    assert by_drag == by_hand, "цвет/толщина зависят от способа привязки"
    assert len(by_drag) == 1, "метка и поток нарисованы по-разному"


# ── квадрат с числом стоит на якорном ребре ────────────────────────────────

#: Одна линия из ДЛИННОГО и короткого ребра — чтобы «якорь» и «самое длинное»
#: не совпали и правило было видно.
SKEW_NODES = [
    {"id": "a", "class_name": "connector", "centroid": [100.0, 0.0]},
    {"id": "t", "class_name": "connector", "centroid": [100.0, 300.0]},
    {"id": "b", "class_name": "connector", "centroid": [100.0, 340.0]},
]
SKEW_LINKS = [
    {"id": "long", "source": "a", "target": "t",
     "source_point": [100.0, 0.0], "target_point": [100.0, 300.0], "waypoints": []},
    {"id": "short", "source": "t", "target": "b",
     "source_point": [100.0, 300.0], "target_point": [100.0, 340.0], "waypoints": []},
]


@pytest.fixture
def skew_ed(qapp, tmp_path):
    from ui.editors.ocr_binding_editor import OcrBindingEditor

    img = tmp_path / "skew.png"
    QImage(400, 400, QImage.Format.Format_RGB32).save(str(img))
    ed = OcrBindingEditor()
    ed._project_config_dir = str(PROJECT_YAML.parent)
    ed.load_data(str(img), [dict(DIAM_LABEL)],
                 {"nodes": [dict(n) for n in SKEW_NODES],
                  "links": [dict(e) for e in SKEW_LINKS]}, [])
    yield ed
    ed.deleteLater()


def _label_centers(ed):
    """Центры квадратов с числом, в координатах сцены (x, y)."""
    return [((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)
            for r in ed._diameter_label_rects]


def _edge_center(ed, edge_idx):
    """Середина ребра в координатах сцены. Точки графа — [y, x]!"""
    cy, cx = ed._edge_midpoint(edge_idx)
    return (cx, cy)


def _near(a, b, eps=2.0):
    return abs(a[0] - b[0]) < eps and abs(a[1] - b[1]) < eps


def test_kvadrat_stoit_na_yakornom_rebre(skew_ed):
    """Решение оператора 26.08: число появляется ТАМ, где сделан жест.

    Раньше квадрат уезжал на самое длинное ребро линии — оператор привязывал
    подпись в одном конце схемы, а число загоралось в другом.
    """
    assert skew_ed._add_diameter_mark(1, 300, "manual", "300")

    centers = _label_centers(skew_ed)
    assert len(centers) == 1, centers
    assert _near(centers[0], _edge_center(skew_ed, 1)), "квадрат не на якоре"
    assert not _near(centers[0], _edge_center(skew_ed, 0)), \
        "квадрат остался на самом длинном ребре"


def test_dve_metki_dva_kvadrata(skew_ed):
    """Сколько жестов сделал оператор — столько квадратов, а не по одному
    на ребро: два одинаковых Ду на линии конфликтом не считаются."""
    skew_ed._add_diameter_mark(0, 300, "manual", "300")
    skew_ed._add_diameter_mark(1, 300, "manual", "300")

    centers = _label_centers(skew_ed)
    assert len(centers) == 2, centers
    assert any(_near(c, _edge_center(skew_ed, 0)) for c in centers)
    assert any(_near(c, _edge_center(skew_ed, 1)) for c in centers)


def test_konflikt_pokazyvaet_odin_kvadrat(skew_ed):
    """Конфликт — исключение: один квадрат со всеми претендентами, по клику
    открывается выбор. Два разных числа рядом читались бы как «так и надо»."""
    skew_ed._add_diameter_mark(0, 300, "manual", "300")
    skew_ed._add_diameter_mark(1, 400, "manual", "400")

    assert skew_ed._diam_conflicts, "конфликт не распознан"
    assert len(_label_centers(skew_ed)) == 1


# ── подсветка цели броска ──────────────────────────────────────────────────

def test_podsvetka_celi_vidna_nad_sloem_du(ed):
    """Ctrl+drag на ребро с Ду: подсветка обязана быть ВИДНА.

    Ребро лежит на z=5, слой Ду рисуется поверх — жёлтая подсветка цели
    оказывалась под ним, и оператор не видел, куда попадёт подпись.
    """
    ed._add_diameter_mark(0, 300, "manual", "300")
    top = max(it.zValue() for it in ed._diameter_items)

    ed._highlight_edge(0)
    assert ed._edge_items[0].zValue() > top, "подсветка осталась под слоем Ду"

    was = ed._edge_items[0].zValue()
    ed._clear_highlights()
    assert ed._edge_items[0].zValue() < was, "ребро осталось поднятым"


# ── бокс не возвращается сам ───────────────────────────────────────────────

def test_boks_ostajotsya_spryatannym_posle_pererisovki(ed):
    """Массовая перерисовка слоя OCR не должна воскрешать спрятанный бокс:
    видимость считается из МЕТОК, а не из порядка вызовов отрисовки."""
    ed._bind_to_edge(0, 0)
    assert all(not it.isVisible() for it in ed._ocr_text_items.values())

    ed.refresh_ocr_layer()
    assert all(not it.isVisible() for it in ed._ocr_text_items.values()), \
        "бокс вернулся после refresh_ocr_layer"

    ed._apply_block_filter()
    assert all(not it.isVisible() for it in ed._ocr_text_items.values()), \
        "бокс вернулся после применения фильтра"


def test_otfiltrovannyi_boks_ne_vsplyvaet_pri_otvyazke(ed):
    """Отвязка возвращает бокс только в рамках общих правил видимости."""
    ed._bind_to_edge(0, 0)
    ed.set_block_filter(set())          # вкладка спрятала все блоки
    ed._unbind_diameter_at(0)
    assert all(not it.isVisible() for it in ed._ocr_text_items.values())

    ed.set_block_filter(None)
    assert all(it.isVisible() for it in ed._ocr_text_items.values())


def test_otkaz_metki_ne_stanovitsya_zolotoi_privyazkoi(qapp, tmp_path, monkeypatch):
    """Подпись с числом брошена как Ду — отказ обязан остаться отказом.

    Раньше неудача постановки метки проваливалась в обычную текстовую привязку:
    бокс золотел, ребро золотело, а причина отказа затиралась бодрым
    «Привязано → ребро». Оператор читал это как «сработало».
    """
    from ui.editors.ocr_binding_editor import OcrBindingEditor

    img = tmp_path / "noid.png"
    QImage(400, 400, QImage.Format.Format_RGB32).save(str(img))
    links = [dict(e) for e in LINKS]
    for e in links:
        e.pop("id")                      # рёбер без id метке держать не на чем
    ed = OcrBindingEditor()
    ed._project_config_dir = str(PROJECT_YAML.parent)
    ed.load_data(str(img), [dict(DIAM_LABEL)],
                 {"nodes": [dict(n) for n in NODES], "links": links}, [])
    seen = _statuses(ed)

    ed._bind_to_edge(0, 0)

    assert ed.get_diameter_marks() == []
    assert ed.get_bindings() == [], "отказ обернулся текстовой привязкой"
    assert seen and "id" in seen[-1], seen
    ed.deleteLater()


# ── настоящий жест мыши: Ctrl+drag подписи на трубу ────────────────────────

def _mouse(ed, kind, scene_pt, button, buttons):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    return QMouseEvent(kind, QPointF(ed.mapFromScene(QPointF(*scene_pt))),
                       button, buttons, Qt.KeyboardModifier.ControlModifier)


def _ctrl_drag(ed, frm, to):
    """Ctrl+ЛКМ от точки сцены `frm` до `to` — как рукой оператора."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QFrame

    ed.setFrameShape(QFrame.Shape.NoFrame)
    ed.resize(400, 400)
    ed.show()
    ed.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Control,
                               Qt.KeyboardModifier.NoModifier))
    ed.mousePressEvent(_mouse(ed, QEvent.Type.MouseButtonPress, frm,
                              Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
    ed.mouseMoveEvent(_mouse(ed, QEvent.Type.MouseMove, to,
                             Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton))
    ed.mouseReleaseEvent(_mouse(ed, QEvent.Type.MouseButtonRelease, to,
                                Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))


def test_ctrl_drag_myshyu_stavit_du_i_pryachet_boks(ed):
    """Весь жест целиком, событиями мыши: подпись Ду брошена на трубу.

    Прямой вызов `_bind_to_edge` мимо этого пути проходил зелёным, а на экране
    оператор получал переставленный бокс — путь мыши обязан быть покрыт.
    """
    _ctrl_drag(ed, (110.0, 50.0), (50.0, 100.0))   # бокс → на трубу a—t

    assert [m.value for m in ed.get_diameter_marks()] == [300]
    assert ed._ocr_blocks[0]["bbox"] == list(DIAM_LABEL["bbox"]), "бокс уехал"
    assert all(not it.isVisible() for it in ed._ocr_text_items.values())


def test_brosok_mimo_truby_nazyvaet_prichinu(ed):
    """Промах не молчит: «Бокс перемещён» без причины оператор читал как
    поломку привязки — он целился в трубу, а слов не было ни одного."""
    seen = _statuses(ed)
    _ctrl_drag(ed, (110.0, 50.0), (350.0, 350.0))   # далеко от всех труб

    assert ed.get_diameter_marks() == []
    assert seen and "Цели под боксом нет" in seen[-1], seen
    assert "px" in seen[-1], seen[-1]


# ── разбор подписи, испорченной OCR ────────────────────────────────────────

def test_du_prochtennyi_kak_0y_ostajotsya_diametrom():
    """OCR системно читает «Ду» как «0y» — замер на листе оператора.

    Разбор брал ПЕРВОЕ число и, увидев ведущий ноль, сдавался: подпись «0y50»
    считалась текстом без числа и к трубе не привязывалась вовсе.
    """
    from ui.editors.ocr_binding_editor import _confident_diameter, _first_number

    for text, value in (("0y50", 50), ("0y200", 200), ("Оу100", 100),
                        ("Dy150", 150), ("Ду300", 300), ("DN80", 80)):
        assert _first_number(text) == value, text
        assert _confident_diameter(text) == value, text


def test_kks_teg_diametrom_ne_priznajotsya():
    """Цена расширения образца: тег не должен стать «уверенным» Ду."""
    from ui.editors.ocr_binding_editor import _confident_diameter

    for text in ("10LAH04 AA103", "10LFN10 AA017", "VTB-107", "ICBF-1"):
        assert _confident_diameter(text) is None, text


def test_ctrl_drag_podpisi_0y50_stavit_du(qapp, tmp_path, monkeypatch):
    """Весь путь целиком на подписи с листа оператора, а не на чистой «Dy300»."""
    label = {"bbox": [90.0, 40.0, 130.0, 60.0], "text": "0y50", "confidence": 0.9}
    ed = _editor(qapp, tmp_path, [label], monkeypatch)
    try:
        _ctrl_drag(ed, (110.0, 50.0), (50.0, 100.0))
        assert [m.value for m in ed.get_diameter_marks()] == [50]
        assert all(not it.isVisible() for it in ed._ocr_text_items.values())
    finally:
        ed.deleteLater()

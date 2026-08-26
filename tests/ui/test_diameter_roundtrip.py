# -*- coding: utf-8 -*-
"""Блок 4 линии Ду: сохранение и восстановление меток диаметра.

Приёмка блока — «разметил → сохранил → закрыл → открыл: всё на месте, дублей
нет». Проверяется на ДАННЫХ, ушедших на сервер, и на состоянии редактора после
восстановления: ни одного утверждения про внутреннюю кухню вкладки.

Почему это отдельный тест, а не круг в юнитах модуля: круг
`apply_marks → marks_from_edges → apply_marks` там уже есть, а здесь ловится
проводка — что вкладка отдаёт серверу тот же граф, что показывает оператору,
и что при открытии она не создаёт вторую метку на то же ребро.

⛔ Ду хранится ТОЛЬКО в графе. Второго хранилища (записи в `ocr_binding.json`)
нет сознательно: два ответа на «что показать при открытии» дают дубли. Тест
запирает это с обеих сторон — и что метка вернулась, и что в `ocr_binding.json`
диаметров нет.
"""
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from modules.binding.diameter_lines import DiameterMark  # noqa: E402

UID = "d1a11e70"
PROJECT_YAML = Path("configs/projects/thermohydraulics/thermohydraulics.yaml")

#: Магистраль a—t—b (одна линия, сшивается на транзитном узле степени 2)
#: и отвод t—c за `perehod` (стоп-класс, отдельная линия).
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

LINE_EDGES = {"e1", "e2"}       # линия, которую красит метка на e1
OFF_LINE = {"e3", "e4"}         # отвод и кусок за переходом — не её дело


class FakeAPI:
    """Сервер: помнит разобранное содержимое заливок."""

    def __init__(self):
        self.uploads = {}

    def _take(self, kind, path):
        with open(path, encoding="utf-8") as fh:
            self.uploads[kind] = json.load(fh)
        return True

    def save_ocr_binding(self, uid, path):
        return self._take("binding", path)

    def upload_validated_graph(self, uid, path):
        return self._take("graph", path)

    def save_ocr_validation(self, uid, path):
        return self._take("validation", path)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_tab(qapp, monkeypatch, nodes=None, links=None):
    """Вкладка на графе из фикстуры; `nodes`/`links` — для своего графа."""
    from ui.tabs.ocr_binding_tab import OcrBindingTab

    monkeypatch.setattr(OcrBindingTab, "_start_download", lambda self: None)
    api = FakeAPI()
    tab = OcrBindingTab(UID, "проба Ду", api)

    tab._graph_data = {
        "nodes": [dict(n) for n in (nodes if nodes is not None else NODES)],
        "links": [dict(e) for e in (links if links is not None else LINKS)],
    }
    tab._classifications = []

    ed = tab.editor
    ed._graph_nodes = tab._graph_data["nodes"]
    ed._graph_edges = tab._graph_data["links"]
    ed._project_config_dir = str(PROJECT_YAML.parent)
    ed._ocr_blocks = []
    ed._bindings = []
    ed._kks_bindings = []
    ed._validation_results = []
    tab._saved = False
    return tab, api


@pytest.fixture
def tab(qapp, monkeypatch):
    t, api = _make_tab(qapp, monkeypatch)
    yield t, api
    t.cleanup()


def _edges_by_id(graph):
    return {e["id"]: e for e in graph["links"]}


# ── сохранение ─────────────────────────────────────────────────────────────

def test_metka_uezzhaet_na_server_vsei_liniei(tab):
    """Одна метка на `e1` — Ду получают оба ребра линии и только они."""
    t, api = tab
    t.editor.set_diameter_marks([DiameterMark("e1", 300, "manual", "300")])

    assert t._save_binding() is True

    saved = _edges_by_id(api.uploads["graph"])
    for eid in LINE_EDGES:
        assert saved[eid]["diameter_value"] == 300, eid
    for eid in OFF_LINE:
        assert "diameter_value" not in saved[eid], eid


def test_istochnik_razlichaet_metku_i_potok_na_diske(tab):
    t, api = tab
    t.editor.set_diameter_marks([DiameterMark("e1", 300, "manual", "300")])
    t._save_binding()

    saved = _edges_by_id(api.uploads["graph"])
    assert saved["e1"]["diameter_source"] == "manual"
    assert saved["e1"]["diameter_propagated"] is False
    assert saved["e2"]["diameter_source"] == "line"
    assert saved["e2"]["diameter_propagated"] is True


def test_v_ocr_binding_diametrov_net(tab):
    """Хранилище одно — граф. Иначе на открытии будет два ответа и дубли."""
    t, api = tab
    t.editor.set_diameter_marks([DiameterMark("e1", 300, "manual", "300")])
    t._save_binding()

    dumped = json.dumps(api.uploads["binding"], ensure_ascii=False)
    assert "diameter" not in dumped
    assert "300" not in dumped


def test_razmetka_du_ne_ustarivaet_holst(tab):
    """`graph_projection_sha` не должна сдвинуться — иначе раскладка
    перезапустится после каждой правки диаметра."""
    from modules.graph.core import canvas_state

    t, api = tab
    before = canvas_state.graph_projection_sha(
        {"nodes": NODES, "links": [dict(e) for e in LINKS]})

    t.editor.set_diameter_marks([DiameterMark("e1", 300, "manual", "300")])
    t._save_binding()

    assert canvas_state.graph_projection_sha(api.uploads["graph"]) == before


# ── восстановление ─────────────────────────────────────────────────────────

def test_krug_metka_vozvraschaetsya_bez_dublei(qapp, monkeypatch, tab):
    """Разметил → сохранил → открыл заново: метка одна и та же."""
    t, api = tab
    t.editor.set_diameter_marks([DiameterMark("e1", 300, "manual", "300")])
    t._save_binding()
    saved_graph = api.uploads["graph"]

    t2, api2 = _make_tab(qapp, monkeypatch)
    try:
        t2._graph_data = saved_graph
        t2.editor._graph_nodes = saved_graph["nodes"]
        t2.editor._graph_edges = saved_graph["links"]
        t2._restore_bindings_from_graph()

        marks = t2.editor.get_diameter_marks()
        assert len(marks) == 1
        assert marks[0].edge_id == "e1"
        assert marks[0].value == 300
        assert marks[0].kind == "manual"

        # Второе сохранение не плодит ни меток, ни рёбер с Ду.
        t2._save_binding()
        again = _edges_by_id(api2.uploads["graph"])
        assert sum(1 for e in again.values() if e.get("diameter_value")) == 2
        assert len(t2.editor.get_diameter_marks()) == 1
    finally:
        t2.cleanup()


def test_potok_ne_vozvraschaetsya_metkoi(qapp, monkeypatch, tab):
    """Ребро с `source == "line"` — не метка: иначе после каждого открытия
    меток становилось бы столько же, сколько рёбер в линии."""
    t, api = tab
    t.editor.set_diameter_marks([DiameterMark("e1", 300, "manual", "300")])
    t._save_binding()

    t2, _ = _make_tab(qapp, monkeypatch)
    try:
        t2._graph_data = api.uploads["graph"]
        t2.editor._graph_nodes = t2._graph_data["nodes"]
        t2.editor._graph_edges = t2._graph_data["links"]
        t2._restore_bindings_from_graph()
        assert [m.edge_id for m in t2.editor.get_diameter_marks()] == ["e1"]
    finally:
        t2.cleanup()


# ── чужие записи ───────────────────────────────────────────────────────────

def test_chuzhaya_zapis_perezhivaet_sohranenie(tab):
    """Ду без нашего источника вкладка не стирает: снести диаметр, которого мы
    не ставили, значит молча стереть чужую работу."""
    t, api = tab
    t._graph_data["links"][3]["diameter_value"] = 250     # e4, за переходом
    t.editor.set_diameter_marks([DiameterMark("e1", 300, "manual", "300")])

    t._save_binding()

    saved = _edges_by_id(api.uploads["graph"])
    assert saved["e4"]["diameter_value"] == 250


def test_snyatie_vseh_metok_ochischaet_tolko_svoe(tab):
    """Оператор снял все метки — наши поля уходят, чужие остаются."""
    t, api = tab
    t.editor.set_diameter_marks([DiameterMark("e1", 300, "manual", "300")])
    t._save_binding()

    t._graph_data = api.uploads["graph"]
    t.editor._graph_nodes = t._graph_data["nodes"]
    t.editor._graph_edges = t._graph_data["links"]
    t._graph_data["links"][3]["diameter_value"] = 250      # чужая запись на e4
    t.editor.set_diameter_marks([])

    t._save_binding()

    saved = _edges_by_id(api.uploads["graph"])
    for eid in LINE_EDGES:
        assert "diameter_value" not in saved[eid], eid
    assert saved["e4"]["diameter_value"] == 250


def test_otkrytie_vkladki_podnimaet_metki_iz_grafa():
    """⛔ Сторож ПРОВОДКИ: боевой путь открытия обязан звать восстановление.

    Тесты выше зовут `_restore_bindings_from_graph` напрямую и потому не видели
    главного: с 2026-07-01 (`5c6d3f4`, вместе с подвкладками) вызов был снят из
    `_on_download_finished` и не вернулся. Вкладка открывалась пустой, метки не
    поднимались, а следующее сохранение сносило Ду с сервера — `_stamp_diameters`
    без меток чистит своё. Замер редтима: сеанс 2 уезжал на сервер с `[{}, {}]`.

    Сторож структурный сознательно: поведенческий прогон всего
    `_on_download_finished` требует полного набора артефактов и сети, а потерять
    здесь можно ровно одну строку — её и стережём.
    """
    import inspect

    from ui.tabs.ocr_binding_tab import OcrBindingTab

    src = inspect.getsource(OcrBindingTab._on_download_finished)
    assert "_restore_bindings_from_graph" in src, (
        "боевой путь открытия вкладки не поднимает метки Ду из графа — "
        "следующее сохранение сотрёт их с сервера"
    )


# ── инструкция по Ду ───────────────────────────────────────────────────────

def test_instrukciya_po_du_dostupna_iz_tulbara(tab):
    """Жесты Ду нигде не подписаны — кнопка обязана их называть.

    Tooltip и окно берут ОДИН текст: подсказка на наведении и по клику не
    должны разъезжаться.
    """
    from ui.tabs.ocr_binding_tab import DIAMETER_HELP

    t, _api = tab
    assert t.btn_diam_help.toolTip() == DIAMETER_HELP
    assert t.btn_diam_help.text() == "Ø", "значок диаметра на кнопке потерян"
    for gesture in ("Ctrl+drag", "Ctrl+2×клик", "Пробел", "Enter",
                    "Ctrl+ПКМ", "Ctrl+Z"):
        assert gesture in DIAMETER_HELP, gesture


def test_schjotchik_pokrytiya_obyasnyaet_svoi_cifry(tab):
    """«линии 1/48 · длина 1%» без подсказки не читается."""
    from ui.tabs.ocr_binding_tab import STATS_HELP

    t, _api = tab
    assert t.stats_label.toolTip() == STATS_HELP
    assert "длина" in STATS_HELP and "не требуется" in STATS_HELP


def test_knopka_du_fiktivnaya_i_ne_lovit_probel(tab, monkeypatch):
    """Кнопка живёт ради подсказки и ничего не вызывает (решение 26.08).

    Фокус ей запрещён нарочно: сфокусированная кнопка съела бы Пробел и Enter,
    а это клавиши обхода линий без Ду.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QMessageBox

    t, _api = tab
    opened = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: opened.append(self))
    monkeypatch.setattr(QDialog, "exec", lambda self: opened.append(self))
    before = list(t.editor.get_diameter_marks())

    t.btn_diam_help.click()

    assert opened == [], "фиктивная кнопка что-то открыла"
    assert list(t.editor.get_diameter_marks()) == before
    assert t.btn_diam_help.focusPolicy() == Qt.FocusPolicy.NoFocus


# ── замок «Подтвердить» при неразрешённых конфликтах Ду ────────────────────

def _record(seen):
    """Заглушка сервера: помнит вызов и отвечает так же, как настоящий API."""
    def _apply(uid):
        seen.append(uid)
        return {"updated_nodes": 0}
    return _apply


def _conflict(t):
    """Две метки с разными Ду на ОДНОЙ линии (e1 и e2 — одна магистраль)."""
    t.editor.set_diameter_marks([
        DiameterMark("e1", 300, "manual", "300"),
        DiameterMark("e2", 400, "manual", "400"),
    ])


def test_konflikt_vidno_snaruzhi(tab):
    """Вкладка обязана уметь спросить редактор про конфликты до сохранения."""
    t, _api = tab
    assert t.editor.diameter_conflicts() == []
    _conflict(t)
    conflicts = t.editor.diameter_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0][1] == [300, 400]


def test_zamok_ne_puskaet_podtverzhdenie_s_konfliktom(tab, monkeypatch):
    """Линия с двумя Ду уедет проставленной НАПОЛОВИНУ, а пустое ребро в
    расчётной схеме — заводской Ду300. Выпускать такое молча нельзя."""
    from PySide6.QtWidgets import QMessageBox

    t, api = tab
    _conflict(t)
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(self.text()))
    before = dict(api.uploads)
    applied = []
    monkeypatch.setattr(t.api_client, "apply_ocr_binding",
                        _record(applied), raising=False)

    t._on_confirm()

    assert shown, "оператора не предупредили"
    assert "разными диаметрами" in shown[0], shown
    assert api.uploads == before, "схема всё-таки ушла на сервер"
    assert applied == [], "привязки всё-таки применили"


def test_bez_konflikta_zamok_ne_meshaet(tab, monkeypatch):
    t, api = tab
    t.editor.set_diameter_marks([DiameterMark("e1", 300, "manual", "300")])
    applied = []
    monkeypatch.setattr(t.api_client, "apply_ocr_binding",
                        _record(applied), raising=False)

    t._on_confirm()

    assert applied == [UID], "замок сработал там, где конфликта нет"
    assert "graph" in api.uploads


def test_razreshjonnyi_konflikt_otkryvaet_dver(tab, monkeypatch):
    """Оператор выбрал одно значение — дверь открывается без перезапуска."""
    from PySide6.QtWidgets import QMessageBox

    t, api = tab
    _conflict(t)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: None)
    t._on_confirm()
    assert "graph" not in api.uploads

    t.editor.set_diameter_marks([DiameterMark("e1", 300, "manual", "300")])
    applied = []
    monkeypatch.setattr(t.api_client, "apply_ocr_binding",
                        _record(applied), raising=False)

    t._on_confirm()

    assert applied == [UID]


def test_flag_ne_trebuetsya_uezzhaet_na_server(qapp, monkeypatch):
    """Контракт с расчётной схемой: «Ду не нужен» едет ЯВНО, полем на ребре.

    Иначе канал без Ду и канал, которому Ду не положен, для конвертера
    неразличимы — оба остаются с заводскими 0.3 м, то есть приезжают в САПФИР
    честным Ду300. Дренаж под видом Ду300 — самая дорогая ошибка цепочки.
    """
    from modules.binding.diameter_lines import NOT_REQUIRED_FIELD

    # Отвод к датчику уходит от развилки `t` вкось. Продолжение магистрали
    # (хоть прямо, хоть под 90°) сшилось бы с ней в ОДНУ линию, и тогда
    # «не требуется» накрыло бы всю магистраль — проверено на этой же фикстуре.
    nodes = [dict(n) for n in NODES] + [
        {"id": "d", "class_name": "datchik", "centroid": [50.0, 150.0]}]
    links = [dict(e) for e in LINKS] + [
        {"id": "e5", "source": "t", "target": "d",
         "source_point": [100.0, 100.0], "target_point": [50.0, 150.0],
         "waypoints": []}]
    t, api = _make_tab(qapp, monkeypatch, nodes=nodes, links=links)
    try:
        t.editor.set_diameter_marks([DiameterMark("e1", 300, "manual", "300")])
        assert t._save_binding() is True

        saved = _edges_by_id(api.uploads["graph"])
        flagged = sorted(eid for eid, e in saved.items()
                         if e.get(NOT_REQUIRED_FIELD))
        assert flagged == ["e5"], flagged
        assert NOT_REQUIRED_FIELD not in saved["e1"], "флаг поверх Ду"
    finally:
        t.cleanup()


def test_list_bez_edinogo_du_podtverzhdaetsya_svobodno(tab, monkeypatch):
    """Замок стоит на КОНФЛИКТЕ, а не на отсутствии Ду.

    Схема без единого диаметра обязана проходить «Подтвердить» как раньше:
    Ду — не обязательное поле конвейера, и запирать на нём дверь никто не
    просил.
    """
    t, api = tab
    t.editor.set_diameter_marks([])
    assert t.editor.diameter_conflicts() == []
    applied = []
    monkeypatch.setattr(t.api_client, "apply_ocr_binding",
                        _record(applied), raising=False)

    t._on_confirm()

    assert applied == [UID], "замок сработал на схеме вообще без Ду"
    assert "graph" in api.uploads

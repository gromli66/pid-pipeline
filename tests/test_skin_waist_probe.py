# -*- coding: utf-8 -*-
"""Пробник талий `tools/skin_waist_probe.py` (Р6, блок 2 road/waist-seat).

Логика доли и сверки — без среды. Живой рендер JavaFX — только при заданных
JAVA_HOME (JDK с JavaFX), PID_SKIN_JAR (custom-control-skin-based-1.6.0.jar) и
PID_SLF4J_JAR (jar с org.slf4j); без них skip (в CI jar заказчика нет).
"""
from __future__ import annotations

import pytest

from tools import skin_waist_probe as probe


def _rec(orient, frame, tip):
    """Запись WAIST: бабочка из двух треугольников с общей вершиной tip."""
    x0, y0, x1, y1 = frame
    return {"skin": "S", "orient": orient, "frame": list(frame),
            "left": [[x0, y0], [x0, y1], list(tip)],
            "right": [list(tip), [x1, y0], [x1, y1]]}


# ---------------------------------------------------------------------------
# Без среды
# ---------------------------------------------------------------------------


def test_waist_sign_follows_table_convention():
    """H — ниже центра (+), V — левее (+), VR — правее (+); доля от поперечного."""
    fr = (30.0, 30.0, 72.0, 67.8)                        # 42 x 37.8, центр (51, 48.9)
    f, px = probe.waist(_rec("HORIZONTAL", fr, (51.0, 60.78)))
    assert px == pytest.approx(11.88) and f == pytest.approx(11.88 / 37.8)
    f, px = probe.waist(_rec("HORIZONTAL", fr, (51.0, 47.36)))
    assert px == pytest.approx(-1.54) and f < 0          # талия ВЫШЕ центра
    fv = (32.1, 27.9, 69.9, 69.9)                        # 37.8 x 42, центр (51, 48.9)
    f, px = probe.waist(_rec("VERTICAL", fv, (45.36, 48.9)))
    assert px == pytest.approx(5.64) and f == pytest.approx(5.64 / 37.8)
    f, px = probe.waist(_rec("VERTICAL_REVERSE", fv, (56.64, 48.9)))
    assert px == pytest.approx(5.64) and f == pytest.approx(5.64 / 37.8)


def test_waist_without_single_common_vertex_is_none():
    fr = (0.0, 0.0, 42.0, 21.0)
    r = _rec("HORIZONTAL", fr, (21.0, 10.5))
    r["right"][0] = [21.0, 11.5]                         # вершины разошлись
    assert probe.waist(r) is None
    assert probe.waist({**r, "left": None}) is None


def test_targets_read_working_size_from_code():
    tg = {skin: (cn, w, h) for cn, skin, w, h in probe.targets()}
    assert len(tg) == 10
    assert tg["AOC_VLV"] == ("armatura_seroprivod", 28.0, 42.0)
    assert tg["AOC_CTRL_VLV"] == ("regulator_seroprivod", 42.0, 31.5)
    assert tg["CTRL_VLV_ELEC"] == ("regulator_electro", 42.0, 37.8)
    assert tg["HANDLE_VLV"] == ("armatura_ruchn", 42.0, 21.0)


def test_mismatches_use_h_for_horizontal_v_for_vertical():
    rows = [{"skin": "X", "orient": o, "span": 37.8, "frac": 0.2, "px": 7.56}
            for o in probe.ORIENTS]
    assert probe.mismatches(rows, 0.1, {"X": (0.2, 0.25)}) == [
        ("X", "VERTICAL", 0.2, 0.25, pytest.approx(-1.89)),
        ("X", "VERTICAL_REVERSE", 0.2, 0.25, pytest.approx(-1.89))]
    assert probe.mismatches(rows, 0.1, {"X": (0.2, 0.2)}) == []
    # скин вне таблицы — симметричный (0, 0)
    assert [m[:2] for m in probe.mismatches(rows, 0.1, {})] == [
        ("X", o) for o in probe.ORIENTS]


def test_measure_takes_one_root_and_export_miss_is_reported():
    """Записи двух корней: таблица — Pane, AnchorPane (корень выгрузки) — справка
    о промахе: худший на скин и только больше допуска."""
    fr = (30.0, 30.0, 72.0, 61.5)                        # 42 x 31.5, центр y 45.75
    recs = []
    for root, tip_y in (("Pane", 45.75 + 8.064), ("AnchorPane", 45.75 + 8.39)):
        r = _rec("HORIZONTAL", fr, (51.0, tip_y))
        r.update(skin="AOC_CTRL_VLV", root=root)
        recs.append(r)
    old = _rec("HORIZONTAL", fr, (51.0, 45.75))          # запись без root — Pane
    old["skin"] = "X"
    tg = [("regulator_seroprivod", "AOC_CTRL_VLV", 42.0, 31.5), ("c", "X", 42.0, 31.5)]
    pane = probe.measure(recs + [old], tg)
    assert [(r["skin"], round(r["px"], 3)) for r in pane] == [
        ("AOC_CTRL_VLV", 8.064), ("X", 0.0)]
    anchor = probe.measure(recs + [old], tg, "AnchorPane")
    assert [round(r["px"], 3) for r in anchor] == [8.39]
    table = {"AOC_CTRL_VLV": (0.256, 0.256)}
    assert probe.mismatches(pane, 0.1, table) == []
    over = probe.print_export(anchor, 0.1, table)
    assert list(over) == ["AOC_CTRL_VLV"]
    assert over["AOC_CTRL_VLV"][0] == "HORIZONTAL"
    assert over["AOC_CTRL_VLV"][1] == pytest.approx(8.39 - 0.256 * 31.5)
    assert probe.print_export(anchor, 0.4, table) == {}


def test_no_environment_is_exit_2(tmp_path, monkeypatch, capsys):
    """Без JDK/jar — «судить нечем» (2), не «совпало» и не «расхождение»."""
    for k in ("JAVA_HOME", "PID_SKIN_JAR", "PID_SLF4J_JAR"):
        monkeypatch.delenv(k, raising=False)
    assert probe.main(["--check"]) == 2
    assert probe.main(["--check", "--java-home", str(tmp_path / "nojdk"),
                       "--jar", str(tmp_path / "a.jar"),
                       "--slf4j", str(tmp_path / "b.jar")]) == 2
    assert "судить нечем" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Живой рендер JavaFX
# ---------------------------------------------------------------------------


def _env():
    try:
        return probe.resolve_env()
    except probe.Unavailable as e:
        return str(e)


_ENV = _env()
needs_javafx = pytest.mark.skipif(
    isinstance(_ENV, str), reason=f"среды пробника нет: {_ENV}")

# Замер 2026-09-23 (scratchpad skin-crop/out2.txt), решение Р6: доля на рабочем
# размере, одна на скин (H == V == VR).
MEASURED = {
    "HANDLE_VLV": 0.0, "CHECK_VLV": 0.0,
    "ELECTRIC_VLV": 0.2222, "ELECTROMAGNETIC_VLV": 0.1492,
    "CTRL_VLV_ELEC": 0.3143, "CTRL_VLV_HANDLE": 0.1314,
    "CHECK_HYDRO_VLV": -0.0407, "AOC_CTRL_VLV": 0.2561,
    "AOC_VLV": 0.3409, "RELIEF_VLV": 0.1667,
}

# Таблица deploy 238fe6b (graph_to_fxml.py:223-232 базы), без мёртвой строки
# AOC_VLV_V2_NPP: её замер обязан найти расходящимися ровно эти 8 скинов.
OLD_TABLE = {
    "ELECTRIC_VLV": (0.222, 0.20), "AOC_VLV": (0.336, 0.336),
    "CHECK_HYDRO_VLV": (0.15, 0.15), "CTRL_VLV_ELEC": (0.15, 0.15),
    "RELIEF_VLV": (0.15, 0.15), "CTRL_VLV_HANDLE": (0.10, 0.111),
    "AOC_CTRL_VLV": (0.15, 0.111),
}


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """Один прогон рендера: записи обоих корней и каталог снятых PNG."""
    png = tmp_path_factory.mktemp("skins")
    return probe.render(_ENV, probe.targets(), png), png


@pytest.fixture(scope="module")
def rows(rendered):
    return probe.measure(rendered[0], probe.targets())


@needs_javafx
def test_render_matches_measurement(rows):
    assert len(rows) == 30
    for r in rows:
        assert r["frac"] == pytest.approx(MEASURED[r["skin"]], abs=5e-4), r
    ctrl = [r for r in rows if r["skin"] == "CTRL_VLV_ELEC"]
    assert {round(r["px"], 2) for r in ctrl} == {11.88}


@needs_javafx
def test_old_table_diverges_on_eight_skins(rows):
    bad = probe.mismatches(rows, 0.1, OLD_TABLE)
    assert sorted({b[0] for b in bad}) == [
        "AOC_CTRL_VLV", "AOC_VLV", "CHECK_HYDRO_VLV", "CTRL_VLV_ELEC",
        "CTRL_VLV_HANDLE", "ELECTRIC_VLV", "ELECTROMAGNETIC_VLV", "RELIEF_VLV"]
    # ELECTRIC_VLV расходится только вертикалью (0.20 против 0.222)
    assert sorted(b[1] for b in bad if b[0] == "ELECTRIC_VLV") == [
        "VERTICAL", "VERTICAL_REVERSE"]
    rounded = {k: (round(v, 3), round(v, 3)) for k, v in MEASURED.items()}
    assert probe.mismatches(rows, 0.1, rounded) == []


@needs_javafx
def test_contact_table_matches_render(rows):
    """Р6 (шаг 2b): таблица холста `contact.SKIN_CONTACT_OFFSET` совпадает с
    рендером на рабочем размере (== `skin_waist_probe.py --check` exit 0)."""
    from modules.graph.core import contact
    assert probe.mismatches(rows, 0.1, contact.SKIN_CONTACT_OFFSET) == []


@needs_javafx
def test_export_root_anchorpane_misses_the_waist_by_known_amount(rendered):
    """Ограничение выгрузки (MEASUREMENTS §130.5): корень AnchorPane округляет
    pref-размер вверх (31.5 -> 32, 37.8 -> 38), контрол растёт и поворачивается
    вокруг другого центра. Промах талии против таблицы больше 0.15 px — только
    AOC_CTRL_VLV H (+0.33) и RELIEF_VLV V/VR (−0.26, в знаке таблицы); у рамок 37.8 V/VR — 0.1."""
    from modules.graph.core import contact
    anchor = probe.measure(rendered[0], probe.targets(), "AnchorPane")
    assert len(anchor) == 30
    d = {(r["skin"], probe.SHORT[r["orient"]]):
         r["px"] - probe.table_frac(r["skin"], r["orient"]) * r["span"] for r in anchor}
    big = {k: round(v, 2) for k, v in d.items() if abs(v) > 0.15}
    assert big == {("AOC_CTRL_VLV", "H"): 0.33, ("RELIEF_VLV", "V"): -0.26,
                   ("RELIEF_VLV", "VR"): -0.26}
    assert contact.SKIN_CONTACT_OFFSET["AOC_CTRL_VLV"] == (0.256, 0.256)


@needs_javafx
def test_editor_skin_pngs_are_the_probe_render(rendered):
    """PNG-скины клапанов в ui/resources/skins — это `--png` пробника: те же
    размеры (рабочая рамка x10) и те же пиксели (расходится > 32/255 меньше
    0.1% пикселей — сглаживание; подпись «0%» дала бы больше)."""
    Image = pytest.importorskip("PIL.Image")
    np = pytest.importorskip("numpy")
    skins = probe.ROOT / "ui" / "resources" / "skins"
    for cn, _skin, _w, _h in probe.targets():
        got = np.asarray(Image.open(rendered[1] / f"{cn}.png").convert("RGBA"), float)
        have = np.asarray(Image.open(skins / f"{cn}.png").convert("RGBA"), float)
        assert got.shape == have.shape, cn
        assert (np.abs(got - have).max(axis=2) > 32).mean() < 1e-3, cn

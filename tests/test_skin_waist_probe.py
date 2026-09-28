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
def rows():
    return probe.measure(probe.render(_ENV, probe.targets()), probe.targets())


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

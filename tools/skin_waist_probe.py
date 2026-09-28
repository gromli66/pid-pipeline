# -*- coding: utf-8 -*-
"""skin_waist_probe.py — замер талий клапанов JavaFX-рендером (Р6, блок 2 road/waist-seat).

Талия — общая вершина `#leftPoly`/`#rightPoly` скина `ValveControl` («бабочка»),
куда на холсте входит труба (`modules/graph/core/contact.py`, `SKIN_CONTACT_OFFSET`).
Пробник рендерит каждый клапан на РАБОЧЕМ размере (`pretransform.FIXED_SIZES`, скин
по `contact.CLASS_NAME_TO_SKIN`) в HORIZONTAL / VERTICAL / VERTICAL_REVERSE и печатает
сдвиг талии от центра рамки: долю поперечного размера и px. Знак — как в таблице:
H — талия НИЖЕ центра (+), V — ЛЕВЕЕ (+), VR — ПРАВЕЕ (+).

Рендер идёт в двух корнях. Таблица и `--check` — корень Pane (геометрия контрола
как есть). Второй столбик — корень AnchorPane, как у выгрузки `canvas_to_fxml`: он
округляет pref-размер вверх до целого (31.5 -> 32, 37.8 -> 38), и талия уезжает на
0.1–0.33 px. Это ограничение выгрузки, код выхода оно не меняет (MEASUREMENTS §130.5).

`--png DIR` снимает PNG-скины «Ручной правки»: `DIR/<class_name>.png` — контрол
на рабочей рамке x10, ровно по номинальной рамке, фон прозрачный, в базе редактора
(вертикаль, привод справа). Редактор растягивает такой PNG на bbox без обрезки по
альфе (`_skin_pixmap_for`, `_fit_pixmap`) и рисует талию там же, где САПФИР.

Нужны (в git их нет): JDK с JavaFX (Liberica Full), jar скинов
`custom-control-skin-based-1.6.0.jar` и jar с `org.slf4j` (например
`scenebuilder-21.0.0-all.jar`). Java компилируется и запускается во временном
каталоге (cwd тоже там — `logs/` библиотеки в репо не попадает).

    python -X utf8 tools/skin_waist_probe.py --jar <skins.jar> --slf4j <slf4j.jar> \
        [--java-home <JDK>] [--check] [--tol 0.1] [--png ui/resources/skins]

То же из окружения: JAVA_HOME, PID_SKIN_JAR, PID_SLF4J_JAR.
Выход: 0 — таблица совпала с замером (или без --check); 1 — расхождение больше
--tol px (перечень скинов); 2 — «судить нечем» (нет JDK/jar или рендер не прошёл).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.graph.core import contact  # noqa: E402
from modules.graph.core.pretransform import FIXED_SIZES  # noqa: E402

JAVA_SRC = Path(__file__).resolve().parent / "skin_probe" / "WaistProbe.java"
ORIENTS = ("HORIZONTAL", "VERTICAL", "VERTICAL_REVERSE")
ROOTS = ("Pane", "AnchorPane")      # геометрия таблицы / корень выгрузки canvas_to_fxml
SHORT = {"HORIZONTAL": "H", "VERTICAL": "V", "VERTICAL_REVERSE": "VR"}
TIMEOUT_S = 180


class Unavailable(RuntimeError):
    """Среды нет или рендер не прошёл — «судить нечем» (exit 2)."""


def targets():
    """[(class_name, skin, W, H)] — клапаны с рабочим размером, по одному на скин."""
    out, seen = [], set()
    for cn, info in contact.CLASS_NAME_TO_SKIN.items():
        if info[0] != "ValveControl" or cn not in FIXED_SIZES or info[1] in seen:
            continue
        seen.add(info[1])
        w, h = FIXED_SIZES[cn]
        out.append((cn, info[1], float(w), float(h)))
    return out


def resolve_env(java_home=None, jar=None, slf4j=None):
    """(javac, java, jar, slf4j) или Unavailable с причиной."""
    java_home = java_home or os.environ.get("JAVA_HOME")
    jar = jar or os.environ.get("PID_SKIN_JAR")
    slf4j = slf4j or os.environ.get("PID_SLF4J_JAR")
    if not java_home:
        raise Unavailable("нет JAVA_HOME / --java-home (нужен JDK с JavaFX)")
    exe = ".exe" if os.name == "nt" else ""
    javac = Path(java_home) / "bin" / ("javac" + exe)
    java = Path(java_home) / "bin" / ("java" + exe)
    if not javac.is_file() or not java.is_file():
        raise Unavailable(f"в {java_home} нет bin/javac или bin/java")
    if not jar or not Path(jar).is_file():
        raise Unavailable("нет jar скинов (--jar / PID_SKIN_JAR)")
    if not slf4j or not Path(slf4j).is_file():
        raise Unavailable("нет jar с org.slf4j (--slf4j / PID_SLF4J_JAR)")
    return str(javac), str(java), str(jar), str(slf4j)


def render(env, tg, png_dir=None):
    """Компиляция и прогон WaistProbe во временном каталоге -> записи WAIST.

    png_dir — туда же снять PNG-скины `<class_name>.png` (база редактора, рамка x10).
    """
    javac, java, jar, slf4j = env
    args = []
    for _cn, skin, w, h in tg:
        for o in ORIENTS:
            args += [skin, o, repr(w), repr(h)]
    with tempfile.TemporaryDirectory(prefix="waist_probe_") as tmp:
        if png_dir is not None:
            args = ["--png=" + tmp] + args
        shutil.copy(JAVA_SRC, tmp)
        try:
            c = subprocess.run([javac, "-cp", jar, "-d", tmp, "WaistProbe.java"],
                               cwd=tmp, capture_output=True, text=True,
                               timeout=TIMEOUT_S)
            if c.returncode != 0:
                raise Unavailable("javac: " + (c.stderr or c.stdout).strip()[-800:])
            cp = os.pathsep.join([jar, slf4j, tmp])
            r = subprocess.run([java, "-cp", cp, "WaistProbe", *args],
                               cwd=tmp, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise Unavailable(f"запуск java: {e}") from e
        recs = [json.loads(ln[6:]) for ln in r.stdout.splitlines()
                if ln.startswith("WAIST ")]
        want = len(tg) * len(ORIENTS) * len(ROOTS)
        if r.returncode != 0 or len(recs) != want:
            raise Unavailable(f"рендер: код {r.returncode}, записей {len(recs)} "
                              f"из {want}; " + (r.stderr or "").strip()[-800:])
        if png_dir is not None:
            Path(png_dir).mkdir(parents=True, exist_ok=True)
            for cn, skin, _w, _h in tg:
                shutil.copyfile(Path(tmp) / f"{skin}.png", Path(png_dir) / f"{cn}.png")
    return recs


def waist(rec):
    """(frac, px) сдвига талии от центра рамки в знаке таблицы; None — нет бабочки."""
    left, right = rec.get("left"), rec.get("right")
    if not left or not right:
        return None
    common = [p for p in left
              if any(abs(p[0] - q[0]) < 1e-3 and abs(p[1] - q[1]) < 1e-3 for q in right)]
    if len(common) != 1:
        return None
    tx, ty = common[0]
    x0, y0, x1, y1 = rec["frame"]
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    o = rec["orient"]
    if o == "HORIZONTAL":
        px, span = ty - cy, y1 - y0
    elif o == "VERTICAL":
        px, span = cx - tx, x1 - x0
    else:
        px, span = tx - cx, x1 - x0
    return px / span, px


def table_frac(skin, orient, table=None):
    table = contact.SKIN_CONTACT_OFFSET if table is None else table
    h, v = table.get(skin, (0.0, 0.0))
    return h if orient == "HORIZONTAL" else v


def measure(recs, tg, root="Pane"):
    """Строки замера корня root: [{class, skin, W, H, orient, frac, px, span}]."""
    cls = {skin: (cn, w, h) for cn, skin, w, h in tg}
    rows = []
    for rec in recs:
        if rec.get("root", "Pane") != root:
            continue
        cn, w, h = cls[rec["skin"]]
        m = waist(rec)
        x0, y0, x1, y1 = rec["frame"]
        span = (y1 - y0) if rec["orient"] == "HORIZONTAL" else (x1 - x0)
        rows.append({"class": cn, "skin": rec["skin"], "W": w, "H": h,
                     "orient": rec["orient"], "span": span,
                     "frac": None if m is None else m[0],
                     "px": None if m is None else m[1]})
    return rows


def mismatches(rows, tol_px, table=None):
    """[(skin, orient, frac, table_frac, delta_px)] при |Δ| > tol_px или без талии."""
    bad = []
    for r in rows:
        tf = table_frac(r["skin"], r["orient"], table)
        if r["frac"] is None:
            bad.append((r["skin"], r["orient"], None, tf, None))
            continue
        d = r["px"] - tf * r["span"]
        if abs(d) > tol_px:
            bad.append((r["skin"], r["orient"], r["frac"], tf, d))
    return bad


def print_table(rows, table=None):
    by = {}
    for r in rows:
        by.setdefault((r["skin"], r["class"], r["W"], r["H"]), {})[r["orient"]] = r
    print(f"{'skin':<20} {'class':<25} {'W x H':<11} "
          + " ".join(f"{SHORT[o] + ' frac':>9} {'px':>7}" for o in ORIENTS)
          + f" {'table h':>8} {'table v':>8}")
    for (skin, cn, w, h), per in by.items():
        cells = []
        for o in ORIENTS:
            r = per.get(o)
            if r is None or r["frac"] is None:
                cells.append(f"{'—':>9} {'—':>7}")
            else:
                cells.append(f"{r['frac']:>9.4f} {r['px']:>7.3f}")
        th, tv = table_frac(skin, "HORIZONTAL", table), table_frac(skin, "VERTICAL", table)
        print(f"{skin:<20} {cn:<25} {f'{w:g}x{h:g}':<11} " + " ".join(cells)
              + f" {th:>8.3f} {tv:>8.3f}")


def print_export(rows, tol_px, table=None):
    """Промах талии в корне выгрузки (AnchorPane) против таблицы — справка."""
    worst = {}
    for r in rows:
        if r["frac"] is None:
            continue
        d = r["px"] - table_frac(r["skin"], r["orient"], table) * r["span"]
        cur = worst.get(r["skin"])
        if cur is None or abs(d) > abs(cur[1]):
            worst[r["skin"]] = (r["orient"], d)
    over = {k: v for k, v in worst.items() if abs(v[1]) > tol_px}
    print(f"\nвыгрузка (корень AnchorPane, pref-размер округлён вверх): "
          f"промах талии больше {tol_px} px — {len(over)} из {len(worst)}")
    for skin, (o, d) in sorted(over.items(), key=lambda kv: -abs(kv[1][1])):
        print(f"  {skin} {SHORT[o]}: {d:+.3f} px")
    return over


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--java-home")
    ap.add_argument("--jar", help="custom-control-skin-based-1.6.0.jar")
    ap.add_argument("--slf4j", help="jar с org.slf4j (scenebuilder-*-all.jar)")
    ap.add_argument("--check", action="store_true",
                    help="сверить с contact.SKIN_CONTACT_OFFSET")
    ap.add_argument("--tol", type=float, default=0.1,
                    help="допуск сверки, px (по умолчанию 0.1)")
    ap.add_argument("--png", metavar="DIR",
                    help="снять PNG-скины клапанов в DIR/<class_name>.png")
    a = ap.parse_args(argv)
    tg = targets()
    try:
        recs = render(resolve_env(a.java_home, a.jar, a.slf4j), tg, a.png)
    except Unavailable as e:
        print(f"судить нечем: {e}", file=sys.stderr)
        return 2
    rows = measure(recs, tg)
    print_table(rows)
    print_export(measure(recs, tg, "AnchorPane"), a.tol)
    if a.png:
        print(f"\nPNG-скины: {len(tg)} в {a.png}")
    if not a.check:
        return 0
    bad = mismatches(rows, a.tol)
    if not bad:
        print(f"\n--check: таблица совпала с замером (допуск {a.tol} px)")
        return 0
    print(f"\n--check: РАСХОЖДЕНИЕ больше {a.tol} px — {len(bad)}:")
    for skin, o, f, tf, d in bad:
        if f is None:
            print(f"  {skin} {SHORT[o]}: талия не найдена (нет общей вершины бабочки)")
        else:
            print(f"  {skin} {SHORT[o]}: замер {f:.4f}, таблица {tf:.3f}, Δ {d:+.2f} px")
    print("расходятся: " + ", ".join(sorted({b[0] for b in bad})))
    return 1


if __name__ == "__main__":
    sys.exit(main())

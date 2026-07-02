#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_skin_geometry.py — вытащить геометрию скинов прямо из JAR библиотеки.

Зачем: чтобы таблица аспектов (skin_geometry.json) не подбиралась руками, а
считалась из custom-control-skin-based-*.jar. Для каждого класса скина берём
getPreferredWidth / getPreferredHeight / getAspectRatio (константы в байткоде) и
имя ресурс-FXML. AR = prefH/prefW — то соотношение, которое skin.resize()
сохраняет; если бокс контрола не совпадает с ним, графика съезжает.

Требует:  pip install jawa
Запуск:   python3 tools/extract_skin_geometry.py custom-control-skin-based-1.6.0.jar -o tools/skin_geometry_full.json
"""
import argparse, json, os, tempfile, zipfile
from pathlib import Path

try:
    from jawa.classloader import ClassLoader
except ImportError:
    raise SystemExit("нужен jawa:  pip install jawa --break-system-packages")


def method_consts(cf, mname):
    out = []
    for m in cf.methods:
        if m.name.value != mname or not m.code:
            continue
        for ins in m.code.disassemble():
            if ins.mnemonic in ("ldc", "ldc_w", "ldc2_w"):
                c = cf.constants.get(ins.operands[0].value)
                v = getattr(c, "value", None)
                if isinstance(v, (int, float)):
                    out.append(round(float(v), 4))
            elif ins.mnemonic in ("bipush", "sipush"):
                out.append(float(ins.operands[0].value))
    return out


def first(lst, d=None):
    return lst[0] if lst else d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jar")
    ap.add_argument("-o", "--output", default="skin_geometry_full.json")
    ap.add_argument("--prefix", default="ru/get/common/controls/skins",
                    help="откуда брать классы скинов")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="skinjar_")
    with zipfile.ZipFile(args.jar) as z:
        z.extractall(tmp)
    cl = ClassLoader(tmp)

    rows = {}
    base = os.path.join(tmp, args.prefix)
    for root, _, files in os.walk(base):
        for fn in files:
            if not fn.endswith(".class") or "$" in fn:
                continue
            name = os.path.join(root, fn).replace(tmp + "/", "").replace(".class", "")
            try:
                cf = cl.load(name)
            except Exception:
                continue
            methods = {m.name.value for m in cf.methods}
            if not ({"getPreferredWidth", "getPreferredHeight"} & methods):
                continue
            strs = [s.string.value for s in cf.constants if type(s).__name__ == "String"]
            res = [s for s in strs if s and s[:1].islower() and s.isalnum() and 3 < len(s) < 30]
            pw = first(method_consts(cf, "getPreferredWidth"))
            ph = first(method_consts(cf, "getPreferredHeight"))
            ar = first(method_consts(cf, "getAspectRatio"))
            cls = fn[:-6]
            aspect = ar if ar else (round(ph / pw, 4) if (pw and ph) else None)
            rows[cls] = {
                "skin_class": cls,
                "package": root.split("/skins/")[-1],
                "resource": first(res),
                "pref_w": pw, "pref_h": ph,
                "aspect_hw": aspect,
                "square": (abs(aspect - 1.0) < 1e-3) if aspect else None,
            }

    Path(args.output).write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(rows)} скинов -> {args.output}")


if __name__ == "__main__":
    main()

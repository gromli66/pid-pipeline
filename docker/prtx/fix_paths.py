# -*- coding: utf-8 -*-
"""Переписать абсолютные windows-пути коробки на пути образа.

Default.reg лежит в UTF-16 и держит абсолютные пути (LibName -> ClassLib_CMS.csl
и др.). Без этого движок молча не грузит библиотеку классов и на выходе
вырожденный .prtx (замер 2026-08-21: 3 КБ вместо 82 КБ).
"""
import pathlib

WIN = "C:" + "\\\\" + "project" + "\\\\" + "prt_convertor" + "\\\\" + "converter-box" + "\\\\" + "engine"
NEW = "/opt/box/engine"

for name in ("Default.reg", "Default_lib.reg"):
    p = pathlib.Path("/opt/box/engine/SETTINGS") / name
    if not p.is_file():
        continue
    raw = p.read_bytes()
    enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
    out = []
    for line in raw.decode(enc, errors="replace").splitlines():
        if "prt_convertor" in line:
            line = line.replace(WIN, NEW)
            head, sep, rest = line.partition("=")
            line = head + sep + rest.replace("\\\\", "/")
        out.append(line)
    p.write_bytes("\r\n".join(out).encode(enc))
    print("пути переписаны:", name)

# -*- coding: utf-8 -*-
"""cmp_bitexact.py — главный критерий Э2: порт раскладки не изменил поведения.

Сравнивает выход `modules.graph.core.layout.layout()` с эталоном стенда
`_scratch/layout_align/out/<uid8>/s1/v56_final/result.json` БИТ-В-БИТ по всем
восьми графам корпуса. Рефакторинг доказывается равенством, а не осмотром.

Вход графа берётся из `storage/diagrams/<uid>/graph/graph_validated.json` и
переводится в холст модулем `canvas_input.to_canvas`. Для `5137af27` сырого
входа не существует (потерян до Э0) — его канвас-вход лежит в архиве Э0
(`inputs_recovered/5137af27/layout_input_canvas.json`) и подставляется
ключом `--canvas-input-dir`, где файл называется `5137af27.json`.

Запуск (из корня репо):
    python -X utf8 tools/cmp_bitexact.py
    python -X utf8 tools/cmp_bitexact.py --uid 51b339ab --canvas-input-dir D:\\rec
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modules.graph.core.layout import LayoutParams, layout      # noqa: E402
from modules.graph.core.canvas_input import to_canvas           # noqa: E402

# uid8 -> полное имя каталога в storage/diagrams; None = сырой вход потерян
CORPUS = {
    "51b339ab": "51b339ab-3e4c-429c-ac5f-49c44cb9c755",
    "a6d28736": "a6d28736-5039-4f40-9f7a-37b476f6fafb",
    "8d517a35": "8d517a35-0435-48d7-bc17-300345a15162",
    "89ca7583": "89ca7583-233e-44da-b30b-f2f08eb2567d",
    "13d1ef5f": "13d1ef5f-d251-4a88-bfd4-e0b51f144b69",
    "6e7144d5": "6e7144d5-19fc-4661-b90a-f26dcaa8c71a",
    "d74eb9f1": "d74eb9f1-668d-4596-891f-d4e5a16d04d0",
    "5137af27": None,
}
# §7 SOLUTION.md — дефекты до и после раздвигания
EXPECT = {"51b339ab": (311, 10), "a6d28736": (128, 6), "8d517a35": (81, 2),
          "89ca7583": (26, 1), "13d1ef5f": (20, 1), "6e7144d5": (14, 0),
          "5137af27": (8, 0), "d74eb9f1": (0, 0)}

DEFAULT_REF = REPO / "_scratch" / "layout_align" / "out"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def load_input(uid8, canvas_dir):
    """Граф в координатах холста — вход раскладки."""
    if canvas_dir:
        p = Path(canvas_dir) / f"{uid8}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")), f"canvas:{p.name}"
    full = CORPUS[uid8]
    if full is None:
        return None, "сырого входа нет и канвас-вход не передан"
    src = REPO / "storage" / "diagrams" / full / "graph" / "graph_validated.json"
    if not src.exists():
        return None, f"нет {src}"
    g, _t = to_canvas(json.loads(src.read_text(encoding="utf-8")))
    return g, "graph_validated"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", action="append", default=None,
                    help="uid8; можно повторять. По умолчанию — весь корпус")
    ap.add_argument("--ref-dir", default=str(DEFAULT_REF),
                    help="каталог out/ стенда с эталоном v56_final")
    ap.add_argument("--canvas-input-dir", default=None,
                    help="каталог с <uid8>.json — готовыми канвас-входами "
                         "(для графов с потерянным graph_validated)")
    ap.add_argument("--dump-dir", default=None,
                    help="куда сложить полученные result.json для разбора")
    args = ap.parse_args()

    uids = args.uid or list(CORPUS)
    ref_dir = Path(args.ref_dir)
    dump = Path(args.dump_dir) if args.dump_dir else None
    if dump:
        dump.mkdir(parents=True, exist_ok=True)

    rows, failed = [], 0
    for uid8 in uids:
        g, src_kind = load_input(uid8, args.canvas_input_dir)
        if g is None:
            rows.append((uid8, "-", "-", "ВХОДА НЕТ", src_kind, 0.0))
            failed += 1
            print(f"{uid8}: ПРОПУЩЕН — {src_kind}", flush=True)
            continue

        t0 = time.perf_counter()
        g, st = layout(g, LayoutParams())
        dt = time.perf_counter() - t0

        got = json.dumps(g, ensure_ascii=False).encode("utf-8")
        if dump:
            (dump / f"{uid8}.json").write_bytes(got)

        ref_path = ref_dir / uid8 / "s1" / "v56_final" / "result.json"
        if not ref_path.exists():
            verdict, shas = "ЭТАЛОНА НЕТ", ""
            failed += 1
        else:
            ref = ref_path.read_bytes()
            ok = got == ref
            verdict = "бит-в-бит" if ok else "РАСХОДИТСЯ"
            shas = f"{sha(ref)} / {sha(got)}"
            if not ok:
                failed += 1

        d = f"{st['defects_before']} -> {st['defects_after']}"
        exp = EXPECT.get(uid8)
        if exp and (st["defects_before"], st["defects_after"]) != exp:
            d += f"  (§7 ждёт {exp[0]} -> {exp[1]})"
            failed += 1
        rows.append((uid8, len(g.get("nodes", [])), d, verdict, shas, dt))
        print(f"{uid8}: {d}, {verdict}, {dt:.1f}s", flush=True)

    print()
    print("| uid | узлов | дефекты | эталон v56_final | sha эталон / прогон | время |")
    print("|---|---|---|---|---|---|")
    for uid8, n, d, verdict, shas, dt in rows:
        print(f"| {uid8} | {n} | {d} | {verdict} | {shas} | {dt:.1f}s |")
    print()
    print("ИТОГ:", "ПАРИТЕТ ДОКАЗАН" if not failed
          else f"НЕ СОШЛОСЬ, проблемных графов: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

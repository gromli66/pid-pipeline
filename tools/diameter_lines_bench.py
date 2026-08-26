# -*- coding: utf-8 -*-
"""diameter_lines_bench.py — стенд правила линии Ду на боевом корпусе.

Корпуса нет в git (`storage/diagrams/*` в .gitignore), поэтому корпусные
проверки живут стендом, а не тестом: `suite_baseline` параметризации по корпусу
из карты исключает, и тест на несуществующих данных дал бы «усыхание набора».

Контракт как у остальных стендов проекта (`suite_baseline.py`, `edit_bench.py`):

    python -X utf8 tools/diameter_lines_bench.py --check            # exit 1 при регрессии
    python -X utf8 tools/diameter_lines_bench.py --write-baseline   # пересъём базы

Что судится:

1. **Детерминизм разбиения.** Порядок `links` в файле не инвариант
   (`modules/graph/core/canvas_state.py`), поэтому перестановка рёбер обязана
   давать то же разбиение на КАЖДОМ листе. До детерминированной склейки жадный
   обход в порядке инцидентности расходился на 8 листах из 24.
2. **Числа правила** — линий, требующих Ду, «Ду не требуется», экономия.
   Сравниваются с базой ПОЛИСТНО и только там, где сам граф не менялся: корпус
   живёт на диске оператора и переписывается при каждом сохранении схемы, а
   сверка абсолютных сумм краснела бы от чужой работы. У листа в базе лежит sha
   его `graph_validated.json`; разошёлся sha — лист печатается как «не судим»,
   пропал или появился — печатается тоже.
3. **Смешанных линий ноль** — на линию не должны попасть две подписи Ду с
   разными значениями. Подписи берутся из `ocr/ocr_result.json` тем же
   способом, что и в замере: ближайшее ребро, порог 150 px.

Корпус берётся из `--corpus`, переменной `PID_CORPUS` или `storage/diagrams`.
"""

import argparse
import collections
import hashlib
import json
import os
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.binding.diameter_lines import LineRules, build_lines  # noqa: E402
from modules.binding.geometry import bbox_to_edge_distance_weighted  # noqa: E402

BASELINE = Path(__file__).with_name("bench") / "diameter_lines_baseline.json"
PROJECT_YAML = "configs/projects/thermohydraulics/thermohydraulics.yaml"

# Подпись диаметра на чертеже: «Ду300», «Dy 300», «DN300».
DIAM_RE = re.compile(r"[DdДд][yYvVнНnNуУ]\s*(\d{2,4})", re.IGNORECASE)
LABEL_MAX_DIST = 150.0
SHUFFLE_SEED = 7


def corpus_dirs(root: Path):
    return sorted(d for d in root.glob("*") if (d / "graph" / "graph_validated.json").exists())


def read_graph(uid_dir: Path):
    raw = (uid_dir / "graph" / "graph_validated.json").read_bytes()
    g = json.loads(raw.decode("utf-8"))
    sha = hashlib.sha256(raw).hexdigest()[:16]
    return g.get("links", []), g.get("nodes", []), sha


def partition_ids(edges, lines):
    """Разбиение как множество множеств `id` — сравнение без оглядки на порядок."""
    return {
        frozenset(str(edges[i].get("id") or i) for i in group)
        for group in lines.edges_of_line
    }


def labels_on_lines(uid_dir: Path, edges, lines):
    """{номер линии: множество Ду с подписей}. Пустой, если OCR нет."""
    ocr = uid_dir / "ocr" / "ocr_result.json"
    if not ocr.exists():
        return {}
    with open(ocr, encoding="utf-8") as f:
        blocks = json.load(f).get("target") or []
    hits = collections.defaultdict(set)
    for b in blocks:
        m = DIAM_RE.search((b.get("text") or "").strip())
        bbox = b.get("bbox")
        if not m or not bbox or len(bbox) != 4:
            continue
        best, best_d = None, float("inf")
        for i, e in enumerate(edges):
            d = bbox_to_edge_distance_weighted(bbox, e)
            if d is not None and d < best_d:
                best_d, best = d, i
        if best is None or best_d >= LABEL_MAX_DIST:
            continue
        li = lines.line_for(best)
        if li is not None:
            hits[li].add(int(m.group(1)))
    return hits


def measure(root: Path, rules: LineRules, verbose: bool):
    rnd = random.Random(SHUFFLE_SEED)
    result = {
        "sheets": 0, "edges": 0, "lines": 0, "need": 0, "skip": 0,
        "labelled_lines": 0, "mixed_lines": 0,
    }
    per_sheet = {}
    unstable = []
    mixed_where = []

    for uid_dir in corpus_dirs(root):
        edges, nodes, sha = read_graph(uid_dir)
        if not edges:
            continue
        lines = build_lines(nodes, edges, rules)

        order = list(range(len(edges)))
        rnd.shuffle(order)
        shuffled = [edges[i] for i in order]
        if partition_ids(shuffled, build_lines(nodes, shuffled, rules)) != \
                partition_ids(edges, lines):
            unstable.append(uid_dir.name[:8])

        hits = labels_on_lines(uid_dir, edges, lines)
        mixed = [li for li, vals in hits.items() if len(vals) > 1]
        if mixed:
            mixed_where.append((uid_dir.name[:8], len(mixed)))

        result["sheets"] += 1
        result["edges"] += len(edges)
        result["lines"] += len(lines)
        result["need"] += lines.needing_diameter
        result["skip"] += len(lines) - lines.needing_diameter
        result["labelled_lines"] += len(hits)
        result["mixed_lines"] += len(mixed)
        per_sheet[uid_dir.name] = {
            "sha": sha, "edges": len(edges), "lines": len(lines),
            "need": lines.needing_diameter, "labelled": len(hits),
        }

        if verbose:
            print("  %-10s рёбер %5d  линий %5d  требуют Ду %5d  подписей на линиях %3d"
                  % (uid_dir.name[:8], len(edges), len(lines),
                     lines.needing_diameter, len(hits)))

    result["unstable_sheets"] = sorted(unstable)
    result["mixed_where"] = mixed_where
    result["per_sheet"] = per_sheet
    return result


def report(r):
    print()
    print("листов %d, рёбер %d, линий %d" % (r["sheets"], r["edges"], r["lines"]))
    print("  требуют Ду: %d   «Ду не требуется»: %d   экономия: %.2fx"
          % (r["need"], r["skip"], r["edges"] / max(1, r["need"])))
    print("  подписи легли в линий: %d, из них смешанных: %d"
          % (r["labelled_lines"], r["mixed_lines"]))
    print("  листов с неустойчивым разбиением: %d" % len(r["unstable_sheets"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", default=os.environ.get("PID_CORPUS", "storage/diagrams"))
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    root = Path(args.corpus)
    if not root.is_dir() or not corpus_dirs(root):
        print("СУДИТЬ НЕЧЕМ: корпус не найден или пуст: %s" % root)
        print("Укажите --corpus или PID_CORPUS (каталог с <uid>/graph/graph_validated.json).")
        return 2

    rules = LineRules.from_project_yaml(PROJECT_YAML)
    r = measure(root, rules, args.verbose)
    report(r)

    if args.write_baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2, sort_keys=True)
        print("\nбаза записана: %s" % BASELINE)
        return 0

    if not args.check:
        return 0

    failures = []
    if r["unstable_sheets"]:
        failures.append(
            "разбиение неустойчиво к перестановке `links` на листах: %s. "
            "Номер линии уезжает на диск (`diameter_line`) — после пересохранения "
            "графа Ду окажется на других рёбрах."
            % ", ".join(r["unstable_sheets"]))
    if r["mixed_lines"]:
        failures.append(
            "смешанные линии (две подписи с разным Ду на одной линии): %s. "
            "Правило линии обещает постоянство Ду внутри линии."
            % r["mixed_where"])

    if BASELINE.exists():
        with open(BASELINE, encoding="utf-8") as f:
            base = json.load(f)
        old, new = base.get("per_sheet", {}), r["per_sheet"]
        judged = changed = 0
        for uid, was in sorted(old.items()):
            now = new.get(uid)
            if now is None:
                print("  лист пропал из корпуса, не судим: %s" % uid[:8])
                continue
            if now["sha"] != was["sha"]:
                changed += 1
                print("  граф переписан, не судим: %s (линий было %d, стало %d)"
                      % (uid[:8], was["lines"], now["lines"]))
                continue
            judged += 1
            diff = {k: (was[k], now[k])
                    for k in ("edges", "lines", "need", "labelled")
                    if was[k] != now[k]}
            if diff:
                failures.append("%s: %s" % (uid[:8], diff))
        added = sorted(set(new) - set(old))
        if added:
            print("  новые листы, в базе их нет: %s" % ", ".join(u[:8] for u in added))
        print("\nсудимых листов: %d, переписано с момента базы: %d" % (judged, changed))
        if judged == 0:
            print("СУДИТЬ НЕЧЕМ: ни один лист из базы не сохранился неизменным")
            return 2
    else:
        print("\nбазы нет — сравнивать не с чем, снимите её --write-baseline")

    if failures:
        print("\nОПРОВЕРГНУТО:")
        for f_ in failures:
            print("  * %s" % f_)
        return 1
    print("\nОК: правило воспроизвелось, разбиение устойчиво, смешанных линий нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())

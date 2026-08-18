# -*- coding: utf-8 -*-
"""layout_determinism.py — стенд ПР1: повторяем ли выход раскладки.

Зачем. `tools/cmp_bitexact.py` — главный критерий «рефакторинг ничего не
изменил»: он сравнивает выход `layout()` с эталоном БИТ-В-БИТ. На
недетерминированном графе такой критерий краснеет ложно, и половина гейтов
дороги превращается в фикцию. Стенд отвечает на вопрос «какая часть корпуса
воспроизводима» числом, а не мнением.

Как меряет. Каждый прогон — ОТДЕЛЬНЫЙ процесс со своим `PYTHONHASHSEED`:
так ловится и обход множеств строк (хеш-сид), и всё, что зависит от
состояния кучи. Сравнивается sha256 от `json.dumps(graph)` — та же проекция,
что у `cmp_bitexact`.

Контракт (как у `suite_baseline.py` и `edit_bench.py`):
    python -X utf8 tools/layout_determinism.py                # отчёт
    python -X utf8 tools/layout_determinism.py --check        # 0 · 1 · 2
    python -X utf8 tools/layout_determinism.py --write-baseline
Провал `--check` (exit 1) — граф, который был воспроизводим, перестал им быть
(или новый граф корпуса неповторим). Обратное движение — печатается, exit 0.
⚠ **exit 2 — «судить нечем»** (`PROTOCOL §5`): часть графов эталона не
измерена. Так выглядит чистый клон и CI — в git лежат 3 графа из 17, а 8
известных неповторимых среди невидимых. Вердикт по корпусу даёт только
локальный прогон; в CI шаг обязан различать 1 и 2 (`.github/workflows/tests.yml`).

Ключ `--no-routing` выключает этап роутинга (`LayoutParams.routing`): им
недетерминизм и локализуется — расстановка с раздвиганием отдельно от обхода
чужих форм через libavoid.

Корпус берётся загрузчиком `tools/corpus.py`: в git лежат три графа, на
машине разработки к ним добавляется локальный `storage/`. `--git-only` —
только то, что видит CI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import corpus                                      # noqa: E402

BASELINE = REPO / "tools" / "bench" / "determinism_baseline.json"
DEFAULT_RUNS = 2


# ─────────────────────────── один прогон (дочерний) ───────────────────────────

def child_main(uids: list[str], routing: bool) -> int:
    """Режим дочернего процесса: sha и дефекты по каждому графу в stdout."""
    from modules.graph.core.canvas_input import to_canvas
    from modules.graph.core.layout import LayoutParams, layout

    out = {}
    for uid8 in uids:
        graph, _t = to_canvas(corpus.load_graph(uid8))
        graph, st = layout(graph, LayoutParams(routing=routing))
        blob = json.dumps(graph, ensure_ascii=False).encode("utf-8")
        out[uid8] = {"sha": hashlib.sha256(blob).hexdigest(),
                     "defects": [st.get("defects_before"),
                                 st.get("defects_after")]}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


def run_pass(uids: list[str], routing: bool, seed: int) -> dict[str, dict]:
    """Прогон в отдельном процессе. -> {uid8: {sha, defects}}.
    RuntimeError, если прогон убит, оборван или молча потерял графы."""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = str(seed)
    # родительские -x/-k и прочее детям не наследуем (урок пункта 0.3)
    env.pop("PYTEST_ADDOPTS", None)
    cmd = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
           "--child", ",".join(uids), "--routing", "1" if routing else "0"]
    proc = subprocess.run(cmd, cwd=str(REPO), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"прогон (seed={seed}) вернул {proc.returncode}: "
                           f"{(proc.stderr or '').strip()[-400:]}")
    try:
        got = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"прогон (seed={seed}) не дал разбираемого вывода: "
                           f"{exc}; хвост stdout: {proc.stdout[-200:]!r}") from exc
    missing = [u for u in uids if u not in got]
    if missing:
        raise RuntimeError(f"прогон (seed={seed}) молча потерял графы: {missing}")
    return got


def measure(uids: list[str], runs: int, routing: bool) -> dict[str, list[dict]]:
    """-> {uid8: [запись прогона 1, запись прогона 2, ...]}."""
    passes = [run_pass(uids, routing, seed) for seed in range(1, runs + 1)]
    return {u: [p[u] for p in passes] for u in uids}


def shas(result: dict[str, list[dict]]) -> dict[str, list[str]]:
    return {u: [r["sha"] for r in runs] for u, runs in result.items()}


def defect_lines(result: dict[str, list[dict]]) -> list[str]:
    """Гуляет ли вместе с геометрией КАЧЕСТВО. Не гейт — замер (0.10)."""
    out = []
    for uid8, runs in sorted(result.items()):
        seen = {tuple(r["defects"]) for r in runs}
        if len(seen) == 1:
            before, after = seen.pop()
            out.append(f"  {uid8}  дефекты одни и те же: {before} -> {after}")
        else:
            pairs = ", ".join(f"{b} -> {a}" for b, a in sorted(seen))
            out.append(f"  {uid8}  ⚠ ДЕФЕКТЫ ГУЛЯЮТ ({len(seen)} варианта): {pairs}")
    return out


# ─────────────────────────────── вердикт ───────────────────────────────

def stability(shas: list[str]) -> bool:
    return len(set(shas)) == 1


def report_lines(result: dict[str, list[str]]) -> list[str]:
    """Что именно померили — строка на граф."""
    out = []
    for uid8, shas in sorted(result.items()):
        mark = "воспроизводим" if stability(shas) else \
            f"НЕПОВТОРИМ ({len(set(shas))} исхода из {len(shas)})"
        out.append(f"  {uid8}  {mark}  {shas[0][:16]}")
    return out


def verdict(result: dict[str, list[str]], base: dict) -> tuple[int, list[str]]:
    """-> (код возврата, строки отчёта). Отделено от печати, чтобы проверялось
    тестом, а не глазами.

    Три исхода (`PROTOCOL §5`), а не два:
    0 — регресса воспроизводимости нет, и весь эталон при этом измерен;
    1 — опровергнуто: воспроизводимый граф сломался или новый неповторим;
    2 — СУДИТЬ НЕЧЕМ: эталона нет, или часть его графов не измерена. Так
        выглядит усечённый корпус — в git лежат 3 графа из 17, остальные
        только в локальном `storage/`, и среди неизмеренных 8 известных
        неповторимых. Раньше такой прогон печатал «регресса нет» и exit 0.
    Доказанный регресс сильнее неполноты: если сломался измеренный граф, это 1.
    """
    known = base.get("stable", {})
    if not known:
        return 2, report_lines(result) + [
            "[СУДИТЬ НЕЧЕМ] эталона нет или он пуст — сначала --write-baseline"]

    broke, fixed, fresh = [], [], []
    for uid8, shas in sorted(result.items()):
        now = stability(shas)
        was = known.get(uid8)
        if was is None:
            if not now:
                fresh.append(uid8)
        elif was and not now:
            broke.append(uid8)
        elif not was and now:
            fixed.append(uid8)

    lines = report_lines(result)
    if broke:
        lines.append(f"[ПРОВАЛ] перестали воспроизводиться: {', '.join(broke)}")
    if fresh:
        lines.append(f"[ПРОВАЛ] новый граф корпуса неповторим: {', '.join(fresh)}")
    if fixed:
        lines.append(f"[стало лучше] воспроизводятся впервые: {', '.join(fixed)}")

    unmeasured = sorted(set(known) - set(result))
    if unmeasured:
        lines.append(f"[СУДИТЬ НЕЧЕМ] не измерено {len(unmeasured)} графов "
                     f"эталона из {len(known)} — корпус усечён: "
                     f"{', '.join(unmeasured)}")
    if broke or fresh:
        return 1, lines
    if unmeasured:
        return 2, lines
    lines.append("[OK] регресса воспроизводимости нет")
    return 0, lines


def read_baseline() -> dict:
    if not BASELINE.exists():
        return {}
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--routing", default="1", help=argparse.SUPPRESS)
    ap.add_argument("--uid", action="append", default=None,
                    help="uid8; можно повторять. По умолчанию — весь корпус")
    ap.add_argument("--git-only", action="store_true",
                    help="только фикстуры из git (то, что видит CI)")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                    help=f"сколько прогонов сравнивать (по умолчанию {DEFAULT_RUNS})")
    ap.add_argument("--no-routing", action="store_true",
                    help="выключить этап роутинга (локализация недетерминизма)")
    ap.add_argument("--check", action="store_true",
                    help="сверить с эталоном; exit 1 при регрессе")
    ap.add_argument("--write-baseline", action="store_true",
                    help="переснять эталон (отдельным коммитом, Д6)")
    args = ap.parse_args()

    if args.child is not None:
        return child_main(args.child.split(","), args.routing == "1")

    routing = not args.no_routing
    available = corpus.corpus_paths(include_storage=not args.git_only)
    uids = args.uid or sorted(available)
    unknown = [u for u in uids if u not in available]
    if unknown:
        print(f"нет в корпусе: {', '.join(unknown)}")
        return 1
    if args.runs < 2:
        print("сравнивать нечего: --runs меньше двух")
        return 1

    print(f"корпус: {len(uids)} графов, прогонов: {args.runs}, "
          f"роутинг: {'вкл' if routing else 'выкл'}", flush=True)
    result = measure(uids, args.runs, routing)

    if args.write_baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(
            {"runs": args.runs, "routing": routing,
             "stable": {u: stability(s) for u, s in sorted(shas(result).items())}},
            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"эталон переснят: {BASELINE}")
        return 0

    if args.check:
        code, lines = verdict(shas(result), read_baseline())
    else:
        code, lines = 0, report_lines(shas(result))
    print("\n".join(lines))
    print("качество (гуляет ли вместе с геометрией):")
    print("\n".join(defect_lines(result)))
    return code


if __name__ == "__main__":
    sys.exit(main())

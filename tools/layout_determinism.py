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
измерена, замер слабее эталонного (`--runs` меньше, другой ключ роутинга —
пункт 1-30) или дочерний прогон убит. Так выглядит чистый клон и CI — в git
лежат 3 графа из 19, а 9 известных неповторимых среди невидимых. Вердикт по
корпусу даёт только локальный прогон; в CI шаг обязан различать 1 и 2
(`.github/workflows/tests.yml`) — и различать ПРИЧИНУ двойки: усечённый
корпус там штатен, убитый прогон нет. Причина — последней строкой вывода,
`MARK_UNJUDGED_CORPUS` или `MARK_UNJUDGED_OTHER`.

Те же три исхода у `--write-baseline` (пункт 1-28): 0 — эталон переснят,
1 — ОТКАЗ (пересъём узаконил бы поломку), 2 — СУДИТЬ НЕЧЕМ (корпус усечён,
пересъём вычеркнул бы неизмеренное). См. `write_blocked()`.
⚠ Пересъём обязан быть не слабее эталона (пункт 1-29): `--runs` меньше
эталонных или другой ключ роутинга — тоже «судить нечем». Иначе одна удачная
выборка молча вычёркивает графы из списка неповторимых (§49.27). Одного пола
мало: `false` в эталоне ЛИПКИЙ (`merge_stable()`), снять его может только явная
починка `--allow-fixed <uid>` — замер 1-29 показал, что при `--runs 6`
неповторимый граф всё равно способен выйти с одним хешем (§50.19).

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

# Последняя строка вывода при exit 2 — ЧЕМ именно судить нечем. Читает её шаг
# CI (`.github/workflows/tests.yml`), которому усечённый корпус штатен (в git
# 3 графа из 19), а сломанная обстановка — нет. До пункта 1-30 убитый дочерний
# прогон умирал трейсбеком с кодом 1 и потому в CI краснел; сделать его честным
# «судить нечем», не разделив причины, значило бы завести шагу зелёную дыру.
# Метки ASCII: их грепает bash на windows-раннере.
MARK_UNJUDGED_CORPUS = "unjudged=corpus"
MARK_UNJUDGED_OTHER = "unjudged=environment"


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


def weaker_than_baseline(base: dict, runs: int, routing: bool) -> list[str]:
    """Причины «этот замер слабее того, что записано в эталоне» (пункт 1-29).

    Гейт по природе выборочный: «воспроизводим» читается как «за N прогонов
    не поймали расхождения» (`TESTING §8.2`). Замер меньшим числом прогонов
    или другим ключом роутинга — замер другой (более слабой) величины.

    Арифметика общая у записи и у чтения (пункт 1-30): у пересъёма она с 1-29,
    а `--check` при малом `--runs` до 1-30 печатал «[стало лучше] воспроизводятся
    впервые» про заведомо неповторимые графы и отдавал exit 0 — то есть
    «доказано, что регресса нет» по выборке, которая этого доказать не может.
    """
    reasons = []
    base_runs = base.get("runs")
    if base_runs and runs < base_runs:
        reasons.append(f"замер слабее эталона: прогонов {runs} против "
                       f"{base_runs} — при малой выборке неповторимый граф "
                       f"выходит воспроизводимым (§49.27: 4 из 8 известных "
                       f"при --runs 2). Мерить при --runs не меньше {base_runs}")
    base_routing = base.get("routing")
    if base_routing is not None and routing is not base_routing:
        reasons.append(f"замер снят другим ключом: роутинг "
                       f"{'вкл' if routing else 'выкл'} против "
                       f"{'вкл' if base_routing else 'выкл'} у эталона — "
                       "это замер другой величины (без роутинга раскладка "
                       "воспроизводима вся, замер 0.10)")
    return reasons


def verdict(result: dict[str, list[str]], base: dict,
            runs: int, routing: bool) -> tuple[int, list[str]]:
    """-> (код возврата, строки отчёта). Отделено от печати, чтобы проверялось
    тестом, а не глазами.

    Три исхода (`PROTOCOL §5`), а не два:
    0 — регресса воспроизводимости нет, и весь эталон при этом измерен
        замером не слабее эталонного;
    1 — опровергнуто: воспроизводимый граф сломался или новый неповторим;
    2 — СУДИТЬ НЕЧЕМ: эталона нет, часть его графов не измерена, или замер
        слабее эталона. Усечённый корпус — в git лежат 3 графа из 19,
        остальные только в локальном `storage/`, и среди неизмеренных
        8 известных неповторимых; раньше такой прогон печатал «регресса нет»
        и exit 0 (пункт 1-25). Слабый замер — пункт 1-30: `--check --runs 2`
        против эталона в 6 прогонов печатал «[стало лучше] воспроизводятся
        впервые» про графы, у которых расхождение просто не успело выпасть,
        и отдавал 0.
    Доказанный регресс сильнее неполноты: если сломался измеренный граф, это 1.
    Ложного КРАСНОГО малый `--runs` дать не может — расхождение это наблюдение,
    а не порог, — поэтому поломка судится и по слабой выборке.
    """
    known = base.get("stable", {})
    if not known:
        return 2, report_lines(result) + [
            "[СУДИТЬ НЕЧЕМ] эталона нет или он пуст — сначала --write-baseline",
            MARK_UNJUDGED_OTHER]

    weak = weaker_than_baseline(base, runs, routing)
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
    if fixed and weak:
        # То же правило, что у `merge_stable()` на записи: неповторимость —
        # наблюдение положительное, и слабая выборка её не отменяет.
        lines.append(f"[ВЫБОРКА, НЕ ПОЧИНКА] дали один хеш, но замер слабее "
                     f"эталона — «воспроизводятся впервые» из него не следует: "
                     f"{', '.join(fixed)}")
    elif fixed:
        lines.append(f"[стало лучше] воспроизводятся впервые: {', '.join(fixed)}")
    for msg in weak:
        lines.append(f"[СУДИТЬ НЕЧЕМ] {msg}")

    unmeasured = sorted(set(known) - set(result))
    if unmeasured:
        lines.append(f"[СУДИТЬ НЕЧЕМ] не измерено {len(unmeasured)} графов "
                     f"эталона из {len(known)} — корпус усечён: "
                     f"{', '.join(unmeasured)}")
    if broke or fresh:
        return 1, lines
    if weak:                          # обстановка замера, а не данные корпуса
        return 2, lines + [MARK_UNJUDGED_OTHER]
    if unmeasured:
        return 2, lines + [MARK_UNJUDGED_CORPUS]
    lines.append("[OK] регресса воспроизводимости нет")
    return 0, lines


def write_blocked(result: dict[str, list[str]], base: dict,
                  runs: int, routing: bool) -> tuple[list[str], list[str]]:
    """-> (причины «судить нечем», причины отказа). Обе пустые = пересъём законен.

    Пересъём — единственный путь, которым эталон вообще меняется, и по Д6 он
    идёт ОТДЕЛЬНЫМ коммитом; красный флаг №4 протокола («эталон изменён тем же
    коммитом, что и код») его поэтому не видит. 1-25 научил отвечать «судить
    нечем» только `--check`, а запись осталась слепой (пункт 1-28): замер
    2026-08-19 — `--git-only --runs 2 --write-baseline` печатал «эталон
    переснят», exit 0 и оставлял в файле 3 графа из 17, вычёркивая заодно все
    восемь известных неповторимых.

    Те же три исхода, что у `verdict()`:
    2 — СУДИТЬ НЕЧЕМ: замер не годен в замену эталону — часть эталона не
        измерена (корпус усечён), либо снят он слабее эталонного: прогонов
        меньше, чем у эталона, или другим ключом роутинга (пункт 1-29);
    1 — ОТКАЗ: воспроизводимый граф сломался, пересъём записал бы поломку нормой;
    0 — законно (в том числе первый снимок: сверять не с чем).

    ⚠ Почему число прогонов — это «судить нечем», а не мелочь: гейт по своей
    природе выборочный, «воспроизводим» читается как «за N прогонов не поймали»
    (`TESTING §8.2`). Замер 2026-08-19 (§49.27): пересъём при `--runs 2` назвал
    воспроизводимыми ЧЕТЫРЕ из восьми известных неповторимых графов, а эталон
    снят при `runs 6`. Отказ по сломавшемуся графу этого не ловит по замыслу —
    `false -> true` законное «стало лучше», — поэтому стережётся сам замер.
    """
    known = base.get("stable", {})
    if not known:
        return [], []

    # Слабость замера считается тем же `weaker_than_baseline`, что и на чтении
    # (пункт 1-30): два судьи одной выборки не должны разъехаться.
    unjudged = [msg + " — иначе эталон ослаб бы молча"
                for msg in weaker_than_baseline(base, runs, routing)]
    refused = []
    unmeasured = sorted(set(known) - set(result))
    if unmeasured:
        unjudged.append(f"не измерено {len(unmeasured)} графов эталона из "
                        f"{len(known)} — корпус усечён, пересъём вычеркнул бы "
                        f"их из эталона: {', '.join(unmeasured)}")
    broke = sorted(u for u, s in result.items() if known.get(u) and not stability(s))
    if broke:
        refused.append(f"перестали воспроизводиться: {', '.join(broke)} — "
                       "пересъём записал бы поломку нормой. Такой граф чинят "
                       "или объясняют, а не переснимают")
    return unjudged, refused


def merge_stable(result: dict[str, list[str]], base: dict,
                 allow_fixed: list[str]) -> tuple[dict[str, bool], list[str]]:
    """-> (что записать в эталон, строки-объяснения к печати).

    Неповторимость — наблюдение ПОЛОЖИТЕЛЬНОЕ: два разных исхода её доказывают,
    а одна тихая серия одинаковых не опровергает (`TESTING §8.2`). Поэтому
    `false` в эталоне липкий: замер, показавший один хеш там, где эталон помнит
    расхождение, его не стирает. Снять `false` может только явно объявленная
    починка — `--allow-fixed <uid>` плюс объяснение в коммите, ЧТО изменилось
    в коде; без такого ключа храповик стал бы вечным и починку недетерминизма
    libavoid (ВН3) записать было бы нечем.

    ⛔ Замерено СОБСТВЕННЫМ пересъёмом этого пункта (§50.19), а не выведено:
    полный корпус, `--runs 6` — тот самый пол, который пункт и ввёл, — и
    `0aea61c0` вышел с одним хешем. Пересъём записал бы его `true`, хотя §32.14
    наблюдал у него ТРИ разных исхода из шести. Пол по числу прогонов такое не
    держит: он необходим, но недостаточен.
    """
    known = base.get("stable", {})
    stable, lines = {}, []
    for uid8, sha_list in sorted(result.items()):
        now = stability(sha_list)
        if now and known.get(uid8) is False:
            if uid8 in allow_fixed:
                lines.append(f"[ПОЧИНКА по --allow-fixed] {uid8}: неповторим -> "
                             "воспроизводим. Объяснить в коммите, ЧТО изменилось "
                             "в коде — иначе это просто удачная выборка")
            else:
                now = False
                lines.append(f"[ВЫБОРКА, НЕ ПОЧИНКА] {uid8}: эталон помнит "
                             "неповторимость, а этот замер дал один хеш — "
                             "оставлено «неповторим» (TESTING §8.2). Если это "
                             f"починка кода: --allow-fixed {uid8}")
        stable[uid8] = now
    return stable, lines


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
    ap.add_argument("--allow-fixed", action="append", default=None, metavar="UID8",
                    help="при пересъёме записать граф из списка неповторимых "
                         "как воспроизводимый — явная починка; можно повторять")
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
    try:
        result = measure(uids, args.runs, routing)
    except RuntimeError as exc:
        # Убитый, оборванный или молча потерявший графы дочерний прогон — это
        # сломанная обстановка, а не регресс кода (пункт 1-30). Раньше он
        # умирал трейсбеком и кодом 1, неотличимым от доказанной поломки.
        print(f"[СУДИТЬ НЕЧЕМ] замер не состоялся: {exc}")
        print(MARK_UNJUDGED_OTHER)
        return 2

    if args.write_baseline:
        unjudged, refused = write_blocked(shas(result), read_baseline(),
                                          args.runs, routing)
        for msg in unjudged:
            print(f"[СУДИТЬ НЕЧЕМ] {msg}")
        for msg in refused:
            print(f"[ОТКАЗ] {msg}")
        if refused:                      # доказанная поломка сильнее неполноты
            return 1
        if unjudged:
            print(MARK_UNJUDGED_OTHER if weaker_than_baseline(
                read_baseline(), args.runs, routing) else MARK_UNJUDGED_CORPUS)
            return 2
        stable, notes = merge_stable(shas(result), read_baseline(),
                                     args.allow_fixed or [])
        for msg in notes:
            print(msg)
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(
            {"runs": args.runs, "routing": routing,
             "stable": stable},
            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"эталон переснят: {BASELINE}")
        return 0

    if args.check:
        code, lines = verdict(shas(result), read_baseline(), args.runs, routing)
    else:
        code, lines = 0, report_lines(shas(result))
    print("\n".join(lines))
    print("качество (гуляет ли вместе с геометрией):")
    print("\n".join(defect_lines(result)))
    return code


if __name__ == "__main__":
    sys.exit(main())

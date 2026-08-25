# -*- coding: utf-8 -*-
"""smooth_bench.py — судья кнопки «Авто-выравнивание» (Э4), блок 0 плана
`pains-auto-align.md`.

Зачем отдельный стенд. `edit_bench.py` меряет ХОЛСТ, но `smooth` не зовёт
вовсе, а докстринг движка (`edit_smooth.py:23`) ссылается на этот файл,
которого до сегодня не существовало ни в одном коммите. Все числа, которыми
живут пороги (`BUDGET`, `STEP_MAX`, «косых 51->12», «ступенек 96->40»), сняты
БЕЗ Qt — то есть с выключенной ступенью «колено» и без пересадки концов.

Что здесь меряется. Кнопка целиком: живой `AdvancedGraphEditor` в offscreen-Qt,
`_canvas_mode = True` ДО `load_data` (как ставит вкладка,
`ui/tabs/base_graph_tab.py:978` и `:1021`), и вызывается РОДНОЙ метод
`AdvancedGraphEditor.smooth_canvas` — он сам отдаёт движку свою лестницу
маршрутов (`route_fn` — враппер над `_route_orthogonal`) и мини-жест пересадки
(`reseat_fn = _reseat_after_resize`), сам ставит боевой контекст
(`_drag_route_ctx = None`, `_drag_routable_edges = set()`, `_amnesty_cache`)
и сам выбирает сигнатуру. Переписывать враппер в стенде нельзя: тогда стенд
мерил бы СВОЮ кнопку, а не боевую.

Две популяции, они НЕ смешиваются (решение Р-А4):
  * `edited` — `tools/bench/smooth_corpus/edited/` (20 сохранений «Ручной
    правки», после жестов оператора). ГЛАВНАЯ: суть кнопки — прибраться
    после оператора, и патология скорости живёт только здесь;
  * `raw` — `tools/bench/smooth_corpus/raw/*_canvas.json` (срез storage,
    холст сразу после раскладки). Вторичная: кнопку жмут и на них.
⚠ `*_validated.json` из того же среза сюда НЕ входят: у них `image_size`
раскадровки (напр. 1247x1978), а движок судит лист 1920x1080 (`CANVAS_W/H`),
то есть это не координаты холста и не то, на чём живёт кнопка.

⛔ ПРОТОКОЛ НЕДЕТЕРМИНИЗМА. `_reseat_after_resize` строит libavoid-сессию на
каждый вызов, а libavoid недетерминирован (`MEASUREMENTS §32.14-15`, `PROTOCOL`:
10 графов корпуса из 19 дают разный байт). Поэтому стенд гоняет N прогонов
(`--runs`, стартово 5) и судит МЕДИАНУ, а эталон помнит ещё и ДИАПАЗОН
(`lo`/`hi`) — он же «допуск шума» из правила влития Р-А1: колонка объявляется
выросшей, только если новая медиана вышла ЗА верх диапазона эталона.

⛔ АССЕРТ БИНДИНГА. У `edit_avoid.load_binding()` молчаливый фолбэк на
самописную лестницу: без SWIG-модуля стенд замерил бы НЕ ТУ кнопку и об этом
не сказал бы ни слова. Нет биндинга — «судить нечем» (exit 2) ПЕРВОЙ веткой,
до любых измерений (`PROTOCOL`: вердикт не может собираться из ненаблюдений).

Три исхода (`PROTOCOL §5`), общие с `edit_bench`/`suite_baseline`/`lint_gate`:
    0 — не хуже эталона, и измерен весь его корпус;
    1 — опровергнуто: дефектная колонка выросла сверх диапазона шума;
    2 — СУДИТЬ НЕЧЕМ: нет биндинга · нет Qt · эталон пуст · корпус усечён ·
        отпечаток входа разошёлся (форма v2, урок GATE-6) · прогонов меньше,
        чем у эталона (урок `layout_determinism --runs`).
Доказанный рост сильнее неполноты.

ВРЕМЯ в `--check` НЕ судится (флап от машины) — только печатается: p50 и
худший по популяции. Метрики честности (обесточенность reseat-гейта, доля
принятых ходов) тоже справочные: их целевые пункты — блоки 1 и 2, и рост
принятых ходов там ожидаем по построению.

Запуск (из корня репо, системный python 3.11.9):
    python -X utf8 tools/smooth_bench.py --all
    python -X utf8 tools/smooth_bench.py --population edited --runs 1
    python -X utf8 tools/smooth_bench.py --all --write-baseline
    python -X utf8 tools/smooth_bench.py --all --check

Координаты двойственны (`CODING_GUIDE §6`): `centroid`/`source_point` = [y, x],
`bbox` = [x1, y1, x2, y2]. Проверки листа ниже написаны по этому правилу.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import corpus  # noqa: E402

BASELINE = REPO / "tools" / "bench" / "smooth_baseline.json"
BASELINE_VERSION = 2
DEFAULT_RUNS = 5

# Колонки-ДЕФЕКТЫ: в --check не могут расти сверх диапазона эталона.
DEFECT_COLS = [
    ("diag", "косых"),
    ("dev", "отклонение"),
    ("steps", "ступенек"),
    ("bends", "изломов"),
    ("near", "почти-оси"),
    ("along_own", "вдоль своего"),
    ("along_foreign", "вдоль чужого"),
    ("through", "сквозь узел"),
    ("corner", "концы в углах"),
    ("adrift", "оторванных"),
    ("conn_off", "коннектор мимо"),
    ("poly_off", "полигон мимо"),
    ("w", "габарит w"),
    ("h", "габарит h"),
    ("offsheet", "узлов за листом"),
    ("rollback_bad", "откатов с мусором"),
]
# Справочные: печатаются, но не судятся.
REF_COLS = [
    ("ms", "мс"),
    ("moves", "принято ходов"),
    ("refused", "отклонено"),
    ("rounds", "раундов"),
    ("reseat_calls", "вызовов reseat"),
    ("reseat_blind", "из них обесточено"),
]

SCORE_KEYS = ("diag", "dev", "steps", "near", "along_own", "along_foreign",
              "through", "corner", "adrift", "conn_off", "poly_off", "w", "h")
REMEDIES = ("колено", "излом", "скольжение", "коннектор", "узел")
REFUSAL_KINDS = ("нет кандидата", "сверх бюджета", "лестница исчерпана")


# ── корпус ─────────────────────────────────────────────────────────────
def population_paths(name: str) -> list[Path]:
    """Файлы популяции. Пусто — популяции нет в дереве."""
    if name == "raw":
        return list(corpus.canvas_slice_paths().values())
    if name == "edited":
        return list(corpus.edited_canvas_paths().values())
    raise KeyError(name)


# ── метрики холста ─────────────────────────────────────────────────────
def count_bends(graph) -> tuple[int, int]:
    """(изломов всего, рёбер с геометрией).

    Излом — промежуточная точка, в которой маршрут МЕНЯЕТ направление.
    Нужен блоку 6: слияние двух ступенек в одно колено счётчику `steps`
    невидимо (20+20 -> 40 — по-прежнему «не ступенька»), а изломов при этом
    становится меньше. Предикат «лишняя точка» тот же, что у
    `edit_smooth.drop_collinear`, — сторож и судья не должны расходиться.
    """
    from modules.graph.core import edit_checks as ec
    from modules.graph.core import edit_smooth as es
    from modules.graph.core.graph_access import edges as g_edges

    tol = ec.DIAG_TOL
    bends = edges = 0
    for e in g_edges(graph):
        pts = es.edge_pts(e)
        if not pts:
            continue
        edges += 1
        for a, b, c in zip(pts, pts[1:], pts[2:]):
            same_x = abs(a[0] - b[0]) <= tol and abs(b[0] - c[0]) <= tol
            same_y = abs(a[1] - b[1]) <= tol and abs(b[1] - c[1]) <= tol
            if not (same_x or same_y):
                bends += 1
    return bends, edges


def offsheet(graph) -> tuple[int, int]:
    """(узлов за листом 1920x1080, концов рёбер за листом).

    ⚠ Ось: `bbox` = [x1, y1, x2, y2], `centroid` и `*_point` = [y, x]
    (`CODING_GUIDE §6`). Перепутанная ось молчала бы в полосе 0..1080
    и стреляла только при x = 1080..1920 — ровно та ловушка, о которой
    предупреждает пункт 2.1 плана.
    """
    from modules.graph.core import edit_smooth as es
    from modules.graph.core.graph_access import edges as g_edges

    w, h = es.CANVAS_W, es.CANVAS_H
    eps = 0.5
    bad_nodes = 0
    for n in graph.get("nodes") or []:
        out = False
        bb = n.get("bbox")
        if bb and len(bb) == 4:
            out = (bb[0] < -eps or bb[2] > w + eps
                   or bb[1] < -eps or bb[3] > h + eps)
        if not out:
            c = n.get("centroid")
            if c and len(c) >= 2:          # коннекторы: bbox null, есть точка
                y, x = float(c[0]), float(c[1])
                out = x < -eps or x > w + eps or y < -eps or y > h + eps
        bad_nodes += bool(out)
    bad_pts = 0
    for e in g_edges(graph):
        for key in ("source_point", "target_point"):
            p = e.get(key)
            if not p:
                continue
            y, x = float(p[0]), float(p[1])
            if x < -eps or x > w + eps or y < -eps or y > h + eps:
                bad_pts += 1
    return bad_nodes, bad_pts


def canvas_metrics(graph) -> dict:
    """Полный набор метрик холста: score движка + изломы + лист."""
    from modules.graph.core import edit_smooth as es

    out = {k: es.score(graph)[k] for k in SCORE_KEYS}
    out["bends"], out["edges"] = count_bends(graph)
    out["offsheet"], out["offsheet_pts"] = offsheet(graph)
    return out


# ── инструментовка (движок не трогаем) ─────────────────────────────────
def _graph_fingerprint(graph) -> str:
    """Ровно то, что обязан вернуть откат: узлы и рёбра целиком.

    Не проекция геометрии: пункт 1.3 плана как раз про то, возвращает ли
    адресный `restore()` СТОРОНЫ (`_src_side`/`_tgt_side`) и все ROUTE_KEYS.
    Порядок ключей значим — корректный `clear()+update()` его сохраняет,
    поэтому `sort_keys` здесь был бы поблажкой.
    """
    from modules.graph.core.graph_access import edges as g_edges
    return json.dumps((graph.get("nodes"), list(g_edges(graph))),
                      ensure_ascii=False, default=str)


class Probe:
    """Счётчики честности вокруг боевого вызова. Кода движка НЕ меняет.

    Три вещи, которых движок про себя не рассказывает:
      * ОБЕСТОЧЕННОСТЬ reseat-гейта (пункт 1.1): гейт в
        `_reseat_after_resize` (:2346) снимает `was_ortho` уже ПОСЛЕ
        `_shift_node` (`edit_smooth.py:427`, вызовы :713-714 и :739-740),
        то есть сравнивает косое с косым. Ловится сравнением статуса рёбер
        узла ДО сдвига и в момент вызова колбэка — теми же глазами, что
        у гейта (`_edge_is_ortho`, tol=1.0);
      * ЦЕЛОСТНОСТЬ ОТКАТА (пункт 1.3): у отклонённого хода граф обязан быть
        бит-в-бит прежним;
      * ПРИЧИНА ОТКАЗА (у движка один счётчик «отклонено»): три ветки
        `_attempt` различимы снаружи по `shift_target` на том же состоянии,
        в котором его читает движок (лекарства 1 и 1б к этому моменту уже
        откатили за собой).
    """

    def __init__(self, editor, rollback_cap: int):
        self.ed = editor
        self.rollback_cap = rollback_cap
        self.reseat_calls = 0
        self.reseat_blind = 0
        self.reseat_edges = 0
        self.reseat_blind_edges = 0
        self.reseat_orphan = 0
        self.rollback_checked = 0
        self.rollback_bad = 0
        self.refusals = {k: 0 for k in REFUSAL_KINDS}
        self.smooth_kwargs: dict = {}
        self._pre_shift: tuple = (None, {})
        self._saved: list = []

    # -- вспомогательное ------------------------------------------------
    def _incident(self, node_id):
        return [e for e in self.ed.edges_data
                if node_id in (e.get("source"), e.get("target"))]

    def install(self):
        from modules.graph.core import edit_smooth as es

        orig_shift = es._shift_node
        orig_try = es._try_remedies
        orig_smooth = es.smooth
        orig_reseat = self.ed._reseat_after_resize

        def shift(graph, node_id, dx, dy):
            self._pre_shift = (node_id, {
                id(e): self.ed._edge_is_ortho(e)
                for e in self._incident(node_id)})
            return orig_shift(graph, node_id, dx, dy)

        def reseat(node_id):
            self.reseat_calls += 1
            was_node, pre = self._pre_shift
            blind_here = 0
            if was_node != node_id:
                self.reseat_orphan += 1     # вызов не после сдвига этого узла
            else:
                for e in self._incident(node_id):
                    self.reseat_edges += 1
                    if pre.get(id(e)) and not self.ed._edge_is_ortho(e):
                        blind_here += 1
            self.reseat_blind_edges += blind_here
            self.reseat_blind += bool(blind_here)
            self._pre_shift = (None, {})
            return orig_reseat(node_id)

        def try_remedies(graph, e, stats, route_fn, reseat_fn, budget,
                         allow_node_shift, big_area):
            step = es.shift_target(e)
            if step is None:
                kind = "нет кандидата"
            elif step[1] > budget:
                kind = "сверх бюджета"
            else:
                kind = "лестница исчерпана"
            check = self.rollback_checked < self.rollback_cap
            fp = _graph_fingerprint(graph) if check else None
            ok = orig_try(graph, e, stats, route_fn, reseat_fn, budget,
                          allow_node_shift, big_area)
            if not ok:
                self.refusals[kind] += 1
                if check:
                    self.rollback_checked += 1
                    if _graph_fingerprint(graph) != fp:
                        self.rollback_bad += 1
            return ok

        def smooth(graph, **kw):
            self.smooth_kwargs = dict(kw)
            self.smooth_kwargs.pop("route_fn", None)
            self.smooth_kwargs.pop("reseat_fn", None)
            self.smooth_kwargs["route_fn?"] = kw.get("route_fn") is not None
            self.smooth_kwargs["reseat_fn?"] = kw.get("reseat_fn") is not None
            return orig_smooth(graph, **kw)

        self._saved = [(es, "_shift_node", orig_shift),
                       (es, "_try_remedies", orig_try),
                       (es, "smooth", orig_smooth)]
        es._shift_node = shift
        es._try_remedies = try_remedies
        es.smooth = smooth
        self.ed._reseat_after_resize = reseat

    def remove(self):
        for obj, name, value in self._saved:
            setattr(obj, name, value)
        self._saved = []
        self.ed.__dict__.pop("_reseat_after_resize", None)


# ── прогон одного холста ───────────────────────────────────────────────
def _raster(tmp: Path, w: int, h: int) -> Path:
    from PySide6.QtGui import QImage, QColor
    p = tmp / f"raster_{w}x{h}.png"
    if not p.exists():
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(QColor("white"))
        img.save(str(p))
    return p


def _drop(editor):
    """Снести редактор ДЕТЕРМИНИРОВАННО: `processEvents` не доставляет
    `DeferredDelete` вовсе (`PROTOCOL`, замер 1-42) — 38 холстов на 5 прогонов
    оставили бы почти две сотни живых сцен."""
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication
    editor.close()
    editor.setParent(None)
    editor.deleteLater()
    QApplication.instance().sendPostedEvents(None, QEvent.Type.DeferredDelete)


def run_once(path: Path, tmp: Path, rollback_cap: int) -> dict:
    """Один боевой прогон кнопки по одному холсту -> метрики до/после."""
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    graph = json.loads(path.read_text(encoding="utf-8"))
    size = (graph.get("graph") or {}).get("image_size") or [1080, 1920]
    ip = _raster(tmp, int(size[1]), int(size[0]))
    gp = tmp / "graph.json"
    gp.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")

    ed = AdvancedGraphEditor()
    # Как в бою: вкладка ставит режим холста ДО load_data
    # (`ui/tabs/base_graph_tab.py:978`, `:1021`; прецедент —
    # tests/test_seat_contract.py:276).
    # ⚠ ЕДИНСТВЕННОЕ отличие от боевого открытия: вкладка перед выдачей файла
    # ещё зовёт `_reseat_canvas_endpoints` (:976, «на каноничном холсте —
    # no-op»). Стенд его не зовёт СОЗНАТЕЛЬНО: он мутирует входной файл, а
    # корпус запинован отпечатками. Величина отличия замерена — MEASUREMENTS §AL0.
    ed._canvas_mode = True
    if not ed.load_data(str(ip), str(gp)):
        _drop(ed)
        raise ValueError("редактор не принял холст (load_data вернул False)")

    live = ed.model.graph_data
    before = canvas_metrics(live)
    probe = Probe(ed, rollback_cap)
    probe.install()
    try:
        t0 = time.perf_counter()
        stats = ed.smooth_canvas()          # ровно то, что жмёт оператор
        ms = (time.perf_counter() - t0) * 1000.0
    finally:
        probe.remove()
    after = canvas_metrics(live)
    _drop(ed)

    row = {"before": before, "after": after, "ms": round(ms, 1),
           "stats": {k: stats.get(k, 0) for k in
                     (*REMEDIES, "отклонено", "осталось", "раунды")},
           "refusals": dict(probe.refusals),
           "reseat_calls": probe.reseat_calls,
           "reseat_blind": probe.reseat_blind,
           "reseat_edges": probe.reseat_edges,
           "reseat_blind_edges": probe.reseat_blind_edges,
           "reseat_orphan": probe.reseat_orphan,
           "rollback_checked": probe.rollback_checked,
           "rollback_bad": probe.rollback_bad,
           "smooth_kwargs": probe.smooth_kwargs}
    row["cols"] = _columns(row)
    return row


def _columns(row: dict) -> dict:
    """Плоские колонки одного прогона — то, что попадает в эталон и таблицу."""
    a, st = row["after"], row["stats"]
    cols = {k: (round(float(a[k]), 1) if k in ("dev", "w", "h") else int(a[k]))
            for k, _ru in DEFECT_COLS if k in a}
    cols["rollback_bad"] = row["rollback_bad"]
    cols["ms"] = row["ms"]
    cols["moves"] = sum(st[k] for k in REMEDIES)
    cols["refused"] = st["отклонено"]
    cols["rounds"] = st["раунды"]
    cols["reseat_calls"] = row["reseat_calls"]
    cols["reseat_blind"] = row["reseat_blind"]
    return cols


# ── свод по N прогонам ─────────────────────────────────────────────────
def summarize(runs: list[dict]) -> dict:
    """{колонка: {med, lo, hi}} по N прогонам одного холста."""
    keys = sorted({k for r in runs for k in r["cols"]})
    out = {}
    for k in keys:
        vals = [r["cols"][k] for r in runs]
        out[k] = {"med": round(statistics.median(vals), 1),
                  "lo": round(min(vals), 1), "hi": round(max(vals), 1)}
    return out


def measure(paths: list[Path], runs: int, tmp: Path,
            rollback_cap: int) -> tuple[dict, dict, list[str]]:
    """-> ({файл: свод}, {файл: [прогоны]}, [почему по файлу судить нечем])."""
    summary, detail, unjudged = {}, {}, []
    for p in sorted(paths, key=lambda q: q.name):
        got = []
        for i in range(runs):
            try:
                got.append(run_once(p, tmp, rollback_cap))
            except (OSError, ValueError, KeyError) as exc:
                # Холст — ДАННЫЕ: битый файл обязан давать «судить нечем»,
                # а не трейсбек с кодом 1 (`PROTOCOL §5`, урок 1-42).
                unjudged.append(f"{p.name}: прогон {i + 1} — {exc}")
                break
        if len(got) != runs:
            continue
        detail[p.name] = got
        summary[p.name] = summarize(got)
        print(f"  {p.name}: {_line(summary[p.name])}")
    return summary, detail, unjudged


def _line(s: dict) -> str:
    parts = []
    for k, _ru in (*DEFECT_COLS, *REF_COLS):
        if k not in s:
            continue
        v = s[k]
        span = "" if v["lo"] == v["hi"] else f"[{v['lo']}..{v['hi']}]"
        parts.append(f"{k}={v['med']}{span}")
    return " ".join(parts)


# ── эталон ─────────────────────────────────────────────────────────────
def read_baseline() -> dict:
    if not BASELINE.exists():
        return {}
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def input_fingerprints(paths: list[Path]) -> dict[str, str]:
    return {p.name: corpus.data_fingerprint(p) for p in paths}


def input_drift(inputs: dict[str, str], known: dict[str, str],
                files: dict) -> dict[str, str]:
    """{файл: чем судить нечем} — холсты, о числах которых эталон не отвечает.

    Форма та же, что у `edit_bench.input_drift` (урок GATE-6): имя файла
    говорит «как называется», а не «что внутри».
    """
    drift = {}
    for name in sorted(inputs):
        if name not in files:
            continue
        was = known.get(name)
        if was is None:
            drift[name] = "эталон не помнит отпечатка входа"
        elif was != inputs[name]:
            drift[name] = (f"вход сменился: {was[:16]} -> {inputs[name][:16]}")
    return drift


def growth(summary: dict, files: dict, drift: dict) -> dict:
    """{файл: [(колонка, верх эталона, медиана сейчас)]} — только рост.

    ⛔ Сравнение с ВЕРХОМ диапазона эталона, а не с его медианой: libavoid
    недетерминирован, и «выросло на единицу» между двумя прогонами одного
    и того же кода — норма, а не регресс. Этот зазор и есть «допуск шума»
    правила влития Р-А1.
    """
    grown: dict[str, list] = {}
    for name, cur in summary.items():
        if name in drift:
            continue
        base = files.get(name)
        if base is None:
            continue
        for key, _ru in DEFECT_COLS:
            if key not in cur or key not in base:
                continue
            top, now = base[key]["hi"], cur[key]["med"]
            if now > top:
                grown.setdefault(name, []).append((key, top, now))
    return grown


def unmeasured(summary: dict, files: dict) -> list[str]:
    return sorted(set(files) - set(summary))


# ── отчёт ──────────────────────────────────────────────────────────────
def report(pop: str, summary: dict, detail: dict):
    """Базовая линия популяции: что кнопка снимает, чем платит, за сколько."""
    if not summary:
        print(f"\n[{pop}] таблица пуста: ни один холст не измерен")
        return
    print(f"\n=== [{pop}] итог по {len(summary)} холстам ===")
    # Габарит суммировать бессмысленно (это размах листа, а не счётчик) —
    # по нему берётся максимум популяции.
    span = {"w", "h"}
    tot_before = {k: 0.0 for k, _ru in DEFECT_COLS}
    tot_after = dict(tot_before)
    tot_stats = {k: 0 for k in (*REMEDIES, "отклонено", "осталось")}
    tot_ref = {k: 0 for k in REFUSAL_KINDS}
    calls = blind = edges_seen = blind_edges = orphan = 0
    rb_checked = rb_bad = 0
    times = []
    for name, runs in detail.items():
        mid = len(runs) // 2
        base = sorted(runs, key=lambda r: r["cols"]["diag"])[mid]
        for k, _ru in DEFECT_COLS:
            if k not in base["before"]:
                continue
            if k in span:
                tot_before[k] = max(tot_before[k], float(base["before"][k]))
                tot_after[k] = max(tot_after[k], float(base["after"][k]))
            else:
                tot_before[k] += float(base["before"][k])
                tot_after[k] += float(base["after"][k])
        for k in tot_stats:
            tot_stats[k] += base["stats"][k]
        for k in tot_ref:
            tot_ref[k] += base["refusals"][k]
        calls += base["reseat_calls"]
        blind += base["reseat_blind"]
        edges_seen += base["reseat_edges"]
        blind_edges += base["reseat_blind_edges"]
        orphan += base["reseat_orphan"]
        rb_checked += base["rollback_checked"]
        rb_bad += base["rollback_bad"]
        times += [r["cols"]["ms"] for r in runs]
    print("  дефекты до -> после (сумма по популяции; габарит — максимум):")
    for k, ru in DEFECT_COLS:
        if k in ("rollback_bad",):
            continue
        b, a = tot_before.get(k, 0.0), tot_after.get(k, 0.0)
        mark = "" if abs(a - b) < 1e-9 else ("  <-- лучше" if a < b
                                             else "  <-- ХУЖЕ")
        print(f"    {ru:<18} {b:>9.1f} -> {a:>9.1f}{mark}")
    print("  ходы движка: " + ", ".join(f"{k} {tot_stats[k]}"
                                        for k in REMEDIES))
    print(f"    принято всего {sum(tot_stats[k] for k in REMEDIES)}, "
          f"отклонено {tot_stats['отклонено']}, "
          f"осталось косых рёбер {tot_stats['осталось']}")
    print("  причины отказа: " + ", ".join(f"{k} {v}"
                                           for k, v in tot_ref.items()))
    share = (100.0 * blind / calls) if calls else 0.0
    e_share = (100.0 * blind_edges / edges_seen) if edges_seen else 0.0
    print(f"  честность reseat-гейта: вызовов {calls}, обесточено {blind} "
          f"({share:.1f} %); рёбер {edges_seen}, сломано до снимка "
          f"{blind_edges} ({e_share:.1f} %); вызовов не после сдвига {orphan}")
    print(f"  целостность отката: проверено {rb_checked} отклонённых ходов, "
          f"расхождений {rb_bad}")
    print(f"  узлов за листом 1920x1080: до {tot_before['offsheet']:.0f}, "
          f"после {tot_after['offsheet']:.0f}")
    if times:
        print(f"  время: p50 {statistics.median(times):.0f} мс, "
              f"худший {max(times):.0f} мс, всего прогонов {len(times)}")


# ── main ───────────────────────────────────────────────────────────────
def env_problem() -> str | None:
    """Почему стенд НЕ ИМЕЕТ ПРАВА судить. Ветка первая — до измерений.

    `PROTOCOL`: «гейт, судящий по ОТСУТСТВИЮ наблюдения, обязан сначала
    доказать, что наблюдение вообще могло состояться». У кнопки два молчаливых
    фолбэка: без Qt её нет вовсе, а без SWIG-биндинга `edit_avoid` уводит
    маршруты на самописную лестницу — и то и другое даёт числа НЕ ТОЙ кнопки.
    """
    try:
        from PySide6.QtWidgets import QApplication  # noqa: F401
    except ImportError as exc:
        return f"нет PySide6 ({exc}) — кнопки без Qt не существует"
    from modules.graph.core import edit_avoid
    if edit_avoid.load_binding() is None:
        return ("SWIG-биндинг libavoid не грузится — `_reseat_after_resize` "
                "уйдёт на самописную лестницу, и стенд замерит НЕ ТУ кнопку")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--population", choices=("raw", "edited"), default=None,
                    help="одна популяция вместо обеих")
    ap.add_argument("--all", action="store_true", help="обе популяции")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                    help=f"прогонов на холст (недетерминизм libavoid), "
                         f"по умолчанию {DEFAULT_RUNS}")
    ap.add_argument("--limit", type=int, default=0,
                    help="взять только первые N холстов популяции (отладка)")
    ap.add_argument("--rollback-cap", type=int, default=400,
                    help="сколько отклонённых ходов на холст сверять "
                         "бит-в-бит (полная сверка дорога)")
    ap.add_argument("--json", type=Path, default=None,
                    help="выгрузить подробности прогонов")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if not args.all and args.population is None:
        ap.error("нужна --population или --all")
    pops = ("raw", "edited") if args.all else (args.population,)

    problem = env_problem()
    if problem:
        print(f"[СУДИТЬ НЕЧЕМ] {problem}")
        return 2

    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    assert app is not None

    import tempfile
    base = read_baseline()
    base_pops = base.get("populations", {})
    unjudged: list[str] = []
    out: dict[str, dict] = {}

    with tempfile.TemporaryDirectory(prefix="smooth_bench_") as td:
        tmp = Path(td)
        for pop in pops:
            paths = population_paths(pop)
            if args.limit:
                paths = sorted(paths, key=lambda q: q.name)[:args.limit]
            print(f"\n=== популяция [{pop}]: {len(paths)} холстов, "
                  f"{args.runs} прогонов ===")
            if not paths:
                unjudged.append(f"популяции [{pop}] нет в дереве")
                continue
            summary, detail, bad = measure(paths, args.runs, tmp,
                                           args.rollback_cap)
            unjudged += bad
            out[pop] = {"summary": summary, "detail": detail,
                        "inputs": input_fingerprints(
                            [p for p in paths if p.name in summary])}
            report(pop, summary, detail)

    if args.json:
        args.json.write_text(json.dumps(
            {p: {"summary": d["summary"],
                 "detail": d["detail"]} for p, d in out.items()},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nподробности -> {args.json}")

    if args.write_baseline:
        return _write(out, base, base_pops, unjudged, args.runs)
    if args.check:
        return _check(out, base, base_pops, unjudged, args.runs)
    return 2 if unjudged else 0


def _check(out, base, base_pops, unjudged, runs) -> int:
    if not base_pops:
        print("\n[СУДИТЬ НЕЧЕМ] эталона нет — сначала --write-baseline")
        return 2
    base_runs = int(base.get("runs", 0))
    weak = runs < base_runs
    bad = 0
    holes: list[str] = list(unjudged)
    said: list[str] = []          # уже названо строкой выше — не повторять
    for pop, data in out.items():
        bp = base_pops.get(pop) or {}
        files, known = bp.get("files", {}), bp.get("inputs", {})
        drift = input_drift(data["inputs"], known, files)
        grown = growth(data["summary"], files, drift)
        for name, why in sorted(drift.items()):
            print(f"[СУДИТЬ НЕЧЕМ] {pop}/{name}: {why}")
            said.append(f"{pop}/{name}")
        for name, cols in sorted(grown.items()):
            for key, top, now in cols:
                print(f"[ПРОВАЛ] {pop}/{name}: {key} медиана {now} > "
                      f"верха диапазона эталона {top}")
                bad += 1
        for name, cur in sorted(data["summary"].items()):
            b = files.get(name)
            if b is None:
                print(f"{pop}/{name}: в эталоне нет — пропуск сравнения")
                continue
            if name in drift:
                continue
            for key, _ru in DEFECT_COLS:
                if key in cur and key in b and cur[key]["med"] < b[key]["lo"]:
                    print(f"лучше {pop}/{name}: {key} {b[key]['lo']} -> "
                          f"{cur[key]['med']}")
        missing = unmeasured(data["summary"], files)
        if missing:
            holes.append(f"[{pop}] не измерено {len(missing)} холстов эталона "
                         f"из {len(files)}: {', '.join(missing)}")
        if not files:
            holes.append(f"[{pop}] эталон о популяции молчит")
    for pop in base_pops:
        if pop not in out:
            holes.append(f"[{pop}] популяция эталона вовсе не измерена")
    print(f"\nрост дефектов: {bad}")
    if bad:                                  # доказанный рост сильнее неполноты
        return 1
    if weak:
        print(f"[СУДИТЬ НЕЧЕМ] прогонов {runs}, а эталон снят на {base_runs} — "
              f"замер слабее эталона, диапазон шума несопоставим")
        return 2
    for h in holes:
        print(f"[СУДИТЬ НЕЧЕМ] {h}")
    return 2 if (holes or said) else 0


def _write(out, base, base_pops, unjudged, runs) -> int:
    """Пересъём с теми же тремя исходами (Д6, урок 1-25/1-28/1-30)."""
    refused, holes = [], list(unjudged)
    for pop, data in out.items():
        bp = base_pops.get(pop) or {}
        files, known = bp.get("files", {}), bp.get("inputs", {})
        if not files:
            continue                          # первый снимок популяции
        drift = input_drift(data["inputs"], known, files)
        for name, why in sorted(drift.items()):
            print(f"[ВХОД НЕ ТОТ] {pop}/{name}: {why}; вердикт эталона "
                  f"к нынешним данным не относится")
        missing = unmeasured(data["summary"], files)
        if missing:
            holes.append(f"[{pop}] не измерено {len(missing)} холстов эталона "
                         f"из {len(files)} — пересъём вычеркнул бы их: "
                         f"{', '.join(missing)}")
        grown = growth(data["summary"], files, drift)
        if grown:
            refused.append(f"[{pop}] дефекты выросли — пересъём узаконит рост:\n"
                           + "\n".join(f"    {n}: {k} {top} -> {now}"
                                       for n, cols in sorted(grown.items())
                                       for k, top, now in cols))
    base_runs = int(base.get("runs", 0))
    if base_pops and runs < base_runs:
        holes.append(f"прогонов {runs}, а эталон снят на {base_runs} — "
                     f"пересъём сузил бы диапазон шума")
    for msg in holes:
        print(f"[СУДИТЬ НЕЧЕМ] {msg}")
    for msg in refused:
        print(f"[ОТКАЗ] {msg}")
    if refused:
        return 1
    if holes:
        return 2
    pops = dict(base_pops)
    for pop, data in out.items():
        pops[pop] = {"files": data["summary"], "inputs": data["inputs"]}
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps(
        {"version": BASELINE_VERSION, "runs": runs, "populations": pops},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nбаза заморожена -> {BASELINE.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

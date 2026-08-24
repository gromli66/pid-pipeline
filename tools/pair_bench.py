# -*- coding: utf-8 -*-
"""pair_bench.py — стенд диффов «до/после оператора» (ДН4, пункт 0.11 дороги).

Зачем. Конвейер артефакт-ориентирован: на каждом ручном этапе рядом лежат две
версии одного артефакта — машинная и та, что оператор оставил после правки.
Разница между ними и есть цена ошибок модели, уже записанная на диск. Стенд
читает семь таких пар и печатает по каждой «объём правок» — сколько объектов
оператору пришлось добавить, удалить, подвинуть, переназвать.

Отвечает на вопрос «стала ли модель лучше» РЕТРОАКТИВНО: числа снимаются с
того, что уже есть, боевой код для этого править не нужно (и не правится —
`tools/` не импортируется из `app/`/`worker/`/`ui/`).

Пары (до → после), покрытие корпуса этой машины на 2026-08-18:

| стадия       | до                            | после                              | есть  |
|--------------|-------------------------------|------------------------------------|-------|
| detection    | detection/yolo_predicted.txt  | detection/yolo_validated.txt       | 17/17 |
| segmentation | segmentation/pipe_mask.png    | segmentation/pipe_mask_validated.png | 17/17 |
| junction     | junction/points.json          | junction/points_validated.json     | 12/17 |
| graph        | graph/graph.json              | graph/graph_validated.json         | 17/17 |
| contours     | contours/contours_auto.json   | contours/contours_validated.json   | 11/17 |
| ocr          | ocr/ocr_result.json           | ocr_binding/ocr_binding.json       | 17/17 |
| layout       | graph/graph_validated.json    | graph/graph_canvas.json            | 15/17 |

Что считается «правкой» — колонки в `EDIT`: только они не имеют права расти в
`--check`. Остальные числа справочные (сколько объектов было и стало).

Оговорки, которые стенд НЕ прячет:
- **Тождество объектов.** `id` есть только у графа (пары `graph`, `layout`).
  Боксы, точки и текстовые блоки сопоставляются геометрически — боксы по IoU,
  точки по радиусу. Пара, не нашедшая соответствия, читается как
  добавленная/удалённая: сильно переставленный объект даст +1 и −1 вместо
  «сдвинут». Это занижает «сдвиг» и завышает «добавлено/удалено», а не наоборот.
- **Раскладка координаты не сравнивает.** `graph_canvas.json` живёт в системе
  координат холста, `graph_validated.json` — в пиксельной; сравнивать их
  положения бессмысленно. Пара `layout` считает только состав (что оператор
  дорисовал/снёс) и число объектов с флагом `manual`.
- **Порядок пар не гарантирован.** Если «до» новее «после» (стадию перезапустили
  ПОСЛЕ правки оператора), пара помечается `!` и её числа — не правки оператора.
  Замер 2026-08-18: так стоят 4 пары `layout` и 2 пары `ocr`.
- **Сдвиг начала координат.** Если половина набора точек не сопоставилась, а
  остаток объясняется одним общим смещением, стенд печатает `origin_shift` (px):
  стороны пары сняты на разных обрезках листа, и «правки» там мнимые. Замер
  2026-08-18: так стоит `junction` у `8d14cf73` (+26/−21 px).
- **OCR.** Устойчивого `id` у блока нет до формата v3
  (`docs/planning/PLAN_2026-08-13_ocr_markup_persistence.md`), поэтому «исправлен
  текст» опирается на пересечение рамок; блок, у которого рамку переделали,
  попадает в `box_edited`, а не в «добавлено + удалено».

Данные корпуса в git не лежат (`storage/diagrams/` — данные заказчика), поэтому
стенд НЕ включён в CI: на чистом клоне ему нечего мерить, и `--check` там честно
возвращает 2 («данных нет»), а не зелёный ноль. Логика диффов закрыта юнит-тестами
`tests/test_pair_bench.py`, они в CI и идут на синтетических артефактах.

Три исхода `--check` (`PROTOCOL §5`; полярность выправлена пунктом 1-45):
    0 — объём правок не вырос;
    1 — вырос: оператору пришлось доделывать больше. Единственное здешнее
        наблюдение, которое говорит о коде;
    2 — СУДИТЬ НЕЧЕМ: мерить нечего (нет данных, нет эталона, нет общих uid),
        пара эталона не измерена — её нет на диске, — или вход пары не тот,
        на котором эталон снят (`inputs`, версия 2 эталона).
До 1-45 стенд собирал вердикт «регресс кода» из ОТСУТСТВИЯ наблюдения: пропавшая
пара шла в один счётчик с ростом правок и давала exit 1 «рост правок: 1» при нуле
строк «ХУЖЕ», а отпечатка входа у эталона не было вовсе — дрейф данных читался
как рост правок (`TESTING §9`, замеры §109 и §120).

Запуск (из корня репо):
    python -X utf8 tools/pair_bench.py                    # таблицы + сводка с медианами
    python -X utf8 tools/pair_bench.py --stage graph      # одна пара
    python -X utf8 tools/pair_bench.py --json out.json    # машинный вывод
    python -X utf8 tools/pair_bench.py --write-baseline   # заморозить эталон (Д6)
    python -X utf8 tools/pair_bench.py --check            # против эталона, 0 / 1 / 2

Только чтение: ни один артефакт не изменяется.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.dn2_edit_diff import INTENT, diff_pair  # noqa: E402

BASELINE = REPO / "tools" / "bench" / "pair_baseline.json"
# Версия формата эталона. 2 — рядом с числами лежит `inputs`, отпечаток входа
# каждой пары (пункт 1-45). Старый плоский вид `{uid8: {стадия: числа}}`
# читается как «отпечатков нет»: судить по нему нечем, пока его не пересняли.
BASELINE_VERSION = 2

# Пары «до → после». Порядок = порядок конвейера.
STAGES = [
    ("detection", "detection/yolo_predicted.txt", "detection/yolo_validated.txt"),
    ("segmentation", "segmentation/pipe_mask.png",
     "segmentation/pipe_mask_validated.png"),
    ("junction", "junction/points.json", "junction/points_validated.json"),
    ("graph", "graph/graph.json", "graph/graph_validated.json"),
    ("contours", "contours/contours_auto.json", "contours/contours_validated.json"),
    ("ocr", "ocr/ocr_result.json", "ocr_binding/ocr_binding.json"),
    ("layout", "graph/graph_validated.json", "graph/graph_canvas.json"),
]

# Метрики «объём правок»: в --check не имеют права расти. Всё остальное —
# справочное (сколько было и стало) и на вердикт не влияет.
EDIT = {
    "detection": ("added", "removed", "moved", "retyped"),
    "segmentation": ("px_added", "px_removed"),
    "junction": ("added", "removed", "moved", "br_added", "br_removed", "br_moved"),
    # У графа тождество по `id`, поэтому классы берутся у судьи ДН2 (пункт 0.9)
    # вместе с его же делением «намерение против следствия»: переезд конца
    # вслед за своим узлом и пересчёт waypoints — не правки.
    "graph": tuple(sorted(INTENT)),
    "contours": ("edited",),
    "ocr": ("added", "removed", "text_fixed", "box_edited"),
    "layout": ("node_added", "node_removed", "edge_added", "edge_removed",
               "edge_rewired"),
}

# Порог IoU для «это тот же бокс». 0.5 — общепринятая граница совпадения.
IOU_MATCH = 0.5
# Сдвиг бокса в долях стороны листа: 0.001 ≈ 4 px на листе 4000 px. Ниже — шум
# округления: yolo_validated.txt пересобирается из COCO и расходится с
# yolo_predicted.txt в шестом знаке даже там, где оператор не трогал бокс.
BOX_EPS = 0.001
# Радиус, в котором точка перекрёстка считается той же. 15 px — размер маркера,
# который клиент кладёт в `points_validated.json` полем `size`.
POINT_RADIUS = 15.0
# Сдвиг точки, ниже которого она «принята без правки» (координаты целые).
POINT_EPS = 0.5


# --------------------------------------------------------------------------
# Геометрия: сопоставление объектов без `id`
# --------------------------------------------------------------------------

def _iou(a, b) -> float:
    """IoU двух рамок `(x1, y1, x2, y2)` в одной системе координат."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = ix2 - ix1, iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _greedy(cand, n_before: int, n_after: int):
    """Жадное сопоставление по отсортированным кандидатам `(вес, i, j)`.

    Вес уже отсортирован «лучшее первым»; ничьи разбиваются индексами, поэтому
    результат не зависит от порядка перебора и повторяется от прогона к прогону.
    """
    used_b: set[int] = set()
    used_a: set[int] = set()
    pairs = []
    for _w, i, j in cand:
        if i in used_b or j in used_a:
            continue
        used_b.add(i)
        used_a.add(j)
        pairs.append((i, j))
    lost = [i for i in range(n_before) if i not in used_b]
    new = [j for j in range(n_after) if j not in used_a]
    return pairs, lost, new


def match_boxes(before, after, thr: float = IOU_MATCH):
    """Пары рамок по IoU ≥ thr; остаток — удалённые и добавленные."""
    cand = []
    for i, b in enumerate(before):
        for j, a in enumerate(after):
            iou = _iou(b, a)
            if iou >= thr:
                cand.append((-iou, i, j))
    cand.sort()
    return _greedy(cand, len(before), len(after))


def match_points(before, after, radius: float = POINT_RADIUS):
    """Пары точек в радиусе; остаток — удалённые и добавленные."""
    cand = []
    for i, (bx, by) in enumerate(before):
        for j, (ax, ay) in enumerate(after):
            dist = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
            if dist <= radius:
                cand.append((dist, i, j))
    cand.sort()
    return _greedy(cand, len(before), len(after))


def modal_shift(before, after) -> tuple[int, int, float]:
    """Самое частое смещение «точка → ближайшая к ней» и его доля.

    Ловит сдвиг начала координат: пара, снятая на разных обрезках листа, даёт
    не правки оператора, а перенос всего набора целиком (замер 2026-08-18 на
    `8d14cf73`: +26/−21 px у 66 точек из 154 при разбросе ±2 px).
    """
    if not before or not after:
        return 0, 0, 0.0
    offsets: dict[tuple[int, int], int] = {}
    for ax, ay in after:
        bx, by = min(before, key=lambda p: (p[0] - ax) ** 2 + (p[1] - ay) ** 2)
        key = (round(ax - bx), round(ay - by))
        offsets[key] = offsets.get(key, 0) + 1
    (dx, dy), hits = max(offsets.items(), key=lambda kv: (kv[1], kv[0]))
    return dx, dy, hits / len(after)


# --------------------------------------------------------------------------
# Замер по стадиям
# --------------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _yolo_boxes(path: Path):
    """Строки YOLO `cls cx cy w h` → `(cls, x1, y1, x2, y2)`, координаты 0..1."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        cls = int(float(parts[0]))
        cx, cy, w, h = (float(v) for v in parts[1:5])
        out.append((cls, cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
    return out


def measure_detection(before: Path, after: Path) -> dict:
    bb, aa = _yolo_boxes(before), _yolo_boxes(after)
    pairs, lost, new = match_boxes([b[1:] for b in bb], [a[1:] for a in aa])
    moved = retyped = 0
    for i, j in pairs:
        if any(abs(x - y) > BOX_EPS for x, y in zip(bb[i][1:], aa[j][1:])):
            moved += 1
        if bb[i][0] != aa[j][0]:
            retyped += 1
    return {"before": len(bb), "after": len(aa), "added": len(new),
            "removed": len(lost), "moved": moved, "retyped": retyped}


def _binary_mask(path: Path):
    """Маска как булев массив. Белое = маска, независимо от режима PNG.

    `pipe_mask.png` пишется воркером в градациях серого, а
    `pipe_mask_validated.png` приходит из редактора клиента в RGBA — сравнивать
    их «как есть» нельзя (замер 2026-08-18: альфа сплошь 255, то есть отдельным
    каналом маска не задана, она в RGB).
    """
    from PIL import Image  # локальный импорт: numpy/Pillow нужны только здесь
    import numpy as np

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as img:
        arr = np.array(img)
    if arr.ndim == 2:
        return arr > 127
    mask = arr[..., :3].max(axis=2) > 127
    if arr.shape[2] == 4:
        mask &= arr[..., 3] > 127
    return mask


def measure_segmentation(before: Path, after: Path) -> dict:
    b, a = _binary_mask(before), _binary_mask(after)
    if b.shape != a.shape:
        # Разный размер листа — сравнивать нечего, но и молчать нельзя.
        return {"shape_mismatch": 1, "px_before": int(b.sum()),
                "px_after": int(a.sum()), "px_added": 0, "px_removed": 0,
                "changed_ppm": 0}
    added = int((~b & a).sum())
    removed = int((b & ~a).sum())
    return {"shape_mismatch": 0, "px_before": int(b.sum()), "px_after": int(a.sum()),
            "px_added": added, "px_removed": removed,
            "changed_ppm": round((added + removed) * 1_000_000 / b.size)}


def _points(data: dict, key: str):
    return [(float(p["x"]), float(p["y"])) for p in data.get(key) or []]


def _point_counts(before, after) -> tuple[int, int, int, int]:
    """(добавлено, удалено, сдвинуто, сопоставлено)."""
    pairs, lost, new = match_points(before, after)
    moved = sum(1 for i, j in pairs
                if abs(before[i][0] - after[j][0]) > POINT_EPS
                or abs(before[i][1] - after[j][1]) > POINT_EPS)
    return len(new), len(lost), moved, len(pairs)


def _origin_shift(before, after, matched: int) -> int:
    """Сдвиг начала координат в пикселях; 0 — сдвига нет.

    Считается, только когда сопоставилось меньше половины набора: иначе это
    обычные точечные правки, а не перенос листа.
    """
    if matched >= max(len(before), len(after), 1) / 2:
        return 0
    dx, dy, share = modal_shift(before, after)
    if share < 0.2 or (dx, dy) == (0, 0):
        return 0
    return round((dx * dx + dy * dy) ** 0.5)


def measure_junction(before: Path, after: Path) -> dict:
    b, a = _read_json(before), _read_json(after)
    jb, ja = _points(b, "junctions"), _points(a, "junctions")
    added, removed, moved, matched = _point_counts(jb, ja)
    br_added, br_removed, br_moved, _br_matched = _point_counts(
        _points(b, "bridges"), _points(a, "bridges"))
    return {"before": len(jb), "after": len(ja),
            "added": added, "removed": removed, "moved": moved,
            "br_before": len(b.get("bridges") or []),
            "br_after": len(a.get("bridges") or []),
            "br_added": br_added, "br_removed": br_removed, "br_moved": br_moved,
            "origin_shift": _origin_shift(jb, ja, matched)}


def measure_graph(before: Path, after: Path) -> dict:
    """Дифф графа классификатором ДН2 (`tools/dn2_edit_diff.py`, пункт 0.9)."""
    b, a = _read_json(before), _read_json(after)
    res = diff_pair(b, a)
    res["before_nodes"] = len(b.get("nodes") or [])
    res["after_nodes"] = len(a.get("nodes") or [])
    res["before_edges"] = len(b.get("links") or [])
    res["after_edges"] = len(a.get("links") or [])
    return res


def measure_contours(before: Path, after: Path) -> dict:
    """Контуры: правка видна внутри самого `contours_validated.json`.

    Файл несёт обе версии полигона (`polygon_auto` и `polygon_validated`) и
    флаг `was_edited`, поэтому «переделан» считается по нему, а `contours_auto`
    нужен только чтобы увидеть узлы, до которых оператор не дошёл.
    """
    auto = _read_json(before).get("nodes") or []
    val = _read_json(after).get("nodes") or []
    edited = sum(1 for n in val
                 if n.get("was_edited")
                 or (n.get("polygon_validated") is not None
                     and n.get("polygon_validated") != n.get("polygon_auto")))
    return {"auto": len(auto), "validated": len(val), "edited": edited,
            "approved": sum(1 for n in val if n.get("status") == "approved"),
            "skipped": sum(1 for n in val if n.get("status") == "skipped"),
            "not_reviewed": max(0, len(auto) - len(val))}


def _norm_text(value) -> str:
    return " ".join(str(value or "").split())


def _ocr_blocks(data: dict, keys) -> list:
    out = []
    for key in keys:
        for block in data.get(key) or []:
            bbox = block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            x1, y1, x2, y2 = (float(v) for v in bbox)
            out.append(((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
                        _norm_text(block.get("text"))))
    return out


def measure_ocr(before: Path, after: Path) -> dict:
    """OCR: машинные блоки против блоков, оставшихся после правки оператора.

    Тождество блока — только рамка (устойчивого `id` в формате v2 нет), а
    оператор рамки как раз и растягивает: замер 2026-08-18 на `620cc50d` —
    «орифер» → «калорифер 1Б» с IoU 0.18. Поэтому сопоставление в два прохода:
    сперва IoU ≥ 0.5 («та же рамка»), потом остаток по любому пересечению
    («рамку переделали», колонка `box_edited`). Без второго прохода одна правка
    рамки давала +1 добавлено и +1 удалено, и таблица врала на порядок.

    Соглашение о порядке осей в bbox для IoU безразлично: обе стороны пишет
    один конвейер.
    """
    b = _read_json(before)
    a = _read_json(after)
    bb = _ocr_blocks(b, ("target", "secondary"))
    aa = _ocr_blocks(a, ("edited_blocks",))
    pairs, lost, new = match_boxes([x[0] for x in bb], [x[0] for x in aa])
    loose, lost2, new2 = match_boxes([bb[i][0] for i in lost],
                                     [aa[j][0] for j in new], thr=1e-9)
    pairs += [(lost[i], new[j]) for i, j in loose]
    fixed = sum(1 for i, j in pairs if bb[i][1] != aa[j][1])
    return {"before": len(bb), "after": len(aa), "added": len(new2),
            "removed": len(lost2), "text_fixed": fixed,
            "box_edited": len(loose), "bindings": len(a.get("bindings") or [])}


def measure_layout(before: Path, after: Path) -> dict:
    """Раскладка: только состав холста. Координаты у пары разные по построению."""
    b, a = _read_json(before), _read_json(after)
    nb = {n["id"] for n in b.get("nodes") or []}
    na = {n["id"] for n in a.get("nodes") or []}
    eb = {e["id"]: e for e in b.get("links") or []}
    ea = {e["id"]: e for e in a.get("links") or []}
    rewired = sum(1 for eid in set(eb) & set(ea)
                  if (eb[eid].get("source"), eb[eid].get("target"))
                  != (ea[eid].get("source"), ea[eid].get("target")))
    transform = (a.get("graph") or {}).get("canvas_transform") or {}
    return {"node_added": len(na - nb), "node_removed": len(nb - na),
            "edge_added": len(set(ea) - set(eb)),
            "edge_removed": len(set(eb) - set(ea)),
            "edge_rewired": rewired,
            "manual_nodes": sum(1 for n in a.get("nodes") or [] if n.get("manual")),
            "manual_edges": sum(1 for e in a.get("links") or [] if e.get("manual")),
            "operator_saved": 1 if transform.get("operator_saved") else 0}


MEASURE = {
    "detection": measure_detection,
    "segmentation": measure_segmentation,
    "junction": measure_junction,
    "graph": measure_graph,
    "contours": measure_contours,
    "ocr": measure_ocr,
    "layout": measure_layout,
}


# --------------------------------------------------------------------------
# Сбор корпуса
# --------------------------------------------------------------------------

def default_storage() -> Path:
    """Каталог диаграмм: как у воркера (`STORAGE_PATH`), иначе storage репо."""
    env = os.getenv("STORAGE_PATH")
    return Path(env) if env else REPO / "storage" / "diagrams"


def _rel(path: Path) -> str:
    """Путь от корня репо, если он внутри. Иначе — как есть.

    Близнец `edit_bench._rel` и по причине тоже: стенд под тестом получает
    временный корпус и временный эталон ВНЕ дерева, а голый `relative_to`
    падает там `ValueError` ещё на сборке справки argparse — то есть стенд
    нельзя было прогнать от края до края (пункт 1-45).
    """
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def pair_fingerprint(before: Path, after: Path) -> str:
    """Отпечаток ВХОДА пары — обе её стороны (пункт 1-45, идиома GATE-6).

    Эталон ключуется по `uid8/стадия` и до 1-45 не помнил, НА КАКИХ данных
    он снят. Корпус в `storage/` живой: обычный запуск конвейера переписывает
    артефакты под тем же uid, и разница «эталон 18.08 против файлов 19.08»
    читалась как рост правок оператора, то есть как регресс модели.

    Считается по БАЙТАМ, а не по разобранному содержимому, — в отличие от
    `corpus.data_fingerprint()` у ПР1. Две причины: половина пар вообще не
    json (`yolo_*.txt`, `pipe_mask*.png`), а EOL-ловушки здесь нет — эти
    файлы лежат вне git (`.gitignore:32`), и `.gitattributes` их не трогает.
    """
    digest = hashlib.sha256()
    for path in (before, after):
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def collect(storage: Path, stages=None) -> dict:
    """{uid8: {стадия: {метрики}}} по всем диаграммам, где пара есть на диске.

    Рядом — `inputs` тех же ключей (отпечаток входа каждой пары) и `stale`:
    пары с нарушенным порядком (`до` новее `после`).
    """
    wanted = [s for s in STAGES if stages is None or s[0] in stages]
    rows: dict[str, dict] = {}
    stale: dict[str, list] = {}
    inputs: dict[str, dict] = {}
    report = {"rows": rows, "stale": stale, "inputs": inputs,
              "storage": str(storage), "stages": [s[0] for s in wanted]}
    if not storage.is_dir():
        return report
    for diagram in sorted(p for p in storage.iterdir() if p.is_dir()):
        uid8 = diagram.name[:8]
        for name, rel_before, rel_after in wanted:
            before, after = diagram / rel_before, diagram / rel_after
            if not (before.exists() and after.exists()):
                continue
            rows.setdefault(uid8, {})[name] = MEASURE[name](before, after)
            inputs.setdefault(uid8, {})[name] = pair_fingerprint(before, after)
            if before.stat().st_mtime > after.stat().st_mtime + 1:
                stale.setdefault(uid8, []).append(name)
    return report


# --------------------------------------------------------------------------
# Печать
# --------------------------------------------------------------------------

def _stage_keys(rows: dict, stage: str) -> list:
    keys: list[str] = []
    for row in rows.values():
        for key in row.get(stage, {}):
            if key not in keys:
                keys.append(key)
    return keys


def print_report(report: dict) -> None:
    rows, stale = report["rows"], report["stale"]
    print(f"корпус: {report['storage']} — диаграмм с парами {len(rows)}")
    for stage, rel_before, rel_after in STAGES:
        if stage not in report["stages"]:
            continue
        have = {uid: row[stage] for uid, row in rows.items() if stage in row}
        print(f"\n=== {stage}: {rel_before} → {rel_after} — пар {len(have)}")
        if not have:
            print("    пар нет")
            continue
        keys = _stage_keys(rows, stage)
        edits = set(EDIT[stage])
        head = "uid".ljust(10) + " ".join(
            (k + ("*" if k in edits else "")).rjust(13) for k in keys)
        print(head)
        print("-" * len(head))
        for uid in sorted(have):
            mark = "!" if stage in stale.get(uid, []) else " "
            print(mark + uid.ljust(9) + " ".join(
                str(have[uid].get(k, "")).rjust(13) for k in keys))
        print("    медиана: " + ", ".join(
            f"{k}={statistics.median([have[u].get(k, 0) for u in have]):g}"
            for k in keys if k in edits))
    if stale:
        print("\n! пары с нарушенным порядком (файл «до» новее файла «после» — "
              "стадию перезапустили после правки оператора, числа не читать "
              "как правки):")
        for uid in sorted(stale):
            print(f"    {uid}: {', '.join(stale[uid])}")
    print("\n* — колонки «объём правок»: только они судятся в --check")


# --------------------------------------------------------------------------
# Эталон и вердикт
# --------------------------------------------------------------------------

def read_baseline() -> dict:
    """Эталон в нынешнем виде: `{"rows": ..., "inputs": ...}`.

    Плоский вид `{uid8: {стадия: числа}}` — эталон, снятый до пункта 1-45.
    Отпечатков входа в нём нет, и это читается не как «вход тот же», а как
    «эталон не может назвать данные, о которых судит»: та же развилка, что
    у версии 2 эталона «Ручной правки» (`TESTING §8.2`).
    """
    if not BASELINE.exists():
        return {"rows": {}, "inputs": {}}
    raw = json.loads(BASELINE.read_text(encoding="utf-8"))
    if raw.get("version"):
        return {"rows": raw.get("rows") or {}, "inputs": raw.get("inputs") or {}}
    return {"rows": raw, "inputs": {}}


def verdict(rows: dict, base: dict, inputs: dict) -> tuple[int, list]:
    """(код возврата, строки отчёта) — сравнение с эталоном.

    Три исхода (`PROTOCOL §5`), и вердикт О КОДЕ собирается только из
    НАБЛЮДЕНИЙ (`PROTOCOL §Гейты`, правило заведено пунктом 1-44):
    2 — СУДИТЬ НЕЧЕМ: пусто в замере или в эталоне, ни одного общего uid,
        эталон не помнит отпечатков входа, пара эталона не измерена (её нет
        на диске) или вход пары не тот, на котором эталон снят;
    1 — вырос объём правок: оператору пришлось доделывать больше. Это
        единственное здешнее наблюдение, которое говорит о коде;
    0 — не хуже эталона.

    ⛔ До 1-45 пропавшая пара шла в один счётчик с ростом правок, и стенд
    отдавал **1 «рост правок: 1»** при НУЛЕ строк «ХУЖЕ» (замер §109: весь
    `bad` — это «ПРОПАЛА `8d14cf73/layout`»). Асимметрия была и внутри
    одного стенда: пропала ВСЯ диаграмма — «пропуск» и exit 0, пропала ОДНА
    пара — «регресс» и exit 1. Обе пропажи — одно и то же отсутствие
    наблюдения, и обе теперь «судить нечем».

    Доказанный рост сильнее неполноты (1-25): пара без вердикта не глушит
    выросшего соседа, иначе хватило бы стереть один артефакт, чтобы стенд
    замолчал обо всех.
    """
    lines: list[str] = []
    known = base.get("rows") or {}
    known_inputs = base.get("inputs") or {}
    if not known:
        return 2, ["эталона нет или он пуст — сначала --write-baseline"]
    if not rows:
        return 2, ["замер пуст: ни одной пары не найдено — данных корпуса нет"]
    common = set(rows) & set(known)
    if not common:
        return 2, [f"ни одного uid эталона нет на диске (в эталоне {len(known)}, "
                   f"замерено {len(rows)}) — судить нечем"]
    if not known_inputs:
        return 2, ["эталон не помнит отпечатков входа — он снят до пункта 1-45, "
                   "и его числа могут относиться к другим артефактам (корпус "
                   "в storage живой, TESTING §3). Лечится пересъёмом (Д6)"]
    worse, unjudged = 0, []
    for uid in sorted(set(known) - set(rows)):
        lines.append(f"НЕ ИЗМЕРЕНА {uid}: диаграммы эталона нет на диске")
        unjudged.append(uid)
    for uid in sorted(set(rows) - set(known)):
        lines.append(f"новое   {uid}: диаграммы нет в эталоне — пропуск")
    for uid in sorted(common):
        for stage in [s[0] for s in STAGES]:
            was, now = known[uid].get(stage), rows[uid].get(stage)
            if was is None:
                if now is not None:
                    lines.append(f"новое   {uid}/{stage}: в эталоне нет — пропуск")
                continue
            if now is None:
                lines.append(f"НЕ ИЗМЕРЕНА {uid}/{stage}: пара была в эталоне, "
                             f"на диске её нет — о ней вердикта нет")
                unjudged.append(f"{uid}/{stage}")
                continue
            was_input = (known_inputs.get(uid) or {}).get(stage)
            now_input = (inputs.get(uid) or {}).get(stage)
            if was_input != now_input:
                lines.append(f"ВХОД НЕ ТОТ {uid}/{stage}: артефакты пары сменились "
                             f"({(was_input or 'нет')[:12]} -> "
                             f"{(now_input or 'нет')[:12]}) — числа эталона сняты "
                             f"на других данных")
                unjudged.append(f"{uid}/{stage}")
                continue
            for key in EDIT[stage]:
                old, new = was.get(key, 0), now.get(key, 0)
                if new > old:
                    lines.append(f"ХУЖЕ    {uid}/{stage}: {key} {old} -> {new}")
                    worse += 1
                elif new < old:
                    lines.append(f"лучше   {uid}/{stage}: {key} {old} -> {new}")
    lines.append(f"\nрост правок: {worse}")
    if unjudged:
        shown = ", ".join(unjudged[:5]) + ("…" if len(unjudged) > 5 else "")
        lines.append(f"[СУДИТЬ НЕЧЕМ] без вердикта {len(unjudged)}: {shown}")
    if worse:                        # доказанный рост сильнее неполноты (1-25)
        return 1, lines
    return (2 if unjudged else 0), lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="ДН4: диффы артефактов «до/после оператора»")
    ap.add_argument("--storage", type=Path, default=None,
                    help="каталог storage/diagrams (по умолчанию STORAGE_PATH или репо)")
    ap.add_argument("--stage", action="append",
                    choices=[s[0] for s in STAGES],
                    help="считать только эту пару (можно повторять)")
    ap.add_argument("--json", type=Path, default=None, help="выгрузить числа в json")
    ap.add_argument("--write-baseline", action="store_true",
                    help=f"заморозить эталон в {_rel(BASELINE)}")
    ap.add_argument("--check", action="store_true",
                    help="сравнить с эталоном, exit 1 при росте правок")
    args = ap.parse_args(argv)
    if args.stage and (args.check or args.write_baseline):
        # Иначе неотобранные пары выглядели бы как пропавшие с диска.
        ap.error("--stage сочетается только с отчётом: эталон и --check "
                 "работают по всем парам сразу")

    storage = args.storage or default_storage()
    report = collect(storage, args.stage)
    rows = report["rows"]
    print_report(report)

    if args.json:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"\nчисла -> {args.json}")
    if args.write_baseline:
        if not rows:
            print("\nзамер пуст — эталон не тронут")
            return 2
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(
            {"version": BASELINE_VERSION, "rows": rows,
             "inputs": report["inputs"]},
            ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        print(f"\nэталон заморожен -> {_rel(BASELINE)}")
    if args.check:
        code, lines = verdict(rows, read_baseline(), report["inputs"])
        print()
        for line in lines:
            print(line)
        return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

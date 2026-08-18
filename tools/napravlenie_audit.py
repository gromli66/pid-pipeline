# -*- coding: utf-8 -*-
"""napravlenie_audit.py — приёмка стрелок направления по артефактам (пункт ВН1б дороги).

Зачем. `docs/NAPRAVLENIE_E2E_CHECKLIST.md` требует проверок §4.1–§4.3 руками, в
питон-консоли; главный вопрос («133 бокса со степенью 0 — где потерялась труба»)
руками не считается вовсе. Стенд считает то же самое по всему корпусу и делит
сироты на две кучи, как требует решение Максима 2026-08-18: «восстановимо
однозначно» против «оставить оператору».

Что меряется (только с диска, боевой код не трогается):

| проверка чек-листа | что делает стенд |
|---|---|
| §4.1 направление в COCO | боксов `napravlenie` / из них с `attributes.direction` |
| §4.2 бокс не стал узлом | доля бокса в `node_mask` (обязана быть 0) и в `pipe_mask` |
| §4.3 степени | степень СЧИТАЕТСЯ ПО `links`, а не читается из поля `degree` |
| §4.3 сироты | классификация каждого бокса без рёбер (см. ниже) |

⛔ Поле `degree` в `graph.json` доверия не заслуживает: редактор
(`ui/editors/graph_data.py::save`) синхронизирует только `num_edges`/`num_nodes`/
`num_isolated_nodes` и не пересчитывает степени узлов. В любом графе с клеймом
`_editor_commit` поле протухшее — на корпусе 2026-08-18 это 7 файлов из 18 и
37 боксов, «сирот» по полю и с ребром по факту. Поэтому стенд везде считает
инцидентность сам, а расхождение печатает отдельной колонкой.

Классификация сироты (бокс `napravlenie` без единого ребра):

- `восстановимо: одна осевая сторона` — труба на `pipe_mask` подходит вплотную
  (≤2 px) ровно к ОДНОЙ осевой грани (ось задаёт `flow_direction`), скелет там
  же не дальше 10 px. Восстановление — подключить существующую трубу, а не
  дорисовать новую;
- `восстановимо: сквозная труба` — то же с ОБЕИХ осевых сторон;
- `оператору: трубы по оси нет` — на маске нет трубы у осевых граней;
- `оператору: только перпендикуляр` — труба есть, но лишь у глухих граней
  (по модели §3.4 бокс её цеплять не имеет права);
- `оператору: скелет не дотянулся` — труба у грани есть, а скелет обрывается
  дальше 10 px: чинить это значит гадать, где именно проходил центр трубы.

Порог «100 % уверенности» — это `PIPE_TOUCH_PX`: труба ЕСТЬ на маске и КАСАЕТСЯ
грани. Всё, что дальше порога, уходит оператору осознанно (решение Максима:
догаданная труба уезжает в FXML как настоящая топология).

Данных корпуса в git нет (`storage/diagrams/` — данные заказчика), поэтому в CI
стенда нет: там `--check` честно отдаёт 2 «судить нечем». Логика классификации
закрыта юнит-тестами `tests/test_napravlenie_audit.py` на синтетических
артефактах — они в CI.

Запуск (из корня репо):
    python -X utf8 tools/napravlenie_audit.py                  # таблица по корпусу
    python -X utf8 tools/napravlenie_audit.py --orphans        # список всех сирот
    python -X utf8 tools/napravlenie_audit.py --uid 0fc9d04c --check   # приёмка прогона
    python -X utf8 tools/napravlenie_audit.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional

import numpy as np

REPO = Path(__file__).resolve().parents[1]

# Класс стрелки: внутренний YOLO id 40, в CVAT — имя категории (id = 40 + 1).
NAPRAVLENIE = "napravlenie"
NAPRAVLENIE_CLASS_ID = 40

PIPE_TOUCH_PX = 2      # труба «вплотную» к грани бокса
SKELETON_REACH_PX = 10  # насколько скелету позволено не дотянуться до грани
PROBE_PX = 60          # как далеко наружу вообще смотрим

# §4.2 «бокс не стал узлом» считается по СВОЕЙ площади бокса: соседнее
# оборудование сплошь и рядом перекрывает рамку стрелки (замер 2026-08-18:
# 68 боксов корпуса с ненулевым `node_mask`, 67 из них объясняются накрытием
# чужим боксом). Наивное «доля > 0» дало бы ложную тревогу на две трети из них.
NEIGHBOR_PAD_PX = 2    # запас на полигон, вылезший за рамку соседа
NODE_MASK_TOL = 0.05   # доля СВОЕЙ площади бокса, ниже которой это шум границы

# Классы, которые в node_mask не попадают вовсе (эталон:
# worker/tasks/segmentation.py + app/api/validation.py) — их рамки не могут
# служить объяснением заливки.
NOT_A_NODE = {"truba", "annotation", "strelka", "background", NAPRAVLENIE}

AXIS_SIDES = {"h": ("L", "R"), "v": ("T", "B")}

BUCKET_AXIS_ONE = "восстановимо: одна осевая сторона"
BUCKET_AXIS_BOTH = "восстановимо: сквозная труба"
BUCKET_NO_PIPE = "оператору: трубы по оси нет"
BUCKET_PERP_ONLY = "оператору: только перпендикуляр"
BUCKET_SKELETON = "оператору: скелет не дотянулся"

RESTORABLE = (BUCKET_AXIS_ONE, BUCKET_AXIS_BOTH)
BUCKETS = (BUCKET_AXIS_ONE, BUCKET_AXIS_BOTH, BUCKET_NO_PIPE,
           BUCKET_PERP_ONLY, BUCKET_SKELETON)


# --------------------------------------------------------------------------
# Геометрия: зазор от грани бокса до первого пикселя маски наружу
# --------------------------------------------------------------------------

def gap_to_mask(mask, box, side: str, reach: int = PROBE_PX) -> Optional[int]:
    """Расстояние (px) от грани `side` бокса до ближайшего пикселя маски наружу.

    Смотрим не всю грань, а центральную половину: труба подходит к середине,
    а по углам в полосу попадают соседние объекты. `None` — не нашли в `reach`
    (или грань упёрлась в край листа).
    """
    x1, y1, x2, y2 = box
    height, width = mask.shape[:2]
    cy, cx = (y1 + y2) // 2, (x1 + x2) // 2
    half_v = max(2, (y2 - y1) // 4)
    half_h = max(2, (x2 - x1) // 4)
    for dist in range(1, reach + 1):
        if side in ("L", "R"):
            x = x1 - dist if side == "L" else x2 + dist
            if not 0 <= x < width:
                return None
            band = mask[max(0, cy - half_v):cy + half_v + 1, x]
        else:
            y = y1 - dist if side == "T" else y2 + dist
            if not 0 <= y < height:
                return None
            band = mask[y, max(0, cx - half_h):cx + half_h + 1]
        if band.size and (band > 0).any():
            return dist
    return None


def fill_ratio(mask, box, ignore=None) -> float:
    """Доля площади бокса, залитая маской (`ignore` — что не считать своим)."""
    x1, y1, x2, y2 = box
    sub = mask[max(0, y1):y2 + 1, max(0, x1):x2 + 1] > 0
    if not sub.size:
        return 0.0
    if ignore is not None:
        sub = sub & ~(ignore[max(0, y1):y2 + 1, max(0, x1):x2 + 1] > 0)
    return float(sub.mean())


def neighbour_mask(shape, annotations, names: dict):
    """Рамки соседних узлов — то, что имеет право быть залитым в `node_mask`."""
    height, width = shape[:2]
    mask = np.zeros((height, width), dtype=np.uint8)
    for ann in annotations:
        if names.get(ann.get("category_id"), "") in NOT_A_NODE:
            continue
        bbox = ann.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x, y, w, h = (int(v) for v in bbox)   # COCO: [x, y, ширина, высота]
        x1 = max(0, x - NEIGHBOR_PAD_PX)
        y1 = max(0, y - NEIGHBOR_PAD_PX)
        mask[y1:y + h + NEIGHBOR_PAD_PX + 1, x1:x + w + NEIGHBOR_PAD_PX + 1] = 255
    return mask


def classify_orphan(box, axis: str, pipe_mask, skeleton) -> dict:
    """Куда девать бокс без рёбер: чинить однозначно или отдать оператору."""
    axis_sides = AXIS_SIDES.get(axis, AXIS_SIDES["h"])
    perp_sides = AXIS_SIDES["v" if axis == "h" else "h"]
    pipe_gap = {s: gap_to_mask(pipe_mask, box, s) for s in "LRTB"}
    skel_gap = {s: gap_to_mask(skeleton, box, s) for s in "LRTB"} if skeleton is not None \
        else {s: None for s in "LRTB"}

    def touches(gaps, side, limit):
        return gaps[side] is not None and gaps[side] <= limit

    axial = [s for s in axis_sides if touches(pipe_gap, s, PIPE_TOUCH_PX)]
    perp = [s for s in perp_sides if touches(pipe_gap, s, PIPE_TOUCH_PX)]
    if not axial:
        bucket = BUCKET_PERP_ONLY if perp else BUCKET_NO_PIPE
    elif not all(touches(skel_gap, s, SKELETON_REACH_PX) for s in axial):
        bucket = BUCKET_SKELETON
    else:
        bucket = BUCKET_AXIS_BOTH if len(axial) == 2 else BUCKET_AXIS_ONE
    return {"bucket": bucket, "axial_sides": axial, "perp_sides": perp,
            "pipe_gap": pipe_gap, "skeleton_gap": skel_gap}


# --------------------------------------------------------------------------
# Чтение артефактов одной диаграммы
# --------------------------------------------------------------------------

def _read_mask(path: Path):
    """Маска в оттенках серого. Pillow, а не cv2: имя `cv2` в наборе тестов
    подменяется заглушкой на уровне модуля (долг 0.3x), и стенд на нём молча
    считал бы пустоту. Тот же приём в `tools/pair_bench.py:245`."""
    if not path.exists():
        return None
    from PIL import Image  # локальный импорт: Pillow нужен только здесь

    Image.MAX_IMAGE_PIXELS = None   # чертежи крупнее дефолтного лимита
    with Image.open(path) as img:
        return np.array(img.convert("L"))


def _napravlenie_annotations(coco: dict) -> list:
    names = {c["id"]: str(c.get("name", "")).lower() for c in coco.get("categories", [])}
    return [a for a in coco.get("annotations", [])
            if names.get(a.get("category_id")) == NAPRAVLENIE]


def audit_diagram(diagram: Path) -> Optional[dict]:
    """Числа по одной диаграмме; `None` — нечего мерить (нет графа)."""
    graph_path = diagram / "graph" / "graph.json"
    if not graph_path.exists():
        return None
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    nodes = graph.get("nodes", [])
    links = graph.get("links", graph.get("edges", []))

    incidence: dict[str, int] = {}
    for edge in links:
        for key in ("source", "target"):
            nid = edge.get(key)
            if nid is not None:
                incidence[nid] = incidence.get(nid, 0) + 1

    dir_nodes = [n for n in nodes if n.get("direction_node")
                 or n.get("class_id") == NAPRAVLENIE_CLASS_ID]
    stale = sum(1 for n in nodes if n.get("degree", 0) != incidence.get(n["id"], 0))

    coco_path = diagram / "detection" / "coco_validated.json"
    boxes = with_direction = 0
    coco: dict = {}
    if coco_path.exists():
        coco = json.loads(coco_path.read_text(encoding="utf-8"))
        anns = _napravlenie_annotations(coco)
        boxes = len(anns)
        with_direction = sum(1 for a in anns
                             if (a.get("attributes") or {}).get("direction"))

    pipe_mask = _read_mask(diagram / "segmentation" / "pipe_mask.png")
    node_mask = _read_mask(diagram / "segmentation" / "node_mask.png")
    skeleton = _read_mask(diagram / "skeleton" / "skeleton_final.png")

    neighbours = None
    if node_mask is not None and coco:
        names = {c["id"]: str(c.get("name", "")).lower()
                 for c in coco.get("categories", [])}
        neighbours = neighbour_mask(node_mask.shape, coco.get("annotations", []), names)

    degrees: dict[int, int] = {}
    node_mask_hits = 0
    node_mask_overlap = 0
    pipe_empty = 0
    orphans = []
    for node in dir_nodes:
        deg = incidence.get(node["id"], 0)
        degrees[deg] = degrees.get(deg, 0) + 1
        bbox = node.get("bbox")
        if not bbox:
            continue
        box = [int(v) for v in bbox]
        if node_mask is not None:
            if fill_ratio(node_mask, box) > 0:
                node_mask_overlap += 1
            if fill_ratio(node_mask, box, neighbours) > NODE_MASK_TOL:
                node_mask_hits += 1
        if pipe_mask is not None and fill_ratio(pipe_mask, box) == 0:
            pipe_empty += 1
        if deg == 0 and pipe_mask is not None:
            item = classify_orphan(box, node.get("flow_axis", "h"), pipe_mask, skeleton)
            item.update(node_id=node["id"], box=box, flow_axis=node.get("flow_axis"),
                        flow_direction=node.get("flow_direction"),
                        degree_field=node.get("degree"))
            orphans.append(item)

    return {
        "uid8": diagram.name[:8],
        "boxes": boxes,
        "with_direction": with_direction,
        "dir_nodes": len(dir_nodes),
        "degrees": degrees,
        "stale_degree_nodes": stale,
        "editor_saved": "_editor_commit" in graph.get("graph", {}),
        "node_mask_hits": node_mask_hits,
        "node_mask_overlap": node_mask_overlap,
        "pipe_empty": pipe_empty,
        "edges_with_direction": sum(1 for e in links if e.get("direction")),
        "orphans": orphans,
    }


def default_storage() -> Path:
    """Каталог диаграмм: как у воркера (`STORAGE_PATH`), иначе storage репо."""
    env = os.getenv("STORAGE_PATH")
    return Path(env) if env else REPO / "storage" / "diagrams"


def collect(storage: Path, uid: Optional[str] = None) -> list:
    if not storage.is_dir():
        return []
    rows = []
    for diagram in sorted(p for p in storage.iterdir() if p.is_dir()):
        if uid and not diagram.name.startswith(uid):
            continue
        row = audit_diagram(diagram)
        if row is not None:
            rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Печать
# --------------------------------------------------------------------------

def print_report(rows: list, show_orphans: bool = False) -> None:
    print(f"{'uid':9} {'боксы':>6} {'+напр':>6} {'узлы':>5} {'d0':>4} {'d1':>4} "
          f"{'d>=2':>5} {'node_mask':>10} {'протух degree':>14}")
    for row in rows:
        deg = row["degrees"]
        print(f"{row['uid8']:9} {row['boxes']:6} {row['with_direction']:6} "
              f"{row['dir_nodes']:5} {deg.get(0, 0):4} {deg.get(1, 0):4} "
              f"{sum(v for k, v in deg.items() if k >= 2):5} {row['node_mask_hits']:10} "
              f"{row['stale_degree_nodes']:14}"
              f"{'  ← редактор' if row['editor_saved'] else ''}")

    boxes = sum(r["boxes"] for r in rows)
    with_dir = sum(r["with_direction"] for r in rows)
    nodes = sum(r["dir_nodes"] for r in rows)
    orphans = [o for r in rows for o in r["orphans"]]
    by_field = sum(1 for r in rows for o in r["orphans"] if o["degree_field"] == 0)
    print(f"\nИТОГО: боксов {boxes}, с направлением {with_dir}, узлов графа {nodes}")
    print(f"степени по факту рёбер: " + ", ".join(
        f"{k}: {sum(r['degrees'].get(k, 0) for r in rows)}"
        for k in sorted({k for r in rows for k in r['degrees']})))
    print(f"боксов в node_mask своей площадью (должно быть 0): "
          f"{sum(r['node_mask_hits'] for r in rows)}"
          f"; накрыты рамкой соседа: {sum(r['node_mask_overlap'] for r in rows)}")
    print(f"боксов без трубы на pipe_mask: {sum(r['pipe_empty'] for r in rows)}")
    print(f"рёбер с полем direction: {sum(r['edges_with_direction'] for r in rows)} "
          f"(норма: 0, граф ненаправленный)")

    print(f"\nсирот (нет ни одного ребра): {len(orphans)}; "
          f"из них поле degree тоже 0: {by_field}")
    for bucket in BUCKETS:
        count = sum(1 for o in orphans if o["bucket"] == bucket)
        if count:
            print(f"  {bucket:38} {count}")
    good = sum(1 for o in orphans if o["bucket"] in RESTORABLE)
    print(f"  ИТОГ: восстановимо однозначно {good}, оператору {len(orphans) - good}")

    if show_orphans:
        print("\nсироты поштучно (зазор до трубы / до скелета, px):")
        for row in rows:
            for orphan in row["orphans"]:
                gaps = " ".join(f"{s}={orphan['pipe_gap'][s]}/{orphan['skeleton_gap'][s]}"
                                for s in "LRTB")
                print(f"  {row['uid8']} {orphan['node_id']:>10} "
                      f"ось={orphan['flow_axis']} {str(orphan['flow_direction']):>5} "
                      f"{gaps}  {orphan['bucket']}")


def verdict(rows: list) -> tuple:
    """Критерии §5 чек-листа, которые обязаны держаться на ЛЮБОМ прогоне.

    Это ровно два: у каждого бокса есть направление и ни один бокс не попал
    в `node_mask`. Отсутствие трубы под боксом сюда НЕ входит: это качество
    модели сегментации на конкретном листе, а не дефект кода — такие случаи
    по решению Максима уходят оператору и считаются в отчёте.
    """
    lines, bad = [], 0
    for row in rows:
        if row["boxes"] and row["with_direction"] < row["boxes"]:
            lines.append(f"ПРОВАЛ  {row['uid8']}: направление проставлено "
                         f"{row['with_direction']} из {row['boxes']} боксов")
            bad += 1
        if row["node_mask_hits"]:
            lines.append(f"ПРОВАЛ  {row['uid8']}: {row['node_mask_hits']} боксов "
                         f"napravlenie попали в node_mask — труба под ними вырезается")
            bad += 1
    lines.append(f"\nнарушений критериев приёмки: {bad}")
    return (1 if bad else 0), lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="ВН1б: приёмка napravlenie и разбор боксов без рёбер")
    parser.add_argument("--storage", type=Path, default=None,
                        help="каталог storage/diagrams (по умолчанию STORAGE_PATH или репо)")
    parser.add_argument("--uid", default=None, help="только эта диаграмма (префикс uid)")
    parser.add_argument("--orphans", action="store_true", help="печатать сирот поштучно")
    parser.add_argument("--json", type=Path, default=None, help="выгрузить числа в json")
    parser.add_argument("--check", action="store_true",
                        help="вердикт по критериям приёмки, exit 1 при нарушении")
    args = parser.parse_args(argv)

    storage = args.storage or default_storage()
    rows = collect(storage, args.uid)
    print(f"корпус: {storage} — диаграмм с графом {len(rows)}")
    if rows:
        print_report(rows, args.orphans)
    if args.json:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"\nчисла -> {args.json}")
    if args.check:
        if not rows:
            print("\nсудить нечем: диаграмм с графом нет")
            return 2
        code, lines = verdict(rows)
        print()
        for line in lines:
            print(line)
        return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

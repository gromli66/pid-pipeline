# -*- coding: utf-8 -*-
"""contours_merge.py — влив ручных SAM2-контуров в граф ДО построения холста.

Контуры оператор выбирает во вкладке «Контуры» (поле `polygon_validated` в
`contours/contours_validated.json`), но в сам граф они не записываются.
Раньше их подклеивал только экспорт FXML (`task_generate_fxml`) — в пикселях
растра. Для холста 1920x1080 та склейка мертва: bbox графа уже в холсте,
bbox контуров в растре, IoU нулевой, и полигоны молча терялись (аудит
2026-08-03). Поэтому влив происходит здесь — в координатах РАСТРА, до
`transform_to_canvas`: дальше полигон масштабируется вместе со всем графом,
оператор видит его в «Ручной правке», а 1:1-экспорт печатает как есть.

Правила — те же, что были в старом вливе (решения не пересматривались):
  * вливаются только `polygon_validated` (выбранные оператором); polygon_auto
    не применяется — иначе SAM2-контур лёг бы и на невыбранные узлы;
  * сопоставление по IoU bbox > 0.5. Прямая связь в данных есть
    (node.ann_idx ↔ contour.ann_id), но старый влив её никогда не использовал
    и заполнена она не у всех узлов — сохраняем IoU как единственный канал;
    bbox графа [x1, y1, x2, y2], bbox контура COCO [x, y, w, h];
  * только equipment-узлы.

Скиновым классам влив безвреден: `apply_fixed_sizes` контур снимает (у
словарного узла одна форма — словарная, решение заказчика 2026-07-28).

ИНВАРИАНТ СВЕЖЕСТИ: вливать можно только в КОПИЮ графа. Sha-проекция холста
(`canvas_state`, `_NODE_KEYS`) включает `segmentation`, а штамп обязан
считаться от `graph_validated`, каким он лежит в файле, — иначе холст навечно
«устареет» и правки оператора будут выбрасываться при каждом открытии.
Свежесть самих контуров меряется отдельной меткой `contours_merged_sha`
(по аналогии с `text_imported_sha`), канон — тот же `canvas_state._canon`:
две реализации канона дали бы расходящиеся sha (§3.6).

Здесь только числа, без Qt/shapely/numpy: модуль зовут воркер, диспетчер и
UI-фолбэк.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from .canvas_state import _canon, _dump

logger = logging.getLogger(__name__)

# Порог совпадения bbox узла и bbox контура — унаследован от старого влива.
IOU_THRESHOLD = 0.5


def load_validated_contours(path) -> list:
    """Узлы `contours_validated.json` с непустым `polygon_validated`.

    Пустой список — и когда файла нет (контуры не выбирались), и когда он
    не читается (влив пропускается, построение холста не падает).
    """
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("contours_merge: не читается %s: %s", p, exc)
        return []
    return [n for n in (data.get("nodes") or []) if n.get("polygon_validated")]


def _flat(poly) -> list:
    """Полигон плоским списком [x1, y1, ...] (COCO бывает вложенным).

    Multi-part в polygon_validated не встречается (формат — одиночный flat);
    защитная ветка берёт ПЕРВУЮ часть — склейка частей дала бы фантомное
    ребро между кольцами.
    """
    if poly and isinstance(poly[0], (list, tuple)):
        return [float(v) for v in poly[0]]
    return [float(v) for v in (poly or [])]


def merge_validated_contours(graph: dict, contour_nodes: list) -> int:
    """Вклеить `polygon_validated` в `node["segmentation"]` по IoU bbox.

    In-place, координаты растра. Возвращает число вливов.
    """
    if not contour_nodes:
        return 0

    merged = 0
    for node in graph.get("nodes", []):
        if node.get("type") != "equipment":
            continue
        nb = node.get("bbox")
        if not nb or len(nb) != 4:
            continue
        nx1, ny1, nx2, ny2 = nb

        best_iou = 0.0
        best_poly = None
        for cn in contour_nodes:
            cb = cn.get("bbox") or []
            if len(cb) != 4:
                continue
            cx, cy, cw, ch = cb
            cx2, cy2 = cx + cw, cy + ch

            ix1, iy1 = max(nx1, cx), max(ny1, cy)
            ix2, iy2 = min(nx2, cx2), min(ny2, cy2)
            inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
            area_n = max(0.0, nx2 - nx1) * max(0.0, ny2 - ny1)
            area_c = cw * ch
            union = area_n + area_c - inter
            iou = inter / union if union > 0 else 0.0

            if iou > best_iou:
                best_iou = iou
                best_poly = cn.get("polygon_validated")

        if best_iou > IOU_THRESHOLD and best_poly:
            node["segmentation"] = _flat(best_poly)
            merged += 1

    return merged


def contours_projection_sha(contour_nodes: list) -> str:
    """Хеш выбранных контуров: bbox + полигон, безразличный к порядку узлов.

    Проекция, а не sha файла: пере-сохранение вкладки без изменений не должно
    объявлять холст устаревшим (тот же довод, что у graph_projection_sha).
    """
    recs = sorted(
        _dump({"bbox": _canon(n.get("bbox")),
               "poly": _canon(_flat(n.get("polygon_validated") or []))})
        for n in contour_nodes or []
    )
    return hashlib.sha256(_dump(recs).encode("utf-8")).hexdigest()[:16]


def stamp_contours(canvas_graph: dict, contour_nodes: list) -> None:
    """Записать метку влитых контуров. Звать ПОСЛЕ `canvas_state.stamp`.

    Метка ставится и при пустом списке: «вливали ничего» отличимо от старого
    холста без метки (см. `contours_are_stale`).
    """
    tr = canvas_graph.setdefault("graph", {}).setdefault("canvas_transform", {})
    tr["contours_merged_sha"] = contours_projection_sha(contour_nodes)


def contours_are_stale(canvas_graph: dict, contour_nodes: list):
    """(устарели ли контуры холста, причина).

    Отдельно от геометрической свежести (`canvas_state.is_stale`): контуры —
    единственный вход холста, не покрытый sha-проекцией graph_validated.
    Холст без метки (собран до этого механизма) устаревает только когда
    выбранные контуры существуют — старым схемам без контуров пересборка
    не навязывается.
    """
    tr = ((canvas_graph.get("graph") or {}).get("canvas_transform") or {})
    mark = tr.get("contours_merged_sha")
    actual = contours_projection_sha(contour_nodes)
    if mark == actual:
        return False, None
    if mark is None:
        if not contour_nodes:
            return False, None
        return True, "холст собран до влива контуров, а контуры уже выбраны"
    return True, "контуры изменились после сборки холста"

# -*- coding: utf-8 -*-
"""seating.py — контрактная посадка концов рёбер (одна на всю систему).

Источник: стенд `_scratch/layout_align/harness/shared_geom.py` (строки 30-226).
Перенесено без изменения логики — см. docs/planning/AUTO_LAYOUT_INTEGRATION.md, Э2.

Правила из contract-разведки (FXML + WYSIWYG-редактор):
  * connector-конец: sp/tp ДОЛЖНЫ равняться centroid коннектора
    (FXML игнорирует source_point для connector-концов и берёт центроид).
  * узел из FIXED_SIZES (скин): конец на границе рамки bbox — скин тянется
    на рамку, как контрол FXML (решение 2026-08-01; сервер переведён с
    letterbox `_skin_content_rect` на bbox в блоке 2, Р2, 2026-09-28),
    поперечная координата в пределах рамки.
  * узел с сегментацией без скина: конец на контуре. Луч идёт ВДОЛЬ оси
    прямизны, если она задана и пересекает контур, иначе — из центроида
    (см. `_poly_hit`).
  * прочие (скин вне FIXED_SIZES, rectangle-fallback): конец на границе bbox.
  * waypoints — только промежуточные точки [y, x], ключ обязан существовать.

Вызывать reseat_all_endpoints(graph) ПОСЛЕ расстановки узлов. Прямизна: если
ребро без waypoints и концы почти на одной оси — конец сажается на общую ось.

МОДЕЛЬ ТОЧЕК ПРИКРЕПЛЕНИЯ (`model=True`, решение заказчика 2026-09-22) —
посадка холста: сборка (`pretransform.seat_edge_endpoints`), финальная посадка
раскладки, роутинг, открытие холста. Отличия от канона (`model=False`, по
умолчанию — внутренние стадии раскладки, растровые вкладки):
  * выверенный порт клапана (`contact.face_refs`) — ЖЁСТКАЯ точка общей оси,
    как центроид коннектора: сосед выравнивается на линию талии;
  * середина прочей рамки уступает ради прямой не дальше `contact.MID_SNAP`;
  * конец без замка оси садится в опорную точку грани (порт/середину), а не
    в зажатую координату соседа («куда-то»);
  * пин оператора (`ports.pinned_port`) — истина;
  * `reseat_all_endpoints(..., slots=True)` раскладывает несколько концов на
    одной грани слотами вокруг опорной точки (`seat_slots`);
  * `axial=True` (Р5, сервер — всегда): клапан степени 1–2
    (`contact.axial_only`) принимает трубы только на осевых гранях, в
    талию; какой конец в какую грань — план `axial_faces` (одна функция для
    сервера и редактора). Замок прямизны поперёк оси клапана у трубы без
    waypoints отбрасывается — ступеньку или колено за клапаном строит
    роутер. Степень 3+ и predohran (`contact.AXIAL_EXEMPT_SKINS`) — прежнее.
Полигоны и коннекторы — как в каноне.

Лежит рядом с pretransform, а не в пакете layout: посадка одна на всю систему
(§3.4 плана), а импорт подмодуля layout тянул бы shapely, которого в
requirements/ui.txt нет.
"""
from __future__ import annotations

from . import contact
from .pretransform import (
    FIXED_SIZES,
    _skin_content_rect,
    project_ray_to_polygon,
)
from .graph_access import (degrees, edge_ends, edges, is_connector,
                           node_cxy, nodes_by_id)

STRAIGHT_TOL = 3.0   # px: концы почти на оси -> строгая прямая
_SIDE_TOL = 1.5      # px: «конец лежит на грани рамки»


def _anchor_rect(node):
    """Прямоугольник посадки — рамка bbox у любого узла, скин тоже (Р2).

    Та же рамка, что у редактора (`edit_checks.seat_rect`) и у FXML: сторож
    и судья смотрят в одно. Letterbox `_skin_content_rect` остался только
    миграциям открытия старых холстов. tuple(bb), а не float: int-bbox
    растра не должен превращаться в float в JSON.

    Узел старого холста (`contact.LEGACY_KEY`, метки модели нет, Р1) из
    FIXED_SIZES — letterbox, как в 238fe6b: иначе старое открытие не
    бит-в-бит (зажим касательной и замок оси по разным рамкам)."""
    bb = node.get("bbox")
    if not bb or len(bb) != 4:
        return None
    if node.get(contact.LEGACY_KEY) and node.get("class_name") in FIXED_SIZES:
        return _skin_content_rect(node) or tuple(bb)
    return tuple(bb)


def _poly_hit(node, seg, toward_x, toward_y, lock):
    """Точка на контуре: луч ВДОЛЬ оси прямизны, иначе из центроида.

    Прежде луч всегда шёл из центроида в соседа, и ветка возвращалась РАНЬШЕ,
    чем проверялся `lock` (он работает только в прямоугольной ветке ниже).
    Для крупного контура это значит, что труба рисуется диагональю по
    построению: конец садится на прямую центроид->сосед, а центроид
    невыпуклого контура лежит далеко от места врезки. Замер c2f79462
    (деаэратор node_28, bbox 616x373, 13 рёбер): все 13 труб неортогональны
    на холсте отрисовки ДО всякой раскладки; с лучом вдоль оси — ноль.
    """
    cx, cy = node_cxy(node)
    if lock:
        xs, ys = seg[0::2], seg[1::2]
        if lock[0] == "H" and min(ys) <= lock[1] <= max(ys):
            pt = project_ray_to_polygon(seg, cx, lock[1], toward_x, lock[1])
            if pt:
                return pt
        elif lock[0] == "V" and min(xs) <= lock[1] <= max(xs):
            pt = project_ray_to_polygon(seg, lock[1], cy, lock[1], toward_y)
            if pt:
                return pt
    return project_ray_to_polygon(seg, cx, cy, toward_x, toward_y)


def _is_poly_node(node):
    """Узел сажается на контур (полигон без скина) — ветка `node_anchor`."""
    seg = node.get("segmentation")
    return (bool(seg) and isinstance(seg, list) and len(seg) >= 6
            and node.get("class_name") not in FIXED_SIZES
            and not node.get("_axis"))


def node_anchor(node, toward_x, toward_y, lock=None, model=False, cur=None,
                face=None):
    """Точка подключения на форме узла, обращённая к (toward_x, toward_y).

    lock: None | ('H', y) | ('V', x) — посадить на общую ось, если возможно.
    model: без замка рамочный конец садится в опорную точку грани
    (выверенный порт клапана или середину), а не в зажатую координату соседа.
    cur: (x, y) текущего конца — в модели без замка грань берётся та, на
    которой он уже сидит (её выбрали стадии раскладки по исходной схеме;
    выбор заново по направлению к соседу уводил вход на чужую сторону).
    face: 'L'|'R'|'T'|'B' — грань задана планом `axial_faces` (Р5): без
    замка конец садится на неё (модель — в опорную точку, канон — зажатая
    координата соседа на этой грани); замок, если он есть, главнее.
    Возвращает (x, y).
    """
    if is_connector(node):
        return node_cxy(node)

    seg = node.get("segmentation")
    has_poly = bool(seg) and isinstance(seg, list) and len(seg) >= 6
    rect = _anchor_rect(node)

    if rect is None:
        if has_poly:
            pt = _poly_hit(node, seg, toward_x, toward_y, lock)
            return pt if pt else node_cxy(node)
        return node_cxy(node)

    # полигонный узел без скина: FXML эмитит контур
    if has_poly and node.get("class_name") not in FIXED_SIZES \
            and not node.get("_axis"):
        pt = _poly_hit(node, seg, toward_x, toward_y, lock)
        if pt:
            return pt

    x1, y1, x2, y2 = rect
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0

    if lock and lock[0] == "H" and y1 - 0.5 <= lock[1] <= y2 + 0.5:
        y = min(max(lock[1], y1), y2)
        return (x2 if toward_x >= cx else x1, y)
    if lock and lock[0] == "V" and x1 - 0.5 <= lock[1] <= x2 + 0.5:
        x = min(max(lock[1], x1), x2)
        return (x, y2 if toward_y >= cy else y1)

    dx, dy = toward_x - cx, toward_y - cy
    if model:
        refs = contact.face_refs(node, rect) or {}
        by_dir = ("R" if dx > 0 else "L") if abs(dx) >= abs(dy) \
            else ("B" if dy > 0 else "T")
        on = _end_sides(rect, cur[0], cur[1]) if cur else ()
        if face:
            side = face
        elif len(on) == 1:
            side = on[0]
        elif len(on) == 2:
            # конец в углу — на двух гранях сразу: из них та, что смотрит
            # на соседа (иначе порядок L,R,T,B увёл бы вход «вбок»)
            hor = [s for s in on if s in ("L", "R")]
            ver = [s for s in on if s in ("T", "B")]
            side = hor[0] if abs(dx) >= abs(dy) and hor else \
                (ver[0] if ver else hor[0])
        else:
            side = by_dir
        if side == "R":
            return (x2, refs.get("R", cy))
        if side == "L":
            return (x1, refs.get("L", cy))
        if side == "B":
            return (refs.get("B", cx), y2)
        return (refs.get("T", cx), y1)
    if face in ("L", "R"):
        return (x2 if face == "R" else x1, min(max(toward_y, y1), y2))
    if face in ("T", "B"):
        return (min(max(toward_x, x1), x2), y2 if face == "B" else y1)
    if abs(dx) >= abs(dy):
        return (x2 if dx > 0 else x1, min(max(toward_y, y1), y2))
    return (min(max(toward_x, x1), x2), y2 if dy > 0 else y1)


def poly_station_port(node, side, frac):
    """Порт станции равномерки Э10 на контуре полигонного узла без скина
    (ПОПРАВКА контракта, решение заказчика 2026-07-21): пересечение
    ПЕРПЕНДИКУЛЯРНОГО стороне луча на доле frac с контуром."""
    seg = node.get("segmentation")
    bb = node.get("bbox")
    if not seg or not isinstance(seg, list) or len(seg) < 6 or not bb:
        return None
    far = 10000.0
    if side in ("L", "R"):
        y = bb[1] + frac * (bb[3] - bb[1])
        o, t = ((bb[0] - far, y), (bb[2] + far, y)) if side == "R" \
            else ((bb[2] + far, y), (bb[0] - far, y))
    else:
        x = bb[0] + frac * (bb[2] - bb[0])
        o, t = ((x, bb[1] - far), (x, bb[3] + far)) if side == "B" \
            else ((x, bb[3] + far), (x, bb[1] - far))
    pt = project_ray_to_polygon(seg, o[0], o[1], t[0], t[1])
    return (float(pt[0]), float(pt[1])) if pt else None


def _poly_even_seat(node, e, key, ref, tol=1.5):
    """Станция Э10 из атрибутов ребра (_poly_side_*/_poly_frac_*), если
    подход ref уже перпендикулярен станции; иначе None (старый осевой луч)."""
    if node is None or e is None:
        return None
    side = e.get("_poly_side_" + key)
    frac = e.get("_poly_frac_" + key)
    if side is None or frac is None:
        return None
    bb = node.get("bbox")
    if not bb:
        return None
    if side in ("L", "R"):
        st = bb[1] + frac * (bb[3] - bb[1])
        if abs(ref[1] - st) > tol:
            return None
    else:
        st = bb[0] + frac * (bb[2] - bb[0])
        if abs(ref[0] - st) > tol:
            return None
    return poly_station_port(node, side, frac)


def _axis_range(node, model=False, snap=None):
    """Диапазон, в котором может лежать общая ось через узел: (y1,y2,x1,x2).

    Канон: коннектор — точка (центроид), прочие — рамка посадки.
    model: выверенный порт клапана на осевой грани — точка (жёсткая);
    середина прочей рамки — ± snap (по умолчанию `contact.MID_SNAP`,
    inf — вся грань); полигон — рамка, как в каноне.
    """
    if is_connector(node):
        cx, cy = node_cxy(node)
        return (cy, cy, cx, cx)
    rect = _anchor_rect(node)
    if rect is None:
        cx, cy = node_cxy(node)
        return (cy, cy, cx, cx)
    x1, y1, x2, y2 = rect
    if not model or _is_poly_node(node):
        return (y1, y2, x1, x2)
    if snap is None:
        snap = contact.MID_SNAP
    refs = contact.face_refs(node, rect) or {}
    if "L" in refs:
        ylo = yhi = refs["L"]
    else:
        my = (y1 + y2) / 2.0
        ylo, yhi = max(y1, my - snap), min(y2, my + snap)
    if "T" in refs:
        xlo = xhi = refs["T"]
    else:
        mx = (x1 + x2) / 2.0
        xlo, xhi = max(x1, mx - snap), min(x2, mx + snap)
    return (ylo, yhi, xlo, xhi)


def _hard_val(node, axis, model):
    """Жёсткая координата общей оси через узел или None.

    Канон: только центроид коннектора. model: ещё выверенный порт клапана —
    на оси вдоль его трубы (H — порт граней L/R, V — граней T/B)."""
    if is_connector(node):
        cx, cy = node_cxy(node)
        return cy if axis == "H" else cx
    if not model:
        return None
    rect = _anchor_rect(node)
    refs = contact.face_refs(node, rect) if rect else None
    if not refs:
        return None
    return refs.get("L" if axis == "H" else "T")


def _pref_val(node, axis, model):
    """Опорная координата узла на оси: жёсткая, иначе центроид."""
    hard = _hard_val(node, axis, model)
    if hard is not None:
        return hard
    cx, cy = node_cxy(node)
    return cy if axis == "H" else cx


def reseat_edge(byid, e, straight_tol=STRAIGHT_TOL, model=False, snap=None,
                faces=None):
    """Посадить оба конца ребра `e` (мутирует source/target_point).

    faces: план граней `axial_faces` {(id(ребра), 'source'|'target'): грань}
    (Р5). У конца из плана грань задана; у ребра без waypoints замок
    прямизны поперёк оси клапана отбрасывается ДО выбора общей оси — иначе
    партнёр сел бы на невыполнимый замок и труба вышла бы косой. У ребра с
    waypoints замок стаба не трогается (маршрут — чьё-то решение).
    """
    src = byid.get(e.get("source") or e.get("from"))
    tgt = byid.get(e.get("target") or e.get("to"))
    if not src or not tgt:
        return
    wps = e.get("waypoints") or []
    if model and not wps and e.get("_slack"):
        # вынужденная прямая (`straighten_unrouted`): ступеньке, которой
        # требует модель, не нашлось места у роутера — повторная посадка
        # (открытие холста, пересадка) держит трубу прямой, а не косой
        snap = float("inf")
        if e.get("_slack") == 2:
            model = False                   # порт клапана уже уступил
    scx, scy = node_cxy(src)
    tcx, tcy = node_cxy(tgt)

    # опорные точки направления
    s_ref = (wps[0][1], wps[0][0]) if wps else (tcx, tcy)
    t_ref = (wps[-1][1], wps[-1][0]) if wps else (scx, scy)

    s_face = t_face = None
    if faces:
        s_face = faces.get((id(e), "source"))
        t_face = faces.get((id(e), "target"))

    s_lock = t_lock = None
    if not wps:
        sy1, sy2, sx1, sx2 = _axis_range(src, model, snap)
        ty1, ty2, tx1, tx2 = _axis_range(tgt, model, snap)
        # близость пары — по опорным координатам (в каноне это центроиды;
        # в модели у клапана — порт талии, иначе он стоит поодаль от своего
        # центра и пара с соседом на линии талии не считалась бы соосной)
        spy, tpy = (_pref_val(src, "H", True), _pref_val(tgt, "H", True)) \
            if model else (scy, tcy)
        spx, tpx = (_pref_val(src, "V", True), _pref_val(tgt, "V", True)) \
            if model else (scx, tcx)
        # H-прямая: диапазоны Y пересекаются (с допуском)
        ylo, yhi = max(sy1, ty1), min(sy2, ty2)
        xlo, xhi = max(sx1, tx1), min(sx2, tx2)
        h_lock = v_lock = None
        if ylo - yhi <= straight_tol and abs(spy - tpy) <= \
                (sy2 - sy1) / 2 + (ty2 - ty1) / 2 + straight_tol:
            y = _pick_axis_coord(src, tgt, ylo, yhi, axis="H", model=model)
            if y is not None:
                h_lock = ("H", y)
        if xlo - xhi <= straight_tol and abs(spx - tpx) <= \
                (sx2 - sx1) / 2 + (tx2 - tx1) / 2 + straight_tol:
            x = _pick_axis_coord(src, tgt, xlo, xhi, axis="V", model=model)
            if x is not None:
                v_lock = ("V", x)
        # Р5: замок поперёк оси клапана невыполним для осевой грани
        for f in (s_face, t_face):
            if f in ("L", "R"):
                v_lock = None
            elif f in ("T", "B"):
                h_lock = None
        # Э2: ось — от реальной геометрии связи, не от порядка веток.
        # Прежний код брал H всегда, когда H достижима; у коннектора
        # диапазон вырожден в точку, и когда его координата попадала в
        # окно рамки соседа, V-ветка была недостижима — конец садился на
        # боковую грань при вертикальной связи (паттерн «зазор форм 0.00,
        # нарисовано 10.50», §15 стенда). Правило сужено до корня бага:
        # геометрия решает только в парах С КОННЕКТОРОМ; у пары блоков
        # оба диапазона широкие, прежний H-приоритет там осмыслен, а его
        # смена двигает оси решателя по всему корпусу (замер: 610→22
        # против 610→16, дефекты +3 на 51b и a6d — не-Парето).
        if h_lock and v_lock and (is_connector(src) or is_connector(tgt)) \
                and abs(tcy - scy) > abs(tcx - scx):
            s_lock = t_lock = v_lock
        else:
            s_lock = t_lock = h_lock or v_lock
    else:
        # конец сажаем на ось первого/последнего сегмента, если он H/V
        sp = e.get("source_point")
        tp = e.get("target_point")
        if sp is not None:
            s_lock = _seg_lock((sp[1], sp[0]), s_ref)
        if tp is not None:
            t_lock = _seg_lock((tp[1], tp[0]), t_ref)

    sp2 = _poly_even_seat(src, e, "s", s_ref)
    tp2 = _poly_even_seat(tgt, e, "t", t_ref)
    s_cur = t_cur = None
    if model:
        sp0, tp0 = e.get("source_point"), e.get("target_point")
        s_cur = (float(sp0[1]), float(sp0[0])) if sp0 else None
        t_cur = (float(tp0[1]), float(tp0[0])) if tp0 else None
    sx, sy = sp2 if sp2 else node_anchor(src, s_ref[0], s_ref[1], s_lock,
                                         model=model, cur=s_cur, face=s_face)
    tx, ty = tp2 if tp2 else node_anchor(tgt, t_ref[0], t_ref[1], t_lock,
                                         model=model, cur=t_cur, face=t_face)
    if model:
        # ручной вход оператора — истина (модель «пин на ребре», Э5)
        from .ports import pinned_port
        pin = pinned_port(src, e)
        if pin is not None:
            sx, sy = pin[0], pin[1]
        pin = pinned_port(tgt, e)
        if pin is not None:
            tx, ty = pin[0], pin[1]
    e["source_point"] = [sy, sx]
    e["target_point"] = [ty, tx]
    if "waypoints" not in e or e["waypoints"] is None:
        e["waypoints"] = []


def _pick_axis_coord(src, tgt, lo, hi, axis, model=False):
    """Координата общей оси. Жёсткая точка (центроид коннектора; в модели —
    ещё выверенный порт клапана) не сдвигается: ось проходит через неё."""
    s_hard = _hard_val(src, axis, model)
    t_hard = _hard_val(tgt, axis, model)
    scx, scy = node_cxy(src)
    tcx, tcy = node_cxy(tgt)
    s_val = scy if axis == "H" else scx
    t_val = tcy if axis == "H" else tcx
    if s_hard is not None and t_hard is not None:
        return s_hard if abs(s_hard - t_hard) <= 0.75 else None
    if s_hard is not None:
        return s_hard if lo - 0.75 <= s_hard <= hi + 0.75 else None
    if t_hard is not None:
        return t_hard if lo - 0.75 <= t_hard <= hi + 0.75 else None
    if lo > hi:
        return None
    mid = (s_val + t_val) / 2.0
    return min(max(mid, lo), hi)


def _seg_lock(endpoint_xy, ref_xy, tol=1.0):
    ex, ey = endpoint_xy
    rx, ry = ref_xy
    if abs(ey - ry) <= tol:
        return ("H", ry)
    if abs(ex - rx) <= tol:
        return ("V", rx)
    return None


def straight_slack_lock(src, tgt, anchor_x, anchor_y, straight_tol=STRAIGHT_TOL):
    """Замок прямизны для посадки ОДНОГО конца при неподвижном втором
    (drag adjusting=End, Э3): условия слабины — те же, что в `reseat_edge`
    для рёбер без waypoints, но ось проходит через ЯКОРЬ (неподвижный
    дальний конец), а не через середину пары. Дальний конец не двигается,
    ближний садится на его ось: почти-соосная пара даёт строго прямую
    трубу с концом на грани (не лучом в угол). -> ('H', y)|('V', x)|None.

    `reseat_edge` сознательно НЕ переведён на этот хелпер: его ось — mid
    пары (обоюдная пересадка), и любая правка меняет бит-эталон корпуса.
    """
    scx, scy = node_cxy(src)
    tcx, tcy = node_cxy(tgt)
    sy1, sy2, sx1, sx2 = _axis_range(src)
    ty1, ty2, tx1, tx2 = _axis_range(tgt)
    ylo, yhi = max(sy1, ty1), min(sy2, ty2)
    xlo, xhi = max(sx1, tx1), min(sx2, tx2)
    # Ось обязана реально накрываться гранью сажаемого узла: иначе
    # node_anchor клампит координату в край диапазона — конец в УГЛУ и
    # чуть косая «прямая» (скрин заказчика 2026-07-31). За пределами
    # грани честнее луч/маршрут, чем угол.
    h = (ylo - yhi <= straight_tol and abs(scy - tcy) <=
         (sy2 - sy1) / 2 + (ty2 - ty1) / 2 + straight_tol
         and sy1 - 0.5 <= anchor_y <= sy2 + 0.5)
    v = (xlo - xhi <= straight_tol and abs(scx - tcx) <=
         (sx2 - sx1) / 2 + (tx2 - tx1) / 2 + straight_tol
         and sx1 - 0.5 <= anchor_x <= sx2 + 0.5)
    if h and v:
        # как в Э2: при двух достижимых осях — доминирующее направление
        if abs(tcx - scx) >= abs(tcy - scy):
            v = False
        else:
            h = False
    if h:
        return ("H", anchor_y)
    if v:
        return ("V", anchor_x)
    return None


def reseat_all_endpoints(graph, straight_tol=STRAIGHT_TOL, model=False,
                         slots=False, axial=False):
    """Посадить концы всех рёбер. axial — правило Р5 по плану граней
    `axial_plan` (считается один раз по текущим концам)."""
    byid = nodes_by_id(graph)
    faces = axial_plan(graph, byid) if axial else None
    for e in edges(graph):
        reseat_edge(byid, e, straight_tol, model=model, faces=faces)
    if slots:
        seat_slots(graph, byid)


# ─────────── осевые входы клапана степени 1–2 (Р5, 2026-09-29) ───────────

_AXIAL_FACES = {"H": ("L", "R"), "V": ("T", "B")}


def axial_faces(node, ends, degree):
    """Раскладка концов клапана по осевым граням (Р5): {(id(e), role): side}.

    ends — [(e, role, ref_xy, cur_xy|None, pinned)] концов у клапана; ref —
    смежный waypoint, иначе опорная точка партнёра; cur — текущий конец.
    Не `contact.axial_only(node, degree)` — {}. Пиннутые концы не участвуют
    (пин — истина), но занятая ими осевая грань отдаёт свободному концу
    вторую. Грани: H — L/R, V — T/B.
      * один конец: грань из cur, если она осевая; иначе осевая грань,
        смотрящая на ref вдоль оси;
      * два конца: оба cur на РАЗНЫХ осевых гранях — остаются (решение
        оператора); иначе по координате ref вдоль оси: меньший — L/T,
        больший — R/B (равенство — по id ребра). Это «колено за клапаном»:
        поперечная труба берёт свободную осевую талию;
      * два конца к ОДНОМУ партнёру (кратные рёбра старого холста) разных
        граней не требуют — каждый как одиночный.
    Одна функция для сервера (`axial_plan`) и редактора.
    """
    if not ends or not contact.axial_only(node, degree):
        return {}
    rect = _anchor_rect(node)
    if rect is None:
        return {}
    lo_f, hi_f = _AXIAL_FACES[node["_axis"]]
    horiz = node["_axis"] == "H"
    mid = (rect[0] + rect[2]) / 2.0 if horiz else (rect[1] + rect[3]) / 2.0

    def along(xy):
        return xy[0] if horiz else xy[1]

    def on_axial(cur):
        if cur is None:
            return None
        on = [s for s in _end_sides(rect, cur[0], cur[1]) if s in (lo_f, hi_f)]
        return on[0] if on else None

    def partner(e, role):
        s, t = edge_ends(e)
        return t if role == "source" else s

    free = [t for t in ends if not t[4]]
    out = {}
    if len(free) == 2 and partner(*free[0][:2]) != partner(*free[1][:2]):
        f0, f1 = on_axial(free[0][3]), on_axial(free[1][3])
        if f0 and f1 and f0 != f1:
            pick = (f0, f1)
        else:
            order = sorted((0, 1), key=lambda i: (along(free[i][2]),
                                                  str(free[i][0].get("id"))))
            pick = [None, None]
            pick[order[0]], pick[order[1]] = lo_f, hi_f
        for t, f in zip(free, pick):
            out[(id(t[0]), t[1])] = f
        return out
    for t in free:
        f = on_axial(t[3])
        taken = {on_axial(q[3]) for q in ends if q[4] and
                 partner(*q[:2]) != partner(*t[:2])} - {None} \
            if len(free) == 1 else set()
        if len(taken) == 1:
            f = hi_f if lo_f in taken else lo_f
        elif f is None:
            f = hi_f if along(t[2]) >= mid else lo_f
        out[(id(t[0]), t[1])] = f
    return out


def axial_plan(graph, byid=None):
    """План граней Р5 по всему графу: {(id(e), role): side} — `axial_faces`
    по текущим концам каждого клапана; степень — по рёбрам
    (`graph_access.degrees`)."""
    from .ports import pinned_port

    byid = byid or nodes_by_id(graph)
    deg = degrees(graph)
    ends = {}
    for e in edges(graph):
        s, t = edge_ends(e)
        wps = e.get("waypoints") or []
        for nid, role, key, other in ((s, "source", "source_point", t),
                                      (t, "target", "target_point", s)):
            node = byid.get(nid)
            if node is None or not contact.axial_only(node, deg.get(nid, 0)):
                continue
            part = byid.get(other)
            if wps:
                w = wps[0] if role == "source" else wps[-1]
                ref = (float(w[1]), float(w[0]))
            elif part is not None:
                ref = (_pref_val(part, "V", True), _pref_val(part, "H", True))
            else:
                continue
            p = e.get(key)
            cur = (float(p[1]), float(p[0])) if p else None
            ends.setdefault(nid, []).append(
                (e, role, ref, cur, pinned_port(node, e) is not None))
    faces = {}
    for nid, lst in ends.items():
        faces.update(axial_faces(byid[nid], lst, deg[nid]))
    return faces


# ─────────────── слоты вокруг опорной точки (модель, 2026-09-22) ───────────────

def _end_sides(rect, px, py, tol=_SIDE_TOL):
    """Все грани рамки, на которых лежит точка (в углу — две)."""
    x1, y1, x2, y2 = rect
    out = []
    if y1 - tol <= py <= y2 + tol:
        if abs(px - x1) <= tol:
            out.append("L")
        elif abs(px - x2) <= tol:
            out.append("R")
    if x1 - tol <= px <= x2 + tol:
        if abs(py - y1) <= tol:
            out.append("T")
        elif abs(py - y2) <= tol:
            out.append("B")
    return tuple(out)


def _end_side(rect, px, py, tol=_SIDE_TOL):
    """Грань рамки, на которой лежит точка: 'L'|'R'|'T'|'B'|None."""
    x1, y1, x2, y2 = rect
    if abs(px - x1) <= tol and y1 - tol <= py <= y2 + tol:
        return "L"
    if abs(px - x2) <= tol and y1 - tol <= py <= y2 + tol:
        return "R"
    if abs(py - y1) <= tol and x1 - tol <= px <= x2 + tol:
        return "T"
    if abs(py - y2) <= tol and x1 - tol <= px <= x2 + tol:
        return "B"
    return None


def _face_groups(graph, byid, only=None, rect_of=None):
    """(node_id, side) -> [(along, edge_id, edge, pkey, other_key), ...].

    Концы на гранях рамочных узлов: без коннекторов, полигонов и пинов.
    Ребро под легаси-заморозкой `_manual_route` не входит целиком (как в
    `edit_engine._slot_seat`): открытие холста переводит его концы в пины
    (`base_graph_tab._reseat_canvas_endpoints`, Э5b), а пин в слоты не
    входит — иначе он раздвигал бы соседей по грани раньше, чем стал пином.
    `only` — множество id узлов, чьи грани собирать (None — все);
    `rect_of(node)` — рамка граней (по умолчанию `_anchor_rect`, как на
    сервере; редактор передаёт свою).
    along — проекция ДАЛЬНЕГО ориентира ребра (смежный waypoint, иначе
    противоположный конец) на ось грани — порядок слотов, как у движка
    редактора (`edit_engine._slot_seat`): стабилен и без пересечений.

    Кратные рёбра одной пары узлов (в корпусе такие есть: стрелка
    направления и арматура, два параллельных стояка) — РАЗНЫЕ трубы и
    занимают разные слоты: правило заказчика «несколько линий в одну грань —
    на равном удалении от точки прикрепления» не различает, сколько у них
    общих концов."""
    from .ports import pinned_on_node

    rect_of = rect_of or _anchor_rect
    groups = {}
    for e in edges(graph):
        s, t = edge_ends(e)
        wps = e.get("waypoints") or []
        if e.get("_slack") and not wps:
            continue                      # вынужденная прямая — не в слоты
        if e.get("_manual_route"):
            continue                      # будущий пин — не в слоты
        for nid, pkey, other_key in ((s, "source_point", "target_point"),
                                     (t, "target_point", "source_point")):
            if only is not None and nid not in only:
                continue
            node = byid.get(nid)
            if node is None or is_connector(node) or _is_poly_node(node):
                continue
            if pinned_on_node(node, e) is not None:
                continue
            rect = rect_of(node)
            p = e.get(pkey)
            if rect is None or not p:
                continue
            side = _end_side(rect, float(p[1]), float(p[0]))
            if side is None:
                continue
            refp = (wps[0] if pkey == "source_point" else wps[-1]) if wps \
                else e.get(other_key)
            if not refp:
                continue
            along = float(refp[1]) if side in ("T", "B") else float(refp[0])
            groups.setdefault((nid, side), []).append(
                (along, str(e.get("id")), e, pkey, other_key))
    return groups


def slot_faces(graph, byid=None):
    """Грани, на которых сидят >= 2 концов: {(node_id, side), ...}."""
    byid = byid or nodes_by_id(graph)
    return {key for key, ends in _face_groups(graph, byid).items()
            if len(ends) >= 2}


def _follow_partner(byid, e, other_key, side, sx, sy, busy, rect_of=None):
    """Прямая труба без waypoints: конец уехал в слот — партнёр, который
    может уступить (середина рамки в пределах MID_SNAP), доезжает на ту же
    линию. Жёсткий партнёр (порт клапана, коннектор, пин), полигон и конец
    на грани со слотами не двигаются — ступеньку разводит роутер."""
    from .ports import pinned_on_node

    s, t = edge_ends(e)
    other_id = t if other_key == "target_point" else s
    other = byid.get(other_id)
    q = e.get(other_key)
    if other is None or not q or is_connector(other) or _is_poly_node(other):
        return
    if pinned_on_node(other, e) is not None:
        return
    rect = (rect_of or _anchor_rect)(other)
    if rect is None:
        return
    oside = _end_side(rect, float(q[1]), float(q[0]))
    if oside is None or (other_id, oside) in busy:
        return
    horiz_face = side in ("T", "B")          # слот двигает конец по x
    if horiz_face != (oside in ("T", "B")):
        return                               # партнёр на поперечной грани
    mid, hard = contact.face_ref(other, rect, oside)
    want = sx if horiz_face else sy
    x1, y1, x2, y2 = rect
    lo, hi = (x1, x2) if horiz_face else (y1, y2)
    if hard or abs(want - mid) > contact.MID_SNAP or not lo <= want <= hi:
        return
    if horiz_face:
        e[other_key] = [float(q[0]), float(want)]
    else:
        e[other_key] = [float(want), float(q[1])]


def seat_slots(graph, byid=None):
    """Несколько концов на одной грани рамочного узла — слоты: равный шаг,
    симметрично вокруг опорной точки (`ports.side_slots`: середина или
    выверенный порт клапана), шаг адаптивен к числу и толщине линий
    (`ports._ink_pitch`). Одна реализация с редактором. -> число концов,
    сдвинутых в слоты."""
    byid = byid or nodes_by_id(graph)
    groups = _face_groups(graph, byid)
    busy = {key for key, ends in groups.items() if len(ends) >= 2}
    moved = 0
    for (nid, side), ends in sorted(groups.items(),
                                    key=lambda kv: (str(kv[0][0]), kv[0][1])):
        if len(ends) < 2:
            continue
        node = byid[nid]
        moved += len(_seat_group(byid, node, side, ends, _anchor_rect(node),
                                 busy))
    return moved


def _seat_group(byid, node, side, ends, rect, busy, rect_of=None):
    """Разложить концы `ends` одной грани `side` рамки `rect` слотами
    (`ports.side_slots`); смежные колени и мягкие партнёры — следом.
    -> [(edge, pkey), ...] сдвинутых концов."""
    from . import ports

    k = len(ends)
    x1, y1, x2, y2 = rect
    length = (x2 - x1) if side in ("T", "B") else (y2 - y1)
    widths = [ports._ink_w(t[2]) for t in ends]
    slots = ports.side_slots(node, side, k, rect=rect,
                             pitch=ports._ink_pitch(widths, length, k))
    ends.sort(key=lambda t: (t[0], t[1]))
    moved = []
    for (_along, _eid, e, pkey, other_key), (sx, sy, _nx, _ny, _m) \
            in zip(ends, slots):
        old = e.get(pkey)
        new = [float(sy), float(sx)]
        if abs(old[0] - new[0]) <= 1e-9 and abs(old[1] - new[1]) <= 1e-9:
            continue
        e[pkey] = new
        moved.append((e, pkey))
        wps = e.get("waypoints") or []
        if not wps:
            _follow_partner(byid, e, other_key, side, sx, sy, busy, rect_of)
        else:
            _follow_knee(wps, pkey, side, old, new)
    return moved


def seat_face(graph, byid, nid, side, rect_of=None):
    """Слоты ОДНОЙ грани (nid, side) — та же раскладка, что `seat_slots`,
    но только этой грани (редактор: ребро добавлено — чужие грани и их
    маршруты не трогаются). `rect_of(node)` — рамка граней (по умолчанию
    `_anchor_rect`); ею же судятся мягкие партнёры. Меньше двух концов на
    грани — ничего. -> [(edge, pkey), ...] сдвинутых концов."""
    rect_of = rect_of or _anchor_rect
    ends = _face_groups(graph, byid, only={nid},
                        rect_of=rect_of).get((nid, side)) or []
    if len(ends) < 2:
        return []
    # партнёр на грани со слотами не уступает (`_follow_partner`): грани
    # собираются только у партнёров этой грани — не по всему графу
    partners = {edge_ends(t[2])[1 if t[3] == "source_point" else 0]
                for t in ends}
    busy = {key for key, grp in _face_groups(graph, byid, only=partners,
                                             rect_of=rect_of).items()
            if len(grp) >= 2}
    busy.add((nid, side))
    node = byid[nid]
    return _seat_group(byid, node, side, ends, rect_of(node), busy, rect_of)


def _follow_knee(wps, pkey, side, old, new):
    """Конец уехал вдоль грани в слот — смежное колено едет с ним, и стаб
    остаётся перпендикулярным грани (зеркало `edit_engine._shift_rect_stub`:
    только если стаб был перпендикулярен и сдвиг слотового масштаба)."""
    from .ports import SLOT_PITCH

    adj = wps[0] if pkey == "source_point" else wps[-1]
    along = 1 if side in ("T", "B") else 0       # индекс [y, x] вдоль грани
    d = float(new[along]) - float(old[along])
    if abs(d) > 2 * SLOT_PITCH \
            or abs(float(adj[along]) - float(old[along])) > 1.0:
        return
    adj[along] = float(adj[along]) + d


def straighten_unrouted(graph, straight_tol=STRAIGHT_TOL, stats=None,
                        axial=False):
    """Страховка после роутинга: труба без waypoints, чьи концы разошлись
    (посадка в опорные точки дала ступеньку, а роутер её не развёл),
    выпрямляется слабиной канона — середина рамки уступает на всю грань;
    выверенный порт, коннектор и пин по-прежнему жёсткие. Лучше прямая с
    концом не в середине, чем косая: конец, стоявший в слоте, может его
    покинуть (редкий случай «вторая труба на осевой грани клапана»), если
    не садится ближе 2 px к соседнему концу той же грани.

    Последний рубеж — только если иначе труба осталась бы КОСОЙ: пара
    жёстких точек (порт клапана и коннектор) разошлась, роутер ступеньку не
    построил — порт клапана уступает, труба ложится прямой каноном.
    Такие случаи считаются отдельно (`stats['ports_yielded']`).

    Выпрямленное ребро помечается `_slack` (1 — уступила середина, 2 — и
    порт клапана): повторная посадка по модели (открытие холста) держит его
    прямым, иначе снова развалила бы в косую. Ключ вне sha-проекции холста.

    axial (Р5): план граней `axial_plan` считается один раз и идёт в обе
    пробы — замок поперёк оси клапана степени 1–2 отбрасывается и в модели,
    и в каноне: страховка не уводит клапан вбок. Параллельный канон-замок
    по-прежнему может увести конец вдоль осевой грани мимо талии
    (ports_yielded). Косая, которую так не выпрямить, остаётся.
    -> число выпрямленных рёбер."""
    byid = nodes_by_id(graph)
    groups = _face_groups(graph, byid)
    faces = axial_plan(graph, byid) if axial else None
    fixed = yielded = 0
    for e in edges(graph):
        if e.get("waypoints"):
            continue
        sp, tp = e.get("source_point"), e.get("target_point")
        if not sp or not tp:
            continue
        if abs(sp[0] - tp[0]) <= 0.5 or abs(sp[1] - tp[1]) <= 0.5:
            continue                        # уже прямая (H или V)
        done = False
        for model in (True, False):
            probe = dict(e)
            probe["waypoints"] = []
            pf = {(id(probe), r): faces[(id(e), r)]
                  for r in ("source", "target")
                  if (id(e), r) in faces} if faces else None
            reseat_edge(byid, probe, straight_tol, model=model,
                        snap=float("inf"), faces=pf)
            nsp, ntp = probe["source_point"], probe["target_point"]
            if not (abs(nsp[0] - ntp[0]) <= 0.5
                    or abs(nsp[1] - ntp[1]) <= 0.5):
                continue
            if _crowds_face(byid, groups, e, (nsp, ntp)):
                continue
            e["source_point"], e["target_point"] = nsp, ntp
            e["_slack"] = 1 if model else 2
            fixed += 1
            yielded += 0 if model else 1
            done = True
            break
        if not done:
            continue
    if stats is not None:
        stats["ports_yielded"] = yielded
    return fixed


def _crowds_face(byid, groups, e, new_pts):
    """Новый конец сел ближе 2 px к другому концу той же грани?"""
    s, t = edge_ends(e)
    for nid, p in zip((s, t), new_pts):
        node = byid.get(nid)
        rect = _anchor_rect(node) if node is not None else None
        if rect is None:
            continue
        side = _end_side(rect, float(p[1]), float(p[0]))
        horiz = side in ("T", "B")
        for _a, _id, oe, pkey, _ok in groups.get((nid, side), ()):
            if oe is e:
                continue
            q = oe.get(pkey)
            if q and abs((float(q[1]) - float(p[1])) if horiz
                         else (float(q[0]) - float(p[0]))) < 2.0:
                return True
    return False

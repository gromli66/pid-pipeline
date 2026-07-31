# -*- coding: utf-8 -*-
"""avoid_router.py — этап роутинга: обход препятствий libavoid (Э7-b).

Слой раздвигания оставляет транзиты: труба прошивает чужой блок, потому что
единица его хода — узел, а не маршрут ребра (§7.1.2 плана; расстановкой это
структурно не лечится — увод узла даёт диагональ). Этот модуль даёт рёбрам
waypoints: ортогональный маршрут вокруг чужих форм считает vendored-биндинг
libavoid (`vendor/adaptagrams`, загрузчик `_avoid_binding`).

Контракт:
  * координаты УЗЛОВ не трогаются вообще — меняются только `waypoints`
    (и посадка концов routed-рёбер: пины — в ПОРТАХ узлов (`ports.py`,
    этап A, судья тот же, что у drag в редакторе), финальная пересадка —
    тем же каноном `seating.reseat_edge` по замку оси стаба);
  * рёбра с `_manual_route` или непустыми `waypoints` оператора — не роутятся;
  * waypoints — [[y, x], ...], ТОЛЬКО промежуточные точки (концевые точки в
    списке не дублируются — мина терминальной точки, T-A.5 плана);
  * итог судит trial-and-revert: сначала на каждом ребре (диагональ или новое
    прошивание = молчаливый fallback libavoid -> ребру оставляется прежняя
    геометрия), затем на всём прогоне гейтом «не хуже входа» (`apply_routing`).

Две ловушки разведки (обе покрыты tests/test_avoid_router.py):
  1. SWIG-GC: питоньи прокси владеют C++-объектами; пины/шейпы/коннекторы
     обязаны жить в python-ссылках до конца извлечения маршрутов, иначе gc
     молча удаляет их из роутера посреди транзакции (список `keep`).
  2. Молчаливый fallback: при недостижимом пине libavoid не падает, а рисует
     маршрут «как получится» сквозь препятствие (проверено живым тестом:
     утопленный конец даёт прямую с изломом СКВОЗЬ бокс). Детект — по
     прошиванию форм, не по числу изломов.

Чистый модуль: без Celery/БД, тестируется на синтетике.
"""
from __future__ import annotations

import logging
from copy import deepcopy

from . import _gate, spread
# ports лежит в core/, НЕ в layout/: его импортирует UI (port_model), а
# layout/__init__ тянет shapely+numpy, которых нет в requirements/ui.txt —
# та же ловушка, от которой canvas_state вынесен из пакета (см. его docstring).
from .. import ports
from ._avoid_binding import avoid_available, load  # noqa: F401 (re-export)
from .params import LayoutParams
from ..graph_access import edge_ends, edge_polyline, edges, is_connector, \
    nodes_by_id
from ..pretransform import FIXED_SIZES
from ..seating import _anchor_rect, reseat_edge

log = logging.getLogger(__name__)

SIDE_EPS = 1.5      # px: конец «на грани» формы — как SIDE_EPS в _gate
ORTHO_TOL = 1.5     # px: сегмент маршрута ортогонален — как _gate.STRAIGHT_TOL
PIN_TOL = 0.5       # px: маршрут обязан начинаться/кончаться в пине
PIERCE_SHRINK = 2.0  # px: усадка формы при детекте прошивания — как
                     # spread.box_on_magi_drawn (сторож мерит как судья)
PORT_SNAP = 8.0      # px: snap-порог гистерезиса `ports.choose_port` — как
                     # у drag в редакторе (сторож == судья, этап A)
STRAIGHT_EPS = 0.5   # px: канон посадил конец строгой прямой к другому
                     # концу — прямая неприкосновенна, порт не применяется


def _polygon_pts(node):
    """Точки полигонного препятствия [(x, y), ...] или None.

    Настоящий контур — только у полигонных узлов БЕЗ скина: те же ветки, что
    в `seating.node_anchor` (скин из FIXED_SIZES рисуется прямоугольником, и
    препятствие обязано совпадать с тем, что видит глаз).
    """
    seg = node.get("segmentation")
    if not (seg and isinstance(seg, list) and len(seg) >= 6):
        return None
    if node.get("class_name") in FIXED_SIZES or node.get("_axis"):
        return None
    return [(float(seg[i]), float(seg[i + 1])) for i in range(0, len(seg), 2)]


def _obstacle(node):
    """(pts, bbox) препятствия узла или None: контур либо прямоугольник bbox."""
    bb = node.get("bbox")
    if not bb or len(bb) != 4:
        return None
    poly = _polygon_pts(node)
    if poly:
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        return poly, (min(xs), min(ys), max(xs), max(ys))
    x1, y1, x2, y2 = (float(v) for v in bb)
    if x2 - x1 <= 0 or y2 - y1 <= 0:
        return None
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)], (x1, y1, x2, y2)


def _conn_dirs(ag, px, py, rect, eps=SIDE_EPS):
    """ConnDirFlags по грани ПОСАДКИ, на которой лежит пин (угол — обе).

    rect — прямоугольник канона посадки (`seating._anchor_rect`: у скина
    content-rect, он УЖЕ bbox-препятствия), не сам bbox: конец сажается на
    его грань, и выход трубы обязан идти от неё. Точка не на гранях
    (контур внутри bbox) — ConnDirAll: направление там диктует форма.
    """
    x1, y1, x2, y2 = rect
    dirs = 0
    if abs(px - x1) <= eps:
        dirs |= ag.ConnDirLeft
    if abs(px - x2) <= eps:
        dirs |= ag.ConnDirRight
    if abs(py - y1) <= eps:
        dirs |= ag.ConnDirUp
    if abs(py - y2) <= eps:
        dirs |= ag.ConnDirDown
    return dirs if dirs else ag.ConnDirAll


def _shape_geoms(graph, byid):
    """{node_id: shapely-форма с усадкой} — детектор прошивания.

    Геометрия та же, что уходит препятствием в роутер: контур у полигонных
    без скина, иначе bbox; усадка PIERCE_SHRINK, как у судьи
    `spread.box_on_magi_drawn`. Схлопнувшиеся от усадки формы выбрасываются —
    их не прошить.
    """
    from shapely.geometry import Polygon as ShpPolygon

    out = {}
    for n in graph.get("nodes", []):
        if is_connector(n):
            continue
        ob = _obstacle(n)
        if ob is None:
            continue
        try:
            shp = ShpPolygon(ob[0]).buffer(0).buffer(-PIERCE_SHRINK)
        except Exception:  # noqa: BLE001 — кривой контур не валит роутинг
            continue
        if not shp.is_empty:
            out[n["id"]] = shp
    return out


def _pierced(geoms, polyline, exclude):
    """Множество узлов, чью усаженную форму прошивает полилиния [(x, y),...]."""
    from shapely.geometry import LineString

    if len(polyline) < 2:
        return set()
    ls = LineString(polyline)
    return {nid for nid, shp in geoms.items()
            if nid not in exclude and ls.intersects(shp)}


def _simplify(pts, tol=1e-6):
    """Убрать коллинеарные и совпадающие точки ортогональной полилинии."""
    out = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - out[-1][0]) <= tol and abs(p[1] - out[-1][1]) <= tol:
            continue
        if len(out) >= 2:
            a, b = out[-2], out[-1]
            if (abs(a[0] - b[0]) <= tol and abs(b[0] - p[0]) <= tol) or \
                    (abs(a[1] - b[1]) <= tol and abs(b[1] - p[1]) <= tol):
                out[-1] = p
                continue
        out.append(p)
    return out


def _ortho(pts, tol=ORTHO_TOL):
    return all(min(abs(pts[i][0] - pts[i - 1][0]),
                   abs(pts[i][1] - pts[i - 1][1])) <= tol
               for i in range(1, len(pts)))


def _pin_port(node, cur_xy, ref_xy):
    """Точка пина конца на узле — порт (этап A), не канон-луч.

    Судья тот же, что у drag в редакторе: `ports.choose_port` с гистерезисом
    от канонической точки. Ручные порты оператора (node['_ports']) — в
    приоритете: если среди них есть не-изнаночный, выбирается лучший из них.

    Порт не имеет права увести конец на ЧУЖУЮ сторону bbox: судья
    side_changed (`_gate._side_set`) режет такой ход, и пер-рёберный сторож
    apply_routing откатил бы всё ребро вместе с обходом — сторож == судья,
    поэтому фильтр стоит уже на выборе пина. У крупного контура это ровно
    случай «порт на дальнем прямом участке» (node_28 c2f79462): вход трубы
    не переезжает на другой бок аппарата, конец остаётся канонным.
    """
    rx, ry = ref_xy
    manual = [p for p in ports.manual_ports(node)
              if not ports._backside(p, rx, ry)]
    if manual:
        best = min(manual, key=lambda p: (ports._bends(p, rx, ry),
                                          ports._l1(p, rx, ry)))
        cand = (best[0], best[1])
    else:
        cand = ports.choose_port(node, cur_xy, ref_xy, PORT_SNAP)
    bb = node.get("bbox")
    if bb and len(bb) == 4:
        if not (_gate._side_set(cur_xy[0], cur_xy[1], bb)
                & _gate._side_set(cand[0], cand[1], bb)):
            return cur_xy
    return cand


def route_graph(graph, params=None, ends_out=None):
    """Ортогональные маршруты рёбер вокруг чужих форм.

    -> {edge_id: [[y, x], ...]} — только рёбра, чей маршрут принят и
    отличается от прямой (пустой словарь = менять нечего). Координаты узлов
    и сами рёбра graph НЕ мутируются — применяет вызывающий
    (`apply_routing`).

    Пины — в ПОРТАХ узлов (этап A, `layout/ports.py`): канон-луч у пары
    «бокс -> крупный контур» даёт УГОЛ рамки (жалоба заказчика: edge_52
    graph_edited_33), портовый пин — центр грани/прямого участка, тем же
    судьёй, что drag в редакторе. Исключения: конец, посаженный каноном
    СТРОГОЙ прямой к другому концу (соосность/слабина, STRAIGHT_EPS), и
    коннектор (точечный ConnEnd в центроиде) — остаются как есть.
    Соосная пара, чей маршрут НЕ остался прямым (прямую не пропустили
    препятствия — прямизны всё равно нет), перероучивается вторым проходом
    уже с портовыми пинами: иначе конец так и стоит в углу канона.
    ConnDir — по грани рамки посадки.

    ends_out: если передан dict, туда кладутся НОВЫЕ концы принятых рёбер,
    ушедшие от канона в порт: {edge_id: ([y, x] source, [y, x] target)} —
    в том числе для рёбер, чей портовый маршрут остался прямым (в основном
    словаре их нет). Применяет вызывающий.
    """
    p = params or LayoutParams()
    byid = nodes_by_id(graph)

    routable = []
    for e in edges(graph):
        eid = e.get("id")
        # неприкосновенность: ручной маршрут и waypoints оператора
        if eid is None or e.get("_manual_route") or (e.get("waypoints") or []):
            continue
        if not e.get("source_point") or not e.get("target_point"):
            continue
        routable.append(e)
    if not routable:
        return {}

    geoms = _shape_geoms(graph, byid)
    acc = _route_pass(graph, byid, geoms, routable, p, force_ports=False)
    redo = [e for e in routable
            if acc.get(e.get("id")) is not None
            and acc[e.get("id")][2]                    # был строгий exempt
            and len(acc[e.get("id")][0]) > 2]          # но маршрут с изломами
    if redo:
        acc.update(_route_pass(graph, byid, geoms, redo, p, force_ports=True))

    out = {}
    for e in routable:
        eid = e.get("id")
        got = acc.get(eid)
        if got is None:
            continue
        pts, pin_xy, _exempt = got
        (spx, spy), (tpx, tpy) = pin_xy
        sp, tp = e["source_point"], e["target_point"]
        new_sp, new_tp = [float(spy), float(spx)], [float(tpy), float(tpx)]
        if ends_out is not None and (
                abs(new_sp[0] - sp[0]) > 1e-9 or abs(new_sp[1] - sp[1]) > 1e-9
                or abs(new_tp[0] - tp[0]) > 1e-9
                or abs(new_tp[1] - tp[1]) > 1e-9):
            ends_out[eid] = (new_sp, new_tp)           # концы — в порты
        mid = pts[1:-1]                                # без концевых (T-A.5)
        if not mid:
            continue                                   # прямая — менять нечего
        out[eid] = [[float(y), float(x)] for x, y in mid]
    return out


def _route_pass(graph, byid, geoms, edge_list, p, force_ports):
    """Один проход libavoid по рёбрам edge_list.

    -> {edge_id: (pts, pin_xy, straight_exempt)} — только ПРИНЯТЫЕ маршруты
    (пины/ортогональность/прошивание — те же сторожа, что и раньше);
    pts — [(x, y), ...] упрощённой полилинии ВМЕСТЕ с концами,
    pin_xy — ((x, y) source, (x, y) target) выбранных пинов,
    straight_exempt — хотя бы один блочный конец оставлен на каноне из-за
    строгой прямой пары (при force_ports=True всегда False).
    """
    ag = load()
    router = ag.Router(ag.OrthogonalRouting)
    router.setRoutingParameter(ag.shapeBufferDistance, float(p.route_buffer))
    router.setRoutingParameter(ag.idealNudgingDistance, float(p.route_nudge))

    # SWIG-GC (ловушка 1): все прокси живут здесь до конца извлечения
    keep = [router]
    shape_refs = {}     # node_id -> (ShapeRef, (bx1, by1), bbox)
    for i, n in enumerate(graph.get("nodes", [])):
        if is_connector(n):
            continue
        ob = _obstacle(n)
        if ob is None:
            continue
        pts, bb = ob
        poly = ag.Polygon(len(pts))
        for j, (x, y) in enumerate(pts):
            poly.setPoint(j, ag.Point(x, y))
        ref = ag.ShapeRef(router, poly, i + 1)
        keep += [poly, ref]
        shape_refs[n["id"]] = (ref, (bb[0], bb[1]), bb)

    conns = []          # (edge_id, edge, ConnRef, pin_xy, straight_exempt)
    pin_class = 100
    for e in edge_list:
        eid = e.get("id")
        sp, tp = e.get("source_point"), e.get("target_point")
        s, t = edge_ends(e)
        ends = []
        pin_xy = []
        exempt = False
        for nid, pt, other in ((s, sp, tp), (t, tp, sp)):
            node = byid.get(nid)
            if node is None:
                break
            x, y = float(pt[1]), float(pt[0])          # [y, x] -> (x, y)
            if is_connector(node) or nid not in shape_refs:
                pin_xy.append((x, y))
                ends.append(ag.ConnEnd(ag.Point(x, y), ag.ConnDirAll))
            else:
                # этап A: пин — в порт узла, кроме конца на строгой прямой
                # к другому концу (прямизна пары важнее порта)
                ox, oy = float(other[1]), float(other[0])
                straight = abs(x - ox) <= STRAIGHT_EPS \
                    or abs(y - oy) <= STRAIGHT_EPS
                if straight and not force_ports:
                    exempt = True
                else:
                    x, y = _pin_port(node, (x, y), (ox, oy))
                pin_xy.append((x, y))
                ref, (bx1, by1), bb = shape_refs[nid]
                pin_class += 1
                seat_rect = _anchor_rect(node) or bb
                pin = ag.ShapeConnectionPin(
                    ref, pin_class, x - bx1, y - by1, False, 0.0,
                    _conn_dirs(ag, x, y, seat_rect))
                keep.append(pin)
                ends.append(ag.ConnEnd(ref, pin_class))
        if len(ends) != 2:
            continue
        conn = ag.ConnRef(router, ends[0], ends[1])
        keep.append(conn)
        conns.append((eid, e, conn, pin_xy, exempt))

    if not conns:
        return {}
    router.processTransaction()

    out = {}
    for eid, e, conn, pin_xy, exempt in conns:
        r = conn.displayRoute()
        pts = [(r.ps[i].x, r.ps[i].y) for i in range(r.size())]
        if len(pts) < 2:
            continue
        pts = _simplify(pts)
        (spx, spy), (tpx, tpy) = pin_xy
        # маршрут обязан начинаться и кончаться в пинах (= выбранных портах)
        if abs(pts[0][0] - spx) > PIN_TOL or abs(pts[0][1] - spy) > PIN_TOL \
                or abs(pts[-1][0] - tpx) > PIN_TOL \
                or abs(pts[-1][1] - tpy) > PIN_TOL:
            log.debug("роутинг %s: маршрут ушёл от посаженного конца — пропуск",
                      eid)
            continue
        # ловушка 2: молчаливый fallback — диагональ или НОВОЕ прошивание
        if not _ortho(pts):
            log.debug("роутинг %s: неортогональный маршрут (fallback) — пропуск",
                      eid)
            continue
        exclude = set(edge_ends(e))
        new_hit = _pierced(geoms, pts, exclude)
        old_hit = _pierced(geoms, edge_polyline(e), exclude)
        if new_hit - old_hit:
            log.debug("роутинг %s: маршрут прошивает %s (недостижимый пин, "
                      "fallback) — пропуск", eid, sorted(new_hit - old_hit))
            continue
        out[eid] = (pts, pin_xy, exempt and not force_ports)
    del keep
    return out


# ───────────────────────── применение с гейтом ─────────────────────────

def _snapshot(e):
    return (deepcopy(e.get("source_point")), deepcopy(e.get("target_point")),
            deepcopy(e.get("waypoints")))


def _end_sides(byid, e):
    """{node_id: множество сторон} блочных концов ребра — судья `_gate`."""
    out = {}
    for nid, x, y in _gate._block_endpoints(e):
        n = byid.get(nid)
        if n is not None and not is_connector(n) and n.get("bbox"):
            out[nid] = _gate._side_set(x, y, n["bbox"])
    return out


def _restore(e, snap):
    e["source_point"], e["target_point"], e["waypoints"] = deepcopy(snap[0]), \
        deepcopy(snap[1]), deepcopy(snap[2])


def apply_routing(graph, orig, base_v16, params=None, legal=None):
    """Роутинг + посадка + гейт «не хуже входа»; хуже — полный откат.

    graph мутируется на месте. -> статистика прогона (dict), в т.ч.
    `reverted` и причина. Судья тот же, что у приёмки: `_gate.verify`
    (диагонали/прямизна/стороны/наложения/бокс-на-магистрали), плюс
    `spread.defects` не хуже входа и `spread.box_on_magi_drawn` — упасть
    или остаться (ради него роутинг и затевался).
    """
    p = params or LayoutParams()
    byid = nodes_by_id(graph)

    pre_gate = _gate.verify(graph, orig, base_v16, legal)
    pre_magi = len(spread.box_on_magi_drawn(graph, byid))
    pre_defects = len(spread.defects(graph, byid, p.floor))

    ends_new = {}
    routed = route_graph(graph, p, ends_new)
    stats = {"routed": len(set(routed) | set(ends_new)),
             "reverted": False, "reasons": []}
    if not routed and not ends_new:
        return stats

    saved = {}
    for e in edges(graph):
        eid = e.get("id")
        if eid not in routed and eid not in ends_new:
            continue
        saved[eid] = (e, _snapshot(e))
        pre_sides = _end_sides(byid, e)
        if eid in ends_new:
            # этап A: концы — в выбранные порты (пины роутинга)
            e["source_point"] = deepcopy(ends_new[eid][0])
            e["target_point"] = deepcopy(ends_new[eid][1])
        e["waypoints"] = deepcopy(routed.get(eid, []))
        # пере-посадка концов по осям подводящих сегментов — тем же каноном:
        # порт задаёт координату вдоль грани (замок оси стаба), стаб к нему
        # перпендикулярен (ConnDir пина) — канон воспроизводит порт. У
        # прямого портового маршрута (без waypoints) концы уже в портах,
        # пересаживать не по чему — прямизну судит _ortho ниже.
        if e["waypoints"]:
            reseat_edge(byid, e)
        # посадка не имеет права ни скосить подводящий сегмент, ни увести
        # конец на другую грань (пин с ConnDirAll может выйти не той
        # стороной — судья `_side_changed` такое режет, откатываем адресно)
        post_sides = _end_sides(byid, e)
        side_ok = all(post_sides.get(nid, s) & s for nid, s in
                      pre_sides.items())
        if not _ortho(edge_polyline(e)) or not side_ok:
            _restore(e, saved.pop(eid)[1])
            stats["routed"] -= 1

    post_gate = _gate.verify(graph, orig, base_v16, legal)
    post_magi = len(spread.box_on_magi_drawn(graph, byid))
    post_defects = len(spread.defects(graph, byid, p.floor))

    reasons = [k for k in ("new_diagonals", "straight_broken", "side_changed",
                           "overlaps", "box_on_magistral", "order_broken")
               if post_gate[k] > pre_gate[k]]
    if post_gate["connectivity_changed"] and not pre_gate["connectivity_changed"]:
        reasons.append("connectivity")
    if post_defects > pre_defects:
        reasons.append("defects")
    if post_magi > pre_magi:
        reasons.append("box_on_magi_drawn")

    if reasons:
        for e, snap in saved.values():
            _restore(e, snap)
        log.warning("роутинг откатен целиком: хуже входа по %s "
                    "(magi %d -> %d, дефекты %d -> %d)",
                    ", ".join(reasons), pre_magi, post_magi,
                    pre_defects, post_defects)
        stats.update(reverted=True, reasons=reasons)
        return stats

    stats.update(magi_before=pre_magi, magi_after=post_magi)
    log.info("роутинг: %d рёбер получили обход, бокс-на-магистрали %d -> %d",
             stats["routed"], pre_magi, post_magi)
    return stats

# -*- coding: utf-8 -*-
"""edit_avoid.py — Этап B: оконная libavoid-сессия для drag редактора.

Модуль живёт ВНЕ layout/ (как ports.py): layout/__init__ тянет
shapely+numpy, которых нет в requirements/ui.txt. Биндинг подгружается
лениво через layout._avoid_binding; ЛЮБОЙ сбой импорта/сборки = сессии
нет, редактор остаётся на самописной лестнице (_route_orthogonal) —
запасной путь Этапа B по заданию.

Контракт сессии (задание Этапа B, согласовано 2026-08-01):
  * Router-сессия на ЖЕСТ: окно фигур вокруг таскаемых узлов
    (<= MAX_SHAPES ближайших), кадр = moveShape + processTransaction;
  * live-рёбра (routable-инцидентные жеста) перекладываются libavoid с
    клиренсом shapeBufferDistance и нуджингом idealNudgingDistance —
    «вдоль/сквозь» исчезают из пространства поиска по построению;
  * пины live-концов — В ПОСАЖЕННЫХ КОНЦАХ редакторского контракта:
    edit_engine.seat_end кладёт конец в порт/слот/участок ДО кадра
    сессии (сторож == судья), сессия порт сама НЕ выбирает;
  * все прочие рёбра окна — ФИКСИРОВАННЫЕ маршруты (setFixedRoute):
    чужая труба байт-в-байт (решение заказчика 2026-08-01, третья
    итерация), _manual_route неприкосновенен; фиксы стоят в пространстве
    нуджинга как непроходимые каналы;
  * полигонные узлы — препятствия РЕАЛЬНЫМ контуром (edit_checks.
    poly_contour — та же форма, что у судьи), прочие — bbox.

Ловушки биндинга (сняты живым пробником, 2026-08-01):
  * SWIG-прокси владеют C++-объектами: всё созданное живёт в self._keep
    до конца жеста, иначе gc молча вынимает пины из роутера;
  * ShapeConnectionPin НЕЛЬЗЯ ни удалить (SWIG не сгенерил деструктор),
    ни подвинуть (updatePosition в биндинге нет): сместившийся слот =
    пин с НОВЫМ classId + conn.setEndpoints (два пина одного класса
    роутер считает альтернативами и берёт старый);
  * при недостижимом пине libavoid молча рисует «как получится»: детект —
    концы маршрута не в пинах / диагональ (флаги ends_ok/ortho в выдаче
    route_frame); прошивание/hug судит вызывающий тем же _fb_seg_bad.

_conn_dirs/_ortho/_simplify — копии одноимённых из layout/avoid_router.py
(импорт невозможен: avoid_router тянет _gate/spread => shapely). Менять
синхронно с оригиналом.
"""
from __future__ import annotations

import logging
import os

from .edit_checks import poly_contour
from .graph_access import is_connector

log = logging.getLogger(__name__)

CLEARANCE = 6.0     # px: клиренс маршрута — как ROUTE_CLEARANCE редактора
                    # и route_buffer сервера (params.py)
BUFFER_EPS = 0.25   # px: добавка к shapeBufferDistance. libavoid кладёт
                    # обходной сегмент ровно на грань+буфер; при буфере ==
                    # порогу судьи (strict gap < 6.0) координаты в 6px-полосах
                    # ниже степеней двойки дают gap 5.9999999999999858 —
                    # судья браковал бы собственный маршрут сессии (float,
                    # замер скептика ревью). Добавка выводит из полосы.
NUDGE = 8.0         # px: idealNudgingDistance — как route_nudge сервера
MAX_SHAPES = 225    # фигур в окне: замер Э7-b — 100-225 фигур = 28-123мс
                    # на кадр; полный лист (900) = 2.4с, нельзя
WINDOW_PAD = 64.0   # px: запас прямоугольника окна вокруг фигур
REBUILD_DIST = 256.0  # px: таскаемый узел уехал от центра сборки капнутого
                      # окна — сессию пересобрать (фигуры за кромкой)
PIN_BUDGET = 400    # пинов на сессию: в биндинге пин нельзя ни удалить, ни
                    # подвинуть — скользящая посадка (замок оси у контуров)
                    # рождает пин на кадр, транзакция дорожает без плато
                    # (замер: 900 кадров = 902 пина, кадр 1.8 -> 7.3 мс).
                    # Перебор бюджета = пересборка сессии (needs_rebuild).
SIDE_EPS = 1.5      # px: конец «на грани» рамки — как в avoid_router
ORTHO_TOL = 1.5     # px: сегмент маршрута ортогонален — как в avoid_router
PIN_TOL = 0.5       # px: маршрут обязан начинаться/кончаться в пине
_EPS = 1e-6


def enabled() -> bool:
    """Рычаг A/B для стресс-замеров (лестница vs сессия): PID_EDIT_AVOID=0
    выключает сессию, редактор работает по-старому."""
    return os.environ.get("PID_EDIT_AVOID", "1") != "0"


def load_binding():
    """SWIG-модуль adaptagrams или None (лестница — запасной путь).

    _avoid_binding загружается ПО ПУТИ ФАЙЛА, минуя импорт пакета layout/:
    `from .layout import _avoid_binding` исполнил бы layout/__init__, а тот
    безусловно тянет _shapes -> shapely, которого нет в requirements/ui.txt
    (та же ловушка, из-за которой ports.py живёт вне layout/) — на боевом
    клиенте Этап B молча умирал бы. Сам _avoid_binding — чистый stdlib.

    Широкий except осознанно: любой отказ (нет vendored-бинаря в бандле,
    чужой питон) — штатная деградация в лестницу; однократный лог, чтобы
    отказ не был молчаливым."""
    global _BINDING, _LOGGED_UNAVAILABLE
    if _BINDING is not None:
        return _BINDING
    try:
        import importlib.util
        from pathlib import Path
        p = Path(__file__).resolve().parent / "layout" / "_avoid_binding.py"
        spec = importlib.util.spec_from_file_location(
            "modules.graph.core.layout._avoid_binding_edit", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _BINDING = mod.load()
        return _BINDING
    except Exception as exc:  # noqa: BLE001 — деградация в лестницу
        if not _LOGGED_UNAVAILABLE:
            _LOGGED_UNAVAILABLE = True
            log.info("edit_avoid: биндинг libavoid недоступен (%s) — "
                     "drag остаётся на самописной лестнице", exc)
        return None


_BINDING = None
_LOGGED_UNAVAILABLE = False


class StaleModelError(RuntimeError):
    """Модель пересобрана под живым жестом (undo/redo/delete снапшотом):
    словари рёбер сессии отвязались от данных — сессию закрыть, жест
    доезжает на лестнице. Штатная ситуация, не сбой."""


def _obstacle(node):
    """(pts[(x,y),...], bbox(x1,y1,x2,y2)) препятствия узла или None.

    Реальный контур у полигонных без скина (форма судьи edit_checks),
    прочим — прямоугольник bbox. Коннектор — не препятствие."""
    if is_connector(node):
        return None
    bb = node.get("bbox")
    if not bb or len(bb) != 4:
        return None
    poly = poly_contour(node)
    if poly:
        pts = [(float(x), float(y)) for x, y in poly]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return pts, (min(xs), min(ys), max(xs), max(ys))
    x1, y1, x2, y2 = (float(v) for v in bb)
    if x2 - x1 <= 0 or y2 - y1 <= 0:
        return None
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)], (x1, y1, x2, y2)


def _conn_dirs(ag, px, py, rect, eps=SIDE_EPS):
    """ConnDirFlags по грани рамки посадки (копия avoid_router._conn_dirs;
    rect у редактора = edit_checks.seat_rect == bbox). Точка не на гранях
    (пин на контуре внутри bbox) — ConnDirAll: направление диктует форма."""
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


def _simplify(pts, tol=1e-6):
    """Убрать коллинеарные и совпадающие точки ортогональной полилинии
    (копия avoid_router._simplify)."""
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
    """Все сегменты маршрута осевые (копия avoid_router._ortho)."""
    return all(min(abs(pts[i][0] - pts[i - 1][0]),
                   abs(pts[i][1] - pts[i - 1][1])) <= tol
               for i in range(1, len(pts)))


def _edge_pts(e):
    """Полный путь ребра в (x, y) или None без концов."""
    sp, tp = e.get("source_point"), e.get("target_point")
    if not sp or not tp:
        return None
    return ([(float(sp[1]), float(sp[0]))]
            + [(float(w[1]), float(w[0])) for w in e.get("waypoints") or []]
            + [(float(tp[1]), float(tp[0]))])


class AvoidDragSession:
    """Оконная Router-сессия одного drag-жеста.

    Жизненный цикл: build() в start_drag_node -> route_frame() на каждом
    кадре (после посадки концов движком) -> close() в end_drag_node.
    Все вызовы — UI-поток; исключения ловит вызывающий (сбой кадра =
    жест доезжает на лестнице)."""

    def __init__(self, ag, moving_ids, live_keys):
        self._ag = ag
        self._moving = set(moving_ids)
        self._live_keys = set(live_keys)
        self._keep = [None]  # [0] — Router, заполняется в build
        self._router = None
        self._shapes = {}      # nid -> (ShapeRef, (bx1, by1))
        self._pins = {}        # nid -> {(off_x, off_y): class_id} — реюз:
                               # пин нельзя удалить, но вернувшийся на то же
                               # смещение конец берёт СТАРЫЙ пин (зигзаг,
                               # гистерезис порта — без чурна)
        self._vhalf = {}       # nid -> (hw, hh) виртуального бокса коннектора
        self._live = {}        # key -> {'edge','conn','ends':[инфо конца x2]}
        self._fixed = {}       # key -> {'edge','conn','pts'} (следимые фиксы)
        self._edge_key = None
        self._pin_count = 0
        self._pin_class = 100
        self._capped = False
        self._center = (0.0, 0.0)  # центр окна на момент сборки

    # ── сборка ──────────────────────────────────────────────────────

    @classmethod
    def build(cls, nodes, edges_data, moving_ids, live_keys, edge_key,
              virtual_boxes=None):
        """Собрать сессию окна или вернуть None (нет биндинга/нечего вести).

        nodes: {nid: node}; edges_data: list[dict]; live_keys — ключи
        routable-рёбер жеста (edge_key(src, tgt)); edge_key — функция
        ключа модели. virtual_boxes: {nid: (x1,y1,x2,y2)} — виртуальные
        боксы узлов БЕЗ bbox (коннекторы): судья редактора (_fb_seg_bad)
        судит их этим боксом, значит и роутер обязан видеть их фигурами —
        иначе сессионный маршрут через стык труб систематически бракуется
        и жест молча вырождается в лестницу (находка ревью). Первая
        транзакция гоняется здесь же (прогрев)."""
        if not enabled() or not live_keys:
            return None
        ag = load_binding()
        if ag is None:
            return None
        ses = cls(ag, moving_ids, live_keys)
        ses._build(nodes, edges_data, edge_key, virtual_boxes or {})
        return ses

    def _node_shape_geom(self, nid, node):
        """(pts, bbox) фигуры узла: реальная форма либо виртуальный бокс
        коннектора (текущий центроид +- полуразмеры сборки)."""
        ob = _obstacle(node)
        if ob is not None:
            return ob
        half = self._vhalf.get(nid)
        c = node.get("centroid")
        if half is None or not c:
            return None
        cx, cy = float(c[1]), float(c[0])
        bb = (cx - half[0], cy - half[1], cx + half[0], cy + half[1])
        return ([(bb[0], bb[1]), (bb[2], bb[1]),
                 (bb[2], bb[3]), (bb[0], bb[3])], bb)

    def _build(self, nodes, edges_data, edge_key, virtual_boxes):
        ag = self._ag
        self._edge_key = edge_key
        router = ag.Router(ag.OrthogonalRouting)
        router.setRoutingParameter(ag.shapeBufferDistance,
                                   CLEARANCE + BUFFER_EPS)
        router.setRoutingParameter(ag.idealNudgingDistance, NUDGE)
        self._router = router
        self._keep[0] = router

        # полуразмеры виртуальных боксов — до сбора фигур
        for nid, bb in virtual_boxes.items():
            if bb and len(bb) == 4 and bb[2] > bb[0] and bb[3] > bb[1]:
                self._vhalf[nid] = ((bb[2] - bb[0]) / 2.0,
                                    (bb[3] - bb[1]) / 2.0)

        # окно фигур: все препятствия либо MAX_SHAPES ближайших к жесту
        entries = []
        for nid, node in nodes.items():
            ob = self._node_shape_geom(nid, node)
            if ob is not None:
                entries.append((nid, ob))
        cx, cy = self._gesture_center(nodes)
        self._center = (cx, cy)
        if len(entries) > MAX_SHAPES:
            self._capped = True
            must = set(self._moving)
            for e in edges_data:
                if edge_key(e.get("source"), e.get("target")) \
                        in self._live_keys:
                    must.add(e.get("source"))
                    must.add(e.get("target"))
            # must-узлы не режутся капом никогда (иначе таскаемый узел
            # выпал бы из окна препятствий — находка ревью)
            must_entries = [it for it in entries if it[0] in must]
            rest = [it for it in entries if it[0] not in must]
            rest.sort(key=lambda it: max(
                abs((it[1][1][0] + it[1][1][2]) / 2.0 - cx),
                abs((it[1][1][1] + it[1][1][3]) / 2.0 - cy)))
            entries = must_entries \
                + rest[:max(0, MAX_SHAPES - len(must_entries))]

        wx1 = wy1 = float("inf")
        wx2 = wy2 = float("-inf")
        for i, (nid, (pts, bb)) in enumerate(entries):
            poly = self._mkpoly(pts)
            ref = ag.ShapeRef(router, poly, i + 1)
            self._keep += [poly, ref]
            self._shapes[nid] = (ref, (bb[0], bb[1]))
            self._pins[nid] = {}
            wx1, wy1 = min(wx1, bb[0]), min(wy1, bb[1])
            wx2, wy2 = max(wx2, bb[2]), max(wy2, bb[3])

        # рёбра окна: live — ConnRef с пинами, прочие — фиксы.
        # Дедуп по ключу (last-wins — как индекс модели find_edge_data):
        # дубль-записи рождали бы фантомный ConnRef с несвежими концами.
        by_key = {}
        for e in edges_data:
            pts = _edge_pts(e)
            if pts is not None:
                by_key[edge_key(e.get("source"), e.get("target"))] = (e, pts)

        # вырожденное окно (ни одной фигуры) — прямоугольник из полилиний
        # live-рёбер, чтобы фиксы соседних труб не потерялись
        if not entries:
            for key in self._live_keys:
                rec = by_key.get(key)
                if not rec:
                    continue
                for x, y in rec[1]:
                    wx1, wy1 = min(wx1, x), min(wy1, y)
                    wx2, wy2 = max(wx2, x), max(wy2, y)
        if wx1 > wx2:
            wx1 = wy1 = 0.0
            wx2 = wy2 = 0.0
        window = (wx1 - WINDOW_PAD, wy1 - WINDOW_PAD,
                  wx2 + WINDOW_PAD, wy2 + WINDOW_PAD)

        for key, (e, pts) in by_key.items():
            if key in self._live_keys:
                self._add_live(key, e, nodes)
            elif self._pts_hit_rect(pts, window):
                self._add_fixed(key, e, pts)
        router.processTransaction()   # прогрев: первая транзакция — дорогая

    def _gesture_center(self, nodes):
        xs, ys = [], []
        for nid in self._moving:
            node = nodes.get(nid)
            if node and node.get("centroid"):
                ys.append(float(node["centroid"][0]))
                xs.append(float(node["centroid"][1]))
        if not xs:
            return 0.0, 0.0
        return sum(xs) / len(xs), sum(ys) / len(ys)

    @staticmethod
    def _pts_hit_rect(pts, rect):
        x1, y1, x2, y2 = rect
        bx1 = min(p[0] for p in pts)
        bx2 = max(p[0] for p in pts)
        by1 = min(p[1] for p in pts)
        by2 = max(p[1] for p in pts)
        return bx2 >= x1 and bx1 <= x2 and by2 >= y1 and by1 <= y2

    def _mkpoly(self, pts):
        ag = self._ag
        poly = ag.Polygon(len(pts))
        for i, (x, y) in enumerate(pts):
            poly.setPoint(i, ag.Point(float(x), float(y)))
        return poly

    def _end_info(self, e, role, nodes):
        """Инфо конца live-ребра: где пин и какого он рода.

        Пин шейпа — если узел в окне фигур (не коннектор); иначе точечный
        ConnEnd (едет через setEndpoints). Позиция — ПОСАЖЕННЫЙ конец из
        данных ребра ([y, x] -> (x, y)). Пин учитывается СМЕЩЕНИЕМ от
        origin формы: при moveShape пин едет с ней, и неизменное смещение
        не рождает новый пин (иначе — churn пинов каждый кадр: пины в
        этом биндинге не удаляются)."""
        nid = e.get("source") if role == "s" else e.get("target")
        p = e.get("source_point" if role == "s" else "target_point")
        x, y = float(p[1]), float(p[0])
        # коннектор с виртуальным боксом — тоже пин (центроид == центр
        # бокса, смещение стабильно): роутер исключает СВОЮ фигуру из
        # препятствий этого коннектора — маршрут доходит до стыка
        kind = "pin" if nid in self._shapes else "point"
        return {"nid": nid, "kind": kind, "x": x, "y": y,
                "class_id": None, "off": None}

    def _end_dirty(self, info):
        """Конец разъехался с тем, что стоит в роутере?"""
        if info["off"] is None:
            return True
        if info["kind"] == "point":
            return abs(info["off"][0] - info["x"]) > _EPS \
                or abs(info["off"][1] - info["y"]) > _EPS
        _ref, origin = self._shapes[info["nid"]]
        return abs(info["off"][0] - (info["x"] - origin[0])) > _EPS \
            or abs(info["off"][1] - (info["y"] - origin[1])) > _EPS

    def _make_end(self, info, nodes):
        """ConnEnd для инфо конца; для пина — создать/переиспользовать пин.

        Пины в биндинге вечные (нет деструктора) — кэш «смещение ->
        classId» на фигуру: конец, вернувшийся на прежнее смещение
        (зигзаг, гистерезис порта), берёт старый пин без чурна; новое
        смещение = новый classId (перебор бюджета лечит needs_rebuild)."""
        ag = self._ag
        if info["kind"] == "point":
            info["off"] = (info["x"], info["y"])
            return ag.ConnEnd(ag.Point(info["x"], info["y"]))
        ref, origin = self._shapes[info["nid"]]
        off = (info["x"] - origin[0], info["y"] - origin[1])
        if info["class_id"] is None or self._end_dirty(info):
            cache = self._pins[info["nid"]]
            ckey = (round(off[0], 4), round(off[1], 4))
            cached = cache.get(ckey)
            if cached is not None:
                info["class_id"] = cached
            else:
                self._pin_class += 1
                self._pin_count += 1
                info["class_id"] = self._pin_class
                node = nodes.get(info["nid"]) or {}
                rect = node.get("bbox")
                if not rect or len(rect) != 4:
                    half = self._vhalf.get(info["nid"], (0.0, 0.0))
                    rect = (info["x"] - half[0], info["y"] - half[1],
                            info["x"] + half[0], info["y"] + half[1])
                pin = ag.ShapeConnectionPin(
                    ref, info["class_id"], off[0], off[1],
                    False, 0.0, _conn_dirs(ag, info["x"], info["y"], rect))
                self._keep.append(pin)
                cache[ckey] = info["class_id"]
            info["off"] = off
        return ag.ConnEnd(ref, info["class_id"])

    def _add_live(self, key, e, nodes):
        ag = self._ag
        s = self._end_info(e, "s", nodes)
        t = self._end_info(e, "t", nodes)
        conn = ag.ConnRef(self._router, self._make_end(s, nodes),
                          self._make_end(t, nodes))
        self._keep.append(conn)
        self._live[key] = {"edge": e, "conn": conn, "ends": [s, t]}

    def _add_fixed(self, key, e, pts):
        ag = self._ag
        conn = ag.ConnRef(self._router,
                          ag.ConnEnd(ag.Point(*pts[0])),
                          ag.ConnEnd(ag.Point(*pts[-1])))
        # Polygon в setFixedRoute КОПИРУЕТСЯ на C++-стороне (проверено
        # пробником: освобождение до processTransaction безопасно, фикс
        # байт-в-байт) — в keep его не держим, иначе рост на кадр
        conn.setFixedRoute(self._mkpoly(pts))
        self._keep.append(conn)
        tracked = e.get("source") in self._moving \
            or e.get("target") in self._moving
        # неследимые фиксы байт-неподвижны по контракту — их не диффим
        if tracked:
            self._fixed[key] = {"edge": e, "conn": conn, "pts": pts}

    # ── кадр ────────────────────────────────────────────────────────

    def needs_rebuild(self, nodes) -> bool:
        """Сессию пора пересобрать: (а) капнутое окно и жест уехал от
        центра сборки (за кромкой фигуры, которых роутер не видит);
        (б) перебор бюджета пинов (вечные пины дорожат транзакцию)."""
        if self._pin_count > PIN_BUDGET:
            return True
        if not self._capped:
            return False
        cx, cy = self._gesture_center(nodes)
        return max(abs(cx - self._center[0]),
                   abs(cy - self._center[1])) > REBUILD_DIST

    def route_frame(self, nodes, edges_data):
        """Кадр жеста: синхронизировать геометрию, одна транзакция, выдать
        маршруты live-рёбер.

        Возвращает {key: {'pts': [(x, y), ...], 'ends_ok': bool,
        'ortho': bool}}; pts упрощены (без дублей/коллинеарных), концы
        включены. ends_ok=False / ortho=False = молчаливый fallback
        libavoid (недостижимый пин) — вызывающий отдаёт ребро лестнице.

        StaleModelError — модель пересобрана под жестом (undo/redo/delete
        снапшотом перепривязали словари): сессия недействительна."""
        ag = self._ag
        # 0. сторож отвязки: словари сессии обязаны быть ТЕМИ ЖЕ объектами,
        # что в модели (restore снапшота подменяет их на deepcopy — сессия
        # писала бы маршруты в мёртвые словари)
        fresh = {}
        for e in edges_data:
            fresh[self._edge_key(e.get("source"), e.get("target"))] = e
        for key, rec in list(self._live.items()) \
                + list(self._fixed.items()):
            if fresh.get(key) is not rec["edge"]:
                raise StaleModelError(f"ребро {key} отвязано от модели")
        # 1. фигуры таскаемых узлов — на текущую геометрию
        for nid in self._moving:
            entry = self._shapes.get(nid)
            node = nodes.get(nid)
            if entry is None or node is None:
                continue
            ob = self._node_shape_geom(nid, node)
            if ob is None:
                continue
            pts, bb = ob
            ref, origin = entry
            if abs(bb[0] - origin[0]) < _EPS and abs(bb[1] - origin[1]) < _EPS:
                continue
            # Polygon копируется в moveShape (пробник) — в keep не держим
            self._router.moveShape(ref, self._mkpoly(pts))
            self._shapes[nid] = (ref, (bb[0], bb[1]))
        # 2. пины/точки live-концов — на посаженные движком концы
        for rec in self._live.values():
            e = rec["edge"]
            dirty = False
            for role, info in zip(("s", "t"), rec["ends"]):
                p = e.get("source_point" if role == "s" else "target_point")
                if not p:
                    continue
                info["x"], info["y"] = float(p[1]), float(p[0])
                if self._end_dirty(info):
                    dirty = True
            if dirty:
                s, t = rec["ends"]
                rec["conn"].setEndpoints(self._make_end(s, nodes),
                                         self._make_end(t, nodes))
        # 3. следимые фиксы (ребро таскаемого узла вне live: ручной маршрут,
        #    полилиния оператора, internal batch) — едут с данными
        for rec in self._fixed.values():
            pts = _edge_pts(rec["edge"])
            if pts is None or pts == rec["pts"]:
                continue
            rec["pts"] = pts
            rec["conn"].setEndpoints(ag.ConnEnd(ag.Point(*pts[0])),
                                     ag.ConnEnd(ag.Point(*pts[-1])))
            rec["conn"].setFixedRoute(self._mkpoly(pts))

        self._router.processTransaction()

        out = {}
        for key, rec in self._live.items():
            r = rec["conn"].displayRoute()
            raw = [(r.ps[i].x, r.ps[i].y) for i in range(r.size())]
            pts = _simplify(raw) if raw else []
            s, t = rec["ends"]
            ends_ok = (len(pts) >= 2
                       and abs(pts[0][0] - s["x"]) <= PIN_TOL
                       and abs(pts[0][1] - s["y"]) <= PIN_TOL
                       and abs(pts[-1][0] - t["x"]) <= PIN_TOL
                       and abs(pts[-1][1] - t["y"]) <= PIN_TOL)
            out[key] = {"pts": pts, "ends_ok": ends_ok, "ortho": _ortho(pts)}
        return out

    # ── завершение ──────────────────────────────────────────────────

    def close(self):
        """Отпустить SWIG-прокси (никогда не бросает: конец жеста обязан
        дойти до undo-снапшота даже при сбое роутера)."""
        try:
            self._live.clear()
            self._fixed.clear()
            self._shapes.clear()
            self._router = None
            self._keep = None
        except Exception:  # noqa: BLE001 — см. докстринг
            log.warning("edit_avoid: сбой освобождения сессии", exc_info=True)

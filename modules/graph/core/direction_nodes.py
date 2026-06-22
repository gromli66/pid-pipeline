"""
Direction nodes — обработка боксов `napravlenie` (стрелка направления НА трубе)
при построении графа.

МОДЕЛЬ (согласовано):
  - napravlenie = equipment-узел с bbox (UI рисует бокс). Направление стрелки —
    это атрибут потока, а НЕ фильтр связей сам по себе.
  - Бокс «владеет» своей внутренностью. Для каждого пайпа, примыкающего к боксу,
    смотрим грань примыкания:
      * осевая грань (по стрелке: left/right → LEFT/RIGHT, up/down → TOP/BOTTOM)
        → ребро ПОДКЛЮЧАЕТСЯ к боксу (вход/выход), роль in/out;
      * перпендикуляр НАСКВОЗЬ (входит и выходит через обе противоположные глухие
        грани = труба пересекает) → одно сквозное ребро В ОБХОД бокса;
      * перпендикуляр-поворот (одна глухая грань) → у бокса ставится ОТДЕЛЬНЫЙ
        connector, труба идёт через него, к боксу НЕ подключается;
      * мелкие огрызки у грани (короткие висячие хвостики от заливки бокса) →
        выбрасываются.

Пайплайн в builder.build (этап 5), по порядку:
  annotate_direction_nodes → stitch_collinear_stubs → drop_degenerate_stubs →
  apply_direction_rules → cap_dangling_ends → collapse_straight_connectors →
  set_direction_pass_through.

Координаты: bbox узла/аннотации — (x_min,y_min,x_max,y_max);
centroid/точки/path рёбер — [y, x].
"""

from typing import Dict, List, Optional, Tuple

NAPRAVLENIE_CLASS_ID = 40

_AXIS_BY_DIRECTION = {"left": "h", "right": "h", "up": "v", "down": "v"}
_AXIS_FACES = {"h": ("LEFT", "RIGHT"), "v": ("TOP", "BOTTOM")}
_PERP_FACES = {"h": ("TOP", "BOTTOM"), "v": ("LEFT", "RIGHT")}
_IN_FACE = {"right": "LEFT", "left": "RIGHT", "down": "TOP", "up": "BOTTOM"}
_VALID_DIRECTIONS = set(_AXIS_BY_DIRECTION)

# Терминальные огрызки ≤ этого у грани бокса — шум заливки, выбросить.
_BOX_NOISE_LEN = 25


# --------------------------------------------------------------------------- #
# Хелперы
# --------------------------------------------------------------------------- #
def _ann_direction(ann: Dict) -> Optional[str]:
    d = None
    attrs = ann.get("attributes")
    if isinstance(attrs, dict):
        d = attrs.get("direction")
    if not d:
        d = ann.get("direction")
    if isinstance(d, str):
        d = d.strip().lower()
    return d if d in _VALID_DIRECTIONS else None


def _ann_box(ann: Dict):
    b = ann.get("bbox")
    if not b or len(b) < 4:
        return None
    return (int(b[0]), int(b[1]), int(b[2]), int(b[3]))


def _napr_anns(annotations):
    return [a for a in (annotations or []) if a.get("class_id") == NAPRAVLENIE_CLASS_ID]


def _centroid_in_box(centroid, box) -> bool:
    if not centroid:
        return False
    cy, cx = centroid[0], centroid[1]
    x1, y1, x2, y2 = box
    return x1 <= cx <= x2 and y1 <= cy <= y2


def _nearest_face(x, y, box) -> str:
    x1, y1, x2, y2 = box
    d = {"LEFT": abs(x - x1), "RIGHT": abs(x - x2),
         "TOP": abs(y - y1), "BOTTOM": abs(y - y2)}
    return min(d, key=d.get)


def _max_suffix(items, prefix) -> int:
    mx = -1
    for it in items:
        i = it.get("id", "")
        if isinstance(i, str) and i.startswith(prefix):
            tail = i[len(prefix):]
            if tail.isdigit():
                mx = max(mx, int(tail))
    return mx


def _make_connector(cid: str, point_yx) -> Dict:
    y, x = int(point_yx[0]), int(point_yx[1])
    return {"id": cid, "type": "connector", "class_id": -1, "class_name": "connector",
            "centroid": [y, x], "bbox": None, "area": 0, "degree": 0}


def _path_pts(edge) -> List:
    p = edge.get("path")
    if p:
        return [pt for pt in p if pt]
    return [pt for pt in (edge.get("source_point"), edge.get("target_point")) if pt]


def _node_side_point(edge, end):
    """Точка ребра у узла (для определения грани)."""
    pt = edge.get("source_point") if end == "from" else edge.get("target_point")
    if pt:
        return pt
    path = _path_pts(edge)
    if path:
        return path[0] if end == "from" else path[-1]
    return None


def _oriented_far_to_node(edge, node_end) -> List:
    """path, ориентированный far_end → node_end."""
    path = _path_pts(edge)
    return path if node_end == "to" else list(reversed(path))


def _recompute_degrees(nodes: List[Dict], edges: List[Dict]) -> None:
    idx = {n["id"]: n for n in nodes}
    for n in nodes:
        n["degree"] = 0
    for e in edges:
        f, t = e.get("from"), e.get("to")
        if f in idx:
            idx[f]["degree"] += 1
        if t and t in idx:
            idx[t]["degree"] += 1


# --------------------------------------------------------------------------- #
# Маск-инъекция (до трассировки)
# --------------------------------------------------------------------------- #
def paint_direction_boxes_on_mask(equipment_mask, annotations, debug: bool = False) -> int:
    """bbox'ы napravlenie → в equipment-маску (бокс становится узлом с bbox)."""
    if equipment_mask is None:
        return 0
    H, W = equipment_mask.shape[:2]
    count = 0
    for ann in _napr_anns(annotations):
        box = _ann_box(ann)
        if box is None:
            continue
        x1, y1, x2, y2 = box
        x1 = max(0, x1); y1 = max(0, y1); x2 = min(W, x2); y2 = min(H, y2)
        if x2 <= x1 or y2 <= y1:
            continue
        equipment_mask[y1:y2, x1:x2] = True
        count += 1
    if debug:
        print(f"[direction_nodes] equipment += napravlenie: {count}")
    return count


def carve_boxes_from_mask(mask, annotations, debug: bool = False) -> int:
    """bbox'ы napravlenie вырезать из connection-маски (нет конкурента-connector)."""
    if mask is None:
        return 0
    H, W = mask.shape[:2]
    count = 0
    for ann in _napr_anns(annotations):
        box = _ann_box(ann)
        if box is None:
            continue
        x1, y1, x2, y2 = box
        x1 = max(0, x1); y1 = max(0, y1); x2 = min(W, x2); y2 = min(H, y2)
        if x2 <= x1 or y2 <= y1:
            continue
        mask[y1:y2, x1:x2] = False
        count += 1
    if debug:
        print(f"[direction_nodes] connection -= napravlenie: {count}")
    return count


# --------------------------------------------------------------------------- #
# Метаданные направления (type остаётся equipment — UI рисует бокс)
# --------------------------------------------------------------------------- #
def annotate_direction_nodes(nodes, edges, annotations, debug: bool = False) -> Dict[str, int]:
    stats = {"napr_nodes": 0, "relabeled": 0, "no_direction": 0}
    napr = [a for a in _napr_anns(annotations) if _ann_direction(a)]
    if not napr:
        return stats
    for node in nodes:
        if node.get("class_id") != NAPRAVLENIE_CLASS_ID or node.get("direction_node"):
            continue
        stats["napr_nodes"] += 1
        best = None
        for a in napr:
            box = _ann_box(a)
            if box and _centroid_in_box(node.get("centroid"), box):
                best = a
                break
        direction = _ann_direction(best) if best else None
        if direction is None:
            stats["no_direction"] += 1
            continue
        node["class_name"] = "napravlenie"
        node["direction_node"] = True
        node["flow_axis"] = _AXIS_BY_DIRECTION[direction]
        node["flow_direction"] = direction
        node["ann_id"] = best.get("idx", best.get("id"))
        stats["relabeled"] += 1
    if debug:
        print(f"[direction_nodes] annotate {stats}")
    return stats


# --------------------------------------------------------------------------- #
# Сшивка встречных обрывков по реальному скелету (разрывы НЕ у боксов)
# --------------------------------------------------------------------------- #
def _free_stub_ends(nodes, edges):
    """Обрывки: ребро, один конец которого — connector степени 1 или None.
    Возвращает [(edge, free_end, free_point[y,x], real_node_id)]."""
    deg = {}
    for e in edges:
        for k in ("from", "to"):
            v = e.get(k)
            if v is not None:
                deg[v] = deg.get(v, 0) + 1
    byid = {n["id"]: n for n in nodes}
    out = []
    for e in edges:
        for end, ptkey, other in (("to", "target_point", "from"), ("from", "source_point", "to")):
            v = e.get(end)
            real = e.get(other)
            if real is None:
                continue
            is_free = False
            pt = None
            if v is None:
                is_free = True
                pt = e.get(ptkey)
            else:
                n = byid.get(v)
                if n is not None and n.get("type") == "connector" and deg.get(v, 0) == 1:
                    is_free = True
                    pt = n.get("centroid") or e.get(ptkey)
            if is_free and pt is not None:
                out.append((e, end, [int(pt[0]), int(pt[1])], real))
    return out


def _segment_on_skeleton(p1, p2, skeleton, radius=3, min_frac=0.8, step=4) -> bool:
    H, W = skeleton.shape[:2]
    y1, x1 = p1; y2, x2 = p2
    n = max(abs(y2 - y1), abs(x2 - x1))
    if n == 0:
        return False
    pts = max(2, n // step)
    hit = 0
    total = 0
    for i in range(pts + 1):
        t = i / pts
        y = int(round(y1 + (y2 - y1) * t)); x = int(round(x1 + (x2 - x1) * t))
        y0, yA = max(0, y - radius), min(H, y + radius + 1)
        x0, xA = max(0, x - radius), min(W, x + radius + 1)
        total += 1
        if skeleton[y0:yA, x0:xA].any():
            hit += 1
    return total > 0 and (hit / total) >= min_frac


def _equip_between(pa, pb, nodes, margin: int = 12) -> bool:
    """Есть ли equipment-узел между точками pa,pb (его центр на отрезке)."""
    ay, ax = pa; by, bx = pb
    vy, vx = by - ay, bx - ax
    L2 = vy * vy + vx * vx
    if L2 == 0:
        return False
    for n in nodes:
        if n.get("type") != "equipment":
            continue
        c = n.get("centroid")
        if not c:
            continue
        cy, cx = c
        t = ((cy - ay) * vy + (cx - ax) * vx) / L2
        if t <= 0.05 or t >= 0.95:
            continue
        py, px = ay + t * vy, ax + t * vx
        d = ((cy - py) ** 2 + (cx - px) ** 2) ** 0.5
        b = n.get("bbox")
        half = (max(b[2] - b[0], b[3] - b[1]) / 2 + margin) if b and len(b) == 4 else margin
        if d <= half:
            return True
    return False


def stitch_collinear_stubs(nodes, edges, skeleton, max_gap: int = 800,
                           align_tol: int = 10, debug: bool = False) -> Dict[str, int]:
    """Сшить два встречных обрывка в одно ребро, если между ними непрерывный скелет."""
    stats = {"stitched": 0}
    if skeleton is None:
        return stats
    stubs = _free_stub_ends(nodes, edges)
    if len(stubs) < 2:
        return stats

    adj = set()
    for e in edges:
        f, t = e.get("from"), e.get("to")
        if f is not None and t is not None:
            adj.add(frozenset((f, t)))
    box_ids = {n["id"] for n in nodes if n.get("direction_node")}

    used = set()
    remove_edge_ids = set()
    drop_connectors = set()
    add_edges = []
    edge_counter = _max_suffix(edges, "edge_") + 1
    byid = {n["id"]: n for n in nodes}

    for i in range(len(stubs)):
        ea, end_a, pa, real_a = stubs[i]
        if id(ea) in used:
            continue
        best = None
        for j in range(i + 1, len(stubs)):
            eb, end_b, pb, real_b = stubs[j]
            if id(eb) in used or real_b == real_a:
                continue
            dy, dx = abs(pa[0] - pb[0]), abs(pa[1] - pb[1])
            if not (dx <= align_tol or dy <= align_tol):
                continue
            gap = (dy * dy + dx * dx) ** 0.5
            if gap > max_gap:
                continue
            if real_a in box_ids and real_b in box_ids:
                continue  # два бокса napravlenie — не сшиваем
            if frozenset((real_a, real_b)) in adj:
                continue  # уже связаны — не дублируем
            if _equip_between(pa, pb, nodes):
                continue  # между концами стоит узел — не прыгаем через него
            if not _segment_on_skeleton(pa, pb, skeleton):
                continue
            if best is None or gap < best[0]:
                best = (gap, j, eb, end_a, end_b, pa, pb, real_a, real_b)
        if best is None:
            continue
        _, j, eb, end_a, end_b, pa, pb, real_a, real_b = best
        used.add(id(ea)); used.add(id(eb))
        for e, end in ((ea, end_a), (eb, end_b)):
            remove_edge_ids.add(e["id"])
            v = e.get(end)
            if v is not None and byid.get(v, {}).get("type") == "connector":
                drop_connectors.add(v)
        add_edges.append({
            "id": f"edge_{edge_counter}", "from": real_a, "to": real_b,
            "source_point": pa, "target_point": pb,
            "path": [pa, pb], "length": int((sum((a - b) ** 2 for a, b in zip(pa, pb))) ** 0.5),
            "is_terminal": False, "color": None,
        })
        edge_counter += 1
        stats["stitched"] += 1

    if remove_edge_ids or add_edges:
        edges[:] = [e for e in edges if e["id"] not in remove_edge_ids] + add_edges
    if drop_connectors:
        nodes[:] = [n for n in nodes if n["id"] not in drop_connectors]
    if debug:
        print(f"[direction_nodes] stitch {stats}")
    return stats


# --------------------------------------------------------------------------- #
# Выброс вырожденных огрызков (общий шум трассировки)
# --------------------------------------------------------------------------- #
def drop_degenerate_stubs(nodes, edges, max_len: int = 3, debug: bool = False) -> Dict[str, int]:
    stats = {"dropped": 0}
    deg = {}
    for e in edges:
        for k in ("from", "to"):
            v = e.get(k)
            if v is not None:
                deg[v] = deg.get(v, 0) + 1
    byid = {n["id"]: n for n in nodes}

    keep = []
    for e in edges:
        terminal = e.get("to") is None or e.get("from") is None
        free_conn = False
        for k in ("from", "to"):
            v = e.get(k)
            n = byid.get(v)
            if n is not None and n.get("type") == "connector" and deg.get(v, 0) == 1:
                free_conn = True
        if (terminal or free_conn) and e.get("length", 0) <= max_len:
            stats["dropped"] += 1
            continue
        keep.append(e)
    if stats["dropped"]:
        edges[:] = keep
        deg2 = {}
        for e in edges:
            for k in ("from", "to"):
                v = e.get(k)
                if v is not None:
                    deg2[v] = deg2.get(v, 0) + 1
        nodes[:] = [n for n in nodes
                    if not (n.get("type") == "connector" and deg2.get(n["id"], 0) == 0
                            and str(n["id"]).startswith(("capconn_", "dirconn_")))]
    if debug:
        print(f"[direction_nodes] drop_degenerate {stats}")
    return stats


# --------------------------------------------------------------------------- #
# Разрешитель бокса: ось / перпендикуляр-насквозь / перпендикуляр-поворот
# --------------------------------------------------------------------------- #
def apply_direction_rules(nodes, edges, debug: bool = False) -> Dict[str, int]:
    """Правила оси у каждого direction-узла. На месте."""
    stats = {"nodes": 0, "axis_kept": 0, "through_merged": 0,
             "perp_capped": 0, "noise_dropped": 0}

    dir_nodes = [n for n in nodes if n.get("direction_node") and n.get("flow_direction")]
    if not dir_nodes:
        return stats

    conn_counter = _max_suffix(nodes, "dirconn_") + 1
    edge_counter = _max_suffix(edges, "edge_") + 1
    new_connectors: List[Dict] = []
    remove_ids = set()
    add_edges: List[Dict] = []

    for N in dir_nodes:
        stats["nodes"] += 1
        nid = N["id"]
        box = N["bbox"]
        d = N["flow_direction"]
        axis = _AXIS_BY_DIRECTION[d]
        axis_faces = set(_AXIS_FACES[axis])
        fa, fb = _PERP_FACES[axis]

        incident: List[Tuple[Dict, str, str]] = []
        for e in edges:
            if e["id"] in remove_ids:
                continue
            for end in ("from", "to"):
                if e.get(end) == nid:
                    pt = _node_side_point(e, end)
                    if pt is None:
                        continue
                    face = _nearest_face(pt[1], pt[0], box)
                    incident.append((e, end, face))

        # 0) отсеять мелкие огрызки у грани (терминальные короткие хвостики заливки),
        #    чтобы они не путали классификацию/сшивку ниже
        kept_inc = []
        for e, end, face in incident:
            far = e.get("to") if end == "from" else e.get("from")
            if far is None and e.get("length", 0) <= _BOX_NOISE_LEN:
                remove_ids.add(e["id"])
                stats["noise_dropped"] += 1
            else:
                kept_inc.append((e, end, face))
        incident = kept_inc

        # осевые — оставляем + роль in/out
        for e, end, face in incident:
            if face in axis_faces:
                e["direction"] = d
                e["flow_role"] = "in" if face == _IN_FACE[d] else "out"
                stats["axis_kept"] += 1

        # перпендикулярные
        perp = [(e, end, face) for (e, end, face) in incident if face not in axis_faces]
        side_a = [t for t in perp if t[2] == fa]
        side_b = [t for t in perp if t[2] == fb]

        # перпендикуляр НАСКВОЗЬ (пары противоположных глухих граней) → в обход бокса
        for (ea, enda, _), (eb, endb, _) in zip(side_a, side_b):
            path_a = _oriented_far_to_node(ea, enda)
            path_b = list(reversed(_oriented_far_to_node(eb, endb)))
            merged = path_a + path_b
            far_a = ea["to"] if enda == "from" else ea["from"]
            far_b = eb["to"] if endb == "from" else eb["from"]
            add_edges.append({
                "id": f"edge_{edge_counter}", "from": far_a, "to": far_b,
                "source_point": (merged[0] if merged else None),
                "target_point": (merged[-1] if merged else None),
                "path": merged, "length": len(merged),
                "is_terminal": False, "color": ea.get("color"),
            })
            edge_counter += 1
            remove_ids.add(ea["id"]); remove_ids.add(eb["id"])
            stats["through_merged"] += 1

        # одиночный перпендикуляр-поворот → отдельный connector (к боксу НЕ цепляем)
        paired = min(len(side_a), len(side_b))
        leftovers = side_a[paired:] + side_b[paired:]
        for e, end, face in leftovers:
            pt = _node_side_point(e, end)
            conn = _make_connector(f"dirconn_{conn_counter}", pt)
            conn_counter += 1
            new_connectors.append(conn)
            e[end] = conn["id"]
            stats["perp_capped"] += 1

    if remove_ids or add_edges:
        edges[:] = [e for e in edges if e["id"] not in remove_ids] + add_edges
    if new_connectors:
        nodes.extend(new_connectors)

    if debug:
        print(f"[direction_nodes] rules {stats}")
    return stats


# --------------------------------------------------------------------------- #
# Капинг висячих концов (не теряем трубы); внутри бокса — выбросить
# --------------------------------------------------------------------------- #
def drop_duplicate_contact_stubs(nodes, edges, max_gap: int = 15,
                                 debug: bool = False) -> Dict[str, int]:
    """Выбросить дубль-контактные «огрызки вникуда».

    Висячее ребро S (один конец to/from=None — ведёт в никуда) удаляется, если у того
    же узла есть ДРУГОЕ ребро R, уходящее в ТУ ЖЕ сторону (одинаковый _dir_to_side), и
    их node-side контактные точки близки (≤max_gap). Это признак дубль-контакта: одна
    труба отметилась на границе узла двумя соседними точками → лишняя короткая ветка.
    Удаляем только S; реальную трубу R оставляем. Реальные рёбра между двумя узлами и
    одиночные висячие концы (без параллельного соседа) не трогаем.

    Должно идти ДО cap_dangling_ends (пока огрызок ещё to=None, а не capconn), иначе он
    станет лишним connector'ом и заблокирует схлопывание прямой (узел станет степени 3).
    """
    stats = {"dropped": 0}
    incid: Dict = {}
    for e in edges:
        for k in ("from", "to"):
            v = e.get(k)
            if v is not None:
                incid.setdefault(v, []).append(e)

    remove = set()
    for e in edges:
        if e["id"] in remove:
            continue
        if e.get("to") is None and e.get("from") is not None:
            node_id, node_end = e["from"], "from"
        elif e.get("from") is None and e.get("to") is not None:
            node_id, node_end = e["to"], "to"
        else:
            continue  # не висячее — пропускаем
        s_side = _dir_to_side(_edge_outward_dir(e, node_id))
        s_pt = _node_side_point(e, node_end)
        if s_side is None or s_pt is None:
            continue
        for r in incid.get(node_id, []):
            if r is e or r["id"] in remove:
                continue
            r_end = "from" if r.get("from") == node_id else "to"
            r_side = _dir_to_side(_edge_outward_dir(r, node_id))
            r_pt = _node_side_point(r, r_end)
            if r_side != s_side or r_pt is None:
                continue
            d = ((s_pt[0] - r_pt[0]) ** 2 + (s_pt[1] - r_pt[1]) ** 2) ** 0.5
            if d <= max_gap:
                remove.add(e["id"])
                stats["dropped"] += 1
                break

    if remove:
        edges[:] = [e for e in edges if e["id"] not in remove]
    if debug:
        print(f"[direction_nodes] drop_dup_contact {stats}")
    return stats


def cap_dangling_ends(nodes, edges, debug: bool = False) -> Dict[str, int]:
    """Закрыть connector'ом реальные висячие концы труб (to=None / from=None).

    Закрываем ВСЕ концы, включая перпендикуляр-поворот у грани бокса (его конец уже
    «за боксом» — скелет под боксом вырезан, бокс остаётся на оси, разрыв намеренный).
    Мелкие огрызки ≤3px должен погасить drop_degenerate_stubs ДО cap, иначе они дадут
    лишние connector'ы.
    """
    stats = {"capped": 0}
    conn_counter = _max_suffix(nodes, "capconn_") + 1
    new_connectors: List[Dict] = []

    for e in edges:
        for end, ptkey in (("from", "source_point"), ("to", "target_point")):
            if e.get(end) is not None:
                continue
            pt = e.get(ptkey)
            if pt is None:
                path = _path_pts(e)
                pt = (path[0] if end == "from" else path[-1]) if path else None
            if pt is None:
                continue
            conn = _make_connector(f"capconn_{conn_counter}", pt)
            conn_counter += 1
            new_connectors.append(conn)
            e[end] = conn["id"]
            e["is_terminal"] = False
            stats["capped"] += 1

    if new_connectors:
        nodes.extend(new_connectors)
    if debug:
        print(f"[direction_nodes] cap {stats}")
    return stats


def detach_degenerate_box_stubs(nodes, edges, max_len: int = 12,
                                debug: bool = False) -> Dict[str, int]:
    """Отцепить короткие вырожденные стабы direction-бокса, упирающиеся в
    connector перпендикулярной трубы (степени >=3). Бокс и труба должны быть
    раздельными элементами; после отцепления connector становится степени 2 и
    схлопывается в collapse_straight_connectors. На месте.

    Не трогаем осевые рёбра (есть flow_role) — это настоящие подключения к боксу.
    """
    stats = {"detached": 0}
    box_ids = {n["id"] for n in nodes if n.get("direction_node")}
    if not box_ids:
        return stats

    node_by_id = {n["id"]: n for n in nodes}
    deg: Dict = {}
    for e in edges:
        for nid in (e.get("from"), e.get("to")):
            if nid is not None:
                deg[nid] = deg.get(nid, 0) + 1

    remove = set()
    for e in edges:
        if e["id"] in remove or e.get("flow_role"):
            continue
        frm, to = e.get("from"), e.get("to")
        if frm in box_ids and to is not None and to not in box_ids:
            far = to
        elif to in box_ids and frm is not None and frm not in box_ids:
            far = frm
        else:
            continue
        far_node = node_by_id.get(far)
        if not far_node or far_node.get("type") != "connector":
            continue
        # connector реальной (перпендикулярной) трубы: помимо стаба у него >=2 ребра
        if deg.get(far, 0) >= 3 and e.get("length", 0) <= max_len:
            remove.add(e["id"])
            stats["detached"] += 1

    if remove:
        edges[:] = [e for e in edges if e["id"] not in remove]
        _recompute_degrees(nodes, edges)
    if debug:
        print(f"    [detach_box_stubs] отцеплено {stats['detached']}")
    return stats


def set_direction_pass_through(nodes, edges) -> None:
    """Пересчитать pass_through у direction-узлов по фактической степени."""
    deg = {n["id"]: 0 for n in nodes}
    for e in edges:
        f, t = e.get("from"), e.get("to")
        if f in deg:
            deg[f] += 1
        if t in deg:
            deg[t] += 1
    for n in nodes:
        if n.get("direction_node"):
            n["degree"] = deg.get(n["id"], 0)
            n["pass_through"] = n["degree"] >= 2


# --------------------------------------------------------------------------- #
# Схлопывание прямых проходных connector'ов (по сторонам квадрата)
# --------------------------------------------------------------------------- #
def _edge_outward_dir(edge, node_id, probe: int = 15):
    path = edge.get("path")
    if path and len(path) >= 2:
        seq = list(reversed(path)) if edge.get("to") == node_id else list(path)
        p0 = seq[0]
        pk = seq[min(len(seq) - 1, probe)]
        return (pk[0] - p0[0], pk[1] - p0[1])
    sp, tp = edge.get("source_point"), edge.get("target_point")
    if not sp or not tp:
        return None
    p0, pk = (tp, sp) if edge.get("to") == node_id else (sp, tp)
    return (pk[0] - p0[0], pk[1] - p0[1])


def _dir_to_side(vec):
    if vec is None:
        return None
    dy, dx = vec
    if dy == 0 and dx == 0:
        return None
    if abs(dx) >= abs(dy):
        return "RIGHT" if dx > 0 else "LEFT"
    return "BOTTOM" if dy > 0 else "TOP"


_OPPOSITE = {frozenset(("LEFT", "RIGHT")), frozenset(("TOP", "BOTTOM"))}


def collapse_straight_connectors(nodes, edges, debug: bool = False) -> Dict[str, int]:
    """Схлопнуть проходные connector'ы степени 2, рёбра которых на противоположных
    сторонах квадрата (лево↔право / верх↔низ). На месте."""
    stats = {"collapsed": 0}
    edge_counter = _max_suffix(edges, "edge_") + 1

    changed = True
    while changed:
        changed = False
        incid = {}
        for e in edges:
            for k in ("from", "to"):
                v = e.get(k)
                if v is not None:
                    incid.setdefault(v, []).append(e)

        for node in nodes:
            if node.get("type") != "connector":
                continue
            nid = node["id"]
            inc = incid.get(nid, [])
            if len(inc) != 2:
                continue
            e1, e2 = inc[0], inc[1]
            if e1 is e2:
                continue
            a = e1["to"] if e1["from"] == nid else e1["from"]
            b = e2["to"] if e2["from"] == nid else e2["from"]
            if a is None or b is None or a == b:
                continue
            s1 = _dir_to_side(_edge_outward_dir(e1, nid))
            s2 = _dir_to_side(_edge_outward_dir(e2, nid))
            if s1 is None or s2 is None:
                continue
            if frozenset((s1, s2)) not in _OPPOSITE:
                continue  # соседние стороны = поворот, не схлопываем

            # антидубль: A и B уже связаны другим ребром — просто убрать connector
            already = any(
                frozenset((e.get("from"), e.get("to"))) == frozenset((a, b))
                for e in edges if e is not e1 and e is not e2
            )
            if already:
                edges.remove(e1); edges.remove(e2); nodes.remove(node)
                stats["collapsed"] += 1
                changed = True
                break

            def oriented(e, far):
                p = e.get("path")
                if p:
                    return list(p) if e.get("from") == far else list(reversed(p))
                pts = [x for x in (e.get("source_point"), e.get("target_point")) if x]
                return pts if e.get("from") == far else list(reversed(pts))

            merged_path = oriented(e1, a) + oriented(e2, nid)
            direction = e1.get("direction") or e2.get("direction")
            role = e1.get("flow_role") or e2.get("flow_role")
            new_edge = {
                "id": f"edge_{edge_counter}", "from": a, "to": b,
                "source_point": (merged_path[0] if merged_path else e1.get("source_point")),
                "target_point": (merged_path[-1] if merged_path else e2.get("target_point")),
                "path": merged_path,
                "length": len(merged_path) or (e1.get("length", 0) + e2.get("length", 0)),
                "is_terminal": False, "color": e1.get("color"),
            }
            if direction:
                new_edge["direction"] = direction
            if role:
                new_edge["flow_role"] = role
            edge_counter += 1
            edges.remove(e1); edges.remove(e2); edges.append(new_edge)
            nodes.remove(node)
            stats["collapsed"] += 1
            changed = True
            break

    if debug:
        print(f"[direction_nodes] collapse_straight {stats}")
    return stats

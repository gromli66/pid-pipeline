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


def _make_connector(cid: str, point_yx, area: int = 0) -> Dict:
    y, x = int(point_yx[0]), int(point_yx[1])
    return {"id": cid, "type": "connector", "class_id": -1, "class_name": "connector",
            "centroid": [y, x], "bbox": None, "area": area, "degree": 0}


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
            if node.get("bend"):
                # излом трубы: узел стоит именно потому, что тут не прямая, а
                # признак «противоположные грани» поворота на 30° не видит
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


# --------------------------------------------------------------------------- #
# Излом трубы → узел (геометрия ребра должна совпадать с растром)
# --------------------------------------------------------------------------- #
# Ребро без узла на повороте вырождается в хорду: реальный путь в 1157 px
# записывается прямой между концами, и оператор такое ребро удаляет и рисует
# заново по узлам. Замер по 14 парам graph.json/graph_validated.json (3981
# ребро): отклонение пути от хорды >30 px → ребро удалено в 71% случаев,
# 10–30 px → в 26%, ≤10 px → в 2%.
_CORNER_EPS = 6        # допуск упрощения пути (Ramer–Douglas–Peucker), px
_CORNER_MIN_ARM = 8    # плечо короче — шум скелетизации, а не поворот
_PATH_JUMP = 2         # разрыв пути больше этого = телепорт через мост
_CONNECTOR_AREA = 225  # 15x15 — как у connector'ов со стадии стыков


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _rdp_keep(path, i0: int, i1: int, eps: float) -> List[int]:
    """Вершины упрощённого пути на участке [i0, i1] (Ramer–Douglas–Peucker)."""
    keep = {i0, i1}
    stack = [(i0, i1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        (y0, x0), (y1, x1) = path[i], path[j]
        chord = ((y1 - y0) ** 2 + (x1 - x0) ** 2) ** 0.5
        worst, worst_i = -1.0, -1
        for k in range(i + 1, j):
            py, px = path[k]
            if chord >= 1.0:
                d = abs((y1 - y0) * (x0 - px) - (x1 - x0) * (y0 - py)) / chord
            else:
                d = ((py - y0) ** 2 + (px - x0) ** 2) ** 0.5
            if d > worst:
                worst, worst_i = d, k
        if worst > eps:
            keep.add(worst_i)
            stack.append((i, worst_i))
            stack.append((worst_i, j))
    return sorted(keep)


def _path_runs(path) -> List[Tuple[int, int]]:
    """Непрерывные куски пути; режем по телепортам через мосты."""
    runs, start = [], 0
    for i in range(len(path) - 1):
        if _dist(path[i], path[i + 1]) > _PATH_JUMP:
            runs.append((start, i))
            start = i + 1
    runs.append((start, len(path) - 1))
    return runs


def _corner_indices(path, eps: float = _CORNER_EPS,
                    min_arm: float = _CORNER_MIN_ARM) -> List[int]:
    """Индексы точек пути, где труба реально поворачивает.

    Путь сначала режется на непрерывные куски по телепортам через мосты — иначе
    прыжок моста читается как излом и узел встаёт посреди перекрёстка. Внутри
    куска работает RDP: после разреза по его вершинам отклонение любого сегмента
    от собственной хорды не превышает eps по построению.
    """
    if not path or len(path) < 3:
        return []
    out: List[int] = []
    for a, b in _path_runs(path):
        if b - a < 2:
            continue
        idx = _rdp_keep(path, a, b, eps)
        for t in range(1, len(idx) - 1):
            k, prev, nxt = idx[t], idx[t - 1], idx[t + 1]
            if _dist(path[prev], path[k]) < min_arm:
                continue
            if _dist(path[k], path[nxt]) < min_arm:
                continue
            out.append(k)
    return sorted(out)


def _split_edge_at(edge: Dict, cuts: List[int], node_ids: List[str],
                   edge_counter: int) -> Tuple[List[Dict], int]:
    """Разрезать ребро в точках cuts, подставив node_ids между кусками."""
    path = edge["path"]
    ends = [edge.get("from")] + list(node_ids) + [edge.get("to")]
    bounds = [0] + list(cuts) + [len(path) - 1]
    last = len(bounds) - 2
    pieces = []
    for i in range(len(bounds) - 1):
        seg = path[bounds[i]:bounds[i + 1] + 1]
        piece = dict(edge)
        piece["id"] = f"edge_{edge_counter}"
        edge_counter += 1
        piece["from"], piece["to"] = ends[i], ends[i + 1]
        piece["path"] = seg
        piece["source_point"] = edge.get("source_point") if i == 0 else list(seg[0])
        piece["target_point"] = edge.get("target_point") if i == last else list(seg[-1])
        piece["length"] = len(seg)
        piece["straight_line_distance"] = float(_dist(seg[0], seg[-1]))
        piece["is_terminal"] = bool(edge.get("is_terminal")) and i == last
        pieces.append(piece)
    return pieces, edge_counter


def split_edges_at_corners(nodes, edges, eps: float = _CORNER_EPS,
                           min_arm: float = _CORNER_MIN_ARM,
                           debug: bool = False) -> Dict[str, int]:
    """Поставить connector в каждый излом трассированного пути. На месте.

    Топологически нейтрально: connector степени 2 внутри ребра эквивалентен
    исходному ребру — меняется только геометрия, она перестаёт быть хордой.
    Узлы помечаются `bend`, чтобы collapse_straight_connectors их не схлопнул
    обратно (его признак «прямой» — противоположные грани — поворота на 30°
    не видит).
    """
    stats = {"corners": 0, "edges_split": 0}
    conn_counter = _max_suffix(nodes, "bendconn_") + 1
    edge_counter = _max_suffix(edges, "edge_") + 1
    new_nodes: List[Dict] = []
    result: List[Dict] = []

    for e in edges:
        path = e.get("path")
        cuts = _corner_indices(path, eps, min_arm) if path else []
        if not cuts:
            result.append(e)
            continue

        node_ids = []
        for k in cuts:
            conn = _make_connector(f"bendconn_{conn_counter}", path[k],
                                   area=_CONNECTOR_AREA)
            conn["bend"] = True
            conn_counter += 1
            new_nodes.append(conn)
            node_ids.append(conn["id"])

        pieces, edge_counter = _split_edge_at(e, cuts, node_ids, edge_counter)
        result.extend(pieces)
        stats["edges_split"] += 1
        stats["corners"] += len(cuts)

    if new_nodes:
        edges[:] = result
        nodes.extend(new_nodes)
    if debug:
        print(f"[direction_nodes] split_corners {stats}")
    return stats


# --------------------------------------------------------------------------- #
# Врезка висячего конца в проходящую трубу
# --------------------------------------------------------------------------- #
# Модель стыков иногда не даёт отклика на реальном тройнике; развилку скелета
# съедает первый прошедший трассер, ветка упирается в чужую трубу и остаётся
# висячей, а cap вешает на неё connector прямо посреди трубы — «коннектор стоит
# на линии, но не в ней». Замер на 26747a10: из 21 висячего конца 7 лежат в
# ≤1.4 px от чужой трубы, остальные — дальше 10 px, так что порог 3 px безопасен.
_STITCH_TOL = 3.0        # ближе этого висячий конец считаем упёршимся в трубу
_STITCH_CROSS_GAP = 6    # два конца ближе этого на одной трубе = «крест»
_STITCH_CELL = 16        # шаг корзин пространственного индекса


def stitch_dangling_into_pipe(nodes, edges, tol: float = _STITCH_TOL,
                              debug: bool = False) -> Dict[str, int]:
    """Висячий конец, упёршийся в чужую трубу → разрезать её и соединить.

    Должно идти ДО cap_dangling_ends, пока конец ещё from/to=None. Пересечения
    «крестом» (два висячих конца в одном месте по разные стороны трубы) не
    трогаем: там правильный ответ — сквозной проход или узел степени 4, а не
    врезка в точку.
    """
    stats = {"stitched": 0, "crossings_skipped": 0}

    buckets: Dict[Tuple[int, int], List[Tuple[str, int]]] = {}
    by_id: Dict[str, Dict] = {}
    for e in edges:
        path = e.get("path")
        if not path or len(path) < 3:
            continue
        by_id[e["id"]] = e
        for i, pt in enumerate(path):
            buckets.setdefault((pt[0] // _STITCH_CELL, pt[1] // _STITCH_CELL),
                               []).append((e["id"], i))
    if not by_id:
        return stats

    def nearest_host(point, own_id):
        cy, cx = int(point[0]) // _STITCH_CELL, int(point[1]) // _STITCH_CELL
        best_d, best_id, best_i = tol, None, None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for eid, i in buckets.get((cy + dy, cx + dx), ()):
                    if eid == own_id:
                        continue
                    d = _dist(point, by_id[eid]["path"][i])
                    if d < best_d:
                        best_d, best_id, best_i = d, eid, i
        return best_id, best_i

    cuts: Dict[str, List[Tuple[int, Dict, str]]] = {}
    for e in edges:
        for end, key in (("from", "source_point"), ("to", "target_point")):
            if e.get(end) is not None:
                continue
            pt = e.get(key)
            if pt is None:
                path = e.get("path") or []
                pt = (path[0] if end == "from" else path[-1]) if path else None
            if pt is None:
                continue
            host_id, idx = nearest_host(pt, e.get("id"))
            if host_id is None:
                continue
            host_len = len(by_id[host_id]["path"])
            if idx < _CORNER_MIN_ARM or idx > host_len - 1 - _CORNER_MIN_ARM:
                continue          # у самого узла — это не врезка
            cuts.setdefault(host_id, []).append((idx, e, end))

    # висячее ребро, которое само является хозяином врезки, не трогаем:
    # его словарь будет заменён кусками, и правка конца потеряется
    for host_id in list(cuts):
        cuts[host_id] = [it for it in cuts[host_id] if it[1].get("id") not in cuts]
        if not cuts[host_id]:
            del cuts[host_id]

    conn_counter = _max_suffix(nodes, "teeconn_") + 1
    edge_counter = _max_suffix(edges, "edge_") + 1
    new_nodes: List[Dict] = []
    replaced: Dict[str, List[Dict]] = {}

    for host_id, items in cuts.items():
        items.sort(key=lambda it: it[0])
        kept = []
        for i, it in enumerate(items):
            if any(abs(it[0] - other[0]) <= _STITCH_CROSS_GAP
                   for j, other in enumerate(items) if j != i):
                stats["crossings_skipped"] += 1
                continue
            kept.append(it)
        if not kept:
            continue

        host = by_id[host_id]
        idxs = [it[0] for it in kept]
        node_ids = []
        for idx in idxs:
            conn = _make_connector(f"teeconn_{conn_counter}", host["path"][idx],
                                   area=_CONNECTOR_AREA)
            conn_counter += 1
            new_nodes.append(conn)
            node_ids.append(conn["id"])

        pieces, edge_counter = _split_edge_at(host, idxs, node_ids, edge_counter)
        replaced[host_id] = pieces

        for (idx, dangling, end), node_id in zip(kept, node_ids):
            seat = list(host["path"][idx])
            dangling[end] = node_id
            path = list(dangling.get("path") or [])
            if end == "from":
                dangling["source_point"] = seat
                dangling["path"] = [seat] + path
            else:
                dangling["target_point"] = seat
                dangling["path"] = path + [seat]
            dangling["length"] = len(dangling["path"])
            dangling["is_terminal"] = dangling.get("to") is None
            stats["stitched"] += 1

    if replaced:
        out: List[Dict] = []
        for e in edges:
            out.extend(replaced.get(e.get("id"), [e]))
        edges[:] = out
        nodes.extend(new_nodes)
    if debug:
        print(f"[direction_nodes] stitch_dangling {stats}")
    return stats


# --------------------------------------------------------------------------- #
# Кластеры дублей стыков — только пометка для оператора
# --------------------------------------------------------------------------- #
# Правила, которые надёжно отличают дубль от настоящего стыка, из текущих
# артефактов не нашлось: на корпусе из 1353 коннекторов лучший предикат
# («уверенность <0.6 и сосед-коннектор ближе 25 px») даёт точность 33%. Поэтому
# такие места не удаляем, а помечаем — решает оператор.
_CLUSTER_RADIUS = 30
_CLUSTER_MIN = 3


def flag_connector_clusters(nodes, radius: int = _CLUSTER_RADIUS,
                            min_size: int = _CLUSTER_MIN,
                            debug: bool = False) -> Dict[str, int]:
    """Пометить `cluster_suspect` у коннекторов, сбившихся в кучу."""
    stats = {"flagged": 0}
    conns = [n for n in nodes if n.get("type") == "connector" and n.get("centroid")]
    for n in conns:
        near = sum(1 for m in conns
                   if m is not n and _dist(n["centroid"], m["centroid"]) <= radius)
        if near >= min_size - 1:
            n["cluster_suspect"] = True
            stats["flagged"] += 1
    if debug:
        print(f"[direction_nodes] flag_clusters {stats}")
    return stats


# --------------------------------------------------------------------------- #
# Зигзаг — не поворот: схлопнуть цепочки придуманных узлов обратно в прямую
# --------------------------------------------------------------------------- #
# split_edges_at_corners режет путь локально, поэтому дрожание скелета (текст,
# попавший в маску трубы) даёт «повороты» там, где труба идёт прямо: ушло вбок
# на 15 px и вернулось. Считаем цепочку целиком: если её реальный путь нигде не
# отходит от прямой между концами дальше _CHAIN_TOL, прямая — честное
# представление, а узлы внутри — мусор. Порог согласован с критерием, ради
# которого П1 и делался: оператор удаляет ребро, когда путь отходит от хорды
# больше чем на 30 px.
_CHAIN_TOL = 30.0


def merge_straight_chains(nodes, edges, tol: float = _CHAIN_TOL,
                          debug: bool = False) -> Dict[str, int]:
    """Схлопнуть цепочки узлов-изломов, которые в целом лежат на прямой.

    Трогаем только узлы, поставленные нами (`bend`): стыки, утверждённые
    оператором, и оборудование — якоря, они неприкосновенны.
    """
    stats = {"merged": 0, "nodes_dropped": 0}
    edge_counter = _max_suffix(edges, "edge_") + 1

    changed = True
    while changed:
        changed = False
        incid: Dict[str, List[Dict]] = {}
        for e in edges:
            for k in ("from", "to"):
                v = e.get(k)
                if v is not None:
                    incid.setdefault(v, []).append(e)
        by_id = {n["id"]: n for n in nodes}

        def movable(nid):
            n = by_id.get(nid)
            return bool(n and n.get("bend")) and len(incid.get(nid, ())) == 2

        for anchor in list(by_id):
            if movable(anchor):
                continue                      # не якорь, а внутренность цепочки
            for first in list(incid.get(anchor, ())):
                path: List = []
                interior: List[str] = []
                cur, e = anchor, first
                while True:
                    path += _oriented_from(e, cur)
                    nxt = e["to"] if e.get("from") == cur else e.get("from")
                    if nxt is None or not movable(nxt):
                        break
                    interior.append(nxt)
                    pair = incid[nxt]
                    e = pair[0] if pair[0] is not e else pair[1]
                    cur = nxt
                if not interior or len(path) < 2 or nxt == anchor:
                    continue
                if _max_deviation(path) > tol:
                    continue                  # настоящий поворот — оставляем

                keep = [x for x in edges
                        if x["id"] not in {y["id"] for y in _chain_edges(incid, interior)}]
                merged = {
                    "id": f"edge_{edge_counter}", "from": anchor, "to": nxt,
                    "source_point": list(path[0]), "target_point": list(path[-1]),
                    "path": path, "length": len(path), "is_terminal": False,
                    "color": first.get("color"),
                    "straight_line_distance": float(_dist(path[0], path[-1])),
                }
                edge_counter += 1
                edges[:] = keep + [merged]
                drop = set(interior)
                nodes[:] = [n for n in nodes if n["id"] not in drop]
                stats["merged"] += 1
                stats["nodes_dropped"] += len(interior)
                changed = True
                break
            if changed:
                break

    if debug:
        print(f"[direction_nodes] merge_straight_chains {stats}")
    return stats


def _oriented_from(edge, start) -> List:
    path = list(edge.get("path") or [])
    if not path:
        pts = [p for p in (edge.get("source_point"), edge.get("target_point")) if p]
        path = pts
    return path if edge.get("from") == start else list(reversed(path))


def _chain_edges(incid, interior) -> List[Dict]:
    out, seen = [], set()
    for nid in interior:
        for e in incid.get(nid, ()):
            if id(e) not in seen:
                seen.add(id(e))
                out.append(e)
    return out


def _max_deviation(path) -> float:
    if len(path) < 3:
        return 0.0
    (y0, x0), (y1, x1) = path[0], path[-1]
    chord = _dist(path[0], path[-1])
    if chord < 1:
        return max(_dist(p, path[0]) for p in path)
    return max(abs((y1 - y0) * (x0 - px) - (x1 - x0) * (y0 - py)) / chord
               for py, px in path)


# --------------------------------------------------------------------------- #
# Коннектор, прижатый к элементу — это связь, а не узел
# --------------------------------------------------------------------------- #
# Модель стыков нередко ставит квадрат прямо на границе символа. Между ним и
# элементом остаётся огрызок скелета в 4-20 px, и в графе появляется лишний
# узел там, где труба просто входит в элемент. Растворяем: огрызок убираем,
# трубу цепляем к элементу напрямую.
_BOUNDARY_STUB = 20


def dissolve_boundary_connectors(nodes, edges, max_stub: int = _BOUNDARY_STUB,
                                 debug: bool = False) -> Dict[str, int]:
    """Коннектор степени 2, приклеенный огрызком к элементу → убрать. На месте."""
    stats = {"dissolved": 0}
    by_id = {n["id"]: n for n in nodes}

    def is_connector(nid):
        n = by_id.get(nid)
        return bool(n) and n.get("class_name") == "connector"

    changed = True
    while changed:
        changed = False
        incid: Dict[str, List[Dict]] = {}
        for e in edges:
            for k in ("from", "to"):
                v = e.get(k)
                if v is not None:
                    incid.setdefault(v, []).append(e)

        for node in nodes:
            nid = node["id"]
            if not is_connector(nid):
                continue
            pair = incid.get(nid, [])
            if len(pair) != 2:
                continue
            stubs = [e for e in pair
                     if e.get("length", 10 ** 9) <= max_stub
                     and not is_connector(e["to"] if e["from"] == nid else e["from"])]
            if len(stubs) != 1:
                continue                      # ни одного или оба — не наш случай
            stub = stubs[0]
            keep = pair[0] if pair[1] is stub else pair[1]
            element = stub["to"] if stub["from"] == nid else stub["from"]
            far = keep["to"] if keep["from"] == nid else keep["from"]
            if far == element:
                continue                      # петля на элемент

            # merged идёт far -> nid -> element
            merged = _oriented_from(keep, far) + _oriented_from(stub, nid)
            if keep.get("from") == nid:
                keep["from"] = element
                keep["source_point"] = list(merged[-1]) if merged else keep.get("source_point")
                path = list(reversed(merged))          # from=element -> far
            else:
                keep["to"] = element
                keep["target_point"] = list(merged[-1]) if merged else keep.get("target_point")
                path = merged                          # from=far -> element
            if merged:
                keep["path"] = path
                keep["length"] = len(path)
                keep["straight_line_distance"] = float(_dist(path[0], path[-1]))
            edges.remove(stub)
            nodes.remove(node)
            by_id.pop(nid, None)
            stats["dissolved"] += 1
            changed = True
            break

    if debug:
        print(f"[direction_nodes] dissolve_boundary {stats}")
    return stats

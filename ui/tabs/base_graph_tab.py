"""
Base Graph Tab — базовый класс вкладок редактора графа P&ID.

Template method: скачивание артефактов, сохранение, подтверждение.
Потомки: SimpleGraphTab, AdvancedGraphTab.

Принцип: Base НЕ обращается к атрибутам потомков напрямую.
Вся расширяемость — через _create_editor() и _setup_toolbar().
"""

import json
import logging
import struct
import tempfile
from abc import abstractmethod
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QMessageBox, QApplication,
)
from PySide6.QtCore import Signal, Slot, Qt, QThread

from ui.services.api_client import APIClient, APIError
from ui.services.thread_lifetime import hand_over
from ui.services.artifact_downloader import (
    ArtifactDownloader, Job, artifact, one,
)
from ui.editors.base_graph_editor import BaseGraphEditor
from ui.tabs.blind_overwrite import BlindOverwriteGuard
from ui.tabs.save_mode import NonInteractiveSaveMixin
from ui.widgets.appearance_panel import AppearanceMixin
from ui.widgets.toolbar_buttons import (
    make_undo_button, make_redo_button, make_save_button, make_confirm_button,
)
from ui.tabs.scene_lifetime import adopt_editor_scene

logger = logging.getLogger(__name__)


def _png_size(path: Path):
    """(height, width) PNG из заголовка, без Qt. None если не PNG."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", head[16:24])
            return (h, w)
    except OSError:
        pass
    return None


def _pretransform_to_canvas(graph_path: Path, image_path: Path, out_path: Path,
                            contours_path: Path = None,
                            contours_unknown: bool = False) -> bool:
    """WYSIWYG: перевести граф в холст 1920x1080 (фикс-размеры + declust).

    Идемпотентно (уже-1920 граф не трогается). Пишет out_path. True при успехе.

    Это ФОЛБЭК-путь без раскладки, поэтому метка ставится с
    `layout_applied=False`: холст, собранный здесь, не должен приниматься за
    продукт раскладки (§3.6 плана).

    contours_path: скачанный contours_validated.json — выбранные контуры
    вливаются до pretransform, как у воркера (contours_merge).
    contours_unknown: скачивание сорвалось (не-404) — вливать нечего, но и
    штамповать «вливали ничего» нельзя: холст остаётся без контурной метки,
    следующее открытие с контурами честно объявит его устаревшим.
    """
    import copy

    from modules.graph.core.pretransform import pretransform
    from modules.graph.core import canvas_state
    from modules.graph.core.contours_merge import (
        load_validated_contours, merge_validated_contours, stamp_contours,
    )

    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    # Влив строго в КОПИЮ: штамп ниже обязан считаться от graph_validated,
    # каким он лежит в файле, иначе холст рождается «устаревшим»
    # (sha-проекция включает segmentation — см. contours_merge).
    contour_nodes = [] if contours_unknown \
        else load_validated_contours(contours_path)
    src = graph
    if contour_nodes:
        src = copy.deepcopy(graph)
        merged = merge_validated_contours(src, contour_nodes)
        logger.info("контуры влиты в холст: %d (выбрано %d)",
                    merged, len(contour_nodes))
    g, transform, stats = pretransform(src, image_hw=_png_size(image_path))
    # Метка источника: по ней при следующем открытии видно, что geometry
    # graph_validated изменилась (оператор возвращался на Контуры/Проверку) и
    # холст надо пересобрать. Считается по ПРОЕКЦИИ, а не по файлу: OCR и
    # привязка переписывают файл, не трогая геометрию.
    canvas_state.stamp(g, graph, layout_applied=False)
    if not contours_unknown:
        stamp_contours(g, contour_nodes)
    out_path.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    logger.info("pre-transform → холст 1920x1080: %s", stats)
    return True


def _import_text_into_canvas(canvas_path: Path, source_path: Path) -> bool:
    """Подписи и привязки из graph_validated — на холст (§3.8 плана).

    Холст считает задача раскладки ДО OCR, когда в графе нет ни одного
    text_block. Без этого импорта вкладка открылась бы вообще без подписей, а
    оператор к тому моменту уже прошёл привязку.

    Позиции — по якорям §3.9: блок остаётся у СВОЕГО элемента, а не там, где
    он был на листе. Тот же модуль подмешивает текст воркеру на экспорте.

    Правится файл во временном каталоге (скачанная копия), не артефакт на
    сервере: на сервер он уедет обычным сохранением холста.
    """
    from modules.graph.core import canvas_state, text_import

    try:
        canvas = json.loads(Path(canvas_path).read_text(encoding="utf-8"))
        source = json.loads(Path(source_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("текст на холст не импортирован (%s)", exc)
        return False

    if canvas_state.read_state(canvas)["text_edited"]:
        return False        # текст правился на холсте — он и первоисточник
    stale, reason = canvas_state.text_is_stale(canvas, source)
    if not stale:
        return False
    try:
        stats = text_import.import_text(canvas, source)
    except Exception as exc:  # noqa: BLE001 — без подписей вкладка всё равно нужна
        logger.exception("импорт текста на холст не удался: %s", exc)
        return False

    canvas.setdefault("graph", {}).setdefault(
        "canvas_transform", {})["text_imported_sha"] = \
        canvas_state.text_projection_sha(source)
    Path(canvas_path).write_text(json.dumps(canvas, ensure_ascii=False),
                                 encoding="utf-8")
    logger.info("текст импортирован на холст (%s): %s", reason, stats)
    return True


def _stub_pierces_foreign(byid, e, a_yx, b_yx) -> bool:
    """Осевой стаб a->b ([y, x]) прошивает ЧУЖУЮ рамку? Простая проверка по
    bbox (контур — консервативно его рамкой); узлы самого ребра исключены,
    касание грани (усадка 1 px) прошиванием не считается."""
    own = {e.get("source") or e.get("from"), e.get("target") or e.get("to")}
    lox, hix = sorted((float(a_yx[1]), float(b_yx[1])))
    loy, hiy = sorted((float(a_yx[0]), float(b_yx[0])))
    for nid, n in byid.items():
        if nid in own:
            continue
        bb = n.get("bbox")
        if not bb or len(bb) != 4:
            continue
        x1, y1, x2, y2 = (float(v) for v in bb)
        if lox < x2 - 1.0 and hix > x1 + 1.0 \
                and loy < y2 - 1.0 and hiy > y1 + 1.0:
            return True
    return False


def _migrate_stub_end(e, end_key, node, byid, snap=8.0) -> bool:
    """Этап A: конец С waypoints и перпендикулярным стабом -> порт узла
    ВМЕСТЕ с осевым сдвигом смежного waypoint (координата wp вдоль грани
    сдвигается на ту же величину — ортогональность сохранена). Стоп-краны:
    порт ушёл с той же грани, сдвиг ломает следующий сегмент, сдвинутый
    стаб прошил бы чужую рамку."""
    from ui.editors import port_model

    wps = e.get("waypoints") or []
    if not wps:
        return False
    p = e[end_key]
    first = end_key == "source_point"
    wp = wps[0] if first else wps[-1]
    horiz = abs(p[0] - wp[0]) <= 0.5            # стаб горизонтален ([y, x])
    px, py = port_model.choose_port(node, (p[1], p[0]), (wp[1], wp[0]), snap)
    if horiz:
        if abs(px - p[1]) > 0.75:               # порт не на той же грани
            return False
        new_p, new_wp = [py, px], [wp[0] + (py - p[0]), wp[1]]
    else:
        if abs(py - p[0]) > 0.75:
            return False
        new_p, new_wp = [py, px], [wp[0], wp[1] + (px - p[1])]
    # сдвиг не должен скосить следующий сегмент (за смежным wp)
    nxt = (wps[1] if first else wps[-2]) if len(wps) >= 2 else \
        (e.get("target_point") if first else e.get("source_point"))
    if nxt is not None:
        if horiz and abs(wp[0] - float(nxt[0])) <= 0.5:
            return False
        if not horiz and abs(wp[1] - float(nxt[1])) <= 0.5:
            return False
    if _stub_pierces_foreign(byid, e, new_p, new_wp):
        return False
    e[end_key] = new_p
    if first:
        wps[0] = new_wp
    else:
        wps[-1] = new_wp
    return True


def _materialize_ray_ends(canvas: dict, byid: dict) -> int:
    """Э1 «экран == данные»: неканоничный конец контурного узла — В ДАННЫЕ.

    До Э1 конец, лежащий не на канонической границе своего узла (внутри
    формы / в стороне — старые файлы, как правило `_manual_route`),
    дорисовывался на экране лучом из центроида (`_contour_endpoint`),
    причём в зависимости от show_skins: файл один, картинок две. Доводка
    удалена; её результат материализуется здесь один раз и честно —
    двигается только сам конец, waypoints оператора нетронуты.

    Канонической границей считается та же тройка, что у доводки:
    контур (point_on_polygon), грань bbox, граница content-rect скина.
    Узлы рамочной посадки (скин/_axis/без контура) не трогаются — их
    неканон чинит канон `seat_edge_endpoints` (кроме manual, где решает
    оператор)."""
    from modules.graph.core.edit_checks import on_rect_border
    from modules.graph.core.pretransform import (FIXED_SIZES,
                                                 _skin_content_rect,
                                                 point_on_polygon,
                                                 project_ray_to_polygon)

    edges_list = (canvas.get("links") if "links" in canvas
                  else canvas.get("edges")) or []
    moved = 0
    for e in edges_list:
        wps = e.get("waypoints") or []
        for end_key, node_key, alt_key, toward in (
                ("source_point", "source", "from",
                 wps[0] if wps else e.get("target_point")),
                ("target_point", "target", "to",
                 wps[-1] if wps else e.get("source_point"))):
            p = e.get(end_key)
            node = byid.get(e.get(node_key) or e.get(alt_key))
            if p is None or node is None or toward is None:
                continue
            seg = node.get("segmentation")
            if not seg or not isinstance(seg, list) or len(seg) < 6 \
                    or node.get("class_name") in FIXED_SIZES \
                    or node.get("_axis"):
                continue
            c = node.get("centroid")
            if not c:
                continue
            px, py = float(p[1]), float(p[0])
            if point_on_polygon(seg, px, py):
                continue
            bb = node.get("bbox")
            if bb and len(bb) == 4 and on_rect_border(bb, px, py):
                continue
            cr = _skin_content_rect(node)
            if cr and on_rect_border(cr, px, py):
                continue
            r = project_ray_to_polygon(seg, c[1], c[0], toward[1], toward[0])
            if r:
                e[end_key] = [r[1], r[0]]
                moved += 1
    return moved


def _lift_skin_ends_to_bbox(canvas: dict, byid: dict) -> int:
    """«Символ тянется на рамку» (решение 2026-08-01): конец скин-узла,
    сидящий на letterbox-грани СЕРВЕРНОГО канона (_skin_content_rect),
    поднимается наружу вдоль нормали грани на рамку bbox — редакторский
    контракт. Тангенс сохраняется (слоты целы); сдвиг идёт вдоль
    подводящего стаба, ортогональность цела по построению. Пропуск:
    _manual_route; грань letterbox == грани bbox; смежное колено ближе
    рамки (лифт перепрыгнул бы колено)."""
    from modules.graph.core.pretransform import FIXED_SIZES, _skin_content_rect

    edges_list = (canvas.get("links") if "links" in canvas
                  else canvas.get("edges")) or []
    moved = 0
    for e in edges_list:
        if e.get("_manual_route"):
            continue
        wps = e.get("waypoints") or []
        for end_key, node_key, alt_key, adj in (
                ("source_point", "source", "from",
                 wps[0] if wps else e.get("target_point")),
                ("target_point", "target", "to",
                 wps[-1] if wps else e.get("source_point"))):
            p = e.get(end_key)
            node = byid.get(e.get(node_key) or e.get(alt_key))
            if p is None or node is None \
                    or node.get("class_name") not in FIXED_SIZES:
                continue
            cr = _skin_content_rect(node)
            bb = node.get("bbox")
            if cr is None or not bb or len(bb) != 4:
                continue
            x, y = float(p[1]), float(p[0])
            cx1, cy1, cx2, cy2 = cr
            bx1, by1, bx2, by2 = (float(v) for v in bb)
            # (грань letterbox, координата рамки, ось 'x'|'y', знак наружу)
            faces = ((cx1, bx1, "x", -1), (cx2, bx2, "x", 1),
                     (cy1, by1, "y", -1), (cy2, by2, "y", 1))
            for cface, bface, axis, sign in faces:
                if abs(cface - bface) <= 0.75:
                    continue                    # letterbox == рамка
                val = x if axis == "x" else y
                tang_ok = (cy1 - 0.75 <= y <= cy2 + 0.75) if axis == "x" \
                    else (cx1 - 0.75 <= x <= cx2 + 0.75)
                if abs(val - cface) > 0.75 or not tang_ok:
                    continue
                if adj is not None:
                    av = float(adj[1]) if axis == "x" else float(adj[0])
                    # колено/дальний конец ближе рамки — лифт перепрыгнул бы
                    if sign > 0 and av < bface or sign < 0 and av > bface:
                        break
                if axis == "x":
                    e[end_key] = [y, bface]
                else:
                    e[end_key] = [bface, x]
                moved += 1
                break
    return moved


def _spread_stacked_ends(canvas: dict, byid: dict) -> int:
    """Развод СТОПОК при открытии (репро graph_edited971: три трубы w=14
    в одной точке — след толщины, применённой кодом до амнистии/членства):
    несколько АВТО-концов одного узла, слипшихся в точку (<2px),
    пересаживаются движком (слоты/участки, шаг по чернилам). Гейт «не
    хуже входа»: роутера при открытии нет, поэтому ребро, которое
    пересадка сделала бы косым, откатывается (дочинит жест/кисть).
    _manual_route и коннекторы (порт-точка) не трогаются."""
    from modules.graph.core import edit_engine
    from modules.graph.core.graph_access import is_connector

    edges_list = (canvas.get("links") if "links" in canvas
                  else canvas.get("edges")) or []
    ends_by_node = {}
    for e in edges_list:
        if e.get("_manual_route"):
            continue
        for role, end_key, node_key, alt_key in (
                ("s", "source_point", "source", "from"),
                ("t", "target_point", "target", "to")):
            nid = e.get(node_key) or e.get(alt_key)
            p = e.get(end_key)
            node = byid.get(nid)
            if p is None or node is None or is_connector(node):
                continue
            ends_by_node.setdefault(nid, []).append((e, role, end_key, p))
    moved = 0
    for nid, ends in ends_by_node.items():
        stacked = [
            (e, role, end_key, p) for e, role, end_key, p in ends
            if sum(1 for e2, _r2, _k2, p2 in ends
                   if e2 is not e and abs(p[0] - p2[0]) < 2.0
                   and abs(p[1] - p2[1]) < 2.0) > 0]
        if len(stacked) < 2:
            continue
        node = byid[nid]
        node_edges = [it[0] for it in ends]
        for e, role, end_key, p in stacked:
            wps = e.get("waypoints") or []
            ref = (wps[0] if role == "s" else wps[-1]) if wps \
                else e.get("target_point" if role == "s" else "source_point")
            if not ref:
                continue
            bak = (list(p), [list(w) for w in wps])
            x, y = edit_engine.seat_end(
                node, None, e, role, p, float(ref[1]), float(ref[0]),
                try_slack=False, snap_threshold=8.0, node_edges=node_edges)
            e[end_key] = [y, x]
            pts = [e["source_point"]] + list(e.get("waypoints") or []) \
                + [e["target_point"]]
            if any(min(abs(b[1] - a[1]), abs(b[0] - a[0])) > 1.0
                   for a, b in zip(pts, pts[1:])):
                e[end_key] = bak[0]              # косая хуже стопки — откат
                e["waypoints"] = bak[1]
            elif e[end_key] != bak[0]:
                moved += 1
    return moved


def _reseat_canvas_endpoints(canvas_path: Path) -> bool:
    """Страховка §8.3.1 EDITOR_AFTER_LAYOUT_PLAN (решение заказчика: чинить
    при открытии): пересадить концы рёбер холста по канону `seating`.

    Свежий выход раскладки каноничен by construction — для него это no-op.
    Чинится смесь контрактов посадки, которую редакторские инструменты
    создают своими дубль-реализациями, пока Э1 не сделан (замер
    tools/reseat_preview_probe.py: на старых холстах до 14.8% концов).

    Правится скачанная temp-копия (паттерн `_import_text_into_canvas`):
    undo-стек и автосейв не затрагиваются, на сервер починка уедет обычным
    сохранением оператора.

    Э5b (модель «пин на ребре», утверждена 2026-08-03): единственное
    персистентное намерение оператора — пин входа edge['pin_source'|
    'pin_target'] (локальное смещение от центроида узла). Открытие мигрирует
    легаси-хранилища ОДИН раз: node['_ports'] с владельцем-парой -> пины
    живых рёбер пары (безвладельные «подсказки судье», сироты без ребра и
    коннекторы — дроп с логом, решение Д4); _manual_route -> пины обоих
    не-коннекторных концов, флаг снимается (решение заказчика 2026-08-02).
    Закреплённый пином конец — вне канона и конкурса портов: pin-restore
    ставит его после канона и ПОСЛЕДНИМ словом прогона (доводки лифта/
    развода пин не двигают).

    Этап A (портовая модель, `ui/editors/port_model.py`): конец, сидевший на
    каталожном порту-кандидате, каноном не срывается — иначе ray-посадка
    возвращала бы «гуляние по периметру» при каждом открытии. Исключение —
    прямизна: если канон посадил конец СТРОГОЙ прямой к его ref (соосная
    пара/слабина), прямая важнее порта — «как сейчас».
    """
    from copy import deepcopy

    from modules.graph.core.graph_access import is_connector
    from modules.graph.core.pretransform import seat_edge_endpoints
    from ui.editors import port_model
    from ui.editors import port_model as _pm

    try:
        canvas = json.loads(Path(canvas_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("починка посадки концов пропущена (%s)", exc)
        return False
    edges_list = (canvas.get("links") if "links" in canvas
                  else canvas.get("edges")) or []
    byid = {n.get("id"): n for n in canvas.get("nodes") or []}

    # ─── Э5b (а): миграция node['_ports'] -> пины рёбер ───
    # Владелец-пара не различает мультирёбра — пин дублируется на все живые
    # рёбра пары (как отдавал их pinned_port, решение Д3). Уже стоящий пин
    # ребра не перетирается: он новее легаси-записи узла.
    pair_edges: dict = {}
    for e in edges_list:
        ref = _pm.edge_ref(e)
        if ref:
            pair_edges.setdefault(ref, []).append(e)
    ports_nodes = ports_migrated = ports_dropped = 0
    for n in canvas.get("nodes") or []:
        if "_ports" not in n:
            continue
        plist = n.pop("_ports") or []
        ports_nodes += 1
        for p in plist:
            ref = p.get("edge")
            live = pair_edges.get(ref) or []
            if ref is None or is_connector(n) or not live:
                ports_dropped += 1
                continue
            for e in live:
                role = _pm.pin_role(n, e)
                if role is None or _pm.edge_pin(e, role) is not None:
                    continue
                e[_pm.PIN_KEYS[role]] = {"dx": float(p.get("dx", 0.0)),
                                         "dy": float(p.get("dy", 0.0))}
                ports_migrated += 1
    if ports_nodes:
        logger.info("reseat: якоря узлов -> пины рёбер: %d перенесено, "
                    "%d отброшено (без владельца/ребра или коннектор)",
                    ports_migrated, ports_dropped)

    def _end_ref(e, end_key):
        """Ref конца ПОСЛЕ канона: смежный waypoint, иначе другой конец."""
        wps = e.get("waypoints") or []
        if end_key == "source_point":
            return wps[0] if wps else e.get("target_point")
        return wps[-1] if wps else e.get("source_point")

    snap = [deepcopy((e.get("source_point"), e.get("target_point"),
                      e.get("waypoints"))) for e in edges_list]
    seat_edge_endpoints(canvas)

    def _apply_pins():
        """ПИН ВХОДА выше канона (решение заказчика 2026-08-02): канон про
        пины не знает и сорвал бы закреплённую оператором точку при каждом
        открытии. Зовётся после канона и последним словом прогона."""
        for e in edges_list:
            for end_key, node_key in (("source_point", "source"),
                                      ("target_point", "target")):
                pin = _pm.pinned_port(byid.get(e.get(node_key)), e)
                if pin is not None:
                    e[end_key] = [pin[1], pin[0]]

    _apply_pins()
    moved = 0
    for e, (sp0, tp0, wp0) in zip(edges_list, snap):
        if e.get("_manual_route"):
            # Последний миграционный проход: геометрия ещё под защитой
            # флага, канон откатывается; пины поставит блок (б) ниже —
            # ПОСЛЕ материализации луча/лифта (точки оператора там уже
            # доведены до границы формы). Дальше флаг мёртв.
            e["source_point"] = sp0
            e["target_point"] = tp0
            if wp0 is None:
                e.pop("waypoints", None)
            else:
                e["waypoints"] = wp0
            continue
        for end_key, node_key, alt_key, orig in (
                ("source_point", "source", "from", sp0),
                ("target_point", "target", "to", tp0)):
            if _pm.edge_pin(e, node_key):
                continue        # вход закреплён пином — его вернул _apply_pins
            new = e.get(end_key)
            if orig is None or new == orig:
                continue
            node = byid.get(e.get(node_key) or e.get(alt_key))
            if node is None or not port_model.is_on_port(node, orig[1], orig[0]):
                continue
            ref = _end_ref(e, end_key)
            if ref and (abs(new[0] - ref[0]) <= 0.5
                        or abs(new[1] - ref[1]) <= 0.5):
                continue        # канон дал строгую прямую — прямизна важнее
            e[end_key] = orig   # конец остаётся в своём порту
        # Этап A, обратное направление: конец НЕ на порту мигрирует на
        # лучший порт узла (канон-луч сажает бокс->полигон в УГОЛ — скрин
        # заказчика graph_edited_33). Тот же судья, что в drag
        # (`port_model.choose_port`) — сторож == судья. Строгая прямая ПАРЫ
        # (без waypoints) неприкосновенна; конец С waypoints и
        # перпендикулярным стабом мигрирует вместе с осевым сдвигом
        # смежного waypoint (`_migrate_stub_end`), свежие холсты после
        # портовых пинов роутинга выходят уже портовыми.
        for end_key, node_key, alt_key in (("source_point", "source", "from"),
                                           ("target_point", "target", "to")):
            if _pm.edge_pin(e, node_key):
                continue        # закреплённый вход вне конкурса портов
            p = e.get(end_key)
            node = byid.get(e.get(node_key) or e.get(alt_key))
            if p is None or node is None:
                continue
            if port_model.is_on_port(node, p[1], p[0]):
                continue
            ref = _end_ref(e, end_key)
            aligned = ref is not None and (abs(p[0] - ref[0]) <= 0.5
                                           or abs(p[1] - ref[1]) <= 0.5)
            if aligned:
                if e.get("waypoints"):
                    # перпендикулярный стаб к waypoint — на порт со сдвигом
                    _migrate_stub_end(e, end_key, node, byid)
                continue        # строгая прямая пары — прямизна важнее порта
            px, py = port_model.choose_port(node, (p[1], p[0]),
                                            (ref[1], ref[0]) if ref
                                            else (p[1], p[0]), 8.0)
            e[end_key] = [py, px]
        del sp0, tp0
    _materialize_ray_ends(canvas, byid)
    _lift_skin_ends_to_bbox(canvas, byid)
    _spread_stacked_ends(canvas, byid)
    # ─── Э5b (б): заморозка _manual_route -> пины концов ───
    # Решение заказчика 2026-08-02: «маршрут руками» отменён, вход держит
    # якорь; с 2026-08-03 якорь живёт на ребре. Стоит ПОСЛЕ материализации/
    # лифта: точки оператора уже доведены до границы формы — пин фиксирует
    # именно их. «Оба конца или только реально-ручной» — код эпохи флага не
    # различал, данных нет: консервативно оба (как прежний перевод в якоря).
    # У коннектора якорить некуда — конец сразу в центроид.
    unfrozen = 0
    for e in edges_list:
        if not e.get("_manual_route"):
            continue
        for end_key, node_key in (("source_point", "source"),
                                  ("target_point", "target")):
            node = byid.get(e.get(node_key))
            pt = e.get(end_key)
            if node is None or not pt:
                continue
            if is_connector(node):
                c = node.get("centroid")
                if c:
                    e[end_key] = [float(c[0]), float(c[1])]
                continue
            _pm.set_edge_pin(node, e, node_key, float(pt[1]), float(pt[0]))
        e.pop("_manual_route", None)
        unfrozen += 1
    if unfrozen:
        logger.info("reseat: заморозка снята с %d рёбер, входы закреплены "
                    "пинами", unfrozen)
    # Последнее слово прогона: доводки выше про пины не знают и могли
    # сдвинуть закреплённый конец (луч/лифт/развод) — вернуть в пин.
    _apply_pins()
    # Э5c: редактор больше не читает _auto_route (waypoints — кэш, жест
    # ведёт все инцидентные рёбра) — флаг стирается из данных при
    # открытии; серверная подпись (avoid_router) становится инертной и
    # уходит здесь же. Счётчик держит идемпотентность (файл переписывается
    # один раз, второй прогон — no-op).
    flagged = 0
    for e in edges_list:
        if "_auto_route" in e:
            e.pop("_auto_route", None)
            flagged += 1
    # идемпотентность: канон и редакторские доводки (лифт скинов) могут
    # взаимно компенсироваться — считаем итог против файла, не по ходу
    moved = sum(
        (e.get("source_point") != s0) + (e.get("target_point") != t0)
        for e, (s0, t0, _w0) in zip(edges_list, snap))
    moved += flagged + unfrozen + ports_nodes
    if not moved:
        return False
    Path(canvas_path).write_text(json.dumps(canvas, ensure_ascii=False),
                                 encoding="utf-8")
    logger.info("посадка концов при открытии: пересажено %d концов", moved)
    return True


def _canvas_has_layout(canvas_path: Path) -> bool:
    """Холст — продукт авто-раскладки, а не pretransform-фолбэка."""
    from modules.graph.core import canvas_state

    try:
        canvas = json.loads(Path(canvas_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return canvas_state.has_layout(canvas)


def _canvas_is_stale(canvas_path: Path, source_path: Path,
                     download_failed: bool = False) -> bool:
    """Холст устарел, если геометрия graph_validated изменилась после сборки.

    Смёржить их нельзя — pretransform необратим, поэтому устаревший холст
    пересобирается с нуля (правки оператора в нём теряются).

    Считает общий модуль `modules.graph.core.canvas_state` — тот же, что зовёт
    воркер. Две реализации канона = расходящиеся sha = ложное «устарело».

    download_failed: graph_validated не скачался по НЕ-404 (сеть, 5xx) — тогда
    `source_path` это ФОЛБЭК `graph_json`, а не источник холста, и сверка с ним
    врёт: свежий холст объявляется устаревшим, а модалка «Схема изменилась»
    называет причину, которой не было (замер §68.3). Судить нечем — холст
    остаётся оператору: цена ложного «устарел» — выброшенная ручная раскладка.
    То же различение, что у `_canvas_contours_stale` (пункт 0.5).
    """
    if download_failed:
        return False
    from modules.graph.core import canvas_state

    try:
        canvas = json.loads(Path(canvas_path).read_text(encoding="utf-8"))
        source = json.loads(Path(source_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    stale, reason = canvas_state.is_stale(canvas, source)
    if stale:
        logger.info("холст устарел: %s", reason)
    return stale


def _canvas_contours_stale(canvas_path: Path, contours_path,
                           download_failed: bool = False) -> bool:
    """Выбранные контуры менялись после сборки холста → пересборка.

    Отдельно от `_canvas_is_stale`: контуры не входят в sha-проекцию графа
    (см. contours_merge). Ошибка чтения холста = устарел (как в соседе).

    download_failed: contours_validated не скачался по НЕ-404 (сеть, 5xx) —
    состояние контуров неизвестно, инвалидировать холст нельзя: цена ложного
    «устарел» — выброшенные правки оператора.
    """
    if download_failed:
        return False
    from modules.graph.core.contours_merge import (
        contours_are_stale, load_validated_contours,
    )

    try:
        canvas = json.loads(Path(canvas_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    stale, reason = contours_are_stale(
        canvas, load_validated_contours(contours_path))
    if stale:
        logger.info("холст устарел по контурам: %s", reason)
    return stale


def _graph_jobs(want_canvas: bool) -> tuple[Job, ...]:
    """Что вкладка редактора графа тянет с сервера.

    ⛔ `swallow=(APIError,)` у необязательных — не косметика: не-APIError (диск,
    прокси) обязан увести вкладку в ошибку, потому что тихо потерянный
    `graph_canvas` = «холст пересобран заново, прежние правки не сохранятся»,
    а тихо потерянные контуры = выброшенные правки оператора (см. `_on_downloaded`).

    ⛔ `failure_key` у графа и холста (пункт 1.23) — то же различение 404/прочее,
    что у контуров: 404 = сохранённой работы законно нет (первый заход), любой
    другой отказ = она могла быть. Молча взять фолбэк = открыть оператору не его
    работу, а `_save_graph` затрёт ею серверную.
    """
    jobs = [
        one(artifact("original_image", "original.png"), required=True),
        # Граф: предпочитаем validated (сохранённый), fallback на оригинальный.
        Job((artifact("graph_validated", "graph.json", key="graph_json"),
             artifact("graph_json", "graph.json")), required=True,
            failure_key="saved_graph_download_failed"),
        one(artifact("coco_validated", "coco_validated.json"),
            swallow=(APIError,),
            silent_ok="COCO обратно на сервер графовая вкладка не пишет: "
                      "потеря стоит оператору рамок узлов на подложке, "
                      "а не его работы"),
    ]
    # WYSIWYG-вкладка: свой артефакт-холст, если он уже сохранялся,
    # плюс выбранные контуры — их вливает пересборка холста (фолбэк).
    if want_canvas:
        jobs += [
            one(artifact("graph_canvas", "graph_canvas.json"),
                swallow=(APIError,),
                failure_key="canvas_download_failed"),
            one(artifact("contours_validated", "contours_validated.json"),
                swallow=(APIError,),
                failure_key="contours_download_failed"),
        ]
    return tuple(jobs)



class BaseGraphTab(BlindOverwriteGuard, NonInteractiveSaveMixin,
                   AppearanceMixin, QWidget):
    """Базовый класс вкладки редактора графа P&ID.

    Template method:
      - скачивание артефактов через ArtifactDownloader (список — _graph_jobs)
      - _setup_ui() → toolbar (из _setup_toolbar) + loading + status
      - _save_graph() → editor.save_graph + upload_validated_graph
      - _on_confirm() → безусловный save + emit confirmed
      - has_unsaved_changes → undo_mgr.revision != _saved_revision

    Потомки обязаны реализовать:
      _create_editor() → BaseGraphEditor
      _setup_toolbar(toolbar: QHBoxLayout)
    """

    # WYSIWYG: True → граф переводится в холст 1920x1080 (фикс-размеры).
    # False → вкладка работает в ОРИГИНАЛЬНЫХ координатах (Проверка схемы и др.).
    USE_CANVAS = False

    confirmed = Signal()           # Пользователь подтвердил
    status_message = Signal(str)   # Сообщение для статусбара workspace

    @staticmethod
    def _add_separator(toolbar: QHBoxLayout):
        """Добавить визуальный разделитель в toolbar."""
        sep = QLabel(" | ")
        sep.setStyleSheet("color: #666;")
        toolbar.addWidget(sep)

    def __init__(
        self,
        diagram_uid: str,
        diagram_name: str,
        api_client: APIClient,
        parent: Optional[QWidget] = None,
    ):
        if type(self) is BaseGraphTab:
            raise TypeError(
                "BaseGraphTab is abstract, use SimpleGraphTab or AdvancedGraphTab"
            )
        super().__init__(parent)

        self.uid = diagram_uid
        self.diagram_name = diagram_name
        self.api_client = api_client

        self._temp_dir_obj = tempfile.TemporaryDirectory(prefix="pid_graph_")
        self.temp_dir = Path(self._temp_dir_obj.name)

        self._editor: Optional[BaseGraphEditor] = None
        # undo_mgr.revision на момент последнего успешного save
        self._saved_revision: int = 0
        # Артефакты, чьё состояние на сервере НЕИЗВЕСТНО: скачать не удалось
        # не по 404. Запись в них ждёт явного «да» оператора (1.x9).
        self._init_blind_overwrite()

        self._setup_ui()
        self._download_artifacts()

    # =================================================================
    # Abstract interface
    # =================================================================

    @abstractmethod
    def _create_editor(self) -> BaseGraphEditor:
        """Создать конкретный экземпляр редактора."""
        ...

    @abstractmethod
    def _setup_toolbar(self, toolbar: QHBoxLayout) -> None:
        """Наполнить toolbar кнопками режимов и инструментов."""
        ...

    def _setup_secondary_toolbar(self, layout: QVBoxLayout) -> None:
        """Опциональный второй ряд тулбара. По умолчанию ничего не добавляет.

        Потомки могут переопределить и добавить второй QHBoxLayout в layout
        (он встаёт сразу под основным рядом кнопок).
        """
        return

    # =================================================================
    # UI Setup
    # =================================================================

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # === Toolbar (одна строка, авто-подгонка шрифта/кнопок под ширину) ===
        toolbar_container = QWidget()
        toolbar = QHBoxLayout(toolbar_container)
        toolbar.setContentsMargins(8, 4, 8, 4)
        toolbar.setSpacing(8)

        # --- Undo / Redo (единые; справа от ⚙ после инжекта Назад/⚙) ---
        self.btn_undo = make_undo_button(self._undo)
        toolbar.addWidget(self.btn_undo)
        self.btn_redo = make_redo_button(self._redo)
        toolbar.addWidget(self.btn_redo)

        # Потомок заполняет toolbar
        self._setup_toolbar(toolbar)

        toolbar.addStretch()

        # --- Save (единая) рядом с Подтвердить ---
        self.btn_save = make_save_button(self._save_graph, "Сохранить граф на сервер (Ctrl+S)")
        toolbar.addWidget(self.btn_save)

        # --- Confirm (единая) ---
        self.btn_confirm = make_confirm_button(
            self._on_confirm,
            tooltip="Сохранить и подтвердить граф.\nПереход к следующему этапу.",
        )
        toolbar.addWidget(self.btn_confirm)

        layout.addWidget(toolbar_container)
        from ui.widgets.responsive_toolbar import install_responsive_toolbar
        self._toolbar_responsive = install_responsive_toolbar(toolbar_container)

        # Опциональный второй ряд тулбара (потомки могут наполнить)
        self._setup_secondary_toolbar(layout)

        # === Loading placeholder ===
        self.loading_label = QLabel("Загрузка артефактов...")
        self.loading_label.setAlignment(Qt.AlignCenter)
        self.loading_label.setStyleSheet("font-size: 18px; color: #666;")
        layout.addWidget(self.loading_label)

        # Сохраняем ссылку для вставки редактора
        self._editor_layout = layout

        # === Status ===
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(
            "color: #888; font-size: 11px; padding: 2px 8px;"
        )
        layout.addWidget(self.status_label)

    # =================================================================
    # Download
    # =================================================================

    def _download_artifacts(self):
        self._download_thread = QThread()
        self._downloader = ArtifactDownloader(
            self.api_client, self.uid, self.temp_dir,
            _graph_jobs(want_canvas=self.USE_CANVAS)
        )
        self._downloader.moveToThread(self._download_thread)
        self._download_thread.started.connect(self._downloader.run)
        self._downloader.finished.connect(self._on_downloaded)
        self._downloader.error.connect(self._on_download_error)
        self._downloader.progress.connect(self._on_download_progress)
        # Гасит поток САМ поток, а не слот вкладки. Связи со слотами Qt рвёт
        # вместе с получателем, а вкладку РАЗРУШАЮТ («← Назад» →
        # `_remove_tab_widget`), причём `cleanup()` не зовёт ни один путь
        # закрытия. Уйти из вкладки до конца загрузки — значит оставить
        # бегущий `QThread`, разрушение которого роняет процесс (пункт 1.x17).
        self._downloader.finished.connect(self._download_thread.quit)
        self._downloader.error.connect(self._download_thread.quit)
        # ⛔ И уносит СЕБЯ САМ, в СВОЁМ потоке (пункт 1-46, замер §119а):
        # вкладку РАЗРУШАЮТ, а рабочий объект держит только её словарь —
        # без этого он остаётся жить сиротой в потоке, которого больше нет,
        # и снести его сможет лишь питоний сборщик и лишь ЧУЖИМ потоком.
        # Взводить снос ПОСЛЕ конца потока бесполезно: `deleteLater()` для
        # объекта в кончившемся потоке не доставляется вовсе (замер §119б).
        self._downloader.finished.connect(self._downloader.deleteLater)
        self._downloader.error.connect(self._downloader.deleteLater)
        # ⛔ И, наконец, поток обязан КОНЧИТЬСЯ раньше, чем процесс начнёт
        # разрушать объекты (пункт 1-50, замер §127): гашение выше
        # срабатывает только когда работник ДОРАБОТАЛ, а на молчащем
        # сервере он не дорабатывает вовсе — уход из вкладки давал тогда
        # 4 краха процесса из 4 (`0xC0000409`).
        hand_over(self, self._download_thread, self._downloader,
                  name="загрузка графовой вкладки", uid=self.uid)
        self._download_thread.start()

    @Slot(str)
    def _on_download_progress(self, msg: str):
        """Ход загрузки — в GUI-потоке (пункт 1.x17).

        Раньше здесь стояла лямбда, связанная БЕЗ получателя-`QObject`:
        такая связь принадлежит отправителю, а отправитель переехал
        `moveToThread` в рабочий поток — то есть `setText` красил виджет
        оттуда, на каждом артефакте. Со `@Slot`-ом вкладки
        `Qt.AutoConnection` разворачивается в очередь GUI-потока, и связь
        заодно рвётся вместе с разрушенной вкладкой.
        """
        self.status_label.setText(msg)

    @Slot(dict)
    def _on_downloaded(self, artifacts: dict):
        self._download_thread.quit()
        self._download_thread.wait()
        self.loading_label.hide()

        # Не-404 при загрузке = на сервере МОГЛА лежать работа оператора,
        # которую прочитать не удалось, а открытое собрано вслепую. Запись
        # в такой артефакт запирается до явного «да» (1.x9): предупреждения
        # 1.23 записи не мешали, и первый же save затирал непрочитанное.
        for flag, name in (("saved_graph_download_failed", "graph_validated"),
                           ("canvas_download_failed", "graph_canvas")):
            if artifacts.get(flag):
                self._unreadable_on_server.add(name)

        if artifacts.get("saved_graph_download_failed"):
            # Сохранённый граф МОГ лежать на сервере и просто не отдался (5xx,
            # сеть, диск): открыт исходный, а `_save_graph` пишет обратно
            # в graph_validated — прежняя валидация была бы затёрта молча.
            logger.warning("graph_validated не скачался — открыт исходный граф")
            QMessageBox.warning(
                self, "Сохранённый граф не загружен",
                "Не удалось скачать сохранённый граф — открыт ИСХОДНЫЙ, "
                "без ваших прежних правок.\n\n"
                "Сохранение из этой вкладки затрёт сохранённый граф на "
                "сервере. Закройте вкладку и откройте её заново, когда связь "
                "восстановится.",
            )

        try:
            # Остаток раскладки (Э12): показывает только AdvancedGraphTab,
            # путь сохраняется здесь — artifacts дальше не передаются.
            editor = self._create_editor()
            editor.status_callback = lambda msg: self.status_label.setText(msg)
            editor.stats_callback = self._update_stats
            editor.mode_callback = self._on_mode_changed
            editor.save_requested_callback = self._save_from_hotkey
            # WYSIWYG: pre-transform графа в холст 1920x1080 перед загрузкой.
            # При неудаче — грузим как есть (граф в исходных координатах).
            graph_for_editor = artifacts["graph_json"]
            if self.USE_CANVAS:
                # WYSIWYG-вкладка (Ручная правка): работаем в холсте 1920x1080.
                # Вкладки в оригинале (Проверка схемы и др.) сюда не заходят —
                # иначе они бы сконвертили граф и автосейв залил бы 1920.
                try:
                    saved = artifacts.get("graph_canvas")
                    # graph_json — это ФОЛБЭК, когда сохранённый граф не отдался:
                    # ни судить по нему о свежести холста, ни подмешивать из
                    # него подписи нельзя — источник не прочитан (1.x9).
                    source_unknown = bool(
                        artifacts.get("saved_graph_download_failed"))
                    if saved and not _canvas_is_stale(
                                saved, artifacts["graph_json"],
                                download_failed=source_unknown) \
                            and not _canvas_contours_stale(
                                saved, artifacts.get("contours_validated"),
                                download_failed=bool(artifacts.get(
                                    "contours_download_failed"))):
                        # Холст актуален — грузим правки оператора как есть
                        if not source_unknown:
                            _import_text_into_canvas(
                                Path(saved), Path(artifacts["graph_json"]))
                        # §8.3.1: посадка концов чинится при каждом открытии
                        # (на каноничном холсте — no-op).
                        _reseat_canvas_endpoints(Path(saved))
                        graph_for_editor = saved
                        editor._canvas_mode = True
                        # Холст с раскладкой узлы переставил, а подложка — это
                        # исходный растр: она больше не система отсчёта и по
                        # умолчанию прячется (включается в панели вида).
                        editor._bg_visible = not _canvas_has_layout(Path(saved))
                        logger.info("Загружен сохранённый холст graph_canvas")
                    else:
                        if saved:
                            # Схема правилась на предыдущих вкладках. Смёржить нельзя
                            # (pretransform необратим) — пересобираем холст с нуля.
                            logger.info("graph_canvas устарел → пересборка из graph_validated")
                            QMessageBox.information(
                                self, "Схема изменилась",
                                "Схема правилась после «Ручной правки» "
                                "(контуры/OCR/проверка схемы).\n\n"
                                "Холст будет пересобран заново — прежние правки "
                                "в нём не сохранятся.",
                            )
                        elif artifacts.get("canvas_download_failed"):
                            # Холст МОГ лежать на сервере и просто не отдался:
                            # пустота от 5xx неотличима от «холста нет», а цена
                            # ошибки — ручная раскладка, затёртая первым Ctrl+S.
                            logger.warning(
                                "graph_canvas не скачался — холст пересобран заново")
                            QMessageBox.warning(
                                self, "Холст не загружен",
                                "Не удалось скачать сохранённый холст «Ручной "
                                "правки» — он будет пересобран заново, прежние "
                                "правки в нём не сохранятся.\n\n"
                                "Сохранение из этой вкладки затрёт холст на "
                                "сервере. Закройте вкладку и откройте её "
                                "заново, когда связь восстановится.",
                            )
                        canvas_graph = self.temp_dir / "graph_1920.json"
                        if _pretransform_to_canvas(
                            Path(artifacts["graph_json"]),
                            Path(artifacts["original_image"]),
                            canvas_graph,
                            contours_path=artifacts.get("contours_validated"),
                            contours_unknown=bool(artifacts.get(
                                "contours_download_failed")),
                        ):
                            graph_for_editor = canvas_graph
                            editor._canvas_mode = True   # сцена в холсте 1920x1080
                except Exception as exc:
                    # Не фатально: грузим граф в исходных координатах (legacy).
                    logger.exception("pre-transform не выполнен, гружу как есть: %s", exc)

            editor.load_data(
                image_path=str(artifacts["original_image"]),
                graph_path=str(graph_for_editor),
                coco_path=str(artifacts.get("coco_validated", "")),
            )
            # Вставить редактор перед status_label (последний виджет)
            self._editor_layout.insertWidget(
                self._editor_layout.count() - 1, editor
            )
            self._editor = editor  # присвоить только после успеха
            adopt_editor_scene(editor)   # сцена умирает с виджетом (1-46)
            self.status_label.setText("Граф загружен")
            self.apply_saved_appearance()
            self._on_editor_ready()
        except Exception as exc:
            logger.error("Failed to init graph editor: %s", exc, exc_info=True)
            QMessageBox.critical(
                self, "Ошибка",
                f"Не удалось инициализировать редактор графа:\n{exc}"
            )

    def _appearance_editor(self):
        return self._editor

    def _on_light_sheet_toggled(self, on: bool):
        """Светлый лист + тёмные рёбра (как в САПР) или прежний тёмный вид."""
        from ui.services.ui_settings import UISettings

        UISettings.instance().set_appearance(self.uid, "light_sheet",
                                             1 if on else 0)
        if self._editor is not None:
            self._editor.set_light_theme(on)

    def _build_appearance_controls(self, panel):
        from PySide6.QtGui import QColor
        from ui.services.ui_settings import UISettings

        # Подложка после раскладки не соответствует графу и скрыта по
        # умолчанию; включают её, чтобы свериться с оригиналом и прочитать
        # текст. Затемнение имеет смысл только при включённой подложке.
        _s = UISettings.instance()
        panel.add_checkbox(
            "Показать подложку",
            bool(getattr(self._editor, "_bg_visible", True)),
            lambda on: self._editor and self._editor.set_background_visible(on),
        )
        panel.add_checkbox(
            "Светлый лист",
            bool(_s.get_appearance(self.uid, "light_sheet", 1)),
            self._on_light_sheet_toggled,
        )
        self._add_bg_darkness_slider(panel)
        self._add_color_setting(
            panel, "Цвет рёбер", "edge_color",
            QColor(self._editor.COLOR_EDGE_DEFAULT if self._editor
                   else BaseGraphEditor.COLOR_EDGE_DEFAULT),
            lambda c: self._editor and self._editor.set_edge_color(c),
        )
        # Субъективные размеры (только визуал). Наследуются «Контурами».
        self._add_size_setting(
            panel, "Размер коннекторов", "size_connector",
            lambda f: self._set_editor_size("CONNECTOR_DRAW_RADIUS", f),
        )
        self._add_size_setting(
            panel, "Толщина рамки боксов", "size_outline",
            lambda f: self._set_editor_size("OUTLINE_WIDTH", f),
        )
        # П8: подсветка стороны блока, где есть подключение. По умолчанию — вкл.
        self._add_flag_setting(
            panel, "Подсветка сторон с подключением", "side_marks", True,
            lambda v: self._editor and self._editor.set_side_marks_visible(v),
        )
        self._add_color_setting(
            panel, "Цвет подсветки сторон", "side_mark_color",
            QColor(self._editor.COLOR_EQUIPMENT if self._editor else "#3498db"),
            lambda c: self._editor and self._editor.set_side_mark_color(c),
        )

    def _set_editor_size(self, key: str, factor: float):
        ed = self._editor
        if ed is not None and hasattr(ed, "set_size_factor"):
            ed.set_size_factor(key, factor)

    def apply_saved_appearance(self):
        super().apply_saved_appearance()
        ed = self._editor
        if ed is None:
            return
        from PySide6.QtGui import QColor
        from ui.services.ui_settings import UISettings
        # Тема — ПЕРЕД цветом рёбер: она пересобирает сцену, а цвет рёбер
        # применяется после и остаётся главнее. (Сам цвет от темы уже не
        # зависит — базовый кислотно-зелёный виден и на белом листе.)
        if hasattr(ed, "set_light_theme"):
            ed.set_light_theme(
                bool(UISettings.instance().get_appearance(
                    self.uid, "light_sheet", 1)))
        if hasattr(ed, "set_edge_color"):
            self._apply_saved_color("edge_color", QColor(ed.COLOR_EDGE_DEFAULT),
                                    ed.set_edge_color)
        if hasattr(ed, "set_size_factor"):
            self._apply_saved_size(
                "size_connector", lambda f: ed.set_size_factor("CONNECTOR_DRAW_RADIUS", f))
            self._apply_saved_size(
                "size_outline", lambda f: ed.set_size_factor("OUTLINE_WIDTH", f))
        if hasattr(ed, "set_side_marks_visible"):
            self._apply_saved_flag("side_marks", True, ed.set_side_marks_visible)
            self._apply_saved_color("side_mark_color", QColor(ed.COLOR_EQUIPMENT),
                                    ed.set_side_mark_color)

    def apply_default_appearance(self):
        super().apply_default_appearance()
        ed = self._editor
        if ed is None:
            return
        if hasattr(ed, "set_edge_color"):
            ed.set_edge_color(None)          # None → базовый цвет редактора
        # Общий сброс обязан вернуть и размерные регуляторы (иначе T6 красный).
        if hasattr(ed, "reset_size_factors"):
            ed.reset_size_factors()
        if hasattr(ed, "set_side_marks_visible"):
            ed.set_side_mark_color(None)      # None → цвет узла
            ed.set_side_marks_visible(True)

    @Slot(str)
    def _on_download_error(self, error_msg: str):
        self._download_thread.quit()
        self._download_thread.wait()
        self.loading_label.setText(f"Ошибка: {error_msg}")

    def _on_editor_ready(self):
        """Хук: вызывается сразу после успешной загрузки редактора.

        Потомки могут переопределить для дополнительной инициализации.
        """

    # =================================================================
    # Stats callbacks
    # =================================================================

    def _update_stats(self, stats: dict):
        """Callback от редактора. Счётчик в toolbar убран — оставлено для совместимости."""
        return

    # =================================================================
    # Common mode/tool helpers
    # =================================================================

    def _set_mode(self, mode: str):
        """Установить режим редактора по строковому ключу."""
        if self._editor:
            self._editor.set_mode(mode)

    def _toggle_mode(self, mode: str):
        """Клик оператора по кнопке-инструменту: повторный клик по активной
        кнопке выводит в `idle`, а не перезапускает инструмент (блок 7.1).

        ⛔ Toggle стоит ЗДЕСЬ, а не ранним выходом в `BaseGraphEditor.set_mode`:
        на переисполнение того же режима завязаны
        `tests/ui/test_mode_exit_finishes_gesture.py` и
        `SimpleGraphEditor._enter_resize_mode`, а программные повторные входы
        (`AdvancedGraphTab._apply_regime_ui`, `_on_edge_color_selected`) зовут
        `_set_mode` напрямую и под toggle попадать не должны.

        Спрашивается состояние КНОПКИ уже после Qt — тот же приём, что у
        эталона рядом (`AdvancedGraphTab._set_regime`): группа неэксклюзивна,
        поэтому клик по нажатой кнопке её отжимает, и «отжали» = «выйти».
        """
        btn = self._get_mode_button_map().get(mode)
        self._set_mode(mode if (btn is None or btn.isChecked()) else "idle")

    def _on_mode_changed(self, mode: str):
        """Callback от editor при смене режима — синхронизировать кнопки toolbar.

        Снимает checked со всех кнопок mode_group.
        Потомки переопределяют _get_mode_button_map() для автоматической активации.

        Группа неэксклюзивна по построению (7.1), поэтому снимать и возвращать
        `setExclusive` здесь больше незачем: «одна нажата за раз» держит этот
        обработчик, а не Qt.
        """
        if not hasattr(self, 'mode_group'):
            return
        btn_map = self._get_mode_button_map()
        for btn in self.mode_group.buttons():
            btn.setChecked(False)
        # Активируем нужную кнопку если есть маппинг
        target_btn = btn_map.get(mode)
        if target_btn:
            target_btn.setChecked(True)

    def _get_mode_button_map(self) -> dict:
        """Маппинг mode_name → QPushButton. Потомки переопределяют."""
        return {}

    def _undo(self):
        if self._editor:
            self._editor.undo()

    def _redo(self):
        if self._editor:
            self._editor.redo()

    # =================================================================
    # Save & Confirm
    # =================================================================

    def has_unsaved_changes(self) -> bool:
        """True если есть несохранённые изменения после последнего save."""
        if self._editor:
            return self._editor.undo_mgr.revision != self._saved_revision
        return False

    #: артефакт → о чём предупредить, если писать в него придётся вслепую
    _BLIND_WRITE_WARNING = {
        "graph_canvas":
            "Сохранённый холст «Ручной правки» не удалось скачать — открытый "
            "собран заново, без прежних правок.\n\n"
            "Сохранение затрёт на сервере холст, в котором могла остаться "
            "ваша ручная раскладка.",
        "graph_validated":
            "Сохранённый граф не удалось скачать — открыт ИСХОДНЫЙ, без ваших "
            "прежних правок.\n\n"
            "Сохранение затрёт на сервере сохранённый граф, в котором могла "
            "остаться ваша прежняя валидация.",
    }

    # `_confirm_blind_overwrite` — общая дверь `BlindOverwriteGuard`
    # (`ui/tabs/blind_overwrite.py`, пункт 1-41). Сюда сходятся все боевые
    # входы на запись: кнопка 💾, «Подтвердить» и автосохранение; Ctrl+S
    # редактора с 1.19 жмёт ту же кнопку 💾 (`_save_from_hotkey`), то есть
    # тоже приходит сюда, а не мимо.

    def _save_from_hotkey(self):
        """Ctrl+S редактора = нажатие кнопки 💾, а не отдельный путь записи.

        Через саму кнопку, а не через `_save_graph`, потому что её состояние
        И ЕСТЬ право на сохранение: `AdvancedGraphTab` гасит её на время
        распознавания (гонка с фоновым потоком — иначе на сервер уйдёт граф
        без распознанного текста), а `ContourTab` прячет вовсе — там
        сохранение идёт «Подтвердить». `click()` на выключенной кнопке —
        no-op, поэтому отдельной проверки `isEnabled()` не нужно.
        """
        if self.btn_save.isHidden():
            self.status_label.setText("Сохранение — кнопкой «Подтвердить»")
            return
        self.btn_save.click()

    def _save_graph(self) -> bool:
        """Сохранить граф на сервер. Возвращает True при успехе."""
        if not self._editor:
            return False

        # Буфер уже помечен несвежим гейтом пересборки — писать некуда.
        if self.save_blocked_reason:
            self.status_label.setText(self.save_blocked_reason)
            return False

        # Куда пишем — тем и определяется, что под угрозой: холст уходит
        # в graph_canvas, вкладки в оригинальных координатах — в graph_validated.
        artifact = ("graph_canvas"
                    if getattr(self._editor, "_canvas_mode", False)
                    else "graph_validated")
        if not self._confirm_blind_overwrite(artifact):
            self.status_label.setText("Сохранение отменено")
            return False

        lifted_preview = None
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)

            # На сервер уходит ровно то, что считает дёрти-флаг: живое превью
            # идёт мимо стека команд, флаг его не видит, а оператор не
            # подтверждал (1.5). Без снятия сервер расходился с моделью —
            # превью уезжало заливкой, а выход из режима откатывал его в модели.
            #
            # ⛔ Но снять превью НАСОВСЕМ может только ЖЕСТ. Тик таймера жестом
            # не является (1-38), а путь записи у него общий с кнопкой: раз
            # в 120 с фоновый тик заходил сюда и стирал размер, который оператор
            # в эту минуту подбирал бегунком, — без единого его действия и без
            # следа в стеке (§83.32). Поэтому по таймеру превью снимается только
            # НА ВРЕМЯ записи и возвращается в `finally`. Инвариант 1.5 при этом
            # не ослаблен ни на шаг: серверу и там, и там достаётся ровно
            # зафиксированное состояние.
            if self._save_interactive:
                self._editor.drop_uncommitted_preview()
            else:
                lifted_preview = self._editor.take_uncommitted_preview()

            # Холст пишется в свой артефакт: graph_validated принадлежит вкладкам
            # в оригинальных координатах и затирать его 1920-графом нельзя.
            graph_path = self.temp_dir / f"{artifact}.json"
            if not self._editor.save_graph(str(graph_path)):
                self.status_label.setText("Не удалось сохранить локально")
                return False

            self.status_label.setText("Загрузка графа на сервер...")
            if artifact == "graph_canvas":
                self.api_client.upload_canvas_graph(self.uid, graph_path)
            else:
                self.api_client.upload_validated_graph(self.uid, graph_path)

            self._saved_revision = self._editor.undo_mgr.revision
            self.status_label.setText("Граф сохранён")
            return True

        except Exception as exc:
            # Гейт пересборки — не «не удалось», а «больше некуда»: буфер
            # мёртв, и говорить о нём надо иначе (блок 5).
            banner = self.rebuild_banner(exc)
            # По таймеру — строкой, а не модалкой посреди работы (1-38).
            if self._save_interactive:
                QMessageBox.warning(
                    self, "Схема пересобрана" if banner else "Ошибка",
                    banner or f"Не удалось сохранить граф:\n{exc}"
                )
            else:
                self._refuse_save(
                    banner or f"⚠️ Автосохранение не удалось: {exc}")
            if banner:
                self.status_label.setText(banner)
            return False
        finally:
            QApplication.restoreOverrideCursor()
            # Превью, снятое ради записи по таймеру, — обратно на холст
            # (в `finally`: отказ записи не должен стоить оператору работы).
            self._editor.restore_uncommitted_preview(lifted_preview)

    @Slot()
    def _on_confirm(self):
        """Подтвердить: безусловно сохранить + emit confirmed.

        Save не гейтится дырти-флагом: он может ложно давать «нет изменений»,
        а без свежего graph_validated сервер сгенерирует FXML из устаревшего
        графа. Лишний POST дёшев — страхует от потери правок.
        """
        if self.has_unsaved_changes():
            reply = QMessageBox.question(
                self, "Сохранение",
                "Несохранённые изменения будут сохранены. Продолжить?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        if not self._save_graph():
            return

        self.status_message.emit("Граф подтверждён")
        self.confirmed.emit()

    # =================================================================
    # Cleanup
    # =================================================================

    def cleanup(self):
        """Остановить фоновую загрузку и освободить ресурсы."""
        if hasattr(self, "_download_thread") and self._download_thread.isRunning():
            self._download_thread.quit()
            self._download_thread.wait(3000)
        if hasattr(self, "_temp_dir_obj"):
            self._temp_dir_obj.cleanup()

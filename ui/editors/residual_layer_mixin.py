"""Residual Layer Mixin — маркеры остаточных очагов раскладки (Э12).

Подмешивается в AdvancedGraphEditor. Данные приходят из артефакта
`residual_defects.json` (пишет задача раскладки, см.
`modules/graph/core/layout/residual.py`); вкладка передаёт их через
`set_residual_defects()` только для холста с совпадающим `canvas_sha`.

Маркер — нумерованное красное кольцо на Z=9 (поверх всех слоёв редактора,
см. карту Z в base_graph_editor.setup_scene). Позиция маркера живая — очаг
едет за правкой оператора: невидимая труба — середина ТЕКУЩИХ нарисованных
концов ребра (не центроидов: у крупного контура центроид в сотнях px от
щели), остальные виды — от item'ов узлов; точка из файла — фолбэк для
удалённых рёбер/узлов.

`_redraw_all` базового редактора сносит все item'ы с Z>0 — слой пересоздаётся
в `AdvancedGraphEditor._redraw_all` (тот же контракт, что у OCR-слоя).
"""

from PySide6.QtCore import QPointF
from PySide6.QtGui import QBrush, QColor, QFont, QPen
from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsSimpleTextItem

_MARKER_R = 12.0
_MARKER_Z = 9.0
_COLOR = QColor(255, 59, 48)
_FILL = QColor(255, 59, 48, 56)
_FOCUS_SCALE = 1.5     # масштаб перехода-зума к очагу

_KIND_LABELS = {
    "invisible_edge": "невидимая труба",
    "box_on_magi": "бокс на чужой трубе",
    "overlap": "наложение блоков",
}


class ResidualLayerMixin:
    """Слой очагов остатка. Не перехватывает мышь — только отрисовка и зум."""

    def _init_residual_layer(self):
        self._residual_spots: list[dict] = []
        self._residual_items: list = []
        self._residual_visible = True

    # -----------------------------------------------------------------
    # Данные
    # -----------------------------------------------------------------
    def set_residual_defects(self, data: dict):
        """Принять содержимое residual_defects.json и отрисовать маркеры."""
        spots = []
        for rec in data.get("invisible_edges") or []:
            spots.append({
                "kind": "invisible_edge",
                "node_ids": [n for n in (rec.get("nodes") or []) if n],
                "point": rec.get("point"),
                "descr": f"зазор {rec.get('gap')} px",
            })
        for rec in data.get("box_on_magi") or []:
            nid = rec.get("node_id")
            spots.append({
                "kind": "box_on_magi",
                "node_ids": [nid] if nid else [],
                "point": rec.get("point"),
                "descr": str(nid or ""),
            })
        for rec in data.get("overlaps") or []:
            spots.append({
                "kind": "overlap",
                "node_ids": [n for n in (rec.get("nodes") or []) if n],
                "point": rec.get("point"),
                "descr": " × ".join(rec.get("nodes") or []),
            })
        self._residual_spots = spots
        self._redraw_residual_markers()

    def clear_residual_defects(self):
        self._residual_spots = []
        self._redraw_residual_markers()

    def residual_spots(self):
        """Для панели-списка: [(номер, вид, описание)]."""
        return [(i + 1, _KIND_LABELS.get(s["kind"], s["kind"]), s["descr"])
                for i, s in enumerate(self._residual_spots)]

    def set_residual_visible(self, visible: bool):
        self._residual_visible = bool(visible)
        self._redraw_residual_markers()

    # -----------------------------------------------------------------
    # Отрисовка
    # -----------------------------------------------------------------
    def _residual_scene_point(self, spot):
        # Невидимая труба: очаг — щель между ФОРМАМИ, живая точка — середина
        # текущих нарисованных концов ребра. Целиться по центроидам нельзя:
        # у крупного контура центроид лежит в сотнях px от щели.
        if spot["kind"] == "invisible_edge" and len(spot["node_ids"]) == 2:
            model = getattr(self, "model", None)
            if model is not None:
                e = model.find_edge_data(model.edge_key(*spot["node_ids"]))
                if e:
                    sp, tp = e.get("source_point"), e.get("target_point")
                    if sp and tp:   # [y,x] → [x,y]
                        return ((sp[1] + tp[1]) / 2.0, (sp[0] + tp[0]) / 2.0)
            p = spot.get("point")
            if p and len(p) >= 2:
                return (float(p[0]), float(p[1]))
        pts = []
        for nid in spot["node_ids"]:
            it = self.node_items.get(nid)
            if it is not None:
                c = it.sceneBoundingRect().center()
                pts.append((c.x(), c.y()))
        if pts:
            return (sum(p[0] for p in pts) / len(pts),
                    sum(p[1] for p in pts) / len(pts))
        p = spot.get("point")
        return (float(p[0]), float(p[1])) if p and len(p) >= 2 else None

    def _redraw_residual_markers(self):
        for it in self._residual_items:
            if it.scene() is not None:
                self.scene.removeItem(it)
        self._residual_items = []
        if not self._residual_visible:
            return
        for i, spot in enumerate(self._residual_spots, start=1):
            pt = self._residual_scene_point(spot)
            if pt is None:
                continue
            x, y = pt
            tip = (f"Очаг №{i}: {_KIND_LABELS.get(spot['kind'], spot['kind'])}"
                   f" — {spot['descr']}")
            ring = QGraphicsEllipseItem(x - _MARKER_R, y - _MARKER_R,
                                        2 * _MARKER_R, 2 * _MARKER_R)
            ring.setPen(QPen(_COLOR, 2))
            ring.setBrush(QBrush(_FILL))
            ring.setZValue(_MARKER_Z)
            ring.setToolTip(tip)
            self.scene.addItem(ring)
            num = QGraphicsSimpleTextItem(str(i))
            font = QFont()
            font.setBold(True)
            num.setFont(font)
            num.setBrush(QBrush(_COLOR))
            br = num.boundingRect()
            num.setPos(x + _MARKER_R - br.width() / 2.0,
                       y - _MARKER_R - br.height())
            num.setZValue(_MARKER_Z)
            num.setToolTip(tip)
            self.scene.addItem(num)
            self._residual_items += [ring, num]

    # -----------------------------------------------------------------
    # Переход-зум
    # -----------------------------------------------------------------
    def focus_residual(self, index: int):
        """Отцентрировать вид на очаге под читаемым масштабом."""
        if not (0 <= index < len(self._residual_spots)):
            return
        pt = self._residual_scene_point(self._residual_spots[index])
        if pt is None:
            return
        self.resetTransform()
        self.scale(_FOCUS_SCALE, _FOCUS_SCALE)
        self.centerOn(QPointF(pt[0], pt[1]))

# -*- coding: utf-8 -*-
"""edit_engine.py — единый движок геометрии рёбер «Ручной правки» (Э2).

Одни ворота: всякая посадка конца ребра в редакторе проходит здесь.
Пересборка 2026-07 (план tender-launching-umbrella) сводит сюда все
дубль-реализации; редакторские методы — тонкие делегаты.

Целевой контракт движка (по этапам):
  C1 конец на порту: бокс — слоты вокруг середины стороны, полигон —
     контур/прямые участки, коннектор — центроид, ручной порт свят (Э2b);
  C2 конец НИКОГДА в углу рамки (Э2b, приоритет над всем);
  C3 полилиния строго ортогональна, запись со снапом к оси (Э3);
  C4 не сквозь бокс, контур приоритетнее bbox (Э3);
  C5 не вдоль границы ближе клиренса (Э3);
  C6 дальний конец жеста неприкосновенен (инвариант вызова);
  C7 предпросмотр == итог (инвариант вызова: один и тот же код на
     протяжке и отпускании).
Приоритет при конфликте: C2 > C3 («прямая важнее порта» ОТМЕНЕНО
решением заказчика 2026-07-31 «А->В»: конец всегда жёстко в порту,
малое колено честно остаётся — его лечит микро-доводка сдвигом узла) > C1.

Судьи дефектов — `edit_checks` (сторож == судья, импорт не копия).
Чистый stdlib: shapely/numpy/Qt в requirements/ui.txt нет.

Координаты (CODING_GUIDE §6): точки данных [y, x]; внутренняя математика
и возвраты — (x, y), как у `seating.node_anchor`.
"""
from __future__ import annotations

from . import ports as port_model
from . import seating


def seat_end(node, other_node, edge_data, role, cur, ref_x, ref_y,
             try_slack, snap_threshold):
    """Посадка конца ребра на узел (перенос `_seat_end_ported`, Э2a).

    Порядок (спека заказчика, §2.1/§6.1 плана):
      1. станция Э10 (`seating._poly_even_seat`) — канон, приоритетнее всего;
      2. ЭФФЕКТИВНЫЙ замок прямизны: ось подводящего сегмента
         (`_seg_lock`), иначе слабина по дальнему якорю
         (`straight_slack_lock`, только try_slack). Замок берётся, только
         если форма реально накрыла ось (`port_model.lock_respected`) —
         «прямая, как сейчас»; неэффективный замок раньше молча
         превращался в ray-посадку (кламп в угол) — источник «конец
         гуляет по периметру»;
      3. порт с гистерезисом (`port_model.choose_port`): конец сидит в
         порту (центр грани / прямой участок контура / центроид
         коннектора / ручной порт) и НЕ ползёт при смене направления на
         соседа; смена — только с изнанки (обобщение side-flip) или при
         радикальном выигрыше маршрута.

    cur — текущий конец [y, x] (гистерезис «остаться на своём порту»),
    role — 's'|'t' для станции Э10. Возвращает (x, y).
    """
    station = seating._poly_even_seat(node, edge_data, role, (ref_x, ref_y))
    if station:
        return station
    lock = None
    if cur:
        lock = seating._seg_lock((cur[1], cur[0]), (ref_x, ref_y))
        if lock and not port_model.lock_respected(
                seating.node_anchor(node, ref_x, ref_y, lock), lock):
            lock = None
    if lock is None and try_slack and other_node is not None:
        lock = seating.straight_slack_lock(node, other_node, ref_x, ref_y)
        if lock and not port_model.lock_respected(
                seating.node_anchor(node, ref_x, ref_y, lock), lock):
            lock = None
    if lock:
        return seating.node_anchor(node, ref_x, ref_y, lock)
    return port_model.choose_port(
        node, (cur[1], cur[0]) if cur else None, (ref_x, ref_y),
        float(snap_threshold))

# -*- coding: utf-8 -*-
"""Стражник Э2e: геометрию концов рёбер пишут только известные руки.

Пересборка 2026-08 свела посадку в единый движок
`modules/graph/core/edit_engine`. Этот тест замораживает КАРТУ мест,
которым разрешено присваивать source_point/target_point (включая
динамические ключи point_key/end_key/far_key/pk) в ui/. Новая запись
геометрии мимо движка меняет счётчик и роняет тест — добавляйте её
ОСОЗНАННО: либо через движок, либо с обновлением карты и обоснованием
в коммите.

Легальные руки (карта EXPECTED):
  advanced_graph_editor — делегаты движка (_reseat_moved_end,
    _engine_finish_ends, side-flip, batch-трансляции, endpoint-drag);
  commands/* — undo/redo (восстановление снапшотов, семантики нет);
  simple_graph_editor — база легаси-resize (переопределена в advanced,
    Э5 переведёт graph_validation_window) + бэкапы/сплит;
  contour_editor — вкладка «Контуры», растровые координаты (канон холста
    неприменим — осознанное решение Э1);
  base_graph_tab — миграции при открытии (канон + движковые порты).
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

GUARD = re.compile(
    r"\[\s*('source_point'|\"source_point\"|'target_point'"
    r"|\"target_point\"|point_key|end_key|far_key|pk)\s*\]\s*=[^=]")

EXPECTED = {
    # 14 = делегаты движка + side-flip + batch-трансляции + endpoint-drag
    # + откат гейта «не хуже входа» при разводе толщиной (2026-08-01)
    # + разворот ДАЛЬНЕГО конца после ручной смены порта (2026-08-02,
    #   _reseat_far_end_after_endpoint_drag: репро «диагональ через блок» —
    #   протяжка двигала только ближний конец, дальний оставался на грани,
    #   которая новому положению не смотрит; запись идёт через движок
    #   _seat_end_ported, своей геометрии не изобретает)
    "ui/editors/advanced_graph_editor.py": 14,
    "ui/editors/commands/advanced_commands.py": 6,
    "ui/editors/contour_editor.py": 2,
    "ui/editors/simple_graph_editor.py": 4,
    # 10 = миграции при открытии + лифт скинов + развод стопок (2 записи:
    # пересадка и откат гейта)
    "ui/tabs/base_graph_tab.py": 10,
}


def test_endpoint_writers_frozen():
    actual = {}
    for p in sorted((REPO / "ui").rglob("*.py")):
        n = len(GUARD.findall(p.read_text(encoding="utf-8")))
        if n:
            actual[p.relative_to(REPO).as_posix()] = n
    assert actual == EXPECTED, (
        "Карта записей геометрии разошлась (см. докстринг модуля):\n"
        f"  стало:   {actual}\n  ожидали: {EXPECTED}")

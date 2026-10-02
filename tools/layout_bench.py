# -*- coding: utf-8 -*-
"""layout_bench.py — приёмка раскладки на корпусе: метрики + список регрессий.

Порт `_scratch/layout_align/check_result.py` на прод-модуль, плюс метрики по
тексту (§3.9 плана). Гоняется руками. Вход берётся загрузчиком `tools/corpus.py`
(КД7): `d74eb9f1` и `6e7144d5` лежат в git (`tests/fixtures/graph/`, пункт 0.8),
остальные графы корпуса — только в локальном `storage/` этой машины.

Судья по перестановкам — `order_broken_local` (локальные пары), а НЕ глобальный
`order_broken` из гейта: на 51b339ab они дают 0 и 1096, потому что глобальный
считает парами узлов, разнесёнными на пол-листа.

Главное правило приёмки: **любой запрет сравнивается с базой входа, а не с
нулём.** Поэтому «до» здесь — это геометрия ПОСЛЕ расстановки (то, что слой
раздвигания получил на вход), а не исходная схема.

Запуск (из корня репо):
    python -X utf8 tools/layout_bench.py
    python -X utf8 tools/layout_bench.py --uid 51b339ab --json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from copy import deepcopy
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modules.graph.core.layout import LayoutParams, layout          # noqa: E402
from modules.graph.core.layout import _gate, _shapes, spread        # noqa: E402
from modules.graph.core.graph_access import (degrees, edge_ends,   # noqa: E402
                                             edge_polyline, edges,
                                             is_connector, nodes_by_id)
from modules.graph.core import contact, seating                     # noqa: E402
from modules.graph.core.ports import pinned_port                    # noqa: E402
from modules.graph.core.canvas_input import to_canvas               # noqa: E402
from modules.graph.core import pretransform                         # noqa: E402
from tools import corpus                                            # noqa: E402

# Состав корпуса стенда: uid8 -> каталог в storage/diagrams (справочно, файл
# ищет tools/corpus.py); None = сырого входа не существует.
CORPUS = {
    "51b339ab": "51b339ab-3e4c-429c-ac5f-49c44cb9c755",
    "a6d28736": "a6d28736-5039-4f40-9f7a-37b476f6fafb",
    "8d517a35": "8d517a35-0435-48d7-bc17-300345a15162",
    "89ca7583": "89ca7583-233e-44da-b30b-f2f08eb2567d",
    "13d1ef5f": "13d1ef5f-d251-4a88-bfd4-e0b51f144b69",
    "6e7144d5": "6e7144d5-19fc-4661-b90a-f26dcaa8c71a",
    "d74eb9f1": "d74eb9f1-668d-4596-891f-d4e5a16d04d0",
    "5137af27": None,          # сырой вход потерян, см. Э0
}
# Эталонная таблица корпуса. ПЕРЕОБЪЯВЛЕНА 2026-07-30 (решение заказчика):
# алгоритм намеренно изменён тремя правками, и прежние числа §7 SOLUTION.md
# сравнивать больше не с чем.
#   1. посадка конца ребра на контур ВДОЛЬ оси прямизны, а не лучом из
#      центроида (`seating._poly_hit`);
#   2. наложения считаются по НАРИСОВАННОЙ ФОРМЕ узла, а не по габариту
#      (`layout/_shapes.py`, `axial.conflicts`, `_overlaps.collect_items`);
#   3. пары, наложенные в ДЕТЕКТИРОВАННОЙ геометрии (до `apply_fixed_sizes`),
#      законны и не разводятся — решение заказчика 2026-07-29 «если на графе
#      после построения и проверки есть наложения, им можно там находиться»,
#      причём легальны размеры ИЗ ДЕТЕКЦИИ, а не после словаря.
# Итог перезамера: 601 -> 19 стало 590 -> 14, нелегальных наложений 0 на всех
# семи, косых рёбер по корпусу 62 (у трёх крупных контурных блоков 12 из 37
# против 36 из 37 на прежнем алгоритме), стороны/прямизна/связность/
# перестановки — нули. Прежняя таблица §7 (311/10, 128/6, 81/2, 26/1, 20/1,
# 14/0, 8/0, 0/0) сохранена в истории git, коммит с этой правкой.
# 5137af27 не считается — сырой вход потерян (Э0), число оставлено прежним.
#   4. створ крупного контурного блока (`band_block=True`, ярус 1) —
#      включён 2026-07-30: косых рёбер по корпусу 60 -> 53, на c2f79462
#      2 -> 1; ценой двух дефектов (8d517a35 103->0 стало 104->0 — там
#      бесплатно, 13d1ef5f 22->2 стало 23->3, c2f79462 13->3 стало 13->4).
#      На пяти графах без крупного блока — бит-в-бит как без него.
#   5. Э13 (решение заказчика 2026-07-31, §7.1.1 EDITOR_AFTER_LAYOUT_PLAN):
#      судья без допуска близости — legal для `_gate.verify` считается с
#      tol=0 (легально только пересечение площадью > 0 в детекции), допуск
#      BORDER_TOL остаётся решателю внутри `layout()`. По аудиту
#      (tools/legal_audit_probe.py) все 11 псевдо-пар корпуса не наложены
#      после раскладки — числа таблицы бит-в-бит, новая колонка
#      «утечка» (legal_leak) обязана быть 0.
#   6. Э2 (принято заказчиком 2026-07-31): ось посадки у пар с коннектором —
#      от реальной геометрии связи (`seating.reseat_edge`), вертикальная
#      связь больше не садится на боковую грань. Размен, принятый глазами:
#      диагонали a6d 8->4, боксы-на-магистралях a6d 11->7, ценой +4
#      соосных невидимых пар коннектор x арматура (a6d 4->7, 51b 7->8;
#      итог 610->20) — они подсвечиваются Э12 и ждут нуджинга Э11.
#      ВНИМАНИЕ: числа сняты фактическим прогоном 2026-07-31, и в «до» уже
#      входил неразъяснённый дрейф относительно прежнего EXPECT (51b
#      297->310, 8d5 104->99, 89ca 26->36 воспроизводились и ДО Э2) —
#      причина дрейфа выясняется отдельной задачей, при выяснении дописать
#      правку №7.
#   7. модель точек прикрепления (решение заказчика 2026-09-22, ветка
#      road/waist-seat): клапан ставится на трубу ТАЛИЕЙ — центроид на линии
#      трубы растра, бокс со смещением талии (`pretransform.apply_fixed_sizes`);
#      финальная посадка по модели (выверенный порт клапана жёсткий, середина
#      бокса уступает не дальше MID_SNAP=3, слоты вокруг опорной точки,
#      страховка неразведённых ступенек `_slack`). Перезамер: итог 619->24 ->
#      611->24; косые 10->5; концы клапанов в талии 1162/1349 -> 1351/1354;
#      бокс-на-магистрали 8->10 (a6d 2->5, 13d 1->2, 51b 3->1 — бокс клапана
#      сдвинут поперёк трубы до 14 px); «сторона входа» 0->3 (a6d 1, 8d5 2:
#      вход у угла крупного контура, судья сторон мерит по рамке; пара
#      бокс–коннектор наискось без замка оси) — разобраны, приняты.
#      Числа «до» на 51b/8d5 разошлись с прежним EXPECT и ДО правки (дрейф
#      из п.6: 302/116 против 310/99) — здесь сняты фактическим прогоном.
#   8. блок 2 модели точек прикрепления (решения заказчика 2026-09-28 Р2/Р6,
#      MEASUREMENTS §130): (а) рамка посадки сервера = bbox, а не letterbox
#      `_skin_content_rect` (0952530) — бенч бит-в-бит по счётчикам;
#      (б) таблица талий `contact.SKIN_CONTACT_OFFSET` по замеру JavaFX на
#      рабочем размере (aab3642); (в) веер у смещённой талии не сжимается,
#      а сдвигается внутрь грани (`ports.side_slots`, b77538c) — бенч
#      бит-в-бит с (б). Весь сдвиг — от (б), и из классов, чья талия
#      сменилась, на графах бенча есть только armatura_electro (a6d 22 узла,
#      8d5 27): ELECTRIC_VLV V 0.20 -> 0.222, рамка V едет на 0.832 px.
#      a6d28736 126->8 -> 126->7: диаг 6->3 -> 5->2, бокс-на-магистрали
#      11->5 -> 8->3 (с магистрали ушли node_367/369/379 на входе и
#      node_367/369 на выходе); «сторона входа» 1 -> 0 (edge_546
#      armatura_electro node_361 -> коннектор node_362 входит в допустимую
#      грань) — объявление снято.
#      8d517a35 123->3 -> 126->3: вход раскладки другой (бокс-на-магистрали
#      19->2 -> 20->1); «сторона входа» 2 -> 2, но состав сменился: edge_82
#      (unknow node_44 — коннектор node_59) ушёл, пришёл edge_200 (коннектор
#      node_135 — unknow node_140) — тот же класс «пара бокс–коннектор
#      наискось без замка оси», edge_manual_49 на месте.
#      Остальные 5 графов — бит-в-бит по колонкам. Итог 611->24 -> 614->23.
#      Сняты тремя прогонами (PYTHONHASHSEED 1..3): счётчики одни и те же,
#      хотя a6d/8d5 по хешу неповторимы (эталон детерминизма).
#   9. блок 4 модели точек прикрепления — кратные рёбра (Р7, 8709c92,
#      MEASUREMENTS §132): одно ребро на пару узлов, страховка
#      `parallel_edges.normalize_parallel_edges` в `to_canvas` меняет вход
#      раскладки. Рост второстепенных счётчиков принят решением заказчика
#      2026-09-29 как цена «одно ребро на пару». Сдвинулись три графа:
#      13d1ef5f 23->3 -> 22->3: удалён 1 дубль (рёбер 162 -> 161), остальные
#      колонки бит-в-бит.
#      51b339ab 289->8 -> 279->8: удалено 10 дублей в 8 парах (рёбер
#      938 -> 928), рассечений нет; бокс-на-магистрали 1 -> 2 (+node_772
#      рядом с удалёнными дублями node_773/774).
#      8d517a35 126->3 -> 127->1: 1 пара с unknow — дубль edge_233 рассечён
#      коннектором parconn_1 (рёбер 513 -> 514); «сторона входа» 2 -> 4:
#      было edge_200, edge_manual_49, стало edge_82 (node_44 — node_59),
#      edge_232 (node_140 — node_159, оставленный близнец рассечённого
#      edge_233), edge_manual_49, edge_manual_89 (node_378 — node_manual_15).
#      a6d28736, 89ca7583, 6e7144d5, d74eb9f1 — пар нет, бит-в-бит.
#      Итог 614->23 -> 604->21. Сняты тремя прогонами (PYTHONHASHSEED 1..3):
#      счётчики и состав сторон одни и те же.
#  10. обрыв осевой расстановки (0b278b9, docs/AUTO_LAYOUT.md): пол труб
#      клапана и ступень «только трубы клапанов» перед attempt(0.0) — клапан,
#      стоящий талией на трубе, больше не теряет трубы разом. Первое число
#      («до» = дефекты после расстановки, до раздвигания) упало на 5 графах —
#      расстановка сама разводит больше пар; второе — итог:
#      51b339ab 279->8 -> 130->10; бокс-на-магистрали 2 -> 0 (ушли node_772,
#      node_840).
#      a6d28736 126->7 -> 125->4; бокс-на-магистрали 3 -> 1 (остался node_64).
#      8d517a35 127->1 -> 126->3; диагонали 0->0 -> 1->1 (вход не хуже);
#      бокс-на-магистрали 1 -> 2 (+node_114 к node_323); «сторона входа»
#      4 -> 2: остались edge_manual_49 (node_156 — node_53) и edge_233
#      (node_140 — parconn_1, половина рассечённого дубля), ушли edge_82,
#      edge_232, edge_manual_89 — ACCEPTED_SIDE 4 -> 2.
#      89ca7583 36->2 -> 15->2; бокс-на-магистрали 0 -> 1 (+node_255).
#      13d1ef5f 22->3 -> 15->2; бокс-на-магистрали те же node_71, node_155.
#      6e7144d5 14->0 -> 4->0. d74eb9f1 — бит-в-бит.
#      Итог 604->21 -> 415->21 (сумма «после» та же: +2 51b339ab, +2
#      8d517a35, -3 a6d28736, -1 13d1ef5f). Регрессий по правилам стенда нет.
#      Сняты тремя прогонами (PYTHONHASHSEED 1..3): счётчики одни и те же.
#  11. блок 5 модели точек прикрепления — только осевые входы клапана
#      степени 1–2 (Р5; 82cc3c3, 7c2ba45, 7a33b88). Новые столбцы: концы
#      клапанов по граням (`valve_ends`: всего / в талии ± WAIST_TOL /
#      неосевые у степени 1–2 без пина и без RELIEF / неосевые у 3+ / концы
#      RELIEF), поворот turned/turn_shift_blocked/turn_blocked и
#      ports_yielded. Неосевой вход у степени 1–2 без пина — регрессия,
#      кроме клапанов turn_blocked (id — `valve_turn` blocked_ids; печатаются
#      поимённо, приняты). Замер 2026-09-30, выход layout() на fe5d66d (вход
#      блока 5) -> 7a33b88, судья тот же: концов 1376 = 1376; в талии
#      1334 -> 1360; неосевые у 1–2 20 -> 0 (a6d28736 11, 89ca7583 6,
#      13d1ef5f 3); у 3+ 7 = 7 (89ca7583 node_16/17/40/49/113); RELIEF 6 = 6
#      (13d1ef5f). Мимо талии на осевой грани: a6d28736 node_245 edge_369
#      (степень 1, у угла R, -10.5 px — порт уступил страховке, ports_yielded
#      1), 89ca7583 node_113 (степень 4, веер). Поворот: a6d28736 2/0/5
#      (повёрнуты node_260, node_281; turn_blocked node_173/174/175/240/282 —
#      все концы в талии), остальные 0/0/0. Прочие столбцы бит-в-бит с
#      7a33b88 (415 -> 21); «сторона входа» a6d28736 и 89ca7583 — от 82cc3c3,
#      EXPECT/ACCEPTED_SIDE здесь не тронуты (Д6 в гейте блока 5).
#  12. Д6 гейта блока 5 (Р5, cfba935): «сторона входа» a6d28736 0 -> 2 и
#      89ca7583 0 -> 1 — от 82cc3c3 (осевые входы), объявлены поимённо.
#      Все три — вход клапана из неосевой грани в осевую талию по правилу Р5:
#      a6d28736 edge_244 (filtr_meh node_158 — armatura_ruchn node_173) и
#      edge_245 (filtr_meh node_156 — armatura_ruchn node_174): клапан
#      степени 1 оси V встал в ряд с фильтром, труба поперёк оси, поворот
#      не прошёл (turn_blocked) — крючок в осевую талию B (3 излома), на
#      входе раскладки конец был на T (T -> B судья сторон считает
#      сменой: он мерит грань, а не осевость).
#      89ca7583 edge_manual_38 (armatura_ruchn node_211, степень 2 —
#      nasos node_197): вход раскладки в боковую L, выход в осевую T (1
#      излом); грань насоса B -> R — вслед за новым маршрутом.
#      EXPECT не сдвинут (415 -> 21 бит-в-бит). Сняты тремя прогонами
#      (PYTHONHASHSEED 1..3): состав сторон один и тот же.
#  13. Д6 гейта блока 6: EXPECT сдвинут в лучшую сторону, «до» прежнее.
#      Сдвиг дал ebfd797 (раздвигание не заводит коннектор трубы клапана в
#      рамку клапана); 7df036c, 0703aaf, 01d61fd выход этих двух графов не
#      меняют — холсты бит-в-бит с ebfd797. 51b339ab 130 -> 10 -> 130 -> 5:
#      ушли edge_858/859/860/861 (коннекторы node_570/572/573/574 у
#      armatura_ruchn node_576..579, 0.0), edge_870 (armatura_ruchn
#      node_575 — коннектор node_585, 0.0), edge_908 (коннектор–коннектор,
#      0.3); edge_6 (коннектор node_1 в рамке armatura_ruchn node_3, -6.5)
#      -> 0.0; пришёл edge_10 (node_3 — коннектор node_6, 0.0; у deploy
#      -10.5). Остались edge_122 (коннектор–коннектор, 0.14),
#      edge_manual_89 (коннектор node_615 в рамке armatura_ruchn node_639,
#      -10.5, как у deploy), edge_manual_96 (datchik node_884 —
#      armatura_ruchn node_868, -12.0). a6d28736 125 -> 4 -> 125 -> 3: ушёл
#      edge_110 (коннектор node_76 в рамке armatura_ruchn node_79, -8.55;
#      у deploy его нет); остались edge_369 (1.0), edge_484 (1.5), edge_491
#      (2.0). Итог 415 -> 21 -> 415 -> 15; прочие графы и ACCEPTED_SIDE не
#      тронуты. Сняты тремя прогонами (PYTHONHASHSEED 1..3).
#  14. Д6 правки E (1b2dc1e, переброска на свободную ось на обрыве; решение
#      заказчика 2026-10-02 (1)): 51b339ab 130 -> 5 -> 125 -> 6, «после»
#      хуже на 1 — принято. Расстановка на этом листе берёт ступень с 8
#      парами, переброшенными на свободную ось. «До» (вход раздвигания):
#      ушли edge_1167 (armatura_ruchn node_772 — napravlenie node_795),
#      edge_182..187 (коннекторы node_129..134 — armatura_ruchn
#      node_135..140), edge_8 (zaglushka node_4 — armatura_ruchn node_5);
#      пришли edge_941/942 (armatura_ruchn node_611/612 — napravlenie
#      node_631/632), edge_961 (armatura_ruchn node_646 — napravlenie
#      node_648); edge_manual_89 -10.5 -> 0.0. «После»: ушёл edge_manual_89
#      (коннектор node_615 в рамке armatura_ruchn node_639, -10.5; у deploy
#      есть); пришли edge_1125 (коннектор node_725 — коннектор node_769, 4.5)
#      и edge_908 (коннектор node_602 — коннектор node_606, 0.15), у deploy
#      обоих нет; остались edge_10 (0.0), edge_6 (0.0), edge_122
#      (коннектор–коннектор, 0.14 -> 0.07), edge_manual_96 (-12.0). Против
#      deploy 238fe6b 51b339ab 11 -> 6. Итог 415 -> 15 -> 410 -> 16; прочие
#      графы и ACCEPTED_SIDE не тронуты. Сняты тремя прогонами
#      (PYTHONHASHSEED 1..3).
EXPECT ={"51b339ab": (125, 6), "a6d28736": (125, 3), "8d517a35": (126, 3),
          "89ca7583": (15, 2), "13d1ef5f": (15, 2), "6e7144d5": (4, 0),
          "5137af27": (8, 0), "d74eb9f1": (0, 0)}

# Объявленные «стороны входа» — разобраны поимённо и приняты (правки №7–№12):
# 8d517a35 — два ребра (edge_manual_49, edge_233; правка №10). a6d28736 —
# edge_244, edge_245; 89ca7583 — edge_manual_38 (правка №12, Р5). Больше
# объявленного — регрессия.
ACCEPTED_SIDE = {"8d517a35": 2, "a6d28736": 2, "89ca7583": 1}

# §3.5: порог якоря подписи. Числа в SOLUTION.md нет — стенд текстом не
# занимался; ориентир плана 64 px холста. Здесь это ручка приёмки, штатное
# место значения — LayoutParams, когда механизм якоря появится (Э5).
ANCHOR_MAX = 64.0


# ───────────────────────────── текст (§3.9) ─────────────────────────────

def _active_blocks(graph):
    """Блоки, которые реально уходят в FXML: не слитые в другой блок."""
    return [t for t in (graph.get("text_blocks") or [])
            if t.get("merged_into") is None and t.get("bbox")]


def _pt_rect(px, py, r):
    """Расстояние от точки до прямоугольника (0 внутри)."""
    dx = max(r[0] - px, 0.0, px - r[2])
    dy = max(r[1] - py, 0.0, py - r[3])
    return math.hypot(dx, dy)


def _pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx
                                               + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _elements(graph):
    """(узлы, рёбра) в форме, удобной для поиска ближайшего к подписи."""
    nodes = []
    for n in graph.get("nodes", []):
        bb = n.get("bbox")
        c = n.get("centroid")
        if bb and len(bb) == 4 and not is_connector(n):
            nodes.append((n["id"], (bb[0], bb[1], bb[2], bb[3])))
        elif c:
            nodes.append((n["id"], (c[1], c[0], c[1], c[0])))
    edg = []
    for i, e in enumerate(edges(graph)):
        s, t = edge_ends(e)
        pl = edge_polyline(e)
        if len(pl) >= 2:
            edg.append((f"{s}->{t}#{i}", pl))
    return nodes, edg


def _nearest(bbox, nodes, edg):
    """(id ближайшего элемента, расстояние) от ЦЕНТРА подписи."""
    cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
    best_id, best = None, float("inf")
    for nid, r in nodes:
        d = _pt_rect(cx, cy, r)
        if d < best:
            best_id, best = nid, d
    for eid, pl in edg:
        for i in range(len(pl) - 1):
            d = _pt_seg(cx, cy, pl[i][0], pl[i][1], pl[i + 1][0], pl[i + 1][1])
            if d < best:
                best_id, best = eid, d
    return best_id, best


def _rect_hit(bbox, r):
    return not (bbox[2] <= r[0] or r[2] <= bbox[0]
                or bbox[3] <= r[1] or r[3] <= bbox[1])


def _seg_hit(bbox, pl):
    """Проходит ли полилиния сквозь прямоугольник подписи (Liang-Barsky)."""
    x1, y1, x2, y2 = bbox
    for i in range(len(pl) - 1):
        ax, ay = pl[i]
        bx, by = pl[i + 1]
        t0, t1 = 0.0, 1.0
        dx, dy = bx - ax, by - ay
        ok = True
        for p, q in ((-dx, ax - x1), (dx, x2 - ax), (-dy, ay - y1), (dy, y2 - ay)):
            if abs(p) < 1e-9:
                if q < 0:
                    ok = False
                    break
            else:
                rr = q / p
                if p < 0:
                    t0 = max(t0, rr)
                else:
                    t1 = min(t1, rr)
                if t0 > t1:
                    ok = False
                    break
        if ok:
            return True
    return False


def text_metrics(before, after, anchor_max=ANCHOR_MAX):
    """Что стало с подписями после перекладки (§3.9).

    Подписи раскладка НЕ трогает — уезжают элементы. Поэтому «сменила
    ближайший элемент» и означает «подпись оказалась не у своего объекта».
    """
    blocks = _active_blocks(after)
    if not blocks:
        return {"text_total": 0, "text_moved_owner": 0, "text_no_anchor": 0,
                "text_on_shape_before": 0, "text_on_shape_after": 0}
    nb, eb = _elements(before)
    na, ea = _elements(after)
    moved = no_anchor = hit_b = hit_a = 0
    for t in blocks:
        bb = t["bbox"]
        id_b, d_b = _nearest(bb, nb, eb)
        id_a, d_a = _nearest(bb, na, ea)
        if d_b > anchor_max:
            no_anchor += 1
        elif id_b != id_a:
            moved += 1
        for nodes, edg, flag in ((nb, eb, "b"), (na, ea, "a")):
            hit = any(_rect_hit(bb, r) for _i, r in nodes) \
                or any(_seg_hit(bb, pl) for _i, pl in edg)
            if hit and flag == "b":
                hit_b += 1
            elif hit and flag == "a":
                hit_a += 1
    return {"text_total": len(blocks), "text_moved_owner": moved,
            "text_no_anchor": no_anchor,
            "text_on_shape_before": hit_b, "text_on_shape_after": hit_a}


# ───────────────────────────── приёмка ─────────────────────────────

def _seat_kind(node):
    """Тип узла-хозяина конца — теми же ветками, что `seating.node_anchor`:
    connector / skin (FIXED_SIZES, рамка bbox — Р2) / polygon (контур) / bbox."""
    if node is None:
        return "bbox"
    if is_connector(node):
        return "connector"
    bb = node.get("bbox")
    has_bbox = bool(bb) and len(bb) == 4
    if has_bbox and node.get("class_name") in pretransform.FIXED_SIZES:
        return "skin"
    seg = node.get("segmentation")
    has_poly = bool(seg) and isinstance(seg, list) and len(seg) >= 6
    if has_poly and (not has_bbox or not node.get("_axis")):
        return "polygon"
    return "bbox"


def seat_violations(graph, tol=0.5):
    """§3.2 EDITOR_AFTER_LAYOUT_PLAN: концы рёбер вне канона посадки.

    Канон — `pretransform.seat_edge_endpoints` (единый модуль посадки, судья
    не переизобретается): прогон на deepcopy графа и счёт концов, ушедших от
    сохранённых дальше tol px, с разбивкой по типу узла-хозяина. Точки
    source/target_point — [y, x], расстояние осе-симметрично.

    Колонка ОТЧЁТНАЯ: выход раскладки каноничен by construction (ожидаемо 0);
    в regressions НЕ входит — набор метрик приёмки заморожен (§3.4 плана).
    """
    ref = deepcopy(graph)
    pretransform.seat_edge_endpoints(ref)
    byid = nodes_by_id(graph)
    by_type = {"connector": 0, "skin": 0, "polygon": 0, "bbox": 0}
    total = viol = 0
    worst = 0.0
    for e0, e1 in zip(edges(graph), edges(ref)):
        s, t = edge_ends(e0)
        for nid, key in ((s, "source_point"), (t, "target_point")):
            p0, p1 = e0.get(key), e1.get(key)
            if p1 is None:
                continue
            total += 1
            if p0 is None:
                d = float("inf")   # конца в данных нет — канон его требует
            else:
                d = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
            if d > tol:
                viol += 1
                by_type[_seat_kind(byid.get(nid))] += 1
                if d != float("inf"):
                    worst = max(worst, d)
    return {"total_ends": total, "violations": viol, "by_type": by_type,
            "max_px": round(worst, 2)}


# px: конец на осевой грани в талии — вдоль грани не дальше этого от порта
# (`contact.face_refs`); пробник scratchpad waist_hits.py брал тот же допуск.
WAIST_TOL = 0.75
_AXIAL = {"H": ("L", "R"), "V": ("T", "B")}


def valve_ends(graph, exempt=()):
    """Р5 (блок 5): концы рёбер у клапанов холста (`is_valve`, `_axis`) по
    граням рамки посадки `seating._anchor_rect`. Степень — по рёбрам
    (`graph_access.degrees`), правило — `contact.axial_only`.

    ends — всего концов; in_waist — на осевой грани в талии (± WAIST_TOL);
    nonaxial_12 — ["узел/ребро"] неосевых концов у клапана под правилом
    (степень 1–2, без RELIEF и старого холста) без пина на своём конце и не
    из `exempt`; nonaxial_blocked — такие же у клапанов из `exempt`
    (turn_blocked раскладки, поимённо); nonaxial_3p — неосевые у степени
    3+ (правила нет, отчётно); relief — концы у `AXIAL_EXEMPT_SKINS`.
    """
    byid = nodes_by_id(graph)
    deg = degrees(graph)
    out = {"ends": 0, "in_waist": 0, "nonaxial_12": [],
           "nonaxial_blocked": [], "nonaxial_3p": 0, "relief": 0}
    for e in edges(graph):
        for nid, key in zip(edge_ends(e), ("source_point", "target_point")):
            n = byid.get(nid)
            if n is None or not contact.is_valve(n) \
                    or n.get("_axis") not in _AXIAL:
                continue
            out["ends"] += 1
            if contact.skin_info(n)[1] in contact.AXIAL_EXEMPT_SKINS:
                out["relief"] += 1
                continue
            rect, pt = seating._anchor_rect(n), e.get(key)
            side = (seating._end_side(rect, float(pt[1]), float(pt[0]))
                    if pt and rect else None)
            if side in _AXIAL[n["_axis"]]:
                ref = (contact.face_refs(n, rect) or {}).get(side)
                c = pt[0] if side in ("L", "R") else pt[1]
                out["in_waist"] += ref is not None and abs(c - ref) <= WAIST_TOL
            elif deg.get(nid, 0) >= 3:
                out["nonaxial_3p"] += 1
            elif contact.axial_only(n, deg.get(nid, 0)) \
                    and pinned_port(n, e) is None:
                out["nonaxial_blocked" if nid in exempt
                    else "nonaxial_12"].append(f"{nid}/{e.get('id')}")
    return out


def _legal_leak(aft, base, pseudo_pairs):
    """Псевдолегальные пары, СТАВШИЕ наложенными после раскладки. Порог 0.

    Псевдо-пара — амнистия близости BORDER_TOL (во вкладке правки графа
    коллизии НЕТ). Строгий судья Э13 такие пары больше не прощает — они уже
    входят в `overlaps_after`; колонка показывает, кого именно перестали
    амнистировать (§3.2 EDITOR_AFTER_LAYOUT_PLAN).

    Метаправило «сравнение с базой входа»: пара, наложенная по нарисованной
    форме уже НА ВХОДЕ раскладки (base = stages["orig"], после словаря
    размеров), — не утечка слоя, слой её не создавал.
    """
    if not pseudo_pairs:
        return 0
    a_byid = {n["id"]: n for n in aft.get("nodes") or []}
    b_byid = {n["id"]: n for n in base.get("nodes") or []}

    def _overlapped(byid, a, b):
        na, nb = byid.get(a), byid.get(b)
        if na is None or nb is None:
            return False
        sa, sb = _shapes.shape_of(na), _shapes.shape_of(nb)
        return (sa is not None and sb is not None
                and sa.intersection(sb).area > 1e-6)

    return sum(1 for a, b in pseudo_pairs
               if _overlapped(a_byid, a, b) and not _overlapped(b_byid, a, b))


def check(uid8, canvas_dir=None, with_text=True):
    g = _load_input(uid8, canvas_dir)
    if g is None:
        return None
    t0 = time.perf_counter()
    stages = {}
    aft, st = layout(g, LayoutParams(), stages=stages)
    dt = time.perf_counter() - t0
    orig, v16 = stages["orig"], stages["placed"]

    # Наложения, пришедшие из ПОСТРОЕНИЯ И ПРОВЕРКИ, законны (решение
    # заказчика 2026-07-29) — судья их не считает. Таблица строится по
    # детектированным габаритам, которые кладёт `to_canvas`.
    # Э13: у СУДЬИ допуска близости нет (tol=0 — легально только пересечение
    # площадью > 0), BORDER_TOL остаётся решателю внутри `layout()`.
    det = orig.get("graph", {}).get("detected_bbox")
    legal_solver = _shapes.legal_pairs(orig, det, LayoutParams().border_tol)
    legal = _shapes.legal_pairs(orig, det, 0.0)
    vb, ab = nodes_by_id(v16), nodes_by_id(aft)
    g16 = _gate.verify(v16, orig, v16, legal)
    gaf = _gate.verify(aft, orig, v16, legal)

    vt = st.get("valve_turn") or {}
    ve = valve_ends(aft, set(vt.get("blocked_ids") or ()))
    r = {
        "uid": uid8,
        "nodes": len(aft.get("nodes", [])),
        "edges": len(list(edges(aft))),
        "time_s": round(dt, 1),
        "defects_before": st["defects_before"],
        "defects_after": st["defects_after"],
        "diag_before": g16["new_diagonals"], "diag_after": gaf["new_diagonals"],
        "overlaps_before": g16["overlaps"], "overlaps_after": gaf["overlaps"],
        "legal_leak": _legal_leak(aft, orig, legal_solver - legal),
        "side_changed": gaf["side_changed"],
        "straight_broken": gaf["straight_broken"],
        # СВЯЗНОСТЬ — «не хуже базы», а не абсолют: гейт сравнивает набор id с
        # ОРИГИНАЛОМ, и если исходный граф отредактировали уже после
        # расстановки, флаг поднимается у самой базы, а слой тут ни при чём
        "connectivity_changed": (gaf["connectivity_changed"]
                                 and not g16["connectivity_changed"]),
        "connectivity_base_differs": g16["connectivity_changed"],
        "order_local": spread.order_broken_local(aft, v16),
        "magi_before": len(spread.box_on_magi_drawn(v16, vb)),
        "magi_after": len(spread.box_on_magi_drawn(aft, ab)),
        "pipe_depth_before": round(spread.box_on_pipe_depth(v16, vb)[1], 1),
        "pipe_depth_after": round(spread.box_on_pipe_depth(aft, ab)[1], 1),
        "pen_before": round(spread.penetration_sum(v16, vb), 1),
        "pen_after": round(spread.penetration_sum(aft, ab), 1),
        # Р5 (блок 5): клапаны степени 1, повёрнутые по трубе / без сдвига /
        # не повёрнутые (крючок в осевую талию); порты, уступившие страховке
        "turned": vt.get("turned", 0),
        "turn_shift_blocked": vt.get("turn_shift_blocked", 0),
        "turn_blocked": vt.get("turn_blocked", 0),
        "ports_yielded": st.get("ports_yielded", 0),
        # Р5: концы у клапанов по граням (`valve_ends`)
        "valve_ends": ve["ends"], "valve_in_waist": ve["in_waist"],
        "nonaxial_12": ve["nonaxial_12"],
        "nonaxial_blocked": ve["nonaxial_blocked"],
        "nonaxial_3p": ve["nonaxial_3p"], "relief_ends": ve["relief"],
    }
    sv = seat_violations(aft)
    r["seat_violations"] = sv["violations"]
    r["seat_violations_by_type"] = sv["by_type"]
    r["seat_max_px"] = sv["max_px"]
    if with_text:
        r.update(text_metrics(orig, aft))

    reg = []
    if r["diag_after"] > r["diag_before"]:
        reg.append("диагонали")
    if r["overlaps_after"] > r["overlaps_before"]:
        reg.append("наложения")
    if r["legal_leak"]:
        reg.append("утечка легальности")
    if r["side_changed"] > ACCEPTED_SIDE.get(uid8, 0):
        reg.append("сторона входа")
    if r["straight_broken"]:
        reg.append("прямизна")
    if r["connectivity_changed"]:
        reg.append("связность")
    if r["order_local"]:
        reg.append("перестановки")
    if r["magi_after"] > r["magi_before"]:
        reg.append("бокс-на-магистрали")
    if r["defects_after"] > r["defects_before"]:
        reg.append("дефекты")
    if r["nonaxial_12"]:
        reg.append("неосевой вход клапана степени 1–2")
    exp = EXPECT.get(uid8)
    if exp and (r["defects_before"], r["defects_after"]) != exp:
        reg.append(f"расхождение с §7 SOLUTION.md ({exp[0]} -> {exp[1]})")
    if r["connectivity_base_differs"]:
        r["warning"] = ("исходный граф отредактирован после расстановки — "
                        "база уже расходится с ним; сравнение идёт с базой")
    r["regressions"] = reg
    r["ok"] = not reg
    return r


def _load_input(uid8, canvas_dir):
    if canvas_dir:
        p = Path(canvas_dir) / f"{uid8}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    if CORPUS[uid8] is None:          # сырой вход потерян (5137af27, см. Э0)
        return None
    src = corpus.graph_path(uid8)
    if src is None:
        return None
    g, _t = to_canvas(json.loads(src.read_text(encoding="utf-8")))
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", action="append", default=None)
    ap.add_argument("--canvas-input-dir", default=None,
                    help="каталог с <uid8>.json — готовыми канвас-входами")
    ap.add_argument("--no-text", action="store_true",
                    help="без метрик по тексту (они квадратичны и небыстры)")
    ap.add_argument("--json", action="store_true", help="сырой JSON по строке")
    args = ap.parse_args()

    rows = []
    for uid8 in (args.uid or list(CORPUS)):
        r = check(uid8, args.canvas_input_dir, with_text=not args.no_text)
        if r is None:
            print(f"{uid8}: ПРОПУЩЕН — входа нет", flush=True)
            continue
        rows.append(r)
        if args.json:
            print(json.dumps(r, ensure_ascii=False), flush=True)
        else:
            print(f"{uid8}: {r['defects_before']} -> {r['defects_after']}, "
                  f"{'РЕГРЕССИИ: ' + ', '.join(r['regressions']) if r['regressions'] else 'регрессий нет'}"
                  f", поворот {r['turned']}/{r['turn_shift_blocked']}/"
                  f"{r['turn_blocked']}, уступило портов {r['ports_yielded']}"
                  f", {r['time_s']}s", flush=True)

    if not rows:
        return 1
    print()
    print("| uid | узлов | дефекты | диаг | налож | утечка | стор | прям | связн | перест | "
          "бокс-на-магистр | проникн.форм | посадка | клапаны: концы/в талии/"
          "неос.1–2/неос.3+/RELIEF | поворот | уступ. | время |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['uid']} | {r['nodes']} | "
              f"{r['defects_before']} -> {r['defects_after']} | "
              f"{r['diag_before']}->{r['diag_after']} | "
              f"{r['overlaps_before']}/{r['overlaps_after']} | "
              f"{r['legal_leak']} | "
              f"{r['side_changed']} | {r['straight_broken']} | "
              f"{'OK' if not r['connectivity_changed'] else 'СЛОМАНА'} | "
              f"{r['order_local']} | {r['magi_before']} -> {r['magi_after']} | "
              f"{r['pen_before']} -> {r['pen_after']} | "
              f"{r['seat_violations']} | "
              f"{r['valve_ends']}/{r['valve_in_waist']}/"
              f"{len(r['nonaxial_12'])}+{len(r['nonaxial_blocked'])}/"
              f"{r['nonaxial_3p']}/{r['relief_ends']} | "
              f"{r['turned']}/{r['turn_shift_blocked']}/{r['turn_blocked']} | "
              f"{r['ports_yielded']} | {r['time_s']}s |")
    tb = sum(r["defects_before"] for r in rows)
    ta = sum(r["defects_after"] for r in rows)
    print(f"\nИТОГО дефектов: {tb} -> {ta}")

    if not args.no_text and any("text_total" in r for r in rows):
        print("\n| uid | подписей | сменили ближайший элемент | без якоря "
              f"(>{ANCHOR_MAX:g}px) | налезают до | налезают после |")
        print("|---|---|---|---|---|---|")
        for r in rows:
            if "text_total" not in r:
                continue
            print(f"| {r['uid']} | {r['text_total']} | {r['text_moved_owner']} | "
                  f"{r['text_no_anchor']} | {r['text_on_shape_before']} | "
                  f"{r['text_on_shape_after']} |")

    for r in rows:
        if r["nonaxial_12"] or r["nonaxial_blocked"]:
            print(f"\nНЕОСЕВЫЕ ВХОДЫ {r['uid']}: степень 1–2 "
                  f"{r['nonaxial_12'] or '—'}; turn_blocked (приняты) "
                  f"{r['nonaxial_blocked'] or '—'}")

    bad = [r for r in rows if r["regressions"]]
    warn = [r for r in rows if r.get("warning")]
    for r in warn:
        print(f"\nПРЕДУПРЕЖДЕНИЕ {r['uid']}: {r['warning']}")
    print("\nРЕГРЕССИИ:", "нет ни одной" if not bad
          else "; ".join(f"{r['uid']}: {', '.join(r['regressions'])}" for r in bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

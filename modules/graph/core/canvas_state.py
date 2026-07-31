# -*- coding: utf-8 -*-
"""canvas_state.py — устаревание холста: одна реализация на UI и воркер.

Зачем отдельный модуль (§3.6 плана `docs/planning/AUTO_LAYOUT_INTEGRATION.md`).
Раньше метка источника считалась как sha256 ВСЕГО файла `graph_validated.json`,
а этот файл переписывают уже после сборки холста — трижды: слияние OCR, полная
перезаливка вкладкой «Привязка подписей» и `binding/apply`. Значит холст,
собранный до OCR, к моменту открытия «Ручной правки» гарантированно объявлялся
устаревшим и пересобирался с нуля. Для автозапуска раскладки это приговор:
её выбрасывало бы каждый раз.

Поэтому sha считается не по файлу, а по ПРОЕКЦИИ графа — тем полям, которые
раскладка и правда читает. `text_blocks` и `bindings` в проекцию НЕ входят:
их правят OCR и привязка уже после сборки холста, и на геометрию они не влияют.
Свежесть текста меряется отдельным хешем (`text_imported_sha`).

Почему модуль лежит в `modules/graph/core/`, а не в пакете `layout/`: его
импортирует клиент, а `layout/__init__.py` на импорте тянет shapely и numpy,
которых в `requirements/ui.txt` нет. Две независимые реализации канона дали бы
расходящиеся sha, ложное «устарело» и молча выброшенную раскладку — ровно тот
отказ, против которого §3.6 и написан. Поэтому Qt здесь тоже нет.
"""
from __future__ import annotations

import hashlib
import json

from .pretransform import FIXED_SIZES

# Версия кода раскладки. Поднимать руками, когда меняется её поведение: смена
# версии = все сохранённые холсты устарели. Иначе после выката оператор снова
# скажет «ничего не изменилось», глядя на холст, собранный старым кодом.
LAYOUT_CODE_VERSION = "3"   # 3 (2026-07-30, Э2): выбор оси посадки — от
                            # реальной геометрии связи, не от порядка веток
                            # H/V (`seating.reseat_edge`): у коннектора с
                            # вырожденным диапазоном вертикальная связь
                            # больше не садится на боковую грань соседа.
                            # 2 (2026-07-30): посадка конца на контур вдоль оси
                            # прямизны, наложения по реальной форме узла с
                            # амнистией детектированных пар, створ крупного
                            # блока. Результат другой на всех графах с
                            # контурами — холсты версии 1 обязаны считаться
                            # устаревшими, иначе оператор увидит старую
                            # картинку и решит, что правка не приехала.

# Поля узла и ребра, от которых зависит раскладка.
# `source_point`/`target_point` обязательны: по ним `_node_axis` выбирает
# ориентацию узла и СВОПИТ W/H словарного бокса, declust берёт поперечную
# координату, репроекция — сторону выхода. Без них правка точки подключения в
# «Проверке схемы» не устарила бы холст.
_NODE_KEYS = ("id", "type", "class_name", "centroid", "bbox", "segmentation")
_EDGE_KEYS = ("source", "target", "source_point", "target_point",
              "waypoints", "path")
_TEXT_KEYS = ("id", "bbox", "text", "merged_into")

_TRANSFORM_DEFAULTS = {
    "layout_version": None,
    "layout_applied": False,
    "operator_saved": False,
    "source_sha": None,
    "text_imported_sha": None,
    "text_edited": False,
}


def layout_version() -> str:
    """Версия раскладки = версия кода + отпечаток таблицы `FIXED_SIZES`.

    Таблица входит в версию не для красоты: её правка меняет бокс у 91 %
    скин-узлов корпуса, и результат со старой таблицей не воспроизводится
    (замер стенда: 20 дефектов против 34). Холст, собранный до правки
    таблицы, обязан считаться устаревшим сам, без ручного поднятия версии.
    """
    table = json.dumps(sorted((k, float(w), float(h))
                              for k, (w, h) in FIXED_SIZES.items()),
                       separators=(",", ":"))
    return (f"{LAYOUT_CODE_VERSION}+fs"
            f"{hashlib.sha256(table.encode('utf-8')).hexdigest()[:8]}")


# ───────────────────────── канонизация и хеши ─────────────────────────

def _canon(value):
    """Значение в канонической форме: числа — строкой `repr(float)`.

    `sort_keys` в json сортирует только ключи словарей, а нам нужен канон
    целиком: 1 и 1.0 обязаны давать один хеш, иначе JSON-обход (файл ->
    память -> файл) менял бы sha на ровном месте.
    """
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)):
        return repr(float(value))
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, dict):
        return {k: _canon(value[k]) for k in sorted(value)}
    return str(value)


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"),
                      sort_keys=True)


def _records(items, keys):
    """Канонические записи, отсортированные ПО СОДЕРЖИМОМУ.

    Сортировка по содержимому, а не по порядку в файле: переписыватели графа
    (OCR, привязка) не обязаны сохранять порядок списков, а хеш обязан быть
    к нему безразличен.
    """
    return sorted(_dump({k: _canon(it.get(k)) for k in keys})
                  for it in items or [])


def _edges(graph):
    return graph.get("edges") if "edges" in graph else graph.get("links", [])


def graph_projection_sha(graph: dict) -> str:
    """Хеш геометрической проекции графа — метка источника холста."""
    payload = _dump({
        "nodes": _records(graph.get("nodes"), _NODE_KEYS),
        "edges": _records(_edges(graph), _EDGE_KEYS),
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def text_projection_sha(graph: dict) -> str:
    """Хеш текстовой проекции: `text_blocks` + `bindings`.

    Отдельный от `graph_projection_sha` сознательно: текстовые ключи из
    геометрической проекции исключены, а «показать расхождение» больше не на
    чем — §3.8 п. 3.
    """
    bindings = graph.get("bindings") or []
    payload = _dump({
        "text_blocks": _records(graph.get("text_blocks"), _TEXT_KEYS),
        "bindings": sorted(_dump(_canon(b)) for b in bindings),
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ───────────────────────── чтение и запись метки ─────────────────────────

def read_state(canvas_graph: dict) -> dict:
    """Состояние холста с дефолтами (холст без метки = всё по нулям)."""
    tr = ((canvas_graph.get("graph") or {}).get("canvas_transform") or {})
    state = dict(_TRANSFORM_DEFAULTS)
    for k in state:
        if k in tr:
            state[k] = tr[k]
    return state


def stamp(canvas_graph: dict, validated_graph: dict, *, layout_applied: bool,
          operator_saved: bool = False, text_imported_sha=None,
          text_edited: bool = False) -> dict:
    """Проставить метку на собранный холст. Возвращает `canvas_transform`.

    `layout_applied` — отдельно от версии, и это принципиально: холст собирают
    ДВА производителя — воркер (с раскладкой) и клиент (pretransform-фолбэк,
    когда задача упала, и сегодняшняя пересборка при устаревании). Без флага
    гейт либо примет фолбэк за готовую раскладку, либо объявит его вечно
    устаревшим и будет терять правки оператора при каждом открытии.
    """
    tr = canvas_graph.setdefault("graph", {}).setdefault("canvas_transform", {})
    tr["source_sha"] = graph_projection_sha(validated_graph)
    tr["layout_version"] = layout_version()
    tr["layout_applied"] = bool(layout_applied)
    tr["operator_saved"] = bool(operator_saved)
    tr["text_imported_sha"] = text_imported_sha
    tr["text_edited"] = bool(text_edited)
    return tr


def mark_operator_saved(canvas_graph: dict) -> bool:
    """Пометить холст как правленный руками. Зовёт СЕРВЕР на `canvas/save`.

    Этим путём пишет только клиент (автосейв шлёт запрос лишь при
    несохранённых правках), а воркер пишет холст напрямую и флага не ставит.
    На этом флаге стоят запрет молчаливой перезаписи правок и сверка перед
    записью результата раскладки.
    """
    tr = canvas_graph.setdefault("graph", {}).setdefault("canvas_transform", {})
    was = bool(tr.get("operator_saved"))
    tr["operator_saved"] = True
    return not was


def has_layout(canvas_graph: dict) -> bool:
    """Холст — продукт раскладки, а не pretransform-фолбэка."""
    return bool(read_state(canvas_graph)["layout_applied"])


def is_stale(canvas_graph: dict, validated_graph: dict):
    """(устарел, причина). Причина — для лога и сообщения оператору."""
    state = read_state(canvas_graph)
    if not state["layout_version"]:
        return True, "холст без метки версии — доверять нечему"
    if state["layout_version"] != layout_version():
        return True, (f"версия раскладки сменилась: "
                      f"{state['layout_version']} -> {layout_version()}")
    if not state["source_sha"]:
        return True, "холст без метки источника"
    actual = graph_projection_sha(validated_graph)
    if state["source_sha"] != actual:
        return True, "геометрия схемы изменилась после сборки холста"
    return False, None


def text_is_stale(canvas_graph: dict, validated_graph: dict):
    """(устарел ли текст на холсте, причина).

    Отдельно от геометрии: текст на холст импортируется из `graph_validated`
    при открытии вкладки, и его свежесть живёт своей жизнью.
    """
    state = read_state(canvas_graph)
    actual = text_projection_sha(validated_graph)
    if not state["text_imported_sha"]:
        return True, "текст на холст ещё не импортирован"
    if state["text_imported_sha"] != actual:
        return True, "текст или привязки изменились после импорта"
    return False, None

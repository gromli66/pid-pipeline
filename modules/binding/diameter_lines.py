"""
modules/binding/diameter_lines.py — «правило линии» для диаметров труб.

Диаметр, привязанный оператором к ребру, красит всю ЛИНИЮ этого ребра и ничего
кроме. Линия — не объект на диске, а разбиение рёбер графа, посчитанное по
геометрии ОРИГИНАЛА (`source_point` / `waypoints` / `target_point` как есть на
этапе привязки, а не после раскладки холста).

Правило (таблица классов — в YAML проекта, секция `diameter_lines`):

    узел степени <=2, класс transit  -> идти всегда (колено 90° — та же линия)
    узел степени >=3, класс transit  -> только коллинеарное продолжение
                                        (|180°-a| <= tol), одно, лучшее; в отвод не идти
    класс stop или вне таблицы       -> не идти никогда, при любой степени
    степень 1                        -> конец линии

Заменяет `modules/text_binding/binder.py::TextBinder.propagate_diameters`, где
правило было другим: свободный проход через любой `type == equipment` при любой
степени (2152 протечки из 4745 проходов на корпусе 24 схем), сравнение
ориентаций H/V вместо угла (ломалось на 342 диагоналях) и единственное классовое
правило `perehod`.

⛔ **Детерминизм — обязательное свойство, а не вкус.** Порядок `links` в файле не
инвариант: `modules/graph/core/canvas_state.py` прямо пишет, что переписыватели
графа не обязаны сохранять порядок списков. Поэтому склейка на врезке идёт по
парам, отсортированным по `(отклонение, id, id)`, а не по порядку инцидентности,
и номер линии выдаётся по минимальному `id` ребра в ней. Жадный обход в порядке
файла давал разное разбиение на 8 листах корпуса из 24 — а номер линии уезжает
на диск (`diameter_line`), и после пересохранения графа Ду оказался бы на других
рёбрах.

⚠ Координаты точек ребра — `[y, x]` (см. `CODING_GUIDE.md §6`). Для угла между
направлениями перестановка осей ничего не меняет, но ось всё равно названа явно,
чтобы правка не внесла ошибку по невнимательности.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Классы, которых нет в `classes:` YOLO-проекта, но которые законно встречаются
# в `class_name` узлов графа: их создаёт построение графа, а не детектор.
# Всё остальное, чего нет в `classes:`, — опечатка в таблице, и это ошибка.
SYNTHETIC_CLASSES = frozenset({"connector"})

_DEFAULT_TOL_DEG = 30.0
_DEFAULT_NO_DIAM_ENDS = frozenset({"datchik"})
_DEFAULT_NO_DIAM_MAX_EDGES = 3


class LineRulesError(ValueError):
    """Таблица классов в конфиге не полна или противоречива."""


def _name_list(value, field_name: str, source: str) -> frozenset:
    """Список имён классов из YAML. Скаляр — ошибка, а не строка по буквам.

    `transit: truba` без дефиса даёт `frozenset("truba")` = набор букв, и все
    классы молча становятся стопом. Такую опечатку надо ловить, а не переживать.
    """
    if value is None:
        return frozenset()
    if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
        raise LineRulesError(
            f"{source}: `{field_name}` должен быть списком имён классов, "
            f"получено {value!r}"
        )
    names = [str(v) for v in value]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        raise LineRulesError(f"{source}: дубли в `{field_name}`: {dups}")
    return frozenset(names)


@dataclass(frozen=True)
class LineRules:
    """Таблица классов и допуск. Источник — секция `diameter_lines` YAML проекта."""

    transit: frozenset[str]
    stop: frozenset[str]
    collinear_tol_deg: float = _DEFAULT_TOL_DEG
    no_diameter_ends: frozenset[str] = _DEFAULT_NO_DIAM_ENDS
    no_diameter_max_edges: int = _DEFAULT_NO_DIAM_MAX_EDGES

    def passes(self, class_name: str) -> bool:
        """Проходим ли узел этого класса. Класс вне таблицы — не проходим."""
        return class_name in self.transit

    @classmethod
    def from_project_yaml(cls, yaml_path: str | Path) -> "LineRules":
        """Прочитать таблицу из YAML проекта и сверить её с `classes:`.

        Raises:
            LineRulesError: секции нет, класс не отнесён ни к одному списку,
                класс в обоих списках, или в таблице есть имя, которого нет
                среди классов проекта.
        """
        # ⛔ `yaml` импортируется ЗДЕСЬ, а не на уровне модуля: у клиентского
        # окружения PyYAML может не быть, и тогда импорт модуля ронял бы всю
        # вкладку привязки — `load_data` падал `ModuleNotFoundError` ещё до
        # первой метки. Разбиение на линии и раскладка меток в yaml не нуждаются.
        import yaml

        path = Path(yaml_path)
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        section = data.get("diameter_lines")
        if not isinstance(section, dict):
            raise LineRulesError(
                f"{path.name}: нет секции `diameter_lines`. Без таблицы классов "
                f"правило линии не определено — молча считать все классы стопом "
                f"нельзя, это тихо режет линии и грузит оператора ручным трудом."
            )

        known = {
            c.get("name")
            for c in (data.get("classes") or [])
            if isinstance(c, dict) and c.get("name")
        }
        if not known:
            # Без списка классов сверять таблицу не с чем, и вся защита ниже
            # выключается МОЛЧА: опечатка в `transit` уводит класс в стоп, линии
            # режутся, оператор получает лишний ручной труд вместо ошибки.
            raise LineRulesError(
                f"{path.name}: не прочитан блок `classes:` — сверить таблицу "
                f"`diameter_lines` не с чем. Проверять её «как получится» нельзя."
            )
        rules = cls.from_section(section, known_classes=known, source=path.name)
        logger.info(
            "diameter_lines: transit %d, stop %d, tol %.0f°, «Ду не требуется» до "
            "%d рёбер до %s",
            len(rules.transit), len(rules.stop), rules.collinear_tol_deg,
            rules.no_diameter_max_edges, sorted(rules.no_diameter_ends),
        )
        return rules

    @classmethod
    def from_section(cls, section: dict, known_classes: set[str] | None = None,
                     source: str = "<config>") -> "LineRules":
        """Собрать правила из уже прочитанной секции (точка входа для тестов)."""
        transit = _name_list(section.get("transit"), "transit", source)
        stop = _name_list(section.get("stop"), "stop", source)
        if not transit:
            raise LineRulesError(
                f"{source}: `transit` пуст — проходимых классов нет, каждое ребро "
                f"становится своей линией, и правило линии выключено целиком."
            )

        both = sorted(transit & stop)
        if both:
            raise LineRulesError(
                f"{source}: классы в обоих списках `diameter_lines` сразу: {both}"
            )

        if known_classes:
            table = transit | stop
            missing = sorted(known_classes - table)
            if missing:
                raise LineRulesError(
                    f"{source}: классы есть в `classes:`, но не отнесены ни к "
                    f"`transit`, ни к `stop`: {missing}. Отнесите каждый явно — "
                    f"молчаливый стоп рвёт линии на новом классе, и оператор "
                    f"видит лишний ручной труд вместо ошибки конфига."
                )
            stray = sorted(table - known_classes - SYNTHETIC_CLASSES)
            if stray:
                raise LineRulesError(
                    f"{source}: в `diameter_lines` есть имена, которых нет среди "
                    f"классов проекта: {stray}. Похоже на опечатку."
                )

        try:
            tol = float(section.get("collinear_tol_deg", _DEFAULT_TOL_DEG))
        except (TypeError, ValueError) as exc:
            raise LineRulesError(
                f"{source}: `collinear_tol_deg` не число: "
                f"{section.get('collinear_tol_deg')!r}") from exc
        if not 0.0 < tol < 90.0:
            raise LineRulesError(
                f"{source}: `collinear_tol_deg` = {tol}; допуск коллинеарности "
                f"должен лежать в (0, 90)."
            )

        ends = section.get("no_diameter_ends")
        if "no_diameter_ends" in section:
            # Явный `null` или `[]` — это «правило выключено», а не «по умолчанию».
            ends = frozenset(_name_list(ends, "no_diameter_ends", source)) \
                if ends else frozenset()
        else:
            ends = _DEFAULT_NO_DIAM_ENDS
        try:
            max_edges = int(section.get("no_diameter_max_edges",
                                        _DEFAULT_NO_DIAM_MAX_EDGES))
        except (TypeError, ValueError) as exc:
            raise LineRulesError(
                f"{source}: `no_diameter_max_edges` не целое: "
                f"{section.get('no_diameter_max_edges')!r}") from exc
        if max_edges < 1:
            raise LineRulesError(
                f"{source}: `no_diameter_max_edges` = {max_edges}; порог длины "
                f"тупика должен быть не меньше 1."
            )
        return cls(
            transit=transit,
            stop=stop,
            collinear_tol_deg=tol,
            no_diameter_ends=ends,
            no_diameter_max_edges=max_edges,
        )


@dataclass
class Lines:
    """Разбиение рёбер графа на линии.

    `edges_of_line[i]` — индексы рёбер линии `i` в исходном списке рёбер.
    Нумерация устойчива: линии отсортированы по минимальному `id` ребра, так что
    перестановка `links` в файле номера не меняет.
    """

    edges_of_line: list[list[int]] = field(default_factory=list)
    line_of_edge: dict[int, int] = field(default_factory=dict)
    requires_diameter: list[bool] = field(default_factory=list)
    fingerprint: str = ""
    """Отпечаток списка рёбер, на котором построено разбиение.

    Линия хранит ИНДЕКСЫ рёбер, а метка резолвится по `id`. Если между
    `build_lines` и `apply_marks` список рёбер переписали (а порядок `links` —
    не инвариант), индексы поедут и Ду ляжет на чужие рёбра. Отпечаток
    превращает этот молчаливый промах в внятную ошибку.
    """

    def __len__(self) -> int:
        return len(self.edges_of_line)

    def line_for(self, edge_idx: int) -> int | None:
        """Номер линии ребра или None, если ребро вне разбиения (без концов)."""
        return self.line_of_edge.get(edge_idx)

    def edges_like(self, edge_idx: int) -> list[int]:
        """Все рёбра линии, которой принадлежит это ребро (включая его само)."""
        li = self.line_of_edge.get(edge_idx)
        return list(self.edges_of_line[li]) if li is not None else [edge_idx]

    @property
    def needing_diameter(self) -> int:
        return sum(1 for r in self.requires_diameter if r)


def edge_ray(edge: dict, node_id: str) -> tuple[float, float] | None:
    """Единичное направление ребра ОТ узла `node_id` наружу, в осях (y, x).

    Берётся первый сегмент со стороны узла: у ребра с изломами направление на
    врезке задаёт именно он, а не хорда «конец-в-конец».
    Возвращает None, если сегмент вырожден (обе точки совпали).
    """
    sp = _point(edge.get("source_point"))
    tp = _point(edge.get("target_point"))
    if sp is None or tp is None:
        return None
    wps = [w for w in (_point(q) for q in (edge.get("waypoints") or []))
           if w is not None]
    if edge.get("source") == node_id:
        p0, p1 = sp, (wps[0] if wps else tp)
    else:
        p0, p1 = tp, (wps[-1] if wps else sp)
    dy, dx = p1[0] - p0[0], p1[1] - p0[1]
    n = math.hypot(dy, dx)
    if not (n > 1e-6) or n != n:        # 0, NaN — направления нет
        return None
    return (dy / n, dx / n)


def _point(p):
    """Точка ребра как (y, x) или None, если это не точка.

    Мусор на входе (None, короткий список, строка, NaN) не должен ронять
    разбиение целиком: ребро просто не участвует в склейке на этом узле.
    """
    if not isinstance(p, (list, tuple)) or len(p) < 2:
        return None
    try:
        y, x = float(p[0]), float(p[1])
    except (TypeError, ValueError):
        return None
    if y != y or x != x:          # NaN
        return None
    return (y, x)


def _content_key(edge: dict) -> str:
    """Ключ ребра по содержимому — на случай, когда `id` нет или он не уникален.

    Инвариантен к перестановке `links`: зависит только от концов и геометрии.
    У полных дублей ключ совпадёт, но и разбиение на них симметрично.
    """
    pts = [_point(edge.get("source_point"))] \
        + [_point(q) for q in (edge.get("waypoints") or [])] \
        + [_point(edge.get("target_point"))]
    return "#%s|%s|%s" % (
        edge.get("source"), edge.get("target"),
        ";".join("%.3f,%.3f" % q if q else "-" for q in pts),
    )


def _edge_keys(edges: list[dict]):
    """Функция ключа ребра: `id`, если он есть у всех и уникален, иначе содержимое.

    ⛔ Детерминизм разбиения держится на этом ключе. Полагаться на `id`
    безусловно нельзя: ребро без `id`, с `id = 0` или с дублем ключа вернуло бы
    сортировку к индексам, а индекс перестановке `links` не инвариантен —
    и разбиение поехало бы вместе с номерами линий на диске.
    """
    ids = [e.get("id") for e in edges]
    ok = all(i not in (None, "", 0) for i in ids) and len(set(map(str, ids))) == len(ids)
    if ok:
        return lambda i: str(edges[i].get("id"))
    cache = {i: _content_key(e) for i, e in enumerate(edges)}
    logger.warning("id рёбер не уникальны или пусты — ключ линии считается "
                   "по геометрии (разбиение остаётся устойчивым)")
    return lambda i: cache[i]


def _fingerprint(nodes: list[dict], edges: list[dict], key) -> str:
    """Отпечаток входа разбиения: рёбра И классы узлов.

    ⛔ Классы обязательны. Разбиение зависит от них не меньше, чем от рёбер:
    смена `class_name` на стоп-класс рвёт линию. Отпечаток только по рёбрам
    пропускал такую правку молча — экран показывал залитую магистраль, а на
    диск уходила её половина (замер редтима: реклассификация узла при
    авто-привязке KKS).
    """
    import hashlib
    h = hashlib.sha256()
    for i in range(len(edges)):
        h.update(key(i).encode("utf-8"))
        h.update(b"\x00")
    h.update(b"\x01nodes\x01")
    for nid, cls in sorted(
            ((str(n.get("id")), str(n.get("class_name", ""))) for n in nodes)):
        h.update(nid.encode("utf-8"))
        h.update(b"\x00")
        h.update(cls.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _deviation_deg(ra: tuple[float, float], rb: tuple[float, float]) -> float:
    """Отклонение пары направлений от прямой (180°). 0 = идеальное продолжение.

    Оба луча смотрят ОТ узла наружу, поэтому прямая труба даёт угол 180°.
    """
    dot = ra[0] * rb[0] + ra[1] * rb[1]
    dot = max(-1.0, min(1.0, dot))
    return abs(180.0 - math.degrees(math.acos(dot)))


class _Union:
    """Система непересекающихся множеств по индексам рёбер."""

    def __init__(self, n: int) -> None:
        self._p = list(range(n))

    def find(self, a: int) -> int:
        p = self._p
        while p[a] != a:
            p[a] = p[p[a]]
            a = p[a]
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._p[ra] = rb


def build_lines(nodes: list[dict], edges: list[dict], rules: LineRules) -> Lines:
    """Разбить рёбра на линии по правилу линии.

    Args:
        nodes: узлы графа (нужны `id` и `class_name`).
        edges: рёбра графа (`source`, `target`, точки — координаты ОРИГИНАЛА).
        rules: таблица классов и допуск.
    """
    node_class = {n.get("id"): n.get("class_name", "") for n in nodes}

    incident: dict[str, list[int]] = {}
    degree: dict[str, int] = {}
    for i, e in enumerate(edges):
        src, tgt = e.get("source"), e.get("target")
        if not src or not tgt:
            continue
        incident.setdefault(src, []).append(i)
        degree[src] = degree.get(src, 0) + 1
        if tgt != src:              # самопетля не удваивает степень узла
            incident.setdefault(tgt, []).append(i)
            degree[tgt] = degree.get(tgt, 0) + 1

    key = _edge_keys(edges)

    union = _Union(len(edges))

    for node_id in sorted(incident, key=str):
        eids = incident[node_id]
        if not rules.passes(node_class.get(node_id, "")):
            continue                      # stop или класс вне таблицы

        if len(eids) <= 2:
            # Транзит и колено: всё, что сходится, — одна линия.
            for i in eids[1:]:
                union.union(eids[0], i)
            continue

        # Врезка: только коллинеарные пары, и каждое ребро — не более чем в одной.
        rays = {}
        for i in eids:
            r = edge_ray(edges[i], node_id)
            if r is not None:
                rays[i] = r

        candidates = []
        ordered = sorted(rays, key=key)
        for a in range(len(ordered)):
            for b in range(a + 1, len(ordered)):
                i, j = ordered[a], ordered[b]
                dev = _deviation_deg(rays[i], rays[j])
                if dev <= rules.collinear_tol_deg:
                    candidates.append((dev, key(i), key(j), i, j))

        # Жадно по отсортированному списку пар: результат не зависит от порядка
        # рёбер в файле — только от геометрии и от `id`.
        candidates.sort()
        taken: set[int] = set()
        for _dev, _ki, _kj, i, j in candidates:
            if i in taken or j in taken:
                continue
            union.union(i, j)
            taken.add(i)
            taken.add(j)

    groups: dict[int, list[int]] = {}
    for i in range(len(edges)):
        groups.setdefault(union.find(i), []).append(i)

    # Номер линии — по минимальному ключу ребра, а не по порядку обхода.
    ordered_groups = sorted(groups.values(), key=lambda g: min(key(i) for i in g))

    lines = Lines(fingerprint=_fingerprint(nodes, edges, key))
    for li, group in enumerate(ordered_groups):
        group = sorted(group, key=key)
        lines.edges_of_line.append(group)
        for i in group:
            lines.line_of_edge[i] = li
        lines.requires_diameter.append(
            _requires_diameter(group, edges, node_class, degree, incident, rules)
        )
    return lines


def _requires_diameter(group: list[int], edges: list[dict],
                       node_class: dict[str, str], degree: dict[str, int],
                       incident: dict, rules: LineRules) -> bool:
    """Нужен ли линии Ду.

    Не нужен тупиковому отводу не длиннее `no_diameter_max_edges` рёбер, который
    оканчивается на прибор из `no_diameter_ends`: импульсную линию к прибору на
    чертеже не подписывают.
    """
    if len(group) > rules.no_diameter_max_edges or not rules.no_diameter_ends:
        return True

    own = set(group)
    has_instrument_end = False
    attachments = 0          # узлы, которыми линия держится за остальной граф
    for nid in {n for i in group for n in (edges[i].get("source"),
                                           edges[i].get("target")) if n}:
        deg = degree.get(nid, 0)
        inside = sum(1 for i in incident.get(nid, ()) if i in own)
        if deg == 1 and node_class.get(nid, "") in rules.no_diameter_ends:
            has_instrument_end = True
        if deg > inside:
            attachments += 1

    # Отвод держится за граф ОДНИМ узлом. Если узлов два и больше — это кусок
    # магистрали, и освобождать его от Ду нельзя: на корпусе такой случай уже
    # есть (лист `89ca7583`, импульсный отвод склеен с двумя кусками врезки),
    # а пустой Ду в расчётной схеме превращается в заводские 0.3 м = Ду300.
    return not (has_instrument_end and attachments <= 1)


# ── метки оператора и запись Ду в рёбра ────────────────────────────────────

# Поля Ду у ребра `graph_validated.json`. Наружу, в конвертер расчётной схемы,
# уезжает ровно одно из них — `diameter_value` (мм, целое): `json2xml.py` берёт
# `float(e["diameter_value"]) / 1000`, всё остальное — наше внутреннее.
DIAMETER_FIELDS = (
    "diameter_value",       # мм, целое — единственное поле контракта с prtx
    "diameter_text",        # то же число строкой, для подписи на холсте
    "diameter_source",      # "ocr" | "manual" | "line" | "editor"
    "diameter_line",        # номер линии; на диске — чтобы правка на холсте
                            # не зависела от пересчёта разбиения по холсту
    "diameter_propagated",  # == (source == "line"), для старых читателей
)

# Поля, оставшиеся от прежней схемы: их писал старый поток, читателей у них нет.
LEGACY_DIAMETER_FIELDS = (
    "diameter_prefix", "diameter_suffix", "diameter_confidence",
    "diameter_ocr_block_idx",
)

SOURCE_OCR = "ocr"          # оператор перетащил распознанную подпись
SOURCE_MANUAL = "manual"    # оператор набрал число
SOURCE_LINE = "line"        # поток по линии от метки оператора

#: Источники, которые ставим МЫ. Всё остальное — чужая запись.
#:
#: Правка Ду живёт только во вкладке привязки текста (решение заказчика 26.08):
#: «Ручная правка» пишет в `graph_canvas`, а `.prtx` собирается из
#: `graph_validated` (`app/api/graph.py:562`) и назад холст не пишется
#: (`app/models/artifact.py:61`) — Ду оттуда просто не доехал бы до расчётной
#: схемы. Но чужую запись мы всё равно не трогаем: снести Ду, которого мы не
#: ставили, значит молча стереть чью-то работу.
OWN_SOURCES = (SOURCE_OCR, SOURCE_MANUAL, SOURCE_LINE)


def is_foreign(edge: dict) -> bool:
    """Ду на ребре есть, а источник не наш — чужая запись, не наше дело."""
    return bool(edge.get("diameter_value") or edge.get("diameter_text")) \
        and edge.get("diameter_source") not in OWN_SOURCES


@dataclass(frozen=True)
class DiameterMark:
    """Метка оператора: единственное, что он создаёт руками.

    Ключ — `edge_id`, а не `"{min}|{max}"` из source/target: ключ по концам
    неоднозначен на параллельных рёбрах (51 ребро в 28 группах на корпусе), и
    Ду садился бы сразу на всю пару.
    """

    edge_id: str
    value: int
    kind: str = SOURCE_MANUAL
    text: str = ""
    ocr_block_idx: int | None = None

    def __post_init__(self):
        # Проверка на входе, а не «где-нибудь потом»: негодное значение иначе
        # доедет до диска и превратится в САПФИР в заводские 0.3 м = Ду300.
        object.__setattr__(self, "value", _check_value(self.value))

    def as_text(self) -> str:
        return self.text or str(self.value)


class LinesOutOfDateError(RuntimeError):
    """`Lines` построено на другом списке рёбер — индексы указывают не туда."""


@dataclass
class ApplyReport:
    """Что получилось при раскладке меток по линиям."""

    edges_stamped: int = 0
    lines_covered: int = 0
    conflicts: list[tuple[int, list[int]]] = field(default_factory=list)
    orphan_marks: list[str] = field(default_factory=list)
    kept_foreign_edges: int = 0
    ineffective_marks: list[str] = field(default_factory=list)
    """Метки, не давшие ни одного ребра: линия целиком занята чужой записью.

    Без этого списка «ничего не произошло» выглядело бы как успех: оператор
    нажал, отчёт зелёный, на диске пусто.
    """

    @property
    def ok(self) -> bool:
        return not (self.conflicts or self.orphan_marks or self.ineffective_marks)


def clear_diameters(edges: list[dict]) -> int:
    """Снять поля Ду с рёбер, которые проставили МЫ. Чужое не трогать.

    Возвращает число чужих записей, которые оставлены как есть.
    """
    kept = 0
    for e in edges:
        if is_foreign(e):
            kept += 1
            continue
        for k in DIAMETER_FIELDS + LEGACY_DIAMETER_FIELDS:
            e.pop(k, None)
    return kept


def apply_marks(edges: list[dict], lines: Lines, marks: list[DiameterMark],
                nodes: list[dict]) -> ApplyReport:
    """Разложить метки по линиям и проставить Ду в рёбра.

    Ду метки красит ВСЮ линию её ребра и ничего кроме. Рёбра с чужой записью
    Ду (`is_foreign`) не перезаписываются.

    Две метки с разными значениями на одной линии — конфликт: линия не
    заливается, каждое помеченное ребро остаётся при своём значении, конфликт
    возвращается оператору. Молча выбирать «по большинству» нельзя — на выходе
    расчётная схема, и незамеченная ошибка там дороже вопроса.
    """
    if lines.fingerprint and nodes is not None and \
            lines.fingerprint != _fingerprint(nodes, edges, _edge_keys(edges)):
        raise LinesOutOfDateError(
            "разбиение на линии построено на другом графе — пересчитайте его "
            "перед раскладкой меток, иначе Ду ляжет на чужие рёбра"
        )

    report = ApplyReport()
    report.kept_foreign_edges = clear_diameters(edges)

    by_id = {}
    for i, e in enumerate(edges):
        eid = e.get("id")
        if eid:
            by_id[str(eid)] = i

    # Метки, разложенные по линиям.
    per_line: dict[int, list[tuple[int, DiameterMark]]] = {}
    for m in marks:
        idx = by_id.get(str(m.edge_id))
        if idx is None:
            report.orphan_marks.append(m.edge_id)
            continue
        li = lines.line_for(idx)
        if li is None:
            li = -1 - idx          # ребро вне разбиения — сама себе линия
        per_line.setdefault(li, []).append((idx, m))

    for li, items in sorted(per_line.items()):
        group = lines.edges_of_line[li] if li >= 0 else [items[0][0]]

        # Значения-претенденты на линию: метки оператора И чужие записи, уже
        # лежащие на этой линии. Чужое мы не перезаписываем, а значит
        # расхождение с меткой — такой же конфликт, а не «победила метка».
        values = {int(m.value) for _idx, m in items}
        for i in group:
            if is_foreign(edges[i]) and edges[i].get("diameter_value"):
                values.add(int(edges[i]["diameter_value"]))

        if len(values) > 1:
            report.conflicts.append((li, sorted(values)))
            for idx, m in items:
                if is_foreign(edges[idx]):
                    continue          # чужая запись не перезаписывается
                _stamp(edges[idx], m.value, m.as_text(), m.kind, li)
                report.edges_stamped += 1
            continue

        m = items[0][1]
        marked = {idx for idx, _ in items}
        covered = False
        for i in group:
            if is_foreign(edges[i]):
                continue                      # чужая запись не перезаписывается
            src = m.kind if i in marked else SOURCE_LINE
            _stamp(edges[i], m.value, m.as_text(), src, li)
            report.edges_stamped += 1
            covered = True
        if covered:
            report.lines_covered += 1
        else:
            report.ineffective_marks.extend(mm.edge_id for _i, mm in items)

    return report


def _stamp(edge: dict, value: int, text: str, source: str, line_idx: int) -> None:
    edge["diameter_value"] = _check_value(value)
    edge["diameter_text"] = text
    edge["diameter_source"] = source
    edge["diameter_line"] = int(line_idx)
    edge["diameter_propagated"] = (source == SOURCE_LINE)


def _check_value(value) -> int:
    """Ду — целое положительное число миллиметров. Иначе это не Ду.

    Проверка здесь, а не «где-нибудь потом»: `json2xml.py` читает поле как
    `float(d) / 1000`, а `if _d:` пропускает ноль. Ноль, дробь и отрицательное
    молча превратились бы в заводские 0.3 м, то есть в честный Ду300 в САПФИР.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Ду должен быть числом, получено %r" % (value,))
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("Ду должен быть целым числом миллиметров, получено %r"
                         % (value,))
    ivalue = int(value)
    if ivalue <= 0:
        raise ValueError("Ду должен быть положительным, получено %r" % (value,))
    return ivalue


def marks_from_edges(edges: list[dict]) -> list[DiameterMark]:
    """Собрать метки обратно из графа — граф и есть их хранилище.

    Второго хранилища (записи в `ocr_binding.json`) сознательно нет: два ответа
    на вопрос «что показать при открытии» дают дубли и расхождения. Поток
    (`diameter_source == "line"`) метками не считается — он пересчитывается.
    """
    out = []
    for e in edges:
        src = e.get("diameter_source")
        if src not in (SOURCE_OCR, SOURCE_MANUAL):
            continue
        value = e.get("diameter_value")
        if not value:
            continue
        out.append(DiameterMark(
            edge_id=str(e.get("id") or ""),
            value=int(value),
            kind=str(src),
            text=str(e.get("diameter_text") or value),
        ))
    return out

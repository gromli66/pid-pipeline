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

import yaml

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
        transit = frozenset(section.get("transit") or ())
        stop = frozenset(section.get("stop") or ())

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

        tol = float(section.get("collinear_tol_deg", _DEFAULT_TOL_DEG))
        if not 0.0 < tol < 90.0:
            raise LineRulesError(
                f"{source}: `collinear_tol_deg` = {tol}; допуск коллинеарности "
                f"должен лежать в (0, 90)."
            )

        ends = section.get("no_diameter_ends")
        max_edges = int(section.get("no_diameter_max_edges",
                                    _DEFAULT_NO_DIAM_MAX_EDGES))
        return cls(
            transit=transit,
            stop=stop,
            collinear_tol_deg=tol,
            no_diameter_ends=frozenset(ends) if ends is not None
            else _DEFAULT_NO_DIAM_ENDS,
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
    sp, tp = edge.get("source_point"), edge.get("target_point")
    if not sp or not tp:
        return None
    wps = [tuple(w) for w in (edge.get("waypoints") or [])]
    if edge.get("source") == node_id:
        p0, p1 = tuple(sp), (wps[0] if wps else tuple(tp))
    else:
        p0, p1 = tuple(tp), (wps[-1] if wps else tuple(sp))
    dy, dx = p1[0] - p0[0], p1[1] - p0[1]
    n = math.hypot(dy, dx)
    if n <= 1e-6:
        return None
    return (dy / n, dx / n)


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
        incident.setdefault(tgt, []).append(i)
        degree[src] = degree.get(src, 0) + 1
        degree[tgt] = degree.get(tgt, 0) + 1

    # Ключ ребра для устойчивой сортировки: `id` уникален во всех боевых графах,
    # но на синтетике его может не быть — тогда индекс, он тоже детерминирован.
    def key(i: int) -> str:
        return str(edges[i].get("id") or f"@{i:08d}")

    union = _Union(len(edges))

    for node_id in sorted(incident):
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

    lines = Lines()
    for li, group in enumerate(ordered_groups):
        group = sorted(group, key=key)
        lines.edges_of_line.append(group)
        for i in group:
            lines.line_of_edge[i] = li
        lines.requires_diameter.append(
            _requires_diameter(group, edges, node_class, degree, rules)
        )
    return lines


def _requires_diameter(group: list[int], edges: list[dict],
                       node_class: dict[str, str], degree: dict[str, int],
                       rules: LineRules) -> bool:
    """Нужен ли линии Ду.

    Не нужен тупиковому отводу не длиннее `no_diameter_max_edges` рёбер, который
    оканчивается на прибор из `no_diameter_ends`: импульсную линию к прибору на
    чертеже не подписывают.
    """
    if len(group) > rules.no_diameter_max_edges:
        return True
    for i in group:
        e = edges[i]
        for nid in (e.get("source"), e.get("target")):
            if nid and degree.get(nid, 0) == 1:
                if node_class.get(nid, "") in rules.no_diameter_ends:
                    return False
    return True

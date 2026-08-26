"""Правило линии Ду: разбиение графа на линии (`modules/binding/diameter_lines.py`).

Синтетика здесь намеренно мелкая и читаемая: каждый тест проверяет ровно один
пункт правила. Корпусные гейты (детерминизм на 24 боевых графах, воспроизведение
замера 2601/2530/71) живут в `tools/diameter_lines_bench.py` — корпуса нет в git,
и `suite_baseline` такие параметризации из карты исключает.

Координаты точек — `[y, x]` (`CODING_GUIDE.md §6`).
"""

import pytest

from modules.binding.diameter_lines import (
    LineRules,
    LineRulesError,
    build_lines,
    edge_ray,
)

TRANSIT = {"connector", "truba", "napravlenie", "datchik", "armatura_ruchn"}
STOP = {"perehod", "nasos", "unknow"}


def rules(**kw) -> LineRules:
    return LineRules(transit=frozenset(TRANSIT), stop=frozenset(STOP), **kw)


def node(nid: str, cls: str = "connector") -> dict:
    return {"id": nid, "class_name": cls}


def edge(eid: str, src: str, tgt: str, p0, p1, waypoints=None) -> dict:
    return {
        "id": eid,
        "source": src,
        "target": tgt,
        "source_point": list(p0),
        "target_point": list(p1),
        "waypoints": [list(w) for w in (waypoints or [])],
    }


def line_sets(lines) -> set[frozenset[int]]:
    """Разбиение как множество множеств — сравнение без оглядки на нумерацию."""
    return {frozenset(g) for g in lines.edges_of_line}


# ── геометрия ──────────────────────────────────────────────────────────────

def test_ray_smotrit_ot_uzla_v_obe_storony():
    e = edge("e1", "a", "b", (100, 0), (100, 50))
    assert edge_ray(e, "a") == pytest.approx((0.0, 1.0))
    assert edge_ray(e, "b") == pytest.approx((0.0, -1.0))


def test_ray_beret_pervyi_segment_a_ne_hordu():
    """У ребра с изломом направление на врезке задаёт первый сегмент."""
    e = edge("e1", "a", "b", (0, 0), (100, 100), waypoints=[(0, 100)])
    assert edge_ray(e, "a") == pytest.approx((0.0, 1.0))   # вправо, не по диагонали


def test_ray_vyrozhdennogo_segmenta_est_none():
    e = edge("e1", "a", "b", (10, 10), (10, 10))
    assert edge_ray(e, "a") is None


# ── правило: степень <= 2 ──────────────────────────────────────────────────

def test_koleno_90_gradusov_odna_liniya():
    """Поворот трубы на 90° — та же линия, а не две."""
    nodes = [node("a"), node("corner"), node("b")]
    edges = [
        edge("e1", "a", "corner", (0, 0), (0, 100)),
        edge("e2", "corner", "b", (0, 100), (100, 100)),
    ]
    lines = build_lines(nodes, edges, rules())
    assert len(lines) == 1


def test_stop_klass_rvet_liniyu_pri_stepeni_2():
    """`perehod` меняет Ду между патрубками — две линии, а не одна."""
    nodes = [node("a"), node("p", "perehod"), node("b")]
    edges = [
        edge("e1", "a", "p", (0, 0), (0, 100)),
        edge("e2", "p", "b", (0, 100), (0, 200)),
    ]
    lines = build_lines(nodes, edges, rules())
    assert line_sets(lines) == {frozenset({0}), frozenset({1})}


def test_apparat_rvet_liniyu_dazhe_kogda_truba_pryamaya():
    """Старое правило пускало Ду через любой `equipment` при любой степени."""
    nodes = [node("a"), node("n", "nasos"), node("b")]
    edges = [
        edge("e1", "a", "n", (0, 0), (0, 100)),
        edge("e2", "n", "b", (0, 100), (0, 200)),
    ]
    assert len(build_lines(nodes, edges, rules())) == 2


def test_klass_vne_tablicy_rvet_liniyu():
    """Незнакомый класс — стоп, а не транзит: играем от безопасности."""
    nodes = [node("a"), node("x", "sovsem_novyi_klass"), node("b")]
    edges = [
        edge("e1", "a", "x", (0, 0), (0, 100)),
        edge("e2", "x", "b", (0, 100), (0, 200)),
    ]
    assert len(build_lines(nodes, edges, rules())) == 2


def test_strelka_napravleniya_stepeni_2_sshivaetsya():
    """Стрелка на трубе — транзит: степень 2 сшиваем (решение заказчика)."""
    nodes = [node("a"), node("arrow", "napravlenie"), node("b")]
    edges = [
        edge("e1", "a", "arrow", (0, 0), (0, 100)),
        edge("e2", "arrow", "b", (0, 100), (0, 200)),
    ]
    assert len(build_lines(nodes, edges, rules())) == 1


def test_stepen_1_konec_linii():
    nodes = [node("a"), node("arrow", "napravlenie")]
    edges = [edge("e1", "a", "arrow", (0, 0), (0, 100))]
    lines = build_lines(nodes, edges, rules())
    assert len(lines) == 1 and lines.edges_of_line[0] == [0]


# ── правило: степень >= 3 ──────────────────────────────────────────────────

def _troinik():
    """Магистраль a—t—b по горизонтали и отвод t—c вниз."""
    nodes = [node("a"), node("t"), node("b"), node("c")]
    edges = [
        edge("e1", "a", "t", (100, 0), (100, 100)),
        edge("e2", "t", "b", (100, 100), (100, 200)),
        edge("e3", "t", "c", (100, 100), (200, 100)),
    ]
    return nodes, edges


def test_vrezka_idet_v_prodolzhenie_i_ne_idet_v_otvod():
    nodes, edges = _troinik()
    lines = build_lines(nodes, edges, rules())
    assert line_sets(lines) == {frozenset({0, 1}), frozenset({2})}


def test_vrezka_diagonal_sshivaetsya_hotya_H_V_by_slomalos():
    """Диагональная магистраль: у старого правила H/V она рвалась (342 случая)."""
    nodes = [node("a"), node("t"), node("b"), node("c")]
    edges = [
        edge("e1", "a", "t", (0, 0), (100, 100)),
        edge("e2", "t", "b", (100, 100), (200, 200)),
        edge("e3", "t", "c", (100, 100), (200, 100)),
    ]
    lines = build_lines(nodes, edges, rules())
    assert line_sets(lines) == {frozenset({0, 1}), frozenset({2})}


def test_vrezka_beret_odno_luchshee_prodolzhenie():
    """Два кандидата в продолжение — сшивается только лучший, второй сам по себе."""
    nodes = [node("a"), node("t"), node("b"), node("c")]
    edges = [
        edge("e1", "a", "t", (100, 0), (100, 100)),
        edge("e2", "t", "b", (100, 100), (100, 200)),          # ровно 180°
        edge("e3", "t", "c", (100, 100), (110, 200)),          # ~174°, тоже в допуске
    ]
    lines = build_lines(nodes, edges, rules())
    assert line_sets(lines) == {frozenset({0, 1}), frozenset({2})}


def test_vrezka_vertikalnaya_magistral_sshivaetsya():
    """Тот же тройник, но магистраль вертикальная — проверка оси [y, x]."""
    nodes = [node("t"), node("a"), node("b"), node("c")]
    edges = [
        edge("e1", "t", "a", (100, 100), (0, 100)),      # вверх
        edge("e2", "t", "b", (100, 100), (100, 200)),    # вправо (отвод)
        edge("e3", "t", "c", (100, 100), (200, 100)),    # вниз
    ]
    lines = build_lines(nodes, edges, rules())
    assert line_sets(lines) == {frozenset({0, 2}), frozenset({1})}


def test_vrezka_bez_prodolzheniya_ne_sshivaet_nichego():
    """Ни одной пары в допуске — три линии (28% рёбер корпуса без продолжения)."""
    nodes = [node("t"), node("a"), node("b"), node("c")]
    edges = [
        edge("e1", "t", "a", (100, 100), (100, 200)),    # вправо
        edge("e2", "t", "b", (100, 100), (200, 100)),    # вниз
        edge("e3", "t", "c", (100, 100), (171, 171)),    # вниз-вправо, 45°
    ]
    lines = build_lines(nodes, edges, rules())
    assert len(lines) == 3


def test_stop_klass_ne_puskaet_i_pri_stepeni_3():
    """На аппарате коллинеарность роли не играет — не идти никогда."""
    nodes = [node("a"), node("n", "nasos"), node("b"), node("c")]
    edges = [
        edge("e1", "a", "n", (100, 0), (100, 100)),
        edge("e2", "n", "b", (100, 100), (100, 200)),
        edge("e3", "n", "c", (100, 100), (200, 100)),
    ]
    assert len(build_lines(nodes, edges, rules())) == 3


def test_kollektor_bolshoi_stepeni_sshivaet_tolko_magistral():
    """Коллектор: 20 отводов вниз и сквозная магистраль."""
    nodes = [node("k"), node("l"), node("r")] + [node(f"t{i}") for i in range(20)]
    edges = [
        edge("e_left", "l", "k", (100, 0), (100, 500)),
        edge("e_right", "k", "r", (100, 500), (100, 1000)),
    ]
    for i in range(20):
        edges.append(edge(f"e_t{i}", "k", f"t{i}", (100, 500), (200, 500 + i)))
    lines = build_lines(nodes, edges, rules())
    assert lines.edges_of_line[lines.line_for(0)] == [0, 1]
    assert len(lines) == 21


def test_vyrozhdennoe_rebro_ne_ronyaet_razbienie():
    """Ребро с нулевым первым сегментом просто не участвует в склейке."""
    nodes = [node("a"), node("t"), node("b"), node("c")]
    edges = [
        edge("e1", "a", "t", (100, 0), (100, 100)),
        edge("e2", "t", "b", (100, 100), (100, 200)),
        edge("e3", "t", "c", (100, 100), (100, 100)),
    ]
    lines = build_lines(nodes, edges, rules())
    assert line_sets(lines) == {frozenset({0, 1}), frozenset({2})}


# ── детерминизм ────────────────────────────────────────────────────────────

def test_perestanovka_reber_ne_menyaet_razbienie():
    """Порядок `links` не инвариант — разбиение обязано от него не зависеть."""
    nodes = [node("t"), node("a"), node("b"), node("c"), node("d")]
    base = [
        edge("e1", "a", "t", (100, 0), (100, 100)),
        edge("e2", "t", "b", (100, 100), (100, 200)),      # 180° к e1
        edge("e3", "t", "c", (100, 100), (101, 200)),      # ~179.4°, тоже кандидат
        edge("e4", "t", "d", (100, 100), (200, 100)),
    ]
    ref = build_lines(nodes, base, rules())
    ref_ids = {frozenset(base[i]["id"] for i in g) for g in ref.edges_of_line}

    for perm in ([3, 2, 1, 0], [2, 0, 3, 1], [1, 3, 0, 2]):
        shuffled = [base[i] for i in perm]
        got = build_lines(nodes, shuffled, rules())
        got_ids = {frozenset(shuffled[i]["id"] for i in g) for g in got.edges_of_line}
        assert got_ids == ref_ids, f"перестановка {perm} дала другое разбиение"


def test_nomer_linii_ustoichiv_k_perestanovke():
    """`diameter_line` уезжает на диск — номер обязан быть от содержимого."""
    nodes = [node("a"), node("b"), node("c"), node("d")]
    base = [
        edge("zz", "a", "b", (0, 0), (0, 100)),
        edge("aa", "c", "d", (500, 0), (500, 100)),
    ]
    forward = build_lines(nodes, base, rules())
    backward = build_lines(nodes, list(reversed(base)), rules())
    # линия ребра "aa" получает номер 0 в обоих случаях
    assert forward.line_for(1) == 0
    assert backward.line_for(0) == 0


# ── «Ду не требуется» ──────────────────────────────────────────────────────

def test_impulsnyi_tupik_k_datchiku_du_ne_trebuet():
    nodes = [node("t"), node("m"), node("d", "datchik")]
    edges = [
        edge("e1", "t", "m", (100, 100), (150, 100)),
        edge("e2", "m", "d", (150, 100), (200, 100)),
    ]
    lines = build_lines(nodes, edges, rules())
    assert lines.requires_diameter == [False]
    assert lines.needing_diameter == 0


def test_dlinnyi_otvod_k_datchiku_du_trebuet():
    """Порог по длине — не «любой отвод к прибору бесплатный»."""
    nodes = [node(f"n{i}") for i in range(4)] + [node("d", "datchik")]
    edges = [
        edge(f"e{i}", f"n{i}", f"n{i + 1}", (100, 100 * i), (100, 100 * (i + 1)))
        for i in range(3)
    ] + [edge("e3", "n3", "d", (100, 300), (100, 400))]
    lines = build_lines(nodes, edges, rules())
    assert lines.requires_diameter == [True]


def test_tupik_k_ne_priboru_du_trebuet():
    nodes = [node("t"), node("v", "armatura_ruchn")]
    edges = [edge("e1", "t", "v", (100, 100), (150, 100))]
    lines = build_lines(nodes, edges, rules())
    assert lines.requires_diameter == [True]


# ── конфиг ─────────────────────────────────────────────────────────────────

def test_klass_ne_otnesen_ni_k_odnomu_spisku_ronyaet_zagruzku():
    with pytest.raises(LineRulesError, match="не отнесены"):
        LineRules.from_section(
            {"transit": ["truba"], "stop": ["nasos"]},
            known_classes={"truba", "nasos", "novyi_klass"},
        )


def test_klass_v_oboih_spiskah_ronyaet_zagruzku():
    with pytest.raises(LineRulesError, match="в обоих списках"):
        LineRules.from_section({"transit": ["truba"], "stop": ["truba"]})


def test_opechatka_v_tablice_ronyaet_zagruzku():
    with pytest.raises(LineRulesError, match="опечатк"):
        LineRules.from_section(
            {"transit": ["truba", "trubaa"], "stop": ["nasos"]},
            known_classes={"truba", "nasos"},
        )


def test_connector_razreshen_hot_ego_net_v_classes():
    r = LineRules.from_section(
        {"transit": ["truba", "connector"], "stop": ["nasos"]},
        known_classes={"truba", "nasos"},
    )
    assert r.passes("connector")


def test_dopusk_vne_diapazona_ronyaet_zagruzku():
    with pytest.raises(LineRulesError, match="допуск"):
        LineRules.from_section(
            {"transit": ["truba"], "stop": ["nasos"], "collinear_tol_deg": 120}
        )


def test_konfig_proekta_gruzitsya_i_pokryvaet_vse_klassy():
    """Боевой YAML обязан проходить ту же проверку, что и синтетика."""
    path = "configs/projects/thermohydraulics/thermohydraulics.yaml"
    r = LineRules.from_project_yaml(path)
    assert r.passes("connector")
    assert not r.passes("perehod")
    assert not r.passes("unknow")
    assert r.passes("napravlenie")
    assert r.collinear_tol_deg == 30.0
    assert r.no_diameter_ends == frozenset({"datchik"})

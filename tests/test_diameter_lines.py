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


# ── метки и запись Ду в рёбра (блок 2) ─────────────────────────────────────

from modules.binding.diameter_lines import (  # noqa: E402
    DIAMETER_FIELDS,
    LEGACY_DIAMETER_FIELDS,
    SOURCE_LINE,
    SOURCE_MANUAL,
    SOURCE_OCR,
    DiameterMark,
    apply_marks,
    clear_diameters,
    marks_from_edges,
)


def _magistral_s_otvodom():
    """a—t—b по горизонтали (одна линия) и отвод t—c (другая)."""
    nodes = [node("a"), node("t"), node("b"), node("c")]
    edges = [
        edge("e1", "a", "t", (100, 0), (100, 100)),
        edge("e2", "t", "b", (100, 100), (100, 200)),
        edge("e3", "t", "c", (100, 100), (200, 100)),
    ]
    return nodes, edges


def test_metka_krasit_vsyu_liniyu_i_ne_zahodit_v_otvod():
    nodes, edges = _magistral_s_otvodom()
    lines = build_lines(nodes, edges, rules())
    rep = apply_marks(edges, lines, [DiameterMark("e1", 300, SOURCE_OCR, "300")])

    assert rep.ok and rep.lines_covered == 1
    assert edges[0]["diameter_value"] == 300
    assert edges[1]["diameter_value"] == 300          # продолжение той же линии
    assert "diameter_value" not in edges[2]           # отвод не тронут


def test_istochnik_razlichaet_metku_i_potok():
    nodes, edges = _magistral_s_otvodom()
    lines = build_lines(nodes, edges, rules())
    apply_marks(edges, lines, [DiameterMark("e1", 300, SOURCE_OCR, "300")])

    assert edges[0]["diameter_source"] == SOURCE_OCR
    assert edges[0]["diameter_propagated"] is False
    assert edges[1]["diameter_source"] == SOURCE_LINE
    assert edges[1]["diameter_propagated"] is True


def test_du_v_rebre_celoe_v_millimetrah():
    """Контракт с расчётной схемой: `json2xml` берёт `float(d)/1000`."""
    nodes, edges = _magistral_s_otvodom()
    lines = build_lines(nodes, edges, rules())
    apply_marks(edges, lines, [DiameterMark("e1", 300, SOURCE_MANUAL)])
    assert isinstance(edges[0]["diameter_value"], int)
    assert edges[0]["diameter_text"] == "300"


def test_nomer_linii_zapisan_v_rebro():
    nodes, edges = _magistral_s_otvodom()
    lines = build_lines(nodes, edges, rules())
    apply_marks(edges, lines, [DiameterMark("e1", 300)])
    assert edges[0]["diameter_line"] == edges[1]["diameter_line"] == lines.line_for(0)


def test_parallelnye_rebra_klyuch_po_id_a_ne_po_koncam():
    """51 ребро корпуса делит ключ `min|max` — метка обязана идти по `id`.

    Концы здесь стоп-классы, поэтому нитки лежат в РАЗНЫХ линиях: старый ключ
    `f"{min}|{max}"` залил бы Ду сразу на обе.
    """
    nodes = [node("a", "perehod"), node("b", "perehod")]
    edges = [
        edge("p1", "a", "b", (100, 0), (100, 100)),
        edge("p2", "a", "b", (120, 0), (120, 100)),   # тот же `min|max`
    ]
    lines = build_lines(nodes, edges, rules())
    assert len(lines) == 2
    apply_marks(edges, lines, [DiameterMark("p1", 200)])
    assert edges[0]["diameter_value"] == 200
    assert "diameter_value" not in edges[1]


def test_parallelnye_rebra_mezhdu_tranzitami_odna_liniya():
    """А вот между двумя транзитными концами пара ниток — законно одна линия.

    Обе стороны — узлы степени 2 транзитного класса, правило велит идти всегда.
    Зафиксировано намеренно: это не дыра ключа, а прямое следствие правила.
    """
    nodes = [node("a"), node("b")]
    edges = [
        edge("p1", "a", "b", (100, 0), (100, 100)),
        edge("p2", "a", "b", (120, 0), (120, 100)),
    ]
    lines = build_lines(nodes, edges, rules())
    assert len(lines) == 1


def test_chuzhaya_zapis_ne_perezapisyvaetsya():
    """Ду, поставленный не нами, вкладка привязки не трогает."""
    nodes, edges = _magistral_s_otvodom()
    edges[1]["diameter_value"] = 250
    edges[1]["diameter_source"] = "chuzhoi"
    lines = build_lines(nodes, edges, rules())

    rep = apply_marks(edges, lines, [DiameterMark("e1", 300, SOURCE_OCR)])
    assert edges[0]["diameter_value"] == 300
    assert edges[1]["diameter_value"] == 250          # правка редактора цела
    assert edges[1]["diameter_source"] == "chuzhoi"
    assert rep.kept_foreign_edges == 1


def test_ochistka_ne_trogaet_chuzhuyu_zapis():
    nodes, edges = _magistral_s_otvodom()
    edges[0]["diameter_value"] = 100
    edges[0]["diameter_source"] = SOURCE_LINE
    edges[1]["diameter_value"] = 250
    edges[1]["diameter_source"] = "chuzhoi"

    kept = clear_diameters(edges)
    assert kept == 1
    assert "diameter_value" not in edges[0]
    assert edges[1]["diameter_value"] == 250


def test_starye_polya_snimayutsya_so_SVOEI_zapisi():
    """`prefix/suffix/confidence` уходят: читателей у них нет, в prtx не едут."""
    nodes, edges = _magistral_s_otvodom()
    edges[0].update({"diameter_value": 50, "diameter_source": SOURCE_LINE,
                     "diameter_prefix": "Dy", "diameter_suffix": "",
                     "diameter_confidence": 0.9, "diameter_ocr_block_idx": 7})
    clear_diameters(edges)
    for k in LEGACY_DIAMETER_FIELDS:
        assert k not in edges[0]
    assert "diameter_value" not in edges[0]


def test_chuzhaya_zapis_bez_istochnika_ne_stiraetsya():
    """Ду без `diameter_source` — не наш: так пишет «Ручная правка» и старый
    поток. Снести его значило бы молча стереть работу оператора."""
    nodes, edges = _magistral_s_otvodom()
    edges[0].update({"diameter_value": 250, "diameter_prefix": "Dy"})
    kept = clear_diameters(edges)
    assert kept == 1
    assert edges[0]["diameter_value"] == 250


def test_dve_metki_s_raznym_du_na_odnoi_linii_konflikt():
    """Молча выбрать «по большинству» нельзя — на выходе расчётная схема."""
    nodes, edges = _magistral_s_otvodom()
    lines = build_lines(nodes, edges, rules())
    rep = apply_marks(edges, lines, [
        DiameterMark("e1", 300, SOURCE_OCR),
        DiameterMark("e2", 250, SOURCE_OCR),
    ])
    assert not rep.ok
    assert rep.conflicts and rep.conflicts[0][1] == [250, 300]
    assert edges[0]["diameter_value"] == 300          # каждое при своём
    assert edges[1]["diameter_value"] == 250
    assert rep.lines_covered == 0                     # линия не залита


def test_dve_metki_s_odnim_du_ne_konflikt():
    nodes, edges = _magistral_s_otvodom()
    lines = build_lines(nodes, edges, rules())
    rep = apply_marks(edges, lines, [
        DiameterMark("e1", 300), DiameterMark("e2", 300),
    ])
    assert rep.ok and rep.lines_covered == 1


def test_metka_na_ischeznuvshee_rebro_ne_ronyaet_a_soobschaet():
    """Пересборка графа меняет `id` — метка осиротеет, и это надо увидеть."""
    nodes, edges = _magistral_s_otvodom()
    lines = build_lines(nodes, edges, rules())
    rep = apply_marks(edges, lines, [DiameterMark("net-takogo", 300)])
    assert rep.orphan_marks == ["net-takogo"]
    assert rep.edges_stamped == 0


def test_metki_chitayutsya_obratno_iz_grafa_bez_potoka():
    """Граф — единственное хранилище меток; поток метками не считается."""
    nodes, edges = _magistral_s_otvodom()
    lines = build_lines(nodes, edges, rules())
    apply_marks(edges, lines, [DiameterMark("e1", 300, SOURCE_OCR, "300")])

    back = marks_from_edges(edges)
    assert len(back) == 1
    assert back[0].edge_id == "e1" and back[0].value == 300
    assert back[0].kind == SOURCE_OCR


def test_krug_zamykaetsya_bez_dublei():
    nodes, edges = _magistral_s_otvodom()
    lines = build_lines(nodes, edges, rules())
    first = [DiameterMark("e1", 300, SOURCE_OCR, "300")]
    apply_marks(edges, lines, first)

    for _ in range(3):
        again = marks_from_edges(edges)
        apply_marks(edges, lines, again)
    assert len(marks_from_edges(edges)) == 1
    assert [e.get("diameter_value") for e in edges] == [300, 300, None]


# ── проекции холста: разметка Ду не должна устаривать холст ────────────────

def test_du_ne_menyaet_geometricheskuyu_proekciyu_holsta():
    """Иначе холст всех схем объявится устаревшим и раскладка перезапустится."""
    from modules.graph.core import canvas_state

    nodes, edges = _magistral_s_otvodom()
    graph = {"nodes": nodes, "links": edges}
    before = canvas_state.graph_projection_sha(graph)

    lines = build_lines(nodes, edges, rules())
    apply_marks(edges, lines, [DiameterMark("e1", 300, SOURCE_OCR, "300")])

    assert canvas_state.graph_projection_sha(graph) == before


def test_polya_du_ne_vhodyat_v_belyi_spisok_proekcii():
    from modules.graph.core import canvas_state
    assert not (set(DIAMETER_FIELDS) & set(canvas_state._EDGE_KEYS))


# ── дыры, найденные красной командой ───────────────────────────────────────

from modules.binding.diameter_lines import LinesOutOfDateError  # noqa: E402


def _nichya():
    """Врезка с ТОЧНОЙ ничьёй: два кандидата в продолжение с равным отклонением.

    Тайбрейк по ключу — единственное, что делает выбор устойчивым; на 0° и 0.6°
    ничья не воспроизводится, и порядко-зависимая версия проходит тест.
    """
    nodes = [node("t"), node("a"), node("b"), node("c")]
    edges = [
        edge("e1", "t", "a", (100, 100), (100, 200)),          # восток
        edge("e2", "t", "b", (100, 100), (117.36, 1.52)),      # 170° к востоку
        edge("e3", "t", "c", (100, 100), (82.64, 1.52)),       # тоже 170°
    ]
    return nodes, edges


def _partition_by_ident(edges, lines, ident):
    return {frozenset(ident(edges[i]) for i in g) for g in lines.edges_of_line}


def _vse_perestanovki_dayut_odno(nodes, edges, ident):
    import itertools
    ref = None
    for perm in itertools.permutations(range(len(edges))):
        shuffled = [edges[i] for i in perm]
        got = _partition_by_ident(shuffled, build_lines(nodes, shuffled, rules()),
                                  ident)
        if ref is None:
            ref = got
        elif got != ref:
            return False, perm
    return True, None


def test_determinizm_pri_tochnoi_nichye():
    nodes, edges = _nichya()
    ok, perm = _vse_perestanovki_dayut_odno(nodes, edges, lambda e: e["id"])
    assert ok, "перестановка %s дала другое разбиение" % (perm,)


def test_determinizm_bez_id_u_reber():
    """Ключ по содержимому: без него сортировка падала на индексы."""
    nodes, edges = _nichya()
    for e in edges:
        e.pop("id")
    ok, perm = _vse_perestanovki_dayut_odno(
        nodes, edges, lambda e: (e["source"], e["target"], tuple(e["target_point"])))
    assert ok, "перестановка %s дала другое разбиение" % (perm,)


def test_determinizm_pri_dublyah_id():
    nodes, edges = _nichya()
    for e in edges:
        e["id"] = "dup"
    ok, perm = _vse_perestanovki_dayut_odno(
        nodes, edges, lambda e: tuple(e["target_point"]))
    assert ok, "перестановка %s дала другое разбиение" % (perm,)


def test_id_nol_i_pustoi_ne_lomayut_determinizm():
    nodes, edges = _nichya()
    edges[0]["id"] = 0
    edges[1]["id"] = ""
    ok, perm = _vse_perestanovki_dayut_odno(
        nodes, edges, lambda e: tuple(e["target_point"]))
    assert ok, "перестановка %s дала другое разбиение" % (perm,)


def test_dubli_id_ne_puskayut_metku_na_chuzhoe_rebro():
    """При дублях `id` метка неоднозначна — разбиение уходит на ключ содержимого."""
    nodes = [node("a", "perehod"), node("b", "perehod")]
    edges = [
        edge("dup", "a", "b", (100, 0), (100, 100)),
        edge("dup", "a", "b", (120, 0), (120, 100)),
    ]
    lines = build_lines(nodes, edges, rules())
    assert len(lines) == 2      # рёбра всё равно разведены по линиям


def test_ustarevshee_razbienie_ne_krasit_chuzhie_rebra():
    """`Lines` держит индексы; список рёбер переписали — индексы указывают не туда."""
    nodes, edges = _magistral_s_otvodom()
    lines = build_lines(nodes, edges, rules())
    pereputannye = [edges[2], edges[0], edges[1]]
    with pytest.raises(LinesOutOfDateError):
        apply_marks(pereputannye, lines, [DiameterMark("e1", 300)])


def test_ukorochennoe_razbienie_ne_vyklyuchaet_pravilo_molcha():
    nodes, edges = _magistral_s_otvodom()
    lines = build_lines(nodes, edges, rules())
    with pytest.raises(LinesOutOfDateError):
        apply_marks(edges[:2], lines, [DiameterMark("e1", 300)])


def test_konflikt_ne_zatiraet_chuzhuyu_zapis():
    nodes, edges = _magistral_s_otvodom()
    edges[1]["diameter_value"] = 250
    edges[1]["diameter_source"] = "chuzhoi"
    lines = build_lines(nodes, edges, rules())
    rep = apply_marks(edges, lines, [
        DiameterMark("e1", 400, SOURCE_OCR), DiameterMark("e2", 300, SOURCE_MANUAL),
    ])
    assert not rep.ok and rep.conflicts
    assert edges[1]["diameter_value"] == 250
    assert edges[1]["diameter_source"] == "chuzhoi"


def test_metka_protiv_chuzhoi_zapisi_eto_konflikt():
    """Иначе на линии два разных Ду, а отчёт зелёный — и в prtx уедет пустота."""
    nodes, edges = _magistral_s_otvodom()
    edges[0]["diameter_value"] = 250
    edges[0]["diameter_source"] = "chuzhoi"
    lines = build_lines(nodes, edges, rules())
    rep = apply_marks(edges, lines, [DiameterMark("e1", 300, SOURCE_OCR)])
    assert rep.conflicts and rep.conflicts[0][1] == [250, 300]
    assert edges[0]["diameter_value"] == 250


def test_liniya_celikom_iz_chuzhih_zapisei_metka_ne_srabotala():
    """«Ничего не произошло» не должно выглядеть как успех."""
    nodes, edges = _magistral_s_otvodom()
    for i in (0, 1):
        edges[i]["diameter_value"] = 300
        edges[i]["diameter_source"] = "chuzhoi"
    lines = build_lines(nodes, edges, rules())
    rep = apply_marks(edges, lines, [DiameterMark("e1", 300, SOURCE_OCR)])
    assert rep.edges_stamped == 0 and rep.lines_covered == 0
    assert rep.ineffective_marks == ["e1"]
    assert not rep.ok


@pytest.mark.parametrize("bad", [0, -50, 250.7, "300", None, True])
def test_negodnoe_znachenie_du_ne_uezzhaet_na_disk(bad):
    """`json2xml` читает поле как `float(d)/1000`, а ноль пропускает: мусор там
    молча становится заводскими 0.3 м = Ду300."""
    nodes, edges = _magistral_s_otvodom()
    lines = build_lines(nodes, edges, rules())
    with pytest.raises(ValueError):
        apply_marks(edges, lines, [DiameterMark("e1", bad)])


def test_celoe_v_vide_float_prinimaetsya():
    nodes, edges = _magistral_s_otvodom()
    lines = build_lines(nodes, edges, rules())
    apply_marks(edges, lines, [DiameterMark("e1", 300.0)])
    assert edges[0]["diameter_value"] == 300
    assert isinstance(edges[0]["diameter_value"], int)


def test_krug_s_chuzhoi_zapisyu_ne_teryaet_ee():
    nodes, edges = _magistral_s_otvodom()
    edges[1]["diameter_value"] = 250
    edges[1]["diameter_source"] = "chuzhoi"
    lines = build_lines(nodes, edges, rules())
    for _ in range(3):
        apply_marks(edges, lines, marks_from_edges(edges))
    assert edges[1]["diameter_value"] == 250
    assert edges[1]["diameter_source"] == "chuzhoi"


def test_du_ne_trebuetsya_ne_dayut_kusku_magistrali():
    """Отвод к прибору, склеенный с двумя кусками врезки, — не тупик.

    На корпусе такой случай есть (лист `89ca7583`), и он молча уносил кусок
    магистрали в «Ду не требуется».
    """
    nodes = [node(n) for n in ("p", "t1", "u", "v", "b")] + [node("d", "datchik")]
    edges = [
        # линия: A (p→t1) + C (t1→d), они коллинеарны на t1
        edge("A", "p", "t1", (100, 100), (100, 200)),
        edge("C", "t1", "d", (100, 200), (100, 300)),
        # у p своя сквозная пара (север-юг) — A там отдельная ветка
        edge("P1", "p", "u", (100, 100), (0, 100)),
        edge("P2", "p", "v", (100, 100), (200, 100)),
        # у t1 свой отвод вниз
        edge("B", "t1", "b", (100, 200), (200, 200)),
    ]
    lines = build_lines(nodes, edges, rules())
    li = lines.line_for(0)
    assert lines.edges_of_line[li] == [0, 1]        # A + C — одна линия
    # линия держится за граф ДВУМЯ узлами (p и t1) — это кусок магистрали,
    # а не тупиковый отвод, и Ду ему нужен
    assert lines.requires_diameter[li] is True


def test_chistyi_tupik_k_priboru_du_ne_trebuet_i_pri_dline_2():
    """Контроль к предыдущему: тот же отвод, но держащийся ОДНИМ узлом."""
    nodes = [node("p"), node("t1"), node("b"), node("d", "datchik")]
    edges = [
        edge("A", "p", "t1", (100, 100), (100, 200)),
        edge("C", "t1", "d", (100, 200), (100, 300)),
        edge("B", "t1", "b", (100, 200), (200, 200)),
    ]
    lines = build_lines(nodes, edges, rules())
    li = lines.line_for(0)
    assert lines.requires_diameter[li] is False


def test_samopetlya_ne_udvaivaet_stepen():
    nodes = [node("a"), node("loop"), node("b")]
    edges = [
        edge("e1", "a", "loop", (100, 0), (100, 100)),
        edge("e2", "loop", "loop", (100, 100), (100, 100)),
        edge("e3", "loop", "b", (100, 100), (100, 200)),
    ]
    lines = build_lines(nodes, edges, rules())
    assert lines.line_for(0) == lines.line_for(2)


def test_nan_v_tochke_ne_ronyaet_razbienie():
    nodes, edges = _troinik()
    edges[2]["target_point"] = [float("nan"), float("nan")]
    lines = build_lines(nodes, edges, rules())
    assert line_sets(lines) == {frozenset({0, 1}), frozenset({2})}


def test_granica_dopuska_rabotaet_tolko_na_vrezke():
    """Допуск судит ВРЕЗКУ (степень >= 3). На степени 2 угол не смотрят вовсе —
    колено 90° это та же линия, и это отдельное правило."""
    import math as _m
    nodes = [node("t"), node("a"), node("b"), node("c")]

    def troika(dev_deg):
        a = _m.radians(180.0 - dev_deg)
        return [
            edge("e1", "t", "a", (100, 100), (100, 200)),
            edge("e2", "t", "b", (100, 100),
                 (100 + 100 * _m.sin(a), 100 + 100 * _m.cos(a))),
            edge("e3", "t", "c", (100, 100), (0, 100)),   # третье ребро — врезка
        ]

    assert len(build_lines(nodes, troika(29.99), rules())) == 2   # сшилось
    assert len(build_lines(nodes, troika(30.5), rules())) == 3    # не сшилось


# ── конфиг: валидация не должна выключаться молча ──────────────────────────

def test_yaml_bez_classes_ronyaet_zagruzku(tmp_path):
    y = tmp_path / "p.yaml"
    y.write_text("diameter_lines:\n  transit: [trubaa]\n  stop: [nasoss]\n",
                 encoding="utf-8")
    with pytest.raises(LineRulesError, match="classes"):
        LineRules.from_project_yaml(y)


def test_transit_skalyarom_ronyaet_zagruzku():
    with pytest.raises(LineRulesError, match="списком"):
        LineRules.from_section({"transit": "truba", "stop": ["nasos"]})


def test_dubli_v_spiske_ronyayut_zagruzku():
    with pytest.raises(LineRulesError, match="Дубли|дубли"):
        LineRules.from_section({"transit": ["truba", "truba"], "stop": ["nasos"]})


def test_pustoi_transit_ronyaet_zagruzku():
    with pytest.raises(LineRulesError, match="transit"):
        LineRules.from_section({"transit": [], "stop": ["nasos"]})


def test_tol_ne_chislo_daet_LineRulesError():
    with pytest.raises(LineRulesError, match="не число"):
        LineRules.from_section(
            {"transit": ["truba"], "stop": ["nasos"], "collinear_tol_deg": "abc"})


def test_max_edges_ne_chislo_i_nol_dayut_LineRulesError():
    with pytest.raises(LineRulesError):
        LineRules.from_section(
            {"transit": ["truba"], "stop": ["nasos"], "no_diameter_max_edges": "abc"})
    with pytest.raises(LineRulesError, match="не меньше 1"):
        LineRules.from_section(
            {"transit": ["truba"], "stop": ["nasos"], "no_diameter_max_edges": 0})


def test_pustoi_no_diameter_ends_vyklyuchaet_pravilo():
    r = LineRules.from_section(
        {"transit": ["truba", "datchik"], "stop": ["nasos"], "no_diameter_ends": []})
    assert r.no_diameter_ends == frozenset()

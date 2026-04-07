#!/usr/bin/env python3
"""
Фазы 4-5: Тест binding с кириллическим профилем.

Проверяет:
  Фаза 4: DomainBindingConfig загрузка + UnifiedMatcher
  Фаза 5: UnifiedBinder интеграция с фейковыми данными
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pathlib import Path

YAML_PATH = Path(__file__).parent.parent / "configs" / "projects" / "thermohydraulics" / "domain_profile_cyrillic.yaml"

passed = 0
failed = 0
errors = []


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        msg = f"  ✗ {name}"
        if detail:
            msg += f"  — {detail}"
        print(msg)
        errors.append(name)


def main():
    global passed, failed

    print("=" * 60)
    print("Фазы 4-5: Binding с кириллическим профилем")
    print("=" * 60)

    # ═══════════════════════════════════════════════════
    # ФАЗА 4: DomainBindingConfig + UnifiedMatcher
    # ═══════════════════════════════════════════════════

    print("\n── Фаза 4.1: Загрузка DomainBindingConfig ──")
    from modules.binding.config import DomainBindingConfig

    try:
        cfg = DomainBindingConfig.from_yaml(str(YAML_PATH))
        check("DomainBindingConfig loaded", True)
    except Exception as e:
        print(f"FATAL: {e}")
        import traceback; traceback.print_exc()
        return 1

    # code_types
    check("equipment_code in code_types", "equipment_code" in cfg.code_types,
          f"got: {list(cfg.code_types.keys())}")
    check("diameter in code_types", "diameter" in cfg.code_types,
          f"got: {list(cfg.code_types.keys())}")

    if "equipment_code" in cfg.code_types:
        eq_ct = cfg.code_types["equipment_code"]
        check("equipment_code has match_patterns",
              len(eq_ct.match_patterns) >= 2,
              f"got: {len(eq_ct.match_patterns)}")
        check("equipment_code binding.target = 'node'",
              eq_ct.binding.target == "node")

        pattern_names = [mp.name for mp in eq_ct.match_patterns]
        check("'cyr_full' pattern", "cyr_full" in pattern_names, f"got: {pattern_names}")
        check("'cyr_bare' pattern", "cyr_bare" in pattern_names, f"got: {pattern_names}")

    if "diameter" in cfg.code_types:
        dm_ct = cfg.code_types["diameter"]
        check("diameter binding.target = 'edge'", dm_ct.binding.target == "edge")
        check("diameter has match_patterns", len(dm_ct.match_patterns) >= 1)

    # unit_to_classes
    check("unit_to_classes not empty", len(cfg.unit_to_classes) > 0,
          f"got: {len(cfg.unit_to_classes)}")
    check("ПД in unit_to_classes", "ПД" in cfg.unit_to_classes)
    check("ПО in unit_to_classes", "ПО" in cfg.unit_to_classes)
    check("КТ in unit_to_classes", "КТ" in cfg.unit_to_classes)

    if "ПД" in cfg.unit_to_classes:
        classes = cfg.unit_to_classes["ПД"]
        check("ПД maps to equipment classes", len(classes) > 10,
              f"got: {len(classes)} classes")
        check("armatura_ruchn in ПД classes", "armatura_ruchn" in classes)
        check("nasos in ПД classes", "nasos" in classes)

    # class_rules
    check("class_rules not empty", len(cfg.class_rules) > 0,
          f"got: {len(cfg.class_rules)}")
    if cfg.class_rules:
        arm_rule = cfg.class_rules.get("armatura_ruchn")
        check("armatura_ruchn rule exists", arm_rule is not None)
        if arm_rule:
            check("armatura_ruchn.kks_target = 'node'",
                  arm_rule.kks_target == "node", f"got: '{arm_rule.kks_target}'")
            check("armatura_ruchn expected_units has ПД",
                  "ПД" in arm_rule.expected_units)

        # non-bindable
        truba_rule = cfg.class_rules.get("truba")
        check("truba.kks_target = 'none'",
              truba_rule and truba_rule.kks_target == "none")

    # binding params
    check("node_max_distance > 0", cfg.node_max_distance > 0,
          f"got: {cfg.node_max_distance}")
    check("edge_max_distance > 0", cfg.edge_max_distance > 0,
          f"got: {cfg.edge_max_distance}")

    # ─── Фаза 4.2: UnifiedMatcher ───
    print("\n── Фаза 4.2: UnifiedMatcher ──")
    from modules.binding.matcher import UnifiedMatcher

    try:
        matcher = UnifiedMatcher(cfg)
        check("UnifiedMatcher created", True)
    except Exception as e:
        print(f"FATAL: {e}")
        import traceback; traceback.print_exc()
        return 1

    # Equipment codes — полные с блоком
    eq_full_cases = [
        ("Т1-ПД-585", "ПД", "585"),
        ("Т1-ПО-45", "ПО", "45"),
        ("Т1-ПР-3К", "ПР", "3"),
        ("Е1-ПД-32Л", "ПД", "32"),
        ("Т1-ПО-45СК", "ПО", "45"),
        ("Г1-КТ-100", "КТ", "100"),
    ]
    print("\n  ── Equipment (full with block) ──")
    for text, exp_unit, exp_num in eq_full_cases:
        m = matcher.match_equipment(text)
        if m:
            ok_unit = m.unit == exp_unit
            ok_num = m.num == exp_num
            check(f"match('{text}'): unit={m.unit} num={m.num}",
                  ok_unit and ok_num,
                  f"expected unit={exp_unit} num={exp_num}")
        else:
            check(f"match('{text}')", False, "returned None")

    # Equipment codes — bare (без блока)
    eq_bare_cases = [
        ("ПД-601Р", "ПД", "601"),
        ("ПО-119", "ПО", "119"),
        ("ВП-25", "ВП", "25"),
    ]
    print("\n  ── Equipment (bare, no block) ──")
    for text, exp_unit, exp_num in eq_bare_cases:
        m = matcher.match_equipment(text)
        if m:
            check(f"match('{text}'): unit={m.unit} num={m.num}",
                  m.unit == exp_unit and m.num == exp_num,
                  f"expected unit={exp_unit} num={exp_num}")
        else:
            check(f"match('{text}')", False, "returned None")

    # Non-codes — не должны матчиться
    non_cases = ["Привет", "01.02.2024", "Гл. инженер", "БЭРН", "123"]
    print("\n  ── Non-codes (should not match) ──")
    for text in non_cases:
        m = matcher.match_equipment(text)
        check(f"no_match('{text}')", m is None,
              f"unexpected match: {m.full if m else ''}")

    # Diameter
    diam_cases = [
        ("⌀50", 50),
        ("Ø125", 125),
        ("Ø 200", 200),
        ("ø20", 20),
    ]
    print("\n  ── Diameter ──")
    for text, exp_diam in diam_cases:
        d = matcher.match_diameter(text)
        if d:
            check(f"diameter('{text}') = {d.diameter}",
                  d.diameter == exp_diam,
                  f"expected {exp_diam}")
        else:
            check(f"diameter('{text}')", False, "returned None")

    # Diameter — не-диаметры
    non_diam = ["Т1-ПД-585", "123", "abc"]
    print("\n  ── Non-diameters ──")
    for text in non_diam:
        d = matcher.match_diameter(text)
        check(f"no_diameter('{text}')", d is None,
              f"unexpected: {d}")

    # ═══════════════════════════════════════════════════
    # ФАЗА 5: Интеграция UnifiedBinder
    # ═══════════════════════════════════════════════════

    print("\n── Фаза 5: UnifiedBinder интеграция ──")
    from modules.binding.engine import UnifiedBinder

    try:
        binder = UnifiedBinder(cfg)
        check("UnifiedBinder created", True)
    except Exception as e:
        print(f"FATAL: {e}")
        import traceback; traceback.print_exc()
        return 1

    # Фейковые данные
    ocr_blocks = [
        # Код оборудования рядом с нодой
        {"bbox": [100, 100, 250, 130], "text": "Т1-ПД-585", "confidence": 0.9},
        # Код оборудования bare
        {"bbox": [400, 400, 530, 430], "text": "ПД-601Р", "confidence": 0.85},
        # Диаметр рядом с ребром
        {"bbox": [300, 200, 350, 230], "text": "⌀50", "confidence": 0.9},
        # Шум — не должен привязаться
        {"bbox": [600, 600, 700, 630], "text": "Привет", "confidence": 0.5},
        # Ещё один код
        {"bbox": [500, 100, 650, 130], "text": "Т1-ПО-45СК", "confidence": 0.88},
    ]

    graph_nodes = [
        {"id": "n1", "class_name": "armatura_ruchn", "bbox": [90, 50, 160, 100], "type": "node"},
        {"id": "n2", "class_name": "nasos", "bbox": [390, 350, 450, 400], "type": "node"},
        {"id": "n3", "class_name": "teploobmen", "bbox": [490, 50, 560, 100], "type": "node"},
        # Не привязываемые
        {"id": "n4", "class_name": "truba", "bbox": [200, 200, 300, 210], "type": "node"},
        {"id": "n5", "class_name": "strelka", "bbox": [700, 700, 720, 720], "type": "node"},
    ]

    graph_edges = [
        {
            "id": "e1", "source": "n1", "target": "n2",
            "source_point": [125, 100], "target_point": [420, 350],
            "waypoints": [[125, 215], [420, 215]],
        },
    ]

    print("\n  ── bind() ──")
    try:
        report = binder.bind(ocr_blocks, graph_nodes, graph_edges)
        check("bind() succeeded", True)

        check(f"total_ocr_blocks = {report.total_ocr_blocks}",
              report.total_ocr_blocks == len(ocr_blocks))
        check(f"equipment_matched = {report.equipment_matched}",
              report.equipment_matched >= 3,
              f"expected >= 3, got {report.equipment_matched}")
        check(f"diameter_matched = {report.diameter_matched}",
              report.diameter_matched >= 1,
              f"expected >= 1, got {report.diameter_matched}")
        check(f"nodes_bound = {report.nodes_bound}",
              report.nodes_bound >= 2,
              f"expected >= 2, got {report.nodes_bound}")

        # Проверить конкретные привязки
        node_ids_bound = {nb.node_id for nb in report.node_bindings}
        node_codes = {nb.node_id: nb.code_match.full for nb in report.node_bindings}

        print(f"\n  Node bindings ({len(report.node_bindings)}):")
        for nb in report.node_bindings:
            print(f"    {nb.code_match.full} → {nb.node_id} ({nb.node_class}) "
                  f"dist={nb.distance}")

        print(f"\n  Edge bindings ({len(report.edge_bindings)}):")
        for eb in report.edge_bindings:
            print(f"    {eb.diam_match.text} → {eb.edge_id} dist={eb.distance}")

        # n4 (truba) и n5 (strelka) не должны быть привязаны
        check("truba not bound", "n4" not in node_ids_bound)
        check("strelka not bound", "n5" not in node_ids_bound)

        # Статистика
        print(f"\n  Статистика:")
        print(f"    equipment_matched: {report.equipment_matched}")
        print(f"    diameter_matched:  {report.diameter_matched}")
        print(f"    nodes_bound:       {report.nodes_bound}")
        print(f"    edges_bound:       {report.edges_bound}")
        print(f"    skipped_no_target: {report.skipped_no_target}")
        print(f"    unit_mismatches:   {report.unit_mismatches}")

    except Exception as e:
        check("bind()", False, str(e))
        import traceback; traceback.print_exc()

    # ═══════════════════════════════════════════════════
    # ФАЗА 5.1: Validator
    # ═══════════════════════════════════════════════════

    print("\n── Фаза 5.1: BindingValidator ──")
    from modules.binding.validator import BindingValidator

    validator = BindingValidator(cfg)

    # armatura_ruchn + ПД → valid
    v = validator.validate("ПД", "armatura_ruchn")
    check("validate(ПД, armatura_ruchn) is_valid", v.is_valid)

    # nasos + ПО → valid
    v = validator.validate("ПО", "nasos")
    check("validate(ПО, nasos) is_valid", v.is_valid)

    # truba → kks_target=none → not valid
    v = validator.validate("ПД", "truba")
    check("validate(ПД, truba) not valid", not v.is_valid)

    # annotation → none
    v = validator.validate("ПД", "annotation")
    check("validate(ПД, annotation) not valid", not v.is_valid)

    # ═══════════════════════════════════════════════════
    # ФАЗА 5.2: Compat layer (legacy KksMatcher)
    # ═══════════════════════════════════════════════════

    print("\n── Фаза 5.2: Compat (KksMatcher) ──")
    try:
        from modules.binding.compat import KksMatcher
        km = KksMatcher(cfg)
        check("KksMatcher(DomainBindingConfig) created", True)

        m = km.match("Т1-ПД-585")
        if m:
            check(f"KksMatcher.match('Т1-ПД-585') = {m.full}",
                  m.unit == "ПД" and m.num == "585")
        else:
            check("KksMatcher.match('Т1-ПД-585')", False, "returned None")

        m = km.match("ПД-601Р")
        if m:
            check(f"KksMatcher.match('ПД-601Р') = {m.full}",
                  m.unit == "ПД" and m.num == "601")
        else:
            check("KksMatcher.match('ПД-601Р')", False, "returned None")

    except Exception as e:
        check("KksMatcher compat", False, str(e))

    # ═══ Итог ═══
    print("\n" + "=" * 60)
    total = passed + failed
    print(f"Результат: {passed}/{total} passed, {failed} failed")
    if errors:
        print(f"\nПроваленные тесты:")
        for e in errors:
            print(f"  - {e}")
    if failed == 0:
        print("\n✓ Binding полностью работает с кириллическим профилем!")
        print("  Можно переходить к E2E тестированию на реальных данных.")
    else:
        print("\n✗ Есть проблемы — см. выше")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


def test_cyrillic_binding():
    """Pytest wrapper: runs all check()-based binding tests."""
    result = main()
    assert result == 0, f"{failed} checks failed: {errors}"

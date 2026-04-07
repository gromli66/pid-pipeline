#!/usr/bin/env python3
"""
Фаза 2: Проверка загрузки профиля domain_profile_cyrillic.yaml

Проверяет:
  1. Meta (name, languages, version)
  2. Patterns скомпилированы
  3. Classify работает на всех типах кодов
  4. Нормализация lat→cyr
  5. Grouping passes
  6. Noise engine
  7. has_any_target_pattern
  8. split_inline
  9. has_secondary_script
  10. Загрузка через project loader (thermohydraulics.yaml → domain_profile_cyrillic)
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
    print("Фаза 2: Проверка загрузки профиля")
    print("=" * 60)

    # ═══ 1. Загрузка ═══
    print("\n── 1. Загрузка ConfigDrivenProfile ──")
    from modules.ocr.domain_profile import ConfigDrivenProfile

    if not YAML_PATH.exists():
        print(f"FATAL: {YAML_PATH} not found")
        return 1

    try:
        p = ConfigDrivenProfile(yaml_path=str(YAML_PATH))
        check("Profile loaded", True)
    except Exception as e:
        print(f"FATAL: Cannot load profile: {e}")
        return 1

    # ═══ 2. Meta ═══
    print("\n── 2. Meta ──")
    check("name", p.name == "P&ID Кириллические коды", f"got: '{p.name}'")
    check("version", p.version == "2.0", f"got: '{p.version}'")
    check("languages contain 'ru'", "ru" in p.languages, f"got: {p.languages}")
    check("languages contain 'en'", "en" in p.languages, f"got: {p.languages}")

    # ═══ 3. Units ═══
    print("\n── 3. Units ──")
    check("units loaded", len(p.units) >= 10, f"got: {len(p.units)}")
    check("ПД in units", "ПД" in p.units)
    check("ПО in units", "ПО" in p.units)
    check("КТ in units", "КТ" in p.units)
    check("unit_re not empty", bool(p.unit_re))

    # ═══ 4. Patterns ═══
    print("\n── 4. Patterns скомпилированы ──")
    expected_patterns = [
        "full_code", "bare_code", "head_block_sys", "head_block",
        "head_code", "tail_sys_num", "tail_num_dash", "tail_code",
        "diameter", "diameter_inline", "fragment_ocr_block",
    ]
    for pn in expected_patterns:
        check(f"pattern '{pn}'", pn in p.patterns, "MISSING")

    check("full_patterns > 0", len(p.full_patterns) > 0, f"got: {len(p.full_patterns)}")
    check("head_patterns > 0", len(p.head_patterns) > 0, f"got: {len(p.head_patterns)}")
    check("tail_patterns > 0", len(p.tail_patterns) > 0, f"got: {len(p.tail_patterns)}")
    check("standalone_patterns > 0", len(p.standalone_patterns) > 0, f"got: {len(p.standalone_patterns)}")

    # ═══ 5. Classify ═══
    print("\n── 5. Classify ──")
    classify_cases = [
        ("Т1-ПД-585", "FULL"),
        ("Т1-ПО-45СК", "FULL"),
        ("ПД-601Р", "FULL"),
        ("Е1-ПД-32Л", "FULL"),
        ("Т1-ПД-373К Т1-ПД-372", "MULTI"),
        ("⌀50", "DN"),
        ("Ø 125", "DN"),
        ("Т1-ПД", "HEAD"),
        ("Т1", "HEAD"),
        ("-585К", "TAIL"),
        ("Привет мир", "OTHER"),
        ("", "OTHER"),
    ]
    for text, expected in classify_cases:
        result = p.classify(text)
        check(f"classify('{text}') = '{expected}'", result == expected,
              f"got: '{result}'")

    # ═══ 6. Normalization (lat→cyr) ═══
    print("\n── 6. Нормализация символов ──")
    check("normalize_enabled", p.normalize_enabled == True)

    norm_cases = [
        ("T", "Т"),    # Latin T → Cyrillic Т
        ("P", "Р"),    # Latin P → Cyrillic Р
        ("K", "К"),    # Latin K → Cyrillic К
        ("C", "С"),    # Latin C → Cyrillic С
        ("H", "Н"),    # Latin H → Cyrillic Н
        ("E", "Е"),    # Latin E → Cyrillic Е
        ("A", "А"),    # Latin A → Cyrillic А
        ("B", "В"),    # Latin B → Cyrillic В
        ("O", "О"),    # Latin O → Cyrillic О
    ]
    for lat, cyr in norm_cases:
        result = p.normalize_text(lat)
        check(f"normalize('{lat}') = '{cyr}'", result == cyr, f"got: '{result}'")

    # Проверка реального OCR-сценария: латинская T1 → кириллическая Т1
    result = p.normalize_text("T1-PD-585")
    check("normalize('T1-PD-585') содержит 'Т'", "Т" in result, f"got: '{result}'")

    # ═══ 7. Grouping passes ═══
    print("\n── 7. Grouping passes ──")
    gp = p.get_grouping_passes()
    check("grouping passes >= 4", len(gp) >= 4, f"got: {len(gp)}")

    pass_names = [g.name for g in gp]
    check("'full_codes' pass exists", "full_codes" in pass_names)
    check("'head_to_tail' pass exists", "head_to_tail" in pass_names)
    check("'diameters' pass exists", "diameters" in pass_names)

    # head_to_tail pass config
    h2t = next((g for g in gp if g.name == "head_to_tail"), None)
    if h2t:
        check("head_to_tail direction = below_only",
              h2t.direction == "below_only", f"got: '{h2t.direction}'")
        check("head_to_tail chain = True", h2t.chain == True)

    # ═══ 8. is_noise ═══
    print("\n── 8. Noise engine ──")
    noise_cases = [
        ("", True),
        ("01.02.2024", True),
        ("Гл. инженер", True),
        ("Конденсат подогревателя высокого давления", True),
        ("Т1-ПД-585", False),    # код → не шум
        ("ПД-601Р", False),
        ("⌀50", False),
    ]
    for text, expected in noise_cases:
        result = p.is_noise(text)
        check(f"is_noise('{text}') = {expected}", result == expected,
              f"got: {result}")

    # ═══ 9. has_any_target_pattern ═══
    print("\n── 9. has_any_target_pattern ──")
    target_pattern_cases = [
        ("Т1-ПД", True),       # head_code
        ("-ПД-585", True),     # tail_code (начинается с дефиса)
        ("⌀50", True),         # diameter
        ("Привет", False),
        ("12345", False),
    ]
    for text, expected in target_pattern_cases:
        result = p.has_any_target_pattern(text)
        check(f"has_any_target_pattern('{text}') = {expected}",
              result == expected, f"got: {result}")

    # ═══ 10. is_target ═══
    print("\n── 10. is_target ──")
    target_cases = [
        ("Т1-ПД-585", True),
        ("ПД-601Р", True),
        ("⌀50", True),
        ("Ø 125", True),
        ("Привет", False),
        ("Т1", False),          # только head без tail
    ]
    for text, expected in target_cases:
        result = p.is_target(text)
        check(f"is_target('{text}') = {expected}", result == expected,
              f"got: {result}")

    # ═══ 11. split_inline ═══
    print("\n── 11. split_inline ──")
    # Строка с несколькими кодами должна разбиться
    multi = "Т1-ПД-373К Т1-ПД-372"
    parts = p.split_inline(multi)
    check(f"split_inline('{multi}') > 1 parts", len(parts) > 1,
          f"got: {parts}")

    single = "Т1-ПД-585"
    parts = p.split_inline(single)
    check(f"split_inline('{single}') = 1 part", len(parts) == 1,
          f"got: {parts}")

    # ═══ 12. has_secondary_script ═══
    print("\n── 12. has_secondary_script ──")
    # Для кириллического профиля secondary_script = "cyrillic"
    # Значит has_secondary_script ищет кириллицу
    check("has_secondary_script('Привет')", p.has_secondary_script("Привет") == True)
    check("has_secondary_script('Hello')", p.has_secondary_script("Hello") == False)
    check("has_secondary_script('Т1-ПД-585')", p.has_secondary_script("Т1-ПД-585") == True)

    # ═══ 13. category_role mapping ═══
    print("\n── 13. category_role ──")
    role_cases = [
        ("FULL", "full"),
        ("MULTI", "full"),
        ("HEAD", "head"),
        ("HEAD_DN", "head"),
        ("TAIL", "tail"),
        ("DN", "standalone"),
        ("OTHER", "other"),
    ]
    for cat, expected_role in role_cases:
        result = p.category_role(cat)
        check(f"category_role('{cat}') = '{expected_role}'",
              result == expected_role, f"got: '{result}'")

    # ═══ 14. OCR коррекции (block_fixes) ═══
    print("\n── 14. OCR block_fixes ──")
    # Проверяем что block_fixes загрузились из ocr_corrections
    block_fixes = p._block_fixes
    check("block_fixes loaded", len(block_fixes) > 0, f"got: {len(block_fixes)}")
    check("'71' → 'Т1'", block_fixes.get("71") == "Т1", f"got: {block_fixes.get('71')}")
    check("'[1' → 'Т1'", block_fixes.get("[1") == "Т1", f"got: {block_fixes.get('[1')}")

    # Проверяем что preprocess применяет block_fixes
    result = p.classify("71-ПД-585")
    check("classify('71-ПД-585') after block_fix = FULL", result == "FULL",
          f"got: '{result}'")

    # ═══ 15. Project loader integration ═══
    print("\n── 15. Project loader ──")
    try:
        from app.services.project_loader import ProjectLoader
        loader = ProjectLoader(
            configs_dir=Path(__file__).parent.parent / "configs" / "projects"
        )
        project = loader.load("thermohydraulics")
        if project:
            check("project loaded", True)
            check("project.ocr.domain_profile_path is set",
                  project.ocr.domain_profile_path is not None,
                  f"got: {project.ocr.domain_profile_path}")

            dp_path = project.ocr.domain_profile_path
            if dp_path:
                check("domain_profile_path contains 'cyrillic'",
                      "cyrillic" in dp_path,
                      f"got: '{dp_path}'")

                # Проверяем что файл существует (относительный путь)
                abs_path = Path(dp_path)
                if not abs_path.is_absolute():
                    abs_path = Path(__file__).parent.parent / dp_path
                check("domain_profile file exists", abs_path.exists(),
                      f"path: {abs_path}")
        else:
            check("project loaded", False, "loader.load() returned None")
    except Exception as e:
        check("project loader import", False, str(e))

    # ═══ 16. Binding config (КРИТИЧЕСКИЙ ТЕСТ) ═══
    print("\n── 16. Binding config (DomainBindingConfig) ──")
    try:
        from modules.binding.config import DomainBindingConfig
        bcfg = DomainBindingConfig.from_yaml(str(YAML_PATH))
        has_ct = len(bcfg.code_types) > 0
        check("code_types loaded", has_ct,
              f"got: {len(bcfg.code_types)} (0 = БЛОКЕР: binding не будет работать!)")
        if has_ct:
            check("'equipment_code' in code_types",
                  "equipment_code" in bcfg.code_types)
            check("'diameter' in code_types",
                  "diameter" in bcfg.code_types)
        else:
            print()
            print("  ⚠⚠⚠  КРИТИЧЕСКИЙ БЛОКЕР  ⚠⚠⚠")
            print("  domain_profile_cyrillic.yaml не содержит секцию code_types!")
            print("  UnifiedBinder не сможет привязать коды к графу.")
            print("  Нужно добавить секции code_types + binding в YAML.")
            print()
    except Exception as e:
        check("DomainBindingConfig.from_yaml()", False, str(e))

    # ═══ Итог ═══
    print("\n" + "=" * 60)
    total = passed + failed
    print(f"Результат: {passed}/{total} passed, {failed} failed")
    if errors:
        print(f"\nПроваленные тесты:")
        for e in errors:
            print(f"  - {e}")
    if failed == 0:
        print("\n✓ Профиль полностью готов к использованию")
    else:
        print("\n✗ Есть проблемы — см. выше")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


def test_cyrillic_load():
    """Pytest wrapper: runs all check()-based profile loading tests."""
    result = main()
    assert result == 0, f"{failed} checks failed: {errors}"

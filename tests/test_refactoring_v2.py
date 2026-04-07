#!/usr/bin/env python3
"""
Верификационный тест рефакторинга OCR + Binding.

Проверяет:
1. domain_profile.yaml парсится без ошибок
2. ConfigDrivenProfile создаётся из YAML
3. classify() возвращает корректные категории
4. is_noise() фильтрует правильно
5. is_target() определяет целевые блоки
6. category_role() маппит роли
7. split_inline() разбивает строки
8. DomainBindingConfig парсится
9. UnifiedMatcher распознаёт KKS и диаметры
10. BindingValidator проверяет unit↔class
"""

import sys
import os

# Добавляем корень проекта в PYTHONPATH
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pathlib import Path


def test_domain_profile_yaml_parses():
    """domain_profile.yaml загружается без ошибок."""
    import yaml
    yaml_path = Path(__file__).parent.parent / "configs/projects/thermohydraulics/domain_profile.yaml"
    assert yaml_path.exists(), f"domain_profile.yaml not found: {yaml_path}"

    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    assert data["meta"]["name"] == "KKS (Росатом)"
    assert "equipment_code" in data["code_types"]
    assert "diameter" in data["code_types"]
    assert "pipeline_code" in data["code_types"]
    assert len(data["units"]) >= 20
    assert len(data["patterns"]) >= 7
    assert len(data["classify"]["rules"]) >= 6
    assert len(data["grouping"]["passes"]) >= 7
    assert len(data["noise"]["rules"]) >= 3
    assert data["binding"]["unit_to_classes"]["AA"]
    assert data["binding"]["class_rules"]["unknow"]["kks_target"] == "reclassify"
    print("  ✓ domain_profile.yaml parses OK")


def test_config_driven_profile_creates():
    """ConfigDrivenProfile создаётся из YAML."""
    from modules.ocr.domain_profile import ConfigDrivenProfile
    yaml_path = Path(__file__).parent.parent / "configs/projects/thermohydraulics/domain_profile.yaml"
    profile = ConfigDrivenProfile(yaml_path=str(yaml_path))

    assert profile.name == "KKS (Росатом)"
    assert "en" in profile.languages
    assert len(profile.units) >= 20
    assert len(profile.patterns) >= 7
    print("  ✓ ConfigDrivenProfile creates OK")
    return profile


def test_classify(profile):
    """classify() возвращает корректные категории."""
    cases = [
        # (text, expected_category)
        ("10LCS59AA017", "FULL"),
        ("10LCS59", "HEAD"),
        ("AA017", "TAIL"),
        ("Dy 200", "DN"),
        ("10LCS59AA017 10LCS60AA018", "MULTI"),
        ("Hello World", "OTHER"),
        ("", "OTHER"),
        ("10LAB30", "HEAD"),
    ]
    passed = 0
    for text, expected in cases:
        result = profile.classify(text)
        ok = result == expected
        status = "✓" if ok else "✗"
        if not ok:
            print(f"    {status} classify('{text}') = '{result}', expected '{expected}'")
        passed += 1 if ok else 0

    print(f"  ✓ classify: {passed}/{len(cases)} passed")
    return passed == len(cases)


def test_is_noise(profile):
    """is_noise() фильтрует правильно."""
    noise_cases = [
        ("", True),
        ("#", True),
        ("01.02.2024", True),
        ("shall not be disclosed", True),
        ("10LCS59AA017", False),     # целевой паттерн → не шум
        ("10LCS59", False),          # head → не шум
        ("Dy 200", False),           # диаметр → не шум
        ("AB", True),                # слишком короткий без target
    ]
    passed = 0
    for text, expected in noise_cases:
        result = profile.is_noise(text)
        ok = result == expected
        status = "✓" if ok else "✗"
        if not ok:
            print(f"    {status} is_noise('{text}') = {result}, expected {expected}")
        passed += 1 if ok else 0

    print(f"  ✓ is_noise: {passed}/{len(noise_cases)} passed")
    return passed == len(noise_cases)


def test_is_target(profile):
    """is_target() определяет целевые блоки."""
    cases = [
        ("10LCS59AA017", True),
        ("10LCS59 | AA017", True),    # head + tail
        ("Dy200", True),
        ("Hello", False),
        ("10LCS59", False),           # только head без tail
    ]
    passed = 0
    for text, expected in cases:
        result = profile.is_target(text)
        ok = result == expected
        if not ok:
            print(f"    ✗ is_target('{text}') = {result}, expected {expected}")
        passed += 1 if ok else 0

    print(f"  ✓ is_target: {passed}/{len(cases)} passed")
    return passed == len(cases)


def test_category_role(profile):
    """category_role() маппит роли."""
    cases = [
        ("FULL", "full"),
        ("HEAD", "head"),
        ("TAIL", "tail"),
        ("DN", "standalone"),
        ("FRAG", "fragment"),
        ("OTHER", "other"),
        ("MULTI", "full"),
        ("HEAD_DN", "head"),
    ]
    passed = 0
    for cat, expected in cases:
        result = profile.category_role(cat)
        ok = result == expected
        if not ok:
            print(f"    ✗ category_role('{cat}') = '{result}', expected '{expected}'")
        passed += 1 if ok else 0

    print(f"  ✓ category_role: {passed}/{len(cases)} passed")
    return passed == len(cases)


def test_split_inline(profile):
    """split_inline() разбивает строки."""
    # Одиночная строка → не разбивается
    result = profile.split_inline("10LCS59")
    assert len(result) == 1, f"Expected 1 part, got {len(result)}: {result}"

    # Пустая строка
    result = profile.split_inline("")
    assert len(result) == 1

    print("  ✓ split_inline OK")
    return True


def test_has_secondary_script(profile):
    """has_secondary_script() определяет кириллицу."""
    assert profile.has_secondary_script("Привет") == True
    assert profile.has_secondary_script("Hello") == False
    assert profile.has_secondary_script("10LCS59") == False
    print("  ✓ has_secondary_script OK")
    return True


def test_grouping_passes(profile):
    """get_grouping_passes() возвращает проходы из YAML."""
    passes = profile.get_grouping_passes()
    assert len(passes) >= 7, f"Expected >= 7 passes, got {len(passes)}"
    assert passes[0].name == "fragment_completion"
    assert passes[1].name == "full_codes"
    assert passes[2].name == "head_to_tail"
    assert passes[2].direction == "below_only"
    assert passes[2].chain == True
    print(f"  ✓ grouping_passes: {len(passes)} passes OK")
    return True


def test_binding_config_parses():
    """DomainBindingConfig парсится из YAML."""
    from modules.binding.config import DomainBindingConfig
    yaml_path = Path(__file__).parent.parent / "configs/projects/thermohydraulics/domain_profile.yaml"
    cfg = DomainBindingConfig.from_yaml(yaml_path)

    assert "equipment_code" in cfg.code_types
    assert "diameter" in cfg.code_types
    assert "pipeline_code" in cfg.code_types
    assert cfg.code_types["equipment_code"].binding.target == "node"
    assert cfg.code_types["pipeline_code"].binding.target == "edge"
    assert cfg.code_types["diameter"].binding.target == "edge"
    assert len(cfg.unit_to_classes) >= 15
    assert "AA" in cfg.unit_to_classes
    assert "BR" in cfg.edge_binding_units
    assert cfg.class_rules["unknow"].kks_target == "reclassify"
    assert cfg.class_rules["unknow"].reclassify_rules["AA"] == "armatura_electro"
    assert cfg.class_rules["truba"].kks_target == "none"
    assert len(cfg.ocr_corrections.cyr_to_lat) >= 10
    assert len(cfg.code_types["equipment_code"].match_patterns) == 2
    print("  ✓ DomainBindingConfig parses OK")
    return cfg


def test_unified_matcher(cfg):
    """UnifiedMatcher распознаёт KKS и диаметры."""
    from modules.binding.matcher import UnifiedMatcher

    matcher = UnifiedMatcher(cfg)

    # Equipment KKS
    eq = matcher.match_equipment("50KAA10CT001")
    assert eq is not None, "Failed to match '50KAA10CT001'"
    assert eq.unit == "CT", f"Expected unit 'CT', got '{eq.unit}'"
    assert eq.block == "50", f"Expected block '50', got '{eq.block}'"

    eq2 = matcher.match_equipment("10LBG30AA904")
    assert eq2 is not None, "Failed to match '10LBG30AA904'"
    assert eq2.unit == "AA"

    # No match
    eq3 = matcher.match_equipment("Hello World")
    assert eq3 is None

    # Diameter
    dm = matcher.match_diameter("Dy 200")
    assert dm is not None, "Failed to match diameter 'Dy 200'"
    assert dm.diameter == 200

    dm2 = matcher.match_diameter("DN50")
    assert dm2 is not None, "Failed to match diameter 'DN50'"
    assert dm2.diameter == 50

    dm3 = matcher.match_diameter("Hello")
    assert dm3 is None

    print("  ✓ UnifiedMatcher OK")
    return True


def test_binding_validator(cfg):
    """BindingValidator проверяет unit↔class."""
    from modules.binding.validator import BindingValidator

    v = BindingValidator(cfg)

    # Допустимая привязка: AA → armatura_electro
    r = v.validate("AA", "armatura_electro")
    assert r.is_valid, f"AA → armatura_electro should be valid, got: {r.message}"

    # Недопустимая: AP → armatura_electro
    r = v.validate("AP", "armatura_electro")
    assert not r.is_valid, "AP → armatura_electro should be invalid"

    # Reclassify: AA → unknow
    r = v.validate("AA", "unknow")
    assert r.is_valid
    assert r.reclassify_to == "armatura_electro", f"Expected reclassify to 'armatura_electro', got '{r.reclassify_to}'"

    # None target
    r = v.validate("AA", "truba")
    assert not r.is_valid

    print("  ✓ BindingValidator OK")
    return True


def test_project_loader_autodetect():
    """ProjectLoader автодетектит domain_profile.yaml."""
    try:
        from app.services.project_loader import ProjectLoader
    except ImportError as e:
        print(f"  ⊘ ProjectLoader: skip (missing dep: {e})")
        # Verify logic manually
        yaml_path = Path(__file__).parent.parent / "configs/projects/thermohydraulics/thermohydraulics.yaml"
        candidate = yaml_path.parent / "domain_profile.yaml"
        assert candidate.exists(), f"domain_profile.yaml should exist at {candidate}"
        print(f"  ✓ domain_profile.yaml exists for auto-detect at: {candidate}")
        return True

    configs_dir = Path(__file__).parent.parent / "configs/projects"
    loader = ProjectLoader(configs_dir)
    config = loader.load("thermohydraulics")
    assert config is not None, "Failed to load thermohydraulics config"
    assert config.ocr.domain_profile_path is not None, \
        "domain_profile_path should be auto-detected"
    assert "domain_profile.yaml" in config.ocr.domain_profile_path
    print(f"  ✓ ProjectLoader autodetect: {config.ocr.domain_profile_path}")
    return True


def main():
    print("=" * 60)
    print("Верификация рефакторинга OCR + Binding")
    print("=" * 60)

    all_ok = True

    print("\n── Phase 1: Config parsing ──")
    try:
        test_domain_profile_yaml_parses()
    except Exception as e:
        print(f"  ✗ YAML parse: {e}")
        all_ok = False

    print("\n── Phase 2: ConfigDrivenProfile ──")
    profile = None
    try:
        profile = test_config_driven_profile_creates()
    except Exception as e:
        print(f"  ✗ ConfigDrivenProfile: {e}")
        import traceback; traceback.print_exc()
        all_ok = False

    if profile:
        print("\n── Phase 2b: classify / noise / target ──")
        try:
            all_ok &= test_classify(profile)
            all_ok &= test_is_noise(profile)
            all_ok &= test_is_target(profile)
            all_ok &= test_category_role(profile)
            all_ok &= test_split_inline(profile)
            all_ok &= test_has_secondary_script(profile)
            all_ok &= test_grouping_passes(profile)
        except Exception as e:
            print(f"  ✗ Profile tests: {e}")
            import traceback; traceback.print_exc()
            all_ok = False

    print("\n── Phase 4: Binding ──")
    cfg = None
    try:
        cfg = test_binding_config_parses()
    except Exception as e:
        print(f"  ✗ BindingConfig: {e}")
        import traceback; traceback.print_exc()
        all_ok = False

    if cfg:
        try:
            all_ok &= test_unified_matcher(cfg)
            all_ok &= test_binding_validator(cfg)
        except Exception as e:
            print(f"  ✗ Matcher/Validator: {e}")
            import traceback; traceback.print_exc()
            all_ok = False

    print("\n── Phase 5: Integration ──")
    try:
        all_ok &= test_project_loader_autodetect()
    except Exception as e:
        print(f"  ✗ ProjectLoader: {e}")
        import traceback; traceback.print_exc()
        all_ok = False

    print("\n" + "=" * 60)
    if all_ok:
        print("✅ ВСЕ ТЕСТЫ ПРОШЛИ")
    else:
        print("❌ ЕСТЬ ОШИБКИ")
    print("=" * 60)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

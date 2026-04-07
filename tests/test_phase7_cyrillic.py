#!/usr/bin/env python3
"""
Тест Фазы 7: кириллический профиль из чистого YAML vs Python-класс.

Сравнивает ConfigDrivenProfile(tec_boiler/domain_profile.yaml) с PidCyrillicProfile.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pathlib import Path
from modules.ocr.domain_profile import ConfigDrivenProfile

# Загрузить Python-профиль из файла
import importlib.util
py_path = Path(__file__).parent.parent / "files_66" / "pid_cyrillic.py"
if not py_path.exists():
    # Попробуем другие места
    for candidate in [
        Path(__file__).parent.parent / "pid_cyrillic.py",
        Path(__file__).parent.parent / "configs" / "projects" / "tec_boiler" / "pid_cyrillic.py",
        Path(__file__).parent.parent / "modules" / "ocr" / "profiles" / "pid_cyrillic.py",
    ]:
        if candidate.exists():
            py_path = candidate
            break

yaml_path = Path(__file__).parent.parent / "configs" / "projects" / "tec_boiler" / "domain_profile.yaml"

# ═══ Тестовые строки из документации профиля ═══
TEST_CASES_CLASSIFY = [
    # (text, expected_category)
    ("Т1-ПД-585", "FULL"),
    ("Т1-ПО-45", "FULL"),
    ("Т1-ПП-24", "FULL"),
    ("Т1-ПР-3К", "FULL"),
    ("ПД-601Р", "FULL"),
    ("ПО-119", "FULL"),
    ("Е1-ПД-32Л", "FULL"),
    ("Т1-ПО-45СК", "FULL"),
    # Multi
    ("Т1-ПД-373К Т1-ПД-372", "MULTI"),
    # Диаметры
    ("⌀50", "DN"),
    ("Ø 125", "DN"),
    ("ø20", "DN"),
    # HEAD
    ("Т1-ПД", "HEAD"),
    ("Т1", "HEAD"),
    # TAIL
    ("ПД-585К", "FULL"),
    ("-585К", "TAIL"),
    # OTHER
    ("Привет мир", "OTHER"),
    ("", "OTHER"),
    ("01.02.2024", "OTHER"),  # noise, but classify returns OTHER
]

TEST_CASES_NOISE = [
    ("", True),
    ("#", True),
    ("01.02.2024", True),
    ("Гл. инженер", True),
    ("Т1-ПД-585", False),     # код → не шум
    ("ПД-601Р", False),
    ("⌀50", False),
    ("AB", True),              # слишком короткий
    ("Конденсат подогревателя высокого давления", True),  # длинное описание
]

TEST_CASES_TARGET = [
    ("Т1-ПД-585", True),
    ("ПД-601Р", True),
    ("⌀50", True),
    ("Ø 125", True),
    ("Привет", False),
    ("Т1", False),             # только head без tail
]


def main():
    print("=" * 60)
    print("Фаза 7: Кириллический профиль — чистый YAML")
    print("=" * 60)

    # ── Загрузить YAML-профиль ──
    if not yaml_path.exists():
        print(f"FAIL: {yaml_path} not found")
        return 1

    new = ConfigDrivenProfile(yaml_path=str(yaml_path))
    print(f"ConfigDrivenProfile loaded: {new.name}")
    print(f"  units: {len(new.units)}")
    print(f"  patterns: {len(new.patterns)}")
    print(f"  passes: {len(new.get_grouping_passes())}")

    # ── Загрузить Python-профиль (если есть) ──
    old = None
    if py_path.exists():
        try:
            spec = importlib.util.spec_from_file_location("pid_cyrillic", str(py_path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            old = mod.PidCyrillicProfile()
            print(f"PidCyrillicProfile loaded from: {py_path.name}")
        except Exception as e:
            print(f"WARNING: Cannot load PidCyrillicProfile: {e}")
    else:
        print("WARNING: pid_cyrillic.py not found — testing YAML-only")

    all_ok = True

    # ── classify тесты ──
    print("\n── classify ──")
    passed = 0
    for text, expected in TEST_CASES_CLASSIFY:
        result = new.classify(text)
        ok = result == expected
        if not ok:
            print(f"  FAIL: classify('{text}') = '{result}', expected '{expected}'")
            # Если есть Python-профиль, показать что он говорит
            if old:
                old_r = old.classify(text)
                print(f"        PidCyrillicProfile says: '{old_r}'")
        passed += 1 if ok else 0
    print(f"  classify: {passed}/{len(TEST_CASES_CLASSIFY)} passed")
    all_ok &= (passed == len(TEST_CASES_CLASSIFY))

    # ── is_noise тесты ──
    print("\n── is_noise ──")
    passed = 0
    for text, expected in TEST_CASES_NOISE:
        result = new.is_noise(text)
        ok = result == expected
        if not ok:
            print(f"  FAIL: is_noise('{text}') = {result}, expected {expected}")
            if old:
                print(f"        PidCyrillicProfile says: {old.is_noise(text)}")
        passed += 1 if ok else 0
    print(f"  is_noise: {passed}/{len(TEST_CASES_NOISE)} passed")
    all_ok &= (passed == len(TEST_CASES_NOISE))

    # ── is_target тесты ──
    print("\n── is_target ──")
    passed = 0
    for text, expected in TEST_CASES_TARGET:
        result = new.is_target(text)
        ok = result == expected
        if not ok:
            print(f"  FAIL: is_target('{text}') = {result}, expected {expected}")
            if old:
                print(f"        PidCyrillicProfile says: {old.is_target(text)}")
        passed += 1 if ok else 0
    print(f"  is_target: {passed}/{len(TEST_CASES_TARGET)} passed")
    all_ok &= (passed == len(TEST_CASES_TARGET))

    # ── A/B сравнение с Python-профилем (если есть) ──
    if old:
        print("\n── A/B: ConfigDrivenProfile vs PidCyrillicProfile ──")
        all_texts = [t for t, _ in TEST_CASES_CLASSIFY] + [t for t, _ in TEST_CASES_NOISE]
        mis = 0
        for text in all_texts:
            if not text:
                continue
            oc, nc = old.classify(text), new.classify(text)
            if oc != nc:
                print(f"  DIFF classify('{text}'): old={oc} new={nc}")
                mis += 1
            on, nn = old.is_noise(text), new.is_noise(text)
            if on != nn:
                print(f"  DIFF is_noise('{text}'): old={on} new={nn}")
                mis += 1
        if mis == 0:
            print("  A/B: FULL MATCH")
        else:
            print(f"  A/B: {mis} differences")
            all_ok = False

    # ── Итог ──
    print("\n" + "=" * 60)
    if all_ok:
        print("OK: Кириллический профиль работает из чистого YAML")
    else:
        print("FAIL: Есть расхождения")
    print("=" * 60)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())


def test_phase7_cyrillic():
    """Pytest wrapper: YAML-driven cyrillic profile vs Python class."""
    result = main()
    assert result == 0, "Phase 7 cyrillic profile checks failed — see stdout"

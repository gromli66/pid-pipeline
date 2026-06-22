#!/usr/bin/env python3
"""
check_profile_setup.py — Диагностика: все ли файлы на месте, работает ли цепочка профиль→привязка.

Запуск из корня проекта:
    python check_profile_setup.py
"""

import sys
import os
import traceback

# Добавить корень проекта в sys.path
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Цвета для терминала
OK = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m⚠\033[0m"
BOLD = "\033[1m"
RESET = "\033[0m"

errors = []
warnings = []


def check(label, condition, detail=""):
    if condition:
        print(f"  {OK} {label}")
        return True
    else:
        msg = f"{label}: {detail}" if detail else label
        errors.append(msg)
        print(f"  {FAIL} {label}  — {detail}")
        return False


def warn(label, detail=""):
    warnings.append(f"{label}: {detail}" if detail else label)
    print(f"  {WARN} {label}  — {detail}")


print(f"\n{BOLD}{'='*60}")
print("ДИАГНОСТИКА: OCR профиль → привязка")
print(f"{'='*60}{RESET}\n")

# ═══════════════════════════════════════════════════════
# 1. Проверка файлов
# ═══════════════════════════════════════════════════════
print(f"{BOLD}[1] Файлы на месте{RESET}")

files = {
    "modules/ocr/domain_profile.py": "Базовый класс (CodeMatch, DiamMatch)",
    "modules/ocr/profiles/pid_cyrillic.py": "Профиль PidCyrillicProfile",
    "modules/ocr/profiles/pid_cyrillic.yaml": "YAML конфиг профиля",
    "modules/ocr_validation/profile_adapter.py": "Адаптер профиля → привязка (НОВЫЙ)",
    "ui/tabs/ocr_binding_tab.py": "Вкладка привязки (обновлённая)",
    "configs/projects/thermohydraulics/thermohydraulics.yaml": "Конфиг проекта",
}

all_exist = True
for path, desc in files.items():
    exists = os.path.exists(path)
    check(f"{path}", exists, f"НЕ НАЙДЕН — {desc}")
    if not exists:
        all_exist = False

if not all_exist:
    print(f"\n{FAIL} Не все файлы на месте. Скопируйте недостающие и перезапустите.")
    sys.exit(1)

# ═══════════════════════════════════════════════════════
# 2. domain_profile.py содержит CodeMatch, DiamMatch, parse_code, parse_diameter
# ═══════════════════════════════════════════════════════
print(f"\n{BOLD}[2] domain_profile.py — новые классы и методы{RESET}")

with open("modules/ocr/domain_profile.py", encoding="utf-8") as f:
    dp_content = f.read()

check("CodeMatch dataclass", "class CodeMatch" in dp_content,
      "Нет class CodeMatch — файл старый?")
check("DiamMatch dataclass", "class DiamMatch" in dp_content,
      "Нет class DiamMatch — файл старый?")
check("parse_code абстрактный метод", "def parse_code" in dp_content,
      "Нет def parse_code — файл старый?")
check("parse_diameter абстрактный метод", "def parse_diameter" in dp_content,
      "Нет def parse_diameter — файл старый?")

# ═══════════════════════════════════════════════════════
# 3. pid_cyrillic.py реализует parse_code, parse_diameter
# ═══════════════════════════════════════════════════════
print(f"\n{BOLD}[3] pid_cyrillic.py — реализация методов{RESET}")

with open("modules/ocr/profiles/pid_cyrillic.py", encoding="utf-8") as f:
    pc_content = f.read()

check("import CodeMatch, DiamMatch", "CodeMatch" in pc_content and "DiamMatch" in pc_content,
      "Нет импорта CodeMatch/DiamMatch")
check("def parse_code", "def parse_code" in pc_content,
      "Нет реализации parse_code")
check("def parse_diameter", "def parse_diameter" in pc_content,
      "Нет реализации parse_diameter")

# ═══════════════════════════════════════════════════════
# 4. profile_adapter.py — существует, содержит нужные классы
# ═══════════════════════════════════════════════════════
print(f"\n{BOLD}[4] profile_adapter.py — адаптеры{RESET}")

with open("modules/ocr_validation/profile_adapter.py", encoding="utf-8") as f:
    pa_content = f.read()

check("ProfileKksMatcher", "class ProfileKksMatcher" in pa_content,
      "Нет класса ProfileKksMatcher")
check("ProfileDiameterMatcher", "class ProfileDiameterMatcher" in pa_content,
      "Нет класса ProfileDiameterMatcher")
check("SimpleKksBinder", "class SimpleKksBinder" in pa_content,
      "Нет класса SimpleKksBinder")
check("load_profile_from_project", "def load_profile_from_project" in pa_content,
      "Нет функции load_profile_from_project")

# ═══════════════════════════════════════════════════════
# 5. ocr_binding_tab.py — использует profile_adapter
# ═══════════════════════════════════════════════════════
print(f"\n{BOLD}[5] ocr_binding_tab.py — интеграция{RESET}")

with open("ui/tabs/ocr_binding_tab.py", encoding="utf-8") as f:
    tab_content = f.read()

check("_create_matchers метод", "_create_matchers" in tab_content,
      "Нет _create_matchers — файл старый?")
check("import ProfileKksMatcher", "ProfileKksMatcher" in tab_content,
      "Нет import ProfileKksMatcher — файл старый?")
check("import ProfileDiameterMatcher", "ProfileDiameterMatcher" in tab_content,
      "Нет import ProfileDiameterMatcher — файл старый?")
check("import SimpleKksBinder", "SimpleKksBinder" in tab_content,
      "Нет import SimpleKksBinder — файл старый?")
check("load_profile_from_project", "load_profile_from_project" in tab_content,
      "Нет load_profile_from_project — файл старый?")

# ═══════════════════════════════════════════════════════
# 6. thermohydraulics.yaml — правильный профиль
# ═══════════════════════════════════════════════════════
print(f"\n{BOLD}[6] thermohydraulics.yaml — конфиг{RESET}")

import yaml
with open("configs/projects/thermohydraulics/thermohydraulics.yaml", encoding="utf-8") as f:
    proj = yaml.safe_load(f)

ocr_cfg = proj.get("ocr", {})
check("ocr.profile_path указан",
      ocr_cfg.get("profile_path") == "modules/ocr/profiles/pid_cyrillic.py",
      f"Ожидается modules/ocr/profiles/pid_cyrillic.py, а стоит: {ocr_cfg.get('profile_path')}")
check("ocr.profile_class указан",
      ocr_cfg.get("profile_class") == "PidCyrillicProfile",
      f"Ожидается PidCyrillicProfile, а стоит: {ocr_cfg.get('profile_class')}")

# Предупреждение о лишних паттернах в text_recognition
tr = proj.get("text_recognition", {})
kks_patterns = tr.get("kks", {}).get("patterns", [])
dia_patterns = tr.get("pipe_diameter", {}).get("patterns", [])
if kks_patterns:
    warn("text_recognition.kks.patterns",
         f"Есть {len(kks_patterns)} паттерн(ов) — они не используются при наличии профиля. Можно удалить.")
if dia_patterns:
    warn("text_recognition.pipe_diameter.patterns",
         f"Есть {len(dia_patterns)} паттерн(ов) — они не используются при наличии профиля. Можно удалить.")

# ═══════════════════════════════════════════════════════
# 7. Импорты — попробовать загрузить модули
# ═══════════════════════════════════════════════════════
print(f"\n{BOLD}[7] Python импорты{RESET}")

try:
    from modules.ocr.domain_profile import BaseDomainProfile, CodeMatch, DiamMatch
    check("import domain_profile (CodeMatch, DiamMatch)", True)
except Exception as e:
    check("import domain_profile", False, str(e))

try:
    from modules.ocr.profiles.pid_cyrillic import PidCyrillicProfile
    check("import PidCyrillicProfile", True)
except Exception as e:
    check("import PidCyrillicProfile", False, str(e))

try:
    from modules.ocr_validation.profile_adapter import (
        load_profile_from_project, ProfileKksMatcher, ProfileDiameterMatcher, SimpleKksBinder,
    )
    check("import profile_adapter", True)
except Exception as e:
    check("import profile_adapter", False, str(e))

# ═══════════════════════════════════════════════════════
# 8. Функциональный тест — загрузка профиля из YAML
# ═══════════════════════════════════════════════════════
print(f"\n{BOLD}[8] Загрузка профиля из project YAML{RESET}")

try:
    profile = load_profile_from_project("configs/projects/thermohydraulics/thermohydraulics.yaml")
    check("load_profile_from_project()", profile is not None,
          "Вернул None — профиль не найден")
    if profile:
        check(f"profile.name = '{profile.name}'", True)
        check(f"units: {len(profile.units)} шт", len(profile.units) > 0,
              "Пустой список units")
except Exception as e:
    check("load_profile_from_project()", False, traceback.format_exc())
    profile = None

# ═══════════════════════════════════════════════════════
# 9. Тест parse_code
# ═══════════════════════════════════════════════════════
if profile:
    print(f"\n{BOLD}[9] profile.parse_code(){RESET}")
    code_tests = [
        ("Т1-ПД-585", True, "Т1", "ПД"),
        ("Т1-ПП-30Р", True, "Т1", "ПП"),
        ("ПД-601Р", True, "", "ПД"),
        ("71-ПД-328К", True, "Т1", "ПД"),
        ("Описание", False, "", ""),
        ("Ø50", False, "", ""),
    ]
    for text, should_match, exp_block, exp_sys in code_tests:
        try:
            cm = profile.parse_code(text)
            if should_match:
                ok = cm is not None and cm.block == exp_block and cm.system == exp_sys
                check(f"parse_code('{text}') → {cm.full if cm else 'None'}", ok,
                      f"Ожидался block={exp_block} sys={exp_sys}, получил {cm}")
            else:
                check(f"parse_code('{text}') → None", cm is None,
                      f"Должен быть None, получил {cm}")
        except Exception as e:
            check(f"parse_code('{text}')", False, str(e))

    # ═══════════════════════════════════════════════════════
    # 10. Тест parse_diameter
    # ═══════════════════════════════════════════════════════
    print(f"\n{BOLD}[10] profile.parse_diameter(){RESET}")
    dia_tests = [
        ("Ø50", True, 50),
        ("Ø 125", True, 125),
        ("Ø28*4", True, 28),
        ("ø20", True, 20),
        ("Т1-ПД-585", False, 0),
    ]
    for text, should_match, exp_dia in dia_tests:
        try:
            dm = profile.parse_diameter(text)
            if should_match:
                ok = dm is not None and dm.diameter == exp_dia
                check(f"parse_diameter('{text}') → {dm.diameter if dm else 'None'}", ok,
                      f"Ожидался diameter={exp_dia}")
            else:
                check(f"parse_diameter('{text}') → None", dm is None,
                      f"Должен быть None")
        except Exception as e:
            check(f"parse_diameter('{text}')", False, str(e))

    # ═══════════════════════════════════════════════════════
    # 11. Тест адаптеров → OcrBlockClassifier
    # ═══════════════════════════════════════════════════════
    print(f"\n{BOLD}[11] Адаптеры → OcrBlockClassifier{RESET}")
    try:
        from modules.ocr_validation.classifier import OcrBlockClassifier
        kks_m = ProfileKksMatcher(profile)
        dia_m = ProfileDiameterMatcher(profile)
        classifier = OcrBlockClassifier(dia_m, kks_m)

        blocks = [
            {"text": "Т1-ПД-585"},
            {"text": "Ø50"},
            {"text": "ПД-601Р"},
            {"text": "Конденсатор"},
        ]
        report = classifier.classify_all(blocks)
        check(f"KKS найдено: {report.kks_exact}", report.kks_exact >= 2,
              f"Ожидалось >= 2 KKS")
        check(f"Диаметров найдено: {report.diameter_exact}", report.diameter_exact >= 1,
              f"Ожидалось >= 1 диаметр")
        for cl in report.classifications:
            print(f"    [{cl.block_type.value:10s}] {cl.original_text}")
    except Exception as e:
        check("OcrBlockClassifier", False, traceback.format_exc())

# ═══════════════════════════════════════════════════════
# ИТОГ
# ═══════════════════════════════════════════════════════
print(f"\n{BOLD}{'='*60}")
if not errors:
    print(f"{OK} ВСЁ ОК — профиль настроен, все файлы на месте")
    if warnings:
        print(f"{WARN} {len(warnings)} предупреждений (не критично):")
        for w in warnings:
            print(f"    {WARN} {w}")
else:
    print(f"{FAIL} НАЙДЕНО {len(errors)} ОШИБОК:")
    for e in errors:
        print(f"    {FAIL} {e}")
print(f"{'='*60}{RESET}\n")

sys.exit(1 if errors else 0)

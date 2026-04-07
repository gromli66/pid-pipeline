import json, sys, os
from pathlib import Path

# Определяем корень проекта (откуда лежит скрипт)
ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from configs.projects.thermohydraulics.ocr_profile import KKSProfile
from modules.ocr.domain_profile import ConfigDrivenProfile

yaml_path = ROOT / "configs" / "projects" / "thermohydraulics" / "domain_profile.yaml"
if not yaml_path.exists():
    print("domain_profile.yaml not found:", yaml_path)
    sys.exit(1)

old = KKSProfile()
new = ConfigDrivenProfile(yaml_path=str(yaml_path))
print("Profiles loaded OK")

uid = sys.argv[1] if len(sys.argv) > 1 else None
if not uid:
    storage = ROOT / "storage" / "diagrams"
    if storage.exists():
        for d in sorted(storage.iterdir()):
            ocr = d / "ocr" / "ocr_result.json"
            if ocr.exists():
                uid = d.name
                print("Auto-found:", uid)
                break
    if not uid:
        print("No OCR results found in", storage)
        print("Usage: python compare_profiles.py <diagram_uid>")
        sys.exit(1)

ocr_path = ROOT / "storage" / "diagrams" / uid / "ocr" / "ocr_result.json"
if not ocr_path.exists():
    print("Not found:", ocr_path)
    sys.exit(1)

with open(ocr_path, encoding="utf-8") as f:
    data = json.load(f)

target = data.get("target", [])
secondary = data.get("secondary", [])
all_blocks = target + secondary
mis_cls, mis_noise, mis_target = [], [], []

for i, block in enumerate(all_blocks):
    text = block.get("text", "")
    oc, nc = old.classify(text), new.classify(text)
    if oc != nc:
        mis_cls.append((i, text[:60], oc, nc))
    on, nn = old.is_noise(text), new.is_noise(text)
    if on != nn:
        mis_noise.append((i, text[:60], on, nn))
    ot, nt = old.is_target(text), new.is_target(text)
    if ot != nt:
        mis_target.append((i, text[:60], ot, nt))

print("UID:", uid)
print("Blocks:", len(all_blocks), "(target=%d, secondary=%d)" % (len(target), len(secondary)))
print("classify  mismatches:", len(mis_cls))
print("is_noise  mismatches:", len(mis_noise))
print("is_target mismatches:", len(mis_target))

for label, lst in [("classify", mis_cls), ("is_noise", mis_noise), ("is_target", mis_target)]:
    if lst:
        print("\n--", label, "--")
        for i, t, o, n in lst[:15]:
            print("  [%d] %r  old=%s new=%s" % (i, t, o, n))
        if len(lst) > 15:
            print("  ... +%d more" % (len(lst) - 15))

total = len(mis_cls) + len(mis_noise) + len(mis_target)
if total == 0:
    print("\nOK: FULL MATCH")
else:
    print("\nWARNING: %d differences" % total)


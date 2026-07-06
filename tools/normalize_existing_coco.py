#!/usr/bin/env python3
"""Однократная миграция уже сохранённых coco_validated.json.

Прослойка в app/api/cvat.py чинит COCO при получении из CVAT. Но файлы, сохранённые
ДО фикса, всё ещё могут содержать RLE-маски (например «эллипс»). Этот скрипт
прогоняет по ним ту же нормализацию (RLE -> полигон; нераспознанное -> bbox).

Использование:
    # весь storage (по умолчанию берёт STORAGE_PATH из настроек или ./storage)
    python tools/normalize_existing_coco.py

    # конкретная папка или файл
    python tools/normalize_existing_coco.py storage/diagrams
    python tools/normalize_existing_coco.py .../detection/coco_validated.json

    # посмотреть что изменится, не записывая
    python tools/normalize_existing_coco.py --dry-run

Перед перезаписью рядом создаётся резервная копия <file>.bak (если нет --no-backup).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# repo root в sys.path, чтобы импортировать modules.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.coco_normalize import normalize_coco_segmentation  # noqa: E402


def _default_root() -> Path:
    try:
        from app.config import settings

        return Path(settings.STORAGE_PATH)
    except Exception:
        return Path("storage")


def _iter_coco_files(target: Path):
    if target.is_file():
        yield target
    elif target.is_dir():
        yield from target.rglob("coco_validated.json")


def _count_rle(coco: dict) -> int:
    return sum(
        1
        for a in coco.get("annotations", [])
        if isinstance(a.get("segmentation"), dict) and "counts" in a["segmentation"]
    )


def process(path: Path, dry_run: bool, backup: bool) -> bool:
    try:
        coco = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  ! пропуск (не читается): {path} -> {exc}")
        return False

    rle_before = _count_rle(coco)
    if rle_before == 0:
        print(f"  = без изменений (RLE нет): {path}")
        return False

    normalize_coco_segmentation(coco)
    rle_after = _count_rle(coco)
    converted = rle_before - rle_after

    action = "будет исправлено" if dry_run else "исправлено"
    print(f"  * {action}: {path}  (RLE {rle_before} -> полигон/bbox, осталось {rle_after})")

    if not dry_run:
        if backup:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        path.write_text(
            json.dumps(coco, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return converted > 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", nargs="?", default=None,
                    help="файл coco_validated.json или папка (по умолчанию STORAGE_PATH)")
    ap.add_argument("--dry-run", action="store_true", help="только показать, не записывать")
    ap.add_argument("--no-backup", action="store_true", help="не создавать .bak")
    args = ap.parse_args()

    target = Path(args.target) if args.target else _default_root()
    if not target.exists():
        print(f"Путь не найден: {target}")
        return 1

    files = list(_iter_coco_files(target))
    if not files:
        print(f"Не найдено coco_validated.json в: {target}")
        return 0

    print(f"Найдено файлов: {len(files)}")
    changed = 0
    for f in files:
        if process(f, args.dry_run, backup=not args.no_backup):
            changed += 1
    print(f"\nГотово. Затронуто файлов: {changed} из {len(files)}"
          + (" (dry-run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

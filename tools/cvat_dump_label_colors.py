#!/usr/bin/env python3
"""Снять цвета меток проекта CVAT (read-only).

Цвета меток CVAT назначает сам, если при создании проекта их не передали, — так
и вышло с новым русским проектом: раскраска классов уехала. Этот скрипт читает
цвета из СТАРОГО (английского) проекта и печатает готовый блок `class_colors:`
для YAML проекта, чтобы закрепить эталон в репозитории.

Ключи меток старого проекта — английские, то есть ровно `classes[].name` из YAML.
Классы, которых в старом проекте нет (его схема старее конфига), добираются из
проекта, указанного в --fallback-project-id: там метки русские, и обратный разбор
идёт через `display_labels`.

Запускать надо внутри api-контейнера: там есть app/, httpx и сеть до CVAT
(CVAT_URL=http://cvat_server:8080 виден только изнутри docker-сети). Каталог
`tools/` в контейнер НЕ смонтирован (docker-compose.yml монтирует app/worker/
modules/configs), поэтому файл сначала доставляется туда:

    docker compose exec -T api sh -c 'mkdir -p /app/tools && cat > /app/tools/cvat_dump_label_colors.py' < tools/cvat_dump_label_colors.py

    docker compose exec api python tools/cvat_dump_label_colors.py --list
    docker compose exec api python tools/cvat_dump_label_colors.py --project-id 2
    docker compose exec api python tools/cvat_dump_label_colors.py --project-id 2 --fallback-project-id 3

Обратный режим — вернуть цвета из конфига уже СОЗДАННОМУ проекту (метки там
русские, сопоставление ru → en идёт через `display_labels`). Без --yes только
показывает, что поменялось бы:

    docker compose exec api python tools/cvat_dump_label_colors.py --apply-to 3
    docker compose exec api python tools/cvat_dump_label_colors.py --apply-to 3 --yes

Без --yes ничего не пишет и не меняет — только читает.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.class_display import display_name, display_order, sort_key
from app.services.cvat_client import get_cvat_client
from app.services.project_loader import ProjectLoader


def list_projects(client) -> None:
    for p in client.get_projects():
        print(f"  project {p.get('id'):>4}  {p.get('name')}")


def colors_by_name(client, project_id: int) -> dict:
    """{имя метки: цвет} — как их отдаёт CVAT."""
    return {
        lbl["name"]: (lbl.get("color") or "").lower()
        for lbl in client.get_project_labels(project_id)
    }


def apply_colors(client, project_id: int, project_code: str, confirmed: bool) -> int:
    """Вернуть цвета из конфига меткам уже созданного проекта."""
    cfg = ProjectLoader().load(project_code)
    if cfg is None:
        print(f"[ERR] проект '{project_code}' не найден в конфигах")
        return 1
    if not cfg.class_colors:
        print(f"[ERR] в конфиге '{project_code}' нет блока class_colors — нечего применять")
        return 1

    # метки в проекте русские: ru → en, как в `to_internal`
    internal = {sort_key(display_name(cfg, cls.name)): cls.name for cls in cfg.classes}

    planned, skipped = [], []
    for lbl in client.get_project_labels(project_id):
        en = internal.get(sort_key(lbl["name"]))
        wanted = cfg.class_colors.get(en) if en else None
        if not wanted:
            skipped.append(lbl["name"])
        elif (lbl.get("color") or "").lower() != wanted:
            planned.append((lbl["id"], lbl["name"], lbl.get("color") or "—", wanted))

    for label_id, name, was, now in planned:
        print(f"  label {label_id:>4}  {was} -> {now}    {name}")
    if skipped:
        print(f"[ВНИМАНИЕ] метки без цвета в конфиге ({len(skipped)}): {skipped}")

    if not planned:
        print("[ОК] цвета уже совпадают с конфигом — менять нечего")
        return 0

    if not confirmed:
        print(f"\n[ПРОБНЫЙ ПРОГОН] поменялось бы меток: {len(planned)}. Повторите с --yes, чтобы записать.")
        return 0

    for label_id, name, _was, now in planned:
        client.set_label_color(label_id, now)
    print(f"[ГОТОВО] перекрашено меток: {len(planned)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="показать все проекты CVAT (id + имя)")
    ap.add_argument("--project-id", type=int, help="проект-эталон со старыми (английскими) метками")
    ap.add_argument("--fallback-project-id", type=int, help="проект-добивка для классов, которых нет в эталоне")
    ap.add_argument("--project-code", default="thermohydraulics", help="код проекта пайплайна (по умолчанию thermohydraulics)")
    ap.add_argument("--apply-to", type=int, metavar="PROJECT_ID", help="вернуть цвета из конфига существующему проекту CVAT")
    ap.add_argument("--yes", action="store_true", help="действительно записать цвета (без него — только показать)")
    args = ap.parse_args()

    client = get_cvat_client()

    if args.list:
        list_projects(client)
        return 0

    if args.apply_to:
        return apply_colors(client, args.apply_to, args.project_code, args.yes)

    if not args.project_id:
        ap.error("нужен --project-id, --apply-to (или --list, чтобы найти их)")

    cfg = ProjectLoader().load(args.project_code)
    if cfg is None:
        print(f"[ERR] проект '{args.project_code}' не найден в конфигах")
        return 1

    reference = colors_by_name(client, args.project_id)
    fallback = colors_by_name(client, args.fallback_project_id) if args.fallback_project_id else {}
    # метки добивки русские — ключуем их так же, как `to_internal`
    fallback_by_key = {sort_key(name): color for name, color in fallback.items()}

    picked: dict = {}
    source: dict = {}
    for cls in display_order(cfg):
        color = reference.get(cls.name)
        if color:
            picked[cls.name] = color
            source[cls.name] = "эталон"
            continue
        color = fallback_by_key.get(sort_key(display_name(cfg, cls.name)))
        if color:
            picked[cls.name] = color
            source[cls.name] = "добивка"

    print("# --- блок для configs/projects/%s/%s.yaml ---" % (args.project_code, args.project_code))
    print("class_colors:")
    for cls in display_order(cfg):
        color = picked.get(cls.name)
        if color:
            print(f'  {cls.name}: "{color}"    # {display_name(cfg, cls.name)}')
    print()

    total = len(cfg.classes)
    from_ref = sum(1 for s in source.values() if s == "эталон")
    from_fb = sum(1 for s in source.values() if s == "добивка")
    print(f"[ИТОГ] классов в конфиге: {total}; из эталона: {from_ref}; из добивки: {from_fb}")

    uncovered = [c.name for c in cfg.classes if c.name not in picked]
    if uncovered:
        print(f"[ВНИМАНИЕ] без цвета остались ({len(uncovered)}): {sorted(uncovered)}")

    extra = sorted(set(reference) - {c.name for c in cfg.classes})
    if extra:
        print(f"[ПРИМЕЧАНИЕ] метки эталона, которых нет в конфиге ({len(extra)}): {extra}")

    dupes = {}
    for name, color in picked.items():
        dupes.setdefault(color, []).append(name)
    repeated = {c: sorted(n) for c, n in dupes.items() if len(n) > 1}
    if repeated:
        print(f"[ВНИМАНИЕ] повторяющиеся цвета: {repeated}")
    else:
        print("[ОК] все цвета уникальны")

    return 0


if __name__ == "__main__":
    sys.exit(main())

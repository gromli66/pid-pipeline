"""
Отображаемые названия классов оборудования (en → ru).

Перевод применяется ТОЛЬКО на границах отображения: метки CVAT и списки классов
в клиенте. Внутри системы, в артефактах на диске и во всех остальных конфигах
класс всегда зовётся английским `name` из блока `classes:` YAML проекта.

Источник — блок `display_labels` в YAML. Пустой словарь = показывать английские
имена, то есть поведение любого проекта без этого блока не меняется.

Ключ сортировки нормализует регистр и «ё», чтобы алфавитный порядок не зависел
от того, как именно набрано название.
"""

import re
from typing import Dict, List, Sequence

# CVAT хранит имя метки в SafeCharField(max_length=64) и МОЛЧА обрезает лишнее
# (cvat/apps/engine/models.py). Проверяем сами, чтобы не поймать обрезанное имя
# на возврате аннотаций.
CVAT_LABEL_MAX_LEN = 64

# Цвет метки CVAT хранит строкой вида "#rrggbb" (cvat/apps/engine/serializers.py).
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def sort_key(name: str) -> str:
    """Ключ алфавитной сортировки: без регистра, «ё» приравнена к «е»."""
    return name.casefold().replace("ё", "е")


def display_name(config, en_name: str) -> str:
    """Отображаемое название класса; английское имя, если перевода нет."""
    return config.display_labels.get(en_name) or en_name


def display_order(config) -> List:
    """`config.classes`, отсортированные по отображаемому названию.

    Порядок самого `classes:` в YAML не трогается — от него зависят канонические
    `class_id` и `_shared_class_mapping`.
    """
    return sorted(config.classes, key=lambda cls: sort_key(display_name(config, cls.name)))


def to_internal(config) -> Dict[str, str]:
    """Обратная карта: ключ(отображаемое название) → английское имя."""
    return {
        sort_key(display_name(config, cls.name)): cls.name
        for cls in config.classes
    }


def validate(class_names: Sequence[str], labels: Dict[str, str]) -> None:
    """Проверить блок `display_labels`. Пустой словарь допустим.

    Raises:
        ValueError: перевод неполный, ведёт на несуществующий класс, не уникален,
            совпадает с английским именем класса или длиннее лимита CVAT.
    """
    if not labels:
        return

    known = set(class_names)

    unknown = sorted(set(labels) - known)
    if unknown:
        raise ValueError(
            f"display_labels: перевод задан для несуществующих классов: {unknown}"
        )

    missing = sorted(known - set(labels))
    if missing:
        raise ValueError(
            f"display_labels: нет перевода для классов: {missing}. "
            f"Метки CVAT строятся из этого блока — класс без перевода уедет в CVAT "
            f"под английским именем и сломает алфавитный порядок."
        )

    too_long = sorted(n for n in labels.values() if len(n) > CVAT_LABEL_MAX_LEN)
    if too_long:
        raise ValueError(
            f"display_labels: названия длиннее {CVAT_LABEL_MAX_LEN} символов "
            f"(CVAT обрежет их молча): {too_long}"
        )

    # Дубли отображаемых названий: по ним же идёт обратный разбор ru → en,
    # так что коллизия сделала бы возврат аннотаций неоднозначным.
    seen: Dict[str, str] = {}
    for en, ru in sorted(labels.items()):
        key = sort_key(ru)
        if key in seen:
            raise ValueError(
                f"display_labels: одинаковое название у классов "
                f"'{seen[key]}' и '{en}': {ru!r}"
            )
        seen[key] = en

    # Русское название не должно совпадать с английским именем ЛЮБОГО класса:
    # при разборе возврата ветка «имя уже каноническое» проверяется первой и
    # перехватила бы такое название раньше ветки перевода.
    canonical_keys = {sort_key(n): n for n in known}
    for en, ru in sorted(labels.items()):
        clash = canonical_keys.get(sort_key(ru))
        if clash:
            raise ValueError(
                f"display_labels: название класса '{en}' совпадает с внутренним "
                f"именем класса '{clash}': {ru!r}"
            )


def validate_colors(class_names: Sequence[str], colors: Dict[str, str]) -> None:
    """Проверить блок `class_colors`. Пустой словарь допустим.

    Пустой блок = цвет меток выбирает сам CVAT, то есть поведение до появления
    блока. Непустой обязан покрывать все классы: дыра означала бы, что часть
    меток снова красит CVAT, и раскраска разъедется от проекта к проекту.

    Raises:
        ValueError: цвет задан для несуществующего класса, покрытие неполное
            или значение не в формате `#rrggbb`.
    """
    if not colors:
        return

    known = set(class_names)

    unknown = sorted(set(colors) - known)
    if unknown:
        raise ValueError(
            f"class_colors: цвет задан для несуществующих классов: {unknown}"
        )

    missing = sorted(known - set(colors))
    if missing:
        raise ValueError(
            f"class_colors: нет цвета для классов: {missing}. "
            f"Класс без цвета CVAT покрасит сам, и раскраска разъедется."
        )

    bad = sorted(
        f"{en}={value!r}" for en, value in colors.items() if not _COLOR_RE.match(str(value))
    )
    if bad:
        raise ValueError(
            f"class_colors: цвет должен быть в формате '#rrggbb': {bad}"
        )

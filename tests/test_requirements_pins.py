# -*- coding: utf-8 -*-
"""Точные пины не расходятся между файлами требований (пункт 0.3x дороги).

`shapely==2.1.2` нужен и воркеру (арбитр наложений в раскладке зависит от API
STRtree), и НАБОРУ тестов (без него 22 теста раскладки молча уходят из сбора).
Общего файла у них нет: `dev.txt` не может тянуть `worker.txt` — там торч.
Значит пин физически живёт в двух местах, и разъехаться он может молча: воркер
поедет на одной версии, гейт — на другой. Сторож на это и стоит.

Проверяются ТОЛЬКО точные пины (`==`) и только те имена, что есть в обоих
файлах: `>=` — намеренно свободные требования, их сверять нечего.
"""
import re
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements"

_PIN = re.compile(r"^([A-Za-z0-9._-]+)==([^\s;#]+)")


def _pins(name: str) -> dict[str, str]:
    text = (REQUIREMENTS / name).read_text(encoding="utf-8")
    out = {}
    for line in text.splitlines():
        if m := _PIN.match(line.strip()):
            out[m.group(1).lower().replace("_", "-")] = m.group(2)
    return out


def test_exact_pins_agree_between_worker_and_dev():
    worker, dev = _pins("worker.txt"), _pins("dev.txt")
    common = sorted(set(worker) & set(dev))
    assert common, "ни одного общего точного пина — перенос shapely в dev.txt потерян?"

    mismatched = {name: (worker[name], dev[name]) for name in common
                  if worker[name] != dev[name]}
    assert not mismatched, (
        "точный пин разъехался между requirements/worker.txt и requirements/dev.txt "
        f"(воркер поедет на одной версии, гейт набора — на другой): {mismatched}"
    )


def test_shapely_is_pinned_for_the_suite():
    """Именно shapely: без него набор усыхает на 22 теста, а гейт видит только пол."""
    assert _pins("dev.txt").get("shapely"), (
        "shapely пропал из requirements/dev.txt — тесты раскладки уйдут из сбора "
        "через importorskip (docs/TESTING.md §7.1)"
    )

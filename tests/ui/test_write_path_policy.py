# -*- coding: utf-8 -*-
"""Пункт 1-41 дороги — сторож семьи `swallow`/`failure_key` и одна дверь запрета.

Пять поколений семьи чинили ОДИН И ТОТ ЖЕ дефект в разных вкладках: отказ
чтения артефакта, который вкладка потом пишет обратно, проходил молча, и
первое же сохранение затирало работу оператора. 0.5 → 1.23 → 1.x8 → 1.x10 →
1.x12 → 1.x14 → 1-41. Каждое поколение закрывало перечень, снятый грепом,
и каждое следующее находило, что перечень был неполон.

Здесь перечень перестаёт быть несущей конструкцией (`PROTOCOL §3`): вместо
списка «таких мест ровно N» политика спрашивается У САМОГО ЗАДАНИЯ. Задание,
способное СМОЛЧАТЬ об отказе, обязано объявить, что с этим молчанием делать:
либо `failure_key` (вкладка узнает и запретит слепую запись), либо `silent_ok`
— явная запись «глотаем осознанно» с причиной. Не объявил — не построился.
Ошибка в N перестаёт быть дефектом: новое задание, добавленное завтра, попадёт
под то же требование, и никакой перечень для этого обновлять не нужно.

Перечень ниже (`test_every_job_declares_its_read_policy`) остаётся
ДОКАЗАТЕЛЬСТВОМ, что сегодня боевые задания политику несут, — но лечение
на нём больше не держится.

Вторая половина файла — про дверь запрета. `_confirm_blind_overwrite` был
реализован ДВАЖДЫ (`base_graph_tab.py` и `ocr_binding_tab.py`), то есть
готовое расхождение ровно того класса, что 1-18 нашёл у дверей вопроса о
несохранённом: там две двери разошлись в ЧЕТЫРЁХ клетках из четырёх. Пункт
сводит их в одну и запирает это НАБЛЮДАЕМЫМ СВОЙСТВОМ, видимым всем путям
(`PROTOCOL §5`), а не совпадением текста диалогов.
"""
import importlib
import inspect
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                    # noqa: E402

pytest.importorskip("PySide6")

from ui.services.artifact_downloader import (                    # noqa: E402
    Fetch, Job, artifact, one,
)

TABS_DIR = Path(__file__).resolve().parents[2] / "ui" / "tabs"


# =========================================================================
# 1. Сторож: политика спрашивается у задания, а не у перечня
# =========================================================================

def _all_tab_jobs():
    """Все задания всех вкладок клиента: (модуль, откуда, задание).

    Список СНИМАЕТСЯ, а не пишется руками (`PROTOCOL §3`): каталог вкладок
    обходится `glob`, у каждого модуля берутся кортежи заданий уровня модуля
    и функции-построители (`*_jobs`) со всеми сочетаниями булевых аргументов.
    Новая вкладка попадает сюда сама.
    """
    found = []
    for path in sorted(TABS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        mod = importlib.import_module(f"ui.tabs.{path.stem}")
        for name, value in vars(mod).items():
            if isinstance(value, (tuple, list)) and any(
                    isinstance(v, Job) for v in value):
                found += [(mod.__name__, name, j) for j in value
                          if isinstance(j, Job)]
            elif callable(value) and name.endswith("_jobs") and \
                    getattr(value, "__module__", None) == mod.__name__:
                params = list(inspect.signature(value).parameters)
                combos = [{}] if not params else [
                    {p: v for p in params} for v in (False, True)]
                for kwargs in combos:
                    for j in value(**kwargs):
                        found.append((mod.__name__, f"{name}({kwargs})", j))
    return found


def test_the_sweep_finds_every_tab_that_downloads_anything():
    """Порог самого перебора: молчащий `glob` не должен читаться как «чисто»."""
    jobs = _all_tab_jobs()
    modules = {mod for mod, _, _ in jobs}

    assert len(jobs) >= 20, f"перебор собрал подозрительно мало заданий: {len(jobs)}"
    for expected in ("ui.tabs.base_graph_tab", "ui.tabs.junction_tab",
                     "ui.tabs.pipe_tab", "ui.tabs.ocr_binding_tab"):
        assert expected in modules, f"перебор не увидел {expected}: {sorted(modules)}"


def test_every_job_declares_its_read_policy():
    """Перечень как ДОКАЗАТЕЛЬСТВО: сегодня политику несут все задания.

    Лечение на этом перечне не держится — его держит `Job.__post_init__`
    (тест ниже). Здесь просто предъявлена таблица «задание × свойство».
    """
    undeclared = [
        f"{mod}:{where} -> {job.fetches[0].source}"
        for mod, where, job in _all_tab_jobs()
        if job.can_be_silent and not (job.failure_key or job.silent_ok)
    ]
    assert undeclared == [], (
        "задание способно смолчать об отказе и не объявило политику: "
        f"{undeclared}")


def test_a_silent_job_cannot_be_built_without_a_declaration():
    """Д1 сторожа: инъекция в ТЕКУЩЕЕ дерево — недообъявленное задание не строится."""
    with pytest.raises(ValueError, match="failure_key"):
        one(artifact("coco_validated", "coco.json"))

    with pytest.raises(ValueError, match="failure_key"):
        Job((artifact("graph_validated", "g.json"),
             artifact("graph_json", "g.json")), required=True)


def test_declaring_both_is_a_contradiction():
    """`failure_key` и `silent_ok` вместе — противоречие, а не усиление."""
    with pytest.raises(ValueError):
        one(artifact("graph_canvas", "c.json"),
            failure_key="canvas_download_failed",
            silent_ok="и то и другое")


def test_a_job_that_cannot_be_silent_needs_no_declaration():
    """Порог с другой стороны: сторож не шире своего смысла.

    Обязательное задание из ОДНОГО кандидата отказ поднимает наверх, значит
    смолчать не может — требовать от него объявления не за что. То же и
    у необязательного с пустым `swallow`.
    """
    assert one(artifact("original_image", "o.png"), required=True).can_be_silent \
        is False
    assert one(artifact("pipe_mask", "p.png"), swallow=()).can_be_silent is False


def test_silence_is_declared_for_every_shape_that_can_swallow():
    """Три формы молчания названы поимённо — чтобы сторож не сузился молча."""
    chain = (Fetch("a.json", "a", art_type="graph_validated"),
             Fetch("a.json", "a", art_type="graph_json"))

    # (1) цепочка: фолбэк берётся вместо предпочтённого
    assert Job(chain, required=True, silent_ok="проба").can_be_silent is True
    # (2) необязательное задание: отказ глотается целиком
    assert one(artifact("coco_validated", "c.json"),
               silent_ok="проба").can_be_silent is True
    # (3) оба сразу
    assert Job(chain, silent_ok="проба").can_be_silent is True


# =========================================================================
# 2. Одна дверь запрета слепой перезаписи
# =========================================================================

def _tab_classes():
    """Вкладки, которые пишут артефакты обратно на сервер, — все четыре."""
    from ui.tabs.base_graph_tab import BaseGraphTab
    from ui.tabs.junction_tab import JunctionTab
    from ui.tabs.ocr_binding_tab import OcrBindingTab
    from ui.tabs.pipe_tab import PipeTab
    return {"BaseGraphTab": BaseGraphTab, "JunctionTab": JunctionTab,
            "OcrBindingTab": OcrBindingTab, "PipeTab": PipeTab}


def test_the_blind_overwrite_door_is_one_for_every_writing_tab():
    """Дверь одна: у всех четырёх вкладок это ОДНА И ТА ЖЕ функция.

    Совпадение текста диалогов такой проверкой не является (`PROTOCOL §5`):
    две копии с одинаковым текстом расходятся на первой же правке, а 1-18
    замерил, что расходятся они в четырёх клетках из четырёх.
    """
    doors = {name: cls._confirm_blind_overwrite
             for name, cls in _tab_classes().items()}
    distinct = {getattr(d, "__code__", d) for d in doors.values()}

    assert len(distinct) == 1, (
        "дверь запрета реализована больше одного раза: "
        f"{ {n: d.__qualname__ for n, d in doors.items()} }")


def test_a_new_ability_of_the_door_is_seen_by_every_writing_tab(monkeypatch):
    """Наблюдаемое свойство, видимое ВСЕМ путям: новое умение двери.

    Рабочая форма замка на «свёл два пути в один» (`PROTOCOL §5`): дать двери
    умение, которого у неё не было, и показать, что его видят все вкладки.
    Копипаста этот тест не проходит по построению.
    """
    from ui.tabs.blind_overwrite import BlindOverwriteGuard

    seen = []
    monkeypatch.setattr(BlindOverwriteGuard, "_confirm_blind_overwrite",
                        lambda self, *a: seen.append(type(self).__name__) or True)

    for name, cls in _tab_classes().items():
        assert cls._confirm_blind_overwrite(object.__new__(cls)) is True

    assert sorted(seen) == sorted(_tab_classes()), (
        f"умение двери увидели не все вкладки: {seen}")


def test_every_writing_tab_keeps_the_registry_of_unreadable_artifacts():
    """Вторая половина той же двери: реестр непрочитанного тоже общий."""
    from ui.tabs.blind_overwrite import BlindOverwriteGuard

    for name, cls in _tab_classes().items():
        assert issubclass(cls, BlindOverwriteGuard), \
            f"{name} держит свой реестр непрочитанного мимо общей двери"
        assert isinstance(cls._BLIND_WRITE_WARNING, dict) and \
            cls._BLIND_WRITE_WARNING, \
            f"{name} не объявил, о чём предупреждать при слепой записи"

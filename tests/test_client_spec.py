# -*- coding: utf-8 -*-
"""Сторож упаковки клиента: PyInstaller-спека обязана класть vendored-биндинг
libavoid туда, где его ищет загрузчик.

Зачем тест. `modules/graph/core/layout/_avoid_binding` резолвит путь от
`__file__` (parents[4]/vendor/adaptagrams/<платформа>), а у замороженного
модуля это `<MEIPASS>/vendor/adaptagrams/...`. Пока спека кладёт файлы с тем
же относительным путём от корня репо — в бандле сходится без правок кода
(проверено запуском собранного клиента). Если файлы из спеки выпадут, клиент
НЕ упадёт: он молча уйдёт на самописную лестницу с одной строкой INFO в лог,
то есть Этап B окажется выключен, а глазами это неотличимо от «роутер сегодня
не в духе». Такую тишину и ловит этот тест.

Тест не ищет строчки регуляркой — он ИСПОЛНЯЕТ спеку с заглушками вместо
классов PyInstaller и смотрит фактические списки `datas`/`binaries`. Значит
поймает и переименование переменных, и перенос блока, и опечатку в пути.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "deploy_ready" / "pid_client_windows.spec"


def _run_spec():
    """Исполнить спеку с заглушками PyInstaller -> (datas, binaries).

    Заглушки ставятся В sys.modules ДО exec: спека делает
    `from PyInstaller.utils.hooks import ...`, и подмена одних только globals
    её не перехватывает — импорт вернул бы настоящие сборщики (20 секунд
    работы, а на машине без PyInstaller — ImportError). Нас интересует
    собственный код спеки, а не содержимое PySide6.
    """
    if not SPEC.is_file():
        pytest.skip(f"нет спеки клиента: {SPEC}")

    import sys
    import types

    captured = {}

    def _analysis(*a, **kw):
        captured["datas"] = list(kw.get("datas") or [])
        captured["binaries"] = list(kw.get("binaries") or [])
        return type("A", (), {"pure": None, "scripts": None,
                              "binaries": None, "datas": None})()

    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_submodules = lambda *a, **k: []
    hooks.collect_all = lambda *a, **k: ([], [], [])
    utils = types.ModuleType("PyInstaller.utils")
    utils.hooks = hooks
    root = types.ModuleType("PyInstaller")
    root.utils = utils
    saved = {k: sys.modules.get(k) for k in
             ("PyInstaller", "PyInstaller.utils", "PyInstaller.utils.hooks")}
    sys.modules.update({"PyInstaller": root, "PyInstaller.utils": utils,
                        "PyInstaller.utils.hooks": hooks})
    g = {
        "__file__": str(SPEC),
        "SPECPATH": str(SPEC.parent),
        "Analysis": _analysis,
        "PYZ": lambda *a, **k: None,
        "EXE": lambda *a, **k: None,
        "COLLECT": lambda *a, **k: None,
    }
    try:
        exec(compile(SPEC.read_text(encoding="utf-8"), str(SPEC), "exec"), g)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    if "datas" not in captured:
        pytest.fail("спека не вызвала Analysis — структура изменилась")
    return captured["datas"], captured["binaries"]


def test_loader_path_is_inside_repo():
    """Инвариант, на котором держится упаковка: путь загрузчика — подкаталог
    репозитория, значит его можно положить в бандл «как есть»."""
    from modules.graph.core.layout import _avoid_binding as ab

    rel = os.path.relpath(ab._platform_dir(), REPO).replace("\\", "/")
    assert rel.startswith("vendor/adaptagrams/"), (
        f"загрузчик смотрит вне репо ({rel}) — упаковка в спеке перестала "
        "соответствовать; сверить _avoid_binding._platform_dir() и спеку")


def test_spec_packs_vendored_binding_files():
    """Оба файла биндинга попадают в сборку, и КАЖДЫЙ — по пути, который
    после заморозки резолвит загрузчик."""
    from modules.graph.core.layout import _avoid_binding as ab

    d = ab._platform_dir()
    if not d.is_dir():
        pytest.skip(f"нет каталога биндинга для этой платформы: {d}")

    datas, binaries = _run_spec()
    want_dest = os.path.relpath(d, REPO)          # 'vendor\\adaptagrams\\win'
    packed = {os.path.basename(src): dest for src, dest in datas + binaries}

    proxy = "adaptagrams.py"
    assert proxy in packed, (
        f"SWIG-прокси {proxy} не попадает в сборку — замороженный клиент "
        "не найдёт биндинг и молча уйдёт на самописную лестницу")
    assert os.path.normpath(packed[proxy]) == os.path.normpath(want_dest), (
        f"{proxy} кладётся в {packed[proxy]}, а загрузчик ищет в {want_dest}")

    bins = [f for f in packed if f.lower().endswith((".pyd", ".so"))]
    assert bins, "бинарь биндинга (.pyd/.so) не попадает в сборку"
    for b in bins:
        assert os.path.normpath(packed[b]) == os.path.normpath(want_dest), (
            f"{b} кладётся в {packed[b]}, а загрузчик ищет в {want_dest}")


def test_binary_goes_to_binaries_not_datas():
    """.pyd обязан идти в binaries: только там PyInstaller анализирует его
    зависимые DLL (MSVC runtime) и кладёт их рядом. В datas он попадёт как
    «просто файл» — и на чистой машине без runtime импорт упадёт."""
    from modules.graph.core.layout import _avoid_binding as ab

    if not ab._platform_dir().is_dir():
        pytest.skip("нет каталога биндинга для этой платформы")
    datas, binaries = _run_spec()
    in_datas = [os.path.basename(s) for s, _ in datas
                if s.lower().endswith((".pyd", ".so"))]
    in_bins = [os.path.basename(s) for s, _ in binaries
               if s.lower().endswith((".pyd", ".so"))]
    assert not in_datas, f"бинарь биндинга ушёл в datas: {in_datas}"
    assert in_bins, "бинарь биндинга не найден в binaries"


def test_binding_pycache_not_packed():
    """Из каталога биндинга __pycache__ не тащим: скомпилированный кэш
    старого прокси рядом со свежим бинарём — источник тихих рассинхронов.

    Проверка узкая (только vendor/adaptagrams): в сборку по другим причинам
    попадает и чужой __pycache__ (обход ui/resources) — это предсуществующее
    поведение спеки, не предмет этого теста."""
    if not (REPO / "vendor" / "adaptagrams").is_dir():
        pytest.skip("нет vendor/adaptagrams")
    datas, binaries = _run_spec()
    bad = [s for s, _ in datas + binaries
           if "adaptagrams" in s.replace("\\", "/") and "__pycache__" in s]
    assert not bad, f"в сборку попал __pycache__ биндинга: {bad[:3]}"

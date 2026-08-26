# -*- coding: utf-8 -*-
"""Штамп версии коробки в образе prtx.

Сервис приезжает готовым образом (в `docker-compose.yml` у `prtx` нет секции
`build`), поэтому по работающему контейнеру нельзя установить, какой конвертер
внутри. Штамп — единственный способ; здесь заперты его разбор и живучесть.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "docker" / "prtx" / "server.py"


def _load(monkeypatch, stamp_path):
    """Загрузить server.py, подсунув ему путь к файлу штампа."""
    real_open = open

    def fake_open(path, *a, **kw):
        if str(path) == "/opt/box/box.commit":
            return real_open(stamp_path, *a, **kw)
        return real_open(path, *a, **kw)

    monkeypatch.setitem(sys.modules, "_prtx_server_probe", None)
    spec = importlib.util.spec_from_file_location("_prtx_server_probe", SERVER)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setattr("builtins.open", fake_open)
    spec.loader.exec_module(mod)
    return mod


def test_shtamp_razbiraetsya(tmp_path, monkeypatch):
    """Обычный файл, как его пишет build.ps1."""
    f = tmp_path / "box.commit"
    f.write_text("commit=82d012c\ndirty=0\ndiam=1\nbuilt=2026-08-26 11:12\n",
                 encoding="utf-8")
    mod = _load(monkeypatch, f)
    assert mod.BOX == {"commit": "82d012c", "dirty": "0", "diam": "1",
                       "built": "2026-08-26 11:12"}


def test_bom_ne_prilipaet_k_pervomu_klyuchu(tmp_path, monkeypatch):
    """`Set-Content -Encoding utf8` в Windows PowerShell 5.1 пишет BOM.

    Без `utf-8-sig` первый ключ приехал бы как `﻿commit` и /health отдавал
    бы версию, которую никто не найдёт.
    """
    f = tmp_path / "box.commit"
    f.write_bytes("commit=82d012c\ndirty=1\n".encode("utf-8-sig"))
    mod = _load(monkeypatch, f)
    assert mod.BOX["commit"] == "82d012c"
    assert mod.BOX["dirty"] == "1"


def test_bez_faila_servis_ne_padaet(tmp_path, monkeypatch):
    """Образ, собранный до появления штампа, обязан работать и честно молчать."""
    mod = _load(monkeypatch, tmp_path / "нет-такого-файла")
    assert mod.BOX == {}


def test_kanareika_i_shtamp_opisany_v_readme():
    """Обещание в README и код не должны разъезжаться."""
    readme = (SERVER.parent / "README.md").read_text(encoding="utf-8")
    for word in ("канарейка", "box.commit", "dirty=1", "82d012c"):
        assert word in readme, word


def test_dockerfile_kladet_shtamp_v_obraz():
    dockerfile = (SERVER.parent / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY box.commit   /opt/box/box.commit" in dockerfile


def test_build_ps1_padaet_bez_fazy_du():
    """Канарейка обязана быть именно проверкой с `throw`, а не предупреждением."""
    ps1 = (SERVER.parent / "build.ps1").read_text(encoding="utf-8-sig")
    assert "diameter_value" in ps1 and "channelDiameters" in ps1
    canary = ps1[ps1.index("Канарейка"):ps1.index("Штамп версии")]
    assert canary.count("throw") == 3, "проверка не падает, а только предупреждает"

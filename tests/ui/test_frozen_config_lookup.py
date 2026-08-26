# -*- coding: utf-8 -*-
"""Конфиг проекта в СОБРАННОМ клиенте.

Замер 2026-08-26 на боевой сборке: питоний код PyInstaller кладёт внутрь
бандла, а `configs` — рядом с exe (`build_client.ps1`). Путь, посчитанный от
`__file__`, упирался в бандл, конфиг не находился, и вкладка привязки писала
«Диаметры выключены: каталог конфигов проекта не задан». То есть Ду в
собранном клиенте не ставились ВООБЩЕ, хотя из исходников всё работало.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

CODE = "thermohydraulics"


class _Stub:
    """Только то, что метод спрашивает у себя."""

    def _get_project_code(self):
        return CODE


def _lookup():
    from ui.widgets.diagram_workspace import DiagramWorkspace
    return DiagramWorkspace._get_project_config_path(_Stub())


def _make_layout(root):
    cfg = root / "configs" / "projects" / CODE
    cfg.mkdir(parents=True)
    yaml = cfg / (CODE + ".yaml")
    yaml.write_text("diameter_lines:\n  transit: [truba]\n", encoding="utf-8")
    return yaml


def test_sobrannyi_klient_nahodit_konfig_ryadom_s_exe(tmp_path, monkeypatch):
    """Раскладка боевой сборки: exe и `configs` в одной папке."""
    yaml = _make_layout(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "PID-Client.exe"))
    monkeypatch.chdir(tmp_path.parent)      # запуск НЕ из папки клиента

    found = _lookup()

    assert found, "конфиг не найден — Ду в собранном клиенте не поставить"
    assert os.path.samefile(found, yaml), found


def test_iz_ishodnikov_po_prezhnemu_rabotaet(monkeypatch):
    """Обычный запуск `python -m ui.main` ломаться не должен."""
    monkeypatch.delattr(sys, "frozen", raising=False)

    found = _lookup()

    assert found.endswith(CODE + ".yaml"), found
    assert os.path.exists(found)

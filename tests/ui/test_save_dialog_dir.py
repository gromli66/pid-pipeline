# -*- coding: utf-8 -*-
"""Пункт дороги 0.2 — редактор не сорит сохранениями в корень репозитория.

Дефект: `BaseGraphEditor.save_graph()` без пути отдавал диалогу ОТНОСИТЕЛЬНОЕ
имя `graph_edited.json`, поэтому диалог открывался в CWD; у клиента, запущенного
из корня репо, там и оседали локальные сейвы (20 файлов корпуса, MEASUREMENTS
§29.17-29.18). Перенос корпуса в папку это не лечит — файлы появятся снова.

Тест бьёт в сам стык «редактор → диалог»: проверяется путь, который редактор
ПРЕДЛАГАЕТ диалогу, а не результат записи. CWD принудительно ставится в корень
репо — условия того самого запуска клиента.
"""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _patch_dialog(monkeypatch, answer: str = ""):
    """Подменить QFileDialog в модуле редактора; вернуть перехваченные аргументы."""
    from ui.editors import base_graph_editor as mod

    seen = {}

    class _FakeDialog:
        @staticmethod
        def getSaveFileName(parent, caption, directory="", filt="", *a, **kw):
            seen["directory"] = directory
            return answer, filt

    monkeypatch.setattr(mod, "QFileDialog", _FakeDialog)
    return seen


def test_save_dialog_does_not_open_in_repo_root(qapp, monkeypatch):
    """Предложенный диалогу путь абсолютный и не ведёт в корень репо."""
    from ui.editors.base_graph_editor import BaseGraphEditor

    monkeypatch.chdir(REPO)          # как у клиента, запущенного из корня
    seen = _patch_dialog(monkeypatch)

    ed = BaseGraphEditor()
    assert ed.save_graph() is False  # диалог отменён — записи нет

    suggested = seen["directory"]
    assert os.path.isabs(suggested), \
        f"диалогу отдан относительный путь {suggested!r} → он откроется в CWD"
    assert Path(os.path.abspath(suggested)).parent != REPO, \
        f"сохранение по умолчанию ляжет в корень репо: {suggested!r}"


def test_save_dialog_remembers_last_folder(qapp, monkeypatch, tmp_path):
    """Второй Ctrl+S открывается там, где сохранили в первый раз."""
    from ui.editors.base_graph_editor import BaseGraphEditor

    monkeypatch.chdir(REPO)
    target = tmp_path / "graph_edited.json"
    _patch_dialog(monkeypatch, answer=str(target))

    ed = BaseGraphEditor()
    ed.model.graph_data = {"directed": False, "nodes": [], "links": []}
    assert ed.save_graph() is True
    assert target.exists()

    seen = _patch_dialog(monkeypatch)
    assert ed.save_graph() is False
    assert Path(seen["directory"]).parent == tmp_path

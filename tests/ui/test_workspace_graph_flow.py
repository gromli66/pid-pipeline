"""Двухфазный графовый flow в DiagramWorkspace — сверка по исходнику.

Переехало из `tests/test_stage7_graph_flow.py`, снесённого вместе с пакетом
мёртвых окон `ui/windows/` (пункт 10.3 дороги). Оттуда взяты только проверки,
которые читают живой `ui/widgets/diagram_workspace.py` и ничего не импортируют
из UI, — поэтому здесь нет ни заглушек Qt, ни `sys.modules`.

Пять проверок старого файла не переехали: они требовали флаг
`_simple_graph_done` и переоткрытие вкладки из `_open_graph_validation`.
Такой модели в коде нет и никогда не было (`git log --all -S_simple_graph_done`
по `ui/widgets/diagram_workspace.py` — пусто); фазы разведены по двум методам,
`_open_graph_validation` (фаза 1, SimpleGraphTab) и `_open_graph_editor`
(фаза 2, AdvancedGraphTab), а порядок ведёт цепочка бусин.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKSPACE = REPO / "ui" / "widgets" / "diagram_workspace.py"
SOURCE = WORKSPACE.read_text(encoding="utf-8")


def _method_body(marker: str) -> str:
    """Тело метода от его `def` до следующего `def` того же уровня."""
    idx = SOURCE.index(marker)
    idx_next = SOURCE.index("\n    def ", idx + 1)
    return SOURCE[idx:idx_next]


def test_source_imports_simple_graph_tab():
    """Фаза 1 открывает SimpleGraphTab."""
    assert "from ui.tabs.simple_graph_tab import SimpleGraphTab" in SOURCE


def test_source_imports_advanced_graph_tab():
    """Фаза 2 открывает AdvancedGraphTab."""
    assert "from ui.tabs.advanced_graph_tab import AdvancedGraphTab" in SOURCE


def test_source_no_old_graph_tab_import():
    """Старый GraphTab больше не импортируется."""
    assert "from ui.tabs.graph_tab import GraphTab" not in SOURCE


def test_source_has_on_simple_graph_confirmed():
    """_on_simple_graph_confirmed метод существует."""
    assert "def _on_simple_graph_confirmed" in SOURCE


def test_source_graph_confirmed_calls_complete():
    """_on_graph_confirmed вызывает complete_graph_validation.

    Проверяется именно ВЫЗОВ, а не вхождение имени в тело: docstring метода
    (`:2183`) называет `complete_graph_validation` сам, и проверка «имя есть
    в теле» оставалась зелёной при подменённом вызове (инъекция пункта 10.3).
    """
    body = _method_body("def _on_graph_confirmed")
    assert "self.api_client.complete_graph_validation(" in body


def test_workspace_two_phase_docstring():
    """_open_graph_validation имеет docstring про фазу с SimpleGraphTab."""
    idx = SOURCE.index("def _open_graph_validation")
    idx_body = SOURCE.index('"""', idx)
    idx_end = SOURCE.index('"""', idx_body + 3)
    docstring = SOURCE[idx_body:idx_end]
    assert "SimpleGraphTab" in docstring or "двухфазный" in docstring

"""
Tests for Stage 7 — graph_validation_window.py + DiagramWorkspace two-phase flow.

7.1 GraphValidationWindow:
    - Импортирует AdvancedGraphEditor (не GraphValidatorEditor)
    - Не импортирует GraphEditorMode (enum удалён)
    - Режимы — строковые ключи
    - _has_unsaved_changes использует undo_mgr.stack_depth
    - _saved_stack_depth обновляется при save
    - _set_mode делегирует в editor.set_mode(str)
    - _MODE_BUTTONS dict корректен

7.2 DiagramWorkspace:
    - _simple_graph_done флаг существует
    - _simple_graph_done сбрасывается в load_diagram
    - _open_graph_validation: фаза 1 → SimpleGraphTab
    - _open_graph_validation: фаза 2 → AdvancedGraphTab
    - _on_simple_graph_confirmed → _simple_graph_done=True + переоткрытие
    - _on_graph_confirmed → complete_graph_validation (без изменений)

Все тесты работают без запуска Qt Application (используют MagicMock).
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Stub Qt classes
# ---------------------------------------------------------------------------

def _stub_qt():
    pyside6 = types.ModuleType("PySide6")
    for mod_name in ["PySide6.QtWidgets", "PySide6.QtCore", "PySide6.QtGui"]:
        sys.modules[mod_name] = types.ModuleType(mod_name)
    sys.modules["PySide6"] = pyside6

    # QtCore
    qtcore = sys.modules["PySide6.QtCore"]

    class FakeSignal:
        def __init__(self, *args):
            self._handlers = []
        def connect(self, fn):
            self._handlers.append(fn)
        def disconnect(self, fn=None):
            if fn:
                self._handlers.remove(fn)
            else:
                self._handlers.clear()
        def emit(self, *args):
            for h in self._handlers:
                h(*args)

    class QObject:
        def __init__(self, *a, **kw): pass
        def moveToThread(self, t): pass

    class QThread(QObject):
        started = FakeSignal()
        def quit(self): pass
        def wait(self, *a): pass
        def start(self): pass
        def isRunning(self): return False

    class Qt:
        AlignCenter = 0
        WaitCursor = 0

    qtcore.Signal = FakeSignal
    qtcore.Slot = lambda *a: (lambda f: f)
    qtcore.Qt = Qt
    qtcore.QThread = QThread
    qtcore.QObject = QObject

    # QtWidgets
    qtw = sys.modules["PySide6.QtWidgets"]

    class QWidget:
        def __init__(self, *a, **kw): pass
        def hide(self): pass
        def show(self): pass
        def setAlignment(self, *a): pass
        def setStyleSheet(self, *a): pass
        def setText(self, *a): pass
        def text(self): return ""
        def setToolTip(self, *a): pass
        def setCheckable(self, *a): pass
        def setChecked(self, *a): pass
        def addButton(self, *a): pass
        def setContentsMargins(self, *a): pass
        def setSpacing(self, *a): pass
        def addWidget(self, *a): pass
        def addLayout(self, *a): pass
        def addStretch(self, *a): pass
        def insertWidget(self, *a): pass
        def count(self): return 0
        def layout(self): return None
        def setParent(self, *a): pass
        def deleteLater(self): pass
        def clicked(self): pass

    class QVBoxLayout(QWidget): pass
    class QHBoxLayout(QWidget): pass
    class QPushButton(QWidget):
        clicked = FakeSignal()
    class QLabel(QWidget): pass
    class QButtonGroup(QWidget): pass
    class QToolBar(QWidget):
        def setMovable(self, *a): pass
        def addSeparator(self): pass
    class QStatusBar(QWidget):
        def showMessage(self, *a): pass

    class QMainWindow(QWidget):
        def setCentralWidget(self, *a): pass
        def addToolBar(self, *a): pass
        def setStatusBar(self, *a): pass
        def setWindowTitle(self, *a): pass
        def setMinimumSize(self, *a): pass
        def showMaximized(self): pass
        def close(self): pass

    class QMessageBox:
        class StandardButton:
            Yes = 1; No = 2; Cancel = 4; Save = 8; Discard = 16
        Yes = 1; No = 2; Cancel = 4; Save = 8; Discard = 16
        @staticmethod
        def question(*a, **kw): return 1
        @staticmethod
        def warning(*a, **kw): pass
        @staticmethod
        def information(*a, **kw): pass
        @staticmethod
        def critical(*a, **kw): pass

    class QApplication:
        @staticmethod
        def setOverrideCursor(*a): pass
        @staticmethod
        def restoreOverrideCursor(): pass

    class QFileDialog:
        @staticmethod
        def getOpenFileName(*a, **kw): return ("", "")

    for cls in [QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
                QButtonGroup, QToolBar, QStatusBar, QMainWindow,
                QMessageBox, QApplication, QFileDialog]:
        setattr(qtw, cls.__name__, cls)

    # QtGui
    qtgui = sys.modules["PySide6.QtGui"]
    for name in ["QColor", "QBrush", "QPen", "QPainterPath", "QImage",
                 "QPixmap", "QPainter"]:
        setattr(qtgui, name, MagicMock())

    return FakeSignal

_FakeSignal = _stub_qt()

# Stub api_client
_api_stub = types.ModuleType("ui.services.api_client")
class _FakeAPIClient: pass
class _FakeAPIError(Exception):
    def __init__(self, message="error"):
        self.message = message
        super().__init__(message)
_api_stub.APIClient = _FakeAPIClient
_api_stub.APIError = _FakeAPIError
sys.modules["ui.services.api_client"] = _api_stub

_svc_stub = types.ModuleType("ui.services")
_svc_stub.APIClient = _FakeAPIClient
_svc_stub.APIError = _FakeAPIError
sys.modules["ui.services"] = _svc_stub

# Stub editors
_bge_stub = types.ModuleType("ui.editors.base_graph_editor")
class _FakeBaseGraphEditor:
    def __init__(self):
        self.undo_mgr = MagicMock()
        self.undo_mgr.stack_depth = 0
        self.model = MagicMock()
        self.status_callback = None
        self.stats_callback = None
_bge_stub.BaseGraphEditor = _FakeBaseGraphEditor
sys.modules["ui.editors.base_graph_editor"] = _bge_stub

_sge_stub = types.ModuleType("ui.editors.simple_graph_editor")
class _FakeSimpleGraphEditor(_FakeBaseGraphEditor): pass
_sge_stub.SimpleGraphEditor = _FakeSimpleGraphEditor
sys.modules["ui.editors.simple_graph_editor"] = _sge_stub

_age_stub = types.ModuleType("ui.editors.advanced_graph_editor")
class _FakeAdvancedGraphEditor(_FakeBaseGraphEditor):
    def set_mode(self, m): pass
    def load_data(self, *a, **kw): return True
    def save_graph(self, *a): return True
    def undo(self): pass
    def optimize_all_edges(self): return 0
    def get_perpendicularity_stats(self): return {"good": 0, "total": 0, "avg_score": 0.0}
_age_stub.AdvancedGraphEditor = _FakeAdvancedGraphEditor
sys.modules["ui.editors.advanced_graph_editor"] = _age_stub

# Stub other editor dependencies
_nld_stub = types.ModuleType("ui.editors.node_list_dialog")
_nld_stub.NodeListDialog = MagicMock()
sys.modules["ui.editors.node_list_dialog"] = _nld_stub
sys.modules["ui.editors.graph_data"] = MagicMock()
sys.modules["ui.editors.undo_manager"] = MagicMock()
sys.modules["ui.editors.resize_overlay"] = MagicMock()
for _m in [
    "ui.editors.graph_geometry", "ui.editors.edge_routing",
    "ui.editors.mode_handlers.base_handler",
    "ui.editors.mode_handlers.simple_handlers",
    "ui.editors.mode_handlers.advanced_handlers",
    "ui.editors.commands.simple_commands",
    "ui.editors.commands.advanced_commands",
]:
    sys.modules[_m] = MagicMock()

# Project path
# Project path — работает и из tests/, и из корня проекта
_here = Path(__file__).parent.resolve()
if (_here / "ui").is_dir():
    PROJECT_ROOT = _here
elif (_here.parent / "ui").is_dir():
    PROJECT_ROOT = _here.parent
else:
    PROJECT_ROOT = _here.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Block ui.windows.__init__ from importing MainWindow (pulls too many deps)
_win_stub = types.ModuleType("ui.windows")
_win_stub.__path__ = [str(PROJECT_ROOT / "ui" / "windows")]
sys.modules["ui.windows"] = _win_stub
# Block ui.editors.graph_editor (old module, should not be imported)
sys.modules["ui.editors.graph_editor"] = types.ModuleType("ui.editors.graph_editor")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_api():
    api = MagicMock()
    api.download_artifact = MagicMock()
    api.upload_validated_graph = MagicMock()
    api.get_project_classes = MagicMock(return_value={"classes": []})
    api.complete_graph_validation = MagicMock(return_value={"task_id": "abc123"})
    api.start_graph_validation = MagicMock()
    api.get_diagram = MagicMock()
    return api


# ===========================================================================
# 7.1 GraphValidationWindow
# ===========================================================================

class TestGraphValidationWindowImports:
    def test_imports_advanced_graph_editor(self):
        """Window импортирует AdvancedGraphEditor, не GraphValidatorEditor."""
        import ui.windows.graph_validation_window as gvw
        # Должен быть AdvancedGraphEditor в модуле
        source = Path(gvw.__file__).read_text(encoding="utf-8")
        assert "AdvancedGraphEditor" in source
        assert "GraphValidatorEditor" not in source

    def test_no_graph_editor_mode_enum(self):
        """GraphEditorMode (enum) не импортируется."""
        import ui.windows.graph_validation_window as gvw
        source = Path(gvw.__file__).read_text(encoding="utf-8")
        assert "GraphEditorMode" not in source

    def test_string_modes_used(self):
        """Режимы — строковые ключи."""
        import ui.windows.graph_validation_window as gvw
        source = Path(gvw.__file__).read_text(encoding="utf-8")
        for mode in ["add_edge", "delete_edge", "add_connector",
                      "delete_node", "optimize_edge", "drag_node"]:
            assert f'"{mode}"' in source


class TestGraphValidationWindowLogic:
    def _make_window(self):
        from ui.windows.graph_validation_window import GraphValidationWindow

        win = GraphValidationWindow.__new__(GraphValidationWindow)
        win.uid = "test-uid"
        win.diagram_name = "Test Diagram"
        win.api_client = _make_mock_api()

        # Mock editor
        editor = MagicMock()
        editor.undo_mgr = MagicMock()
        editor.undo_mgr.stack_depth = 0
        editor.model = MagicMock()
        editor.set_mode = MagicMock()
        editor.save_graph = MagicMock(return_value=True)
        editor.optimize_all_edges = MagicMock(return_value=3)
        editor.get_perpendicularity_stats = MagicMock(
            return_value={"good": 5, "total": 10, "avg_score": 0.9}
        )
        win.graph_editor = editor

        # Mock UI
        win.statusbar = MagicMock()
        win.graph_stats_label = MagicMock()
        win.perp_stats_label = MagicMock()
        win.graph_status = MagicMock()
        win.btn_add_edge = MagicMock()
        win.btn_del_edge = MagicMock()
        win.btn_add_connector = MagicMock()
        win.btn_del_node = MagicMock()
        win.btn_optimize_edge = MagicMock()
        win.btn_drag_node = MagicMock()

        import tempfile
        win._temp_dir_obj = tempfile.TemporaryDirectory(prefix="t_")
        win.temp_dir = Path(win._temp_dir_obj.name)
        win._saved_stack_depth = 0

        return win

    def test_set_mode_delegates_string(self):
        win = self._make_window()
        win._set_mode("optimize_edge")
        win.graph_editor.set_mode.assert_called_once_with("optimize_edge")

    def test_set_mode_checks_correct_button(self):
        win = self._make_window()
        win._set_mode("delete_edge")
        win.btn_del_edge.setChecked.assert_called_with(True)
        win.btn_add_edge.setChecked.assert_called_with(False)

    def test_has_unsaved_changes_uses_stack_depth(self):
        win = self._make_window()
        win._saved_stack_depth = 0
        win.graph_editor.undo_mgr.stack_depth = 0
        assert win._has_unsaved_changes() is False

        win.graph_editor.undo_mgr.stack_depth = 3
        assert win._has_unsaved_changes() is True

    def test_has_unsaved_changes_after_save(self):
        win = self._make_window()
        win.graph_editor.undo_mgr.stack_depth = 5
        win._saved_stack_depth = 5
        assert win._has_unsaved_changes() is False

        win.graph_editor.undo_mgr.stack_depth = 7
        assert win._has_unsaved_changes() is True

    def test_save_updates_saved_stack_depth(self):
        win = self._make_window()
        win.graph_editor.undo_mgr.stack_depth = 4
        win._save_graph()
        assert win._saved_stack_depth == 4

    def test_optimize_all_delegates(self):
        win = self._make_window()
        win._optimize_all_edges()
        win.graph_editor.optimize_all_edges.assert_called_once()

    def test_update_perp_stats(self):
        win = self._make_window()
        win._update_perp_stats()
        win.graph_editor.get_perpendicularity_stats.assert_called_once()
        call_text = win.perp_stats_label.setText.call_args[0][0]
        assert "5/10" in call_text

    def test_mode_buttons_dict_complete(self):
        from ui.windows.graph_validation_window import GraphValidationWindow
        expected_modes = {
            "add_edge", "delete_edge", "add_connector",
            "delete_node", "optimize_edge", "drag_node",
        }
        assert set(GraphValidationWindow._MODE_BUTTONS.keys()) == expected_modes


# ===========================================================================
# 7.2 DiagramWorkspace two-phase flow
# ===========================================================================

class TestDiagramWorkspaceTwoPhase:
    """Тесты двухфазного flow в DiagramWorkspace.

    Поскольку DiagramWorkspace сложный виджет с множеством зависимостей,
    тестируем через чтение кода + минимальные проверки атрибутов.
    """

    def test_source_has_simple_graph_done_flag(self):
        """_simple_graph_done инициализируется в __init__."""
        source = Path(
            PROJECT_ROOT / "ui" / "widgets" / "diagram_workspace.py"
        ).read_text(encoding="utf-8")
        assert "_simple_graph_done = False" in source

    def test_source_resets_flag_in_load_diagram(self):
        """_simple_graph_done сбрасывается в load_diagram."""
        source = Path(
            PROJECT_ROOT / "ui" / "widgets" / "diagram_workspace.py"
        ).read_text(encoding="utf-8")
        # Находим load_diagram и проверяем что _simple_graph_done сбрасывается
        idx_load = source.index("def load_diagram")
        # Ищем следующий def после load_diagram
        idx_next = source.index("\n    def ", idx_load + 1)
        load_body = source[idx_load:idx_next]
        assert "_simple_graph_done = False" in load_body

    def test_source_imports_simple_graph_tab(self):
        """_open_graph_validation импортирует SimpleGraphTab."""
        source = Path(
            PROJECT_ROOT / "ui" / "widgets" / "diagram_workspace.py"
        ).read_text(encoding="utf-8")
        assert "from ui.tabs.simple_graph_tab import SimpleGraphTab" in source

    def test_source_imports_advanced_graph_tab(self):
        """_open_graph_validation импортирует AdvancedGraphTab."""
        source = Path(
            PROJECT_ROOT / "ui" / "widgets" / "diagram_workspace.py"
        ).read_text(encoding="utf-8")
        assert "from ui.tabs.advanced_graph_tab import AdvancedGraphTab" in source

    def test_source_no_old_graph_tab_import(self):
        """Старый GraphTab больше не импортируется."""
        source = Path(
            PROJECT_ROOT / "ui" / "widgets" / "diagram_workspace.py"
        ).read_text(encoding="utf-8")
        assert "from ui.tabs.graph_tab import GraphTab" not in source

    def test_source_has_on_simple_graph_confirmed(self):
        """_on_simple_graph_confirmed метод существует."""
        source = Path(
            PROJECT_ROOT / "ui" / "widgets" / "diagram_workspace.py"
        ).read_text(encoding="utf-8")
        assert "def _on_simple_graph_confirmed" in source

    def test_source_simple_confirmed_sets_flag(self):
        """_on_simple_graph_confirmed устанавливает _simple_graph_done = True."""
        source = Path(
            PROJECT_ROOT / "ui" / "widgets" / "diagram_workspace.py"
        ).read_text(encoding="utf-8")
        idx = source.index("def _on_simple_graph_confirmed")
        idx_next = source.index("\n    def ", idx + 1)
        body = source[idx:idx_next]
        assert "_simple_graph_done = True" in body

    def test_source_simple_confirmed_reopens_graph(self):
        """_on_simple_graph_confirmed вызывает _open_graph_validation."""
        source = Path(
            PROJECT_ROOT / "ui" / "widgets" / "diagram_workspace.py"
        ).read_text(encoding="utf-8")
        idx = source.index("def _on_simple_graph_confirmed")
        idx_next = source.index("\n    def ", idx + 1)
        body = source[idx:idx_next]
        assert "_open_graph_validation()" in body

    def test_source_graph_confirmed_calls_complete(self):
        """_on_graph_confirmed вызывает complete_graph_validation."""
        source = Path(
            PROJECT_ROOT / "ui" / "widgets" / "diagram_workspace.py"
        ).read_text(encoding="utf-8")
        idx = source.index("def _on_graph_confirmed")
        idx_next = source.index("\n    def ", idx + 1)
        body = source[idx:idx_next]
        assert "complete_graph_validation" in body

    def test_source_phase_branching(self):
        """_open_graph_validation содержит if not self._simple_graph_done."""
        source = Path(
            PROJECT_ROOT / "ui" / "widgets" / "diagram_workspace.py"
        ).read_text(encoding="utf-8")
        idx = source.index("def _open_graph_validation")
        idx_next = source.index("\n    def ", idx + 1)
        body = source[idx:idx_next]
        assert "not self._simple_graph_done" in body


# ===========================================================================
# 7.x Cross-module consistency
# ===========================================================================

class TestCrossModuleConsistency:
    def test_window_no_old_imports(self):
        """graph_validation_window не импортирует graph_editor.py."""
        source = Path(
            PROJECT_ROOT / "ui" / "windows" / "graph_validation_window.py"
        ).read_text(encoding="utf-8")
        assert "from ui.editors.graph_editor" not in source

    def test_window_uses_undo_mgr(self):
        """graph_validation_window использует undo_mgr.stack_depth."""
        source = Path(
            PROJECT_ROOT / "ui" / "windows" / "graph_validation_window.py"
        ).read_text(encoding="utf-8")
        assert "undo_mgr.stack_depth" in source
        # Не должно быть len(undo_stack)
        assert "len(self.graph_editor.undo_stack)" not in source

    def test_workspace_two_phase_docstring(self):
        """_open_graph_validation имеет docstring про двухфазный flow."""
        source = Path(
            PROJECT_ROOT / "ui" / "widgets" / "diagram_workspace.py"
        ).read_text(encoding="utf-8")
        idx = source.index("def _open_graph_validation")
        idx_body = source.index('"""', idx)
        idx_end = source.index('"""', idx_body + 3)
        docstring = source[idx_body:idx_end]
        assert "SimpleGraphTab" in docstring or "двухфазный" in docstring

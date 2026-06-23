"""
Diagram Workspace — рабочее пространство диаграммы.

Header (бусины + кнопки действий) прячется при открытии вкладки валидации.
Вкладка валидации занимает 100% пространства.

Навигация:
  header виден → [← Назад] = вернуться к списку диаграмм
  вкладка открыта → [← Назад] = закрыть вкладку, вернуться к header
"""

import logging
from pathlib import Path
from typing import Optional, Dict

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QMessageBox, QFrame,
    QStackedWidget, QApplication, QFileDialog, QInputDialog,
    QMenu,
)
from PySide6.QtCore import Qt, Signal, Slot, QTimer
from PySide6.QtGui import QAction, QFont

from ui.services.api_client import APIClient, APIError, DiagramStatus
from ui.services.status_provider import StatusProvider
from ui.widgets.progress_beads import ProgressBeads, BeadInfo, BeadState
from ui.widgets.bead_gif_player import BeadGifPlayer

logger = logging.getLogger(__name__)


# =====================================================================
# Бусины — индексы (11 бусин, 1:1 с кнопками)
# =====================================================================

BEAD_FRAME = 0
BEAD_DETECTION = 1
BEAD_VAL_DET = 2
BEAD_SEGMENTATION = 3
BEAD_VAL_PIPE = 4
BEAD_VAL_JUNCTION = 5
BEAD_GRAPH = 6
BEAD_VAL_GRAPH = 7
BEAD_CONTOURS = 8
BEAD_OCR = 9
BEAD_OCR_BINDING = 10
BEAD_EDIT_GRAPH = 11
BEAD_FXML = 12
NUM_BEADS = 13

# =====================================================================
# Порядок статусов и маппинг бусин/кнопок
# =====================================================================

# Линейный порядок статусов (для сравнения "дальше/раньше")
_STATUS_ORDER = [
    DiagramStatus.UPLOADED,              # 0
    DiagramStatus.CLEANING_FRAME,        # frame removal (in progress)
    DiagramStatus.FRAME_CLEANED,         # frame removed / skipped
    DiagramStatus.DETECTING,             # 1
    DiagramStatus.DETECTED,              # 2
    DiagramStatus.VALIDATING_BBOX,       # 3
    DiagramStatus.VALIDATED_BBOX,        # 4
    DiagramStatus.SEGMENTING,            # 5
    DiagramStatus.SKELETONIZING,         # 6  Skeleton #1
    DiagramStatus.SKELETONIZED,          # 7
    DiagramStatus.VALIDATING_MASKS,      # 8
    DiagramStatus.VALIDATED_MASKS,       # 9
    DiagramStatus.SKELETONIZING_FINAL,   # 10 Skeleton #2 (final)
    DiagramStatus.SKELETONIZED_FINAL,    # 11
    DiagramStatus.DETECTING_JUNCTIONS,   # 12
    DiagramStatus.DETECTED_JUNCTIONS,    # 13
    DiagramStatus.VALIDATING_JUNCTIONS,  # 14
    DiagramStatus.VALIDATED_JUNCTIONS,   # 15
    DiagramStatus.BUILDING_GRAPH,        # 16
    DiagramStatus.BUILT,                 # 17
    DiagramStatus.VALIDATING_GRAPH,      # 18
    DiagramStatus.VALIDATED_GRAPH,       # 19
    DiagramStatus.EXTRACTING_CONTOURS,   # 20
    DiagramStatus.CONTOURS_EXTRACTED,    # 21
    DiagramStatus.CONTOURS_VALIDATED,    # 22
    DiagramStatus.OCR_PROCESSING,        # 23
    DiagramStatus.OCR_COMPLETED,         # 24
    DiagramStatus.OCR_BOUND,             # 25
    DiagramStatus.GENERATING_FXML,       # 26
    DiagramStatus.COMPLETED,             # 27
]
_STATUS_IDX = {s: i for i, s in enumerate(_STATUS_ORDER)}


def _status_ge(current: DiagramStatus, threshold: DiagramStatus) -> bool:
    """Текущий статус >= порогового."""
    return _STATUS_IDX.get(current, -1) >= _STATUS_IDX.get(threshold, 999)


# Каждая бусина: (idx, key, completed_when, in_progress_statuses, available_when)
_BEAD_DEFS = [
    (BEAD_FRAME,         "frame",       DiagramStatus.FRAME_CLEANED,
     {DiagramStatus.CLEANING_FRAME},
     DiagramStatus.UPLOADED),

    (BEAD_DETECTION,     "detect",      DiagramStatus.DETECTED,
     {DiagramStatus.DETECTING},
     DiagramStatus.FRAME_CLEANED),

    (BEAD_VAL_DET,       "cvat",        DiagramStatus.VALIDATED_BBOX,
     set(),
     DiagramStatus.DETECTED),

    (BEAD_SEGMENTATION,  "segment",     DiagramStatus.SKELETONIZED,
     {DiagramStatus.SEGMENTING, DiagramStatus.SKELETONIZING},
     DiagramStatus.VALIDATED_BBOX),

    (BEAD_VAL_PIPE,      "pipe",        DiagramStatus.VALIDATED_MASKS,
     set(),
     DiagramStatus.SKELETONIZED),

    (BEAD_VAL_JUNCTION,  "junction",    DiagramStatus.VALIDATED_JUNCTIONS,
     {DiagramStatus.VALIDATED_MASKS,
      DiagramStatus.SKELETONIZING_FINAL, DiagramStatus.SKELETONIZED_FINAL,
      DiagramStatus.DETECTING_JUNCTIONS, DiagramStatus.VALIDATING_JUNCTIONS},
     DiagramStatus.VALIDATED_MASKS),

    (BEAD_GRAPH,         "graph",       DiagramStatus.BUILT,
     {DiagramStatus.BUILDING_GRAPH},
     DiagramStatus.VALIDATED_JUNCTIONS),

    (BEAD_VAL_GRAPH,     "val_graph",   DiagramStatus.VALIDATED_GRAPH,
     set(),
     DiagramStatus.BUILT),

    (BEAD_CONTOURS,      "contours",    DiagramStatus.CONTOURS_VALIDATED,
     set(),
     DiagramStatus.VALIDATED_GRAPH),

    (BEAD_OCR,           "ocr",         DiagramStatus.OCR_COMPLETED,
     {DiagramStatus.OCR_PROCESSING,
      DiagramStatus.BUILDING_GRAPH, DiagramStatus.BUILT,
      DiagramStatus.VALIDATING_GRAPH, DiagramStatus.VALIDATED_GRAPH,
      DiagramStatus.EXTRACTING_CONTOURS, DiagramStatus.CONTOURS_EXTRACTED,
      DiagramStatus.CONTOURS_VALIDATED},
     DiagramStatus.VALIDATED_GRAPH),

    (BEAD_OCR_BINDING,   "ocr_binding", DiagramStatus.OCR_BOUND,
     set(),
     DiagramStatus.OCR_COMPLETED),

    (BEAD_EDIT_GRAPH,    "edit_graph",  DiagramStatus.GENERATING_FXML,
     set(),
     DiagramStatus.OCR_BOUND),

    (BEAD_FXML,          "fxml",        DiagramStatus.COMPLETED,
     {DiagramStatus.GENERATING_FXML},
     DiagramStatus.GENERATING_FXML),
]


def _beads_for_status(status: DiagramStatus) -> list:
    """Вычислить состояние каждой бусины по текущему статусу."""
    result = []
    for bead_idx, key, completed_at, in_progress_set, available_at in _BEAD_DEFS:
        if _status_ge(status, completed_at):
            result.append((bead_idx, BeadState.COMPLETED))
        elif status in in_progress_set:
            result.append((bead_idx, BeadState.IN_PROGRESS))
        elif _status_ge(status, available_at):
            result.append((bead_idx, BeadState.AVAILABLE))
        # else: UNAVAILABLE (default)
    return result


def _buttons_for_status(status: DiagramStatus):
    """Вычислить available/completed/processing кнопки по текущему статусу."""
    available = set()
    completed = set()
    processing = set()

    for bead_idx, key, completed_at, in_progress_set, available_at in _BEAD_DEFS:
        if _status_ge(status, completed_at):
            completed.add(key)
        elif status in in_progress_set:
            processing.add(key)
        elif _status_ge(status, available_at):
            available.add(key)

    return available, completed, processing


# =====================================================================
# Стили кнопок
# =====================================================================

_BTN_STYLE_GRAY = """
    QPushButton {
        background-color: #555; color: #999;
        padding: 6px 10px; border-radius: 4px; border: none;
    }
"""

_BTN_STYLE_YELLOW = """
    QPushButton {
        background-color: #FF9800; color: white; font-weight: bold;
        padding: 6px 10px; border-radius: 4px; border: none;
    }
    QPushButton:hover { background-color: #F57C00; }
"""

_BTN_STYLE_GREEN = """
    QPushButton {
        background-color: #4CAF50; color: white;
        padding: 6px 10px; border-radius: 4px; border: none;
    }
"""

_BTN_STYLE_BLUE = """
    QPushButton {
        background-color: #2196F3; color: white;
        padding: 6px 10px; border-radius: 4px; border: none;
    }
"""

_BTN_STYLE_RED = """
    QPushButton {
        background-color: #F44336; color: white;
        padding: 6px 10px; border-radius: 4px; border: none;
    }
"""


# =====================================================================
# GIF активного этапа
# =====================================================================

_IDX_KEY = {idx: key for (idx, key, *_rest) in _BEAD_DEFS}
_GIF_DIR = Path(__file__).resolve().parent.parent / "resources" / "beads"
_GIF_FILES = {
    "detect":   "pid_detection_light.gif",
    "cvat":     "pid_cvat_manual.gif",
    "segment":  "pid_pipe_trace.gif",
    "pipe":     "pid_valpipe_fix.gif",
    "graph":    "pid_graph.gif",
    "val_graph": "pid_valgraph.gif",
    "contours": "pid_contours.gif",
    "ocr":      "pid_ocr.gif",
    # frame / ocr_binding / edit_graph / fxml — гифки пока нет (плейсхолдер)
}


def _gif_path_for(key: str, status: DiagramStatus):
    """Путь к GIF для активного этапа (с нюансом junction: процесс/проверка)."""
    if key == "junction":
        # в процессе авто-поиска узлов — скелет; на проверке — разметка узлов
        fname = ("pid_valjb_fix.gif"
                 if status == DiagramStatus.VALIDATING_JUNCTIONS
                 else "pid_skeleton.gif")
        p = _GIF_DIR / fname
        return str(p) if p.exists() else None
    fname = _GIF_FILES.get(key)
    if not fname:
        return None
    p = _GIF_DIR / fname
    return str(p) if p.exists() else None


class _StagePanel(QWidget):
    """Панель этапа: вертикальные бусины + кнопки слева, GIF справа.

    Адаптивно масштабирует высоту строк/шрифт/радиус бусин под размер окна.
    """

    def __init__(self, beads, buttons, parent=None):
        super().__init__(parent)
        self._beads = beads
        self._buttons = buttons

    def resizeEvent(self, event):
        super().resizeEvent(event)
        row_h = max(22, min(40, self.height() // 16))
        font = QFont("Segoe UI", max(9, int(row_h * 0.40)))
        for btn in self._buttons:
            btn.setFixedHeight(row_h)
            btn.setFont(font)
        self._beads.set_radius(int(row_h * 0.30))


# =====================================================================
# DiagramWorkspace
# =====================================================================

class DiagramWorkspace(QWidget):
    """
    Рабочее пространство диаграммы.

    Signals:
        back_requested(): вернуться к списку диаграмм
        status_message(str, int): сообщение для статусбара
    """

    back_requested = Signal()
    status_message = Signal(str, int)

    # Описание кнопок: (key, label) — 13 этапов, 1:1 с бусинами.
    # Порядок — по пайплайну (pipe → junction); подписи — по таблице.
    _BUTTON_DEFS = [
        ("frame",       "Очистка рамки"),
        ("detect",      "Поиск элементов"),
        ("cvat",        "Проверка элементов"),
        ("segment",     "Выделение труб"),
        ("pipe",        "Проверка труб"),
        ("junction",    "Проверка узлов"),
        ("graph",       "Сборка схемы"),
        ("val_graph",   "Проверка схемы"),
        ("contours",    "Контуры элемента"),
        ("ocr",         "Распознавание текста"),
        ("ocr_binding", "Привязка подписей"),
        ("edit_graph",  "Ручная правка"),
        ("fxml",        "Экспорт"),
    ]

    def __init__(
        self,
        api_client: APIClient,
        status_provider: StatusProvider,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)

        self.api_client = api_client
        self.status_provider = status_provider

        # Состояние
        self._uid: Optional[str] = None
        self._diagram_name: str = ""
        self._junction_confirmed = False
        self._pipe_confirmed = False
        self._active_tab = None  # Текущий открытый tab-виджет
        self._active_tab_key = ""  # Ключ: "cvat", "junction", "pipe", "graph"
        self._btn_back_injected = None  # Кнопка ← Назад внутри вкладки
        self._ocr_notified = False  # B6.4: OCR ready notification sent
        self._was_status_watching = False  # Paused status polling while tab is open

        # OCR artifact polling timer (independent of status changes)
        self._ocr_poll_timer = QTimer(self)
        self._ocr_poll_timer.setInterval(3000)  # 3 sec
        self._ocr_poll_timer.timeout.connect(self._check_ocr_artifact)

        # Autosave service
        from ui.services.autosave import AutoSaveService
        self._autosave = AutoSaveService(
            status_callback=lambda msg: self.status_message.emit(msg, 3000),
            parent=self,
        )

        self._setup_ui()

    def _setup_ui(self):
        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(0, 0, 0, 0)
        self._root_layout.setSpacing(0)

        # === Header panel: ← Назад | Название ===
        self.header_panel = QWidget()
        header_layout = QVBoxLayout(self.header_panel)
        header_layout.setContentsMargins(8, 4, 8, 4)
        header_layout.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        self.btn_back_header = QPushButton("← Назад")
        self.btn_back_header.setFixedSize(80, 28)
        self.btn_back_header.clicked.connect(self._on_back_to_list)
        top_row.addWidget(self.btn_back_header)
        self.title_label = QLabel("Диаграмма")
        self.title_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        top_row.addWidget(self.title_label)
        top_row.addStretch()
        header_layout.addLayout(top_row)

        self._root_layout.addWidget(self.header_panel)

        # === Content area (панель этапа / вкладка) ===
        self.content_stack = QStackedWidget()

        # --- Вертикальные бусины (слева) ---
        self.beads = ProgressBeads(orientation="vertical")
        self.beads.set_beads([BeadInfo(cap) for _, cap in self._BUTTON_DEFS])

        # --- Кнопки этапов (столбик, по центру по вертикали) ---
        buttons_widget = QWidget()
        buttons_col = QVBoxLayout(buttons_widget)
        buttons_col.setContentsMargins(0, 0, 0, 0)
        buttons_col.setSpacing(8)
        buttons_col.addStretch()
        self._action_buttons: Dict[str, QPushButton] = {}
        for key, label in self._BUTTON_DEFS:
            btn = QPushButton(label)
            btn.setFixedHeight(30)
            btn.setMinimumWidth(180)
            btn.setEnabled(False)
            btn.setStyleSheet(_BTN_STYLE_GRAY)
            buttons_col.addWidget(btn)
            self._action_buttons[key] = btn
        buttons_col.addStretch()

        left_col = QWidget()
        left_row = QHBoxLayout(left_col)
        left_row.setContentsMargins(0, 0, 0, 0)
        left_row.setSpacing(6)
        left_row.addWidget(self.beads)
        left_row.addWidget(buttons_widget)

        # Привязываем бусины к позициям кнопок (по вертикали)
        self.beads.set_anchor_widgets(list(self._action_buttons.values()))

        # --- GIF активного этапа (справа, по центру) ---
        self.gif_player = BeadGifPlayer()

        # Page 0: панель этапа
        self.stage_panel = _StagePanel(self.beads,
                                       list(self._action_buttons.values()))
        stage_layout = QHBoxLayout(self.stage_panel)
        stage_layout.setContentsMargins(12, 8, 12, 8)
        stage_layout.setSpacing(16)
        stage_layout.addWidget(left_col, stretch=0)
        stage_layout.addWidget(self.gif_player, stretch=1)
        self.content_stack.addWidget(self.stage_panel)

        # Page 1: контейнер для вкладки
        self._tab_container = QWidget()
        self._tab_container_layout = QVBoxLayout(self._tab_container)
        self._tab_container_layout.setContentsMargins(0, 0, 0, 0)
        self._tab_container_layout.setSpacing(0)
        self.content_stack.addWidget(self._tab_container)

        self.content_stack.setCurrentIndex(0)
        self._root_layout.addWidget(self.content_stack, stretch=1)

        # === Подключение кнопок ===
        # Маппинг: кнопка → целевой статус для отката (статус ПЕРЕД этим этапом)
        self._ROLLBACK_TARGET = {
            "frame": "uploaded",
            "detect": "frame_cleaned",
            "cvat": "detected",
            "segment": "validated_bbox",
            "pipe": "skeletonized",
            "junction": "detected_junctions",
            "graph": "validated_junctions",
            "val_graph": "built",
            "contours": "validated_graph",
            "ocr": "validated_graph",
            "ocr_binding": "ocr_completed",
            "edit_graph": "ocr_bound",
            "fxml": "ocr_bound",
        }

        # Оригинальные обработчики
        self._original_handlers = {
            "frame": self._open_frame,
            "detect": self._start_detection,
            "cvat": self._open_cvat,
            "segment": self._start_segmentation,
            "junction": self._open_junction,
            "pipe": self._open_pipe,
            "graph": self._start_graph_build,
            "val_graph": self._open_graph_validation,
            "contours": self._open_contours,
            "edit_graph": self._open_graph_editor,
            "ocr": self._start_ocr,
            "ocr_binding": self._open_ocr_binding,
            "fxml": self._start_fxml,
        }

        for key, btn in self._action_buttons.items():
            handler = self._original_handlers.get(key)
            if handler:
                btn.clicked.connect(lambda checked=False, k=key, h=handler: self._on_button_click(k, h))

    # =================================================================
    # Public API
    # =================================================================

    def load_diagram(self, uid: str, name: str):
        """Загрузить диаграмму в workspace."""
        self._uid = uid
        self._diagram_name = name
        self._junction_confirmed = False
        self._pipe_confirmed = False
        self._stop_ocr_poll()
        self._ocr_notified = False
        self._fxml_save_prompted = True
        self._last_status = DiagramStatus.UPLOADED

        self.title_label.setText(f"Диаграмма — {name}")

        # Закрыть вкладку если открыта
        self._force_close_tab()

        # Показать header
        self.header_panel.show()
        self.content_stack.setCurrentIndex(0)

        # Обновить бусины и кнопки
        self._refresh_status()

        # Подписаться на обновления (один раз)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            try:
                self.status_provider.status_updated.disconnect(
                    self._on_status_updated,
                )
            except (RuntimeError, TypeError):
                pass
        self.status_provider.status_updated.connect(self._on_status_updated)

    def cleanup(self):
        """Вызвать при уходе из workspace."""
        self._stop_ocr_poll()
        self._force_close_tab()
        if self._uid:
            self.status_provider.unwatch(self._uid)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            try:
                self.status_provider.status_updated.disconnect(
                    self._on_status_updated,
                )
            except (RuntimeError, TypeError):
                pass

    # =================================================================
    # Refresh
    # =================================================================

    def _refresh_status(self):
        """Обновить бусины и кнопки из текущего статуса в БД."""
        if not self._uid:
            return
        try:
            diagram = self.api_client.get_diagram(self._uid)
            logger.info("Refresh: %s → %s", self._uid[:8], diagram.status.value)
            self._apply_status(diagram.status,
                               error_stage=getattr(diagram, 'error_stage', None))
        except Exception as exc:
            logger.error("Refresh failed: %s", exc)

    def _apply_status(self, status: DiagramStatus, error_stage: str = None):
        """Применить статус к бусинам и кнопкам."""
        _prev_status = self._last_status
        self._last_status = status
        self._update_beads(status)
        self._update_buttons(status, error_stage=error_stage)
        self._update_gif(status)

        # Авто-сохранение FXML на компьютер пользователя сразу после генерации
        if (status == DiagramStatus.COMPLETED
                and _prev_status != DiagramStatus.COMPLETED
                and not getattr(self, "_fxml_save_prompted", True)):
            self._fxml_save_prompted = True
            self._download_fxml()

        # B6.4: При параллельных статусах — проверить готовность OCR по артефакту
        if not self._ocr_notified and status in (
            DiagramStatus.BUILDING_GRAPH,
            DiagramStatus.BUILT,
            DiagramStatus.VALIDATING_GRAPH,
            DiagramStatus.VALIDATED_GRAPH,
            DiagramStatus.EXTRACTING_CONTOURS,
            DiagramStatus.CONTOURS_EXTRACTED,
            DiagramStatus.CONTOURS_VALIDATED,
        ):
            try:
                ocr_info = self.api_client.get_ocr_status(self._uid)
                if ocr_info.get("has_ocr_result"):
                    self._ocr_notified = True
                    self._stop_ocr_poll()
                    self.beads.set_state(BEAD_OCR, BeadState.COMPLETED)
                    if "ocr" in self._action_buttons:
                        self._action_buttons["ocr"].setEnabled(True)
                        self._action_buttons["ocr"].setStyleSheet(_BTN_STYLE_GREEN)
                    if "ocr_binding" in self._action_buttons:
                        self._action_buttons["ocr_binding"].setEnabled(True)
                        self._action_buttons["ocr_binding"].setStyleSheet(_BTN_STYLE_YELLOW)
                        self.beads.set_state(BEAD_OCR_BINDING, BeadState.AVAILABLE)
                else:
                    # OCR ещё работает — запустить периодическую проверку
                    self._start_ocr_poll()
            except Exception:
                # При ошибке API — тоже запустить polling (retry)
                self._start_ocr_poll()

        # OCR уже завершён ранее — убедиться что бусины отражают это,
        # даже если _update_beads выставил IN_PROGRESS из-за параллельного статуса
        elif self._ocr_notified and status in (
            DiagramStatus.BUILDING_GRAPH,
            DiagramStatus.BUILT,
            DiagramStatus.VALIDATING_GRAPH,
            DiagramStatus.VALIDATED_GRAPH,
            DiagramStatus.EXTRACTING_CONTOURS,
            DiagramStatus.CONTOURS_EXTRACTED,
            DiagramStatus.CONTOURS_VALIDATED,
        ):
            self.beads.set_state(BEAD_OCR, BeadState.COMPLETED)
            if "ocr" in self._action_buttons:
                self._action_buttons["ocr"].setEnabled(True)
                self._action_buttons["ocr"].setStyleSheet(_BTN_STYLE_GREEN)
            if "ocr_binding" in self._action_buttons:
                self._action_buttons["ocr_binding"].setEnabled(True)
                self._action_buttons["ocr_binding"].setStyleSheet(_BTN_STYLE_YELLOW)
                self.beads.set_state(BEAD_OCR_BINDING, BeadState.AVAILABLE)

    # =================================================================
    # OCR artifact polling (independent of DiagramStatus changes)
    # =================================================================

    def _start_ocr_poll(self):
        """Запустить периодическую проверку OCR артефакта."""
        if not self._ocr_notified and self._uid and not self._ocr_poll_timer.isActive():
            logger.info("Starting OCR artifact poll for %s", self._uid[:8] if self._uid else "?")
            self.beads.setToolTip("OCR в процессе (параллельно с графом)")
            self._ocr_poll_timer.start()

    def _stop_ocr_poll(self):
        """Остановить проверку OCR артефакта."""
        if self._ocr_poll_timer.isActive():
            self._ocr_poll_timer.stop()
            self.beads.setToolTip("")

    @Slot()
    def _check_ocr_artifact(self):
        """Проверить наличие OCR артефакта (вызывается таймером)."""
        if self._ocr_notified or not self._uid:
            self._stop_ocr_poll()
            return
        try:
            ocr_info = self.api_client.get_ocr_status(self._uid)
            if ocr_info.get("has_ocr_result"):
                self._ocr_notified = True
                self._stop_ocr_poll()
                logger.info("OCR artifact detected via poll for %s", self._uid[:8])
                self.status_message.emit(
                    "✅ OCR завершён! Можно переходить к привязке.", 5000,
                )
                self.beads.set_state(BEAD_OCR, BeadState.COMPLETED)
                if "ocr" in self._action_buttons:
                    self._action_buttons["ocr"].setEnabled(True)
                    self._action_buttons["ocr"].setStyleSheet(_BTN_STYLE_GREEN)
                if "ocr_binding" in self._action_buttons:
                    self._action_buttons["ocr_binding"].setEnabled(True)
                    self._action_buttons["ocr_binding"].setStyleSheet(_BTN_STYLE_YELLOW)
                    self.beads.set_state(BEAD_OCR_BINDING, BeadState.AVAILABLE)
        except Exception:
            pass  # не блокируем UI

    # =================================================================
    # Бусины
    # =================================================================

    def _update_beads(self, status: DiagramStatus):
        # Получить целевое состояние
        target = {}
        for idx, state in _beads_for_status(status):
            target[idx] = state

        # Перекрыть pipe/junction если подтверждены по отдельности
        if status == DiagramStatus.VALIDATING_MASKS:
            if self._pipe_confirmed:
                target[BEAD_VAL_PIPE] = BeadState.COMPLETED
        if status == DiagramStatus.VALIDATING_JUNCTIONS:
            if self._junction_confirmed:
                target[BEAD_VAL_JUNCTION] = BeadState.COMPLETED

        # Применить — не описанные = UNAVAILABLE
        for i in range(NUM_BEADS):
            self.beads.set_state(i, target.get(i, BeadState.UNAVAILABLE))

    # =================================================================
    # GIF активного этапа
    # =================================================================

    def _update_gif(self, status: DiagramStatus):
        """Показать анимацию активного этапа: в процессе, иначе доступного."""
        states = dict(_beads_for_status(status))

        # Перекрыть pipe/junction если подтверждены по отдельности
        if status == DiagramStatus.VALIDATING_MASKS and self._pipe_confirmed:
            states[BEAD_VAL_PIPE] = BeadState.COMPLETED
        if status == DiagramStatus.VALIDATING_JUNCTIONS and self._junction_confirmed:
            states[BEAD_VAL_JUNCTION] = BeadState.COMPLETED

        # самый ранний активный этап (передний план оператора),
        # фоновые параллельные процессы — позже по индексу
        active = sorted(i for i, s in states.items()
                        if s in (BeadState.IN_PROGRESS, BeadState.AVAILABLE))
        active_idx = active[0] if active else None

        if active_idx is None:
            self.gif_player.set_gif(None)
            return
        key = _IDX_KEY.get(active_idx)
        self.gif_player.set_gif(_gif_path_for(key, status))

    # =================================================================
    # Кнопки
    # =================================================================

    def _update_buttons(self, status: DiagramStatus, error_stage: str = None):
        available, completed, processing = _buttons_for_status(status)

        # Перекрыть pipe/junction если подтверждены по отдельности
        if status == DiagramStatus.VALIDATING_MASKS:
            if self._pipe_confirmed:
                completed.add("pipe")
                available.discard("pipe")
        if status == DiagramStatus.VALIDATING_JUNCTIONS:
            if self._junction_confirmed:
                completed.add("junction")
                available.discard("junction")

        # Map error_stage to button key for retry
        _STAGE_TO_KEY = {
            "detecting": "detect",
            "segmenting": "segment",
            "skeletonizing": "segment",
            "skeletonizing_simple": "pipe",
            "detecting_junctions": "junction",
            "building_graph": "graph",
            "validating_graph": "val_graph",
            "contour_extraction": "contours",
            "generating_fxml": "fxml",
            "ocr": "ocr",
        }
        _error_key = None
        if status == DiagramStatus.ERROR and error_stage:
            _error_key = _STAGE_TO_KEY.get(error_stage.lower())

        # Original button labels for resetting retry text
        _KEY_LABELS = {k: v for k, v in self._BUTTON_DEFS}

        for key, btn in self._action_buttons.items():
            if key in processing:
                btn.setEnabled(False)
                btn.setStyleSheet(_BTN_STYLE_BLUE)
            elif key in available:
                btn.setEnabled(True)
                btn.setStyleSheet(_BTN_STYLE_YELLOW)
            elif key in completed:
                btn.setEnabled(True)  # кликабельна для отката
                btn.setStyleSheet(_BTN_STYLE_GREEN)
            elif status == DiagramStatus.ERROR:
                if _error_key and key == _error_key:
                    # Failed stage — retry button (red, clickable)
                    btn.setEnabled(True)
                    btn.setText(f"🔄 {_KEY_LABELS.get(key, key)}")
                    btn.setStyleSheet(_BTN_STYLE_RED)
                else:
                    btn.setEnabled(False)
                    btn.setStyleSheet(_BTN_STYLE_GRAY)
            else:
                btn.setEnabled(False)
                btn.setStyleSheet(_BTN_STYLE_GRAY)

        # Сбросить текст кнопок если не в ошибке
        if status != DiagramStatus.ERROR:
            for key, btn in self._action_buttons.items():
                label = _KEY_LABELS.get(key)
                if label:
                    btn.setText(label)
    # =================================================================
    # Навигация
    # =================================================================

    def _on_back_to_list(self):
        """← Назад (из header) → вернуться к списку диаграмм."""
        self.cleanup()
        self.back_requested.emit()

    # =================================================================
    # Открытие / закрытие вкладок
    # =================================================================

    def _open_tab(self, tab_widget: QWidget, tab_key: str):
        """Открыть виджет как полноэкранную вкладку."""
        self._force_close_tab()

        self._active_tab = tab_widget
        self._active_tab_key = tab_key

        # Приостановить фоновые таймеры — они вызывают repaints
        # и дёргают WebEngine (мерцание CVAT, просадка FPS)
        self._stop_ocr_poll()
        self._was_status_watching = (
            self._uid and self.status_provider.is_watching(self._uid)
        )
        if self._was_status_watching:
            self.status_provider.unwatch(self._uid)

        # Вставляем кнопку "← Назад" в начало layout'а вкладки
        # Ищем первый QHBoxLayout (toolbar вкладки) и вставляем туда
        tab_layout = tab_widget.layout()
        if tab_layout:
            # Создаём маленькую кнопку назад
            self._btn_back_injected = QPushButton("← Назад")
            self._btn_back_injected.setFixedSize(70, 26)
            self._btn_back_injected.setStyleSheet(
                "QPushButton { background: #555; color: white; "
                "border-radius: 3px; font-size: 11px; }"
                "QPushButton:hover { background: #777; }"
            )
            self._btn_back_injected.clicked.connect(self._close_active_tab)

            # Вставляем в первый layout-item если это QHBoxLayout
            first_item = tab_layout.itemAt(0)
            target_layout = first_item.layout() if (first_item and first_item.layout()) else None
            if target_layout:
                target_layout.insertWidget(0, self._btn_back_injected)
            else:
                # Fallback: вставить сверху
                tab_layout.insertWidget(0, self._btn_back_injected)

            # Кнопка ⚙ — настройки оформления (затемнение фона, цвета), если
            # вкладка их поддерживает. Рядом с «← Назад».
            if hasattr(tab_widget, "toggle_appearance_panel"):
                self._btn_appearance_injected = QPushButton("⚙")
                self._btn_appearance_injected.setFixedSize(28, 26)
                self._btn_appearance_injected.setToolTip(
                    "Оформление вкладки (затемнение фона, цвета)"
                )
                self._btn_appearance_injected.setStyleSheet(
                    "QPushButton { background: #555; color: white; "
                    "border-radius: 3px; font-size: 14px; }"
                    "QPushButton:hover { background: #777; }"
                )
                self._btn_appearance_injected.clicked.connect(
                    tab_widget.toggle_appearance_panel
                )
                if target_layout:
                    target_layout.insertWidget(1, self._btn_appearance_injected)
                else:
                    tab_layout.insertWidget(1, self._btn_appearance_injected)

        # Добавить в контейнер
        self._tab_container_layout.addWidget(tab_widget)

        # Скрыть header, показать tab
        self.header_panel.setUpdatesEnabled(False)
        self.header_panel.hide()
        self.content_stack.setCurrentIndex(1)

        # Запустить автосохранение
        self._autosave.start(tab_widget)

    # Маппинг: tab_key → статус, из которого нужно откатить при закрытии без confirm
    _VALIDATING_ROLLBACK = {
        "frame": (DiagramStatus.CLEANING_FRAME, "uploaded"),
        "junction": (DiagramStatus.VALIDATING_JUNCTIONS, "detected_junctions"),
        "pipe":     (DiagramStatus.VALIDATING_MASKS, "skeletonized"),
        "val_graph": (DiagramStatus.VALIDATING_GRAPH, "built"),
    }

    def _close_active_tab(self):
        """← Назад (из вкладки) → закрыть, вернуться к header."""
        if not self._active_tab:
            return

        # Остановить автосохранение
        self._autosave.stop()

        # Спросить о сохранении
        tab = self._active_tab
        tab_key = self._active_tab_key
        if hasattr(tab, 'has_unsaved_changes') and tab.has_unsaved_changes():
            reply = QMessageBox.question(
                self, "Закрыть вкладку",
                "Есть несохранённые изменения. Сохранить перед закрытием?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                return
            if reply == QMessageBox.StandardButton.Yes:
                saved = False
                if hasattr(tab, '_save_graph'):
                    saved = tab._save_graph()
                elif hasattr(tab, '_save_masks'):
                    saved = tab._save_masks()
                elif hasattr(tab, '_save_mask'):
                    saved = tab._save_mask()
                if not saved:
                    return  # сохранение не удалось — не закрываем

        # Проверить _saved перед удалением (двойная страховка для масок)
        self._detect_saved_mask(tab)

        # Откатить VALIDATING_* статус если вкладка закрыта без подтверждения
        self._rollback_if_validating()

        self._close_tab_and_restore_header()

        # Проверить завершение масок
        self._check_masks_completion()

    def _force_close_tab(self):
        """Закрыть вкладку без вопросов (при смене диаграммы / cleanup)."""
        if self._active_tab:
            self._autosave.stop()
            self._rollback_if_validating()
            self._remove_tab_widget()
            self.header_panel.setUpdatesEnabled(True)
            self._was_status_watching = False

    def _rollback_if_validating(self):
        """Откатить VALIDATING_* статус если вкладка не была подтверждена."""
        tab = self._active_tab
        tab_key = self._active_tab_key
        if not tab or not tab_key:
            return
        confirmed = getattr(tab, '_confirmed', False)
        if confirmed or tab_key not in self._VALIDATING_ROLLBACK:
            return
        validating_status, rollback_target = self._VALIDATING_ROLLBACK[tab_key]
        try:
            diagram = self.api_client.get_diagram(self._uid)
            if diagram.status == validating_status:
                preserve_ocr = tab_key == "val_graph"
                preserve_contours = tab_key == "val_graph"
                self.api_client.rollback_diagram(
                    self._uid, rollback_target,
                    preserve_ocr=preserve_ocr,
                    preserve_contours=preserve_contours,
                )
                logger.info(
                    "Rolled back %s → %s (tab %s force-closed)",
                    validating_status.value, rollback_target, tab_key,
                )
        except Exception as exc:
            logger.warning("Failed to rollback on force close: %s", exc)

    def _remove_tab_widget(self):
        """Убрать виджет вкладки из layout."""
        if self._active_tab:
            self._tab_container_layout.removeWidget(self._active_tab)
            self._active_tab.setParent(None)
            self._active_tab.deleteLater()
            self._active_tab = None
            self._active_tab_key = ""
            self._btn_back_injected = None

    def _close_tab_and_restore_header(self):
        """Общий метод: убрать вкладку, восстановить header и таймеры."""
        self._remove_tab_widget()

        # Восстановить header (отменить setUpdatesEnabled(False) из _open_tab)
        self.header_panel.setUpdatesEnabled(True)
        self.header_panel.show()
        self.content_stack.setCurrentIndex(0)

        # Возобновить фоновые таймеры
        if getattr(self, '_was_status_watching', False) and self._uid:
            self.status_provider.watch(self._uid)
            self._was_status_watching = False

        self._refresh_status()
        self._start_ocr_poll()

    # =================================================================
    # Маски — двойная страховка
    # =================================================================

    def _detect_saved_mask(self, tab_widget):
        """Перед удалением вкладки — проверить _confirmed флаг."""
        key = self._active_tab_key
        confirmed = getattr(tab_widget, '_confirmed', False)

        if key == "junction" and confirmed:
            logger.info("Junction tab _confirmed=True → confirmed")
            self._junction_confirmed = True
        elif key == "pipe" and confirmed:
            logger.info("Pipe tab _confirmed=True → confirmed")
            self._pipe_confirmed = True

    def _check_masks_completion(self):
        """Если pipe маска подтверждена → complete_mask_validation → скелетизация + junction detection."""
        if not self._pipe_confirmed:
            return

        try:
            logger.info("Pipe mask confirmed, calling complete_mask_validation")
            result = self.api_client.complete_mask_validation(self._uid)
            task_id = result.get("task_id")
            logger.info("complete_mask_validation OK, task_id=%s", task_id)
            msg = "✅ Валидация масок завершена"
            if task_id:
                msg += " → скелетизация + детекция перекрёстков запущена"
            else:
                msg += " (цепочка уже запущена)"
            self.status_provider.watch(self._uid)
            self.status_message.emit(msg, 5000)
        except Exception as exc:
            logger.error("complete_mask_validation failed: %s", exc)
            # Don't show error dialog — chain may have already moved past this point
            self.status_provider.watch(self._uid)
            self._refresh_status()

    # =================================================================
    # Кнопки действий — фоновые процессы
    # =================================================================

    @Slot()
    def _on_button_click(self, key: str, original_handler):
        """Обработчик клика по кнопке — если этап уже пройден, предложить откат."""
        status = self._last_status
        _, completed, _ = _buttons_for_status(status)

        # FXML: всегда перегенерировать (диалог выбора размера внутри _start_fxml)
        if key == "fxml":
            original_handler()
            return

        if key in completed:
            # Этап уже пройден — предложить откат
            target = self._ROLLBACK_TARGET.get(key, "")
            reply = QMessageBox.question(
                self, "Откат",
                f"Этап «{key}» уже пройден.\n"
                f"Откатить до «{target}» и перезапустить?\n\n"
                f"Все последующие артефакты будут удалены.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            try:
                QApplication.setOverrideCursor(Qt.WaitCursor)
                # Preserve OCR/contour artifacts when rolling back graph/contour stages,
                # because OCR and SAM2 run in parallel and are independent of graph.
                preserve_ocr = key in ("graph", "val_graph", "contours")
                preserve_contours = key in ("graph", "val_graph", "contours")
                result = self.api_client.rollback_diagram(
                    self._uid, target,
                    preserve_ocr=preserve_ocr,
                    preserve_contours=preserve_contours,
                )
                deleted = result.get("deleted_artifacts", 0)
                self.status_message.emit(
                    f"↩ Откат до {target}: удалено {deleted} артефактов", 3000
                )
                # Reset OCR notification — let _refresh_status re-detect from artifact
                self._ocr_notified = False
                self._refresh_status()
            except APIError as exc:
                QMessageBox.warning(
                    self, "Ошибка отката",
                    f"Не удалось откатить:\n{exc.message}",
                )
                return
            finally:
                QApplication.restoreOverrideCursor()

        # Запустить оригинальный обработчик
        original_handler()

    def _start_detection(self):
        """Запустить детекцию — выбор модели если доступно несколько."""
        try:
            project_code = self._get_project_code()
            models_info = self.api_client.get_detection_models(project_code)
            models = models_info.get("models", [])
            default_id = models_info.get("default_model", "default")

            if len(models) <= 1:
                # Одна модель — запускаем сразу
                self._run_detection(None)
                return

            # Несколько моделей — показываем меню выбора
            btn = self._action_buttons.get("detect")
            if not btn:
                return

            menu = QMenu(self)
            for m in models:
                mid = m["id"]
                label = m["name"] or mid
                if m.get("description"):
                    label += f"  ({m['description']})"
                if mid == default_id:
                    label = f"⭐ {label}"
                action = QAction(label, menu)
                action.setData(mid)
                action.triggered.connect(
                    lambda checked=False, _mid=mid: self._run_detection(_mid)
                )
                menu.addAction(action)

            # Показать под кнопкой
            menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

        except APIError as exc:
            # Если API не отвечает — fallback на default
            logger.warning("Failed to get detection models: %s, using default", exc)
            self._run_detection(None)
        except Exception as exc:
            logger.warning("Detection model fetch error: %s, using default", exc)
            self._run_detection(None)

    def _run_detection(self, model_id: str = None):
        """Запустить детекцию с выбранной моделью."""
        try:
            self.api_client.start_detection(self._uid, model_id=model_id)
            self.status_provider.watch(self._uid)
            label = f" ({model_id})" if model_id else ""
            self.status_message.emit(f"🔍 Детекция запущена{label}", 3000)
            self._refresh_status()
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось запустить детекцию:\n{exc.message}",
            )

    @Slot()
    def _start_segmentation(self):
        try:
            self.api_client.start_segmentation(self._uid)
            self.status_provider.watch(self._uid)
            self.status_message.emit("🖼️ Сегментация запущена", 3000)
            self._refresh_status()
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось запустить сегментацию:\n{exc.message}",
            )

    @Slot()
    def _start_graph_build(self):
        try:
            self.api_client.build_graph(self._uid)
            self.status_provider.watch(self._uid)
            self.status_message.emit("📊 Построение графа запущено", 3000)
            self._refresh_status()
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось запустить построение графа:\n{exc.message}",
            )

    @Slot()
    def _start_ocr(self):
        """Ручной запуск/retry OCR."""
        try:
            self.api_client.start_ocr(self._uid)
            # Immediately show OCR bead as in-progress (don't wait for poll)
            self.beads.set_state(BEAD_OCR, BeadState.IN_PROGRESS)
            if "ocr" in self._action_buttons:
                self._action_buttons["ocr"].setEnabled(False)
                self._action_buttons["ocr"].setStyleSheet(_BTN_STYLE_BLUE)
            # Reset downstream beads — OCR result is deleted on retry
            self.beads.set_state(BEAD_OCR_BINDING, BeadState.UNAVAILABLE)
            if "ocr_binding" in self._action_buttons:
                self._action_buttons["ocr_binding"].setEnabled(False)
                self._action_buttons["ocr_binding"].setStyleSheet(_BTN_STYLE_GRAY)
            self._ocr_notified = False
            self.status_provider.watch(self._uid)
            self._start_ocr_poll()
            self.status_message.emit("🔍 OCR запущен", 3000)
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось запустить OCR:\n{exc.message}",
            )

    def _open_ocr_binding(self):
        """Открыть вкладку привязки OCR текста к узлам."""
        if not self._uid:
            return

        try:
            from ui.tabs.ocr_binding_tab import OcrBindingTab

            # Определить путь к project config
            project_config_path = self._get_project_config_path()

            tab = OcrBindingTab(
                diagram_uid=self._uid,
                diagram_name=self._diagram_name,
                api_client=self.api_client,
                project_config_path=project_config_path,
            )
            tab.status_message.connect(
                lambda msg: self.status_message.emit(msg, 5000)
            )
            tab.confirmed.connect(self._on_ocr_binding_confirmed)

            self._open_tab(tab, "ocr_binding")
        except Exception as exc:
            logger.error("Failed to open OCR binding tab: %s", exc, exc_info=True)
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось открыть привязку OCR:\n{exc}",
            )

    def _start_fxml(self):
        # Без диалога размера — всегда оригинальные пиксели изображения.
        page_size = None

        # Запускаем генерацию (перегенерация если уже COMPLETED)
        try:
            self._fxml_save_prompted = False
            self.api_client.generate_fxml(self._uid, page_size=page_size)
            self.status_provider.watch(self._uid)
            size_label = page_size or "оригинал"
            self.status_message.emit(f"📄 Генерация FXML ({size_label}) запущена", 3000)
            self._refresh_status()
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось запустить генерацию FXML:\n{exc.message}",
            )

    def _ask_page_size(self):
        """Диалог выбора размера страницы. Возвращает 'A3', 'A4'... или None, или False (отмена)."""
        items = [
            "Оригинал (пиксели изображения)",
            "A4 landscape (297×210 мм)",
            "A3 landscape (420×297 мм)",
            "A2 landscape (594×420 мм)",
            "A1 landscape (841×594 мм)",
            "A0 landscape (1189×841 мм)",
        ]
        item, ok = QInputDialog.getItem(
            self, "Размер страницы FXML",
            "Выберите целевой размер:",
            items, 2, False,  # default = A3
        )
        if not ok:
            return False

        mapping = {
            items[0]: None,
            items[1]: "A4",
            items[2]: "A3",
            items[3]: "A2",
            items[4]: "A1",
            items[5]: "A0",
        }
        return mapping.get(item)

    def _download_fxml(self):
        """Скачать сгенерированный FXML на компьютер пользователя."""
        import os
        base = (self._diagram_name or "diagram").strip()
        default_name = (os.path.splitext(base)[0] or "diagram") + ".fxml"
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить FXML", default_name,
            "FXML files (*.fxml);;XML files (*.xml);;All files (*)",
        )
        if not save_path:
            return

        try:
            from pathlib import Path
            self.api_client.download_artifact(self._uid, "fxml", Path(save_path))
            self.status_message.emit(f"📄 FXML сохранён: {save_path}", 5000)
            QMessageBox.information(
                self, "Готово",
                f"FXML файл сохранён:\n{save_path}",
            )
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось скачать FXML:\n{exc.message}",
            )

    # =================================================================
    # Кнопки действий — валидации (открывают вкладки)
    # =================================================================

    @Slot()
    def _open_frame(self):
        """Открыть вкладку очистки рамки (этап 0)."""
        try:
            diagram = self.api_client.get_diagram(self._uid)
            if diagram.status == DiagramStatus.UPLOADED:
                try:
                    self.api_client.start_frame_removal(self._uid)
                except APIError:
                    pass

            from ui.tabs.frame_tab import FrameTab
            tab = FrameTab(
                diagram_uid=self._uid,
                diagram_name=self._diagram_name,
                api_client=self.api_client,
            )
            tab.confirmed.connect(self._on_frame_confirmed)
            tab.status_message.connect(
                lambda msg: self.status_message.emit(msg, 5000),
            )
            self._open_tab(tab, "frame")

        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось открыть очистку рамки:\n{exc.message}",
            )

    @Slot()
    def _on_frame_confirmed(self):
        """Очистка рамки завершена (save+complete или skip) — статус уже FRAME_CLEANED."""
        logger.info("Frame confirmed via signal")
        self._close_tab_and_restore_header()
        self._refresh_status()

    def _open_cvat(self):
        try:
            diagram = self.api_client.get_diagram(self._uid)

            # Создать CVAT task если нет
            if not diagram.cvat_task_id:
                self.api_client.create_cvat_task(self._uid)
                diagram = self.api_client.get_diagram(self._uid)

            # Перевести в VALIDATING_BBOX если нужно
            if diagram.status == DiagramStatus.DETECTED:
                result = self.api_client.open_cvat_validation(self._uid)
                cvat_url = result.get("cvat_url")
            else:
                cvat_url = self.api_client.get_cvat_url(self._uid)

            if not cvat_url:
                QMessageBox.warning(self, "Ошибка", "CVAT URL не найден")
                return

            from ui.tabs.cvat_tab import CvatTab
            tab = CvatTab(
                diagram_uid=self._uid,
                cvat_url=cvat_url,
                diagram_name=self._diagram_name,
                cvat_task_id=diagram.cvat_task_id,
                cvat_job_id=diagram.cvat_job_id,
            )
            tab.confirmed.connect(self._on_cvat_confirmed)
            tab.status_message.connect(
                lambda msg: self.status_message.emit(msg, 5000),
            )
            self._open_tab(tab, "cvat")

        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось открыть CVAT:\n{exc.message}",
            )

    @Slot()
    def _open_junction(self):
        try:
            diagram = self.api_client.get_diagram(self._uid)
            # Start junction validation if coming from DETECTED_JUNCTIONS
            if diagram.status == DiagramStatus.DETECTED_JUNCTIONS:
                try:
                    self.api_client.start_junction_validation(self._uid)
                except APIError:
                    pass

            from ui.tabs.junction_tab import JunctionTab
            tab = JunctionTab(
                diagram_uid=self._uid,
                diagram_name=self._diagram_name,
                api_client=self.api_client,
            )
            tab.confirmed.connect(self._on_junction_confirmed)
            tab.status_message.connect(
                lambda msg: self.status_message.emit(msg, 5000),
            )
            self._open_tab(tab, "junction")

        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось открыть валидацию junction:\n{exc.message}",
            )

    @Slot()
    def _open_pipe(self):
        try:
            diagram = self.api_client.get_diagram(self._uid)
            if diagram.status == DiagramStatus.SKELETONIZED:
                try:
                    self.api_client.start_mask_validation(self._uid)
                except APIError:
                    pass

            from ui.tabs.pipe_tab import PipeTab
            tab = PipeTab(
                diagram_uid=self._uid,
                diagram_name=self._diagram_name,
                api_client=self.api_client,
            )
            tab.set_project_code(self._get_project_code())
            tab.confirmed.connect(self._on_pipe_confirmed)
            tab.status_message.connect(
                lambda msg: self.status_message.emit(msg, 5000),
            )
            self._open_tab(tab, "pipe")

        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось открыть валидацию pipe:\n{exc.message}",
            )

    @Slot()
    def _open_graph_validation(self):
        """Фаза 1 — SimpleGraphTab: базовая валидация графа."""
        try:
            diagram = self.api_client.get_diagram(self._uid)
            if diagram.status == DiagramStatus.BUILT:
                try:
                    self.api_client.start_graph_validation(self._uid)
                except APIError:
                    pass

            _pc = self._get_project_code()

            from ui.tabs.simple_graph_tab import SimpleGraphTab
            tab = SimpleGraphTab(
                diagram_uid=self._uid,
                diagram_name=self._diagram_name,
                api_client=self.api_client,
            )
            tab.set_project_code(_pc)
            tab.confirmed.connect(self._on_simple_graph_confirmed)
            tab.status_message.connect(
                lambda msg: self.status_message.emit(msg, 5000),
            )
            self._open_tab(tab, "graph_val")

        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось открыть валидацию графа:\n{exc.message}",
            )

    @Slot()
    def _open_graph_editor(self):
        """Фаза 2 — AdvancedGraphTab: продвинутый редактор графа."""
        try:
            _pc = self._get_project_code()

            from ui.tabs.advanced_graph_tab import AdvancedGraphTab
            tab = AdvancedGraphTab(
                diagram_uid=self._uid,
                diagram_name=self._diagram_name,
                api_client=self.api_client,
            )
            tab.set_project_code(_pc)
            tab.confirmed.connect(self._on_graph_confirmed)
            tab.status_message.connect(
                lambda msg: self.status_message.emit(msg, 5000),
            )
            self._open_tab(tab, "edit_graph")

        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось открыть редактор графа:\n{exc.message}",
            )

    def _get_project_code(self) -> str:
        """Получить код проекта (с кэшированием)."""
        _pc = getattr(self, "_project_code", None)
        if not _pc:
            try:
                _diag = self.api_client.get_diagram(self._uid)
                _pc = _diag.project_code
                self._project_code = _pc
            except Exception:
                _pc = "thermohydraulics"
        return _pc

    def _get_project_config_path(self) -> str:
        """Получить путь к YAML конфигу проекта."""
        from pathlib import Path
        code = self._get_project_code()
        project_root = Path(__file__).resolve().parent.parent.parent
        logger.info("Project code=%s, project_root=%s", code, project_root)
        candidates = [
            # Подпапка: configs/projects/thermohydraulics/thermohydraulics.yaml
            project_root / "configs" / "projects" / code / f"{code}.yaml",
            project_root / "configs" / "projects" / code / f"{code}.yml",
            # Плоская: configs/projects/thermohydraulics.yaml
            project_root / "configs" / "projects" / f"{code}.yaml",
            project_root / "configs" / "projects" / f"{code}.yml",
            Path(f"configs/projects/{code}/{code}.yaml"),
            Path(f"configs/projects/{code}.yaml"),
        ]
        for p in candidates:
            logger.debug("Trying: %s (exists=%s)", p, p.exists())
            if p.exists():
                logger.info("Project config found: %s", p)
                return str(p)
        logger.warning("Project config not found for code=%s, tried: %s", code,
                        [str(c) for c in candidates])
        return ""

    # =================================================================
    # Обработчики confirmed-сигналов от вкладок
    # =================================================================

    @Slot()
    def _on_cvat_confirmed(self):
        """CVAT сохранил аннотации → скачать аннотации → закрыть."""
        logger.info("CVAT confirmed, fetching annotations")
        try:
            result = self.api_client.fetch_cvat_annotations(self._uid)
            count = result.get("annotation_count", 0)
            self.status_message.emit(
                f"✅ Получено {count} валидированных аннотаций", 5000,
            )
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось получить аннотации:\n{exc.message}",
            )

        self._close_tab_and_restore_header()

    @Slot()
    def _on_junction_confirmed(self):
        """Junction маски подтверждены → complete_junction_validation → graph build."""
        logger.info("Junction confirmed via signal")
        self._junction_confirmed = True

        self._close_tab_and_restore_header()

        try:
            result = self.api_client.complete_junction_validation(self._uid)
            task_id = result.get("task_id")
            msg = "✅ Валидация перекрёстков завершена"
            if task_id:
                msg += " → построение графа запущено"
                self.status_provider.watch(self._uid)
            self.status_message.emit(msg, 5000)
        except Exception as exc:
            logger.error("complete_junction_validation failed: %s", exc)
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось завершить валидацию перекрёстков:\n{exc}",
            )

        self._refresh_status()

    @Slot()
    def _on_pipe_confirmed(self):
        """Pipe маска подтверждена (сигнал confirmed от PipeTab)."""
        logger.info("Pipe confirmed via signal")
        self._pipe_confirmed = True

        self._close_tab_and_restore_header()
        self._check_masks_completion()

    @Slot()
    def _on_simple_graph_confirmed(self):
        """Простая валидация графа завершена."""
        logger.info("Simple graph confirmed")
        try:
            self.api_client.complete_simple_graph_validation(self._uid)
            self.status_message.emit("✅ Валидация графа завершена", 5000)
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось завершить валидацию графа:\n{exc.message}",
            )
            self._close_tab_and_restore_header()
            return

        self._close_tab_and_restore_header()

        # Не форсим OCR_PROCESSING — следующая бусина "Контуры"
        # _refresh_status покажет: CONTOURS=AVAILABLE, OCR=IN_PROGRESS (parallel)
        self._refresh_status()

        # Polling для OCR artifact (параллельный)
        self.status_provider.watch(self._uid)

    @Slot()
    def _open_contours(self):
        """Открыть вкладку валидации SAM2 контуров."""
        if not self._uid:
            return

        # Контуры распознаются ПО ТРЕБОВАНИЮ внутри вкладки — открываем её
        # всегда, даже если результата ещё нет (там кнопки распознавания).
        try:
            from ui.tabs.contour_tab import ContourTab

            tab = ContourTab(
                diagram_uid=self._uid,
                diagram_name=self._diagram_name,
                api_client=self.api_client,
            )
            tab.confirmed.connect(self._on_contours_confirmed)
            tab.status_message.connect(
                lambda msg: self.status_message.emit(msg, 5000)
            )
            self._open_tab(tab, "contours")

        except Exception as exc:
            logger.error(
                "Failed to open contour tab: %s", exc, exc_info=True
            )
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось открыть редактор контуров:\n{exc}",
            )

    @Slot()
    def _on_contours_confirmed(self):
        """Контуры подтверждены → CONTOURS_VALIDATED."""
        logger.info("Contours confirmed")
        try:
            self.api_client.complete_contour_validation(self._uid)
            self.status_message.emit("✅ Контуры приняты", 5000)
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось завершить валидацию контуров:\n{exc.message}",
            )
            self._close_tab_and_restore_header()
            return

        self._close_tab_and_restore_header()

    @Slot()
    def _on_graph_confirmed(self):
        """Граф подтверждён (Advanced) → complete_graph_validation → auto FXML."""
        logger.info("Graph confirmed via signal")
        try:
            self.api_client.complete_graph_validation(self._uid)
            self.status_message.emit(
                "✅ Валидация графа завершена → генерация FXML запущена", 5000,
            )
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось завершить валидацию графа:\n{exc.message}",
            )
            self._close_tab_and_restore_header()
            return

        self._close_tab_and_restore_header()

        # Backend auto-dispatches FXML generation after graph validation.
        # Show GENERATING_FXML immediately and start polling for COMPLETED.
        self._apply_status(DiagramStatus.GENERATING_FXML)
        self.status_provider.watch(self._uid)

    @Slot()
    def _on_ocr_binding_confirmed(self):
        """Привязка OCR подтверждена."""
        logger.info("OCR binding confirmed")
        self.status_message.emit("✅ OCR привязки применены", 5000)

        self._close_tab_and_restore_header()

    # =================================================================
    # Status Provider callback
    # =================================================================

    @Slot(str, object)
    def _on_status_updated(self, uid: str, status_info):
        if uid == self._uid:
            logger.info("Poll update: %s", status_info.status.value)
            was_notified = self._ocr_notified
            self._apply_status(status_info.status,
                               error_stage=getattr(status_info, 'error_stage', None))

            # B6.4: Показать уведомление при первом обнаружении OCR артефакта
            if self._ocr_notified and not was_notified:
                self.status_message.emit(
                    "✅ OCR завершён! Можно переходить к привязке.", 5000,
                )

            # Авто-скачивание FXML при завершении генерации
            if status_info.status == DiagramStatus.COMPLETED:
                self.status_message.emit(
                    "✅ FXML готов! Нажмите 📄 FXML для скачивания.", 5000,
                )

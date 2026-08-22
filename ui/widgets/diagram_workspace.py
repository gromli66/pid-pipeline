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
    QMenu, QDialog, QComboBox, QLineEdit, QDialogButtonBox,
)
from PySide6.QtCore import Qt, Signal, Slot, QTimer, QThread
from PySide6.QtGui import QAction, QFont

from ui.services.api_client import APIClient, APIError, DiagramStatus
from ui.services.client_logging import bind_uid
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
     # Гейт (решение заказчика 2026-07-28, §3.2 плана авто-раскладки): вкладка
     # не открывается, пока не подтверждена привязка. Раньше здесь стоял
     # OCR_COMPLETED — «привязка внутри ручной правки»; теперь привязка
     # делается в своей вкладке, а этот гейт прячет 2-5 минут счёта раскладки.
     # Режим «ОКР привязка» внутри редактора остаётся для доводки после входа.
     DiagramStatus.OCR_BOUND),

    (BEAD_FXML,          "fxml",        DiagramStatus.COMPLETED,
     {DiagramStatus.GENERATING_FXML},
     DiagramStatus.GENERATING_FXML),
]


# Ручные этапы, которые при ОТКРЫТИИ переводят диаграмму в *ING-статус.
# status → (ключ кнопки для повторного входа, стабильный этап для отката).
_MANUAL_INPROGRESS = {
    DiagramStatus.CLEANING_FRAME:       ("frame",     "uploaded"),
    DiagramStatus.VALIDATING_JUNCTIONS: ("junction",  "detected_junctions"),
    DiagramStatus.VALIDATING_MASKS:     ("pipe",      "skeletonized"),
    DiagramStatus.VALIDATING_GRAPH:     ("val_graph", "built"),
}


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



def _stage_stuck(stage, limit_s: float = None, now=None) -> bool:
    """Стадия висит дольше предела ожидания — кнопку больше не глушим.

    Тот же предел, что у гейта «Ручной правки» (`layout_gate.WAIT_LIMIT_S`):
    правило одно на оба места, иначе гейт пускал бы, а кнопка оставалась
    мёртвой. Отсчёт от `started_at`, а если задача так и не стартовала — от
    `created_at`; нет ни того ни другого — не глушим вовсе.
    """
    from datetime import datetime
    from ui.services.layout_gate import WAIT_LIMIT_S, _parse_dt
    t = _parse_dt(stage.get("started_at")) or _parse_dt(stage.get("created_at"))
    if t is None:
        return True
    return ((now or datetime.utcnow()) - t).total_seconds() > (
        limit_s or WAIT_LIMIT_S)


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


def _btn_fill_style(frac: float) -> str:
    """Стиль кнопки бегущего этапа с заливкой прогресса: зелёное слева до frac, синее справа."""
    f = max(0.02, min(0.98, frac))
    g = min(f + 0.006, 0.999)
    return (
        "QPushButton {"
        "  background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
        f"    stop:0 #43A047, stop:{f:.3f} #43A047,"
        f"    stop:{g:.3f} #2196F3, stop:1 #2196F3);"
        "  color: white; font-weight: bold;"
        "  padding: 6px 10px; border-radius: 4px; border: none;"
        "}"
    )


# =====================================================================
# GIF активного этапа
# =====================================================================

_IDX_KEY = {idx: key for (idx, key, *_rest) in _BEAD_DEFS}
_KEY_IDX = {key: idx for idx, key in _IDX_KEY.items()}

# ProcessingStage.stage_type (value) → ключ бусины/кнопки (по-этапная изоляция ошибок)
_STAGE_TYPE_TO_KEY = {
    "frame_removal": "frame",
    "detection": "detect",
    "cvat_validation": "cvat",
    # Классификация направления своей бусины не имеет: она встроена между
    # валидацией детекции и сегментацией, статуса не меняет и перезапускается
    # тем же `POST /segment` (сервер по `error_stage` заводит цепочку заново
    # С НАПРАВЛЕНИЯ — `app/api/segmentation.py:22,96`). Без этой строки её
    # падение не показывалось вовсе: ни красной бусины, ни окна отчёта.
    "direction_classification": "segment",
    "segmentation": "segment",
    "skeletonization": "segment",
    "mask_validation": "pipe",
    "junction_classification": "junction",
    "final_skeletonization": "segment",
    "graph_building": "graph",
    "graph_validation": "val_graph",
    "contour_extraction": "contours",
    "ocr": "ocr",
    # Раскладка считается за спиной оператора, пока он в «Привязке подписей»,
    # и льёт процентами кнопку той вкладки, которую держит закрытой (§3.2).
    "layout": "edit_graph",
    "fxml_generation": "fxml",
}

# Завершённый stage_type → соответствующий DiagramStatus (реконструкция прогресса при ERROR)
_STAGE_DONE_STATUS = {
    "frame_removal": DiagramStatus.FRAME_CLEANED,
    "detection": DiagramStatus.DETECTED,
    "cvat_validation": DiagramStatus.VALIDATED_BBOX,
    "segmentation": DiagramStatus.SKELETONIZED,
    "skeletonization": DiagramStatus.SKELETONIZED,
    "mask_validation": DiagramStatus.VALIDATED_MASKS,
    "junction_classification": DiagramStatus.DETECTED_JUNCTIONS,
    "final_skeletonization": DiagramStatus.SKELETONIZED_FINAL,
    "graph_building": DiagramStatus.BUILT,
    "graph_validation": DiagramStatus.VALIDATED_GRAPH,
    "contour_extraction": DiagramStatus.CONTOURS_VALIDATED,
    "ocr": DiagramStatus.OCR_COMPLETED,
    "fxml_generation": DiagramStatus.COMPLETED,
}
_GIF_DIR = Path(__file__).resolve().parent.parent / "resources" / "beads"
_GIF_FILES = {
    "frame":       "pid_frame_clean.gif",
    "detect":      "pid_detection_light.gif",
    "cvat":        "pid_cvat_manual.gif",
    "segment":     "pid_pipe_trace.gif",
    "pipe":        "pid_valpipe_fix.gif",
    "graph":       "pid_graph.gif",
    "val_graph":   "pid_valgraph.gif",
    "contours":    "pid_contours.gif",
    "ocr":         "pid_ocr.gif",
    "ocr_binding": "pid_ocr_binding.gif",
    "edit_graph":  "pid_edit_graph.gif",
    "fxml":        "pid_fxml_export.gif",
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
        font = QFont(self.font().family(), max(9, int(row_h * 0.40)))
        for btn in self._buttons:
            btn.setFixedHeight(row_h)
            btn.setFont(font)
        self._beads.set_radius(int(row_h * 0.30))


# =====================================================================
# Диалог экспорта FXML
# =====================================================================

class FxmlExportDialog(QDialog):
    """Единый диалог экспорта FXML: размер страницы + папка + имя файла.

    values() → (page_size, save_path). page_size: '1920x1080'/'A4'…/None (оригинал).
    """

    # (подпись, значение page_size) — как в прежнем _ask_page_size
    _SIZE_ITEMS = [
        ("1920×1080 (экран, стандартизация скинов)", "1920x1080"),
        ("Оригинал (пиксели изображения)", None),
        ("A4 landscape (297×210 мм)", "A4"),
        ("A3 landscape (420×297 мм)", "A3"),
        ("A2 landscape (594×420 мм)", "A2"),
        ("A1 landscape (841×594 мм)", "A1"),
        ("A0 landscape (1189×841 мм)", "A0"),
    ]

    def __init__(self, parent, default_dir: str, default_name: str):
        super().__init__(parent)
        self.setWindowTitle("Экспорт FXML")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Размер страницы:"))
        self._size_combo = QComboBox()
        self._size_combo.addItems([label for label, _ in self._SIZE_ITEMS])
        layout.addWidget(self._size_combo)

        layout.addWidget(QLabel("Имя файла:"))
        self._name_edit = QLineEdit(default_name)
        layout.addWidget(self._name_edit)

        layout.addWidget(QLabel("Папка:"))
        dir_row = QHBoxLayout()
        self._dir_edit = QLineEdit(default_dir or "")
        dir_row.addWidget(self._dir_edit)
        btn_browse = QPushButton("Обзор…")
        btn_browse.clicked.connect(self._browse)
        dir_row.addWidget(btn_browse)
        layout.addLayout(dir_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Экспорт")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self):
        start = self._dir_edit.text().strip()
        chosen = QFileDialog.getExistingDirectory(self, "Папка для сохранения", start)
        if chosen:
            self._dir_edit.setText(chosen)

    def values(self):
        """(page_size, save_path). save_path — абсолютный путь с расширением .fxml."""
        import os
        page_size = self._SIZE_ITEMS[self._size_combo.currentIndex()][1]
        name = (self._name_edit.text() or "diagram").strip() or "diagram"
        if not name.lower().endswith((".fxml", ".xml")):
            name += ".fxml"
        directory = self._dir_edit.text().strip()
        return page_size, os.path.join(directory, name)


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
        self._masks_completion_sent = False  # команда о масках уже отправлена
        self._active_tab = None  # Текущий открытый tab-виджет
        self._active_tab_key = ""  # Ключ: "cvat", "junction", "pipe", "graph"
        self._btn_back_injected = None  # Кнопка ← Назад внутри вкладки
        self._ocr_notified = False  # B6.4: OCR ready notification sent
        self._was_status_watching = False  # Paused status polling while tab is open
        # Отказы отправки, о которых сказал веер: ключ бусины → отпечаток строк
        # `/stages` этого этапа, известных НА МОМЕНТ отказа (None — не прочитались).
        # Отпечаток, а не флаг: `ProcessingStage` откатом не удаляются, поэтому
        # строка прошлого прогона сняла бы свежий отказ и вернула бы молчание.
        self._dispatch_refusals: Dict[str, Optional[frozenset]] = {}

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

        # СТРАХОВКА ОТ ТУПИКА (пункт 1.x11). Появляется только тогда, когда
        # ошибка есть, а нажать нечего: ни одна из 13 кнопок не доступна.
        # Не входит в `_action_buttons` — там 13 кнопок 1:1 с бусинами, и
        # четырнадцатая сломала бы привязку (`beads.set_anchor_widgets`).
        self.btn_error_retry = QPushButton("🔄 Повторить")
        self.btn_error_retry.setFixedHeight(28)
        self.btn_error_retry.setStyleSheet(_BTN_STYLE_RED)
        self.btn_error_retry.setVisible(False)
        self.btn_error_retry.clicked.connect(self._on_error_retry)
        top_row.addWidget(self.btn_error_retry)

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
        # Дальше каждая строка лога клиента несёт uid= — сшивается с серверной.
        bind_uid(uid)
        self._diagram_name = name
        self._junction_confirmed = False
        self._pipe_confirmed = False
        self._masks_completion_sent = False
        self._stop_ocr_poll()
        self._ocr_notified = False
        self._fxml_save_prompted = True
        self._awaiting_fxml_save = False
        self._prtx_armed = False
        self._fxml_target_path = None
        self._stage_errors = {}
        self._dispatch_refusals = {}
        self._last_status = DiagramStatus.UPLOADED

        self.title_label.setText(f"Диаграмма — {name}")

        # Закрыть вкладку если открыта
        self._force_close_tab()

        # Показать header
        self.header_panel.show()
        self.content_stack.setCurrentIndex(0)

        # Обновить бусины и кнопки
        self._refresh_status()

        # #2: прерванный ручной этап — сказать вслух, ничего не откатывая
        self._note_unconfirmed_stage()

        # Подписаться на обновления (один раз)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            for _sig, _slot in (
                (self.status_provider.status_updated, self._on_status_updated),
                (self.status_provider.stages_updated, self._on_stages_updated),
            ):
                try:
                    _sig.disconnect(_slot)
                except (RuntimeError, TypeError):
                    pass
        self.status_provider.status_updated.connect(self._on_status_updated)
        self.status_provider.stages_updated.connect(self._on_stages_updated)

    def cleanup(self):
        """Вызвать при уходе из workspace."""
        self._stop_ocr_poll()
        self._force_close_tab()
        if self._uid:
            self.status_provider.unwatch(self._uid)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            for _sig, _slot in (
                (self.status_provider.status_updated, self._on_status_updated),
                (self.status_provider.stages_updated, self._on_stages_updated),
            ):
                try:
                    _sig.disconnect(_slot)
                except (RuntimeError, TypeError):
                    pass

    # =================================================================
    # Refresh
    # =================================================================

    def _note_unconfirmed_stage(self):
        """Сказать оператору, что ручной этап остался неподтверждённым.

        Раньше здесь стоял авто-откат («самолечение», пункт 1.3 дороги): если
        предыдущий сеанс редактирования этапа был прерван (открыл, не сохранил,
        вышел не через «← Назад» / закрыл приложение), статус оставался *ING —
        и клиент молча звал `rollback_diagram(uid, target)` БЕЗ preserve-флагов.
        Для «Проверки схемы» это сносило `graph_validated`, оба контурных
        артефакта, все четыре OCR-овых, холст (файл `graph_canvas.json` уходит
        с диска) и FXML — девять артефактов из одиннадцати, замер §51.

        Покупал этот откат ровно один цвет бусины: набор доступных/пройденных
        кнопок у `*ING`-статуса и у стабильного совпадает побитово для всех
        четырёх ручных этапов (§51), потому что кнопку и так держит
        кликабельной поправка `_MANUAL_INPROGRESS` в `_update_buttons`. Бусину
        теперь красит такая же поправка в `_update_beads`, а данные оператора
        сносит только он сам — кнопкой пройденного этапа, с вопросом
        (`_on_button_click`).
        """
        if self._active_tab is not None:
            return
        mi = _MANUAL_INPROGRESS.get(self._last_status)
        if not mi:
            return
        key, _target = mi
        label = dict(self._BUTTON_DEFS).get(key, key)
        logger.info("Этап «%s» остался неподтверждённым (%s) — данные не тронуты",
                    label, self._last_status.value)
        self.status_message.emit(
            f"«{label}»: этап не завершён — можно продолжить с того же места",
            5000,
        )

    def _refresh_status(self):
        """Обновить бусины и кнопки из текущего статуса в БД."""
        if not self._uid:
            return
        try:
            diagram = self.api_client.get_diagram(self._uid)
            logger.info("Refresh: %s → %s", self._uid[:8], diagram.status.value)
            self._apply_status(diagram.status,
                               error_stage=getattr(diagram, 'error_stage', None),
                               error_message=getattr(diagram, 'error_message', None))
        except Exception as exc:
            logger.error("Refresh failed: %s", exc)

    def _apply_status(self, status: DiagramStatus, error_stage: str = None,
                      error_message: str = None):
        """Применить статус к бусинам и кнопкам."""
        _prev_status = self._last_status
        self._last_status = status

        # Страховку показывает только ветка тупика — новое состояние её снимает.
        self.btn_error_retry.setVisible(False)

        # ERROR: по-этапная изоляция — не морозим весь пайплайн, а
        # реконструируем прогресс из ProcessingStage и красим только упавший этап.
        if status == DiagramStatus.ERROR:
            self._apply_error_status(error_stage, error_message)
            return
        self._stage_errors = {}

        self._update_beads(status)
        self._update_buttons(status, error_stage=error_stage)
        self._update_gif(status)

        # Армируем сохранение, как только началась генерация FXML
        if status == DiagramStatus.GENERATING_FXML:
            self._awaiting_fxml_save = True

        # Автоконвертор .prtx взводим ШИРЕ, чем сохранение FXML: на сам
        # GENERATING_FXML опрос (2 с) почти никогда не попадает — генерация
        # FXML занимает 0.1 с (замер по 75 стадиям, max 1.3 с), и на авто-пути
        # после «Проверки схемы» клиент видит VALIDATED_GRAPH → COMPLETED.
        # Признак «конвейер дошёл до конца при нас» — увиденный ранее НЕ-готовый
        # статус; при открытии уже готовой схемы взвода нет и сборка не
        # запускается (иначе .prtx пересобирался бы на каждом открытии).
        if status != DiagramStatus.COMPLETED:
            self._prtx_armed = True

        # По завершении генерации — тихо сохранить в заранее выбранный путь
        # (без второго окна). Не завязано на переход статуса (на готовой схеме
        # перехода нет).
        if status == DiagramStatus.COMPLETED:
            _awaiting = getattr(self, "_awaiting_fxml_save", False)
            _armed = getattr(self, "_prtx_armed", False)
            # Путь экспорта снимаем ДО сохранения: _save_fxml_silently его гасит,
            # а .prtx должен лечь рядом с тем же файлом.
            _fxml_target = getattr(self, "_fxml_target_path", None)
            if _awaiting:
                self._awaiting_fxml_save = False
                self._save_fxml_silently()
            if _awaiting or _armed:
                self._prtx_armed = False
                self._start_prtx_conversion(_fxml_target)

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
                    # «Ручная правка» по факту OCR-артефакта БОЛЬШЕ НЕ ОТКРЫВАЕТСЯ:
                    # гейт §3.2 — сначала привязка, потом готовая раскладка
                    # (ui/services/layout_gate.py, применяется в _on_stages_updated).
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
            # «Ручная правка» по факту OCR-артефакта БОЛЬШЕ НЕ ОТКРЫВАЕТСЯ:
            # гейт §3.2 — сначала привязка, потом готовая раскладка
            # (ui/services/layout_gate.py, применяется в _on_stages_updated).

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
                    self._action_buttons["ocr"].setText("Распознавание текста")
                    if getattr(self, "_filled_keys", None):
                        self._filled_keys.pop("ocr", None)
                if "ocr_binding" in self._action_buttons:
                    self._action_buttons["ocr_binding"].setEnabled(True)
                    self._action_buttons["ocr_binding"].setStyleSheet(_BTN_STYLE_YELLOW)
                    self.beads.set_state(BEAD_OCR_BINDING, BeadState.AVAILABLE)
                # «Ручная правка» по факту OCR-артефакта БОЛЬШЕ НЕ ОТКРЫВАЕТСЯ:
                # гейт §3.2 — сначала привязка, потом готовая раскладка
                # (ui/services/layout_gate.py, применяется в _on_stages_updated).
            else:
                # OCR ещё бежит, а основной опрос статуса на паузе (built/гейт →
                # unwatch): тикаем заливку OCR-кнопки здесь, чтобы её % рос
                # (своя процентовка параллельной стадии, §9 #15).
                try:
                    self._on_stages_updated(
                        self._uid, self.api_client.get_stages(self._uid))
                except Exception:
                    pass
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

        # Ручной *ING-этап без открытой вкладки — бусина «доступен», а не «в
        # процессе»: в процессе ничего нет, оператор просто вышел, не завершив.
        # Та же поправка, что держит кнопку кликабельной в _update_buttons (#3);
        # раньше эту бусину гасил авто-откат, снесённый пунктом 1.3 дороги.
        # Пока вкладка открыта — синяя «в процессе» честна, поэтому и условие.
        if self._active_tab is None:
            _mi = _MANUAL_INPROGRESS.get(status)
            if _mi:
                _idx = _KEY_IDX.get(_mi[0])
                if _idx is not None and target.get(_idx) == BeadState.IN_PROGRESS:
                    target[_idx] = BeadState.AVAILABLE

        # Перекрыть pipe/junction если подтверждены по отдельности
        if status == DiagramStatus.VALIDATING_MASKS:
            if self._pipe_confirmed:
                target[BEAD_VAL_PIPE] = BeadState.COMPLETED
        if status == DiagramStatus.VALIDATING_JUNCTIONS:
            if self._junction_confirmed:
                target[BEAD_VAL_JUNCTION] = BeadState.COMPLETED

        # Отказ отправки: сервер сказал, что задачи в очереди НЕТ. Держим бусину
        # красной, пока не увидим свидетельство НОВЕЕ отказа — иначе оператор
        # смотрит на «в процессе» там, где не отправлено ничего (пункт 1-48).
        for key, seen in list(self._dispatch_refusals.items()):
            idx = _KEY_IDX.get(key)
            if idx is None or self._stage_ran_since_refusal(key, seen, target):
                self._dispatch_refusals.pop(key, None)
                continue
            target[idx] = BeadState.ERROR

        # Применить — не описанные = UNAVAILABLE
        for i in range(NUM_BEADS):
            self.beads.set_state(i, target.get(i, BeadState.UNAVAILABLE))

    def _stage_ran_since_refusal(self, key: str, seen, target: dict) -> bool:
        """Появилось ли свидетельство, что этап всё-таки отработал ПОСЛЕ отказа.

        Три источника, от самого надёжного к самому слабому:
        этап дошёл до конца по статусу · артефакт OCR на диске · строка
        `/stages`, которой на момент отказа НЕ БЫЛО.

        Последнее условие и есть смысл отпечатка: откат `ProcessingStage`
        не удаляет (`start_stage` кладёт новую попытку рядом), поэтому у
        оператора, вернувшегося с `ocr_completed` и переподтвердившего
        перекрёстки, строка `ocr` прошлого прогона уже лежит в `/stages` —
        свидетельством о СВЕЖЕЙ отправке она не является.
        """
        idx = _KEY_IDX.get(key)
        if idx is not None and target.get(idx) == BeadState.COMPLETED:
            return True
        if key == "ocr" and getattr(self, "_ocr_notified", False):
            # Единственный этап с отдельной проверкой по артефакту (B6.4):
            # результат на диске — доказательство сильнее любой стадии.
            return True
        if seen is None:
            return False        # стадии на момент отказа не прочитались — не гадаем
        for s in (getattr(self, "_last_stages", None) or []):
            if (_STAGE_TYPE_TO_KEY.get(s.get("stage_type")) == key
                    and s.get("id") not in seen):
                return True
        return False

    def _note_dispatch_refusals(self, result: dict) -> list:
        """Записать отказы отправки из ответа веера и вернуть их подписи.

        Клиент не перечисляет поля ответа и не разбирает человеческий текст:
        сервер называет отказавший этап словарём `ProcessingStage.stage_type`,
        а перевод «этап → бусина» делает та же карта `_STAGE_TYPE_TO_KEY`,
        которой клиент уже читает `/stages`. Новый этап веера доедет до своей
        бусины без правки этого метода; этап без бусины — не молча, а строкой
        в лог и подписью оператору под своим серверным именем.
        """
        labels = dict(self._BUTTON_DEFS)
        refused = []
        for stage_type in (result.get("dispatch_failed") or []):
            key = _STAGE_TYPE_TO_KEY.get(stage_type)
            if key is None:
                logger.warning("Отказ отправки «%s»: бусины у этого этапа нет",
                               stage_type)
                refused.append(stage_type)
                continue
            self._dispatch_refusals[key] = self._known_stage_ids(key)
            refused.append(labels.get(key, key))
        if refused:
            logger.warning("Веер не отправил: %s", ", ".join(refused))
        return refused

    def _known_stage_ids(self, key: str):
        """Строки `/stages` этого этапа, существующие ДО отказа. None — не прочитались.

        Про широту `except`: отпечаток нужен только чтобы НЕ снять отказ раньше
        времени, и `None` здесь — консервативный ответ («свидетельству стадий
        не верим»), а не потеря. Молчать при этом нельзя: без строки в логе
        разница между «стадий нет» и «стадии не прочитались» пропадает.
        """
        try:
            stages = self.api_client.get_stages(self._uid)
        except Exception as exc:  # noqa: BLE001 — причина в логе, решение консервативно
            logger.warning("Стадии не прочитаны при отказе отправки (%s: %s) — "
                           "отказ снимется только концом этапа",
                           type(exc).__name__, exc)
            return None
        return frozenset(
            s.get("id") for s in stages
            if _STAGE_TYPE_TO_KEY.get(s.get("stage_type")) == key
        )

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

        # #3: ручной *ING-этап — кнопка остаётся кликабельной (повторный вход),
        # хотя бид показывает «в процессе». Иначе после «открыл и вышел» не зайти.
        _mi = _MANUAL_INPROGRESS.get(status)
        if _mi:
            processing.discard(_mi[0])
            available.add(_mi[0])

        # БЕГУЩАЯ СТАДИЯ — «в процессе» тем же механизмом, что у всех
        # остальных этапов. `_buttons_for_status` считает состояние по
        # СТАТУСУ диаграммы, а у раскладки своего статуса нет: она
        # невидимая стадия между привязкой и «Ручной правкой», и её кнопка
        # оставалась доступной, пока раскладка ещё считалась. Здесь
        # бегущая стадия переводит свою кнопку в processing напрямую —
        # дальше её рисует и заливает процентами штатный путь
        # (`_STAGE_TYPE_TO_KEY` уже содержит "layout": "edit_graph").
        for _st in (getattr(self, "_last_stages", None) or []):
            if (_st.get("status") or "").lower() != "running":
                continue          # pending без старта кнопку не глушит
            # ВЫХОД ПО ПРЕДЕЛУ. Иначе повисшая задача глушила бы кнопку
            # навсегда: гейт-то пускает после WAIT_LIMIT_S, но нажать
            # было бы нечего. Отсчёт от created_at, если старта не было.
            if _stage_stuck(_st):
                continue
            _k = _STAGE_TYPE_TO_KEY.get(
                (_st.get("stage_type") or "").lower())
            # РУЧНОЙ ЭТАП СВОЕЙ СТРОКОЙ НЕ ГЛУШИТСЯ. Строку `frame_removal`
            # открывает сервер при входе во вкладку (`app/api/frame.py:105`,
            # канон §8.5), а закрыть её при выходе некому: «← Назад» на сервер
            # не ходит. Без этой ветки строка перекрывала поправку #3 выше, и
            # после «открыл → 💾 → ← Назад» кнопка гасла на WAIT_LIMIT_S = 600 с
            # — оператор не мог вернуться к своей же сохранённой очистке
            # (пункт 1.17 дороги, замер §75.11-75.14). Ручной этап показывает
            # «в процессе» по СТАТУСУ, ему строка стадии для этого не нужна;
            # раскладке и авто-стадиям — нужна, поэтому ветка узкая.
            if _mi and _k == _mi[0]:
                continue
            if _k and _k in self._action_buttons:
                processing.add(_k)
                available.discard(_k)
                completed.discard(_k)

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
            # Пишет СЕРВЕР, а не воркер (`app/api/cvat.py:374`), поэтому
            # значение шло мимо решёток, перебиравших `set_diagram_error`, —
            # и давало ноль кнопок из 13. Кнопка выбрана не на глаз: сервер
            # по этому значению возвращает диаграмму в `validating_bbox`
            # (`app/api/diagrams.py:453`), а `cvat_validation` — та же кнопка
            # в карте основного пути (`_STAGE_TYPE_TO_KEY`). Дверь кнопки —
            # `reopen_bbox_validation`, и она из `error` пускает.
            "fetching_annotations": "cvat",
            # Та же кнопка, что у сегментации: см. `_STAGE_TYPE_TO_KEY`.
            # Эта ветка работает, когда `/stages` недоступен, — без строки
            # оператор оставался с тринадцатью серыми кнопками.
            "direction_classification": "segment",
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


        # ГЕЙТ ПОСЛЕДНИМ. Статус и стадии приходят ДВУМЯ независимыми
        # опросами: статус меняется сразу после подтверждения привязки и
        # включает кнопку, а стадии подтянутся следующим тиком. В это окно
        # оператор успевал войти во вкладку до готовности раскладки и
        # видел холст БЕЗ неё — «то же, что в проверке». Повторный заход
        # уже показывал раскладку, и выглядело это как случайность.
        stages = getattr(self, "_last_stages", None)
        if stages is not None:
            self._apply_layout_gate(stages)

    def _restyle_button(self, key: str, status: DiagramStatus):
        """Вернуть ОДНОЙ кнопке базовый вид по статусу, не трогая остальные.

        Для снятия заливки с параллельной стадии, переставшей бежать, БЕЗ
        _update_buttons (тот перерисовывает ВСЕ кнопки → стирал бы % соседней
        ещё бегущей стадии — регрессия §8.10 / §9 #15).
        """
        btn = self._action_buttons.get(key)
        if btn is None:
            return
        available, completed, processing = _buttons_for_status(status)
        _mi = _MANUAL_INPROGRESS.get(status)
        if _mi:
            processing.discard(_mi[0])
            available.add(_mi[0])
        label = dict(self._BUTTON_DEFS).get(key, key)
        if key in processing:
            btn.setEnabled(False)
            btn.setStyleSheet(_BTN_STYLE_BLUE)
        elif key in available:
            btn.setEnabled(True)
            btn.setStyleSheet(_BTN_STYLE_YELLOW)
        elif key in completed:
            btn.setEnabled(True)
            btn.setStyleSheet(_BTN_STYLE_GREEN)
        else:
            btn.setEnabled(False)
            btn.setStyleSheet(_BTN_STYLE_GRAY)
        btn.setText(label)

    def _apply_error_status(self, error_stage: str = None,
                            error_message: str = None):
        """ERROR без «заморозки»: реконструировать прогресс из ProcessingStage.

        Завершённые этапы остаются зелёными, параллельные ветки — независимыми,
        красной становится только упавшая бусина; клик по ней — лог + перезапуск.
        Если этапы недоступны — фолбэк на старое поведение (по error_stage).

        Про широту `except` ниже: отказ сервера сюда НЕ доходит — `get_stages`
        ловит `APIError` сам и отдаёт пустой список (там же и пишет причину).
        Значит здесь остаются только настоящие поломки чтения, и перехват
        оставлен широким сознательно: без окна оператор не увидит вообще ничего,
        а с фолбэком увидит хотя бы упавший этап. Молчать при этом нельзя —
        иначе поломка выглядит как «стадий нет».
        """
        self._stage_errors = {}
        try:
            stages = self.api_client.get_stages(self._uid)
        except Exception as exc:  # noqa: BLE001 — окно оператора важнее причины
            logger.warning("Стадии не прочитаны (%s: %s) — фолбэк по error_stage",
                           type(exc).__name__, exc)
            stages = []

        if not stages:
            # Фолбэк: хотя бы retry упавшего этапа по глобальному error_stage
            self._update_beads(DiagramStatus.ERROR)
            self._update_buttons(DiagramStatus.ERROR, error_stage=error_stage)
            self._update_gif(DiagramStatus.ERROR)
            self._offer_error_retry(error_stage, error_message)
            return

        # Последняя попытка каждого stage_type
        latest = {}
        for s in stages:
            st = s.get("stage_type")
            if st:
                latest[st] = s

        completed_keys, running_keys = set(), set()
        failed = {}
        best_status = DiagramStatus.UPLOADED
        for st, s in latest.items():
            key = _STAGE_TYPE_TO_KEY.get(st)
            sstatus = (s.get("status") or "").lower()
            if sstatus == "completed":
                if key:
                    completed_keys.add(key)
                done = _STAGE_DONE_STATUS.get(st)
                if done and _STATUS_IDX.get(done, -1) > _STATUS_IDX.get(best_status, -1):
                    best_status = done
            elif sstatus == "failed":
                if key:
                    failed[key] = s  # полная строка ProcessingStage (для окна отчёта)
            elif sstatus == "running":
                if key:
                    running_keys.add(key)

        # База: рендер по достигнутому прогрессу (не по ERROR → лишнего не гасим)
        self._update_beads(best_status)
        self._update_buttons(best_status)
        self._update_gif(best_status)

        _KEY_LABELS = {k: v for k, v in self._BUTTON_DEFS}

        # Overlay фактических статусов этапов из ProcessingStage
        for key in completed_keys:
            idx = _KEY_IDX.get(key)
            if idx is not None:
                self.beads.set_state(idx, BeadState.COMPLETED)
            btn = self._action_buttons.get(key)
            if btn is not None:
                btn.setEnabled(True)
                btn.setStyleSheet(_BTN_STYLE_GREEN)
                btn.setText(_KEY_LABELS.get(key, key))
        for key in running_keys:
            if key in failed:
                continue
            idx = _KEY_IDX.get(key)
            if idx is not None:
                self.beads.set_state(idx, BeadState.IN_PROGRESS)
            btn = self._action_buttons.get(key)
            if btn is not None:
                btn.setEnabled(False)
                btn.setStyleSheet(_BTN_STYLE_BLUE)

        # OCR по артефакту может опережать ProcessingStage
        if getattr(self, "_ocr_notified", False) and "ocr" not in failed:
            self.beads.set_state(BEAD_OCR, BeadState.COMPLETED)
            if "ocr" in self._action_buttons:
                self._action_buttons["ocr"].setEnabled(True)
                self._action_buttons["ocr"].setStyleSheet(_BTN_STYLE_GREEN)

        # Overlay упавших этапов — красная бусина + красная retry-кнопка
        self._stage_errors = failed
        for key, s in failed.items():
            msg = s.get("error_message") or ""
            idx = _KEY_IDX.get(key)
            if idx is not None:
                self.beads.set_state(idx, BeadState.ERROR)
            btn = self._action_buttons.get(key)
            if btn is not None:
                btn.setEnabled(True)
                btn.setText(f"🔄 {_KEY_LABELS.get(key, key)}")
                btn.setStyleSheet(_BTN_STYLE_RED)
                short = msg.strip().splitlines()[0] if msg else ""
                btn.setToolTip(
                    (f"Ошибка: {short}\n" if short else "")
                    + "Нажмите — показать лог и перезапустить"
                )

    def _offer_error_retry(self, error_stage: str = None,
                           error_message: str = None):
        """Ноль кнопок из 13 — показать ошибку и дать выход, а не тупик.

        Ветка узкая по УСЛОВИЮ, а не по списку значений: она включается ровно
        тогда, когда оператору нечего нажать. Так закрывается весь класс, а не
        одно значение: `error_stage` пишут и воркер, и API (`app/api/cvat.py:374`
        — `fetching_annotations`), карта клиента о писателях со стороны API
        не знала, и любое новое значение снова обнулило бы столбец кнопок.

        Куда откатывать — решает СЕРВЕР: у `POST /api/diagrams/{uid}/retry`
        своя карта `error_stage → предыдущий статус` (`app/api/diagrams.py:448`).
        Клиент здесь не гадает, а показывает, что именно сломалось.
        """
        if any(btn.isEnabled() for btn in self._action_buttons.values()):
            return

        stage = error_stage or "неизвестен"
        text = error_message or "Обработка остановлена с ошибкой"
        self.btn_error_retry.setToolTip(
            f"Этап: {stage}\n{text}\n\n"
            "Нажмите — сервер вернёт диаграмму на предыдущий шаг."
        )
        self.btn_error_retry.setVisible(True)
        logger.warning(
            "Этап «%s» клиенту неизвестен, доступных кнопок нет — "
            "предложен откат сервером", stage,
        )
        self.status_message.emit(f"Ошибка на этапе «{stage}»: {text}", 10000)

    def _on_error_retry(self):
        """Выход из тупика: откат на предыдущий шаг решением сервера."""
        reply = QMessageBox.question(
            self, "Повторить",
            "Обработка остановлена с ошибкой, а этап клиенту неизвестен.\n"
            "Сервер вернёт диаграмму на предыдущий шаг — часть обработки "
            "придётся повторить.\n\nПродолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            result = self.api_client.retry_operation(self._uid)
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка", f"Не удалось повторить:\n{exc.message}",
            )
            return

        new_status = (result or {}).get("status", "")
        # Сервер откатил статус — значит строки стадий, лежащие у клиента
        # в памяти, описывают состояние, которого больше нет. Это инвариант,
        # а не догадка: снимок принесён опросом, пока стадия ещё шла.
        # Без чистки та же БЕГУЩАЯ строка, что и создала тупик, продолжает
        # держать свою кнопку в `processing` уже в ЦЕЛЕВОМ статусе — до
        # `WAIT_LIMIT_S` = 600 с (`_update_buttons`). Замер §107.2: у двух
        # значений из четырёх так глушится ровно дверь целевого статуса
        # (`direction_classification` → `segment`, `contour_extraction` →
        # `contours`), и оператору остаются только откаты с УДАЛЕНИЕМ
        # артефактов. Чистка безопасна и ничего не прячет: на сервере строка
        # упавшей стадии уже `failed` (замер §107.9), поэтому следующий
        # удачный опрос вернёт её как есть; а пока `/stages` недоступен —
        # это предусловие самого тупика — глушить просто нечем.
        self._last_stages = []
        logger.info("Откат из ошибки: %s → %s", self._uid[:8], new_status)
        self.status_message.emit(
            f"↩ Диаграмма возвращена на шаг «{new_status}»", 5000,
        )
        self._refresh_status()

    def _show_stage_error_dialog(self, key: str, original_handler):
        """Окно отчёта об ошибке этапа (traceback/phase/step/code + копировать/сохранить)."""
        stage = getattr(self, "_stage_errors", {}).get(key) or {}
        _KEY_LABELS = {k: v for k, v in self._BUTTON_DEFS}
        from ui.widgets.error_report_dialog import ErrorReportDialog
        dlg = ErrorReportDialog(
            stage,
            parent=self,
            phase_label=_KEY_LABELS.get(key, key),
            diagram_name=self._diagram_name,
        )
        if dlg.exec_retry():
            original_handler()

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
            self._btn_back_injected.setStyleSheet(
                "QPushButton { background: #555; color: white; "
                "border-radius: 3px; padding: 2px 8px; min-width: 0px; }"
                "QPushButton:hover { background: #777; }"
            )
            self._btn_back_injected.clicked.connect(self._close_active_tab)

            # Найти горизонтальный тулбар вкладки: это либо прямой QHBoxLayout,
            # либо QHBoxLayout внутри виджета-контейнера (тулбар обёрнут в контейнер
            # ради авто-подгонки ширины) — иначе Назад/⚙ встали бы отдельными ярусами.
            first_item = tab_layout.itemAt(0)
            target_layout = None
            if first_item is not None:
                lay = first_item.layout()
                if isinstance(lay, QHBoxLayout):
                    target_layout = lay
                else:
                    w = first_item.widget()
                    if w is not None and isinstance(w.layout(), QHBoxLayout):
                        target_layout = w.layout()
            if target_layout:
                target_layout.insertWidget(0, self._btn_back_injected)
            else:
                # Fallback: вставить сверху
                tab_layout.insertWidget(0, self._btn_back_injected)

            # Кнопка ⚙ — настройки оформления (затемнение фона, цвета), если
            # вкладка их поддерживает. Рядом с «← Назад».
            if hasattr(tab_widget, "toggle_appearance_panel"):
                self._btn_appearance_injected = QPushButton("⚙")
                self._btn_appearance_injected.setToolTip(
                    "Оформление вкладки (затемнение фона, цвета)"
                )
                self._btn_appearance_injected.setStyleSheet(
                    "QPushButton { background: #555; color: white; "
                    "border-radius: 3px; padding: 2px 6px; min-width: 0px; }"
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

    # Здесь была таблица `_VALIDATING_ROLLBACK` и вызовы `_rollback_if_validating()`
    # из обоих путей закрытия: незакрытая вкладка откатывала статус `rollback_diagram`,
    # то есть сносила артефакты всех последующих этапов. Снято пунктом 1.3 дороги.
    # Ключи таблицы (`"val_graph"`) и вкладок (`"graph_val"`, `:1931`) разошлись, и для
    # графовой вкладки ветка не срабатывала никогда — а для «Очистки рамки»
    # срабатывала и уносила сохранённую оператором очистку вместе с `image_raw.png`.
    # Чинить надо было не ключ: совпадение включило бы штатный откат со всеми
    # последствиями. Замер §51: откат не менял НИ ОДНОЙ кнопки — только цвет бусины,
    # и её теперь красит поправка `_MANUAL_INPROGRESS` в `_update_beads`.

    #: Имена методов сохранения, которыми вкладка отвечает на «Да». Порядок
    #: значим: у вкладки привязки OCR `_save_graph` — псевдоним `_save_binding`.
    _TAB_SAVE_METHODS = ("_save_graph", "_save_masks", "_save_mask", "_save_image")

    def confirm_discard_active_tab(self, action: str = "закрытием") -> bool:
        """Единственный вопрос о несохранённом. `False` — вкладку не трогать.

        Дверь одна на оба пути ухода: «← Назад» из вкладки и ✕ окна
        (`MainWindow.closeEvent`, пункт 1.18). Дверей было две, и они разошлись
        в четырёх клетках (замер §81): текст вопроса; «Да» у вкладки без метода
        сохранения («← Назад» молча оставлял вкладку открытой — тупик, окно
        закрывалось с записью в лог); след в логе на «Нет»; объяснение при
        отказе сохранения. Своё у путей осталось одно — слово о действии.

        Вкладка без `has_unsaved_changes()` вопроса не поднимает, и это была
        буква пункта 1.21: контракт — часть договора вкладки с воркспейсом,
        а «Очистка рамки» единственная его не имела и уходила молча на обоих
        путях. Вкладка, которая сохраняться не умеет, закрытию не мешает:
        тупик «не закрывается и не объясняет» дороже несохранённой вкладки,
        о которой оператора спросили (решение пункта 1.18).
        """
        tab = self._active_tab
        if tab is None or not hasattr(tab, 'has_unsaved_changes'):
            return True
        if not tab.has_unsaved_changes():
            return True

        reply = QMessageBox.question(
            self, "Несохранённые изменения",
            f"Есть несохранённые изменения. Сохранить перед {action}?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return False
        if reply == QMessageBox.StandardButton.No:
            logger.warning("Уход из вкладки %s без сохранения — выбор оператора",
                           type(tab).__name__)
            return True

        for name in self._TAB_SAVE_METHODS:
            save = getattr(tab, name, None)
            if save is None:
                continue
            if save():
                return True
            # Об ошибке говорит сама вкладка (диалог или своя строка статуса);
            # здесь — почему на экране ничего не изменилось.
            logger.warning("Сохранение %s.%s не удалось — закрытие отменено",
                           type(tab).__name__, name)
            self.status_message.emit(
                "Не удалось сохранить — закрытие отменено", 5000)
            return False

        logger.warning("Вкладка %s не умеет сохраняться — уход без сохранения",
                       type(tab).__name__)
        return True

    def _close_active_tab(self):
        """← Назад (из вкладки) → закрыть, вернуться к header."""
        if not self._active_tab:
            return

        tab_key = self._active_tab_key
        if not self.confirm_discard_active_tab():
            # Вкладка остаётся на экране — вместе со своим автосохранением.
            # Раньше `_autosave.stop()` стоял ДО вопроса, и отказ закрывать
            # оставлял открытую вкладку с мёртвым таймером (замер §81).
            return

        # Проверить _saved перед удалением (двойная страховка для масок)
        self._detect_saved_mask(self._active_tab)

        self._close_tab_and_restore_header()

        # Проверить завершение масок — только для вкладки труб: это двойная
        # страховка на случай, когда сигнал `confirmed` до воркспейса не дошёл
        # (`_detect_saved_mask` выше). Раньше вызов стоял безусловно, и закрытие
        # ЛЮБОЙ вкладки при поднятом `_pipe_confirmed` отправляло на сервер
        # команду о завершении валидации масок — в том числе когда оператор
        # только что ОТКАЗАЛСЯ сохранять изменения (пункт 1.20 дороги).
        if tab_key == "pipe":
            self._check_masks_completion()

    def _force_close_tab(self):
        """Закрыть вкладку без вопросов (при смене диаграммы / cleanup).

        Молчит законно: жеста к этому пути при открытой вкладке нет — header
        с кнопкой «← Назад в список» спрятан, а список диаграмм лежит за ним
        (замер §81, заперт тестом `test_header_back_is_unreachable_...`).
        Появится жест — молчание станет дефектом, и тест об этом скажет.
        """
        if self._active_tab:
            self._remove_tab_widget()
            self.header_panel.setUpdatesEnabled(True)
            self._was_status_watching = False

    def _remove_tab_widget(self):
        """Убрать виджет вкладки из layout.

        Автосохранение живёт ровно столько, сколько вкладка, поэтому гасится
        здесь — в единственной точке, где вкладка умирает. Раньше `stop()`
        стоял в двух путях закрытия из пятнадцати, и после «Подтвердить»
        таймер оставался крутиться на снесённом виджете (замер §81: в
        `_close_tab_and_restore_header` ведут 12 путей, `stop()` был у одного).
        """
        if self._active_tab:
            self._autosave.stop()
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
        """Если pipe маска подтверждена → complete_mask_validation → скелетизация + junction detection.

        Команда отправляется ОДИН раз на одно подтверждение. `_pipe_confirmed`
        для этого не годится: он живёт до конца сеанса с диаграммой (его читают
        бусина, гифка и кнопка, пока сервер не доехал до `validated_masks`),
        поэтому раньше каждый следующий выход из вкладки слал команду заново.
        Пока статус ещё `validating_masks`/`skeletonized`/`validated_masks`,
        сервер не считает это «ушли вперёд» (`app/api/validation.py:481-503`)
        и диспатчит скелетизацию повторно. Право доложить возвращает только
        новое подтверждение — `_on_pipe_confirmed`.
        """
        if not self._pipe_confirmed or self._masks_completion_sent:
            return
        self._masks_completion_sent = True

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
            # ⚠ Здесь стояло «Don't show error dialog — chain may have already
            # moved past this point», и до ноги 1.16 это было правдой: отказ
            # отправки отвечал 200 с `task_id: null`, то есть исключение могло
            # означать только «цепочка ушла вперёд». Теперь «ушли вперёд» —
            # по-прежнему 200, а 503 означает ровно обратное: задача НЕ
            # поставлена, состояние на сервере возвращено, повторять придётся
            # оператору (`docs/STATUS_MACHINE.md §5`). Молчать об этом нельзя —
            # три соседних подтверждения (перекрёстки, простой граф, граф)
            # показывают на своих отказах ровно такое окно.
            logger.error("complete_mask_validation failed: %s", exc)
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось завершить валидацию масок:\n{exc}",
            )
            self.status_provider.watch(self._uid)
            self._refresh_status()

    # =================================================================
    # Кнопки действий — фоновые процессы
    # =================================================================

    @Slot()
    def _on_button_click(self, key: str, original_handler):
        """Обработчик клика по кнопке — если этап уже пройден, предложить откат."""
        # Упавший этап — показать лог и предложить перезапуск только его.
        if key in getattr(self, "_stage_errors", {}):
            self._show_stage_error_dialog(key, original_handler)
            return

        # CVAT «Проверка элементов»: первый вход и жёсткий возврат к bbox с
        # позднего этапа целиком в _open_cvat (§9 #4). Общий rollback-диалог
        # (он не ревокает бегущую авто-стадию) для cvat не применяем.
        if key == "cvat":
            original_handler()
            return

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
            # «Ручная правка» тоже зависит от OCR — сбросить на время повторного OCR
            self.beads.set_state(BEAD_EDIT_GRAPH, BeadState.UNAVAILABLE)
            if "edit_graph" in self._action_buttons:
                self._action_buttons["edit_graph"].setEnabled(False)
                self._action_buttons["edit_graph"].setStyleSheet(_BTN_STYLE_GRAY)
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
        import os
        # Единый диалог: размер + папка + имя. По завершении генерации файл
        # сохранится автоматически в выбранный путь (без второго окна).
        base = (self._diagram_name or "diagram").strip()
        default_name = (os.path.splitext(base)[0] or "diagram") + ".fxml"
        default_dir = getattr(self, "_last_fxml_dir", None) or os.path.expanduser("~")

        dialog = FxmlExportDialog(self, default_dir, default_name)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        page_size, save_path = dialog.values()
        if not save_path:
            return
        self._fxml_target_path = save_path
        self._last_fxml_dir = os.path.dirname(save_path)

        # Разрыв моста — из настроек редактора этой диаграммы (если задан пользователем)
        bridge_gap = None
        try:
            from ui.services.ui_settings import UISettings
            _bg = UISettings.instance().get_appearance(self._uid, "bridge_gap_factor", None)
            if _bg is not None:
                bridge_gap = float(_bg)
        except Exception:
            bridge_gap = None

        # Запускаем генерацию (перегенерация если уже COMPLETED)
        try:
            self._awaiting_fxml_save = True
            self.api_client.generate_fxml(self._uid, page_size=page_size, bridge_gap=bridge_gap)
            self.status_provider.watch(self._uid)
            size_label = page_size or "оригинал"
            self.status_message.emit(f"📄 Генерация FXML ({size_label}) запущена", 3000)
            self._refresh_status()
        except APIError as exc:
            self._awaiting_fxml_save = False
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось запустить генерацию FXML:\n{exc.message}",
            )

    def _save_fxml_silently(self):
        """Тихо сохранить готовый FXML в заранее выбранный путь (без диалога).

        Путь берётся из _fxml_target_path (задан в _start_fxml). Если он не задан
        (напр. генерацию запустили не через диалог) — окно не открываем, показываем
        подсказку нажать 📄 FXML.
        """
        target = getattr(self, "_fxml_target_path", None)
        if not target:
            # Генерация без заранее выбранного пути (напр. авто-пайплайн):
            # окно сами НЕ открываем — просто подсказываем нажать 📄 FXML.
            self.status_message.emit(
                "✅ FXML готов — нажмите 📄 FXML, чтобы сохранить.", 6000,
            )
            return
        try:
            from pathlib import Path
            self.api_client.download_artifact(self._uid, "fxml", Path(target))
            self.status_message.emit(f"📄 FXML сохранён: {target}", 6000)
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось сохранить FXML:\n{exc.message}",
            )
        finally:
            self._fxml_target_path = None

    def _start_prtx_conversion(self, export_path=None):
        """Автоконвертор: собрать .prtx из того же валидированного графа.

        Считает СЕРВЕР (контейнер `prtx`), клиент отдаёт только ключ лицензии
        САПФИР из профиля оператора — подробности в
        ui/services/prtx_converter.py. Результат уезжает в storage рядом с
        diagram.fxml и, если оператор выбирал путь экспорта, ложится рядом с
        сохранённым .fxml.
        """
        from ui.services.prtx_converter import PrtxWorker, license_key_path

        if not license_key_path().is_file():
            self.status_message.emit(
                "⚠ Ключ лицензии САПФИР не найден — расчётная схема не собрана", 6000)
            return

        # Повторная генерация FXML поверх бегущей сборки затёрла бы ссылку на
        # живой QThread — он остался бы без владельца.
        running = getattr(self, "_prtx_thread", None)
        if running is not None and running.isRunning():
            logger.info("PRTX: сборка уже идёт, повтор пропущен")
            return

        self.status_message.emit("⏳ Сборка расчётной схемы .prtx…", 4000)

        self._prtx_thread = QThread()
        self._prtx_worker = PrtxWorker(self.api_client, self._uid, export_path)
        self._prtx_worker.moveToThread(self._prtx_thread)
        self._prtx_thread.started.connect(self._prtx_worker.run)
        self._prtx_worker.finished.connect(self._on_prtx_done)
        self._prtx_worker.error.connect(self._on_prtx_error)
        # Поток гасит сам работник — рабочая область живёт дольше конвертации,
        # но связи со слотами Qt рвёт вместе с получателем (образец — pipe_tab).
        self._prtx_worker.finished.connect(self._prtx_thread.quit)
        self._prtx_worker.error.connect(self._prtx_thread.quit)
        self._prtx_thread.start()

    @Slot(str)
    def _on_prtx_done(self, where: str):
        self.status_message.emit(f"📐 Расчётная схема .prtx собрана: {where}", 8000)

    @Slot(str)
    def _on_prtx_error(self, message: str):
        # Не окно: .prtx — производная от готового FXML, срыв сборки не должен
        # перебивать оператору результат основного конвейера.
        logger.error("PRTX conversion failed: %s", message)
        self.status_message.emit(f"⚠ Расчётная схема .prtx не собрана: {message}", 10000)

    def _download_fxml(self):
        """Скачать сгенерированный FXML на компьютер пользователя (ручной фолбэк)."""
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

            status = diagram.status
            if status == DiagramStatus.DETECTED:
                # Первый вход: detected → validating_bbox.
                result = self.api_client.open_cvat_validation(self._uid)
                cvat_url = result.get("cvat_url")
            elif status == DiagramStatus.VALIDATING_BBOX:
                # Уже в проверке — просто открыть ту же job.
                cvat_url = self.api_client.get_cvat_url(self._uid)
            else:
                # §9 #4: возврат к проверке элементов с более позднего этапа.
                # Жёсткий стоп + переоткрытие ТОЙ ЖЕ CVAT-job (ручная разметка цела).
                if not self._confirm_reopen_bbox():
                    return
                result = self.api_client.reopen_bbox_validation(self._uid)
                cvat_url = result.get("cvat_url")
                deleted = result.get("deleted_artifacts", 0)
                self.status_message.emit(
                    f"↩ Возврат к проверке элементов: сброшено {deleted} артефактов",
                    4000,
                )
                self._refresh_status()

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

    def _confirm_reopen_bbox(self) -> bool:
        """Подтверждение жёсткого возврата к проверке элементов (§9 #4)."""
        reply = QMessageBox.question(
            self, "Проверка элементов",
            "Диаграмма уже прошла проверку элементов.\n"
            "Вернуться к ней? Текущая обработка будет остановлена, "
            "а последующие артефакты — удалены.\n\n"
            "Ручная разметка в CVAT сохранится.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

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
            # Раскладка не удалась или не успела: пускаем, но говорим об этом
            # прямо. Молча открыть холст без раскладки нельзя — оператор
            # увидит ровно то, на что жаловался («ничего не изменилось»).
            # Состояние кнопки ведёт ШТАТНЫЙ механизм: бегущая стадия ->
            # processing (см. _update_buttons), готовая -> available. Своего
            # замка здесь больше нет: он делал синхронный HTTP прямо в
            # обработчике клика, на GUI-потоке, и окно замирало до ответа.
            _gate = getattr(self, "_layout_gate", None)
            if _gate is not None and _gate.warn:
                QMessageBox.information(self, "Раскладка схемы", _gate.warn)

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
        """CVAT сохранил аннотации → скачать аннотации → авто-старт сегментации.

        Сегментация запускается автоматически (как junction → построение графа),
        без отдельной кнопки.
        """
        logger.info("CVAT confirmed, fetching annotations")
        fetched = False
        try:
            result = self.api_client.fetch_cvat_annotations(self._uid)
            count = result.get("annotation_count", 0)
            fetched = True
            self.status_message.emit(
                f"✅ Получено {count} валидированных аннотаций", 5000,
            )
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось получить аннотации:\n{exc.message}",
            )

        self._close_tab_and_restore_header()

        # Авто-запуск сегментации сразу после CVAT — не по кнопке.
        if fetched:
            self._start_segmentation()

    def _has_saved_canvas(self) -> bool:
        """Есть ли у диаграммы сохранённый холст «Ручной правки».

        Проверяем по артефакту graph_canvas: GET download → 404 значит нет.
        """
        import tempfile
        from pathlib import Path

        try:
            with tempfile.TemporaryDirectory(prefix="pid_canvas_probe_") as td:
                self.api_client.download_artifact(
                    self._uid, "graph_canvas", Path(td) / "canvas.json")
            return True
        except Exception:
            return False

    @Slot()
    def _on_junction_confirmed(self):
        """Junction маски подтверждены → complete_junction_validation → graph build."""
        logger.info("Junction confirmed via signal")

        # Пересборка графа делает новый graph_validated → холст устареет по
        # source_sha и будет пересобран с нуля (base_graph_tab._canvas_is_stale),
        # т.е. ручная раскладка WYSIWYG пропадёт. Поведение конвейера НЕ меняем —
        # только предупреждаем оператора (решение заказчика 2026-07-28).
        if self._has_saved_canvas():
            answer = QMessageBox.question(
                self, "Пересборка графа",
                "Пересборка графа уничтожит ручную раскладку в «Ручной правке»: "
                "холст будет собран заново из нового graph_validated.\n\n"
                "Продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.status_message.emit("Подтверждение перекрёстков отменено", 5000)
                return

        self._junction_confirmed = True

        self._close_tab_and_restore_header()

        try:
            result = self.api_client.complete_junction_validation(self._uid)
            task_id = result.get("task_id")
            msg = "✅ Валидация перекрёстков завершена"
            if task_id:
                msg += " → построение графа запущено"
                self.status_provider.watch(self._uid)
            # Веер отправляет ДВЕ задачи, и отказ второй не откатывает первую
            # (граница §5): про такой отказ сервер говорит полем, а не молчит
            # `null`-ом. Без чтения этого поля оператор узнавал бы о неотправленном
            # OCR только от страховки в «Проверке схемы» (пункт 1-48).
            refused = self._note_dispatch_refusals(result)
            if refused:
                msg += f" · НЕ запущено: {', '.join(refused)}"
            self.status_message.emit(msg, 10000 if refused else 5000)
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
        # Новое подтверждение — новое право доложить серверу (после отката
        # этапа оператор подтверждает повторно, и команда должна уйти снова).
        self._masks_completion_sent = False

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

        # Слежение ОБЯЗАТЕЛЬНО: сразу за привязкой идёт «Ручная правка», а её
        # гейт живёт стадией `layout` — без опроса /stages он не считается
        # вовсе. Раньше этот обработчик только закрывал вкладку, схема из
        # наблюдения выпадала, и кнопка оставалась активной и без процентов,
        # хотя раскладка ещё считалась (~15 с против 7 с на весь маршрут).
        self.status_provider.watch(self._uid)
        self._refresh_status()

    # =================================================================
    # Status Provider callback
    # =================================================================

    @Slot(str, object)
    def _on_stages_updated(self, uid: str, stages):
        """Заливка бегущих кнопок-стадий (per-diagram, параллельно-безопасно).

        КАЖДАЯ бегущая авто-стадия льёт СВОЮ кнопку своим %: graph и ocr бегут
        одновременно → у каждой своя заливка (§9 #15). Снятие заливки — по одной
        кнопке через _restyle_button (НЕ _update_buttons: тот трёт ВСЕ кнопки →
        стирал бы % соседней ещё бегущей стадии, регрессия §8.10).
        """
        if uid != self._uid:
            return
        self._last_stages = stages
        from ui.services.progress_model import compute_progress
        # Бюджеты — p50 реальных длительностей с боевого железа (кэш на сессию);
        # {} при недоступности → progress_model берёт свой статический сид.
        ps = compute_progress(stages, budgets=self.api_client.get_stage_durations())

        new_filled: dict = {}
        if ps.state == "running":
            for _st, _pct in (ps.stage_percents or {}).items():
                _key = _STAGE_TYPE_TO_KEY.get(_st)
                if _key and _key in self._action_buttons:
                    new_filled[_key] = _pct

        for _key, _pct in new_filled.items():
            btn = self._action_buttons[_key]
            label = dict(self._BUTTON_DEFS).get(_key, _key)
            btn.setStyleSheet(_btn_fill_style(_pct / 100.0))
            btn.setText(f"{label} · {_pct}%")
        # Снять заливку с кнопок, переставших бежать (точечно, не _update_buttons).
        for _key in getattr(self, "_filled_keys", {}):
            if _key not in new_filled:
                self._restyle_button(_key, self._last_status)
        # Набор бегущих стадий изменился -> перерисовать кнопки штатным
        # путём: он и переводит кнопку в processing (см. _update_buttons).
        # Заливку возвращаем сразу после, иначе _update_buttons сотрёт %
        # у соседних ещё бегущих кнопок (регрессия §8.10).
        if set(new_filled) != set(getattr(self, "_filled_keys", {})):
            self._update_buttons(self._last_status)
            for _key, _pct in new_filled.items():
                btn = self._action_buttons[_key]
                label = dict(self._BUTTON_DEFS).get(_key, _key)
                btn.setStyleSheet(_btn_fill_style(_pct / 100.0))
                btn.setText(f"{label} · {_pct}%")
        self._filled_keys = new_filled
        self._apply_layout_gate(stages)

    def _apply_layout_gate(self, stages):
        """Гейт «Ручной правки»: пускать только с готовой раскладкой (§3.2).

        Состояние берётся из стадии `layout`, а не из наличия холста:
        отсутствие файла неразличимо между «считает», «упала» и «не
        стартовала». Пока гейт ждёт, вкладка закрыта — значит и клиентский
        автосейв холста не активен, отдельного выключателя не нужно.
        """
        from ui.services.layout_gate import gate_state

        btn = self._action_buttons.get("edit_graph")
        if btn is None:
            return
        gate = gate_state(stages, getattr(self._last_status, "value", None))
        self._layout_gate = gate

        if gate.allow:
            # РАЗБЛОКИРОВАТЬ. Гейт только выключал кнопку и полагался на то,
            # что её включит `_update_buttons` — а тот зовётся по смене
            # СТАТУСА, и статус после привязки так и остаётся `ocr_bound`.
            # Раскладка заканчивалась, а кнопка оставалась серой навсегда.
            if getattr(self, "_gate_blocked", False):
                self._gate_blocked = False
                btn.setEnabled(True)
                btn.setToolTip("")
                self._restyle_button("edit_graph", self._last_status)
            return          # дальше обычные правила кнопки в силе
        self._gate_blocked = True
        btn.setToolTip(gate.reason)
        if gate.waiting:
            # Вид кнопки (синяя, некликабельная, с процентами) ставит
            # штатный путь `_update_buttons` -> processing. Здесь только
            # бусина и подсказка, чтобы не спорить с ним за стиль.
            self.beads.set_state(BEAD_EDIT_GRAPH, BeadState.IN_PROGRESS)
        else:
            btn.setEnabled(False)
            btn.setStyleSheet(_BTN_STYLE_GRAY)

    @Slot(str, object)
    def _on_status_updated(self, uid: str, status_info):
        if uid == self._uid:
            logger.info("Poll update: %s", status_info.status.value)
            was_notified = self._ocr_notified
            self._apply_status(status_info.status,
                               error_stage=getattr(status_info, 'error_stage', None),
                               error_message=getattr(status_info, 'error_message', None))

            # B6.4: Показать уведомление при первом обнаружении OCR артефакта
            if self._ocr_notified and not was_notified:
                self.status_message.emit(
                    "✅ OCR завершён! Можно переходить к привязке.", 5000,
                )

            # По завершении генерации FXML сохраняется автоматически внутри
            # _apply_status → _save_fxml_silently (в выбранный путь, без окна).

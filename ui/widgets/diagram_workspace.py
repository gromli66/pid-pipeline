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
    QMenu, QDialog, QCheckBox, QLineEdit, QDialogButtonBox,
)
from PySide6.QtCore import Qt, Signal, Slot, QTimer, QThread
from PySide6.QtGui import QAction, QFont

from ui.services.api_client import APIClient, APIError, DiagramStatus
from ui.services.client_logging import bind_uid
from ui.services.status_provider import StatusProvider
from ui.services.thread_lifetime import hand_over
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


def _status_label(status_value: str) -> str:
    """Русская подпись статуса для диалога — без значка и без ключа.

    Словарь подписей один на клиента (`ui/widgets/diagram_list.py`); пятый
    самодельный маппинг здесь не заводится. Значок («✓», «⏳») — метка строки
    списка, а не часть названия, и в тексте вопроса лишний. Термины `OCR` и
    `Bbox` внутри подписи легальны: это слова, а не технические ключи.
    """
    from ui.widgets.diagram_list import STATUS_LABELS

    try:
        label = STATUS_LABELS.get(DiagramStatus(status_value), "")
    except ValueError:
        label = ""
    if not label:
        return status_value
    while label and not label[0].isalnum():
        label = label[1:]
    return label.strip() or status_value


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
     # РАСПОЗНАВАНИЕ СЧИТАЕТСЯ ПАРАЛЛЕЛЬНО, И СВОЕГО СТАТУСА У НЕГО НЕТ.
     # Здесь стояли ВОСЕМЬ статусов подряд (сборка графа, обе валидации,
     # все контуры), поэтому кружок «идёт распознавание» вертелся всю
     # сборку и все контуры — независимо от того, бежит ли OCR. Теперь
     # «в процессе» ведёт живая строка `/stages` (`_ocr_stage_running`,
     # применяется в `_update_beads`/`_sync_ocr_bead`), а `OCR_PROCESSING`
     # остаётся мёртвым рудиментом: в прямом конвейере он не присваивается
     # нигде, попасть в него можно только откатом (`app/api/rollback.py`).
     {DiagramStatus.OCR_PROCESSING},
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



def _ocr_stage_running(stages) -> bool:
    """Бежит ли распознавание ПРЯМО СЕЙЧАС — по строке `/stages`, не по статусу.

    Своего статуса у OCR нет: он считается параллельно сборке графа и контурам,
    а `DiagramStatus` — одно поле на всех.

    ⛔ Предел `_stage_stuck` здесь НЕ применяется, хотя кнопки его применяют:
    он считается от `WAIT_LIMIT_S` = 600 с (предел ожидания РАСКЛАДКИ), а у
    распознавания свой потолок — `time_limit` 3600 с (`worker/tasks/ocr.py`).
    Часовая задача на CPU-only сервере — норма, и по чужому пределу бусина
    гасла бы посреди живой работы. Конец работы приносит либо артефакт
    (`_ocr_notified`), либо `ERROR` от `set_diagram_error`.
    """
    for s in (stages or []):
        if (s.get("stage_type") or "").lower() != "ocr":
            continue
        if (s.get("status") or "").lower() == "running":
            return True
    return False


def _binding_reachable(status: DiagramStatus) -> bool:
    """Есть ли привязке что открывать при этом статусе.

    Готовый OCR-артефакт — только ПОЛОВИНА условия. Вкладка привязки кладёт
    подписи на узлы ПРОВЕРЕННОГО графа и читает `graph_validated.json`,
    которого до «Проверки схемы» не существует; распознавание же идёт
    параллельно сборке и заканчивается раньше неё. Поэтому кнопка и бусина,
    зажжённые по одному `has_ocr_result`, вели оператора в пустую вкладку
    из `building_graph`/`built`/`validating_graph`.

    Порог назван не на глаз: ровно с `VALIDATED_GRAPH` пускает серверный гейт
    `POST /api/ocr/{uid}/binding/save` (`app/api/ocr.py:244-249`) — раньше
    него сохранение привязок отвечает 400. Кнопку «Распознавание текста»
    этот порог НЕ трогает: OCR при любом из этих статусов действительно
    завершён, и зелёная кнопка про него не врёт.
    """
    return _status_ge(status, DiagramStatus.VALIDATED_GRAPH)


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
    # Финальная скелетизация принадлежит «Проверке узлов», а не «Выделению
    # труб»: её статусы (`SKELETONIZING_FINAL`/`SKELETONIZED_FINAL`) лежат в
    # наборе бусины `junction`, и оператор смотрел на крутящуюся бусину
    # одного этапа и заполняющуюся кнопку другого (боль 1, Б16). Решётка —
    # `tests/ui/test_stage_maps_invariant.py`.
    "final_skeletonization": "junction",
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
# Диалог экспорта
# =====================================================================

class ExportDialog(QDialog):
    """Забрать готовое или пересобрать заново.

    Формирование и сохранение разведены: оба файла — чертёж FXML в размерах
    холста и расчётная схема .prtx — собирает сам этап «Экспорт», а диалог
    только забирает их с сервера. Раньше он спрашивал формат и ЗАПУСКАЛ
    генерацию одного из двух, поэтому за вторым файлом оператор жал «Экспорт»
    ещё раз и попадал на «схема уже собирается — повторите».

    action() → 'download' | 'rebuild_fxml' | 'rebuild_prtx' | None (отмена)
    """

    def __init__(self, parent, default_dir: str, default_name: str,
                 prtx_state: dict):
        super().__init__(parent)
        self.setWindowTitle("Экспорт")
        self.setMinimumWidth(520)
        self._action = None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Папка:"))
        dir_row = QHBoxLayout()
        self._dir_edit = QLineEdit(default_dir or "")
        dir_row.addWidget(self._dir_edit)
        btn_browse = QPushButton("Обзор…")
        btn_browse.clicked.connect(self._browse)
        dir_row.addWidget(btn_browse)
        layout.addLayout(dir_row)

        layout.addWidget(QLabel("Имя файла (без расширения):"))
        self._name_edit = QLineEdit(default_name)
        layout.addWidget(self._name_edit)

        self._fxml_check = QCheckBox("Чертёж FXML (холст 1:1)")
        self._fxml_check.setChecked(True)
        layout.addWidget(self._fxml_check)

        self._prtx_check = QCheckBox("Расчётная схема .prtx (САПФИР)")
        layout.addWidget(self._prtx_check)

        note = QLabel(_prtx_state_text(prtx_state))
        note.setWordWrap(True)
        note.setIndent(20)
        ready = bool(prtx_state.get("artifact_ready"))
        note.setStyleSheet("color: gray;" if ready else "color: #b00;")
        layout.addWidget(note)

        self._prtx_check.setEnabled(ready)
        self._prtx_check.setChecked(ready)

        self._btn_download = QPushButton("💾 Скачать")
        self._btn_download.setDefault(True)
        self._btn_download.clicked.connect(lambda: self._finish("download"))
        layout.addWidget(self._btn_download)

        self._fxml_check.toggled.connect(self._sync_download_button)
        self._prtx_check.toggled.connect(self._sync_download_button)
        self._sync_download_button()

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(line)

        layout.addWidget(QLabel("Пересобрать заново на сервере:"))
        rebuild_row = QHBoxLayout()
        btn_fxml = QPushButton("♻ Чертёж FXML")
        btn_fxml.clicked.connect(lambda: self._finish("rebuild_fxml"))
        rebuild_row.addWidget(btn_fxml)
        btn_prtx = QPushButton(
            "♻ Расчётную схему" if ready else "▶ Собрать расчётную схему")
        btn_prtx.clicked.connect(lambda: self._finish("rebuild_prtx"))
        rebuild_row.addWidget(btn_prtx)
        layout.addLayout(rebuild_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "Папка для сохранения", self._dir_edit.text().strip())
        if chosen:
            self._dir_edit.setText(chosen)

    def _sync_download_button(self):
        self._btn_download.setEnabled(
            self._fxml_check.isChecked() or self._prtx_check.isChecked())

    def _finish(self, action: str):
        self._action = action
        self.accept()

    def action(self):
        return self._action

    def values(self):
        """(путь к .fxml, качать ли FXML, качать ли .prtx).

        Путь отдаём с расширением .fxml — имя для .prtx из него считает
        `prtx_converter.prtx_target`, который умеет не резать имена вида
        «1. Схема отборов … турбины 1» по первой точке.
        """
        import os
        name = (self._name_edit.text() or "diagram").strip() or "diagram"
        if not name.lower().endswith((".fxml", ".xml")):
            name += ".fxml"
        return (os.path.join(self._dir_edit.text().strip(), name),
                self._fxml_check.isChecked(),
                self._prtx_check.isChecked())


def _prtx_state_text(prtx_state: dict) -> str:
    """Подпись под галочкой .prtx по ответу /prtx/status."""
    ready = bool(prtx_state.get("artifact_ready"))
    if prtx_state.get("state") == "building":
        return ("⏳ Идёт пересборка; скачать сейчас можно предыдущую версию."
                if ready else
                "⏳ Схема собирается на сервере — скачать можно будет позже.")
    if ready:
        size = prtx_state.get("artifact_size") or 0
        return f"✔ Схема собрана на сервере ({size // 1024} КБ)."
    error = prtx_state.get("error")
    return ("⚠ Схема не собрана: " + error if error
            else "⚠ Схемы на сервере нет — соберите её кнопкой ниже.")


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

    #: Русская подпись кнопки по её ключу. Одна на класс: тот же словарь
    #: собирался локально трижды (`_KEY_LABELS = {k: v for k, v in ...}`),
    #: и четвёртую копию плодить незачем.
    _KEY_LABELS = dict(_BUTTON_DEFS)

    #: Ключи фазы B: «Проверка схемы» ⇄ «Контуры» ⇄ «Привязка» ⇄ «Ручная
    #: правка». Вход в уже пройденный этап здесь НЕ откат (решения Максима
    #: №7/№8): этапы разные, работа независимая, и посмотреть на сделанное
    #: оператор имеет право без разрушения конвейера. Серверные гейты фазы B
    #: повторное сохранение и подтверждение принимают (Н2/Н2с/3.1в/Н8+).
    #: Фаза A (`frame`…`graph`) линейна, повторный проход там разрушающий —
    #: она остаётся за диалогом отката.
    _PHASE_B_FREE_ENTRY = ("val_graph", "contours", "ocr_binding", "edit_graph")

    #: Сырой OCR независим от графа и переживает откат: распознавание считается
    #: параллельно сборке и от узлов не зависит. Что именно сохраняет флаг —
    #: решает сервер (`app/api/rollback.py`): с блока 5 это `OCR_RESULT`,
    #: `OCR_CLEANED` и `OCR_VALIDATION`, но НЕ `OCR_BINDING` — привязка держит
    #: `node_id`, а `id` узлов пересборку не переживают.
    #:
    #: `junction` здесь с блока 5: его цель (`detected_junctions`) тоже раньше
    #: сборки, и без флага «Проверка узлов» сносила сырой OCR — против решения
    #: Максима «сырой живёт». Политики `graph` и `junction` выровнены.
    _PRESERVE_OCR_KEYS = ("junction", "graph", "val_graph", "contours")
    #: `ocr` здесь — пункт 5-1 дороги: «Переделать OCR» не должно сносить
    #: SAM2-контуры (их считают поточечно руками). После Н3+ клик по
    #: пройденному распознаванию откатов не делает вовсе, но rollback-путь
    #: остаётся достижим с других кнопок — страховка та же однострочная.
    #: В `_PRESERVE_OCR_KEYS` ключа `ocr` быть НЕ должно: при «Переделать OCR»
    #: старые OCR-артефакты обязаны сноситься.
    #:
    #: ⛔ `graph` отсюда УБРАН блоком 5: «Сборка схемы» — это ПЕРЕСБОРКА, а
    #: контуры сидят на `ann_id` детекции и вливаются в новый граф по IoU
    #: (`modules/graph/core/contours_merge.py`) без единого сигнала устаревания.
    #: Сохранённые поверх нового поколения, они садятся на чужие узлы молча.
    _PRESERVE_CONTOURS_KEYS = ("val_graph", "contours", "ocr")

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
        self._ocr_rerunning = False  # идёт подтверждённый перезапуск OCR
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
        self._ocr_rerunning = False
        self._fxml_save_prompted = True
        self._prtx_armed = False
        self._prtx_skip_next = False
        self._stage_errors = {}
        self._dispatch_refusals = {}
        self._last_status = DiagramStatus.UPLOADED
        # СОСТОЯНИЕ СТАДИЙ ПРИНАДЛЕЖИТ ДИАГРАММЕ, А НЕ ОКНУ (боль Б1). Без
        # сброса кнопки, бусины и гейт новой схемы решались стадиями
        # предыдущей, а вкладки получали код проекта ЧУЖОГО проекта
        # (`_get_project_code` кешировал его на первый вопрос и не сбрасывал
        # нигде). `_last_stages = None` — «стадии ещё не читались»: пустой
        # список означал бы «прочитаны, их нет», и гейт судил бы по нулю.
        self._last_stages = None
        self._filled_keys = {}
        self._gate_blocked = False
        self._layout_gate = None
        self._project_code = None

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
        # И ВКЛЮЧИТЬ СЛЕЖЕНИЕ. Подписка на сигналы без него молчит: `watch`
        # звал только список диаграмм и только для «обрабатываемых» статусов
        # (`diagram_list.py`), поэтому у схемы, открытой в любом другом
        # статусе, стадии не приезжали вовсе. Идемпотентно (множество uid), а
        # опрос сам снимется на финальном статусе без бегущих стадий.
        self.status_provider.watch(uid)

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
            # Подавление автосборки .prtx живёт один прогон генерации. Прогон
            # кончился ошибкой — снимаем, иначе флаг дожил бы до следующего
            # COMPLETED и съел бы уже честную автосборку.
            self._prtx_skip_next = False
            self._apply_error_status(error_stage, error_message)
            return
        self._stage_errors = {}

        self._update_beads(status)
        self._update_buttons(status, error_stage=error_stage)
        self._update_gif(status)

        # Автоконвертор .prtx взводим ШИРЕ, чем переход в COMPLETED: на сам
        # GENERATING_FXML опрос (2 с) почти никогда не попадает — генерация
        # FXML занимает 0.1 с (замер по 75 стадиям, max 1.3 с), и на авто-пути
        # после «Проверки схемы» клиент видит VALIDATED_GRAPH → COMPLETED.
        # Признак «конвейер дошёл до конца при нас» — увиденный ранее НЕ-готовый
        # статус; при открытии уже готовой схемы взвода нет и сборка не
        # запускается (иначе .prtx пересобирался бы на каждом открытии).
        if status != DiagramStatus.COMPLETED:
            self._prtx_armed = True

        # По завершении генерации FXML — добрать второй файл этапа, расчётную
        # схему. На диск оператора здесь не сохраняется ничего: этап ФОРМИРУЕТ
        # оба файла на сервере, забирает их отдельный диалог (ExportDialog).
        if status == DiagramStatus.COMPLETED:
            if getattr(self, "_prtx_skip_next", False):
                # «Пересобрать чертёж FXML» — только чертёж: расчётная схема
                # стоит минут счёта и повторного чтения ключа лицензии, и для
                # неё в диалоге экспорта есть своя кнопка.
                self._prtx_skip_next = False
                self._prtx_armed = False
            elif getattr(self, "_prtx_armed", False):
                self._prtx_armed = False
                self._start_prtx_conversion()

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
                    self._ocr_rerunning = False     # новый результат пришёл
                    self._stop_ocr_poll()
                    self.beads.set_state(BEAD_OCR, BeadState.COMPLETED)
                    if "ocr" in self._action_buttons:
                        self._action_buttons["ocr"].setEnabled(True)
                        self._action_buttons["ocr"].setStyleSheet(_BTN_STYLE_GREEN)
                    # Привязка — только с проверенным графом (`_binding_reachable`),
                    # и цветом по тому, пройден ли этап (`_light_binding_button`).
                    self._light_binding_button(status)
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
            # Привязка — только с проверенным графом (`_binding_reachable`),
            # и цветом по тому, пройден ли этап (`_light_binding_button`).
            self._light_binding_button(status)
            # «Ручная правка» по факту OCR-артефакта БОЛЬШЕ НЕ ОТКРЫВАЕТСЯ:
            # гейт §3.2 — сначала привязка, потом готовая раскладка
            # (ui/services/layout_gate.py, применяется в _on_stages_updated).

        # Решение Максима 2026-08-25 (возврат ревизии связки, дефект 2):
        # во время сборки графа перезапуск распознавания заведомо откажет
        # (гейт пересборки блока 5) — кнопку гасим, а не предлагаем тупиковый
        # вопрос. Обе красящие ветки выше уже отработали — гашение последним.
        if status is DiagramStatus.BUILDING_GRAPH and "ocr" in self._action_buttons:
            self._action_buttons["ocr"].setEnabled(False)
            self._action_buttons["ocr"].setStyleSheet(_BTN_STYLE_GRAY)

    # =================================================================
    # OCR artifact polling (independent of DiagramStatus changes)
    # =================================================================

    def _light_binding_button(self, status) -> None:
        """Зажечь кнопку и бусину привязки по факту готового OCR-результата.

        ⛔ Цвет решает НЕ артефакт, а ПРОЙДЕН ЛИ ЭТАП: при `ocr_bound` и позже
        привязка уже сделана, и жёлтое «сделай это» там врёт. Замечание приёмки
        2026-08-25: во время пересчёта раскладки тик поллера перекрашивал
        готовую зелёную привязку в жёлтую, и оператор видел, как пройденный
        этап снова просится в работу (замер §P3.11).

        Порог достижимости — тот же `_binding_reachable`, что и у гейта
        сохранения на сервере; раньше него привязка ВПЕРЕДИ и жёлтый честен.

        Одна точка на три ветки параллельного OCR (`_apply_status` × 2 и
        `_check_ocr_artifact`): правило было написано трижды, и разъехаться
        ему было нечем помешать.
        """
        btn = self._action_buttons.get("ocr_binding")
        if btn is None or not _binding_reachable(status):
            return
        done = _status_ge(status, DiagramStatus.OCR_BOUND)
        btn.setEnabled(True)
        btn.setStyleSheet(_BTN_STYLE_GREEN if done else _BTN_STYLE_YELLOW)
        self.beads.set_state(
            BEAD_OCR_BINDING,
            BeadState.COMPLETED if done else BeadState.AVAILABLE)

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
                self._ocr_rerunning = False         # новый результат пришёл
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
                # Привязка — только с проверенным графом (`_binding_reachable`).
                # Здесь порог берётся от `_last_status`: у тика поллера своего
                # статуса нет. Тик, заставший статус раньше «Проверки схемы»,
                # дверь не открывает — её откроет `_apply_status` тем же
                # правилом, когда статус дойдёт (ветка `_ocr_notified` выше).
                self._light_binding_button(self._last_status)
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

        # OCR: «в процессе» — по живой строке стадии, а не по чужому статусу
        # (боль 1.4). Ставится ДО отказов веера: отказ старше этого признака и
        # обязан перекрывать его красным (пункт 1-48).
        if (target.get(BEAD_OCR) != BeadState.COMPLETED
                and _ocr_stage_running(getattr(self, "_last_stages", None))):
            target[BEAD_OCR] = BeadState.IN_PROGRESS

        # ПЕРЕЗАПУСК РАСПОЗНАВАНИЯ: строки стадии может ещё не быть (её заводит
        # воркер), а результат сервер уже удалил. Пока новый не пришёл, OCR
        # крутится, а привязке кормиться нечем — и то и другое ВОПРЕКИ статусу,
        # который про перезапуск не знает (у OCR своего статуса нет).
        if self._ocr_rerun_in_flight():
            target[BEAD_OCR] = BeadState.IN_PROGRESS
            target[BEAD_OCR_BINDING] = BeadState.UNAVAILABLE
            target[BEAD_EDIT_GRAPH] = BeadState.UNAVAILABLE

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

        # Второй файл этапа. Статус диаграммы про .prtx не знает — COMPLETED
        # ставится по готовому чертежу, — поэтому пока схема считается, бусину
        # «Экспорт» держим сами. Иначе оператор видит зелёный этап и идёт
        # качать .prtx, которой ещё нет.
        if self._prtx_busy():
            target[BEAD_FXML] = BeadState.IN_PROGRESS

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

        # Сборка .prtx — вторая половина этапа «Экспорт» (см. _update_beads).
        # Глушим кнопку на это время: раньше нажатие поверх бегущей сборки
        # упиралось в «схема уже собирается, повторите».
        if self._prtx_busy():
            processing.add("fxml")
            available.discard("fxml")
            completed.discard("fxml")

        # ПЕРЕЗАПУСК РАСПОЗНАВАНИЯ: сервер удаляет `OCR_RESULT` в самом начале
        # `/ocr/start`, значит кормиться привязке сейчас нечем. Гасим её
        # НЕЗАВИСИМО от статуса: `_buttons_for_status` считает `ocr_binding`
        # пройденным при `ocr_bound` и позже, поэтому `_start_ocr` гасил кнопку,
        # а первый же тик опроса возвращал её на место (замер §P3.11).
        if self._ocr_rerun_in_flight():
            processing.add("ocr")
            available.discard("ocr")
            completed.discard("ocr")
            # Привязка и «Ручная правка» кормятся одним и тем же результатом:
            # холст несёт подписи, привязанные к узлам, и пересобирать его
            # поверх исчезнувшего OCR не на чем (решение Максима на приёмке
            # 2026-08-25). По завершении перезапуска доступность «Ручной
            # правки» снова решает ГЕЙТ РАСКЛАДКИ штатным путём — сам гейт
            # эта ветка не трогает.
            for _k in ("ocr_binding", "edit_graph"):
                processing.discard(_k)
                available.discard(_k)
                completed.discard(_k)

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
            # Писатель этого значения переехал на `skeletonizing_final`
            # (`worker/tasks/skeleton.py`, тот же блок болей); клетка остаётся
            # для строк `error_stage`, записанных ДО правки, — они лежат в БД
            # у уже сломанных диаграмм.
            "skeletonizing_simple": "pipe",
            "skeletonizing_final": "junction",
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

    def _rollback_consequence(self, target: str) -> str:
        """Что станет с холстом «Ручной правки» при откате до `target`.

        Граница та же, что на сервере (`app/api/rollback.py: canvas_dies`):
        цель «Привязка подписей» и позже холст сохраняет, более ранняя —
        сносит вместе с файлом. `graph_canvas.json` — единственное место, где
        живут правки оператора, и до этого пункта диалог о нём молчал: строка
        «все последующие артефакты будут удалены» формально не врала, но слов
        «Ручная правка» и «холст» в ней не было.
        """
        try:
            keeps_canvas = _status_ge(DiagramStatus(target), DiagramStatus.OCR_BOUND)
        except ValueError:
            keeps_canvas = False
        if keeps_canvas:
            return ("Холст «Ручной правки» сохранится, "
                    "раскладка будет пересчитана.")
        return ("Правки в «Ручной правке» будут потеряны: "
                "холст соберётся заново из проверенной схемы.")

    def _ocr_already_ran(self) -> bool:
        """Запускалось ли распознавание для этой диаграммы хоть раз.

        ⛔ По статусу диаграммы это НЕ определяется, и на этом сломался первый
        заход пункта: у распознавания своего статуса нет — оно идёт параллельно
        сборке графа, — а `OCR_COMPLETED` в конвейере не присваивает НИКТО
        (`MEASUREMENTS §P3.2`). Вопрос стоял под `key in completed`, то есть
        в живом конвейере не срабатывал ни разу: оператор жмёт зелёную кнопку
        при `validated_graph`/`contours_validated`, где этап «пройденным» не
        числится, а кнопка зеленеет по АРТЕФАКТУ.

        Признаков четыре, и любого достаточно:
          · результат уже видели (`_ocr_notified` — тот же признак, по которому
            зеленеет кнопка);
          · распознавание числится упавшим (`_stage_errors`) — это путь красной
            кнопки, и там признак есть ВСЕГДА, без опоры на кэш стадий;
          · строка стадии `ocr` есть в `/stages` — бежит, упала или закончилась;
          · статус всё-таки дошёл до `ocr_completed` (достижим откатом).
        «Первый запуск» — это «не бежало И результата нет», обе половины.
        """
        if self._ocr_notified:
            return True
        if "ocr" in getattr(self, "_stage_errors", {}):
            return True
        if _status_ge(self._last_status, DiagramStatus.OCR_COMPLETED):
            return True
        for s in (getattr(self, "_last_stages", None) or []):
            if (s.get("stage_type") or "").lower() == "ocr":
                return True
        return False

    def _ocr_rerun_in_flight(self) -> bool:
        """Идёт подтверждённый ПЕРЕЗАПУСК распознавания — результата сейчас нет.

        Флаг живёт от успешного `POST /ocr/start` до нового `has_ocr_result`.
        Отдельный от `_ocr_notified` он не для красоты: тот False и на СВЕЖЕЙ
        загрузке готовой схемы (ветка опроса артефакта не заходит на статусы
        после контуров), и по нему привязку гасить нельзя — она там законна.
        """
        return bool(getattr(self, "_ocr_rerunning", False))

    def _confirm_ocr_restart(self) -> bool:
        """Спросить про повторный запуск распознавания. True — запускаем."""
        reply = QMessageBox.question(
            self, "Распознавание текста",
            "Распознавание текста уже запускалось.\n"
            "Запустить его заново?\n\n"
            "Прежний результат распознавания будет удалён, "
            "привязку подписей придётся пройти снова.\n"
            "Схема и контуры не пострадают.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

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

        # Экспорт: откат не предлагаем — готовый этап открывает диалог
        # «скачать / пересобрать» (_start_fxml), а не переигрывает конвейер.
        if key == "fxml":
            original_handler()
            return

        # Фаза B: вход в пройденный этап — не откат. Раньше здесь был один
        # диалог на все кнопки: «Да» разрушал конвейер, «Нет» не открывал
        # ничего, то есть посмотреть на сделанное было нельзя вовсе.
        # Устаревание холста после правки ловит sha (`canvas_state.is_stale`) —
        # удалять руками нечего.
        if key in self._PHASE_B_FREE_ENTRY:
            original_handler()
            return

        # Распознавание — не вкладка, а POST: `_start_ocr` сносит сырой
        # результат (`app/api/ocr.py`) и жжёт минуты CPU. Конвейер при этом
        # назад не идёт — отката тут нет (решение №6 редтима).
        # ⛔ Вопрос про ПЕРЕЗАПУСК стоит НЕ здесь, а в самом `_start_ocr`:
        # здесь это была бы одна дверь из двух (вторая — окно отчёта об
        # ошибке), да ещё и под мёртвым условием `key in completed`. Приёмка
        # глазами 2026-08-25: при статусах фазы B кнопка зелёная по артефакту,
        # а «пройденным» этап не числится — вопроса не было ни разу.
        if key == "ocr":
            original_handler()
            return

        if key in completed:
            # Этап уже пройден — предложить откат
            target = self._ROLLBACK_TARGET.get(key, "")
            reply = QMessageBox.question(
                self, "Откат",
                f"Этап «{self._KEY_LABELS.get(key, key)}» уже пройден.\n"
                f"Вернуться к «{_status_label(target)}» и пройти его заново?\n\n"
                f"{self._rollback_consequence(target)}\n"
                f"Артефакты последующих этапов будут удалены.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            try:
                QApplication.setOverrideCursor(Qt.WaitCursor)
                # Preserve OCR/contour artifacts when rolling back graph/contour stages,
                # because OCR and SAM2 run in parallel and are independent of graph.
                preserve_ocr = key in self._PRESERVE_OCR_KEYS
                preserve_contours = key in self._PRESERVE_CONTOURS_KEYS
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
        """Ручной запуск/retry OCR.

        Вопрос про повторный запуск стоит ЗДЕСЬ, а не в ветке клика: это
        единственная воронка, через которую все пути оператора попадают в
        `POST /ocr/start` (греп `start_ocr` по `ui/` даёт один вызывающий).
        Перечень путей остаётся ДОКАЗАТЕЛЬСТВОМ, а не несущей конструкцией:
        новая кнопка, ведущая сюда, получит вопрос без правки её ветки.
        """
        if self._ocr_already_ran() and not self._confirm_ocr_restart():
            return
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
            # Метка «идёт перезапуск» — её читают `_update_buttons`/`_update_beads`.
            # Без неё они перекрашивают привязку обратно по статусу на первом же
            # тике опроса, и оператор видит открытую дверь в пустую вкладку.
            self._ocr_rerunning = True
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
        """Кнопка «Экспорт».

        Этап формирует ОБА файла сам — чертёж FXML в размерах холста и
        расчётную схему .prtx. Поэтому на готовой схеме кнопка ничего не
        переигрывает, а открывает диалог «скачать / пересобрать»; ветка
        генерации нужна, только когда авто-путь не доехал (ERROR, зависшая
        стадия) и качать ещё нечего.
        """
        if self._last_status == DiagramStatus.COMPLETED:
            self._open_export_dialog()
        else:
            self._regenerate_fxml(with_prtx=True)

    def _regenerate_fxml(self, with_prtx: bool):
        """Запустить генерацию чертежа.

        Размер листа НЕ передаём: чертёж всегда 1:1 с холстом. Воркер по
        `canvas_transform` уходит в canvas_to_fxml и page_size там игнорирует —
        ровно то же делает авто-путь после «Ручной правки», так что ручная
        пересборка даёт тот же файл, а не «другой FXML».

        with_prtx=False — пересборка ТОЛЬКО чертежа: расчётную схему не
        трогаем, иначе кнопка «Пересобрать чертёж» тянула бы за собой минуты
        счёта и повторное чтение ключа лицензии, о которых не просили.
        """
        # Разрыв моста — из настроек редактора этой диаграммы (если задан пользователем)
        bridge_gap = None
        try:
            from ui.services.ui_settings import UISettings
            _bg = UISettings.instance().get_appearance(self._uid, "bridge_gap_factor", None)
            if _bg is not None:
                bridge_gap = float(_bg)
        except Exception:
            bridge_gap = None

        try:
            self._prtx_skip_next = not with_prtx
            self.api_client.generate_fxml(self._uid, bridge_gap=bridge_gap)
            self.status_provider.watch(self._uid)
            self.status_message.emit("📄 Формирование чертежа FXML запущено", 3000)
            self._refresh_status()
        except APIError as exc:
            self._prtx_skip_next = False
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось запустить генерацию FXML:\n{exc.message}",
            )

    def _open_export_dialog(self):
        """Забрать готовые файлы этапа или пересобрать один из них."""
        import os

        try:
            prtx_state = self.api_client.prtx_status(self._uid)
        except APIError as exc:
            logger.warning("PRTX status unavailable: %s", exc)
            prtx_state = {"state": "idle",
                          "error": f"сервер не ответил ({exc.message})"}

        # Идущую сборку клиент знает точнее сервера: пока жив наш поток, лежащий
        # на сервере артефакт — от ПРОШЛОГО прогона.
        if self._prtx_busy():
            prtx_state = dict(prtx_state, state="building")

        base = (self._diagram_name or "diagram").strip()
        default_name = os.path.splitext(base)[0] or "diagram"
        default_dir = getattr(self, "_last_fxml_dir", None) or os.path.expanduser("~")

        dialog = ExportDialog(self, default_dir, default_name, prtx_state)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        action = dialog.action()
        if action == "rebuild_fxml":
            self._regenerate_fxml(with_prtx=False)
            return
        if action == "rebuild_prtx":
            self._start_prtx_conversion()
            return

        fxml_path, want_fxml, want_prtx = dialog.values()
        self._last_fxml_dir = os.path.dirname(fxml_path)
        self._download_export(fxml_path, want_fxml, want_prtx)

    def _download_export(self, fxml_path: str, want_fxml: bool, want_prtx: bool):
        """Скачать готовые артефакты этапа в выбранные пути."""
        from ui.services.prtx_converter import prtx_target

        targets = []
        if want_fxml:
            targets.append(("fxml", "Чертёж FXML", Path(fxml_path)))
        if want_prtx:
            targets.append(("prtx", "Расчётная схема .prtx", prtx_target(fxml_path)))

        saved, failed = [], []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for art_type, title, path in targets:
                try:
                    self.api_client.download_artifact(self._uid, art_type, path)
                    saved.append(str(path))
                except APIError as exc:
                    logger.error("Export download failed (%s): %s", art_type, exc)
                    failed.append(f"{title}: {exc.message}")
        finally:
            QApplication.restoreOverrideCursor()

        if failed:
            QMessageBox.warning(
                self, "Экспорт",
                "Сохранено:\n" + ("\n".join(saved) if saved else "— ничего —")
                + "\n\nНе удалось сохранить:\n" + "\n".join(failed))
            return

        self.status_message.emit("💾 Сохранено: " + "; ".join(saved), 8000)
        QMessageBox.information(self, "Экспорт", "Сохранено:\n" + "\n".join(saved))

    def _start_prtx_conversion(self):
        """Собрать .prtx — второй файл этапа «Экспорт».

        Считает СЕРВЕР (контейнер `prtx`), клиент отдаёт только ключ лицензии
        САПФИР из профиля оператора — подробности в
        ui/services/prtx_converter.py. Результат ложится артефактом рядом с
        diagram.fxml; на диск оператора его забирает ExportDialog.
        """
        from ui.services.prtx_converter import PrtxWorker
        from ui.services.prtx_license import diagnose_local

        report = diagnose_local()
        if not report.ok:
            problem = report.first_problem
            self._on_prtx_error(
                f"{problem.title}\n\n{problem.detail}\n\n{problem.hint}")
            return

        # Повторный запуск поверх бегущей сборки затёр бы ссылку на живой
        # QThread — он остался бы без владельца.
        running = getattr(self, "_prtx_thread", None)
        if running is not None and running.isRunning():
            logger.info("PRTX: сборка уже идёт, повтор пропущен")
            self.status_message.emit("⏳ Расчётная схема уже собирается…", 4000)
            return

        self.status_message.emit("⏳ Сборка расчётной схемы .prtx…", 4000)

        self._prtx_thread = QThread()
        self._prtx_worker = PrtxWorker(self.api_client, self._uid, None)
        self._prtx_worker.moveToThread(self._prtx_thread)
        self._prtx_thread.started.connect(self._prtx_worker.run)
        self._prtx_worker.finished.connect(self._on_prtx_done)
        self._prtx_worker.error.connect(self._on_prtx_error)
        # Сборка идёт минутами — без этого окно выглядит замершим
        self._prtx_worker.progress.connect(
            lambda msg: self.status_message.emit(msg, 4000))
        # Поток гасит сам работник — рабочая область живёт дольше конвертации,
        # но связи со слотами Qt рвёт вместе с получателем (образец — pipe_tab).
        self._prtx_worker.finished.connect(self._prtx_thread.quit)
        self._prtx_worker.error.connect(self._prtx_thread.quit)
        self._prtx_running = True
        self._prtx_uid = self._uid
        # ⛔ Рабочая область живёт дольше конвертации, но НЕ дольше
        # процесса: `cleanup()` этот поток не упоминает вовсе, и клиент,
        # закрытый посреди сборки, падал `0xC0000409` — 6 раз из 6 на
        # каждом жесте оператора (пункт 1-50, замер §127). Дверь ниже
        # просит работника остановиться и даёт выходу его дождаться.
        hand_over(self, self._prtx_thread, self._prtx_worker,
                  name="сборка .prtx", uid=self._uid)
        self._prtx_thread.start()
        self._refresh_export_state()

    def _prtx_busy(self) -> bool:
        """Идёт ли сборка .prtx ИМЕННО для открытой сейчас диаграммы.

        Сверка с uid обязательна: рабочая область переиспользуется, и оператор
        может уйти к другой схеме, пока считается эта — без сверки её бусина
        «Экспорт» показывала бы чужую работу.
        """
        return (getattr(self, "_prtx_running", False)
                and getattr(self, "_prtx_uid", None) == self._uid)

    def _refresh_export_state(self):
        """Перерисовать бусину и кнопку «Экспорт» под ход сборки .prtx.

        Через `_apply_status` этого не сделать: COMPLETED для StatusProvider —
        финальный статус, опрос после него остановлен
        (`ui/services/status_provider.py`), и нового вызова просто не будет.
        """
        if not self._uid:
            return
        self._update_beads(self._last_status)
        self._update_buttons(self._last_status)

    @Slot(str)
    def _on_prtx_done(self, where: str):
        self._prtx_running = False
        self._refresh_export_state()
        self.status_message.emit("📐 Расчётная схема .prtx собрана: " + where, 8000)

    @Slot(str)
    def _on_prtx_error(self, message: str):
        # Раньше автосборка молчала строкой статусбара на 10 с: оператор её не
        # видел, шёл качать .prtx и получал 404 «артефакта нет». Провал ВТОРОГО
        # файла этапа показываем окном всегда — этап не пройден.
        logger.error("PRTX conversion failed: %s", message)
        self._prtx_running = False
        self._refresh_export_state()
        QMessageBox.warning(
            self, "Расчётная схема",
            f"Не удалось собрать расчётную схему:\n\n{message}\n\n"
            "Чертежа FXML это не касается — он готов, скачайте его кнопкой "
            "«Экспорт».")

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
        """Очистка рамки завершена (save+complete или skip) — детекцию ставит сервер.

        Слежение будим БЕЗУСЛОВНО. `_close_tab_and_restore_header` возвращает
        опрос «как было», а было никак: поллер снимает его на финальном статусе
        (`_FINAL_STATUSES`), и вход во вкладку запоминает уже мёртвое состояние.
        До Б9 дыры не было видно — следующее звено двигал сам клиент и звал
        `watch` в своём теле; теперь звено двигает сервер, и без этой строки
        оператор видит неподвижную схему до ручного «Обновить».
        `watch` идемпотентен (множество uid) и снимется сам.
        """
        logger.info("Frame confirmed via signal")
        self._close_tab_and_restore_header()
        self._refresh_status()
        if self._uid:
            self.status_provider.watch(self._uid)

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
        """CVAT сохранил аннотации → скачать аннотации.

        Сегментацию ставит СЕРВЕР — тем же вызовом, что скачивает аннотации
        (`app/api/cvat.py`, Б8). Прежде звено двигал этот метод, и закрытая
        вкладка или упавший клиент останавливали схему навсегда. Свой вызов
        `_start_segmentation` убран, иначе задача уходила бы дважды; кнопка
        «Выделение труб» остаётся — она нужна откатам, ERROR и случаю, когда
        у сервера не поднялся брокер.
        """
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
        # Слежение будим БЕЗУСЛОВНО — та же дыра, что у подтверждения рамки:
        # `_close_tab_and_restore_header` возвращает опрос «как было», а было
        # никак (поллер снял его на финальном `detected`/`validated_bbox` ещё
        # до входа во вкладку). Прежде это чинил сам `_start_segmentation`,
        # который звал `watch`; он убран, а сегментацию ставит сервер.
        if self._uid:
            self.status_provider.watch(self._uid)

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
        self._sync_ocr_bead()
        self._apply_layout_gate(stages)

    def _sync_ocr_bead(self):
        """Свести бусину OCR со свежими стадиями — без смены статуса.

        При статусах фазы B опрос статуса остановлен
        (`status_provider._FINAL_STATUSES`), и `_update_beads` звать некому:
        единственный источник свежести — тик OCR-поллера, который приходит
        сюда через `_on_stages_updated`. Чужие вердикты не трогаем: отказ
        веера и упавшая стадия старше признака «бежит», а готовность OCR
        доказывает артефакт (`_ocr_notified`), а не строка стадии.
        """
        if (self._ocr_notified
                or "ocr" in self._dispatch_refusals
                or "ocr" in getattr(self, "_stage_errors", {})
                or _status_ge(self._last_status, DiagramStatus.OCR_COMPLETED)):
            return
        state = dict(_beads_for_status(self._last_status)).get(
            BEAD_OCR, BeadState.UNAVAILABLE)
        if _ocr_stage_running(getattr(self, "_last_stages", None)):
            state = BeadState.IN_PROGRESS
        self.beads.set_state(BEAD_OCR, state)

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
                # И БУСИНУ: крутит её сам гейт (ветка `waiting` ниже), значит
                # сам и обязан вернуть — иначе кружок вертится до ручного
                # «Обновить» (боль 1.3). Состояние берём тем же путём, что и
                # `_update_beads`, — по статусу. Красное не трогаем: отказ
                # отправки и упавшая стадия старше открытой двери.
                if ("edit_graph" not in self._dispatch_refusals
                        and "edit_graph" not in getattr(self, "_stage_errors", {})):
                    self.beads.set_state(
                        BEAD_EDIT_GRAPH,
                        dict(_beads_for_status(self._last_status)).get(
                            BEAD_EDIT_GRAPH, BeadState.UNAVAILABLE))
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

            # По завершении генерации FXML внутри _apply_status запускается
            # сборка .prtx — второй файл этапа. Оба забирает ExportDialog.

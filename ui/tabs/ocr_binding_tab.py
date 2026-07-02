"""
OCR Binding Tab — вкладка привязки OCR текста к узлам графа.

НОВАЯ АРХИТЕКТУРА (v2):
  3 подвкладки вместо единого пространства:
  1) KKS — коррекция и привязка названия KKS
  2) Диаметр — привязка диаметров к рёбрам
  3) Другое — прочие блоки + финальное подтверждение

Классификация запускается автоматически при загрузке.
Цвета OCR-боксов = соответствие паттерну (зелёный/жёлтый/оранжевый/серый).
На каждой подвкладке: добавить, удалить, перенести, корректировать.
Навигация между подвкладками без потери прогресса.
"""

import json
import logging
import tempfile
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QMessageBox, QApplication,
    QTabWidget, QTabBar,
)
from PySide6.QtCore import Signal, Slot, Qt, QThread, QObject, QTimer
from PySide6.QtGui import QColor
from ui.widgets.appearance_panel import AppearanceMixin

from ui.services.api_client import APIClient, APIError
from ui.editors.ocr_binding_editor import OcrBindingEditor
from ui.widgets.toolbar_buttons import (
    make_undo_button, make_save_button, make_confirm_button,
)

logger = logging.getLogger(__name__)


def _bbox_to_bbox_dist(a: list, b: list) -> float:
    """Расстояние между границами двух bbox."""
    dx = max(0, max(a[0] - b[2], b[0] - a[2]))
    dy = max(0, max(a[1] - b[3], b[1] - a[3]))
    return (dx * dx + dy * dy) ** 0.5


def _sorted_edge_key(ek: str) -> str:
    """Нормализовать edge_key: 'B|A' → 'A|B' (sorted)."""
    parts = ek.split("|", 1)
    if len(parts) == 2:
        return f"{min(parts[0], parts[1])}|{max(parts[0], parts[1])}"
    return ek


# =====================================================================
# Background downloader (unchanged logic)
# =====================================================================

class _OcrArtifactDownloader(QObject):
    """Фоновый загрузчик артефактов для OCR tab (параллельный)."""

    finished = Signal(dict)
    error = Signal(str)
    progress = Signal(str)

    def __init__(self, api_client: APIClient, uid: str, temp_dir: Path):
        super().__init__()
        self.api_client = api_client
        self.uid = uid
        self.temp_dir = temp_dir

    def _dl_original(self):
        dest = self.temp_dir / "original.png"
        self.api_client.download_artifact(self.uid, "original_image", dest)
        return ("original_image", dest)

    def _dl_ocr_result(self):
        dest = self.temp_dir / "ocr_result.json"
        self.api_client.download_ocr_result(self.uid, dest)
        return ("ocr_result", dest)

    def _dl_graph(self):
        dest = self.temp_dir / "graph_validated.json"
        try:
            self.api_client.download_artifact(self.uid, "graph_validated", dest)
        except (APIError, Exception):
            self.api_client.download_artifact(self.uid, "graph_json", dest)
        return ("graph", dest)

    def _dl_coco(self):
        dest = self.temp_dir / "coco_validated.json"
        try:
            self.api_client.download_artifact(self.uid, "coco_validated", dest)
            return ("coco", dest)
        except (APIError, Exception):
            pass
        try:
            dest = self.temp_dir / "coco_predicted.json"
            self.api_client.download_artifact(self.uid, "coco_predicted", dest)
            return ("coco", dest)
        except (APIError, Exception):
            return None

    def _dl_binding(self):
        try:
            dest = self.temp_dir / "ocr_binding.json"
            self.api_client.download_ocr_binding(self.uid, dest)
            return ("binding", dest)
        except (APIError, Exception):
            return None

    def _dl_validation(self):
        try:
            dest = self.temp_dir / "ocr_validation.json"
            self.api_client.download_ocr_validation(self.uid, dest)
            return ("ocr_validation", dest)
        except (APIError, Exception):
            return None

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        try:
            self.progress.emit("Загрузка артефактов...")
            artifacts = {}

            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = [
                    pool.submit(self._dl_original),
                    pool.submit(self._dl_ocr_result),
                    pool.submit(self._dl_graph),
                    pool.submit(self._dl_coco),
                    pool.submit(self._dl_binding),
                    pool.submit(self._dl_validation),
                ]
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        art_type, dest = result
                        artifacts[art_type] = dest
                        self.progress.emit(f"Загружен {art_type}")

            self.finished.emit(artifacts)
        except Exception as exc:
            self.error.emit(str(exc))


# =====================================================================
# Sub-tab toolbar widget (shared toolbar template)
# =====================================================================

class _RecognizeWorker(QObject):
    """Фоновое распознавание вручную добавленных боксов (не блокирует UI)."""

    finished = Signal(list)
    error = Signal(str)

    def __init__(self, api_client, uid, boxes):
        super().__init__()
        self.api_client = api_client
        self.uid = uid
        self.boxes = boxes

    def run(self):
        try:
            resp = self.api_client.recognize_boxes(self.uid, self.boxes)
            results = resp.get("results", []) if isinstance(resp, dict) else []
            self.finished.emit(results)
        except Exception as exc:
            self.error.emit(str(exc))


class _SubTabToolbar(QWidget):
    """Панель OCR: Добавить бокс | Распознать | ...инструкция... | Отменить | Сохранить | Подтвердить."""

    add_clicked = Signal()
    delete_clicked = Signal()
    move_clicked = Signal()
    undo_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        # «Добавить бокс» (рисование прямоугольника)
        self.btn_add = QPushButton("Добавить бокс")
        self.btn_add.setToolTip(
            "Добавить бокс для ручного распознавания.\n"
            "• нажми кнопку — включить режим;\n"
            "• зажми ЛКМ и обведи прямоугольником текст на схеме;\n"
            "• повтори для каждого нужного участка;\n"
            "• затем нажми «Распознать вручную»;\n"
            "• Esc или повторное нажатие — выйти из режима."
        )
        self.btn_add.setCheckable(True)
        self.btn_add.clicked.connect(self.add_clicked.emit)
        layout.addWidget(self.btn_add)

        # левая группа доп. кнопок (Распознать)
        self.custom_layout = QHBoxLayout()
        self.custom_layout.setSpacing(4)
        layout.addLayout(self.custom_layout)

        # скрытые кнопки удаления/перемещения (совместимость с обработчиками)
        self.btn_del = QPushButton()
        self.btn_del.setCheckable(True)
        self.btn_del.setVisible(False)
        self.btn_del.clicked.connect(self.delete_clicked.emit)
        self.btn_move = QPushButton()
        self.btn_move.setCheckable(True)
        self.btn_move.setVisible(False)
        self.btn_move.clicked.connect(self.move_clicked.emit)

        # инструкция
        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet("color: #999; font-size: 11px;")
        self.hint_label.setWordWrap(False)
        layout.addWidget(self.hint_label)

        layout.addStretch()

        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color: #aaa; font-size: 11px;")
        layout.addWidget(self.stats_label)

        # правая группа: Отменить | Сохранить | Подтвердить
        self.btn_undo = make_undo_button(self.undo_clicked.emit)
        layout.addWidget(self.btn_undo)

        self.right_layout = QHBoxLayout()
        self.right_layout.setSpacing(4)
        layout.addLayout(self.right_layout)

    def reset_modes(self):
        self.btn_add.setChecked(False)
        self.btn_del.setChecked(False)
        self.btn_move.setChecked(False)


# =====================================================================
# Main OcrBindingTab
# =====================================================================

class OcrBindingTab(AppearanceMixin, QWidget):
    """
    Вкладка привязки OCR текста к узлам графа.

    Содержит 3 подвкладки:
    1) KKS — коррекция и привязка
    2) Диаметр — привязка Ø к рёбрам
    3) Другое — финальное подтверждение

    Сигналы:
        confirmed — пользователь подтвердил привязку
        status_message(str) — сообщение для statusbar
    """

    confirmed = Signal()
    status_message = Signal(str)

    # Sub-tab indices
    TAB_KKS = 0
    TAB_DIAMETER = 1
    TAB_OTHER = 2

    def __init__(
        self,
        diagram_uid: str,
        diagram_name: str,
        api_client: APIClient,
        project_config_path: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.uid = diagram_uid
        self.diagram_name = diagram_name
        self.api_client = api_client
        self._project_config_path = project_config_path

        self._temp_dir_obj = tempfile.TemporaryDirectory(prefix="ocr_binding_")
        self.temp_dir = Path(self._temp_dir_obj.name)

        # Данные
        self._ocr_blocks = []
        self._graph_data = {}
        self._bindings = []
        self._saved = True

        # Classification / validation
        self._classifications = []       # list[BlockClassification]
        self._kks_config = None
        self._cls_to_kks_config = None
        self._diameter_matcher = None
        self._auto_bind_diameters_data = None
        self._retry_count = 0

        # Confirmation flags per sub-tab
        self._kks_confirmed = False
        self._diam_confirmed = False

        # Block ownership: idx → subtab name
        self._block_subtab: dict[int, str] = {}  # "kks"/"diameter"/"other"
        # Derived index sets
        self._kks_indices = set()
        self._diameter_indices = set()
        self._other_indices = set()

        self._setup_ui()
        self._start_download()

    # =================================================================
    # UI Setup
    # =================================================================

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # === Тулбар первой строкой (QHBoxLayout) — workspace вставит «← Назад»/⚙
        #     в этот же ряд, как в базовых вкладках ===
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(8, 4, 8, 4)
        toolbar.setSpacing(8)

        self.btn_add = QPushButton("Добавить бокс")
        self.btn_add.setCheckable(True)
        self.btn_add.setToolTip(
            "Добавить бокс для ручного распознавания.\n"
            "• нажми кнопку — включить режим;\n"
            "• зажми ЛКМ и обведи прямоугольником текст на схеме;\n"
            "• повтори для каждого нужного участка;\n"
            "• затем нажми «Распознать вручную»;\n"
            "• Esc или повторное нажатие — выйти из режима."
        )
        self.btn_add.clicked.connect(self._toggle_add_mode_btn)
        toolbar.addWidget(self.btn_add)

        self.btn_recognize = QPushButton("Распознать вручную")
        self.btn_recognize.setToolTip(
            "Распознать текст во всех вручную добавленных (пустых) боксах одним прогоном.\n"
            "После распознавания режим добавления выключается."
        )
        self.btn_recognize.clicked.connect(self._run_recognize)
        toolbar.addWidget(self.btn_recognize)

        toolbar.addStretch()

        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color: #aaa; font-size: 11px;")
        toolbar.addWidget(self.stats_label)

        self.btn_undo = QPushButton("Undo")
        self.btn_undo.setToolTip("Отменить последнее действие (Ctrl+Z)")
        self.btn_undo.clicked.connect(self._undo)
        toolbar.addWidget(self.btn_undo)

        self.btn_save = make_save_button(self._save_binding, "Сохранить привязки на сервер")
        toolbar.addWidget(self.btn_save)

        self.btn_confirm_all = make_confirm_button(
            self._on_confirm,
            tooltip="Финальное подтверждение — сохранить и применить привязки")
        toolbar.addWidget(self.btn_confirm_all)

        layout.addLayout(toolbar)

        # Loading label (под тулбаром)
        self.loading_label = QLabel("Загрузка артефактов...")
        self.loading_label.setAlignment(Qt.AlignCenter)
        self.loading_label.setStyleSheet("color: #888; font-size: 14px; padding: 40px;")
        layout.addWidget(self.loading_label)

        # === Sub-tab 1: KKS ===
        self.kks_toolbar = _SubTabToolbar()
        self.kks_toolbar.hint_label.setText(
            "Ctrl+drag: привязка/слияние | Ctrl+ПКМ: отвязка | Ctrl+2×клик: текст | Shift+клик: подтвердить"
        )
        # Custom KKS buttons
        self.btn_auto_bind_kks = QPushButton("🏷 Авто-привязка KKS")
        self.btn_auto_bind_kks.setToolTip("Привязать подтверждённые KKS к узлам оборудования")
        self.btn_auto_bind_kks.clicked.connect(self._auto_bind_kks)
        self.kks_toolbar.custom_layout.addWidget(self.btn_auto_bind_kks)

        self.btn_confirm_kks = QPushButton("✅ Подтвердить KKS")
        self.btn_confirm_kks.setToolTip("Подтвердить привязку KKS и перейти к диаметрам")
        self.btn_confirm_kks.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; "
            "font-weight: bold; padding: 4px 12px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #45a049; }"
        )
        self.btn_confirm_kks.clicked.connect(self._confirm_kks_step)
        self.kks_toolbar.custom_layout.addWidget(self.btn_confirm_kks)

        self.btn_clear_kks = QPushButton("🗑 Очистить KKS")
        self.btn_clear_kks.setToolTip("Очистить все KKS-привязки")
        self.btn_clear_kks.clicked.connect(self._clear_kks_bindings)
        self.kks_toolbar.custom_layout.addWidget(self.btn_clear_kks)

        self.kks_toolbar.add_clicked.connect(lambda: self._toggle_add_mode(self.kks_toolbar))
        self.kks_toolbar.delete_clicked.connect(lambda: self._toggle_del_mode(self.kks_toolbar))
        self.kks_toolbar.move_clicked.connect(lambda: self._toggle_move_mode(self.kks_toolbar))
        self.kks_toolbar.undo_clicked.connect(self._undo)
        # П3: подвкладка KKS убрана
        # self.sub_tabs.addTab(self.kks_toolbar, "🏷 KKS")

        # === Sub-tab 2: Diameter ===
        self.diam_toolbar = _SubTabToolbar()
        self.diam_toolbar.hint_label.setText(
            "Ctrl+drag: привязка к ребру | Ctrl+ПКМ: отвязка | Ctrl+2×клик: текст"
        )
        self.btn_auto_bind_diam = QPushButton("🔗 Авто-привязка Ø")
        self.btn_auto_bind_diam.setToolTip("Привязать диаметры к рёбрам графа автоматически")
        self.btn_auto_bind_diam.clicked.connect(self._run_auto_bind_diameters)
        self.diam_toolbar.custom_layout.addWidget(self.btn_auto_bind_diam)

        self.btn_confirm_diam = QPushButton("✅ Подтвердить Ø")
        self.btn_confirm_diam.setToolTip("Подтвердить привязку диаметров")
        self.btn_confirm_diam.setStyleSheet(
            "QPushButton { background-color: #2E86C1; color: white; "
            "font-weight: bold; padding: 4px 12px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #2874A6; }"
        )
        self.btn_confirm_diam.clicked.connect(self._confirm_diam_step)
        self.diam_toolbar.custom_layout.addWidget(self.btn_confirm_diam)

        self.btn_clear_diam = QPushButton("🗑 Очистить Ø")
        self.btn_clear_diam.setToolTip("Очистить все привязки диаметров")
        self.btn_clear_diam.clicked.connect(self._clear_diam_bindings)
        self.diam_toolbar.custom_layout.addWidget(self.btn_clear_diam)

        self.diam_toolbar.add_clicked.connect(lambda: self._toggle_add_mode(self.diam_toolbar))
        self.diam_toolbar.delete_clicked.connect(lambda: self._toggle_del_mode(self.diam_toolbar))
        self.diam_toolbar.move_clicked.connect(lambda: self._toggle_move_mode(self.diam_toolbar))
        self.diam_toolbar.undo_clicked.connect(self._undo)
        # П3: подвкладка Диаметр убрана
        # self.sub_tabs.addTab(self.diam_toolbar, "Ø Диаметр")

        # kks/diam-тулбары создаются выше (не показываются) — тулбар OCR построен в начале _setup_ui


        # Editor (shared across sub-tabs)
        self.editor = OcrBindingEditor(self)
        self.editor.setVisible(False)
        self.editor.binding_changed.connect(self._on_binding_changed)
        self.editor.blocks_changed.connect(self._on_blocks_changed)
        self.editor.status_message.connect(self._on_editor_status)
        self.editor.mode_changed.connect(self._on_mode_changed)
        self.editor.validation_exit_requested.connect(self._on_validation_esc)
        if self._project_config_path:
            self.editor._project_config_dir = str(Path(self._project_config_path).parent)
        layout.addWidget(self.editor, stretch=1)

        # Status
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #aaa; padding: 4px 8px;")
        layout.addWidget(self.status_label)

    # =================================================================
    # Sub-tab switching — filter blocks
    # =================================================================

    @Slot(int)
    def _on_sub_tab_changed(self, index: int):
        """П3: подвкладки убраны — всегда простой режим, все блоки видны."""
        self._reset_all_modes()
        self.editor._bind_mode = None
        self.editor.set_block_filter(None)

    def _run_recognize(self):
        """П3: распознать вручную добавленные (пустые) боксы В ФОНЕ — вкладка не блокируется,
        можно продолжать править/привязывать/удалять."""
        if getattr(self, "_recog_thread", None) is not None:
            QMessageBox.information(self, "Распознавание",
                                    "Распознавание уже идёт, подождите.")
            return
        # держим ССЫЛКИ на dict-блоки (устойчиво к сдвигу индексов при правках)
        pending = [
            b for b in self.editor._ocr_blocks
            if b.get("merged_into") != -1
            and not (b.get("text") or "").strip()
            and b.get("bbox")
        ]
        if not pending:
            QMessageBox.information(self, "Распознавание",
                                    "Нет пустых блоков для распознавания.")
            return
        self._recog_pending = pending
        boxes = [[int(v) for v in b["bbox"]] for b in pending]

        # выйти из режима добавления, чтобы можно было сразу править
        self.editor._add_mode = False
        self.btn_add.setChecked(False)
        self.btn_recognize.setEnabled(False)
        self.status_label.setText(
            f"Распознавание {len(boxes)} боксов в фоне… можно продолжать править")

        self._recog_thread = QThread()
        self._recog_worker = _RecognizeWorker(self.api_client, self.uid, boxes)
        self._recog_worker.moveToThread(self._recog_thread)
        self._recog_thread.started.connect(self._recog_worker.run)
        self._recog_worker.finished.connect(self._on_recognize_done)
        self._recog_worker.error.connect(self._on_recognize_error)
        self._recog_thread.start()

    def _cleanup_recog_thread(self):
        t = getattr(self, "_recog_thread", None)
        if t is not None:
            t.quit()
            t.wait()
        self._recog_thread = None
        self._recog_worker = None

    @Slot(list)
    def _on_recognize_done(self, results):
        self._cleanup_recog_thread()
        pending = getattr(self, "_recog_pending", [])
        n = 0
        for b, r in zip(pending, results):
            # блок могли удалить за время распознавания
            if b.get("merged_into") == -1:
                continue
            txt = (r.get("text") or "").strip()
            b["text"] = txt
            if txt:
                n += 1
        self._recog_pending = []
        self.btn_recognize.setEnabled(True)
        self.editor.refresh_ocr_layer()
        self.status_label.setText(f"Распознано {n}/{len(results)} блоков")

    @Slot(str)
    def _on_recognize_error(self, msg):
        self._cleanup_recog_thread()
        self._recog_pending = []
        self.btn_recognize.setEnabled(True)
        self.status_label.setText("Ошибка распознавания")
        QMessageBox.warning(self, "Ошибка", f"Не удалось распознать:\n{msg}")

    # === Панель оформления (⚙) ===
    def _appearance_editor(self):
        return getattr(self, "editor", None)

    def _build_appearance_controls(self, panel):
        self._add_bg_darkness_slider(panel)
        ed = self._appearance_editor()
        if ed is None:
            return
        self._add_color_setting(panel, "Цвет рамки текста", "text_border",
                                QColor(80, 160, 255), ed.set_text_border_color)
        self._add_color_setting(panel, "Цвет рамки бокса", "box_border",
                                QColor(235, 235, 235), ed.set_box_border_color)
        self._add_color_setting(panel, "Цвет ребра", "edge_color",
                                QColor(0, 255, 220), ed.set_edge_color)
        self._add_pct_setting(panel, "Размер подписи", "label_size",
                              float(getattr(ed, "_label_pt", 9)),
                              ed.set_label_font_size, lo=6, hi=24)

    def apply_saved_appearance(self):
        super().apply_saved_appearance()
        ed = self._appearance_editor()
        if ed is None:
            return
        self._apply_saved_color("text_border", QColor(80, 160, 255), ed.set_text_border_color)
        self._apply_saved_color("box_border", QColor(235, 235, 235), ed.set_box_border_color)
        self._apply_saved_color("edge_color", QColor(0, 255, 220), ed.set_edge_color)
        self._apply_saved_pct("label_size", 9.0, ed.set_label_font_size)

    def apply_default_appearance(self):
        super().apply_default_appearance()
        ed = self._appearance_editor()
        if ed is None:
            return
        ed.set_text_border_color(QColor(80, 160, 255))
        ed.set_box_border_color(QColor(235, 235, 235))
        ed.set_edge_color(QColor(0, 255, 220))
        ed.set_label_font_size(9)

    def _classify_blocks_into_groups(self):
        """Разбить classifications на 3 группы по типу для подвкладок.

        Заполняет _block_subtab для блоков у которых ещё нет назначения.
        Блоки, уже назначенные (вручную добавленные), не переназначаются.
        """
        from modules.ocr_validation.result import BlockType

        for cl in self._classifications:
            idx = cl.block_idx
            # Не переназначать блоки с явным назначением
            if idx in self._block_subtab:
                continue
            if cl.block_type == BlockType.KKS:
                self._block_subtab[idx] = "kks"
            elif cl.block_type == BlockType.DIAMETER:
                self._block_subtab[idx] = "diameter"
            else:
                self._block_subtab[idx] = "other"

        # Блоки без classification → "other"
        for idx, block in enumerate(self._ocr_blocks):
            if block.get("merged_into") is not None:
                continue
            if idx not in self._block_subtab:
                self._block_subtab[idx] = "other"

        self._rebuild_indices_from_subtab()

    def _rebuild_indices_from_subtab(self):
        """Пересчитать _kks_indices/_diameter_indices/_other_indices из _block_subtab."""
        self._kks_indices = {idx for idx, st in self._block_subtab.items() if st == "kks"}
        self._diameter_indices = {idx for idx, st in self._block_subtab.items() if st == "diameter"}
        self._other_indices = {idx for idx, st in self._block_subtab.items() if st == "other"}
        logger.info(
            "Block groups: KKS=%d, Diameter=%d, Other=%d",
            len(self._kks_indices), len(self._diameter_indices), len(self._other_indices),
        )

    # =================================================================
    # Stats per sub-tab
    # =================================================================

    def _update_kks_stats(self):
        kks_total = len(self._kks_indices)
        kks_bound = len(self.editor.get_kks_bindings()) if self.editor.isVisible() else 0
        status = f"KKS блоков: {kks_total} | Привязано: {kks_bound}"
        if self._kks_confirmed:
            status += " | ✅ Подтверждено"
        self.kks_toolbar.stats_label.setText(status)

    def _update_diam_stats(self):
        diam_total = len(self._diameter_indices)
        diam_bound = len(self.editor.get_diameter_bindings()) if self.editor.isVisible() else 0
        status = f"Диаметров: {diam_total} | Привязано: {diam_bound}"
        if self._diam_confirmed:
            status += " | ✅ Подтверждено"
        self.diam_toolbar.stats_label.setText(status)

    def _update_other_stats(self):
        other_total = len(self._other_indices)
        bound = len(self._bindings)
        self.stats_label.setText(
            f"Прочих блоков: {other_total} | Привязок: {bound}"
        )

    # =================================================================
    # Editor callbacks
    # =================================================================

    def _on_binding_changed(self):
        self._bindings = self.editor.get_bindings()
        self._saved = False
        self._update_current_stats()

    def _on_blocks_changed(self):
        # П3: подвкладок нет — просто держим все блоки видимыми
        self._ocr_blocks = self.editor._ocr_blocks
        self._saved = False
        self.editor.set_block_filter(None)
        self._update_other_stats()

    def _on_editor_status(self, msg: str):
        self.status_label.setText(msg)

    def _on_mode_changed(self, mode: str):
        self._reset_all_modes()

    def _on_validation_esc(self):
        """Esc в editor — просто сбросить режимы."""
        self._reset_all_modes()

    def _update_current_stats(self):
        # П3: единственная панель — статистика по привязкам
        self._update_other_stats()

    # =================================================================
    # Download
    # =================================================================

    def _start_download(self):
        self._download_thread = QThread()
        self._downloader = _OcrArtifactDownloader(
            self.api_client, self.uid, self.temp_dir
        )
        self._downloader.moveToThread(self._download_thread)

        self._download_thread.started.connect(self._downloader.run)
        self._downloader.finished.connect(self._on_download_finished)
        self._downloader.error.connect(self._on_download_error)
        self._downloader.progress.connect(
            lambda msg: self.loading_label.setText(msg)
        )

        self._download_thread.start()

    @Slot(dict)
    def _on_download_finished(self, artifacts: dict):
        self._download_thread.quit()
        self._download_thread.wait()

        try:
            # Загрузить OCR данные
            with open(artifacts["ocr_result"], encoding="utf-8") as f:
                ocr_raw = json.load(f)

            secondary_blocks = []
            if isinstance(ocr_raw, dict) and "target" in ocr_raw:
                self._ocr_blocks = ocr_raw["target"]
                secondary_blocks = ocr_raw.get("secondary", [])
                logger.info("Loaded new OCR format: %d target, %d secondary",
                            len(self._ocr_blocks), len(secondary_blocks))
            elif isinstance(ocr_raw, list):
                self._ocr_blocks = ocr_raw
                logger.info("Loaded legacy OCR format: %d blocks", len(self._ocr_blocks))
            else:
                raise ValueError(f"Unknown OCR format: {type(ocr_raw)}")

            with open(artifacts["graph"], encoding="utf-8") as f:
                self._graph_data = json.load(f)

            if "binding" in artifacts:
                with open(artifacts["binding"], encoding="utf-8") as f:
                    binding_raw = json.load(f)
                if isinstance(binding_raw, dict) and "bindings" in binding_raw:
                    self._bindings = binding_raw["bindings"]
                    edited_blocks = binding_raw.get("edited_blocks")
                    if edited_blocks:
                        self._ocr_blocks = edited_blocks
                        logger.info("Loaded edited blocks from binding (v2): %d blocks",
                                    len(edited_blocks))
                elif isinstance(binding_raw, list):
                    self._bindings = binding_raw
                else:
                    logger.warning("Unknown binding format: %s", type(binding_raw))

            # Единый граф-JSON: если граф несёт text_blocks/bindings (сделано в
            # ручной правке или ранее здесь) — берём их как источник правды.
            _gtb = self._graph_data.get("text_blocks") or []
            if _gtb:
                _id_to_idx = {}
                self._ocr_blocks = []
                for _i, _tb in enumerate(_gtb):
                    _id_to_idx[_tb.get("id")] = _i
                    self._ocr_blocks.append({
                        "bbox": _tb.get("bbox"),
                        "text": _tb.get("text", ""),
                        "confidence": _tb.get("confidence", 0),
                        "source": _tb.get("source", "graph"),
                    })
                self._bindings = []
                for _gb in (self._graph_data.get("bindings") or []):
                    _idx = _id_to_idx.get(_gb.get("block_id"))
                    if _idx is None:
                        continue
                    _nb = {"ocr_block_idx": _idx, "text": _gb.get("text", ""),
                           "bbox": self._ocr_blocks[_idx]["bbox"]}
                    if _gb.get("node_id"):
                        _nb["node_id"] = _gb["node_id"]
                    elif _gb.get("edge_key"):
                        _nb["edge_key"] = _gb["edge_key"]
                    self._bindings.append(_nb)
                logger.info("Загружено из графа (единый источник): %d блоков / %d привязок",
                            len(self._ocr_blocks), len(self._bindings))

            # COCO
            coco_data = {}
            if "coco" in artifacts:
                with open(artifacts["coco"], encoding="utf-8") as f:
                    coco_data = json.load(f)

            # Предыдущие результаты валидации
            if "ocr_validation" in artifacts:
                try:
                    with open(artifacts["ocr_validation"], encoding="utf-8") as f:
                        val_data = json.load(f)
                    from modules.ocr_validation.result import (
                        BlockClassification, BlockType, MatchQuality,
                        ValidationColor, ConfirmStatus,
                    )
                    self._classifications = []
                    for d in val_data.get("classifications", []):
                        cl = BlockClassification(
                            block_idx=d["block_idx"],
                            block_type=BlockType(d["block_type"]),
                            match_quality=MatchQuality(d["match_quality"]),
                            color=ValidationColor(d["color"]),
                            confirm_status=ConfirmStatus(d.get("confirm_status", "unconfirmed")),
                        )
                        cl.kks_full = d.get("kks_full")
                        cl.kks_block = d.get("kks_block")
                        cl.kks_system = d.get("kks_system")
                        cl.kks_fn = d.get("kks_fn")
                        cl.kks_unit = d.get("kks_unit")
                        cl.kks_num = d.get("kks_num")
                        cl.kks_suffix = d.get("kks_suffix")
                        cl.kks_span = tuple(d["kks_span"]) if d.get("kks_span") else None
                        cl.diameter_text = d.get("diameter_text")
                        cl.diameter_value = d.get("diameter_value")
                        cl.diameter_prefix = d.get("diameter_prefix")
                        cl.diameter_suffix = d.get("diameter_suffix")
                        cl.remaining_text = d.get("remaining_text", "")
                        cl.original_text = d.get("original_text", "")
                        cl.corrected_text = d.get("corrected_text", "")
                        self._classifications.append(cl)
                    logger.info("Loaded %d previous classifications", len(self._classifications))
                except Exception as exc:
                    logger.warning("Failed to load ocr_validation: %s", exc)

            # П3: подтянуть контуры узлов (реальная форма после этапа контуров)
            node_contours = {}
            try:
                cpath = self.temp_dir / "contours_validated.json"
                self.api_client.download_contours_validated(self.uid, cpath)
                cdata = json.load(open(cpath, encoding="utf-8"))
                for cn in cdata.get("nodes", []):
                    ann = cn.get("ann_id")
                    poly = cn.get("polygon_validated") or cn.get("polygon_auto")
                    if ann is not None and poly and len(poly) >= 3:
                        node_contours[ann] = poly
                logger.info("Контуры узлов подтянуты: %d", len(node_contours))
            except Exception as exc:
                logger.info("Контуры не подтянуты (нет/ошибка): %s", exc)

            # Загрузить данные в визуальный редактор
            image_path = str(artifacts["original_image"])
            self.editor.load_data(
                image_path,
                self._ocr_blocks,
                self._graph_data,
                self._bindings,
                coco_data=coco_data,
                secondary_blocks=secondary_blocks,
                node_contours=node_contours,
            )
            self.apply_saved_appearance()

            # П3: авто-классификация KKS/диаметр и цветовая валидация убраны
            self.loading_label.setVisible(False)
            self.editor.setVisible(True)
            self._on_sub_tab_changed(0)

        except Exception as exc:
            logger.error("Failed to load OCR data: %s", exc, exc_info=True)
            QMessageBox.critical(
                self, "Ошибка",
                f"Не удалось загрузить OCR данные:\n{exc}"
            )

    @Slot(str)
    def _on_download_error(self, error_msg: str):
        self._download_thread.quit()
        self._download_thread.wait()
        if self._retry_count < 10:
            self._retry_count += 1
            self.loading_label.setText(
                f"⏳ OCR в процессе... (попытка {self._retry_count}/10)")
            QTimer.singleShot(5000, self._start_download)
        else:
            self.loading_label.setText(f"❌ Ошибка: {error_msg}")

    # =================================================================
    # Auto-classification (runs at load)
    # =================================================================

    def _run_auto_classification(self):
        """Классифицировать все OCR-блоки автоматически при загрузке."""
        if not self._ocr_blocks:
            return

        try:
            from modules.kks_binding.matcher import KksMatcher
            from modules.ocr_validation.classifier import OcrBlockClassifier
            from modules.text_binding.matcher import DiameterMatcher

            # Попробовать domain_profile (v2.0) — единый конфиг
            bcfg = self._try_load_domain_binding_config()
            if bcfg:
                self._kks_config = bcfg
                self._domain_binding_config = bcfg
                kks_matcher = KksMatcher(bcfg)
                if self._diameter_matcher is None:
                    self._diameter_matcher = DiameterMatcher(bcfg)
                logger.info("Auto-classification: using DomainBindingConfig")
            else:
                # Fallback: legacy configs
                from modules.kks_binding.config import KksConfig
                kks_config_path = self._resolve_config_path("kks_config.yaml")
                self._kks_config = KksConfig.from_yaml(kks_config_path)
                kks_matcher = KksMatcher(self._kks_config)
                if self._diameter_matcher is None:
                    cfg = self._load_text_recognition_config()
                    self._diameter_matcher = DiameterMatcher(cfg.diameter)

            # Classify
            classifier = OcrBlockClassifier(self._diameter_matcher, kks_matcher)
            report = classifier.classify_all(self._ocr_blocks)

            self._classifications = report.classifications
            self.editor._ocr_classifier = classifier

            self.status_label.setText(
                f"Классификация: KKS {report.kks_exact}✓ {report.kks_corrected}~ | "
                f"Ø {report.diameter_exact + report.diameter_corrected} | "
                f"? {report.orange_count} | текст {report.annotation_count}"
            )
            logger.info(
                "Auto-classification: %d KKS exact, %d corrected, %d diameters, %d orange, %d annotations",
                report.kks_exact, report.kks_corrected,
                report.diameter_exact + report.diameter_corrected,
                report.orange_count, report.annotation_count,
            )

        except ImportError as exc:
            logger.warning("Classification modules not available: %s", exc)
        except Exception as exc:
            logger.error("Auto-classification failed: %s", exc, exc_info=True)

    def _get_classifier(self):
        """Получить или создать OcrBlockClassifier."""
        if hasattr(self.editor, '_ocr_classifier') and self.editor._ocr_classifier is not None:
            return self.editor._ocr_classifier
        try:
            from modules.kks_binding.matcher import KksMatcher
            from modules.ocr_validation.classifier import OcrBlockClassifier
            from modules.text_binding.matcher import DiameterMatcher

            self._ensure_kks_config()
            kks_matcher = KksMatcher(self._kks_config)
            if self._diameter_matcher is None:
                bcfg = getattr(self, '_domain_binding_config', None)
                if bcfg:
                    self._diameter_matcher = DiameterMatcher(bcfg)
                else:
                    cfg = self._load_text_recognition_config()
                    self._diameter_matcher = DiameterMatcher(cfg.diameter)
            classifier = OcrBlockClassifier(self._diameter_matcher, kks_matcher)
            self.editor._ocr_classifier = classifier
            return classifier
        except Exception as exc:
            logger.warning("Cannot create classifier: %s", exc)
            return None

    def _load_text_recognition_config(self):
        """Загрузить TextRecognitionConfig."""
        from modules.text_binding.config import TextRecognitionConfig
        if self._project_config_path:
            try:
                return TextRecognitionConfig.from_project_yaml(self._project_config_path)
            except Exception as exc:
                logger.warning("Failed to load config from %s: %s", self._project_config_path, exc)
        # Fallback: поиск project yaml рядом с kks_config
        from pathlib import Path as _Path
        kks_dir = _Path(self._resolve_config_path("kks_config.yaml")).parent
        project_yamls = list(kks_dir.glob("*.yaml"))
        project_yamls = [y for y in project_yamls
                         if "kks_config" not in y.name
                         and "class_to_kks" not in y.name]
        for y in project_yamls:
            test_cfg = TextRecognitionConfig.from_project_yaml(str(y))
            if test_cfg.diameter.patterns:
                logger.info("Loaded diameter config from %s", y)
                return test_cfg
        return TextRecognitionConfig()

    # =================================================================
    # KKS sub-tab actions
    # =================================================================

    def _auto_bind_kks(self):
        """Привязать подтверждённые KKS к equipment nodes."""
        if self.editor._validation_results:
            self._classifications = self.editor._validation_results
        if not self._classifications:
            QMessageBox.information(self, "Info", "Нет классификации блоков")
            return

        from modules.ocr_validation.result import ConfirmStatus, BlockType

        # Все KKS-блоки уже валидированы по конфигу — автоматически подтверждаем
        kks_count = 0
        for cl in self._classifications:
            if cl.block_type == BlockType.KKS:
                cl.confirm_status = ConfirmStatus.CONFIRMED
                kks_count += 1

        if kks_count == 0:
            QMessageBox.information(
                self, "Info",
                "Нет KKS-блоков для привязки."
            )
            return

        try:
            from modules.kks_binding.config import ClassToKksConfig
            from modules.kks_binding.binder import KksBinder

            self._ensure_kks_config()

            # ClassToKksConfig: из domain_profile (v2.0) или legacy yaml
            bcfg = getattr(self, '_domain_binding_config', None)
            if bcfg:
                self._cls_to_kks_config = self._domain_binding_to_cls_config(bcfg)
            else:
                cls_config_path = self._resolve_config_path("class_to_kks_config.yaml")
                self._cls_to_kks_config = ClassToKksConfig.from_yaml(cls_config_path)

            binder = KksBinder(self._kks_config, self._cls_to_kks_config)
            report = binder.bind(
                self._classifications,
                self._ocr_blocks,
                self._graph_data.get("nodes", []),
            )

            kks_dicts = []
            for kb in report.bindings:
                kks_dicts.append({
                    "ocr_block_idx": kb.ocr_block_idx,
                    "node_id": kb.node_id,
                    "node_class": kb.node_class,
                    "kks_full": kb.kks_full,
                    "confidence": kb.confidence,
                    "distance": kb.distance,
                    "unit_valid": kb.unit_valid,
                    "validation_msg": kb.validation_msg,
                    "reclassify_to": kb.reclassify_to,
                })

            self.editor.set_kks_bindings(kks_dicts)
            self._saved = False

            parts = [f"KKS: {report.bound} привязано"]
            if report.skipped_br:
                parts.append(f"{report.skipped_br} BR пропущено")
            if report.unit_mismatches:
                parts.append(f"{report.unit_mismatches} несовпадений unit↔class")
            if report.reclassified:
                parts.append(f"{report.reclassified} реклассифицировано")
            self.status_label.setText(" | ".join(parts))

        except ImportError as exc:
            logger.warning("KKS binding modules not available: %s", exc)
            QMessageBox.warning(self, "Ошибка", f"Модули KKS недоступны:\n{exc}")
        except Exception as exc:
            logger.error("KKS binding failed: %s", exc, exc_info=True)
            QMessageBox.warning(self, "Ошибка", f"Привязка KKS не удалась:\n{exc}")

    def _confirm_kks_step(self):
        """Подтвердить KKS-привязку, перейти к диаметрам."""
        self._kks_confirmed = True
        self._saved = False
        self._update_kks_stats()
        self.status_label.setText("✅ KKS привязка подтверждена")
        # Перейти к следующей подвкладке
        pass  # (подвкладки убраны)

    # =================================================================
    # Diameter sub-tab actions
    # =================================================================

    def _run_auto_bind_diameters(self):
        """Привязать диаметры к рёбрам графа."""
        if not self._ocr_blocks:
            QMessageBox.warning(self, "Привязка Ø", "Нет OCR-блоков для привязки диаметров")
            return
        self._auto_bind_diameters_data = None
        self._auto_bind_diameters()

        if self._auto_bind_diameters_data:
            d, p, c = self._auto_bind_diameters_data
            self.editor.set_diameter_bindings(d, p, c)
            self._auto_bind_diameters_data = None
            self._saved = False
            self._update_diam_stats()

    def _auto_bind_diameters(self):
        """Автоматическая привязка диаметров OCR к рёбрам графа через TextBinder."""
        edges = self._graph_data.get("links", [])
        if not edges or not self._ocr_blocks:
            return

        try:
            from modules.text_binding.config import TextRecognitionConfig
            from modules.text_binding.binder import TextBinder
            from modules.text_binding.matcher import DiameterMatcher

            if self._project_config_path:
                try:
                    cfg = TextRecognitionConfig.from_project_yaml(self._project_config_path)
                except Exception as exc:
                    logger.warning("Failed to load config from %s: %s — using defaults",
                                   self._project_config_path, exc)
                    cfg = TextRecognitionConfig()
            else:
                logger.warning("No project_config_path — using default TextRecognitionConfig (no patterns)")
                cfg = TextRecognitionConfig()

            # domain_profile (v2.0) может содержать ⌀/Ø паттерны
            # которых нет в legacy text_recognition
            bcfg = getattr(self, '_domain_binding_config', None)

            if not cfg.diameter.patterns and not bcfg:
                logger.info("No diameter patterns configured, skipping auto-bind")
                return

            binder = TextBinder(cfg)

            # Override diameter matcher если domain_profile имеет свои паттерны
            if bcfg and "diameter" in bcfg.code_types:
                domain_diam_matcher = DiameterMatcher(bcfg)
                binder._diameter_matcher = domain_diam_matcher
                logger.info("Using diameter patterns from domain_profile")

            report = binder.bind_diameters(self._ocr_blocks, edges)

            self.editor._text_binder = binder
            self.editor._diameter_matcher = binder._diameter_matcher
            self._diameter_matcher = binder._diameter_matcher

            if report.bindings:
                import re as _re
                diameter_dicts = []
                split_happened = False

                real_blocks = [
                    self._ocr_blocks[d.ocr_block_idx]
                    for d in report.bindings
                    if d.ocr_block_idx < len(self._ocr_blocks)
                ]
                if real_blocks:
                    avg_w = sum(b["bbox"][2] - b["bbox"][0] for b in real_blocks) / len(real_blocks)
                    avg_h = sum(b["bbox"][3] - b["bbox"][1] for b in real_blocks) / len(real_blocks)
                else:
                    avg_w, avg_h = 60, 25
                avg_w = max(avg_w, 40)
                avg_h = max(avg_h, 20)

                for db in report.bindings:
                    ocr_idx = db.ocr_block_idx
                    block = self._ocr_blocks[ocr_idx]
                    full_text = block.get("text", "").strip()

                    esc_prefix = _re.escape(db.prefix)
                    esc_suffix = _re.escape(db.suffix) if db.suffix else ""
                    remove_pat = esc_prefix + r'\s*' + str(db.diameter) + (r'\s*' + esc_suffix if esc_suffix else '')
                    remaining = _re.sub(remove_pat, '', full_text, count=1, flags=_re.IGNORECASE).strip()
                    remaining = _re.sub(r'^[\s,;.\-]+|[\s,;.\-]+$', '', remaining)

                    diam_ocr_idx = ocr_idx  # по умолчанию — исходный блок
                    if remaining:
                        block["text"] = remaining
                        bbox = block.get("bbox", [0, 0, 0, 0])
                        bx2 = bbox[2]
                        by1 = bbox[1]
                        new_bbox = [bx2 + 2, by1, bx2 + 2 + avg_w, by1 + avg_h]
                        new_idx = len(self._ocr_blocks)
                        self._ocr_blocks.append({
                            "bbox": new_bbox, "text": db.text,
                            "confidence": db.confidence,
                        })
                        # Новый блок → diameter subtab
                        self._block_subtab[new_idx] = "diameter"
                        split_happened = True
                        diam_ocr_idx = new_idx
                        logger.info("Auto-split OCR[%d]: '%s' → '%s' + '%s'",
                                    ocr_idx, full_text, remaining, db.text)

                    diameter_dicts.append({
                        "ocr_block_idx": diam_ocr_idx,
                        "edge_idx": db.edge_idx,
                        "edge_id": db.edge_id,
                        "edge_key": _sorted_edge_key(db.edge_key),
                        "text": db.text,
                        "prefix": db.prefix,
                        "diameter": db.diameter,
                        "suffix": db.suffix,
                        "confidence": db.confidence,
                    })

                if split_happened:
                    self.editor._ocr_blocks = self._ocr_blocks
                    for items_dict in (self.editor._ocr_items, self.editor._ocr_text_items,
                                       self.editor._ocr_text_bg_items, self.editor._ocr_inner_text_items):
                        for item in items_dict.values():
                            self.editor.scene.removeItem(item)
                        items_dict.clear()
                    self.editor._draw_ocr_blocks()
                    self.editor._redraw_all_colors()
                    # Rebuild indices after split
                    self._rebuild_indices_from_subtab()
                    logger.info("Editor notified about split blocks")

                nodes = self._graph_data.get("nodes", [])
                prop_report = binder.propagate_diameters(nodes, edges, report.bindings)

                propagated_dicts = []
                for pd in prop_report.propagated:
                    propagated_dicts.append({
                        "edge_idx": pd.edge_idx,
                        "edge_id": pd.edge_id,
                        "edge_key": _sorted_edge_key(pd.edge_key),
                        "text": pd.text,
                        "prefix": pd.prefix,
                        "diameter": pd.diameter,
                        "suffix": pd.suffix,
                        "confidence": pd.confidence,
                        "propagated": True,
                    })

                conflict_dicts = []
                for cf in prop_report.conflicts:
                    conflict_dicts.append({
                        "edge_idx": cf.edge_idx,
                        "edge_id": cf.edge_id,
                        "edge_key": _sorted_edge_key(cf.edge_key),
                        "candidates": cf.candidates,
                    })

                self._auto_bind_diameters_data = (diameter_dicts, propagated_dicts, conflict_dicts)
                conflicts_msg = f", {len(conflict_dicts)} конфликтов" if conflict_dicts else ""
                self.status_label.setText(
                    f"Диаметры: {report.bound_count} OCR + "
                    f"{len(prop_report.propagated)} распространено{conflicts_msg}"
                )
            else:
                logger.info("Diameter auto-bind: no bindings found")

        except ImportError as exc:
            logger.warning("text_binding module not available: %s", exc)
        except Exception as exc:
            logger.error("Diameter auto-bind failed: %s", exc, exc_info=True)

    def _confirm_diam_step(self):
        """Подтвердить привязку диаметров, перейти к «Другое»."""
        self._diam_confirmed = True
        self._saved = False
        self._update_diam_stats()
        self.status_label.setText("✅ Привязка диаметров подтверждена")
        pass  # (подвкладки убраны)

    # =================================================================
    # Other sub-tab / clear / modes
    # =================================================================

    def _clear_kks_bindings(self):
        """Очистить только KKS-привязки."""
        reply = QMessageBox.question(
            self, "Очистка KKS",
            "Удалить все KKS-привязки?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._kks_confirmed = False
            self._saved = False
            if self.editor.isVisible():
                self.editor.set_kks_bindings([])
            self._update_current_stats()

    def _clear_diam_bindings(self):
        """Очистить только привязки диаметров."""
        reply = QMessageBox.question(
            self, "Очистка Ø",
            "Удалить все привязки диаметров?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._auto_bind_diameters_data = None
            self._diam_confirmed = False
            self._saved = False
            if self.editor.isVisible():
                self.editor.set_diameter_bindings([], [])
            self._update_current_stats()

    def _clear_other_bindings(self):
        """Очистить прочие привязки (text→node)."""
        reply = QMessageBox.question(
            self, "Очистка",
            "Удалить все прочие привязки?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._bindings = []
            self._saved = False
            if self.editor.isVisible():
                self.editor.clear_bindings()
            self._update_current_stats()

    def _reset_all_modes(self):
        """Сброс всех режимов → idle (pan)."""
        for tb in (self.kks_toolbar, self.diam_toolbar):
            tb.reset_modes()
        self.btn_add.setChecked(False)
        self.editor._add_mode = False
        self.editor._del_mode = False
        self.editor._move_mode = False
        from PySide6.QtWidgets import QGraphicsView
        self.editor.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.editor.setCursor(Qt.CursorShape.ArrowCursor)

    def _toggle_add_mode_btn(self):
        """Toggle режима «Добавить бокс» (кнопка в главном тулбаре)."""
        active = self.btn_add.isChecked()
        self._reset_all_modes()
        if active:
            self.btn_add.setChecked(True)
            self.editor._add_mode = True
            from PySide6.QtWidgets import QGraphicsView
            self.editor.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.editor.setCursor(Qt.CursorShape.CrossCursor)
            self.status_label.setText("Режим добавления: обведите текст прямоугольником")

    def _toggle_add_mode(self, toolbar: _SubTabToolbar):
        active = toolbar.btn_add.isChecked()
        self._reset_all_modes()
        if active:
            toolbar.btn_add.setChecked(True)
            self.editor._add_mode = True
            from PySide6.QtWidgets import QGraphicsView
            self.editor.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.editor.setCursor(Qt.CursorShape.CrossCursor)
            self.status_label.setText("Режим добавления: кликните на сцене")

    def _toggle_del_mode(self, toolbar: _SubTabToolbar):
        active = toolbar.btn_del.isChecked()
        self._reset_all_modes()
        if active:
            toolbar.btn_del.setChecked(True)
            self.editor._del_mode = True
            from PySide6.QtWidgets import QGraphicsView
            self.editor.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.editor.setCursor(Qt.CursorShape.PointingHandCursor)
            self.status_label.setText("Режим удаления: кликните на бокс")

    def _toggle_move_mode(self, toolbar: _SubTabToolbar):
        active = toolbar.btn_move.isChecked()
        self._reset_all_modes()
        if active:
            toolbar.btn_move.setChecked(True)
            self.editor._move_mode = True
            from PySide6.QtWidgets import QGraphicsView
            self.editor.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.editor.setCursor(Qt.CursorShape.SizeAllCursor)
            self.status_label.setText("Режим перемещения: перетащите бокс")

    # =================================================================
    # Restore bindings from graph
    # =================================================================

    def _restore_bindings_from_graph(self):
        """Восстановить KKS и diameter привязки из graph_validated.json."""
        nodes = self._graph_data.get("nodes", [])
        edges = self._graph_data.get("links", [])
        ocr_blocks = self._ocr_blocks
        n_blocks = len(ocr_blocks)

        # --- KKS ---
        kks_dicts = []
        for node in nodes:
            kks_full = node.get("kks_full")
            if not kks_full:
                continue
            node_id = node.get("id", "")
            ocr_idx = node.get("kks_ocr_block_idx", -1)
            if ocr_idx < 0 or ocr_idx >= n_blocks:
                ocr_idx = self._find_ocr_near_node(node, ocr_blocks)
            if ocr_idx is None or ocr_idx < 0:
                continue
            kks_dicts.append({
                "ocr_block_idx": ocr_idx,
                "node_id": node_id,
                "node_class": node.get("class_name", ""),
                "kks_full": kks_full,
                "confidence": node.get("kks_confidence", 1.0),
                "distance": 0.0,
                "unit_valid": True,
                "validation_msg": "",
                "reclassify_to": None,
            })

        if kks_dicts:
            self.editor.set_kks_bindings(kks_dicts)
            logger.info("Restored %d KKS bindings from graph", len(kks_dicts))

        # --- Диаметры ---
        diameter_dicts = []
        seen_edge_keys = set()
        for edge_idx, edge in enumerate(edges):
            diam_text = edge.get("diameter_text")
            if not diam_text:
                continue
            src, tgt = edge.get("source", ""), edge.get("target", "")
            edge_key = f"{min(src, tgt)}|{max(src, tgt)}"
            if edge_key in seen_edge_keys:
                continue
            seen_edge_keys.add(edge_key)

            # Найти OCR-блок соответствующий этому диаметру
            ocr_block_idx = self._find_diameter_ocr_block(
                edge, diam_text, edge.get("diameter_value", 0), ocr_blocks
            )

            diameter_dicts.append({
                "edge_idx": edge_idx,
                "edge_id": edge.get("id", ""),
                "edge_key": edge_key,
                "text": diam_text,
                "prefix": edge.get("diameter_prefix", ""),
                "diameter": edge.get("diameter_value", 0),
                "suffix": edge.get("diameter_suffix", ""),
                "confidence": edge.get("diameter_confidence", 1.0),
                "propagated": edge.get("diameter_propagated", False),
                "ocr_block_idx": ocr_block_idx,
            })

        if diameter_dicts:
            direct = [d for d in diameter_dicts if not d.get("propagated")]
            propagated = [d for d in diameter_dicts if d.get("propagated")]
            self.editor.set_diameter_bindings(direct, propagated)
            logger.info("Restored %d diameter bindings from graph (%d direct, %d propagated)",
                        len(diameter_dicts), len(direct), len(propagated))

    def _find_ocr_near_node(self, node: dict, ocr_blocks: list) -> Optional[int]:
        """Fallback: найти ближайший OCR-блок к узлу."""
        import re as _re
        _diam_re = _re.compile(r'^(Dy|DN|Ду|ДУ)\s*\d', _re.IGNORECASE)
        node_bbox = node.get("bbox")
        if not node_bbox or len(node_bbox) != 4:
            return None
        best_idx = None
        best_dist = 100
        for idx, block in enumerate(ocr_blocks):
            if block.get("merged_into") is not None:
                continue
            text = block.get("text", "").strip()
            if len(text) < 3:
                continue
            if _diam_re.match(text):
                continue
            bbox = block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            dist = _bbox_to_bbox_dist(bbox, node_bbox)
            if dist < best_dist:
                best_dist = dist
                best_idx = idx
        return best_idx

    def _find_diameter_ocr_block(self, edge: dict, diam_text: str,
                                  diam_value: int, ocr_blocks: list) -> Optional[int]:
        """Найти OCR-блок диаметра, соответствующий ребру.

        Ищет блок с текстом, содержащим значение диаметра (например 'Dy100'),
        ближайший к середине ребра.
        """
        import re as _re

        # Середина ребра
        sp = edge.get("source_point")
        tp = edge.get("target_point")
        if not sp or not tp:
            return None
        wps = edge.get("waypoints", [])
        if wps:
            mid = wps[len(wps) // 2]
            emx, emy = mid[1], mid[0]
        else:
            emx = (sp[1] + tp[1]) / 2
            emy = (sp[0] + tp[0]) / 2

        # Паттерн для поиска: текст содержит число диаметра
        diam_str = str(diam_value) if diam_value else ""
        best_idx = None
        best_dist = float("inf")

        for idx, block in enumerate(ocr_blocks):
            if block.get("merged_into") is not None:
                continue
            text = block.get("text", "").strip()
            if not text:
                continue
            # Текст должен содержать значение диаметра
            if diam_str and diam_str not in text:
                continue
            # Текст должен быть похож на диаметр (Dy/DN/Ду/Dv + число)
            if not _re.search(r'(?:Dy|DN|Ду|ДУ|Dv)\s*\d', text, _re.IGNORECASE):
                continue
            bbox = block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            bcx = (bbox[0] + bbox[2]) / 2
            bcy = (bbox[1] + bbox[3]) / 2
            dist = ((bcx - emx) ** 2 + (bcy - emy) ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_idx = idx

        return best_idx

    # =================================================================
    # Config helpers
    # =================================================================

    def _try_load_domain_binding_config(self):
        """Попробовать загрузить DomainBindingConfig из domain_profile.yaml.

        Возвращает DomainBindingConfig или None (если нет domain_profile).
        Это нужно чтобы UI работал и с KKS (Latin), и с кириллическим профилем.
        """
        try:
            from modules.binding.config import DomainBindingConfig
            from pathlib import Path as _Path

            # 1. Через project config → ocr.domain_profile_path
            if self._project_config_path:
                import yaml
                with open(self._project_config_path, encoding="utf-8") as f:
                    proj = yaml.safe_load(f)
                dp_path = (proj.get("ocr", {}) or {}).get("domain_profile_path")
                if dp_path:
                    p = _Path(dp_path)
                    if not p.is_absolute():
                        p = _Path(self._project_config_path).parent / dp_path
                    if not p.exists():
                        # Попробовать от корня проекта
                        p = _Path(dp_path)
                    if p.exists():
                        cfg = DomainBindingConfig.from_yaml(str(p))
                        if cfg.code_types:
                            logger.info("Loaded DomainBindingConfig from %s (%d code_types)",
                                        p, len(cfg.code_types))
                            return cfg

            # 2. Auto-detect domain_profile.yaml рядом с project config
            if self._project_config_path:
                dp = _Path(self._project_config_path).parent / "domain_profile.yaml"
                if dp.exists():
                    cfg = DomainBindingConfig.from_yaml(str(dp))
                    if cfg.code_types:
                        logger.info("Auto-detected DomainBindingConfig from %s", dp)
                        return cfg

        except Exception as exc:
            logger.debug("DomainBindingConfig not available: %s", exc)
        return None

    def _domain_binding_to_cls_config(self, bcfg):
        """Адаптер: DomainBindingConfig → ClassToKksConfig для legacy KksBinder."""
        from modules.kks_binding.config import ClassToKksConfig, ClassKksRule
        cls_cfg = ClassToKksConfig()
        cls_cfg.unit_to_classes = dict(bcfg.unit_to_classes)
        for class_name, rule in bcfg.class_rules.items():
            cls_cfg.class_to_kks[class_name] = ClassKksRule(
                description="",
                kks_target=rule.kks_target,
                expected_units=list(rule.expected_units),
                reclassify_rules=dict(rule.reclassify_rules),
            )
        return cls_cfg

    def _ensure_kks_config(self):
        if self._kks_config is not None:
            return
        # Попробовать domain_profile (v2.0) — для кириллического и любого нового профиля
        bcfg = self._try_load_domain_binding_config()
        if bcfg:
            self._kks_config = bcfg  # KksMatcher принимает DomainBindingConfig
            self._domain_binding_config = bcfg
            return
        # Fallback: legacy kks_config.yaml
        from modules.kks_binding.config import KksConfig
        kks_config_path = self._resolve_config_path("kks_config.yaml")
        self._kks_config = KksConfig.from_yaml(kks_config_path)

    def _resolve_config_path(self, filename: str) -> str:
        from pathlib import Path as _Path
        if self._project_config_path:
            p = _Path(self._project_config_path).parent / filename
            if p.exists():
                return str(p)
        p = _Path("configs/projects") / filename
        if p.exists():
            return str(p)
        return filename

    # =================================================================
    # Save & Confirm
    # =================================================================

    def _save_binding(self) -> bool:
        """Сохранить привязки и обновлённые OCR-блоки на сервер."""
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)

            self._bindings = self.editor.get_bindings()
            ocr_blocks = self.editor._ocr_blocks

            # === 1. Подготовить активные блоки и idx_map ===
            active_blocks = []
            idx_map = {}
            for i, block in enumerate(ocr_blocks):
                if block.get("merged_into") is not None:
                    continue
                # П3: пустые боксы (без текста) не сохраняем
                if not (block.get("text") or "").strip():
                    continue
                idx_map[i] = len(active_blocks)
                active_blocks.append({
                    "bbox": block.get("bbox"),
                    "text": block.get("text", ""),
                    "confidence": block.get("confidence", 0),
                    "source": block.get("source", "unknown"),
                    "origin": block.get("origin", "unknown"),
                })

            # === 2. Сохранить привязки с пересчитанными индексами ===
            clean_bindings = []
            for b in self._bindings:
                old_idx = b.get("ocr_block_idx")
                if old_idx is None or old_idx not in idx_map:
                    continue
                cb = dict(b)
                cb["ocr_block_idx"] = idx_map[old_idx]
                new_idx = idx_map[old_idx]
                cb["text"] = active_blocks[new_idx]["text"]
                cb["bbox"] = active_blocks[new_idx]["bbox"]
                clean_bindings.append(cb)

            binding_payload = {
                "version": 2,
                "bindings": clean_bindings,
                "edited_blocks": active_blocks,
            }
            binding_path = self.temp_dir / "ocr_binding.json"
            with open(binding_path, "w", encoding="utf-8") as f:
                json.dump(binding_payload, f, ensure_ascii=False, indent=2)
            self.api_client.save_ocr_binding(self.uid, binding_path)

            # === 3. Записать диаметры в рёбра графа ===
            diameter_bindings = self.editor.get_diameter_bindings()
            propagated = self.editor._propagated_diameters
            diameter_count = 0
            if self._graph_data.get("links"):
                diam_by_edge_key = {}
                for db in (diameter_bindings or []):
                    ek = db.get("edge_key")
                    if ek:
                        diam_by_edge_key[ek] = db
                for pd in (propagated or []):
                    ek = pd.get("edge_key")
                    if ek and ek not in diam_by_edge_key:
                        diam_by_edge_key[ek] = pd

                for edge in self._graph_data["links"]:
                    src, tgt = edge.get("source", ""), edge.get("target", "")
                    ek = f"{min(src, tgt)}|{max(src, tgt)}"
                    if ek in diam_by_edge_key:
                        db = diam_by_edge_key[ek]
                        edge["diameter_text"] = db.get("text", "")
                        edge["diameter_value"] = db.get("diameter", 0)
                        edge["diameter_prefix"] = db.get("prefix", "")
                        edge["diameter_suffix"] = db.get("suffix", "")
                        edge["diameter_confidence"] = db.get("confidence", 1.0)
                        edge["diameter_propagated"] = bool(db.get("propagated", False))
                        edge.pop("diameter_ocr_block_idx", None)
                        diameter_count += 1
                    else:
                        for key in ("diameter_text", "diameter_value",
                                    "diameter_prefix", "diameter_suffix",
                                    "diameter_confidence", "diameter_propagated"):
                            edge.pop(key, None)

            # === 4. Записать KKS в узлы графа ===
            kks_bindings = self.editor.get_kks_bindings()
            kks_count = 0
            node_by_id = {n["id"]: n for n in self._graph_data.get("nodes", [])}
            for kb in (kks_bindings or []):
                node = node_by_id.get(kb.get("node_id"))
                if not node:
                    continue
                node["kks_full"] = kb.get("kks_full", "")
                node["kks_confidence"] = kb.get("confidence", 1.0)
                for _old_key in ("kks_block", "kks_system", "kks_fn",
                                 "kks_unit", "kks_num", "kks_suffix",
                                 "label"):
                    node.pop(_old_key, None)
                old_idx = kb.get("ocr_block_idx")
                node["kks_ocr_block_idx"] = idx_map.get(old_idx, -1)
                if kb.get("reclassify_to"):
                    node["class_name"] = kb["reclassify_to"]
                kks_count += 1

            # === Единый граф-JSON: text_blocks + bindings (общий источник) ===
            # Ручная правка читает эти ключи → её «ОКР привязка» увидит то,
            # что сделано здесь, в «бусине».
            self._graph_data["text_blocks"] = [
                {
                    "id": f"block_{i + 1}",
                    "bbox": b.get("bbox"),
                    "text": b.get("text", ""),
                    "confidence": b.get("confidence", 0),
                    "source": b.get("source", "unknown"),
                    "merged_into": None,
                }
                for i, b in enumerate(active_blocks)
            ]
            _g_bindings = []
            for cb in clean_bindings:
                _bid = f"block_{cb.get('ocr_block_idx', -1) + 1}"
                if cb.get("node_id"):
                    _g_bindings.append({"block_id": _bid, "node_id": cb["node_id"],
                                        "kind": "node", "text": cb.get("text", "")})
                elif cb.get("edge_key"):
                    _g_bindings.append({"block_id": _bid, "edge_key": cb["edge_key"],
                                        "kind": "edge", "text": cb.get("text", "")})
            self._graph_data["bindings"] = _g_bindings

            # === 5. Сохранить обновлённый граф ===
            if self._graph_data.get("links") or kks_count or active_blocks:
                graph_path = self.temp_dir / "graph_validated.json"
                with open(graph_path, "w", encoding="utf-8") as f:
                    json.dump(self._graph_data, f, ensure_ascii=False, indent=2)
                self.api_client.upload_validated_graph(self.uid, graph_path)

            # === 6. Сохранить ocr_validation.json ===
            if self.editor._validation_results:
                self._classifications = self.editor._validation_results
            if self._classifications:
                from dataclasses import asdict
                val_path = self.temp_dir / "ocr_validation.json"
                val_data = {
                    "version": 1,
                    "classifications": [asdict(cl) for cl in self._classifications],
                }
                with open(val_path, "w", encoding="utf-8") as f:
                    json.dump(val_data, f, ensure_ascii=False, indent=2)
                try:
                    self.api_client.save_ocr_validation(self.uid, val_path)
                except Exception as exc:
                    logger.warning("Failed to upload ocr_validation: %s", exc)

            self._saved = True
            parts = [
                f"{len(clean_bindings)} привязок",
                f"{len(active_blocks)} блоков",
            ]
            if diameter_count:
                parts.append(f"{diameter_count} диаметров")
            if kks_count:
                parts.append(f"{kks_count} KKS")
            self.status_label.setText(f"✅ Сохранено: {', '.join(parts)}")
            return True

        except Exception as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось сохранить привязки:\n{exc}"
            )
            return False
        finally:
            QApplication.restoreOverrideCursor()

    @Slot()
    def _on_confirm(self):
        """Финальное подтверждение: сохранить + применить к графу + emit confirmed."""
        if not self._save_binding():
            return

        try:
            result = self.api_client.apply_ocr_binding(self.uid)
            updated = result.get("updated_nodes", 0)
            self.status_message.emit(f"✅ Привязки применены ({updated} узлов обновлено)")
        except APIError as exc:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось применить привязки:\n{exc.message}"
            )
            return

        self.confirmed.emit()

    # =================================================================
    # Undo
    # =================================================================

    def _undo(self):
        """Отменить последнее действие в editor."""
        if self.editor and self.editor.isVisible():
            self.editor._undo()

    # =================================================================
    # Workspace integration
    # =================================================================

    def has_unsaved_changes(self) -> bool:
        return not self._saved

    def _save_graph(self) -> bool:
        return self._save_binding()

    # =================================================================
    # Public accessors
    # =================================================================

    def get_propagated_diameters(self) -> list[dict]:
        if self.editor.isVisible():
            return self.editor._propagated_diameters or []
        return []

    # =================================================================
    # Cleanup
    # =================================================================

    def cleanup(self):
        if hasattr(self, "_download_thread") and self._download_thread.isRunning():
            self._download_thread.quit()
            self._download_thread.wait(3000)
        if hasattr(self, "_temp_dir_obj"):
            self._temp_dir_obj.cleanup()


"""
Advanced Graph Tab — вкладка продвинутого редактора графа P&ID.

Инструменты SimpleGraphTab + routing, optimise, drag, multi-select,
waypoints, batch delete, auto-fix, perp stats.

Используется на второй фазе двухфазного флоу val_graph:
  SimpleGraphTab → ✅ → AdvancedGraphTab → ✅ → complete_graph_validation()
"""

import logging

from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QVBoxLayout, QWidget, QPushButton, QLabel,
    QSpinBox, QMenu, QButtonGroup,
)
from PySide6.QtCore import Slot, Qt, QThread
from PySide6.QtGui import QColor, QPixmap, QIcon

from ui.services.api_client import APIClient, APIError
from ui.services.recognize_worker import RecognizeWorker
from ui.services.thread_lifetime import hand_over
from ui.services.ui_settings import UISettings
from ui.editors.advanced_graph_editor import AdvancedGraphEditor
from ui.editors.base_graph_editor import BaseGraphEditor
from ui.tabs.simple_graph_tab import SimpleGraphTab
from ui.widgets.error_report_dialog import report_exception

logger = logging.getLogger(__name__)


class AdvancedGraphTab(SimpleGraphTab):
    """Вкладка продвинутого редактора графа.

    Наследует SimpleGraphTab (добавляет к его тулбару).
    Редактор: AdvancedGraphEditor.
    """

    # Ручная правка — единственная вкладка в холсте 1920x1080 (WYSIWYG).
    USE_CANVAS = True

    def __init__(
        self,
        diagram_uid: str,
        diagram_name: str,
        api_client: APIClient,
        parent=None,
    ):
        super().__init__(diagram_uid, diagram_name, api_client, parent)

    # =================================================================
    # BaseGraphTab interface
    # =================================================================

    def _create_editor(self) -> BaseGraphEditor:
        return AdvancedGraphEditor()

    def _setup_toolbar(self, toolbar: QHBoxLayout):
        # Инициализация кисти рёбер (используется панелью «Размер и цвет»).
        self._size_sync = False
        self._current_edge_color = self._EDGE_PALETTE[0][0]

        # --- Общие кнопки (всегда видимы): добавить ребро/перекрёсток/узел ---
        super()._setup_toolbar(toolbar)

        # --- Общие: Размер объектов (режим, открывает левую панель) ---
        self.btn_resize_objects = QPushButton("Размеры")
        self.btn_resize_objects.setCheckable(True)
        self.btn_resize_objects.setToolTip(
            "Массовое изменение размеров объектов одного класса.\n"
            "Слева открывается панель: выбор класса, набор экземпляров, "
            "ширина/высота (боксы) или масштаб (полигоны).\n"
            "Ctrl+ЛКМ — добавить экземпляр в набор, Ctrl+ПКМ — убрать, "
            "Shift+рамка — добавить группу.\n"
            "Повторное нажатие кнопки или Esc — выйти из инструмента."
        )
        self.btn_resize_objects.setStyleSheet(
            "QPushButton:checked { background-color: #16a085; color: white; }"
        )
        self.btn_resize_objects.clicked.connect(lambda: self._toggle_mode("resize_objects"))
        self.mode_group.addButton(self.btn_resize_objects)
        toolbar.addWidget(self.btn_resize_objects)

        # --- Общие: Показать скины (переключатель отображения, не режим) ---
        self.btn_show_skins = QPushButton("Скины")
        self.btn_show_skins.setCheckable(True)
        self.btn_show_skins.setToolTip(
            "Показать символы (FXML-скины) для классов оборудования внутри бокса — "
            "примерно как уедет в FXML.\nРаботает для классов, у которых есть скин."
        )
        self.btn_show_skins.setStyleSheet(
            "QPushButton:checked { background-color: #8e44ad; color: white; }"
        )
        self.btn_show_skins.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_show_skins.toggled.connect(self._on_toggle_skins)
        toolbar.addWidget(self.btn_show_skins)

        self._add_separator(toolbar)

        # --- Состояния: перп / окр / линии. Базовое = ни одно не активно
        #     (повторный клик по активному состоянию выключает его → базовое).
        self.regime_group = QButtonGroup(self)
        self.regime_group.setExclusive(False)

        self.btn_regime_perp = QPushButton("Перпендикулярность")
        self.btn_regime_perp.setCheckable(True)
        self.btn_regime_perp.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_regime_perp.setToolTip(
            "Состояние «Перпендикулярность».\n"
            "Подсветка: оранжевые неперпендикулярные рёбра + утолщение.\n"
            "Появляются: Оптимизировать / Оптимизировать все / Авто-выравнивание / "
            "Точки изгиба."
        )
        self.btn_regime_perp.setStyleSheet(
            "QPushButton:checked { background-color: #e67e22; color: white; }"
        )
        self.btn_regime_perp.clicked.connect(lambda: self._set_regime("perp"))
        self.regime_group.addButton(self.btn_regime_perp)
        toolbar.addWidget(self.btn_regime_perp)

        self.btn_regime_ocr = QPushButton("ОКР привязка")
        self.btn_regime_ocr.setCheckable(True)
        self.btn_regime_ocr.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_regime_ocr.setToolTip(
            "Состояние «ОКР привязка» (как вкладка «бусина привязка»).\n"
            "Подсветка: зелёный/красный узел — есть/нет KKS, красное ребро — нет "
            "диаметра.\n"
            "Появляются: Добавить блок / Распознать добавленные. Перетаскивание "
            "блока на узел/ребро — привязка."
        )
        self.btn_regime_ocr.setStyleSheet(
            "QPushButton:checked { background-color: #2ecc71; color: white; }"
        )
        self.btn_regime_ocr.clicked.connect(lambda: self._set_regime("ocr"))
        self.regime_group.addButton(self.btn_regime_ocr)
        toolbar.addWidget(self.btn_regime_ocr)

        self.btn_regime_style = QPushButton("Линии")
        self.btn_regime_style.setCheckable(True)
        self.btn_regime_style.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_regime_style.setToolTip(
            "Состояние «Размер и цвет».\n"
            "Рёбра рисуются своим цветом/размером (по умолчанию белые → чёрные в FXML).\n"
            "Появляются: Цвет (палитра) и Размер.\n"
            "Ctrl+ЛКМ — применить; Shift+протяжка — обвести только рёбра; "
            "Ctrl+ПКМ — убрать из обводки; Ctrl+колесо — размер."
        )
        self.btn_regime_style.setStyleSheet(
            "QPushButton:checked { background-color: #3498db; color: white; }"
        )
        self.btn_regime_style.clicked.connect(lambda: self._set_regime("style"))
        self.regime_group.addButton(self.btn_regime_style)
        toolbar.addWidget(self.btn_regime_style)

        self._add_separator(toolbar)

        # --- Сменяющиеся панели инструментов состояний ---
        self._build_perp_panel(toolbar)
        self._build_ocr_panel(toolbar)
        self._build_style_panel(toolbar)


    # =================================================================
    # Панели инструментов состояний (сменяющееся окно)
    # =================================================================

    def _build_perp_panel(self, toolbar: QHBoxLayout):
        """Панель состояния «Перпендикулярность»."""
        panel = QWidget()
        row = QHBoxLayout(panel)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.btn_optimize_edge = QPushButton("Оптимизировать")
        self.btn_optimize_edge.setCheckable(True)
        self.btn_optimize_edge.setToolTip(
            "Выравнивание одного ребра под прямой угол.\n"
            "Ctrl+ЛКМ по оранжевому (неперпендикулярному) ребру — выровнять его под 90°.\n"
            "Повторное нажатие кнопки или Esc — выйти из инструмента."
        )
        self.btn_optimize_edge.setStyleSheet(
            "QPushButton:checked { background-color: #9C27B0; color: white; }"
        )
        self.btn_optimize_edge.clicked.connect(lambda: self._toggle_mode("optimize_edge"))
        self.mode_group.addButton(self.btn_optimize_edge)
        row.addWidget(self.btn_optimize_edge)

        self.btn_optimize_all = QPushButton("Оптимизировать все")
        self.btn_optimize_all.setToolTip(
            "Выровнять под прямой угол сразу все неперпендикулярные рёбра."
        )
        self.btn_optimize_all.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_optimize_all.clicked.connect(self._optimize_all_edges)
        row.addWidget(self.btn_optimize_all)

        self.btn_auto_fix = QPushButton("Авто-выравнивание")
        self.btn_auto_fix.setToolTip(
            "Сгладить косые трубы и мелкие зигзаги после ручных правок.\n"
            "Сначала трубе строится обход, потом подтягиваются изломы; "
            "оборудование двигается только чуть-чуть и только если иначе\n"
            "прямой не получить. Закреплённые вами точки входа не трогаются.\n"
            "Ctrl+Z — отменить одним шагом."
        )
        self.btn_auto_fix.setStyleSheet(
            "QPushButton { background-color: #FF9800; color: white; font-weight: bold; }"
            "QPushButton:hover { background-color: #F57C00; }"
        )
        self.btn_auto_fix.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_auto_fix.clicked.connect(self._auto_fix)
        row.addWidget(self.btn_auto_fix)

        self.btn_waypoints = QPushButton("Точки изгиба")
        self.btn_waypoints.setCheckable(True)
        self.btn_waypoints.setToolTip(
            "Изломы ребра (точки изгиба трубы).\n"
            "Ctrl+ЛКМ по сегменту ребра — добавить точку изгиба.\n"
            "Ctrl+ЛКМ по точке и тянуть — двигать её (примагничивание к сетке).\n"
            "Маркеры точек видны только в этом инструменте.\n"
            "Повторное нажатие кнопки или Esc — выйти из инструмента."
        )
        self.btn_waypoints.setStyleSheet(
            "QPushButton:checked { background-color: #00BCD4; color: white; }"
        )
        self.btn_waypoints.clicked.connect(lambda: self._toggle_mode("edit_waypoint"))
        self.mode_group.addButton(self.btn_waypoints)
        row.addWidget(self.btn_waypoints)

        self._perp_panel = panel
        self._perp_panel.setVisible(False)
        toolbar.addWidget(panel)

    def _build_ocr_panel(self, toolbar: QHBoxLayout):
        """Панель состояния «ОКР привязка» (функционал бусины привязки)."""
        panel = QWidget()
        row = QHBoxLayout(panel)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.btn_add_block = QPushButton("Добавить блок")
        self.btn_add_block.setCheckable(True)
        self.btn_add_block.setToolTip(
            "Добавить текстовый блок рамкой.\n"
            "Зажмите ЛКМ и обведите область текста — создаётся пустой блок.\n"
            "Затем «Распознать добавленные» — распознать текст в них.\n"
            "Ctrl+перетаскивание блока на узел/ребро — привязка.\n"
            "Повторное нажатие кнопки или Esc — выйти из инструмента."
        )
        self.btn_add_block.setStyleSheet(
            "QPushButton:checked { background-color: #2ecc71; color: white; }"
        )
        self.btn_add_block.clicked.connect(lambda: self._toggle_mode("add_ocr_block"))
        self.mode_group.addButton(self.btn_add_block)
        row.addWidget(self.btn_add_block)

        self.btn_recognize = QPushButton("Распознать добавленные")
        self.btn_recognize.setToolTip(
            "Распознать текст во всех пустых добавленных блоках.\n"
            "Работает в фоне — окно не зависает."
        )
        self.btn_recognize.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_recognize.clicked.connect(self._run_ocr_recognize)
        row.addWidget(self.btn_recognize)

        self._ocr_panel = panel
        self._ocr_panel.setVisible(False)
        toolbar.addWidget(panel)

    def _build_style_panel(self, toolbar: QHBoxLayout):
        """Панель состояния «Размер и цвет» (бывший второй ряд)."""
        panel = QWidget()
        row = QHBoxLayout(panel)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        # --- Цвет ---
        self.btn_edge_color = QPushButton("Цвет")
        self.btn_edge_color.setCheckable(True)
        self.btn_edge_color.setToolTip(
            "Инструмент изменения цвета ребра.\n"
            "Ctrl+ЛКМ по ребру — покрасить в текущий цвет палитры.\n"
            "Обведённые рёбра (Shift+протяжка) красятся все сразу.\n"
            "Цвет выбирается в палитре справа и сохраняется в FXML.\n"
            "Повторное нажатие кнопки или Esc — выйти из инструмента."
        )
        self.btn_edge_color.setStyleSheet(
            "QPushButton:checked { background-color: #E91E63; color: white; }"
        )
        self.btn_edge_color.clicked.connect(lambda: self._toggle_mode("edit_edge_color"))
        self.mode_group.addButton(self.btn_edge_color)
        row.addWidget(self.btn_edge_color)

        # Образец цвета + скрытая палитра (поповер)
        self.btn_edge_swatch = QPushButton()
        self.btn_edge_swatch.setFixedSize(26, 24)
        self.btn_edge_swatch.setToolTip(
            "Текущий цвет ребра. Нажмите — открыть палитру (пресеты)."
        )
        self._edge_palette_menu = QMenu(self)
        for hexc, name in self._EDGE_PALETTE:
            act = self._edge_palette_menu.addAction(self._color_icon(hexc), name)
            act.triggered.connect(lambda checked=False, c=hexc: self._on_edge_color_selected(c))
        self._edge_palette_menu.addSeparator()
        act_custom = self._edge_palette_menu.addAction("Свой цвет (RGB/hex)…")
        act_custom.setToolTip("Задать цвет числами: RGB, HSV или hex-код")
        act_custom.triggered.connect(self._pick_custom_edge_color)
        self.btn_edge_swatch.setMenu(self._edge_palette_menu)
        row.addWidget(self.btn_edge_swatch)
        self._update_edge_swatch(self._current_edge_color)

        sep = QLabel(" | ")
        sep.setStyleSheet("color: #666;")
        row.addWidget(sep)

        # --- Размер ---
        self.btn_edge_size = QPushButton("Размер")
        self.btn_edge_size.setCheckable(True)
        self.btn_edge_size.setToolTip(
            "Инструмент изменения размера (толщины) ребра.\n"
            "Ctrl+ЛКМ по ребру — задать текущий размер.\n"
            "Ctrl+колесо в редакторе — менять размер. Обведённым — всем сразу.\n"
            "Размер виден в редакторе и записывается в FXML.\n"
            "Повторное нажатие кнопки или Esc — выйти из инструмента."
        )
        self.btn_edge_size.setStyleSheet(
            "QPushButton:checked { background-color: #795548; color: white; }"
        )
        self.btn_edge_size.clicked.connect(lambda: self._toggle_mode("edit_edge_size"))
        self.mode_group.addButton(self.btn_edge_size)
        row.addWidget(self.btn_edge_size)

        self.spin_edge_size = QSpinBox()
        self.spin_edge_size.setRange(1, 40)
        self.spin_edge_size.setValue(4)
        self.spin_edge_size.setMaximumWidth(60)
        self.spin_edge_size.setToolTip(
            "Размер (толщина) ребра числом.\n"
            "Можно менять здесь или Ctrl+колесом в редакторе."
        )
        self.spin_edge_size.valueChanged.connect(self._on_edge_size_spin)
        row.addWidget(self.spin_edge_size)

        # --- Пунктир (штриховой стиль ребра) ---
        self.btn_edge_dash = QPushButton("Пунктир")
        self.btn_edge_dash.setCheckable(True)
        self.btn_edge_dash.setToolTip(
            "Инструмент штриховки (пунктира) ребра.\n"
            "Ctrl+ЛКМ по ребру — переключить пунктирный стиль.\n"
            "Обведённые рёбра (Shift+протяжка) переключаются все сразу.\n"
            "Пунктирный стиль сохраняется в FXML.\n"
            "Повторное нажатие кнопки или Esc — выйти из инструмента."
        )
        self.btn_edge_dash.setStyleSheet(
            "QPushButton:checked { background-color: #607D8B; color: white; }"
        )
        self.btn_edge_dash.clicked.connect(lambda: self._toggle_mode("edit_edge_dash"))
        self.mode_group.addButton(self.btn_edge_dash)
        row.addWidget(self.btn_edge_dash)

        self._style_panel = panel
        self._style_panel.setVisible(False)
        toolbar.addWidget(panel)

    def _get_mode_button_map(self) -> dict:
        btn_map = super()._get_mode_button_map()
        btn_map.update({
            "optimize_edge": self.btn_optimize_edge,
            "edit_waypoint": self.btn_waypoints,
            "edit_edge_color": self.btn_edge_color,
            "edit_edge_size": self.btn_edge_size,
            "edit_edge_dash": self.btn_edge_dash,
            "resize_objects": self.btn_resize_objects,
            "add_ocr_block": self.btn_add_block,
        })
        return btn_map

    # =================================================================
    # Панель «Размер объектов»
    # =================================================================

    def _ensure_resize_panel(self):
        if getattr(self, "_resize_panel", None) is None:
            from ui.widgets.object_resize_panel import ObjectResizePanel
            p = ObjectResizePanel(self)
            p.on_class_changed = lambda name: self._editor and self._editor.set_resize_class(name)
            p.on_select_all = lambda: self._editor and self._editor.resize_select_all()
            p.on_select_one = lambda: self._editor and self._editor.resize_select_one_mode()
            p.on_filter = lambda kind: self._editor and self._editor.resize_filter(kind)
            p.on_preview = lambda w, h, s: self._editor and self._editor.preview_resize(w, h, s)
            p.on_apply = lambda w, h, s: self._editor and self._editor.apply_resize(w, h, s)
            p.on_visibility = self._on_resize_panel_visibility
            # Разрыв моста — общая настройка схемы (per uid), уходит в FXML-генерацию.
            p.on_bridge_gap = self._on_bridge_gap_changed
            cur = float(UISettings.instance().get_appearance(
                self.uid, "bridge_gap_factor", 3.0
            ))
            p.set_bridge_gap(cur)
            self._resize_panel = p
        return self._resize_panel

    def _on_bridge_gap_changed(self, value: float):
        """«Применить разрыв»: множитель в настройки И в предпросмотр (7.5).

        До правки он доходил только до запроса генерации
        (`diagram_workspace.py:2370`), а редактор рисовал разрывы дефолтом
        `BRIDGE_GAP_STROKE_FACTOR` = 3.0 — оператор, сдвинувший бегунок, видел
        новую ширину только в готовом файле. Регулятор единственный в таблице
        оформления, который уходит в ВЫГРУЗКУ (`UI_GUIDE §11.4`), поэтому
        расхождение экрана и файла здесь дороже обычного.
        """
        value = float(value)
        UISettings.instance().set_appearance(self.uid, "bridge_gap_factor", value)
        self._apply_bridge_gap(value)

    def _apply_bridge_gap(self, value: float):
        """Положить множитель в редактор и свести разрывы предпросмотра.

        Пересчёт — та же дверь, что у жестов (`_sync_bridge_cuts`): при
        выключенных «Скинах» она выходит сразу, а включение скинов считает
        разрывы заново уже с новым множителем.
        """
        ed = self._editor
        if ed is None or not hasattr(ed, "bridge_gap_factor"):
            return
        ed.bridge_gap_factor = float(value)
        if hasattr(ed, "_sync_bridge_cuts"):
            ed._sync_bridge_cuts()

    def _on_resize_panel_visibility(self, shown: bool):
        """Панель показана → сдвинуть видимую область редактора вправо,
        чтобы панель не перекрывала левый край листа; скрыта → вернуть."""
        self._update_left_gutter()

    def _update_left_gutter(self):
        """Левый отступ редактора от состояния шторки «Размеры»."""
        from ui.widgets.object_resize_panel import PANEL_WIDTH as RESIZE_W
        gutter = 0
        panel = getattr(self, "_resize_panel", None)
        if panel is not None and panel.is_shown:
            gutter = RESIZE_W
        if self._editor and hasattr(self._editor, "set_left_gutter"):
            self._editor.set_left_gutter(gutter)

    def _update_resize_panel_bounds(self):
        p = getattr(self, "_resize_panel", None)
        if p is None or self._editor is None:
            return
        try:
            geo = self._editor.geometry()
            p.set_bounds(geo.top(), geo.height())
        except Exception:
            pass

    def _show_resize_panel(self, visible: bool):
        p = self._ensure_resize_panel()
        self._update_resize_panel_bounds()
        if visible:
            p.show_panel()
        else:
            p.hide_panel()

    @Slot(bool)
    def _on_toggle_skins(self, checked: bool):
        if self._editor and hasattr(self._editor, "set_show_skins"):
            self._editor.set_show_skins(checked)
            self._editor.setFocus()

    def _resize_classes_cb(self, names, current):
        self._ensure_resize_panel().set_classes(names, current)

    def _resize_state_cb(self, kind, count, mw, mh):
        self._ensure_resize_panel().set_state(kind, count, mw, mh)

    # =================================================================
    # ОКР привязка: асинхронное распознавание добавленных блоков
    # =================================================================

    @Slot()
    def _run_ocr_recognize(self):
        """Распознать текст во всех пустых добавленных блоках (в фоне)."""
        if self._editor is None or not hasattr(self._editor, "get_pending_ocr_boxes"):
            return
        if getattr(self, "_recog_thread", None) is not None:
            self.status_label.setText("Распознавание уже выполняется…")
            return

        pending_ids, boxes = self._editor.get_pending_ocr_boxes()
        if not boxes:
            self.status_label.setText("Нет пустых блоков для распознавания")
            return

        # Выйти из режима добавления блока, чтобы не мешал.
        if self._editor._current_mode == "add_ocr_block":
            self._set_mode("idle")

        self._recog_pending_ids = pending_ids
        self.status_label.setText(f"Распознавание {len(boxes)} блоков…")
        self.btn_recognize.setEnabled(False)
        # Гонка с фоновым потоком: save/confirm до прихода результата
        # зафиксировали бы граф без распознанного текста.
        self.btn_save.setEnabled(False)
        self.btn_confirm.setEnabled(False)

        self._recog_thread = QThread()
        self._recog_worker = RecognizeWorker(self.api_client, self.uid, boxes)
        self._recog_worker.moveToThread(self._recog_thread)
        self._recog_thread.started.connect(self._recog_worker.run)
        self._recog_worker.finished.connect(self._on_recognize_done)
        self._recog_worker.error.connect(self._on_recognize_error)
        # Гасит поток САМ поток: `_cleanup_recog_thread` зовут только слоты
        # выше, а связи с ними Qt рвёт вместе с разрушаемой вкладкой
        # (пункт 1.x17).
        self._recog_worker.finished.connect(self._recog_thread.quit)
        self._recog_worker.error.connect(self._recog_thread.quit)
        # ⛔ И уносит СЕБЯ САМ, в СВОЁМ потоке (пункт 1-46, замер §119а):
        # иначе рабочий объект переживает разрушенную вкладку сиротой
        # в кончившемся потоке — тот же шов, что у загрузчика.
        self._recog_worker.finished.connect(self._recog_worker.deleteLater)
        self._recog_worker.error.connect(self._recog_worker.deleteLater)
        # ⛔ И, наконец, поток обязан КОНЧИТЬСЯ раньше, чем процесс начнёт
        # разрушать объекты (пункт 1-50, замер §127): гашение выше
        # срабатывает только когда работник ДОРАБОТАЛ, а на молчащем
        # сервере он не дорабатывает вовсе — уход из вкладки давал тогда
        # 4 краха процесса из 4 (`0xC0000409`).
        hand_over(self, self._recog_thread, self._recog_worker,
                  name="распознавание блоков", uid=self.uid)
        self._recog_thread.start()

    def _cleanup_recog_thread(self):
        if getattr(self, "_recog_thread", None) is not None:
            self._recog_thread.quit()
            self._recog_thread.wait()
            self._recog_thread = None
            self._recog_worker = None
        self.btn_recognize.setEnabled(True)
        self.btn_save.setEnabled(True)
        self.btn_confirm.setEnabled(True)

    @Slot(list)
    def _on_recognize_done(self, results: list):
        pending_ids = getattr(self, "_recog_pending_ids", [])
        self._cleanup_recog_thread()
        if self._editor and hasattr(self._editor, "apply_ocr_results"):
            n = self._editor.apply_ocr_results(pending_ids, results)
            self.status_label.setText(f"Распознано {n}/{len(results)} блоков")

    @Slot(str)
    def _on_recognize_error(self, msg: str):
        self._cleanup_recog_thread()
        self.status_label.setText(f"Ошибка распознавания: {msg}")

    # =================================================================
    # Второй ряд тулбара: изменение ребра (цвет / размер)
    # =================================================================

    # Пресеты палитры цветов рёбер.
    _EDGE_PALETTE = [
        ("#e74c3c", "Красный"), ("#e67e22", "Оранжевый"),
        ("#f1c40f", "Жёлтый"),  ("#2ecc71", "Зелёный"),
        ("#1abc9c", "Бирюзовый"), ("#3498db", "Синий"),
        ("#9b59b6", "Фиолетовый"), ("#34495e", "Тёмно-синий"),
        ("#7f8c8d", "Серый"), ("#333333", "Тёмный"),
        ("#000000", "Чёрный"), ("#ffffff", "Белый"),
    ]

    # =================================================================
    # Переключение состояния (base / perp / ocr / style)
    # =================================================================

    def _regime_buttons(self) -> dict:
        """Соответствие состояние → кнопка (кроме базового — у него нет кнопки)."""
        return {
            "perp": self.btn_regime_perp,
            "ocr": self.btn_regime_ocr,
            "style": self.btn_regime_style,
        }

    def _set_regime(self, regime: str):
        """Клик по кнопке состояния. Повторный клик по активному → базовое."""
        btn = self._regime_buttons().get(regime)
        # Qt уже переключил checked к моменту clicked: если стало выкл — базовое.
        target = regime if (btn is not None and btn.isChecked()) else "base"
        self._activate_regime(target)

    def _activate_regime(self, regime: str):
        """Активировать состояние: синхронизировать кнопки, редактор и панели."""
        for r, b in self._regime_buttons().items():
            b.setChecked(r == regime)  # base → все выключены
        if self._editor and hasattr(self._editor, "set_display_regime"):
            self._editor.set_display_regime(regime)
        self._apply_regime_ui(regime)

    def _on_regime_changed(self, regime: str):
        """Callback из редактора → синхронизировать кнопки и UI (без рекурсии)."""
        for r, b in self._regime_buttons().items():
            b.setChecked(r == regime)
        self._apply_regime_ui(regime)

    def _apply_regime_ui(self, regime: str):
        """Показать панель инструментов активного состояния, спрятать остальные."""
        if hasattr(self, "_perp_panel"):
            self._perp_panel.setVisible(regime == "perp")
        if hasattr(self, "_ocr_panel"):
            self._ocr_panel.setVisible(regime == "ocr")
        if hasattr(self, "_style_panel"):
            self._style_panel.setVisible(regime == "style")
        if regime == "style":
            # Авто-вход в инструмент «Цвет» для удобства
            self.btn_edge_color.setChecked(True)
            self._set_mode("edit_edge_color")

    @staticmethod
    def _color_icon(hexc: str) -> QIcon:
        pm = QPixmap(16, 16)
        pm.fill(QColor(hexc))
        return QIcon(pm)

    def _update_edge_swatch(self, hexc: str):
        """Отрисовать образец текущего цвета на кнопке."""
        self._current_edge_color = hexc
        border = "#000" if hexc.lower() in ("#ffffff", "#fff") else "#222"
        self.btn_edge_swatch.setStyleSheet(
            f"QPushButton {{ background-color: {hexc}; border: 1px solid {border}; "
            f"border-radius: 3px; }}"
        )

    @Slot()
    def _pick_custom_edge_color(self):
        """Диалог произвольного цвета: RGB/HSV числами или hex-код."""
        from PySide6.QtWidgets import QColorDialog
        color = QColorDialog.getColor(
            QColor(self._current_edge_color), self, "Цвет ребра",
        )
        if color.isValid():
            self._on_edge_color_selected(color.name())

    @Slot()
    def _on_edge_color_selected(self, hexc: str):
        """Выбран цвет в палитре → установить кисть + включить режим цвета."""
        self._update_edge_swatch(hexc)
        if self._editor and hasattr(self._editor, "set_edge_brush_color"):
            self._editor.set_edge_brush_color(QColor(hexc))
        # Активировать режим цвета для удобства
        self.btn_edge_color.setChecked(True)
        self._set_mode("edit_edge_color")
        self.status_label.setText(f"Цвет кисти рёбер: {hexc}")

    @Slot(int)
    def _on_edge_size_spin(self, value: int):
        """Спинбокс размера → передать в редактор (без петли обратного вызова)."""
        if self._size_sync:
            return
        if self._editor and hasattr(self._editor, "edge_brush_size"):
            self._editor.edge_brush_size = max(1, int(value))

    def _on_editor_size_changed(self, size: int):
        """Callback из редактора (Ctrl+колесо) → обновить спинбокс."""
        self._size_sync = True
        self.spin_edge_size.setValue(int(size))
        self._size_sync = False

    # =================================================================
    # Editor-ready hook
    # =================================================================

    def _on_editor_ready(self):
        """После загрузки — обновить perp stats + config dir для KKS."""
        self._update_perp_stats()
        self._sync_editor_config_dir()
        # Инициализировать кисть изменения ребра (цвет/размер).
        # Размер синхронизируем ОТ редактора: стартовый размер = базовая толщина
        # трубы в его системе координат (в холсте 2 == LINE_STROKE_WIDTH, в
        # legacy 4), а не наоборот — иначе спинбокс навязывал бы legacy-значение.
        if self._editor and hasattr(self._editor, "set_edge_brush_color"):
            self._editor.set_edge_brush_color(QColor(self._current_edge_color))
            self._size_sync = True
            self.spin_edge_size.setValue(int(round(self._editor.edge_brush_size)))
            self._size_sync = False
            self._editor.edge_size_callback = self._on_editor_size_changed
        # Подключить состояния (по умолчанию — «Базовое»)
        if self._editor and hasattr(self._editor, "set_display_regime"):
            self._editor.regime_callback = self._on_regime_changed
            self._editor.set_display_regime("base")
        self._apply_regime_ui("base")
        # Колбэки панели «Размеры»
        if self._editor is not None and hasattr(self._editor, "resize_panel_show_cb"):
            self._editor.resize_panel_show_cb = self._show_resize_panel
            self._editor.resize_panel_classes_cb = self._resize_classes_cb
            self._editor.resize_panel_state_cb = self._resize_state_cb
        self._apply_layout_lock()

    _LAYOUT_LOCK_REASON = (
        "Авто-раскладка уже выполнена — она сделала эту работу.\n"
        "Инструмент доступен только на холстах без раскладки."
    )

    # 4.1 (К-1 / «НД5»): у «Авто-выравнивания» замок ОБРАТНЫЙ. За одной
    # кнопкой стояли два движка, и на холсте БЕЗ раскладки это прежний
    # `auto_fix_graph` — медианное выравнивание цепочек без единой проверки
    # коллизий (замер §1.1 EDITOR_AFTER_LAYOUT: регрессия на 5 листах корпуса
    # из 7, ±120 px дрейфа). Ветка достижима не в экзотике, а в обычном цикле:
    # холст с `layout_applied=False` мог быть СОХРАНЁН на сервер прошлой
    # сессией и грузится как актуальный (`base_graph_tab.py:963-977`), а любой
    # сбой загрузки уводит в except-хвост (`:1022`) — там граф вообще без метки.
    _NO_LAYOUT_LOCK_REASON = (
        "Доступно только на холсте после авто-раскладки.\n"
        "Здесь холст собран без неё, а прежнее авто-выравнивание двигало\n"
        "узлы без проверки коллизий — на 5 листах корпуса из 7 это регрессия.\n"
        "Вернитесь в «Контуры» и нажмите «Подтвердить» — это запустит пересчёт."
    )

    @staticmethod
    def _lock_button(btn, locked: bool, reason: str):
        """Запереть/отпустить кнопку, вернув ей исходную подсказку."""
        if locked:
            if btn.isEnabled():
                btn.setProperty("_pre_lock_tooltip", btn.toolTip())
            btn.setEnabled(False)
            btn.setToolTip(reason)
        elif not btn.isEnabled():
            btn.setEnabled(True)
            orig = btn.property("_pre_lock_tooltip")
            if orig is not None:
                btn.setToolTip(orig)

    def _apply_layout_lock(self):
        """Э4-00: после авто-раскладки авто-инструменты выравнивания выключены.

        Замер §1.1 плана EDITOR_AFTER_LAYOUT: поверх раскладки автовыравнивание/
        оптимизация — регрессия на 5 из 7 графов корпуса. На фолбэк- и legacy-
        холстах (layout_applied=False или метки нет) кнопки работают как раньше.

        2026-08-02: «Авто-выравнивание» ИЗ ЗАМКА ВЫВЕДЕНО — за кнопкой теперь
        не прежний auto_fix, а Э4-сглаживание (`smooth_canvas`), которое как
        раз и предназначено для холста после раскладки: адресно, под гейтом,
        с откатом каждого хода. Замер по 19 холстам заказчика: косых 51 -> 12,
        побочных ухудшений нет ни на одном файле. «Оптимизация» остаётся
        запертой — её замер не переделывался.

        2026-08-25 (4.1): «Авто-выравнивание» вернулось в замок ОБРАТНОЙ
        стороной — оно запирается там, где раскладки НЕТ, потому что фолбэк-
        ветка кнопки вела в прежний `auto_fix` и осталась достижимой
        (см. `_NO_LAYOUT_LOCK_REASON`). Так у кнопки остаётся один движок.
        """
        from modules.graph.core import canvas_state

        has_layout = False
        if self._editor is not None:
            has_layout = canvas_state.has_layout(
                getattr(self._editor, "graph_data", None) or {})
        for btn in (self.btn_optimize_edge, self.btn_optimize_all):
            self._lock_button(btn, has_layout, self._LAYOUT_LOCK_REASON)
        self._lock_button(self.btn_auto_fix, not has_layout,
                          self._NO_LAYOUT_LOCK_REASON)

    # =================================================================
    # Оформление: + цвета рёбер по стадиям
    # =================================================================

    def _build_appearance_controls(self, panel):
        from PySide6.QtGui import QColor
        super()._build_appearance_controls(panel)
        # ⛔ Здесь был регулятор «Ребро без диаметра» (`edge_no_diam_color`).
        # Снят вердиктом Максима по таблице 7.3: замер `MEASUREMENTS §MEFX7.5` —
        # 0 изменённых предметов сцены во ВСЕХ четырёх состояниях при поле шума 0.
        # Умер не опечаткой ключа, а потерянным читателем: `COLOR_NO_DIAMETER`
        # никто не читал с тех пор, как из состояния «ОКР привязка» убрали красную
        # подсветку рёбер без диаметра. Правило Максима: не оптимизируем — убираем.
        self._add_color_setting(
            panel, "Неперпенд. ребро (в «Перпендикулярности»)", "edge_bad_color",
            QColor("#e67e22"),
            lambda c: self._editor and self._editor.set_edge_bad_color(c),
        )
        # Слой ОКР (текст-блоки и рамки ОКР-объектов) — одна ручка.
        self._add_size_setting(
            panel, "Толщина рамки текст-боксов", "size_ocr_border",
            lambda f: self._set_editor_size("OCR_BORDER_W", f),
        )

    def apply_saved_appearance(self):
        super().apply_saved_appearance()
        ed = self._editor
        if ed is None:
            return
        from PySide6.QtGui import QColor
        if hasattr(ed, "set_edge_bad_color"):
            self._apply_saved_color("edge_bad_color", QColor("#e67e22"),
                                    ed.set_edge_bad_color)
        if hasattr(ed, "set_size_factor"):
            self._apply_saved_size(
                "size_ocr_border", lambda f: ed.set_size_factor("OCR_BORDER_W", f))
        # 7.5: множитель разрыва живёт per-uid и уходит в выгрузку — предпросмотр
        # обязан считать его тем же числом при ЛЮБОМ положении бегунка, включая
        # сохранённое с прошлого открытия (панель «Размеры» может не открываться
        # вовсе, а генератор читает настройку всегда).
        self._apply_bridge_gap(UISettings.instance().get_appearance(
            self.uid, "bridge_gap_factor", 3.0))

    def apply_default_appearance(self):
        super().apply_default_appearance()
        ed = self._editor
        if ed is None:
            return
        from PySide6.QtGui import QColor
        if hasattr(ed, "set_edge_bad_color"):
            ed.set_edge_bad_color(QColor("#e67e22"))

    def set_project_code(self, project_code: str):
        """Установить код проекта + передать config dir и названия классов в editor."""
        super().set_project_code(project_code)
        self._sync_editor_config_dir()
        self._sync_editor_class_names()

    def _sync_editor_class_names(self):
        """Забрать из API русские названия классов для панели «Размер объектов».

        Не критично: не ответил сервер или поля нет (старый сервер) — редактор
        покажет внутренние английские имена, как до перевода.
        """
        if not (self._editor and self._project_code):
            return
        try:
            result = self.api_client.get_project_classes(self._project_code)
        except APIError as exc:
            logger.warning("Названия классов не загружены (%s) — покажем англ. имена", exc)
            return
        self._editor.class_display_names = {
            c["name"]: c["display_name"]
            for c in result.get("classes", [])
            if c.get("name") and c.get("display_name")
        }

    def _sync_editor_config_dir(self):
        """Передать путь к configs в editor для KKS нормализации (B6.5)."""
        if self._editor and hasattr(self._editor, '_project_config_dir') and self._project_code:
            from pathlib import Path
            cfg_dir = Path("configs/projects") / self._project_code
            if cfg_dir.is_dir():
                self._editor._project_config_dir = str(cfg_dir)

    # =================================================================
    # Stats override
    # =================================================================

    def _update_stats(self, stats: dict):
        """Обновить основные + перп статистику."""
        super()._update_stats(stats)
        self._update_perp_stats()

    def _update_perp_stats(self):
        """Метка перпендикулярности убрана из тулбара — оставлено для совместимости."""
        return

    # =================================================================
    # Advanced tools
    # =================================================================

    @Slot()
    def _optimize_all_edges(self):
        """Оптимизировать все неперпендикулярные рёбра."""
        if self._editor and hasattr(self._editor, "optimize_all_edges"):
            count = self._editor.optimize_all_edges()
            self._update_perp_stats()
            self.status_label.setText(f"Оптимизировано {count} рёбер")
            self._editor.setFocus()  # вернуть фокус — чтобы Ctrl+Z работал

    @Slot()
    def _batch_delete(self):
        """Удалить все выделенные узлы и рёбра."""
        if self._editor and hasattr(self._editor, "batch_delete"):
            self._editor.batch_delete()

    @Slot()
    def _auto_fix(self):
        """Кнопка «Авто-выравнивание» = Э4, адресное СГЛАЖИВАНИЕ.

        Решение заказчика 2026-08-02: «это вместо автовыравнивания кнопки».
        Сглаживание адресно по дефектам судьи, лестницей от бесплатных
        лекарств к сдвигу узла, каждый ход под гейтом с откатом
        (modules/graph/core/edit_smooth).

        4.1 (2026-08-25): второго движка за кнопкой больше НЕТ. Прежний
        `auto_fix_graph` уходил в фолбэк-ветку `else` и двигал узлы без единой
        проверки коллизий и без отката хода; ветка была живой (см.
        `_NO_LAYOUT_LOCK_REASON`). Теперь холст без раскладки просто запирает
        кнопку. Гейт стоит И здесь, а не только на кнопке: `_auto_fix` —
        обычный метод вкладки, а обещание «прежний auto_fix не запускается
        никогда» обязано держаться на КАЖДОМ пути, который его потребляет
        (PROTOCOL §110.18), иначе оно верно ровно наполовину.
        """
        if not self._editor:
            return
        from modules.graph.core import canvas_state

        if not canvas_state.has_layout(
                getattr(self._editor, "graph_data", None) or {}):
            self.status_label.setText(
                "Авто-выравнивание недоступно: холст собран без раскладки — "
                "подтвердите «Контуры», это запустит пересчёт")
            self._editor.setFocus()
            return

        # 4.4 (С8): сглаживание блокирующее — по 20 холстам корпуса медиана
        # 2.8 с, худший 6.6 с (замер §MEFX4Bб), и окно всё это время не
        # отвечает.
        # ⛔ В фоновый поток не выносится: движок мутирует ЖИВУЮ модель и зовёт
        # Qt-колбэки (`route_fn` трогает QGraphicsItem, снапшот — `_redraw_all`).
        # Значит оператору остаётся честная надпись. `repaint()` — синхронная
        # перерисовка ОДНОГО ярлыка; `processEvents()` здесь не заводится (в
        # `ui/` его нет ни разу: он крутит общую очередь и доставляет чужие
        # отложенные удаления, PROTOCOL §5).
        self.status_label.setText("Сглаживание…")
        self.status_label.repaint()
        try:
            # Курсор снимается СВОИМ finally, вложенным: иначе отчёт об отказе
            # (модалка ниже) открывался бы под песочными часами — то самое
            # «выглядит зависшей», от которого пункт и заводился.
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                stats = self._editor.smooth_canvas() or {}
            finally:
                QApplication.restoreOverrideCursor()
            self._show_refusals(stats)
        except Exception as exc:
            # Здесь стоял traceback.print_exc(): в собранном .exe
            # (console=False) sys.stdout и sys.stderr = None, печать уходила
            # в никуда, логгер не звался вовсе — прирост файла лога на отказе
            # был 0 байт (замер §96). Оператору оставалась одна строка модалки.
            logger.error("Авто-выравнивание: отказ", exc_info=True)
            report_exception(
                self, "Авто-выравнивание", exc,
                canvas=self._canvas_snapshot(),
                diagram_name=self.diagram_name,
            )
        finally:
            # Вернуть фокус редактору, иначе Ctrl+Z не дойдёт и не отменит.
            # В finally, а не в конце try: после отказа холст уже изменён,
            # шаг отмены открыт (пункт 1.1) — и нужен оператору тем более.
            self._editor.setFocus()

    def _show_refusals(self, stats: dict):
        """4.4 (К-4): назвать оператору, ПОЧЕМУ осталось несглаженное.

        В строке стояло только «осталось N» — сколько, но не почему. Движок
        с mefx-4a считает причину сам, тремя вёдрами (`REFUSAL_KINDS`), и
        вёдра не декоративны: на 20 холстах корпуса они дали
        {нет кандидата 7, сверх бюджета 71, лестница исчерпана 53}
        (замер `MEASUREMENTS §MEFX4Aд`).

        Перечень берётся ИЗ движка, а не переписывается сюда: появится
        четвёртое ведро — оно придёт в строку само, без правки вкладки.
        Дописываем к строке редактора, а не заменяем её: сводку ходов
        («колен N, изломов M… осталось K») пишет `smooth_canvas` в этот же
        ярлык через `status_callback`, и терять её незачем.
        """
        from modules.graph.core.edit_smooth import REFUSAL_KINDS

        named = [f"{kind} {stats[kind]}" for kind in REFUSAL_KINDS
                 if stats.get(kind)]
        if named:
            self.status_label.setText(
                f"{self.status_label.text()}; не вышло: {', '.join(named)}")

    def _canvas_snapshot(self):
        """Холст таким, каким его видел оператор в момент отказа.

        Отчёт важнее снимка: отрисовка испорченного холста может сама бросить,
        и тогда оператор остался бы вообще без отчёта — поэтому отказ снимка
        уходит строкой в лог, а отчёт собирается без картинки.
        """
        try:
            shot = self._editor.grab()
        except Exception:
            logger.warning("Снимок холста для отчёта не сделан", exc_info=True)
            return None
        return None if shot.isNull() else shot

"""
Contour Tab -- SAM2 contour selection and validation.

Inherits BaseGraphTab: artifact download, save, confirm, undo.
Editor: ContourEditor (extends SimpleGraphEditor).

Workflow:
  1. Load graph + contours_auto.json
  2. Ctrl+Click on equipment centroid -> toggle SAM2 polygon
  3. "Apply all" applies all contours with confidence >= threshold
  4. Save -> graph (updated segmentation/centroid) + contours_validated.json
  5. Confirm -> complete_contour_validation -> CONTOURS_VALIDATED
"""

import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QHBoxLayout, QPushButton, QLabel, QMessageBox,
    QApplication,
)
import time

from PySide6.QtCore import Slot, Qt, QThread, Signal

from ui.services.api_client import APIClient, APIError
from ui.editors.base_graph_editor import BaseGraphEditor
from ui.editors.contour_editor import ContourEditor
from ui.tabs.base_graph_tab import BaseGraphTab

logger = logging.getLogger(__name__)


class _ContourExtractWorker(QThread):
    """Фоновый запуск распознавания контуров + поллинг готовности."""

    done = Signal(bool, str)  # success, message

    def __init__(self, api_client, uid, ann_ids):
        super().__init__()
        self._api = api_client
        self._uid = uid
        self._ann_ids = ann_ids
        self._stop = False

    def stop(self):
        self._stop = True

    def _sleep(self, ms):
        """Прерываемый сон (проверяет флаг остановки каждые 100 мс)."""
        end = time.time() + ms / 1000.0
        while time.time() < end:
            if self._stop:
                return
            self.msleep(100)

    def run(self):
        try:
            self._api.extract_contours(self._uid, self._ann_ids)
        except Exception as exc:
            if not self._stop:
                self.done.emit(False, f"Не удалось запустить распознавание: {exc}")
            return
        deadline = time.time() + 600
        while time.time() < deadline:
            if self._stop:
                return
            self._sleep(2000)
            if self._stop:
                return
            try:
                st = self._api.get_contours_status(self._uid)
            except Exception:
                continue
            if st.get("has_auto"):
                self.done.emit(True, "Распознавание завершено")
                return
        if not self._stop:
            self.done.emit(False, "Превышено время ожидания распознавания (10 мин)")


class ContourTab(BaseGraphTab):
    """Tab for SAM2 contour selection.

    Click on equipment centroid to apply/remove SAM2 polygon.
    Saves both graph (with segmentation) and contours_validated.json.
    """

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
        return ContourEditor()

    def _setup_ui(self):
        super()._setup_ui()
        # На вкладке контуров кнопка «Сохранить» не нужна (сохранение по «Подтвердить»)
        if getattr(self, "btn_save", None) is not None:
            self.btn_save.hide()

    def _setup_toolbar(self, toolbar: QHBoxLayout):
        # === Блок РАСПОЗНАВАНИЯ (сначала, слева) ===
        self.btn_select_recog = QPushButton("Выбрать для распознавания")
        self.btn_select_recog.setCheckable(True)
        self.btn_select_recog.setToolTip(
            "Режим выбора узлов для распознавания формы (SAM2).\n"
            "• Нажми кнопку — включить режим выбора;\n"
            "• Shift+ЛКМ по узлу оборудования — отметить (жёлтый);\n"
            "• повторный Shift+ЛКМ — снять отметку;\n"
            "• отожми кнопку — режим выбора выключен."
        )
        self.btn_select_recog.setStyleSheet(
            "QPushButton:checked { background-color: #9b59b6; color: white; }"
        )
        self.btn_select_recog.clicked.connect(self._toggle_select_recog)
        toolbar.addWidget(self.btn_select_recog)

        self.btn_recog_selected = QPushButton("Распознать выбранные")
        self.btn_recog_selected.setToolTip(
            "Запустить распознавание формы (SAM2) только для отмеченных узлов.\n"
            "Распознанные станут синими — затем форму можно применить."
        )
        self.btn_recog_selected.clicked.connect(
            lambda: self._start_recognition(selected_only=True))
        toolbar.addWidget(self.btn_recog_selected)

        self.btn_recog_all = QPushButton("Распознать все")
        self.btn_recog_all.setToolTip(
            "Запустить распознавание формы (SAM2) для всех подходящих узлов.\n"
            "Дольше по времени; на CPU может занять заметное время."
        )
        self.btn_recog_all.setStyleSheet(
            "QPushButton { background-color: #e74c3c; color: white; }"
            "QPushButton:hover { background-color: #c0392b; }"
        )
        self.btn_recog_all.clicked.connect(
            lambda: self._start_recognition(selected_only=False))
        toolbar.addWidget(self.btn_recog_all)

        self._add_separator(toolbar)

        # === Блок ПРИМЕНЕНИЯ / ПРАВКИ (потом, справа) ===
        self.btn_edit_polygon = QPushButton("Редактировать реальную форму")
        self.btn_edit_polygon.setCheckable(True)
        self.btn_edit_polygon.setToolTip(
            "Правка реальной формы (контура) оборудования.\n"
            "Ctrl+ЛКМ по центроиду узла — выбрать его для редактирования формы.\n"
            "• тянуть вершину — двигать её;\n"
            "• клик по линии между вершинами — добавить новую вершину;\n"
            "• Ctrl+ПКМ по вершине — удалить вершину;\n"
            "• Ctrl+ПКМ по центроиду узла — удалить форму целиком и обвести заново;\n"
            "• Delete — стереть форму и нарисовать с нуля.\n"
            "Ctrl+ЛКМ по центроиду другого узла — перейти к нему.\n"
            "Esc или повторное нажатие кнопки — выйти из режима."
        )
        self.btn_edit_polygon.setStyleSheet(
            "QPushButton:checked { background-color: #FF8F00; color: white; }"
        )
        self.btn_edit_polygon.clicked.connect(self._toggle_edit_mode)
        toolbar.addWidget(self.btn_edit_polygon)

        self._add_separator(toolbar)

        btn_apply_all = QPushButton("Применить все")
        btn_apply_all.setToolTip(
            "Применить распознанную форму ко всем узлам, где программа "
            "уверена в результате.\nОстальные узлы можно обвести вручную."
        )
        btn_apply_all.clicked.connect(self._apply_all_contours)
        toolbar.addWidget(btn_apply_all)

        btn_remove_all = QPushButton("Снять все")
        btn_remove_all.setToolTip("Убрать все применённые формы со всех узлов")
        btn_remove_all.clicked.connect(self._remove_all_contours)
        toolbar.addWidget(btn_remove_all)

    @Slot()
    def _toggle_edit_mode(self):
        """Toggle edit_polygon mode on/off."""
        if self.btn_edit_polygon.isChecked():
            self._set_mode("edit_polygon")
        else:
            self._set_mode("apply_contour")

    def _on_mode_changed(self, mode: str):
        """Sync button state when mode changes (Escape, etc.)."""
        self.btn_edit_polygon.setChecked(mode == "edit_polygon")

    def _get_mode_button_map(self) -> dict:
        return {}

    # =================================================================
    # Editor ready hook -- load contours
    # =================================================================

    def _on_editor_ready(self):
        """After graph is loaded, load SAM2 contour data for selective apply.

        Graph is loaded from graph_validated (result of graph editor work).
        SAM2 contours are available but NOT auto-applied.
        User selectively clicks on nodes to apply SAM2 polygons.
        """
        super()._on_editor_ready()

        editor: ContourEditor = self._editor

        # Load SAM2 contour data (for selective apply)
        contours_path = self.temp_dir / "contours_auto.json"
        try:
            self.api_client.download_contours_auto(self.uid, contours_path)
        except APIError as exc:
            logger.warning("Failed to download contours_auto: %s", exc)
            self.status_label.setText(
                "Контуры ещё не распознаны. Shift+ЛКМ по нужным узлам, затем "
                "«Распознать выбранные» (или «Распознать все»)."
            )
            self._update_contour_stats()
            return

        if not editor.load_contours(str(contours_path)):
            self.status_label.setText("Не удалось загрузить контуры")
            self._update_contour_stats()
            return

        # Activate contour mode — no auto-apply, user clicks selectively
        editor.set_mode("apply_contour")
        editor._refresh_equipment_brushes()

        self._update_contour_stats()
        self.status_label.setText(
            "Готово — Ctrl+ЛКМ по центроиду узла применяет или снимает форму"
        )

    # =================================================================
    # On-demand recognition
    # =================================================================

    @Slot()
    def _toggle_select_recog(self):
        if self.btn_select_recog.isChecked():
            self._set_mode("select_recognize")
            self.status_label.setText(
                "Режим выбора: Shift+ЛКМ по узлам оборудования (жёлтый — выбран)"
            )
        else:
            self._set_mode("apply_contour")

    def _start_recognition(self, selected_only: bool):
        editor: ContourEditor = self._editor
        if not editor:
            return
        if selected_only:
            ann_ids = editor.get_recog_ann_idx()
            if not ann_ids:
                QMessageBox.information(
                    self, "Выбор пуст",
                    "Сначала включите «Выбрать для распознавания» и кликните по узлам.",
                )
                return
        else:
            ann_ids = None
            cnt = editor.get_contour_stats().get("with_ann_idx", 0)
            if QMessageBox.question(
                self, "Распознать все",
                f"Запустить распознавание формы для всех узлов (~{cnt})?\n"
                "На CPU это может занять несколько минут.",
            ) != QMessageBox.StandardButton.Yes:
                return
        self._stop_recog_worker()
        self.status_label.setText("Запуск распознавания контуров… (можно продолжать ждать)")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self._recog_worker = _ContourExtractWorker(self.api_client, self.uid, ann_ids)
        self._recog_worker.done.connect(self._on_recognition_done)
        # Гасит поток разрушение САМОЙ вкладки, а не её слоты: `closeEvent`
        # при закрытии вкладки не поднимается вовсе (`_remove_tab_widget`
        # делает `setParent(None)` + `deleteLater()`), а связь с
        # `_on_recognition_done` Qt рвёт вместе с получателем. Получатель
        # ЭТОЙ связи — сам поток, поэтому она вкладку переживает: иначе
        # брошенный воркер опрашивает сервер ещё до 600 с, а процесс падает
        # на его разрушении (пункт 1.x17, замер §102ж).
        self.destroyed.connect(self._recog_worker.stop)
        self._recog_worker.start()

    def _stop_recog_worker(self):
        """Остановить фоновый поток распознавания, если он запущен."""
        w = getattr(self, "_recog_worker", None)
        if w is not None and w.isRunning():
            w.stop()
            w.wait(3000)
            try:
                QApplication.restoreOverrideCursor()
            except Exception:
                pass

    def closeEvent(self, event):
        self._stop_recog_worker()
        super().closeEvent(event)

    @Slot(bool, str)
    def _on_recognition_done(self, ok: bool, msg: str):
        QApplication.restoreOverrideCursor()
        if not ok:
            self.status_label.setText(msg)
            QMessageBox.warning(self, "Распознавание контуров", msg)
            return
        editor: ContourEditor = self._editor
        contours_path = self.temp_dir / "contours_auto.json"
        try:
            self.api_client.download_contours_auto(self.uid, contours_path)
        except APIError as exc:
            self.status_label.setText(f"Контуры не скачались: {exc}")
            return
        editor.clear_recog_selection()
        if self.btn_select_recog.isChecked():
            self.btn_select_recog.setChecked(False)
        editor.load_contours(str(contours_path))
        editor.set_mode("apply_contour")
        editor._refresh_equipment_brushes()
        self.status_label.setText(
            msg + " — Ctrl+ЛКМ по центроиду узла применяет/снимает форму"
        )

    # =================================================================
    # Stats
    # =================================================================

    def _update_stats(self, stats: dict):
        """Override graph stats callback to also update contour stats."""
        super()._update_stats(stats)
        self._update_contour_stats()

    def _update_contour_stats(self):
        """Счётчик контуров в toolbar убран — метод оставлен для совместимости."""
        return

    # =================================================================
    # Apply all / Remove all
    # =================================================================

    @Slot()
    def _apply_all_contours(self):
        """Apply all contours with confidence >= 0.85."""
        editor: ContourEditor = self._editor
        if not editor:
            return

        from ui.editors.commands.contour_commands import ToggleContourCommand

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            count = 0
            for ann_id, cn in editor._ann_to_contour.items():
                node_id = editor._ann_to_node.get(ann_id)
                if not node_id:
                    continue
                if node_id in editor._applied_nodes:
                    continue
                if not cn.get("polygon_auto"):
                    continue
                if cn.get("confidence", 0) < 0.85:
                    continue

                cmd = ToggleContourCommand(editor, node_id, apply=True)
                editor.undo_mgr.execute(cmd)
                count += 1

            self.status_label.setText(f"Применено {count} контуров")
            self._update_contour_stats()
        finally:
            QApplication.restoreOverrideCursor()

    @Slot()
    def _remove_all_contours(self):
        """Remove all applied contours."""
        editor: ContourEditor = self._editor
        if not editor:
            return

        from ui.editors.commands.contour_commands import ToggleContourCommand

        count = 0
        for node_id in list(editor._applied_nodes):
            cmd = ToggleContourCommand(editor, node_id, apply=False)
            editor.undo_mgr.execute(cmd)
            count += 1

        self.status_label.setText(f"Снято {count} контуров")
        self._update_contour_stats()

    # =================================================================
    # Save -- graph + contours_validated.json
    # =================================================================

    def _save_graph(self) -> bool:
        """Override: save graph AND contours_validated.json."""
        # 1. Save graph (with updated segmentation + centroid)
        if not super()._save_graph():
            return False

        # 2. Save contours_validated.json
        try:
            self._save_contours_validated()
            return True
        except Exception as exc:
            logger.error("Failed to save contours: %s", exc)
            # По таймеру — строкой, а не модалкой посреди работы (1-38).
            if self._save_interactive:
                QMessageBox.warning(
                    self, "Ошибка",
                    f"Граф сохранён, но контуры не сохранены:\n{exc}",
                )
            else:
                self._refuse_save(
                    f"⚠️ Автосохранение: граф сохранён, контуры — нет ({exc})")
            return True  # graph saved OK, contours failed

    def _save_contours_validated(self):
        """Build and upload contours_validated.json."""
        editor: ContourEditor = self._editor
        if not editor or not editor._contour_data:
            return

        validated = deepcopy(editor._contour_data)

        for cn in validated.get("nodes", []):
            ann_id = cn.get("ann_id")
            node_id = editor._ann_to_node.get(ann_id)

            if node_id and node_id in editor._applied_nodes:
                # Use actual node segmentation (may differ from polygon_auto
                # after vertex editing or redrawing)
                node = editor.nodes.get(node_id)
                seg = node.get("segmentation") if node else None
                cn["polygon_validated"] = seg if seg else cn["polygon_auto"]
                cn["status"] = "approved"
                # Preserve was_edited from contour data (set by mark_was_edited)
                src_cn = editor._ann_to_contour.get(ann_id, {})
                cn["was_edited"] = src_cn.get("was_edited", False)
            else:
                cn["polygon_validated"] = None
                cn["status"] = "skipped"

        # Update stats
        approved = sum(
            1 for cn in validated.get("nodes", [])
            if cn.get("status") == "approved"
        )
        skipped = sum(
            1 for cn in validated.get("nodes", [])
            if cn.get("status") == "skipped"
        )
        stats = validated.get("stats", {})
        stats["auto"] = approved
        stats["manual_review"] = skipped
        validated["stats"] = stats

        path = self.temp_dir / "contours_validated.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(validated, f, indent=2, ensure_ascii=False)

        self.api_client.upload_contours_validated(self.uid, path)
        logger.info(
            "Saved contours_validated.json: %d approved, %d skipped",
            approved, skipped,
        )

        # Also save training data artifact
        self._save_contours_training(validated)

    def _save_contours_training(self, validated_data: dict):
        """Build and upload contours_training.json for SAM2 fine-tuning.

        Contains only approved polygons (accepted by engineer).
        Training script reconstructs 6-channel input from pipeline artifacts:
          - original_image → RGB crop
          - coco_validated → rough mask (ch3) + other nodes (ch5)
          - pipe_mask_refined → pipe mask (ch4)
          - polygon from this file → GT mask
        """
        editor: ContourEditor = self._editor
        if not editor:
            return

        samples = []
        from_sam2 = 0
        from_sam2_edited = 0
        from_manual = 0

        for cn in validated_data.get("nodes", []):
            if cn.get("status") != "approved":
                continue

            polygon = cn.get("polygon_validated")
            if not polygon or len(polygon) < 6:
                continue

            ann_id = cn.get("ann_id")
            has_sam2 = ann_id is not None and ann_id in editor._ann_to_contour
            was_edited = cn.get("was_edited", False)

            if has_sam2:
                source = "sam2"
                from_sam2 += 1
                if was_edited:
                    from_sam2_edited += 1
            else:
                source = "manual"
                from_manual += 1

            # bbox in COCO [x, y, w, h] format
            bbox_xywh = cn.get("bbox")
            if not bbox_xywh:
                # Fallback: convert from graph node [x1,y1,x2,y2]
                node_id = editor._ann_to_node.get(ann_id)
                node = editor.nodes.get(node_id) if node_id else None
                if node and node.get("bbox"):
                    x1, y1, x2, y2 = node["bbox"]
                    bbox_xywh = [x1, y1, x2 - x1, y2 - y1]
                else:
                    continue  # skip — no bbox available

            samples.append({
                "ann_id": ann_id,
                "category_id": cn.get("category_id"),
                "class_name": cn.get("class_name", "unknown"),
                "bbox_xywh": bbox_xywh,
                "polygon": polygon,
                "source": source,
                "was_edited": was_edited,
                "confidence": cn.get("confidence"),
                "n_points": len(polygon) // 2,
            })

        if not samples:
            logger.info("No approved contours for training data")
            return

        training_data = {
            "version": "1.0",
            "diagram_uid": self.uid,
            "samples": samples,
            "stats": {
                "total_approved": len(samples),
                "from_sam2": from_sam2,
                "from_sam2_edited": from_sam2_edited,
                "from_manual": from_manual,
            },
        }

        path = self.temp_dir / "contours_training.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(training_data, f, indent=2, ensure_ascii=False)

        try:
            self.api_client.upload_contours_training(self.uid, path)
            logger.info(
                "Saved contours_training.json: %d samples "
                "(sam2=%d, edited=%d, manual=%d)",
                len(samples), from_sam2, from_sam2_edited, from_manual,
            )
        except Exception as exc:
            logger.warning("Failed to upload contours_training: %s", exc)

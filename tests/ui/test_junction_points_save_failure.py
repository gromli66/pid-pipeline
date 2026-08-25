# -*- coding: utf-8 -*-
"""Пункт 1.x14, часть (3) — зеркало семьи 0.5 на ПУТИ ЗАПИСИ.

Четвёртый заход в одну семью, и первый — с другой стороны. 1.x9/1.23 закрыли
ЧТЕНИЕ у графовых вкладок, 1.x10 — у вкладки привязки, 1.x12 — у вкладок масок.
После них читающая сторона честная: отказ, из-за которого оператор открыл не
свою работу, он ВИДИТ. Пишущая осталась прежней.

`JunctionTab._upload_points` (`junction_tab.py:405`) ловит `except Exception`
и уходит в `logger.warning` — то есть отказ ЗАПИСИ центров не выходит наружу
вовсе. Дальше `_save_masks` как ни в чём не бывало ставит `_saved = True`
и пишет «Маски перекрёстков и мостов сохранены».

Цена ЗАМЕРЕНА (§89б), а не выведена: оператор применил к мостам размер 24 при
сохранённых 20 — на сервере остаётся 20 во ВСЕХ четырёх классах отказа (диск,
400 старого сервера, 500, обрыв сети), модалки нет ни в одном, а `_saved = True`
гасит `has_unsaved_changes()`, поэтому и вопроса при закрытии вкладки не будет.
Ровно та потеря работы, которую семья чинит с 1.23, только тише: при ЧТЕНИИ
оператор хотя бы видел чужие данные на экране, здесь он не видит ничего.

Инвариант пункта: **центры не легли на сервер → оператор ВИДИТ это, а вкладка
не считает себя сохранённой.** Граница по классу отказа сохраняется той же, что
у семьи, но выводы из неё на записи РАЗНЫЕ, и это замер, а не рассуждение:
на чтении `APIError` мог означать «артефакта законно нет» (404), на записи он
означает ровно одно — «работа оператора до сервера не доехала».

⛔ Контроль честности (`test_control_*`) обязателен и утверждает РАЗНИЦУ, а не
совпадение с состоянием «до» (`PROTOCOL §3`, замеры §76.7 и §82.14): «после
сохранения на сервере те же центры» зелено и при обезвреженной записи.

⛔ Один прогон обязан идти ПОСЛЕ чужого действия оператора, а не с чистого
листа (`PROTOCOL §3`, замер ревизии связки «превью»): `test_second_save_*`
сначала сохраняется успешно, и только потом ломает запись.

⚠ `QMessageBox` подменён с утверждением о ФАКТЕ вызова, а не таймаутом
(`PROTOCOL §5`); виджеты сносятся детерминированно (ловушка 1-32).
"""
import json
import logging
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                    # noqa: E402

pytest.importorskip("PySide6")

from pathlib import Path                                         # noqa: E402

from PySide6.QtCore import QThread                               # noqa: E402
from PySide6.QtWidgets import QMessageBox                        # noqa: E402

from ui.services.api_client import APIError                      # noqa: E402

# Растры, сохранённая работа оператора и подставной сервер — те же, что
# у читающей половины пункта 1.x12: стенд один, стороны разные.
from tests.ui.test_mask_tabs_download_failure import (           # noqa: E402
    UID, SQ, BRIDGE_SQUARES, POINTS_SAVED, FakeAPI,
    _junction_server, _download, _white_pixels,
    qapp, rasters,                                               # noqa: F401
)

#: заголовок модалки об отказе записи — по нему оператор отличает
#: «сохранено» от «сохранено не всё»
TITLE_POINTS = "Центры не сохранены"

#: размер, который оператор применяет в сценариях (сохранён другой — SQ)
SQ_APPLIED = SQ + 4


# ── подставной сервер, у которого отказывает ЗАПИСЬ ──────────────────────

class WriteFailingAPI(FakeAPI):
    """Тот же сервер 1.x12, но запись заданного типа отказывает своей ошибкой.

    Читает вкладка отсюда же, куда пишет, поэтому по блобам видно ровно то,
    что первое сохранение сделало с работой оператора.
    """

    def __init__(self, blobs, write_failures=None):
        super().__init__(blobs)
        self.write_failures = dict(write_failures or {})

    def upload_validated_mask(self, uid, mask_type, file_path):
        exc = self.write_failures.get(mask_type)
        if exc is not None:
            raise exc
        return super().upload_validated_mask(uid, mask_type, file_path)


def _server(rasters, **write_failures):
    """Сервер с сохранённой работой оператора; отказывает ЗАПИСЬ названного."""
    return WriteFailingAPI(_junction_server(rasters).blobs, write_failures)


#: полное множество классов отказа записи — перебор, а не пример
WRITE_FAILURES = [
    pytest.param(OSError("нет места на диске"), id="disk"),
    pytest.param(APIError("Invalid mask_type 'junction_points_validated'", 400),
                 id="old-server-400"),
    pytest.param(APIError("Internal Server Error", 500), id="server-500"),
    pytest.param(APIError("Connection failed after 4 attempts", 0), id="network"),
]


# ── харнесс ──────────────────────────────────────────────────────────────

@pytest.fixture
def dialogs(monkeypatch):
    """Все модалки вкладки → список (title, text).

    Отвечает `Yes`: единственный вопрос на пути сценариев — подтверждение
    «Применить размер ко ВСЕМ мостам», и оператор на него соглашается.
    """
    seen = []

    def _rec(parent, title, text, *a, **kw):
        seen.append((title, text))
        return QMessageBox.StandardButton.Yes

    for name in ("warning", "critical", "information", "question"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(_rec))
    # Вопрос «да / отмена» с пункта 5.3 собирается своими кнопками (русскими),
    # мимо статической двери `QMessageBox.question`, — подменяется отдельно.
    from ui.tabs.blind_overwrite import BlindOverwriteGuard
    monkeypatch.setattr(
        BlindOverwriteGuard, "_ask_yes_cancel",
        lambda self, title, text:
            _rec(self, title, text) == QMessageBox.StandardButton.Yes)
    return seen


@pytest.fixture
def open_junction(qapp, monkeypatch):
    """Открыть вкладку тем словарём, который отдал НАСТОЯЩИЙ загрузчик."""
    from ui.tabs.junction_tab import JunctionTab

    opened = []

    def _open(api):
        monkeypatch.setattr(
            JunctionTab, "_download_artifacts",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        tab = JunctionTab(UID, "проба 1.x14", api)
        opened.append(tab)
        artifacts, error = _download("junction", api, tab.temp_dir)
        assert error is None, f"загрузка упала до сценария записи: {error}"
        tab._on_downloaded(artifacts)
        return tab

    yield _open
    for tab in opened:
        # ⛔ Снос детерминированный (ловушка 1-32): брошенные на сборщик мусора
        # виджеты Qt детонируют отложенным удалением в ЧУЖОМ наборе.
        tab.deleteLater()
    qapp.processEvents()


def _apply_bridge_size(tab, size: int):
    """Действие оператора: применить к мостам другой размер."""
    tab._editor.current_class = 2                 # мосты
    tab.spin_obj_size.setValue(size)
    tab._apply_object_size()


def _size_on_server(api) -> int:
    """Размер мостов, который увидит СЛЕДУЮЩЕЕ открытие вкладки."""
    return json.loads(
        api.blobs["junction_points_validated"].decode())["bridges"][0]["size"]


def _titles(dialogs) -> list:
    return [title for title, _ in dialogs]


# =========================================================================
# 1. Контроль честности стенда
# =========================================================================

def test_control_points_write_reaches_the_server(rasters, open_junction, dialogs):
    """Запись центров ДОХОДИТ и ВИДНА читателю — иначе стенд ничего не значит.

    ⛔ Утверждается РАЗНИЦА (`PROTOCOL §3`): оператор применяет к мостам ДРУГОЙ
    размер, и он обязан появиться у читателя. Контроль «на сервере столько же
    центров, сколько было» зелен и при обезвреженной записи.
    """
    api = _server(rasters)
    tab = open_junction(api)
    assert _size_on_server(api) == SQ, "стенд начал не с того размера"

    _apply_bridge_size(tab, SQ_APPLIED)
    assert tab._save_masks() is True

    assert _size_on_server(api) == SQ_APPLIED, (
        "запись центров не видна читателю — на таком стенде «центры потеряны» "
        "ничего не доказывает")


def test_healthy_save_reports_success_and_asks_nothing(rasters, open_junction,
                                                       dialogs):
    """Обратная граница: всё прошло → успех, ни одной модалки об отказе."""
    api = _server(rasters)
    tab = open_junction(api)

    _apply_bridge_size(tab, SQ_APPLIED)
    assert tab._save_masks() is True

    assert tab._saved is True
    assert tab.has_unsaved_changes() is False
    assert TITLE_POINTS not in _titles(dialogs), (
        f"вкладка жалуется на успешной записи: {_titles(dialogs)}")
    assert "сохранены" in tab.status_label.text()


# =========================================================================
# 2. Сценарий уровня дефекта: отказ записи центров
# =========================================================================

@pytest.mark.parametrize("exc", WRITE_FAILURES)
def test_failed_points_write_is_visible_to_the_operator(
        rasters, open_junction, dialogs, exc):
    """Любой класс отказа записи центров оператор ВИДИТ.

    До правки все четыре класса были неразличимы и молчали (§89б): «Маски
    перекрёстков и мостов сохранены» при 20 на сервере против применённых 24.
    """
    api = _server(rasters, junction_points_validated=exc)
    tab = open_junction(api)

    _apply_bridge_size(tab, SQ_APPLIED)
    tab._save_masks()

    assert TITLE_POINTS in _titles(dialogs), (
        "отказ записи центров проглочен молча — оператор считает работу "
        f"сохранённой; модалки: {_titles(dialogs)}")


@pytest.mark.parametrize("exc", WRITE_FAILURES)
def test_failed_points_write_does_not_claim_a_full_save(
        rasters, open_junction, dialogs, exc):
    """Статус вкладки не говорит «сохранены», когда центры не легли."""
    api = _server(rasters, junction_points_validated=exc)
    tab = open_junction(api)

    _apply_bridge_size(tab, SQ_APPLIED)
    tab._save_masks()

    text = tab.status_label.text()
    assert "Маски перекрёстков и мостов сохранены" not in text, (
        f"вкладка объявляет полное сохранение при непрошедшей записи: {text!r}")


@pytest.mark.parametrize("exc", WRITE_FAILURES)
def test_failed_points_write_keeps_the_tab_unsaved(
        rasters, open_junction, dialogs, exc):
    """`_saved` не взводится: иначе вкладка закроется без вопроса.

    Это вторая половина цены и она почти невидима: `has_unsaved_changes()`
    у вкладки читает `_saved`, а `diagram_workspace` по нему решает, спрашивать
    ли при закрытии. Взведённый флаг = работа исчезает без единого следа.
    """
    api = _server(rasters, junction_points_validated=exc)
    tab = open_junction(api)

    _apply_bridge_size(tab, SQ_APPLIED)
    result = tab._save_masks()

    assert result is False, "неудавшееся сохранение отчиталось успехом"
    assert tab._saved is False
    assert tab.has_unsaved_changes() is True, (
        "вкладка считает себя сохранённой — при закрытии вопроса не будет")


@pytest.mark.parametrize("exc", WRITE_FAILURES)
def test_failed_points_write_leaves_a_log_line(rasters, open_junction,
                                               dialogs, caplog, exc):
    """Д2: причина отказа есть в логе клиента, а не только на экране."""
    api = _server(rasters, junction_points_validated=exc)
    tab = open_junction(api)
    _apply_bridge_size(tab, SQ_APPLIED)

    with caplog.at_level(logging.WARNING, logger="ui.tabs.junction_tab"):
        tab._save_masks()

    lines = [r.getMessage() for r in caplog.records
             if r.name == "ui.tabs.junction_tab" and r.levelno >= logging.WARNING]
    assert any("ентр" in m for m in lines), \
        f"в логе вкладки нет строки об отказе записи центров: {lines}"


def test_masks_that_did_land_are_not_rolled_back(rasters, open_junction,
                                                 dialogs, tmp_path):
    """Граница правки: маски, которые ДОШЛИ, на сервере остаются.

    Отказ центров — не повод объявить потерянным то, что уже записано:
    оператор теряет ровно центры, и ровно про них ему и говорят.
    """
    api = _server(rasters,
                  junction_points_validated=APIError("boom", 500))
    tab = open_junction(api)

    tab._editor.current_class = 2
    tab._editor.set_square_size(SQ)
    tab._editor.add_square(200, 240)              # оператор поставил ещё один
    tab._save_masks()

    assert "bridge_mask_validated" in api.saves, "маска мостов не доехала"
    assert _white_pixels(api.blobs["bridge_mask_validated"], tmp_path) == (
        len(BRIDGE_SQUARES) * SQ * SQ + SQ * SQ), (
        "маска мостов откачена из-за отказа СОСЕДНЕЙ записи")


# =========================================================================
# 3. Прогон ПОСЛЕ чужого действия оператора (не с чистого листа)
# =========================================================================

def test_second_save_after_a_successful_one_still_reports_the_failure(
        rasters, open_junction, dialogs):
    """Предыстория: вкладка уже сохранялась успешно — и всё равно честна.

    ⛔ `PROTOCOL §3`: тест на СВЕЖЕМ объекте не проверяет взаимодействие
    с предысторией. Здесь первое сохранение проходит целиком (взводит `_saved`
    и двигает `_undo_baseline`), и только второе упирается в отказ записи —
    то есть проверяется путь, на котором флаги уже НЕ в начальном состоянии.
    """
    api = _server(rasters)
    tab = open_junction(api)

    _apply_bridge_size(tab, SQ_APPLIED)
    assert tab._save_masks() is True                    # первое — успешное
    assert tab._saved is True
    assert _size_on_server(api) == SQ_APPLIED
    dialogs.clear()

    api.write_failures["junction_points_validated"] = APIError("boom", 503)
    _apply_bridge_size(tab, SQ_APPLIED + 6)             # оператор правит дальше
    assert tab._save_masks() is False

    assert TITLE_POINTS in _titles(dialogs), (
        "после успешного сохранения отказ снова стал незаметным")
    assert tab._saved is False, "флаг сохранённости остался от прошлого раза"
    assert _size_on_server(api) == SQ_APPLIED, (
        "на сервере оказался размер, который туда не доехал")


def test_points_written_by_a_previous_save_are_not_destroyed(
        rasters, open_junction, dialogs):
    """Отказ ВТОРОЙ записи не портит центры, записанные ПЕРВОЙ."""
    api = _server(rasters)
    tab = open_junction(api)

    _apply_bridge_size(tab, SQ_APPLIED)
    assert tab._save_masks() is True
    assert _size_on_server(api) == SQ_APPLIED

    api.write_failures["junction_points_validated"] = OSError("диск отвалился")
    _apply_bridge_size(tab, SQ_APPLIED + 6)
    tab._save_masks()

    assert _size_on_server(api) == SQ_APPLIED, (
        "прошлая успешная запись центров затёрта отказавшей")
    assert len(json.loads(
        api.blobs["junction_points_validated"].decode())["bridges"]) == (
        len(POINTS_SAVED["bridges"])), "центры оператора потеряны целиком"


def test_failed_points_write_blocks_the_confirm(rasters, open_junction, dialogs):
    """Следствие, названное в `UI_GUIDE §5.3`: подтвердить вкладку нечем.

    `_on_confirm` идёт через тот же `_save_masks`, поэтому непрошедшая запись
    центров не даёт объявить этап законченным. Это осознанная граница правки,
    а не побочный эффект: сохранённой вкладка не считается — подтверждать
    нечего.
    """
    api = _server(rasters, junction_points_validated=APIError("boom", 500))
    tab = open_junction(api)
    emitted = []
    tab.confirmed.connect(lambda: emitted.append(True))

    _apply_bridge_size(tab, SQ_APPLIED)
    tab._on_confirm()

    assert emitted == [], "этап подтверждён при непрошедшей записи центров"
    assert tab._confirmed is False

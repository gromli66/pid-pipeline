# -*- coding: utf-8 -*-
"""Пункт 1.x12 дороги — политика `swallow` у вкладок масок (junction / pipe).

Третий заход в одну семью: решение 0.5 («отказ СЕРВЕРА у необязательного
артефакта терпим, отказ ЛОКАЛЬНОГО ДИСКА — нет») жило только у графовой
вкладки, 1.x10 привело к нему вкладку привязки, а у двух вкладок масок
осталось шесть заданий с дефолтным `swallow=(Exception,)` — четыре у junction,
два у pipe (замер 1.x10 §82.6). Политика копируется ВМЕСТЕ с заданием
и теряется молча, поэтому здесь она сторожится не примерами, а перебором
ВСЕХ необязательных заданий обеих вкладок.

Цена молчания у junction ровно та же, что была у привязки, и здесь она
ЗАМЕРЕНА, а не выведена:

  * `bridge_mask_validated` не доехал → `_on_downloaded` зовёт
    `load_images(mask2_path="")`, редактор кладёт себе ПУСТУЮ маску мостов
    (`square_mask_editor.py:300-306`), а `_save_masks` шлёт её на сервер
    БЕЗУСЛОВНО (`upload_validated_mask(..., "bridge_mask_validated", ...)`) —
    сохранённые мосты оператора стираются;
  * `junction_points_validated` не доехал → `_load_points(None)` доопределяет
    центры из масок (`ensure_points`), а `_upload_points` шлёт их обратно —
    правленые оператором центры заменяются машинными.

Путь достижим и БЕЗ оператора: `ui/services/autosave.py:_SAVE_METHODS` зовёт
у `JunctionTab` тот же `_save_masks` по таймеру (120 с, включено по умолчанию).

⚠ `QMessageBox` подменён с утверждением о ФАКТЕ вызова, а не таймаутом
(`PROTOCOL §5`); виджеты сносятся детерминированно (ловушка 1-32: брошенные
на сборщик мусора виджеты Qt детонируют в ЧУЖОМ наборе).
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                    # noqa: E402

pytest.importorskip("PySide6")

from pathlib import Path                                         # noqa: E402

from PySide6.QtCore import Qt, QThread                           # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPen         # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox          # noqa: E402

from ui.editors.square_mask_editor import SQUARE_SIZE            # noqa: E402
from ui.services.api_client import APIError                      # noqa: E402

UID = "1cx12a01"
W, H = 400, 300

#: сохранённая работа оператора в маске мостов — три квадрата 20×20
BRIDGE_SQUARES = [(80, 80), (200, 80), (320, 80)]
SQ = 20
BRIDGE_WHITE = len(BRIDGE_SQUARES) * SQ * SQ          # 1200 белых пикселей

#: сохранённые оператором центры мостов (их пишет обратно `_upload_points`)
POINTS_SAVED = {
    "junctions": [{"x": 120, "y": 200, "size": 21}],
    "bridges": [{"x": x, "y": y, "size": SQ} for x, y in BRIDGE_SQUARES],
}
N_BRIDGE_POINTS = len(POINTS_SAVED["bridges"])

#: размер оператора обязан отличаться от машинного дефолта, иначе подмена
#: центров машинными не наблюдаема
assert SQ != SQUARE_SIZE


# ── данные ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _png(img: QImage, path: Path) -> bytes:
    assert img.save(str(path))
    return path.read_bytes()


def _white_pixels(data: bytes, tmp: Path) -> int:
    """Сколько белых пикселей в PNG-маске — по ним видно, чья это работа."""
    path = tmp / "probe.png"
    path.write_bytes(data)
    img = QImage(str(path))
    assert not img.isNull(), "маска не читается"
    return sum(1 for y in range(img.height()) for x in range(img.width())
               if img.pixelColor(x, y).value() >= 128)


@pytest.fixture(scope="module")
def rasters(qapp, tmp_path_factory) -> dict:
    """Оригинал, маска перекрёстков, сохранённая маска мостов, скелет."""
    root = tmp_path_factory.mktemp("mask_rasters")

    original = QImage(W, H, QImage.Format.Format_RGB32)
    original.fill(QColor("white"))

    def squares(centers) -> QImage:
        img = QImage(W, H, QImage.Format.Format_RGB32)
        img.fill(QColor("black"))
        p = QPainter(img)
        p.setPen(QPen(Qt.PenStyle.NoPen))
        p.setBrush(QColor("white"))
        for cx, cy in centers:
            p.drawRect(cx - SQ // 2, cy - SQ // 2, SQ, SQ)
        p.end()
        return img

    def lines() -> QImage:
        img = QImage(W, H, QImage.Format.Format_RGB32)
        img.fill(QColor("black"))
        p = QPainter(img)
        p.setPen(QPen(QColor("white"), 3, Qt.SolidLine, Qt.FlatCap))
        p.drawLine(40, 200, 360, 200)
        p.end()
        return img

    def lines_plus_square() -> QImage:
        """Сохранённая оператором маска труб: скелет ПЛЮС дорисованный участок."""
        img = lines()
        p = QPainter(img)
        p.setPen(QPen(Qt.PenStyle.NoPen))
        p.setBrush(QColor("white"))
        p.drawRect(40 - SQ // 2, 60 - SQ // 2, SQ, SQ)
        p.end()
        return img

    return {
        "original_image": _png(original, root / "original.png"),
        "junction_mask": _png(squares([(120, 200)]), root / "junction.png"),
        # сырая маска перекрёстков от сборщика: ДВА пятна вместо одного —
        # по их числу видно, чья маска доехала до оператора
        "junction_mask_raw": _png(squares([(120, 200), (260, 120)]),
                                  root / "junction_raw.png"),
        "bridge_mask": _png(squares(BRIDGE_SQUARES), root / "bridge.png"),
        "skeleton": _png(lines(), root / "skeleton.png"),
        "pipe_mask": _png(lines(), root / "pipe.png"),
        # сохранённая оператором маска труб отличается от сырого скелета —
        # иначе «у оператора оказался фолбэк» ненаблюдаемо
        "pipe_validated": _png(lines_plus_square(), root / "pipe_validated.png"),
        "points": json.dumps(POINTS_SAVED).encode(),
        "coco": json.dumps({"annotations": []}).encode(),
    }


# ── подставной сервер ────────────────────────────────────────────────────

class FakeAPI:
    """Отдаёт заданные байты; чего нет — `APIError` 404; заданное — своей ошибкой.

    Пишет сохранённое ТУДА ЖЕ, откуда вкладка потом читает: только так видно,
    что первое сохранение сделало с работой оператора.
    """

    def __init__(self, blobs, failures=None):
        self.blobs = dict(blobs)
        self.failures = dict(failures or {})
        self.calls = []
        self.saves = []

    def download_artifact(self, uid, artifact_type, dest_path):
        self.calls.append(artifact_type)
        exc = self.failures.get(artifact_type)
        if exc is not None:
            raise exc
        data = self.blobs.get(artifact_type)
        if data is None:
            raise APIError(f"artifact {artifact_type} not found", 404)
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        return dest_path

    def upload_validated_mask(self, uid, mask_type, file_path):
        self.blobs[mask_type] = Path(file_path).read_bytes()
        self.saves.append(mask_type)
        return {}

    def upload_updated_nodes(self, uid, file_path):
        self.blobs["coco_validated"] = Path(file_path).read_bytes()
        self.saves.append("coco_validated")
        return {}


def _junction_server(rasters, **failures):
    """Сервер, на котором лежит сохранённая работа оператора."""
    return FakeAPI({
        "original_image": rasters["original_image"],
        "junction_mask_validated": rasters["junction_mask"],
        "bridge_mask_validated": rasters["bridge_mask"],
        "skeleton_final": rasters["skeleton"],
        "coco_validated": rasters["coco"],
        "junction_points_validated": rasters["points"],
    }, failures)


def _pipe_server(rasters, **failures):
    """Сервер вкладки труб: сохранённая маска оператора ОТЛИЧАЕТСЯ от сырой.

    `skeleton_mask` — второй кандидат цепочки (фолбэк сборщика); до пункта 1-41
    подмена сохранённой маски этим фолбэком проходила молча.
    """
    return FakeAPI({
        "original_image": rasters["original_image"],
        "pipe_mask_validated": rasters["pipe_validated"],
        "skeleton_mask": rasters["skeleton"],
        "coco_validated": rasters["coco"],
        "pipe_mask": rasters["pipe_mask"],
    }, failures)


@pytest.fixture
def dialogs(monkeypatch):
    """Все модалки вкладки → список (title, text).

    По умолчанию отвечает `Yes`: единственный вопрос на старом пути тестов —
    подтверждение «Применить размер ко ВСЕМ мостам», и оператор в контрольном
    сценарии на него соглашается. Для `warning`/`critical` возврат никто
    не читает.

    `dialogs.answer` переключает ответ СЛЕДУЮЩИХ диалогов: пункт 1-41 завёл
    у этих вкладок вопрос о слепой перезаписи, и обе его ветки («да» снимает
    запрет насовсем, «нет» отменяет запись) обязаны проверяться отдельно.
    """

    class _Seen(list):
        answer = QMessageBox.StandardButton.Yes

    seen = _Seen()

    def _rec(parent, title, text, *a, **kw):
        seen.append((title, text))
        return seen.answer

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


def _download(kind, api, tmp_dir):
    """Прогнать НАСТОЯЩИЙ список заданий вкладки синхронно: (artifacts, error)."""
    from ui.services.artifact_downloader import ArtifactDownloader

    if kind == "junction":
        from ui.tabs.junction_tab import _ARTIFACTS as jobs
    else:
        from ui.tabs.pipe_tab import _ARTIFACTS as jobs

    dl = ArtifactDownloader(api, UID, tmp_dir, jobs)
    out = {}
    dl.finished.connect(lambda a: out.__setitem__("artifacts", a))
    dl.error.connect(lambda m: out.__setitem__("error", m))
    dl.run()
    return out.get("artifacts"), out.get("error")


@pytest.fixture
def open_junction(qapp, monkeypatch):
    """Открыть вкладку тем словарём, который отдал НАСТОЯЩИЙ загрузчик.

    Поток снят; шов «загрузчик → вкладка» не подменён — результат уходит
    во вкладку тем же слотом `_on_downloaded`, которым его доставляет бой.
    Отказ загрузки — законный исход пункта, поэтому сессия обязана уметь его
    пережить, а не падать своим `assert`.
    """
    from ui.tabs.junction_tab import JunctionTab

    opened = []

    def _open(api):
        monkeypatch.setattr(
            JunctionTab, "_download_artifacts",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        tab = JunctionTab(UID, "проба 1.x12", api)
        opened.append(tab)
        artifacts, error = _download("junction", api, tab.temp_dir)
        if error is not None:
            return None, error
        tab._on_downloaded(artifacts)
        return tab, None

    yield _open
    for tab in opened:
        # ⛔ Снос детерминированный (ловушка 1-32): брошенная вкладка оставляет
        # в очереди Qt отложенные удаления, а падает на них ЧУЖОЙ набор — тот,
        # что первым крутит `processEvents`. Очередь выпивается здесь, своим.
        tab.deleteLater()
    qapp.processEvents()


def _bridge_on_server(api, tmp: Path) -> int:
    return _white_pixels(api.blobs["bridge_mask_validated"], tmp)


def _bridge_points_on_server(api) -> int:
    stored = json.loads(api.blobs["junction_points_validated"].decode())
    return len(stored["bridges"])


def _bridge_size_on_server(api) -> int:
    """Последний размер, применённый оператором к мостам.

    Ради него центры и сохраняются: без файла экстрактор доопределяет их
    с дефолтом `square_mask_editor.SQUARE_SIZE` = 15 и ужатых квадратов
    не разбирает (докстрока `_upload_points`).
    """
    stored = json.loads(api.blobs["junction_points_validated"].decode())
    return stored["bridges"][0]["size"]


# =========================================================================
# 1. Цена отказа: сценарий уровня дефекта
# =========================================================================

def test_a_save_reaches_the_server_and_the_reader_sees_it(
        rasters, open_junction, dialogs, tmp_path):
    """Контроль честности следующих тестов: запись ДОХОДИТ и ВИДНА.

    ⛔ Проверять «после сохранения на сервере столько же, сколько было» нельзя:
    такой контроль зелен и при обезвреженной записи (`PROTOCOL §3`, замеры
    §76.7 и §82.14). Экзамен сдаёт только РАЗНИЦА — оператор ставит ещё один
    мост, и он обязан появиться у читателя.
    """
    api = _junction_server(rasters)
    tab, error = open_junction(api)
    assert error is None and tab is not None

    tab._editor.current_class = 2                 # мосты
    tab._editor.set_square_size(SQ)
    tab._editor.add_square(200, 240)              # оператор поставил ещё один
    assert tab._save_masks() is True

    assert "bridge_mask_validated" in api.saves, "сохранение до сервера не дошло"
    assert _bridge_on_server(api, tmp_path) == BRIDGE_WHITE + SQ * SQ, (
        "запись не видна читателю — на таком стенде «работа цела» ничего не значит")


def test_a_points_save_reaches_the_server_and_the_reader_sees_it(
        rasters, open_junction, dialogs):
    """Тот же контроль для ВТОРОЙ половины `_save_masks` — записи центров.

    ⛔ Без него тест ниже был бы декоративным: обезврежен `_upload_points` —
    на сервере остаётся файл оператора, и «работа цела» зелено по совпадению
    (`PROTOCOL §3`, замер §82.14). Экзамен сдаёт РАЗНИЦА: оператор применяет
    к мостам другой размер, и он обязан появиться у читателя.
    """
    api = _junction_server(rasters)
    tab, error = open_junction(api)
    assert error is None and tab is not None
    assert _bridge_size_on_server(api) == SQ, "стенд начал не с того размера"

    tab._editor.current_class = 2                 # мосты
    tab.spin_obj_size.setValue(SQ + 4)
    tab._apply_object_size()                      # «Применить размер»
    assert tab._save_masks() is True

    assert "junction_points_validated" in api.saves, "центры до сервера не дошли"
    assert _bridge_size_on_server(api) == SQ + 4, (
        "запись центров не видна читателю — на таком стенде «центры целы» "
        "ничего не значит")


def test_disk_failure_never_costs_the_operator_the_saved_bridge_mask(
        rasters, open_junction, dialogs, tmp_path):
    """Сценарий целиком: отказ диска → маска мостов на сервере цела.

    До правки: `OSError` глотался, вкладка открывалась с ПУСТОЙ маской мостов,
    и первое же сохранение писало эту пустоту поверх работы оператора
    (`_save_masks` шлёт `bridge_mask_validated` БЕЗУСЛОВНО). Путь достижим
    и без оператора — `_SAVE_METHODS["JunctionTab"] = "_save_masks"`.
    """
    api = _junction_server(rasters,
                           bridge_mask_validated=OSError("диск отвалился"),
                           bridge_mask=OSError("диск отвалился"))

    tab, error = open_junction(api)
    if tab is not None:                       # вкладка всё же открылась —
        tab._save_masks()                     # ... вот чем кончается её работа

    assert _bridge_on_server(api, tmp_path) == BRIDGE_WHITE, (
        "сохранённые мосты оператора затёрты из-за отказа ЛОКАЛЬНОГО диска")
    assert error is not None, "вкладка обязана уйти в отказ, а не работать вслепую"


def test_disk_failure_never_costs_the_operator_the_saved_points(
        rasters, open_junction, dialogs, tmp_path):
    """То же для центров квадратов: их пишет обратно `_upload_points`."""
    api = _junction_server(rasters,
                           junction_points_validated=OSError("нет места"),
                           junction_points=OSError("нет места"))

    tab, error = open_junction(api)
    if tab is not None:
        tab._save_masks()

    assert _bridge_points_on_server(api) == N_BRIDGE_POINTS, (
        "правленые оператором центры заменены машинными из-за отказа диска")
    assert _bridge_size_on_server(api) == SQ, (
        f"размер мостов подменён машинным дефолтом {SQUARE_SIZE} — при "
        "следующем открытии экстрактор не разберёт ужатые квадраты")
    assert error is not None


@pytest.fixture
def open_pipe(qapp, monkeypatch):
    """То же для вкладки труб: настоящий загрузчик, настоящий слот, без потока."""
    from ui.tabs.pipe_tab import PipeTab

    opened = []

    def _open(api):
        monkeypatch.setattr(
            PipeTab, "_download_artifacts",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        tab = PipeTab(UID, "проба 1.x12", api)
        opened.append(tab)
        artifacts, error = _download("pipe", api, tab.temp_dir)
        if error is not None:
            return None, error
        tab._on_downloaded(artifacts)
        return tab, None

    yield _open
    for tab in opened:
        tab.deleteLater()
    qapp.processEvents()


def test_disk_failure_on_pipe_mask_is_visible_to_the_operator(
        rasters, open_pipe, dialogs):
    """Вкладка труб: отказ диска виден, а не спрятан за дефолтной кистью.

    Обратно на сервер эта маска не уходит, поэтому цена другая — до правки
    вкладка молча открывалась со стартовой шириной «по умолчанию» вместо
    медианной толщины труб, ровно как в дефекте 1.x3. Политика одна на все
    необязательные задания, и здесь она проверена сценарием, а не только
    составом заданий.
    """
    api = _pipe_server(rasters, pipe_mask=OSError("диск отвалился"))

    tab, error = open_pipe(api)

    assert tab is None, "вкладка открылась вслепую"
    assert error is not None and "диск отвалился" in error


def test_absent_pipe_mask_still_opens_the_tab_silently(rasters, open_pipe, dialogs):
    """Порог с другой стороны: 404 маски сегментации вкладку не роняет."""
    api = _pipe_server(rasters)
    del api.blobs["pipe_mask"]

    tab, error = open_pipe(api)

    assert error is None and tab is not None
    assert dialogs == []


# =========================================================================
# 2. Политика на КАЖДОМ необязательном задании обеих вкладок
# =========================================================================

@pytest.mark.parametrize("kind,expected", [("junction", 4), ("pipe", 2)])
def test_every_optional_job_carries_the_policy(kind, expected):
    """Политика не теряется при копировании задания — это и есть её замок.

    Абсолютное требование ко ВСЕМ необязательным заданиям вкладки, а не
    к известным сегодня: `swallow` копируется вместе с заданием, и потерять
    его в следующем — ровно тот дефект, который чинит этот пункт.
    """
    if kind == "junction":
        from ui.tabs.junction_tab import _ARTIFACTS as jobs
    else:
        from ui.tabs.pipe_tab import _ARTIFACTS as jobs

    optional = [j for j in jobs if not j.required]
    assert len(optional) == expected, "изменился состав заданий — политику пересмотреть"
    assert all(j.swallow == (APIError,) for j in optional), \
        [j.fetches[0].source for j in optional if j.swallow != (APIError,)]


# =========================================================================
# 3. Границы: отказ СЕРВЕРА пункт не трогает
# =========================================================================

def test_absent_bridge_mask_still_opens_the_tab_silently(
        rasters, open_junction, dialogs):
    """404 = маски мостов законно нет (первый заход) — ни ошибки, ни модалки."""
    api = _junction_server(rasters)
    del api.blobs["bridge_mask_validated"]

    tab, error = open_junction(api)

    assert error is None and tab is not None
    assert dialogs == [], "404 — законный первый заход, пугать оператора нечем"


# =========================================================================
# 4. Пункт 1-41: молчащий 5xx на ЧТЕНИИ у вкладок масок
#
# 1.x12 подвинул границу по ДИСКУ и осадок назвал прямо: «сохранённые мосты
# могли лежать на сервере, и вкладка откроется без них так же молча, как при
# 404; лечится это не `swallow`, а `failure_key`». Тест, стороживший тот
# остаток как ФАКТ, заменён здесь на приёмку: 404 по-прежнему молчит, а любой
# другой отказ виден оператору И ЗАПИРАЕТ запись (та же дверь, что 1.x9
# у графовой вкладки и 1.x14 у привязки).
#
# ⚠ Поток вкладки в этих сценариях не поднимается, и это не слепота вида 1-43:
# проверяемое поведение — что вкладка сделала с ПОЛУЧЕННЫМ словарём артефактов
# и что ушло на сервер при сохранении. Ни одна его стадия от того, бежит ли
# загрузочный поток, не зависит; стадии жизни потока сторожит
# `test_tab_close_stops_threads.py`.
# =========================================================================

#: заголовки предупреждений — по ним оператор отличает отказ от «его нет»
TITLE_JUNCTION = "Сохранённая маска перекрёстков не загружена"
TITLE_BRIDGE = "Сохранённая маска мостов не загружена"
TITLE_POINTS = "Сохранённые центры не загружены"
TITLE_PIPE = "Сохранённая маска труб не загружена"
TITLE_BLIND = "Сохранение затрёт серверную копию"


def _titles(dialogs):
    return [title for title, _ in dialogs]


def _junction_on_server(api, tmp: Path) -> int:
    return _white_pixels(api.blobs["junction_mask_validated"], tmp)


def test_server_failure_on_bridge_mask_is_visible_and_locks_the_write(
        rasters, open_junction, dialogs, tmp_path):
    """5xx у маски мостов: оператор предупреждён, «нет» спасает его работу.

    До пункта 1-41 обе половины молчали: задание не несло `failure_key`,
    вкладка открывалась с ПУСТОЙ маской мостов (`bridge_mask` тоже 502),
    а `_save_masks` слал эту пустоту на сервер безусловно.
    """
    api = _junction_server(rasters,
                           bridge_mask_validated=APIError("bad gateway", 502),
                           bridge_mask=APIError("bad gateway", 502))

    tab, error = open_junction(api)

    assert error is None and tab is not None, "5xx необязательного — не отказ вкладки"
    assert TITLE_BRIDGE in _titles(dialogs), (
        f"оператор не предупреждён об отказе 502: {_titles(dialogs)}")

    dialogs.clear()
    dialogs.answer = QMessageBox.StandardButton.Cancel
    assert tab._save_masks() is False, "запись не заперта — вопроса не было"
    assert TITLE_BLIND in _titles(dialogs), (
        f"вопрос о слепой перезаписи не задан: {_titles(dialogs)}")
    assert "bridge_mask_validated" not in api.saves, "пустота ушла на сервер"
    assert _bridge_on_server(api, tmp_path) == BRIDGE_WHITE, (
        "сохранённые мосты оператора затёрты из-за отказа СЕРВЕРА")


def test_operator_may_allow_the_blind_overwrite_and_is_asked_once(
        rasters, open_junction, dialogs, tmp_path):
    """Порог с другой стороны — и ПОСЛЕ предыстории, а не с чистого листа.

    «Да» — решение оператора: запись проходит. Второе сохранение вопроса уже
    не задаёт (запрет снят насовсем), то есть проверяется взаимодействие
    с предысторией сессии, а не поведение свежего объекта (`PROTOCOL §3`).
    """
    api = _junction_server(rasters,
                           bridge_mask_validated=APIError("bad gateway", 502),
                           bridge_mask=APIError("bad gateway", 502))

    tab, error = open_junction(api)
    assert error is None and tab is not None

    tab._editor.current_class = 2
    tab._editor.set_square_size(SQ)
    tab._editor.add_square(200, 240)              # работа оператора вслепую

    dialogs.clear()
    dialogs.answer = QMessageBox.StandardButton.Yes
    assert tab._save_masks() is True, "оператор разрешил — запись обязана пройти"
    assert TITLE_BLIND in _titles(dialogs)
    assert _bridge_on_server(api, tmp_path) == SQ * SQ, (
        "на сервер ушло не то, что нарисовал оператор поверх пустоты")

    dialogs.clear()
    tab._editor.add_square(260, 240)
    assert tab._save_masks() is True
    assert TITLE_BLIND not in _titles(dialogs), (
        "вопрос вернулся после «да» — запрет обязан сниматься насовсем")


def test_server_failure_on_saved_junction_mask_is_visible_and_locks_the_write(
        rasters, open_junction, dialogs, tmp_path):
    """5xx у ОБЯЗАТЕЛЬНОЙ цепочки: фолбэк на сырую маску сборщика.

    Цепочка `junction_mask_validated` → `junction_mask` брала фолбэк молча,
    хотя это ровно дефект 1.23 в другой вкладке: оператору открывали НЕ ЕГО
    работу, а `_save_masks` писал её обратно поверх сохранённой.
    """
    api = _junction_server(rasters,
                           junction_mask_validated=APIError("bad gateway", 502))
    api.blobs["junction_mask"] = rasters["junction_mask_raw"]   # фолбэк сборщика

    tab, error = open_junction(api)

    assert error is None and tab is not None, "фолбэк обязан состояться"
    assert TITLE_JUNCTION in _titles(dialogs), (
        f"подмена сохранённой маски сырой прошла молча: {_titles(dialogs)}")

    dialogs.clear()
    dialogs.answer = QMessageBox.StandardButton.Cancel
    assert tab._save_masks() is False
    assert "junction_mask_validated" not in api.saves
    assert _junction_on_server(api, tmp_path) == SQ * SQ, (
        "сохранённая маска перекрёстков затёрта сырой из-за отказа сервера")


def test_server_failure_on_points_is_visible_and_locks_the_write(
        rasters, open_junction, dialogs):
    """5xx у центров: цена — подмена применённого размера машинным."""
    api = _junction_server(
        rasters,
        junction_points_validated=APIError("bad gateway", 502),
        junction_points=APIError("bad gateway", 502))

    tab, error = open_junction(api)

    assert error is None and tab is not None
    assert TITLE_POINTS in _titles(dialogs), (
        f"отказ 502 у центров прошёл молча: {_titles(dialogs)}")

    dialogs.clear()
    dialogs.answer = QMessageBox.StandardButton.Cancel
    assert tab._save_masks() is False
    assert "junction_points_validated" not in api.saves
    assert _bridge_size_on_server(api) == SQ, (
        f"размер мостов подменён машинным дефолтом {SQUARE_SIZE}")


def test_unreadable_points_file_is_visible_and_locks_the_write(
        rasters, open_junction, dialogs):
    """«Файл лёг, но не читается» — класс, которого семья не закрывала.

    Отказы сети, диска и сервера закрыты 0.5 → 1.23 → 1.x10 → 1.x12 → 1.x14,
    а ПОРЧА СОДЕРЖИМОГО скачанного проходила мимо всех слоёв: `_load_points`
    ловил `(OSError, ValueError)` строкой в лог, `ensure_points` доопределял
    центры машинным дефолтом, и `_upload_points` отправлял их обратно.
    """
    api = _junction_server(rasters)
    api.blobs["junction_points_validated"] = b'{"junctions": [{"x": 1,'  # обрыв

    tab, error = open_junction(api)

    assert error is None and tab is not None, "битый файл вкладку не роняет"
    assert TITLE_POINTS in _titles(dialogs), (
        f"нечитаемый points.json прошёл молча: {_titles(dialogs)}")

    dialogs.clear()
    dialogs.answer = QMessageBox.StandardButton.Cancel
    assert tab._save_masks() is False
    assert "junction_points_validated" not in api.saves, (
        "машинные центры ушли поверх нечитаемого файла оператора")


def test_server_failure_on_saved_pipe_mask_is_visible_and_locks_the_write(
        rasters, open_pipe, dialogs, tmp_path):
    """Вкладка труб: 5xx у сохранённой маски → фолбэк на сырой скелет.

    Цена измерима: у оператора в редакторе оказывается скелет сборщика, а
    `_save_mask` пишет его обратно в `pipe_mask_validated`.
    """
    saved_white = _white_pixels(rasters["pipe_validated"], tmp_path)
    raw_white = _white_pixels(rasters["skeleton"], tmp_path)
    assert saved_white != raw_white, "стенд не различает сохранённое и сырое"

    api = _pipe_server(rasters,
                       pipe_mask_validated=APIError("bad gateway", 502))

    tab, error = open_pipe(api)

    assert error is None and tab is not None, "фолбэк обязан состояться"
    assert TITLE_PIPE in _titles(dialogs), (
        f"подмена сохранённой маски труб скелетом прошла молча: {_titles(dialogs)}")

    dialogs.clear()
    dialogs.answer = QMessageBox.StandardButton.Cancel
    assert tab._save_mask() is False
    assert "pipe_mask_validated" not in api.saves
    assert _white_pixels(api.blobs["pipe_mask_validated"], tmp_path) == saved_white, (
        "сохранённая маска труб затёрта сырым скелетом из-за отказа сервера")


def test_absent_saved_pipe_mask_still_opens_the_tab_silently(
        rasters, open_pipe, dialogs):
    """Порог с другой стороны: 404 сохранённой маски — законный первый заход."""
    api = _pipe_server(rasters)
    del api.blobs["pipe_mask_validated"]

    tab, error = open_pipe(api)

    assert error is None and tab is not None
    assert dialogs == [], "404 — первый заход на этап, пугать оператора нечем"


def test_server_failure_on_pipe_coco_does_not_lock_the_write(
        rasters, open_pipe, dialogs):
    """Граница «глотаем осознанно»: COCO вкладки труб запись НЕ затирает.

    `upload_updated_nodes` вызывается только при `has_coco_changes`, а
    `PolylineMaskEditor.save_coco` без прочитанного `coco_full_data` отдаёт
    `False` (`:927`) — то есть запись ГЕЙТИРОВАНА успешным чтением, и слепой
    перезаписи здесь быть не может. Поэтому у задания стоит явное
    «глотаем осознанно», а не `failure_key`; тест запирает это замером.
    """
    api = _pipe_server(rasters, coco_validated=APIError("bad gateway", 502))

    tab, error = open_pipe(api)

    assert error is None and tab is not None
    assert dialogs == [], f"лишнее предупреждение по COCO: {_titles(dialogs)}"

    tab._editor.coco_annotations.append({"id": 1, "bbox": [1, 1, 2, 2]})
    tab._editor._coco_dirty = True                # оператор добавил узел
    assert tab._save_mask() is True
    assert "coco_validated" not in api.saves, (
        "непрочитанный COCO ушёл на сервер — запись НЕ гейтирована чтением")

"""Характеризационный набор на фоновую загрузку артефактов вкладок (пункт 0.5).

Зона без покрытия: четыре загрузчика вкладок (graph / junction / pipe / ocr)
никем не проверялись. Набор фиксирует ТЕКУЩЕЕ поведение до сведения копий
в один класс и остаётся гейтом после:

  * карта «ключ артефакта → имя файла в temp_dir» — её читают `_on_downloaded`
    вкладок по конкретным ключам;
  * цепочки фолбэков (`*_validated` → обычный) и то, где ключ зависит от
    сработавшего кандидата (pipe), а где нет (junction);
  * политика ошибок: что обязательно, что глотается, что уводит вкладку в error;
  * бит-в-бит: sha256 приземлившегося файла равен sha256 источника.

⛔ Строгость графовой вкладки — несущее поведение, а не придирка: тихо потерянный
`graph_canvas` означает «холст пересобран заново, прежние правки не сохранятся»
(base_graph_tab.py `_on_downloaded`), тихо потерянный `contours_validated` —
выброшенные правки оператора (см. `_canvas_contours_stale`).
"""
import hashlib
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path                                   # noqa: E402

import pytest                                              # noqa: E402

pytest.importorskip("PySide6")

from ui.services.api_client import APIError                # noqa: E402


# ---------------------------------------------------------------------------
# Поддельный APIClient
# ---------------------------------------------------------------------------

#: Что «лежит на сервере» по умолчанию. Значение = байты файла.
_ALL_BLOBS = {
    "original_image": b"PNG-original",
    "graph_validated": b'{"nodes": [1]}',
    "graph_json": b'{"nodes": [0]}',
    "graph_canvas": b'{"canvas": true}',
    "contours_validated": b'{"contours": []}',
    "coco_validated": b'{"annotations": ["v"]}',
    "coco_predicted": b'{"annotations": ["p"]}',
    "junction_mask_validated": b"PNG-junction-validated",
    "junction_mask": b"PNG-junction",
    "bridge_mask_validated": b"PNG-bridge-validated",
    "bridge_mask": b"PNG-bridge",
    "skeleton_final": b"PNG-skeleton-final",
    "skeleton": b"PNG-skeleton",
    "junction_points_validated": b'{"points": ["v"]}',
    "junction_points": b'{"points": []}',
    "pipe_mask_validated": b"PNG-pipe-validated",
    "skeleton_mask": b"PNG-skeleton-mask",
    "pipe_mask": b"PNG-pipe-mask",
    "download_ocr_result": b'{"target": []}',
    "download_ocr_binding": b'{"bindings": []}',
    "download_ocr_validation": b'{"validated": []}',
}


class FakeAPI:
    """Отдаёт заданные байты; отсутствующее — APIError 404, заданное — своей ошибкой."""

    def __init__(self, missing=(), failures=None):
        self.blobs = {k: v for k, v in _ALL_BLOBS.items() if k not in missing}
        self.failures = dict(failures or {})
        self.calls = []

    def _serve(self, name: str, dest):
        self.calls.append(name)
        exc = self.failures.get(name)
        if exc is not None:
            raise exc
        data = self.blobs.get(name)
        if data is None:
            raise APIError(f"artifact {name} not found", 404)
        Path(dest).write_bytes(data)
        return Path(dest)

    def download_artifact(self, uid, artifact_type, dest_path):
        return self._serve(artifact_type, dest_path)

    def download_ocr_result(self, uid, dest):
        return self._serve("download_ocr_result", dest)

    def download_ocr_binding(self, uid, dest):
        return self._serve("download_ocr_binding", dest)

    def download_ocr_validation(self, uid, dest):
        return self._serve("download_ocr_validation", dest)


# ---------------------------------------------------------------------------
# Адаптер: ЕДИНСТВЕННОЕ место, которое двигает правка 0.5
# ---------------------------------------------------------------------------

def _downloader(kind: str, api, uid: str, tmp: Path):
    """Собрать загрузчик вкладки `kind`."""
    from ui.services.artifact_downloader import ArtifactDownloader

    if kind in ("graph", "graph_canvas"):
        from ui.tabs.base_graph_tab import _graph_jobs
        jobs = _graph_jobs(want_canvas=(kind == "graph_canvas"))
    elif kind == "junction":
        from ui.tabs.junction_tab import _ARTIFACTS as jobs
    elif kind == "pipe":
        from ui.tabs.pipe_tab import _ARTIFACTS as jobs
    elif kind == "ocr":
        from ui.tabs.ocr_binding_tab import _ARTIFACTS as jobs
    else:
        raise AssertionError(f"неизвестная вкладка: {kind}")
    return ArtifactDownloader(api, uid, tmp, jobs)


ALL_KINDS = ("graph", "graph_canvas", "junction", "pipe", "ocr")


def run_download(kind, tmp_path, api=None):
    """Прогнать загрузчик синхронно; вернуть (artifacts, error, api)."""
    api = api or FakeAPI()
    dl = _downloader(kind, api, "uid-0", tmp_path)
    out = {}
    dl.finished.connect(lambda a: out.__setitem__("artifacts", a))
    dl.error.connect(lambda m: out.__setitem__("error", m))
    dl.run()
    return out.get("artifacts"), out.get("error"), api


def sha(data) -> str:
    if isinstance(data, (str, Path)):
        data = Path(data).read_bytes()
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 1. Карта «ключ → имя файла»: её читают _on_downloaded вкладок
# ---------------------------------------------------------------------------

EXPECTED_MAP = {
    "graph": {
        "original_image": "original.png",
        "graph_json": "graph.json",
        "coco_validated": "coco_validated.json",
    },
    "graph_canvas": {
        "original_image": "original.png",
        "graph_json": "graph.json",
        "coco_validated": "coco_validated.json",
        "graph_canvas": "graph_canvas.json",
        "contours_validated": "contours_validated.json",
    },
    "junction": {
        "original_image": "original.png",
        "junction_mask": "junction_mask.png",
        "bridge_mask": "bridge_mask.png",
        "skeleton": "skeleton.png",
        "coco_validated": "coco_validated.json",
        "points": "points.json",
    },
    "pipe": {
        "original_image": "original.png",
        "pipe_mask_validated": "mask.png",
        "coco_validated": "coco_validated.json",
        "pipe_mask": "pipe_mask.png",
    },
    "ocr": {
        "original_image": "original.png",
        "ocr_result": "ocr_result.json",
        "graph": "graph_validated.json",
        "coco": "coco_validated.json",
        "binding": "ocr_binding.json",
        "ocr_validation": "ocr_validation.json",
    },
}


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_artifact_key_to_filename_map(kind, tmp_path):
    artifacts, error, _ = run_download(kind, tmp_path)
    assert error is None
    assert {k: Path(v).name for k, v in artifacts.items()} == EXPECTED_MAP[kind]


# ---------------------------------------------------------------------------
# 2. Бит-в-бит: файл на диске = то, что отдал сервер
# ---------------------------------------------------------------------------

#: ключ артефакта → тип, который его наполнил при полном сервере
_SOURCE_OF = {
    "graph_json": "graph_validated",
    "graph": "graph_validated",
    "junction_mask": "junction_mask_validated",
    "bridge_mask": "bridge_mask_validated",
    "skeleton": "skeleton_final",
    "points": "junction_points_validated",
    "coco": "coco_validated",
    "ocr_result": "download_ocr_result",
    "binding": "download_ocr_binding",
    "ocr_validation": "download_ocr_validation",
}


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_downloaded_files_are_bit_exact(kind, tmp_path):
    artifacts, error, _ = run_download(kind, tmp_path)
    assert error is None
    for key, path in artifacts.items():
        src = _ALL_BLOBS[_SOURCE_OF.get(key, key)]
        assert sha(path) == sha(src), f"{kind}/{key} приземлился не бит-в-бит"


# ---------------------------------------------------------------------------
# 3. Цепочки фолбэков
# ---------------------------------------------------------------------------

def test_graph_falls_back_to_graph_json(tmp_path):
    artifacts, error, _ = run_download(
        "graph", tmp_path, FakeAPI(missing=("graph_validated",)))
    assert error is None
    assert sha(artifacts["graph_json"]) == sha(_ALL_BLOBS["graph_json"])


@pytest.mark.parametrize("key,drop,fallback", [
    ("junction_mask", ("junction_mask_validated",), "junction_mask"),
    ("bridge_mask", ("bridge_mask_validated",), "bridge_mask"),
    ("skeleton", ("skeleton_final",), "skeleton"),
    ("points", ("junction_points_validated",), "junction_points"),
])
def test_junction_fallbacks_keep_one_key(key, drop, fallback, tmp_path):
    """У junction ключ НЕ зависит от сработавшего кандидата."""
    artifacts, error, _ = run_download("junction", tmp_path, FakeAPI(missing=drop))
    assert error is None
    assert sha(artifacts[key]) == sha(_ALL_BLOBS[fallback])


def test_pipe_mask_key_depends_on_the_candidate(tmp_path):
    """У pipe ключ = сработавший кандидат: вкладка читает их по очереди."""
    artifacts, error, _ = run_download(
        "pipe", tmp_path, FakeAPI(missing=("pipe_mask_validated",)))
    assert error is None
    assert "pipe_mask_validated" not in artifacts
    assert Path(artifacts["skeleton_mask"]).name == "mask.png"
    assert sha(artifacts["skeleton_mask"]) == sha(_ALL_BLOBS["skeleton_mask"])


def test_ocr_coco_falls_back_to_predicted_with_own_filename(tmp_path):
    artifacts, error, _ = run_download(
        "ocr", tmp_path, FakeAPI(missing=("coco_validated",)))
    assert error is None
    assert Path(artifacts["coco"]).name == "coco_predicted.json"
    assert sha(artifacts["coco"]) == sha(_ALL_BLOBS["coco_predicted"])


def test_ocr_graph_falls_back_to_graph_json(tmp_path):
    artifacts, error, _ = run_download(
        "ocr", tmp_path, FakeAPI(missing=("graph_validated",)))
    assert error is None
    assert sha(artifacts["graph"]) == sha(_ALL_BLOBS["graph_json"])


# ---------------------------------------------------------------------------
# 4. Обязательные артефакты: их отсутствие уводит вкладку в error
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,missing", [
    ("graph", ("original_image",)),
    ("graph", ("graph_validated", "graph_json")),
    ("junction", ("original_image",)),
    ("junction", ("junction_mask_validated", "junction_mask")),
    ("pipe", ("original_image",)),
    ("pipe", ("pipe_mask_validated", "skeleton_mask")),
    ("ocr", ("original_image",)),
    ("ocr", ("download_ocr_result",)),
    ("ocr", ("graph_validated", "graph_json")),
])
def test_required_artifact_missing_gives_error(kind, missing, tmp_path):
    artifacts, error, _ = run_download(kind, tmp_path, FakeAPI(missing=missing))
    assert artifacts is None
    assert error


# ---------------------------------------------------------------------------
# 5. Необязательные артефакты: 404 не мешает загрузке
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,missing,gone", [
    ("graph", ("coco_validated",), "coco_validated"),
    ("graph_canvas", ("graph_canvas",), "graph_canvas"),
    ("graph_canvas", ("contours_validated",), "contours_validated"),
    ("junction", ("bridge_mask_validated", "bridge_mask"), "bridge_mask"),
    ("junction", ("skeleton_final", "skeleton"), "skeleton"),
    ("junction", ("coco_validated",), "coco_validated"),
    ("junction", ("junction_points_validated", "junction_points"), "points"),
    ("pipe", ("coco_validated",), "coco_validated"),
    ("pipe", ("pipe_mask",), "pipe_mask"),
    ("ocr", ("coco_validated", "coco_predicted"), "coco"),
    ("ocr", ("download_ocr_binding",), "binding"),
    ("ocr", ("download_ocr_validation",), "ocr_validation"),
])
def test_optional_artifact_404_is_not_an_error(kind, missing, gone, tmp_path):
    artifacts, error, _ = run_download(kind, tmp_path, FakeAPI(missing=missing))
    assert error is None
    assert gone not in artifacts


def test_optional_404_does_not_raise_contours_flag(tmp_path):
    """404 контуров = «контуры легитимно не снимали», а не «неизвестно»."""
    artifacts, error, _ = run_download(
        "graph_canvas", tmp_path, FakeAPI(missing=("contours_validated",)))
    assert error is None
    assert "contours_download_failed" not in artifacts


# ---------------------------------------------------------------------------
# 6. ⛔ Политика ошибок графовой вкладки — несущее поведение
# ---------------------------------------------------------------------------

def test_contours_non_404_sets_unknown_flag(tmp_path):
    """Сеть/5xx на контурах = «состояние неизвестно», холст инвалидировать нельзя."""
    api = FakeAPI(failures={"contours_validated": APIError("bad gateway", 502)})
    artifacts, error, _ = run_download("graph_canvas", tmp_path, api)
    assert error is None
    assert artifacts["contours_download_failed"] is True
    assert "contours_validated" not in artifacts


def test_contours_non_api_error_stops_the_tab(tmp_path):
    """Не-APIError на контурах НЕ глотается: молча продолжить = выбросить правки."""
    api = FakeAPI(failures={"contours_validated": OSError("диск полон")})
    artifacts, error, _ = run_download("graph_canvas", tmp_path, api)
    assert artifacts is None
    assert error


def test_graph_canvas_non_api_error_stops_the_tab(tmp_path):
    """Тихая потеря graph_canvas = «холст пересобран, правки не сохранятся»."""
    api = FakeAPI(failures={"graph_canvas": OSError("диск полон")})
    artifacts, error, _ = run_download("graph_canvas", tmp_path, api)
    assert artifacts is None
    assert error


def test_graph_optional_non_api_error_stops_the_tab(tmp_path):
    api = FakeAPI(failures={"coco_validated": OSError("диск полон")})
    artifacts, error, _ = run_download("graph", tmp_path, api)
    assert artifacts is None
    assert error


@pytest.mark.parametrize("kind,failing,gone", [
    ("junction", "coco_validated", "coco_validated"),
    ("pipe", "pipe_mask", "pipe_mask"),
    ("ocr", "download_ocr_binding", "binding"),
])
def test_masks_and_ocr_swallow_any_optional_failure(kind, failing, gone, tmp_path):
    """Три остальные вкладки глотают ЛЮБУЮ ошибку необязательного артефакта."""
    api = FakeAPI(failures={failing: OSError("диск полон")})
    artifacts, error, _ = run_download(kind, tmp_path, api)
    assert error is None
    assert gone not in artifacts


# ---------------------------------------------------------------------------
# 7. Состав запросов к серверу
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,expected", [
    ("graph", {"original_image", "graph_validated", "coco_validated"}),
    ("graph_canvas", {"original_image", "graph_validated", "coco_validated",
                      "graph_canvas", "contours_validated"}),
    ("junction", {"original_image", "junction_mask_validated",
                  "bridge_mask_validated", "skeleton_final", "coco_validated",
                  "junction_points_validated"}),
    ("pipe", {"original_image", "pipe_mask_validated", "coco_validated",
              "pipe_mask"}),
    ("ocr", {"original_image", "download_ocr_result", "graph_validated",
             "coco_validated", "download_ocr_binding", "download_ocr_validation"}),
])
def test_server_is_asked_only_for_what_the_tab_needs(kind, expected, tmp_path):
    """При полном сервере фолбэки не дёргаются — запрошен ровно первый кандидат."""
    _, error, api = run_download(kind, tmp_path)
    assert error is None
    assert set(api.calls) == expected


def test_graph_does_not_ask_for_canvas_artifacts_without_wysiwyg(tmp_path):
    """Холст и контуры тянет только WYSIWYG-вкладка (USE_CANVAS)."""
    _, _, api = run_download("graph", tmp_path)
    assert "graph_canvas" not in api.calls
    assert "contours_validated" not in api.calls

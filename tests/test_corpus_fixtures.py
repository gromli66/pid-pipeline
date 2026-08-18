# -*- coding: utf-8 -*-
"""Корпус-фикстура в git и загрузчик КД7 (пункт 0.8 дороги рефакторинга).

Дефект, который ловится здесь: корпус существовал только в `storage/` машины
разработки, поэтому в CI корпусные проверки не падали, а ИСЧЕЗАЛИ из сбора —
набор оставался зелёным с дырой. Проверяем ровно это: без локального storage
корпус всё равно есть, и он тот же самый.
"""
import json

import pytest

from tools import corpus

MIN_FIXTURES = 3          # d74eb9f1, 089feca2, 6e7144d5 (README рядом с ними)


def test_fixture_corpus_is_in_git():
    paths = corpus.fixture_paths()
    assert len(paths) >= MIN_FIXTURES, f"фикстур меньше {MIN_FIXTURES}: {sorted(paths)}"
    for uid8, path in paths.items():
        assert len(uid8) == 8, f"имя фикстуры — uid8, а не {uid8}"
        graph = json.loads(path.read_text(encoding="utf-8"))
        assert graph["nodes"] and graph["links"], f"{uid8}: пустой граф"


def test_corpus_survives_without_local_storage(tmp_path, monkeypatch):
    """Главная проверка пункта: на чистом клоне (CI) корпус не пустеет."""
    monkeypatch.setattr(corpus, "STORAGE_DIR", tmp_path / "нет-такого")
    paths = corpus.corpus_paths()
    assert len(paths) >= MIN_FIXTURES
    assert paths == corpus.fixture_paths()


def test_fixture_wins_over_storage():
    """Одноимённый граф из storage не подменяет зафиксированный в git."""
    for uid8, path in corpus.fixture_paths().items():
        assert corpus.graph_path(uid8) == path


def test_load_graph_reports_unknown_uid():
    with pytest.raises(KeyError):
        corpus.load_graph("00000000")


@pytest.mark.skipif(not corpus.storage_paths(), reason="локального storage нет")
def test_fixtures_match_their_storage_origin():
    """Фикстура — байт-в-байт копия источника, а не пересобранный файл."""
    storage = corpus.storage_paths()
    for uid8, path in corpus.fixture_paths().items():
        origin = storage.get(uid8)
        if origin is None:
            continue
        assert path.read_bytes() == origin.read_bytes(), \
            f"{uid8}: фикстура разошлась с {origin}"

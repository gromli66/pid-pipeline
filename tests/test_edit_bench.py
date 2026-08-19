# -*- coding: utf-8 -*-
"""Судья вкладки «Ручная правка» как ГЕЙТ (пункт ГЕЙТ-1).

Дефект уровня стенда: корпус лежит вне git (`.gitignore:37`), поэтому на
чистом дереве мерить нечего — а `--check` печатал «рост дефектов: 0» и
отдавал exit 0. Зелёный, снятый там, где судить нечем, хуже красного
(`PROTOCOL §5`: доказано · опровергнуто · судить нечем).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import edit_bench as eb  # noqa: E402

BASE = {"a.json": {"diag": 3, "through": 1, "max_dev": 10},
        "b.json": {"diag": 0, "through": 0, "max_dev": 0}}


def _rows(counts_by_name: dict) -> dict:
    return {name: {"counts": c} for name, c in counts_by_name.items()}


def test_no_growth_is_green():
    code = eb._compare(_rows({"a.json": {"diag": 3, "through": 1, "max_dev": 99},
                              "b.json": {"diag": 0, "through": 0}}), BASE)
    assert code == 0


def test_growth_of_defect_column_is_red():
    code = eb._compare(_rows({"a.json": {"diag": 4, "through": 1},
                              "b.json": {"diag": 0, "through": 0}}), BASE)
    assert code == 1


def test_file_absent_from_baseline_is_skipped():
    """Новый холст в корпусе судить не с чем — это пропуск, а не провал."""
    code = eb._compare(_rows({"a.json": {"diag": 3, "through": 1},
                              "b.json": {"diag": 0, "through": 0},
                              "new.json": {"diag": 9, "through": 9}}), BASE)
    assert code == 0


def test_unmeasured_baseline_file_is_not_green(capsys):
    """Дыра ГЕЙТ-1: замерена часть корпуса — это НЕ «дефекты не выросли»."""
    code = eb._compare(_rows({"a.json": {"diag": 3, "through": 1}}), BASE)
    assert code == 2
    out = capsys.readouterr().out
    assert "судить нечем" in out
    assert "b.json" in out


def test_empty_baseline_is_not_green(capsys):
    code = eb._compare(_rows({"a.json": {"diag": 3, "through": 1}}), {})
    assert code == 2
    assert "судить нечем" in capsys.readouterr().out


def test_growth_beats_unmeasured():
    """Доказанный рост дефектов важнее неполноты: 1 (опровергнуто), не 2."""
    code = eb._compare(_rows({"a.json": {"diag": 4, "through": 1}}), BASE)
    assert code == 1


def test_empty_corpus_says_nothing_to_judge(monkeypatch, capsys):
    """Зонд «убрать корпус»: `--all --check` на пустом корпусе — exit 2."""
    monkeypatch.setattr(eb, "CORPUS", REPO / "tools" / "bench" / "no_such_corpus")
    monkeypatch.setattr(sys, "argv", ["edit_bench.py", "--all", "--check"])
    assert eb.main() == 2
    assert "судить нечем" in capsys.readouterr().out


# ───── путь ЗАПИСИ: пересъём тоже обязан судить (пункт 1-30) ─────

def _write_stand(tmp_path, monkeypatch, measured, base=BASE, argv=("--all", "--write-baseline")):
    """Стенд пересъёма: свой эталон, свой корпус, замер подменён.

    `measured` — {имя файла: counts}: и корпус, и то, что по нему намерилось,
    задаются одним словарём. Файлы настоящие (глоб `--all` ходит по диску),
    а разбор холста подменён — предикаты `edit_checks` тут ни при чём.
    """
    corpus = tmp_path / "edit_corpus"
    corpus.mkdir()
    for name in measured:
        (corpus / name).write_text("{}", encoding="utf-8")
    path = tmp_path / "edit_baseline.json"
    if base is not None:
        path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(eb, "BASELINE", path)
    monkeypatch.setattr(eb, "CORPUS", corpus)
    monkeypatch.setattr(eb, "measure_file",
                        lambda p: {"counts": measured[p.name], "findings": {}})
    monkeypatch.setattr(sys, "argv", ["edit_bench.py", *argv])
    return path


WRITE_BASE = {"graph_edited_a.json": {"diag": 3, "through": 1, "max_dev": 10},
              "graph_edited_b.json": {"diag": 0, "through": 0, "max_dev": 0}}


def test_write_refuses_to_legalize_grown_defects(tmp_path, monkeypatch, capsys):
    """⛔ Дыра 1-30 (а): `--write-baseline` не читал эталон ВООБЩЕ.

    Четвёртый стенд с той же дырой, что 1-25/1-26/1-28 закрыли у трёх соседей
    (`tools/edit_bench.py:185`): пересъём записывал то, что намерилось, и
    печатал «база заморожена». Пересъём идёт отдельным коммитом, как требует
    Д6, поэтому красный флаг №4 протокола этого не видит — рост дефектов
    «Ручной правки» становился нормой молча, и следующий `--check` был зелёным.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"graph_edited_a.json": {"diag": 4, "through": 1},
                         "graph_edited_b.json": {"diag": 0, "through": 0}},
                        base=WRITE_BASE)
    before = path.read_text(encoding="utf-8")

    assert eb.main() == 1
    assert "graph_edited_a.json: diag 3 -> 4" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before, "эталон переписан вопреки отказу"


def test_write_on_truncated_corpus_is_not_a_snapshot(tmp_path, monkeypatch, capsys):
    """Корпус вне git (`.gitignore:37`) — пересъём по части вычеркнул бы остальное.

    Код 2, а не 1: тут не доказан рост, а мерить было нечем. Ровно то,
    что 1-28 замерил у ПР1 («эталон переснят», exit 0, 3 графа вместо 17).
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"graph_edited_a.json": {"diag": 3, "through": 1}},
                        base=WRITE_BASE)
    before = path.read_text(encoding="utf-8")

    assert eb.main() == 2
    out = capsys.readouterr().out
    assert "СУДИТЬ НЕЧЕМ" in out and "graph_edited_b.json" in out
    assert path.read_text(encoding="utf-8") == before, "эталон переписан вопреки отказу"


def test_write_of_the_whole_corpus_records_it(tmp_path, monkeypatch):
    """Положительный контроль: измерен весь эталон, дефекты не выросли.

    Без него правка односторонняя — стенд, отказывающий всегда, гейтом не
    является. Оплата долга («лучше») и есть то, ради чего пересъём и нужен.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"graph_edited_a.json": {"diag": 1, "through": 1},
                         "graph_edited_b.json": {"diag": 0, "through": 0}},
                        base=WRITE_BASE)

    assert eb.main() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["graph_edited_a.json"]["diag"] == 1


def test_write_of_the_first_snapshot_has_nothing_to_compare(tmp_path, monkeypatch):
    """Эталона нет — сверять не с чем; первый снимок законен."""
    path = _write_stand(tmp_path, monkeypatch,
                        {"graph_edited_a.json": {"diag": 9, "through": 9}}, base=None)

    assert eb.main() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["graph_edited_a.json"]["diag"] == 9


def test_growth_outranks_a_truncated_corpus_on_write(tmp_path, monkeypatch, capsys):
    """Приоритет как у трёх соседей: доказанный рост сильнее неполноты — 1, не 2."""
    path = _write_stand(tmp_path, monkeypatch,
                        {"graph_edited_a.json": {"diag": 4, "through": 1}},
                        base=WRITE_BASE)
    before = path.read_text(encoding="utf-8")

    assert eb.main() == 1
    out = capsys.readouterr().out
    assert "[ОТКАЗ]" in out and "СУДИТЬ НЕЧЕМ" in out
    assert path.read_text(encoding="utf-8") == before


def test_reference_column_does_not_block_the_snapshot(tmp_path, monkeypatch):
    """`max_dev` — справочная колонка, её рост дефектом не считается.

    Иначе стенд отказывал бы на шуме: дефектные колонки перечислены в `COLS`
    третьим полем, и запись судит ровно их — те же, что `--check`.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"graph_edited_a.json": {"diag": 3, "through": 1, "max_dev": 999},
                         "graph_edited_b.json": {"diag": 0, "through": 0}},
                        base=WRITE_BASE)

    assert eb.main() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["graph_edited_a.json"]["max_dev"] == 999

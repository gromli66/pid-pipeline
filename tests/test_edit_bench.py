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

BASE = {"version": 2,
        "files": {"a.json": {"diag": 3, "through": 1, "max_dev": 10},
                  "b.json": {"diag": 0, "through": 0, "max_dev": 0}},
        "inputs": {"a.json": "in-a", "b.json": "in-b"}}


def _rows(counts_by_name: dict) -> dict:
    return {name: {"counts": c} for name, c in counts_by_name.items()}


def _cmp(rows: dict, base=BASE, inputs=None) -> int:
    """Сверка с эталоном. `inputs` — отпечатки ВХОДНЫХ данных (пункт GATE-6).

    По умолчанию равны эталонным: «холсты те же, на которых эталон снят», —
    так тесты ниже судят ровно то, что судили до GATE-6. Подмена холста
    разбирается отдельными тестами, и отпечаток там задаётся явно.
    """
    if inputs is None:
        known = base.get("inputs", {})
        inputs = {n: known.get(n, f"свежий-{n}") for n in rows}
    return eb._compare(rows, base, inputs)


def test_no_growth_is_green():
    code = _cmp(_rows({"a.json": {"diag": 3, "through": 1, "max_dev": 99},
                       "b.json": {"diag": 0, "through": 0}}))
    assert code == 0


def test_growth_of_defect_column_is_red():
    code = _cmp(_rows({"a.json": {"diag": 4, "through": 1},
                       "b.json": {"diag": 0, "through": 0}}))
    assert code == 1


def test_file_absent_from_baseline_is_skipped():
    """Новый холст в корпусе судить не с чем — это пропуск, а не провал."""
    code = _cmp(_rows({"a.json": {"diag": 3, "through": 1},
                       "b.json": {"diag": 0, "through": 0},
                       "new.json": {"diag": 9, "through": 9}}))
    assert code == 0


def test_unmeasured_baseline_file_is_not_green(capsys):
    """Дыра ГЕЙТ-1: замерена часть корпуса — это НЕ «дефекты не выросли»."""
    code = _cmp(_rows({"a.json": {"diag": 3, "through": 1}}))
    assert code == 2
    out = capsys.readouterr().out
    assert "судить нечем" in out
    assert "b.json" in out


def test_empty_baseline_is_not_green(capsys):
    code = _cmp(_rows({"a.json": {"diag": 3, "through": 1}}), base={})
    assert code == 2
    assert "судить нечем" in capsys.readouterr().out


def test_growth_beats_unmeasured():
    """Доказанный рост дефектов важнее неполноты: 1 (опровергнуто), не 2."""
    code = _cmp(_rows({"a.json": {"diag": 4, "through": 1}}))
    assert code == 1


def test_empty_corpus_says_nothing_to_judge(monkeypatch, capsys):
    """Зонд «убрать корпус»: `--all --check` на пустом корпусе — exit 2."""
    monkeypatch.setattr(eb, "CORPUS", REPO / "tools" / "bench" / "no_such_corpus")
    monkeypatch.setattr(sys, "argv", ["edit_bench.py", "--all", "--check"])
    assert eb.main() == 2
    assert "судить нечем" in capsys.readouterr().out


# ───── путь ЗАПИСИ: пересъём тоже обязан судить (пункт 1-30) ─────

def _write_stand(tmp_path, monkeypatch, measured, base=BASE,
                 argv=("--all", "--write-baseline"), inputs=None):
    """Стенд пересъёма: свой эталон, свой корпус, замер подменён.

    `measured` — {имя файла: counts}: и корпус, и то, что по нему намерилось,
    задаются одним словарём. Файлы настоящие (глоб `--all` ходит по диску),
    а разбор холста подменён — предикаты `edit_checks` тут ни при чём.

    `inputs` — отпечатки входных данных (пункт GATE-6). По умолчанию равны
    эталонным: холсты те же, на которых эталон снят.
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
    # Холсты здесь пустые заглушки, отпечаток с них не снять: он задаётся
    # эталоном (вход не менялся) либо тестом явно (вход подменён).
    known = (base or {}).get("inputs", {})
    monkeypatch.setattr(eb, "input_fingerprints", lambda paths: dict(
        inputs if inputs is not None else
        {p.name: known.get(p.name, f"свежий-{p.name}") for p in paths}))
    monkeypatch.setattr(eb, "measure_file",
                        lambda p: {"counts": measured[p.name], "findings": {}})
    monkeypatch.setattr(sys, "argv", ["edit_bench.py", *argv])
    return path


WRITE_BASE = {"version": 2,
              "files": {"graph_edited_a.json": {"diag": 3, "through": 1,
                                                "max_dev": 10},
                        "graph_edited_b.json": {"diag": 0, "through": 0,
                                                "max_dev": 0}},
              "inputs": {"graph_edited_a.json": "in-a",
                         "graph_edited_b.json": "in-b"}}


def _written(path: Path) -> dict:
    """counts из переснятого эталона (форма v2, пункт GATE-6)."""
    return json.loads(path.read_text(encoding="utf-8"))["files"]


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
    assert _written(path)["graph_edited_a.json"]["diag"] == 1


def test_write_of_the_first_snapshot_has_nothing_to_compare(tmp_path, monkeypatch):
    """Эталона нет — сверять не с чем; первый снимок законен."""
    path = _write_stand(tmp_path, monkeypatch,
                        {"graph_edited_a.json": {"diag": 9, "through": 9}}, base=None)

    assert eb.main() == 0
    assert _written(path)["graph_edited_a.json"]["diag"] == 9


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
    assert _written(path)["graph_edited_a.json"]["max_dev"] == 999


# ───── отпечаток ВХОДНЫХ данных: подмена холста ≠ рост дефектов (GATE-6) ─────
#
# Дыра одна на два стенда: эталон ключевался ИМЕНЕМ файла и не помнил его
# содержимого. У ПР1 это стоило разбора по датам файлов (§70), здесь — то же
# самое: холст под тем же именем может быть другим, и «дефекты выросли»
# сказало бы о нём неправду.

def test_changed_canvas_is_unjudgeable_not_growth(capsys):
    """⛔ Сам дефект: то же имя, ДРУГОЙ холст — судить нечем, а не «выросли»."""
    code = _cmp(_rows({"a.json": {"diag": 9, "through": 1},
                       "b.json": {"diag": 0, "through": 0}}),
                inputs={"a.json": "in-a-НОВЫЙ", "b.json": "in-b"})

    assert code == 2
    out = capsys.readouterr().out
    assert "a.json" in out and "судить нечем" in out.lower()
    assert "ХУЖЕ" not in out, "подмена холста выдана за рост дефектов"


def test_changed_canvas_names_what_changed(capsys):
    """Названы оба отпечатка — иначе разбор снова пойдёт по датам файлов."""
    _cmp(_rows({"a.json": {"diag": 9, "through": 1},
                "b.json": {"diag": 0, "through": 0}}),
         inputs={"a.json": "in-a-НОВЫЙ", "b.json": "in-b"})

    out = capsys.readouterr().out
    assert "in-a" in out and "in-a-НОВЫЙ" in out


def test_same_canvas_still_proves_growth(capsys):
    """⭐ Обратная полярность: холст ТОТ ЖЕ — рост дефектов по-прежнему exit 1."""
    code = _cmp(_rows({"a.json": {"diag": 9, "through": 1},
                       "b.json": {"diag": 0, "through": 0}}),
                inputs={"a.json": "in-a", "b.json": "in-b"})

    assert code == 1
    assert "ХУЖЕ  a.json: diag 3 -> 9" in capsys.readouterr().out


def test_legacy_flat_baseline_cannot_judge(capsys):
    """Плоский эталон (до GATE-6) отпечатков не помнит — значит не судит.

    Формат versioned: старый вид `{файл: counts}` читается как «отпечатков
    нет», и это честное «судить нечем» до первого пересъёма.
    """
    flat = {"a.json": {"diag": 3, "through": 1},
            "b.json": {"diag": 0, "through": 0}}
    code = _cmp(_rows({"a.json": {"diag": 9, "through": 1},
                       "b.json": {"diag": 0, "through": 0}}),
                base=flat, inputs={"a.json": "in-a", "b.json": "in-b"})

    assert code == 2
    assert "не помнит отпечатка" in capsys.readouterr().out


def test_changed_canvas_does_not_hide_growth_elsewhere(capsys):
    """Доказанное сильнее неполноты: подменённый холст не глушит соседа."""
    code = _cmp(_rows({"a.json": {"diag": 9, "through": 1},
                       "b.json": {"diag": 5, "through": 0}}),
                inputs={"a.json": "in-a-НОВЫЙ", "b.json": "in-b"})

    assert code == 1
    out = capsys.readouterr().out
    assert "ХУЖЕ  b.json: diag 0 -> 5" in out
    assert "a.json" in out and "судить нечем" in out.lower()


def test_write_records_the_input_fingerprints(tmp_path, monkeypatch):
    """Положительный контроль: пересъём уносит с собой отпечаток каждого холста."""
    path = _write_stand(tmp_path, monkeypatch,
                        {"graph_edited_a.json": {"diag": 3, "through": 1},
                         "graph_edited_b.json": {"diag": 0, "through": 0}},
                        base=WRITE_BASE)

    assert eb.main() == 0
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["version"] == 2
    assert written["inputs"] == {"graph_edited_a.json": "in-a",
                                 "graph_edited_b.json": "in-b"}


def test_write_of_a_changed_canvas_is_not_refused(tmp_path, monkeypatch, capsys):
    """⭐ Отказ «дефекты выросли» — утверждение о ТОМ ЖЕ холсте.

    Холст другой — утверждения нет, есть новые данные: их и записывают.
    Молча нельзя, причина печатается.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"graph_edited_a.json": {"diag": 9, "through": 1},
                         "graph_edited_b.json": {"diag": 0, "through": 0}},
                        base=WRITE_BASE,
                        inputs={"graph_edited_a.json": "in-a-НОВЫЙ",
                                "graph_edited_b.json": "in-b"})

    assert eb.main() == 0
    assert _written(path)["graph_edited_a.json"]["diag"] == 9
    out = capsys.readouterr().out
    assert "graph_edited_a.json" in out and "in-a" in out


def test_write_still_refuses_growth_on_the_same_canvas(tmp_path, monkeypatch, capsys):
    """⭐ Обратная полярность записи: отказ 1-30 цел, пока холст ТОТ ЖЕ."""
    path = _write_stand(tmp_path, monkeypatch,
                        {"graph_edited_a.json": {"diag": 9, "through": 1},
                         "graph_edited_b.json": {"diag": 0, "through": 0}},
                        base=WRITE_BASE,
                        inputs={"graph_edited_a.json": "in-a",
                                "graph_edited_b.json": "in-b"})
    before = path.read_text(encoding="utf-8")

    assert eb.main() == 1
    assert "graph_edited_a.json: diag 3 -> 9" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before, "эталон переписан вопреки отказу"


def test_check_names_the_canvas_that_changed(tmp_path, monkeypatch, capsys):
    """Проводка целиком: `main()` обязан САМ снять отпечатки и отдать их судье.

    Отдельно от `_compare()`: верный судья при неподключённом отпечатке —
    это гейт, который молчит.
    """
    corpus_dir = tmp_path / "edit_corpus"
    corpus_dir.mkdir()
    (corpus_dir / "graph_edited_a.json").write_text(
        '{"nodes": ["ПОДМЕНА"], "links": []}', encoding="utf-8")
    path = tmp_path / "edit_baseline.json"
    path.write_text(json.dumps(
        {"version": 2,
         "files": {"graph_edited_a.json": {"diag": 3}},
         "inputs": {"graph_edited_a.json": "снят-с-другого-холста"}},
        ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(eb, "BASELINE", path)
    monkeypatch.setattr(eb, "CORPUS", corpus_dir)
    monkeypatch.setattr(eb, "measure_file",
                        lambda p: {"counts": {"diag": 9}, "findings": {}})
    monkeypatch.setattr(sys, "argv", ["edit_bench.py", "--all", "--check"])

    assert eb.main() == 2
    out = capsys.readouterr().out
    assert "graph_edited_a.json" in out and "судить нечем" in out.lower()
    assert "ХУЖЕ" not in out

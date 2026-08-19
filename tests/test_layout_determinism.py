# -*- coding: utf-8 -*-
"""Стенд воспроизводимости раскладки (пункт 0.10, ПР1; ГЕЙТ-1).

Дефект уровня стенда: гейт, зелёный на графе, который перестал быть
воспроизводимым, — именно такой молчаливый регресс и делает `cmp_bitexact`
ложно-красным. Плюс убитый дочерний прогон не имеет права выглядеть удачным.

ГЕЙТ-1 добавил третий исход: корпус усечён (в git 3 графа из 17) — вердикт
не «регресса нет», а «судить нечем».
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import layout_determinism as det  # noqa: E402

BASE = {"runs": 6, "routing": True,
        "stable": {"aaaaaaaa": True, "bbbbbbbb": False}}


def _lines(result, base=BASE):
    code, lines = det.verdict(result, base)
    return code, "\n".join(lines)


def test_stability_needs_all_runs_equal():
    assert det.stability(["x", "x", "x"])
    assert not det.stability(["x", "y", "x"])


def test_known_stable_graph_that_broke_fails():
    code, text = _lines({"aaaaaaaa": ["x", "y"], "bbbbbbbb": ["z", "z"]})
    assert code == 1
    assert "перестали воспроизводиться: aaaaaaaa" in text


def test_known_unstable_graph_stays_green():
    """Неповторимость роутинга записана в эталон — гейт на ней не валится.

    Обратная сторона ГЕЙТ-1: весь эталон измерен, значит вердикт выносится —
    «судить нечем» тут ложным быть не имеет права.
    """
    code, text = _lines({"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "w"]})
    assert code == 0
    assert "НЕПОВТОРИМ" in text
    assert "СУДИТЬ НЕЧЕМ" not in text
    assert "[OK] регресса воспроизводимости нет" in text


def test_unstable_graph_that_settled_is_reported_but_green():
    code, text = _lines({"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "z"]})
    assert code == 0
    assert "воспроизводятся впервые: bbbbbbbb" in text


def test_new_unstable_graph_fails():
    code, text = _lines({"aaaaaaaa": ["x", "x"], "cccccccc": ["p", "q"]})
    assert code == 1
    assert "новый граф корпуса неповторим: cccccccc" in text


# ───────────────── третий исход: судить нечем (ГЕЙТ-1) ─────────────────

def test_unmeasured_baseline_graph_is_not_green():
    """Дыра ГЕЙТ-1: uid эталона не измерен — это НЕ «регресса нет».

    Так выглядит чистый клон и CI: меряются 3 графа из 17, а среди
    неизмеренных 8 известных неповторимых.
    """
    code, text = _lines({"aaaaaaaa": ["x", "x"]})
    assert code == 2
    assert "СУДИТЬ НЕЧЕМ" in text
    assert "bbbbbbbb" in text
    assert "регресса воспроизводимости нет" not in text


@pytest.mark.parametrize("result", [
    {"aaaaaaaa": ["x", "x"]},                      # всё воспроизводимо
    {"aaaaaaaa": ["x", "y"]},                      # неповторим
])
def test_missing_baseline_is_not_green(result):
    """Эталона нет — сравнивать не с чем ни в ту, ни в другую сторону:
    неповторимый граф здесь не «опровергнуто», а всё то же «судить нечем»."""
    code, text = _lines(result, base={})
    assert code == 2
    assert "СУДИТЬ НЕЧЕМ" in text


def test_proven_regression_beats_unmeasured():
    """Доказанный регресс важнее неполноты: 1 (опровергнуто), не 2."""
    code, text = _lines({"aaaaaaaa": ["x", "y"]})
    assert code == 1
    assert "перестали воспроизводиться: aaaaaaaa" in text
    assert "СУДИТЬ НЕЧЕМ" in text


def test_baseline_in_git_covers_the_fixtures():
    """Три графа фикстуры (то, что видит CI) обязаны быть в эталоне."""
    from tools import corpus

    base = json.loads(det.BASELINE.read_text(encoding="utf-8"))
    for uid8 in corpus.fixture_paths():
        assert uid8 in base["stable"], f"граф {uid8} не замерен стендом"


@pytest.mark.parametrize("stdout,rc,match", [
    ("", 77, "вернул 77"),
    ("не json", 0, "не дал разбираемого вывода"),
    ('{"aaaaaaaa": "x"}', 0, "потерял графы"),
])
def test_killed_child_is_loud(monkeypatch, stdout, rc, match):
    class _Proc:
        returncode = rc
        stderr = "boom"

    _Proc.stdout = stdout
    monkeypatch.setattr(det.subprocess, "run", lambda *a, **kw: _Proc())
    with pytest.raises(RuntimeError, match=match):
        det.run_pass(["aaaaaaaa", "bbbbbbbb"], True, 1)


# ───────── путь ЗАПИСИ: пересъём тоже обязан судить (пункт 1-28) ─────────

def _write_stand(tmp_path, monkeypatch, measured, base=BASE, argv=("--runs", "6")):
    """Стенд пересъёма: свой эталон, свой корпус, замер подменён.

    `measured` — {uid8: [sha прогона 1, sha прогона 2, ...]}: и корпус, и то,
    что по нему намерилось, задаются одним словарём.

    `argv` по умолчанию несёт `--runs 6` — ровно столько, сколько записано
    в `BASE`: с пункта 1-29 пересъём слабее эталона отказывает, и без этого
    ключа любой из тестов ниже мерил бы отказ по числу прогонов вместо
    своей ветки (`DEFAULT_RUNS` = 2).
    """
    path = tmp_path / "determinism_baseline.json"
    if base is not None:
        path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(det, "BASELINE", path)
    monkeypatch.setattr(det.corpus, "corpus_paths",
                        lambda include_storage=True: dict.fromkeys(measured))
    monkeypatch.setattr(det, "measure", lambda uids, runs, routing: {
        uid: [{"sha": sha, "defects": [1, 0]} for sha in measured[uid]] for uid in uids})
    monkeypatch.setattr(sys, "argv",
                        ["layout_determinism.py", "--write-baseline", *argv])
    return path


def test_write_on_truncated_corpus_is_not_a_snapshot(tmp_path, monkeypatch, capsys):
    """⛔ Дыра 1-28 (а): пересъём на усечённом корпусе молча урезал эталон.

    Замер до правки: `--git-only --runs 2 --write-baseline` печатал «эталон
    переснят», exit 0 и оставлял в файле 3 графа вместо 17 — вместе с восемью
    известными неповторимыми. 1-25 научил отвечать «судить нечем» только
    `--check`, а меняет эталон именно запись.
    """
    path = _write_stand(tmp_path, monkeypatch, {"aaaaaaaa": ["x", "x"]})
    before = path.read_text(encoding="utf-8")

    assert det.main() == 2
    assert "СУДИТЬ НЕЧЕМ" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before, "эталон переписан вопреки отказу"


def test_write_refuses_to_record_a_broken_graph(tmp_path, monkeypatch, capsys):
    """Отказ (1), а не «судить нечем»: воспроизводимый граф сломался.

    Пересъём идёт ОТДЕЛЬНЫМ коммитом (так требует Д6), поэтому красный флаг №4
    протокола («эталон изменён тем же коммитом, что и код») его не видит.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"aaaaaaaa": ["x", "y"], "bbbbbbbb": ["z", "w"]})
    before = path.read_text(encoding="utf-8")

    assert det.main() == 1
    assert "перестали воспроизводиться: aaaaaaaa" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before, "эталон переписан вопреки отказу"


def test_write_on_the_whole_baseline_records_it(tmp_path, monkeypatch):
    """Положительный контроль: измерен весь эталон — пересъём проходит.

    Без него правка односторонняя: стенд, отказывающий всегда, гейтом не является.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "z"]})

    assert det.main() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["stable"] == {
        "aaaaaaaa": True, "bbbbbbbb": True}


def test_write_of_the_first_snapshot_has_nothing_to_compare(tmp_path, monkeypatch):
    """Эталона нет — сверять не с чем ни в одну сторону; первый снимок законен."""
    path = _write_stand(tmp_path, monkeypatch, {"aaaaaaaa": ["x", "y"]}, base=None)

    assert det.main() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["stable"] == {"aaaaaaaa": False}


# ───── пересъём не имеет права быть слабее эталона (пункт 1-29) ─────

def test_write_with_fewer_runs_than_the_baseline_is_not_a_snapshot(
        tmp_path, monkeypatch, capsys):
    """⛔ Дыра 1-29 (§49.27): при малом `--runs` неповторимый граф выходит `true`.

    Замерено 2026-08-19 на живом корпусе: пересъём при `--runs 2` записал
    `true` ЧЕТЫРЁМ из восьми известных неповторимых графов (`54a60fd1`,
    `620cc50d`, `6e7144d5`, `751116c9`), а действующий эталон снят при
    `runs 6`. Отказ 1-28 этого не ловит по замыслу: `false -> true` — законное
    «стало лучше», так же судит и `--check` с 1-25. Значит стеречь надо сам
    замер: слабее эталона — судить нечем, а не «эталон переснят».
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "z"]},
                        argv=("--runs", "5"))
    before = path.read_text(encoding="utf-8")

    assert det.main() == 2
    out = capsys.readouterr().out
    assert "СУДИТЬ НЕЧЕМ" in out
    assert "прогонов 5 против 6" in out
    assert path.read_text(encoding="utf-8") == before, "эталон переписан вопреки отказу"


@pytest.mark.parametrize("runs", ["6", "7"])
def test_write_at_or_above_the_baseline_runs_is_legal(tmp_path, monkeypatch, runs):
    """Порог заперт с двух сторон АБСОЛЮТНЫМИ числами (урок 0.4).

    Эталон снят при 6 прогонах: 5 — отказ (тест выше), 6 и 7 — законный
    пересъём. Ни одно из чисел не вычисляется из проверяемого поля, иначе тест
    остался бы зелёным при любом его значении. Больше прогонов — храповик:
    в эталон уезжает новое число, и следующий пересъём судится уже по нему.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "z"]},
                        argv=("--runs", runs))

    assert det.main() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["runs"] == int(runs)


def test_write_with_another_routing_key_is_not_a_snapshot(
        tmp_path, monkeypatch, capsys):
    """Тот же класс, что малый `--runs`: замер снят не тем ключом, что эталон.

    Без роутинга раскладка воспроизводима вся (замер 0.10: недетерминизм
    целиком в vendored libavoid, `--no-routing` даёт один исход из 30), поэтому
    `--no-routing --write-baseline` записал бы «стабильны все» и стёр бы список
    неповторимых разом — молча, одной командой, отдельным коммитом по Д6.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "z"]},
                        argv=("--runs", "6", "--no-routing"))
    before = path.read_text(encoding="utf-8")

    assert det.main() == 2
    out = capsys.readouterr().out
    assert "СУДИТЬ НЕЧЕМ" in out
    assert "роутинг" in out
    assert path.read_text(encoding="utf-8") == before, "эталон переписан вопреки отказу"


def test_broken_graph_outranks_a_weak_sample(tmp_path, monkeypatch, capsys):
    """Приоритет 1-28 сохранён: доказанная поломка сильнее неполноты.

    Две причины разом — воспроизводимый граф сломался И прогонов меньше
    эталонных — дают 1, а не 2: поломка наблюдается двумя РАЗНЫМИ исходами
    и от числа прогонов не зависит (малая выборка её прячет, а не выдумывает).
    """
    _write_stand(tmp_path, monkeypatch,
                 {"aaaaaaaa": ["x", "y"], "bbbbbbbb": ["z", "z"]},
                 argv=("--runs", "5"))

    assert det.main() == 1
    out = capsys.readouterr().out
    assert "перестали воспроизводиться: aaaaaaaa" in out
    assert "прогонов 5 против 6" in out

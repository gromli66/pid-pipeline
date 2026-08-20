# -*- coding: utf-8 -*-
"""Храповик линтеров (пункт 0.10): вердикт проверяется тестом, а не глазами.

Дефект уровня стенда, ради которого тесты и написаны, — гейт, зелёный там, где
долг вырос: новый широкий `except` в уже грязном файле, приписка к чистому,
и убитый прогон линтера с пустым выводом (тот же класс, что блокер пункта 0.3).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import lint_gate  # noqa: E402

BASE = {"ruff": {"select": ["BLE001", "E722"], "total": 5,
                 "per_file": {"app/api/validation.py": 3, "ui/tabs/frame_tab.py": 2}}}


def _lines(counts, mypy_bad=0, base=BASE):
    """-> (код вердикта, текст). Код, а не «да/нет»: исходов три (пункт GATE-8)."""
    code, lines = lint_gate.verdict(counts, mypy_bad, base)
    return code, "\n".join(lines)


def test_no_growth_is_green():
    code, text = _lines({"app/api/validation.py": 3, "ui/tabs/frame_tab.py": 2})
    assert code == 0
    assert "[OK]" in text


def test_growth_in_dirty_file_fails():
    """Главный сценарий: файл уже в долгу, туда дописали ещё один except."""
    code, text = _lines({"app/api/validation.py": 4, "ui/tabs/frame_tab.py": 2})
    assert code == 1
    assert "app/api/validation.py: широких except 3 -> 4" in text


def test_first_violation_in_clean_file_fails():
    code, text = _lines({"app/api/validation.py": 3, "ui/tabs/frame_tab.py": 2,
                         "worker/tasks/graph.py": 1})
    assert code == 1
    assert "worker/tasks/graph.py: широких except 0 -> 1" in text


def test_paid_debt_is_reported_but_green():
    code, text = _lines({"app/api/validation.py": 1, "ui/tabs/frame_tab.py": 2})
    assert code == 0
    assert "[долг оплачен] app/api/validation.py: 3 -> 1" in text


def test_empty_result_on_nonempty_baseline_is_unjudgeable():
    """⛔ Полярность GATE-8: убитый линтер — «судить нечем», а не рост долга.

    Ноль нарушений при эталоне в 174 места — это не оплата долга одним
    прогоном, а мёртвый ruff. До GATE-8 путь ЧТЕНИЯ печатал здесь `[ПРОВАЛ]`
    и отдавал 1 (замер 2026-08-20, §101в), тогда как путь ЗАПИСИ на том же
    условии говорил «судить нечем» и отдавал 2: два судьи одного стенда
    разошлись формой. Зелёным это не становится — 2 так же не ноль.
    """
    code, text = _lines({})
    assert code == 2
    assert "прогон ruff убит" in text
    assert "[ПРОВАЛ]" not in text
    assert "[долг оплачен]" not in text, "мёртвый прогон оплаты не наблюдал — это артефакт"


def test_mypy_errors_fail_the_gate():
    code, text = _lines({"app/api/validation.py": 3, "ui/tabs/frame_tab.py": 2},
                        mypy_bad=2)
    assert code == 1
    assert "mypy" in text


def test_baseline_in_git_matches_configured_rules():
    """Эталон снят тем же набором правил, что стоит в pyproject.toml."""
    base = json.loads(lint_gate.BASELINE.read_text(encoding="utf-8"))
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    for code in base["ruff"]["select"]:
        assert f'"{code}"' in text, f"правило {code} есть в эталоне, но не в конфиге"
    assert base["ruff"]["total"] == sum(base["ruff"]["per_file"].values())


@pytest.mark.parametrize("rc", [2, 77])
def test_abnormal_linter_exit_is_loud(monkeypatch, rc):
    """Код возврата вне (0, 1) — исключение, а не тихий ноль нарушений."""
    class _Proc:
        returncode = rc
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(lint_gate, "_run", lambda args: _Proc())
    with pytest.raises(RuntimeError, match=str(rc)):
        lint_gate.ruff_counts()


def test_baseline_holds_only_tracked_files():
    """Мусор рабочего дерева в эталон не попадает.

    Замер 2026-08-18: нетрекнутый `tools/tz_lint.py` давал 200/74 локально
    против 199/73 на раннере, а его строка в эталоне заранее прощала бы долг
    файлу, которого в git ещё нет.
    """
    base = json.loads(lint_gate.BASELINE.read_text(encoding="utf-8"))
    under_git = lint_gate.tracked()
    stray = sorted(set(base["ruff"]["per_file"]) - under_git)
    assert not stray, f"в эталоне файлы вне git: {stray}"


# ───────── путь ЗАПИСИ: пересъём не легализует долг (пункт 1-28) ─────────

def _write_stand(tmp_path, monkeypatch, counts, base=BASE, mypy_bad=0):
    """Стенд пересъёма: свой эталон, линтеры подменены числами."""
    path = tmp_path / "lint_baseline.json"
    if base is not None:
        path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(lint_gate, "BASELINE", path)
    monkeypatch.setattr(lint_gate, "ruff_counts", lambda: counts)
    monkeypatch.setattr(lint_gate, "mypy_errors", lambda: (mypy_bad, ""))
    monkeypatch.setattr(sys, "argv", ["lint_gate.py", "--write-baseline"])
    return path


def test_write_refuses_to_legalize_grown_debt(tmp_path, monkeypatch, capsys):
    """⛔ Дыра 1-28 (б): `--write-baseline` не сверялся с эталоном вовсе.

    Замер до правки на живом дереве: широкий `except` в чистом `tools/corpus.py`
    → `--check` exit 1 «0 -> 1»; один `--write-baseline` (эталон 178 → 179,
    exit 0) → `--check` снова зелёный. Пересъём идёт отдельным коммитом, как
    требует Д6, поэтому красный флаг №4 протокола этого не видит.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"app/api/validation.py": 3, "ui/tabs/frame_tab.py": 2,
                         "worker/tasks/graph.py": 1})
    before = path.read_text(encoding="utf-8")

    assert lint_gate.main() == 1
    assert "worker/tasks/graph.py: 0 -> 1" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before, "эталон переписан вопреки отказу"


def test_write_of_a_killed_linter_is_unjudgeable(tmp_path, monkeypatch, capsys):
    """Ноль нарушений при непустом эталоне — прогон убит: 2, а не 0 и не 1.

    Разница с отказом принципиальна: тут не «долг вырос», а мерить было нечем,
    и записывать пустой долг нельзя тем более.
    """
    path = _write_stand(tmp_path, monkeypatch, {})
    before = path.read_text(encoding="utf-8")

    assert lint_gate.main() == 2
    assert "СУДИТЬ НЕЧЕМ" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before, "эталон переписан вопреки отказу"


def test_write_records_paid_debt(tmp_path, monkeypatch):
    """Положительный контроль: долг оплачен — пересъём и есть способ это записать."""
    path = _write_stand(tmp_path, monkeypatch,
                        {"app/api/validation.py": 1, "ui/tabs/frame_tab.py": 2})

    assert lint_gate.main() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["ruff"]["total"] == 3


def test_write_does_not_judge_mypy(tmp_path, monkeypatch):
    """Грязный mypy пересъём не блокирует: в эталон пишется только счёт ruff.

    Долг mypy пересъёмом не легализуется в принципе — его судит `--check`
    на каждом прогоне, а в файле эталона его нет.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"app/api/validation.py": 3, "ui/tabs/frame_tab.py": 2},
                        mypy_bad=2)

    assert lint_gate.main() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["ruff"]["total"] == 5


def test_write_of_the_first_snapshot_has_nothing_to_compare(tmp_path, monkeypatch):
    """Эталона нет — сверять не с чем; первый снимок законен."""
    path = _write_stand(tmp_path, monkeypatch, {"worker/tasks/graph.py": 9}, base=None)

    assert lint_gate.main() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["ruff"]["per_file"] == {
        "worker/tasks/graph.py": 9}


# ─── убитый прогон линтера — «судить нечем», а не трейсбек (пункт 1-30) ───

class _Killed:
    """Ответ подменённого `_run`: код вне штатных (0, 1)."""
    returncode = 77
    stdout = ""
    stderr = "boom"


def _live_stand(tmp_path, monkeypatch, argv):
    """Свой эталон, но настоящие `ruff_counts`/`mypy_errors` — подменён `_run`."""
    path = tmp_path / "lint_baseline.json"
    path.write_text(json.dumps(BASE, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(lint_gate, "BASELINE", path)
    monkeypatch.setattr(sys, "argv", ["lint_gate.py", *argv])
    return path


@pytest.mark.parametrize("argv", [["--check"], ["--write-baseline"]])
def test_killed_linter_is_unjudgeable_not_a_traceback(tmp_path, monkeypatch,
                                                      capsys, argv):
    """⛔ Дыра 1-30 (г): убитый ruff/mypy умирал `RuntimeError` → exit 1.

    Полярность была безопасная (громко и красным), но перепутанная: тем же
    кодом 1 отвечает доказанный рост долга, а тут замер не состоялся —
    чинят обстановку, а не код. Трейсбек уходит, причина остаётся в тексте.
    """
    path = _live_stand(tmp_path, monkeypatch, argv)
    monkeypatch.setattr(lint_gate, "_run", lambda args: _Killed())
    before = path.read_text(encoding="utf-8")

    assert lint_gate.main() == 2
    out = capsys.readouterr().out
    assert "[СУДИТЬ НЕЧЕМ]" in out and "77" in out
    assert path.read_text(encoding="utf-8") == before, "эталон тронут на убитом прогоне"


def test_silent_git_is_unjudgeable(tmp_path, monkeypatch, capsys):
    """Та же семья: `git ls-files` не ответил — списка трекнутых файлов нет.

    Без него счётчик долга считался бы по мусору рабочего дерева (замер
    2026-08-18: 200/74 локально против 199/73 на раннере), то есть замер
    не годен в принципе.
    """
    _live_stand(tmp_path, monkeypatch, ["--check"])
    monkeypatch.setattr(lint_gate, "ruff_counts", lambda: (_ for _ in ()).throw(
        RuntimeError("git ls-files вернул 128: fatal: not a git repository")))

    assert lint_gate.main() == 2
    out = capsys.readouterr().out
    assert "[СУДИТЬ НЕЧЕМ]" in out and "not a git repository" in out


def test_killed_ruff_on_check_is_unjudgeable_end_to_end(tmp_path, monkeypatch, capsys):
    """Тот же убитый ruff, но через весь путь ЧТЕНИЯ, до кода возврата `main()`.

    Отдельно от юнита на `verdict()`: между ними лежит `main()`, которая до
    GATE-8 переводила «не ok» в `EXIT_REFUTED` и тем возвращала перепутанную
    полярность обратно. Гейт пункта требует буквально: ни одной строки
    `[ПРОВАЛ]` и код 2.
    """
    _live_stand(tmp_path, monkeypatch, ["--check"])
    monkeypatch.setattr(lint_gate, "ruff_counts", lambda: {})
    monkeypatch.setattr(lint_gate, "mypy_errors", lambda: (0, ""))

    assert lint_gate.main() == 2
    out = capsys.readouterr().out
    assert "[СУДИТЬ НЕЧЕМ]" in out and "прогон ruff убит" in out
    assert "[ПРОВАЛ]" not in out


def test_live_linters_still_judge(tmp_path, monkeypatch, capsys):
    """Обратная полярность: живой замер судит по-прежнему — 0 или 1, не 2.

    Иначе правка односторонняя: стенд, отвечающий «судить нечем» на всё
    подряд, гейтом не является.
    """
    _live_stand(tmp_path, monkeypatch, ["--check"])
    monkeypatch.setattr(lint_gate, "ruff_counts",
                        lambda: {"app/api/validation.py": 4, "ui/tabs/frame_tab.py": 2})
    monkeypatch.setattr(lint_gate, "mypy_errors", lambda: (0, ""))

    assert lint_gate.main() == 1
    assert "широких except 3 -> 4" in capsys.readouterr().out


def test_missing_git_binary_is_unjudgeable_too(monkeypatch):
    """Та же семья: без git в PATH `subprocess.run` кидает `FileNotFoundError`.

    Он идёт мимо проверки `returncode != 0`, поэтому до пункта 1-30 стенд
    умирал трейсбеком с кодом 1 — тем же, что доказанный рост долга.
    """
    def no_git(cmd, *a, **kw):
        raise FileNotFoundError(2, "The system cannot find the file specified", "git")

    monkeypatch.setattr(lint_gate.subprocess, "run", no_git)

    with pytest.raises(RuntimeError, match="git ls-files не запустился"):
        lint_gate.tracked()

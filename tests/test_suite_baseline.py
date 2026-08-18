# -*- coding: utf-8 -*-
"""Сторож базовой линии набора (`tools/suite_baseline.py`, пункт 0.3).

Стенд сам стал гейтом дороги, поэтому его разбор вывода pytest проверяется:
молча съеденная строка `FAILED ...` означает, что регрессия проедет в зелёном
CI. Тесты чистые — на синтетическом выводе, pytest внутрь себя не запускают.
"""
import json

from tools import suite_baseline as sb

REPORT = """\
....F..E...
=================================== FAILURES ===================================
=========================== short test summary info ============================
FAILED tests/test_alpha.py::test_one - AssertionError: assert 1 == 2
FAILED tests/test_beta.py::TestX::test_two - ValueError
ERROR tests/ui/test_gamma.py::test_three - ValueError: not enough values
2 failed, 661 passed, 13 skipped, 5 warnings, 1 error in 19.11s
"""


BASE = {
    "recorded": "2026-08-17",
    "platform": "win32",
    "collected": 738,
    "min_collected": 706,
    "totals": {"failed": 2, "passed": 661, "skipped": 13, "errors": 1},
    "red": [
        "tests/test_alpha.py::test_one",
        "tests/test_beta.py::TestX::test_two",
        "tests/ui/test_gamma.py::test_three",
    ],
}


def test_parses_red_ids():
    assert sb.parse_red(REPORT) == set(BASE["red"])


def test_parses_red_id_with_spaces_and_cyrillic():
    """Пробел в id не обрывает разбор: в storage/ уже лежит «Новая папка»,
    и параметр по корпусу приезжает в идентификатор как есть."""
    line = "FAILED tests/test_corpus.py::test_pair[Новая папка 2/graph.json] - AssertionError: 1 != 2"
    assert sb.parse_red(line) == {"tests/test_corpus.py::test_pair[Новая папка 2/graph.json]"}


def test_parses_red_id_without_reason():
    assert sb.parse_red("ERROR tests/test_x.py::test_y") == {"tests/test_x.py::test_y"}


def test_parses_totals_including_single_error():
    assert sb.parse_totals(REPORT) == {
        "failed": 2,
        "passed": 661,
        "skipped": 13,
        "errors": 1,
    }


def test_parses_collected_count():
    assert sb.parse_collected("tests/x.py::test_a\n\n732 tests collected in 0.91s\n") == 732
    assert sb.parse_collected("1 test collected in 0.1s") == 1
    assert sb.parse_collected("ничего похожего") == -1


def test_compare_separates_regression_from_fix():
    new, fixed = sb.compare({"a::t1", "b::t2"}, {"b::t2", "c::t3"})
    assert new == ["c::t3"]
    assert fixed == ["a::t1"]


def test_killed_run_is_a_failure_not_a_green_gate(monkeypatch, capsys):
    """Дефект ревизии 0.3: гейт был ЗЕЛЁНЫМ при убитом прогоне.

    `os._exit(77)` в первом тесте (класс §24.6 — access violation в этом же
    наборе уже случался) обрывает вывод: итоговой строки нет, красных не
    разобрано ни одного, и сравнение с базой читает это как «позеленело всё».
    Проверяем именно `cmd_check`, а не отдельный предикат: сломана была
    проводка кода возврата, а не арифметика.
    """
    killed = ("tests/test_alpha.py .\n", 77)

    def fake_pytest(args):
        return ("\n738 tests collected in 1.0s\n", 0) if "--collect-only" in args else killed

    monkeypatch.setattr(sb, "_pytest", fake_pytest)
    monkeypatch.setattr(sb, "_load_baseline", lambda: BASE)

    assert sb.cmd_check() == 1
    out = capsys.readouterr().out
    assert "[ПРОВАЛ]" in out and "77" in out


def test_run_without_summary_line_is_a_failure():
    """Оборванный хвост при штатном коде возврата — тоже не «всё зелено»."""
    assert sb.verdict(BASE, set(), {}, 738, 1)


def test_partial_output_caught_by_counter_invariant():
    """Счётчики говорят про 3 красных, а разобран один — вывод неполон."""
    problems = sb.verdict(BASE, {"tests/test_alpha.py::test_one"}, BASE["totals"], 738, 1)
    assert any("вывод неполон" in p for p in problems)


def test_healthy_run_has_no_problems():
    assert sb.verdict(BASE, set(BASE["red"]), BASE["totals"], 738, 1) == []
    assert sb.verdict(BASE, set(), {"failed": 0, "passed": 700, "errors": 0}, 738, 0) == []


def test_shrunken_suite_is_a_failure():
    assert any("усох" in p for p in sb.verdict(BASE, set(BASE["red"]), BASE["totals"], 705, 1))


def test_red_converted_to_skip_is_suspected():
    grew = dict(BASE["totals"], skipped=14)
    assert sb.skip_conversion_suspected(BASE, grew, ["tests/test_alpha.py::test_one"])
    # порознь оба признака законны
    assert not sb.skip_conversion_suspected(BASE, grew, [])
    assert not sb.skip_conversion_suspected(BASE, BASE["totals"], ["tests/test_alpha.py::test_one"])


def test_baseline_file_is_readable_and_consistent():
    base = json.loads(sb.BASELINE.read_text(encoding="utf-8"))
    assert base["min_collected"] < base["collected"], "нижняя граница не ниже снятого числа"
    assert len(base["red"]) == base["totals"]["failed"] + base["totals"]["errors"]
    assert len(set(base["red"])) == len(base["red"]), "дубли в списке красных"


# --- пол считается от git-видимой части сбора (пункт 0.3y) ---------------------

COLLECT = """\
tests/test_alpha.py::test_one
tests/test_canvas_pipeline_golden.py::test_t10_corpus_chain[d74eb9f1]
tests/test_canvas_pipeline_golden.py::test_t10_corpus_chain[0fc9d04c]
tests/test_canvas_pipeline_golden.py::test_t10_corpus_chain[3263039b]
tests/test_beta.py::test_matrix[3263039b-wide]

5 tests collected in 0.42s
"""

# То же дерево тестов на раннере: локального storage там нет вовсе.
RUNNER_COLLECT = """\
tests/test_alpha.py::test_one
tests/test_canvas_pipeline_golden.py::test_t10_corpus_chain[d74eb9f1]

2 tests collected in 0.11s
"""

# d74eb9f1 лежит в git (tests/fixtures/graph), два других — только в storage.
LOCAL = {"0fc9d04c", "3263039b"}


def test_counts_only_corpus_absent_from_git():
    """Фикстура из git в счёт не идёт — её собирает и раннер."""
    assert sb.count_local_corpus(COLLECT, LOCAL) == 3


def test_counts_nothing_when_storage_is_empty():
    """Чистое дерево: локального корпуса нет, вычитать нечего."""
    assert sb.count_local_corpus(COLLECT, set()) == 0


def test_local_corpus_uids_never_include_git_fixtures():
    """Фикстуры из git не должны попасть в вычитаемое: их видит и раннер."""
    from tools import corpus

    assert not (sb.local_corpus_uids() & set(corpus.fixture_paths()))


def test_floor_ignores_new_diagrams_in_storage():
    """⛔ Дефект 0.3x: пол ехал вверх от каждой новой диаграммы в storage/.

    Машина разработки (5 собранных, из них 3 параметра по локальному корпусу) и
    раннер (2 собранных) обязаны дать ОДНО число git-видимых и один вердикт по
    полу — иначе гейт меряет содержимое `storage/`, а не потерю тестов.
    """
    dev = sb.parse_collected(COLLECT) - sb.count_local_corpus(COLLECT, LOCAL)
    runner = sb.parse_collected(RUNNER_COLLECT) - sb.count_local_corpus(RUNNER_COLLECT, LOCAL)
    assert dev == runner == 2

    base = dict(BASE, collected=5, min_collected=2)
    for visible in (dev, runner):
        assert not any(
            "усох" in p
            for p in sb.verdict(base, set(BASE["red"]), BASE["totals"], visible, 1)
        )


def test_shrink_of_git_visible_part_is_still_caught():
    """Потеря настоящих тестов сквозь новый счёт проходить не должна."""
    problems = sb.verdict(BASE, set(BASE["red"]), BASE["totals"], 705, 1)
    assert any("усох" in p and "git-видимой" in p for p in problems)

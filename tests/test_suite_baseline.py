# -*- coding: utf-8 -*-
"""Сторож базовой линии набора (`tools/suite_baseline.py`, пункт 0.3).

Стенд сам стал гейтом дороги, поэтому его разбор вывода pytest проверяется:
молча съеденная строка `FAILED ...` означает, что регрессия проедет в зелёном
CI. Тесты чистые — на синтетическом выводе, pytest внутрь себя не запускают.
"""
import json

import pytest

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


def _check_stand(monkeypatch, run, collect="tests/test_alpha.py::test_one\n\n1 test collected in 1.0s\n"):
    """Стенд ЧТЕНИЯ: pytest подменён парой (вывод прогона, вывод сбора).

    Пол базы опущен до 1, чтобы сбор из одной строки его не ронял: иначе
    любой тест ниже мерил бы усыхание набора вместо своей ветки — ровно та
    ловушка «покраснел сосед», что поймана в 1-28.
    """
    monkeypatch.setattr(sb, "_pytest",
                        lambda args: (collect, 0) if "--collect-only" in args else run)
    monkeypatch.setattr(sb, "_load_baseline", lambda: dict(BASE, min_collected=1))


def test_killed_run_on_check_is_unjudgeable(monkeypatch, capsys):
    """⛔ Дыра 1-30 (б): у пути ЧТЕНИЯ третьего исхода не было вовсе.

    `os._exit(77)` в первом тесте (класс §24.6 — access violation в этом же
    наборе уже случался) обрывает вывод: итоговой строки нет, красных не
    разобрано ни одного. Гейт 0.3 научился на этом краснеть — но тем же
    кодом 1, что на доказанной регрессии, и вызывающий не мог отличить
    «чини обстановку» от «чини код». Зелёным это не становится ни на шаг:
    2 так же не ноль, и CI на ней так же красен.
    """
    _check_stand(monkeypatch, ("tests/test_alpha.py .\n", 77))

    assert sb.cmd_check() == 2
    out = capsys.readouterr().out
    assert "[СУДИТЬ НЕЧЕМ]" in out and "77" in out
    assert "[ПРОВАЛ]" not in out, "покраснел не тот механизм: провал вместо «судить нечем»"


def test_new_red_on_check_is_still_a_regression(monkeypatch, capsys):
    """Обратная полярность: замер состоялся и говорит про КОД — это 1.

    Без неё правка 1-30 (б) односторонняя: стенд, отвечающий «судить нечем»
    на всё подряд, гейтом не является.
    """
    _check_stand(monkeypatch, (REPORT_GREW, 1))

    assert sb.cmd_check() == 1
    out = capsys.readouterr().out
    assert "[ПРОВАЛ] новые красные" in out
    assert "tests/test_delta.py::test_four" in out


def test_proven_regression_outranks_a_broken_measurement(monkeypatch, capsys):
    """Приоритет тот же, что у трёх соседей: доказанный регресс сильнее неполноты.

    Прогон убит (код 77) И в нём виден красный, которого нет в базе, — вердикт 1,
    а не 2: про код уже есть что сказать.
    """
    _check_stand(monkeypatch, (REPORT_GREW, 77))

    assert sb.cmd_check() == 1
    out = capsys.readouterr().out
    assert "[СУДИТЬ НЕЧЕМ]" in out and "77" in out
    assert "[ПРОВАЛ] новые красные" in out


def test_run_without_summary_line_is_a_failure():
    """Оборванный хвост при штатном коде возврата — тоже не «всё зелено»."""
    assert sb.verdict(set(), {}, 1)


def test_partial_output_caught_by_counter_invariant():
    """Счётчики говорят про 3 красных, а разобран один — вывод неполон."""
    problems = sb.verdict({"tests/test_alpha.py::test_one"}, BASE["totals"], 1)
    assert any("вывод неполон" in p for p in problems)


def test_healthy_run_has_no_problems():
    assert sb.verdict(set(BASE["red"]), BASE["totals"], 1) == []
    assert sb.verdict(set(), {"failed": 0, "passed": 700, "errors": 0}, 0) == []


def test_shrunken_suite_is_a_failure():
    assert any("усох" in p for p in sb.floor_problems(BASE, {"tests/test_alpha.py": 705})[0])


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
    if "per_file" in base:      # базы до 1-27 карты файлов не несут
        assert sum(n for n, _ in base["per_file"].values()) == base["collected_git_visible"], (
            "карта файлов и git-видимое число разошлись"
        )


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
        assert not sb.floor_problems(base, {"tests/test_alpha.py": visible})[0]


def test_shrink_of_git_visible_part_is_still_caught():
    """Потеря настоящих тестов сквозь новый счёт проходить не должна."""
    problems = sb.floor_problems(BASE, {"tests/test_alpha.py": 705})[0]
    assert any("усох" in p and "git-видимой" in p for p in problems)


def test_split_collected_maps_files_and_corpus():
    """Карта файлов и счёт корпуса вне git — из одного разбора."""
    per_file, local = sb.split_collected(COLLECT, LOCAL)
    assert local == 3
    assert per_file == {
        "tests/test_alpha.py": 1,
        "tests/test_canvas_pipeline_golden.py": 1,
    }


# --- пересъём не легализует новых красных (пункт 1-26) -------------------------

# Тот же прогон плюс один упавший тест, которого в базе нет. Счётчики сходятся с
# числом разобранных id — значит вывод полон и отличается ровно состав красных.
REPORT_GREW = """\
....F..E..F
=========================== short test summary info ============================
FAILED tests/test_alpha.py::test_one - AssertionError: assert 1 == 2
FAILED tests/test_beta.py::TestX::test_two - ValueError
FAILED tests/test_delta.py::test_four - AssertionError: подложенный красный
ERROR tests/ui/test_gamma.py::test_three - ValueError: not enough values
3 failed, 660 passed, 13 skipped, 5 warnings, 1 error in 19.11s
"""

# Один из красных починили — законный повод пересъёма.
REPORT_FIXED = """\
....F..E...
=========================== short test summary info ============================
FAILED tests/test_beta.py::TestX::test_two - ValueError
ERROR tests/ui/test_gamma.py::test_three - ValueError: not enough values
1 failed, 662 passed, 13 skipped, 5 warnings, 1 error in 19.11s
"""


def _stand(tmp_path, monkeypatch, report, collected=738, base=BASE, was=1):
    """Стенд пересъёма: своё дерево и своя база, pytest подменён выводом.

    Дерево настоящее: пересъём кладёт в базу отпечаток каждого файла карты,
    поэтому файл из подменённого сбора обязан существовать.
    """
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_alpha.py").write_text("def test_one(): pass\n", encoding="utf-8")
    monkeypatch.setattr(sb, "REPO", tmp_path)
    path = tmp_path / "suite_baseline.json"
    if base is not None:
        # Карта файлов обязана сходиться с подменённым сбором: с пункта 1-28
        # пересъём смотрит и на пол набора, и рассогласованная фикстура ловила
        # бы отказ там, где тест проверяет совсем другое. `was` — сколько тестов
        # база числит за файлом; сбор всегда отдаёт один.
        base = dict(base, per_file={
            "tests/test_alpha.py": [was, sb.file_digest(tmp_path / "tests" / "test_alpha.py")]})
        path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")

    def fake_pytest(args):
        if "--collect-only" in args:
            return (f"tests/test_alpha.py::test_one\n\n{collected} tests collected in 1.0s\n", 0)
        return (report, 1)

    monkeypatch.setattr(sb, "BASELINE", path)
    monkeypatch.setattr(sb, "_pytest", fake_pytest)
    return path


def test_write_refuses_when_red_set_grew(tmp_path, monkeypatch, capsys):
    """⛔ Дыра 1-26: `--write-baseline` не загружал предыдущую базу вовсе.

    Пересъём идёт ОТДЕЛЬНЫМ коммитом (так требует Д6), поэтому красный флаг №4
    протокола («эталон изменён тем же коммитом, что и код») его не видит: без
    этой сверки любой пересъём молча узаконивал новый красный.
    """
    path = _stand(tmp_path, monkeypatch, REPORT_GREW)
    before = path.read_text(encoding="utf-8")

    # Код 1 — именно ОТКАЗ: замер сделан, и он говорит «пересъём узаконил бы
    # регрессию». «Судить нечем» отдаёт 2 и живёт отдельно (пункт 1-28).
    assert sb.cmd_write() == 1
    assert "tests/test_delta.py::test_four" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before, "база переписана вопреки отказу"


def test_write_accepts_when_red_turned_green(tmp_path, monkeypatch):
    """Законный путь №1: красные ушли — пересъём и есть способ это записать."""
    path = _stand(tmp_path, monkeypatch, REPORT_FIXED)

    assert sb.cmd_write() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["red"] == [
        "tests/test_beta.py::TestX::test_two",
        "tests/ui/test_gamma.py::test_three",
    ]


def test_write_accepts_same_red_set(tmp_path, monkeypatch):
    """Законный путь №2: состав тот же, выросло собранное (новые зелёные тесты).

    Это и есть рядовой Д6-пересъём дороги: пункт принёс тесты, красные не
    тронуты, база переезжает на новое `collected`.
    """
    path = _stand(tmp_path, monkeypatch, REPORT, collected=750)

    assert sb.cmd_write() == 0
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["red"] == sorted(BASE["red"])
    assert written["collected"] == 750


def test_write_bootstraps_without_previous_baseline(tmp_path, monkeypatch):
    """Первого снимка сверять не с чем — отказ обязан молчать."""
    path = _stand(tmp_path, monkeypatch, REPORT_GREW, base=None)

    assert sb.cmd_write() == 0
    assert len(json.loads(path.read_text(encoding="utf-8"))["red"]) == 4


def test_write_records_the_file_map(tmp_path, monkeypatch):
    """Пересъём кладёт в базу карту файлов — без неё `--check` судит одним числом."""
    path = _stand(tmp_path, monkeypatch, REPORT_FIXED)

    assert sb.cmd_write() == 0
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["per_file"] == {
        "tests/test_alpha.py": [1, sb.file_digest(tmp_path / "tests" / "test_alpha.py")]
    }


def test_write_on_incomplete_output_is_unjudgeable(tmp_path, monkeypatch, capsys):
    """Сверка на неполном списке ничего не значит: счётчики против числа id.

    Иначе отказ обходится оборванным хвостом: разобрано меньше красных, чем
    было на самом деле, — и «рост» не виден. Код 2, а не 1 (пункт 1-28): тут
    не доказана регрессия, а нечем судить — разница видна вызывающему скрипту.
    """
    truncated = "FAILED tests/test_alpha.py::test_one - X\n2 failed, 661 passed, 1 error in 1.0s\n"
    path = _stand(tmp_path, monkeypatch, truncated)
    before = path.read_text(encoding="utf-8")

    assert sb.cmd_write() == 2
    assert "вывод неполон" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before


# --- откат пункта — не усыхание набора (пункт 1-27) ----------------------------

TWO_TESTS = "def test_a(): pass\ndef test_b(): pass\n"


def _tree(tmp_path, monkeypatch, files, tracked=()):
    """Временное дерево из настоящих файлов + подменённый ответ git про пропавшие."""
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(sb, "REPO", tmp_path)
    monkeypatch.setattr(sb, "tracked_by_git", lambda paths: (set(paths) & set(tracked), ""))


def _base(per_file):
    return dict(BASE, per_file={path: list(value) for path, value in per_file.items()})


def test_rollback_of_an_item_is_not_a_shrink(tmp_path, monkeypatch):
    """⛔ Дефект 1-27: гейт краснел от ЛЕГАЛЬНОГО отката пункта.

    Пункты дороги приносят по 5–53 теста при допуске 5, поэтому `git revert`
    по тегу — обещанная `PROTOCOL §6` точка возврата — ронял git-видимую часть
    ниже пола, и стенд печатал «набор усох». Файл ушёл вместе с записью о нём
    в git: потеря видна в диффе и судится ревизором, а не полом набора.
    """
    _tree(tmp_path, monkeypatch, {"tests/test_alpha.py": "def test_one(): pass\n"})
    base = _base({
        "tests/test_alpha.py": (1, sb.file_digest(tmp_path / "tests/test_alpha.py")),
        "tests/test_item.py": (20, "0" * 12),          # снят откатом пункта
    })

    problems, notes, _ = sb.floor_problems(base, {"tests/test_alpha.py": 1})

    assert problems == []
    assert any("tests/test_item.py: 20 → 0" in note for note in notes), notes


def test_untouched_file_losing_tests_is_still_a_shrink(tmp_path, monkeypatch):
    """Обратная полярность: файл байт-в-байт тот же, а тестов из него меньше.

    Так уходят `importorskip` без установленной зависимости и `collect_ignore`
    каталога — в диффе не видно ничего, и ловит это только пол набора. Правка
    1-27 обязана оставить этот путь красным, иначе она односторонняя.
    """
    _tree(tmp_path, monkeypatch, {"tests/test_layout.py": TWO_TESTS})
    base = _base({"tests/test_layout.py": (22, sb.file_digest(tmp_path / "tests/test_layout.py"))})

    problems, _, _ = sb.floor_problems(base, {"tests/test_layout.py": 0})

    assert any("усох" in p and "тихо потеряно 22" in p for p in problems), problems


def test_silent_loss_is_judged_against_an_absolute_margin(tmp_path, monkeypatch):
    """Допуск заперт с двух сторон: 5 тихо потерянных прощаются, 6 — уже нет.

    Числа абсолютные, из проверяемой константы не вычисляются: иначе тест
    остался бы зелёным при любом её значении, и допуск можно было бы
    расширить до размера отката (пункт 1-27).
    """
    _tree(tmp_path, monkeypatch, {"tests/test_layout.py": TWO_TESTS})
    base = _base({"tests/test_layout.py": (30, sb.file_digest(tmp_path / "tests/test_layout.py"))})

    assert sb.floor_problems(base, {"tests/test_layout.py": 25})[0] == []
    assert sb.floor_problems(base, {"tests/test_layout.py": 24})[0]


def test_file_erased_past_git_is_a_shrink(tmp_path, monkeypatch):
    """Файла в дереве нет, а git его числит — это пропажа, а не откат."""
    _tree(tmp_path, monkeypatch, {}, tracked={"tests/test_item.py"})
    base = _base({"tests/test_item.py": (20, "0" * 12)})

    problems, _, _ = sb.floor_problems(base, {})

    assert any("усох" in p and "tests/test_item.py: 20 → 0" in p for p in problems), problems


def test_edited_file_losing_tests_is_explained(tmp_path, monkeypatch):
    """Файл правили — потеря видна в диффе; стенд её печатает и не краснеет."""
    _tree(tmp_path, monkeypatch, {"tests/test_layout.py": TWO_TESTS})
    base = _base({"tests/test_layout.py": (22, "0" * 12)})   # отпечаток разошёлся

    problems, notes, _ = sb.floor_problems(base, {"tests/test_layout.py": 0})

    assert problems == []
    assert any("в диффе" in note for note in notes), notes


def test_git_silence_is_unjudgeable_not_a_refusal(tmp_path, monkeypatch):
    """⛔ Полярность 1-30 (в): молчащий git — «судить нечем», а не отказ.

    До 1-30 исчезнувшие файлы при сломанном git уходили в ТИХУЮ потерю
    («осторожная сторона»), и стенд краснел провалом — то есть врал про код
    там, где сломана обстановка: откат пункта от тихой пропажи различает
    ровно ответ git, и без него вердикта нет ни в ту, ни в другую сторону.
    Зелёным это не становится: причина громкая, а код — 2.
    """
    _tree(tmp_path, monkeypatch, {})
    monkeypatch.setattr(sb, "tracked_by_git", lambda paths: (set(paths), "fatal: not a git repository"))
    base = _base({"tests/test_item.py": (20, "0" * 12)})

    problems, _, unjudged = sb.floor_problems(base, {})

    assert problems == [], "молчание git не имеет права быть провалом"
    assert any("git не ответил" in u for u in unjudged), unjudged
    assert any("tests/test_item.py: 20 → 0" in u for u in unjudged), unjudged


def test_missing_git_binary_is_unjudgeable_too(monkeypatch):
    """Та же семья, шире записанного: git не вернул код, а вовсе не запустился.

    Замерено 2026-08-19: без git в PATH `subprocess.run(["git", …])` кидает
    `FileNotFoundError`, а он мимо проверки `returncode != 0` — стенд умирал
    трейсбеком с кодом 1 там, где по смыслу «судить нечем».
    """
    def no_git(cmd, *a, **kw):
        raise FileNotFoundError(2, "The system cannot find the file specified", "git")

    monkeypatch.setattr(sb.subprocess, "run", no_git)

    tracked, error = sb.tracked_by_git({"tests/test_item.py"})

    assert tracked == {"tests/test_item.py"}
    assert "git не запустился" in error


def test_git_silence_does_not_hide_a_real_shrink(tmp_path, monkeypatch):
    """Обратная полярность: файлы НА МЕСТЕ судятся отпечатком и без git.

    Иначе правка 1-30 (в) была бы односторонней — сломанный git глушил бы
    пол целиком, а не только ту его часть, которая от git и зависит.
    """
    _tree(tmp_path, monkeypatch, {"tests/test_layout.py": TWO_TESTS})
    monkeypatch.setattr(sb, "tracked_by_git", lambda paths: (set(paths), "fatal: not a git repository"))
    base = _base({"tests/test_layout.py": (30, sb.file_digest(tmp_path / "tests/test_layout.py"))})

    problems, _, unjudged = sb.floor_problems(base, {"tests/test_layout.py": 0})

    assert any("тихо потеряно 30" in p for p in problems), problems
    assert unjudged == [], "исчезнувших файлов нет — судить нечему нечего"


def test_old_baseline_without_map_is_judged_by_the_number():
    """База, снятая до 1-27, карты не несёт — судится прежним полом."""
    assert sb.floor_problems(BASE, {"tests/test_alpha.py": 705})[0]
    assert sb.floor_problems(BASE, {"tests/test_alpha.py": 706})[0] == []


def test_digest_ignores_line_endings(tmp_path):
    """CRLF у машины разработки против LF у раннера — отпечаток обязан совпасть.

    Иначе в CI «изменённым» выглядел бы каждый файл эталона, а изменённому
    файлу потеря тестов прощается — пол ослеп бы целиком.
    """
    crlf, lf, other = tmp_path / "crlf.py", tmp_path / "lf.py", tmp_path / "other.py"
    crlf.write_bytes(b"def test_a():\r\n    pass\r\n")
    lf.write_bytes(b"def test_a():\n    pass\n")
    other.write_bytes(b"def test_b():\n    pass\n")

    assert sb.file_digest(crlf) == sb.file_digest(lf)
    assert sb.file_digest(other) != sb.file_digest(lf)


# --- путь ЗАПИСИ: пол набора и три исхода (пункт 1-28) ------------------------

def test_write_refuses_when_the_floor_dropped(tmp_path, monkeypatch, capsys):
    """⛔ Дыра 1-28 (в): пересъём не смотрел на пол набора вовсе.

    Замер до правки на живом дереве: `collect_ignore` каталога — файлы при этом
    байт-в-байт те же — унёс 147 тестов, `--write-baseline` отдал exit 0 и
    записал пол 952 → 811, карту 89 → 64 файлов; состав красных не изменился,
    поэтому сверка 1-26 ничего не заметила. Следующий `--check` на ЗДОРОВОМ
    дереве после этого зелёный: потеря стала нормой.
    ⚠ Сторож `test_suite_does_not_shrink` тут не замена стенду: он сам из
    набора и уходит вместе с тем, что его съело (замер §48.5).
    """
    path = _stand(tmp_path, monkeypatch, REPORT, was=30)
    before = path.read_text(encoding="utf-8")

    assert sb.cmd_write() == 1
    assert "тихо потеряно 29" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before, "база переписана вопреки отказу"


def test_write_after_a_rollback_of_an_item_is_legal(tmp_path, monkeypatch):
    """Положительный контроль: потерю видно в диффе — пересъём проходит.

    Иначе правка односторонняя. Штатный `git revert` по тегу (`PROTOCOL §6`)
    снимает тест-файл вместе с записью о нём в git, и Д6-пересъём после отката
    обязан оставаться законным — ровно то, что разводил пункт 1-27.
    """
    path = _stand(tmp_path, monkeypatch, REPORT)
    monkeypatch.setattr(sb, "tracked_by_git", lambda paths: (set(), ""))
    base = json.loads(path.read_text(encoding="utf-8"))
    base["per_file"]["tests/test_item.py"] = [20, "0" * 12]   # снят откатом пункта
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")

    assert sb.cmd_write() == 0
    assert "tests/test_item.py" not in json.loads(path.read_text(encoding="utf-8"))["per_file"]


def test_write_tolerates_a_loss_inside_the_margin(tmp_path, monkeypatch):
    """Допуск заперт с двух сторон абсолютными числами, как и у `--check`.

    30 → 25 (тихо потеряно 5) пересъём пропускает, 30 → 24 отказывает: числа
    не вычисляются из `FLOOR_MARGIN`, иначе тест остался бы зелёным при любом
    её значении (урок пункта 0.4).
    """
    _tree(tmp_path, monkeypatch, {"tests/test_layout.py": TWO_TESTS})
    base = _base({"tests/test_layout.py": (30, sb.file_digest(tmp_path / "tests/test_layout.py"))})
    red, totals = set(BASE["red"]), BASE["totals"]

    assert sb.write_blocked(base, red, totals, {"tests/test_layout.py": 25})[1] == []
    assert sb.write_blocked(base, red, totals, {"tests/test_layout.py": 24})[1]


def test_broken_collection_on_write_is_unjudgeable(tmp_path, monkeypatch):
    """⛔ Дыра 1-28 (г): «судить нечем» и «отказ» отдавали один и тот же код 1.

    Замер до правки: сломанный сбор → exit 1 «сбор pytest сломан (exit 4)»,
    выросшие красные → exit 1 «[ОТКАЗ] …». Вызывающий скрипт различить их
    не мог, а это два разных решения: чинить обстановку или чинить код.
    """
    _stand(tmp_path, monkeypatch, REPORT)
    monkeypatch.setattr(sb, "_pytest",
                        lambda args: ("", 4) if "--collect-only" in args else (REPORT, 1))

    with pytest.raises(SystemExit) as exc:
        sb.cmd_write()

    assert exc.value.code == 2


def test_broken_collection_on_check_is_unjudgeable(tmp_path, monkeypatch, capsys):
    """⛔ Пункт 1-30 (б) довёл до чтения то, что 1-28 сделал на записи.

    Один и тот же сломанный сбор давал у пересъёма 2, а у сверки 1 — тот же
    код, что «в наборе новый красный». Между тем сбор ломает пункт 0.0, а не
    база: обвинять базу нечем. CI обе двойки-единицы красит одинаково (шаг
    падает на любом ненулевом), так что менять было безопасно — менялся
    диагноз, а не цвет.
    """
    _stand(tmp_path, monkeypatch, REPORT)
    monkeypatch.setattr(sb, "_pytest",
                        lambda args: ("", 4) if "--collect-only" in args else (REPORT, 1))

    with pytest.raises(SystemExit) as exc:
        sb.cmd_check()

    assert exc.value.code == 2
    assert "[СУДИТЬ НЕЧЕМ]" in capsys.readouterr().out


def test_killed_run_on_write_is_unjudgeable(tmp_path, monkeypatch, capsys):
    """Убитый прогон — тоже «судить нечем» (2), а не отказ."""
    path = _stand(tmp_path, monkeypatch, REPORT)
    monkeypatch.setattr(sb, "_pytest", lambda args: (
        ("tests/test_alpha.py::test_one\n\n738 tests collected in 1.0s\n", 0)
        if "--collect-only" in args else (REPORT, 77)))
    before = path.read_text(encoding="utf-8")

    assert sb.cmd_write() == 2
    assert "СУДИТЬ НЕЧЕМ" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before

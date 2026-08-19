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
        "stable": {"aaaaaaaa": True, "bbbbbbbb": False},
        "inputs": {"aaaaaaaa": "in-a", "bbbbbbbb": "in-b"}}


def _lines(result, base=BASE, runs=6, routing=True, inputs=None):
    """`runs=6` — ровно столько, сколько записано в `BASE`.

    С пункта 1-30 сверка судит и силу самой выборки: без явного числа каждый
    тест ниже мерил бы «замер слабее эталона» вместо своей ветки
    (`DEFAULT_RUNS` = 2). Число абсолютное, из проверяемого поля не считается.

    `inputs` — отпечатки ВХОДНЫХ данных замера (пункт GATE-6). По умолчанию
    берутся из самого эталона: «вход тот же, на котором эталон снят», — так
    тесты выше судят ровно то, что судили до GATE-6. Подмена входа разбирается
    отдельными тестами ниже, и отпечаток там задаётся явно.
    """
    if inputs is None:
        known = base.get("inputs", {})
        inputs = {u: known.get(u, f"свежий-{u}") for u in result}
    code, lines = det.verdict(result, base, runs, routing, inputs)
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


# ───── слабый замер на ЧТЕНИИ — тоже «судить нечем» (пункт 1-30) ─────

def test_check_with_fewer_runs_than_the_baseline_is_not_green(capsys):
    """⛔ Дыра 1-30 (з), кандидат из §53.35: `--check --runs 2` врал зелёным.

    Эталон снят при 6 прогонах, а замер в 2 печатал «[стало лучше]
    воспроизводятся впервые» про заведомо неповторимые графы и отдавал
    exit 0 — то есть «доказано, что регресса нет» по выборке, которая этого
    доказать не может (§49.27: при `--runs 2` четыре из восьми известных
    неповторимых выходят воспроизводимыми). Ложного КРАСНОГО малый `--runs`
    дать не может, поэтому 1-29 пола на чтении не ставил; вводящий в
    заблуждение зелёный — тоже дефект гейта, и лечится он третьим исходом.
    """
    code, text = _lines({"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "z"]}, runs=2)

    assert code == 2
    assert "прогонов 2 против 6" in text
    assert "[ВЫБОРКА, НЕ ПОЧИНКА]" in text and "bbbbbbbb" in text
    assert "[стало лучше]" not in text, "утверждение о починке по слабой выборке"
    assert "[OK] регресса воспроизводимости нет" not in text


@pytest.mark.parametrize("runs", [6, 7])
def test_check_at_or_above_the_baseline_runs_judges(runs):
    """Порог заперт с двух сторон АБСОЛЮТНЫМИ числами (урок 0.4).

    Эталон снят при 6 прогонах: 5 — «судить нечем» (тест ниже), 6 и 7 —
    вердикт выносится. Ни одно число не вычисляется из проверяемого поля.
    """
    code, text = _lines({"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "z"]}, runs=runs)

    assert code == 0
    assert "[стало лучше] воспроизводятся впервые: bbbbbbbb" in text


def test_check_with_five_runs_is_below_the_baseline():
    assert _lines({"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "w"]}, runs=5)[0] == 2


def test_check_with_another_routing_key_is_not_green(capsys):
    """Тот же класс, что малый `--runs`: без роутинга раскладка воспроизводима вся."""
    code, text = _lines({"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "z"]},
                        runs=6, routing=False)

    assert code == 2
    assert "роутинг" in text


def test_broken_graph_outranks_a_weak_sample_on_read():
    """Приоритет тот же: поломка наблюдается двумя исходами и от выборки не зависит."""
    code, text = _lines({"aaaaaaaa": ["x", "y"], "bbbbbbbb": ["z", "z"]}, runs=2)

    assert code == 1
    assert "перестали воспроизводиться: aaaaaaaa" in text


# ───── чем именно судить нечем: метка для шага CI (пункт 1-30) ─────

def test_truncated_corpus_is_marked_as_corpus():
    """Усечённый корпус — штатная обстановка CI, шаг переводит её в ::warning."""
    code, text = _lines({"aaaaaaaa": ["x", "x"]})

    assert code == 2
    assert text.strip().endswith(det.MARK_UNJUDGED_CORPUS)


def test_weak_sample_is_not_marked_as_corpus():
    """⛔ Обратная сторона (г): дыру в шаге CI открывать нельзя.

    Шаг CI прощает exit 2 «корпус усечён» — там в git 3 графа из 19. Если бы
    ту же метку получала любая двойка, убитый дочерний прогон и слабая
    выборка ехали бы в CI зелёным warning'ом. Причина двойки называется в
    последней строке, и корпус от обстановки в ней отличим.
    """
    code, text = _lines({"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "z"]}, runs=2)

    assert code == 2
    assert text.strip().endswith(det.MARK_UNJUDGED_OTHER)
    assert det.MARK_UNJUDGED_CORPUS not in text


def test_killed_child_on_check_is_unjudgeable_not_a_traceback(monkeypatch, capsys):
    """⛔ Дыра 1-30 (г): убитый дочерний прогон умирал `RuntimeError` → exit 1.

    Полярность была безопасная (громко и красным), но перепутанная: замер
    не состоялся, значит «судить нечем», а не «регресс». Трейсбек при этом
    исчезает, а причина остаётся в тексте.
    """
    monkeypatch.setattr(det.corpus, "corpus_paths",
                        lambda include_storage=True: {"aaaaaaaa": "x", "bbbbbbbb": "y"})
    # корпус здесь фиктивный, снимать отпечаток не с чего (пункт GATE-6)
    monkeypatch.setattr(det, "input_fingerprints",
                        lambda paths: {u: f"in-{u}" for u in paths})
    monkeypatch.setattr(det, "measure", lambda *a: (_ for _ in ()).throw(
        RuntimeError("прогон (seed=1) вернул 77: boom")))
    monkeypatch.setattr(sys, "argv",
                        ["layout_determinism.py", "--check", "--runs", "6"])

    assert det.main() == 2
    out = capsys.readouterr().out
    assert "[СУДИТЬ НЕЧЕМ]" in out and "вернул 77" in out
    assert out.strip().endswith(det.MARK_UNJUDGED_OTHER), "CI простил бы убитый прогон"


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

def _write_stand(tmp_path, monkeypatch, measured, base=BASE, argv=("--runs", "6"),
                 inputs=None):
    """Стенд пересъёма: свой эталон, свой корпус, замер подменён.

    `measured` — {uid8: [sha прогона 1, sha прогона 2, ...]}: и корпус, и то,
    что по нему намерилось, задаются одним словарём.

    `argv` по умолчанию несёт `--runs 6` — ровно столько, сколько записано
    в `BASE`: с пункта 1-29 пересъём слабее эталона отказывает, и без этого
    ключа любой из тестов ниже мерил бы отказ по числу прогонов вместо
    своей ветки (`DEFAULT_RUNS` = 2).

    `inputs` — отпечатки входных данных (пункт GATE-6). По умолчанию равны
    эталонным: вход тот же, на котором эталон снят.
    """
    path = tmp_path / "determinism_baseline.json"
    if base is not None:
        path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(det, "BASELINE", path)
    monkeypatch.setattr(det.corpus, "corpus_paths",
                        lambda include_storage=True: dict.fromkeys(measured))
    # Корпус здесь фиктивный (пути `None`), настоящий файл читать нечем:
    # отпечатки берутся из эталона — «вход тот же, на котором он снят».
    # Подменённый вход задаётся `inputs` явно (пункт GATE-6).
    known = (base or {}).get("inputs", {})
    monkeypatch.setattr(det, "input_fingerprints", lambda paths: dict(
        inputs if inputs is not None else
        {u: known.get(u, f"свежий-{u}") for u in paths}))
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

    ⚠ С пункта 1-29 замер `bbbbbbbb` здесь совпадает с эталоном (неповторим):
    случай «эталон числит неповторимым, а замер дал один хеш» разбирается
    отдельно (`test_write_keeps_a_known_unstable_graph_unstable`) и пересъёмом
    больше не проходит. Новый граф `cccccccc` — чтобы записанное отличалось от
    эталона и запись была видна, а не совпала с ним случайно.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "w"],
                         "cccccccc": ["p", "p"]})

    assert det.main() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["stable"] == {
        "aaaaaaaa": True, "bbbbbbbb": False, "cccccccc": True}


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


# ── «стало лучше» на неповторимом графе — не починка, а выборка (пункт 1-29) ──

def test_write_keeps_a_known_unstable_graph_unstable(tmp_path, monkeypatch, capsys):
    """⛔ Замерено СОБСТВЕННЫМ пересъёмом 1-29 (§50.19), а не выведено.

    Полный корпус, `--runs 6` — ровно тот пол, который этот же пункт и ввёл:
    `0aea61c0` вышел с одним хешем, и пересъём молча записал его `true`. Между
    тем §32.14 наблюдал у него **3 разных исхода из 6** — неповторимость
    доказана положительным наблюдением, и удачная серия его не отменяет
    (`TESTING §8.2` называет именно этот граф). Пол по числу прогонов такое
    не держит: он необходим, но недостаточен.

    Правило поэтому храповиковое: `false` в эталоне липкий, снять его может
    только явно объявленная починка (`--allow-fixed`), а не тихий замер.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "z"]})

    assert det.main() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["stable"] == {
        "aaaaaaaa": True, "bbbbbbbb": False}, "неповторимость стёрта выборкой"
    out = capsys.readouterr().out
    assert "bbbbbbbb" in out and "--allow-fixed" in out, "тихо, без объяснения"


def test_write_records_a_declared_fix(tmp_path, monkeypatch, capsys):
    """Обратная сторона: починку записать МОЖНО, но только назвав граф.

    Иначе храповик стал бы вечным: когда недетерминизм libavoid однажды
    починят (ВН3, волна 9), стенд обязан уметь это записать. Ключ и есть то
    самое «объяснение», которого требует `TESTING §8.2`, — он заставляет
    назвать uid руками и объяснить в коммите, ЧТО изменилось.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "z"]},
                        argv=("--runs", "6", "--allow-fixed", "bbbbbbbb"))

    assert det.main() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["stable"] == {
        "aaaaaaaa": True, "bbbbbbbb": True}
    assert "ПОЧИНКА" in capsys.readouterr().out


def test_allow_fixed_does_not_lift_the_refusal(tmp_path, monkeypatch, capsys):
    """⛔ Правда, к которой пункт 1-30 (д) привёл `TESTING §8.2`.

    Док обещал снять отказ по сломавшемуся графу «повторным прогоном и
    пересъёмом с объяснением». Стенд такого пересъёма не знает: `--allow-fixed`
    работает в `merge_stable()` и снимает липкость `false -> true`, а отказ
    ставит `write_blocked()` на обратном движении `true -> false` — и ключа,
    который его снимал бы, нет ни одного. Настоящая лазейка — правка эталона
    руками, видная в диффе; §7.1 про базу набора говорит это честно, §8.2
    теперь тоже.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"aaaaaaaa": ["x", "y"], "bbbbbbbb": ["z", "w"]},
                        argv=("--runs", "6", "--allow-fixed", "aaaaaaaa"))
    before = path.read_text(encoding="utf-8")

    assert det.main() == 1
    assert "перестали воспроизводиться: aaaaaaaa" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before, "отказ обойдён ключом"


# ───── отпечаток ВХОДНЫХ данных: дрейф данных ≠ регресс кода (GATE-6) ─────
#
# Замер долга 1-30 (§70): `8d14cf73` переписали обычным запуском клиента, и
# стенд сказал «опровергнуто» (exit 1) там, где по смыслу «судить нечем» (2).
# Правило трёх исходов `PROTOCOL §5` нарушено с НОВОЙ стороны: обстановка
# ущербна не средой, а ПОДМЕНОЙ ВХОДА. Разбор пришлось вести руками — по датам
# файлов и чужому замеру §62г; CI такой дрейф не увидит никогда.

def test_changed_input_is_unjudgeable_not_a_regression():
    """⛔ Сам дефект пункта: тот же uid, ДРУГИЕ данные — судить нечем.

    Эталон числит граф воспроизводимым, замер даёт два исхода — до GATE-6
    это было «[ПРОВАЛ] перестал воспроизводиться» и exit 1. Но вход другой,
    и вердикт эталона к нему не относится вовсе: ни поломки, ни починки
    здесь не доказано.
    """
    code, text = _lines({"aaaaaaaa": ["x", "y"], "bbbbbbbb": ["z", "w"]},
                        inputs={"aaaaaaaa": "in-a-НОВЫЙ", "bbbbbbbb": "in-b"})

    assert code == 2
    assert "СУДИТЬ НЕЧЕМ" in text and "aaaaaaaa" in text
    assert "перестали воспроизводиться" not in text, \
        "дрейф данных выдан за регресс кода — это и есть дефект GATE-6"


def test_changed_input_names_what_changed():
    """«Судить нечем» обязано называть, ЧТО сменилось, а не только что нечем.

    Ради этого пункт и заводился: разбор §70 стоил ручного сравнения дат
    файлов, потому что стенд сказал только «перестал воспроизводиться».
    """
    code, text = _lines({"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "w"]},
                        inputs={"aaaaaaaa": "in-a-НОВЫЙ", "bbbbbbbb": "in-b"})

    assert code == 2
    assert "in-a" in text and "in-a-НОВЫЙ" in text, \
        "не названы ни прежний отпечаток, ни нынешний"


def test_same_input_still_proves_a_regression():
    """⭐ Обратная полярность: вход ТОТ ЖЕ — поломка по-прежнему exit 1.

    Без этого теста правка односторонняя: стенд, который на всё отвечает
    «судить нечем», гейтом не является.
    """
    code, text = _lines({"aaaaaaaa": ["x", "y"], "bbbbbbbb": ["z", "w"]},
                        inputs={"aaaaaaaa": "in-a", "bbbbbbbb": "in-b"})

    assert code == 1
    assert "перестали воспроизводиться: aaaaaaaa" in text


def test_baseline_without_fingerprints_cannot_judge():
    """Эталон, снятый ДО GATE-6, отпечатка не помнит — значит не судит.

    Это ровно то состояние, в котором стенд прожил всю дорогу: вердикт
    выносился о данных, про которые эталон ничего не знает. Лечится
    единственным пересъёмом (Д6), после которого отпечатки в файле есть.
    """
    old = {"runs": 6, "routing": True,
           "stable": {"aaaaaaaa": True, "bbbbbbbb": False}}
    code, text = _lines({"aaaaaaaa": ["x", "y"], "bbbbbbbb": ["z", "w"]},
                        base=old, inputs={"aaaaaaaa": "in-a", "bbbbbbbb": "in-b"})

    assert code == 2
    assert "не помнит отпечатка" in text
    assert "перестали воспроизводиться" not in text


def test_drift_of_one_graph_does_not_hide_a_regression_in_another():
    """Приоритет тот же, что у 1-25/1-28: доказанное сильнее неполноты.

    Один граф подменён (судить нечем), у второго вход ТОТ ЖЕ и он сломался —
    это 1, и оба факта названы. Иначе достаточно было бы тронуть один файл
    корпуса, чтобы гейт замолчал обо всех.
    """
    base = {"runs": 6, "routing": True,
            "stable": {"aaaaaaaa": True, "cccccccc": True},
            "inputs": {"aaaaaaaa": "in-a", "cccccccc": "in-c"}}
    code, text = _lines({"aaaaaaaa": ["x", "y"], "cccccccc": ["p", "q"]},
                        base=base,
                        inputs={"aaaaaaaa": "in-a-НОВЫЙ", "cccccccc": "in-c"})

    assert code == 1
    assert "перестали воспроизводиться: cccccccc" in text
    assert "aaaaaaaa" in text and "СУДИТЬ НЕЧЕМ" in text
    assert "перестали воспроизводиться: aaaaaaaa" not in text


def test_changed_input_is_not_marked_as_corpus():
    """Метку `unjudged=corpus` шаг CI прощает — подмена входа под неё не идёт.

    В git фикстуры заморожены (`.gitattributes`: `-text`, sha256 в README):
    разошедшийся отпечаток ТАМ означает, что данные сменили, а эталон нет.
    Такое обязано ронять сборку, а не превращаться в `::warning`.
    """
    code, text = _lines({"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "w"]},
                        inputs={"aaaaaaaa": "in-a-НОВЫЙ", "bbbbbbbb": "in-b"})

    assert code == 2
    assert text.strip().endswith(det.MARK_UNJUDGED_OTHER)
    assert det.MARK_UNJUDGED_CORPUS not in text


def test_new_graph_needs_no_fingerprint():
    """Свежий граф корпуса эталону неизвестен — сверять его отпечаток не с чем.

    Ветка «новый граф корпуса неповторим» (1-25) обязана работать по-прежнему:
    отсутствие отпечатка у НЕИЗВЕСТНОГО графа — не «судить нечем», а норма.
    """
    code, text = _lines({"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "w"],
                         "cccccccc": ["p", "q"]},
                        inputs={"aaaaaaaa": "in-a", "bbbbbbbb": "in-b",
                                "cccccccc": "in-c"})

    assert code == 1
    assert "новый граф корпуса неповторим: cccccccc" in text


def test_check_names_the_graph_whose_data_changed(tmp_path, monkeypatch, capsys):
    """Проводка целиком: `main()` обязан САМ снять отпечатки и отдать их судье.

    Отдельно от `verdict()`, потому что верный судья при неподключённом
    отпечатке — это гейт, который молчит: на пустых `inputs` судья зелен,
    и ошибку проводки поймать может только сквозной прогон.
    """
    path = tmp_path / "determinism_baseline.json"
    path.write_text(json.dumps(BASE, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(det, "BASELINE", path)
    monkeypatch.setattr(det.corpus, "corpus_paths",
                        lambda include_storage=True: {"aaaaaaaa": tmp_path / "a.json",
                                                      "bbbbbbbb": tmp_path / "b.json"})
    (tmp_path / "a.json").write_text('{"nodes": ["ПОДМЕНА"]}', encoding="utf-8")
    (tmp_path / "b.json").write_text('{"nodes": []}', encoding="utf-8")
    monkeypatch.setattr(det, "measure", lambda uids, runs, routing: {
        uid: [{"sha": "s", "defects": [1, 0]} for _ in range(runs)] for uid in uids})
    monkeypatch.setattr(sys, "argv",
                        ["layout_determinism.py", "--check", "--runs", "6"])

    assert det.main() == 2
    out = capsys.readouterr().out
    assert "aaaaaaaa" in out and "СУДИТЬ НЕЧЕМ" in out
    assert "[OK] регресса воспроизводимости нет" not in out


# ───── тот же отпечаток на пути ЗАПИСИ: пересъём его записывает ─────

def test_write_records_the_input_fingerprints(tmp_path, monkeypatch):
    """Положительный контроль: эталон уносит с собой отпечаток каждого графа.

    Без этого следующая сверка снова осталась бы без отпечатка — то самое
    состояние, ради которого пункт и заведён.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "w"]},
                        inputs={"aaaaaaaa": "in-a", "bbbbbbbb": "in-b"})

    assert det.main() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["inputs"] == {
        "aaaaaaaa": "in-a", "bbbbbbbb": "in-b"}


def test_write_of_a_graph_whose_input_changed_is_not_refused(
        tmp_path, monkeypatch, capsys):
    """⭐ Чем пункт оплачивает работу первую: пересъём подменённого входа ЗАКОНЕН.

    Отказ 1-28 («перестал воспроизводиться — пересъём записал бы поломку
    нормой») стоит на утверждении, что вход тот же. Когда вход другой,
    утверждения о поломке нет вовсе, а есть новые данные — их и записывают.
    Молча это делать нельзя: причина печатается.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"aaaaaaaa": ["x", "y"], "bbbbbbbb": ["z", "w"]},
                        inputs={"aaaaaaaa": "in-a-НОВЫЙ", "bbbbbbbb": "in-b"})

    assert det.main() == 0
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["stable"] == {"aaaaaaaa": False, "bbbbbbbb": False}
    assert written["inputs"]["aaaaaaaa"] == "in-a-НОВЫЙ"
    out = capsys.readouterr().out
    assert "aaaaaaaa" in out and "in-a" in out, "вход подменили молча"


def test_write_still_refuses_a_broken_graph_on_the_same_input(
        tmp_path, monkeypatch, capsys):
    """⭐ Обратная полярность записи: отказ 1-28 цел, пока вход ТОТ ЖЕ.

    Дверь открыта ровно на подмену входа и ни на палец шире.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"aaaaaaaa": ["x", "y"], "bbbbbbbb": ["z", "w"]},
                        inputs={"aaaaaaaa": "in-a", "bbbbbbbb": "in-b"})
    before = path.read_text(encoding="utf-8")

    assert det.main() == 1
    assert "перестали воспроизводиться: aaaaaaaa" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before, "эталон переписан вопреки отказу"


def test_changed_input_does_not_lift_the_stickiness(tmp_path, monkeypatch, capsys):
    """⛔ Липкость 1-29 подменой входа НЕ снимается — так решил архитектор.

    Направление разрешено только одно: граф ДОБАВЛЯЕТСЯ в неповторимые.
    Обратное (`false -> true`) остаётся за `--allow-fixed` и объяснением
    в коммите, даже когда данные и правда другие: иначе достаточно было бы
    пересохранить схему в клиенте, чтобы вычеркнуть её из списка.
    """
    path = _write_stand(tmp_path, monkeypatch,
                        {"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "z"]},
                        inputs={"aaaaaaaa": "in-a", "bbbbbbbb": "in-b-НОВЫЙ"})

    assert det.main() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["stable"]["bbbbbbbb"] is False
    assert "--allow-fixed" in capsys.readouterr().out


# ───────── сам отпечаток: что он обязан и чего не обязан видеть ─────────

def test_fingerprint_ignores_formatting_but_sees_data(tmp_path):
    """Отпечаток снимается с СОДЕРЖИМОГО, а не с байтов файла.

    `.gitattributes` нормализует json по EOL (`* text=auto eol=lf`), и
    байтовый отпечаток краснел бы от одного переноса строки — вторая
    EOL-ловушка после `.gitattributes` из 0.8 (замечание ревизии 1-28).
    Проекция взята та же, которой стенд хеширует сам граф.
    """
    from tools import corpus

    plain = tmp_path / "plain.json"
    fancy = tmp_path / "fancy.json"
    other = tmp_path / "other.json"
    plain.write_bytes(b'{"nodes": [{"id": "N1"}], "links": []}')
    fancy.write_bytes(
        b'{\r\n "nodes": [\r\n  {"id": "N1"}\r\n ],\r\n "links": []\r\n}\r\n')
    other.write_bytes(b'{"nodes": [{"id": "N2"}], "links": []}')

    assert corpus.data_fingerprint(plain) == corpus.data_fingerprint(fancy)
    assert corpus.data_fingerprint(plain) != corpus.data_fingerprint(other)


def test_baseline_in_git_remembers_the_fixture_fingerprints():
    """Три графа фикстуры (то, что видит CI) — с отпечатком, и он сходится.

    Фикстуры заморожены `.gitattributes` (`-text`) и их sha256 записаны
    в `tests/fixtures/graph/README.md`; расхождение здесь означает, что
    данные сменили, а эталон не переснимали.
    """
    from tools import corpus

    base = json.loads(det.BASELINE.read_text(encoding="utf-8"))
    for uid8, path in corpus.fixture_paths().items():
        assert base.get("inputs", {}).get(uid8) == corpus.data_fingerprint(path), \
            f"отпечаток графа {uid8} в эталоне разошёлся с фикстурой"


@pytest.mark.parametrize("payload,match", [
    ("{ это не json", "отпечаток входа не снят"),
    (None, "отпечаток входа не снят"),        # файла нет вовсе
])
def test_unreadable_corpus_file_is_unjudgeable_not_a_traceback(
        tmp_path, monkeypatch, capsys, payload, match):
    """⛔ Полярность 1-30 на НОВОМ пути: снятие отпечатка идёт ДО замера.

    Отпечаток снимается разбором файла, то есть у стенда появился путь,
    который может умереть раньше `measure()` — и до этого теста умирал
    трейсбеком с кодом 1, неотличимым от доказанной поломки. Нечитаемый
    или неразбираемый файл корпуса — сломанная ОБСТАНОВКА, значит «судить
    нечем» (2) и метка `environment`, а не `corpus`: усечённый корпус в CI
    штатен и прощается, а битый файл — нет.
    """
    path = tmp_path / "determinism_baseline.json"
    path.write_text(json.dumps(BASE, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(det, "BASELINE", path)
    graph = tmp_path / "aaaaaaaa.json"
    if payload is not None:
        graph.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(det.corpus, "corpus_paths",
                        lambda include_storage=True: {"aaaaaaaa": graph})
    monkeypatch.setattr(det, "measure", lambda *a: pytest.fail(
        "замер начался, хотя отпечаток входа снять не удалось"))
    monkeypatch.setattr(sys, "argv",
                        ["layout_determinism.py", "--check", "--runs", "6"])

    assert det.main() == 2
    out = capsys.readouterr().out
    assert match in out
    assert out.strip().endswith(det.MARK_UNJUDGED_OTHER)

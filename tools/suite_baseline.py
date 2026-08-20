# -*- coding: utf-8 -*-
"""suite_baseline.py — базовая линия набора тестов: что падает СЕГОДНЯ (пункт 0.3).

Зачем. Набор красный не весь: часть тестов падает своими причинами, не связанными
с текущей работой. Пока этот список не зафиксирован, любой рефакторинг получает
чужие падения на свой счёт. Поэтому список красных лежит в git отдельным файлом
(`tools/bench/suite_baseline.json`), а гейт проверяет не «ноль красных», а
«ни одного НОВОГО красного».

Контракт как у остальных стендов проекта (`edit_bench.py`, `layout_bench.py`):
    python -X utf8 tools/suite_baseline.py --check           # сверка, exit 1 при регрессии
    python -X utf8 tools/suite_baseline.py --write-baseline  # пересъём базы (Д6: отдельный коммит)

Что считается ОПРОВЕРГНУТЫМ (`--check` → exit 1):
    * НОВЫЙ красный — тест, который в базе зелёный, а сейчас упал;
    * усыхание набора — тесты пропали у файлов, которые в диффе не менялись
      (`importorskip` без установленной зависимости, `collect_ignore`,
      сломанный `conftest`). Считается по КАРТЕ ФАЙЛОВ из базы, а не одним
      числом (пункт 1-27): одно число не отличало тихую пропажу от штатного
      отката пункта, и `git revert` по тегу красил гейт законным возвратом,
      то есть тег переставал быть точкой возврата (`PROTOCOL §6`). Файл,
      который правили, и файл, снятый вместе с записью о нём в git, — это
      не усыхание: их видно в диффе и судит ревизор, а стенд их печатает
      и вычитает из счёта. В карту идёт git-видимая часть сбора — без
      параметров по корпусу, которого нет в git (пункт 0.3y): иначе счёт
      зависел бы от числа диаграмм в локальном `storage/`;
    * массовое «позеленение» (> `MAX_FIXED`) и рост `skipped` на том же
      прогоне, где что-то позеленело (красный превратили в skip).
Единичный позеленевший тест провалом НЕ считается — печатается и требует
пересъёма базы.

Что считается «СУДИТЬ НЕЧЕМ» (`--check` → exit 2, пункт 1-30):
    * прогон не состоялся — сбор pytest сломан (это пункт 0.0, а не база),
      pytest вернул код вне {0, 1}, итоговой строки нет, или разобранных
      идентификаторов меньше, чем красных в счётчиках. Убитый прогон
      (`os._exit`, access violation §24.6) даёт ПУСТОЙ список красных,
      то есть без этих проверок читается как «всё починилось»;
    * git не ответил про исчезнувшие файлы — отличить откат пункта от тихой
      пропажи нечем (`shrinkage()`).
    * **прогон ЗАВИС** — pytest не вернулся за `RUN_TIMEOUT` (сбор — за
      `COLLECT_TIMEOUT`) и убит стендом (пункт 1-47). Последняя строка вывода
      в этом случае — `unjudged=hang`; на чём повис, называет дамп
      `faulthandler` в хвосте.
До 1-30 третьего исхода у `--check` не было вовсе: путь чтения умел только
«доказано/опровергнуто» и на сломанной обстановке отдавал ту же единицу, что
на доказанной регрессии, — вызывающий не мог отличить «чини код» от «чини
обстановку».

⛔ **Четвёртый отказ прибора: прогон не возвращается ВООБЩЕ (пункт 1-47).**
1-44 научил стенд честной полярности для СОСТОЯВШЕГОСЯ и для ОБОРВАННОГО
прогона, но зависший не даёт кода возврата вовсе, и сессия сидит без вердикта
неограниченно долго. Таймаут в `_pytest` стоял и до пункта (1800 с), только
не был перехвачен: `TimeoutExpired` улетал трейсбеком, а трейсбек — это
exit 1, то есть «в наборе новый красный» (замер §116.3). Теперь стенд
возвращается сам, отдаёт 2 «судить нечем» с меткой `unjudged=hang` и НАЗЫВАЕТ
кадр — тем же `faulthandler`, которым пункт 1-43 назвал упавший тест
при крахе, только взведённым по времени (`faulthandler_timeout`).

⛔ **Порядок причин (пункт GATE-8).** Прогон не состоялся (`run_rc` вне
`{0, 1}`, нет итоговой строки, счётчики не сходятся с числом разобранных id)
— это КОРОТКОЕ ЗАМЫКАНИЕ: `--check` отдаёт 2 и не сравнивает состав вовсе.
Приоритет «доказанный регресс сильнее неполноты» (1-25) остаётся в силе для
СОСТОЯВШЕГОСЯ прогона — там неполнота приходит от состава (молчащий git), а
красные разобраны и наблюдаемы. У мёртвого прогона наблюдений нет: прежние
красные не разобраны, поэтому выглядят «позеленевшими», и приоритет пропускал
АРТЕФАКТ ОБРЫВА вперёд честного «судить нечем» (замер §101а — крах
`0xC0000005` давал `[ПРОВАЛ] позеленело сразу 22 тестов` и код 1, который
прочли как красный гейт и сняли `revert`-ом чужую работу).

У `--write-baseline` ТРИ исхода с пункта 1-28:
    * ОТКАЗ (exit 1) — замер годен и говорит, что пересъём узаконил бы
      регрессию: множество красных ВЫРОСЛО против текущей базы (пункт 1-26)
      или набор тихо потерял тесты, то есть уехал бы вниз ПОЛ (пункт 1-28).
      Пересъём идёт отдельным коммитом (так требует Д6), поэтому красный флаг
      №4 протокола («эталон изменён тем же коммитом, что и код») его не видит:
      пока сверка жила только в `--check`, пересъём легализовал и новый красный,
      и потерянные тесты;
    * СУДИТЬ НЕЧЕМ (exit 2) — мерить было нечем: сбор сломан, прогон убит,
      вывод неполон (счётчики против числа разобранных id). Не то же самое,
      что отказ: тут чинят обстановку, а не код.
Законны: красные ушли, состав тот же, потеря видна в диффе (откат пункта).
Первый снимок (базы в дереве нет) сверять не с чем — он проходит, и стенд
об этом говорит.

Вердикты обоих путей печатаются в **stdout** — как у `layout_determinism.py`
и `lint_gate.py` (пункт 1-30: форма у трёх стендов была одна, а поток разный,
и вывод пересъёма разъезжался с выводом сверки в одном и том же логе).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import corpus                                      # noqa: E402

BASELINE = REPO / "tools" / "bench" / "suite_baseline.json"

# ⛔ **Третий исход прогона (пункт 1-47): он может не вернуться ВООБЩЕ.**
# Замер архитектора 2026-08-20: прогон шёл 22 минуты при обычных ~5.5, CPU
# процесса 18.1 с за всё это время, прирост за контрольные 40 с — РОВНО 0.00 с;
# процесс не считал, а стоял. Снаружи такое не читается ничем: захват копится
# до конца прогона, файл вывода 0 байт, — значит таймаут обязан жить ВНУТРИ
# стенда, а не в терминале того, кто его запустил.
#
# Таймера два, и они про разное:
#   TEST_TIMEOUT — окно ОДНОГО теста в дочернем pytest, сторожит его штатный
#     `faulthandler_timeout` самого pytest. По срабатыванию он вываливает стеки
#     всех нитей с файлом, строкой и именем тест-функции — тот же механизм,
#     которым назван упавший тест при КРАХЕ (пункт 1-43), — и прогон НЕ рвёт.
#     Поэтому ложное срабатывание стоит только лишних строк в выводе, а не
#     убитого годного прогона: медленный, но живой тест печатает дамп и идёт
#     дальше (замер §116.2).
#   RUN_TIMEOUT / COLLECT_TIMEOUT — потолок на весь дочерний вызов. Возвращает
#     управление именно он; имя к этому моменту уже лежит в захвате.
# ⛔ Порядок TEST_TIMEOUT < RUN_TIMEOUT обязателен, иначе стенд вернётся
# раньше, чем дамп успеет назвать кадр (заперто тестом).
#
# Числа (§116.1, §116.8). Набор — 3036 собранных. Здоровый прогон стенда
# целиком (прогон ПЛЮС сбор) — 385 с; два оборванных крахом замера входа дошли
# до 97 % за 304 с и 265 с; соседние сессии на меньшем наборе видели 311–404 с.
# RUN_TIMEOUT взят больше ДВОЙНОГО к худшему наблюдению и меньше потолка
# задания CI (`timeout-minutes: 30` на ВСЕ шаги): слишком короткий потолок
# убивал бы годные прогоны, а это ложь в обратную сторону.
# TEST_TIMEOUT: один тест из 3036 длиной 120 с — это 40 % всего прогона,
# то есть уже не медленный тест, а стоящий.
TEST_TIMEOUT = 120
RUN_TIMEOUT = 900
COLLECT_TIMEOUT = 300

PYTEST_RUN = ["-q", "--tb=no", "-rEf", "-p", "no:cacheprovider",
              "-o", f"faulthandler_timeout={TEST_TIMEOUT}"]
PYTEST_COLLECT = ["-q", "--collect-only", "-p", "no:cacheprovider"]

# Прогон не вернулся вовсе. Настоящий процесс таким кодом не отвечает: на
# Windows код возврата беззнаковый, на POSIX отрицательный — это номер сигнала
# (1…64). Значение вне обоих множеств, поэтому спутать его не с чем.
RC_HUNG = -1000

# Последняя строка вывода при exit 2 — ЧЕМ именно судить нечем. Та же идиома,
# что у `layout_determinism.py` (`unjudged=corpus` / `unjudged=environment`),
# метка ASCII: её грепает bash на windows-раннере.
# ⛔ Почему метка, а не ЧЕТВЁРТЫЙ код возврата. Действие вызывающего у краха и
# у зависания одно и то же — «перегони» (`PROTOCOL §Гейты`), а контракт
# «0 доказано · 1 опровергнуто · 2 судить нечем» общий у всех четырёх стендов
# дороги (`PROTOCOL §5`); четвёртый код пришлось бы вписывать в PROTOCOL,
# который исполнителю закрыт красным флагом №5, и до тех пор сессия, увидев
# код вне {0,1,2}, не знала бы, что делать. Разделять надо ПОПУЛЯЦИИ — крах
# это пункт 1-46, зависание 1-47, — и ровно это метка и даёт: она отличима
# грепом и в логе CI, и локально, а полярность остаётся честной.
MARK_UNJUDGED_HANG = "unjudged=hang"

# Прогон вправе вернуть только «всё зелено» (0) или «есть упавшие» (1). 2 —
# прерван, 3 — внутренняя ошибка, 4 — ошибка вызова, 5 — не собрано ни одного
# теста, всё прочее (77 от os._exit, 0xC0000005 от access violation) — смерть
# процесса. Всё это раньше читалось как «красных нет».
RUN_RC_OK = (0, 1)
# Позеленело больше этого — не починка, а обрезанный прогон: настоящая починка
# такого объёма обязана пересъёмкой базы объяснить себя (Д6).
MAX_FIXED = 10

# Допуск на ТИХУЮ потерю тестов — у файлов, которые в диффе не менялись.
# Замер 2026-08-18 (пункт 0.3y, MEASUREMENTS §38): git-видимая часть — 855
# и здесь, и в CI, разница 0; 5 — запас на дребезг окружения, и он вчетверо
# уже 22 тестов раскладки, которые молча уходят без shapely (855 → 833).
# ⛔ Расширять этот допуск, чтобы «пропустить откат пункта», нельзя: пункты
# дороги приносят по 5–53 теста, и запас, перекрывающий откат, ослепил бы
# гейт ровно на ту величину (пункт 1-27). Откат считается отдельно — по
# карте файлов, см. shrinkage().
# До 0.3y запас был 18 и включал в себя корпус (пункт 0.8): пол ехал вверх от
# каждой новой диаграммы в storage/, и к 0.3x от него оставалось 2 теста.
FLOOR_MARGIN = 5

# Три исхода, а не два (`PROTOCOL §5`). Пункт 1-28 развёл их на пути ЗАПИСИ,
# пункт 1-30 — на пути ЧТЕНИЯ: до него «судить нечем» (сломанный сбор, убитый
# прогон, оборванный хвост, молчащий git) и «опровергнуто» (новый красный,
# усохший набор) отдавали одинаковую единицу, и вызывающий не мог отличить
# «чини обстановку» от «чини код». На записи 1 читается как ОТКАЗ.
EXIT_REFUTED = 1
EXIT_UNJUDGED = 2

# Нежадно до « - » (после него причина) — идентификатор может содержать
# пробелы и кириллицу: в storage/ уже лежит «Новая папка», и параметр теста
# по корпусу приезжает в id как есть.
_RED = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - |\s*$)")
_TOTAL = re.compile(r"(\d+) (failed|passed|skipped|errors|error|xfailed|xpassed)")
_COLLECTED = re.compile(r"^(\d+) tests? collected")
# Хвостовой параметр идентификатора: `…::test_x[6e7144d5]`, у многопараметрных —
# `…[6e7144d5-case]`. uid8 шестнадцатеричный, дефиса внутри быть не может.
_PARAM_TAIL = re.compile(r"\[([^\[\]]+)\]\s*$")
# Строка сбора `--collect-only -q`: `tests/ui/test_x.py::TestY::test_z[param]`.
# Путь нежадно до первого `::` — дальше в идентификаторе бывает что угодно.
_TEST_ID = re.compile(r"^(.+?\.py)::")


def _pytest(args: list[str], timeout: float | None = None) -> tuple[str, int]:
    # -X utf8 обязателен: без него режим кодировки решает консоль, а результат
    # набора от неё зависит. Замерено 2026-08-17: test_refactoring.py:697
    # читает файл с кириллицей через read_text() без encoding — под cp1251
    # это UnicodeDecodeError и красный тест, под UTF-8 тест зелёный. База
    # должна быть одна и та же на любой машине, поэтому режим задаём здесь.
    #
    # PYTEST_ADDOPTS у родителя выпалывается: `-x` или `-k` из окружения
    # обрезали бы прогон, а обрезанный прогон — это ложное «позеленело».
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "pytest", *args],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUN_TIMEOUT if timeout is None else timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        # ⛔ Пункт 1-47. Таймаут здесь стоял и раньше (1800 с), но НЕ был
        # перехвачен: `TimeoutExpired` улетал наружу трейсбеком, и стенд
        # отдавал код 1 — «в наборе новый красный». Замерено 2026-08-20
        # (§116.3): ровно та ложь полярностью, которую пункт 1-44 снял
        # у ОБОРВАННОГО прогона, только у ЗАВИСШЕГО. Дочерний процесс убит,
        # захваченное к этому моменту — всё, что есть, и в нём лежит дамп
        # `faulthandler` с именем повисшего теста.
        # `TimeoutExpired` отдаёт захват как `bytes | str` (у `run()` он не
        # знает про `text=True`), поэтому склейка идёт через явную проверку.
        captured = [exc.stdout, exc.stderr]
        return "".join(
            part.decode("utf-8", "replace") if isinstance(part, bytes) else (part or "")
            for part in captured
        ), RC_HUNG
    return (proc.stdout or "") + (proc.stderr or ""), proc.returncode


def parse_red(text: str) -> set[str]:
    """Идентификаторы красных из хвоста `-rEf` (FAILED/ERROR ...)."""
    return {m.group(1) for line in text.splitlines() if (m := _RED.match(line))}


def parse_totals(text: str) -> dict[str, int]:
    """Счётчики из итоговой строки pytest («38 failed, 661 passed, ...»).

    ⛔ Берётся последняя РАЗБИРАЕМАЯ строка, а не последняя похожая (пункт
    1-47). С этого пункта в захвате живут дампы `faulthandler`, и кадр вида
    `File "tests/ui/x.py", line 295 in test_failed_saved_graph_warns` подходит
    под «есть " in " и есть "failed"», а счётчиков не несёт: слепой `tail[-1]`
    отдал бы пустой словарь, то есть «нет итоговой строки» — ложное «судить
    нечем» на ЗДОРОВОМ прогоне. Имён с `failed`/`passed` в наборе 35
    (греп 2026-08-20, §116.4), плюс файл `test_persist_failed_attempt.py`.
    """
    for line in reversed(text.splitlines()):
        if " in " not in line or ("passed" not in line and "failed" not in line):
            continue
        totals: dict[str, int] = {}
        for count, kind in _TOTAL.findall(line):
            totals["errors" if kind == "error" else kind] = int(count)
        if totals:
            return totals
    return {}


def parse_collected(text: str) -> int:
    """Число собранных тестов из хвоста `--collect-only -q`."""
    for line in text.splitlines():
        if m := _COLLECTED.match(line.strip()):
            return int(m.group(1))
    return -1


def local_corpus_uids() -> set[str]:
    """uid8 корпуса, которых нет в git: их параметры видит только эта машина."""
    return set(corpus.corpus_paths()) - set(corpus.corpus_paths(include_storage=False))


def split_collected(collect_text: str, local_uids: set[str]) -> tuple[dict[str, int], int]:
    """Разбор `--collect-only -q`: (git-видимые тесты по файлам, параметров по корпусу вне git).

    Набор обязан считаться одинаково на машине разработки и на раннере, иначе
    счёт ловит не потерю тестов, а число диаграмм в `storage/`. Поэтому та
    часть сбора, которой на чистом дереве не бывает, в карту файлов не идёт
    и считается отдельным числом.
    """
    per_file: dict[str, int] = {}
    local = 0
    for raw in collect_text.splitlines():
        line = raw.rstrip()
        m = _TEST_ID.match(line)
        if not m:
            continue
        tail = _PARAM_TAIL.search(line)
        if local_uids and tail and any(part in local_uids for part in tail.group(1).split("-")):
            local += 1
        else:
            per_file[m.group(1)] = per_file.get(m.group(1), 0) + 1
    return per_file, local


def count_local_corpus(collect_text: str, local_uids: set[str] | None = None) -> int:
    """Сколько собранных тестов параметризованы корпусом вне git."""
    uids = local_corpus_uids() if local_uids is None else local_uids
    return split_collected(collect_text, uids)[1]


def file_digest(path: Path) -> str:
    """Отпечаток файла с нормализованными переводами строк.

    Нормализация обязательна: у машины разработки checkout с CRLF, у раннера
    с LF. Без неё каждый файл выглядел бы изменённым, и пол ослеп бы в CI
    целиком — «изменённому» файлу потеря тестов прощается.
    """
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]


def tracked_by_git(paths: set[str]) -> tuple[set[str], str]:
    """Какие из исчезнувших файлов git всё ещё числит за деревом (+ ошибка git).

    Индекс — ответ самого git на вопрос «этот файл ещё часть дерева?».
    `git revert` пункта снимает файл вместе с записью о нём, а стёртый,
    переименованный или забытый мимо git в индексе остаётся: первое — откат,
    второе — тихая пропажа. Git не ответил — судить об исчезнувших файлах
    нечем, и `shrinkage()` уводит их в отдельную корзину «судить нечем»
    (пункт 1-30); возвращаемое множество в этом случае не значит ничего.
    """
    if not paths:
        return set(), ""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--", *sorted(paths)],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:                  # git не установлен или недоступен
        return set(paths), f"git не запустился: {exc}"
    if proc.returncode != 0:
        return set(paths), (proc.stderr or "").strip() or f"git ls-files вернул {proc.returncode}"
    return {p for p in proc.stdout.split("\0") if p}, ""


def shrinkage(base: dict, per_file: dict[str, int]
              ) -> tuple[dict[str, int], dict[str, int], dict[str, int], str]:
    """Разложить потерю тестов на тихую, объяснённую диффом и неподсудную.

    Гейт умеет судить ровно об одном: файл байт-в-байт тот же, что в базе,
    а тестов из него выходит меньше. В диффе такого не видно ничем — так
    уходят `importorskip` без установленной зависимости, `collect_ignore`,
    сломанный `conftest`, потерянная зависимость окружения.

    Всё остальное в диффе видно, и судит это ревизор, а не пол набора:
    файл правили (отпечаток разошёлся) или файл сняли вместе с записью о нём
    в git. Иначе штатный откат пункта — `git revert` по тегу, `PROTOCOL §6` —
    красил гейт законным возвратом: пункты дороги приносят по 5–53 теста
    при допуске 5 (пункт 1-27).

    ⛔ Третья корзина — «судить нечем» (пункт 1-30). Исчезнувший файл судится
    ТОЛЬКО ответом git, и если git не ответил, различить откат пункта и тихую
    пропажу нечем. До 1-30 такие файлы уходили в тихую потерю (осторожная
    сторона) и стенд ОТКАЗЫВАЛ — то есть на сломанном git гейт врал красным
    про код, хотя сломана была обстановка. Файлы, которые на месте, судятся
    отпечатком и молчания git не замечают — их вердикт остаётся в силе.

    Возвращает ({тихо потеряно}, {потеряно явно}, {судить нечем}, ошибка git).
    """
    base_per: dict[str, list] = base["per_file"]
    absent = {path for path in base_per if not (REPO / path).exists()}
    tracked, git_error = tracked_by_git(absent)

    silent: dict[str, int] = {}
    explained: dict[str, int] = {}
    unknown: dict[str, int] = {}
    for path, (was, digest) in base_per.items():
        delta = was - per_file.get(path, 0)
        if delta <= 0:
            continue
        if path in absent:
            if git_error:                       # об исчезнувших судить нечем
                unknown[path] = delta
            else:
                # снят вместе с записью в git — откат; остался в индексе — пропажа
                (silent if path in tracked else explained)[path] = delta
        else:
            (silent if file_digest(REPO / path) == digest else explained)[path] = delta
    return silent, explained, unknown, git_error


def floor_problems(base: dict, per_file: dict[str, int]
                   ) -> tuple[list[str], list[str], list[str]]:
    """(причины провала по усыханию, что сказать при зелёном, «судить нечем»).

    Пол один на троих: остальные потребители — сторож сбора
    `tests/test_collection_clean.py` и путь записи `write_blocked()`.
    Считается здесь, чтобы они не разъехались.
    """
    if "per_file" not in base:                     # база снята до пункта 1-27
        git_visible = sum(per_file.values())
        if git_visible < base["min_collected"]:
            return ([
                f"набор усох: в git-видимой части {git_visible} < {base['min_collected']} "
                "(база без карты файлов — карта появится с ближайшим пересъёмом)"
            ], [], [])
        return ([], [], [])

    silent, explained, unknown, git_error = shrinkage(base, per_file)
    problems: list[str] = []
    unjudged: list[str] = []

    def _lines(where: dict[str, int]) -> str:
        return "\n".join(
            f"    {path}: {base['per_file'][path][0]} → {per_file.get(path, 0)}"
            for path in sorted(where)
        )

    if unknown:
        unjudged.append(
            f"git не ответил про исчезнувшие файлы ({git_error}) — откат пункта "
            f"от тихой пропажи отличить нечем, {len(unknown)} файлов "
            f"(−{sum(unknown.values())}) остались без вердикта:\n{_lines(unknown)}"
        )
    lost = sum(silent.values())
    if lost > FLOOR_MARGIN:
        problems.append(
            f"набор усох: тихо потеряно {lost} тестов (допуск {FLOOR_MARGIN}) — "
            f"эти файлы в диффе не менялись, а тестов из них выходит меньше:\n{_lines(silent)}"
        )
    notes: list[str] = []
    if silent and not problems:
        notes.append(f"[в допуске] тихо потеряно {lost} из {FLOOR_MARGIN}:\n{_lines(silent)}")
    if explained:
        notes.append(
            f"[в диффе] {len(explained)} файлов эталона отдали меньше тестов "
            f"(−{sum(explained.values())}) — файл правили или сняли вместе с записью в git:\n"
            f"{_lines(explained)}"
        )
    return problems, notes, unjudged


# Маркеры аварийного блока `faulthandler`. Первые два — смерть процесса
# (пункт 1-43), третий — таймаут одного теста: `Timeout (0:02:00)!` (пункт
# 1-47). Механизм один и тот же, поэтому и окно у них одно.
CRASH_MARKERS = ("Windows fatal exception", "Fatal Python error", "Timeout (")


def crash_excerpt(text: str, tail: int = 25) -> str:
    """Хвост прогона, но с НАЧАЛА фатального блока, если он есть.

    ⛔ Слепой хвост в 25 строк УПАВШИЙ ТЕСТ НЕ НАЗЫВАЕТ, и это замерено
    (пункт 1.x17, §102): блок `faulthandler` длиннее хвоста — первые его
    строки несут файл, номер строки и имя тест-функции, а следом идут
    два десятка строк внутренностей `pytest`, которые хвост и показывает.
    Прежняя запись «строки Windows fatal exception с именем теста в выводе
    нет» (§101) была выводом ИЗ ЭТОГО ОБРЕЗАНИЯ, а не фактом: строка есть,
    и краш-прогон 1.x17 назвал по ней `test_unsaved_question.py:223`.

    Поэтому при обрыве печатается не конец текста, а окно ОТ маркера краха.

    ⛔ Маркер берётся ПОСЛЕДНИЙ (пункт 1-47): у зависшего прогона дампов может
    быть несколько — медленный, но живой тест тоже печатает свой и идёт дальше.
    Интересен всегда последний: у зависания это повисший тест, у краха — сам
    крах (он и так последнее, что процесс успевает написать).
    """
    lines = text.splitlines()
    marks = [i for i, line in enumerate(lines)
             if any(mark in line for mark in CRASH_MARKERS)]
    if marks:
        i = marks[-1]
        return "\n".join(lines[max(0, i - 2):i + tail])
    return "\n".join(lines[-tail:])


def compare(base_red: set[str], cur_red: set[str]) -> tuple[list[str], list[str]]:
    """(новые красные — это регрессия, позеленевшие — повод пересъёмки базы)."""
    return sorted(cur_red - base_red), sorted(base_red - cur_red)


def verdict(red: set[str], totals: dict[str, int], run_rc: int) -> list[str]:
    """Причины «СУДИТЬ НЕЧЕМ» у самого прогона (пустой список = прогон состоялся).

    Порядок проверок важен: сначала «прогон вообще состоялся», потом уже
    сравнение с базой. Убитый прогон даёт пустой список красных, и без
    этих проверок он читается как «всё починилось».

    ⚠ Это НЕ регрессия, а сломанная обстановка: с пункта 1-30 обе стороны
    (`--check` и пересъём) отдают на этих причинах 2, а не 1. Здесь чинят
    обстановку, а в `compare()`/`floor_problems()` — код.

    ⛔ С пункта GATE-8 непустой ответ этой функции КОРОТИТ `--check`: сравнения
    состава за ним не идут вовсе. Все три причины говорят об одном — замер
    негоден, — а на негодном замере «позеленело» и «усохло» это не наблюдения,
    а следы обрыва. Замерено, что мало одной ветки по `run_rc`: при штатном
    коде 1 и оборванном хвосте (счётчики против числа разобранных id) стенд
    печатал ту же ложь — `[ПРОВАЛ] позеленело сразу 20 тестов` (§101б).

    Состав набора считает `floor_problems` — у него три потребителя.
    """
    fail: list[str] = []

    if run_rc == RC_HUNG:
        fail.append(
            f"прогон ЗАВИС: pytest не вернулся за {RUN_TIMEOUT} с и убит стендом. "
            f"На чём повис — в дампе `faulthandler` ниже (окно одного теста "
            f"{TEST_TIMEOUT} с). Если дампа ниже НЕТ — зависание пришлось не "
            "на тест: на сбор, на финал сессии или на сам интерпретатор"
        )
    elif run_rc not in RUN_RC_OK:
        fail.append(
            f"прогон не завершился штатно: pytest вернул {run_rc} "
            f"(штатные — {RUN_RC_OK}: 0 всё зелено, 1 есть упавшие). "
            "Обрыв, смерть процесса или ошибка вызова — сравнивать не с чем"
        )
    if not totals:
        fail.append("не разобрал итоговую строку pytest — вывод оборван, счётчиков нет")
    else:
        counted = totals.get("failed", 0) + totals.get("errors", 0)
        if counted != len(red):
            fail.append(
                f"вывод неполон: в итоговой строке {counted} красных, "
                f"а идентификаторов разобрано {len(red)}"
            )
    return fail


def write_blocked(base: dict | None, red: set[str], totals: dict[str, int],
                  per_file: dict[str, int]) -> tuple[list[str], list[str]]:
    """-> (причины «судить нечем», причины отказа). Обе пустые = пересъём законен.

    Пересъём — единственный путь, которым база вообще меняется, и по Д6 он идёт
    ОТДЕЛЬНЫМ коммитом; поэтому красный флаг №4 протокола («эталон изменён тем
    же коммитом, что и код») его не видит. До пункта 1-26 сверка с базой жила
    только в `--check`, а `--write-baseline` перезаписывал `red` тем, что красно
    сейчас, — то есть любой Д6-пересъём легализовал выросшее множество красных.

    Пункт 1-28 закрыл вторую половину: пересъём не смотрел на ПОЛ набора.
    Замер 2026-08-19 — `collect_ignore` каталога унёс 147 тестов, стенд отдал
    exit 0 и записал пол 952 → 811 с картой 89 → 64 файлов; красные при этом
    не тронулись, поэтому сверка 1-26 промолчала, а следующий `--check` на
    здоровом дереве был зелёным: потеря стала нормой. Судим тем же
    `floor_problems`, что и `--check`, — третий потребитель одного пола.

    Три исхода (`PROTOCOL §5`), а не два:
    2 — СУДИТЬ НЕЧЕМ: вывод неполон, сверять состав с базой не на чем, либо
        git не ответил про исчезнувшие файлы (пункт 1-30);
    1 — ОТКАЗ: замер годен и говорит, что пересъём узаконил бы регрессию —
        новый красный или тихо потерянные тесты;
    0 — законно. Законны: красные ушли (починили), состав тот же (пересъём ради
        `collected` после новых зелёных), потеря видна в диффе (откат пункта),
        и первый снимок — сверять не с чем.
    """
    unjudged: list[str] = []
    refused: list[str] = []

    # Сверка на неполном списке ничего не значит: оборванный хвост даёт красных
    # меньше, чем их было, и рост состава становится невидим.
    counted = totals.get("failed", 0) + totals.get("errors", 0)
    if counted != len(red):
        unjudged.append(
            f"вывод неполон: в итоговой строке {counted} красных, а идентификаторов "
            f"разобрано {len(red)} — сверять состав с базой не на чем"
        )
    if base is None:
        return unjudged, refused

    new, _ = compare(set(base["red"]), red)
    if new:
        refused.append(
            f"множество красных выросло против базы ({base['recorded']}): "
            f"{len(base['red'])} → {len(red)}, новых {len(new)}:\n"
            + "\n".join(f"    + {nid}" for nid in new)
            + "\n  Новый красный чинят или откатывают — пересъём его не легализует."
        )
    floor, _notes, floor_unjudged = floor_problems(base, per_file)
    for problem in floor:
        refused.append(problem + "\n  Пересъём записал бы эту потерю нормой.")
    unjudged += floor_unjudged
    return unjudged, refused


def skip_conversion_suspected(base: dict, totals: dict[str, int], fixed: list[str]) -> bool:
    """Красный превратили в skip — тест «позеленел», не будучи починенным.

    Порознь оба признака законны (тест починили; появился новый skip), вместе
    на одном прогоне — почти всегда конверсия. Разбирается пересъёмкой базы.
    """
    grew = totals.get("skipped", 0) - base["totals"].get("skipped", 0)
    return bool(fixed) and grew > 0


def _load_baseline() -> dict:
    if not BASELINE.exists():
        sys.exit(f"нет базы {BASELINE.relative_to(REPO)} — сначала --write-baseline")
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _measure() -> tuple[set[str], dict[str, int], int, dict[str, int], int, str, int]:
    """Замер набора. Мерить оказалось нечем — «судить нечем» на ОБОИХ путях.

    До пункта 1-30 у `--check` тут была единица: считалось, что шаг CI
    1 и 2 не различает. Он их и не различает — но обе не нулевые, значит
    сборка краснеет одинаково, а вызывающий человек получает диагноз
    «чини обстановку (это пункт 0.0), а не код».
    """
    run_text, run_rc = _pytest(PYTEST_RUN)
    collect_text, collect_rc = _pytest(PYTEST_COLLECT, COLLECT_TIMEOUT)
    if collect_rc == RC_HUNG:
        # Отдельная ветка, а не «сбор сломан»: у зависшего сбора код возврата
        # тоже не нулевой, и общая ветка обвинила бы пункт 0.0 — то есть два
        # судьи одного стенда на одном условии сказали бы разное (`PROTOCOL
        # §Гейты`, разбор `lint_gate`). Имени тут не бывает: окно теста
        # сторожит прогон, а у сбора тестов ещё нет.
        print(f"[СУДИТЬ НЕЧЕМ] СБОР ЗАВИС: pytest --collect-only не вернулся "
              f"за {COLLECT_TIMEOUT} с и убит стендом")
        print("\nхвост сбора:\n" + crash_excerpt(collect_text))
        print(MARK_UNJUDGED_HANG)
        sys.exit(EXIT_UNJUDGED)
    if collect_rc != 0:
        print(f"[СУДИТЬ НЕЧЕМ] сбор pytest сломан (exit {collect_rc}) — "
              f"это пункт 0.0, а не база")
        sys.exit(EXIT_UNJUDGED)
    collected = parse_collected(collect_text)
    per_file, local = split_collected(collect_text, local_corpus_uids())
    return parse_red(run_text), parse_totals(run_text), collected, per_file, local, run_text, run_rc


def cmd_write() -> int:
    red, totals, collected, per_file, local, run_text, run_rc = _measure()
    if run_rc == RC_HUNG:
        print(f"[СУДИТЬ НЕЧЕМ] прогон ЗАВИС — не вернулся за {RUN_TIMEOUT} с и убит "
              f"стендом; снимать базу с убитого прогона нельзя:\n"
              + crash_excerpt(run_text))
        print(MARK_UNJUDGED_HANG)
        return EXIT_UNJUDGED
    if run_rc not in RUN_RC_OK:
        print(f"[СУДИТЬ НЕЧЕМ] прогон вернул {run_rc} — снимать базу с оборванного "
              f"прогона нельзя:\n" + run_text[-2000:])
        return EXIT_UNJUDGED
    if not totals:
        print("[СУДИТЬ НЕЧЕМ] не разобрал итоговую строку pytest:\n" + run_text[-2000:])
        return EXIT_UNJUDGED
    base = _load_baseline() if BASELINE.exists() else None
    if base is None:
        print(f"базы {BASELINE.name} нет — первый снимок, сверять не с чем")
    unjudged, refused = write_blocked(base, red, totals, per_file)
    for msg in unjudged:
        print(f"[СУДИТЬ НЕЧЕМ] {msg}")
    for msg in refused:
        print(f"[ОТКАЗ] {msg}")
    if refused:                      # доказанная регрессия сильнее неполноты (1-25)
        return EXIT_REFUTED
    if unjudged:
        return EXIT_UNJUDGED
    git_visible = sum(per_file.values())
    BASELINE.write_text(
        json.dumps(
            {
                "recorded": datetime.date.today().isoformat(),
                "command": "python -m pytest " + " ".join(PYTEST_RUN),
                "platform": f"{sys.platform} · CPython {platform.python_version()}",
                "env": "requirements/api.txt + ui.txt + dev.txt + shapely==2.1.2",
                "totals": totals,
                "collected": collected,
                "corpus_local": local,
                "collected_git_visible": git_visible,
                "min_collected": git_visible - FLOOR_MARGIN,
                # {файл: [сколько git-видимых тестов, отпечаток файла]} — по этой
                # карте `--check` отличает тихую пропажу от видимой в диффе.
                "per_file": {
                    path: [count, file_digest(REPO / path)]
                    for path, count in sorted(per_file.items())
                },
                "red": sorted(red),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"база записана: {len(red)} красных, собрано {collected} "
          f"(git-видимых {git_visible} в {len(per_file)} файлах, корпус вне git {local}), {totals}")
    return 0


def cmd_check() -> int:
    base = _load_baseline()
    red, totals, collected, per_file, local, run_text, run_rc = _measure()
    git_visible = sum(per_file.values())

    print(f"сейчас:  собрано {collected} (git-видимых {git_visible} в {len(per_file)} файлах, "
          f"корпус вне git {local}), {totals}, pytest exit {run_rc}")
    print(f"база ({base['recorded']}, {base['platform']}): собрано {base['collected']}, "
          f"git-видимых {base.get('collected_git_visible', '?')} "
          f"в {len(base.get('per_file') or ())} файлах, {base['totals']}")

    # ⛔ Ветка обрыва стоит ПЕРВОЙ и КОРОТИТ вердикт (пункт GATE-8). Приоритет
    # «доказанная регрессия сильнее неполноты» (1-25) верен для СОСТОЯВШЕГОСЯ
    # прогона: там состав красных — наблюдение. У мёртвого прогона счётчиков
    # нет и красных не разобрано, поэтому «усохло» и «позеленело» становятся
    # АРТЕФАКТАМИ ОБРЫВА, и приоритет пропускает их вперёд честного «судить
    # нечем». Замер 2026-08-20 (§101а): крах `0xC0000005` печатал два верных
    # `[СУДИТЬ НЕЧЕМ]`, а следом `[ПРОВАЛ] позеленело сразу 22 тестов (порог
    # 10)` и отдавал 1 — код 1 прочли как красный гейт и сняли `revert`-ом
    # чужую работу. Сравнивать не с чем — значит не сравнивать вовсе, ни в ту
    # сторону, ни в другую: обстановку чинят и перемеряют.
    dead = verdict(red, totals, run_rc)
    if dead:
        for msg in dead:
            print(f"\n[СУДИТЬ НЕЧЕМ] {msg}")
        print("\nхвост прогона:\n" + crash_excerpt(run_text))
        if run_rc == RC_HUNG:
            print(MARK_UNJUDGED_HANG)
        return EXIT_UNJUDGED

    # Дальше прогон СОСТОЯЛСЯ, и приоритет 1-30/1-25 в силе: сломанный состав —
    # «судить нечем» (2), доказанная регрессия — «опровергнуто» (1), и обе
    # причины разом дают 1.
    new, fixed = compare(set(base["red"]), red)
    problems: list[str] = []
    floor, notes, unjudged = floor_problems(base, per_file)
    problems += floor
    for note in notes:
        print(f"\n{note}")
    if len(fixed) > MAX_FIXED:
        problems.append(
            f"позеленело сразу {len(fixed)} тестов (порог {MAX_FIXED}) — так выглядит "
            "обрезанный прогон; если починка настоящая, пересними базу и объясни её"
        )
    if skip_conversion_suspected(base, totals, fixed):
        problems.append(
            f"skipped вырос ({base['totals'].get('skipped', 0)} → {totals.get('skipped', 0)}) "
            "на том же прогоне, где что-то позеленело: похоже, красный тест превратили в skip, "
            "а не починили"
        )

    bad = bool(problems)
    for msg in unjudged:
        print(f"\n[СУДИТЬ НЕЧЕМ] {msg}")
    for msg in problems:
        print(f"\n[ПРОВАЛ] {msg}")
    if new:
        print(f"\n[ПРОВАЛ] новые красные ({len(new)}) — их не было в базе:")
        for nid in new:
            print(f"  + {nid}")
        bad = True
    if fixed:
        print(f"\n[позеленело] {len(fixed)} — пересними базу отдельным коммитом (--write-baseline):")
        for nid in fixed:
            print(f"  - {nid}")
    if bad:
        print("\nхвост прогона:\n" + crash_excerpt(run_text))
        return EXIT_REFUTED
    if unjudged:
        print("\nхвост прогона:\n" + crash_excerpt(run_text))
        return EXIT_UNJUDGED
    print("\n[OK] новых красных нет")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="сверить прогон с базой, exit 1 при регрессии")
    g.add_argument("--write-baseline", action="store_true", help="пересъём базы")
    args = ap.parse_args()
    return cmd_write() if args.write_baseline else cmd_check()


if __name__ == "__main__":
    sys.exit(main())

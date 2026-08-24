"""Доки против кода: числа и флаги, на которых строятся рассуждения.

Пункт 0.6 дороги рефакторинга. Дефект этого уровня юнитом на модуль не ловится:
док расходится с кодом молча, и следующий читатель принимает решение по ложному числу
(таймаут детекции в WORKER_TASKS.md был занижен втрое, `CLAUDE.md` объявлял OCR
выключенным при `enabled: true` в конфиге).

Здесь сверяются только те утверждения, которые уже врали. Разрастаться этому файлу
не нужно: остальной дрейф доков — Этап 13.

Раздел 4 добавлен пунктом 1-45: у документа про стенды есть свой вид лжи —
ДВЕ ЕГО СТРОКИ называют РАЗНЫЙ код возврата для ОДНОГО условия. Так `TESTING.md`
и жил с 1-30: тремя абзацами выше молчащий git числился кодом 2, а в таблице ниже
— «провалом», то есть единицей.
"""
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# 1. Таймауты Celery: таблицы WORKER_TASKS.md §4 против декораторов worker/tasks
# --------------------------------------------------------------------------- #

_DOC_SECTION = re.compile(r"^### 4\.\d+ (\S+)", re.M)
_DOC_FILE = re.compile(r"\*\*Файл:\*\* `([^`]+)`")
_DOC_NAME = re.compile(r"\*\*Celery name:\*\* `([^`]+)`")
_DOC_LIMIT = re.compile(r"^\| `(soft_time_limit|time_limit)` \| (\d+)", re.M)


def _doc_blocks():
    """[(task_name, py_file, celery_name, {limit: int}), ...] из §4 WORKER_TASKS.md."""
    text = (ROOT / "docs" / "WORKER_TASKS.md").read_text(encoding="utf-8")
    marks = list(_DOC_SECTION.finditer(text))
    assert marks, "в WORKER_TASKS.md не найдено ни одного раздела '### 4.N <task>'"
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        block = text[m.start():end]
        py = _DOC_FILE.search(block)
        name = _DOC_NAME.search(block)
        if not (py and name):
            continue
        limits = {k: int(v) for k, v in _DOC_LIMIT.findall(block)}
        out.append((m.group(1), py.group(1), name.group(1), limits))
    return out


def _code_limits(py_path: str, celery_name: str):
    """Значения time_limit/soft_time_limit из декоратора задачи `celery_name`."""
    src = (ROOT / py_path).read_text(encoding="utf-8")
    consts = dict(re.findall(r"^([A-Z][A-Z0-9_]*)\s*=\s*(\d+)", src, re.M))

    start = None
    for dec in re.finditer(r"@celery_app\.task\(", src):
        head = src[dec.start():src.index("\ndef ", dec.start())]
        if f'name="{celery_name}"' in head:
            start = head
            break
    assert start is not None, f"{py_path}: не найден декоратор задачи {celery_name}"

    limits = {}
    for key in ("time_limit", "soft_time_limit"):
        m = re.search(rf"[^_]\b{key}=(\w+)", start)
        if m:
            raw = m.group(1)
            limits[key] = int(raw) if raw.isdigit() else int(consts[raw])
    return limits


@pytest.mark.parametrize(
    "task,py_path,celery_name,doc_limits",
    [pytest.param(*b, id=b[0]) for b in _doc_blocks()],
)
def test_worker_tasks_doc_timeouts_match_code(task, py_path, celery_name, doc_limits):
    code_limits = _code_limits(py_path, celery_name)
    for key, doc_value in doc_limits.items():
        assert key in code_limits, f"{task}: в доке есть {key}, в декораторе нет"
        assert code_limits[key] == doc_value, (
            f"{task}: WORKER_TASKS.md обещает {key}={doc_value}, "
            f"а {py_path} ставит {code_limits[key]}"
        )


# --------------------------------------------------------------------------- #
# 2. «OCR по умолчанию выключен» против ocr.enabled боевого проекта
# --------------------------------------------------------------------------- #

# Доки, которые читаются ПЕРЕД кодом и потому обязаны совпадать с конфигом.
# `CLAUDE.md` и `docs/planning/` живут вне git (`.git/info/exclude`) — в чистом клоне
# и в CI их нет, там эти параметры пропускаются, а не краснеют.
_OCR_CLAIM_DOCS = (
    "CLAUDE.md",
    "DEPLOY_README.md",
    "docs/planning/ROADMAP_techdebt_and_agent_team.md",
)
_OCR_CLAIM = re.compile(
    r"OCR\s+по умолчанию\s+\*{0,2}(выключен|включён)|"
    r"OCR\s+\*{0,2}(выключен|включён)\*{0,2}\s+по умолчанию"
)


def _ocr_enabled_in_config() -> bool:
    cfg = yaml.safe_load(
        (ROOT / "configs/projects/thermohydraulics/thermohydraulics.yaml").read_text(
            encoding="utf-8"
        )
    )
    return bool(cfg["ocr"]["enabled"])


@pytest.mark.parametrize("doc", _OCR_CLAIM_DOCS)
def test_docs_do_not_contradict_ocr_flag(doc):
    path = ROOT / doc
    if not path.exists():
        pytest.skip(f"{doc} нет в этом клоне (вне git: .git/info/exclude)")
    enabled = _ocr_enabled_in_config()
    expected = "включён" if enabled else "выключен"
    text = path.read_text(encoding="utf-8")
    for m in _OCR_CLAIM.finditer(text):
        word = m.group(1) or m.group(2)
        line = text[: m.start()].count("\n") + 1
        assert word == expected, (
            f"{doc}:{line} — «OCR по умолчанию {word}», а "
            f"thermohydraulics.yaml → ocr.enabled: {str(enabled).lower()}"
        )


# --------------------------------------------------------------------------- #
# 3. napravlenie: исключение из node_mask и список классов классификатора
#    (пункт ВН1а; сам дефект — docs/NAPRAVLENIE.md §1)
# --------------------------------------------------------------------------- #

# Оба генератора node_mask обязаны исключать napravlenie: вернётся хоть один —
# бокс снова вырежется из pipe_mask и труба под ним пропадёт.
_NODE_MASK_GENERATORS = (
    "worker/tasks/segmentation.py",
    "app/api/validation.py",
)
_EXCLUDED_BLOCK = re.compile(r"excluded_names\s*=\s*\{(.*?)\}", re.S)


def _excluded_node_mask_names(py_path: str) -> set:
    """Имена категорий из `excluded_names = {...}`, с раскрытием строковых констант."""
    src = (ROOT / py_path).read_text(encoding="utf-8")
    m = _EXCLUDED_BLOCK.search(src)
    assert m, f"{py_path}: не найден блок excluded_names"
    consts = dict(re.findall(r'^([A-Z][A-Z0-9_]*)\s*=\s*"([^"]+)"', src, re.M))
    names = set()
    for token in (t.strip() for t in m.group(1).split(",")):
        if not token:
            continue
        if token.startswith(('"', "'")):
            names.add(token.strip("\"'"))
        elif token in consts:
            names.add(consts[token])
    return names


@pytest.mark.parametrize("py_path", _NODE_MASK_GENERATORS)
def test_node_mask_generators_exclude_napravlenie(py_path):
    names = _excluded_node_mask_names(py_path)
    assert {"truba", "annotation", "napravlenie"} <= names, (
        f"{py_path}: node_mask исключает {sorted(names)} — без 'napravlenie' бокс "
        f"стрелки вырезается из pipe_mask и труба под ним пропадает "
        f"(docs/NAPRAVLENIE.md §1)"
    )


def test_napravlenie_doc_classes_match_config():
    doc = (ROOT / "docs/NAPRAVLENIE.md").read_text(encoding="utf-8")
    m = re.search(r"^\s*classes:\s*\[([^\]]+)\]", doc, re.M)
    assert m, "docs/NAPRAVLENIE.md: не найдена строка `classes: [...]` в примере конфига"
    doc_classes = [c.strip() for c in m.group(1).split(",")]

    cfg = yaml.safe_load(
        (ROOT / "configs/projects/thermohydraulics/thermohydraulics.yaml").read_text(
            encoding="utf-8"
        )
    )
    cfg_classes = list(cfg["direction_classification"]["classes"])
    assert doc_classes == cfg_classes, (
        f"docs/NAPRAVLENIE.md обещает classes={doc_classes}, "
        f"а thermohydraulics.yaml → {cfg_classes}"
    )


# --------------------------------------------------------------------------- #
# 4. Полярность стендов: код возврата в доке против кода возврата в стенде
#    (пункт 1-45; дефект — две строки docs/TESTING.md про один и тот же исход)
# --------------------------------------------------------------------------- #

# Словарь вердиктов дороги: этими словами документ называет код, не цифрой.
# «Отказ» сюда не берётся намеренно — его строки и так несут `exit 1` цифрой,
# а корень слова живёт в «отказал/отказывает» и дал бы ложные попадания.
_VERDICT_WORDS = {"провал": 1, "судить нечем": 2}
# Цифрой код называется двумя способами: жирной ячейкой таблицы (`**2**`)
# и словами «код 2» / «exit 1».
_CODE_MARKS = (re.compile(r"\*\*([012])\*\*"),
               re.compile(r"(?:код|exit)\s*\**\s*([012])\b"))


def _code_claims(path: Path, condition: str):
    """[(номер строки, текст, {коды})] — что документ обещает про это условие.

    ⛔ Читаются только СТРОКИ ТАБЛИЦ. Проза тех же разделов пересказывает
    историю («печатали строку и отдавали **1**»), и её номера — не обещание,
    а рассказ о том, как было. Граница названа здесь, чтобы её видел
    наследник: противоречие, спрятанное в прозе, этот сторож не поймает.
    """
    where = re.compile(condition)
    out = []
    for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.lstrip().startswith("|") or not where.search(line):
            continue
        codes = {int(m.group(1)) for mark in _CODE_MARKS
                 for m in mark.finditer(line)}
        codes |= {code for word, code in _VERDICT_WORDS.items()
                  if word in line.lower()}
        if codes:
            out.append((num, line.strip(), codes))
    return out


def _code_of_silent_git(monkeypatch) -> int:
    """Базовая линия набора: файл исчез, а git про него не ответил."""
    from tools import suite_baseline as sb

    monkeypatch.setattr(sb, "tracked_by_git",
                        lambda paths: (set(paths), "git не запустился: зонд"))
    problems, _notes, unjudged = sb.floor_problems(
        {"per_file": {"tests/пропал.py": [7, "отпечаток"]}, "min_collected": 0},
        {})
    assert unjudged and not problems, (
        "молчащий git ушёл в провал по составу, а не в «судить нечем»")
    return sb.EXIT_UNJUDGED


def _code_of_a_bad_call(monkeypatch) -> int:
    """ПР1: команда собрана неверно — замер не начинался."""
    from tools import layout_determinism as det

    monkeypatch.setattr(det.corpus, "corpus_paths",
                        lambda include_storage=True: {"aaaaaaaa": "x"})
    monkeypatch.setattr(sys, "argv",
                        ["layout_determinism.py", "--check", "--runs", "1"])
    with pytest.raises(SystemExit) as exc:
        det.main()
    return exc.value.code


def _code_of_an_unmeasured_pair(monkeypatch) -> int:
    """ДН4: пара эталона не измерена — её нет на диске."""
    from tools import pair_bench

    base = {"rows": {"u1": {"graph": {}}}, "inputs": {"u1": {"graph": "вход"}}}
    return pair_bench.verdict({"u1": {}}, base, {"u1": {}})[0]


# (условие, документ, чем это условие названо в строках таблиц, чем меряется код)
_POLARITY_CLAIMS = (
    ("молчащий git", "docs/TESTING.md",
     r"git не ответил про (?:исчезнувшие|пропавшие) файлы", _code_of_silent_git),
    ("ошибка вызова ПР1", "docs/TESTING.md",
     r"`--runs` меньше двух|uid, которого нет в корпусе|неизвестный ключ",
     _code_of_a_bad_call),
    ("пара без вердикта у ДН4", "docs/TESTING.md",
     r"(?:пара|диаграммы) эталона (?:не измерена|нет на диске)",
     _code_of_an_unmeasured_pair),
)


@pytest.mark.parametrize("what,doc,condition,measure", _POLARITY_CLAIMS,
                         ids=[c[0] for c in _POLARITY_CLAIMS])
def test_one_document_names_one_code_for_one_condition(monkeypatch, what, doc,
                                                       condition, measure):
    """⛔ Дефект уровня документа, а не строки (пункт 1-45).

    Гейт читают глазами, и полярность его кода — такое же несущее число,
    как таймаут в `WORKER_TASKS.md`. Замер: `TESTING.md` с пункта 1-30 нёс
    про молчащий git ДВЕ строки — «**2**» в таблице «что валит гейт» и
    «провал» в таблице «что случилось с файлом», — то есть читатель получал
    ложь про полярность ровно там, где её только что чинили.

    Сверяются оба конца: строки документа между собой И с кодом, который
    стенд действительно отдаёт на этом условии.
    """
    claims = _code_claims(ROOT / doc, condition)
    assert claims, (
        f"{doc}: ни одна строка таблиц не говорит про «{what}» — условие "
        f"переписали, а сторож ослеп. Обнови условие в _POLARITY_CLAIMS")

    named = {code for _num, _text, codes in claims for code in codes}
    assert len(named) == 1, (
        f"{doc}: про «{what}» документ называет разные коды {sorted(named)}:\n"
        + "\n".join(f"    :{num} -> {sorted(codes)}  {text[:110]}"
                     for num, text, codes in claims))

    real = measure(monkeypatch)
    assert named == {real}, (
        f"{doc}: про «{what}» документ обещает код {named.pop()}, "
        f"а стенд отдаёт {real}:\n"
        + "\n".join(f"    :{num}  {text[:110]}" for num, text, _c in claims))

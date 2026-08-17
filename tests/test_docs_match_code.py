"""Доки против кода: числа и флаги, на которых строятся рассуждения.

Пункт 0.6 дороги рефакторинга. Дефект этого уровня юнитом на модуль не ловится:
док расходится с кодом молча, и следующий читатель принимает решение по ложному числу
(таймаут детекции в WORKER_TASKS.md был занижен втрое, `CLAUDE.md` объявлял OCR
выключенным при `enabled: true` в конфиге).

Здесь сверяются только те утверждения, которые уже врали. Разрастаться этому файлу
не нужно: остальной дрейф доков — Этап 13.
"""
import re
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

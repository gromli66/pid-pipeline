"""
text_clean.py — ЕДИНАЯ нормализация распознанного текста (П5).

Один нормализатор на весь проект: и авто-распознавание (pipeline_clean), и показ/
редактирование в UI зовут cleanup(). Приводит строку к единому виду:
  - html.unescape (&amp; &lt; &#176; -> символы);
  - убрать <math>...</math> целиком (обычно мусор Surya-VLM в текст-боксе);
  - <br> -> пробел; прочие теги (<b>,<i>,<u>,<sub>,<sup>...) -> убрать без пробела
    (чтобы не разрывать "T1" из <i>T</i><u>1</u>);
  - LaTeX ($...$, \\cmd{..}) и markdown-жирность (**, __);
  - Unicode NFKC (полноширинные/лигатуры/совместимые формы -> канон);
  - удаление управляющих/непечатаемых символов;
  - схлопывание пробелов и зацикленных токенов (артефакт VLM).

Единый шрифт/размер и отсутствие жирности/курсива/подчёркивания обеспечиваются:
(1) снятием inline-разметки здесь, (2) фиксированным шрифтом отрисовки в UI. Стилевых
атрибутов у текста нет — только строка, поэтому нормализация = приведение самой строки.
"""
from __future__ import annotations

import html
import re
import unicodedata
from collections import Counter

_MATH_RE = re.compile(r"<math.*?</math>", re.I | re.S)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_LATEX_BLOCK_RE = re.compile(r"\$[^$]*\$")
_LATEX_CMD_ARG_RE = re.compile(r"\\[a-zA-Z]+\s*\{[^}]*\}")
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+")
# управляющие/непечатаемые (кроме обычных пробелов, которые схлопнем ниже)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_WS_RE = re.compile(r"\s+")


def cleanup(t: str) -> str:
    """Единая нормализация строки текста (см. модульный docstring)."""
    if not t:
        return ""
    # 1) HTML-сущности -> символы (&amp;->&, &#176;->°, &lt;->'<')
    t = html.unescape(t)
    # 2) math-блоки Surya (обычно мусор в текст-боксе) -> убрать целиком
    t = _MATH_RE.sub(" ", t)
    # 3) <br> -> пробел (перенос строки внутри блока)
    t = _BR_RE.sub(" ", t)
    # 4) прочие HTML-теги -> убрать БЕЗ пробела (не рвём "T1" из <i>T</i><u>1</u>)
    t = _TAG_RE.sub("", t)
    # 5) LaTeX
    t = _LATEX_BLOCK_RE.sub(" ", t)       # $...$
    t = _LATEX_CMD_ARG_RE.sub(" ", t)     # \frac{..}{..}, \tilde{..}
    t = _LATEX_CMD_RE.sub(" ", t)         # одиночные \cmd
    # 6) markdown-жирность (Surya обычно даёт <b>, но на всякий случай)
    t = t.replace("**", "").replace("__", "")
    # 7) Unicode NFKC: полноширинные/лигатуры/совместимые -> канон
    t = unicodedata.normalize("NFKC", t)
    # 8) кавычки-ёлочки и переводы строк -> простые
    t = t.replace("\n", " ").replace("\r", " ").replace("“", '"').replace("”", '"')
    # 9) удалить управляющие/непечатаемые
    t = _CONTROL_RE.sub("", t)
    # 10) схлопнуть пробелы
    t = _WS_RE.sub(" ", t).strip()
    # 11) схлопнуть зацикливание токенов (артефакт Surya-VLM)
    toks = t.split(" ")
    if len(toks) > 3:
        c = Counter(toks)
        if c.most_common(1)[0][1] >= max(3, len(toks) * 0.5):
            out = []
            for tok in toks:
                if not out or out[-1] != tok:
                    out.append(tok)
            t = " ".join(out)
    return t


# алиас для читаемости в местах вызова
normalize_text = cleanup

"""Checks on a model-written drive summary, and the deterministic template fallback.

A summary is ``{"sentences": [{"text": str, "stat_ids": [str, ...]}, ...]}``. ``validate``
returns 1 plain problem string per violation (an empty list means the summary passes):

- the shape is exactly that (no other keys, every text a non-empty string);
- ``min_sentences`` to ``max_sentences`` sentences (config ``[summary]``: 3 to 5);
- every sentence cites at least 1 stat id, and every id it cites exists in the input;
- every number written in digits appears, as the same string, in the ``display`` or the
  ``label`` of a stat that sentence cites. Labels count because some carry numbers that are
  facts, not results ("0 to 60 mph"). Numbers spelled as words are not checked;
- no dash used as punctuation: em dash, en dash, figure dash, horizontal bar, a spaced
  hyphen, or a double hyphen. A hyphen inside a word ("short-term") or a leading minus sign
  stays allowed;
- no banned word in any form. The ban covers every inflection and, for the word that is only
  banned as a verb, the noun too, because the 2 cannot be told apart reliably.

The banned words are written split in this file so ``scripts/copy_lint.py``, which scans every
line of ``src/photoncar``, does not flag the list itself.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from typing import TypedDict

from photoncar.summary.stats import StatItem


class Sentence(TypedDict):
    text: str
    stat_ids: list[str]


class Summary(TypedDict):
    sentences: list[Sentence]


MIN_SENTENCES = 3
MAX_SENTENCES = 5

_BANNED_PARTS: tuple[tuple[str, str], ...] = (
    ("lever", "age"),
    ("del", "ve"),
    ("land", "scape"),
    ("show", "case"),
    ("piv", "otal"),
    ("seam", "less"),
    ("rob", "ust"),
    ("em", "power"),
    ("un", "lock"),
    ("trans", "form"),
)
BANNED_WORDS: tuple[str, ...] = tuple(a + b for a, b in _BANNED_PARTS)


def _stem(word: str) -> str:
    return word[:-1] if word.endswith("e") else word


_BANNED_RE = re.compile(
    r"\b(" + "|".join(_stem(w) + r"\w*" for w in BANNED_WORDS) + r")\b", re.IGNORECASE
)

# Figure dash, en dash, em dash, horizontal bar (U+2012 to U+2015); then a spaced hyphen and a
# double hyphen. Built from code points so no formatter or editor turns them into literals here.
_DASH_CHARS = "".join(chr(c) for c in (0x2012, 0x2013, 0x2014, 0x2015))
_DASH_RE = re.compile("[" + _DASH_CHARS + "]| - |--")

NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def numbers_in(text: str) -> list[str]:
    """The numbers written in digits, as strings: "1,234.5 m in 0:58.2" gives 1,234.5, 0, 58.2."""
    return NUMBER_RE.findall(text)


def parse_output(text: str) -> tuple[object | None, list[str]]:
    """Parse the model's text as JSON. Returns (value, problems)."""
    try:
        return json.loads(text), []
    except (json.JSONDecodeError, ValueError):
        return None, ["the reply is not valid JSON"]


def _sentence_problems(n: int, text: str, ids: list[str], stats: dict[str, StatItem]) -> list[str]:
    out: list[str] = []
    if not ids:
        out.append(f"sentence {n} cites no stat id")
    unknown = [i for i in ids if i not in stats]
    out.extend(f"sentence {n} cites the unknown stat id {i!r}" for i in unknown)
    cited = [stats[i] for i in ids if i in stats]
    allowed: set[str] = set()
    for s in cited:
        allowed.update(numbers_in(s["display"]))
        allowed.update(numbers_in(s["label"]))
    for num in numbers_in(text):
        if num not in allowed:
            out.append(f"sentence {n} has the number {num} that no cited stat shows")
    if _DASH_RE.search(text):
        out.append(f"sentence {n} uses a dash")
    for m in _BANNED_RE.finditer(text):
        out.append(f"sentence {n} uses the banned word {m.group(1)!r}")
    return out


def validate(
    output: object,
    stats: Sequence[StatItem],
    *,
    min_sentences: int = MIN_SENTENCES,
    max_sentences: int = MAX_SENTENCES,
) -> list[str]:
    """Every problem with ``output`` as a summary of ``stats``. Empty means it passes."""
    if not isinstance(output, dict):
        return ["the reply is not a JSON object"]
    extra = sorted(str(k) for k in output if k != "sentences")
    problems = [f"the reply has the unexpected key {k!r}" for k in extra]
    sentences = output.get("sentences")
    if not isinstance(sentences, list):
        return [*problems, "the reply has no sentences list"]
    if not min_sentences <= len(sentences) <= max_sentences:
        problems.append(
            f"the reply needs {min_sentences} to {max_sentences} sentences, not {len(sentences)}"
        )
    by_id = {s["id"]: s for s in stats}
    for n, item in enumerate(sentences, 1):
        if not isinstance(item, dict):
            problems.append(f"sentence {n} is not an object")
            continue
        extra_keys = sorted(str(k) for k in item if k not in ("text", "stat_ids"))
        problems.extend(f"sentence {n} has the unexpected key {k!r}" for k in extra_keys)
        text = item.get("text")
        ids = item.get("stat_ids")
        if not isinstance(text, str) or not text.strip():
            problems.append(f"sentence {n} has no text")
            continue
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            problems.append(f"sentence {n} has no stat_ids list of strings")
            continue
        problems.extend(_sentence_problems(n, text, ids, by_id))
    return problems


def normalize(output: object) -> Summary:
    """The validated output with only the contract keys. Call only after ``validate`` passes."""
    if not isinstance(output, dict) or not isinstance(output.get("sentences"), list):
        raise ValueError("normalize needs a validated summary")
    return Summary(
        sentences=[
            Sentence(text=str(s["text"]).strip(), stat_ids=[str(i) for i in s["stat_ids"]])
            for s in output["sentences"]
        ]
    )


def _template_sentence(stat: StatItem) -> str:
    label = stat["label"].strip()
    label = label[:1].upper() + label[1:]
    return f"{label}: {stat['display'].strip()}."


def template_candidates(stats: Iterable[StatItem]) -> list[StatItem]:
    """Stats whose template sentence passes every sentence check on its own."""
    out: list[StatItem] = []
    for s in stats:
        if not s["label"].strip() or not s["display"].strip():
            continue
        text = _template_sentence(s)
        if not _sentence_problems(1, text, [s["id"]], {s["id"]: s}):
            out.append(s)
    return out


def template_summary(
    stats: Sequence[StatItem],
    *,
    min_sentences: int = MIN_SENTENCES,
    max_sentences: int = MAX_SENTENCES,
) -> Summary:
    """A deterministic summary: 1 sentence per stat, "<Label>: <display>.", in stored order.

    Uses the first ``max_sentences`` stats whose sentence passes the checks. Raises
    ``ValueError`` when fewer than ``min_sentences`` such stats exist.
    """
    chosen = template_candidates(stats)[:max_sentences]
    if len(chosen) < min_sentences:
        raise ValueError(f"the template needs {min_sentences} usable stats, found {len(chosen)}")
    return Summary(
        sentences=[Sentence(text=_template_sentence(s), stat_ids=[s["id"]]) for s in chosen]
    )

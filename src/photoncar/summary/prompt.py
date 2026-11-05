"""The summary prompt. Its text is part of the cache key, so any edit here misses the cache.

Bump ``[summary] prompt_version`` in config when this text changes meaning.
"""

from __future__ import annotations

from collections.abc import Sequence

from photoncar.summary.validate import BANNED_WORDS

USER_PREFIX = "Drive stats as JSON. Write the summary from these stats only.\n"


def system_prompt(min_sentences: int, max_sentences: int) -> str:
    words = ", ".join(BANNED_WORDS)
    return (
        "You write a short summary of 1 car drive for the car's owner, from computed stats only.\n"
        "The user message is a JSON list of stats. Each stat has id, label, value, unit, and "
        "display.\n"
        'Reply with JSON only, shaped as {"sentences": [{"text": "...", "stat_ids": ["..."]}]}.\n'
        "Rules:\n"
        f"1. Write {min_sentences} to {max_sentences} sentences.\n"
        "2. Each sentence lists in stat_ids every stat it uses, at least 1. Use only ids from "
        "the input.\n"
        "3. Copy every number exactly as it appears in the display or the label of a stat that "
        "the sentence cites. Do not round, convert units, compute, or add any other number.\n"
        "4. Use plain words, short sentences, active voice, and sentence case.\n"
        "5. Use no em dashes, no en dashes, and no hyphen as a dash.\n"
        f"6. Never use these words or any form of them: {words}.\n"
        "7. Say what the stats show. Do not guess causes and do not give driving advice."
    )


def retry_note(problems: Sequence[str]) -> str:
    """Appended to the system prompt for the single retry after a rejected reply."""
    listed = "\n".join(f"- {p}" for p in problems)
    return (
        "\n\nYour previous reply was rejected for these reasons:\n"
        f"{listed}\n"
        "Write a new reply that follows every rule. Reply with JSON only."
    )

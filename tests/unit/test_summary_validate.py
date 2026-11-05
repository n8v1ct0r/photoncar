"""The summary validator rejects each violation type; the template fallback always passes."""

from __future__ import annotations

import json
from typing import Any

import pytest

from photoncar.summary.prompt import system_prompt
from photoncar.summary.stats import StatItem
from photoncar.summary.validate import (
    BANNED_WORDS,
    normalize,
    numbers_in,
    parse_output,
    template_candidates,
    template_summary,
    validate,
)

STATS: list[StatItem] = [
    {"id": "perf.zero_to_sixty_s", "label": "0 to 60 mph", "value": 5.47, "unit": "s",
     "display": "5.47 s"},
    {"id": "perf.peak_lateral_g", "label": "Peak lateral", "value": 0.46, "unit": "g",
     "display": "0.46 g"},
    {"id": "drive.distance_m", "label": "Distance", "value": 1234.5, "unit": "m",
     "display": "1,234.5 m"},
    {"id": "behavior.hard_brakes", "label": "Hard brakes", "value": 2.0, "unit": "count",
     "display": "2"},
    {"id": "lap.best_s", "label": "Best lap", "value": 83.4, "unit": "s", "display": "1:23.4"},
]  # fmt: skip


def _summary(*sentences: tuple[str, list[str]]) -> dict[str, Any]:
    return {"sentences": [{"text": t, "stat_ids": ids} for t, ids in sentences]}


GOOD = _summary(
    ("You ran 0 to 60 mph in 5.47 s.", ["perf.zero_to_sixty_s"]),
    ("Peak lateral grip reached 0.46 g.", ["perf.peak_lateral_g"]),
    ("The drive covered 1,234.5 m with 2 hard brakes.",
     ["drive.distance_m", "behavior.hard_brakes"]),
    ("Your best lap was 1:23.4.", ["lap.best_s"]),
)  # fmt: skip


def test_a_good_summary_passes() -> None:
    assert validate(GOOD, STATS) == []


def test_numbers_are_read_as_written() -> None:
    assert numbers_in("1,234.5 m in 0:58.2, then 2 and 3.") == ["1,234.5", "0", "58.2", "2", "3"]
    assert numbers_in("Ends at 5.") == ["5"]


def test_missing_stat_id_is_rejected() -> None:
    out = _summary(
        ("You ran 0 to 60 mph in 5.47 s.", ["perf.zero_to_sixty_s"]),
        ("It was a calm drive.", []),
        ("Peak lateral grip reached 0.46 g.", ["perf.peak_lateral_g"]),
    )
    assert validate(out, STATS) == ["sentence 2 cites no stat id"]


def test_unknown_stat_id_is_rejected() -> None:
    out = _summary(
        ("You ran 0 to 60 mph in 5.47 s.", ["perf.zero_to_sixty_s"]),
        ("Peak lateral grip reached 0.46 g.", ["perf.peak_lateral_g", "perf.top_speed"]),
        ("There were 2 hard brakes.", ["behavior.hard_brakes"]),
    )
    assert validate(out, STATS) == ["sentence 2 cites the unknown stat id 'perf.top_speed'"]


def test_a_number_that_matches_no_cited_display_is_rejected() -> None:
    out = _summary(
        ("You ran 0 to 60 mph in 5.5 s.", ["perf.zero_to_sixty_s"]),  # rounded: not the display
        ("Peak lateral grip reached 0.46 g.", ["perf.peak_lateral_g"]),
        ("There were 2 hard brakes.", ["behavior.hard_brakes"]),
    )
    assert validate(out, STATS) == ["sentence 1 has the number 5.5 that no cited stat shows"]


def test_a_number_from_an_uncited_stat_is_rejected() -> None:
    out = _summary(
        ("You ran 0 to 60 mph in 5.47 s.", ["perf.zero_to_sixty_s"]),
        ("Peak lateral grip reached 0.46 g.", ["behavior.hard_brakes"]),  # cites the wrong stat
        ("There were 2 hard brakes.", ["behavior.hard_brakes"]),
    )
    assert validate(out, STATS) == ["sentence 2 has the number 0.46 that no cited stat shows"]


def test_a_number_with_changed_separators_is_rejected() -> None:
    out = _summary(
        ("The drive covered 1234.5 m.", ["drive.distance_m"]),
        ("Peak lateral grip reached 0.46 g.", ["perf.peak_lateral_g"]),
        ("There were 2 hard brakes.", ["behavior.hard_brakes"]),
    )
    assert validate(out, STATS) == ["sentence 1 has the number 1234.5 that no cited stat shows"]


def test_fewer_than_3_sentences_is_rejected() -> None:
    out = _summary(
        ("You ran 0 to 60 mph in 5.47 s.", ["perf.zero_to_sixty_s"]),
        ("Peak lateral grip reached 0.46 g.", ["perf.peak_lateral_g"]),
    )
    assert validate(out, STATS) == ["the reply needs 3 to 5 sentences, not 2"]


def test_more_than_5_sentences_is_rejected() -> None:
    six = [("There were 2 hard brakes.", ["behavior.hard_brakes"])] * 6
    assert validate(_summary(*six), STATS) == ["the reply needs 3 to 5 sentences, not 6"]


@pytest.mark.parametrize(
    "text",
    [
        "Peak lateral grip reached 0.46 g—a firm corner.",
        "Peak lateral grip reached 0.46 g–a firm corner.",
        "Peak lateral grip reached 0.46 g - a firm corner.",
        "Peak lateral grip reached 0.46 g -- a firm corner.",
        "Peak lateral grip reached 0.46 g‒a firm corner.",
        "Peak lateral grip reached 0.46 g―a firm corner.",
    ],
)
def test_a_dash_is_rejected(text: str) -> None:
    out = _summary(
        ("You ran 0 to 60 mph in 5.47 s.", ["perf.zero_to_sixty_s"]),
        (text, ["perf.peak_lateral_g"]),
        ("There were 2 hard brakes.", ["behavior.hard_brakes"]),
    )
    assert validate(out, STATS) == ["sentence 2 uses a dash"]


def test_a_hyphen_inside_a_word_is_allowed() -> None:
    out = _summary(
        ("You ran 0 to 60 mph in 5.47 s from a standing start.", ["perf.zero_to_sixty_s"]),
        ("Peak lateral grip reached 0.46 g in a long left-hander.", ["perf.peak_lateral_g"]),
        ("There were 2 hard brakes.", ["behavior.hard_brakes"]),
    )
    assert validate(out, STATS) == []


@pytest.mark.parametrize(
    ("word", "used"),
    [
        ("leverage", "Leveraging"),
        ("delve", "delves"),
        ("landscape", "landscape"),
        ("showcase", "showcased"),
        ("pivotal", "pivotal"),
        ("seamless", "seamlessly"),
        ("robust", "robust"),
        ("empower", "empowers"),
        ("unlock", "unlocked"),
        ("transform", "transformed"),
    ],
)
def test_each_banned_word_is_rejected_in_any_form(word: str, used: str) -> None:
    assert word in BANNED_WORDS
    out = _summary(
        ("You ran 0 to 60 mph in 5.47 s.", ["perf.zero_to_sixty_s"]),
        (f"{used} grip reached 0.46 g.", ["perf.peak_lateral_g"]),
        ("There were 2 hard brakes.", ["behavior.hard_brakes"]),
    )
    assert validate(out, STATS) == [f"sentence 2 uses the banned word {used!r}"]


def test_banned_list_is_exactly_the_copy_rules() -> None:
    assert BANNED_WORDS == (
        "leverage", "delve", "landscape", "showcase", "pivotal",
        "seamless", "robust", "empower", "unlock", "transform",
    )  # fmt: skip


def test_bad_shapes_are_rejected() -> None:
    assert parse_output("not json") == (None, ["the reply is not valid JSON"])
    assert validate([], STATS) == ["the reply is not a JSON object"]
    assert validate({"text": "x"}, STATS) == [
        "the reply has the unexpected key 'text'",
        "the reply has no sentences list",
    ]
    out: dict[str, Any] = {
        "sentences": [
            "a string",
            {"text": "", "stat_ids": ["lap.best_s"]},
            {"text": "Best lap 1:23.4.", "stat_ids": "lap.best_s"},
            {"text": "There were 2 hard brakes.", "stat_ids": ["behavior.hard_brakes"], "x": 1},
        ]
    }
    assert validate(out, STATS) == [
        "sentence 1 is not an object",
        "sentence 2 has no text",
        "sentence 3 has no stat_ids list of strings",
        "sentence 4 has the unexpected key 'x'",
    ]


def test_several_problems_are_all_reported() -> None:
    out = _summary(("Robust 9 laps—wow.", ["nope"]))
    assert validate(out, STATS) == [
        "the reply needs 3 to 5 sentences, not 1",
        "sentence 1 cites the unknown stat id 'nope'",
        "sentence 1 has the number 9 that no cited stat shows",
        "sentence 1 uses a dash",
        "sentence 1 uses the banned word 'Robust'",
    ]


def test_normalize_keeps_only_contract_keys() -> None:
    assert normalize(json.loads(json.dumps(GOOD))) == GOOD


def test_template_is_deterministic_and_valid() -> None:
    first = template_summary(STATS)
    assert first == template_summary(STATS)
    assert [s["text"] for s in first["sentences"]] == [
        "0 to 60 mph: 5.47 s.",
        "Peak lateral: 0.46 g.",
        "Distance: 1,234.5 m.",
        "Hard brakes: 2.",
        "Best lap: 1:23.4.",
    ]
    assert [s["stat_ids"] for s in first["sentences"]] == [[s["id"]] for s in STATS]
    assert validate(first, STATS) == []


def test_template_skips_stats_whose_own_text_breaks_the_rules() -> None:
    bad: list[StatItem] = [
        {"id": "a", "label": "Grip—peak", "value": 1.0, "unit": "g", "display": "1 g"},
        {"id": "b", "label": "Robust stat", "value": 1.0, "unit": "", "display": "1"},
        {"id": "c", "label": "", "value": 1.0, "unit": "", "display": "1"},
    ]
    assert [s["id"] for s in template_candidates(bad + STATS)] == [s["id"] for s in STATS]
    out = template_summary(bad + STATS, max_sentences=3)
    assert len(out["sentences"]) == 3
    assert validate(out, bad + STATS) == []


def test_template_needs_at_least_3_usable_stats() -> None:
    with pytest.raises(ValueError, match="needs 3 usable stats, found 2"):
        template_summary(STATS[:2])


def test_system_prompt_states_the_rules_and_passes_its_own_word_check() -> None:
    text = system_prompt(3, 5)
    assert "Write 3 to 5 sentences." in text
    for word in BANNED_WORDS:
        assert word in text
    assert "—" not in text and "–" not in text

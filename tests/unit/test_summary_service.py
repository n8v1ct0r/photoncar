"""The summary route's logic with the fake model: cache, retry, fallback, limits, and spend."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from photoncar.clock import SimClock
from photoncar.config import Config, load_config
from photoncar.records import DriveStat
from photoncar.summary.limits import worst_case_usd_micros
from photoncar.summary.model_client import (
    AnthropicSummaryClient,
    FakeSummaryClient,
    ModelCallFailed,
    ModelOffline,
)
from photoncar.summary.prompt import USER_PREFIX
from photoncar.summary.service import SummaryError, SummaryService
from photoncar.summary.stats import stats_json, summary_input
from photoncar.summary.validate import validate

S = 1_000_000_000
NOON = int(datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC).timestamp()) * S
IP = "192.168.4.20"

STATS = [
    DriveStat("perf.zero_to_sixty_s", "0 to 60 mph", 5.47, "s", "5.47 s"),
    DriveStat("perf.peak_lateral_g", "Peak lateral", 0.46, "g", "0.46 g"),
    DriveStat("drive.distance_m", "Distance", 1234.5, "m", "1,234.5 m"),
    DriveStat("behavior.hard_brakes", "Hard brakes", 2.0, "count", "2"),
]
GOOD = json.dumps(
    {
        "sentences": [
            {"text": "You ran 0 to 60 mph in 5.47 s.", "stat_ids": ["perf.zero_to_sixty_s"]},
            {"text": "Peak lateral grip reached 0.46 g.", "stat_ids": ["perf.peak_lateral_g"]},
            {"text": "You braked hard 2 times.", "stat_ids": ["behavior.hard_brakes"]},
        ]
    }
)
BAD = json.dumps({"sentences": [{"text": "A great drive.", "stat_ids": []}]})
COST = 600 * 1 + 120 * 5  # the fake's default usage at $1 / $5 per million tokens


def _cfg(**summary: Any) -> Config:
    raw = copy.deepcopy(dict(load_config().raw))
    raw["summary"] = {**raw["summary"], **summary}
    return Config(raw)


def _service(
    tmp_path: Path, client: Any, *, clock: SimClock | None = None, **summary: Any
) -> SummaryService:
    return SummaryService(
        _cfg(**summary), tmp_path / "index.sqlite", client, clock or SimClock(NOON)
    )


def test_a_valid_reply_is_returned_cached_and_charged(tmp_path: Path) -> None:
    fake = FakeSummaryClient([GOOD])
    svc = _service(tmp_path, fake)
    out = svc.summarize(STATS, IP)
    assert out.to_json() == {
        "sentences": json.loads(GOOD)["sentences"],
        "source": "model",
        "cached": False,
        "model": "fake-summary-model",
    }
    assert len(fake.calls) == 1
    system, payload, max_tokens = fake.calls[0]
    assert system == svc.system and max_tokens == 400
    assert json.loads(payload) == summary_input(STATS)
    assert svc.ledger.spent() == COST


def test_a_cache_hit_skips_the_client_the_spend_and_the_rate_limit(tmp_path: Path) -> None:
    fake = FakeSummaryClient([GOOD])
    svc = _service(tmp_path, fake, per_ip_per_minute=1)
    first = svc.summarize(STATS, IP)
    for _ in range(3):
        again = svc.summarize(STATS, IP)
        assert again.cached is True and again.source == "model"
        assert again.summary == first.summary
    assert len(fake.calls) == 1
    assert svc.ledger.spent() == COST


def test_a_cache_hit_is_served_while_offline(tmp_path: Path) -> None:
    _service(tmp_path, FakeSummaryClient([GOOD])).summarize(STATS, IP)
    offline = FakeSummaryClient(ready=False)
    out = _service(tmp_path, offline).summarize(STATS, IP)
    assert out.cached is True
    assert offline.calls == []


def test_a_rejected_reply_is_retried_once_with_the_problems(tmp_path: Path) -> None:
    fake = FakeSummaryClient([BAD, GOOD])
    svc = _service(tmp_path, fake)
    out = svc.summarize(STATS, IP)
    assert out.source == "model" and out.cached is False
    assert len(fake.calls) == 2
    retry_system = fake.calls[1][0]
    assert retry_system.startswith(svc.system)
    assert "- the reply needs 3 to 5 sentences, not 1" in retry_system
    assert "- sentence 1 cites no stat id" in retry_system
    assert svc.ledger.spent() == 2 * COST
    # The validated retry is cached under the original request's key.
    assert svc.summarize(STATS, IP).cached is True
    assert len(fake.calls) == 2


def test_two_rejections_fall_back_to_the_template_uncached(tmp_path: Path) -> None:
    fake = FakeSummaryClient([BAD, "not json", GOOD])
    svc = _service(tmp_path, fake)
    out = svc.summarize(STATS, IP)
    assert out.source == "template" and out.model is None and out.cached is False
    assert [s["text"] for s in out.summary["sentences"]] == [
        "0 to 60 mph: 5.47 s.",
        "Peak lateral: 0.46 g.",
        "Distance: 1,234.5 m.",
        "Hard brakes: 2.",
    ]
    assert validate(out.summary, summary_input(STATS)) == []
    assert len(fake.calls) == 2
    assert svc.ledger.spent() == 2 * COST
    # The template was not cached: the next request asks the model again.
    again = svc.summarize(STATS, IP)
    assert again.source == "model" and len(fake.calls) == 3


def test_no_api_key_is_503_needs_connection_with_no_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    svc = _service(tmp_path, AnthropicSummaryClient(_cfg()))
    with pytest.raises(SummaryError) as err:
        svc.summarize(STATS, IP)
    assert (err.value.status, err.value.message) == (503, "Needs connection.")
    assert svc.ledger.spent() == 0


def test_an_unreachable_service_is_503_and_releases_the_reservation(tmp_path: Path) -> None:
    fake = FakeSummaryClient([ModelOffline("no route")])
    svc = _service(tmp_path, fake)
    with pytest.raises(SummaryError) as err:
        svc.summarize(STATS, IP)
    assert (err.value.status, err.value.message) == (503, "Needs connection.")
    assert svc.ledger.spent() == 0


def test_a_service_error_is_502_and_releases_the_reservation(tmp_path: Path) -> None:
    fake = FakeSummaryClient([ModelCallFailed("HTTP 529", maybe_billed=False)])
    svc = _service(tmp_path, fake)
    with pytest.raises(SummaryError) as err:
        svc.summarize(STATS, IP)
    assert err.value.status == 502
    assert svc.ledger.spent() == 0


def test_a_timeout_keeps_the_worst_case_charge(tmp_path: Path) -> None:
    fake = FakeSummaryClient([ModelCallFailed("timed out", maybe_billed=True)])
    svc = _service(tmp_path, fake)
    worst = worst_case_usd_micros(
        (svc.system, USER_PREFIX, stats_json(summary_input(STATS))), 400, 1.0, 5.0
    )
    with pytest.raises(SummaryError) as err:
        svc.summarize(STATS, IP)
    assert err.value.status == 502
    assert svc.ledger.spent() == worst


def test_a_failure_during_the_retry_falls_back_to_the_template(tmp_path: Path) -> None:
    fake = FakeSummaryClient([BAD, ModelCallFailed("HTTP 500", maybe_billed=False)])
    svc = _service(tmp_path, fake)
    out = svc.summarize(STATS, IP)
    assert out.source == "template"
    assert svc.ledger.spent() == COST


def test_per_ip_window_returns_429_with_retry_after(tmp_path: Path) -> None:
    clock = SimClock(NOON)
    fake = FakeSummaryClient([BAD] * 100)  # every request misses the cache
    svc = _service(tmp_path, fake, clock=clock)
    for _ in range(5):
        assert svc.summarize(STATS, IP).source == "template"
    with pytest.raises(SummaryError) as err:
        svc.summarize(STATS, IP)
    assert err.value.status == 429
    assert err.value.message == "Too many summary requests. Try again soon."
    assert err.value.retry_after_s == 60
    assert len(fake.calls) == 10
    assert svc.summarize(STATS, "192.168.4.21").source == "template"  # another IP
    clock.advance(60 * S)
    assert svc.summarize(STATS, IP).source == "template"


def test_spend_cap_refuses_before_calling_with_retry_after_midnight(tmp_path: Path) -> None:
    fake = FakeSummaryClient([GOOD])
    svc = _service(tmp_path, fake, daily_cap_usd=0.001)  # 1,000 micro-USD: below 1 worst case
    with pytest.raises(SummaryError) as err:
        svc.summarize(STATS, IP)
    assert err.value.status == 429
    assert err.value.message == "The daily summary budget is spent. Try again tomorrow."
    assert err.value.retry_after_s == 12 * 3600
    assert fake.calls == []
    assert svc.ledger.spent() == 0


def test_spend_cap_that_blocks_the_retry_falls_back_to_the_template(tmp_path: Path) -> None:
    probe = _service(tmp_path / "probe", FakeSummaryClient())
    worst_first = worst_case_usd_micros(
        (probe.system, USER_PREFIX, stats_json(summary_input(STATS))), 400, 1.0, 5.0
    )
    fake = FakeSummaryClient([BAD, GOOD])
    svc = _service(tmp_path, fake, daily_cap_usd=worst_first / 1e6)
    out = svc.summarize(STATS, IP)
    assert out.source == "template"
    assert len(fake.calls) == 1
    assert svc.ledger.spent() == COST


def test_too_few_stats_is_422_without_a_call(tmp_path: Path) -> None:
    fake = FakeSummaryClient()
    svc = _service(tmp_path, fake)
    lat = DriveStat("gps.start_lat", "Start", 40.1, "deg", "40.1°")
    with pytest.raises(SummaryError) as err:
        svc.summarize([*STATS[:2], lat], IP)
    assert (err.value.status, err.value.message) == (
        422,
        "This drive needs at least 3 stats for a summary.",
    )
    assert fake.calls == []


def test_the_model_never_sees_gps_dates_or_ids(tmp_path: Path) -> None:
    fake = FakeSummaryClient()
    svc = _service(tmp_path, fake)
    risky = [
        DriveStat("gps.start_lat", "Start", 40.1, "deg", "40.1°"),
        DriveStat("drive.date", "Date", 0.0, "", "2026-09-23"),
        DriveStat("drive.drive_id", "Drive", 0.0, "", "3f1c9a52"),
    ]
    out = svc.summarize([*risky, *STATS], IP)
    assert out.source == "model"
    payload = fake.calls[0][1]
    assert [s["id"] for s in json.loads(payload)] == [s.stat_id for s in STATS]
    for text in ("gps.start_lat", "2026-09-23", "3f1c9a52"):
        assert text not in payload

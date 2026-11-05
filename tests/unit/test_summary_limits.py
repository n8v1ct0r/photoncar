"""Output cache, per-IP sliding windows, and the daily spend cap with reservations."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from photoncar.clock import SimClock
from photoncar.summary.cache import SummaryCache, cache_key
from photoncar.summary.limits import (
    PROMPT_OVERHEAD_TOKENS,
    SlidingWindowLimiter,
    SpendLedger,
    cost_usd_micros,
    seconds_to_utc_midnight,
    utc_day,
    worst_case_usd_micros,
)
from photoncar.summary.stats import StatItem
from photoncar.summary.validate import Summary

S = 1_000_000_000
# 2026-09-23 12:00:00 UTC, as epoch nanoseconds.
NOON = int(datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC).timestamp()) * S

STATS: list[StatItem] = [
    {"id": "a", "label": "A", "value": 1.0, "unit": "s", "display": "1 s"},
    {"id": "b", "label": "B", "value": 2.0, "unit": "s", "display": "2 s"},
]


# -- cache ---------------------------------------------------------------------------------


def _key(
    *,
    model: str = "m",
    prompt_version: int = 1,
    system: str = "sys",
    stats: list[StatItem] = STATS,
    max_tokens: int = 400,
) -> str:
    return cache_key(
        model=model,
        prompt_version=prompt_version,
        system=system,
        stats=stats,
        max_tokens=max_tokens,
    )


def test_cache_key_is_sha256_of_every_input() -> None:
    base = _key()
    assert len(base) == 64 and base == _key()
    changed = [
        _key(model="n"),
        _key(prompt_version=2),
        _key(system="sys2"),
        _key(stats=STATS[:1]),
        _key(stats=[{**STATS[0], "display": "1.0 s"}, STATS[1]]),
        _key(max_tokens=401),
    ]
    assert len({base, *changed}) == 7


def test_cache_round_trip_and_first_write_wins(tmp_path: Path) -> None:
    cache = SummaryCache(tmp_path / "index.sqlite")
    key = _key()
    assert cache.get(key) is None
    summary = Summary(sentences=[{"text": "A: 1 s.", "stat_ids": ["a"]}])
    cache.put(key, model="m", prompt_version=1, stats_json="[]", summary=summary,
              in_tokens=10, out_tokens=5, cost_usd_micros=35)  # fmt: skip
    other = Summary(sentences=[{"text": "B: 2 s.", "stat_ids": ["b"]}])
    cache.put(key, model="m", prompt_version=1, stats_json="[]", summary=other,
              in_tokens=1, out_tokens=1, cost_usd_micros=6)  # fmt: skip
    hit = cache.get(key)
    assert hit is not None
    assert hit.summary == summary
    assert (hit.model, hit.in_tokens, hit.out_tokens, hit.cost_usd_micros) == ("m", 10, 5, 35)
    with sqlite3.connect(tmp_path / "index.sqlite") as conn:
        rows = conn.execute("SELECT input_sha256, created_seq FROM model_summaries").fetchall()
    assert rows == [(key, 1)]


# -- cost ----------------------------------------------------------------------------------


def test_cost_is_micro_usd_rounded_up() -> None:
    # $1 and $5 per million tokens: 1 token costs 1 and 5 micro-USD.
    assert cost_usd_micros(600, 120, 1.0, 5.0) == 1200
    assert cost_usd_micros(1, 0, 0.8, 4.0) == 1  # 0.8 rounds up
    assert cost_usd_micros(600, 0, 0.8, 4.0) == 480  # no float fuzz past an exact value


def test_worst_case_counts_every_byte_plus_overhead_and_max_output() -> None:
    texts = ("abc", "é")  # 3 + 2 bytes
    expected = (5 + PROMPT_OVERHEAD_TOKENS) * 1 + 400 * 5
    assert worst_case_usd_micros(texts, 400, 1.0, 5.0) == expected


def test_utc_day_and_time_to_midnight() -> None:
    assert utc_day(NOON) == "2026-09-23"
    assert seconds_to_utc_midnight(NOON) == 12 * 3600
    assert utc_day(NOON + 12 * 3600 * S) == "2026-09-24"


# -- per-IP sliding window -----------------------------------------------------------------


def test_minute_window_allows_5_then_waits_for_the_oldest() -> None:
    clock = SimClock(NOON)
    lim = SlidingWindowLimiter([(60.0, 5), (86_400.0, 30)], clock)
    for _ in range(5):
        assert lim.check("10.0.0.2") is None
        clock.advance(10 * S)
    # Hits at 0, 10, 20, 30, 40 s; now 50 s. The next slot opens when the 0 s hit is 60 s old.
    assert lim.check("10.0.0.2") == pytest.approx(10.0, abs=1e-9)
    assert lim.check("10.0.0.3") is None  # other IPs are counted apart
    clock.advance(10 * S)
    assert lim.check("10.0.0.2") is None


def test_day_window_allows_30_per_24_h() -> None:
    clock = SimClock(NOON)
    lim = SlidingWindowLimiter([(60.0, 5), (86_400.0, 30)], clock)
    for _ in range(30):
        assert lim.check("ip") is None
        clock.advance(60 * S)  # 1 per minute never trips the minute window
    # First hit at 0; now 30 min. It leaves the day window at 24 h.
    wait = lim.check("ip")
    assert wait is not None
    assert wait == pytest.approx(86_400.0 - 30 * 60, abs=1e-9)
    clock.advance(round(wait * S))
    assert lim.check("ip") is None


def test_a_refused_hit_is_not_counted() -> None:
    clock = SimClock(NOON)
    lim = SlidingWindowLimiter([(60.0, 1)], clock)
    assert lim.check("ip") is None
    for _ in range(10):
        assert lim.check("ip") is not None
    clock.advance(60 * S)
    assert lim.check("ip") is None


# -- spend ledger --------------------------------------------------------------------------


def test_reserve_then_settle_to_actual_usage(tmp_path: Path) -> None:
    clock = SimClock(NOON)
    ledger = SpendLedger(tmp_path / "index.sqlite", 10_000, clock)
    res = ledger.reserve(4_000)
    assert res is not None and res.day_utc == "2026-09-23"
    assert ledger.spent() == 4_000
    ledger.settle(res, 1_200)
    assert ledger.spent() == 1_200
    res2 = ledger.reserve(3_000)
    assert res2 is not None
    ledger.release(res2)
    assert ledger.spent() == 1_200


def test_reservation_past_the_cap_is_refused(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "index.sqlite", 10_000, SimClock(NOON))
    assert ledger.reserve(6_000) is not None
    assert ledger.reserve(4_001) is None
    assert ledger.spent() == 6_000
    assert ledger.reserve(4_000) is not None
    assert ledger.spent() == 10_000


def test_the_cap_resets_at_utc_midnight(tmp_path: Path) -> None:
    clock = SimClock(NOON)
    ledger = SpendLedger(tmp_path / "index.sqlite", 1_000, clock)
    res = ledger.reserve(1_000)
    assert res is not None
    assert ledger.reserve(1) is None
    clock.advance(12 * 3600 * S)
    assert ledger.today() == "2026-09-24"
    assert ledger.reserve(1_000) is not None
    # Settling yesterday's reservation corrects yesterday's row, not today's.
    ledger.settle(res, 400)
    assert ledger.spent("2026-09-23") == 400
    assert ledger.spent("2026-09-24") == 1_000


def test_concurrent_reservations_never_pass_the_cap(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    clock = SimClock(NOON)
    # 2 ledgers on 1 file stand for 2 server processes; each has 8 threads.
    ledgers = [SpendLedger(path, 25_000, clock), SpendLedger(path, 25_000, clock)]
    granted: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(16)

    def worker(ledger: SpendLedger) -> None:
        start.wait()
        for _ in range(5):
            if ledger.reserve(1_000) is not None:
                with lock:
                    granted.append(1_000)

    threads = [threading.Thread(target=worker, args=(ledgers[i % 2],)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 80 attempts of 1,000 against a 25,000 cap: exactly 25 succeed.
    assert len(granted) == 25
    assert ledgers[0].spent() == 25_000

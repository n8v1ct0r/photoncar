"""Limits on the model route: a per-IP sliding window and a daily spend cap.

Per IP (config ``[summary]``): at most ``per_ip_per_minute`` calls in any 60 s and
``per_ip_per_day`` in any 24 h, counted in memory. A restart clears the windows; the spend cap,
which is the limit that protects money, lives in SQLite and survives restarts.

Spend (table ``spend_ledger`` in ``data/index.sqlite``, 1 row per UTC day, in micro-USD):

1. Before a call, reserve its worst-case cost under a process lock and a SQLite write
   transaction. The reservation fails when it would take the day past ``daily_cap_usd``.
2. After the call, replace the reservation with the cost from the reported token usage.
3. A call that failed before it could be billed releases its reservation; a timeout keeps it.

Worst case: every byte of prompt text counted as 1 input token (a token is at least 1 byte),
plus ``PROMPT_OVERHEAD_TOKENS`` for message framing and the structured-output instructions the
API adds, plus ``max_tokens`` output tokens. The overhead figure is an assumption, not a
measured number; step 2 corrects the ledger either way.

Prices are USD per million tokens, so 1 token at $1 per million costs exactly 1 micro-USD.
Clock time is wall-clock UTC nanoseconds (``WallClock``); tests pass a ``SimClock`` set to an
epoch time.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from photoncar.clock import Clock
from photoncar.summary.cache import connect

NS_PER_S = 1_000_000_000
PROMPT_OVERHEAD_TOKENS = 1024
USD_MICROS = 1_000_000


class WallClock:
    """UTC wall-clock nanoseconds since the Unix epoch."""

    def now_ns(self) -> int:
        return time.time_ns()


def utc_day(t_ns: int) -> str:
    return datetime.fromtimestamp(t_ns / NS_PER_S, UTC).strftime("%Y-%m-%d")


def seconds_to_utc_midnight(t_ns: int) -> float:
    now = datetime.fromtimestamp(t_ns / NS_PER_S, UTC)
    midnight = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
    return (midnight - now).total_seconds()


def cost_usd_micros(
    in_tokens: int, out_tokens: int, in_usd_per_mtok: float, out_usd_per_mtok: float
) -> int:
    """Cost in micro-USD, rounded up."""
    return math.ceil(in_tokens * in_usd_per_mtok + out_tokens * out_usd_per_mtok - 1e-9)


def worst_case_usd_micros(
    prompt_texts: Sequence[str],
    max_tokens: int,
    in_usd_per_mtok: float,
    out_usd_per_mtok: float,
) -> int:
    in_bound = sum(len(t.encode("utf-8")) for t in prompt_texts) + PROMPT_OVERHEAD_TOKENS
    return cost_usd_micros(in_bound, max_tokens, in_usd_per_mtok, out_usd_per_mtok)


class SlidingWindowLimiter:
    """Allow at most ``limit`` hits per key in any ``seconds``-long window, for each window."""

    def __init__(self, windows: Sequence[tuple[float, int]], clock: Clock) -> None:
        if not windows:
            raise ValueError("at least 1 window is needed")
        self._windows = [(round(s * NS_PER_S), int(n)) for s, n in windows]
        self._span_ns = max(w for w, _ in self._windows)
        self._clock = clock
        self._hits: dict[str, deque[int]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> float | None:
        """Record a hit and return None, or return the seconds to wait (and record nothing)."""
        now = self._clock.now_ns()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self._span_ns:
                hits.popleft()
            wait_ns = 0
            for span, limit in self._windows:
                recent = [t for t in hits if now - t < span]
                if len(recent) >= limit:
                    wait_ns = max(wait_ns, recent[len(recent) - limit] + span - now)
            if wait_ns > 0:
                return wait_ns / NS_PER_S
            hits.append(now)
            return None


@dataclass(frozen=True, slots=True)
class Reservation:
    day_utc: str
    usd_micros: int


class SpendLedger:
    def __init__(self, index_path: Path, cap_usd_micros: int, clock: Clock) -> None:
        if cap_usd_micros < 0:
            raise ValueError("the spend cap must not be negative")
        self.path = Path(index_path)
        self.cap_usd_micros = cap_usd_micros
        self._clock = clock
        self._lock = threading.Lock()

    def today(self) -> str:
        return utc_day(self._clock.now_ns())

    def spent(self, day_utc: str | None = None) -> int:
        day = day_utc or self.today()
        with closing(connect(self.path)) as conn:
            row = conn.execute(
                "SELECT usd_micros FROM spend_ledger WHERE day_utc = ?", (day,)
            ).fetchone()
        return 0 if row is None else int(row[0])

    def reserve(self, usd_micros: int) -> Reservation | None:
        """Reserve ``usd_micros`` for today, or return None if that would pass the cap."""
        if usd_micros < 0:
            raise ValueError("a reservation must not be negative")
        day = self.today()
        with self._lock, closing(connect(self.path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT usd_micros FROM spend_ledger WHERE day_utc = ?", (day,)
                ).fetchone()
                current = 0 if row is None else int(row[0])
                if current + usd_micros > self.cap_usd_micros:
                    conn.execute("ROLLBACK")
                    return None
                conn.execute(
                    "INSERT INTO spend_ledger (day_utc, usd_micros) VALUES (?, ?) "
                    "ON CONFLICT (day_utc) DO UPDATE SET usd_micros = usd_micros + "
                    "excluded.usd_micros",
                    (day, usd_micros),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return Reservation(day, usd_micros)

    def settle(self, reservation: Reservation, actual_usd_micros: int) -> None:
        """Replace a reservation with the actual cost (0 releases it)."""
        if actual_usd_micros < 0:
            raise ValueError("a cost must not be negative")
        delta = actual_usd_micros - reservation.usd_micros
        if delta == 0:
            return
        with self._lock, closing(connect(self.path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO spend_ledger (day_utc, usd_micros) VALUES (?, MAX(0, ?)) "
                    "ON CONFLICT (day_utc) DO UPDATE SET usd_micros = "
                    "MAX(0, usd_micros + ?)",
                    (reservation.day_utc, delta, delta),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def release(self, reservation: Reservation) -> None:
        self.settle(reservation, 0)

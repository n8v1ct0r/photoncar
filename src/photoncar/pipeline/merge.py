"""Merge time-ordered sources into the single timeline (docs/phase-1-plan.md section 5).

``merge_sources``: ``heapq.merge`` on ``record_time``. On equal times, records keep source
order: every record of an earlier source comes before a record of a later source with the same
time, and records within 1 source keep their own order. It waits for every source's next
record, so it suits recorded and synthetic sources, which always have one ready.

``merge_live``: the same order for live sources, which may go quiet. A GPS without a fix yields
nothing, so ``heapq.merge`` would stop the whole timeline until the fix returns. Here every
source runs on its own thread into its own queue, and the merge never waits on a quiet source
for longer than the reorder watermark (``REORDER_WATERMARK_NS``, the plan's approved 200 ms):

- While every source that is still running has a record queued, the smallest goes out: the
  same order as ``merge_sources``.
- A source whose queue is empty is quiet once nothing has arrived from it for the watermark.
  Records then go out past it once they are older than what it could still deliver: its last
  record arrived ``lag`` after that record's time, so anything it sends later is assumed to be
  no older than ``now - lag - watermark``. Before its first record the lag counts as 0.
- A record that still arrives older than one already sent is late. It goes to ``on_late``
  (the pipeline stores it without analysing it) and never breaks the time order.

The Source contract says ``records()`` never goes back in time. Both merges check that, and
raise ``ValueError`` naming the source rather than emitting a misordered timeline.
"""

from __future__ import annotations

import heapq
import threading
from collections import deque
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from photoncar.clock import Clock
from photoncar.records import Record, record_time
from photoncar.sources.base import Source

REORDER_WATERMARK_NS = 200_000_000
"""The reorder watermark: 200 ms (docs/phase-1-plan.md section 13, approved)."""
MAX_QUEUED = 10_000
"""Records a live source may hold queued before its thread waits for the merge (proposed).
At the IMU's 104 Hz that is over 90 s, far more than a live merge ever falls behind; it only
bounds memory when a source yields faster than real time."""


def _checked(name: str, records: Iterable[Record]) -> Generator[Record]:
    last: int | None = None
    for rec in records:
        t = record_time(rec)
        if last is not None and t < last:
            raise ValueError(f"source {name} went back in time: {t} ns after {last} ns")
        last = t
        yield rec


def merge_streams(
    streams: Sequence[Iterable[Record]], names: Sequence[str] | None = None
) -> Iterator[Record]:
    """Merge record streams; ties go to the earlier stream."""
    labels = list(names) if names is not None else [f"#{i}" for i in range(len(streams))]
    if len(labels) != len(streams):
        raise ValueError("names must match streams 1 to 1")
    checked = [_checked(n, s) for n, s in zip(labels, streams, strict=True)]
    return heapq.merge(*checked, key=record_time)


def merge_sources(sources: Sequence[Source]) -> Iterator[Record]:
    """Merge sources into 1 time-ordered stream; ties go to the earlier source."""
    return merge_streams([s.records() for s in sources], [s.meta.name for s in sources])


# -- live -------------------------------------------------------------------------------------


@dataclass(eq=False)
class _Lane:
    """1 live source: its queue and what the merge knows about its timing."""

    name: str
    arrived_ns: int
    """Clock time of the last arrival, or of the start before the first record."""
    queue: deque[Record] = field(default_factory=deque)
    lag_ns: int = 0
    """Arrival time minus record time of the last record that arrived."""
    done: bool = False
    error: BaseException | None = None


def _pump(
    lane: _Lane,
    records: Iterable[Record],
    clock: Clock,
    cond: threading.Condition,
    stop: threading.Event,
    max_queued: int,
) -> None:
    """Thread body: move 1 source's records into its lane until the source ends or the merge
    stops. A source error goes to the merge, which raises it."""
    source = iter(records)
    stream = _checked(lane.name, source)
    try:
        for rec in stream:
            with cond:
                while len(lane.queue) >= max_queued and not stop.is_set():
                    cond.wait()
                if stop.is_set():
                    return
                now = clock.now_ns()
                lane.queue.append(rec)
                lane.arrived_ns = now
                lane.lag_ns = now - record_time(rec)
                cond.notify_all()
    except BaseException as e:  # handed to the merge, which raises it
        with cond:
            lane.error = e
    finally:
        try:
            stream.close()
            close = getattr(source, "close", None)
            if callable(close):
                close()
        finally:
            with cond:
                lane.done = True
                cond.notify_all()


def _next_lane(lanes: Sequence[_Lane]) -> _Lane | None:
    """The lane with the smallest queued record; ties go to the earlier source."""
    best: _Lane | None = None
    best_t = 0
    for lane in lanes:
        if lane.queue:
            t = record_time(lane.queue[0])
            if best is None or t < best_t:
                best, best_t = lane, t
    return best


def _wait_ns(t: int, cand: _Lane, lanes: Sequence[_Lane], now: int, watermark_ns: int) -> int:
    """How long a record at ``t`` must still wait for quiet lanes (0: it may go now)."""
    wait = 0
    for lane in lanes:
        if lane is cand or lane.done or lane.queue:
            continue
        quiet_at = lane.arrived_ns + watermark_ns
        safe_at = t + lane.lag_ns + watermark_ns
        wait = max(wait, max(quiet_at, safe_at) - now)
    return wait


def merge_live(
    sources: Sequence[Source],
    clock: Clock,
    *,
    watermark_ns: int = REORDER_WATERMARK_NS,
    on_late: Callable[[Record], None] | None = None,
    max_queued: int = MAX_QUEUED,
) -> Iterator[Record]:
    """Merge live sources in time order without waiting on a quiet one. See the module
    docstring. ``clock`` is the clock the sources stamp their records on. Late records go to
    ``on_late`` (dropped without it). Closing the iterator stops the source threads at their
    next record; a source blocked inside its own ``records()`` ends when that source stops."""
    if watermark_ns < 0 or max_queued < 1:
        raise ValueError("watermark_ns must be at least 0 and max_queued at least 1")
    cond = threading.Condition()
    stop = threading.Event()
    start = clock.now_ns()
    lanes = [_Lane(s.meta.name, arrived_ns=start) for s in sources]
    threads = [
        threading.Thread(
            target=_pump,
            args=(lane, s.records(), clock, cond, stop, max_queued),
            name=f"photoncar-merge-{lane.name}",
            daemon=True,
        )
        for lane, s in zip(lanes, sources, strict=True)
    ]

    def run() -> Iterator[Record]:
        for th in threads:
            th.start()
        last_t: int | None = None
        try:
            while True:
                batch: list[Record] = []
                with cond:
                    while True:
                        failed = next((ln for ln in lanes if ln.error is not None), None)
                        if failed is not None and failed.error is not None:
                            raise failed.error
                        cand = _next_lane(lanes)
                        if cand is None:
                            if batch or all(ln.done for ln in lanes):
                                break
                            cond.wait()
                            continue
                        t = record_time(cand.queue[0])
                        wait = _wait_ns(t, cand, lanes, clock.now_ns(), watermark_ns)
                        if wait <= 0:
                            batch.append(cand.queue.popleft())
                            cond.notify_all()
                            continue
                        if batch:
                            break
                        cond.wait(timeout=wait / 1e9)
                if not batch:
                    return
                for rec in batch:
                    t = record_time(rec)
                    if last_t is not None and t < last_t:
                        if on_late is not None:
                            on_late(rec)
                        continue
                    last_t = t
                    yield rec
        finally:
            with cond:
                stop.set()
                cond.notify_all()

    return run()

"""The live merge: time order like ``merge_sources``, without waiting on a quiet source."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Sequence

import pytest

from photoncar.clock import MonotonicClock, SimClock
from photoncar.pipeline.merge import REORDER_WATERMARK_NS, merge_live, merge_sources
from photoncar.records import Record, Sample, record_time
from photoncar.sources.base import SourceMeta
from photoncar.units import NS_PER_S


class ListSource:
    def __init__(self, name: str, records: Sequence[Record]) -> None:
        self._meta = SourceMeta(name=name, kind="live")
        self._records = records

    @property
    def meta(self) -> SourceMeta:
        return self._meta

    def records(self) -> Iterator[Record]:
        yield from self._records


class GatedSource:
    """Yields ``records`` only once ``go`` is set; ends when ``stop`` is set."""

    def __init__(self, name: str, records: Sequence[Record], stop: threading.Event) -> None:
        self._meta = SourceMeta(name=name, kind="live")
        self._records = records
        self.go = threading.Event()
        self._stop = stop
        self.closed = threading.Event()

    @property
    def meta(self) -> SourceMeta:
        return self._meta

    def records(self) -> Iterator[Record]:
        try:
            while not self.go.is_set():
                if self._stop.wait(0.005) and not self.go.is_set():
                    return
            yield from self._records
            self._stop.wait()
        finally:
            self.closed.set()


class StepClock:
    """Moves 1 s forward on every read: any source quiet for 1 read counts as quiet."""

    def __init__(self) -> None:
        self._t = 10**15
        self._lock = threading.Lock()

    def now_ns(self) -> int:
        with self._lock:
            self._t += NS_PER_S
            return self._t


def _s(t: int, signal: str, value: float = 0.0) -> Sample:
    return Sample(t, signal, value)


def _key(recs: list[Record]) -> list[tuple[int, str, float]]:
    return [(r.t_ns, r.signal, r.value) for r in recs if isinstance(r, Sample)]


def test_sources_that_keep_up_merge_exactly_like_merge_sources() -> None:
    """A clock that never moves makes no source quiet: the live merge waits for every source,
    as ``heapq.merge`` does, ties included."""
    a = [_s(t, "a", i) for i, t in enumerate(range(0, 30_000, 7))]
    b = [_s(t, "b", i) for i, t in enumerate(range(0, 30_000, 5))]
    c = [_s(t, "c", i) for i, t in enumerate(range(3, 30_000, 35))]

    def sources() -> list[ListSource]:
        return [ListSource("a", a), ListSource("b", b), ListSource("c", c)]

    late: list[Record] = []
    got = list(merge_live(sources(), SimClock(0), on_late=late.append, max_queued=64))
    assert _key(got) == _key(list(merge_sources(sources())))
    assert late == []


def test_a_quiet_source_holds_the_others_back_by_the_watermark_only() -> None:
    """A source that yields nothing (a GPS without a fix) delays the others by about the
    reorder watermark, never more."""
    clock = MonotonicClock()
    stop = threading.Event()
    quiet = GatedSource("gps", [], stop)

    class Ticking:
        meta = SourceMeta(name="can", kind="live")

        def records(self) -> Iterator[Record]:
            for i in range(15):
                yield _s(clock.now_ns(), "obd.speed", float(i))
                time.sleep(0.02)

    delays: list[float] = []
    try:
        for rec in merge_live([Ticking(), quiet], clock):
            delays.append((clock.now_ns() - record_time(rec)) / NS_PER_S)
            if len(delays) == 15:
                break
    finally:
        stop.set()
    assert len(delays) == 15
    watermark_s = REORDER_WATERMARK_NS / NS_PER_S
    assert max(delays) < watermark_s + 0.15  # scheduling slack on a busy machine
    assert quiet.closed.wait(2.0)  # closing the merge ends the source thread


def test_a_record_older_than_one_already_sent_goes_to_on_late() -> None:
    stop = threading.Event()
    early = GatedSource("gps", [_s(5, "gps.speed")], stop)
    ahead = ListSource("can", [_s(10, "obd.speed"), _s(20, "obd.speed"), _s(30, "obd.speed")])
    late: list[Record] = []
    it = merge_live([ahead, early], StepClock(), on_late=late.append)
    try:
        first = [next(it) for _ in range(3)]
        early.go.set()
        stop.set()
        rest = list(it)
    finally:
        stop.set()
    assert _key(first) == [(10, "obd.speed", 0.0), (20, "obd.speed", 0.0), (30, "obd.speed", 0.0)]
    assert rest == []
    assert _key(late) == [(5, "gps.speed", 0.0)]


def test_a_source_that_comes_back_within_its_lag_is_not_late() -> None:
    """A quiet source is assumed to send nothing older than its last delivery lag plus the
    watermark, so records wait for it that long."""
    clock = MonotonicClock()
    stop = threading.Event()
    late: list[Record] = []

    class Gps:
        """10 Hz, stamped 50 ms before delivery; quiet from 0.3 s to 0.8 s."""

        meta = SourceMeta(name="gps", kind="live")

        def records(self) -> Iterator[Record]:
            t0 = time.monotonic()
            while not stop.is_set():
                el = time.monotonic() - t0
                if not 0.3 <= el < 0.8:
                    yield _s(clock.now_ns() - 50_000_000, "gps.speed")
                time.sleep(0.1)
                if el > 1.2:
                    return

    class Can:
        """100 Hz, stamped 300 ms before delivery, as the live CAN session holds records."""

        meta = SourceMeta(name="can", kind="live")

        def records(self) -> Iterator[Record]:
            t0 = time.monotonic()
            while time.monotonic() - t0 < 1.3:
                yield _s(clock.now_ns() - 300_000_000, "obd.speed")
                time.sleep(0.01)

    try:
        got = list(merge_live([Can(), Gps()], clock, on_late=late.append))
    finally:
        stop.set()
    times = [record_time(r) for r in got]
    assert times == sorted(times)
    assert late == []
    assert {r.signal for r in got if isinstance(r, Sample)} == {"gps.speed", "obd.speed"}


def test_a_source_error_reaches_the_merge() -> None:
    class Broken:
        meta = SourceMeta(name="imu", kind="live")

        def records(self) -> Iterator[Record]:
            yield _s(1, "imu")
            raise OSError("device unplugged")

    with pytest.raises(OSError, match="device unplugged"):
        list(merge_live([Broken(), ListSource("can", [_s(2, "can")])], StepClock()))


def test_a_source_that_goes_back_in_time_is_refused() -> None:
    bad = ListSource("gps", [_s(20, "gps"), _s(10, "gps")])
    with pytest.raises(ValueError, match="source gps went back in time"):
        list(merge_live([bad], StepClock()))


def test_arguments_are_checked() -> None:
    with pytest.raises(ValueError):
        merge_live([], StepClock(), watermark_ns=-1)
    with pytest.raises(ValueError):
        merge_live([], StepClock(), max_queued=0)
    assert list(merge_live([], StepClock())) == []

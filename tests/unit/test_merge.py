"""Stream merge: time order, stable ties by source order, laziness, and order checks."""

from __future__ import annotations

import itertools
from collections.abc import Iterator

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from photoncar.pipeline.merge import merge_sources, merge_streams
from photoncar.records import Event, Lap, ObdPoll, Record, Sample, record_time
from photoncar.sources.base import SourceMeta


class ListSource:
    def __init__(self, name: str, records: list[Record]) -> None:
        self._meta = SourceMeta(name=name, kind="synthetic")
        self._records = records

    @property
    def meta(self) -> SourceMeta:
        return self._meta

    def records(self) -> Iterator[Record]:
        yield from self._records


def _s(t: int, signal: str, value: float = 0.0) -> Sample:
    return Sample(t, signal, value)


def test_merge_orders_by_record_time() -> None:
    a = ListSource("a", [_s(0, "a"), _s(30, "a"), _s(60, "a")])
    b = ListSource("b", [_s(10, "b"), _s(20, "b"), _s(90, "b")])
    c = ListSource("c", [_s(5, "c"), _s(70, "c")])
    out = list(merge_sources([a, b, c]))
    assert [(r.t_ns, r.signal) for r in out if isinstance(r, Sample)] == [
        (0, "a"),
        (5, "c"),
        (10, "b"),
        (20, "b"),
        (30, "a"),
        (60, "a"),
        (70, "c"),
        (90, "b"),
    ]


def test_ties_keep_source_order_then_order_within_a_source() -> None:
    a = ListSource("a", [_s(10, "a", 1), _s(10, "a", 2), _s(20, "a", 3)])
    b = ListSource("b", [_s(10, "b", 1), _s(20, "b", 2)])
    c = ListSource("c", [_s(10, "c", 1), _s(10, "c", 2)])
    out = [r for r in merge_sources([a, b, c]) if isinstance(r, Sample)]
    assert [(r.t_ns, r.signal, r.value) for r in out] == [
        (10, "a", 1),
        (10, "a", 2),
        (10, "b", 1),
        (10, "c", 1),
        (10, "c", 2),
        (20, "a", 3),
        (20, "b", 2),
    ]
    out_rev = [r for r in merge_sources([c, b, a]) if isinstance(r, Sample)]
    assert [r.signal for r in out_rev if r.t_ns == 10] == ["c", "c", "b", "a", "a"]


def test_merge_uses_record_time_for_every_type() -> None:
    poll = ObdPoll(
        req_t_ns=40, resp_t_ns=55, pid=0x0C, ecu=0x7E8, status="ok", raw=b"", value=1.0, unit="rpm"
    )
    ev = Event("behavior.corner", "behavior.corner", 1, 0, 50, 90, 0.4, 0.4, "g", "Corner.")
    lap = Lap("sf", 1, 52, 99, 47, True)
    out = list(merge_streams([[poll], [ev, lap], [_s(53, "x")]]))
    assert out == [ev, lap, _s(53, "x"), poll]  # 50, 52, 53, 55 (poll sorts on its response)


def test_merge_is_lazy_on_endless_sources() -> None:
    def ticks(step: int, name: str) -> Iterator[Record]:
        for k in itertools.count():
            yield _s(k * step, name)

    first = list(itertools.islice(merge_streams([ticks(3, "a"), ticks(5, "b")]), 6))
    assert [(r.t_ns, r.signal) for r in first if isinstance(r, Sample)] == [
        (0, "a"),
        (0, "b"),
        (3, "a"),
        (5, "b"),
        (6, "a"),
        (9, "a"),
    ]


def test_a_source_going_back_in_time_is_refused_by_name() -> None:
    bad = ListSource("gps0", [_s(10, "g"), _s(5, "g")])
    good = ListSource("imu0", [_s(0, "i"), _s(20, "i")])
    with pytest.raises(ValueError, match="gps0 went back in time"):
        list(merge_sources([good, bad]))
    with pytest.raises(ValueError, match="names must match"):
        merge_streams([[_s(0, "a")]], ["a", "b"])


@settings(max_examples=150, deadline=None, derandomize=True)
@given(st.lists(st.lists(st.integers(0, 50), max_size=30), min_size=1, max_size=5))
def test_merge_equals_a_stable_sort_of_the_concatenation(times: list[list[int]]) -> None:
    streams: list[list[Record]] = [
        [_s(t, f"s{i}", float(j)) for j, t in enumerate(sorted(ts))] for i, ts in enumerate(times)
    ]
    merged = list(merge_streams(streams))
    expected = sorted(itertools.chain.from_iterable(streams), key=record_time)  # stable
    assert merged == expected

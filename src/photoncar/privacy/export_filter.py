"""Drop every record of every stream that touches a privacy trim window (hard rule 7).

A record's time extent is a single instant for point records, the full window for events
(including every citation window) and laps, and request to response for OBD polls. A record
is dropped when its extent overlaps any trim window, endpoints included.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping

from photoncar.privacy.zones import Window, merge_windows
from photoncar.records import Event, Lap, ObdPoll


def record_extent(record: object) -> tuple[int, int]:
    """The closed time interval a record covers, in record-time nanoseconds."""
    if isinstance(record, Event):
        lo, hi = record.t_start_ns, record.t_end_ns
        for c in record.citations:
            lo = min(lo, c.t_start_ns)
            hi = max(hi, c.t_end_ns)
        return lo, hi
    if isinstance(record, Lap):
        return record.t_start_ns, record.t_end_ns
    if isinstance(record, ObdPoll):
        end = record.resp_t_ns if record.resp_t_ns is not None else record.req_t_ns
        return min(record.req_t_ns, end), max(record.req_t_ns, end)
    t = getattr(record, "t_ns", None)
    if not isinstance(t, int):
        raise TypeError(f"cannot place {type(record).__name__} on the timeline: no integer t_ns")
    return t, t


class WindowSet:
    """Merged windows with a fast overlap test."""

    def __init__(self, windows: Iterable[Window]) -> None:
        self.windows: list[Window] = merge_windows(windows)
        self._starts = [w[0] for w in self.windows]

    def overlaps(self, lo: int, hi: int) -> bool:
        """True when the closed interval [lo, hi] touches any window."""
        i = bisect_right(self._starts, hi) - 1
        return i >= 0 and self.windows[i][1] >= lo

    def contains_record(self, record: object) -> bool:
        lo, hi = record_extent(record)
        return self.overlaps(lo, hi)


def filter_for_export[T](
    streams: Mapping[str, Iterable[T]], windows: Iterable[Window] | WindowSet
) -> dict[str, list[T]]:
    """Return the same streams with every record inside a window removed."""
    ws = windows if isinstance(windows, WindowSet) else WindowSet(windows)
    return {name: [r for r in recs if not ws.contains_record(r)] for name, recs in streams.items()}

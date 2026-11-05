"""Live capture from a listen-only SocketCAN interface. This module never transmits.

Time base (docs/phase-1-plan.md section 5): each frame keeps the kernel receive timestamp
(``msg.timestamp``, CLOCK_REALTIME seconds from SO_TIMESTAMPNS), mapped onto the monotonic
timeline through an offset (CLOCK_REALTIME minus CLOCK_MONOTONIC) sampled at 1 Hz. Frame
times never go backwards and never pass the clock's present.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator

import can

from photoncar.clock import Clock
from photoncar.records import CanFrame, Record
from photoncar.safety.listen_only import ListenOnlyStatus, check_listen_only
from photoncar.sources.base import SourceMeta
from photoncar.units import NS_PER_S

log = logging.getLogger(__name__)

RECV_TIMEOUT_S = 0.2
OFFSET_SAMPLE_NS = NS_PER_S
"""Resample the realtime to monotonic offset once per second (plan section 5: 1 Hz)."""


class ListenOnlyError(RuntimeError):
    """The capture interface is not confirmed listen-only, so capture refuses to start."""


class SocketCanSource:
    """Yields every frame seen on ``ifname`` as a CanFrame on the monotonic timeline.

    Refuses to start unless ``check_listen_only(ifname)`` is ok. A bus injected for tests
    skips that check only when ``allow_unchecked_for_tests=True``.

    ``on_ready`` runs once the bus is open and checked, before the first read. ``on_error_frame``
    gets the time of each error frame (error frames are counted, not yielded).
    ``realtime_ns`` reads CLOCK_REALTIME in nanoseconds; tests inject it.
    """

    def __init__(
        self,
        ifname: str,
        clock: Clock,
        *,
        bus: can.BusABC | None = None,
        allow_unchecked_for_tests: bool = False,
        bus_index: int = 0,
        check: Callable[[str], ListenOnlyStatus] = check_listen_only,
        realtime_ns: Callable[[], int] = time.time_ns,
        on_ready: Callable[[], None] | None = None,
        on_error_frame: Callable[[int], None] | None = None,
    ) -> None:
        self._ifname = ifname
        self._clock = clock
        self._bus = bus
        self._unchecked = bus is not None and allow_unchecked_for_tests
        self._bus_index = bus_index
        self._check = check
        self._realtime_ns = realtime_ns
        self._on_ready = on_ready
        self._on_error_frame = on_error_frame
        self._stop = threading.Event()
        self._offset_ns: int | None = None
        self._offset_at_ns = 0
        self._last_t_ns: int | None = None
        self.error_frames = 0
        self.unstamped_frames = 0
        self._require_listen_only()

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name=f"socketcan:{self._ifname}", kind="live")

    def _require_listen_only(self) -> None:
        if self._unchecked:
            return
        status = self._check(self._ifname)
        if not status.ok:
            raise ListenOnlyError(
                f"{self._ifname}: {status.reason}. Capture runs only on a listen-only interface."
            )

    def stop(self) -> None:
        self._stop.set()

    def records(self) -> Iterator[Record]:
        self._require_listen_only()
        owned = self._bus is None
        bus = self._bus if self._bus is not None else self._open()
        try:
            if self._on_ready is not None:
                self._on_ready()
            while not self._stop.is_set():
                msg = bus.recv(timeout=RECV_TIMEOUT_S)
                if msg is None:
                    continue
                t_ns = self.stamp(msg)
                if msg.is_error_frame:
                    self.error_frames += 1
                    if self._on_error_frame is not None:
                        self._on_error_frame(t_ns)
                    continue
                yield CanFrame(
                    t_ns=t_ns,
                    bus=self._bus_index,
                    arb_id=msg.arbitration_id,
                    is_ext=msg.is_extended_id,
                    dlc=msg.dlc,
                    data=b"" if msg.is_remote_frame else bytes(msg.data),
                )
        finally:
            if owned:
                bus.shutdown()

    def _sample_offset(self) -> int:
        before = self._clock.now_ns()
        real = self._realtime_ns()
        after = self._clock.now_ns()
        offset = real - (before + after) // 2
        self._offset_ns = offset
        self._offset_at_ns = after
        return offset

    def stamp(self, msg: can.Message) -> int:
        """The frame's kernel receive time on the monotonic timeline."""
        now = self._clock.now_ns()
        offset = self._offset_ns
        if offset is None or now - self._offset_at_ns >= OFFSET_SAMPLE_NS:
            offset = self._sample_offset()
        last = self._last_t_ns
        ts = float(msg.timestamp or 0.0)
        if ts > 0:
            t_ns = round(ts * NS_PER_S) - offset
            if t_ns > now or (last is not None and t_ns < last):
                # The realtime clock stepped since the last sample. Resample once, then clamp.
                t_ns = round(ts * NS_PER_S) - self._sample_offset()
        else:
            self.unstamped_frames += 1
            t_ns = now
        t_ns = min(t_ns, now)
        if last is not None:
            t_ns = max(t_ns, last)
        self._last_t_ns = t_ns
        return t_ns

    def _open(self) -> can.BusABC:
        return can.Bus(interface="socketcan", channel=self._ifname, receive_own_messages=False)

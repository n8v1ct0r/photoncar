"""OBD-II polling. The only module in Photoncar that opens the request bus or calls send.

Every frame goes through ``TxGuard.check_and_consume`` immediately before ``send``, in
``ObdPoller._transmit``; a test pins that down by walking this file's syntax tree.

``open_live_can_session`` is the only public way to poll a real bus. It checks that ``can0``
is listen-only and ``can1`` is one-shot with its error counters at 0, opens both, and wires
every frame and error frame on ``can0`` into the guard's audit before the first request can go
out. Both interfaces are judged again right before the first request, and while the session
runs a link watch re-judges them every ``LINK_WATCH_S``. An ``ObdPoller`` built
anywhere else refuses to start unless it is on a python-can virtual bus in a test.

Flow: discover support with 01 00 (then 01 20 and 01 40 when the previous bitmap points to
them) on the functional ID 0x7DF, then poll the supported PIDs from ``config [obd]`` on the
physical ID 0x7E0. Fast PIDs run at ``fast_hz_each`` each; the slow group shares
``slow_hz_total`` round-robin. One request is in flight at a time. The poller is
step-driven (``step(now_ns)``) so tests run on a simulated clock; ``run()`` is the thin
real-time loop around it.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import queue
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import can
from can.interfaces.virtual import VirtualBus

from photoncar.clock import Clock
from photoncar.config import Config
from photoncar.decode.obd_pids import (
    NRC_RESPONSE_PENDING,
    PID_DEFS,
    SERVICE_CURRENT_DATA,
    SUPPORT_PIDS,
    decode_pid,
    parse_negative_response,
    parse_response,
    request_frame,
    supported_pids,
)
from photoncar.records import CanFrame, ObdPoll, ObdStatus, Record, Sample, record_time
from photoncar.safety.listen_only import (
    LinkQueryError,
    ListenOnlyStatus,
    Runner,
    check_listen_only,
    query_link,
)
from photoncar.safety.request_link import (
    ErrorCounters,
    LinkFault,
    check_one_shot,
    parse_error_counters,
    recheck_request_link,
)
from photoncar.safety.tx_guard import (
    FUNCTIONAL_ID,
    HARD_MAX_REQUEST_HZ,
    PHYSICAL_IDS,
    TxGuard,
    TxRateLimited,
    TxViolation,
)
from photoncar.signals import OBD_PID_TO_SIGNAL
from photoncar.sources.base import SourceMeta
from photoncar.sources.socketcan import ListenOnlyError, SocketCanSource
from photoncar.units import NS_PER_S, s_to_ns

log = logging.getLogger(__name__)

DISCOVERY_RETRY_S = 5.0
"""Wait before asking for the support bitmaps again when no ECU answered 01 00."""
MAX_WAIT_S = 0.01
"""Longest the real-time loop blocks between steps."""
SEND_TIMEOUT_S = 0.05
REORDER_WATERMARK_S = 0.2
"""Hold live records this long before yielding, so capture and OBD merge in time order.

The plan's reorder watermark (docs/phase-1-plan.md section 13). The session adds the OBD
response timeout on top, since a timed-out poll is reported that much after its request.
"""
LINK_WATCH_S = 1.0
"""How often a live session re-judges can1 (modes, error counters) and can0 (listen-only)."""
LINK_WATCH_STOPPED = "CAN link watch stopped"

BusFactory = Callable[[str], can.BusABC]


class ObdPollingOff(RuntimeError):
    """The request bus stays down: polling is off in config, or the config is unsafe."""


class LiveCanUnavailable(RuntimeError):
    """A live CAN session cannot start: an interface check failed or a bus did not open."""


class _SessionKey:
    """Proof that a caller is ``open_live_can_session``. Never used outside this module."""


_SESSION_KEY = _SessionKey()


def _open_socketcan_bus(channel: str) -> can.BusABC:
    return can.Bus(interface="socketcan", channel=channel, receive_own_messages=False)


def _open_request_bus(cfg: Config, factory: BusFactory) -> can.BusABC:
    """Open the request interface (``can1``). Refuses unless ``[can] obd_polling = true``."""
    can_cfg = cfg.section("can")
    if can_cfg.get("obd_polling") is not True:
        raise ObdPollingOff("OBD polling is off ([can] obd_polling). The request bus stays down.")
    request_if = str(can_cfg["request_interface"])
    if request_if == str(can_cfg["capture_interface"]):
        raise ObdPollingOff("The request interface must not be the listen-only capture interface.")
    return factory(request_if)


@dataclass(slots=True)
class _Stream:
    """A periodic request slot. ``pids`` rotate round-robin; one entry means a single PID."""

    pids: tuple[int, ...]
    period_ns: int
    next_due_ns: int
    idx: int = 0

    def peek(self) -> int:
        return self.pids[self.idx]

    def advance(self, now_ns: int) -> None:
        self.idx = (self.idx + 1) % len(self.pids)
        self.next_due_ns += self.period_ns
        if self.next_due_ns < now_ns - self.period_ns:
            # More than a period behind: skip the missed slots rather than burst.
            self.next_due_ns = now_ns


@dataclass(frozen=True, slots=True)
class _Pending:
    pid: int
    arb_id: int
    req_t_ns: int
    deadline_ns: int
    discovery: bool


class ObdPoller:
    """Polls service 01 PIDs through a TxGuard. Yields ObdPoll and Sample records.

    Built only by ``open_live_can_session``. Tests pass ``virtual_bus_for_tests=True`` with a
    python-can virtual bus; any other bus refuses.
    """

    def __init__(
        self,
        bus: can.BusABC,
        guard: TxGuard,
        cfg: Config,
        clock: Clock,
        *,
        discovery_retry_s: float = DISCOVERY_RETRY_S,
        may_send: Callable[[], bool] | None = None,
        virtual_bus_for_tests: bool = False,
        _key: object = None,
    ) -> None:
        if _key is not _SESSION_KEY:
            if not virtual_bus_for_tests:
                raise ObdPollingOff("OBD polling runs only inside open_live_can_session.")
            if not isinstance(bus, VirtualBus):
                raise ObdPollingOff("virtual_bus_for_tests needs a python-can virtual bus.")
        obd = cfg.section("obd")
        if int(obd["max_in_flight"]) != 1:
            raise ValueError("max_in_flight must be 1")
        self._functional_id = int(obd["functional_id"])
        self._physical_id = int(obd["physical_id"])
        if self._functional_id != FUNCTIONAL_ID:
            raise ValueError("functional_id must be 0x7DF")
        if self._physical_id not in PHYSICAL_IDS:
            raise ValueError("physical_id must be in 0x7E0..0x7E7")
        self._reply_id = self._physical_id + 8
        self._fast_cfg = tuple(int(p) for p in obd["fast_pids"])
        self._slow_cfg = tuple(int(p) for p in obd["slow_pids"])
        self._fast_hz = float(obd["fast_hz_each"])
        self._slow_hz = float(obd["slow_hz_total"])
        max_hz = min(float(obd["max_request_hz"]), HARD_MAX_REQUEST_HZ)
        if max_hz <= 0 or self._fast_hz <= 0 or self._slow_hz <= 0:
            raise ValueError("poll rates must be positive")
        nominal = len(self._fast_cfg) * self._fast_hz + (self._slow_hz if self._slow_cfg else 0)
        if nominal > max_hz:
            raise ValueError(f"poll schedule needs {nominal} Hz, over the {max_hz} Hz cap")
        if guard.rate_hz > max_hz:
            raise ValueError("the guard rate is over max_request_hz")
        unknown = [p for p in (*self._fast_cfg, *self._slow_cfg) if p not in PID_DEFS]
        if unknown:
            raise ValueError(f"no decoder for PIDs {unknown}")
        self._slot_ns = round(NS_PER_S / max_hz)
        self._timeout_ns = s_to_ns(float(obd["response_timeout_s"]))
        self._retry_ns = s_to_ns(discovery_retry_s)
        self._bus = bus
        self._guard = guard
        self._clock = clock
        self._may_send = may_send
        self._pending: _Pending | None = None
        self._discovery_pid: int | None = 0x00
        self._discovery_due_ns = clock.now_ns()
        self._streams: list[_Stream] = []
        self._rx: deque[tuple[int, can.Message]] = deque()
        self._stop = threading.Event()
        self.bitmaps: dict[int, bytes] = {}
        self.supported: frozenset[int] | None = None
        self.sent = 0

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name="obd", kind="live")

    @property
    def polled_pids(self) -> tuple[int, ...]:
        """The PIDs on the schedule (empty until discovery finishes)."""
        return tuple(p for s in self._streams for p in s.pids)

    # The step-driven core.

    def step(self, now_ns: int) -> list[Record]:
        """Read replies, expire a late request, then send the next request if one is due."""
        out: list[Record] = []
        while self._rx:
            t_ns, stashed = self._rx.popleft()
            self._on_message(t_ns, stashed, out)
        while (msg := self._bus.recv(timeout=0.0)) is not None:
            self._on_message(now_ns, msg, out)
        pending = self._pending
        if pending is not None and now_ns >= pending.deadline_ns:
            self._complete(pending, now_ns, "timeout", b"", out)
        if self._pending is None and not self._guard.disabled:
            self._maybe_send(now_ns)
        return out

    def _on_message(self, t_ns: int, msg: can.Message, out: list[Record]) -> None:
        if msg.is_error_frame:
            self._guard.disable("error frame on the request bus")
            return
        pending = self._pending
        if pending is None:
            return
        if t_ns > pending.deadline_ns:
            self._complete(pending, t_ns, "timeout", b"", out)
            return
        if msg.arbitration_id != self._reply_id:
            return
        parsed = parse_response(msg)
        if parsed is not None:
            pid, data = parsed
            if pid == pending.pid:
                self._complete(pending, t_ns, "ok", data, out)
            return
        nrc = parse_negative_response(msg)
        if nrc is not None and nrc[0] == SERVICE_CURRENT_DATA and nrc[1] != NRC_RESPONSE_PENDING:
            raw = bytes([0x7F, nrc[0], nrc[1]])
            self._complete(pending, t_ns, "nrc", raw, out)

    def _complete(
        self, p: _Pending, t_ns: int, status: ObdStatus, raw: bytes, out: list[Record]
    ) -> None:
        self._pending = None
        answered = status != "timeout"
        value = decode_pid(p.pid, raw) if status == "ok" else None
        out.append(
            ObdPoll(
                req_t_ns=p.req_t_ns,
                resp_t_ns=t_ns if answered else None,
                pid=p.pid,
                ecu=self._reply_id if answered else None,
                status=status,
                raw=raw,
                value=value,
                unit=PID_DEFS[p.pid].unit,
            )
        )
        if value is not None and p.pid not in SUPPORT_PIDS:
            out.append(Sample(t_ns=t_ns, signal=OBD_PID_TO_SIGNAL[p.pid], value=value))
        if p.discovery:
            self._on_discovery(p.pid, status, raw, t_ns)

    def _on_discovery(self, pid: int, status: ObdStatus, raw: bytes, t_ns: int) -> None:
        if status == "ok":
            self.bitmaps[pid] = raw
            nxt = pid + 0x20
            if nxt in SUPPORT_PIDS and nxt in supported_pids({pid: raw}):
                self._discovery_pid = nxt
                self._discovery_due_ns = t_ns
                return
            self._finish_discovery(t_ns)
        elif pid == 0x00:
            self._discovery_due_ns = t_ns + self._retry_ns
            log.info("no support bitmap from 0x%03X (%s); retrying", self._reply_id, status)
        else:
            self._finish_discovery(t_ns)

    def _finish_discovery(self, t_ns: int) -> None:
        supported = frozenset(supported_pids(self.bitmaps))
        self.supported = supported
        self._discovery_pid = None
        fast = [p for p in self._fast_cfg if p in supported]
        slow = tuple(p for p in self._slow_cfg if p in supported)
        fast_period = round(NS_PER_S / self._fast_hz)
        streams = [
            _Stream((pid,), fast_period, t_ns + i * self._slot_ns) for i, pid in enumerate(fast)
        ]
        if slow:
            slow_period = round(NS_PER_S / self._slow_hz)
            streams.append(_Stream(slow, slow_period, t_ns + len(fast) * self._slot_ns))
        self._streams = streams
        log.info("supported PIDs found; polling %s", [f"0x{p:02X}" for p in self.polled_pids])

    def _maybe_send(self, now_ns: int) -> None:
        stream: _Stream | None = None
        if self._discovery_pid is not None:
            if now_ns < self._discovery_due_ns:
                return
            pid, arb_id = self._discovery_pid, self._functional_id
        else:
            due = [s for s in self._streams if s.next_due_ns <= now_ns]
            if not due:
                return
            stream = min(due, key=lambda s: s.next_due_ns)
            pid, arb_id = stream.peek(), self._physical_id
        if not self._transmit(request_frame(pid, arb_id)):
            return
        self._pending = _Pending(pid, arb_id, now_ns, now_ns + self._timeout_ns, stream is None)
        if stream is not None:
            stream.advance(now_ns)

    def _transmit(self, msg: can.Message) -> bool:
        """The single send site. The guard checks the frame and the rate first."""
        if self._guard.disabled:
            return False
        if self._may_send is not None and not self._may_send():
            return False
        try:
            self._guard.check_and_consume(msg)
        except TxRateLimited:
            return False
        except TxViolation as exc:
            log.error("request refused, TX is off: %s", exc.reason)
            return False
        try:
            self._bus.send(msg, timeout=SEND_TIMEOUT_S)
        except can.CanError as exc:
            self._guard.disable(f"send failed: {exc}")
            return False
        self.sent += 1
        return True

    # The real-time loop.

    def _wait_s(self, now_ns: int) -> float:
        if self._pending is not None:
            target = self._pending.deadline_ns
        elif self._discovery_pid is not None:
            target = max(self._discovery_due_ns, now_ns + self._guard.ns_until_token())
        elif self._streams:
            next_due = min(s.next_due_ns for s in self._streams)
            target = max(next_due, now_ns + self._guard.ns_until_token())
        else:
            target = now_ns + s_to_ns(MAX_WAIT_S)
        return min(max(target - now_ns, 0) / NS_PER_S, MAX_WAIT_S)

    def run(self, stop: threading.Event | None = None) -> Iterator[Record]:
        """Poll until ``stop`` is set or TX turns off. Uses the injected clock for times."""
        ev = stop if stop is not None else self._stop
        while not ev.is_set():
            yield from self.step(self._clock.now_ns())
            if self._guard.disabled:
                log.warning("OBD polling stopped: %s", self._guard.reason)
                return
            msg = self._bus.recv(timeout=self._wait_s(self._clock.now_ns()))
            if msg is not None:
                self._rx.append((self._clock.now_ns(), msg))

    def records(self) -> Iterator[Record]:
        return self.run(self._stop)

    def stop(self) -> None:
        self._stop.set()


class LiveCanSession:
    """Capture on ``can0`` and OBD polling on ``can1``, wired so the audit sees every frame.

    Build it with ``open_live_can_session``; the constructor refuses any other caller.
    ``records()`` starts capture first and polling only once the capture audit is live, then
    yields CanFrame, ObdPoll and Sample records in time order (each held for the reorder
    watermark plus the response timeout). TX turns off for the rest of the session on any
    audit mismatch, an error frame on ``can0``, a request the capture bus did not see, a
    ``can1`` or ``can0`` state rejected right before the first request or by the link watch
    (``_recheck_links``), if that watch fails or ends, or if capture stops. Capture runs until
    ``stop()``.
    """

    def __init__(
        self,
        key: object,
        *,
        cfg: Config,
        clock: Clock,
        guard: TxGuard,
        capture_if: str,
        request_if: str,
        capture_bus: can.BusABC,
        request_bus: can.BusABC,
        check_capture: Callable[[str], ListenOnlyStatus],
        read_request_link: Callable[[], Mapping[str, Any]],
        counters: ErrorCounters,
    ) -> None:
        if key is not _SESSION_KEY:
            raise LiveCanUnavailable("Open a live CAN session with open_live_can_session.")
        self.guard = guard
        self._clock = clock
        self._name = f"live:{capture_if}+{request_if}"
        self._capture_if = capture_if
        self._request_if = request_if
        self._bitrate = int(cfg.get("can.bitrate"))
        self._buses = (capture_bus, request_bus)
        self._check_capture = check_capture
        self._read_request_link = read_request_link
        self._counters = counters
        response_timeout_ns = s_to_ns(float(cfg.get("obd.response_timeout_s")))
        self._hold_ns = s_to_ns(REORDER_WATERMARK_S) + response_timeout_ns
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._poll_stop = threading.Event()
        # Reentrant: the CLI calls stop() from a signal handler, which can run on a thread
        # that is already inside _shutdown (a second Ctrl+C).
        self._shutdown_lock = threading.RLock()
        self._shut = False
        self._started = False
        self._threads: dict[str, threading.Thread] = {}
        self._out: queue.SimpleQueue[Record] = queue.SimpleQueue()
        self._capture = SocketCanSource(
            capture_if,
            clock,
            bus=capture_bus,
            check=check_capture,
            on_ready=self._on_capture_ready,
            on_error_frame=guard.audit_error_frame,
        )
        self._poller = ObdPoller(
            request_bus, guard, cfg, clock, may_send=self._may_send, _key=_SESSION_KEY
        )

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name=self._name, kind="live")

    @property
    def polling(self) -> bool:
        """True while the poller thread runs."""
        t = self._threads.get("poll")
        return t is not None and t.is_alive()

    def records(self) -> Iterator[Record]:
        if self._started:
            raise RuntimeError("records() runs once per session")
        self._started = True
        try:
            if self._stop.is_set():
                return
            self._start_threads()
            heap: list[tuple[int, int, Record]] = []
            seq = itertools.count()
            while not self._stop.is_set() and self._running():
                self._pull(heap, seq, wait=True)
                cutoff = self._clock.now_ns() - self._hold_ns
                while heap and heap[0][0] <= cutoff:
                    yield heapq.heappop(heap)[2]
            self._shutdown()
            self._pull(heap, seq, wait=False)
            while heap:
                yield heapq.heappop(heap)[2]
        finally:
            self._shutdown()

    def stop(self) -> None:
        """Stop polling first, then capture, and close both buses. Safe to call twice."""
        self._shutdown()

    # Threads.

    def _running(self) -> bool:
        """False once capture and polling have both ended (capture failed, TX is off)."""
        return any(self._threads[n].is_alive() for n in ("capture", "poll"))

    def _start_threads(self) -> None:
        # The link watch starts first: the poller refuses to send while it is not alive.
        targets: dict[str, Callable[[], None]] = {
            "links": self._watch_links,
            "capture": self._capture_loop,
            "poll": self._poll_loop,
        }
        for name, target in targets.items():
            t = threading.Thread(target=target, name=f"photoncar-{name}", daemon=True)
            self._threads[name] = t
            t.start()

    def _on_capture_ready(self) -> None:
        self.guard.mark_audit_live()
        self._ready.set()

    def _capture_loop(self) -> None:
        try:
            for rec in self._capture.records():
                if isinstance(rec, CanFrame):
                    self.guard.audit(rec)
                self._out.put(rec)
        except Exception as exc:
            log.error("capture on the listen-only bus failed: %s", exc)
        finally:
            if not self._stop.is_set():
                self.guard.disable("audit: capture on the listen-only bus stopped")

    def _poll_loop(self) -> None:
        capture = self._threads["capture"]
        while not self._ready.wait(MAX_WAIT_S):
            if self._stop.is_set() or not capture.is_alive():
                return
        try:
            for rec in self._poller.run(self._poll_stop):
                self._out.put(rec)
        except Exception as exc:
            log.error("OBD polling failed: %s", exc)
            self.guard.disable(f"OBD polling failed: {exc}")

    def _watch_links(self) -> None:
        """Re-judge can1 and can0 every LINK_WATCH_S (``_recheck_links``). Fails closed (H2).

        Any way out before ``stop()`` turns TX off: a rejected state, an unreadable link, any
        other error (a failed fork in ``subprocess.run``), or an exit no except clause catches.
        """
        prev = self._counters
        try:
            while not self._stop.wait(LINK_WATCH_S):
                if self.guard.disabled:
                    return
                try:
                    prev = self._recheck_links(prev)
                except LinkFault as exc:
                    self.guard.disable(exc.reason)
                    return
        except Exception as exc:
            log.error("the CAN link watch failed: %r", exc)
            detail = str(exc) or type(exc).__name__
            self.guard.disable(f"CAN link watch failed: {detail}")
        finally:
            if not self._stop.is_set():
                self.guard.disable(LINK_WATCH_STOPPED)

    def _recheck_links(self, prev: ErrorCounters) -> ErrorCounters:
        """One tick: every startup check again, plus the can1 error counters.

        can1: up, one-shot, restart-ms 0, not listen-only, the configured bitrate, and no
        error counter fell (a down and up reset) or rose since ``prev``. can0: up and
        listen-only. Returns the new can1 counters; raises LinkFault on any failure.
        """
        try:
            link = self._read_request_link()
        except LinkQueryError as exc:
            raise LinkFault(f"{self._request_if} state is unreadable: {exc.reason}") from exc
        counters = recheck_request_link(self._request_if, link, self._bitrate, prev)
        listen = self._check_capture(self._capture_if)
        if not listen.ok:
            raise LinkFault(
                f"{self._capture_if} is no longer confirmed listen-only ({listen.reason})"
            )
        return counters

    def _may_send(self) -> bool:
        """The poller asks before every request (on its own thread): none goes out without the
        link watch, and until the first request has gone out, each attempt waits for a full
        check of both links made right then."""
        if self._stop.is_set():
            return False
        watch = self._threads.get("links")
        if watch is None or not watch.is_alive():
            self.guard.disable(LINK_WATCH_STOPPED)
            return False
        if self._poller.sent == 0:
            return self._check_before_first_request()
        return True

    def _check_before_first_request(self) -> bool:
        """``_recheck_links`` against the counters read at open, on the poller's thread right
        before the first request. The watch's first tick comes ``LINK_WATCH_S`` later. Any
        failure turns TX off, with the watch's own words."""
        try:
            self._recheck_links(self._counters)
        except LinkFault as exc:
            self.guard.disable(exc.reason)
            return False
        except Exception as exc:
            log.error("the CAN link check before the first request failed: %r", exc)
            detail = str(exc) or type(exc).__name__
            self.guard.disable(f"CAN link watch failed: {detail}")
            return False
        return True

    def _pull(self, heap: list[tuple[int, int, Record]], seq: Iterator[int], *, wait: bool) -> None:
        try:
            rec = self._out.get(timeout=MAX_WAIT_S) if wait else self._out.get_nowait()
            while True:
                heapq.heappush(heap, (record_time(rec), next(seq), rec))
                rec = self._out.get_nowait()
        except queue.Empty:
            return

    def _shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shut:
                return
            self._shut = True
            self._stop.set()
            self._poll_stop.set()
            if (t := self._threads.get("poll")) is not None:
                t.join()
            self._capture.stop()
            for name in ("capture", "links"):
                if (t := self._threads.get(name)) is not None:
                    t.join()
            for bus in self._buses:
                bus.shutdown()


def open_live_can_session(
    cfg: Config,
    clock: Clock,
    *,
    runner: Runner | None = None,
    bus_factory: BusFactory | None = None,
    system: str | None = None,
) -> LiveCanSession:
    """The only public way to poll a real bus. Checks both interfaces, then opens them.

    Raises ObdPollingOff when ``[can] obd_polling`` is not true or the config is unsafe, and
    LiveCanUnavailable when ``can0`` is not listen-only, ``can1`` is not one-shot, the ``can1``
    error counters cannot be read or are not 0, a bus does not open, or the OS is not Linux.
    Counters above 0 count down on their own as frames go through, and the link watch reads
    any fall as a reset (it cannot tell the two apart), so such a session would turn TX off by
    itself. ``runner``,
    ``bus_factory`` and ``system`` exist for tests (captured ``ip`` output, virtual buses, a
    pretend OS).
    """
    can_cfg = cfg.section("can")
    if can_cfg.get("obd_polling") is not True:
        raise ObdPollingOff("OBD polling is off ([can] obd_polling). The request bus stays down.")
    capture_if = str(can_cfg["capture_interface"])
    request_if = str(can_cfg["request_interface"])
    if request_if == capture_if:
        raise ObdPollingOff("The request interface must not be the listen-only capture interface.")

    def check_capture(ifname: str) -> ListenOnlyStatus:
        return check_listen_only(ifname, runner=runner, system=system)

    def read_request_link() -> Mapping[str, Any]:
        return query_link(request_if, statistics=True, runner=runner, system=system)

    listen = check_capture(capture_if)
    if not listen.ok:
        raise LiveCanUnavailable(
            f"Live CAN is off: {capture_if} is not confirmed listen-only ({listen.reason})."
        )
    one_shot = check_one_shot(request_if, int(can_cfg["bitrate"]), runner=runner, system=system)
    if not one_shot.ok:
        raise LiveCanUnavailable(
            f"Live CAN is off: {request_if} is not ready for requests ({one_shot.reason})."
        )
    try:
        counters = parse_error_counters(read_request_link())
    except LinkQueryError as exc:
        raise LiveCanUnavailable(
            f"Live CAN is off: the {request_if} error counters cannot be read ({exc.reason})."
        ) from exc
    if counters.tx != 0 or counters.rx != 0:
        raise LiveCanUnavailable(
            f"Live CAN is off: the {request_if} error counters are not 0 (tx {counters.tx}, "
            f"rx {counters.rx}). They count down on their own. Start again once both read 0."
        )
    try:
        response_timeout_ns = s_to_ns(float(cfg.get("obd.response_timeout_s")))
        guard = TxGuard.from_config(cfg, clock, echo_timeout_ns=response_timeout_ns)
    except ValueError as exc:
        raise ObdPollingOff(f"The request bus stays down: {exc}.") from exc

    factory = bus_factory or _open_socketcan_bus
    opened: list[can.BusABC] = []
    try:
        for ifname in (capture_if, request_if):
            try:
                bus = factory(ifname) if ifname == capture_if else _open_request_bus(cfg, factory)
            except (OSError, can.CanError) as exc:
                raise LiveCanUnavailable(
                    f"Live CAN is off: {ifname} did not open ({exc})."
                ) from exc
            opened.append(bus)
        try:
            return LiveCanSession(
                _SESSION_KEY,
                cfg=cfg,
                clock=clock,
                guard=guard,
                capture_if=capture_if,
                request_if=request_if,
                capture_bus=opened[0],
                request_bus=opened[1],
                check_capture=check_capture,
                read_request_link=read_request_link,
                counters=counters,
            )
        except ListenOnlyError as exc:
            raise LiveCanUnavailable(f"Live CAN is off: {exc}") from exc
        except ValueError as exc:
            raise ObdPollingOff(f"The request bus stays down: {exc}.") from exc
    except BaseException:
        for bus in opened:
            bus.shutdown()
        raise

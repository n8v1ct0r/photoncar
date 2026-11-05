"""The TX allowlist: the only frame shape Photoncar may put on a CAN bus, and the rate cap.

A frame passes only if every one of these holds (docs/phase-1-plan.md section 1):

- 11-bit ID 0x7DF (support bitmaps only) or 0x7E0..0x7E7; never 0x6F1 (BMW tester ID).
- A classic data frame: not RTR, not FD, not an error frame.
- DLC 8 and exactly 8 data bytes: ``[0x02, 0x01, pid, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC]``.
- ``pid`` is in the allowlist.

Everything else is refused, including ISO-TP flow control, service 09 (VIN), service 04
(clear codes), and UDS sessions, security access, writes, routines and tester present.
The first violation latches TX off for the session. Nothing re-enables it.

The rate has a hard ceiling in code (``HARD_MAX_REQUEST_HZ``) that no config value can raise.
The runtime audit checks the frames seen on the listen-only capture bus against the same
validator, against the most the token bucket can emit in any closed 1 s and 10 s window
(``bucket_cap``, no slack), and for retransmits. In a live session it also proves the capture
bus sees the wire: every request must be seen there before the next one may go out. That
rule is why the wire can never show more than the bucket emits: request n is on the wire
before request n + 1 is taken, so N requests on the wire span more than the bucket needs
for N - 1 takes. Only an error in the capture timestamps can close that gap, and then TX
turns off, the safe side.
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from collections.abc import Iterable
from fractions import Fraction

import can

from photoncar.clock import Clock
from photoncar.config import Config
from photoncar.records import CanFrame
from photoncar.units import NS_PER_S

log = logging.getLogger(__name__)

FUNCTIONAL_ID = 0x7DF
PHYSICAL_IDS = range(0x7E0, 0x7E8)
REQUEST_IDS: frozenset[int] = frozenset({FUNCTIONAL_ID, *PHYSICAL_IDS})
FORBIDDEN_IDS: frozenset[int] = frozenset({0x6F1})
AUDITED_IDS: frozenset[int] = REQUEST_IDS | FORBIDDEN_IDS
SUPPORT_PIDS: frozenset[int] = frozenset({0x00, 0x20, 0x40})
SERVICE_CURRENT_DATA = 0x01
PAD = 0xCC
FRAME_LEN = 8

HARD_MAX_REQUEST_HZ = 10.0
"""The request rate ceiling in code: the approved hard rule 2 cap (config obd.max_request_hz)."""
REQUIRED_BURST = 1
"""The token bucket burst. One request at a time, never a batch."""

FORBIDDEN_SERVICES: dict[int, str] = {
    0x04: "service 04 (clear trouble codes) is forbidden",
    0x09: "service 09 (vehicle information, VIN) is forbidden",
    0x10: "UDS 0x10 (session control) is forbidden",
    0x11: "UDS 0x11 (ECU reset) is forbidden",
    0x14: "UDS 0x14 (clear diagnostic information) is forbidden",
    0x27: "UDS 0x27 (security access) is forbidden",
    0x2E: "UDS 0x2E (write data by identifier) is forbidden",
    0x31: "UDS 0x31 (routine control) is forbidden",
    0x3E: "UDS 0x3E (tester present) is forbidden",
}

AUDIT_WINDOWS_S: tuple[int, ...] = (1, 10)
"""On-wire rate windows. The 10 s window catches a small sustained overrun the 1 s one allows."""
RETRANSMIT_GAP_NS = 1_000_000
"""Identical request frames closer than 1 ms on the wire count as a controller retransmit."""


def bucket_cap(max_hz: float, window_s: int, burst: int = REQUIRED_BURST) -> int:
    """The most takes a ``TokenBucket(max_hz, burst)`` allows in any closed ``window_s``.

    ``floor(max_hz * window_s) + burst``, computed exactly: at most ``burst`` tokens wait at
    the start of the window and ``max_hz * window_s`` more arrive inside it. At 10 Hz that is
    11 in 1 s and 101 in 10 s. The runtime audit allows exactly this on the wire.
    """
    return math.floor(Fraction(max_hz) * window_s) + burst


class TxViolation(Exception):
    """A frame that is not an allowlisted service 01 request, or a send while TX is off."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TxRateLimited(Exception):
    """The token bucket is empty. The frame was not sent. This does not latch TX off."""


class TxAuditPending(TxRateLimited):
    """The capture audit has not started, or has not yet seen the last request. Try later."""


def validate_frame(msg: can.Message, allowed_pids: frozenset[int]) -> None:
    """Raise TxViolation unless ``msg`` has exactly the allowed request shape."""
    if msg.is_extended_id:
        raise TxViolation("29-bit IDs are forbidden")
    if msg.is_fd or msg.bitrate_switch or msg.error_state_indicator:
        raise TxViolation("CAN FD frames are forbidden")
    if msg.is_remote_frame:
        raise TxViolation("remote (RTR) frames are forbidden")
    if msg.is_error_frame:
        raise TxViolation("error frames are forbidden")
    arb_id = msg.arbitration_id
    if arb_id in FORBIDDEN_IDS:
        raise TxViolation(f"ID 0x{arb_id:03X} is forbidden (BMW tester ID)")
    if arb_id not in REQUEST_IDS:
        raise TxViolation(f"ID 0x{arb_id:03X} is not an OBD request ID")
    data = bytes(msg.data)
    if msg.dlc != FRAME_LEN or len(data) != FRAME_LEN:
        raise TxViolation(f"DLC must be 8 (got dlc {msg.dlc}, {len(data)} bytes)")
    if data[0] & 0xF0 == 0x30:
        raise TxViolation("ISO-TP flow control frames are forbidden")
    if data[0] & 0xF0 != 0x00:
        raise TxViolation("only ISO-TP single frames are allowed")
    service = data[1]
    if service in FORBIDDEN_SERVICES:
        raise TxViolation(FORBIDDEN_SERVICES[service])
    if service != SERVICE_CURRENT_DATA:
        raise TxViolation(f"service 0x{service:02X} is not allowed (service 01 only)")
    if data[0] != 0x02:
        raise TxViolation("exactly 1 PID per request (length byte must be 0x02)")
    pid = data[2]
    if pid not in allowed_pids:
        raise TxViolation(f"PID 0x{pid:02X} is not in the allowlist")
    if arb_id == FUNCTIONAL_ID and pid not in SUPPORT_PIDS:
        raise TxViolation("functional ID 0x7DF is for support bitmaps only")
    if any(b != PAD for b in data[3:]):
        raise TxViolation("padding bytes 3 to 7 must be 0xCC")


def allowlist_from_config(cfg: Config) -> frozenset[int]:
    """The support bitmaps plus the configured fast and slow PIDs."""
    obd = cfg.section("obd")
    return SUPPORT_PIDS | frozenset(int(p) for p in (*obd["fast_pids"], *obd["slow_pids"]))


class TokenBucket:
    """A token bucket on an injected clock. Exact rational arithmetic, so no float drift.

    Over any window of T seconds, at most ``rate_hz * T + burst`` takes succeed.
    """

    def __init__(self, rate_hz: float, burst: int = 1, *, clock: Clock) -> None:
        if not math.isfinite(rate_hz) or rate_hz <= 0:
            raise ValueError("rate_hz must be a positive finite number")
        if burst < 1:
            raise ValueError("burst must be at least 1")
        self.rate_hz = rate_hz
        self.burst = burst
        self._rate = Fraction(rate_hz)
        self._clock = clock
        self._tokens = Fraction(burst)
        self._last_ns = clock.now_ns()

    def _refill(self) -> None:
        now = self._clock.now_ns()
        if now > self._last_ns:
            gained = Fraction(now - self._last_ns) * self._rate / NS_PER_S
            self._tokens = min(Fraction(self.burst), self._tokens + gained)
            self._last_ns = now

    def try_take(self) -> bool:
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    def ns_until_token(self) -> int:
        """Nanoseconds until the next take can succeed (0 if one is available now)."""
        self._refill()
        if self._tokens >= 1:
            return 0
        return math.ceil((1 - self._tokens) * NS_PER_S / self._rate)


class TxGuard:
    """The gate every send passes through. Latches TX off on the first violation.

    ``echo_timeout_ns`` turns on the live-session rule: no send until the capture audit is
    running (``mark_audit_live``), and each request must be seen on the capture bus before
    the next one may go out. A request not seen within ``echo_timeout_ns`` turns TX off, and
    so does a request on the wire that this guard did not pass.
    """

    def __init__(
        self,
        allowed_pids: Iterable[int],
        rate_hz: float,
        clock: Clock,
        *,
        burst: int = REQUIRED_BURST,
        echo_timeout_ns: int | None = None,
    ) -> None:
        if not 0 < rate_hz <= HARD_MAX_REQUEST_HZ:
            raise ValueError(
                f"rate_hz must be over 0 and at most {HARD_MAX_REQUEST_HZ} Hz (got {rate_hz})"
            )
        if burst != REQUIRED_BURST:
            raise ValueError(f"burst must be {REQUIRED_BURST} (got {burst})")
        if echo_timeout_ns is not None and echo_timeout_ns <= 0:
            raise ValueError("echo_timeout_ns must be positive")
        pids = frozenset(allowed_pids)
        bad = [p for p in pids if not 0 <= p <= 0xFF]
        if bad:
            raise ValueError(f"PIDs out of range: {bad}")
        self.allowed_pids = pids
        self.rate_hz = rate_hz
        self.burst = burst
        self._clock = clock
        self._bucket = TokenBucket(rate_hz, burst, clock=clock)
        self._lock = threading.RLock()
        self._disabled = False
        self._reason: str | None = None
        self._windows = [
            (w, w * NS_PER_S, bucket_cap(rate_hz, w, burst), deque[int]()) for w in AUDIT_WINDOWS_S
        ]
        self._last_seen: dict[tuple[int, bytes], int] = {}
        self._echo_timeout_ns = echo_timeout_ns
        self._audit_live = False
        self._awaiting: tuple[int, bytes, int] | None = None
        self.audited = 0

    @classmethod
    def from_config(
        cls, cfg: Config, clock: Clock, *, echo_timeout_ns: int | None = None
    ) -> TxGuard:
        return cls(
            allowlist_from_config(cfg),
            float(cfg.get("obd.max_request_hz")),
            clock,
            echo_timeout_ns=echo_timeout_ns,
        )

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def audit_live(self) -> bool:
        return self._audit_live

    def disable(self, reason: str) -> None:
        """Turn TX off for the rest of the session. The first reason is kept."""
        with self._lock:
            if not self._disabled:
                self._disabled = True
                self._reason = reason
                log.error("TX disabled for this session: %s", reason)

    def mark_audit_live(self) -> None:
        """The capture bus is open and every frame on it now reaches ``audit``."""
        with self._lock:
            self._audit_live = True

    def check_and_consume(self, msg: can.Message) -> None:
        """Validate ``msg`` and take a rate token. Call immediately before every send.

        Raises TxViolation (TX is now off) or TxRateLimited (try again later; this includes
        TxAuditPending while the capture audit is not ready).
        """
        with self._lock:
            if self._disabled:
                raise TxViolation(f"TX is off: {self._reason}")
            try:
                validate_frame(msg, self.allowed_pids)
            except TxViolation as exc:
                self.disable(exc.reason)
                raise
            if self._echo_timeout_ns is not None:
                self._check_echo()
            if not self._bucket.try_take():
                raise TxRateLimited("request rate cap reached")
            if self._echo_timeout_ns is not None:
                deadline = self._clock.now_ns() + self._echo_timeout_ns
                self._awaiting = (msg.arbitration_id, bytes(msg.data), deadline)

    def _check_echo(self) -> None:
        if not self._audit_live:
            raise TxAuditPending("the capture audit is not running yet")
        awaiting = self._awaiting
        if awaiting is None:
            return
        arb_id, data, deadline = awaiting
        if self._clock.now_ns() >= deadline:
            reason = f"audit: request {_describe(arb_id, data)} was not seen on the capture bus"
            self.disable(reason)
            raise TxViolation(reason)
        raise TxAuditPending("waiting for the capture bus to see the last request")

    def ns_until_token(self) -> int:
        return self._bucket.ns_until_token()

    def audit_error_frame(self, t_ns: int) -> None:
        """An error frame on the capture bus. TX turns off: the bus is not clean."""
        self.disable(f"audit: error frame on the capture bus at t_ns {t_ns}")

    def audit(self, frame: CanFrame) -> None:
        """Check a frame observed on the listen-only capture bus.

        Any frame on an audited ID (0x7DF, 0x7E0..0x7E7, 0x6F1) must pass the validator, the
        request frames in every closed 1 s and 10 s window must not outnumber what the token
        bucket can emit (``bucket_cap``), and no request may repeat within 1 ms (a
        retransmit). A mismatch turns TX off.
        """
        with self._lock:
            self.audited += 1
            if frame.arb_id not in AUDITED_IDS:
                return
            msg = can.Message(
                arbitration_id=frame.arb_id,
                is_extended_id=frame.is_ext,
                dlc=frame.dlc,
                data=frame.data,
            )
            try:
                validate_frame(msg, self.allowed_pids)
            except TxViolation as exc:
                self.disable(f"audit: {exc.reason}")
                return
            key = (frame.arb_id, bytes(frame.data))
            prev = self._last_seen.get(key)
            self._last_seen[key] = frame.t_ns
            if prev is not None and frame.t_ns - prev < RETRANSMIT_GAP_NS:
                self.disable(f"audit: request {_describe(*key)} repeated within 1 ms (retransmit)")
                return
            for window_s, window_ns, cap, times in self._windows:
                times.append(frame.t_ns)
                while times and times[0] < frame.t_ns - window_ns:
                    times.popleft()
                if len(times) > cap:
                    self.disable(
                        f"audit: {len(times)} requests on the wire within {window_s} s, "
                        f"over the cap of {cap}"
                    )
                    return
            if self._echo_timeout_ns is not None:
                awaiting = self._awaiting
                if awaiting is not None and (awaiting[0], awaiting[1]) == key:
                    self._awaiting = None
                else:
                    self.disable(
                        f"audit: request {_describe(*key)} on the wire was not sent by Photoncar"
                    )


def _describe(arb_id: int, data: bytes) -> str:
    return f"0x{arb_id:03X} {data.hex(' ').upper()}"

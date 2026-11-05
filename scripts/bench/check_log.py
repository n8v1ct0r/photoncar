"""Apply the bench pass rule to a candump log from the independent silent logger.

Pass rule (docs/phase-1-plan.md section 2.2): every logged frame is an emulator reply on
0x7E8..0x7EF or a Photoncar request that passes the TX validator; 0 flow control frames;
0 error frames; 0 retransmits (identical requests less than 1 ms apart). Requests on the
wire must stay within the rate cap in every closed 1 s and 10 s window. The rate is the
config value, never above ``HARD_MAX_REQUEST_HZ``. A log without timestamps fails, since it
cannot prove the rate or the absence of retransmits. An empty log fails: it proves nothing.

The cap is what the token bucket can emit (``bucket_cap``: 11 in 1 s and 101 in 10 s at
10 Hz) plus ``BENCH_JITTER_SLACK``, 1 frame. The slack is there because the logger stamps
frames on its own clock. The live-session echo rule lets a request reach the wire as late
as just before the next take, so 12 legal requests can span only just over 1 s on the wire,
and 1 ms of logger error can show all 12 inside a second. 13 legal requests span more than
1.1 s, so showing 13 inside a second needs 100 ms of error between the first and the last
stamp. The slack covers any per-frame error under half a request period (50 ms at 10 Hz).

Reads ``candump -L`` lines, for example ``(1695500000.123456) can0 7DF#02010CCCCCCCCCCC``,
and the default ``candump`` layout (``can0  7DF   [8]  02 01 0C CC CC CC CC CC``).

    uv run python scripts/bench/check_log.py data/bench/logger.log
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import can

from photoncar.config import load_config
from photoncar.safety.tx_guard import (
    AUDITED_IDS,
    HARD_MAX_REQUEST_HZ,
    RETRANSMIT_GAP_NS,
    TxViolation,
    allowlist_from_config,
    bucket_cap,
    validate_frame,
)

CAN_ERR_FLAG = 0x20000000
NS_PER_S = 1_000_000_000
LONG_WINDOW_S = 10
UNTIMED = "the log has no timestamps; rate not proven"
REPLY_IDS = range(0x7E8, 0x7F0)
DIAG_IDS = frozenset({0x7DF, *range(0x7E0, 0x7F0)})
BENCH_JITTER_SLACK = 1
"""Extra requests allowed per window over ``bucket_cap``, for the logger's timestamp error."""

_LOG_RE = re.compile(
    r"^\((?P<t>\d+\.\d+)\)\s+(?P<iface>\S+)\s+(?P<id>[0-9A-Fa-f]{3}|[0-9A-Fa-f]{8})"
    r"(?P<sep>##?)(?P<rest>\S*)$"
)
_HUMAN_RE = re.compile(
    r"^(?:\((?P<t>\d+\.\d+)\)\s+)?(?P<iface>\S+)\s+(?P<id>[0-9A-Fa-f]{3}|[0-9A-Fa-f]{8})\s+"
    r"\[(?P<len>\d{1,2})\]\s*(?P<rest>.*)$"
)
_HEX_RE = re.compile(r"^(?:[0-9A-Fa-f]{2})*$")


@dataclass(frozen=True, slots=True)
class LogFrame:
    t_ns: int | None
    arb_id: int
    is_ext: bool
    is_error: bool
    is_rtr: bool
    is_fd: bool
    data: bytes


def _id_fields(id_text: str) -> tuple[int, bool, bool]:
    raw = int(id_text, 16)
    if len(id_text) == 8:
        return raw & 0x1FFFFFFF, True, bool(raw & CAN_ERR_FLAG)
    return raw, False, False


def _t_ns(text: str) -> int:
    """Exact nanoseconds from a candump ``seconds.fraction`` stamp (no float rounding)."""
    sec, frac = text.split(".")
    return int(sec) * NS_PER_S + int(frac[:9].ljust(9, "0"))


def parse_line(line: str) -> LogFrame | None:
    """Parse one candump line. None means the line is not a frame this tool understands."""
    text = line.strip()
    m = _LOG_RE.match(text)
    if m:
        arb_id, is_ext, is_error = _id_fields(m["id"])
        rest = m["rest"]
        if m["sep"] == "##":
            if not rest or not _HEX_RE.match(rest[1:]):
                return None
            return LogFrame(
                _t_ns(m["t"]), arb_id, is_ext, is_error, False, True, bytes.fromhex(rest[1:])
            )
        if rest[:1] in ("R", "r"):
            return LogFrame(_t_ns(m["t"]), arb_id, is_ext, is_error, True, False, b"")
        rest = rest.split("_", 1)[0]
        if not _HEX_RE.match(rest) or len(rest) > 16:
            return None
        return LogFrame(_t_ns(m["t"]), arb_id, is_ext, is_error, False, False, bytes.fromhex(rest))
    m = _HUMAN_RE.match(text)
    if m:
        arb_id, is_ext, is_error = _id_fields(m["id"])
        t_ns = _t_ns(m["t"]) if m["t"] else None
        n = int(m["len"])
        rest = m["rest"]
        is_error = is_error or "ERRORFRAME" in rest
        if rest.startswith("remote request"):
            return LogFrame(t_ns, arb_id, is_ext, is_error, True, False, b"")
        tokens = rest.replace("ERRORFRAME", " ").split()
        hex_tokens = [tok for tok in tokens if re.fullmatch(r"[0-9A-Fa-f]{2}", tok)]
        if len(hex_tokens) < n:
            return None
        data = bytes(int(tok, 16) for tok in hex_tokens[:n])
        return LogFrame(t_ns, arb_id, is_ext, is_error, False, len(m["len"]) == 2, data)
    return None


@dataclass(slots=True)
class BenchReport:
    frames: int = 0
    requests: int = 0
    replies: int = 0
    flow_control: int = 0
    error_frames: int = 0
    rejected_requests: int = 0
    other: int = 0
    unparsed_lines: int = 0
    untimed_frames: int = 0
    retransmits: int = 0
    max_requests_per_s: int | None = None
    request_cap_per_s: int = 0
    max_requests_per_10s: int | None = None
    request_cap_per_10s: int = 0
    rate_hz: float = 0.0
    reasons: Counter[str] = field(default_factory=Counter)

    @property
    def failures(self) -> list[str]:
        out: list[str] = []
        if self.frames == 0:
            out.append("the log has no frames")
        if self.flow_control:
            out.append(f"{self.flow_control} flow control frames")
        if self.error_frames:
            out.append(f"{self.error_frames} error frames")
        if self.rejected_requests:
            out.append(f"{self.rejected_requests} requests fail the validator")
        if self.other:
            out.append(f"{self.other} frames are neither replies nor allowed requests")
        if self.unparsed_lines:
            out.append(f"{self.unparsed_lines} lines could not be read")
        if self.frames and self.untimed_frames == self.frames:
            out.append(UNTIMED)
        elif self.untimed_frames:
            out.append(f"{self.untimed_frames} frames have no timestamp; rate not proven")
        if self.retransmits:
            out.append(f"{self.retransmits} retransmits")
        if self.max_requests_per_s is not None and self.max_requests_per_s > self.request_cap_per_s:
            out.append(
                f"{self.max_requests_per_s} requests within 1 s, over the cap of "
                f"{self.request_cap_per_s}"
            )
        long_peak, long_cap = self.max_requests_per_10s, self.request_cap_per_10s
        if long_peak is not None and long_peak > long_cap:
            out.append(f"{long_peak} requests within 10 s, over the cap of {long_cap}")
        return out

    @property
    def passed(self) -> bool:
        return not self.failures


def _peak(times: list[int], window_ns: int) -> int:
    """The most times in any closed window of ``window_ns``. ``times`` must be sorted."""
    peak = 0
    lo = 0
    for hi, t in enumerate(times):
        while times[lo] < t - window_ns:
            lo += 1
        peak = max(peak, hi - lo + 1)
    return peak


def _retransmits(requests: list[tuple[int, int, bytes]]) -> int:
    """Identical request frames less than 1 ms apart. ``requests`` is (t_ns, id, data)."""
    last: dict[tuple[int, bytes], int] = {}
    n = 0
    for t, arb_id, data in sorted(requests):
        prev = last.get((arb_id, data))
        if prev is not None and t - prev < RETRANSMIT_GAP_NS:
            n += 1
        last[(arb_id, data)] = t
    return n


def check_lines(
    lines: Iterable[str], allowed_pids: frozenset[int], max_request_hz: float
) -> BenchReport:
    rate_hz = min(max_request_hz, HARD_MAX_REQUEST_HZ)
    report = BenchReport(
        request_cap_per_s=bucket_cap(rate_hz, 1) + BENCH_JITTER_SLACK,
        request_cap_per_10s=bucket_cap(rate_hz, LONG_WINDOW_S) + BENCH_JITTER_SLACK,
        rate_hz=rate_hz,
    )
    requests: list[tuple[int, int, bytes]] = []
    for line in lines:
        if not line.strip():
            continue
        frame = parse_line(line)
        if frame is None:
            report.unparsed_lines += 1
            continue
        report.frames += 1
        if frame.t_ns is None:
            report.untimed_frames += 1
        classic = not (frame.is_ext or frame.is_rtr or frame.is_fd)
        if frame.is_error:
            report.error_frames += 1
        elif classic and frame.arb_id in DIAG_IDS and frame.data and frame.data[0] & 0xF0 == 0x30:
            report.flow_control += 1
        elif classic and frame.arb_id in REPLY_IDS:
            report.replies += 1
        else:
            msg = can.Message(
                arbitration_id=frame.arb_id,
                is_extended_id=frame.is_ext,
                is_remote_frame=frame.is_rtr,
                is_fd=frame.is_fd,
                dlc=len(frame.data) if not frame.is_rtr else 0,
                data=frame.data,
            )
            try:
                validate_frame(msg, allowed_pids)
            except TxViolation as exc:
                if frame.arb_id in AUDITED_IDS and not frame.is_ext:
                    report.rejected_requests += 1
                    report.reasons[exc.reason] += 1
                else:
                    report.other += 1
                continue
            report.requests += 1
            if frame.t_ns is not None:
                requests.append((frame.t_ns, frame.arb_id, frame.data))
    if report.frames and not report.untimed_frames:
        times = sorted(t for t, _, _ in requests)
        report.max_requests_per_s = _peak(times, NS_PER_S)
        report.max_requests_per_10s = _peak(times, LONG_WINDOW_S * NS_PER_S)
        report.retransmits = _retransmits(requests)
    return report


def format_report(report: BenchReport) -> str:
    untimed = "not checked, the log has no timestamps"
    rate = (
        f"{report.max_requests_per_s} (cap {report.request_cap_per_s})"
        if report.max_requests_per_s is not None
        else untimed
    )
    long_rate = (
        f"{report.max_requests_per_10s} (cap {report.request_cap_per_10s})"
        if report.max_requests_per_10s is not None
        else untimed
    )
    retransmits = str(report.retransmits) if report.max_requests_per_s is not None else untimed
    lines = [
        f"Rate cap: {report.rate_hz:g} Hz",
        f"Frames: {report.frames}",
        f"Allowed requests: {report.requests}",
        f"Emulator replies: {report.replies}",
        f"Flow control frames: {report.flow_control}",
        f"Error frames: {report.error_frames}",
        f"Rejected requests: {report.rejected_requests}",
        f"Other frames: {report.other}",
        f"Unread lines: {report.unparsed_lines}",
        f"Retransmits: {retransmits}",
        f"Most requests in 1 s: {rate}",
        f"Most requests in 10 s: {long_rate}",
    ]
    lines += [f"Rejected because {reason}: {n}" for reason, n in sorted(report.reasons.items())]
    if report.passed:
        lines.append("Result: pass")
    else:
        lines.append("Result: fail")
        lines += [f"Failure: {f}" for f in report.failures]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the bench pass rule to a candump log.")
    parser.add_argument("log", type=Path, help="candump log file")
    parser.add_argument("--config", type=Path, default=None, help="photoncar.toml to read")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    allowed = allowlist_from_config(cfg)
    max_hz = float(cfg.get("obd.max_request_hz"))
    with args.log.open(encoding="utf-8", errors="replace") as f:
        report = check_lines(f, allowed, max_hz)
    print(format_report(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())

"""Checks on the request interface (``can1``), at startup and while polling.

Startup (docs/phase-1-plan.md section 1, H2): ``can1`` must be an up CAN interface in normal
mode with one-shot on (no automatic retransmit), restart-ms 0 (bus-off stays off), and the
configured bitrate. Listen-only on ``can1`` is refused too: the kernel would drop every
request, and it means the two channels are swapped.

While polling (``recheck_request_link``, on every tick of the session's link watch): the same
mode checks again, and any change in the ``can1`` error counters turns TX off for the session.
A rise is a new error. A fall is a fault too: a down and up of the interface resets the
counters, and the watch cannot tell a reset from recovery, so it fails closed.

Reads ``ip -details [-statistics] -json link show can1``. It never changes any setting.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeIs

from photoncar.safety.listen_only import (
    LinkQueryError,
    Runner,
    can_info,
    ctrl_modes,
    query_link,
)

XSTATS_KEYS: tuple[str, ...] = (
    "bus_error",
    "error_warning",
    "error_passive",
    "bus_off",
    "restarts",
)
"""Kernel event counts under ``linkinfo.info_xstats`` with ``-statistics``. They only grow."""


@dataclass(frozen=True, slots=True)
class RequestLinkStatus:
    ok: bool
    reason: str


class LinkFault(Exception):
    """A live interface no longer holds the state TX depends on. ``reason`` is plain text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ErrorCounters:
    """The controller's TX and RX error counters, plus the kernel event counts ``ip`` shows."""

    tx: int
    rx: int
    events: tuple[tuple[str, int], ...] = ()


def check_one_shot(
    ifname: str, bitrate: int, *, runner: Runner | None = None, system: str | None = None
) -> RequestLinkStatus:
    """``ok`` only when ``ifname`` is safe to send allowlisted requests on."""
    try:
        link = query_link(ifname, runner=runner, system=system)
    except LinkQueryError as exc:
        return RequestLinkStatus(False, exc.reason)
    return judge_one_shot(link, bitrate)


def judge_one_shot(link: Mapping[str, Any], bitrate: int) -> RequestLinkStatus:
    info = can_info(link)
    if info is None:
        return RequestLinkStatus(False, "not a CAN interface")
    flags = link.get("flags")
    if not isinstance(flags, list) or "UP" not in flags or link.get("operstate") != "UP":
        return RequestLinkStatus(False, "interface is down")
    info_data = info[1]
    modes = ctrl_modes(info_data)
    if "LISTEN-ONLY" in modes:
        return RequestLinkStatus(False, "listen-only is on, so every request would be dropped")
    if "ONE-SHOT" not in modes:
        return RequestLinkStatus(False, "one-shot is off")
    restart_ms = info_data.get("restart_ms")
    if not _is_int(restart_ms):
        return RequestLinkStatus(False, "restart-ms is missing")
    if restart_ms != 0:
        return RequestLinkStatus(False, f"restart-ms is {restart_ms}, not 0")
    bittiming = info_data.get("bittiming")
    actual = bittiming.get("bitrate") if isinstance(bittiming, dict) else None
    if not _is_int(actual):
        return RequestLinkStatus(False, "bitrate is missing")
    if actual != bitrate:
        return RequestLinkStatus(False, f"bitrate is {actual}, not {bitrate}")
    return RequestLinkStatus(True, "one-shot is on")


def read_error_counters(
    ifname: str, *, runner: Runner | None = None, system: str | None = None
) -> ErrorCounters:
    """Read the error counters. Raises LinkQueryError when they cannot be read."""
    return parse_error_counters(query_link(ifname, statistics=True, runner=runner, system=system))


def parse_error_counters(link: Mapping[str, Any]) -> ErrorCounters:
    info = can_info(link)
    if info is None:
        raise LinkQueryError("not a CAN interface")
    linkinfo, info_data = info
    berr = info_data.get("berr_counter")
    tx = berr.get("tx") if isinstance(berr, dict) else None
    rx = berr.get("rx") if isinstance(berr, dict) else None
    if not _is_int(tx) or not _is_int(rx):
        raise LinkQueryError("no error counters in ip output")
    xstats = linkinfo.get("info_xstats")
    events: list[tuple[str, int]] = []
    if isinstance(xstats, dict):
        for key in XSTATS_KEYS:
            value = xstats.get(key)
            if _is_int(value):
                events.append((key, value))
    return ErrorCounters(tx, rx, tuple(events))


def counters_rose(before: ErrorCounters, after: ErrorCounters) -> str | None:
    """A plain description of every counter that went up, or None if none did."""
    rises: list[str] = []
    if after.tx > before.tx:
        rises.append(f"tx errors {before.tx} to {after.tx}")
    if after.rx > before.rx:
        rises.append(f"rx errors {before.rx} to {after.rx}")
    old = dict(before.events)
    for key, value in after.events:
        if key in old and value > old[key]:
            rises.append(f"{key.replace('_', ' ')} {old[key]} to {value}")
    return ", ".join(rises) or None


def counters_fell(before: ErrorCounters, after: ErrorCounters) -> str | None:
    """A plain description of every counter that went down or vanished, or None if none did."""
    falls: list[str] = []
    if after.tx < before.tx:
        falls.append(f"tx errors {before.tx} to {after.tx}")
    if after.rx < before.rx:
        falls.append(f"rx errors {before.rx} to {after.rx}")
    new = dict(after.events)
    for key, value in before.events:
        name = key.replace("_", " ")
        if key not in new:
            falls.append(f"{name} is no longer reported")
        elif new[key] < value:
            falls.append(f"{name} {value} to {new[key]}")
    return ", ".join(falls) or None


def recheck_request_link(
    ifname: str, link: Mapping[str, Any], bitrate: int, before: ErrorCounters
) -> ErrorCounters:
    """Judge ``can1`` again from one ``ip -details -statistics`` read, during polling.

    Returns the new counters. Raises LinkFault when a startup check no longer holds (up,
    one-shot, restart-ms 0, not listen-only, the bitrate), when the counters cannot be read,
    or when any counter fell or rose since ``before``.
    """
    status = judge_one_shot(link, bitrate)
    if not status.ok:
        raise LinkFault(f"{ifname} is no longer ready for requests ({status.reason})")
    try:
        after = parse_error_counters(link)
    except LinkQueryError as exc:
        raise LinkFault(f"{ifname} error counters are unreadable: {exc.reason}") from exc
    fell = counters_fell(before, after)
    if fell is not None:
        raise LinkFault(f"{ifname} error counters fell: {fell}")
    rose = counters_rose(before, after)
    if rose is not None:
        raise LinkFault(f"{ifname} error counters rose: {rose}")
    return after


def _is_int(value: object) -> TypeIs[int]:
    return isinstance(value, int) and not isinstance(value, bool)

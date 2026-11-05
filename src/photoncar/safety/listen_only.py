"""Startup check that the capture interface is listen-only (hard rule 1).

Reads ``ip -details -json link show <ifname>``. It never changes any setting: CAN settings
come only from the owner-approved files in scripts/provision/. ``query_link`` is shared with
the request interface checks in ``safety/request_link.py``.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

IFNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
IP_TIMEOUT_S = 5.0

Runner = Callable[[Sequence[str]], tuple[int, str]]


@dataclass(frozen=True, slots=True)
class ListenOnlyStatus:
    ok: bool
    reason: str


class LinkQueryError(Exception):
    """``ip`` could not describe the interface. ``reason`` is a short plain phrase."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _run_ip(argv: Sequence[str]) -> tuple[int, str]:
    proc = subprocess.run(
        list(argv), capture_output=True, text=True, timeout=IP_TIMEOUT_S, check=False
    )
    return proc.returncode, proc.stdout


def query_link(
    ifname: str,
    *,
    statistics: bool = False,
    runner: Runner | None = None,
    system: str | None = None,
) -> dict[str, Any]:
    """Run a read-only ``ip -details [-statistics] -json link show`` and return the entry.

    Raises LinkQueryError off Linux, for an odd name, or when ``ip`` fails or says nothing.
    ``runner`` and ``system`` exist for tests (captured ``ip`` output, a pretend OS).
    """
    if (system or platform.system()) != "Linux":
        raise LinkQueryError("not linux")
    if not IFNAME_RE.fullmatch(ifname):
        raise LinkQueryError("invalid interface name")
    argv = ["ip", "-details", *(["-statistics"] if statistics else []), "-json"]
    argv += ["link", "show", ifname]
    run = runner or _run_ip
    try:
        code, out = run(argv)
    except FileNotFoundError as exc:
        raise LinkQueryError("ip command not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise LinkQueryError("ip command timed out") from exc
    if code != 0:
        raise LinkQueryError(f"ip exited with code {code}")
    return find_link(out, ifname)


def find_link(text: str, ifname: str) -> dict[str, Any]:
    """The entry for ``ifname`` in ``ip -json link show`` output, or LinkQueryError."""
    try:
        doc: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LinkQueryError("unreadable ip output") from exc
    if not isinstance(doc, list):
        raise LinkQueryError("unreadable ip output")
    link = next((e for e in doc if isinstance(e, dict) and e.get("ifname") == ifname), None)
    if link is None:
        raise LinkQueryError("interface not found")
    return link


def can_info(link: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """``(linkinfo, info_data)`` of a CAN interface, or None if it is not one."""
    linkinfo = link.get("linkinfo")
    if not isinstance(linkinfo, dict) or linkinfo.get("info_kind") != "can":
        return None
    info_data = linkinfo.get("info_data")
    return linkinfo, info_data if isinstance(info_data, dict) else {}


def ctrl_modes(info_data: Mapping[str, Any]) -> frozenset[str]:
    """The ctrlmode flags. ``ip`` prints a list, older versions a comma separated string."""
    ctrlmode: Any = info_data.get("ctrlmode")
    modes = ctrlmode.split(",") if isinstance(ctrlmode, str) else ctrlmode
    if not isinstance(modes, list):
        return frozenset()
    return frozenset(m for m in modes if isinstance(m, str))


def check_listen_only(
    ifname: str, *, runner: Runner | None = None, system: str | None = None
) -> ListenOnlyStatus:
    """``ok`` is True only when ``ifname`` is an up CAN interface with LISTEN-ONLY set.

    ``runner`` and ``system`` exist for tests (captured ``ip`` output, a pretend OS).
    """
    try:
        link = query_link(ifname, runner=runner, system=system)
    except LinkQueryError as exc:
        return ListenOnlyStatus(False, exc.reason)
    return judge_listen_only(link)


def parse_ip_link_json(text: str, ifname: str) -> ListenOnlyStatus:
    """Judge one interface from ``ip -details -json link show`` output."""
    try:
        link = find_link(text, ifname)
    except LinkQueryError as exc:
        return ListenOnlyStatus(False, exc.reason)
    return judge_listen_only(link)


def judge_listen_only(link: Mapping[str, Any]) -> ListenOnlyStatus:
    info = can_info(link)
    if info is None:
        return ListenOnlyStatus(False, "not a CAN interface")
    flags = link.get("flags")
    if not isinstance(flags, list) or "UP" not in flags:
        return ListenOnlyStatus(False, "interface is down")
    if "LISTEN-ONLY" not in ctrl_modes(info[1]):
        return ListenOnlyStatus(False, "listen-only is off")
    return ListenOnlyStatus(True, "listen-only is on")

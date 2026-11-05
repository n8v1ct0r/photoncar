"""Live state (the moving lock and the pipeline mode) and live frames.

``LiveState`` is how the pipeline talks to the server in the same process: it sets
``moving`` and ``mode`` and publishes live frames. Every method is thread-safe.

A ``LiveFrame`` is ``{t_s, speed_mph, rpm, coolant_c, synthetic_fields, alert}``. For a stored
drive it is computed deterministically from the drive file at ``t_s`` seconds after the drive
start:

- ``speed_mph``, ``rpm``, ``coolant_c``: linear interpolation between the 2 neighbouring rows
  of the stored 10 Hz timeline, null where either row has no value.
- ``synthetic_fields``: those of the 3 with a value whose timeline column a synthetic signal
  feeds (``DriveReader.timeline_sources``), for example ``["coolant_c"]`` on a public-dataset
  replay, where the OBD overlay is synthetic.
- ``alert``: the alert active at that time, or null. An alert is active from the moment it
  became known until its end, the same moment the pipeline's own live frames show it:

  - a rule alert (coolant, voltage, trims) at the end of its on-delay, its latest trigger
    citation window;
  - a stale alert once the gap passes the stale limit (the event's ``threshold``), since the
    rule counts a signal stale only after more than that many seconds without an update;
  - a baseline alert once its departure has lasted ``baseline.sustain_s``;
  - anything else at the end of its trigger evidence, never before.

  An unresolved alert stays active through its end, the drive's last instant. The highest
  severity wins, then the latest to become known.
- The alert text is the event as it read at that time: trigger windows end at ``t_s`` at the
  latest, and a stale gap counts only the seconds elapsed so far. ``headline`` is the first
  clause of the message ("Coolant 106 °C" from "Coolant 106 °C, over 105 °C."). ``citation``
  names the trigger sources, the second clause, and the trigger window ("OBD 0x05 over 105 °C
  for 12 s"); a clause that already states a duration ("no update for 9 s") gets none added.
- ``synthetic`` is true for an alert raised on synthetic data, and its headline then ends with
  ", synthetic fault" (plan H8 and H13: the label shows wherever the alert does).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from photoncar.config import Config
from photoncar.detect.rules import stale_message
from photoncar.records import Event
from photoncar.store.reader import DriveReader
from photoncar.store.rollup import GRID_NS, interp_gap
from photoncar.units import KELVIN_OFFSET, MPH, NS_PER_S, duration_text

Mode = Literal["idle", "live", "replay"]
MODES: tuple[Mode, ...] = ("idle", "live", "replay")
SYNTHETIC_HEADLINE = "synthetic fault"

F64 = NDArray[np.float64]
I64 = NDArray[np.int64]

FRAME_COLUMNS = (("speed_mph", "speed_mps"), ("rpm", "rpm"), ("coolant_c", "coolant_c"))
"""LiveFrame fields and the timeline columns they come from."""


class LiveState:
    """What the pipeline tells the server: moving, mode, and the latest live frame."""

    def __init__(self, *, mode: Mode = "idle", moving: bool = False) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self._lock = threading.Lock()
        self._mode: Mode = mode
        self._moving = moving
        self._frame: dict[str, Any] | None = None
        self._version = 0

    @property
    def mode(self) -> Mode:
        with self._lock:
            return self._mode

    @property
    def moving(self) -> bool:
        with self._lock:
            return self._moving

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
        with self._lock:
            self._mode = mode

    def set_moving(self, moving: bool) -> None:
        """Set by the pipeline. With no speed source while live, the pipeline sets True."""
        with self._lock:
            self._moving = bool(moving)

    def publish(self, frame: Mapping[str, Any]) -> None:
        """Publish the latest live frame (a ``LiveFrame`` dict) for ``/api/live/stream``."""
        with self._lock:
            self._frame = dict(frame)
            self._version += 1

    def latest(self) -> tuple[int, dict[str, Any] | None]:
        """(version, frame). The version grows by 1 on every publish."""
        with self._lock:
            return self._version, None if self._frame is None else dict(self._frame)


# -- alert text --------------------------------------------------------------------------------


def _first_sentence(message: str) -> str:
    text = message.strip()
    head, sep, _ = text.partition(". ")
    return head if sep else text.removesuffix(".")


def _duration_text(seconds: float) -> str:
    """Under 1 s with 1 decimal, else as the web shows durations ("12 s", "10 min 12 s")."""
    if seconds < 1.0:
        return f"{seconds:.1f} s"
    return duration_text(seconds)


def alert_text(ev: Event) -> tuple[str, str]:
    """(headline, citation) for an alert, from its message and trigger citations."""
    triggers = [c for c in ev.citations if c.role == "trigger"]
    sentence = _first_sentence(ev.message)
    headline, sep, clause = sentence.partition(", ")
    sources: list[str] = []
    for c in triggers:
        if c.source not in sources:
            sources.append(c.source)
    who = " and ".join(sources)
    window_ns = max((c.t_end_ns - c.t_start_ns for c in triggers), default=0)
    duration = _duration_text(window_ns / NS_PER_S)
    clause = clause.strip()
    if not sep or not clause:
        citation = f"{who} for {duration}".strip()
    elif " for " in f" {clause} ":
        citation = f"{who} {clause}".strip()
    else:
        citation = f"{who} {clause} for {duration}".strip()
    return headline.strip(), citation


def baseline_sustain_ns(cfg: Config) -> int:
    """How long a baseline departure lasts before its alert is known (``baseline.sustain_s``)."""
    return round(float(cfg.get("baseline.sustain_s")) * NS_PER_S)


def fire_ns(ev: Event, sustain_ns: int) -> int:
    """When the alert became known (see the module docstring), never after its end.

    ``sustain_ns`` is ``baseline_sustain_ns(cfg)``.
    """
    if ev.kind == "alert.stale" and ev.threshold is not None:
        fired = ev.t_start_ns + round(ev.threshold * NS_PER_S) + 1
    elif ev.kind == "alert.baseline":
        fired = ev.t_start_ns + sustain_ns
    else:
        ends = [c.t_end_ns for c in ev.citations if c.role == "trigger"]
        fired = max(ends, default=ev.t_start_ns)
    return min(fired, ev.t_end_ns)


def is_active(ev: Event, t_ns: int, sustain_ns: int) -> bool:
    """Known and not yet ended. An unresolved alert was still on when the drive ended, so it
    stays active through its last instant."""
    if t_ns < fire_ns(ev, sustain_ns):
        return False
    return t_ns < ev.t_end_ns or (not ev.resolved and t_ns == ev.t_end_ns)


def as_of(ev: Event, t_ns: int) -> Event:
    """The alert as it read at ``t_ns``: no trigger window reaches past ``t_ns``, and a stale
    gap counts only the time elapsed so far. At or after its end, the event unchanged."""
    if t_ns >= ev.t_end_ns:
        return ev
    end = max(t_ns, ev.t_start_ns + 1)
    cites = tuple(
        replace(c, t_end_ns=max(min(c.t_end_ns, t_ns), c.t_start_ns + 1))
        if c.role == "trigger"
        else c
        for c in ev.citations
    )
    message = ev.message
    triggers = [c for c in cites if c.role == "trigger"]
    if ev.kind == "alert.stale" and triggers:
        _, sep, rest = ev.message.partition(". ")
        head = stale_message(triggers[0].signal, (end - ev.t_start_ns) / NS_PER_S)
        message = f"{head} {rest}" if sep else head
    return replace(ev, t_end_ns=end, citations=cites, message=message)


def active_alert(alerts: Sequence[Event], t_ns: int, sustain_ns: int) -> Event | None:
    """The alert to show at ``t_ns``, as it read then: the highest severity, then the latest
    to become known."""
    best: Event | None = None
    best_key: tuple[int, int] | None = None
    for ev in alerts:
        if is_active(ev, t_ns, sustain_ns):
            key = (ev.severity, fire_ns(ev, sustain_ns))
            if best_key is None or key > best_key:
                best, best_key = ev, key
    return None if best is None else as_of(best, t_ns)


def alert_json(ev: Event | None) -> dict[str, Any] | None:
    if ev is None:
        return None
    headline, citation = alert_text(ev)
    if ev.synthetic:
        headline = f"{headline}, {SYNTHETIC_HEADLINE}"
    return {
        "severity": ev.severity,
        "headline": headline,
        "citation": citation,
        "synthetic": ev.synthetic,
    }


# -- frames from a stored drive ----------------------------------------------------------------


def _col(values: Sequence[float | None]) -> F64:
    return np.array([np.nan if v is None else v for v in values], dtype=np.float64)


def _opt(x: float) -> float | None:
    return None if np.isnan(x) else float(x)


@dataclass(frozen=True, slots=True, eq=False)
class ReplayData:
    """Everything a stored drive needs to produce live frames, loaded once."""

    t0_ns: int
    duration_s: float
    t_ns: I64
    speed_mps: F64
    rpm: F64
    coolant_k: F64
    alerts: tuple[Event, ...]
    sustain_ns: int
    synthetic_fields: frozenset[str] = frozenset()

    @classmethod
    def load(cls, reader: DriveReader, cfg: Config) -> ReplayData:
        meta = reader.meta()
        rows = reader.timeline_rows()
        t = np.array([r.t_ns for r in rows], dtype=np.int64)
        t0 = meta.t_start_ns if meta.t_start_ns is not None else (int(t[0]) if t.size else 0)
        t_last = int(t[-1]) if t.size else t0
        t1 = meta.t_end_ns if meta.t_end_ns is not None else t_last
        alerts = tuple(e for e in reader.events() if e.kind.startswith("alert."))
        synthetic = set(meta.synthetic_signals)
        sources = reader.timeline_sources()
        return cls(
            t0_ns=t0,
            duration_s=max(0.0, (t1 - t0) / NS_PER_S),
            t_ns=t,
            speed_mps=_col([r.speed_mps for r in rows]),
            rpm=_col([r.rpm for r in rows]),
            coolant_k=_col([r.coolant_k for r in rows]),
            alerts=alerts,
            sustain_ns=baseline_sustain_ns(cfg),
            synthetic_fields=frozenset(
                field for field, col in FRAME_COLUMNS if sources[col] in synthetic
            ),
        )

    def frame_at(self, t_s: float) -> dict[str, Any]:
        """The ``LiveFrame`` at ``t_s`` seconds after the drive start."""
        t_ns = self.t0_ns + round(t_s * NS_PER_S)
        grid = np.array([t_ns], dtype=np.int64)
        speed = float(interp_gap(self.t_ns, self.speed_mps, grid, GRID_NS)[0])
        rpm = float(interp_gap(self.t_ns, self.rpm, grid, GRID_NS)[0])
        coolant = float(interp_gap(self.t_ns, self.coolant_k, grid, GRID_NS)[0])
        values = {
            "speed_mph": _opt(speed / MPH),
            "rpm": _opt(rpm),
            "coolant_c": _opt(coolant - KELVIN_OFFSET),
        }
        return {
            "t_s": (t_ns - self.t0_ns) / NS_PER_S,
            **values,
            "synthetic_fields": [
                k for k, v in values.items() if v is not None and k in self.synthetic_fields
            ],
            "alert": alert_json(active_alert(self.alerts, t_ns, self.sustain_ns)),
        }

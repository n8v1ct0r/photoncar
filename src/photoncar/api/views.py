"""JSON shapes of the HTTP API (docs/contracts.md, "HTTP API"). Times are seconds from the
drive start; values are SI, plus g for acceleration.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray

from photoncar.records import Citation, DriveStat, Event
from photoncar.store.index import DriveListItem, drive_summary
from photoncar.store.reader import DriveReader
from photoncar.store.rollup import TimelineRow
from photoncar.units import NS_PER_S


def drive_list_item(item: DriveListItem) -> dict[str, Any]:
    return dataclasses.asdict(item)


def list_item_from_file(reader: DriveReader) -> DriveListItem:
    """The list fields of a drive that is not in the index, computed from its file.

    The label is the drive ID: nothing else names an unlisted drive.
    """
    meta = reader.meta()
    s = drive_summary(reader)
    duration = (
        (s.t_end_ns - s.t_start_ns) / NS_PER_S
        if s.t_start_ns is not None and s.t_end_ns is not None
        else None
    )
    return DriveListItem(
        drive_id=meta.drive_id,
        label=meta.drive_id,
        source=meta.source,
        duration_s=duration,
        distance_m=s.distance_m,
        n_events=s.n_events,
        n_alerts=s.n_alerts,
        closed_cleanly=s.closed_cleanly,
    )


def drive_detail(reader: DriveReader, item: DriveListItem) -> dict[str, Any]:
    meta = reader.meta()
    out = drive_list_item(item)
    out["vehicle_profile"] = meta.vehicle_profile
    out["dataset_ref"] = meta.dataset_ref
    out["synthetic_signals"] = list(meta.synthetic_signals)
    return out


def stat_json(s: DriveStat) -> dict[str, Any]:
    return {
        "stat_id": s.stat_id,
        "label": s.label,
        "value": s.value,
        "unit": s.unit,
        "display": s.display,
    }


def cumulative_distance(
    rows: Sequence[TimelineRow],
) -> tuple[NDArray[np.int64], NDArray[np.float64]] | None:
    """(t_ns, meters from the drive start) at each timeline row, or None without speed.

    Trapezoid integral of the 10 Hz speed; a step where either row lacks speed adds 0, the
    same rule as the drive index distance.
    """
    if not any(r.speed_mps is not None for r in rows):
        return None
    t = np.array([r.t_ns for r in rows], dtype=np.int64)
    v = np.array([np.nan if r.speed_mps is None else r.speed_mps for r in rows], dtype=np.float64)
    step = (v[1:] + v[:-1]) / 2.0 * np.diff(t).astype(np.float64) / NS_PER_S
    step = np.where(np.isnan(step), 0.0, step)
    dist = np.concatenate(([0.0], np.cumsum(step)))
    return t, dist


def _citation_json(c: Citation, t0: int) -> dict[str, Any]:
    return {
        "signal": c.signal,
        "source": c.source,
        "role": c.role,
        "t_start_s": (c.t_start_ns - t0) / NS_PER_S,
        "t_end_s": (c.t_end_ns - t0) / NS_PER_S,
        "stat": c.stat,
        "stat_value": c.stat_value,
        "unit": c.unit,
        "n": c.n,
    }


ROADBOOK_PREFIXES = ("alert.", "perf.", "lap")
ROADBOOK_KINDS = frozenset({"behavior.hard_brake", "behavior.hard_accel", "behavior.corner"})


def on_roadbook(kind: str) -> bool:
    """Alerts, performance runs, laps, hard brakes, hard accelerations, and corners get a box
    on the roadbook. Shifts (``behavior.shift``) and any other kind do not."""
    return kind.startswith(ROADBOOK_PREFIXES) or kind in ROADBOOK_KINDS


def events_json(reader: DriveReader) -> list[dict[str, Any]]:
    """``EventOut`` for every event, in time order. ``box_no`` numbers the roadbook events
    1, 2, 3, ... (``on_roadbook``); every other event, such as a shift, has ``box_no`` null
    and stays in the list for the report."""
    meta = reader.meta()
    t0 = meta.t_start_ns if meta.t_start_ns is not None else 0
    events: list[tuple[int, Event]] = reader.events_with_ids()
    dist = cumulative_distance(reader.timeline_rows()) if events else None
    out: list[dict[str, Any]] = []
    boxes = 0
    for event_id, ev in events:
        box_no: int | None = None
        if on_roadbook(ev.kind):
            boxes += 1
            box_no = boxes
        distance_m: float | None = None
        if dist is not None:
            distance_m = float(np.interp(float(ev.t_start_ns), dist[0].astype(float), dist[1]))
        out.append(
            {
                "event_id": event_id,
                "box_no": box_no,
                "kind": ev.kind,
                "rule_id": ev.rule_id,
                "severity": ev.severity,
                "t_start_s": (ev.t_start_ns - t0) / NS_PER_S,
                "t_end_s": (ev.t_end_ns - t0) / NS_PER_S,
                "distance_m": distance_m,
                "value": ev.value,
                "threshold": ev.threshold,
                "unit": ev.unit,
                "message": ev.message,
                "resolved": ev.resolved,
                "synthetic": ev.synthetic,
                "citations": [_citation_json(c, t0) for c in ev.citations],
            }
        )
    return out

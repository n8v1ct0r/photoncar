"""Per-car baselines: median and MAD of 10 s window medians, by operating state.

For each (signal, bin) the baseline keeps the median and the MAD of window medians, with
sigma = 1.4826 * MAD. The MAD is floored at the signal's PID resolution, because a
thermostat-held coolant value can give MAD = 0. A baseline is armed after
``baseline.arm_drives`` drives and ``baseline.arm_windows`` windows. Once armed, a window
whose |z| exceeds ``baseline.z_flag`` for ``baseline.sustain_s`` in a row raises an
``alert.baseline`` event (severity 1) that cites both the window evidence and the baseline
it was measured against.

The drive index stores summaries, not every window, so ``update`` treats the previous
baseline as ``n_windows`` windows sitting at its median (for the median) and at its MAD plus
the shift in median (for the MAD, an upper bound on their new deviations). The result is
exact when the previous baseline is empty or all windows agree.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

from photoncar.config import Config
from photoncar.detect._series import FloatArray, IntArray, as_times, as_values
from photoncar.records import Citation, Event
from photoncar.signals import SIGNALS, label
from photoncar.units import c_to_k, k_to_c

RULE_VERSION = 1
SEVERITY = 1
"""A baseline alert is 1 caution mark: it compares the car with itself, not a limit."""
MAD_TO_SIGMA = 1.4826
EXCLUDED = ""
"""Bin label for samples that no baseline may use (for example trims in open loop)."""

PID_RESOLUTION: dict[str, float] = {
    "obd.load": 1.0 / 255.0,  # 100A/255 %
    "obd.coolant_temp": 1.0,  # A - 40 degC
    "obd.stft_b1": 1.0 / 128.0,  # 100A/128 - 100 %
    "obd.ltft_b1": 1.0 / 128.0,
    "obd.stft_b2": 1.0 / 128.0,
    "obd.ltft_b2": 1.0 / 128.0,
    "obd.rpm": 0.25,  # (256A+B)/4
    "obd.speed": 1.0 / 3.6,  # A km/h
    "obd.iat": 1.0,  # A - 40 degC
    "obd.maf": 1e-5,  # (256A+B)/100 g/s
    "obd.throttle": 1.0 / 255.0,  # 100A/255 %
    "obd.module_voltage": 0.001,  # (256A+B)/1000 V
}
"""One step of each OBD PID's encoding, in SI (Wikipedia OBD-II PIDs formulas)."""


@dataclass(frozen=True, slots=True)
class WindowMedians:
    """Fixed windows aligned to the first sample. Windows with no usable samples are omitted."""

    t_start_ns: IntArray
    t_end_ns: IntArray
    median: FloatArray
    n: IntArray
    bins: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BaselineWindow:
    signal: str
    bin: str
    t_start_ns: int
    t_end_ns: int
    median: float
    n: int
    alerted: bool = False


@dataclass(frozen=True, slots=True)
class BaselineStats:
    signal: str
    bin: str
    median: float
    mad: float
    n_windows: int
    n_drives: int
    version: int
    updated_drive: str = ""


def resolution(signal: str) -> float:
    """The PID resolution floor for ``signal`` (0.0 when unknown)."""
    return PID_RESOLUTION.get(signal, 0.0)


def window_medians(
    t_ns: ArrayLike,
    values: ArrayLike,
    window_s: float,
    bins: Sequence[str] | None = None,
) -> WindowMedians:
    """Median of each ``window_s`` window, grid aligned to the first sample.

    With ``bins``, a window is kept only when all its samples share one bin other than
    ``EXCLUDED``; without them every window gets the bin ``"all"``. NaN values are ignored.
    """
    t = as_times(t_ns)
    v = as_values(values, t.size, "values")
    labels = ["all"] * t.size if bins is None else list(bins)
    if len(labels) != t.size:
        raise ValueError("bins must match t_ns")
    w_ns = round(window_s * 1e9)
    if w_ns <= 0:
        raise ValueError("window_s must be > 0")
    starts: list[int] = []
    meds: list[float] = []
    counts: list[int] = []
    out_bins: list[str] = []
    if t.size:
        t0 = int(t[0])
        idx = (t - np.int64(t0)) // np.int64(w_ns)
        cuts = np.flatnonzero(np.diff(idx)) + 1
        for a, b in zip([0, *cuts.tolist()], [*cuts.tolist(), t.size], strict=True):
            vals = v[a:b]
            ok = np.isfinite(vals)
            window_bins = {labels[i] for i in range(a, b) if ok[i - a]}
            if not bool(np.any(ok)) or len(window_bins) != 1:
                continue
            (bin_label,) = window_bins
            if bin_label == EXCLUDED:
                continue
            starts.append(t0 + int(idx[a]) * w_ns)
            meds.append(float(np.median(vals[ok])))
            counts.append(int(np.count_nonzero(ok)))
            out_bins.append(bin_label)
    s = np.asarray(starts, dtype=np.int64)
    return WindowMedians(
        t_start_ns=s,
        t_end_ns=s + np.int64(w_ns),
        median=np.asarray(meds, dtype=np.float64),
        n=np.asarray(counts, dtype=np.int64),
        bins=tuple(out_bins),
    )


def to_windows(
    signal: str, wm: WindowMedians, events: Sequence[Event] = ()
) -> list[BaselineWindow]:
    """Window records for ``update``. A window overlapping an alert that cites ``signal``
    is marked ``alerted`` so it never teaches the baseline."""
    spans = [
        (e.t_start_ns, e.t_end_ns)
        for e in events
        if e.kind.startswith("alert.") and any(c.signal == signal for c in e.citations)
    ]
    out: list[BaselineWindow] = []
    for i in range(wm.median.size):
        lo, hi = int(wm.t_start_ns[i]), int(wm.t_end_ns[i])
        hit = any(a < hi and lo < b for a, b in spans)
        out.append(
            BaselineWindow(
                signal, wm.bins[i], lo, hi, float(wm.median[i]), int(wm.n[i]), alerted=hit
            )
        )
    return out


def _weighted_median(values: NDArray[np.float64], weights: NDArray[np.float64]) -> float:
    order = np.argsort(values, kind="stable")
    v = values[order]
    w = weights[order]
    cum = np.cumsum(w)
    half = cum[-1] / 2.0
    i = int(np.searchsorted(cum, half, side="left"))
    if cum[i] == half and i + 1 < v.size:
        return float((v[i] + v[i + 1]) / 2.0)
    return float(v[i])


def update(
    prev: Sequence[BaselineStats], new_windows: Sequence[BaselineWindow], drive_id: str
) -> list[BaselineStats]:
    """Fold one drive's windows into the baselines. Alerted windows are left out.

    Returns every baseline: unchanged ones as they were, updated ones with ``version + 1``,
    ``n_drives + 1``, and ``updated_drive = drive_id``. Raises ``ValueError`` if the same
    drive is applied twice in a row to a baseline.
    """
    current: dict[tuple[str, str], BaselineStats] = {}
    for s in prev:
        key = (s.signal, s.bin)
        if key not in current or s.version > current[key].version:
            current[key] = s
    grouped: dict[tuple[str, str], list[float]] = {}
    for w in new_windows:
        if w.alerted or w.bin == EXCLUDED or not math.isfinite(w.median):
            continue
        grouped.setdefault((w.signal, w.bin), []).append(w.median)
    for key, meds in grouped.items():
        p = current.get(key)
        x = np.asarray(meds, dtype=np.float64)
        if p is not None and p.updated_drive == drive_id:
            raise ValueError(f"drive {drive_id} already applied to baseline {key}")
        if p is None:
            med = float(np.median(x))
            mad = float(np.median(np.abs(x - med)))
            n_win, n_drv, ver = x.size, 1, 1
        else:
            vals = np.concatenate(([p.median], x))
            wts = np.concatenate(([float(p.n_windows)], np.ones(x.size)))
            med = _weighted_median(vals, wts)
            devs = np.concatenate(([p.mad + abs(med - p.median)], np.abs(x - med)))
            mad = _weighted_median(devs, wts)
            n_win, n_drv, ver = p.n_windows + x.size, p.n_drives + 1, p.version + 1
        current[key] = BaselineStats(
            signal=key[0],
            bin=key[1],
            median=med,
            mad=max(mad, resolution(key[0])),
            n_windows=int(n_win),
            n_drives=n_drv,
            version=ver,
            updated_drive=drive_id,
        )
    return [current[k] for k in sorted(current)]


def is_armed(stats: BaselineStats, cfg: Config) -> bool:
    b = cfg.section("baseline")
    return stats.n_drives >= int(b["arm_drives"]) and stats.n_windows >= int(b["arm_windows"])


def _display(signal: str, value: float) -> str:
    unit = SIGNALS[signal].unit if signal in SIGNALS else ""
    if unit == "K":
        return f"{k_to_c(value):.1f} °C"
    if unit == "ratio":
        return f"{value * 100.0:.1f}%"
    if unit == "V":
        return f"{value:.2f} V"
    if unit == "rpm":
        return f"{value:,.0f} rpm"
    return f"{value:.3g} {unit}".strip()


def _z_text(z: float, z_flag: float) -> str:
    """|z| with 1 to 3 decimals, enough that it never prints as the flag level it exceeded."""
    for d in (1, 2, 3):
        text = f"{z:.{d}f}"
        if float(text) > z_flag:
            return text
    return f"{z:.3f}"


def baseline_stat_text(s: BaselineStats) -> str:
    """The context citation ``stat``: every baseline figure the alert was measured against."""
    return (
        f"baseline bin={s.bin} median={s.median:.6g} mad={s.mad:.6g} "
        f"n_drives={s.n_drives} version={s.version}"
    )


def check(
    stats: Sequence[BaselineStats],
    t_ns: ArrayLike,
    values: ArrayLike,
    bins: Sequence[str],
    cfg: Config,
    *,
    signal: str | None = None,
) -> list[Event]:
    """Flag sustained departures from armed baselines for one signal.

    ``signal`` defaults to the single signal named in ``stats``.
    """
    names = {s.signal for s in stats}
    if signal is None:
        if not names:
            return []
        if len(names) != 1:
            raise ValueError("stats cover several signals; pass signal=")
        (signal,) = names
    b = cfg.section("baseline")
    z_flag = float(b["z_flag"])
    sustain_ns = round(float(b["sustain_s"]) * 1e9)
    by_bin: dict[str, BaselineStats] = {}
    for s in stats:
        if s.signal == signal and (s.bin not in by_bin or s.version > by_bin[s.bin].version):
            by_bin[s.bin] = s
    wm = window_medians(t_ns, values, float(b["window_s"]), bins)
    m = wm.median.size
    z = np.full(m, np.nan)
    for i in range(m):
        st = by_bin.get(wm.bins[i])
        if st is None or not is_armed(st, cfg):
            continue
        scale = MAD_TO_SIGMA * max(st.mad, resolution(signal))
        if scale > 0.0:
            z[i] = (wm.median[i] - st.median) / scale
    flagged = np.abs(np.nan_to_num(z, nan=0.0)) > z_flag
    events: list[Event] = []
    i = 0
    while i < m:
        if not flagged[i]:
            i += 1
            continue
        j = i
        while j + 1 < m and flagged[j + 1] and wm.t_start_ns[j + 1] == wm.t_end_ns[j]:
            j += 1
        lo, hi = int(wm.t_start_ns[i]), int(wm.t_end_ns[j])
        if hi - lo >= sustain_ns:
            events.append(_event(signal, wm, z, i, j, by_bin, z_flag))
        i = j + 1
    return events


def _event(
    signal: str,
    wm: WindowMedians,
    z: FloatArray,
    i: int,
    j: int,
    by_bin: dict[str, BaselineStats],
    z_flag: float,
) -> Event:
    lo, hi = int(wm.t_start_ns[i]), int(wm.t_end_ns[j])
    span = slice(i, j + 1)
    k = i + int(np.argmax(np.abs(z[span])))
    peak = float(z[k])
    st = by_bin[wm.bins[k]]
    level = float(np.median(wm.median[span]))
    unit = SIGNALS[signal].unit if signal in SIGNALS else ""
    trigger = Citation(
        signal=signal,
        source=label(signal),
        role="trigger",
        t_start_ns=lo,
        t_end_ns=hi,
        stat="median",
        stat_value=level,
        unit=unit,
        n=int(np.sum(wm.n[span])),
    )
    context = Citation(
        signal=signal,
        source=label(signal),
        role="context",
        t_start_ns=lo,
        t_end_ns=hi,
        stat=baseline_stat_text(st),
        stat_value=st.median,
        unit=unit,
        n=st.n_windows,
    )
    title = SIGNALS[signal].title if signal in SIGNALS else signal
    side = "over" if peak > 0 else "under"
    return Event(
        kind="alert.baseline",
        rule_id="baseline",
        rule_version=RULE_VERSION,
        severity=SEVERITY,
        t_start_ns=lo,
        t_end_ns=hi,
        value=peak,
        threshold=z_flag,
        unit="z",
        message=(
            f"{title} {_display(signal, level)}, {_z_text(abs(peak), z_flag)} sigma {side} "
            f"its baseline of {_display(signal, st.median)}."
        ),
        citations=(trigger, context),
    )


def operating_bins(
    rpm: ArrayLike,
    coolant_k: ArrayLike,
    speed_mps: ArrayLike,
    cfg: Config,
    *,
    closed_loop: ArrayLike | None = None,
) -> list[str]:
    """Operating-state bin per sample: engine_off, warm_up, warm_idle, or warm_moving.

    Engine off is rpm <= 0 (or unknown); warm-up is coolant under
    ``rules.trim_warn.min_coolant_c``; idle is speed under ``behavior.min_speed_mps``.
    Samples with unknown coolant, or outside closed loop when ``closed_loop`` is given, get
    ``EXCLUDED``. Cruise and high load stay one bin until a load threshold is approved.
    """
    r = np.asarray(rpm, dtype=np.float64)
    n = r.size
    c = as_values(coolant_k, n, "coolant_k")
    v = as_values(speed_mps, n, "speed_mps")
    cl = None if closed_loop is None else np.asarray(closed_loop, dtype=np.bool_)
    warm_k = c_to_k(float(cfg.get("rules.trim_warn.min_coolant_c")))
    idle = float(cfg.get("behavior.min_speed_mps"))
    out: list[str] = []
    for i in range(n):
        if cl is not None and not bool(cl[i]):
            out.append(EXCLUDED)
        elif not (math.isfinite(r[i]) and r[i] > 0.0):
            out.append("engine_off")
        elif not math.isfinite(c[i]):
            out.append(EXCLUDED)
        elif c[i] < warm_k:
            out.append("warm_up")
        elif v[i] < idle:
            out.append("warm_idle")
        else:
            out.append("warm_moving")
    return out


__all__ = [
    "EXCLUDED",
    "PID_RESOLUTION",
    "BaselineStats",
    "BaselineWindow",
    "WindowMedians",
    "baseline_stat_text",
    "check",
    "is_armed",
    "operating_bins",
    "resolution",
    "to_windows",
    "update",
    "window_medians",
]

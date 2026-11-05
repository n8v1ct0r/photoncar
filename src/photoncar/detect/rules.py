"""Anomaly rules over OBD samples, with on-delays, clear holds, gates, and stale detection.

Every timer runs on record time. A rule "fires" when its condition holds from the first
qualifying sample to a later qualifying sample at least ``on_s`` apart. It resolves when
its clear condition holds for ``clear_s`` (rules without an approved clear hold resolve at
the first sample back inside the limit). One alert covers one episode: an episode that
fires again within ``rules.shared.realert_s`` of resolving continues as the same alert, so
an event is emitted once the re-alert interval has passed, or at ``finish``.

Missing evidence never counts as normal. A sample gap longer than
``rules.shared.stale_after_polls`` poll periods, or a failed gate, resets any on-delay and
clear hold in progress. A signal whose updates stop while others continue raises an
``alert.stale`` episode.

Every alert carries at least one ``trigger`` citation: the signal, the on-delay window, and
the statistic over that window that proves the condition (for example the minimum coolant
temperature while it stayed over the limit).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from photoncar.config import Config
from photoncar.records import Citation, Event, Sample
from photoncar.signals import SIGNALS, label
from photoncar.units import c_to_k, duration_text, k_to_c

RULE_VERSION = 1

COOLANT = "obd.coolant_temp"
FUEL_SYSTEM = "obd.fuel_system"
RPM = "obd.rpm"
VOLTAGE = "obd.module_voltage"
TRIMS = {1: ("obd.stft_b1", "obd.ltft_b1"), 2: ("obd.stft_b2", "obd.ltft_b2")}
TRACKED = (COOLANT, FUEL_SYSTEM, RPM, VOLTAGE, *TRIMS[1], *TRIMS[2])
CLOSED_LOOP = 2

SEVERITY = {
    "coolant_high": 2,
    "trim_warn": 1,
    "trim_critical": 2,
    "voltage_low_running": 2,
    "voltage_low_rested": 1,
    "stale": 1,
}


def _ns(s: float) -> int:
    return round(s * 1e9)


def stale_message(signal: str, gap_s: float) -> str:
    """The stale alert message for ``signal`` after ``gap_s`` seconds without an update."""
    return f"{SIGNALS[signal].title} stale, no update for {duration_text(gap_s)}."


def _distinct(
    value: float, limit: float, beyond: Callable[[float, float], bool], min_decimals: int = 0
) -> str:
    """Format ``value`` with the fewest decimals (``min_decimals`` to 3) that still reads beyond
    ``limit``, so rounding never prints a value equal to the limit it broke."""
    for d in range(min_decimals, 4):
        text = f"{value:.{d}f}"
        if beyond(float(text), limit):
            return text
    return f"{value:.3f}"


def _above(a: float, b: float) -> bool:
    return a > b


def _below(a: float, b: float) -> bool:
    return a < b


@dataclass(slots=True)
class _Episode:
    onset_ns: int
    fire_ns: int
    triggers: tuple[Citation, ...]
    extreme: float
    n: int
    clear_start_ns: int | None = None
    resolved_at_ns: int | None = None


@dataclass(frozen=True, slots=True)
class _Eval:
    t_ns: int
    cond: bool
    clear: bool
    value: float


class _History:
    """Recent samples per signal, for citation statistics over a window."""

    def __init__(self, keep_ns: int) -> None:
        self._keep = keep_ns
        self._d: dict[str, deque[tuple[int, float]]] = {}

    def add(self, signal: str, t_ns: int, value: float) -> None:
        d = self._d.setdefault(signal, deque())
        d.append((t_ns, value))
        while d and t_ns - d[0][0] > self._keep:
            d.popleft()

    def window(self, signal: str, lo_ns: int, hi_ns: int) -> list[float]:
        return [v for t, v in self._d.get(signal, ()) if lo_ns <= t <= hi_ns]


def _cite(
    signal: str, lo_ns: int, hi_ns: int, stat: str, value: float, n: int, role: str = "trigger"
) -> Citation:
    return Citation(
        signal=signal,
        source=label(signal),
        role="trigger" if role == "trigger" else "context",
        t_start_ns=lo_ns,
        t_end_ns=hi_ns if hi_ns > lo_ns else lo_ns + 1,
        stat=stat,
        stat_value=value,
        unit=SIGNALS[signal].unit,
        n=n,
    )


class _Tracker:
    """On-delay, clear hold, and re-alert merging for one rule instance."""

    def __init__(
        self,
        *,
        on_ns: int,
        clear_ns: int,
        realert_ns: int,
        worse: Callable[[float, float], bool],
        triggers: Callable[[int, int], tuple[Citation, ...]],
        build: Callable[[_Episode, int, bool], Event],
    ) -> None:
        self._on = on_ns
        self._clear = clear_ns
        self._realert = realert_ns
        self._worse = worse
        self._triggers = triggers
        self._build = build
        self._pending: list[_Eval] = []
        self._ep: _Episode | None = None

    def expire(self, t_ns: int) -> list[Event]:
        ep = self._ep
        if (
            ep is not None
            and ep.resolved_at_ns is not None
            and (t_ns - ep.resolved_at_ns > self._realert)
        ):
            self._ep = None
            return [self._build(ep, self._end(ep), True)]
        return []

    def no_evidence(self, t_ns: int) -> list[Event]:
        """A gap or a failed gate: nothing in progress can be proven across it."""
        self._pending.clear()
        if self._ep is not None and self._ep.resolved_at_ns is None:
            self._ep.clear_start_ns = None
        return self.expire(t_ns)

    def step(self, e: _Eval) -> list[Event]:
        out = self.expire(e.t_ns)
        ep = self._ep
        if ep is not None and ep.resolved_at_ns is None:
            ep.n += 1
            if self._worse(e.value, ep.extreme):
                ep.extreme = e.value
            if e.clear:
                if ep.clear_start_ns is None:
                    ep.clear_start_ns = e.t_ns
                if e.t_ns - ep.clear_start_ns >= self._clear:
                    ep.resolved_at_ns = e.t_ns
            else:
                ep.clear_start_ns = None
            return out
        if not e.cond:
            self._pending.clear()
            return out
        self._pending.append(e)
        onset = self._pending[0].t_ns
        if e.t_ns - onset < self._on:
            return out
        extreme = self._pending[0].value
        for p in self._pending[1:]:
            if self._worse(p.value, extreme):
                extreme = p.value
        if ep is not None:
            # Fired again inside the re-alert interval: the same episode continues.
            ep.resolved_at_ns = None
            ep.clear_start_ns = None
            ep.n += len(self._pending)
            if self._worse(extreme, ep.extreme):
                ep.extreme = extreme
        else:
            self._ep = _Episode(
                onset_ns=onset,
                fire_ns=e.t_ns,
                triggers=self._triggers(onset, e.t_ns),
                extreme=extreme,
                n=len(self._pending),
            )
        self._pending.clear()
        return out

    def finish(self, t_ns: int) -> list[Event]:
        ep, self._ep = self._ep, None
        self._pending.clear()
        if ep is None:
            return []
        if ep.resolved_at_ns is not None:
            return [self._build(ep, self._end(ep), True)]
        return [self._build(ep, max(t_ns, ep.fire_ns, ep.onset_ns + 1), False)]

    def active(self, t_ns: int) -> Event | None:
        """The episode as an ongoing alert ending at ``t_ns``, if it has fired and not resolved.

        Read-only: the episode keeps running and is still emitted once, when it closes.
        """
        ep = self._ep
        if ep is None or ep.resolved_at_ns is not None or t_ns < ep.fire_ns:
            return None
        return self._build(ep, max(t_ns, ep.fire_ns, ep.onset_ns + 1), False)

    @staticmethod
    def _end(ep: _Episode) -> int:
        end = ep.clear_start_ns if ep.clear_start_ns is not None else ep.resolved_at_ns
        assert end is not None
        return max(end, ep.onset_ns + 1)


class RuleEngine:
    """Push OBD ``Sample`` records in time order; returns alert ``Event`` records.

    ``rested_h`` is the time since the previous drive ended, in hours, from the drive index.
    The rested-voltage advisory runs only when it is known and at least
    ``rules.voltage_low_rested.min_rest_h``.
    """

    def __init__(self, cfg: Config, *, rested_h: float | None = None) -> None:
        r = cfg.section("rules")
        shared = r["shared"]
        realert = _ns(float(shared["realert_s"]))
        self._limit = self._stale_limits(cfg)
        self._last: dict[str, tuple[int, float]] = {}
        self._stale_open: dict[str, int] = {}
        self._finished = False

        ch = r["coolant_high"]
        self._coolant_thr = c_to_k(float(ch["threshold_c"]))
        self._coolant_clear = c_to_k(float(ch["clear_below_c"]))
        tw = r["trim_warn"]
        self._trim_warn = float(tw["limit_pct"]) / 100.0
        self._min_coolant = c_to_k(float(tw["min_coolant_c"]))
        tc = r["trim_critical"]
        self._trim_crit = float(tc["limit_pct"]) / 100.0
        vr = r["voltage_low_running"]
        self._v_run = float(vr["threshold_v"])
        self._v_ignore = _ns(float(vr["ignore_after_start_s"]))
        vz = r["voltage_low_rested"]
        kind = str(vz["battery_type"])
        if kind not in ("agm", "flooded"):
            raise ValueError("rules.voltage_low_rested.battery_type must be agm or flooded")
        self._v_rest = float(vz["threshold_agm_v"] if kind == "agm" else vz["threshold_flooded_v"])
        self._rest_enabled = rested_h is not None and rested_h >= float(vz["min_rest_h"])

        on_times = [float(ch["on_s"]), float(tw["on_s"]), float(tc["on_s"]), float(vr["on_s"])]
        self._hist = _History(_ns(2.0 * max(on_times)) + max(self._limit.values()))

        self._running = False
        self._engine_start: int | None = None
        self._ever_running = False
        self._rest_first: tuple[int, float] | None = None
        self._rest_n = 0
        self._rest_done = False

        self._coolant = _Tracker(
            on_ns=_ns(float(ch["on_s"])),
            clear_ns=_ns(float(ch["clear_s"])),
            realert_ns=realert,
            worse=_above,
            triggers=lambda lo, hi: (self._stat_cite(COOLANT, lo, hi, "min"),),
            build=self._coolant_event,
        )
        self._voltage = _Tracker(
            on_ns=_ns(float(vr["on_s"])),
            clear_ns=0,
            realert_ns=realert,
            worse=_below,
            triggers=lambda lo, hi: (self._stat_cite(VOLTAGE, lo, hi, "max"),),
            build=self._voltage_event,
        )
        self._warn: dict[int, _Tracker] = {}
        self._crit: dict[str, _Tracker] = {}
        for bank, (st, lt) in TRIMS.items():
            self._warn[bank] = _Tracker(
                on_ns=_ns(float(tw["on_s"])),
                clear_ns=0,
                realert_ns=realert,
                worse=lambda a, b: abs(a) > abs(b),
                triggers=self._warn_triggers(st, lt),
                build=self._warn_builder(bank),
            )
            for sig in (st, lt):
                self._crit[sig] = _Tracker(
                    on_ns=_ns(float(tc["on_s"])),
                    clear_ns=0,
                    realert_ns=realert,
                    worse=lambda a, b: abs(a) > abs(b),
                    triggers=self._crit_triggers(sig),
                    build=self._crit_builder(bank, sig),
                )

    @staticmethod
    def _stale_limits(cfg: Config) -> dict[str, int]:
        obd = cfg.section("obd")
        polls = int(cfg.get("rules.shared.stale_after_polls"))
        fast = {int(p) for p in obd["fast_pids"]}
        slow = [int(p) for p in obd["slow_pids"]]
        fast_period = 1.0 / float(obd["fast_hz_each"])
        slow_period = len(slow) / float(obd["slow_hz_total"])
        out: dict[str, int] = {}
        for sig in TRACKED:
            pid = SIGNALS[sig].pid
            if pid in fast:
                out[sig] = _ns(polls * fast_period)
            elif pid in slow:
                out[sig] = _ns(polls * slow_period)
            else:
                raise ValueError(f"{sig} is not polled in config [obd]")
        return out

    def stale_limit_s(self, signal: str) -> float:
        """Seconds without an update after which ``signal`` counts as stale."""
        return self._limit[signal] / 1e9

    # Sample intake -------------------------------------------------------------------------

    def push(self, sample: Sample) -> list[Event]:
        if self._finished:
            raise RuntimeError("push after finish")
        sig = sample.signal
        if sig not in self._limit or not math.isfinite(sample.value):
            return []
        t = sample.t_ns
        out = self._stale_step(sig, t)
        prev = self._last.get(sig)
        gap = prev is not None and t - prev[0] > self._limit[sig]
        self._last[sig] = (t, sample.value)
        self._hist.add(sig, t, sample.value)
        for tr in self._trackers():
            out += tr.expire(t)
        if sig == RPM:
            out += self._on_rpm(t, sample.value)
        elif sig == COOLANT:
            out += self._on_coolant(t, sample.value, gap)
        elif sig == VOLTAGE:
            out += self._on_voltage(t, sample.value, gap)
        elif sig != FUEL_SYSTEM:
            out += self._on_trim(sig, t, gap)
        return out

    def finish(self, t_ns: int) -> list[Event]:
        """Close every open episode: resolved ones as resolved, the rest as ongoing."""
        if self._finished:
            return []
        self._finished = True
        out: list[Event] = []
        for tr in self._trackers():
            out += tr.finish(t_ns)
        out += self._close_rest_window(t_ns)
        for sig, last in sorted(self._stale_open.items()):
            out.append(self._stale_event(sig, last, max(t_ns, last + 1), resolved=False, n=1))
        self._stale_open.clear()
        out.sort(key=lambda e: (e.t_start_ns, e.rule_id))
        return out

    def active(self, now_ns: int) -> list[Event]:
        """Alerts active at ``now_ns``, for live frames: fired and not resolved yet.

        Each comes back as an unresolved ``Event`` ending at ``now_ns``, with the same trigger
        citations it will carry when it closes. Signals that have gone without an update for
        longer than their stale limit count as active stale alerts. Read-only: nothing here
        changes what ``push`` and ``finish`` emit. Ordered by start time, then rule.
        """
        out = [ev for tr in self._trackers() if (ev := tr.active(now_ns)) is not None]
        for sig, (last_t, _) in sorted(self._last.items()):
            stale_since = self._stale_open.get(sig)
            if stale_since is None and now_ns - last_t > self._limit[sig]:
                stale_since = last_t
            if stale_since is not None:
                end = max(now_ns, stale_since + 1)
                out.append(self._stale_event(sig, stale_since, end, resolved=False, n=1))
        out.sort(key=lambda e: (e.t_start_ns, e.rule_id))
        return out

    def _trackers(self) -> list[_Tracker]:
        return [self._coolant, self._voltage, *self._warn.values(), *self._crit.values()]

    def _fresh(self, sig: str, t: int) -> tuple[int, float] | None:
        last = self._last.get(sig)
        if last is None or t - last[0] > self._limit[sig]:
            return None
        return last

    def _gates_ok(self, t: int) -> bool:
        fs = self._fresh(FUEL_SYSTEM, t)
        cool = self._fresh(COOLANT, t)
        return (
            fs is not None
            and int(fs[1]) == CLOSED_LOOP
            and cool is not None
            and cool[1] >= self._min_coolant
        )

    # Rules ---------------------------------------------------------------------------------

    def _on_coolant(self, t: int, value: float, gap: bool) -> list[Event]:
        out = self._coolant.no_evidence(t) if gap else []
        out += self._coolant.step(
            _Eval(t, value > self._coolant_thr, value < self._coolant_clear, value)
        )
        return out

    def _on_rpm(self, t: int, value: float) -> list[Event]:
        running = value > 0.0
        if running and not self._running:
            self._engine_start = t
        self._running = running
        out: list[Event] = []
        if running and not self._ever_running:
            self._ever_running = True
            out += self._close_rest_window(t)
        return out

    def _on_voltage(self, t: int, value: float, gap: bool) -> list[Event]:
        if not self._ever_running and not self._rest_done:
            if self._rest_first is None:
                self._rest_first = (t, value)
            self._rest_n += 1
        rpm = self._fresh(RPM, t)
        start = self._engine_start
        if gap or rpm is None or rpm[1] <= 0.0 or start is None or t - start < self._v_ignore:
            return self._voltage.no_evidence(t)
        below = value < self._v_run
        return self._voltage.step(_Eval(t, below, not below, value))

    def _on_trim(self, sig: str, t: int, gap: bool) -> list[Event]:
        bank = 1 if sig in TRIMS[1] else 2
        st, lt = TRIMS[bank]
        out: list[Event] = []
        warn, crit = self._warn[bank], self._crit[sig]
        gates = self._gates_ok(t)
        stv, ltv = self._fresh(st, t), self._fresh(lt, t)
        if gap or not gates or stv is None or ltv is None:
            out += warn.no_evidence(t)
        else:
            total = stv[1] + ltv[1]
            outside = abs(total) > self._trim_warn
            out += warn.step(_Eval(t, outside, not outside, total))
        own = self._last[sig][1]
        if gap or not gates:
            out += crit.no_evidence(t)
        else:
            outside = abs(own) > self._trim_crit
            out += crit.step(_Eval(t, outside, not outside, own))
        return out

    def _close_rest_window(self, t_close: int) -> list[Event]:
        """The key-on window ends at engine start (or at finish): judge the rested voltage."""
        if self._rest_done:
            return []
        self._rest_done = True
        first = self._rest_first
        if not self._rest_enabled or first is None or t_close <= first[0]:
            return []
        t0, v0 = first
        if not v0 < self._v_rest:
            return []
        cite = _cite(VOLTAGE, t0, t_close, "first", v0, self._rest_n)
        v_txt = _distinct(v0, self._v_rest, _below, min_decimals=2)
        return [
            Event(
                kind="alert.voltage_low_rested",
                rule_id="voltage_low_rested",
                rule_version=RULE_VERSION,
                severity=SEVERITY["voltage_low_rested"],
                t_start_ns=t0,
                t_end_ns=t_close,
                value=v0,
                threshold=self._v_rest,
                unit="V",
                message=(
                    f"Rested module voltage {v_txt} V, under {self._v_rest:.2f} V. Advisory only."
                ),
                citations=(cite,),
            )
        ]

    # Stale detection -----------------------------------------------------------------------

    def _stale_step(self, sig: str, t: int) -> list[Event]:
        out: list[Event] = []
        for other, (last_t, _) in self._last.items():
            if other != sig and other not in self._stale_open and t - last_t > self._limit[other]:
                self._stale_open[other] = last_t
        prev = self._last.get(sig)
        if prev is not None and (sig in self._stale_open or t - prev[0] > self._limit[sig]):
            self._stale_open.pop(sig, None)
            out.append(self._stale_event(sig, prev[0], t, resolved=True, n=2))
        return out

    def _stale_event(self, sig: str, last_t: int, end_t: int, *, resolved: bool, n: int) -> Event:
        gap_s = (end_t - last_t) / 1e9
        limit_s = self._limit[sig] / 1e9
        cite = Citation(
            signal=sig,
            source=label(sig),
            role="trigger",
            t_start_ns=last_t,
            t_end_ns=end_t,
            stat="gap_s",
            stat_value=gap_s,
            unit="s",
            n=n,
        )
        return Event(
            kind="alert.stale",
            rule_id="stale",
            rule_version=RULE_VERSION,
            severity=SEVERITY["stale"],
            t_start_ns=last_t,
            t_end_ns=end_t,
            value=gap_s,
            threshold=limit_s,
            unit="s",
            message=stale_message(sig, gap_s),
            citations=(cite,),
            resolved=resolved,
        )

    # Citations and events ------------------------------------------------------------------

    def _stat_cite(self, sig: str, lo: int, hi: int, stat: str) -> Citation:
        vals = self._hist.window(sig, lo, hi)
        value = min(vals) if stat == "min" else max(vals)
        return _cite(sig, lo, hi, stat, value, len(vals))

    def _warn_triggers(self, st: str, lt: str) -> Callable[[int, int], tuple[Citation, ...]]:
        def make(lo: int, hi: int) -> tuple[Citation, ...]:
            out = []
            for sig in (st, lt):
                vals = self._hist.window(sig, lo, hi)
                if not vals:
                    last = self._last[sig][1]
                    vals = [last]
                out.append(_cite(sig, lo, hi, "mean", sum(vals) / len(vals), len(vals)))
            return tuple(out)

        return make

    def _crit_triggers(self, sig: str) -> Callable[[int, int], tuple[Citation, ...]]:
        def make(lo: int, hi: int) -> tuple[Citation, ...]:
            vals = self._hist.window(sig, lo, hi)
            return (_cite(sig, lo, hi, "min_abs", min(abs(v) for v in vals), len(vals)),)

        return make

    def _coolant_event(self, ep: _Episode, t_end: int, resolved: bool) -> Event:
        c = k_to_c(ep.extreme)
        thr_c = k_to_c(self._coolant_thr)
        context = _cite(COOLANT, ep.onset_ns, t_end, "max", ep.extreme, ep.n, role="context")
        return Event(
            kind="alert.coolant_high",
            rule_id="coolant_high",
            rule_version=RULE_VERSION,
            severity=SEVERITY["coolant_high"],
            t_start_ns=ep.onset_ns,
            t_end_ns=t_end,
            value=ep.extreme,
            threshold=self._coolant_thr,
            unit="K",
            message=f"Coolant {_distinct(c, thr_c, _above)} °C, over {thr_c:.0f} °C.",
            citations=(*ep.triggers, context),
            resolved=resolved,
        )

    def _voltage_event(self, ep: _Episode, t_end: int, resolved: bool) -> Event:
        context = _cite(VOLTAGE, ep.onset_ns, t_end, "min", ep.extreme, ep.n, role="context")
        v_txt = _distinct(ep.extreme, self._v_run, _below, min_decimals=1)
        return Event(
            kind="alert.voltage_low_running",
            rule_id="voltage_low_running",
            rule_version=RULE_VERSION,
            severity=SEVERITY["voltage_low_running"],
            t_start_ns=ep.onset_ns,
            t_end_ns=t_end,
            value=ep.extreme,
            threshold=self._v_run,
            unit="V",
            message=f"Module voltage {v_txt} V, under {self._v_run:.1f} V.",
            citations=(*ep.triggers, context),
            resolved=resolved,
        )

    def _warn_builder(self, bank: int) -> Callable[[_Episode, int, bool], Event]:
        def build(ep: _Episode, t_end: int, resolved: bool) -> Event:
            pct = ep.extreme * 100.0
            lim = self._trim_warn * 100.0
            text = _distinct(abs(pct), lim, _above)
            sign = "+" if pct >= 0 else "-"
            return Event(
                kind="alert.trim_warn",
                rule_id="trim_warn",
                rule_version=RULE_VERSION,
                severity=SEVERITY["trim_warn"],
                t_start_ns=ep.onset_ns,
                t_end_ns=t_end,
                value=ep.extreme,
                threshold=self._trim_warn,
                unit="ratio",
                message=f"Bank {bank} fuel trim {sign}{text}%, outside ±{lim:.0f}%.",
                citations=ep.triggers,
                resolved=resolved,
            )

        return build

    def _crit_builder(self, bank: int, sig: str) -> Callable[[_Episode, int, bool], Event]:
        term = "short-term" if sig.startswith("obd.stft") else "long-term"

        def build(ep: _Episode, t_end: int, resolved: bool) -> Event:
            pct = ep.extreme * 100.0
            lim = self._trim_crit * 100.0
            text = _distinct(abs(pct), lim, _above)
            sign = "+" if pct >= 0 else "-"
            return Event(
                kind="alert.trim_critical",
                rule_id="trim_critical",
                rule_version=RULE_VERSION,
                severity=SEVERITY["trim_critical"],
                t_start_ns=ep.onset_ns,
                t_end_ns=t_end,
                value=ep.extreme,
                threshold=self._trim_crit,
                unit="ratio",
                message=f"Bank {bank} {term} fuel trim {sign}{text}%, outside ±{lim:.0f}%.",
                citations=ep.triggers,
                resolved=resolved,
            )

        return build

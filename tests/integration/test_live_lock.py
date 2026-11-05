"""The moving lock on a live run, in real time: a silent source must never freeze it.

The sources here stamp records on the monotonic clock as they yield them, as the live CAN
session, the GPS, and the IMU do. The run goes on a background thread and the test watches
``LiveState.moving`` from outside, the way the API guard does.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from photoncar.api.live import LiveState
from photoncar.clock import MonotonicClock
from photoncar.config import Config, load_config, load_vehicle
from photoncar.pipeline.runner import RunReport, run_to_data_dir
from photoncar.records import GpsFix, Record, Sample
from photoncar.sources.base import Source, SourceMeta

TICK_S = 0.1
"""Both fake sources yield at 10 Hz, the GPS rate and near the fast OBD poll rate."""


class SpeedSession:
    """Stands in for the live CAN session: OBD speed at 10 Hz, set by the test. With
    ``silent`` set it keeps running but yields nothing, as a bus that went quiet."""

    def __init__(self, clock: MonotonicClock, stop: threading.Event) -> None:
        self._clock = clock
        self._stop = stop
        self.speed_mps = 0.0
        self.silent = threading.Event()

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name="live:can0+can1", kind="live")

    def records(self) -> Iterator[Record]:
        while not self._stop.is_set():
            if not self.silent.is_set():
                yield Sample(self._clock.now_ns(), "obd.speed", self.speed_mps)
            self._stop.wait(TICK_S)


class FixLossGps:
    """Stands in for ``GpsUbloxSource``: fixes at 10 Hz while ``has_fix`` is set. Without a fix
    it keeps reading and yields nothing, as the real source drops unusable NAV-PVT frames."""

    def __init__(self, clock: MonotonicClock, stop: threading.Event) -> None:
        self._clock = clock
        self._stop = stop
        self.has_fix = threading.Event()
        self.has_fix.set()

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name="gps:/dev/ttyACM0", kind="live")

    def records(self) -> Iterator[Record]:
        while not self._stop.is_set():
            if self.has_fix.is_set():
                t = self._clock.now_ns()
                yield GpsFix(
                    t_ns=t,
                    rx_t_ns=t,
                    utc_ns=None,
                    lat_deg=0.0,
                    lon_deg=0.0,
                    alt_m=0.0,
                    speed_mps=0.0,
                    course_rad=0.0,
                    hdop=1.0,
                    fix_type=3,
                    n_sats=9,
                )
            self._stop.wait(TICK_S)


def _cfg(**estimator: float) -> Config:
    base = load_config()
    raw: dict[str, Any] = {k: dict(v) if isinstance(v, Mapping) else v for k, v in base.raw.items()}
    raw["estimator"].update(estimator)
    return Config(raw)


def _wait_for(check: Callable[[], bool], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


class LiveRun:
    """1 live drive on a background thread; ``finish`` stops the sources and joins it."""

    def __init__(self, sources: list[Source], cfg: Config, data_dir: Path, clock: MonotonicClock):
        # Locked before the run starts, as ``photoncar record`` does (``lock_live``).
        self.live = LiveState(mode="live", moving=True)
        self.report: RunReport | None = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, args=(sources, cfg, data_dir, clock), daemon=True
        )

    def _run(
        self, sources: list[Source], cfg: Config, data_dir: Path, clock: MonotonicClock
    ) -> None:
        try:
            self.report = run_to_data_dir(
                sources,
                cfg,
                load_vehicle(str(cfg.get("vehicle.profile"))),
                data_dir,
                vehicle_profile=None,
                label="Live drive",
                live=self.live,
                live_clock=clock,
            )
        except BaseException as e:  # reported by finish()
            self.error = e

    def start(self) -> LiveRun:
        self._thread.start()
        return self

    def wait_parked(self, timeout_s: float = 6.0) -> None:
        """Until the lock opens: 2 s at rest (``estimator.stationary_s``) after the first speed."""
        assert _wait_for(lambda: not self.live.moving, timeout_s), "the lock never opened at rest"

    def finish(self, stop: threading.Event) -> RunReport:
        stop.set()
        self._thread.join(10.0)
        assert not self._thread.is_alive(), "the live run did not end after its sources stopped"
        if self.error is not None:
            raise self.error
        assert self.report is not None
        return self.report


def test_a_gps_without_a_fix_does_not_freeze_the_moving_lock(tmp_path: Path) -> None:
    """The car rests (the lock opens), the GPS loses its fix, then the car drives off on OBD
    speed alone. The lock must close again: parked views never stay open while it moves."""
    clock = MonotonicClock()
    stop = threading.Event()
    session = SpeedSession(clock, stop)
    gps = FixLossGps(clock, stop)
    run = LiveRun([session, gps], load_config(), tmp_path, clock).start()
    try:
        run.wait_parked()
        gps.has_fix.clear()  # a garage: the receiver has no usable fix
        time.sleep(0.5)
        session.speed_mps = 20.0  # drives off
        assert _wait_for(lambda: run.live.moving, 2.0), (
            "OBD speed reads 20 m/s but the parked views stay open"
        )
    finally:
        rep = run.finish(stop)
    assert rep.n_errors == 0


def test_the_lock_closes_when_every_source_goes_silent(tmp_path: Path) -> None:
    """With no state estimate for ``estimator.max_dead_reckon_s`` the device counts as moving,
    even when no record arrives at all to drive the pipeline."""
    lost_s = 1.0
    clock = MonotonicClock()
    stop = threading.Event()
    session = SpeedSession(clock, stop)
    run = LiveRun([session], _cfg(max_dead_reckon_s=lost_s), tmp_path, clock).start()
    try:
        run.wait_parked()
        session.silent.set()  # the speed source goes quiet: nothing reaches the pipeline
        t0 = time.monotonic()
        assert _wait_for(lambda: run.live.moving, lost_s + 2.0), (
            "no speed for longer than max_dead_reckon_s, yet the parked views stay open"
        )
        assert time.monotonic() - t0 >= lost_s * 0.9  # not before the limit
    finally:
        run.finish(stop)


@pytest.mark.parametrize("silent_first", [False, True])
def test_a_silent_source_that_comes_back_keeps_time_order(
    tmp_path: Path, silent_first: bool
) -> None:
    """Records merge in time order across a GPS that drops out and comes back."""
    clock = MonotonicClock()
    stop = threading.Event()
    session = SpeedSession(clock, stop)
    gps = FixLossGps(clock, stop)
    if silent_first:
        gps.has_fix.clear()
    run = LiveRun([session, gps], load_config(), tmp_path, clock).start()
    try:
        time.sleep(0.6)
        if silent_first:
            gps.has_fix.set()
        else:
            gps.has_fix.clear()
        time.sleep(0.6)
        gps.has_fix.set()
        time.sleep(0.6)
    finally:
        rep = run.finish(stop)
    assert rep.records_in.get("Sample", 0) > 10 and rep.records_in.get("GpsFix", 0) > 5
    assert rep.errors.get("late_record", 0) == 0

"""Replay of the pinned comma2k19 segment (make fetch-dataset). Counts are exact for the pinned
files, which the first test checks by SHA-256; rates carry stated tolerances."""

from __future__ import annotations

import hashlib
import json
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

from photoncar.records import CanFrame, GpsFix, ImuSample, ObdPoll, record_time
from photoncar.sources.comma2k19 import Comma2k19Source, default_dataset_root
from photoncar.sources.synthetic import SyntheticObdSource
from photoncar.units import MPH_60, NS_PER_S

pytestmark = pytest.mark.dataset

ROOT = default_dataset_root()
SEGMENT_REF = "Example_1/b0c9d2329ad1606b|2018-08-02--08-34-47/40"
ROUTE_DIR = ROOT / "Chunk_2" / "b0c9d2329ad1606b|2018-11-11--13-08-44"
MANIFEST = Path(__file__).resolve().parents[2] / "scripts" / "comma2k19_manifest.json"


@pytest.fixture(scope="module")
def seg() -> Comma2k19Source:
    if not (ROOT / SEGMENT_REF).is_dir():
        pytest.fail("The comma2k19 sample is missing. Run make fetch-dataset.")
    return Comma2k19Source(ROOT / SEGMENT_REF)


def _rate(t_ns: list[int]) -> float:
    return (len(t_ns) - 1) / ((t_ns[-1] - t_ns[0]) / NS_PER_S)


def test_files_match_pinned_hashes(seg: Comma2k19Source) -> None:
    files = json.loads(MANIFEST.read_text())["files"]
    assert len(files) == 38
    for f in files:
        data = (ROOT / f["path"]).read_bytes()
        assert (len(data), hashlib.sha256(data).hexdigest()) == (f["bytes"], f["sha256"])


def test_counts(seg: Comma2k19Source) -> None:
    c = seg.counts()
    assert c.can_by_bus == {0: 53800, 1: 45000}
    assert c.dropped_can_by_src == {128: 12434, 129: 24234}  # logger TX echoes
    assert (c.gps_fixes, c.gps_dropped, c.imu_samples) == (579, 0, 6256)


def test_rates_and_time_order(seg: Comma2k19Source) -> None:
    recs = list(seg.records())
    assert len(recs) == 53800 + 45000 + 579 + 6256
    times = [record_time(r) for r in recs]
    assert all(a <= b for a, b in pairwise(times))
    gps = [r.t_ns for r in recs if isinstance(r, GpsFix)]
    imu = [r.t_ns for r in recs if isinstance(r, ImuSample)]
    assert _rate(gps) == pytest.approx(9.68, abs=0.01)  # about 10 Hz
    assert _rate(imu) == pytest.approx(104.26, abs=0.01)  # about 104 Hz
    for bus, fps in ((0, 896.8), (1, 750.4)):
        t = [r.t_ns for r in recs if isinstance(r, CanFrame) and r.bus == bus]
        assert _rate(t) == pytest.approx(fps, abs=0.5)
    assert (times[-1] - times[0]) / NS_PER_S == pytest.approx(60.0, abs=0.1)


def test_frames_and_axes(seg: Comma2k19Source) -> None:
    frames = list(seg.can_frames())
    assert {len(f.data) for f in frames} == {8}
    assert {f.dlc for f in frames} == {8}
    assert not any(f.is_ext for f in frames)
    assert max(f.arb_id for f in frames) == 1779
    imu = list(seg.imu_samples())
    az = float(np.mean([s.az for s in imu]))
    assert 9.3 < az < 10.3  # z up: about +g
    assert abs(float(np.mean([s.ay for s in imu]))) < 1.0
    gps = list(seg.gps_fixes())
    assert all(g.utc_ns is not None and g.utc_ns > 1_500_000_000 * NS_PER_S for g in gps)
    assert all(np.isnan(g.hdop) and g.n_sats == 0 for g in gps)


def test_meta_and_speed_trace(seg: Comma2k19Source) -> None:
    assert seg.meta.kind == "replay"
    assert seg.meta.dataset_ref == SEGMENT_REF
    t, v = seg.speed_trace()
    assert len(t) == 4974
    assert float(v.min()) == pytest.approx(7.974, abs=0.001)
    assert float(v.max()) == pytest.approx(19.841, abs=0.001)


def test_synthetic_obd_overlay_on_replay(seg: Comma2k19Source) -> None:
    obd = SyntheticObdSource(seg, seed=11)
    assert (obd.meta.kind, obd.meta.dataset_ref) == ("synthetic", SEGMENT_REF)
    polls = [r for r in obd.records() if isinstance(r, ObdPoll)]
    assert len(polls) == pytest.approx(600, abs=1)  # 10 requests/s over 60 s
    t, v = seg.speed_trace()
    for p in polls:
        if p.pid == 0x0D and p.resp_t_ns is not None and p.value is not None:
            true_v = float(np.interp(p.resp_t_ns, t, v))
            assert abs(p.value - true_v) <= 0.5 / 3.6 + 1e-9


def test_route_standing_start() -> None:
    if not (ROUTE_DIR / "14").is_dir():
        pytest.skip("Optional route not fetched: uv run python scripts/fetch_comma2k19_route.py")
    ts, vs = [], []
    n_gps = 0
    for n in range(9, 24):
        s = Comma2k19Source(ROUTE_DIR / str(n))
        t, v = s.speed_trace()
        ts.append(t)
        vs.append(v)
        n_gps += s.counts().gps_fixes
    t = np.concatenate(ts)
    v = np.concatenate(vs)
    assert float(np.max(np.diff(t))) / NS_PER_S < 0.1  # segments 9 to 23 join without gaps
    span_s = (t[-1] - t[0]) / NS_PER_S
    assert span_s == pytest.approx(900.0, abs=0.1)
    assert n_gps == 4505  # about 5 Hz u-blox on this route, not 10 Hz
    runs = []
    last = -1
    for j in range(len(v)):
        if v[j] < 0.5:
            last = j
        if v[j] >= MPH_60 and last >= 0:
            runs.append((t[j] - t[last]) / NS_PER_S)
            last = -1
    assert len(runs) == 1
    assert runs[0] == pytest.approx(24.396, abs=0.001)

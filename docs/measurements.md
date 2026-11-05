# Measurements

Measured 2026-09-24 on the development Mac. These are replay numbers on a laptop. The Pi 5 realtime factor is a Phase 3 measurement and is not known yet.

## Machine

| Item | Value |
|---|---|
| Computer | Apple M5 Max (Mac17,7), 18 cores, 64 GB, on AC power |
| OS | macOS 26.5.2 (25F84) |
| Python, SQLite, numpy | 3.13.5, 3.49.1, 2.5.3 |
| Load | Another build workload shared the machine. Load average 4.66 before the runs and 10.65 after |

## Method

Each run replays into a fresh, empty data folder through the command a person types:

```
uv run photoncar replay --dataset comma2k19 --data-dir <empty folder>
uv run photoncar replay --dataset comma2k19 --route --data-dir <empty folder>
uv run photoncar seed-demo --data-dir <empty folder>
```

The replay output prints every number below: the data span, the wall time, the realtime factor, records per second, and a "Time by stage" line. Each case ran 3 times. The tables give the median of the 3 runs; each stage median is taken on its own.

- **Wall time:** `perf_counter_ns` inside `run_drive`, from the start of the mount rotation pre-pass to the end of the index update. It leaves out Python start-up and imports.
- **Realtime factor:** data span divided by wall time.
- **Records per second:** source records plus the records the pipeline made (decoded samples, state estimates, events, laps), divided by wall time.
- **Stages:** `prepass` is the IMU mount rotation estimate. Inside the streaming loop: `write` (every record into SQLite), `decode` (Toyota DBC), `estimate` (Kalman filter), `rules`, and `read_merge`, which is the rest of the loop: reading the sources, joining segments, `heapq.merge`, and dispatch. At close: `performance`, `behavior`, `laps`, `baseline`, `timeline_stats` (10 Hz timeline and drive stats), `close` (index build and WAL checkpoint), and `index`.

## Replay results

| | Pinned segment | Full default route |
|---|---|---|
| Data | `Example_1/b0c9d2329ad1606b\|2018-08-02--08-34-47/40` | `Chunk_2/b0c9d2329ad1606b\|2018-11-11--13-08-44/9..23` (15 segments) |
| Data span | 60.0 s | 900.0 s |
| Source records | 106,833 | 1,598,458 |
| Records made | 18,756 | 281,392 |
| Errors | 0 in each run | 0 in each run |
| Wall time, 3 runs | 0.52 s, 0.51 s, 0.52 s | 7.65 s, 7.91 s, 6.70 s |
| Wall time, median | 0.52 s | 7.65 s |
| Realtime factor, median | 114.8x | 117.7x |
| Records per second, median | 240,244 | 245,892 |
| Process time with start-up, median | 0.73 s | 7.86 s |

Source records by type. Pinned segment: CanFrame 98,800, GpsFix 579, ImuSample 6,256, ObdPoll 599, Sample 599. Route: CanFrame 1,482,127, GpsFix 4,505, ImuSample 93,828, ObdPoll 8,999, Sample 8,999. The ObdPoll and Sample records come from the synthetic OBD overlay.

Records made. Pinned segment: Event 6, Sample 12,495, StateEstimate 6,255. Route: Event 120, Sample 187,445, StateEstimate 93,827.

## Time by stage (seconds)

| Stage | Pinned segment, median | Pinned segment, 3 runs | Route, median | Route, 3 runs |
|---|---|---|---|---|
| prepass | 0.009 | 0.009, 0.008, 0.009 | 0.131 | 0.147, 0.131, 0.120 |
| write | 0.159 | 0.161, 0.157, 0.159 | 2.390 | 2.390, 2.608, 2.121 |
| decode | 0.042 | 0.042, 0.041, 0.043 | 0.623 | 0.623, 0.648, 0.518 |
| estimate | 0.033 | 0.034, 0.031, 0.033 | 0.495 | 0.495, 0.515, 0.372 |
| rules | 0.004 | 0.004, 0.004, 0.004 | 0.062 | 0.062, 0.067, 0.046 |
| read_merge | 0.219 | 0.220, 0.211, 0.219 | 3.258 | 3.258, 3.338, 2.875 |
| performance | 0.008 | 0.008, 0.008, 0.009 | 0.066 | 0.071, 0.066, 0.066 |
| behavior | 0.003 | 0.003, 0.003, 0.003 | 0.037 | 0.041, 0.037, 0.037 |
| laps | 0.000 | 0.000, 0.000, 0.000 | 0.000 | 0.000, 0.000, 0.000 |
| baseline | 0.001 | 0.002, 0.001, 0.001 | 0.016 | 0.018, 0.016, 0.016 |
| timeline_stats | 0.014 | 0.013, 0.014, 0.015 | 0.174 | 0.197, 0.173, 0.174 |
| close | 0.023 | 0.023, 0.023, 0.025 | 0.322 | 0.322, 0.291, 0.332 |
| index | 0.002 | 0.002, 0.002, 0.002 | 0.013 | 0.013, 0.013, 0.015 |

The stage medians add up to 0.517 s for the pinned segment and 7.587 s for the route, against median wall times of 0.52 s and 7.65 s. On the route, reading and merging (3.258 s) and SQLite writes (2.390 s) take 73.8% of the median wall time. Neither drive has a start and finish line, so `laps` does no work.

## seed-demo

`seed-demo` writes 3 drives: the pinned segment, the full route with its scripted synthetic coolant fault, and the synthetic track day.

| | Run 1 | Run 2 | Run 3 | Median |
|---|---|---|---|---|
| Inside the command ("Seeded 3 drives in ...") | 8.1 s | 8.3 s | 8.0 s | 8.1 s |
| Whole process, with start-up | 8.28 s | 8.47 s | 8.20 s | 8.28 s |

## Figure 1.2: the anomaly alert (plan H13)

The plan's default is a real baseline alert on a dataset CAN signal, armed across 3 or more dataset drives. No real alert fires, so the demo route carries the fallback: a scripted coolant fault on its synthetic OBD overlay, stored with `synthetic = 1`, and a message that ends "Synthetic fault."

What was tried:

1. The pinned segment and each of the 15 route segments ran as 16 separate learning drives through the pipeline, with the drive index on. 0 errors. The baseline learned 0 bins. Each drive reported "Baseline: no window qualified (bins need measured rpm and coolant). Nothing learned." The operating bins need a measured coolant temperature. The vendored Toyota DBC defines no coolant signal, and the replay mapping decodes engine rpm, wheel speeds, and steering angle only.
2. A relaxed check outside the product code treated every engine-running sample as warm and binned `can.engine_rpm` by the approved bins otherwise. All 16 drives gave 88 ten-second windows: 70 warm moving and 18 warm idle. Arming needs 180 windows in a bin (`baseline.arm_windows`), so no bin arms.
3. With arming turned off (`arm_windows = 0`), each drive was checked against a baseline learned from the other 15. 0 baseline alerts fired.

The coolant fault on the route overlay: 90 °C, a 10 s rise to 104 °C from 530 s into the route, 106 °C for 20 s from 540 s, then a 20 s fall from 101 °C to 90 °C. The coolant rule fires on it. `tests/integration/test_api_seeded.py` checks the alert, its trigger citation (OBD 0x05, stat min), its synthetic flag, and the live frame headline "Coolant 106 °C".

## Re-measure after the final Phase 2 fixes (2026-09-26)

Same machine, no other build running. `uv run photoncar replay --dataset comma2k19 --route --data-dir <empty folder>`, 3 runs:

| Run | Wall time | Real-time factor | Records/s | Errors |
|---|---|---|---|---|
| 1 | 6.57 s | 137.1x | 286,263 | 0 |
| 2 | 6.49 s | 138.7x | 289,643 | 0 |
| 3 | 6.48 s | 138.9x | 290,026 | 0 |

Median: 6.49 s of wall time for 900.0 s of data, 138.7x real time. Records in: 1,482,127 CAN frames, 4,505 GPS fixes, 93,828 IMU samples, 8,999 OBD polls, 8,999 samples. Records made: 3 events, 187,445 samples, 93,827 state estimates. The Pi 5 figure is still a Phase 3 measurement.

# Phase 2 build contracts

Every builder codes against these interfaces. Changing a contract needs the orchestrator: report the change you need instead of editing a file you do not own.

Shared, owned by the orchestrator (read, do not edit): `src/photoncar/{__init__,records,units,clock,config,signals}.py`, `src/photoncar/sources/base.py`, `src/photoncar/store/schema.sql`, `src/photoncar/store/index_schema.sql`, `config/`, `pyproject.toml`, `web/package.json`, `web/tsconfig.json`, `web/eslint.config.js`, `web/vite.config.ts`, `Makefile`, `.github/`, `docs/`.

## Ground rules for every builder

- Python 3.13, `uv run ...`. mypy strict and ruff must pass on your files: `uv run ruff check <paths>`, `uv run ruff format <paths>`, `uv run mypy <paths>`.
- Run only your own tests while building (`uv run pytest tests/unit/test_<yours>.py`); other builders work in parallel in the same tree.
- No new dependencies. Installed: python-can, cantools, fastapi (with `fastapi.sse`), uvicorn, numpy, anthropic, jsonschema, pytest, hypothesis, playwright. **httpx is not installed**: do not use `fastapi.testclient`.
- Tests use synthetic data generated in code with a fixed seed. Never commit drive logs, GPS traces, or VINs. Anything under `data/` is gitignored.
- All internal values SI; see `photoncar.units`. Times are `t_ns` integers.
- UI and user-facing strings follow the copy rules: no em or en dashes; never the words leverage (verb), delve, landscape, showcase, pivotal, seamless, robust, empower, unlock, transform; sentence case; short sentences; never invent a number.
- Hard rules that code must enforce: listen-only capture, allowlisted TX only (service 01, single PID), never clear codes, no service 09, every alert has a trigger citation, exports trim the privacy zone and refuse without `config/privacy.local.toml`, the safety line "Not a safety system. Do not use while driving." (`photoncar.SAFETY_LINE`) on every screen.

## Module ownership and interfaces

### Device (builder: device)

Owns `src/photoncar/{safety,decode}/`, `src/photoncar/sources/{socketcan,obd}.py`, `scripts/bench/`, `scripts/provision/`, their tests.

- `decode/obd_pids.py`
  - `PID_DEFS: dict[int, PidDef]` for PIDs 0x00, 0x20, 0x40 (support bitmaps) and every PID in `config [obd]`.
  - `decode_pid(pid: int, data: bytes) -> float | None`: SI value (K, m/s, ratio, kg/s, V; 0x03 returns the raw enum of system 1).
  - `encode_pid(pid: int, value: float) -> bytes`: inverse, for the ECU emulator and tests.
  - `request_frame(pid: int, arb_id: int = 0x7DF) -> can.Message`: `[0x02, 0x01, pid, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC]`, DLC 8, 11-bit.
  - `parse_response(msg: can.Message) -> tuple[int, bytes] | None`: single-frame service 0x41 replies from 0x7E8..0x7EF; returns `(pid, data)`. Negative responses (0x7F) return None and are logged as `nrc`.
  - `supported_pids(bitmaps: dict[int, bytes]) -> set[int]`.
- `safety/tx_guard.py`
  - `validate_frame(msg: can.Message, allowed_pids: frozenset[int]) -> None`, raising `TxViolation`.
  - `class TokenBucket(rate_hz: float, burst: int = 1, clock: Clock)`: `try_take() -> bool`.
  - `class TxGuard(allowed_pids, rate_hz, clock)`: `check_and_consume(msg) -> None` (validates, rate limits, and latches `disabled` on any violation); `audit(frame: CanFrame) -> None` checks every observed frame in 0x7DF / 0x7E0..0x7E7 with the same validator and disables TX on a mismatch; `disable(reason: str)`; `disabled: bool`; `reason: str | None`.
  - Forbidden forever: ID 0x6F1, service 0x09, 0x04 (clear DTCs), UDS (0x10, 0x27, 0x2E, 0x31, 0x3E), ISO-TP flow control (`data[0] & 0xF0 == 0x30`), RTR, FD, 29-bit IDs.
- `safety/listen_only.py`: `check_listen_only(ifname: str) -> ListenOnlyStatus`, reading `ip -details -json link show <if>` (Linux). Returns `{ok, reason}`; `ok` is True only when `ctrlmode` includes `LISTEN-ONLY`. On non-Linux, returns `ok=False, reason="not linux"`. Never changes settings.
- `sources/socketcan.py`: `class SocketCanSource(ifname, clock, *, bus=None)`: implements `Source`, yields `CanFrame` from a listen-only bus. Refuses to start unless `check_listen_only` is ok (a `bus` injected for tests skips the check only when `allow_unchecked_for_tests=True`).
- `sources/obd.py`: the **only** module that may call `.send(` or open the request bus. `class ObdPoller(bus, guard: TxGuard, cfg, clock)`: discovers support (0x00/0x20/0x40, functional 0x7DF), then polls allowed PIDs physically (0x7E0) on the fast / slow schedule from `config [obd]`, 1 in flight, timeout from config; yields `ObdPoll` and `Sample` records (signal names from `signals.OBD_PID_TO_SIGNAL`). Never sends when `guard.disabled`.
- `decode/dbc.py`: `load_dbc(name: str) -> cantools.database.Database` from `decode/vendor/opendbc/`; `class DbcDecoder(db, mapping: dict[str, tuple[str, str]])` turning `CanFrame` into `Sample`s for mapped `(message, signal) -> catalog name`. Vendored: `bmw_e9x_e8x.dbc` and the generated Toyota DBC needed for comma2k19 replay (`toyota_new_mc_pt_generated.dbc`, built by concatenation as opendbc's generator does), each with the opendbc LICENSE and a `SOURCE` file naming the commit.
- `scripts/bench/ecu_emulator.py`: python-can ECU emulator (answers 0x7DF/0x7E0 requests on 0x7E8 with `encode_pid` values). It refuses the configured car interfaces (`can.capture_interface`, `can.request_interface`) and needs `--bench` for any socketcan channel (`--config` names the config file). `scripts/bench/check_log.py`: reads a candump-style log and applies the bench pass rule.
- `scripts/provision/`: `80-can0.network`, `80-can1.network` (systemd-networkd), `README.md`. Written, never installed by code.

- Live polling (as built): `open_live_can_session(cfg, clock)` is the only public way to poll a real bus. It refuses unless `can.obd_polling` is true, can0 is listen-only, can1 is up, one-shot, restart-ms 0, not listen-only, at the configured bitrate, and can1's error counters are 0. It re-checks both links immediately before the first request and then every second, and turns TX off on any change, any counter rise or fall, or a watch failure. Every can0 frame and error frame feeds the TX audit. Hard ceiling: `HARD_MAX_REQUEST_HZ = 10.0`, burst 1.

### Data (builder: data)

Owns `scripts/fetch_comma2k19_sample.py`, `src/photoncar/sources/{comma2k19,synthetic,recorded}.py`, `docs/dataset.md`, their tests.

- `make fetch-dataset` runs the fetch script: download the pinned comma2k19 example segment files needed for replay (processed_log arrays and global_pose) into `data/public/comma2k19/...`, verify SHA-256, skip video and previews.
- `sources/comma2k19.py`: `class Comma2k19Source(segment_dir: Path)`: `Source` yielding `CanFrame`, `GpsFix`, `ImuSample` in time order. Axes rotated to x forward, y left, z up. Meta kind "replay", dataset_ref = segment path relative to the dataset root.
- `sources/synthetic.py`:
  - `class SyntheticObdSource(base: Source-derived speed, seed)`: OBD `ObdPoll` and `Sample` values consistent with a drive's speed and accel (for replay of comma2k19, which has no OBD). Every signal it produces is listed in `meta.synthetic_signals`.
  - `synthetic_drive(seed: int, scenario: str) -> SyntheticDriveSource` with scenarios `"track_day"` (standing start to over 60 mph, a 60 to 0 stop, cornering, 3 laps of a closed loop with a start and finish line, a coolant excursion over 105 °C for more than 10 s) and `"commute"`. GPS near a fake origin (lat 0.0, lon 0.0 region), IMU at 104 Hz, GPS at 10 Hz, OBD on the config schedule.
- `sources/recorded.py`: `class RecordedDriveSource(path)`: replays a Photoncar drive file (for re-analysis).
- `docs/dataset.md`: what was downloaded, bytes, SHA-256, license text, whether a standing start to 60 mph exists anywhere reachable, and how a full multi-segment route could run.

### Analytics (builder: analytics)

Owns `src/photoncar/{estimate,detect}/` and their tests. Pure computation; no I/O.

- `estimate/alignment.py`: `estimate_mount_rotation(imu: ImuArrays, speed_t_ns, speed_mps, cfg) -> NDArray (3,3)` (sensor to vehicle), `apply(R, xyz)`.
- `estimate/kalman.py`: `class StateEstimator(cfg: Config, mount_rot: NDArray | None)`; `push(rec: Record) -> list[StateEstimate]` (emits on each IMU sample once initialized; consumes GpsFix speed, Sample "obd.speed" or "can.wheel_speed"); zero-velocity updates at rest; `dr=True` while GPS is missing.
- `detect/performance.py` (arrays in, `Event`s out; kinds `perf.zero_to_sixty`, `perf.sixty_to_zero`, `perf.peak_lateral`; each carries citations with `role="trigger"` on `state.speed` or `state.accel_lat`):
  - `zero_to_sixty(t_ns, speed_mps, cfg) -> list[Event]`: `value` seconds from standstill; the rollout time is a second citation stat `"rollout_1ft_s"`.
  - `sixty_to_zero(t_ns, speed_mps, cfg) -> list[Event]`: `value` meters.
  - `peak_lateral(t_ns, accel_lat_mps2, cfg) -> Event | None`: `value` in g.
- `detect/laps.py`: `StartFinishLine(line_id, x1, y1, x2, y2)`, `class LapTimer(line, min_lap_s, min_dist_m)`: `push(t_ns, x_m, y_m) -> Lap | None`.
- `detect/behavior.py`: `analyze(t_ns, speed_mps, accel_long, accel_lat, rpm | None, cfg, vehicle) -> BehaviorReport` with `events: list[Event]` (kinds `behavior.hard_brake`, `behavior.hard_accel`, `behavior.corner`, `behavior.shift`), jerk stats (mean and p95 |j| long and lat), `rpm_band_s: list[tuple[lo, hi, seconds]]`, `shifts`.
- `detect/rules.py`: `class RuleEngine(cfg)`: `push(sample: Sample) -> list[Event]`, `finish(t_ns) -> list[Event]` (closes open episodes as resolved or ongoing). Rules: coolant_high, trim_warn, trim_critical, voltage_low_running, voltage_low_rested (advisory), plus stale detection. Every alert event has trigger citations with window and stat (for example `stat="min"`, `stat_value=` the min over the window). Messages are plain: "Coolant 106 °C, over 105 °C."
- `detect/baseline.py`: `window_medians(t_ns, values, window_s) -> arrays`; `class BaselineStats` (signal, bin, median, mad, n_windows, n_drives, version); `update(prev, new_windows, drive_id) -> list[BaselineStats]`; `check(stats, t_ns, values, bins, cfg) -> list[Event]` (kind `alert.baseline`, severity 1, citations include the baseline stats).

### Store, privacy, export, pipeline merge (builder: store)

Owns `src/photoncar/{store,privacy,export}/` (except the schema files), `src/photoncar/pipeline/merge.py`, their tests.

- `store/writer.py`: `class DriveWriter(path, meta: DriveMeta, batch_ms)`: `write(rec: Record)`, `write_stats(stats: list[DriveStat])`, `write_timeline(rows)`, `set_mount_rotation(R)`, `close(clean: bool = True)`. WAL, `synchronous=FULL`, 1 writer, batched commits on record time. Indexes built at close.
- `store/reader.py`: `class DriveReader(path)` (read-only URI): `meta() -> DriveMeta`, `events() -> list[Event]` (with citations, ordered by `t_start_ns`), `laps()`, `stats() -> list[DriveStat]`, `timeline(max_points: int) -> dict[str, list[float | None]]` (min/max-preserving decimation), `gps_fixes()`, `samples(signal)`, `state()`.
- `store/rollup.py`: `build_timeline_10hz(reader_or_arrays) -> rows` with local ENU `x_m, y_m` from the first kept fix.
- `store/index.py`: `class DriveIndex(path)`: `register(meta, label)`, `update_summary(drive_id, ...)`, `list() -> list[DriveListItem]`, `get(drive_id)`, baselines get/put.
- `privacy/zones.py`: `haversine_m`, `in_zone(lat, lon, zone) -> bool`, `trim_windows(gps_fixes, zone) -> list[(t0_ns, t1_ns)]` (intervals where the vehicle is within R, extended to the neighbouring fixes so no in-zone point survives interpolation).
- `privacy/export_filter.py`: `filter_for_export(streams, windows) -> streams` dropping every record of every stream inside a window. The guarantee: 0 exported points within R of home.
- `export/schemas/lap.v0.schema.json` (JSON Schema 2020-12) and `export/laps_v0.py`: `export_laps(reader, zone, out_dir) -> list[Path]` validating each file with jsonschema. Fields per `docs/phase-1-plan.md` section 5. Writes only under `data/exports/`.
- `export/report.py`: `drive_report(reader) -> dict` (the JSON behind `/api/drives/{id}/report`).
- `pipeline/merge.py`: `merge_sources(sources) -> Iterator[Record]` via `heapq.merge` on `record_time`, stable on ties by source order.

### Live sensors (builder: sensors)

Owns `src/photoncar/sources/gps_ublox.py`, `src/photoncar/sources/imu_lsm6dsox.py`, `src/photoncar/sources/live_sensors.py`, and their tests. Standard library only (no pyserial, no smbus): the serial port through `os.open` plus `termios`, I2C through `/dev/i2c-N` with `fcntl.ioctl` (`I2C_RDWR`).

- `GpsUbloxSource(cfg, clock, *, port=None)`: configures the NEO-M9N for UBX NAV-PVT at `gps.rate_hz` on its USB serial port (`gps.device`), parses NAV-PVT into `GpsFix` (fix type, hdop from pDOP where needed and stated, satellites, speed, heading), and stamps `t_ns` on the monotonic timeline with `utc_ns` from the message. The UBX framing and checksum parser is pure and tested on byte fixtures built in the tests.
- `Lsm6dsoxSource(cfg, clock, *, bus=None)`: configures ODR `imu.rate_hz` (104 Hz), full scales `imu.accel_fs_g` and `imu.gyro_fs_dps`, reads the FIFO, and yields `ImuSample` in SI units in the sensor frame. The register protocol is tested against a fake I2C bus object (`FakeI2cBus`, tests only).
- `open_live_sensors(cfg, clock) -> list[Source]`: opens both and raises `LiveSensorsUnavailable(RuntimeError)` with a plain message when a device is missing or the OS is not Linux. The integration builder merges these with `LiveCanSession` in `photoncar record`.
- These talk only to the GPS module and the IMU chip. They never touch a vehicle bus.

### Server (builder: server)

Owns `src/photoncar/{summary,api}/`, `tests/asgi.py` (a stdlib ASGI test client), their tests. Load the `claude-api` skill before writing the model client.

- `summary/stats.py`: `summary_input(stats: list[DriveStat]) -> list[dict]` (`{id, label, value, unit, display}`) with no GPS, no dates, no IDs.
- `summary/model_client.py`: `class SummaryClient(Protocol)`: `complete(system, stats_json, max_tokens) -> ModelResult(text, in_tokens, out_tokens)`. `AnthropicSummaryClient` (reads `ANTHROPIC_API_KEY`, model from config, structured JSON output) and `FakeSummaryClient` (tests and dev only; never selected without an explicit flag).
- `summary/validate.py`: `validate(output, stats) -> list[str]` problems: every sentence has at least 1 stat id that exists, every number in the text string-matches the `display` of a cited stat, 3 to 5 sentences, no dashes, no banned words. `template_summary(stats) -> output` deterministic fallback.
- `summary/cache.py` and `summary/limits.py`: output cache keyed by SHA-256 of `{model, prompt_version, system, stats, max_tokens}` in `index.sqlite` `model_summaries`; per-IP sliding window (config) and daily spend cap with reservation under a lock (`spend_ledger`).
- `api/app.py`: `create_app(cfg, data_dir, *, summary_client=None, clock=None) -> FastAPI`. CLI: `photoncar serve [--host] [--port] [--live] [--data-dir]` (`--live` keeps the dashboard locked for a recording in another process); `replay` and `seed-demo` take `--public-dir` (default `PHOTONCAR_PUBLIC_DIR` or `data/public`), kept apart from `--data-dir`. Serves `web/dist` at `/` with an SPA fallback to `index.html` for non-`/api` paths.

#### HTTP API (the web builder consumes exactly this)

Auth: when `PHOTONCAR_API_TOKEN` is set, every `/api/*` call needs `Authorization: Bearer <token>`; otherwise only loopback clients are allowed. Errors are `{"error": "<plain sentence>"}` with a matching status.

Moving lock: `GET /api/health` reports `moving`. While `moving` is true, every route except `/api/health` and the live stream of the current drive (`/api/live/stream` with no `replay`) returns 423 `{"error": "Parked views open when the car stops."}`. A replay stream of a stored drive is a parked view. `photoncar serve` without a recording starts locked only in live mode; in replay or idle mode nothing moves.

| Method and path | Response |
|---|---|
| `GET /api/health` | `{ok, version, mode: "idle"\|"live"\|"replay", moving}` |
| `GET /api/drives` | `{drives: [DriveListItem]}` |
| `GET /api/drives/{id}` | `{drive: DriveDetail, stats: [DriveStat]}` |
| `GET /api/drives/{id}/timeline?max_points=1500` | `{t_s: number[], speed_mps, rpm, throttle, coolant_c, accel_long_g, accel_lat_g, x_m, y_m: (number\|null)[], dr: boolean[], sources: {<column>: string\|null}}` where `sources` names the catalog signal that fed each column (null when none); a column is synthetic when its source is in `DriveDetail.synthetic_signals` |
| `GET /api/drives/{id}/events` | `{events: [EventOut]}` ordered by time; `box_no` numbers roadbook events only (1, 2, 3... over `alert.*`, `perf.*`, `lap*`, `behavior.hard_brake`, `behavior.hard_accel`, `behavior.corner`); `behavior.shift` and other kinds carry `box_no: null` and stay listed for the report |
| `GET /api/drives/{id}/report` | `{behavior: BehaviorOut, performance: PerformanceOut, laps: [LapOut]}` |
| `GET /api/drives/{id}/live_frame?t_s=12.3` | `LiveFrame` computed from the stored drive at that time (deterministic capture) |
| `GET /api/live/stream?replay={id}&speed=1` | SSE: `event: frame` data `LiveFrame` at about 10 Hz; `event: state` data `{moving}` |
| `POST /api/drives/{id}/summary` | `{sentences: [{text, stat_ids}], source: "model"\|"template", cached, model}`; 429 over limits; 503 `{"error": "Needs connection."}` offline |
| `GET /api/legend` | `{rules: [{rule_id, title, condition, severity, advisory}], privacy_radius_km, max_request_hz, synthetic_note}` built from config, so the legend page never hard-codes a threshold |
| `POST /api/drives/{id}/export?kind=laps` | `{files: [string]}` under `data/exports/`; 409 `{"error": ...}` without the privacy file |

Types (JSON):

- `DriveListItem`: `{drive_id, label, source: "live"|"replay"|"synthetic", duration_s, distance_m, n_events, n_alerts, closed_cleanly}`
- `DriveDetail`: DriveListItem plus `{vehicle_profile, dataset_ref, synthetic_signals: string[]}`
- `DriveStat`: `{stat_id, label, value, unit, display}`. Distances display in miles (`drive.distance_mi`, "11.27 mi") and durations as "15 min 0 s" (`drive.duration_s`), matching the web formatters (`photoncar.units.miles_text`, `duration_text`).
- `EventOut`: `{event_id, box_no: number|null, kind, rule_id, severity: 0|1|2|3, t_start_s, t_end_s, distance_m, value, threshold, unit, message, resolved, synthetic, citations: [{signal, source, role, t_start_s, t_end_s, stat, stat_value, unit, n}]}`
- `BehaviorOut`: `{hard_brakes, hard_accels, corners, jerk_long_mean, jerk_long_p95, jerk_lat_mean, jerk_lat_p95, rpm_bands: [{lo, hi, seconds}], shifts: [{t_s, rpm, from_gear, to_gear}]}`
- `PerformanceOut`: `{zero_to_sixty: [{t_start_s, seconds, rollout_seconds}], sixty_to_zero: [{t_start_s, meters}], peak_lateral_g: number|null}`
- `LapOut`: `{lap_no, line_id, t_start_s, duration_s, valid}`
- `LiveFrame`: `{t_s, speed_mph, rpm, coolant_c, synthetic_fields: string[], alert: null | {severity, headline, citation, synthetic}}` where `headline` is like "Coolant 106 °C" (a synthetic alert's headline ends ", synthetic fault"), `citation` like "OBD 0x05 over 105 °C for 12 s", and `synthetic_fields` lists the frame fields fed by synthetic signals (for example `["coolant_c"]` on a public-dataset replay).

Times in the API are seconds relative to drive start. Speeds in `LiveFrame` are mph; everything else SI plus g for acceleration.

### Web (builder: web)

Owns `web/src/`, `web/index.html`, `web/public/` (fonts are already there), `tests/e2e/`. Uses impeccable: load `reference/craft-floor.md` before editing UI; the direction contract comment is the first child of `<body>` in `web/index.html`; run the detector after.

Routes: `/` stage list (drives), `/drives/:id` review (roadbook, map trace, channel strips, box selection via `?box=N`), `/drives/:id/report` (driver behavior and performance), `/live` (live tripmeter; `?replay=<id>` replays; `&t=<s>` freezes at a time for capture), `/legend` (symbols and thresholds). Every route: loading, empty, and error states; the safety line in a shared frame on every route and state.

### Portfolio embed (owner decision 2026-09-23; builder: web)

A static, read-only build of the review experience for 1 public-dataset drive, hosted as plain files inside the owner's portfolio (no server, no API key, no model calls, no personal data). Portfolio figure type `interactive`.

- **Data-source seam.** Every screen loads data through one interface, `web/src/shared/data.ts`: `DataSource` with `drives()`, `drive(id)`, `timeline(id, maxPoints)`, `events(id)`, `report(id)`, `liveFrame(id, t_s)`, `liveFrames(id)` (async, returning the HTTP API types above). `ApiSource` calls `/api/*`. `StaticSource` reads precomputed JSON relative to the page. No screen calls `fetch` directly.
- **Build mode.** `pnpm build:embed` (`vite build --mode embed`) writes `web/dist-embed/` with relative asset paths (`base: "./"`), hash routing (`#/drives/<id>?box=4`, `#/drives/<id>/report`, `#/live`), and `StaticSource`. Hidden in embed mode: the summary action, the export action, and the drive list when it holds 1 drive (open straight to the review). The safety line, "Synthetic" labels, and a visible "Replay of public data (comma2k19, MIT)" credit stay.
- **Live view in the embed.** Plays the precomputed frames of 1 window of the drive (at most 120 s at 10 Hz) in a loop; `prefers-reduced-motion` shows a single frozen frame. The tripmeter itself never asks for input.
- **Export.** `photoncar export-embed --drive <id> --out <dir>`. The web builder writes `src/photoncar/export/embed.py` with `export_embed(data_dir: Path, drive_id: str, out_dir: Path, *, cfg: Config, web_dist_embed: Path, privacy_path: Path | None = None) -> EmbedReport` raising `EmbedRefused(ValueError)` with a plain message; the integration builder adds the CLI subcommand that calls it writes `<dir>/data/*.json` with exactly the HTTP API response bodies for that drive, then copies `web/dist-embed/` into `<dir>/`. It ships only synthetic drives and replays of the public dataset, and refuses everything else (owner drives never ship, including a live drive re-analyzed through `RecordedDriveSource`, which keeps its `live` origin), a missing privacy file, an unknown drive, a missing embed build, a non-empty output folder, and any output over budget. The privacy trim runs before decimation, so no in-zone value of any column can land on a bucket edge. Timeline positions are local meters only; no latitude, longitude, dates, drive IDs beyond the random one, or raw CAN.
- **Budget [proposed, owner to confirm].** JSON at most 1.5 MB uncompressed; JS plus CSS at most 250 KB uncompressed; fonts as shipped (about 135 KB).
- **Target path for Phase 4.** `portfolio/public/work/photoncar/replay/` with an `index.html` the portfolio can load in an `<iframe>`.
- **Tests.** `StaticSource` and `ApiSource` return identical objects for the same seeded drive; the embed build runs with 0 network requests outside its own folder (Playwright request log); the safety line is visible on every embed route at 1440 and 390; export-embed refuses each forbidden case.

# Photoncar: Phase 1 plan

Status: waiting for owner approval. Written 2026-09-23. No code exists yet.

Tags: **[F]** sourced fact, **[I]** inference, **[A]** assumption (lives in config), **[U]** unknown, **[P]** proposed default that needs owner approval. "[needs a number]" means no source or measurement exists yet.

How this plan was made: 6 parallel researchers, 1 synthesizer, 1 completeness critic (23 gaps found, all addressed below), 1 adversarial fact-checker (64 claims: 54 confirmed, 6 refuted, 4 unverifiable; corrections applied), plus a 2-agent E90 M3 research and refute pass. The visual system came from the impeccable design skill (direction seed 14e368b3).

## 0. Owner decisions

### Already decided (2026-09-23)

| Item | Decision |
|---|---|
| Car | BMW M3 (E90) as the first vehicle profile. The core stays car-agnostic. |
| Hardware | Option A: Raspberry Pi 5 with a CAN HAT, USB GPS, and an IMU. |
| In-car screen | None. The device records. The live view exists for replay and screenshots, and still meets the 1 m glance rule. |
| Lap schema | Photoncar defines lap v0. It mirrors the per-lap arrays the parallel AI Race Engineer session is drafting (`t_ms, dist_m, speed_kph, throttle_pct, brake, gear, rpm, x, y`). |
| GitHub user | n8v1ct0r (gh is signed in). |
| Portfolio embed | Added 2026-09-23: a static, read-only interactive replay of 1 public-dataset drive for the portfolio (figure type interactive). Spec in docs/contracts.md. |
| License | MIT (brief default). |

### Still open (asked at the checkpoint)

1. Visual direction (section 8).
2. E90 wiring: OBD port only for the MVP, or a listen-only PT-CAN tap later (section 1, H5).
3. Privacy radius R.
4. Approval of the plan, the dependency list (section 9), and the proposed unsourced values (section 13).

## 1. Rule conflicts and hazards

| # | Issue | Evidence | Resolution |
|---|---|---|---|
| H1 | Rule 1 (listen-only) and rule 2 (send OBD requests) cannot both hold on 1 interface | In listen-only mode a controller sends nothing, "including error flags or Acknowledge signals" [F, MCP2515 datasheet]. The kernel drops TX in listen-only mode via `can_dev_dropped_skb()`, which shipped in Linux 6.1, and both mcp251xfd and mcp251x call it [F] | Two channels. `can0` is listen-only and captures. `can1` runs in normal mode and sends allowlisted requests only. `can1` stays down unless `obd_polling = true` (off by default). Needs owner approval (D1) |
| H2 | A normal-mode node ACKs, can send error flags, and retransmits a lost frame automatically | CAN controller behavior [F] | `can1` runs with `one-shot on` (no automatic retransmit) and `restart-ms 0` (bus-off stays off). Any error counter increase disables TX for the session. The rate check counts on-wire frames seen on `can0`, not `send()` calls. These are CAN settings, so the owner approves them before Phase 3 |
| H3 | Rule 3 forbids ISO-TP Flow Control frames | A multi-frame reply starts with a First Frame that expects Flow Control [F] | Single-PID service 01 requests only. Every target reply fits in 1 frame [I]. No kernel ISO-TP on `can1` |
| H4 | Service 09 and the VIN | The VIN is 17 bytes and needs Flow Control [F]. Publish rule 12 bans VINs | Drop service 09 entirely (D2). Stricter than rule 2 |
| H5 | The E90 OBD port hides powertrain CAN | OBD pins 6/14 carry D-CAN at 500 kbit/s. The JBE forwards only selected frames to D-CAN and K-CAN [F, BMW E90 training doc]. A sniffer on an E90 OBD port saw almost nothing [F, forum] | MVP: both HAT channels on the OBD port. `can0` listen-only captures D-CAN (our requests, ECU replies, and anything the JBE forwards), and doubles as the in-car TX audit. DBC decoding is off at the OBD port. Option for later (owner decision): move `can0` to a listen-only PT-CAN tap at JBE connector X14271 pins 1/2 (see section 2.3) |
| H6 | Brief item 5 fuses wheel speed | OBD speed has 1 km/h resolution [F]. E90 wheel speeds live on PT-CAN 0xCE [F, opendbc] | The filter accepts OBD speed now and CAN wheel speed when a tap exists |
| H7 | "Battery voltage" | PID 0x42 is control module voltage under ECU load, not the battery terminal [F] | Label it "Module voltage (OBD 0x42)". The rested-battery rule becomes advisory |
| H8 | comma2k19 has no OBD PIDs [F] | Dataset check | OBD values in replay come from a synthetic OBD source, labeled "Synthetic" in the UI, the report, and any figure that shows them |
| H9 | Rule 8 vs rule 5 and build item 9 (1 glanceable alert line, no input) | Brief | The live alert has 2 parts, both at glance size: a headline ("!! Coolant 106 °C") and a citation line ("OBD 0x05 over 105 °C for 12 s"). The full citation appears in post-drive review |
| H10 | Rule 4 (safety line on every screen) vs 1 m legibility on a 390 px phone | Measured with the real font files and word wrap: at 20 arcmin every live element fits in 801 px of the 844 px height (section 8.4) | One shared frame component renders the line on every route and every state. A Playwright test asserts it is visible in the viewport at both sizes |
| H11 | Rule 7 guarantee | A fuzzed home center can under-trim (critic) | Store the true center only in a gitignored local file. Trim radius R around it. The guarantee: 0 points within R of home, in every stream. A property test checks it against a known synthetic home |
| H12 | Publish rules 5 and 12 vs the hero map trace | comma2k19 is real commute driving by real people [F] | Figures draw the dataset trace in local meters, with no basemap, no coordinates, and trimmed ends. The README credits the dataset (MIT) |
| H13 | Shot 1.2 (anomaly alert) vs screenshots rule 9 (public dataset only) | The rules need OBD data that comma2k19 lacks | Default: a baseline alert on a real dataset CAN signal, armed across 3 or more dataset segments. If none fires, a synthetic fault labeled "Synthetic fault" inside the shot |
| H14 | Screenshots rule 2 (clean state) vs rule 9 (no real drives in shots) | Critic | Capture runs against `PHOTONCAR_DATA_DIR=build/capture-data`, seeded only from `data/public/`. It asserts 0 live-source drives before any shot |
| H15 | Rule 6 vs build item 11 (model call leaves the device) | Brief | The summary runs only when the owner presses a button while online. It sends derived stats only, with no GPS, no dates, and no IDs |
| H16 | Rule 5 "while the car moves" | Brief | The server keeps a `moving` flag. While it is set, every route redirects to the live view, which has no inputs. With no speed source, the device stays locked to the live view. Replay is not gated |
| H17 | Network exposure | Critic | The dashboard binds only to the Pi's own Wi-Fi access point (WPA2 or WPA3) and requires a local token. No port forwarding. The device goes online for the summary through a phone hotspot or home Wi-Fi, parked |
| H18 | Publish rule 4 (stealth scan) | A committed term list would leak the terms | `make stealth-scan` reads the terms from a file outside the repo, named by `PHOTONCAR_STEALTH_TERMS_FILE`. The terms never enter the repo |
| H19 | Hardware photo (shot 1.5) | Phone photos carry EXIF GPS | Strip EXIF, check the frame for plates and the VIN plate, crop, and name it per the kit convention. Phase 4 blocks until the photo arrives |

### TX allowlist enforcement

1. **Single TX owner.** Only `sources/obd.py` opens `can1` and calls `send`. An AST test fails CI if `.send(` appears anywhere else, or if `can.Bus(` appears outside `sources/obd.py` and `sources/socketcan.py`.
2. **Validator before every send.** A frame passes only if all hold: 11-bit ID 0x7DF (support bitmaps only) or 0x7E0 to 0x7E7; not RTR; not FD; DLC 8; `data[0]=0x02`; `data[1]=0x01`; `data[2]` in the PID allowlist; fixed padding in bytes 3 to 7. Never ID 0x6F1 (BMW tester ID) [F]. Anything else raises and turns TX off for the session.
3. **Rate limit.** A token bucket at 10 Hz or less, with 1 request in flight, in the same module.
4. **Runtime audit.** `can0` sees every frame `can1` puts on the wire. A watchdog checks each frame in the request ID range against the same validator and stops TX on a mismatch.
5. **No TX path in replay.** Replay has no code path that can send.
6. **Provisioning is separate and approved.** `scripts/provision/` holds systemd-networkd files for `can0` (listen-only, 500 kbit/s) and `can1` (one-shot, restart-ms 0). Photoncar never changes CAN settings at runtime. It checks them at startup and refuses to run if `can0` is not listen-only. The owner approves these files before they are installed.

## 2. Parts list (Option A, confirmed)

Prices looked up 2026-09-23, approximate. Sources are in the research notes.

### 2.1 Core

| Role | Part | Price | Notes |
|---|---|---|---|
| Computer | Raspberry Pi 5, 4GB | $110 [F] | 2GB is $65 [F] and may be enough with no screen. 4GB gives headroom for replay and the build [P] |
| CAN, 2 channels | Waveshare 2-CH CAN FD HAT, SKU 17075 | $46.99 [F] | MCP2518FD, 1 SPI bus per channel, isolated, 120 Ω jumper [F]. Pi 5 receive-delay bug (issue #6407) fixed by PR #6466, merged 2024-11-14 [F]. Waveshare does not list the Pi 5 [F] |
| CAN, alternative | Copperhill PiCAN FD Duo Isolated | $179.95 [F] | Vendor lists the Pi 5 [F]. Avoid the SMPS variants [F] |
| OBD cable | SparkFun CAB-10087 (OBD-II to DB9) | $14.95 [F] | OBD 6 = CAN-H, 14 = CAN-L, 4 = ground, 16 = battery [F]. Not the CiA 303-1 DB9 pinout, so cut the DB9 end and use a terminal block. Leave pin 16 unused [I] |
| GPS | SparkFun NEO-M9N SMA, GPS-17285 | $76.50 [F] | Up to 25 Hz nav rate [F]. Run at 10 Hz |
| GPS antenna | SparkFun GPS-14986, magnetic, 3 m | $16.50 [F] | Active L1 |
| IMU | Adafruit LSM6DSOX, product 4438 | $11.95 [F] | Raw accel and gyro. Output rates step 52, 104, 208 Hz [F], so run at 104 Hz. Rejected BNO085: its fusion hides raw data [I] |
| Storage | SanDisk MAX Endurance 64 GB microSD | $43.99 to $58.25 [F] | NVMe kit deferred: rated 0 to 50 °C ambient [F] |
| RTC battery | Official ML-2020 | $4.95 [F] | Keeps time offline. Rechargeable type only [F] |
| Cooling | Raspberry Pi Active Cooler | $5 list [F] | Retail $4.99 to $15 [F] |
| Power, option P1 | CarPiHAT PRO 5 | £110 incl. VAT [F] | 12 V to 5 V at up to 5 A, fuse, ignition sense, RTC [F]. Stacking with the CAN HAT is [U] |
| Power, option P2 | Pololu D36V50F5 | $39.95 [F] | 5.5 A out, reverse protection [F]. Needs an ignition-sense circuit and a delayed-off relay (parts [U]) |

Totals: the core list (Pi 4GB, HAT, cable, GPS, antenna, IMU, microSD at $43.99, RTC battery, Active Cooler at $5) is $330.83. With power P2: $370.78. With P1: $330.83 plus £110. Not included: enclosure, fuse tap, wiring [U].

Power and heat: at 5 V 3 A the Pi 5 caps USB at 600 mA [F], and the GPS is the only USB load [I]. A halted Pi draws about 1.2 W, or about 0.01 W with `POWER_OFF_ON_HALT=1` [F]. Parked cabins reached 41 to 76 °C against a 0 to 70 °C Pi rating [F], so mount it low, out of the sun, with no Li-ion UPS [I].

### 2.2 Bench rig (acceptance: "a second logger records 0 frames other than allowed requests")

| Part | Price |
|---|---|
| 2x CANable 2.0 | $35 each [F] |
| Official 27W USB-C PSU | $12.95 [F] |
| 2x 120 Ω terminators, wire, terminal blocks | [U] |

- CANable #1 is the ECU emulator: a python-can script on the Mac in normal mode, answering on 0x7E8.
- CANable #2 is an independent logger in silent mode.
- Exactly 2 terminators on the bench.
- **Pass rule:** every logged frame is an emulator reply on 0x7E8 to 0x7EF, or a Photoncar frame that passes the validator. 0 Flow Control frames, 0 error frames, 0 retransmits. Also force a send on `can0` and confirm `tx_dropped` increments while the logger records 0 frames.

### 2.3 E90 M3 notes (research and refute, 2026-09-23)

- OBD protocol: ISO 15765-4 CAN at 500 kbit/s on pins 6/14 [F]. 11-bit IDs [I], detected at runtime.
- Supported PIDs on the MSS60: [U]. No public bitmap. Read 01 00/20/40 on first connect and store it in the vehicle profile.
- The S65 has 2 banks (bank 1 = cylinders 1 to 4, bank 2 = 5 to 8) [F], so trims for both banks. No MAF sensor: air flow is modelled [F]. PID 0x10 may be missing.
- Redline: BMW M quotes 8,300 rpm; other sources say an 8,400 limiter [F, conflict]. RPM bands come from config.
- Optional PT-CAN tap (later, owner approval): JBE connector X14271 pins 1/2, confirmed by one E90 M3 owner [F, forum]. Wire colors CAN-H blue/red or black, CAN-L red or yellow [F]; check the car's wiring diagram. Non-destructive MQS pin adapters exist [F]. Before connecting, measure CAN-H to CAN-L with the battery disconnected: about 60 Ω means terminated, so leave the HAT terminator off. The M3 terminator location is not proven [F].
- DBC: opendbc `opendbc/dbc/bmw_e9x_e8x.dbc`, MIT, 326 message IDs, 38 with signals, 165 signals, loads in cantools 44.1.0 strict and non-strict [F, run locally]. Verified on an M3 by an independent MIT project: 0xAA rpm (raw/4), 0x1D0 coolant and oil (offset -48), 0x1A0 speed, 0xC4/0xC8 steering [F]. 0x1A0 speed scale conflicts (0.1 vs 0.103 km/h per bit), so calibrate against GPS. 0x19E brake pressure is K-CAN only [F]. Do not clamp rpm to the DBC's 8000 maximum.

## 3. Signal list with sample rates

Internal units are SI. g = 9.80665 m/s² [F]. Conversion to g, mph, and °F happens only at the UI and export edge.

| Signal | Source / ID | Unit stored | Nominal rate | Notes |
|---|---|---|---|---|
| `can.raw` | `can0`, all IDs | frame | E90 OBD port: diagnostic traffic only [I]. comma2k19: bus 0 about 897 frames/s, bus 1 about 750 frames/s [F, measured] | Stored, never exported |
| `obd.rpm` | 01 0x0C | rpm | 2 Hz | (256A+B)/4 |
| `obd.speed` | 01 0x0D | m/s | 2 Hz | A km/h |
| `obd.throttle` | 01 0x11 | ratio | 2 Hz | 100A/255 % |
| `obd.fuel_system` | 01 0x03 | enum | slow group | Trims are judged only in closed loop (value 2) [F] |
| `obd.load` | 01 0x04 | ratio | slow group | 100A/255 % |
| `obd.coolant_temp` | 01 0x05 | K | slow group | A - 40 °C |
| `obd.iat` | 01 0x0F | K | slow group | A - 40 °C |
| `obd.maf` | 01 0x10 | kg/s | slow group if supported | (256A+B)/100 g/s. S65 has no MAF sensor [F] |
| `obd.stft_b1`, `obd.ltft_b1` | 01 0x06, 0x07 | ratio | slow group | 100A/128 - 100 % |
| `obd.stft_b2`, `obd.ltft_b2` | 01 0x08, 0x09 | ratio | slow group | Dual bank (S65) |
| `obd.module_voltage` | 01 0x42 | V | slow group if supported | (256A+B)/1000 |
| Supported PIDs | 01 0x00, 0x20, 0x40 | bitmap | once per session | 0x7DF, then 0x7E0 |
| `gps.fix` | NEO-M9N | deg, m, m/s, rad | 10 Hz | comma2k19 measured 9.68 Hz |
| `imu.accel`, `imu.gyro` | LSM6DSOX | m/s², rad/s | 104 Hz | comma2k19 measured 104.26 Hz [F] |
| `can.<msg>.<sig>` | DBC via cantools | SI | per message | PT-CAN tap or dataset only |
| `clock_sync` | host | ns | 1 Hz | REALTIME vs MONOTONIC offset |

Poll schedule (cap 10 Hz): RPM, speed, and throttle at 2 Hz each (6 Hz). The slow group shares 4 Hz round-robin over the supported PIDs, so each gets 4/N Hz (0.4 Hz at N = 10).

Derived at the IMU rate: `state.speed`, `state.accel_long`, `state.yaw_rate`, `state.accel_lat` (v·r), `state.accel_lat_imu` (cross-check), `behavior.jerk_long`, `behavior.jerk_lat`. Per sample: `behavior.gear`, `behavior.rpm_band`. `timeline_10hz` is a rollup built when the drive closes. The live stream runs at about 10 Hz [A].

## 4. Storage format: SQLite, 1 file per drive

- DuckDB allows 1 read-write process, so the dashboard could not read during a drive [F][I]. An unclosed Parquet file has no footer [F]. SQLite is in the stdlib, and in WAL mode "readers do not block writers and a writer does not block readers" [F].
- Settings: `journal_mode=WAL`, `synchronous=FULL`, 1 writer thread committing a batch every 1,000 ms [P], so a power cut loses at most about 1 s [I]. `journal_size_limit` caps the WAL. Tables are append-only in time order, indexes are built at close, and a clean close runs `wal_checkpoint(TRUNCATE)` and sets `closed_cleanly = 1`.
- The web process opens drives read-only. The live view reads the pipeline's in-memory stream, not SQLite.
- Layout: `data/drives/<uuid4>.sqlite` per drive; `data/index.sqlite` for the drive list, baselines, model cache, and spend ledger. Exports go only to `data/exports/`.
- STRICT tables need SQLite 3.37.0 [F]. The Mac's Python 3.13.5 has 3.49.1 [F].
- Size: at about 40 bytes per raw frame [A], about 130 MB/h at comma2k19 bus rates [I]. Retention policy: [needs a number].

## 5. Timeline schema

Time base: `t_ns` is CLOCK_MONOTONIC nanoseconds tied to `boot_id`. SocketCAN timestamps map through an offset sampled at 1 Hz. GPS fits offset plus drift on the lower envelope of `utc - rx_mono`. The IMU uses its FIFO anchor plus 1/ODR. An OBD value takes its response time, with the request-to-response gap as its uncertainty. comma2k19 maps `t_ns = round(t * 1e9)` from boot seconds [F]. Streams merge with `heapq.merge` on `t_ns`. Every window, debounce, and hold uses record time, never the wall clock, which makes replay deterministic.

```sql
-- data/drives/<uuid4>.sqlite (STRICT)
drive(drive_id TEXT PRIMARY KEY, source TEXT CHECK(source IN('live','replay','synthetic')),
  dataset_ref TEXT, boot_id TEXT, t_start_ns INT, t_end_ns INT, utc_offset_ns INT, utc_fit_rms_ns INT,
  vehicle_profile TEXT, dbc_name TEXT, dbc_sha256 TEXT, imu_mount_rot TEXT, schema_version INT,
  closed_cleanly INT DEFAULT 0);
signals(signal_id INTEGER PRIMARY KEY, name TEXT UNIQUE, unit TEXT,
  source TEXT CHECK(source IN('can','obd','gps','imu','derived')), nominal_hz REAL, origin TEXT);
clock_sync(t_ns INT, other_ns INT, source TEXT, offset_ns INT, gap_ns INT, step INT);
raw_can(t_ns INT, bus INT, arb_id INT, is_ext INT, dlc INT, data BLOB, is_tx INT DEFAULT 0);
samples(t_ns INT, signal_id INT, value REAL);
gps_fixes(t_ns INT, rx_t_ns INT, utc_ns INT, lat_deg REAL, lon_deg REAL, alt_m REAL,
  speed_mps REAL, course_rad REAL, hdop REAL, fix_type INT, n_sats INT);
imu_samples(t_ns INT, ax REAL, ay REAL, az REAL, gx REAL, gy REAL, gz REAL, temp_k REAL);
obd_polls(req_t_ns INT, resp_t_ns INT, service INT CHECK(service = 1), pid INT, ecu INT,
  status TEXT CHECK(status IN('ok','timeout','nrc')), raw BLOB, value REAL, unit TEXT);
state(t_ns INT, speed_mps REAL, accel_long_mps2 REAL, accel_lat_mps2 REAL, accel_lat_imu_mps2 REAL,
  yaw_rate_rps REAL, var_speed REAL, var_accel REAL, inputs TEXT, dr INT DEFAULT 0);
events(event_id INTEGER PRIMARY KEY, kind TEXT, rule_id TEXT, rule_version INT, severity INT CHECK(severity IN(1,2,3)),
  t_start_ns INT, t_end_ns INT CHECK(t_end_ns > t_start_ns), value REAL, threshold REAL, unit TEXT,
  resolved INT DEFAULT 0, message TEXT);
event_citations(event_id INT, signal_id INT, role TEXT CHECK(role IN('trigger','context')),
  t_start_ns INT, t_end_ns INT, stat TEXT, stat_value REAL, n INT, PRIMARY KEY(event_id, signal_id, role));
laps(lap_id INTEGER PRIMARY KEY, line_id TEXT, lap_no INT, t_start_ns INT, t_end_ns INT, duration_ns INT, valid INT);
timeline_10hz(t_ns INT PRIMARY KEY, speed_mps REAL, rpm REAL, throttle REAL, accel_long_mps2 REAL,
  accel_lat_mps2 REAL, x_m REAL, y_m REAL, dr INT);
-- data/index.sqlite
drives_index(drive_id TEXT PRIMARY KEY, source TEXT, t_start_ns INT, t_end_ns INT, closed_cleanly INT);
baselines(signal_id INT, bin TEXT, median REAL, mad REAL, n_windows INT, n_drives INT, version INT,
  updated_drive TEXT, PRIMARY KEY(signal_id, bin, version));
model_summaries(input_sha256 TEXT PRIMARY KEY, model TEXT, prompt_version INT, stats_json TEXT,
  output_json TEXT, in_tokens INT, out_tokens INT, cost_usd_micros INT, created_utc INT);
spend_ledger(day_utc TEXT PRIMARY KEY, usd_micros INT);
```

Severity is the caution-mark count (1 to 3), which the UI prints as !, !!, or !!!.

Citations: an alert is valid only with at least 1 `trigger` citation. This query must return 0 rows: `SELECT event_id FROM events WHERE kind LIKE 'alert.%' AND event_id NOT IN (SELECT event_id FROM event_citations WHERE role='trigger')`. The UI builds alert text from citation rows: signal, source (for example "OBD 0x05"), window, stat, threshold.

### Lap schema v0

`src/photoncar/export/schemas/lap.v0.schema.json`, JSON Schema 2020-12. Columnar arrays at 10 Hz, mirroring the AI Race Engineer draft field names:

- `schema: "photoncar.lap/v0"`, `lap_no`, `line_id`, `duration_ms`, `valid`, `source` ("replay", "live", or "synthetic").
- `samples`: `t_ms[]`, `dist_m[]`, `speed_kph[]`, `throttle_pct[]`, `brake[]` (boolean or null; null unless a brake signal exists), `gear[]` (integer or null), `rpm[]`, `x[]`, `y[]` (meters in a local frame centered on the start and finish line, so no absolute coordinates leave the device).
- No date, no drive ID, no VIN. Passes through the privacy filter.
- Acceptance is measured against lap.v0 and revalidated when AI Race Engineer publishes its own schema.

### Privacy filter (rule 7)

- `config/privacy.local.toml` (gitignored) holds the home center and `radius_km`. `config/privacy.example.toml` (committed) holds only `radius_km = "[R]"`.
- Guarantee: 0 exported points within R of home, in every stream (GPS, and IMU, speed, and heading over the trimmed windows, since those could rebuild the path).
- Optional hardening: a per-drive random extra margin, size [needs a number], off until the owner sets it.
- Distance, duration, and average speed are recomputed after trimming. Exports carry relative time only and random IDs. Export and capture refuse to run without the privacy file.

## 6. Anomaly rules

All values live in `config/photoncar.toml`. [P] marks a proposed default.

| Rule | Condition | Debounce / clear | Gates | Source |
|---|---|---|---|---|
| `coolant.high` | over 105 °C [P] | on after 10 s [P]; clear below 102 °C for 30 s [P] | none | HP Academy: safe range 85 to 105 °C [F] |
| `trim.warn` | per bank, STFT + LTFT outside ±10% [P] | on after 30 s [P]; clear [needs a number] | closed loop (0x03 = 2) and coolant 85 °C or more | ±10% "acceptable" [F, snippet only] |
| `trim.critical` | either trim outside ±25% [P] | [needs a number] | same | P0171 usually sets past +25% [F] |
| `voltage.low_running` | under 12.5 V [P] | on after 60 s; ignore the first 10 s after start [P] | engine running | Sources conflict: smart charging can sit at 12.9 V [F] |
| `voltage.low_rested` (advisory) | under 12.24 V (flooded) or 12.29 V (AGM, calcium) at key-on [F] | once per key-on | 24 h or more since the last drive [P] | 50% state of charge values [F]. PID 0x42 reads under ECU load, so this reads low [F] |

Shared: 1 alert per episode. Minimum re-alert interval [needs a number]. A signal with no update for more than 3 poll periods [P] is "stale", never "normal". Cleared alerts stay in the record, marked resolved.

Baseline (build item 7b):
- Bins by operating state: engine off, warm-up (coolant under 85 °C), warm idle, warm cruise, warm high load. Trims use closed-loop data only.
- Stats: median and σ = 1.4826·MAD of 10 s window medians [F]. MAD floored at the PID resolution, because a thermostat-held coolant value can give MAD = 0 [I].
- Flag: |z| over 4 sustained for 30 s [P].
- Arming: at least 3 drives and 180 windows per bin [P]. Until then: "Learning baseline: n of 3 drives".
- Windows that already alerted are left out. The baseline is versioned. Baseline alerts cite `{bin, median, mad, n_drives, version}`.

## 7. Algorithms (each with a known-answer test)

- **IMU mount alignment:** level from gravity while stationary; yaw from straight-line accel and braking samples. Test: rotate synthetic data by (5°, -3°, 30°) and recover it within 1e-9 without noise.
- **Filter A (longitudinal), state [v, b]:** predict at the IMU rate; update with GPS speed, wheel speed or OBD speed, and zero velocity at rest; gate each update at NIS < 6.635. **Filter B (yaw):** r = gyro_z - b_z, with b_z re-estimated at each standstill. Lateral g = v·r/g0 is primary; a_y/g0 is a cross-check. GPS dropout: dead-reckon for at most [needs a number] s with `dr = 1`, and suppress performance metrics when the speed variance passes [needs a number].
- **0 to 60 mph** (60 mph = 26.8224 m/s [F]): arm after 1 s stationary; t0 from the 0.3 m/s crossing, back-extrapolated to v = 0. Report "from standstill" and "1 ft rollout". Test at a constant 0.5 g: 5.470247230 s and 5.117651427 s.
- **60 to 0 mph braking:** start at the downward 60 mph crossing with decel of 0.3 g or more; end extrapolated to v = 0; trapezoid integral. Test: 36.68128983 m at 1.0 g and 45.85161228 m at 0.8 g.
- **Peak lateral g:** max of |v·r|/g0 after a centered 0.5 s moving average, plus a sustained peak held 0.5 s [P]. Test: v²/(R·g0) on a circle; an injected 2 g spike leaves the peak unchanged.
- **Lap timing:** segment intersection in local ENU meters with linear interpolation; minimum lap time and distance guards. The start and finish line is set in config or with a parked-only control. Test: a stadium track at constant v gives exactly L/v; a false re-crossing is ignored. Do not copy GPL code (DovesLapTimer).
- **Driver behavior:** hard braking 0.30 g and severe 0.45 g over a 1.0 s mean; hard acceleration 0.35 g; cornering 0.40 g and 0.50 g [F, telematics sources]; nothing below 2 m/s [P]. Jerk: mean |j| and p95 against the owner's baseline, with no invented "smooth" cutoff. Shift points: v/rpm clusters within ±3% [P] (the E90 6-speed manual has no gear signal; DCT gear frame ID is [U]). RPM bands from the vehicle profile.
- **Tolerances:** noise-free known-answer tests assert within 1e-6. Noisy variants assert within 1%.

## 8. Visual system

Design skill: impeccable (new visual world, Operate mode). Seed 14e368b3 assigned candidate 7 of 7 from a resonance-ordered list of the owner's world: (1) motorsport data-acquisition traces, (2) strip-chart recorder paper, (3) circuit timing screens, (4) workshop manuals and wiring diagrams, (5) flight-data event reconstruction, (6) oscilloscope and CAN decoder, (7) rally roadbook and pace notes.

### 8.1 Direction: Roadbook

A drive is a roadbook: a numbered run of boxes, each pinned to a time and a distance, each citing the signals that prove it. It refuses the category default (a dark gauge cluster with glowing arcs) and the card-grid analytics dashboard.

- **Scene that sets the ground:** the owner reviews a drive parked, often in daylight at a paddock or driveway with a laptop on the trunk lid. That forces a light, high-contrast paper ground.
- **Post-drive review (hero):** the roadbook scroll fills the left. Each row is a box: box number, time and distance (total and partial), a tulip glyph of the heading change, and a note with its cited signals. The map trace and stacked channel strips share one time cursor on the right.
- **Signature interaction:** select a box, and a marker-yellow swipe lands on its exact cited window across every strip and on the map.
- **Live view:** a rally tripmeter. Fixed digit banks, 1 caution line with its citation, and the safety line.
- **Cross-surface reach:** drive list = stage list; export = stage sheet; settings = legend page (symbols and thresholds).
- **Honest risk:** roadbooks are niche even among track-day drivers, and the box scroll can read as a plain list unless it stays tied to the strips.

Raises taken from declined challengers:
- **Fixed digit banks** (from the nixie counter): every live number owns fixed digit positions; a value never shifts the layout; a reset shows as an event.
- **Evidence pins** (from the tensegrity column): every alert pins to its exact samples; signal state shows by line form (live solid, stale dashed, missing struck, trimmed dotted).
- **Stepped motion** (from the acetate manual): nothing eases or fades; changes are at most 2-frame steps under 100 ms; reduced motion removes them.
- **Deterministic replay** (from the rain garden): the same drive and seed render the same frames, so shots rerun identically.
- **Resolved prints gray** (from the goods catalog): a cleared alert stays in the roadbook, printed in box rule gray.

### 8.2 Colors (6)

| Name | Hex | Role | Contrast |
|---|---|---|---|
| Roll white | #F5F7F7 | paper ground (cool, not cream) | |
| Tulip ink | #111417 | text, traces, tulips, full-weight rules | 17.19:1 on roll white |
| Box rule | #5B666D | secondary text, box borders, grid | 5.48:1 |
| Caution red | #C4161C | caution marks and anomaly marks only | 5.61:1; 4.76:1 on marker yellow |
| Marker yellow | #FFE600 | highlighter fill: selected box, cited window | ink on it 14.58:1 |
| Marker green | #5EE38C | highlighter fill: performance runs, laps | ink on it 11.30:1; red or gray on it fails AA (3.69, 3.60), so only ink goes on green |

### 8.3 Type

- **Overpass** (OFL-1.1, variable weight 100 to 900): all text. It descends from Highway Gothic, the lettering of US road signs. It has tabular figures and a slashed zero [F, font inspected]; numbers always use them. Cap height 0.7 em.
- **DSEG7 Classic Bold** (OFL-1.1, v0.46): 7-segment tripmeter digits, live view only. Digit height 1.0 em, advance 0.816 em [F, font inspected].
- Both are self-hosted in the repo with their licenses, because the device runs offline.

Review type scale (16 px base, ratio 1.25): 13 (meta, caps, +0.06 em tracking) / 16 (body) / 20 (box note) / 25 (box number) / 31 (section) / 39 (drive title) / 49 (report cover number).

### 8.4 Live view at 1 m

ISO 15008 character height: 20 arcmin recommended, 16 acceptable, 12 minimum [F, secondary sources]. Proposal: every live text element at 20 arcmin or more, which is a 5.818 mm cap height at 1 m.

| Element | Physical | Laptop 1440x900 [A: 13.3 in, 0.1987 mm/px] | Phone 390x844 [A: 6.1 in, 0.1657 mm/px] |
|---|---|---|---|
| Smallest live text (safety line, alert, citation, units) | 20 arcmin, 5.818 mm cap | Overpass 41.8 px | Overpass 50.2 px |
| Digits (largest) | 40 arcmin or more, 11.636 mm digit height [P] | DSEG7 58.6 px minimum; the laptop layout has room for more | DSEG7 70.2 px |

Fit check on the phone, from measured font advances (Overpass 600 and 800, DSEG7 Bold) with greedy word wrap, 358 px usable width, line height 1.15:
- Number rows with inline units: "074 mph" 288 px, "4280 rpm" 337 px, "106 °C" 240 px. All fit on 1 line.
- Alert headline "!! Coolant 106 °C": 2 lines. Citation "OBD 0x05 over 105 °C for 12 s": 2 lines. Safety line: 4 lines.
- Total: 801 px of 844.
- The longer citation "OBD 0x05 over 105 °C for the last 12 s" takes 3 lines and overflows (859 px). A first check without word wrap wrongly passed it; the rendered mock caught the error.

Phase 3 measures the rendered cap heights with Playwright and reports mm and arcmin.

### 8.5 Alternates offered

- **Emission-line rail** (competitive challenger): every event is a hairline on one off-center time rail, and state shows by line form. It holds on product clarity. Risk: charcoal and glowing hairlines sit near the near-black-with-neon default; car owners will not recognize the world; its reference board uses arrow-suffixed links, which the brief bans.
- **Trace stack** (the designer's own top pick): the stacked-channel language of motorsport data acquisition, on one cursor. Highest recognition. Risk: familiar, and it invites direct comparison with MoTeC and AiM.
- **Category standard** (standing exit): a dark gauge dashboard played straight. If chosen, name 2 or 3 products it should sit beside.
- **Declined:** gravity-rain garden, tensegrity column, nixie counter, acetate tab manual, character goods catalog. Each lost on audience recognition and product clarity; the roadbook kept one discipline from each (8.1).

## 9. Stack

| Layer | Pick | Notes |
|---|---|---|
| Device OS | Raspberry Pi OS Trixie (Debian 13) [A] | Ships Python 3.13.5 [F] |
| Python | 3.13, `>=3.13,<3.14` | The Mac already has 3.13.5 [F], so no download |
| CAN | python-can 4.6.1 (LGPL-3.0-only), cantools 44.1.0 (MIT) | Listed in the brief. python-can stays an unmodified dependency, never vendored [I] |
| DBC | Vendored opendbc DBCs at a pinned commit, with LICENSE and SOURCE | `bmw_e9x_e8x.dbc` (checked in upstream) and the generated Toyota DBC for comma2k19 replay |
| Server | FastAPI + uvicorn; server-sent events for the live stream | One-way push suits rule 5 |
| Front end | Vite + Preact (TSX) + @preact/signals; TypeScript strict; ESLint (typescript-eslint); Prettier | typescript-eslint 8.70.1 needs TypeScript below 6.1.0 [F] |
| Charts | uPlot (MIT) | Plus a text summary and a keyboard-usable events table |
| Map | SVG trace in local meters with a scale bar | No WebGL, no tiles, no network |
| Model | anthropic SDK, `claude-haiku-4-5-20251001` | $1 / $5 per million input / output tokens [F]. Active; retirement "not sooner than October 15, 2026" with at least 60 days' notice [F]. Model ID in config |
| Lap validation | jsonschema (Python) | |
| Capture | Playwright (Python), ffmpeg 8.1.1 (installed) | `make portfolio-capture` |
| Lighthouse | chrome-devtools MCP audit (no new dependency) | Fallback: `lighthouse` as a dev dependency, with approval |

Unlisted dependencies needing approval: uvicorn, numpy, pytest, hypothesis, ruff, mypy, anthropic, jsonschema; vite, preact, @preact/preset-vite, @preact/signals, uplot, typescript, eslint, typescript-eslint, prettier; the Playwright Chromium download (writes to `~/Library/Caches`, outside the repo); optional duckdb (scripts-only extra, for the dataset's Parquet demo split). Already installed, no action: ffmpeg 8.1.1, gitleaks 8.30.1, node 22.22.3, pnpm 10.13.1, uv 0.11.19.

### Model route

Server only; key from `ANTHROPIC_API_KEY`; fails closed without it. The owner sets the key on the device. Phase 2 tests use a fake client, so no key or spend is needed until the owner approves real calls.

1. Cache first: key = SHA-256 of `{model, prompt_version, system, stats, max_tokens}`.
2. Per-IP sliding window: 5 per minute and 30 per day [P]; over the limit returns 429 with `Retry-After`.
3. Daily spend cap $0.25 [P]: reserve the worst case under a lock, then correct from `usage`.
4. Input: stats only, as `{id, label, value, unit, display}`. Output: `{"sentences":[{"text","stat_ids"}]}`, 3 to 5 sentences [P].
5. Validator: every sentence cites existing stat IDs; every number string-matches a cited `display`; no dashes; no banned words. Retry once, then fall back to a deterministic template.
6. Prompt caching does not apply: the Haiku 4.5 minimum cacheable length is 4,096 tokens [F]. Offline shows "Needs connection", with no hidden queue.

## 10. Dataset

- **comma2k19** [F]: one-minute segments of California highway driving. Chunks 1 and 2 are a 2017 Toyota RAV4; chunks 3 to 10 are a 2016 Honda Civic.
- **License** [F]: the LICENSE file reads "MIT License / Copyright (c) 2018 comma.ai"; the Hugging Face card says `license: mit`. Keep the notice and credit the source.
- **CI and replay segment:** `Example_1/b0c9d2329ad1606b|2018-08-02--08-34-47/40` (RAV4), 7,206,664 bytes of `processed_log` and `global_pose`, pinned at commit `4c7f1a6e1957745beadc1def0e7225f559b09a2a`, SHA-256 checked. Skip video, `raw_log.bz2`, and preview images (real plates).
- **Full-drive replay (acceptance):** consecutive segments of 1 route. Phase 2 verifies route membership and columns in the Hugging Face demo split (64 segments, 227,930,673 bytes, revision `4bff77c7254c654c28d4c2726186b4e825adccee`) and drops its `preview` image column.
- **Figure 1.4:** the example segment never stops (speed 7.974 to 19.841 m/s) [F]. Phase 2 scans for a stretch from under 0.5 m/s to at least 26.8224 m/s. If none exists, figure 1.4 is dropped, as the brief allows.
- **Fixture policy:** unit tests generate data in code with a fixed seed and a fake origin. `make fetch-dataset` downloads into gitignored `data/public/`. CI caches it by SHA. 0 drive logs in the repo.

## 11. File tree

```
photoncar/
  PRODUCT.md                 # product record (impeccable)
  docs/phase-1-plan.md       # this plan
  pyproject.toml uv.lock .python-version
  Makefile                   # setup lint typecheck test test-vcan build fetch-dataset replay serve
                             # privacy-scan secrets-scan stealth-scan copy-lint portfolio-capture
  .gitignore                 # data/ build/ *.sqlite* .env .env.* !.env.example config/privacy.local.toml
                             # *.gpx *.kml *.geojson *.csv *.parquet *.asc *.blf *.log *.nmea *.ubx
                             # web/node_modules/ web/dist/ .venv/ __pycache__/ test-results/
  .env.example LICENSE README.md
  config/                    # photoncar.toml (thresholds, rates, model id), privacy.example.toml,
                             # vehicles/e90_m3.toml (bands, PIDs, DBC choice)
  src/photoncar/
    clock.py records.py units.py cli.py
    sources/                 # base socketcan obd(single TX owner) gps imu comma2k19 recorded synthetic
    safety/                  # tx_guard.py listen_only.py
    decode/                  # dbc.py obd_pids.py vendor/opendbc/{*.dbc,LICENSE,SOURCE}
    pipeline/                # merge.py runner.py
    estimate/                # alignment.py kalman.py
    detect/                  # rules baseline behavior performance laps
    store/                   # schema.sql writer.py reader.py rollup.py
    privacy/                 # zones.py export_filter.py
    export/                  # laps_v0.py report.py schemas/lap.v0.schema.json
    summary/                 # stats.py model_client.py limits.py cache.py validate.py
    api/                     # app.py routes/{live,drives,events,export,summary}.py
  web/                       # Vite + Preact: src/{frame,live,review,shared}/, public/fonts/ (+OFL)
  capture/                   # capture.py shots.toml seed_demo.py
  tests/                     # unit/ integration/ vcan/ e2e/ conftest.py
  scripts/                   # fetch_comma2k19_sample.py privacy_scan.py stealth_scan.py copy_lint.py
    provision/               # can0.network (listen-only) can1.network (one-shot) README (owner approves)
  portfolio/                 # public/work/photoncar/ figures.json content/work/photoncar.mdx
  .github/workflows/ci.yml   # python (uv, ruff, mypy, pytest) | web (tsc, eslint, prettier, build)
                             # | replay (cached dataset) | vcan (linux-modules-extra; non-blocking fallback)
  data/                      # gitignored: drives, public dataset cache, exports
```

## 12. Test plan

Phase 2 (recorded and synthetic data, no car):
1. Known answers: 0 to 60 (5.470247230 s, 5.117651427 s), braking (36.68128983 m, 45.85161228 m), peak lateral g, lap time L/v, IMU alignment.
2. OBD PID decode round-trips for every formula in section 3.
3. `tx_guard`: allowed and forbidden frame tables (Flow Control 0x3_, services 04 and 09, UDS 0x10, 0x27, 0x2E, ID 0x6F1), hypothesis fuzzing, token bucket on a fake clock, AST single-owner test.
4. DBC decode: E90 frames encoded with cantools decode to known values (0xAA rpm = raw/4, 0x1D0 temps offset -48).
5. Rules and baseline: injected faults hit each on, off, and hysteresis edge; stale detection; MAD floor; arming at 3 drives.
6. Citation query returns 0 rows.
7. Privacy: property test with a known synthetic home finds 0 exported samples within R in any stream; export fails closed without config.
8. Model route with a fake client: validator rejects each violation; cache hit skips spend; spend reservation and 429.
9. Every exported lap validates against lap.v0.
10. Replay: the comma2k19 segment runs end to end with 0 errors; replaying twice gives an identical hash of `events`; report the realtime factor (data span over `perf_counter_ns` elapsed), frames/s, and time per stage.
11. Frame: the safety line is visible on every route and state at 1440 and 390.

Phase 3 (hardware, after approval): bench pass rule (2.2); Pi realtime factor; provisioning review; polish bar checks (states per screen, 0 console errors and warnings via a Playwright listener, keyboard and focus, contrast, 1440 and 390 layouts, reduced motion, TS strict and lint, Lighthouse top 3, live text sizes in mm and arcmin).

## 13. Proposed values without a source (need approval)

Coolant: on after 10 s, clear below 102 °C for 30 s. Trims: on after 30 s. Voltage: 60 s, ignore 10 s after start, 24 h rest. Stale after 3 poll periods. Baseline |z| over 4 for 30 s, arming at 3 drives and 180 windows. Stationary 2 s; alignment |dv/dt| over 1 m/s²; 0.3 m/s start and v = 0 end extrapolation; lateral g windows 0.5 s; shift clusters ±3%, dwell 0.5 s, 0.3 s look-back; behavior floor 2 m/s. Reorder watermark 200 ms; clock step 1 ms; GPS fit 30 s; 40 bytes per frame; live stream 10 Hz; digits at 40 arcmin. SQLite batch 1,000 ms. Model: $0.25 per day, 5 per minute and 30 per day per IP, 3 to 5 sentences. Video: 240 frames for 8 s at 30 fps, CRF 23 (MP4) and 36 (WebM).

None of these appears in UI copy, the README, or the case study as a fact.

## 14. Risks and open questions

1. MSS60 supported PIDs are unknown until the first connect.
2. Polling rate through the JBE gateway is unknown until measured.
3. PT-CAN tap: terminator location on the M3, exact pins (X14271 pins 1/2 is the only sourced point), wire colors per VIN, and DCT gear frame ID are all unknown.
4. Figure 1.4 may not exist in the dataset.
5. Lap v0 is a stand-in until AI Race Engineer publishes its schema (its session is running in parallel in `~/dev/race-engineer`).
6. Waveshare does not list the Pi 5; CarPiHAT stacking is unknown.
7. ISO 15008 figures come from secondary sources; device mm/px values are assumptions until measured.
8. python-can is LGPL-3.0-only inside an MIT repo; kept as an unmodified dependency. Not legal advice.
9. Haiku 4.5 may retire after October 15, 2026; the model ID is config.
10. The affiliation line for the case study and README is the owner's call (asked in Phase 4).

### 13.1 Added in Phase 2 (no external source; owner approval needed)

| Area | Value |
|---|---|
| OBD poller | Discovery retry 5.0 s when no ECU answers 01 00 |
| TX audit and bench | 1 frame of slack per 1 s window on bench logs only (timestamp jitter); runtime audit cap matched to the token bucket (11 per 1 s, 102 per 10 s at 10 Hz); retransmit = identical request under 1 ms apart; link and counter re-check every 1 s |
| Estimator | accel sigma 0.5 m/s^2, bias random walk 0.01, initial bias sigma 0.5, wheel speed sigma 0.1 m/s, re-init after 3 gated rejects; speed-only mode: jerk PSD 64 (m/s^3)^2 s, accel prior 1 g, switch-over after 3 IMU periods (28.8 ms) |
| Performance | 0.3 s linear fit to extrapolate a run's start or end to v = 0 |
| Behavior | Gear inference needs at least 60% of samples in a cluster; max gears 7 (E90 M3), 10 when unknown; comma2k19 RAV4 and Civic rpm bands 0 to 7000 in 1000 steps; RAV4 transmission "automatic" (not stated in the cited sources) |
| Privacy | 100 m/s speed bound for trimming non-GPS streams across GPS gaps; 0.6% padding on R for the sphere approximation |
| Store | Rollup gap limits of 3 nominal periods per stream; SQLite journal_size_limit 64 MiB |
| Summary | 1024 tokens of prompt overhead in the worst-case spend reservation |
| Sensors | UBX max payload 1024 bytes; ACK wait 1.0 s; GPS read wait 0.2 s, write wait 1.0 s; IMU FIFO read every 0.05 s; IMU reset wait 10 x 1 ms; hdop filled from pDOP (NAV-PVT has no HDOP) |
| Web | Health poll 2 s; live reconnect backoff 1, 2, 4, 8 s (cap 8 s); embed live window opens 20 s before the most severe alert; embed budgets 1.5 MB JSON and 250 KB JS plus CSS; tulip glyph and map label placement heuristics |

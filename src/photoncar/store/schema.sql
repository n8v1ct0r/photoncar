-- Photoncar drive file schema, version 1. One SQLite file per drive: data/drives/<drive_id>.sqlite
-- Pragmas set by the writer: journal_mode=WAL, synchronous=FULL.
-- All times are t_ns on the drive's single timeline. All values are SI.

CREATE TABLE IF NOT EXISTS drive (
  drive_id        TEXT PRIMARY KEY,
  source          TEXT NOT NULL CHECK (source IN ('live', 'replay', 'synthetic')),
  dataset_ref     TEXT,
  boot_id         TEXT,
  t_start_ns      INTEGER,
  t_end_ns        INTEGER,
  utc_offset_ns   INTEGER,
  utc_fit_rms_ns  INTEGER,
  vehicle_profile TEXT,
  dbc_name        TEXT,
  dbc_sha256      TEXT,
  imu_mount_rot   TEXT,
  schema_version  INTEGER NOT NULL,
  closed_cleanly  INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE IF NOT EXISTS signals (
  signal_id  INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  unit       TEXT NOT NULL,
  source     TEXT NOT NULL CHECK (source IN ('can', 'obd', 'gps', 'imu', 'derived')),
  nominal_hz REAL,
  origin     TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS clock_sync (
  t_ns      INTEGER NOT NULL,
  other_ns  INTEGER NOT NULL,
  source    TEXT NOT NULL,
  offset_ns INTEGER NOT NULL,
  gap_ns    INTEGER NOT NULL,
  step      INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE IF NOT EXISTS raw_can (
  t_ns   INTEGER NOT NULL,
  bus    INTEGER NOT NULL,
  arb_id INTEGER NOT NULL,
  is_ext INTEGER NOT NULL,
  dlc    INTEGER NOT NULL,
  data   BLOB NOT NULL,
  is_tx  INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE IF NOT EXISTS samples (
  t_ns      INTEGER NOT NULL,
  signal_id INTEGER NOT NULL REFERENCES signals (signal_id),
  value     REAL NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS gps_fixes (
  t_ns       INTEGER NOT NULL,
  rx_t_ns    INTEGER NOT NULL,
  utc_ns     INTEGER,
  lat_deg    REAL NOT NULL,
  lon_deg    REAL NOT NULL,
  alt_m      REAL,
  speed_mps  REAL,
  course_rad REAL,
  hdop       REAL,
  fix_type   INTEGER,
  n_sats     INTEGER
) STRICT;

CREATE TABLE IF NOT EXISTS imu_samples (
  t_ns   INTEGER NOT NULL,
  ax     REAL NOT NULL,
  ay     REAL NOT NULL,
  az     REAL NOT NULL,
  gx     REAL NOT NULL,
  gy     REAL NOT NULL,
  gz     REAL NOT NULL,
  temp_k REAL
) STRICT;

CREATE TABLE IF NOT EXISTS obd_polls (
  req_t_ns  INTEGER NOT NULL,
  resp_t_ns INTEGER,
  service   INTEGER NOT NULL CHECK (service = 1),
  pid       INTEGER NOT NULL,
  ecu       INTEGER,
  status    TEXT NOT NULL CHECK (status IN ('ok', 'timeout', 'nrc')),
  raw       BLOB NOT NULL,
  value     REAL,
  unit      TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS state (
  t_ns               INTEGER NOT NULL,
  speed_mps          REAL NOT NULL,
  accel_long_mps2    REAL NOT NULL,
  accel_lat_mps2     REAL,             -- NULL: speed-only estimate (no IMU)
  accel_lat_imu_mps2 REAL,             -- NULL: speed-only estimate (no IMU)
  yaw_rate_rps       REAL,             -- NULL: speed-only estimate (no IMU)
  var_speed          REAL NOT NULL,
  var_accel          REAL NOT NULL,
  inputs             TEXT NOT NULL,
  dr                 INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE IF NOT EXISTS events (
  event_id     INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,
  rule_id      TEXT NOT NULL,
  rule_version INTEGER NOT NULL,
  severity     INTEGER NOT NULL CHECK (severity BETWEEN 0 AND 3),
  t_start_ns   INTEGER NOT NULL,
  t_end_ns     INTEGER NOT NULL CHECK (t_end_ns > t_start_ns),
  value        REAL NOT NULL,
  threshold    REAL,
  unit         TEXT NOT NULL,
  message      TEXT NOT NULL,
  resolved     INTEGER NOT NULL DEFAULT 0,
  synthetic    INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE IF NOT EXISTS event_citations (
  event_id   INTEGER NOT NULL REFERENCES events (event_id),
  signal     TEXT NOT NULL,
  source     TEXT NOT NULL,
  role       TEXT NOT NULL CHECK (role IN ('trigger', 'context')),
  t_start_ns INTEGER NOT NULL,
  t_end_ns   INTEGER NOT NULL,
  stat       TEXT NOT NULL,
  stat_value REAL NOT NULL,
  unit       TEXT NOT NULL,
  n          INTEGER NOT NULL,
  PRIMARY KEY (event_id, signal, role)
) STRICT;

CREATE TABLE IF NOT EXISTS laps (
  lap_id      INTEGER PRIMARY KEY,
  line_id     TEXT NOT NULL,
  lap_no      INTEGER NOT NULL,
  t_start_ns  INTEGER NOT NULL,
  t_end_ns    INTEGER NOT NULL,
  duration_ns INTEGER NOT NULL,
  valid       INTEGER NOT NULL
) STRICT;

-- Rollup built at close for the review UI. x_m/y_m are local meters (ENU) from the first fix.
CREATE TABLE IF NOT EXISTS timeline_10hz (
  t_ns            INTEGER PRIMARY KEY,
  speed_mps       REAL,
  rpm             REAL,
  throttle        REAL,
  coolant_k       REAL,
  accel_long_mps2 REAL,
  accel_lat_mps2  REAL,
  x_m             REAL,
  y_m             REAL,
  dr              INTEGER NOT NULL DEFAULT 0
) STRICT;

-- Drive-level computed stats (the only input the model summary may see).
CREATE TABLE IF NOT EXISTS drive_stats (
  stat_id TEXT PRIMARY KEY,
  label   TEXT NOT NULL,
  value   REAL NOT NULL,
  unit    TEXT NOT NULL,
  display TEXT NOT NULL
) STRICT;

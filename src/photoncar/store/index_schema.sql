-- Photoncar index database, version 1: data/index.sqlite

CREATE TABLE IF NOT EXISTS drives_index (
  drive_id       TEXT PRIMARY KEY,
  source         TEXT NOT NULL CHECK (source IN ('live', 'replay', 'synthetic')),
  label          TEXT NOT NULL,
  t_start_ns     INTEGER,
  t_end_ns       INTEGER,
  distance_m     REAL,
  n_events       INTEGER NOT NULL DEFAULT 0,
  n_alerts       INTEGER NOT NULL DEFAULT 0,
  closed_cleanly INTEGER NOT NULL DEFAULT 0,
  created_seq    INTEGER NOT NULL,
  utc_start_ns   INTEGER,
  utc_end_ns     INTEGER
) STRICT;

CREATE TABLE IF NOT EXISTS baselines (
  signal       TEXT NOT NULL,
  bin          TEXT NOT NULL,
  median       REAL NOT NULL,
  mad          REAL NOT NULL,
  n_windows    INTEGER NOT NULL,
  n_drives     INTEGER NOT NULL,
  version      INTEGER NOT NULL,
  updated_drive TEXT,
  PRIMARY KEY (signal, bin, version)
) STRICT;

CREATE TABLE IF NOT EXISTS model_summaries (
  input_sha256    TEXT PRIMARY KEY,
  model           TEXT NOT NULL,
  prompt_version  INTEGER NOT NULL,
  stats_json      TEXT NOT NULL,
  output_json     TEXT NOT NULL,
  in_tokens       INTEGER NOT NULL,
  out_tokens      INTEGER NOT NULL,
  cost_usd_micros INTEGER NOT NULL,
  created_seq     INTEGER NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS spend_ledger (
  day_utc    TEXT PRIMARY KEY,
  usd_micros INTEGER NOT NULL
) STRICT;

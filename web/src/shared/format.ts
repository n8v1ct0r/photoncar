/** Display units: speeds in mph, distances in miles, temperatures in °C, acceleration in g.
 * Every number shown comes from the API; these only convert units and round. */

import type { CitationOut, DriveDetail, DriveSource } from "./types";

export const MPS_PER_MPH = 0.44704;
export const M_PER_MILE = 1609.344;
export const KM_PER_MILE = M_PER_MILE / 1000;
export const M_PER_FOOT = 0.3048;
export const KELVIN_OFFSET = 273.15;
export const G0 = 9.80665;

export const SAFETY_LINE = "Not a safety system. Do not use while driving.";
export const DATASET_CREDIT = "Replay of public data (comma2k19, MIT)";

/** A public-dataset replay carries the dataset credit; a synthetic drive's own seed ref does not. */
export function hasDatasetCredit(d: Pick<DriveDetail, "dataset_ref" | "source">): boolean {
  return (
    d.dataset_ref !== null && d.source !== "synthetic" && !d.dataset_ref.startsWith("synthetic:")
  );
}

/** Keep a number with its unit on 1 line ("106 °C", "11 s", "24 h", "4 sigma"). */
export function keepUnits(text: string): string {
  return text.replace(
    /(\d) (?=(°C|s|min|h|mph|rpm|g|V|m|km|mi|ft|%|sigma)(?![\p{L}\d]))/gu,
    "$1\u00a0",
  );
}

export function fixed(x: number, digits: number): string {
  const scale = 10 ** digits;
  if (Math.round(Math.abs(x) * scale) === 0) x = 0;
  return x.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function mph(mps: number): number {
  return mps / MPS_PER_MPH;
}

export function miles(m: number): number {
  return m / M_PER_MILE;
}

/** "11.27 mi" */
export function milesText(m: number | null): string {
  if (m === null) return "no distance";
  const mi = miles(m);
  return `${fixed(mi, mi < 100 ? 2 : 1)} mi`;
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** "00:03:12", the roadbook clock from the drive start. */
export function clock(tS: number): string {
  const s = Math.max(0, Math.floor(tS + 1e-9));
  return `${pad2(Math.floor(s / 3600))}:${pad2(Math.floor((s % 3600) / 60))}:${pad2(s % 60)}`;
}

/** "03:12.4" (or "1:03:12.4" past an hour), for citation windows and cursors. */
export function clockTenths(tS: number): string {
  const tenths = Math.max(0, Math.round(tS * 10));
  const s = Math.floor(tenths / 10);
  const h = Math.floor(s / 3600);
  const mm = pad2(Math.floor((s % 3600) / 60));
  const ss = `${pad2(s % 60)}.${String(tenths % 10)}`;
  return h > 0 ? `${String(h)}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** "15 min 0 s", "42 s". */
export function durationText(s: number | null): string {
  if (s === null) return "no duration";
  const whole = Math.round(s);
  if (whole < 60) return `${String(whole)} s`;
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const rest = whole % 60;
  return h > 0 ? `${String(h)} h ${String(m)} min` : `${String(m)} min ${String(rest)} s`;
}

export function sourceBadge(source: DriveSource): string {
  if (source === "replay") return "Replay of public data";
  if (source === "synthetic") return "Synthetic";
  return "Live";
}

export function cautionMarks(severity: number): string {
  return "!".repeat(Math.max(0, Math.min(3, severity)));
}

export function severityLabel(severity: number): string {
  return `severity ${String(severity)} of 3`;
}

/** A citation's stat value in display units: "106 °C", "0.46 g", "64.2 mph", "2,403 rpm". */
export function statValueText(value: number | null, unit: string | null): string {
  if (value === null) return "no value";
  switch (unit) {
    case "K":
      return `${fixed(value - KELVIN_OFFSET, 1)} °C`;
    case "m/s":
      return `${fixed(mph(value), 1)} mph`;
    case "m/s^2":
      return `${fixed(value / G0, 2)} g`;
    case "m/s^3":
      return `${fixed(value, 2)} m/s³`;
    case "ratio":
      return `${fixed(value * 100, 1)}%`;
    case "rpm":
      return `${fixed(value, 0)} rpm`;
    case "V":
      return `${fixed(value, 2)} V`;
    case "s":
      return `${fixed(value, 2)} s`;
    case "m":
      return `${fixed(value, 1)} m`;
    case "g":
      return `${fixed(value, 2)} g`;
    case "gear":
      return `gear ${fixed(value, 0)}`;
    case null:
      return fixed(value, 2);
    default:
      return `${fixed(value, 2)} ${unit}`;
  }
}

const STAT_WORDS: Record<string, string> = {
  min: "min",
  max: "max",
  mean: "mean",
  median: "median",
  peak_abs_mean: "peak mean",
  sustained_abs_mean: "sustained mean",
  from_standstill_s: "from standstill",
  rollout_1ft_s: "1 ft rollout",
  time_s: "time",
  from_gear: "from",
  to_gear: "to",
  gap_s: "gap",
  last: "last",
};

export function statWord(stat: string | null): string {
  if (stat === null) return "value";
  return STAT_WORDS[stat] ?? stat.replace(/_/g, " ");
}

export function windowText(c: Pick<CitationOut, "t_start_s" | "t_end_s">): string {
  return `${clockTenths(c.t_start_s)} to ${clockTenths(c.t_end_s)}`;
}

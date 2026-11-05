/** The HTTP API types of docs/contracts.md ("HTTP API"). Times are seconds from the drive start;
 * values are SI plus g for acceleration, except `LiveFrame`, which carries mph and °C. */

export type DriveSource = "live" | "replay" | "synthetic";

export interface Health {
  ok: boolean;
  version: string;
  mode: "idle" | "live" | "replay";
  moving: boolean;
}

export interface DriveListItem {
  drive_id: string;
  label: string;
  source: DriveSource;
  duration_s: number | null;
  distance_m: number | null;
  n_events: number;
  n_alerts: number;
  closed_cleanly: boolean;
}

export interface DriveDetail extends DriveListItem {
  vehicle_profile: string | null;
  dataset_ref: string | null;
  synthetic_signals: string[];
}

export interface DriveStat {
  stat_id: string;
  label: string;
  value: number | null;
  unit: string;
  display: string;
}

export interface DriveBody {
  drive: DriveDetail;
  stats: DriveStat[];
}

export type TimelineColumn =
  "speed_mps" | "rpm" | "throttle" | "coolant_c" | "accel_long_g" | "accel_lat_g" | "x_m" | "y_m";

export type Timeline = {
  t_s: number[];
  dr: boolean[];
  sources?: Partial<Record<string, string | null>>;
} & Record<TimelineColumn, (number | null)[]>;

export interface CitationOut {
  signal: string;
  source: string;
  role: "trigger" | "context";
  t_start_s: number;
  t_end_s: number;
  stat: string | null;
  stat_value: number | null;
  unit: string | null;
  n: number | null;
}

export interface EventOut {
  event_id: number;
  /** 1-based roadbook box number; null for an event that is not a box (a gear shift, which the
   * report lists instead). */
  box_no: number | null;
  kind: string;
  rule_id: string | null;
  severity: 0 | 1 | 2 | 3;
  t_start_s: number;
  t_end_s: number;
  distance_m: number | null;
  value: number | null;
  threshold: number | null;
  unit: string | null;
  message: string;
  resolved: boolean;
  synthetic: boolean;
  citations: CitationOut[];
}

export interface RpmBand {
  lo: number;
  hi: number;
  seconds: number | null;
}

export interface Shift {
  t_s: number;
  rpm: number | null;
  from_gear: number | null;
  to_gear: number | null;
}

export interface BehaviorOut {
  hard_brakes: number;
  hard_accels: number;
  corners: number;
  jerk_long_mean: number | null;
  jerk_long_p95: number | null;
  jerk_lat_mean: number | null;
  jerk_lat_p95: number | null;
  rpm_bands: RpmBand[];
  shifts: Shift[];
}

export interface PerformanceOut {
  zero_to_sixty: { t_start_s: number; seconds: number | null; rollout_seconds: number | null }[];
  sixty_to_zero: { t_start_s: number; meters: number | null }[];
  peak_lateral_g: number | null;
}

export interface LapOut {
  lap_no: number;
  line_id: string;
  t_start_s: number;
  duration_s: number;
  valid: boolean;
}

export interface Report {
  behavior: BehaviorOut;
  performance: PerformanceOut;
  laps: LapOut[];
}

export interface LiveAlert {
  severity: number;
  headline: string;
  citation: string;
  synthetic: boolean;
}

export interface LiveFrame {
  t_s: number;
  speed_mph: number | null;
  rpm: number | null;
  coolant_c: number | null;
  synthetic_fields?: string[];
  alert: LiveAlert | null;
}

export interface LegendRule {
  rule_id: string;
  title: string;
  condition: string;
  severity: number;
  advisory: boolean;
}

export interface Legend {
  rules: LegendRule[];
  privacy_radius_km: number;
  max_request_hz: number;
  synthetic_note: string;
}

export interface SummarySentence {
  text: string;
  stat_ids: string[];
}

export interface SummaryOut {
  sentences: SummarySentence[];
  source: "model" | "template";
  cached: boolean;
  model: string | null;
}

export interface ExportOut {
  files: string[];
}

/** One message of the live feed: a frame, or the moving state. */
export type LiveMessage = { type: "frame"; frame: LiveFrame } | { type: "state"; moving: boolean };

/** Closed time windows, in seconds from the drive start, where an export trimmed positions. */
export type TrimWindows = [number, number][];

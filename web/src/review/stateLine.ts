/** The plain-English read of the car at 1 instant of the timeline, for the replay deck.
 *
 * Every number in the sentence is read straight from the timeline at that instant (speed, rpm,
 * and the estimator's longitudinal and lateral g). The words only sort the instant into a few
 * plain states. They never claim a hard-braking or cornering event: those verdicts come from
 * the analytics, over a window, and print as the roadbook box the instant falls in. */

import { fixed, mph } from "../shared/format";
import type { Timeline } from "../shared/types";
import { indexAt } from "./geometry";

/** Below this speed the car reads as stopped [proposed]. */
const STOPPED_MPS = 0.5;
/** Longitudinal g that reads as braking or speeding up rather than holding speed [proposed]. */
const LONG_G = 0.1;
/** Lateral g that reads as turning [proposed]. */
const LAT_G = 0.15;

export interface CarState {
  /** The sentence, ending in a period. */
  line: string;
  speedMph: number | null;
  rpm: number | null;
  longG: number | null;
  latG: number | null;
}

function at(col: (number | null)[], i: number): number | null {
  const v = col[i];
  return v === undefined || v === null || !Number.isFinite(v) ? null : v;
}

function g(x: number): string {
  return `${fixed(Math.abs(x), 2)} g`;
}

function rpmText(r: number): string {
  return `${Math.round(r).toLocaleString("en-US")} rpm`;
}

export function carStateAt(tl: Timeline, t: number): CarState {
  const i = indexAt(tl.t_s, t);
  const speed = at(tl.speed_mps, i);
  const rpm = at(tl.rpm, i);
  const longG = at(tl.accel_long_g, i);
  const latG = at(tl.accel_lat_g, i);
  const speedMph = speed === null ? null : mph(speed);
  const base = { speedMph, rpm, longG, latG };

  if (speed === null || speedMph === null) {
    return { ...base, line: "No speed at this moment. The signal is missing here." };
  }
  const v = `${String(Math.round(speedMph))} mph`;
  if (speed < STOPPED_MPS) {
    return { ...base, line: rpm === null ? "Stopped." : `Stopped. Engine at ${rpmText(rpm)}.` };
  }
  if (longG !== null && longG <= -LONG_G) {
    return { ...base, line: `Braking, ${g(longG)}, at ${v}.` };
  }
  if (longG !== null && longG >= LONG_G) {
    return { ...base, line: `Speeding up, ${g(longG)}, at ${v}.` };
  }
  if (latG !== null && Math.abs(latG) >= LAT_G) {
    return { ...base, line: `Turning, ${g(latG)} sideways, at ${v}.` };
  }
  return { ...base, line: rpm === null ? `Holding ${v}.` : `Holding ${v} at ${rpmText(rpm)}.` };
}

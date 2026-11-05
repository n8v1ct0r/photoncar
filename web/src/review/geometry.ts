/** Positions and headings from the timeline's local meters (x east, y north). */

import type { EventOut, Timeline, TrimWindows } from "../shared/types";

export interface Point {
  x: number;
  y: number;
}

/** Index of the last sample at or before `t` (0 when `t` is before the first). */
export function indexAt(t: number[], at: number): number {
  let lo = 0;
  let hi = t.length - 1;
  if (hi < 0 || at <= (t[0] ?? 0)) return 0;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if ((t[mid] ?? Infinity) <= at) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

function pointAt(tl: Timeline, i: number): Point | null {
  const x = tl.x_m[i];
  const y = tl.y_m[i];
  return x === null || x === undefined || y === null || y === undefined ? null : { x, y };
}

/** Position at time `at`, interpolated between 2 neighbouring samples that both have one. */
export function positionAt(tl: Timeline, at: number): Point | null {
  const i = indexAt(tl.t_s, at);
  const a = pointAt(tl, i);
  const b = pointAt(tl, Math.min(i + 1, tl.t_s.length - 1));
  const ta = tl.t_s[i];
  const tb = tl.t_s[Math.min(i + 1, tl.t_s.length - 1)];
  if (!a || ta === undefined) return null;
  if (!b || tb === undefined || tb <= ta || at <= ta) return a;
  const f = Math.min(1, (at - ta) / (tb - ta));
  return { x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f };
}

const REACH_M = 30;
const STILL_M = 4;

export type Tulip = { kind: "turn"; degrees: number } | { kind: "still" } | { kind: "none" };

function walk(tl: Timeline, from: number, step: 1 | -1): Point | null {
  const origin = pointAt(tl, from);
  if (!origin) return null;
  let last = origin;
  let travelled = 0;
  for (let i = from + step; i >= 0 && i < tl.t_s.length; i += step) {
    const p = pointAt(tl, i);
    if (!p) return null;
    travelled += Math.hypot(p.x - last.x, p.y - last.y);
    last = p;
    if (travelled >= REACH_M) return p;
  }
  return travelled >= STILL_M ? last : null;
}

/** The heading change across an event: from the path 30 m before its start to the path 30 m
 * after its end (the end capped at 60 s after the start). Positive degrees turn left
 * (counterclockwise), as a roadbook tulip draws them. */
export function tulipAt(tl: Timeline, t0: number, t1: number = t0): Tulip {
  const i = indexAt(tl.t_s, t0);
  const j = Math.max(i, indexAt(tl.t_s, Math.min(t1, t0 + 60)));
  const entry = pointAt(tl, i);
  const exit = pointAt(tl, j);
  if (!entry || !exit) return { kind: "none" };
  const before = walk(tl, i, -1);
  const after = walk(tl, j, 1);
  if (!before && !after && Math.hypot(exit.x - entry.x, exit.y - entry.y) < STILL_M) {
    return { kind: "still" };
  }
  if (!before || !after) return { kind: "turn", degrees: 0 };
  const a1 = Math.atan2(entry.y - before.y, entry.x - before.x);
  const a2 = Math.atan2(after.y - exit.y, after.x - exit.x);
  let d = ((a2 - a1) * 180) / Math.PI;
  while (d > 180) d -= 360;
  while (d < -180) d += 360;
  return { kind: "turn", degrees: d };
}

export function triggerWindows(ev: EventOut): [number, number][] {
  const triggers = ev.citations.filter((c) => c.role === "trigger");
  const cited = triggers.length ? triggers : ev.citations;
  if (cited.length === 0) return [[ev.t_start_s, ev.t_end_s]];
  return cited.map((c) => [c.t_start_s, c.t_end_s]);
}

export function inWindows(t: number, windows: TrimWindows): boolean {
  return windows.some(([a, b]) => t >= a && t <= b);
}

export function isPerformance(ev: EventOut): boolean {
  return ev.kind.startsWith("perf.");
}

export function isAlert(ev: EventOut): boolean {
  return ev.kind.startsWith("alert.");
}

/** An event the roadbook prints as a box: one the API gave a box number. Gear shifts carry
 * none (box_no null); the report lists them. */
export type BoxEvent = EventOut & { box_no: number };

export function roadbookBoxes(events: EventOut[]): BoxEvent[] {
  return events.filter((e): e is BoxEvent => e.box_no !== null);
}

/** The box a review opens on: the most severe alert, else the first box. */
export function defaultBox(boxes: BoxEvent[]): number | null {
  let best: BoxEvent | null = null;
  for (const ev of boxes) {
    if (isAlert(ev) && (!best || ev.severity > best.severity)) best = ev;
  }
  return best?.box_no ?? boxes[0]?.box_no ?? null;
}

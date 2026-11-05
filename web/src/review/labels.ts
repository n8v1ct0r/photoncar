/** Map label placement: every box marker and end label gets a spot where it overlaps no other
 * label and no fixed mark. Greedy, in a fixed order, so a spot never depends on the selection:
 * each label takes the free candidate nearest its preferred spot. A box marker prefers its own
 * point on the trace; when that is taken it steps out on a grid of 1 marker width and a leader
 * line points back to the trace. */

import type { Point } from "./geometry";

export interface Rect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/** Marker circles have r 11 and a stroke up to 2.4 px; 13 px half size leaves a 2 px gap. */
export const MARKER_HALF = 13;
/** Candidate grid for a displaced marker: 1 marker width plus the gap. */
const STEP = 2 * MARKER_HALF;
const RINGS = 5;

export function rectAround(p: Point, half: number): Rect {
  return { x0: p.x - half, y0: p.y - half, x1: p.x + half, y1: p.y + half };
}

export function overlaps(a: Rect, b: Rect): boolean {
  return a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;
}

function inside(r: Rect, w: number, h: number): boolean {
  return r.x0 >= 0 && r.y0 >= 0 && r.x1 <= w && r.y1 <= h;
}

/** Grid offsets around a point, nearest first; ties go up and right first, like a roadbook
 * note written beside the route. */
const GRID: Point[] = (() => {
  const out: Point[] = [];
  for (let i = -RINGS; i <= RINGS; i++) {
    for (let j = -RINGS; j <= RINGS; j++) out.push({ x: i * STEP, y: j * STEP });
  }
  const turn = (p: Point): number =>
    (Math.atan2(-p.y, p.x) - Math.PI / 4 + 4 * Math.PI) % (2 * Math.PI);
  return out.sort((a, b) => Math.hypot(a.x, a.y) - Math.hypot(b.x, b.y) || turn(a) - turn(b));
})();

/** Centers for markers at `anchors`, in order, clear of `taken` (which grows as they land). */
export function placeMarkers(anchors: Point[], taken: Rect[], w: number, h: number): Point[] {
  return anchors.map((a) => {
    let spot: Point | null = null;
    for (const off of GRID) {
      const c = { x: a.x + off.x, y: a.y + off.y };
      const r = rectAround(c, MARKER_HALF);
      if (inside(r, w, h) && !taken.some((t) => overlaps(t, r))) {
        spot = c;
        break;
      }
    }
    // No free spot within the rings: stay on the trace rather than wander off the map.
    const c = spot ?? a;
    taken.push(rectAround(c, MARKER_HALF));
    return c;
  });
}

/** A text label beside a mark: its left x and baseline y. */
export interface TextSpot {
  x: number;
  y: number;
}

/** Map end labels: 13 px text at weight 700 with a 2 px paper halo. The width is a generous
 * estimate (Overpass runs about 6.5 px a letter at this size), so the rect covers the ink. */
const TEXT_ASCENT = 13;
const TEXT_DESCENT = 5;
const TEXT_PX_PER_CHAR = 8.5;

function textRect(s: TextSpot, width: number): Rect {
  return { x0: s.x - 2, y0: s.y - TEXT_ASCENT, x1: s.x + width + 2, y1: s.y + TEXT_DESCENT };
}

/** A spot for `text` near the mark at `p` (right and above first), clear of `taken`. */
export function placeText(text: string, p: Point, taken: Rect[], w: number, h: number): TextSpot {
  const width = text.length * TEXT_PX_PER_CHAR;
  const spots: TextSpot[] = [];
  for (let d = 0; d <= 4 * MARKER_HALF; d += MARKER_HALF) {
    spots.push(
      { x: p.x + 10 + d, y: p.y - 8 - d },
      { x: p.x + 10 + d, y: p.y + 18 + d },
      { x: p.x - 10 - d - width, y: p.y - 8 - d },
      { x: p.x - 10 - d - width, y: p.y + 18 + d },
      { x: p.x + 12 + d, y: p.y + 5 },
      { x: p.x - 12 - d - width, y: p.y + 5 },
      { x: p.x - width / 2, y: p.y - 12 - d },
      { x: p.x - width / 2, y: p.y + 24 + d },
    );
  }
  const fits = spots.find((s) => {
    const r = textRect(s, width);
    return inside(r, w, h) && !taken.some((t) => overlaps(t, r));
  });
  const spot = fits ?? spots[0] ?? { x: p.x + 10, y: p.y - 8 };
  taken.push(textRect(spot, width));
  return spot;
}

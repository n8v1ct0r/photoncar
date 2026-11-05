import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { fixed, M_PER_MILE } from "../shared/format";
import type { Timeline, TrimWindows } from "../shared/types";
import { type BoxEvent, isAlert, positionAt, triggerWindows, type Point } from "./geometry";
import { placeMarkers, placeText, type Rect, rectAround } from "./labels";

interface Frame {
  minX: number;
  maxY: number;
  scale: number;
  offX: number;
  offY: number;
}

const PAD = 28;
const NICE_MILES = [0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 20, 50];
const MAX_CIRCLES = 36;

function bounds(tl: Timeline): { minX: number; maxX: number; minY: number; maxY: number } | null {
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  tl.x_m.forEach((x, i) => {
    const y = tl.y_m[i];
    if (x === null || y === null || y === undefined) return;
    minX = Math.min(minX, x);
    maxX = Math.max(maxX, x);
    minY = Math.min(minY, y);
    maxY = Math.max(maxY, y);
  });
  return Number.isFinite(minX) ? { minX, maxX, minY, maxY } : null;
}

function fit(
  b: NonNullable<ReturnType<typeof bounds>>,
  w: number,
  h: number,
  bottom: number,
): Frame {
  const spanX = Math.max(b.maxX - b.minX, 1);
  const spanY = Math.max(b.maxY - b.minY, 1);
  const scale = Math.min((w - 2 * PAD) / spanX, (h - 2 * PAD - bottom) / spanY);
  return {
    minX: b.minX,
    maxY: b.maxY,
    scale,
    offX: (w - spanX * scale) / 2,
    offY: PAD + (h - 2 * PAD - bottom - spanY * scale) / 2,
  };
}

interface Run {
  d: string;
  dr: boolean;
}

interface Gap {
  d: string;
  trimmed: boolean;
  mid: Point;
}

function project(f: Frame, p: Point): Point {
  return { x: f.offX + (p.x - f.minX) * f.scale, y: f.offY + (f.maxY - p.y) * f.scale };
}

function overlapsTrim(a: number, b: number, trim: TrimWindows): boolean {
  return trim.some(([s, e]) => a <= e && b >= s);
}

function niceBar(scale: number, width: number, traceWidth: number): { px: number; label: string } {
  const target = Math.max(48, Math.min(width * 0.22, traceWidth * 0.6));
  let best = NICE_MILES[0] ?? 0.1;
  for (const mi of NICE_MILES) if (mi * M_PER_MILE * scale <= target) best = mi;
  return { px: best * M_PER_MILE * scale, label: `${best < 1 ? fixed(best, 2) : String(best)} mi` };
}

export function MapTrace(props: {
  tl: Timeline;
  boxes: BoxEvent[];
  selected: BoxEvent | null;
  cursorT: number | null;
  trim: TrimWindows;
  onSelect: (box: number) => void;
}) {
  const holder = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  useEffect(() => {
    const el = holder.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      setSize({ w: el.clientWidth, h: el.clientHeight });
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
    };
  }, []);

  const { tl, trim } = props;
  const box = useMemo(() => bounds(tl), [tl]);
  const bottom = 26;
  const frame = box && size.w > 0 && size.h > 0 ? fit(box, size.w, size.h, bottom) : null;

  const drawn = useMemo(() => {
    if (!frame) return null;
    const pr = (p: Point): Point => project(frame, p);
    const runs: Run[] = [];
    const gaps: Gap[] = [];
    let cur: Run | null = null;
    let last: { p: Point; t: number } | null = null;
    let first: Point | null = null;
    for (let i = 0; i < tl.t_s.length; i++) {
      const x = tl.x_m[i];
      const y = tl.y_m[i];
      const t = tl.t_s[i] ?? 0;
      if (x === null || y === null || x === undefined || y === undefined) {
        cur = null;
        continue;
      }
      const p = pr({ x, y });
      first ??= p;
      const dr = tl.dr[i] === true;
      if (!cur && last) {
        gaps.push({
          d: `M${last.p.x.toFixed(1)} ${last.p.y.toFixed(1)}L${p.x.toFixed(1)} ${p.y.toFixed(1)}`,
          trimmed: overlapsTrim(last.t, t, trim),
          mid: { x: (last.p.x + p.x) / 2, y: (last.p.y + p.y) / 2 },
        });
      }
      if (!cur || cur.dr !== dr) {
        const join: string = cur && last ? `M${last.p.x.toFixed(1)} ${last.p.y.toFixed(1)}L` : "M";
        cur = { d: `${join}${p.x.toFixed(1)} ${p.y.toFixed(1)}`, dr };
        runs.push(cur);
      } else {
        cur.d += `L${p.x.toFixed(1)} ${p.y.toFixed(1)}`;
      }
      last = { p, t };
    }
    const t0 = tl.t_s[0] ?? 0;
    const tEnd = tl.t_s[tl.t_s.length - 1] ?? 0;
    const firstT = tl.t_s[tl.x_m.findIndex((x) => x !== null)] ?? t0;
    return {
      runs,
      gaps,
      start: first,
      finish: last?.p ?? null,
      trimmedStart: overlapsTrim(t0, firstT, trim),
      trimmedEnd: last !== null && overlapsTrim(last.t, tEnd, trim),
      bar: niceBar(
        frame.scale,
        size.w,
        Math.max(box ? box.maxX - box.minX : 0, box ? box.maxY - box.minY : 0) * frame.scale,
      ),
    };
  }, [frame?.scale, frame?.offX, frame?.offY, size.w, size.h, tl, trim]);

  const pr = (p: Point | null): Point | null => (frame && p ? project(frame, p) : null);

  const sel = props.selected;
  const swipe = useMemo(() => {
    if (!frame || !sel) return [];
    return triggerWindows(sel).map(([a, b]) => {
      const pts: Point[] = [];
      const pa = pr(positionAt(tl, a));
      if (pa) pts.push(pa);
      for (let i = 0; i < tl.t_s.length; i++) {
        const t = tl.t_s[i] ?? 0;
        if (t <= a || t >= b) continue;
        const x = tl.x_m[i];
        const y = tl.y_m[i];
        if (x === null || y === null || x === undefined || y === undefined) continue;
        const p = pr({ x, y });
        if (p) pts.push(p);
      }
      const pb = pr(positionAt(tl, b));
      if (pb) pts.push(pb);
      return pts.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join("");
    });
    // pr depends on the frame values listed here.
  }, [sel, frame?.scale, frame?.offX, frame?.offY, size.h, tl]);

  // Markers in box order; past MAX_CIRCLES only alerts, plus the selected box last, so the
  // selection never moves another marker.
  const shown = useMemo(() => {
    const boxes = props.boxes;
    return boxes.length > MAX_CIRCLES ? boxes.filter((e) => isAlert(e)) : boxes;
  }, [props.boxes]);
  const extra = sel && !shown.some((e) => e.box_no === sel.box_no) ? sel : null;

  const layout = useMemo(() => {
    if (!drawn || !frame) return null;
    const { w, h } = size;
    const taken: Rect[] = [];
    if (drawn.start) taken.push(rectAround(drawn.start, drawn.trimmedStart ? 10 : 6));
    if (drawn.finish) taken.push(rectAround(drawn.finish, drawn.trimmedEnd ? 10 : 6));
    const barEnd = PAD + drawn.bar.px + 8 + drawn.bar.label.length * 8.5;
    taken.push({ x0: PAD - 2, y0: h - 27, x1: barEnd + 2, y1: h - 9 });
    const markers: { ev: BoxEvent; at: Point }[] = [];
    for (const ev of extra ? [...shown, extra] : shown) {
      const at = pr(positionAt(tl, ev.t_start_s));
      if (at) markers.push({ ev, at });
    }
    const centers = placeMarkers(
      markers.map((m) => m.at),
      taken,
      w,
      h,
    );
    return {
      markers: markers.map((m, i) => ({ ...m, c: centers[i] ?? m.at })),
      start: drawn.start ? placeText("Start", drawn.start, taken, w, h) : null,
      finish: drawn.finish ? placeText("Finish", drawn.finish, taken, w, h) : null,
    };
    // pr depends on the frame values listed here.
  }, [drawn, shown, extra, size.w, size.h, tl]);

  // Draw order: plain boxes, then alerts, then the selected box on top of them all.
  const rank = (e: BoxEvent): number => (e.box_no === sel?.box_no ? 2 : isAlert(e) ? 1 : 0);
  const markers = layout ? [...layout.markers].sort((a, b) => rank(a.ev) - rank(b.ev)) : [];

  const cursor = props.cursorT === null ? null : pr(positionAt(tl, props.cursorT));

  return (
    <figure class="map" aria-label="Map trace in local meters">
      <div class="map-canvas" ref={holder}>
        {!box ? (
          <p class="state state-empty">No positions on this drive, so there is no trace.</p>
        ) : null}
        {drawn && frame ? (
          <svg width={size.w} height={size.h} role="img" aria-label="Drive trace with box numbers">
            {swipe.map((d, i) => (
              <path key={`sw${String(i)}`} d={d} class="map-swipe" />
            ))}
            {drawn.gaps.map((g, i) => (
              <g key={`g${String(i)}`}>
                <path d={g.d} class={g.trimmed ? "map-trimmed" : "map-missing"} />
                {g.trimmed ? null : (
                  <path
                    d={`M${(g.mid.x - 5).toFixed(1)} ${(g.mid.y + 5).toFixed(1)}l10 -10`}
                    class="map-strike"
                  />
                )}
              </g>
            ))}
            {drawn.runs.map((r, i) => (
              <path key={`r${String(i)}`} d={r.d} class={r.dr ? "map-stale" : "map-live"} />
            ))}
            {markers.map(({ ev, at, c }) =>
              at.x === c.x && at.y === c.y ? null : (
                <g key={`l${String(ev.box_no)}`} class="map-leader">
                  <path
                    d={`M${at.x.toFixed(1)} ${at.y.toFixed(1)}L${c.x.toFixed(1)} ${c.y.toFixed(1)}`}
                  />
                  <circle cx={at.x} cy={at.y} r="2.2" />
                </g>
              ),
            )}
            {drawn.start ? (
              <g class="map-end">
                {drawn.trimmedStart ? (
                  <circle cx={drawn.start.x} cy={drawn.start.y} r="9" class="map-trimmed" />
                ) : null}
                <circle cx={drawn.start.x} cy={drawn.start.y} r="4.5" class="map-start" />
              </g>
            ) : null}
            {drawn.finish ? (
              <g class="map-end">
                {drawn.trimmedEnd ? (
                  <circle cx={drawn.finish.x} cy={drawn.finish.y} r="9" class="map-trimmed" />
                ) : null}
                <rect
                  x={drawn.finish.x - 4.5}
                  y={drawn.finish.y - 4.5}
                  width="9"
                  height="9"
                  class="map-finish"
                />
              </g>
            ) : null}
            {/* Under the markers: at rest it sits on the selected box, which marks the same spot. */}
            {cursor ? <circle cx={cursor.x} cy={cursor.y} r="6" class="map-cursor" /> : null}
            {markers.map(({ ev, c }) => {
              const on = sel?.box_no === ev.box_no;
              return (
                <g
                  key={`c${String(ev.box_no)}`}
                  class={on ? "map-box is-selected" : isAlert(ev) ? "map-box is-alert" : "map-box"}
                  onClick={() => {
                    props.onSelect(ev.box_no);
                  }}
                >
                  <circle cx={c.x} cy={c.y} r="11" />
                  <text x={c.x} y={c.y + 4} text-anchor="middle">
                    {ev.box_no}
                  </text>
                </g>
              );
            })}
            {layout?.start ? (
              <text class="map-label" x={layout.start.x} y={layout.start.y}>
                Start
              </text>
            ) : null}
            {layout?.finish ? (
              <text class="map-label" x={layout.finish.x} y={layout.finish.y}>
                Finish
              </text>
            ) : null}
            <g class="map-scale">
              <path
                d={`M${String(PAD)} ${String(size.h - 20)}V${String(size.h - 14)}h${drawn.bar.px.toFixed(1)}V${String(size.h - 20)}`}
              />
              <text x={PAD + drawn.bar.px + 8} y={size.h - 14}>
                {drawn.bar.label}
              </text>
            </g>
          </svg>
        ) : null}
      </div>
      <figcaption class="map-cap">Local meters, no basemap.</figcaption>
    </figure>
  );
}

import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { clockTenths, fixed, KELVIN_OFFSET, MPS_PER_MPH } from "../shared/format";
import type { EventOut, Timeline, TimelineColumn } from "../shared/types";
import { indexAt, isAlert, triggerWindows } from "./geometry";

const INK = "#111417";
const RULE = "#5B666D";
const RED = "#C4161C";
const YELLOW = "#FFE600";
const HAIR = "#C3C9CC";
const FONT = '600 13px "Overpass", "Helvetica Neue", Arial, sans-serif';
const SYNC_KEY = "photoncar-strips";

interface Channel {
  key: TimelineColumn;
  name: string;
  unit: string;
  conv: (v: number) => number;
  digits: number;
  range?: [number, number];
  symmetric?: boolean;
}

export const CHANNELS: Channel[] = [
  { key: "speed_mps", name: "Speed", unit: "mph", conv: (v) => v / MPS_PER_MPH, digits: 1 },
  { key: "rpm", name: "RPM", unit: "rpm", conv: (v) => v, digits: 0 },
  {
    key: "throttle",
    name: "Throttle",
    unit: "%",
    conv: (v) => v * 100,
    digits: 0,
    range: [0, 100],
  },
  { key: "coolant_c", name: "Coolant", unit: "°C", conv: (v) => v, digits: 1 },
  { key: "accel_lat_g", name: "Lateral", unit: "g", conv: (v) => v, digits: 2, symmetric: true },
];

interface Prepared {
  ch: Channel;
  source: string | null;
  synthetic: boolean;
  live: (number | null)[];
  stale: (number | null)[];
  missing: [number, number][];
  staleWindows: [number, number][];
  empty: boolean;
  lo: number;
  hi: number;
}

function tickStep(lo: number, hi: number): number {
  const raw = (hi - lo) / 2.5;
  const mag = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= raw) ?? 10 * mag;
}

/** 2 or 3 round values inside the strip's range, for the y axis. */
function niceTicks(lo: number, hi: number): number[] {
  const step = tickStep(lo, hi);
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) out.push(v);
  return out;
}

function tickDigits(lo: number, hi: number): number {
  const step = tickStep(lo, hi);
  for (let d = 0; d < 3; d++) {
    const scaled = step * 10 ** d;
    if (Math.abs(Math.round(scaled) - scaled) < 1e-9) return d;
  }
  return 3;
}

function inAny(t: number, ws: [number, number][]): boolean {
  return ws.some(([a, b]) => t >= a && t <= b);
}

function prepare(
  tl: Timeline,
  ch: Channel,
  syntheticSignals: string[],
  events: EventOut[],
): Prepared {
  const source = tl.sources?.[ch.key] ?? null;
  const raw = tl[ch.key].map((v) => (v === null ? null : ch.conv(v)));
  const staleWindows: [number, number][] = [];
  if (source) {
    for (const ev of events) {
      if (ev.kind !== "alert.stale") continue;
      for (const c of ev.citations) {
        if (c.role === "trigger" && c.signal === source)
          staleWindows.push([c.t_start_s, c.t_end_s]);
      }
    }
  }
  const live: (number | null)[] = [];
  const stale: (number | null)[] = [];
  raw.forEach((v, i) => {
    const t = tl.t_s[i] ?? 0;
    const inStale = inAny(t, staleWindows);
    const nextStale = inAny(tl.t_s[i + 1] ?? t, staleWindows);
    const prevStale = inAny(tl.t_s[i - 1] ?? t, staleWindows);
    live.push(!inStale || !prevStale || !nextStale ? v : null);
    stale.push(inStale || nextStale || prevStale ? v : null);
  });
  const missing: [number, number][] = [];
  let runStart: number | null = null;
  for (let i = 0; i < raw.length; i++) {
    const v = raw[i];
    if (v === null && runStart === null) runStart = i;
    if (v !== null && runStart !== null) {
      missing.push([tl.t_s[Math.max(0, runStart - 1)] ?? 0, tl.t_s[i] ?? 0]);
      runStart = null;
    }
  }
  if (runStart !== null) {
    missing.push([tl.t_s[Math.max(0, runStart - 1)] ?? 0, tl.t_s[tl.t_s.length - 1] ?? 0]);
  }
  const vals = raw.filter((v): v is number => v !== null);
  let lo = vals.length ? Math.min(...vals) : 0;
  let hi = vals.length ? Math.max(...vals) : 1;
  if (ch.range) [lo, hi] = ch.range;
  if (ch.symmetric) {
    const m = Math.max(Math.abs(lo), Math.abs(hi), 0.05);
    lo = -m;
    hi = m;
  }
  if (hi - lo < 1e-9) {
    lo -= 1;
    hi += 1;
  }
  const pad = (hi - lo) * 0.1;
  return {
    ch,
    source,
    synthetic: source !== null && syntheticSignals.includes(source),
    live,
    stale,
    missing,
    staleWindows,
    empty: vals.length === 0,
    lo: ch.range ? lo : lo - pad,
    hi: ch.range ? hi : hi + pad,
  };
}

/** Where the selected box points, shared with every chart's draw hooks. */
interface Marks {
  windows: [number, number][];
  cursor: number | null;
  threshold: Map<string, number>;
}

function thresholdFor(ev: EventOut | null): Map<string, number> {
  const out = new Map<string, number>();
  if (!ev || !isAlert(ev) || ev.threshold === null) return out;
  const value = ev.unit === "K" ? ev.threshold - KELVIN_OFFSET : ev.threshold;
  for (const c of ev.citations) if (c.role === "trigger") out.set(c.signal, value);
  return out;
}

function makeChart(
  el: HTMLElement,
  p: Prepared,
  tl: Timeline,
  duration: number,
  marks: { current: Marks },
  last: boolean,
  size: { w: number; h: number },
  onHover: (t: number | null) => void,
  onPick: (t: number) => void,
): uPlot {
  const opts: uPlot.Options = {
    width: size.w,
    height: size.h,
    padding: [8, 12, last ? 0 : 6, 0],
    legend: { show: false },
    select: { show: false, left: 0, top: 0, width: 0, height: 0 },
    cursor: {
      sync: { key: SYNC_KEY },
      y: false,
      points: { show: false },
      drag: { x: false, y: false, setScale: false },
    },
    scales: {
      x: { time: false, range: [0, duration] },
      y: { range: [p.lo, p.hi] },
    },
    axes: [
      {
        show: last,
        stroke: INK,
        font: FONT,
        size: 30,
        gap: 4,
        grid: { show: false },
        ticks: { stroke: RULE, width: 1, size: 5 },
        values: (_u, vals) => vals.map((v) => clockTenths(v).replace(/\.\d$/, "")),
      },
      {
        stroke: RULE,
        font: FONT,
        size: 52,
        gap: 4,
        splits: () => niceTicks(p.lo, p.hi),
        values: (_u, vals) => vals.map((v) => fixed(v, tickDigits(p.lo, p.hi))),
        grid: { stroke: HAIR, width: 1 },
        ticks: { show: false },
      },
    ],
    series: [
      {},
      { stroke: INK, width: 1.6, spanGaps: false, points: { show: false } },
      { stroke: INK, width: 1.6, dash: [6, 5], spanGaps: false, points: { show: false } },
    ],
    hooks: {
      drawClear: [
        (u) => {
          const ctx = u.ctx;
          const { top, height } = u.bbox;
          ctx.save();
          ctx.fillStyle = YELLOW;
          for (const [a, b] of marks.current.windows) {
            const x0 = u.valToPos(a, "x", true);
            const x1 = u.valToPos(b, "x", true);
            ctx.fillRect(x0, top, Math.max(x1 - x0, 3 * devicePixelRatio), height);
          }
          ctx.restore();
        },
      ],
      draw: [
        (u) => {
          const ctx = u.ctx;
          const { top, height, left, width } = u.bbox;
          const dpr = devicePixelRatio;
          ctx.save();
          ctx.lineWidth = dpr;
          // Missing spans: a struck box rule line through the middle of the gap.
          ctx.strokeStyle = RULE;
          for (const [a, b] of p.missing) {
            const x0 = Math.max(left, u.valToPos(a, "x", true));
            const x1 = Math.min(left + width, u.valToPos(b, "x", true));
            const y = top + height / 2;
            ctx.beginPath();
            ctx.moveTo(x0, y);
            ctx.lineTo(x1, y);
            const mid = (x0 + x1) / 2;
            ctx.moveTo(mid - 5 * dpr, y + 5 * dpr);
            ctx.lineTo(mid + 5 * dpr, y - 5 * dpr);
            ctx.stroke();
          }
          const th = p.source ? marks.current.threshold.get(p.source) : undefined;
          if (th !== undefined && th >= p.lo && th <= p.hi) {
            const y = u.valToPos(th, "y", true);
            ctx.strokeStyle = RED;
            ctx.setLineDash([4 * dpr, 3 * dpr]);
            ctx.beginPath();
            ctx.moveTo(left, y);
            ctx.lineTo(left + width, y);
            ctx.stroke();
            ctx.setLineDash([]);
          }
          const cur = marks.current.cursor;
          if (cur !== null) {
            const x = u.valToPos(cur, "x", true);
            ctx.strokeStyle = INK;
            ctx.lineWidth = 2 * dpr;
            ctx.beginPath();
            ctx.moveTo(x, top);
            ctx.lineTo(x, top + height);
            ctx.stroke();
          }
          ctx.restore();
        },
      ],
      setCursor: [
        (u) => {
          const idx = u.cursor.idx;
          const xs = u.data[0];
          onHover(idx === null || idx === undefined ? null : (xs[idx] ?? null));
        },
      ],
    },
  };
  const u = new uPlot(opts, [tl.t_s, p.live, p.stale], el);
  u.over.addEventListener("click", () => {
    const left = u.cursor.left;
    if (left !== undefined && left >= 0) onPick(u.posToVal(left, "x"));
  });
  return u;
}

function Readout(props: { p: Prepared; tl: Timeline; t: number | null }) {
  const { p, tl, t } = props;
  if (p.empty) return <span class="strip-val quiet">No data</span>;
  if (t === null) return <span class="strip-val">&nbsp;</span>;
  const i = indexAt(tl.t_s, t);
  const v = tl[p.ch.key][i];
  return (
    <span class="strip-val">
      {v === null || v === undefined
        ? "No value"
        : `${fixed(p.ch.conv(v), p.ch.digits)} ${p.ch.unit}`}
    </span>
  );
}

export function Strips(props: {
  tl: Timeline;
  duration: number;
  syntheticSignals: string[];
  events: EventOut[];
  selected: EventOut | null;
  readoutT: number | null;
  /** The replay playhead; when set, the cursor line follows it instead of the selected box. */
  cursorT?: number | null;
  onHover: (t: number | null) => void;
  onPick: (t: number) => void;
}) {
  const { tl, events, selected } = props;
  const cursorT = props.cursorT ?? null;
  const prepared = useMemo(
    () => CHANNELS.map((ch) => prepare(tl, ch, props.syntheticSignals, events)),
    [tl, events, props.syntheticSignals],
  );
  const marks = useRef<Marks>({ windows: [], cursor: null, threshold: new Map() });
  const charts = useRef<uPlot[]>([]);
  const hosts = useRef<(HTMLDivElement | null)[]>([]);
  const wrap = useRef<HTMLDivElement>(null);
  const hover = useRef(props.onHover);
  const pick = useRef(props.onPick);
  hover.current = props.onHover;
  pick.current = props.onPick;
  const [fontsReady, setFontsReady] = useState(false);

  useEffect(() => {
    let live = true;
    void document.fonts.ready.then(() => {
      if (live) setFontsReady(true);
    });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    const host = wrap.current;
    if (!host || !fontsReady) return;
    const sizeOf = (el: HTMLElement): { w: number; h: number } => ({
      w: Math.max(80, Math.floor(el.clientWidth)),
      h: Math.max(48, Math.floor(el.clientHeight)),
    });
    charts.current = prepared.map((p, i) => {
      const el = hosts.current[i];
      if (!el) throw new Error("strip host missing");
      return makeChart(
        el,
        p,
        tl,
        props.duration,
        marks,
        i === prepared.length - 1,
        sizeOf(el),
        (t) => {
          hover.current(t);
        },
        (t) => {
          pick.current(t);
        },
      );
    });
    const ro = new ResizeObserver(() => {
      charts.current.forEach((u, i) => {
        const el = hosts.current[i];
        if (el) u.setSize({ width: sizeOf(el).w, height: sizeOf(el).h });
      });
    });
    hosts.current.forEach((el) => {
      if (el) ro.observe(el);
    });
    return () => {
      ro.disconnect();
      charts.current.forEach((u) => {
        u.destroy();
      });
      charts.current = [];
    };
  }, [prepared, fontsReady, tl, props.duration]);

  useEffect(() => {
    marks.current = {
      windows: selected ? triggerWindows(selected) : [],
      cursor: cursorT ?? (selected ? selected.t_start_s : null),
      threshold: thresholdFor(selected),
    };
    charts.current.forEach((u) => {
      u.redraw(false);
    });
  }, [selected, cursorT, charts.current.length]);

  return (
    <div class="strips" ref={wrap} role="group" aria-label="Channel strips on one time cursor">
      {prepared.map((p, i) => (
        <div class={i === prepared.length - 1 ? "strip strip-last" : "strip"} key={p.ch.key}>
          <div
            class="strip-lab"
            title={
              p.source ? `Source: ${p.source}${p.synthetic ? ", synthetic" : ""}` : "No source"
            }
          >
            <span class="strip-name">
              <b>{p.ch.name}</b> <span class="strip-unit">{p.ch.unit}</span>
            </span>
            <Readout p={p} tl={tl} t={props.readoutT} />
            {p.synthetic ? (
              <span class="tag">Synthetic</span>
            ) : (
              <span class="strip-src">{p.source ?? "No source"}</span>
            )}
            {p.synthetic && p.source ? (
              <span class="visually-hidden">Source {p.source}</span>
            ) : null}
          </div>
          <div
            class="strip-plot"
            aria-hidden="true"
            ref={(el) => {
              hosts.current[i] = el;
            }}
          />
        </div>
      ))}
    </div>
  );
}

import { Fragment } from "preact";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { A, Frame } from "../frame/Frame";
import { Empty, Failed, Loading } from "../frame/states";
import { data, EMBED, prefersReducedMotion, TIMELINE_POINTS } from "../shared/data";
import {
  cautionMarks,
  clockTenths,
  DATASET_CREDIT,
  hasDatasetCredit,
  durationText,
  milesText,
  severityLabel,
  sourceBadge,
} from "../shared/format";
import { useLoad } from "../shared/load";
import { drivePath, livePath, navigate, reportPath } from "../shared/router";
import { reloadKey } from "../shared/session";
import type { DriveBody, EventOut, Timeline, TrimWindows } from "../shared/types";
import { ExportOutput, SummaryOutput, useActions } from "./Actions";
import { Box, boxId } from "./Box";
import { Deck, type Rate } from "./Deck";
import { type BoxEvent, defaultBox, isAlert, roadbookBoxes, tulipAt } from "./geometry";
import { MapTrace } from "./MapTrace";
import { carStateAt } from "./stateLine";
import { Strips } from "./Strips";

interface Loaded {
  body: DriveBody;
  events: EventOut[];
  tl: Timeline;
  trim: TrimWindows;
}

async function loadReview(id: string): Promise<Loaded> {
  const [body, events, tl, trim] = await Promise.all([
    data.drive(id),
    data.events(id),
    data.timeline(id, TIMELINE_POINTS),
    data.trimWindows(id),
  ]);
  return { body, events, tl, trim };
}

const NARROW = "(max-width: 900px)";
/** Seconds of drive the replay opens before the selected box, so the box arrives on screen. */
const LEAD_S = 6;
/** The replay ticks at 10 Hz, the rate of the timeline it reads. */
const TICK_MS = 100;
/** A box counts as "now" from a little before its window to a little after it. */
const BOX_BEFORE_S = 0.5;
const BOX_AFTER_S = 2;

function useNarrow(): boolean {
  const [narrow, setNarrow] = useState(() => window.matchMedia(NARROW).matches);
  useEffect(() => {
    const mq = window.matchMedia(NARROW);
    const on = (): void => {
      setNarrow(mq.matches);
    };
    mq.addEventListener("change", on);
    return () => {
      mq.removeEventListener("change", on);
    };
  }, []);
  return narrow;
}

/** Stats shown on the review; jerk and rpm bands sit in the report. */
function headline(stat_id: string): boolean {
  return !stat_id.startsWith("behavior.rpm_band.") && !stat_id.startsWith("behavior.jerk_");
}

function isTyping(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))
  );
}

function ReviewBody(props: { id: string; box: number | null; loaded: Loaded }) {
  const { id } = props;
  const { body, events, tl, trim } = props.loaded;
  const drive = body.drive;
  const narrow = useNarrow();
  // The roadbook: every event with a box number. `?box=N` names a box number, not a position.
  const boxes = useMemo(() => roadbookBoxes(events), [events]);
  const selectedNo = props.box ?? defaultBox(boxes);
  const selected = boxes.find((e) => e.box_no === selectedNo) ?? boxes[0] ?? null;
  const [hoverT, setHoverT] = useState<number | null>(null);
  const [playT, setPlayT] = useState(() =>
    Math.max(0, (selected?.t_start_s ?? 0) - (props.box === null ? LEAD_S : 0)),
  );
  // A link that names a box opens on it, paused; a plain visit opens just before the default
  // box, already playing (never under reduced motion).
  const [playing, setPlaying] = useState(() => props.box === null && !prefersReducedMotion());
  const [rate, setRate] = useState<Rate>(1);
  const scroller = useRef<HTMLDivElement>(null);
  const focusNext = useRef(false);
  const first = useRef(true);
  const { summary, exported } = useActions(id);
  const duration = drive.duration_s ?? tl.t_s[tl.t_s.length - 1] ?? 0;
  const widest = String(Math.max(0, ...boxes.map((e) => e.box_no))).length;
  const digits = widest < 2 ? 2 : widest;
  const tulips = useMemo(() => boxes.map((e) => tulipAt(tl, e.t_start_s, e.t_end_s)), [boxes, tl]);
  const measured = boxes.filter((e) => isAlert(e) && !e.synthetic);
  const synthetic = boxes.filter((e) => isAlert(e) && e.synthetic);

  const select = (no: number, focus: boolean, seek = true): void => {
    const ev = boxes.find((e) => e.box_no === no);
    if (!ev) return;
    if (seek) setPlayT(ev.t_start_s);
    focusNext.current = focus;
    navigate(drivePath(id, no), { replace: true });
  };

  const step = (delta: number): void => {
    if (!selected) return;
    const i = boxes.indexOf(selected);
    const next = boxes[Math.max(0, Math.min(boxes.length - 1, i + delta))];
    if (next) select(next.box_no, true);
  };

  useEffect(() => {
    if (!selected) return;
    const el = document.getElementById(boxId(selected.box_no));
    if (!el) return;
    // On a phone the page itself scrolls: open at the top unless the link names a box.
    if (!(first.current && narrow && props.box === null)) {
      el.scrollIntoView({ block: first.current ? "center" : "nearest" });
    }
    if (focusNext.current) el.focus({ preventScroll: true });
    first.current = false;
    focusNext.current = false;
  }, [selected?.box_no, narrow]);

  // The playhead: 10 ticks a second, at the chosen rate, pausing at the end of the drive.
  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => {
      setPlayT((t) => {
        const next = t + (TICK_MS / 1000) * rate;
        if (next >= duration) {
          setPlaying(false);
          return duration;
        }
        return next;
      });
    }, TICK_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [playing, rate, duration]);

  // The box the playhead is in, if any. While playing, the roadbook follows it.
  const near = (e: BoxEvent): boolean =>
    playT >= e.t_start_s - BOX_BEFORE_S && playT <= e.t_end_s + BOX_AFTER_S;
  // The selected box wins while the playhead is near it; otherwise the latest box that started.
  const nowBox = selected && near(selected) ? selected : (boxes.filter(near).slice(-1)[0] ?? null);
  useEffect(() => {
    if (playing && nowBox && nowBox.box_no !== selected?.box_no)
      select(nowBox.box_no, false, false);
  }, [playing, nowBox?.box_no]);

  const toggle = (): void => {
    if (!playing && playT >= duration) setPlayT(0);
    setPlaying(!playing);
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || isTyping(e.target)) return;
      const inBook = e.target instanceof Node && scroller.current?.contains(e.target) === true;
      const onPage = e.target === document.body || e.target === document.documentElement;
      if (!inBook && !onPage) return;
      if (onPage && e.key === " ") {
        e.preventDefault();
        toggle();
      } else if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        step(1);
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        step(-1);
      } else if (inBook && e.key === "Home") {
        e.preventDefault();
        const f = boxes[0];
        if (f) select(f.box_no, true);
      } else if (inBook && e.key === "End") {
        e.preventDefault();
        const l = boxes[boxes.length - 1];
        if (l) select(l.box_no, true);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  });

  const pick = (t: number): void => {
    let best: BoxEvent | null = null;
    for (const e of boxes) {
      if (!best || Math.abs(e.t_start_s - t) < Math.abs(best.t_start_s - t)) best = e;
    }
    setPlayT(Math.max(0, Math.min(duration, t)));
    if (best) select(best.box_no, false, false);
  };

  const readoutT = hoverT ?? playT;
  const carState = useMemo(() => carStateAt(tl, readoutT), [tl, readoutT]);
  const instruments = (
    <div class="instruments" aria-label="Map and channel strips">
      <MapTrace
        tl={tl}
        boxes={boxes}
        selected={selected}
        cursorT={readoutT}
        trim={trim}
        onSelect={(n) => {
          select(n, false);
        }}
      />
      <Strips
        tl={tl}
        duration={duration}
        syntheticSignals={drive.synthetic_signals}
        events={events}
        selected={selected}
        readoutT={readoutT}
        cursorT={playT}
        onHover={setHoverT}
        onPick={pick}
      />
    </div>
  );

  const stats = body.stats.filter((s) => headline(s.stat_id));
  const busy = summary[0].state === "busy" || exported[0].state === "busy";
  const outputs = summary[0].state !== "idle" || exported[0].state !== "idle";

  return (
    <Frame
      current="review"
      fit={!narrow}
      foot={
        selected ? (
          <span>
            Box {selected.box_no} of {boxes.length} at {clockTenths(readoutT)}
          </span>
        ) : null
      }
    >
      <div class="review">
        <header class="drive-head">
          <div class="drive-title">
            <h1>{drive.label}</h1>
            <span class={`badge badge-${drive.source}`}>{sourceBadge(drive.source)}</span>
          </div>
          <p class="drive-meta">
            <span>{milesText(drive.distance_m)}</span>
            <span>{durationText(drive.duration_s)}</span>
            <span>
              {boxes.length} {boxes.length === 1 ? "box" : "boxes"}
            </span>
            <span>
              {measured.length === 0 ? (
                "No alerts"
              ) : (
                <>
                  <span
                    class="marks"
                    role="img"
                    aria-label={severityLabel(Math.max(...measured.map((e) => e.severity)))}
                  >
                    {cautionMarks(Math.max(...measured.map((e) => e.severity)))}
                  </span>{" "}
                  {measured.length} {measured.length === 1 ? "alert" : "alerts"}
                </>
              )}
            </span>
            {synthetic.length ? (
              <span>
                {synthetic.length} synthetic {synthetic.length === 1 ? "alert" : "alerts"}
              </span>
            ) : null}
            {hasDatasetCredit(drive) ? <span class="credit">{DATASET_CREDIT}</span> : null}
          </p>
          <dl class="stat-rail" aria-label="Drive stats">
            {stats.map((s) => (
              <div key={s.stat_id}>
                <dt>{s.label}</dt>
                <dd>{s.display}</dd>
              </div>
            ))}
          </dl>
          {EMBED ? null : (
            <div class="actions">
              <A href={reportPath(id)} class="btn">
                Report
              </A>
              <A href={livePath(id)} class="btn">
                Live replay
              </A>
              <button type="button" class="btn" disabled={busy} onClick={summary[1]}>
                Write summary
              </button>
              <button type="button" class="btn" disabled={busy} onClick={exported[1]}>
                Export laps
              </button>
            </div>
          )}
        </header>
        {outputs ? (
          <section class="outputs" aria-label="Summary and export">
            <SummaryOutput job={summary[0]} stats={body.stats} />
            <ExportOutput job={exported[0]} />
            <button
              type="button"
              class="btn btn-quiet"
              onClick={() => {
                summary[2]();
                exported[2]();
              }}
            >
              Close
            </button>
          </section>
        ) : null}
        <div class="review-grid">
          <section class="book" aria-label="Roadbook">
            <Deck
              duration={duration}
              t={playT}
              playing={playing}
              rate={rate}
              state={carState}
              boxes={boxes}
              box={nowBox}
              digits={digits}
              onToggle={toggle}
              onSeek={(t) => {
                setPlayT(Math.max(0, Math.min(duration, t)));
              }}
              onRate={setRate}
            />
            <div class="book-scroll" ref={scroller}>
              {boxes.length === 0 ? (
                <Empty>No boxes on this drive. Nothing crossed a rule or a threshold.</Empty>
              ) : (
                <ol
                  class="roadbook"
                  aria-label={`Roadbook, ${String(boxes.length)} boxes. Use j and k or the arrow keys to move.`}
                >
                  {boxes.map((ev, i) => (
                    <Fragment key={ev.event_id}>
                      <Box
                        ev={ev}
                        prev={boxes[i - 1]}
                        digits={digits}
                        tulip={tulips[i] ?? { kind: "none" }}
                        selected={selected?.box_no === ev.box_no}
                        onSelect={() => {
                          select(ev.box_no, true);
                        }}
                      />
                      {narrow && selected?.box_no === ev.box_no ? (
                        <li class="inline-instruments" key={`inst-${String(ev.box_no)}`}>
                          {instruments}
                        </li>
                      ) : null}
                    </Fragment>
                  ))}
                </ol>
              )}
              {narrow && boxes.length === 0 ? instruments : null}
            </div>
          </section>
          {narrow ? null : <section class="panel">{instruments}</section>}
        </div>
      </div>
    </Frame>
  );
}

export function Review(props: { id: string; box: number | null }) {
  const loaded = useLoad(
    () => loadReview(props.id),
    `review:${props.id}:${String(reloadKey.value)}`,
  );
  if (loaded.state === "ok")
    return <ReviewBody id={props.id} box={props.box} loaded={loaded.value} />;
  return (
    <Frame current="review">
      <section class="sheet">
        {loaded.state === "loading" ? <Loading>Loading the roadbook.</Loading> : null}
        {loaded.state === "error" ? (
          <>
            <Failed what="This drive did not load." error={loaded.error} />
            <p>
              <A href="/">Back to the stages</A>
            </p>
          </>
        ) : null}
      </section>
    </Frame>
  );
}

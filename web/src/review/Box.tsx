import type { Ref } from "preact";
import {
  cautionMarks,
  clock,
  fixed,
  keepUnits,
  miles,
  severityLabel,
  statValueText,
  statWord,
  windowText,
} from "../shared/format";
import type { CitationOut } from "../shared/types";
import { type BoxEvent, isAlert, isPerformance, type Tulip } from "./geometry";
import { TulipGlyph } from "./TulipGlyph";

export function CitationChip(props: { c: CitationOut }) {
  const c = props.c;
  const samples = c.n === null ? "" : `, ${String(c.n)} sample${c.n === 1 ? "" : "s"}`;
  return (
    <li class={`cite cite-${c.role}`}>
      <span class="cite-src">{c.source}</span>
      <span class="cite-sig">{c.signal}</span>
      <span class="cite-win">{windowText(c)}</span>
      <span class="cite-stat">
        {statWord(c.stat)} {statValueText(c.stat_value, c.unit)}
        {samples}
      </span>
    </li>
  );
}

function partial(ev: BoxEvent, prev: BoxEvent | undefined): string | null {
  if (ev.distance_m === null || !prev || prev.distance_m === null) return null;
  return `+${fixed(miles(Math.max(0, ev.distance_m - prev.distance_m)), 2)}`;
}

export function boxId(no: number): string {
  return `box-${String(no)}`;
}

export function Box(props: {
  ev: BoxEvent;
  prev: BoxEvent | undefined;
  digits: number;
  tulip: Tulip;
  selected: boolean;
  onSelect: () => void;
  boxRef?: Ref<HTMLLIElement>;
}) {
  const { ev } = props;
  const alert = isAlert(ev);
  const perf = isPerformance(ev);
  const classes = ["box"];
  if (props.selected) classes.push("is-selected");
  if (alert && ev.resolved) classes.push("is-resolved");
  const cites = ev.citations;
  const part = partial(ev, props.prev);
  const no = String(ev.box_no).padStart(props.digits, "0");
  return (
    <li
      id={boxId(ev.box_no)}
      ref={props.boxRef}
      class={classes.join(" ")}
      tabIndex={props.selected ? 0 : -1}
      aria-current={props.selected ? "true" : undefined}
      aria-label={`Box ${String(ev.box_no)}. ${ev.message}`}
      onClick={props.onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          props.onSelect();
        }
      }}
    >
      <div class="box-no">
        <span class="n">{no}</span>
        {alert && ev.severity > 0 ? (
          <span class="marks" role="img" aria-label={severityLabel(ev.severity)}>
            {cautionMarks(ev.severity)}
          </span>
        ) : null}
      </div>
      <div class="box-td">
        <b>{clock(ev.t_start_s)}</b>
        <span>
          {ev.distance_m === null ? "no distance" : `${fixed(miles(ev.distance_m), 2)} mi`}
        </span>
        {part ? <span class="partial">{part}</span> : null}
      </div>
      <div class="box-tu">
        <TulipGlyph tulip={props.tulip} />
      </div>
      <div class="box-note">
        <p class="note">
          {perf ? <span class="hl-g">{keepUnits(ev.message)}</span> : keepUnits(ev.message)}
          {ev.synthetic ? <span class="tag">Synthetic</span> : null}
          {alert && ev.resolved ? <span class="tag tag-quiet">Resolved</span> : null}
        </p>
        {cites.length ? (
          <ul class="cites" aria-label="Citations">
            {cites.map((c, i) => (
              <CitationChip key={`${c.signal}-${c.role}-${String(i)}`} c={c} />
            ))}
          </ul>
        ) : null}
      </div>
    </li>
  );
}

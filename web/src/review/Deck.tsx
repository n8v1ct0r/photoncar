/** The replay deck: play, pause, and scrub the drive. Every strip, the map, and the plain line
 * follow 1 playhead. */

import { clockTenths, fixed } from "../shared/format";
import type { BoxEvent } from "./geometry";
import type { CarState } from "./stateLine";

export const RATES = [1, 10] as const;
export type Rate = (typeof RATES)[number];

function pad(no: number, digits: number): string {
  return String(no).padStart(digits, "0");
}

function Reading(props: { label: string; value: string }) {
  return (
    <div class="deck-reading">
      <dt>{props.label}</dt>
      <dd>{props.value}</dd>
    </div>
  );
}

export function Deck(props: {
  duration: number;
  t: number;
  playing: boolean;
  rate: Rate;
  state: CarState;
  boxes: BoxEvent[];
  box: BoxEvent | null;
  digits: number;
  onToggle: () => void;
  onSeek: (t: number) => void;
  onRate: (r: Rate) => void;
}) {
  const { duration, t, state, box } = props;
  const span = duration > 0 ? duration : 1;
  const s = state;
  return (
    <section class="deck" aria-label="Replay controls">
      <div class="deck-controls">
        <button
          type="button"
          class="btn btn-ink deck-play"
          aria-pressed={props.playing}
          onClick={props.onToggle}
        >
          {props.playing ? "Pause" : "Play"}
        </button>
        <p class="deck-clock">
          <span class="deck-now">{clockTenths(t)}</span>
          <span class="deck-of"> of {clockTenths(duration)}</span>
        </p>
        <div class="deck-track">
          <input
            type="range"
            class="deck-scrub"
            min={0}
            max={duration}
            step={0.1}
            value={t}
            aria-label="Drive time"
            aria-valuetext={`${clockTenths(t)} of ${clockTenths(duration)}`}
            onInput={(e) => {
              props.onSeek(Number((e.target as HTMLInputElement).value));
            }}
          />
          <div class="deck-ticks" aria-hidden="true">
            {props.boxes.map((b) => (
              <span
                key={b.event_id}
                class={box?.box_no === b.box_no ? "deck-tick deck-tick-on" : "deck-tick"}
                style={{ left: `${String((b.t_start_s / span) * 100)}%` }}
              >
                {pad(b.box_no, props.digits)}
              </span>
            ))}
          </div>
        </div>
        <div class="deck-rate" role="group" aria-label="Replay speed">
          {RATES.map((r) => (
            <button
              type="button"
              key={r}
              class="btn btn-quiet"
              aria-pressed={props.rate === r}
              onClick={() => {
                props.onRate(r);
              }}
            >
              {r}x
            </button>
          ))}
        </div>
      </div>
      <div class="deck-read">
        <p class="deck-line" data-state-line>
          {box ? <span class="deck-box">Box {pad(box.box_no, props.digits)}</span> : null}
          <span>{s.line}</span>
          {box ? <span class="deck-verdict">{box.message}</span> : null}
        </p>
        <dl class="deck-readings" data-gauges>
          <Reading
            label="Speed"
            value={s.speedMph === null ? "No value" : `${fixed(s.speedMph, 0)} mph`}
          />
          <Reading
            label="Engine"
            value={s.rpm === null ? "No value" : `${Math.round(s.rpm).toLocaleString("en-US")} rpm`}
          />
          <Reading
            label="Long g"
            value={s.longG === null ? "No value" : `${fixed(s.longG, 2)} g`}
          />
          <Reading label="Side g" value={s.latG === null ? "No value" : `${fixed(s.latG, 2)} g`} />
        </dl>
      </div>
    </section>
  );
}

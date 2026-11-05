import { A, Frame } from "../frame/Frame";
import { Empty, Failed, Loading } from "../frame/states";
import { data } from "../shared/data";
import {
  cautionMarks,
  durationText,
  milesText,
  severityLabel,
  sourceBadge,
} from "../shared/format";
import { useLoad } from "../shared/load";
import { drivePath, livePath, reportPath } from "../shared/router";
import { reloadKey } from "../shared/session";
import type { DriveListItem, EventOut } from "../shared/types";
import { roadbookBoxes } from "../review/geometry";

interface AlertTally {
  count: number;
  worst: number;
}

interface Stage {
  item: DriveListItem;
  /** Roadbook boxes, counted from the events (gear shifts are events but not boxes). */
  boxes: number | null;
  measured: AlertTally | null;
  synthetic: AlertTally | null;
}

function tally(events: EventOut[], synthetic: boolean): AlertTally {
  const alerts = events.filter((e) => e.kind.startsWith("alert.") && e.synthetic === synthetic);
  return { count: alerts.length, worst: Math.max(0, ...alerts.map((e) => e.severity)) };
}

async function loadStages(): Promise<Stage[]> {
  const items = await data.drives();
  return Promise.all(
    items.map(async (item) => {
      try {
        const events = await data.events(item.drive_id);
        return {
          item,
          boxes: roadbookBoxes(events).length,
          measured: tally(events, false),
          synthetic: tally(events, true),
        };
      } catch {
        return { item, boxes: null, measured: null, synthetic: null };
      }
    }),
  );
}

function Marks(props: { t: AlertTally | null; fallback: number; kind: string }) {
  const count = props.t ? props.t.count : props.fallback;
  if (count === 0) return <span class="quiet">None</span>;
  const worst = props.t?.worst ?? 0;
  const noun = `${String(count)} ${props.kind}${count === 1 ? "" : "s"}`;
  return (
    <span>
      {worst > 0 ? (
        <span class="marks" role="img" aria-label={severityLabel(worst)}>
          {cautionMarks(worst)}
        </span>
      ) : null}{" "}
      {noun}
    </span>
  );
}

function StageRow(props: { stage: Stage }) {
  const { item } = props.stage;
  const id = item.drive_id;
  return (
    <li class="stage">
      <div class="stage-body">
        <h2 class="stage-title">
          <A href={drivePath(id)}>{item.label}</A>
        </h2>
        <span class={`badge badge-${item.source}`}>{sourceBadge(item.source)}</span>
        {!item.closed_cleanly ? <span class="badge">Not closed cleanly</span> : null}
      </div>
      <dl class="stage-facts">
        <div>
          <dt>Duration</dt>
          <dd>{durationText(item.duration_s)}</dd>
        </div>
        <div>
          <dt>Distance</dt>
          <dd>{milesText(item.distance_m)}</dd>
        </div>
        <div>
          <dt>Boxes</dt>
          <dd>{props.stage.boxes ?? <span class="quiet">Not read</span>}</dd>
        </div>
        <div>
          <dt>Alerts</dt>
          <dd>
            <Marks t={props.stage.measured} fallback={item.n_alerts} kind="alert" />
          </dd>
        </div>
        <div>
          <dt>Synthetic alerts</dt>
          <dd>
            {props.stage.synthetic ? (
              <Marks t={props.stage.synthetic} fallback={0} kind="synthetic alert" />
            ) : (
              <span class="quiet">Not read</span>
            )}
          </dd>
        </div>
      </dl>
      <p class="stage-links">
        <A href={reportPath(id)}>Report</A>
        <A href={livePath(id)}>Live replay</A>
      </p>
    </li>
  );
}

export function StageList() {
  const stages = useLoad(loadStages, `stages:${String(reloadKey.value)}`);
  return (
    <Frame current="stages">
      <section class="sheet" aria-labelledby="stages-title">
        <header class="sheet-head">
          <h1 id="stages-title">Stages</h1>
          <p class="lede">
            Every drive is a stage. Open one to read its roadbook: numbered boxes, each pinned to a
            time and a distance, each citing the signals that prove it.
          </p>
        </header>
        {stages.state === "loading" ? <Loading>Loading drives.</Loading> : null}
        {stages.state === "error" ? (
          <Failed what="The drive list did not load." error={stages.error} />
        ) : null}
        {stages.state === "ok" && stages.value.length === 0 ? (
          <Empty>
            No drives yet. Record a drive, or run photoncar seed-demo to add the public dataset
            replay.
          </Empty>
        ) : null}
        {stages.state === "ok" && stages.value.length > 0 ? (
          <ol class="stages" aria-label={`${String(stages.value.length)} drives`}>
            {stages.value.map((s) => (
              <StageRow key={s.item.drive_id} stage={s} />
            ))}
          </ol>
        ) : null}
      </section>
    </Frame>
  );
}

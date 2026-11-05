import { useState } from "preact/hooks";
import { A, Frame } from "../frame/Frame";
import { Empty, Failed, Loading } from "../frame/states";
import { data } from "../shared/data";
import {
  clock,
  DATASET_CREDIT,
  fixed,
  hasDatasetCredit,
  M_PER_FOOT,
  sourceBadge,
} from "../shared/format";
import { useLoad } from "../shared/load";
import { drivePath, livePath } from "../shared/router";
import { reloadKey } from "../shared/session";
import type { DriveBody, DriveStat, Report } from "../shared/types";

const SHIFTS_SHOWN = 12;

function statDisplay(
  stats: Map<string, DriveStat>,
  id: string,
  value: number | null,
  unit: string,
): string {
  const s = stats.get(id);
  if (s) return s.display;
  return value === null ? "Not measured" : `${fixed(value, 2)} ${unit}`;
}

function bandId(lo: number, hi: number): string {
  return `behavior.rpm_band.${String(lo)}_${String(hi)}`;
}

function Performance(props: { r: Report }) {
  const p = props.r.performance;
  const none =
    p.zero_to_sixty.length === 0 && p.sixty_to_zero.length === 0 && p.peak_lateral_g === null;
  return (
    <section class="report-part" aria-labelledby="perf-title">
      <h2 id="perf-title">Performance</h2>
      {none ? <Empty>No performance runs on this drive.</Empty> : null}
      <ul class="runs">
        {p.zero_to_sixty.map((run, i) => (
          <li class="run" key={`z${String(i)}`}>
            <span class="run-name">0 to 60 mph</span>
            <span class="run-value">
              <span class="hl-g">
                {run.seconds === null ? "Not measured" : `${fixed(run.seconds, 2)} s`}
              </span>
            </span>
            <span class="run-note">
              from standstill
              {run.rollout_seconds === null
                ? ""
                : `, ${fixed(run.rollout_seconds, 2)} s with 1 ft rollout`}
            </span>
            <span class="run-at">at {clock(run.t_start_s)}</span>
          </li>
        ))}
        {p.sixty_to_zero.map((run, i) => (
          <li class="run" key={`s${String(i)}`}>
            <span class="run-name">60 to 0 mph</span>
            <span class="run-value">
              <span class="hl-g">
                {run.meters === null ? "Not measured" : `${fixed(run.meters, 1)} m`}
              </span>
            </span>
            <span class="run-note">
              {run.meters === null ? "" : `${fixed(run.meters / M_PER_FOOT, 0)} ft of braking`}
            </span>
            <span class="run-at">at {clock(run.t_start_s)}</span>
          </li>
        ))}
        {p.peak_lateral_g !== null ? (
          <li class="run">
            <span class="run-name">Peak lateral</span>
            <span class="run-value">
              <span class="hl-g">{fixed(p.peak_lateral_g, 2)} g</span>
            </span>
            <span class="run-note">the highest on this drive</span>
            <span class="run-at" />
          </li>
        ) : null}
      </ul>
    </section>
  );
}

function Behavior(props: { r: Report; stats: Map<string, DriveStat> }) {
  const b = props.r.behavior;
  const [allShifts, setAllShifts] = useState(false);
  const maxBand = Math.max(0, ...b.rpm_bands.map((x) => x.seconds ?? 0));
  const shifts = allShifts ? b.shifts : b.shifts.slice(0, SHIFTS_SHOWN);
  return (
    <section class="report-part" aria-labelledby="behavior-title">
      <h2 id="behavior-title">Driver behavior</h2>
      <dl class="counts">
        <div>
          <dt>Hard brakes</dt>
          <dd>{b.hard_brakes}</dd>
        </div>
        <div>
          <dt>Hard accelerations</dt>
          <dd>{b.hard_accels}</dd>
        </div>
        <div>
          <dt>Cornering events</dt>
          <dd>{b.corners}</dd>
        </div>
      </dl>
      <h3>Jerk</h3>
      <table class="ruled">
        <thead>
          <tr>
            <th scope="col">Axis</th>
            <th scope="col" class="num">
              Mean
            </th>
            <th scope="col" class="num">
              95th percentile
            </th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">Longitudinal</th>
            <td class="num">
              {statDisplay(props.stats, "behavior.jerk_long_mean", b.jerk_long_mean, "m/s³")}
            </td>
            <td class="num">
              {statDisplay(props.stats, "behavior.jerk_long_p95", b.jerk_long_p95, "m/s³")}
            </td>
          </tr>
          <tr>
            <th scope="row">Lateral</th>
            <td class="num">
              {statDisplay(props.stats, "behavior.jerk_lat_mean", b.jerk_lat_mean, "m/s³")}
            </td>
            <td class="num">
              {statDisplay(props.stats, "behavior.jerk_lat_p95", b.jerk_lat_p95, "m/s³")}
            </td>
          </tr>
        </tbody>
      </table>
      <h3>Time in rpm bands</h3>
      {b.rpm_bands.length === 0 ? (
        <Empty>No rpm on this drive, so no bands.</Empty>
      ) : (
        <table class="ruled bands">
          <thead>
            <tr>
              <th scope="col">Band</th>
              <th scope="col" class="num">
                Time
              </th>
              <th scope="col" class="bar-col">
                <span class="visually-hidden">Share of the longest band</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {b.rpm_bands.map((band) => {
              const share = maxBand > 0 ? (band.seconds ?? 0) / maxBand : 0;
              return (
                <tr key={`${String(band.lo)}-${String(band.hi)}`}>
                  <th scope="row">
                    {fixed(band.lo, 0)} to {fixed(band.hi, 0)} rpm
                  </th>
                  <td class="num">
                    {statDisplay(props.stats, bandId(band.lo, band.hi), band.seconds, "s")}
                  </td>
                  <td class="bar-col" aria-hidden="true">
                    <span class="bar" style={{ width: `${(share * 100).toFixed(1)}%` }} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <h3>Shifts</h3>
      {b.shifts.length === 0 ? (
        <Empty>No shifts found on this drive.</Empty>
      ) : (
        <>
          <table class="ruled shifts">
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col" class="num">
                  Rpm
                </th>
                <th scope="col">Gears</th>
              </tr>
            </thead>
            <tbody>
              {shifts.map((s, i) => (
                <tr key={String(i)}>
                  <td>{clock(s.t_s)}</td>
                  <td class="num">{s.rpm === null ? "No rpm" : fixed(s.rpm, 0)}</td>
                  <td>
                    {s.from_gear === null || s.to_gear === null
                      ? "Gears not known"
                      : `${String(s.from_gear)} to ${String(s.to_gear)}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {b.shifts.length > SHIFTS_SHOWN ? (
            <button
              type="button"
              class="btn btn-quiet"
              aria-expanded={allShifts}
              onClick={() => {
                setAllShifts(!allShifts);
              }}
            >
              {allShifts
                ? `Show the first ${String(SHIFTS_SHOWN)}`
                : `Show all ${String(b.shifts.length)} shifts`}
            </button>
          ) : null}
        </>
      )}
    </section>
  );
}

function Laps(props: { r: Report }) {
  const laps = props.r.laps;
  return (
    <section class="report-part" aria-labelledby="laps-title">
      <h2 id="laps-title">Laps</h2>
      {laps.length === 0 ? (
        <Empty>No laps on this drive. Laps need a start and finish line crossed twice.</Empty>
      ) : (
        <table class="ruled laps">
          <thead>
            <tr>
              <th scope="col">Lap</th>
              <th scope="col">Line</th>
              <th scope="col">Start</th>
              <th scope="col" class="num">
                Time
              </th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {laps.map((lap) => (
              <tr key={String(lap.lap_no)}>
                <th scope="row">{lap.lap_no}</th>
                <td>{lap.line_id}</td>
                <td>{clock(lap.t_start_s)}</td>
                <td class="num">
                  {lap.valid ? (
                    <span class="hl-g">{fixed(lap.duration_s, 3)} s</span>
                  ) : (
                    `${fixed(lap.duration_s, 3)} s`
                  )}
                </td>
                <td>{lap.valid ? "Valid" : "Not valid"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

async function load(id: string): Promise<{ body: DriveBody; report: Report }> {
  const [body, report] = await Promise.all([data.drive(id), data.report(id)]);
  return { body, report };
}

export function ReportPage(props: { id: string }) {
  const loaded = useLoad(() => load(props.id), `report:${props.id}:${String(reloadKey.value)}`);
  return (
    <Frame current="report">
      <article class="sheet report">
        {loaded.state === "loading" ? <Loading>Loading the report.</Loading> : null}
        {loaded.state === "error" ? (
          <Failed what="This report did not load." error={loaded.error} />
        ) : null}
        {loaded.state === "ok" ? (
          <>
            <header class="sheet-head">
              <h1>{loaded.value.body.drive.label}</h1>
              <p class="drive-meta">
                <span class={`badge badge-${loaded.value.body.drive.source}`}>
                  {sourceBadge(loaded.value.body.drive.source)}
                </span>
                {hasDatasetCredit(loaded.value.body.drive) ? (
                  <span class="credit">{DATASET_CREDIT}</span>
                ) : null}
                <A href={drivePath(props.id)}>Roadbook</A>
                <A href={livePath(props.id)}>Live replay</A>
              </p>
            </header>
            <Performance r={loaded.value.report} />
            <Behavior
              r={loaded.value.report}
              stats={new Map(loaded.value.body.stats.map((s) => [s.stat_id, s]))}
            />
            <Laps r={loaded.value.report} />
          </>
        ) : null}
      </article>
    </Frame>
  );
}

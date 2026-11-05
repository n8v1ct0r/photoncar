import { useState } from "preact/hooks";
import { data } from "../shared/data";
import { keepUnits } from "../shared/format";
import { asApiError } from "../shared/load";
import type { DriveStat, ExportOut, SummaryOut } from "../shared/types";

export type Job<T> =
  | { state: "idle" }
  | { state: "busy" }
  | { state: "ok"; value: T }
  | { state: "error"; message: string };

export function useJob<T>(run: () => Promise<T>): [Job<T>, () => void, () => void] {
  const [job, setJob] = useState<Job<T>>({ state: "idle" });
  const start = (): void => {
    setJob({ state: "busy" });
    run().then(
      (value) => {
        setJob({ state: "ok", value });
      },
      (e: unknown) => {
        const err = asApiError(e);
        const wait =
          err.status === 429 && err.retryAfterS !== null
            ? ` Try again in ${String(err.retryAfterS)} s.`
            : "";
        setJob({ state: "error", message: `${err.message}${wait}` });
      },
    );
  };
  return [
    job,
    start,
    () => {
      setJob({ state: "idle" });
    },
  ];
}

export function SummaryOutput(props: { job: Job<SummaryOut>; stats: DriveStat[] }) {
  const { job } = props;
  if (job.state === "idle") return null;
  if (job.state === "busy") {
    return (
      <p class="state state-loading" role="status">
        Writing the drive summary from the computed stats.
      </p>
    );
  }
  if (job.state === "error") {
    return (
      <p class="state state-error" role="alert">
        The drive summary did not run. {job.message}
      </p>
    );
  }
  const byId = new Map(props.stats.map((s) => [s.stat_id, s]));
  const out = job.value;
  const how =
    out.source === "model"
      ? `Written by ${out.model ?? "the model"} from the computed stats only.`
      : "Template summary. The model did not run, so these sentences come from a fixed template.";
  return (
    <div class="summary">
      <ol class="summary-list">
        {out.sentences.map((s, i) => (
          <li key={String(i)}>
            <p>{keepUnits(s.text)}</p>
            <ul class="chips" aria-label="Cited stats">
              {s.stat_ids.map((sid) => {
                const st = byId.get(sid);
                return (
                  <li key={sid} class="chip">
                    {st ? (
                      <>
                        {st.label} <b>{st.display}</b>
                      </>
                    ) : (
                      <>Stat {sid} is not on this drive</>
                    )}
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
      </ol>
      <p class="quiet">
        {how}
        {out.cached ? " Read from the cache." : ""}
      </p>
    </div>
  );
}

export function ExportOutput(props: { job: Job<ExportOut> }) {
  const { job } = props;
  if (job.state === "idle") return null;
  if (job.state === "busy") {
    return (
      <p class="state state-loading" role="status">
        Exporting laps in the lap v0 format.
      </p>
    );
  }
  if (job.state === "error") {
    return (
      <p class="state state-error" role="alert">
        The lap export did not run. {job.message}
      </p>
    );
  }
  const files = job.value.files;
  if (files.length === 0) {
    return <p class="state state-empty">No laps to export on this drive.</p>;
  }
  return (
    <div class="exported">
      <p>
        Wrote {files.length} lap {files.length === 1 ? "file" : "files"} under the device data
        folder. The privacy trim ran first.
      </p>
      <ul class="files">
        {files.map((f) => (
          <li key={f}>
            <code>{f}</code>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function useActions(id: string) {
  const summary = useJob(() => data.summary(id));
  const exported = useJob(() => data.exportLaps(id));
  return { summary, exported };
}

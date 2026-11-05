import { useEffect, useState } from "preact/hooks";
import { LiveFrame as LiveShell } from "../frame/Frame";
import { type ApiError, data, EMBED, sleep } from "../shared/data";
import { cautionMarks, keepUnits, severityLabel } from "../shared/format";
import { asApiError } from "../shared/load";
import { drivePath } from "../shared/router";
import { deviceMode, moving, reloadKey, soloDrive } from "../shared/session";
import type { LiveFrame } from "../shared/types";

/** Reconnect backoff for the device's live feed: the first retry waits 1 s, each failed try
 * doubles the wait up to 8 s (1, 2, 4, 8, 8 s), and a frame that arrives resets it to 1 s. */
export const RETRY_FIRST_S = 1;
export const RETRY_MAX_S = 8;

type Feed =
  | { state: "waiting" }
  | { state: "frame"; frame: LiveFrame; ended: boolean }
  /** The device's live feed dropped; the next try starts in `retryS` seconds (0: now). */
  | { state: "lost"; retryS: number }
  | { state: "error"; message: string };

/** A dropped connection or a device fault can clear on its own; a refused token or a missing
 * drive cannot, so those stop with their message. */
function retryable(err: ApiError): boolean {
  return err.status === 0 || err.status >= 500;
}

/** A fixed bank of 7-segment digits: unlit segments sit behind, blanks pad on the left, so a
 * value never moves the layout. */
function Bank(props: {
  value: number | null;
  digits: number;
  unit: string;
  spoken: string;
  synthetic: boolean;
}) {
  const { value, digits } = props;
  const text =
    value === null
      ? "-".repeat(digits)
      : String(Math.round(value)).slice(-digits).padStart(digits, "!");
  const said =
    value === null ? `${props.spoken}: no value` : `${props.spoken}: ${String(Math.round(value))}`;
  return (
    <div
      class="bank"
      role="group"
      aria-label={`${said} ${props.unit}${props.synthetic ? ", synthetic" : ""}`}
    >
      <span class="seg" aria-hidden="true">
        <span class="seg-ghost">{"8".repeat(digits)}</span>
        <span class="seg-lit">{text}</span>
      </span>
      <span class="unit" aria-hidden="true">
        {props.unit}
      </span>
      {props.synthetic ? (
        <span class="tag tag-live" aria-hidden="true">
          Synthetic
        </span>
      ) : null}
    </div>
  );
}

function AlertZone(props: { feed: Feed; idle: boolean }) {
  const { feed } = props;
  if (feed.state === "error") return <p class="live-line">{feed.message}</p>;
  if (feed.state === "lost") {
    const when =
      feed.retryS > 0 ? `Trying again in ${String(feed.retryS)} s.` : "Trying again now.";
    return <p class="live-line">{keepUnits(`Live feed lost. ${when}`)}</p>;
  }
  if (props.idle) return <p class="live-line">Nothing is recording.</p>;
  if (feed.state === "waiting") return <p class="live-line">Waiting for live frames.</p>;
  const alert = feed.frame.alert;
  if (!alert) {
    return <p class="live-line">{feed.ended ? "Replay ended. No alert." : "No alert."}</p>;
  }
  return (
    <>
      <p class="live-head">
        {alert.severity > 0 ? (
          <span class="marks" role="img" aria-label={severityLabel(alert.severity)}>
            {cautionMarks(alert.severity)}
          </span>
        ) : null}{" "}
        {keepUnits(alert.headline)}
      </p>
      <p class="live-cite">{keepUnits(alert.citation)}</p>
    </>
  );
}

export function Live(props: { replay: string | null; t: number | null }) {
  const [feed, setFeed] = useState<Feed>({ state: "waiting" });
  const replay = moving.value ? null : props.replay;
  const freeze = replay !== null && props.t !== null ? props.t : null;

  useEffect(() => {
    const ctl = new AbortController();
    const signal = ctl.signal;
    setFeed({ state: "waiting" });
    const fail = (e: unknown): void => {
      if (signal.aborted) return;
      const err = asApiError(e);
      const message =
        err.status === 401
          ? "Needs the device token. Enter it on a parked view."
          : err.status === 423
            ? "Parked views open when the car stops."
            : err.message;
      setFeed({ state: "error", message });
    };
    if (replay !== null && freeze !== null) {
      data.liveFrame(replay, freeze).then((frame) => {
        if (!signal.aborted) setFeed({ state: "frame", frame, ended: false });
      }, fail);
    } else {
      void (async () => {
        let wait = RETRY_FIRST_S;
        for (;;) {
          let last: LiveFrame | null = null;
          try {
            for await (const msg of data.liveFrames(replay, signal)) {
              if (signal.aborted) return;
              if (msg.type === "state") {
                if (!EMBED) moving.value = msg.moving;
                // Connected again: the lost line goes until the feed drops once more.
                if (last === null) setFeed({ state: "waiting" });
                continue;
              }
              last = msg.frame;
              wait = RETRY_FIRST_S;
              setFeed({ state: "frame", frame: msg.frame, ended: false });
            }
            if (signal.aborted) return;
            if (replay !== null) {
              setFeed(
                last
                  ? { state: "frame", frame: last, ended: true }
                  : { state: "error", message: "This replay ended with no frames." },
              );
              return;
            }
            // The embed's still frame (reduced motion) ends its feed on purpose.
            if (EMBED) return;
          } catch (e) {
            if (signal.aborted) return;
            if (replay !== null || EMBED || !retryable(asApiError(e))) {
              fail(e);
              return;
            }
          }
          // The device's live feed dropped: dashes and 1 plain line until it is back.
          for (let s = wait; s > 0; s--) {
            setFeed({ state: "lost", retryS: s });
            if (!(await sleep(1000, signal))) return;
          }
          setFeed({ state: "lost", retryS: 0 });
          wait = Math.min(wait * 2, RETRY_MAX_S);
        }
      })();
    }
    return () => {
      ctl.abort();
    };
  }, [replay, freeze, reloadKey.value]);

  // Nothing records while the device is idle, so a frame of an older feed is not shown.
  const idle = replay === null && !EMBED && deviceMode.value === "idle";
  const frame = feed.state === "frame" && !idle ? feed.frame : null;
  const synth = new Set(frame?.synthetic_fields ?? []);
  const back = replay
    ? drivePath(replay)
    : EMBED && soloDrive.value
      ? drivePath(soloDrive.value)
      : "/";
  return (
    <LiveShell leave={back}>
      <h1 class="visually-hidden">Live tripmeter</h1>
      <div class="banks">
        <Bank
          value={frame?.speed_mph ?? null}
          digits={3}
          unit="mph"
          spoken="Speed"
          synthetic={synth.has("speed_mph")}
        />
        <Bank
          value={frame?.rpm ?? null}
          digits={4}
          unit="rpm"
          spoken="Engine speed"
          synthetic={synth.has("rpm")}
        />
        <Bank
          value={frame?.coolant_c ?? null}
          digits={3}
          unit="°C"
          spoken="Coolant"
          synthetic={synth.has("coolant_c")}
        />
      </div>
      <div class="live-rule" aria-hidden="true" />
      <section class="live-alert" aria-label="Alert">
        <AlertZone feed={feed} idle={idle} />
      </section>
    </LiveShell>
  );
}

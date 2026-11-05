import { Frame } from "../frame/Frame";
import { Empty, Failed, Loading } from "../frame/states";
import { data, EMBED } from "../shared/data";
import {
  cautionMarks,
  DATASET_CREDIT,
  fixed,
  keepUnits,
  KM_PER_MILE,
  severityLabel,
} from "../shared/format";
import { useLoad } from "../shared/load";
import { reloadKey, soloCredit } from "../shared/session";

function LineSample(props: { form: "live" | "stale" | "missing" | "trimmed" }) {
  return (
    <svg class="line-sample" viewBox="0 0 72 16" aria-hidden="true" focusable="false">
      <path d="M2 8H70" class={`ls ls-${props.form}`} />
      {props.form === "missing" ? <path d="M31 13l10 -10" class="ls ls-missing" /> : null}
    </svg>
  );
}

const FORMS: { form: "live" | "stale" | "missing" | "trimmed"; name: string; meaning: string }[] = [
  { form: "live", name: "Solid", meaning: "Live. The signal updated on time." },
  {
    form: "stale",
    name: "Dashed",
    meaning: "Stale. The signal stopped updating, or the position was dead reckoned without GPS.",
  },
  { form: "missing", name: "Struck", meaning: "Missing. No value was recorded here." },
  {
    form: "trimmed",
    name: "Dotted",
    meaning: "Trimmed. An export removed this stretch inside the privacy zone.",
  },
];

export function LegendPage() {
  const legend = useLoad(() => data.legend(), `legend:${String(reloadKey.value)}`);
  return (
    <Frame current="legend">
      <article class="sheet legend">
        <header class="sheet-head">
          <h1>Legend</h1>
          <p class="lede">
            The symbols of the roadbook and the thresholds behind every alert. The thresholds come
            from the device configuration.
          </p>
          {EMBED && soloCredit.value ? <p class="credit">{DATASET_CREDIT}</p> : null}
        </header>

        <section class="report-part" aria-labelledby="marks-title">
          <h2 id="marks-title">Caution marks</h2>
          <table class="ruled">
            <tbody>
              {[1, 2, 3].map((n) => (
                <tr key={String(n)}>
                  <th scope="row" class="sym">
                    <span class="marks" role="img" aria-label={severityLabel(n)}>
                      {cautionMarks(n)}
                    </span>
                  </th>
                  <td>Severity {n} of 3.</td>
                </tr>
              ))}
              <tr>
                <th scope="row" class="sym">
                  <span class="resolved-sample">!!</span>
                </th>
                <td>Printed gray: the alert cleared during the drive. It stays in the roadbook.</td>
              </tr>
            </tbody>
          </table>
        </section>

        <section class="report-part" aria-labelledby="hl-title">
          <h2 id="hl-title">Highlighters</h2>
          <table class="ruled">
            <tbody>
              <tr>
                <th scope="row" class="sym">
                  <span class="swatch swatch-yellow" aria-hidden="true" />
                </th>
                <td>
                  Marker yellow: the selected box, and its cited window on every strip and on the
                  map.
                </td>
              </tr>
              <tr>
                <th scope="row" class="sym">
                  <span class="swatch swatch-green" aria-hidden="true" />
                </th>
                <td>Marker green: a performance run or a valid lap.</td>
              </tr>
              <tr>
                <th scope="row" class="sym">
                  <span class="tag">Synthetic</span>
                </th>
                <td>
                  Made by the synthetic generator, not measured on a car. A synthetic alert never
                  counts as a measured one.
                </td>
              </tr>
            </tbody>
          </table>
        </section>

        <section class="report-part" aria-labelledby="lines-title">
          <h2 id="lines-title">Line forms</h2>
          <table class="ruled">
            <tbody>
              {FORMS.map((f) => (
                <tr key={f.form}>
                  <th scope="row" class="sym">
                    <LineSample form={f.form} />
                  </th>
                  <td>
                    <b>{f.name}.</b> {f.meaning}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section class="report-part" aria-labelledby="rules-title">
          <h2 id="rules-title">Thresholds</h2>
          {legend.state === "loading" ? <Loading>Loading the thresholds.</Loading> : null}
          {legend.state === "error" ? (
            <Failed what="The thresholds did not load." error={legend.error} />
          ) : null}
          {legend.state === "ok" && legend.value.rules.length === 0 ? (
            <Empty>No alert rules are configured.</Empty>
          ) : null}
          {legend.state === "ok" && legend.value.rules.length > 0 ? (
            <>
              <table class="ruled rules">
                <thead>
                  <tr>
                    <th scope="col">Rule</th>
                    <th scope="col">Condition</th>
                    <th scope="col">Marks</th>
                  </tr>
                </thead>
                <tbody>
                  {legend.value.rules.map((r) => (
                    <tr key={r.rule_id}>
                      <th scope="row">
                        {r.title}
                        {r.advisory ? <span class="tag tag-quiet">Advisory</span> : null}
                        <span class="rule-id">{r.rule_id}</span>
                      </th>
                      <td>{keepUnits(r.condition)}</td>
                      <td>
                        {r.severity > 0 ? (
                          <span class="marks" role="img" aria-label={severityLabel(r.severity)}>
                            {cautionMarks(r.severity)}
                          </span>
                        ) : (
                          <span class="quiet">None</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <dl class="facts">
                <div>
                  <dt>Privacy zone radius</dt>
                  <dd>
                    {keepUnits(
                      `${fixed(legend.value.privacy_radius_km / KM_PER_MILE, 2)} mi around home ` +
                        `(${fixed(legend.value.privacy_radius_km, 1)} km in the configuration)`,
                    )}
                  </dd>
                </div>
                <div>
                  <dt>OBD request cap</dt>
                  <dd>{fixed(legend.value.max_request_hz, 0)}&nbsp;requests per second</dd>
                </div>
              </dl>
              <p class="note-line">{legend.value.synthetic_note}</p>
            </>
          ) : null}
        </section>
      </article>
    </Frame>
  );
}

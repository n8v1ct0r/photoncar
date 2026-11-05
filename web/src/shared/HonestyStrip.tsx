/** The shared honesty strip of every portfolio prototype (the portfolio's shared/STRIP.md).
 * Embed only: the device app never shows it. It names the data honestly: the drive is a recorded
 * public-dataset replay, and the OBD channels the dataset lacks are simulated. */

export const STRIP_LABEL = "RECORDED";
export const STRIP_SOURCE = "comma2k19 drive";
export const STRIP_MIXED =
  "Speed, rpm, g, and position are recorded (comma2k19, MIT). Throttle and coolant are simulated: the dataset has no OBD-II data.";

export function HonestyStrip() {
  return (
    <div data-honesty-strip class="honesty-strip">
      <a class="hs-home" href="https://nate-victor.com">
        Nate Victor / Sidequest 04
      </a>
      <span class="hs-name">
        <span aria-hidden="true">· </span>Photoncar
      </span>
      <span class="hs-label" title={STRIP_MIXED}>
        <span class="hs-dot hs-dot-rec" aria-hidden="true" />
        {STRIP_LABEL}
        <span class="hs-wide"> · {STRIP_SOURCE}</span>
        <span class="hs-sim">
          <span class="hs-dot hs-dot-sim" aria-hidden="true" />
          SIMULATED<span class="hs-wide"> · throttle, coolant</span>
        </span>
      </span>
    </div>
  );
}

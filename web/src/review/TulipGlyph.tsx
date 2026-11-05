import type { Tulip } from "./geometry";

const STRAIGHT_DEG = 10;
const U_TURN_DEG = 125;

export function tulipLabel(t: Tulip): string {
  if (t.kind === "none") return "No position here";
  if (t.kind === "still") return "Stopped";
  const deg = Math.round(Math.abs(t.degrees));
  if (deg < STRAIGHT_DEG) return "Straight on";
  return `${t.degrees > 0 ? "Left" : "Right"} ${String(deg)}°`;
}

/** A roadbook tulip: entry dot, stem to the junction, and the exit arrow turned by the heading
 * change. Missing positions draw a struck stem; a stop draws a crossbar. */
export function TulipGlyph(props: { tulip: Tulip }) {
  const t = props.tulip;
  const label = tulipLabel(t);
  let body;
  if (t.kind === "none") {
    body = (
      <>
        <path d="M12 21V4" class="tl-missing" />
        <path d="M7 15l10-6" class="tl-strike" />
      </>
    );
  } else if (t.kind === "still") {
    body = (
      <>
        <circle cx="12" cy="19.5" r="2.6" class="tl-dot" />
        <path d="M12 19.5V7" class="tl-line" />
        <path d="M6.5 6h11" class="tl-line" />
      </>
    );
  } else {
    const d = Math.abs(t.degrees) < STRAIGHT_DEG ? 0 : t.degrees;
    if (Math.abs(d) > U_TURN_DEG) {
      // A turn back: the exit loops over the junction and comes down beside the stem.
      const side = d > 0 ? -1 : 1;
      const x = 12 + side * 7;
      body = (
        <>
          <circle cx="12" cy="19.5" r="2.6" class="tl-dot" />
          <path
            d={`M12 19.5V10Q12 4 ${String(12 + side * 3.5)} 4Q${String(x)} 4 ${String(x)} 10V19`}
            class="tl-line"
          />
          <path
            d={`M${String(x - 3.2)} 15.8L${String(x)} 19.2L${String(x + 3.2)} 15.8`}
            class="tl-line"
          />
        </>
      );
    } else {
      const rad = (d * Math.PI) / 180;
      const dx = -Math.sin(rad);
      const dy = -Math.cos(rad);
      const jy = 12;
      const ex = 12 + 8.5 * dx;
      const ey = jy + 8.5 * dy;
      const head = (sign: number): string => {
        const back = Math.atan2(-dy, -dx) + sign * (Math.PI / 5);
        return `M${ex.toFixed(2)} ${ey.toFixed(2)}l${(3.8 * Math.cos(back)).toFixed(2)} ${(3.8 * Math.sin(back)).toFixed(2)}`;
      };
      body = (
        <>
          <circle cx="12" cy="19.5" r="2.6" class="tl-dot" />
          <path d={`M12 19.5V${String(jy)}L${ex.toFixed(2)} ${ey.toFixed(2)}`} class="tl-line" />
          <path d={`${head(1)}${head(-1)}`} class="tl-line" />
        </>
      );
    }
  }
  return (
    <svg class="tulip" viewBox="0 0 24 24" role="img" aria-label={label}>
      <title>{label}</title>
      {body}
    </svg>
  );
}

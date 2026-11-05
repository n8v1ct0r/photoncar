import type { AnchorHTMLAttributes, ComponentChildren } from "preact";
import { EMBED } from "../shared/data";
import { DATASET_CREDIT, SAFETY_LINE } from "../shared/format";
import { drivePath, followLink, hrefFor, reportPath } from "../shared/router";
import { soloCredit, soloDrive } from "../shared/session";

export function A(
  props: { href: string; children: ComponentChildren } & Omit<AnchorHTMLAttributes, "href">,
) {
  const { href, children, ...rest } = props;
  return (
    <a
      {...rest}
      href={hrefFor(href)}
      onClick={(e) => {
        if (!EMBED) followLink(e, href);
      }}
    >
      {children}
    </a>
  );
}

/** The brand mark: a roadbook tulip (entry dot, stem, exit arrow). */
export function TulipMark() {
  return (
    <svg class="mark" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="19.5" r="2.6" fill="currentColor" />
      <path d="M12 19.5V11l6-5.5" fill="none" stroke="currentColor" stroke-width="2.4" />
      <path d="M13.2 5.2h5.4v5.4" fill="none" stroke="currentColor" stroke-width="2.4" />
    </svg>
  );
}

export type NavKey = "stages" | "review" | "report" | "live" | "legend" | null;

function NavLink(props: { href: string; here: boolean; children: ComponentChildren }) {
  return (
    <A href={props.href} class="nav-link" aria-current={props.here ? "page" : undefined}>
      {props.children}
    </A>
  );
}

function Nav(props: { current: NavKey }) {
  const solo = EMBED ? soloDrive.value : null;
  return (
    <nav class="nav" aria-label="Main">
      {solo ? (
        <>
          <NavLink href={drivePath(solo)} here={props.current === "review"}>
            Review
          </NavLink>
          <NavLink href={reportPath(solo)} here={props.current === "report"}>
            Report
          </NavLink>
        </>
      ) : (
        <NavLink href="/" here={props.current === "stages"}>
          Stages
        </NavLink>
      )}
      <NavLink href="/live" here={props.current === "live"}>
        Live
      </NavLink>
      <NavLink href="/legend" here={props.current === "legend"}>
        Legend
      </NavLink>
    </nav>
  );
}

/** The shared frame of every route and state: masthead, content, and the safety line printed
 * as the footer rule. `fit` pins the frame to the viewport (the review at desktop sizes). */
export function Frame(props: {
  current: NavKey;
  fit?: boolean;
  children: ComponentChildren;
  foot?: ComponentChildren;
}) {
  const home = EMBED && soloDrive.value ? drivePath(soloDrive.value) : "/";
  return (
    <div class={props.fit ? "frame frame-fit" : "frame"}>
      <a class="skip" href="#main">
        Skip to content
      </a>
      <header class="masthead">
        <A href={home} class="brand">
          <TulipMark />
          <span>Photoncar</span>
        </A>
        <Nav current={props.current} />
      </header>
      <main id="main" class="main" tabIndex={-1}>
        {props.children}
      </main>
      <footer class="safety">
        <p class="safety-line">{SAFETY_LINE}</p>
        {props.foot ? <div class="safety-extra">{props.foot}</div> : null}
      </footer>
    </div>
  );
}

/** The live frame: no masthead and no controls; the safety line at glance size. On the device
 * the way out shows only on keyboard focus. The portfolio embed never runs in a car, so it adds
 * a bar above the tripmeter, outside its glance-size type: a visible link back to the roadbook
 * and the dataset credit. */
export function LiveFrame(props: { children: ComponentChildren; leave: string }) {
  const tripmeter = (
    <div class="frame frame-live">
      {EMBED ? null : (
        <A href={props.leave} class="leave">
          Leave the live view
        </A>
      )}
      <main id="main" class="live-main">
        {props.children}
      </main>
      <footer class="safety safety-live">
        <p class="safety-line">{SAFETY_LINE}</p>
      </footer>
    </div>
  );
  if (!EMBED) return tripmeter;
  return (
    <div class="live-embed">
      <nav class="embed-bar" aria-label="Replay">
        <A href={props.leave} class="embed-back">
          <TulipMark />
          <span>Roadbook</span>
        </A>
        {soloCredit.value ? <span class="credit">{DATASET_CREDIT}</span> : null}
      </nav>
      {tripmeter}
    </div>
  );
}

import { render } from "preact";
import { App } from "./app";
import { EMBED } from "./shared/data";
import { HonestyStrip } from "./shared/HonestyStrip";
import "./styles.css";

const root = document.getElementById("app");
if (root) {
  if (EMBED) document.documentElement.classList.add("has-strip");
  render(
    EMBED ? (
      <>
        <HonestyStrip />
        <App />
      </>
    ) : (
      <App />
    ),
    root,
  );
}

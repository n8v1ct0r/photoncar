import { useEffect } from "preact/hooks";
import { A, Frame } from "./frame/Frame";
import { Loading } from "./frame/states";
import { TokenGate } from "./frame/TokenGate";
import { LegendPage } from "./legend/LegendPage";
import { Live } from "./live/Live";
import { ReportPage } from "./report/ReportPage";
import { Review } from "./review/Review";
import { EMBED } from "./shared/data";
import { drivePath, navigate, place, routeOf } from "./shared/router";
import {
  findSoloDrive,
  moving,
  needsToken,
  soloChecked,
  soloDrive,
  startHealthPoll,
} from "./shared/session";
import { StageList } from "./stages/StageList";

function Missing(props: { path: string }) {
  return (
    <Frame current={null}>
      <section class="sheet">
        <h1>Not on this roadbook</h1>
        <p class="lede">Nothing lives at {props.path}.</p>
        <p>
          <A href="/">Open the stages</A>
        </p>
      </section>
    </Frame>
  );
}

export function App() {
  useEffect(() => startHealthPoll(), []);
  useEffect(() => {
    void findSoloDrive();
  }, []);

  const route = routeOf(place.value);

  // The moving lock: while the car moves, only the live view shows.
  useEffect(() => {
    if (moving.value && !(route.name === "live" && route.replay === null)) {
      navigate("/live", { replace: true });
    }
  }, [moving.value, route.name]);

  // The embed opens straight to its 1 drive.
  useEffect(() => {
    if (EMBED && route.name === "stages" && soloDrive.value) {
      navigate(drivePath(soloDrive.value), { replace: true });
    }
  }, [route.name, soloDrive.value]);

  if (route.name === "live") return <Live replay={route.replay} t={route.t} />;
  if (moving.value) return <Live replay={null} t={null} />;
  if (needsToken.value && !EMBED) return <TokenGate />;

  switch (route.name) {
    case "stages":
      if (EMBED && (soloDrive.value || !soloChecked.value)) {
        return (
          <Frame current="review">
            <section class="sheet">
              <Loading>Opening the replay.</Loading>
            </section>
          </Frame>
        );
      }
      return <StageList />;
    case "review":
      return <Review key={route.id} id={route.id} box={route.box} />;
    case "report":
      return <ReportPage key={route.id} id={route.id} />;
    case "legend":
      return <LegendPage />;
    case "missing":
      return <Missing path={route.path} />;
  }
}

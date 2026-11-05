import { useLayoutEffect, useRef, useState } from "preact/hooks";
import { saveToken } from "../shared/data";
import { needsToken, reloadKey, tokenRefused } from "../shared/session";
import { Frame } from "./Frame";

const REFUSED = "The device refused that token. Check PHOTONCAR_API_TOKEN on the device.";

/** Parked only: the device answered 401, so ask for its token. Never shown on /live. */
export function TokenGate() {
  const [value, setValue] = useState("");
  const [empty, setEmpty] = useState(false);
  const field = useRef<HTMLInputElement>(null);
  const refused = tokenRefused.value;
  const problem = empty ? "Type the token first." : refused ? REFUSED : null;

  // After a refusal the gate comes back in place of the page: put the caret back in the field.
  useLayoutEffect(() => {
    if (refused) field.current?.focus();
  }, [refused]);

  return (
    <Frame current={null}>
      <section class="sheet token" aria-labelledby="token-title">
        <h1 id="token-title">Enter the device token</h1>
        <p class="lede">
          This device asks for its token before it shows drives. The token is the value of
          PHOTONCAR_API_TOKEN on the device. It stays in this browser.
        </p>
        <form
          class="token-form"
          onSubmit={(e) => {
            e.preventDefault();
            const token = value.trim();
            if (!token) {
              setEmpty(true);
              return;
            }
            saveToken(token);
            tokenRefused.value = false;
            needsToken.value = false;
            reloadKey.value += 1;
          }}
        >
          <label for="token-input">Device token</label>
          <input
            id="token-input"
            ref={field}
            type="password"
            autoComplete="off"
            spellcheck={false}
            value={value}
            aria-invalid={problem ? true : undefined}
            aria-describedby={problem ? "token-problem" : undefined}
            onInput={(e) => {
              setValue(e.currentTarget.value);
              setEmpty(false);
            }}
          />
          {problem ? (
            <p id="token-problem" class="state state-error token-problem" role="alert">
              {problem}
            </p>
          ) : null}
          <button type="submit" class="btn btn-ink">
            Save token
          </button>
        </form>
      </section>
    </Frame>
  );
}

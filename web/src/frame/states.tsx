import type { ComponentChildren } from "preact";
import type { ApiError } from "../shared/data";

/** Loading, empty, and error states: 1 plain line of text, never a spinner. */
export function Loading(props: { children: ComponentChildren }) {
  return (
    <p class="state state-loading" role="status">
      {props.children}
    </p>
  );
}

export function Empty(props: { children: ComponentChildren }) {
  return (
    <p class="state state-empty" role="status">
      {props.children}
    </p>
  );
}

export function Failed(props: { what: string; error: ApiError }) {
  return (
    <p class="state state-error" role="alert">
      {props.what} {props.error.message}
    </p>
  );
}

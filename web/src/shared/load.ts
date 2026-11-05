import { useEffect, useState } from "preact/hooks";
import { ApiError } from "./data";

export type Loaded<T> =
  { state: "loading" } | { state: "ok"; value: T } | { state: "error"; error: ApiError };

export function asApiError(e: unknown): ApiError {
  if (e instanceof ApiError) return e;
  return new ApiError(0, "Something went wrong while reading the data.");
}

/** Run `load` when `key` changes; a newer run replaces an older one. */
export function useLoad<T>(load: () => Promise<T>, key: string): Loaded<T> {
  const [result, setResult] = useState<Loaded<T>>({ state: "loading" });
  useEffect(() => {
    let live = true;
    setResult({ state: "loading" });
    load().then(
      (value) => {
        if (live) setResult({ state: "ok", value });
      },
      (e: unknown) => {
        if (live) setResult({ state: "error", error: asApiError(e) });
      },
    );
    return () => {
      live = false;
    };
    // `key` names everything `load` depends on.
  }, [key]);
  return result;
}

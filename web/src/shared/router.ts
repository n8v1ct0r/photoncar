/** A small router: history paths on the device, hash paths in the portfolio embed. */

import { signal } from "@preact/signals";
import { EMBED } from "./data";

export type Route =
  | { name: "stages" }
  | { name: "review"; id: string; box: number | null }
  | { name: "report"; id: string }
  | { name: "live"; replay: string | null; t: number | null }
  | { name: "legend" }
  | { name: "missing"; path: string };

export interface Place {
  path: string;
  query: URLSearchParams;
}

function readPlace(): Place {
  const raw = EMBED
    ? window.location.hash.replace(/^#/, "") || "/"
    : window.location.pathname + window.location.search;
  const q = raw.indexOf("?");
  const path = q < 0 ? raw : raw.slice(0, q);
  return { path: path || "/", query: new URLSearchParams(q < 0 ? "" : raw.slice(q + 1)) };
}

export const place = signal<Place>(readPlace());

function sync(): void {
  place.value = readPlace();
}

window.addEventListener(EMBED ? "hashchange" : "popstate", sync);

function positiveInt(raw: string | null): number | null {
  if (raw === null || !/^\d+$/.test(raw)) return null;
  const n = Number(raw);
  return n >= 1 ? n : null;
}

function seconds(raw: string | null): number | null {
  if (raw === null || raw.trim() === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

export function routeOf(p: Place): Route {
  const parts = p.path.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts.length === 0) return { name: "stages" };
  if (parts[0] === "drives" && parts[1] !== undefined) {
    if (parts.length === 2)
      return { name: "review", id: parts[1], box: positiveInt(p.query.get("box")) };
    if (parts.length === 3 && parts[2] === "report") return { name: "report", id: parts[1] };
  }
  if (parts.length === 1 && parts[0] === "live") {
    return { name: "live", replay: p.query.get("replay"), t: seconds(p.query.get("t")) };
  }
  if (parts.length === 1 && parts[0] === "legend") return { name: "legend" };
  return { name: "missing", path: p.path };
}

/** The href for an app path ("/drives/x?box=2"): as is on the device, "#/..." in the embed. */
export function hrefFor(path: string): string {
  return EMBED ? `#${path}` : path;
}

export function navigate(path: string, opts: { replace?: boolean } = {}): void {
  if (EMBED) {
    const url = `${window.location.pathname}${window.location.search}#${path}`;
    if (opts.replace) window.history.replaceState(null, "", url);
    else window.history.pushState(null, "", url);
  } else if (opts.replace) {
    window.history.replaceState(null, "", path);
  } else {
    window.history.pushState(null, "", path);
  }
  sync();
}

export function drivePath(id: string, box?: number | null): string {
  const base = `/drives/${encodeURIComponent(id)}`;
  return box ? `${base}?box=${String(box)}` : base;
}

export function reportPath(id: string): string {
  return `/drives/${encodeURIComponent(id)}/report`;
}

export function livePath(replay?: string | null): string {
  return replay ? `/live?replay=${encodeURIComponent(replay)}` : "/live";
}

/** Follow an in-app link without a page load. Leaves modified clicks to the browser. */
export function followLink(e: MouseEvent, path: string): void {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) {
    return;
  }
  e.preventDefault();
  navigate(path);
  window.scrollTo(0, 0);
}

/** The data seam. Every screen loads data through `DataSource`; no screen calls fetch.
 *
 * - `ApiSource` calls the device's `/api/*` routes (docs/contracts.md, "HTTP API"), adding the
 *   device token when one is stored. The live feed is read with fetch streaming, so the
 *   Authorization header travels with it (EventSource cannot send headers).
 * - `StaticSource` reads the JSON files `photoncar export-embed` writes next to the page
 *   (`./data/*.json`): the API response bodies of 1 drive, its precomputed live frames, and the
 *   privacy trim windows. It never touches the network outside its own folder.
 */

import type {
  DriveBody,
  DriveListItem,
  EventOut,
  ExportOut,
  Health,
  Legend,
  LiveFrame,
  LiveMessage,
  Report,
  SummaryOut,
  Timeline,
  TrimWindows,
} from "./types";

/** An API answer that is not 2xx: its status and its plain `{"error"}` sentence. */
export class ApiError extends Error {
  readonly status: number;
  readonly retryAfterS: number | null;

  constructor(status: number, message: string, retryAfterS: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfterS = retryAfterS;
  }
}

export interface DataSource {
  readonly kind: "api" | "static";
  health(): Promise<Health>;
  drives(): Promise<DriveListItem[]>;
  drive(id: string): Promise<DriveBody>;
  timeline(id: string, maxPoints: number): Promise<Timeline>;
  events(id: string): Promise<EventOut[]>;
  report(id: string): Promise<Report>;
  legend(): Promise<Legend>;
  liveFrame(id: string, tS: number): Promise<LiveFrame>;
  /** Live frames as they arrive: the current drive when `id` is null, else a replay. */
  liveFrames(id: string | null, signal: AbortSignal): AsyncGenerator<LiveMessage>;
  trimWindows(id: string): Promise<TrimWindows>;
  summary(id: string): Promise<SummaryOut>;
  exportLaps(id: string): Promise<ExportOut>;
}

/** The number of timeline points the review asks for; the embed export writes the same. */
export const TIMELINE_POINTS = 1500;

// -- the device token -------------------------------------------------------------------------

const TOKEN_KEY = "photoncar.device_token";
let memoryToken: string | null = null;

export function readToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY) ?? memoryToken;
  } catch {
    return memoryToken;
  }
}

export function saveToken(token: string): void {
  memoryToken = token;
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Storage can be blocked; the token then lasts for this page only.
  }
}

/** Forget the stored token (the device refused it). */
export function clearToken(): void {
  memoryToken = null;
  try {
    window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Storage can be blocked; the memory copy is already gone.
  }
}

// -- status listener (401 asks for the token, 423 means the car moves) -------------------------

/** `sentToken` is the token the failed request carried, or null when it carried none. */
type StatusListener = (status: number, sentToken: string | null) => void;
let statusListener: StatusListener | null = null;

export function onApiStatus(listener: StatusListener | null): void {
  statusListener = listener;
}

// -- ApiSource --------------------------------------------------------------------------------

function authHeaders(token: string | null): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function errorFrom(res: Response, sentToken: string | null): Promise<ApiError> {
  let message = `The device answered ${String(res.status)}.`;
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object" && "error" in body && typeof body.error === "string") {
      message = body.error;
    }
  } catch {
    // Not JSON: keep the status sentence.
  }
  const retry = Number(res.headers.get("Retry-After"));
  statusListener?.(res.status, sentToken);
  return new ApiError(res.status, message, Number.isFinite(retry) && retry > 0 ? retry : null);
}

function networkError(): ApiError {
  return new ApiError(0, "The device did not answer. Check the connection to the car.");
}

/** Parse 1 server-sent event block ("event: x" and "data: y" lines). */
export function parseSseBlock(block: string): { event: string; data: string } | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (line === "" || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  return data.length ? { event, data: data.join("\n") } : null;
}

function toMessage(ev: { event: string; data: string }): LiveMessage | null {
  const body: unknown = JSON.parse(ev.data);
  if (ev.event === "frame") return { type: "frame", frame: body as LiveFrame };
  if (ev.event === "state" && body && typeof body === "object" && "moving" in body) {
    return { type: "state", moving: body.moving === true };
  }
  return null;
}

export class ApiSource implements DataSource {
  readonly kind = "api";

  private async get<T>(path: string, init: RequestInit = {}): Promise<T> {
    const token = readToken();
    let res: Response;
    try {
      res = await fetch(path, {
        ...init,
        headers: { Accept: "application/json", ...authHeaders(token) },
      });
    } catch {
      throw networkError();
    }
    if (!res.ok) throw await errorFrom(res, token);
    return (await res.json()) as T;
  }

  private drivePath(id: string): string {
    return `/api/drives/${encodeURIComponent(id)}`;
  }

  health(): Promise<Health> {
    return this.get<Health>("/api/health");
  }

  async drives(): Promise<DriveListItem[]> {
    return (await this.get<{ drives: DriveListItem[] }>("/api/drives")).drives;
  }

  drive(id: string): Promise<DriveBody> {
    return this.get<DriveBody>(this.drivePath(id));
  }

  timeline(id: string, maxPoints: number): Promise<Timeline> {
    return this.get<Timeline>(`${this.drivePath(id)}/timeline?max_points=${String(maxPoints)}`);
  }

  async events(id: string): Promise<EventOut[]> {
    return (await this.get<{ events: EventOut[] }>(`${this.drivePath(id)}/events`)).events;
  }

  report(id: string): Promise<Report> {
    return this.get<Report>(`${this.drivePath(id)}/report`);
  }

  legend(): Promise<Legend> {
    return this.get<Legend>("/api/legend");
  }

  liveFrame(id: string, tS: number): Promise<LiveFrame> {
    return this.get<LiveFrame>(`${this.drivePath(id)}/live_frame?t_s=${String(tS)}`);
  }

  async *liveFrames(id: string | null, signal: AbortSignal): AsyncGenerator<LiveMessage> {
    const query = id === null ? "" : `?replay=${encodeURIComponent(id)}&speed=1`;
    const token = readToken();
    let res: Response;
    try {
      res = await fetch(`/api/live/stream${query}`, {
        headers: { Accept: "text/event-stream", ...authHeaders(token) },
        signal,
      });
    } catch (e) {
      if (signal.aborted) return;
      throw e instanceof ApiError ? e : networkError();
    }
    if (!res.ok) throw await errorFrom(res, token);
    if (!res.body) throw networkError();
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    try {
      for (;;) {
        let chunk: ReadableStreamReadResult<string>;
        try {
          chunk = await reader.read();
        } catch {
          if (signal.aborted) return;
          throw networkError();
        }
        if (chunk.done) return;
        buffer += chunk.value;
        for (;;) {
          const end = /\r?\n\r?\n/.exec(buffer);
          if (!end) break;
          const block = buffer.slice(0, end.index);
          buffer = buffer.slice(end.index + end[0].length);
          const ev = parseSseBlock(block);
          const msg = ev ? toMessage(ev) : null;
          if (msg) yield msg;
        }
      }
    } finally {
      reader.releaseLock();
    }
  }

  trimWindows(): Promise<TrimWindows> {
    // The device shows its own drives untrimmed; only exports and the embed trim.
    return Promise.resolve([]);
  }

  private async post<T>(path: string): Promise<T> {
    const token = readToken();
    let res: Response;
    try {
      res = await fetch(path, {
        method: "POST",
        headers: { Accept: "application/json", ...authHeaders(token) },
      });
    } catch {
      throw networkError();
    }
    if (!res.ok) throw await errorFrom(res, token);
    return (await res.json()) as T;
  }

  summary(id: string): Promise<SummaryOut> {
    return this.post<SummaryOut>(`${this.drivePath(id)}/summary`);
  }

  exportLaps(id: string): Promise<ExportOut> {
    return this.post<ExportOut>(`${this.drivePath(id)}/export?kind=laps`);
  }
}

// -- StaticSource -----------------------------------------------------------------------------

const FRAME_MS = 100;
const NO_DRIVE = "No drive with that ID.";

/** Wait `ms`; resolves false at once when `signal` aborts first. */
export function sleep(ms: number, signal: AbortSignal): Promise<boolean> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve(false);
      return;
    }
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", stop);
      resolve(true);
    }, ms);
    const stop = (): void => {
      window.clearTimeout(timer);
      resolve(false);
    };
    signal.addEventListener("abort", stop, { once: true });
  });
}

export function prefersReducedMotion(): boolean {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export class StaticSource implements DataSource {
  readonly kind = "static";
  private readonly files = new Map<string, Promise<unknown>>();
  private readonly base: URL;

  constructor(base: string = "./data/") {
    this.base = new URL(base, document.baseURI);
  }

  private file<T>(name: string): Promise<T> {
    let p = this.files.get(name);
    if (!p) {
      const url = new URL(name, this.base);
      p = fetch(url).then(
        async (res) => {
          if (!res.ok) throw new ApiError(res.status, "This replay is missing a data file.");
          return (await res.json()) as unknown;
        },
        () => {
          throw new ApiError(0, "This replay could not load its data files.");
        },
      );
      p.catch(() => this.files.delete(name));
      this.files.set(name, p);
    }
    return p as Promise<T>;
  }

  private async own(id: string): Promise<void> {
    const drives = await this.drives();
    if (!drives.some((d) => d.drive_id === id)) throw new ApiError(404, NO_DRIVE);
  }

  health(): Promise<Health> {
    return Promise.resolve({ ok: true, version: "static", mode: "replay", moving: false });
  }

  async drives(): Promise<DriveListItem[]> {
    return (await this.file<{ drives: DriveListItem[] }>("drives.json")).drives;
  }

  async drive(id: string): Promise<DriveBody> {
    await this.own(id);
    return this.file<DriveBody>("drive.json");
  }

  async timeline(id: string): Promise<Timeline> {
    await this.own(id);
    return this.file<Timeline>("timeline.json");
  }

  async events(id: string): Promise<EventOut[]> {
    await this.own(id);
    return (await this.file<{ events: EventOut[] }>("events.json")).events;
  }

  async report(id: string): Promise<Report> {
    await this.own(id);
    return this.file<Report>("report.json");
  }

  legend(): Promise<Legend> {
    return this.file<Legend>("legend.json");
  }

  private async frames(id: string | null): Promise<LiveFrame[]> {
    if (id !== null) await this.own(id);
    return this.file<LiveFrame[]>("live_frames.json");
  }

  async liveFrame(id: string, tS: number): Promise<LiveFrame> {
    const frames = await this.frames(id);
    const first = frames[0];
    const last = frames[frames.length - 1];
    if (!first || !last || tS < first.t_s - FRAME_MS / 2000 || tS > last.t_s + FRAME_MS / 2000) {
      throw new ApiError(422, "That time is outside this replay window.");
    }
    let best = first;
    for (const f of frames) if (Math.abs(f.t_s - tS) < Math.abs(best.t_s - tS)) best = f;
    return best;
  }

  async *liveFrames(id: string | null, signal: AbortSignal): AsyncGenerator<LiveMessage> {
    const frames = await this.frames(id);
    yield { type: "state", moving: false };
    if (frames.length === 0) return;
    if (prefersReducedMotion()) {
      const still = frames[Math.floor(frames.length / 2)];
      if (still) yield { type: "frame", frame: still };
      return;
    }
    for (;;) {
      for (const frame of frames) {
        if (signal.aborted) return;
        yield { type: "frame", frame };
        if (!(await sleep(FRAME_MS, signal))) return;
      }
    }
  }

  async trimWindows(id: string): Promise<TrimWindows> {
    await this.own(id);
    return (await this.file<{ windows_s: TrimWindows }>("trim.json")).windows_s;
  }

  summary(): Promise<SummaryOut> {
    return Promise.reject(new ApiError(404, "The drive summary runs on the device only."));
  }

  exportLaps(): Promise<ExportOut> {
    return Promise.reject(new ApiError(404, "Lap export runs on the device only."));
  }
}

/** True in the portfolio embed build (`vite build --mode embed`). */
export const EMBED = import.meta.env.MODE === "embed";

export const data: DataSource = EMBED ? new StaticSource() : new ApiSource();

/** App-wide state: the moving lock, the token prompt, and the embed's single drive. */

import { signal } from "@preact/signals";
import { clearToken, data, EMBED, onApiStatus, readToken } from "./data";
import { hasDatasetCredit } from "./format";
import type { Health } from "./types";

/** True while the car moves: parked views close and only /live shows. */
export const moving = signal(false);
/** True after a 401: parked views ask for the device token. */
export const needsToken = signal(false);
/** True after the device answered 401 to the stored token: the prompt says it was refused. */
export const tokenRefused = signal(false);
/** Bumped after the token is saved, so every screen loads again. */
export const reloadKey = signal(0);
/** In the embed, the 1 drive the export holds (the list opens straight to it). */
export const soloDrive = signal<string | null>(null);
/** True once the embed has read its drive list (always true on the device). */
export const soloChecked = signal(!EMBED);
/** True when the embed's 1 drive is a public-dataset replay, so every route prints the credit. */
export const soloCredit = signal(false);
/** The device's pipeline mode from the last health poll; null until one answers (and in the
 * embed, which has no device). "idle" means nothing is recording. */
export const deviceMode = signal<Health["mode"] | null>(null);

onApiStatus((status, sentToken) => {
  if (status === 401) {
    // A request that went out before the token changed says nothing about the current token.
    if (sentToken !== readToken()) return;
    if (sentToken !== null) {
      tokenRefused.value = true;
      clearToken();
    }
    needsToken.value = true;
  }
  if (status === 423) moving.value = true;
});

const HEALTH_MS = 2000;

/** Poll /api/health for the moving lock. The embed never moves. */
export function startHealthPoll(): () => void {
  if (EMBED) return () => undefined;
  let stopped = false;
  let timer = 0;
  const tick = async (): Promise<void> => {
    try {
      const h = await data.health();
      moving.value = h.moving;
      deviceMode.value = h.mode;
    } catch {
      // A failed poll keeps the last known state; 401 and 423 reach the listener above.
    }
    if (!stopped) timer = window.setTimeout(() => void tick(), HEALTH_MS);
  };
  void tick();
  return () => {
    stopped = true;
    window.clearTimeout(timer);
  };
}

export async function findSoloDrive(): Promise<void> {
  if (!EMBED) return;
  try {
    const drives = await data.drives();
    const only = drives.length === 1 ? drives[0] : undefined;
    soloDrive.value = only ? only.drive_id : null;
    soloChecked.value = true;
    if (only) soloCredit.value = hasDatasetCredit((await data.drive(only.drive_id)).drive);
  } catch {
    // A missing data file leaves the drive or its credit unknown; each route says so itself.
  }
  soloChecked.value = true;
}

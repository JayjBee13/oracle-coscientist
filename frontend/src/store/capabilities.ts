/**
 * What the backend can do, fetched once and shared.
 *
 * Only one thing reads this today — the top bar's model indicator — but the
 * rule from `store/runs.ts` holds here too: no component fetches for itself.
 * The shell mounts once and every consumer joins the same request, so a second
 * reader (the wizard, a settings screen) costs nothing.
 *
 * The payload is a description of the machine, not run state: it cannot change
 * while the tab is open unless the backend is redeployed under it. So there is
 * no polling — one fetch, cached, with `refreshCapabilities()` for the retry
 * button when that fetch failed.
 */

import { useEffect, useSyncExternalStore } from "react";

import * as api from "../api/client";
import { errorMessage } from "../api/client";
import type { Capabilities } from "../api/types";
import type { LoadState } from "./runs";

export type CapabilitiesState = {
  status: LoadState;
  data: Capabilities | null;
  error: string | null;
};

const IDLE: CapabilitiesState = { status: "idle", data: null, error: null };

let state: CapabilitiesState = IDLE;
const listeners = new Set<() => void>();
let inFlight: Promise<void> | null = null;
let generation = 0;

function setState(next: CapabilitiesState): void {
  state = next;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getCapabilitiesState(): CapabilitiesState {
  return state;
}

/**
 * Fetches once. Repeat calls join the request in flight rather than racing it,
 * which is what StrictMode's double mount would otherwise cause.
 */
export function fetchCapabilities(options: { force?: boolean } = {}): Promise<void> {
  if (inFlight) return inFlight;
  if (!options.force && state.status === "ready") return Promise.resolve();

  setState({ status: "loading", data: state.data, error: null });
  const requestGeneration = ++generation;
  const request = api
    .getCapabilities()
    .then((data) => {
      if (requestGeneration !== generation) return;
      setState({ status: "ready", data, error: null });
    })
    .catch((error: unknown) => {
      if (requestGeneration !== generation) return;
      // Chrome-level background work: it reports itself in place (the indicator
      // says so and offers a retry) rather than raising a toast over the page.
      setState({ status: "error", data: state.data, error: errorMessage(error) });
    })
    .finally(() => {
      if (requestGeneration === generation) inFlight = null;
    });

  inFlight = request;
  return request;
}

/** Ask again after a failure. */
export function refreshCapabilities(): Promise<void> {
  return fetchCapabilities({ force: true });
}

/** The capabilities payload, fetched on first mount. */
export function useCapabilities(): CapabilitiesState {
  const snapshot = useSyncExternalStore(
    subscribe,
    () => state,
    () => state,
  );

  useEffect(() => {
    void fetchCapabilities();
  }, []);

  return snapshot;
}

/* --- Test support --------------------------------------------------------- */

export function resetCapabilitiesStore(): void {
  generation += 1;
  inFlight = null;
  setState(IDLE);
}

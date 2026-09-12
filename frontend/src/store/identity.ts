/** The live request identity, shared by capability-aware UI surfaces. */

import { useEffect, useSyncExternalStore } from "react";

import * as api from "../api/client";
import { errorMessage, isApiError } from "../api/client";
import type { CurrentIdentity } from "../api/types";
import type { LoadState } from "./runs";

export type IdentityState = {
  status: LoadState;
  data: CurrentIdentity | null;
  error: string | null;
  errorCode: string | null;
  errorStatus: number | null;
};

const IDLE: IdentityState = {
  status: "idle",
  data: null,
  error: null,
  errorCode: null,
  errorStatus: null,
};
let state = IDLE;
let inFlight: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<() => void>();
const boundaryListeners = new Set<() => void>();
let focusWatchers = 0;

function setState(next: IdentityState): void {
  if (identityBoundaryKey(state.data) !== identityBoundaryKey(next.data)) {
    for (const listener of boundaryListeners) listener();
  }
  state = next;
  for (const listener of listeners) listener();
}

function identityBoundaryKey(identity: CurrentIdentity | null): string | null {
  return identity
    ? `${identity.source}:${identity.username}:${identity.is_admin ? "admin" : "user"}`
    : null;
}

export function onIdentityBoundaryChange(listener: () => void): () => void {
  boundaryListeners.add(listener);
  return () => boundaryListeners.delete(listener);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function invalidateIdentity(error: api.ApiError): void {
  generation += 1;
  inFlight = null;
  setState({
    status: "error",
    data: null,
    error: error.message,
    errorCode: error.code,
    errorStatus: error.status,
  });
}

// This store is a singleton, so the API boundary gets one listener no matter
// how many components read the identity.
api.onAuthenticationFailure(invalidateIdentity);

function revalidateOnFocus(): void {
  void fetchIdentity({ force: true });
}

function watchWindowFocus(): () => void {
  focusWatchers += 1;
  if (focusWatchers === 1) window.addEventListener("focus", revalidateOnFocus);
  return () => {
    focusWatchers = Math.max(0, focusWatchers - 1);
    if (focusWatchers === 0) window.removeEventListener("focus", revalidateOnFocus);
  };
}

export function fetchIdentity(options: { force?: boolean } = {}): Promise<void> {
  if (inFlight) return inFlight;
  if (!options.force && state.status === "ready") return Promise.resolve();
  setState({
    status: "loading",
    data: state.data,
    error: null,
    errorCode: null,
    errorStatus: null,
  });
  const requestGeneration = ++generation;
  inFlight = api
    .getCurrentIdentity()
    .then((data) => {
      if (requestGeneration !== generation) return;
      setState({
        status: "ready",
        data,
        error: null,
        errorCode: null,
        errorStatus: null,
      });
    })
    .catch((error: unknown) => {
      if (requestGeneration !== generation) return;
      // Identity is the privacy boundary. A stale administrator from an earlier
      // gateway session must never survive a failed refresh and keep private
      // controls or cached workspace data visible.
      setState({
        status: "error",
        data: null,
        error: errorMessage(error),
        errorCode: isApiError(error) ? error.code : null,
        errorStatus: isApiError(error) ? error.status : null,
      });
    })
    .finally(() => {
      if (requestGeneration === generation) inFlight = null;
    });
  return inFlight;
}

export function useIdentity(): IdentityState {
  const snapshot = useSyncExternalStore(
    subscribe,
    () => state,
    () => state,
  );
  useEffect(() => {
    void fetchIdentity();
    return watchWindowFocus();
  }, []);
  return snapshot;
}

export function resetIdentityStore(): void {
  generation += 1;
  inFlight = null;
  setState(IDLE);
}

/** Seeds a verified identity for focused component tests. */
export function primeIdentity(data: CurrentIdentity): void {
  generation += 1;
  inFlight = null;
  setState({
    status: "ready",
    data,
    error: null,
    errorCode: null,
    errorStatus: null,
  });
}

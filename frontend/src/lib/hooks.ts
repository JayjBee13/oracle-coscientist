/**
 * Small hooks the run screens need.
 *
 * Two constraints shaped all of them. They must be StrictMode-safe — cleanup
 * cancels its own work rather than setting a "mounted" flag, which is the bug
 * that killed the previous Research Notebook in development. And none of them
 * calls `setState` synchronously inside an effect: where a status like
 * "loading" is really a function of the argument, it is *derived* during render
 * rather than written into state and read back a render later.
 */

import { useEffect, useState } from "react";

import * as api from "../api/client";
import { errorMessage } from "../api/client";
import type { HypothesisDetail } from "../api/types";

/**
 * A clock that only ticks while something is moving. The in-call spinner and
 * the last-activity chip both need "now"; a completed run needs no timer at
 * all, and a page full of stopped runs should cost nothing.
 */
export function useNow(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  const period = active ? intervalMs : IDLE_TICK_MS;

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), period);
    return () => clearInterval(timer);
  }, [period]);

  return now;
}

/** A finished run still shows "last activity 3 d ago"; once a minute is plenty. */
const IDLE_TICK_MS = 60_000;

/** Debounces a fast-changing value — the search box, so typing is not a DDoS. */
export function useDebounced<T>(value: T, delayMs = 250): T {
  const [settled, setSettled] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);

  return settled;
}

/**
 * Reveals something a fixed time after a trigger — the force-stop escape hatch,
 * which stays hidden for twenty seconds so that "stop" gets a fair chance to
 * work before the violent option is on screen.
 */
export function useDelayedReveal(armedAt: number | null, delayMs: number): boolean {
  const [elapsedFor, setElapsedFor] = useState<number | null>(null);

  useEffect(() => {
    if (armedAt === null) return;
    const remaining = Math.max(0, armedAt + delayMs - Date.now());
    const timer = setTimeout(() => setElapsedFor(armedAt), remaining);
    return () => clearTimeout(timer);
  }, [armedAt, delayMs]);

  return armedAt !== null && elapsedFor === armedAt;
}

export type LoadStatus = "idle" | "loading" | "ready" | "error";

export type HypothesisState = {
  detail: HypothesisDetail | null;
  status: LoadStatus;
  error: string | null;
};

type Loaded<T> = {
  key: string;
  value: T | null;
  error: string | null;
  code: string | null;
};

/**
 * One hypothesis, fetched on demand.
 *
 * Deliberately outside the run store: the store owns what a whole screen shares
 * (the run, its events, its stream), and a hypothesis body is read by one card
 * at a time. There is no cache — reopening a card refetches — because the
 * alternative is showing a body that a later round has already evolved.
 */
export function useHypothesisDetail(hypothesisId: string | null): HypothesisState {
  const [loaded, setLoaded] = useState<Loaded<HypothesisDetail>>({
    key: "",
    value: null,
    error: null,
    code: null,
  });

  useEffect(() => {
    if (!hypothesisId) return;
    const controller = new AbortController();

    api
      .getHypothesis(hypothesisId, controller.signal)
      .then((detail) => {
        if (controller.signal.aborted) return;
        setLoaded({ key: hypothesisId, value: detail, error: null, code: null });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoaded({
          key: hypothesisId,
          value: null,
          error: errorMessage(error),
          code: api.isApiError(error) ? error.code : null,
        });
      });

    return () => controller.abort();
  }, [hypothesisId]);

  if (!hypothesisId) return { detail: null, status: "idle", error: null };
  if (loaded.key !== hypothesisId)
    return { detail: null, status: "loading", error: null };
  if (loaded.error) return { detail: null, status: "error", error: loaded.error };
  return { detail: loaded.value, status: "ready", error: null };
}

export type TextState = {
  text: string | null;
  status: LoadStatus;
  error: string | null;
  /** The API's error code — "overview_missing" is not a failure, it is news. */
  code: string | null;
};

/**
 * The research overview, as markdown. A 404 is not a failure here — most runs
 * have not written one yet — so the error code comes back with it, letting the
 * caller tell "no report yet" apart from "the backend is down".
 *
 * `present` is `RunSummary.has_overview`. When the summary already says there
 * is no overview, asking for it anyway would be a request whose only outcome is
 * a 404 in the console; the answer is reported straight from the summary
 * instead. The summary refetches when a run finishes, so a report that appears
 * mid-watch is picked up on that refresh.
 */
export function useOverview(runId: string, reloadKey = 0, present = true): TextState {
  const requestKey = `${runId}#${reloadKey}#${present ? "1" : "0"}`;
  const [loaded, setLoaded] = useState<Loaded<string>>({
    key: "",
    value: null,
    error: null,
    code: null,
  });

  useEffect(() => {
    if (!present) return;
    const controller = new AbortController();

    api
      .getOverview(runId, controller.signal)
      .then((text) => {
        if (controller.signal.aborted) return;
        setLoaded({ key: requestKey, value: text, error: null, code: null });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoaded({
          key: requestKey,
          value: null,
          error: errorMessage(error),
          code: api.isApiError(error) ? error.code : null,
        });
      });

    return () => controller.abort();
  }, [runId, requestKey, present]);

  if (!present) {
    return { text: null, status: "ready", error: null, code: "overview_missing" };
  }
  if (loaded.key !== requestKey) {
    return { text: null, status: "loading", error: null, code: null };
  }
  if (loaded.error) {
    return { text: null, status: "error", error: loaded.error, code: loaded.code };
  }
  return { text: loaded.value, status: "ready", error: null, code: loaded.code };
}

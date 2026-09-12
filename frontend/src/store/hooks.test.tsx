import { renderHook, waitFor } from "@testing-library/react";
import { StrictMode, act } from "react";
import { describe, expect, it, vi } from "vitest";

import { makeRunDetail, makeRunSummary, mockFetchRoutes } from "../test/fixtures";
import { MockEventSource } from "../test/mockEventSource";
import { primeRuns, useRun, useRunEvents, useRuns } from "./runs";

function stubApi() {
  const fetchMock = vi.fn(
    mockFetchRoutes([
      { match: "/events/ticket", json: { ticket: "t-abc", expires_in: 60 } },
      { match: "/detail", json: makeRunDetail() },
      { match: "/runs", json: { items: [makeRunSummary()], total: 1 } },
    ]),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/**
 * StrictMode stays on in development, which means every effect mounts, tears
 * down and mounts again. The previous UI had a `mountedRef` that was never
 * reset on the second mount and silently swallowed every async result. These
 * tests exist so that cannot come back.
 */
describe("hooks under StrictMode", () => {
  it("holds exactly one live stream across the double mount", async () => {
    stubApi();
    primeRuns([makeRunSummary()]);

    const { result, unmount } = renderHook(() => useRunEvents("run-1"), {
      wrapper: StrictMode,
    });

    await waitFor(() => {
      expect(result.current.connection).toBe("live");
    });
    expect(MockEventSource.openCount).toBe(1);

    act(() => {
      MockEventSource.last.emitRunEvent({ seq: 3, type: "hypothesis_added" });
    });
    expect(result.current.events).toHaveLength(1);
    expect(result.current.lastSeq).toBe(3);

    unmount();
    expect(MockEventSource.openCount).toBe(0);
  });

  it("fetches a run's detail once and delivers it to the component", async () => {
    const fetchMock = stubApi();

    const { result } = renderHook(() => useRun("run-1"), { wrapper: StrictMode });

    await waitFor(() => {
      expect(result.current.status).toBe("ready");
    });
    expect(result.current.detail?.run.id).toBe("run-1");
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]).includes("/detail")),
    ).toHaveLength(1);
  });

  it("refetches the list when the query changes, not on every render", async () => {
    const fetchMock = stubApi();

    const { result, rerender } = renderHook(({ q }: { q: string }) => useRuns({ q }), {
      wrapper: StrictMode,
      initialProps: { q: "ammonia" },
    });

    await waitFor(() => {
      expect(result.current.status).toBe("ready");
    });
    const afterFirst = fetchMock.mock.calls.length;

    rerender({ q: "ammonia" });
    expect(fetchMock.mock.calls).toHaveLength(afterFirst);

    rerender({ q: "catalysis" });
    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThan(afterFirst);
    });
    expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain("q=catalysis");
  });
});

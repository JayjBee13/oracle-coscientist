import { afterEach, describe, expect, it, vi } from "vitest";

import { makeRunDetail, makeRunSummary, mockFetchRoutes } from "../test/fixtures";
import { MockEventSource } from "../test/mockEventSource";
import {
  fetchRuns,
  getActiveRuns,
  getConnectionState,
  getLanes,
  getRunEntry,
  getRunsListState,
  getRunSummary,
  primeRuns,
  refreshLanes,
  resetRunStore,
  subscribeRunStream,
  TERMINAL_DRAIN_MS,
} from "./runs";

const RUN_ID = "run-1";

function stubApi(overrides: Parameters<typeof mockFetchRoutes>[0] = []) {
  const fetchMock = vi.fn(
    mockFetchRoutes([
      ...overrides,
      { match: "/events/ticket", json: { ticket: "t-abc", expires_in: 60 } },
      { match: "/detail", json: makeRunDetail() },
      { match: "/runs", json: { items: [makeRunSummary()], total: 1 } },
    ]),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function detailCalls(fetchMock: ReturnType<typeof vi.fn>): number {
  return fetchMock.mock.calls.filter((call) => String(call[0]).includes("/detail"))
    .length;
}

async function connectRun(id = RUN_ID): Promise<{
  unsubscribe: () => void;
  source: MockEventSource;
}> {
  const unsubscribe = subscribeRunStream(id);
  await vi.waitFor(() => {
    expect(getRunEntry(id).connection).toBe("live");
  });
  return { unsubscribe, source: MockEventSource.last };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("workspace generation", () => {
  it("ignores a previous user's lane response after the store is reset", async () => {
    let aliceStarted = false;
    let resolveAlice: (response: Response) => void = () => {
      throw new Error("Alice's request did not start.");
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            aliceStarted = true;
            resolveAlice = resolve;
          }),
      ),
    );

    const aliceRequest = refreshLanes({ force: true });
    expect(aliceStarted).toBe(true);
    resetRunStore();
    primeRuns([makeRunSummary({ id: "bob-run", title: "Bob's run" })]);

    resolveAlice(
      new Response(
        JSON.stringify({
          items: [makeRunSummary({ id: "alice-run", title: "Alice's private run" })],
          total: 1,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await aliceRequest;

    expect(getLanes().find((lane) => lane.harness === "claude")?.run?.title).toBe(
      "Bob's run",
    );
    expect(getRunSummary("alice-run")).toBeNull();
  });
});

describe("stream connection", () => {
  it("fetches a ticket first and puts it on the stream URL", async () => {
    const fetchMock = stubApi();
    primeRuns([makeRunSummary()]);

    const { unsubscribe, source } = await connectRun();

    expect(fetchMock.mock.calls[0][0]).toContain("/events/ticket");
    expect(source.url).toContain(`/runs/${RUN_ID}/events`);
    expect(source.url).toContain("ticket=t-abc");
    unsubscribe();
  });

  it("keeps one connection for many subscribers and closes it on the last one", async () => {
    stubApi();
    primeRuns([makeRunSummary()]);

    const first = subscribeRunStream(RUN_ID);
    const second = subscribeRunStream(RUN_ID);
    await vi.waitFor(() => {
      expect(MockEventSource.openCount).toBe(1);
    });

    first();
    expect(MockEventSource.openCount).toBe(1);

    second();
    expect(MockEventSource.openCount).toBe(0);
    expect(getRunEntry(RUN_ID).connection).toBe("idle");
  });
});

describe("patching from events", () => {
  it("applies a lifecycle change to the cached run immediately", async () => {
    stubApi();
    primeRuns([makeRunSummary({ lifecycle: "running" })]);
    const { unsubscribe, source } = await connectRun();

    source.emitRunEvent({
      seq: 5,
      type: "lifecycle_changed",
      payload: { lifecycle: "paused" },
    });

    expect(getRunSummary(RUN_ID)?.lifecycle).toBe("paused");
    expect(getRunEntry(RUN_ID).events).toHaveLength(1);
    expect(getRunEntry(RUN_ID).lastSeq).toBe(5);
    unsubscribe();
  });

  it("advances the round from the event's own round number", async () => {
    stubApi();
    primeRuns([makeRunSummary({ round: 1 })]);
    const { unsubscribe, source } = await connectRun();

    source.emitRunEvent({ seq: 2, round: 3, type: "round_started" });

    expect(getRunSummary(RUN_ID)?.round).toBe(3);
    unsubscribe();
  });

  it("ignores events already seen, so a resume overlap does not duplicate", async () => {
    stubApi();
    primeRuns([makeRunSummary()]);
    const { unsubscribe, source } = await connectRun();

    source.emitRunEvent({ seq: 4, type: "review_recorded" });
    source.emitRunEvent({ seq: 3, type: "review_recorded" });
    source.emitRunEvent({ seq: 4, type: "review_recorded" });

    expect(getRunEntry(RUN_ID).events).toHaveLength(1);
    unsubscribe();
  });

  it("keeps an unparseable line instead of swallowing it", async () => {
    stubApi();
    primeRuns([makeRunSummary()]);
    const { unsubscribe, source } = await connectRun();

    source.emit("run_event", "{not json");

    const [event] = getRunEntry(RUN_ID).events;
    expect(event.type).toBe("malformed");
    expect(event.payload.raw).toBe("{not json");
    unsubscribe();
  });

  it("announces a finished run in the document title", async () => {
    stubApi();
    primeRuns([makeRunSummary({ title: "Ammonia routes" })]);
    const { unsubscribe, source } = await connectRun();

    source.emitRunEvent({
      seq: 9,
      type: "run_finished",
      payload: { lifecycle: "completed" },
    });

    expect(document.title).toContain("Completed");
    expect(document.title).toContain("Ammonia routes");
    unsubscribe();
  });
});

describe("draining the last event out of a finished run", () => {
  it("holds the run open past the lifecycle flip, then closes on run_finished", async () => {
    // The engine writes the lifecycle before it emits run_finished, so a
    // watcher that hangs up on the lifecycle never hears the run's last word.
    stubApi([
      {
        match: "/detail",
        json: makeRunDetail({ run: makeRunSummary({ lifecycle: "completed" }) }),
      },
    ]);
    primeRuns([makeRunSummary({ lifecycle: "running" })]);
    const { unsubscribe, source } = await connectRun();

    source.emitRunEvent({
      seq: 1,
      type: "lifecycle_changed",
      payload: { lifecycle: "completed" },
    });
    await vi.waitFor(() => {
      expect(getRunEntry(RUN_ID).draining).toBe(true);
    });

    source.emitRunEvent({
      seq: 2,
      type: "run_finished",
      payload: { lifecycle: "completed" },
    });
    expect(getRunEntry(RUN_ID).draining).toBe(false);
    expect(getRunEntry(RUN_ID).events.map((event) => event.type)).toContain(
      "run_finished",
    );
    unsubscribe();
  });

  it("gives up on a run that will never send one", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    stubApi([
      {
        match: "/detail",
        json: makeRunDetail({ run: makeRunSummary({ lifecycle: "stopped" }) }),
      },
    ]);
    primeRuns([makeRunSummary({ lifecycle: "running" })]);
    const unsubscribe = subscribeRunStream(RUN_ID);
    await vi.waitFor(() => {
      expect(getRunEntry(RUN_ID).connection).toBe("live");
    });

    MockEventSource.last.emitRunEvent({
      seq: 1,
      type: "lifecycle_changed",
      payload: { lifecycle: "stopped" },
    });
    await vi.waitFor(() => {
      expect(getRunEntry(RUN_ID).draining).toBe(true);
    });

    await vi.advanceTimersByTimeAsync(TERMINAL_DRAIN_MS + 100);
    expect(getRunEntry(RUN_ID).draining).toBe(false);
    unsubscribe();
  });
});

describe("detail refetch scheduling", () => {
  it("coalesces a burst of events into one trailing-edge refetch", async () => {
    const fetchMock = stubApi();
    primeRuns([makeRunSummary()]);
    const { unsubscribe, source } = await connectRun();
    const before = detailCalls(fetchMock);

    vi.useFakeTimers();
    source.emitRunEvent({ seq: 1, type: "hypothesis_added" });
    source.emitRunEvent({ seq: 2, type: "review_recorded" });
    source.emitRunEvent({ seq: 3, type: "review_recorded" });

    // Trailing edge: nothing has gone out yet.
    expect(detailCalls(fetchMock)).toBe(before);

    vi.advanceTimersByTime(0);
    expect(detailCalls(fetchMock)).toBe(before + 1);

    unsubscribe();
  });

  it("holds the next refetch to at most one per second", async () => {
    const fetchMock = stubApi();
    primeRuns([makeRunSummary()]);
    const { unsubscribe, source } = await connectRun();
    const before = detailCalls(fetchMock);

    vi.useFakeTimers();
    source.emitRunEvent({ seq: 1, type: "hypothesis_added" });
    vi.advanceTimersByTime(0);
    expect(detailCalls(fetchMock)).toBe(before + 1);

    source.emitRunEvent({ seq: 2, type: "hypothesis_added" });
    vi.advanceTimersByTime(999);
    expect(detailCalls(fetchMock)).toBe(before + 1);

    vi.advanceTimersByTime(1);
    expect(detailCalls(fetchMock)).toBe(before + 2);

    unsubscribe();
  });

  it("refetches immediately on the events where a stale number would mislead", async () => {
    const fetchMock = stubApi();
    primeRuns([makeRunSummary()]);
    const { unsubscribe, source } = await connectRun();

    vi.useFakeTimers();
    for (const [index, type] of [
      "round_completed",
      "lifecycle_changed",
      "run_failed",
    ].entries()) {
      const before = detailCalls(fetchMock);
      source.emitRunEvent({ seq: index + 1, type });
      expect(detailCalls(fetchMock)).toBe(before + 1);
    }

    unsubscribe();
  });
});

describe("reconnect", () => {
  it("backs off and resumes from the last sequence number it saw", async () => {
    stubApi();
    primeRuns([makeRunSummary()]);
    const { unsubscribe, source } = await connectRun();

    source.emitRunEvent({ seq: 7, type: "hypothesis_added" });

    vi.useFakeTimers();
    source.fail();
    expect(getRunEntry(RUN_ID).connection).toBe("reconnecting");
    expect(MockEventSource.instances).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(500);
    await vi.waitFor(() => {
      expect(MockEventSource.instances).toHaveLength(2);
    });

    expect(MockEventSource.last.url).toContain("after_seq=7");
    unsubscribe();
  });

  it("waits longer after each failed attempt", async () => {
    stubApi();
    primeRuns([makeRunSummary()]);
    const { unsubscribe } = await connectRun();

    vi.useFakeTimers();
    MockEventSource.autoOpen = false;

    MockEventSource.last.fail();
    await vi.advanceTimersByTimeAsync(500);
    await vi.waitFor(() => {
      expect(MockEventSource.instances).toHaveLength(2);
    });

    MockEventSource.last.fail();
    await vi.advanceTimersByTimeAsync(999);
    expect(MockEventSource.instances).toHaveLength(2);

    await vi.advanceTimersByTimeAsync(1);
    await vi.waitFor(() => {
      expect(MockEventSource.instances).toHaveLength(3);
    });

    unsubscribe();
  });
});

describe("list, lanes and reachability", () => {
  it("loads the list and derives a lane per harness", async () => {
    stubApi([
      {
        match: "/runs",
        json: {
          items: [
            makeRunSummary({ id: "a", harness: "claude", lifecycle: "running" }),
            makeRunSummary({ id: "b", harness: "demo", lifecycle: "paused" }),
            makeRunSummary({ id: "c", harness: "claude", lifecycle: "completed" }),
          ],
          total: 3,
        },
      },
    ]);

    await fetchRuns({ page_size: 25 });

    const lanes = getLanes();
    expect(lanes.map((lane) => lane.harness)).toEqual(["claude", "demo"]);
    expect(lanes[0].run?.id).toBe("a");
    expect(lanes[1].run?.id).toBe("b");
    expect(
      getActiveRuns()
        .map((run) => run.id)
        .sort(),
    ).toEqual(["a", "b"]);
  });

  it("reports the backend as unreachable rather than showing an empty list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
    );

    await fetchRuns();

    const state = getRunsListState();
    expect(state.status).toBe("error");
    expect(state.ids).toHaveLength(0);
    expect(state.error).toMatch(/could not reach/i);
    expect(getConnectionState()).toBe("offline");
  });

  it("counts an answer that is not the API as offline too", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("<!doctype html>", { status: 200 })),
    );

    await fetchRuns();

    expect(getRunsListState().status).toBe("error");
    expect(getConnectionState()).toBe("offline");
  });
});

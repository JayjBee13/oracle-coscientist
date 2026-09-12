import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  buildQuery,
  errorMessage,
  getRunEventsUrl,
  listRuns,
  sendControl,
} from "./client";

describe("query building", () => {
  it("drops empty values instead of sending them", () => {
    expect(buildQuery({ q: "ammonia", page: 2, harness: undefined, sort: "" })).toBe(
      "?q=ammonia&page=2",
    );
    expect(buildQuery({})).toBe("");
  });

  it("puts the ticket and the resume point on the stream URL", () => {
    const url = getRunEventsUrl("run-1", { ticket: "t-abc", afterSeq: 42 });
    expect(url).toContain("/runs/run-1/events");
    expect(url).toContain("ticket=t-abc");
    expect(url).toContain("after_seq=42");
  });

  it("omits the resume point on a first connection", () => {
    expect(getRunEventsUrl("run-1", { ticket: "t" })).not.toContain("after_seq");
  });
});

describe("error handling", () => {
  it("reads the {code, message, details} envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json(
          {
            code: "lane_busy",
            message: "Another Claude run is already in flight.",
            details: { conflicting_run_id: "run-9" },
          },
          { status: 409 },
        ),
      ),
    );

    const error = await sendControl("run-1", "pause").catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.code).toBe("lane_busy");
    expect(apiError.status).toBe(409);
    expect(apiError.message).toBe("Another Claude run is already in flight.");
    expect(apiError.details).toEqual({ conflicting_run_id: "run-9" });
  });

  it("says the backend is unreachable rather than 'Failed to fetch'", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
    );

    const error = (await listRuns().catch((caught: unknown) => caught)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.isOffline).toBe(true);
    expect(error.code).toBe("backend_unreachable");
    expect(errorMessage(error)).toMatch(/could not reach the oracle backend/i);
  });

  it("says so when a 200 is not the API at all", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () => new Response("<!doctype html><title>app</title>", { status: 200 }),
      ),
    );

    const error = (await listRuns().catch((caught: unknown) => caught)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("invalid_response");
    expect(error.message).toMatch(/could not read/i);
  });

  it("still produces a sentence when the body is not an envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("<html>500</html>", { status: 500 })),
    );

    const error = (await listRuns().catch((caught: unknown) => caught)) as ApiError;
    expect(error.code).toBe("http_500");
    expect(error.message).toMatch(/failed \(500\)/);
  });
});

describe("requests", () => {
  it("passes list filters through as query parameters", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      Response.json({ items: [], total: 0 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await listRuns({ q: "ammonia", show_archived: true, page_size: 50 });

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("q=ammonia");
    expect(url).toContain("show_archived=true");
    expect(url).toContain("page_size=50");
  });

  it("sends controls as a JSON action", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      Response.json({ accepted: true, lifecycle: "pausing" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await sendControl("run-1", "pause");

    expect(response).toEqual({ accepted: true, lifecycle: "pausing" });
    const init = fetchMock.mock.calls[0][1];
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(JSON.stringify({ action: "pause" }));
  });
});

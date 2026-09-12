import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { HypothesisDetail, RunDetail } from "../api/types";
import { makeRunDetail, mockFetchRoutes } from "../test/fixtures";
import {
  makeHypothesisDetail,
  makeHypothesisRow,
  makeMatchRow,
  makeReview,
} from "../test/runFixtures";
import { HypothesisPage } from "./HypothesisPage";

function stub(runDetail: RunDetail, hypothesis: HypothesisDetail | null) {
  const fetchMock = vi.fn(
    mockFetchRoutes([
      hypothesis
        ? { match: "/hypotheses/", json: hypothesis }
        : { match: "/hypotheses/", status: 404, json: { code: "hypothesis_not_found" } },
      { match: "/detail", json: runDetail },
    ]),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPage(hid = "h001") {
  return render(
    <MemoryRouter initialEntries={[`/runs/run-1/hypotheses/${hid}`]}>
      <Routes>
        <Route path="/runs/:id/hypotheses/:hid" element={<HypothesisPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

const runWithOne = makeRunDetail({ leaderboard: [makeHypothesisRow()] });

describe("hypothesis view", () => {
  it("reads the claim as a document", async () => {
    stub(runWithOne, makeHypothesisDetail());
    renderPage();

    expect(
      await screen.findByRole("heading", { name: "Plasmonic nitrogen fixation" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Claim" })).toBeInTheDocument();
    expect(
      screen.getByText("Ammonia forms at ambient pressure on plasmonic gold."),
    ).toBeInTheDocument();
    expect(screen.getByText("Hot electrons cross the barrier")).toBeInTheDocument();
  });

  it("shows all five things the reviewer said", async () => {
    stub(runWithOne, makeHypothesisDetail());
    renderPage();

    expect(await screen.findByText("Passed review")).toBeInTheDocument();
    expect(
      screen.getByTitle("How new the reviewer judged this idea to be."),
    ).toHaveTextContent("Highly novel");
    expect(
      screen.getByText(/no published route uses plasmonic hot electrons/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/the thermodynamics hold/i)).toBeInTheDocument();
    expect(screen.getByText(/a bench photoreactor/i)).toBeInTheDocument();
    expect(screen.getByText(/hot-electron lifetimes/i)).toBeInTheDocument();
    expect(screen.getByText(/worth pursuing/i)).toBeInTheDocument();
  });

  it("prints the Elo it carried into each match and how it moved", async () => {
    stub(runWithOne, makeHypothesisDetail());
    renderPage();

    const row = (await screen.findByText("Electrochemical nitride cycling")).closest(
      "tr",
    );
    expect(row).toHaveTextContent("1200 → 1216");
    expect(row).toHaveTextContent("(+16)");
    expect(row).toHaveTextContent("Won");
  });

  it("draws the curve once there are two points to join", async () => {
    stub(
      runWithOne,
      makeHypothesisDetail({
        match_history: [
          makeMatchRow({ id: "m1", round: 1 }),
          makeMatchRow({
            id: "m2",
            round: 2,
            elo_a_before: 1216,
            elo_a_after: 1232,
            b: { hid: "h009", title: "Another idea" },
          }),
        ],
      }),
    );
    renderPage();

    expect(await screen.findByRole("img", { name: /elo over/i })).toBeInTheDocument();
  });

  it("says why there is no curve when the run never recorded one", async () => {
    stub(
      runWithOne,
      makeHypothesisDetail({
        match_history: [
          makeMatchRow({
            elo_a_before: null,
            elo_a_after: null,
            elo_b_before: null,
            elo_b_after: null,
          }),
        ],
      }),
    );
    renderPage();

    expect(await screen.findByText(/there is no curve to draw/i)).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /elo over/i })).not.toBeInTheDocument();
    const row = screen.getByText("Electrochemical nitride cycling").closest("tr");
    expect(row).toHaveTextContent("—");
    expect(row).not.toHaveTextContent("0 → 0");
  });

  it("links to where the idea came from and what it became", async () => {
    stub(
      runWithOne,
      makeHypothesisDetail({
        lineage: {
          parents: [makeHypothesisRow({ hid: "h000", title: "The seed idea" })],
          children: [makeHypothesisRow({ hid: "h012", title: "The evolved idea" })],
        },
      }),
    );
    renderPage();

    expect(await screen.findByText("Evolved from")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /the seed idea/i })).toHaveAttribute(
      "href",
      "/runs/run-1/hypotheses/h000",
    );
    expect(screen.getByRole("link", { name: /the evolved idea/i })).toHaveAttribute(
      "href",
      "/runs/run-1/hypotheses/h012",
    );
  });

  it("sets a hypothesis aside", async () => {
    const fetchMock = stub(runWithOne, makeHypothesisDetail());
    renderPage();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /set aside/i }));

    await vi.waitFor(() => {
      expect(
        fetchMock.mock.calls.some((call) =>
          String(call[0]).includes("/hypotheses/h001/archive"),
        ),
      ).toBe(true);
    });
  });

  it("offers no archive action for something already rejected", async () => {
    stub(
      makeRunDetail({ leaderboard: [makeHypothesisRow({ status: "rejected" })] }),
      makeHypothesisDetail({
        status: "rejected",
        reviews: [makeReview({ verdict: "reject" })],
      }),
    );
    renderPage();

    expect(await screen.findByText("Rejected in review")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /set aside/i })).not.toBeInTheDocument();
  });

  it("is honest about an address that names nothing", async () => {
    stub(runWithOne, makeHypothesisDetail());
    renderPage("h999");

    expect(
      await screen.findByText(/no such hypothesis in this run/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /see every hypothesis/i })).toHaveAttribute(
      "href",
      "/runs/run-1?tab=hypotheses",
    );
  });

  it("names the problem when the body will not load", async () => {
    stub(runWithOne, null);
    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /could not load this hypothesis/i,
    );
  });
});

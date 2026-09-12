import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { makeRunDetail } from "../../test/fixtures";
import { ResearchTab } from "./ResearchTab";

vi.mock("../../store/capabilities", () => ({
  useCapabilities: () => ({ data: null }),
}));

it("keeps counterevidence visible after a candidate leaves the recommendation portfolio", async () => {
  const detail = makeRunDetail({
    research: {
      phase: "reported",
      source_strategy: {
        published_research_percent: 80,
        rationale: "Empirical claims need published evidence.",
        scholarly_queries: ["systematic review"],
        other_source_queries: ["official dataset"],
      },
      objective: "Solve the problem",
      approaches: [],
      subproblems: [],
      decisions: [],
      portfolio: [],
      synthesis_round: null,
      synthesis_markdown: "",
      challenge_assessment: "",
      blocking_issues: [],
      dependencies: [],
      unresolved: [],
      recommendation_ready: false,
      contradictions: [
        {
          hid: "h1",
          title: "Rejected premise",
          claims: [
            {
              id: "c1",
              claim: "A decisive premise",
              status: "contradicted",
              rationale: "The independent test found a counterexample.",
              sources: [
                {
                  url: "https://example.org/test",
                  finding: "Observed counterexample",
                  relation: "contradicts",
                },
              ],
            },
          ],
        },
      ],
    },
  });
  render(<ResearchTab detail={detail} />);
  await userEvent.setup().click(screen.getByText(/h1.*Rejected premise/));
  expect(screen.getByText(/The independent test found a counterexample/)).toBeVisible();
  expect(screen.getByRole("link", { name: "Observed counterexample" })).toHaveAttribute(
    "href",
    "https://example.org/test",
  );
  expect(screen.getByText(/Research remains provisional/)).toBeInTheDocument();
  expect(screen.getByText("80%")).toBeVisible();
  expect(screen.getByText("20%")).toBeVisible();
  expect(
    screen.getByText(/Planned search emphasis, not measured source counts/),
  ).toBeVisible();
});

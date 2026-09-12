import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeRunDetail, makeRunSummary } from "../../test/fixtures";
import { SettingsTab } from "./SettingsTab";

/**
 * What became of the documents the scientist attached.
 *
 * This panel used to print each document's full character count and nothing
 * else, which reads as confirmation that all of it was used. Every call is
 * capped at 20,000 characters, and until the engine started reporting the
 * outcome the only trace of a megabyte reduced to a fraction of itself was one
 * log line inside the run's working directory.
 */
describe("SettingsTab context documents", () => {
  it("says which documents were cut and which never went at all", () => {
    const detail = makeRunDetail({
      context_docs: [
        {
          name: "protocol.md",
          chars: 200_000,
          delivered: "truncated",
          cap_chars: 20_000,
        },
        { name: "priors.md", chars: 800, delivered: "full", cap_chars: 20_000 },
        { name: "notes.md", chars: 4_000, delivered: "omitted", cap_chars: 20_000 },
      ],
    });

    render(<SettingsTab run={makeRunSummary()} detail={detail} />);

    expect(screen.getByText(/cut to fit 20k characters per call/)).toBeInTheDocument();
    expect(screen.getByText("sent in full")).toBeInTheDocument();
    expect(
      screen.getByText(/not sent — no room under 20k characters/),
    ).toBeInTheDocument();
  });

  it("does not claim an outcome before the run has composed a prompt", () => {
    const detail = makeRunDetail({
      context_docs: [
        { name: "brief.md", chars: 800, delivered: "pending", cap_chars: null },
      ],
    });

    render(<SettingsTab run={makeRunSummary()} detail={detail} />);

    expect(screen.getByText("not yet sent")).toBeInTheDocument();
  });
});

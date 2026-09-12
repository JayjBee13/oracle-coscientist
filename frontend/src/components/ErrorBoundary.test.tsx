import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "./ErrorBoundary";

function Boom(): never {
  throw new Error("leaderboard exploded");
}

beforeEach(() => {
  // React logs the caught error; the boundary logs it too. Neither is a
  // failure, and both drown the test output.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

describe("error boundary", () => {
  it("replaces a screen that throws with a way out, not a white page", () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("This screen stopped working");
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reload/i })).toBeInTheDocument();
    expect(screen.getByText(/leaderboard exploded/)).toBeInTheDocument();
  });

  it("renders its children when nothing throws", () => {
    render(
      <ErrorBoundary>
        <p>All well</p>
      </ErrorBoundary>,
    );

    expect(screen.getByText("All well")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("recovers when the user retries and the child now works", async () => {
    const user = userEvent.setup();
    let shouldThrow = true;
    function Flaky() {
      if (shouldThrow) throw new Error("transient");
      return <p>Recovered</p>;
    }

    render(
      <ErrorBoundary>
        <Flaky />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();

    shouldThrow = false;
    await user.click(screen.getByRole("button", { name: /try again/i }));

    expect(screen.getByText("Recovered")).toBeInTheDocument();
  });

  it("clears the error when the route changes", () => {
    const { rerender } = render(
      <ErrorBoundary resetKey="/runs/a">
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();

    rerender(
      <ErrorBoundary resetKey="/runs/b">
        <p>Different screen</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText("Different screen")).toBeInTheDocument();
  });
});

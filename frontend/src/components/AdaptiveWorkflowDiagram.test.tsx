import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AdaptiveWorkflowDiagram } from "./AdaptiveWorkflowDiagram";

describe("the adaptive workflow explanation", () => {
  it("explains independent search, evidence limits and feedback without promising validation", async () => {
    const user = userEvent.setup();
    render(<AdaptiveWorkflowDiagram models={null} demo />);
    expect(screen.getAllByRole("button")).toHaveLength(7);
    await user.click(screen.getByRole("button", { name: /Critique and check evidence/ }));
    expect(
      screen.getByText(/real experiments and simulations remain pending/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Challenge the whole answer/ }));
    expect(
      screen.getByText(/Blocking findings feed the next checkpoint/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Demo: simulated responses/)).toBeInTheDocument();
  });

  it("exposes the feedback loops and supports keyboard selection", async () => {
    const user = userEvent.setup();
    render(<AdaptiveWorkflowDiagram models={null} />);
    expect(
      screen.getByRole("group", { name: /Oracle workflow: independent approaches/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Challenge findings → next checkpoint/)).toBeInTheDocument();
    expect(
      screen.getByText(/Preserved ideas \+ evidence → further development/),
    ).toBeInTheDocument();
    const control = screen.getByRole("button", { name: "Choose the next research work" });
    control.focus();
    await user.keyboard("{Enter}");
    expect(control).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(/At each checkpoint, Oracle chooses/)).toBeVisible();
  });
});

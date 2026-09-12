import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { pushToast } from "../lib/toast";
import { Toaster } from "./Toaster";

afterEach(() => {
  vi.useRealTimers();
});

describe("toasts", () => {
  it("announces a message and lets it be dismissed", async () => {
    const user = userEvent.setup();
    render(<Toaster />);

    act(() => {
      pushToast({ tone: "go", title: "Note sent", message: "Read next round." });
    });

    expect(screen.getByText("Note sent")).toBeInTheDocument();
    expect(screen.getByText("Read next round.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /dismiss/i }));
    expect(screen.queryByText("Note sent")).not.toBeInTheDocument();
  });

  it("announces failures assertively and successes politely", () => {
    render(<Toaster />);

    act(() => {
      pushToast({ tone: "danger", title: "Stop all failed" });
      pushToast({ tone: "go", title: "Saved" });
    });

    expect(screen.getByRole("alert")).toHaveTextContent("Stop all failed");
    expect(screen.getByRole("status")).toHaveTextContent("Saved");
  });

  it("clears itself after its duration", () => {
    vi.useFakeTimers();
    render(<Toaster />);

    act(() => {
      pushToast({ title: "Transient", duration: 3000 });
    });
    expect(screen.getByText("Transient")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(2999);
    });
    expect(screen.getByText("Transient")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(screen.queryByText("Transient")).not.toBeInTheDocument();
  });

  it("keeps a sticky toast until it is dismissed", () => {
    vi.useFakeTimers();
    render(<Toaster />);

    act(() => {
      pushToast({ title: "Stays put", duration: 0 });
    });

    act(() => {
      vi.advanceTimersByTime(60000);
    });
    expect(screen.getByText("Stays put")).toBeInTheDocument();
  });
});

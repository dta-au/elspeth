import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";
import { ComposerOptions } from "./ComposerOptions";

describe("ComposerOptions", () => {
  beforeEach(() => resetStore(useSessionStore));

  it("reveals Guided by keyboard without changing mode, and Escape restores focus", async () => {
    const user = userEvent.setup();
    const enterGuided = vi.fn();
    const exitToFreeform = vi.fn();
    useSessionStore.setState({ enterGuided, exitToFreeform });
    render(<ComposerOptions hasWork />);
    const trigger = screen.getByRole("button", { name: "Composer options" });
    expect(screen.queryByRole("button", { name: "Switch to guided" })).toBeNull();
    await user.tab();
    expect(trigger).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(enterGuided).not.toHaveBeenCalled();
    expect(exitToFreeform).not.toHaveBeenCalled();
    await user.tab();
    expect(screen.getByRole("button", { name: "Switch to guided" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(screen.getByLabelText("What should this pipeline produce?")).toBeInTheDocument();
    await user.tab();
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("What should this pipeline produce?")).toBeNull();
    expect(enterGuided).not.toHaveBeenCalled();
  });

  it("preserves the disabled action's visible explanation inside the disclosure", async () => {
    const user = userEvent.setup();
    render(<ComposerOptions hasWork disabledReason="Wait for the current operation to finish." />);
    await user.click(screen.getByRole("button", { name: "Composer options" }));
    const action = screen.getByRole("button", { name: "Switch to guided" });
    expect(action).toBeDisabled();
    expect(action).toHaveAccessibleDescription("Wait for the current operation to finish.");
  });

  it("requires the existing goal confirmation before fresh conversion", async () => {
    const user = userEvent.setup();
    const enterGuided = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ enterGuided });
    render(<ComposerOptions hasWork />);
    await user.click(screen.getByRole("button", { name: "Composer options" }));
    await user.click(screen.getByRole("button", { name: "Switch to guided" }));
    expect(screen.getByText(/saved to version history/)).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Confirm switch to guided" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText("What should this pipeline produce?"), "Summarize invoices");
    await user.click(confirm);
    expect(enterGuided).toHaveBeenCalledWith("Summarize invoices");
  });

  it("closes the disclosure when its session key changes", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ComposerOptions key="session-a" hasWork />);
    await user.click(screen.getByRole("button", { name: "Composer options" }));
    act(() => rerender(<ComposerOptions key="session-b" hasWork />));
    expect(screen.getByRole("button", { name: "Composer options" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: "Switch to guided" })).toBeNull();
  });
});

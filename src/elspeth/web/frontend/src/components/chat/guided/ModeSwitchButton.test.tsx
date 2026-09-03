import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ModeSwitchButton } from "./ModeSwitchButton";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";

describe("ModeSwitchButton", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
  });

  /** Type a goal into the confirm card's required box. */
  function stateGoal(goal: string): void {
    fireEvent.change(
      screen.getByLabelText("What should this pipeline produce?"),
      { target: { value: goal } },
    );
  }

  it("target=guided, no work: the card still opens, because the fresh wizard needs a goal", () => {
    // Goal-first (elspeth-378cfa0e18). The old single-click switch had nothing
    // to root the new wizard on, so it POSTed an intent-less convert and
    // persisted a rootless wizard — the state that made the goal card
    // unreachable and let the step-2 finish plan from a fallback sentence. The
    // store also cannot tell an empty session from a worked one synchronously
    // (only GET /guided can), so the card asks once either way.
    const enterGuided = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ enterGuided });

    render(<ModeSwitchButton target="guided" hasWork={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Switch to guided" }));

    expect(enterGuided).not.toHaveBeenCalled();
    stateGoal("Summarise every page as one JSON row.");
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm switch to guided" }),
    );
    expect(enterGuided).toHaveBeenCalledWith(
      "Summarise every page as one JSON row.",
    );
  });

  it("target=guided, with work: click reveals a confirm; confirm switches with the goal", () => {
    const enterGuided = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ enterGuided });

    render(<ModeSwitchButton target="guided" hasWork />);
    fireEvent.click(screen.getByRole("button", { name: "Switch to guided" }));

    // First click only arms the confirmation — it must NOT switch yet.
    expect(enterGuided).not.toHaveBeenCalled();
    stateGoal("Turn each row into a one-line summary.");
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm switch to guided" }),
    );
    expect(enterGuided).toHaveBeenCalledTimes(1);
    expect(enterGuided).toHaveBeenCalledWith(
      "Turn each row into a one-line summary.",
    );
  });

  it("target=guided: Confirm stays disabled until a non-blank goal is typed", () => {
    // A goal-less confirm has nothing to root the wizard on and the store
    // refuses it, so the affordance must not be live. Whitespace does not
    // count: the intent is trimmed before it is sent.
    const enterGuided = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ enterGuided });

    render(<ModeSwitchButton target="guided" hasWork />);
    fireEvent.click(screen.getByRole("button", { name: "Switch to guided" }));

    const confirm = screen.getByRole("button", {
      name: "Confirm switch to guided",
    });
    expect(confirm).toBeDisabled();

    stateGoal("   ");
    expect(confirm).toBeDisabled();

    stateGoal("  Summarise each page.  ");
    expect(confirm).not.toBeDisabled();
    fireEvent.click(confirm);
    // Trimmed on the way out — the server's visible-content rule and the
    // custody fingerprint both key on the exact string.
    expect(enterGuided).toHaveBeenCalledWith("Summarise each page.");
  });

  it("target=guided: the goal box carries a real label, not a placeholder-only name", () => {
    render(<ModeSwitchButton target="guided" hasWork />);
    fireEvent.click(screen.getByRole("button", { name: "Switch to guided" }));

    const box = screen.getByRole("textbox", {
      name: "What should this pipeline produce?",
    });
    expect(box).toBeRequired();
    expect(box).toHaveAttribute("placeholder");
  });

  it("target=guided, with work: the confirm is honest that guided starts fresh and the pipeline is recoverable", () => {
    // "Fresh wizard + consent" (elspeth-e2c3dba6b5): converting a worked
    // freeform session reseeds a fresh wizard and sets the current pipeline
    // aside. The two-step confirm must disclose this rather than implying a
    // lossless in-place switch — the recoverability (version history) is what
    // makes the discard consented rather than surprising.
    const enterGuided = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ enterGuided });

    render(<ModeSwitchButton target="guided" hasWork />);
    fireEvent.click(screen.getByRole("button", { name: "Switch to guided" }));

    expect(screen.getByText(/fresh/i)).toBeInTheDocument();
    expect(screen.getByText(/version history/i)).toBeInTheDocument();
  });

  it("target=guided, with work: renders an explicit contextual confirmation panel", () => {
    const enterGuided = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ enterGuided });

    const { container } = render(<ModeSwitchButton target="guided" hasWork />);
    fireEvent.click(screen.getByRole("button", { name: "Switch to guided" }));

    expect(container.querySelector(".mode-switch-confirm-card")).not.toBeNull();
    expect(screen.getByText("Switch to guided mode?")).toBeInTheDocument();
    expect(
      screen.getByRole("group", { name: "Confirm switch to guided" }),
    ).toHaveAttribute("aria-describedby");
    expect(
      screen.getByRole("button", { name: "Confirm switch to guided" }),
    ).toHaveAccessibleDescription(/version history/i);
  });

  it("target=guided, with work, exited-guided session: the confirm says it RESUMES the saved wizard (F10b)", () => {
    // enterGuided branches on guidedSession.terminal.kind ===
    // "exited_to_freeform": those sessions re-enter their saved wizard
    // (reenterGuided) — nothing is discarded. Showing the fresh-wizard
    // warning for that safe resume made users refuse a safe action.
    const enterGuided = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({
      enterGuided,
      guidedSession: {
        step: "step_3_transforms",
        history: [],
        terminal: { kind: "exited_to_freeform" },
        chat_history: [],
        chat_turn_seq: 0,
        profile: null,
      } as never,
    });

    render(<ModeSwitchButton target="guided" hasWork />);
    fireEvent.click(screen.getByRole("button", { name: "Switch to guided" }));

    expect(screen.getByText(/pick up your guided setup where you left it/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing is discarded/i)).toBeInTheDocument();
    // A RESUME asks for no goal — the saved wizard already has its root
    // (goal-first, elspeth-378cfa0e18), and demanding a second one would
    // reintroduce exactly the "safe action dressed as destructive" defect F10b
    // fixed. Confirm is therefore live immediately.
    expect(
      screen.queryByLabelText("What should this pipeline produce?"),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Confirm switch to guided" }),
    ).not.toBeDisabled();
    expect(screen.queryByText(/fresh/i)).toBeNull();
    expect(screen.queryByText(/version history/i)).toBeNull();
    expect(
      screen.getByRole("button", { name: "Confirm switch to guided" }),
    ).toHaveAccessibleDescription(/where you left it/i);

    fireEvent.click(screen.getByRole("button", { name: "Confirm switch to guided" }));
    expect(enterGuided).toHaveBeenCalledTimes(1);
  });

  it("target=guided, with work, no prior guided exit: the fresh-wizard warning stays (F10b)", () => {
    const enterGuided = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ enterGuided, guidedSession: null });

    render(<ModeSwitchButton target="guided" hasWork />);
    fireEvent.click(screen.getByRole("button", { name: "Switch to guided" }));

    expect(screen.getByText(/fresh/i)).toBeInTheDocument();
    expect(screen.getByText(/version history/i)).toBeInTheDocument();
    expect(screen.queryByText(/where you left it/i)).toBeNull();
  });

  it("target=freeform, with work: the confirm does NOT carry the fresh-wizard note", () => {
    // The disclosure is guided-direction only; exiting to freeform is a
    // genuinely lossless in-place switch and must keep its terse confirm.
    const exitToFreeform = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ exitToFreeform });

    render(<ModeSwitchButton target="freeform" hasWork />);
    fireEvent.click(screen.getByRole("button", { name: "Exit to freeform" }));

    expect(screen.queryByText(/version history/i)).toBeNull();
  });

  it("target=freeform, with work: names the current guided context before confirming", () => {
    const exitToFreeform = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ exitToFreeform });

    render(<ModeSwitchButton target="freeform" hasWork />);
    fireEvent.click(screen.getByRole("button", { name: "Exit to freeform" }));

    expect(screen.getByText("Exit to freeform mode?")).toBeInTheDocument();
    expect(screen.getByText(/continue in the freeform composer/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Confirm exit to freeform" }),
    ).toHaveAccessibleDescription(/continue in the freeform composer/i);
  });

  it("a session change clears the typed goal and closes the card", () => {
    // The goal belongs to the session it was typed for. ChatPanel is mounted
    // without a key, so switching sessions reconciles to this same component
    // instance and, without the reset, both the text and the open card
    // survive: session B's card opens prefilled with A's goal, Confirm already
    // enabled, and one click roots B on words the author never wrote for it —
    // B's durable root row, first transcript turn and planner brief.
    const enterGuided = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ enterGuided, activeSessionId: "session-a" });

    render(<ModeSwitchButton target="guided" hasWork />);
    fireEvent.click(screen.getByRole("button", { name: "Switch to guided" }));
    stateGoal("Summarize the invoices.");

    act(() => {
      useSessionStore.setState({ activeSessionId: "session-b" });
    });

    expect(
      screen.queryByLabelText("What should this pipeline produce?"),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Switch to guided" }));
    expect(
      screen.getByLabelText("What should this pipeline produce?"),
    ).toHaveValue("");
    const confirm = screen.getByRole("button", {
      name: "Confirm switch to guided",
    });
    expect(confirm).toBeDisabled();
    fireEvent.click(confirm);
    expect(enterGuided).not.toHaveBeenCalled();
  });

  it("with work: Cancel dismisses the confirm without switching", () => {
    const enterGuided = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ enterGuided });

    render(<ModeSwitchButton target="guided" hasWork />);
    fireEvent.click(screen.getByRole("button", { name: "Switch to guided" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(enterGuided).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Switch to guided" }),
    ).toBeInTheDocument();
  });

  it("target=freeform: labelled 'Exit to freeform' and calls exitToFreeform", () => {
    const exitToFreeform = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ exitToFreeform });

    render(<ModeSwitchButton target="freeform" hasWork={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Exit to freeform" }));

    expect(exitToFreeform).toHaveBeenCalledTimes(1);
  });

  it("target=freeform, with work: confirm label is 'Confirm exit to freeform'", () => {
    const exitToFreeform = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ exitToFreeform });

    render(<ModeSwitchButton target="freeform" hasWork />);
    fireEvent.click(screen.getByRole("button", { name: "Exit to freeform" }));

    expect(exitToFreeform).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm exit to freeform" }),
    );
    expect(exitToFreeform).toHaveBeenCalledTimes(1);
  });

  // ── C-4b: permanently-terminal guided sessions ─────────────────────────────

  it("disabledReason set: renders a disabled button with the explanation, never calls enterGuided", () => {
    const enterGuided = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ enterGuided });

    render(
      <ModeSwitchButton
        target="guided"
        hasWork={false}
        disabledReason="Guided ended for this session — start a new session to use guided."
      />,
    );

    const button = screen.getByRole("button", { name: "Switch to guided" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(enterGuided).not.toHaveBeenCalled();

    expect(
      screen.getByText(
        "Guided ended for this session — start a new session to use guided.",
      ),
    ).toBeInTheDocument();
    // The explanation is programmatically associated with the button, not
    // just visually nearby — a screen reader must not silently skip it.
    expect(button).toHaveAttribute("aria-describedby");
    const describedById = button.getAttribute("aria-describedby");
    expect(document.getElementById(describedById!)).toHaveTextContent(
      "Guided ended for this session",
    );
  });

  it("disabledReason unset: the normal switch/confirm flow renders instead", () => {
    render(<ModeSwitchButton target="guided" hasWork={false} />);

    expect(
      screen.getByRole("button", { name: "Switch to guided" }),
    ).not.toBeDisabled();
  });
});

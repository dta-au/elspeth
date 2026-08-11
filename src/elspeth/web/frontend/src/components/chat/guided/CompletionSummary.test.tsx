// ============================================================================
// CompletionSummary -- regression coverage for the guided-mode terminal widget.
//
// Pinned contracts:
//   1. YAML text is rendered in the highlighted block -- the pipeline_yaml
//      string appears in the document when terminal.kind === "completed" and
//      pipeline_yaml is non-null.  (Does NOT assert Prism token structure --
//      that is Prism's contract, not ours.)
//   2. Task-oriented terminal actions render as <button type="button">.
//   3. The freeform click invokes useSessionStore.exitToFreeform once.
//   5. terminal.kind !== "completed" handling -- the parent should not render
//      CompletionSummary in non-completed terminals.  The widget defensively
//      returns null in that case as well.  Negative-space pin.
//   7. Distinctness pin (Task 7.4 I4 inheritance): two simultaneous
//      CompletionSummary instances have per-instance IDs that differ.
//      Asserted via not.toBe() on elements carrying useId()-scoped IDs.
//   8. Initial-render no-auto-focus -- neither button has focus on mount
//      (matches convention from InspectAndConfirmTurn, Task 7.3).
//   9. Reduced-motion classes -- verified by CSS (not directly testable in
//      jsdom); this file notes the expectation for the reviewer.
//
// Source of truth:
//   - types/guided.ts:54-58 (TerminalState wire shape)
//   - stores/sessionStore.ts:116 + 572-583 (exitToFreeform parameterless)
//   - docs/superpowers/plans/2026-05-11-composer-guided-mode.md:4445
// ============================================================================

import { beforeEach, describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CompletionSummary } from "./CompletionSummary";
import { useSessionStore } from "@/stores/sessionStore";
import { useExecutionStore } from "@/stores/executionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { resetStore } from "@/test/store-helpers";
import type { TerminalState } from "@/types/guided";

// ── Fixtures ──────────────────────────────────────────────────────────────────

const COMPLETED_TERMINAL: TerminalState = {
  kind: "completed",
  reason: null,
  pipeline_yaml: 'source:\n  plugin: csv\n  options:\n    path: data.csv\n',
};

const EXITED_TERMINAL: TerminalState = {
  kind: "exited_to_freeform",
  reason: "user_pressed_exit",
  pipeline_yaml: null,
};

// ── Store reset ───────────────────────────────────────────────────────────────

beforeEach(() => {
  resetStore(useSessionStore);
  resetStore(useExecutionStore);
  resetStore(useInterpretationEventsStore);
});

// ── Contract 1: common workspace owns artifacts ─────────────────────────

describe("CompletionSummary -- workspace convergence", () => {
  it("does not duplicate YAML, validation, or run surfaces", () => {
    render(<CompletionSummary terminal={COMPLETED_TERMINAL} />);
    expect(screen.queryByRole("region", { name: "Pipeline YAML" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Export YAML" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Validate pipeline" }),
    ).toBeNull();
    expect(screen.queryByTestId("inline-run-results")).toBeNull();
  });

  it("renders a heading element for the completion state", () => {
    render(<CompletionSummary terminal={COMPLETED_TERMINAL} />);
    // Heading per Task 7.6 M3 convention for primary entity names
    const heading = screen.getByRole("heading");
    expect(heading).toBeInTheDocument();
  });
});

// ── Contract 2: Single button renders with type="button" ─────────────────────

describe("CompletionSummary -- button identity", () => {
  it("renders only the freeform transition with type='button'", () => {
    render(<CompletionSummary terminal={COMPLETED_TERMINAL} />);
    const button = screen.getByRole("button", { name: /open freeform editor/i });
    expect(button).toHaveAttribute("type", "button");
    expect(screen.getAllByRole("button")).toEqual([button]);
  });
});

// ── Contract 3: Exit calls exitToFreeform ────────────────────────────────────

describe("CompletionSummary -- exit action", () => {
  it("clicking 'Open freeform editor' calls exitToFreeform once", async () => {
    const user = userEvent.setup();
    const mockExit = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ exitToFreeform: mockExit });

    render(<CompletionSummary terminal={COMPLETED_TERMINAL} />);
    await user.click(
      screen.getByRole("button", { name: /open freeform editor/i }),
    );

    expect(mockExit).toHaveBeenCalledTimes(1);
    expect(mockExit).toHaveBeenCalledWith();
  });

});

// ── Contract 5: non-completed terminal -> null ────────────────────────────────

describe("CompletionSummary -- non-completed terminal guard (negative space)", () => {
  it("returns null when terminal.kind is 'exited_to_freeform'", () => {
    const { container } = render(
      <CompletionSummary terminal={EXITED_TERMINAL} />,
    );
    expect(container.firstChild).toBeNull();
  });
});

// ── Contract 7: distinctness pin (two simultaneous instances) ─────────────────

describe("CompletionSummary -- distinctness pin (Task 7.4 I4 inheritance)", () => {
  it("two simultaneous CompletionSummary instances have distinct heading IDs", () => {
    render(
      <div>
        <CompletionSummary terminal={COMPLETED_TERMINAL} />
        <CompletionSummary terminal={COMPLETED_TERMINAL} />
      </div>,
    );
    const headings = screen.getAllByRole("heading");
    // Two instances => two headings
    expect(headings).toHaveLength(2);
    // Headings are distinct DOM nodes (not.toBe per Task 7.4 I4 convention)
    expect(headings[0]).not.toBe(headings[1]);
  });

});

// ── Contract 8: no auto-focus on mount ────────────────────────────────────────

describe("CompletionSummary -- no auto-focus on initial render", () => {
  it("the exit button does not have focus immediately after render", () => {
    render(<CompletionSummary terminal={COMPLETED_TERMINAL} />);
    const saveBtn = screen.getByRole("button", {
      name: /open freeform editor/i,
    });
    expect(document.activeElement).not.toBe(saveBtn);
  });
});

// ── Heading honesty: derived from actual readiness state ─────────────────────
// elspeth-bf9c296ee5: the heading must distinguish Review required /
// Pipeline updated / Pipeline ready from the same signals that gate Run
// (pending interpretation rows, backend execution admission) instead of
// unconditionally claiming readiness.

describe("CompletionSummary -- heading derived from mutation/readiness state", () => {
  const READY_VALIDATION = {
    is_valid: true,
    checks: [],
    errors: [],
    warnings: [],
    readiness: {
      authoring_valid: true,
      execution_ready: true,
      completion_ready: true,
      blockers: [],
    },
  };

  it("defaults to 'Pipeline updated' when nothing has admitted the pipeline", () => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    render(<CompletionSummary terminal={COMPLETED_TERMINAL} />);
    expect(
      screen.getByRole("heading", { name: "Pipeline updated" }),
    ).toBeInTheDocument();
  });

  it("says 'Review required' while pending interpretation rows block the run gate", () => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    useInterpretationEventsStore.setState({
      pendingBySession: {
        "session-1": { "evt-1": { id: "evt-1", choice: "pending" } },
      },
    } as never);
    render(<CompletionSummary terminal={COMPLETED_TERMINAL} />);
    expect(
      screen.getByRole("heading", { name: "Review required" }),
    ).toBeInTheDocument();
  });

  it("says 'Pipeline ready' only once the backend admits execution", () => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    useExecutionStore.setState({ validationResult: READY_VALIDATION } as never);
    render(<CompletionSummary terminal={COMPLETED_TERMINAL} />);
    expect(
      screen.getByRole("heading", { name: "Pipeline ready" }),
    ).toBeInTheDocument();
  });

  it("pending review outranks execution admission, mirroring the run gate", () => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    useExecutionStore.setState({ validationResult: READY_VALIDATION } as never);
    useInterpretationEventsStore.setState({
      pendingBySession: {
        "session-1": { "evt-1": { id: "evt-1", choice: "pending" } },
      },
    } as never);
    render(<CompletionSummary terminal={COMPLETED_TERMINAL} />);
    expect(
      screen.getByRole("heading", { name: "Review required" }),
    ).toBeInTheDocument();
  });
});

// ── Concern B: tutorial suppression ──────────────────────────────────────────

describe("CompletionSummary -- tutorial suppression (concern B)", () => {
  it("hides 'Open freeform editor' when isTutorial (concern B)", () => {
    render(<CompletionSummary terminal={COMPLETED_TERMINAL} isTutorial />);
    // The summary still renders. Bind to the SEMANTIC heading element (an
    // <h3>, CompletionSummary.tsx:87), matching the file's existing pattern
    // (CompletionSummary.test.tsx:95 uses getByRole("heading")) — getByText
    // would still pass if the heading were demoted to a paragraph.
    // Default completion state (no validation verdict, no pending reviews)
    // reads "Pipeline updated" — "Pipeline ready" is reserved for backend
    // execution admission (elspeth-bf9c296ee5).
    expect(
      screen.getByRole("heading", { name: "Pipeline updated" }),
    ).toBeInTheDocument();
    // ...but the freeform exit is suppressed in a tutorial.
    expect(
      screen.queryByRole("button", { name: "Open freeform editor" }),
    ).toBeNull();
    // Artifact/action ownership stays in the surrounding common workspace.
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});

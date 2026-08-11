// ============================================================================
// CompletionSummary -- regression coverage for the guided-mode terminal widget.
//
// Pinned contracts:
//   1. The readiness-derived completion heading remains visible.
//   2. Non-tutorial completion exposes one real button for the state-machine
//      transition; YAML, validation, and runs belong to the common workspace.
//   3. The click invokes useSessionStore.exitToFreeform exactly once.
//   4. terminal.kind !== "completed" handling -- the parent should not render
//      CompletionSummary in non-completed terminals.  The widget defensively
//      returns null in that case as well.  Negative-space pin.
//   5. Initial render never moves focus.
//   6. Completion retains content-height sizing; deleted YAML/action styles
//      cannot silently restore the legacy 18rem floor.
//
// Source of truth:
//   - types/guided.ts:54-58 (TerminalState wire shape)
//   - stores/sessionStore.ts:116 + 572-583 (exitToFreeform parameterless)
//   - docs/superpowers/plans/2026-05-11-composer-guided-mode.md:4445
// ============================================================================

import { readFileSync } from "node:fs";
import { join } from "node:path";
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

// ── Common workspace owns artifacts ────────────────────────────────────────

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

// ── Single button renders with type="button" ───────────────────────────────

describe("CompletionSummary -- button identity", () => {
  it("renders only the freeform transition with type='button'", () => {
    render(<CompletionSummary terminal={COMPLETED_TERMINAL} />);
    const button = screen.getByRole("button", { name: /open freeform editor/i });
    expect(button).toHaveAttribute("type", "button");
    expect(screen.getAllByRole("button")).toEqual([button]);
  });
});

// ── Exit calls exitToFreeform ───────────────────────────────────────────────
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

// ── Non-completed terminal -> null ─────────────────────────────────────────

describe("CompletionSummary -- non-completed terminal guard (negative space)", () => {
  it("returns null when terminal.kind is 'exited_to_freeform'", () => {
    const { container } = render(
      <CompletionSummary terminal={EXITED_TERMINAL} />,
    );
    expect(container.firstChild).toBeNull();
  });
});

// ── No auto-focus on mount ───────────────────────────────────────────────────────

describe("CompletionSummary -- no auto-focus on initial render", () => {
  it("the exit button does not have focus immediately after render", () => {
    render(<CompletionSummary terminal={COMPLETED_TERMINAL} />);
    const saveBtn = screen.getByRole("button", {
      name: /open freeform editor/i,
    });
    expect(document.activeElement).not.toBe(saveBtn);
  });
});

describe("CompletionSummary -- content-height CSS", () => {
  it("does not retain deleted YAML/action selectors or a legacy minimum height", () => {
    const css = readFileSync(
      join(process.cwd(), "src/components/chat/guided/guided.css"),
      "utf8",
    );
    const completedRule = css.match(
      /\.chat-panel--completed\s*>\s*\.guided-completion\s*\{([^}]*)\}/,
    );

    expect(completedRule).not.toBeNull();
    expect(completedRule?.[1]).not.toMatch(/min-height|overflow/);
    expect(css).not.toMatch(
      /guided-completion-(?:yaml-container|pre|actions|edit-btn)/,
    );
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

// ============================================================================
// DecisionPanel.test.tsx — elspeth-cb0d4b8dba
//
// Widget tests for the chat's single "Awaiting your decision" panel. The
// projection (decisionPanelRows.ts) is tested with no DOM; this file pins the
// render contract: the region's accessible name, the per-row accessible
// button names ChatPanel's wiring tests and the a11y sweep query by, and
// that every action is a callback (the widget owns no store, no prompt).
// ============================================================================

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

import type { CompositionProposal, ValidationEntryDTO } from "@/types/index";

import { DecisionPanel, DecisionPanelLiveRegion, decisionAnnounceText } from "./DecisionPanel";
import type { DecisionRow } from "./decisionPanelRows";

const S1: ValidationEntryDTO = {
  component: "pipeline",
  message:
    "Consider adding error routing to a retention output — failed rows are currently discarded rather than kept for review.",
  severity: "low",
};

const blockerRow: DecisionRow = {
  kind: "blocker",
  id: "blocker:advisor_signoff_blocked:pipeline",
  code: "advisor_signoff_blocked",
  detail: "Completion advisory review did not clear after the available attempts.",
};

const suggestionRow: DecisionRow = {
  kind: "suggestion",
  id: "suggestion:0",
  suggestion: S1,
};

const pointerRow: DecisionRow = {
  kind: "pending_interpretation",
  id: "interpretation:event-1",
  eventId: "event-1",
  userTerm: "llm_prompt_template:colour_questions",
  affectedNodeId: "colour_questions",
};

function proposal(): CompositionProposal {
  return {
    id: "proposal-1",
    session_id: "session-1",
    tool_call_id: "call-1",
    tool_name: "patch_node_options",
    status: "pending",
    summary: "Change one option on colour_questions.",
    rationale: "Requested by the current composer turn.",
    affects: ["nodes"],
    arguments_redacted_json: {},
    base_state_id: null,
    committed_state_id: null,
    audit_event_id: null,
    created_at: "2026-09-13T11:16:03Z",
    updated_at: "2026-09-13T11:16:03Z",
  };
}

const phraseFor = (componentId: string | null): string =>
  componentId === "pipeline" ? "Pipeline" : (componentId ?? "Pipeline");
const stepLabelFor = (): string | null => null;

function renderPanel(
  overrides: Partial<Parameters<typeof DecisionPanel>[0]> = {},
): ReturnType<typeof render> & { handlers: ReturnType<typeof makeHandlers> } {
  const handlers = makeHandlers();
  const result = render(
    <DecisionPanel
      rows={[blockerRow, suggestionRow]}
      blockedVerbs={["save_for_review"]}
      count={2}
      proposals={[]}
      staleProposalIds={[]}
      proposalActionPendingIds={[]}
      isComposing={false}
      applyDisabled={false}
      applyDisabledReason={null}
      phraseFor={phraseFor}
      stepLabelFor={stepLabelFor}
      {...handlers}
      {...overrides}
    />,
  );
  return { ...result, handlers };
}

function makeHandlers() {
  return {
    onApplySuggestion: vi.fn(),
    onOpenChecks: vi.fn(),
    onShowInterpretation: vi.fn(),
    onAcceptProposal: vi.fn(),
    onRejectProposal: vi.fn(),
  };
}

describe("DecisionPanel", () => {
  it("renders nothing when there is nothing to decide", () => {
    const { container } = renderPanel({ rows: [], blockedVerbs: [], count: 0 });
    expect(container.firstChild).toBeNull();
  });

  it("is one named region whose heading carries the count", () => {
    renderPanel();
    const region = screen.getByRole("region", { name: "Awaiting your decision (2)" });
    expect(within(region).getByRole("heading", { level: 3 })).toHaveTextContent(
      "Awaiting your decision (2)",
    );
  });

  it("says which verb is blocked, in plain words, from the readiness axes", () => {
    renderPanel();
    expect(
      screen.getByText("Save for review is blocked. Run pipeline is still available."),
    ).toBeInTheDocument();
  });

  it("names both verbs when execution is withheld too", () => {
    renderPanel({ blockedVerbs: ["run", "save_for_review"] });
    expect(
      screen.getByText("Run pipeline and Save for review are blocked."),
    ).toBeInTheDocument();
  });

  it("lists the blocker's fixed backend detail verbatim", () => {
    renderPanel();
    expect(screen.getByText(blockerRow.detail)).toBeInTheDocument();
  });

  it("offers Apply on a suggestion and hands back the suggestion object", () => {
    const { handlers } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: /^Apply suggestion: Pipeline/ }),
    );
    expect(handlers.onApplySuggestion).toHaveBeenCalledExactlyOnceWith(S1);
  });

  it("offers Open checks and never a review-again action", () => {
    // A no-mutation compose cannot rewrite the durable completion gate, so a
    // "review again" button would read as a fix and do nothing (or worse,
    // publish the fully blocking shape on an advisor re-FLAG). Pinned absent.
    const { handlers } = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Open checks" }));
    expect(handlers.onOpenChecks).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: /review again/i })).toBeNull();
  });

  it("holds Apply closed behind the compose gate with a visible reason", () => {
    const { handlers } = renderPanel({
      applyDisabled: true,
      applyDisabledReason: "Connecting to the composer…",
    });
    const apply = screen.getByRole("button", { name: /^Apply suggestion/ });
    expect(apply).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Connecting to the composer…");
    fireEvent.click(apply);
    expect(handlers.onApplySuggestion).not.toHaveBeenCalled();
  });

  it("relabels Apply while a compose is in flight", () => {
    renderPanel({ isComposing: true, applyDisabled: true });
    expect(screen.getByRole("button", { name: /^Apply suggestion/ })).toHaveTextContent(
      "Applying...",
    );
  });

  it("points at a pending interpretation card by its user term", () => {
    const { handlers } = renderPanel({
      rows: [pointerRow],
      blockedVerbs: ["run", "save_for_review"],
      count: 1,
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: "Show interpretation review: llm_prompt_template:colour_questions",
      }),
    );
    expect(handlers.onShowInterpretation).toHaveBeenCalledExactlyOnceWith("event-1");
  });

  it("hosts the pending-proposals banner inside the panel unchanged", () => {
    const { handlers } = renderPanel({
      rows: [{ kind: "pending_proposal", id: "proposal:proposal-1", proposalId: "proposal-1" }],
      blockedVerbs: [],
      count: 1,
      proposals: [proposal()],
    });
    const panel = screen.getByRole("region", { name: "Awaiting your decision (1)" });
    const banner = within(panel).getByRole("region", { name: "Pending changes (1)" });
    fireEvent.click(
      within(banner).getByRole("button", {
        name: "Accept proposal: Change one option on colour_questions.",
      }),
    );
    expect(handlers.onAcceptProposal).toHaveBeenCalledExactlyOnceWith("proposal-1");
    // Nothing is blocked, so no verb sentence.
    expect(screen.queryByText(/is blocked|are blocked/)).toBeNull();
  });

  it("emits no tool or API vocabulary in its visible copy", () => {
    const { container } = renderPanel({
      rows: [blockerRow, suggestionRow, pointerRow],
      blockedVerbs: ["run", "save_for_review"],
      count: 3,
    });
    expect(container.textContent).not.toMatch(
      /preview_pipeline|patch_node_options|set_pipeline|advisor_signoff/,
    );
  });
});

describe("decisionAnnounceText", () => {
  it("is empty at zero and counts items otherwise", () => {
    expect(decisionAnnounceText(0)).toBe("");
    expect(decisionAnnounceText(1)).toBe("1 item needs your decision");
    expect(decisionAnnounceText(3)).toBe("3 items need your decision");
  });
});

describe("DecisionPanelLiveRegion", () => {
  it("stays mounted and empty when nothing needs a decision", () => {
    render(<DecisionPanelLiveRegion count={0} />);
    const region = screen.getByTestId("decision-panel-live-region");
    expect(region).toHaveAttribute("role", "status");
    expect(region).toHaveTextContent("");
  });

  it("lands the announcement after the mounting commit", async () => {
    render(<DecisionPanelLiveRegion count={2} />);
    expect(
      await screen.findByText("2 items need your decision"),
    ).toBeInTheDocument();
  });
});

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
import { act, fireEvent, render, screen, within } from "@testing-library/react";

import type { CompositionProposal, ValidationEntryDTO } from "@/types/index";

import { DecisionPanel, DecisionPanelLiveRegion, decisionAnnounceText } from "./DecisionPanel";
import type { DecisionRow } from "./decisionPanelRows";
import { actionableProposals } from "./actionableProposals";
import { projectDecisionRows } from "./decisionPanelRows";
import { makeComposition, makeValidationResult } from "@/test/composerFixtures";

const S1: ValidationEntryDTO = {
  component: "pipeline",
  message:
    "Consider adding error routing to a retention output — failed rows are currently discarded rather than kept for review.",
  severity: "low",
};

const blockerRow: Extract<DecisionRow, { kind: "blocker" }> = {
  kind: "blocker",
  id: "blocker:advisor_signoff_blocked:pipeline",
  code: "advisor_signoff_blocked",
  componentId: "pipeline",
  detail: "Completion advisory review did not clear after the available attempts.",
  suggestion: null,
  note: null,
};

function advisorBlockerRow(note: string | null): DecisionRow {
  return { ...blockerRow, note };
}

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
    onAcceptProposal: vi.fn(),
    onRejectProposal: vi.fn(),
  };
}

describe("DecisionPanel", () => {
  it("disables acceptance after a confirmed stale refusal but still permits rejection", () => {
    const pending = { ...proposal(), base_state_id: "older-state" };
    renderPanel({
      proposals: [pending], staleProposalIds: [pending.id],
      rows: [{ kind: "pending_proposal", id: "proposal:proposal-1", proposalId: pending.id }], count: 1,
    });
    expect(screen.getByRole("button", { name: /^Accept proposal:/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^Reject proposal:/ })).toBeEnabled();
    expect(screen.getByText(/Ask the composer to rebase/)).toBeInTheDocument();
  });

  it("does not preempt server admission for pending blob-effect retries", () => {
    const pending = { ...proposal(), tool_name: "update_blob", base_state_id: "earlier-state" };
    renderPanel({ proposals: [pending], rows: [{ kind: "pending_proposal", id: "proposal:proposal-1", proposalId: pending.id }], count: 1 });
    expect(screen.getByRole("button", { name: /^Accept proposal:/ })).toBeEnabled();
  });
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
      screen.getByRole("button", { name: /^Apply optional suggestion: Pipeline/ }),
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
    const apply = screen.getByRole("button", { name: /^Apply optional suggestion/ });
    expect(apply).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Connecting to the composer…");
    fireEvent.click(apply);
    expect(handlers.onApplySuggestion).not.toHaveBeenCalled();
  });

  it("relabels only the Apply that started the compose (L10)", () => {
    const second: DecisionRow = {
      kind: "suggestion",
      id: "suggestion:1",
      suggestion: { component: "other_step", message: "Second nudge.", severity: "low" } as ValidationEntryDTO,
    };
    const props = { rows: [blockerRow, suggestionRow, second], count: 3 };
    const { rerender, handlers } = renderPanel(props);
    const [first, other] = screen.getAllByRole("button", { name: /^Apply optional suggestion/ });
    // Several suggestions: filled secondary, never bare (L3).
    expect(first).toHaveClass("btn-compact");
    expect(first).not.toHaveClass("btn-primary");
    fireEvent.click(first);
    rerender(
      <DecisionPanel
        {...props}
        blockedVerbs={["save_for_review"]}
        proposals={[]}
        staleProposalIds={[]}
        proposalActionPendingIds={[]}
        isComposing
        applyDisabled
        applyDisabledReason={null}
        phraseFor={phraseFor}
        stepLabelFor={stepLabelFor}
        {...handlers}
      />,
    );
    expect(first).toHaveTextContent("Applying...");
    expect(other).toHaveTextContent("Apply");
    expect(other).not.toHaveTextContent("Applying...");
    expect(other).toBeDisabled();
  });

  it("does not claim a compose no row started", () => {
    renderPanel({ isComposing: true, applyDisabled: true });
    expect(screen.getByRole("button", { name: /^Apply optional suggestion/ })).toHaveTextContent(/^Apply$/);
  });

  it("makes a lone Apply primary and marks the suggestion optional in words (L3, L6)", () => {
    renderPanel();
    expect(screen.getByRole("button", { name: /^Apply optional suggestion/ })).toHaveClass("btn-primary");
    expect(screen.getByText("Optional")).toBeInTheDocument();
  });

  it("drafts a question about a blocker and sends nothing (D4)", () => {
    const onAskAboutBlocker = vi.fn();
    const { handlers } = renderPanel({ onAskAboutBlocker });
    fireEvent.click(screen.getByRole("button", {
      name: "Ask the composer about this: Completion advisory review did not clear after the available attempts.",
    }));
    expect(onAskAboutBlocker).toHaveBeenCalledWith(blockerRow.kind === "blocker" ? blockerRow.detail : "", "pipeline", null);
    expect(handlers.onApplySuggestion).not.toHaveBeenCalled();
  });

  it("holds Ask closed, with a visible reason, while the input holds a draft it would replace", () => {
    const onAskAboutBlocker = vi.fn();
    renderPanel({ onAskAboutBlocker, askDisabledReason: "Send or clear your draft before asking about a blocker." });
    expect(screen.getByRole("button", { name: /^Ask the composer about this/ })).toBeDisabled();
    expect(screen.getByText("Send or clear your draft before asking about a blocker.")).toBeInTheDocument();
  });

  it("renders no Ask button where there is no input to draft into", () => {
    renderPanel();
    expect(screen.queryByRole("button", { name: /^Ask the composer about this/ })).toBeNull();
  });

  it("hosts interpretation controls once without a pointer action", () => {
    renderPanel({
      rows: [pointerRow],
      blockedVerbs: ["run", "save_for_review"],
      count: 1,
      interpretationContent: <section aria-label="Interpretation approvals"><button>Approve interpretation</button></section>,
    });
    expect(screen.getAllByRole("button", { name: "Approve interpretation" })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: /Show interpretation/ })).toBeNull();
  });

  it("renders proposal actions as a native decision row without a nested region", () => {
    const { handlers } = renderPanel({
      rows: [{ kind: "pending_proposal", id: "proposal:proposal-1", proposalId: "proposal-1" }],
      blockedVerbs: [],
      count: 1,
      proposals: [proposal()],
    });
    const panel = screen.getByRole("region", { name: "Awaiting your decision (1)" });
    expect(within(panel).queryByRole("region")).toBeNull();
    const banner = within(panel).getByRole("listitem");
    fireEvent.click(
      within(banner).getByRole("button", {
        name: "Accept proposal: Change one option on colour_questions.",
      }),
    );
    expect(handlers.onAcceptProposal).toHaveBeenCalledExactlyOnceWith("proposal-1");
    // Nothing is blocked, so no verb sentence.
    expect(screen.queryByText(/is blocked|are blocked/)).toBeNull();
  });

  it("confirms rejection and preserves the row when cancelled", () => {
    const { handlers } = renderPanel({
      rows: [{ kind: "pending_proposal", id: "proposal:proposal-1", proposalId: "proposal-1" }],
      count: 1,
      proposals: [proposal()],
    });
    fireEvent.click(screen.getByRole("button", { name: `Reject proposal: ${proposal().summary}` }));
    fireEvent.click(screen.getByRole("button", { name: "Keep open" }));
    expect(handlers.onRejectProposal).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: `Reject proposal: ${proposal().summary}` }));
    fireEvent.click(screen.getByRole("button", { name: "Reject proposal" }));
    expect(handlers.onRejectProposal).toHaveBeenCalledExactlyOnceWith("proposal-1");
  });

  it("disables both proposal actions while its request is pending", () => {
    const { handlers } = renderPanel({
      rows: [{ kind: "pending_proposal", id: "proposal:proposal-1", proposalId: "proposal-1" }],
      count: 1,
      proposals: [proposal()],
      proposalActionPendingIds: ["proposal-1"],
    });
    const accept = screen.getByRole("button", { name: `Accept proposal: ${proposal().summary}` });
    const reject = screen.getByRole("button", { name: `Reject proposal: ${proposal().summary}` });
    expect(accept).toBeDisabled();
    expect(reject).toBeDisabled();
    fireEvent.click(accept);
    fireEvent.click(reject);
    expect(handlers.onAcceptProposal).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("forgets rejection confirmation when its proposal disappears", () => {
    const props = {
      rows: [{ kind: "pending_proposal" as const, id: "proposal:proposal-1", proposalId: "proposal-1" }],
      blockedVerbs: [], count: 1, proposals: [proposal()],
      staleProposalIds: [], proposalActionPendingIds: [],
      isComposing: false, applyDisabled: false, applyDisabledReason: null,
      phraseFor, stepLabelFor, ...makeHandlers(),
    };
    const { rerender } = render(<DecisionPanel {...props} />);
    fireEvent.click(screen.getByRole("button", { name: `Reject proposal: ${proposal().summary}` }));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    rerender(<DecisionPanel {...props} rows={[]} count={0} proposals={[]} />);
    rerender(<DecisionPanel {...props} />);
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("restores focus to Open checks or the input callback after a focused row disappears", () => {
    const onEmptyFocus = vi.fn();
    const props = {
      rows: [suggestionRow, blockerRow], blockedVerbs: [], count: 2,
      proposals: [], staleProposalIds: [], proposalActionPendingIds: [],
      isComposing: false, applyDisabled: false, applyDisabledReason: null,
      phraseFor, stepLabelFor, ...makeHandlers(), onEmptyFocus,
    };
    const { rerender } = render(<DecisionPanel {...props} />);
    screen.getByRole("button", { name: /^Apply optional suggestion/ }).focus();
    rerender(<DecisionPanel {...props} rows={[blockerRow]} count={1} />);
    expect(screen.getByRole("button", { name: "Open checks" })).toHaveFocus();
    rerender(<DecisionPanel {...props} rows={[]} count={0} />);
    expect(onEmptyFocus).toHaveBeenCalledOnce();
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
  it("announces changed projected suggestions at equal count but ignores reordering", () => {
    vi.useFakeTimers();
    const project = (suggestions: ValidationEntryDTO[]) => projectDecisionRows({
      validationResult: makeValidationResult({ readiness: {
        ...makeValidationResult().readiness,
        completion_ready: false,
      } }),
      compositionState: makeComposition(1, { validation_suggestions: suggestions }),
      pendingInterpretations: [], proposals: [],
    });
    const initial = project([S1, { ...S1, message: "Review the source fields." }]);
    const replacement = project([S1, { ...S1, message: "Review the output fields." }]);
    const reordered = project([{ ...S1, message: "Review the output fields." }, S1]);
    const renderAnnouncement = (projection: ReturnType<typeof project>) => (
      <DecisionPanelLiveRegion count={projection.count} decisionIds={projection.rows.map((row) => row.id)} />
    );
    try {
      const { rerender } = render(renderAnnouncement(initial));
      const region = screen.getByRole("status");
      act(() => vi.runAllTimers());
      expect(region).toHaveTextContent("2 items need your decision");
      rerender(renderAnnouncement(replacement));
      expect(region).toBeEmptyDOMElement();
      act(() => vi.runAllTimers());
      expect(region).toHaveTextContent("2 items need your decision");
      const observer = new MutationObserver(() => {});
      observer.observe(region, { childList: true, characterData: true, subtree: true });
      rerender(renderAnnouncement(reordered));
      act(() => vi.runAllTimers());
      expect(observer.takeRecords()).toHaveLength(0);
      observer.disconnect();
    } finally {
      vi.useRealTimers();
    }
  });
  it("announces equal-count arrivals once without remounting the live region", () => {
    vi.useFakeTimers();
    try {
      const { rerender } = render(<DecisionPanelLiveRegion count={1} decisionIds={["p1"]} />);
      const region = screen.getByRole("status");
      act(() => vi.runAllTimers());
      expect(region).toHaveTextContent("1 item needs your decision");
      rerender(<DecisionPanelLiveRegion count={1} decisionIds={["p2"]} />);
      expect(screen.getByRole("status")).toBe(region);
      expect(region).toBeEmptyDOMElement();
      act(() => vi.runAllTimers());
      expect(region).toHaveTextContent("1 item needs your decision");
      const observer = new MutationObserver(() => {});
      observer.observe(region, { childList: true, characterData: true, subtree: true });
      rerender(<DecisionPanelLiveRegion count={1} decisionIds={["p2"]} />);
      act(() => vi.runAllTimers());
      expect(observer.takeRecords()).toHaveLength(0);
      observer.disconnect();
    } finally {
      vi.useRealTimers();
    }
  });
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

// Ruling 2026-09-22 (elspeth-032ec69c41): the advisory reviewer's own words
// reach the user here, labelled and as plain text. They are provider output:
// never markdown and never a link. The Ask button drafts it as labelled,
// untrusted evidence for the composer.
describe("DecisionPanel reviewer's note", () => {
  it("renders the reviewer's note under an advisor blocker as plain text", () => {
    renderPanel({ rows: [advisorBlockerRow("Choose per-branch sinks **or** best_effort.")], count: 1 });

    const note = screen.getByTestId("decision-panel-reviewer-note");
    expect(note).toHaveTextContent("Reviewer's note");
    expect(note).toHaveTextContent("Choose per-branch sinks **or** best_effort.");
    expect(note.querySelector("strong")).toBeNull();
    expect(note.querySelector("a")).toBeNull();
  });

  it("says the words are the advisor's and unverified", () => {
    renderPanel({ rows: [advisorBlockerRow("x")], count: 1 });
    expect(screen.getByTestId("decision-panel-reviewer-note")).toHaveTextContent("not verified by ELSPETH");
  });

  it("renders no note block when note is null", () => {
    renderPanel({ rows: [advisorBlockerRow(null)], count: 1 });
    expect(screen.queryByTestId("decision-panel-reviewer-note")).toBeNull();
  });

  it("passes the displayed note to the ask-the-composer draft", () => {
    const onAskAboutBlocker = vi.fn();
    renderPanel({ rows: [advisorBlockerRow("SECRET_NOTE")], count: 1, onAskAboutBlocker });

    fireEvent.click(screen.getByRole("button", { name: /Ask the composer about this/ }));

    expect(onAskAboutBlocker).toHaveBeenCalledTimes(1);
    expect(onAskAboutBlocker).toHaveBeenCalledWith(blockerRow.kind === "blocker" ? blockerRow.detail : "", "pipeline", "SECRET_NOTE");
  });

  it("keeps the note out of the button's accessible name", () => {
    renderPanel({ rows: [advisorBlockerRow("SECRET_NOTE")], count: 1, onAskAboutBlocker: vi.fn() });
    expect(screen.getByRole("button", { name: /Ask the composer about this/ }).getAttribute("aria-label")).not.toContain("SECRET_NOTE");
  });
});

describe("actionableProposals", () => {
  it("preserves pending rejection eligibility without mutating input", () => {
    const pending = proposal();
    const stale = { ...proposal(), id: "stale" };
    const rejected = { ...proposal(), id: "rejected", status: "rejected" as const };
    const committed = { ...proposal(), id: "committed", status: "committed" as const };
    const input = Object.freeze([pending, stale, rejected, committed]);
    expect(actionableProposals(input)).toEqual([pending, stale]);
    expect(input).toHaveLength(4);
  });
});

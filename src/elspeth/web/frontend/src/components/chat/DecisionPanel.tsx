// ============================================================================
// DecisionPanel.tsx — the chat's one "Awaiting your decision" surface
// (elspeth-cb0d4b8dba).
//
// Operator ruling (2026-09-13): a state that blocks the user's progress, and
// the affordance that clears it, sit where the user is working — above the
// chat input — never two tab changes away in the Pipeline → Checks sub-tab.
// Measured on session 94f6f00c: the advisor gate withheld completion, the
// chat got a fixed note with no action, Save for review was disabled with the
// reason in a hover title, the Checks badge read "1 warning", and the only
// button that changed anything (the validator's S1 suggestion's Apply) lived
// in ChecksView.
//
// This panel is a DUMB RENDER of decisionPanelRows.projectDecisionRows plus
// callbacks. It owns no store and builds no prompt: ChatPanel computes the
// rows and the compose gate, and sends the canned prompts through
// useComposer.sendMessage, so every click that changes the pipeline is a
// planner call (AGENTS.md § Composer invariants).
//
// Phase 1 scope: the pending-proposals banner is HOSTED inside this region
// unchanged (its accessible names are pinned by ChatPanel.test.tsx and the
// proposals E2E spec) and pending interpretation cards get pointer rows that
// scroll to the existing card. Phase 2 migrates both in full; the row kinds
// are already in place for it.
//
// Contract (test-pinned, InlineSourceFallbackPrompt style):
//   * root is `<section role="region" aria-label="Awaiting your decision (N)">`;
//   * Apply buttons are named `Apply suggestion: <humanised text>`;
//   * pointer buttons are named `Show interpretation review: <user term>`;
//   * `Open checks` always renders. On a narrow viewport the Checks tab lives
//     behind the Pipeline view tab, which this event does not switch; the
//     panel itself is in the Compose view so the information is never hidden.
//   * no tool or API vocabulary in visible copy (F-3).
//
// There is deliberately no "review again" button. A compose turn that
// mutates nothing saves no composition-state row, so the durable completion
// gate cannot be rewritten by it, and an advisor FLAG on a turn with no
// runtime preflight publishes the fully blocking shape — the button would
// read as a fix and do the opposite (see lib/suggestionPrompts.ts). The
// clearing actions are the mutating ones: Apply, resolving a review card,
// accepting a proposal, or the user's next real change.
// ============================================================================

import { useEffect, useState } from "react";

import { Button } from "@/components/ui";
import { humaniseValidationSuggestion } from "@/lib/validationHumaniser";
import type { CompositionProposal, ValidationEntryDTO } from "@/types/index";

import { PendingProposalsBanner } from "./PendingProposalsBanner";
import type { BlockedVerb, DecisionRow } from "./decisionPanelRows";

export interface DecisionPanelProps {
  rows: readonly DecisionRow[];
  blockedVerbs: readonly BlockedVerb[];
  count: number;
  proposals: CompositionProposal[];
  staleProposalIds: string[];
  proposalActionPendingIds: string[];
  /** A compose is in flight: Apply reads "Applying..." like the side rail. */
  isComposing: boolean;
  /** Apply is held closed (composing, or the compose wall-clock has not
   *  landed at boot — the bootstrap race the side rail's SuggestionList
   *  documents). */
  applyDisabled: boolean;
  /** Visible reason for the gate while it is closed and not composing. */
  applyDisabledReason: string | null;
  phraseFor: (componentId: string | null) => string;
  stepLabelFor: (componentId: string) => string | null;
  onApplySuggestion: (suggestion: ValidationEntryDTO) => void;
  onOpenChecks: () => void;
  onShowInterpretation: (eventId: string) => void;
  onAcceptProposal: (proposalId: string) => void;
  onRejectProposal: (proposalId: string) => void;
}

/** Plain-words sentence for the withheld verbs, in action-bar order. */
export function blockedVerbsSentence(blockedVerbs: readonly BlockedVerb[]): string | null {
  const run = blockedVerbs.includes("run");
  const save = blockedVerbs.includes("save_for_review");
  if (run && save) return "Run pipeline and Save for review are blocked.";
  if (run) return "Run pipeline is blocked.";
  if (save) return "Save for review is blocked. Run pipeline is still available.";
  return null;
}

/** Announce text for the live region ("" at zero — clears without announcing). */
export function decisionAnnounceText(count: number): string {
  if (count === 0) return "";
  return count === 1
    ? "1 item needs your decision"
    : `${count} items need your decision`;
}

/**
 * Persistent, ALWAYS-mounted `role="status"` live region for the panel —
 * the PendingProposalsLiveRegion idiom: the panel returns null when empty,
 * so a live role on its own section would enter the DOM with its content
 * already present (the WAI-ARIA unreliable pattern). Mount silent, land the
 * text one commit later so every announcement is a content mutation inside
 * an already-inserted node.
 */
export function DecisionPanelLiveRegion({ count }: { count: number }): JSX.Element {
  const announceText = decisionAnnounceText(count);
  const [renderedText, setRenderedText] = useState("");
  useEffect(() => {
    setRenderedText(announceText);
  }, [announceText]);
  return (
    <div
      role="status"
      className="visually-hidden"
      data-testid="decision-panel-live-region"
    >
      {renderedText}
    </div>
  );
}

export function DecisionPanel({
  rows,
  blockedVerbs,
  count,
  proposals,
  staleProposalIds,
  proposalActionPendingIds,
  isComposing,
  applyDisabled,
  applyDisabledReason,
  phraseFor,
  stepLabelFor,
  onApplySuggestion,
  onOpenChecks,
  onShowInterpretation,
  onAcceptProposal,
  onRejectProposal,
}: DecisionPanelProps): JSX.Element | null {
  if (count === 0) return null;

  const verbSentence = blockedVerbsSentence(blockedVerbs);
  const listRows = rows.filter((row) => row.kind !== "pending_proposal");
  const hasProposals = rows.some((row) => row.kind === "pending_proposal");
  const gateReason = !isComposing && applyDisabled ? applyDisabledReason : null;
  const title = `Awaiting your decision (${count})`;

  return (
    <section
      role="region"
      aria-label={title}
      data-testid="decision-panel"
      className="decision-panel"
    >
      <header className="decision-panel-header">
        <h3 className="decision-panel-title">{title}</h3>
        {verbSentence !== null && (
          <p className="decision-panel-verbs">{verbSentence}</p>
        )}
      </header>
      {gateReason !== null && (
        // Visible + announced reason the buttons below are disabled; the
        // per-button title alone is not reliably read by screen readers.
        <div role="status" className="decision-panel-gate">
          {gateReason}
        </div>
      )}
      {listRows.length > 0 && (
        <ul className="decision-panel-list">
          {listRows.map((row) => (
            <li key={row.id} className={`decision-panel-item decision-panel-item--${row.kind}`}>
              {row.kind === "blocker" && (
                <span className="decision-panel-item-text">{row.detail}</span>
              )}
              {row.kind === "suggestion" && (
                <SuggestionItem
                  suggestion={row.suggestion}
                  phraseFor={phraseFor}
                  stepLabelFor={stepLabelFor}
                  isComposing={isComposing}
                  applyDisabled={applyDisabled}
                  gateReason={gateReason}
                  onApply={onApplySuggestion}
                />
              )}
              {row.kind === "pending_interpretation" && (
                <>
                  <span className="decision-panel-item-text">
                    Interpretation review pending
                    {row.userTerm !== null && (
                      <>
                        {" "}
                        for <em>{row.userTerm}</em>
                      </>
                    )}
                    .
                  </span>
                  <Button
                    compact
                    className="decision-panel-show-btn"
                    aria-label={`Show interpretation review: ${row.userTerm ?? row.eventId}`}
                    onClick={() => onShowInterpretation(row.eventId)}
                  >
                    Show
                  </Button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      {hasProposals && (
        <PendingProposalsBanner
          proposals={proposals}
          staleProposalIds={staleProposalIds}
          proposalActionPendingIds={proposalActionPendingIds}
          onAccept={onAcceptProposal}
          onReject={onRejectProposal}
        />
      )}
      <div className="decision-panel-actions">
        <Button compact className="decision-panel-checks-btn" onClick={onOpenChecks}>
          Open checks
        </Button>
      </div>
    </section>
  );
}

interface SuggestionItemProps {
  suggestion: ValidationEntryDTO;
  phraseFor: (componentId: string | null) => string;
  stepLabelFor: (componentId: string) => string | null;
  isComposing: boolean;
  applyDisabled: boolean;
  gateReason: string | null;
  onApply: (suggestion: ValidationEntryDTO) => void;
}

function SuggestionItem({
  suggestion,
  phraseFor,
  stepLabelFor,
  isComposing,
  applyDisabled,
  gateReason,
  onApply,
}: SuggestionItemProps): JSX.Element {
  const finding = humaniseValidationSuggestion(suggestion, phraseFor, stepLabelFor);
  const phrase = phraseFor(suggestion.component);
  return (
    <>
      <span className="decision-panel-item-text">
        <strong>{phrase}:</strong> {finding.headline}
      </span>
      <Button
        variant="bare"
        className="decision-panel-apply-btn"
        disabled={applyDisabled}
        title={gateReason ?? undefined}
        aria-label={`Apply suggestion: ${phrase}: ${finding.headline}`}
        onClick={() => {
          if (applyDisabled) return;
          onApply(suggestion);
        }}
      >
        {isComposing ? "Applying..." : "Apply"}
      </Button>
    </>
  );
}

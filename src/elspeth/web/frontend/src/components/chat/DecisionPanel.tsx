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
// rows and the compose gate. Suggestion and source actions use provider-backed
// composition; proposal and interpretation approvals use their existing APIs.
//
// Pending proposals, interpretation reviews and source fallback actions
// have one interactive home here; anchored transcript cards remain history.
//
// Contract (test-pinned, InlineSourceFallbackPrompt style):
//   * root is `<section role="region" aria-label="Awaiting your decision (N)">`;
//   * Apply buttons are named `Apply optional suggestion: <humanised text>`
//     and the row says "Optional" in visible text. Observed live (session
//     46224626): with Run blocked by a policy no edit could clear, Apply was
//     the only button in the box and read as the fix. A suggestion never
//     clears a block by itself, and the row must say so;
//   * a blocker row offers `Ask the composer about this: <detail>`, which
//     DRAFTS a question into the chat input and sends nothing (ruling D4).
//     It is not a fix button: a blocker has no server-vetted remedy text;
//   * `Open checks` always renders. The workspace handles the view intent by
//     revealing Pipeline on narrow screens, then selecting and focusing Checks.
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

import { type JSX, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui";
import { humaniseValidationSuggestion } from "@/lib/validationHumaniser";
import type { CompositionProposal, ValidationEntryDTO } from "@/types/index";

import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { actionableProposals } from "./actionableProposals";
import { proposalEffectLabel } from "./proposalEffectLabel";
import type { BlockedVerb, DecisionRow } from "./decisionPanelRows";

export interface DecisionPanelProps {
  rows: readonly DecisionRow[];
  blockedVerbs: readonly BlockedVerb[];
  count: number;
  proposals: CompositionProposal[];
  staleProposalIds: string[];
  proposalActionPendingIds: string[];
  /** A compose is in flight. Only the row whose Apply started it reads
   *  "Applying..."; the rest stay "Apply", disabled. */
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
  /** Draft a question about a blocker into the chat input. Undefined where
   *  there is no freeform input to draft into: the button is not rendered. */
  onAskAboutBlocker?: (detail: string, componentId: string | null) => void;
  /** Visible reason Ask is held closed (the input already holds a draft the
   *  click would replace), or null while it is open. */
  askDisabledReason?: string | null;
  onOpenChecks: () => void;
  interpretationContent?: ReactNode;
  renderSourceFallback?: (candidateText: string) => ReactNode;
  onEmptyFocus?: () => void;
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
 * the panel returns null when empty,
 * so a live role on its own section would enter the DOM with its content
 * already present (the WAI-ARIA unreliable pattern). Mount silent, land the
 * text one commit later so every announcement is a content mutation inside
 * an already-inserted node.
 */
export function DecisionPanelLiveRegion({ count, decisionIds }: {
  count: number;
  decisionIds?: readonly string[];
}): JSX.Element {
  const announceText = decisionAnnounceText(count);
  const [renderedText, setRenderedText] = useState("");
  const previousIds = useRef<readonly string[]>([]);
  const identity = JSON.stringify(decisionIds ?? []);
  useEffect(() => {
    const ids: string[] = JSON.parse(identity);
    const arrived = ids.some((id) => !previousIds.current.includes(id));
    previousIds.current = ids;
    if (arrived) {
      setRenderedText("");
      const timer = window.setTimeout(() => setRenderedText(announceText), 0);
      return () => window.clearTimeout(timer);
    }
    setRenderedText(announceText);
  }, [announceText, identity]);
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
  onAskAboutBlocker,
  askDisabledReason = null,
  onOpenChecks,
  interpretationContent,
  renderSourceFallback,
  onEmptyFocus,
  onAcceptProposal,
  onRejectProposal,
}: DecisionPanelProps): JSX.Element | null {
  const [rejectConfirmId, setRejectConfirmId] = useState<string | null>(null);
  // The row whose Apply started the compose in flight; a typed turn leaves it
  // null, so no row claims a compose it did not start.
  const [applyingRowId, setApplyingRowId] = useState<string | null>(null);
  useEffect(() => {
    if (!isComposing) setApplyingRowId(null);
  }, [isComposing]);
  const panelRef = useRef<HTMLElement>(null);
  const focusedElement = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    if (focusedElement.current !== null && !focusedElement.current.isConnected) {
      focusedElement.current = null;
      if (document.activeElement === document.body) {
        const fallback = panelRef.current?.querySelector<HTMLButtonElement>(".decision-panel-checks-btn");
        if (fallback) fallback.focus();
        else onEmptyFocus?.();
      }
    }
  });
  const actionable = actionableProposals(proposals);
  const rejectTarget = actionable.find((proposal) => proposal.id === rejectConfirmId);
  useEffect(() => {
    if (rejectConfirmId !== null && rejectTarget === undefined) {
      setRejectConfirmId(null);
    }
  }, [rejectConfirmId, rejectTarget]);
  if (count === 0) return null;

  const verbSentence = blockedVerbsSentence(blockedVerbs);
  const listRows = rows.filter((row) => row.kind !== "pending_interpretation");
  const gateReason = !isComposing && applyDisabled ? applyDisabledReason : null;
  const title = `Awaiting your decision (${count})`;
  const suggestionCount = listRows.filter((row) => row.kind === "suggestion").length;

  return (
    <section
      ref={panelRef}
      onFocusCapture={(event) => { focusedElement.current = event.target; }}
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
                <>
                  <span className="decision-panel-item-text">{row.detail}{row.suggestion !== null && <> {row.suggestion}</>}</span>
                  {row.note !== null && (
                    // The advisory reviewer's own words (elspeth-032ec69c41).
                    // Labelled as unverified and rendered as TEXT: this is
                    // provider output, so no markdown renderer and no
                    // dangerouslySetInnerHTML may ever touch it.
                    <div className="decision-panel-reviewer-note" data-testid="decision-panel-reviewer-note">
                      <span className="decision-panel-reviewer-note-label">
                        {"Reviewer's note (the advisor's own words — not verified by ELSPETH):"}
                      </span>
                      <p className="decision-panel-reviewer-note-text">{row.note}</p>
                    </div>
                  )}
                  {onAskAboutBlocker !== undefined && (
                    <Button
                      compact
                      className="decision-panel-ask-btn"
                      disabled={askDisabledReason !== null}
                      aria-label={`Ask the composer about this: ${row.detail}`}
                      onClick={() => onAskAboutBlocker(row.detail, row.componentId)}
                    >
                      Ask the composer about this
                    </Button>
                  )}
                </>
              )}
              {row.kind === "suggestion" && (
                <SuggestionItem
                  suggestion={row.suggestion}
                  phraseFor={phraseFor}
                  stepLabelFor={stepLabelFor}
                  applying={isComposing && applyingRowId === row.id}
                  prominent={suggestionCount === 1}
                  applyDisabled={applyDisabled}
                  gateReason={gateReason}
                  onApply={(suggestion) => {
                    setApplyingRowId(row.id);
                    onApplySuggestion(suggestion);
                  }}
                />
              )}
              {row.kind === "pending_proposal" && actionable.filter((proposal) => proposal.id === row.proposalId).map((proposal) => (
                <ProposalItem
                  key={proposal.id}
                  proposal={proposal}
                  isStale={staleProposalIds.includes(proposal.id)}
                  isBusy={proposalActionPendingIds.includes(proposal.id)}
                  onAccept={onAcceptProposal}
                  onReject={setRejectConfirmId}
                />
              ))}
              {row.kind === "inline_source_fallback" && renderSourceFallback?.(row.candidateText)}
            </li>
          ))}
        </ul>
      )}
      {askDisabledReason !== null && onAskAboutBlocker !== undefined && listRows.some((row) => row.kind === "blocker") && (
        <div role="status" className="decision-panel-gate">
          {askDisabledReason}
        </div>
      )}
      {interpretationContent}
      {rejectTarget && (
        <ConfirmDialog
          title="Reject proposal"
          message="The composer's proposed change will be discarded. You can ask the composer to revise the proposal afterwards."
          confirmLabel="Reject proposal"
          cancelLabel="Keep open"
          variant="danger"
          onConfirm={() => {
            onRejectProposal(rejectTarget.id);
            setRejectConfirmId(null);
          }}
          onCancel={() => setRejectConfirmId(null)}
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

function ProposalItem({ proposal, isBusy, isStale, onAccept, onReject }: {
  proposal: CompositionProposal;
  isBusy: boolean;
  isStale: boolean;
  onAccept: (id: string) => void;
  onReject: (id: string) => void;
}): JSX.Element {
  return (
    <>
      <div className="decision-panel-item-text">
        <p>{proposal.summary}</p>
        {isStale && <p>Ask the composer to rebase or revise this proposal before accepting it. You can still reject it.</p>}
        {proposal.affects.length > 0 && <p>Affects: {proposal.affects.map(proposalEffectLabel).join(", ")}</p>}
      </div>
      <div className="decision-panel-proposal-actions">
        <Button variant="primary" disabled={isBusy || isStale} aria-label={`Accept proposal: ${proposal.summary}`} onClick={() => onAccept(proposal.id)}>Accept</Button>
        <Button variant="danger" disabled={isBusy} aria-label={`Reject proposal: ${proposal.summary}`} onClick={() => onReject(proposal.id)}>Reject</Button>
      </div>
    </>
  );
}

interface SuggestionItemProps {
  suggestion: ValidationEntryDTO;
  phraseFor: (componentId: string | null) => string;
  stepLabelFor: (componentId: string) => string | null;
  /** This row's Apply started the compose in flight. */
  applying: boolean;
  /** The only suggestion listed: primary. Several: secondary, so the panel
   *  is not a column of primaries. Never bare — the 2026-09-13 ruling is
   *  that the fix affordance must not be the quietest thing in the box. */
  prominent: boolean;
  applyDisabled: boolean;
  gateReason: string | null;
  onApply: (suggestion: ValidationEntryDTO) => void;
}

function SuggestionItem({
  suggestion,
  phraseFor,
  stepLabelFor,
  applying,
  prominent,
  applyDisabled,
  gateReason,
  onApply,
}: SuggestionItemProps): JSX.Element {
  const finding = humaniseValidationSuggestion(suggestion, phraseFor, stepLabelFor);
  const phrase = phraseFor(suggestion.component);
  return (
    <>
      <span className="decision-panel-item-text">
        <span className="decision-panel-optional">Optional</span>{" "}
        <strong>{phrase}:</strong> {finding.headline}
      </span>
      <Button
        compact
        variant={prominent ? "primary" : "secondary"}
        className="decision-panel-apply-btn"
        disabled={applyDisabled}
        title={gateReason ?? undefined}
        aria-label={`Apply optional suggestion: ${phrase}: ${finding.headline}`}
        onClick={() => {
          if (applyDisabled) return;
          onApply(suggestion);
        }}
      >
        {applying ? "Applying..." : "Apply"}
      </Button>
    </>
  );
}

import { useMemo, useState } from "react";

import type { CompositionProposal, CompositionState, ToolCall } from "@/types/api";
import { Button } from "@/components/ui";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { ArgumentFields, buildProposalDiff, ProposalChanges } from "./ProposalDiff";
import {
  TOOL_CALL_DESCRIPTIONS,
  describeToolCall,
  toolCallOutcomeLabelParts,
} from "./toolCallDescriptions";

/**
 * A button-triggered tooltip explaining what a composer tool call does in
 * general — not what it did to the current pipeline. Hover or keyboard-focus
 * the "i" button to reveal. Uses CSS-only show/hide; no JS state.
 */
function ToolCallInfo({
  toolName,
  describedById,
}: {
  toolName: string;
  describedById: string;
}) {
  return (
    <span className="tool-call-info">
      <Button
        variant="bare"
        className="tool-call-info-trigger"
        aria-label={`What does ${toolName} do?`}
        aria-describedby={describedById}
      >
        i
      </Button>
      <span
        id={describedById}
        role="tooltip"
        className="tool-call-info-bubble"
      >
        <strong className="tool-call-info-bubble-name">{toolName}</strong>
        <span className="tool-call-info-bubble-body">
          {describeToolCall(toolName)}
        </span>
      </span>
    </span>
  );
}

interface ToolCallCardProps {
  toolCall: ToolCall;
  proposal: CompositionProposal | null;
  /**
   * Current composition state — the "before" side of the proposal diff
   * (elspeth-10f76f9250). Only consulted for pending, non-stale proposals;
   * for anything else the current state is no longer the state the proposal
   * targeted and the card falls back to structured argument fields.
   */
  currentState?: CompositionState | null;
  isStale?: boolean;
  isBusy?: boolean;
  onAccept: (proposalId: string) => void;
  onReject: (proposalId: string) => void;
}

export function ToolCallCard({
  toolCall,
  proposal,
  currentState = null,
  isStale = false,
  isBusy = false,
  onAccept,
  onReject,
}: ToolCallCardProps) {
  const [rejectConfirmOpen, setRejectConfirmOpen] = useState(false);
  // Fragment-level before/after projection of the proposal against the
  // current pipeline. null = not derivable (unknown tool, malformed args, no
  // state) → structured argument fields render instead. Computed before the
  // no-proposal early return to keep hooks unconditional.
  const diffEntries = useMemo(() => {
    if (proposal === null || proposal.status !== "pending" || isStale) {
      return null;
    }
    return buildProposalDiff(
      proposal.tool_name,
      proposal.arguments_redacted_json,
      currentState,
    );
  }, [proposal, isStale, currentState]);
  if (!proposal) {
    // Proposal-less calls carry a server-derived outcome stamped by
    // GET /messages from the Tier-1 tool rows (elspeth-f5e6723133). In
    // auto_commit mode mutations execute without proposal rows, so without
    // the stamp every applied change rendered as a read. "Looked up" is
    // reserved for names in the read-only description map; a "completed"
    // stamp on any other name (durable blob/interpretation writes) renders
    // "Completed", and an unstamped non-read-only row renders "Ran" — no
    // stamp means no server evidence of success, so no success is claimed.
    const outcome = toolCall.outcome;
    const { prefix, qualifier } = toolCallOutcomeLabelParts(
      toolCall.function.name,
      outcome,
    );
    // Sentence primary, identifier secondary — the ComposingIndicator ruling
    // (ComposingIndicator.tsx:320-346) applied to the settled ribbon. The
    // evidential prefix stays attached to the PRIMARY line: a failed mutation
    // rendered as a bare sentence would claim the mutation happened. An
    // unmapped name has no honest sentence (describeToolCall's fallback is
    // generic), so it keeps the raw-name label and no secondary.
    const sentence: string | undefined =
      TOOL_CALL_DESCRIPTIONS[toolCall.function.name];
    const appliedVersion =
      outcome === "applied" && typeof toolCall.applied_state_version === "number"
        ? toolCall.applied_state_version
        : null;
    return (
      <div
        className={`tool-call-ribbon${outcome ? ` tool-call-ribbon--${outcome}` : ""}`}
      >
        <ToolCallInfo
          toolName={toolCall.function.name}
          describedById={`tool-call-info-${toolCall.id}`}
        />
        <span className="tool-call-ribbon-text">
          {prefix}: {sentence ?? toolCall.function.name}
          {qualifier}
        </span>
        {sentence !== undefined && (
          <code className="tool-call-ribbon-name">{toolCall.function.name}</code>
        )}
        {appliedVersion !== null && (
          <code
            className="tool-call-ribbon-version"
            title={`Pipeline advanced to version ${appliedVersion}`}
          >
            v{appliedVersion}
          </code>
        )}
      </div>
    );
  }

  const isPending = proposal.status === "pending";
  const proposalSentence: string | undefined =
    TOOL_CALL_DESCRIPTIONS[proposal.tool_name];
  const headingPrefix =
    proposal.status === "pending"
      ? "Proposed"
      : proposal.status === "committed"
        ? "Applied"
        : "Rejected";
  const heading = `${headingPrefix}: ${proposalSentence ?? proposal.tool_name}`;

  return (
    <article className={`tool-call-card tool-call-card--${proposal.status}`}>
      <header className="tool-call-card-header">
        <strong>{heading}</strong>
        {proposalSentence !== undefined && (
          <code className="tool-call-ribbon-name">{proposal.tool_name}</code>
        )}
        <ToolCallInfo
          toolName={proposal.tool_name}
          describedById={`tool-call-info-${proposal.id}`}
        />
        {proposal.audit_event_id && (
          <code className="tool-call-audit-id">
            audit {proposal.audit_event_id.slice(0, 8)}
          </code>
        )}
      </header>
      <p className="tool-call-summary">{proposal.summary}</p>
      <p className="tool-call-rationale">
        <strong>Why:</strong> {proposal.rationale}
      </p>
      {proposal.affects.length > 0 && (
        <p className="tool-call-affects">
          <strong>Affects:</strong> {proposal.affects.join(", ")}
        </p>
      )}
      {/* Primary change surface (elspeth-10f76f9250): a before/after diff of
          the affected pipeline fragment when derivable, otherwise structured
          per-argument fields. The raw JSON stays available behind the
          details expander below in both cases. */}
      {diffEntries !== null ? (
        <ProposalChanges diff={diffEntries} />
      ) : (
        <ArgumentFields args={proposal.arguments_redacted_json} />
      )}
      <details className="tool-call-details">
        <summary>View arguments (JSON)</summary>
        <pre
          tabIndex={0}
          aria-label="Tool call arguments (scrollable)"
        >{JSON.stringify(proposal.arguments_redacted_json, null, 2)}</pre>
      </details>
      {isStale && (
        <p className="tool-call-stale">
          Stale proposal. Ask the composer to rebase or revise this proposal.
        </p>
      )}
      {isPending && !isStale && (
        <div className="tool-call-actions">
          <Button
            variant="primary"
            onClick={() => onAccept(proposal.id)}
            aria-label={`Accept proposal: ${proposal.summary}`}
            disabled={isBusy}
            className="btn-small"
          >
            Accept
          </Button>
          <Button
            variant="danger"
            onClick={() => setRejectConfirmOpen(true)}
            aria-label={`Reject proposal: ${proposal.summary}`}
            disabled={isBusy}
            className="btn-small"
          >
            Reject
          </Button>
        </div>
      )}
      {rejectConfirmOpen && proposal && (
        <ConfirmDialog
          title="Reject proposal"
          message="The composer's proposed change will be discarded. You can ask the composer to revise the proposal afterwards."
          confirmLabel="Reject proposal"
          cancelLabel="Keep open"
          variant="danger"
          onConfirm={() => {
            onReject(proposal.id);
            setRejectConfirmOpen(false);
          }}
          onCancel={() => setRejectConfirmOpen(false)}
        />
      )}
    </article>
  );
}

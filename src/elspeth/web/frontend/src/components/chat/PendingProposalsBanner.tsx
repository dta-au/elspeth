import { useState } from "react";

import type { CompositionProposal } from "@/types/api";
import { Button } from "@/components/ui";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";

interface PendingProposalsBannerProps {
  proposals: CompositionProposal[];
  staleProposalIds: string[];
  proposalActionPendingIds: string[];
  onAccept: (proposalId: string) => void;
  onReject: (proposalId: string) => void;
}

/**
 * Single authority for which proposals the banner can act on: pending and
 * not stale. Exported so ChatPanel's arrival mechanics — the live-region
 * announce text and the dock's scroll-to-banner trigger
 * (elspeth-2d1cf8908c) — derive from the same predicate the banner renders
 * from, never a second definition.
 */
export function actionableProposals(
  proposals: readonly CompositionProposal[],
  staleProposalIds: readonly string[],
): CompositionProposal[] {
  return proposals.filter(
    (p) => p.status === "pending" && !staleProposalIds.includes(p.id),
  );
}

/** Announce text for the live region ("" at zero — clears without announcing). */
export function pendingProposalsAnnounceText(count: number): string {
  if (count === 0) return "";
  return count === 1
    ? "1 pending change needs your approval"
    : `${count} pending changes need your approval`;
}

export interface PendingProposalsLiveRegionProps {
  proposals: CompositionProposal[];
  staleProposalIds: string[];
}

/**
 * Persistent, ALWAYS-mounted `role="status"` live region for the pending
 * proposals banner (elspeth-2d1cf8908c).
 *
 * The banner itself returns null when nothing is actionable, so a live-region
 * role on its <section> would enter the DOM *with its content already present*
 * on the 0→1 transition — the WAI-ARIA-documented unreliable pattern (a polite
 * live region must pre-exist its content for the change to be announced).
 * Same idiom as AcknowledgementLiveRegion: ChatPanel mounts this node
 * regardless of pending count and only the text mutates, so "announce on
 * appearance" (0→1) and "announce on count change" (N→M) are both reliable
 * content mutations inside a stable node.
 */
export function PendingProposalsLiveRegion({
  proposals,
  staleProposalIds,
}: PendingProposalsLiveRegionProps) {
  return (
    <div
      role="status"
      className="visually-hidden"
      data-testid="pending-proposals-live-region"
    >
      {pendingProposalsAnnounceText(
        actionableProposals(proposals, staleProposalIds).length,
      )}
    </div>
  );
}

/**
 * Persistent banner above the chat input that surfaces pending composer
 * proposals with inline Accept/Reject controls.
 *
 * Pending proposals are also rendered inside ToolCallCard on the originating
 * assistant message, but that surface is buried inside the message's
 * collapsible "Tool calls (N)" panel and the user must scroll up from the
 * agent's most recent prose to reach it. The banner keeps the action visible
 * regardless of scroll position, which matches the user's mental model: when
 * the agent says "this change needs approval", the button to approve it is
 * right there.
 *
 * Stale proposals (base_state_id no longer matches the current committed
 * state) are excluded — the user cannot accept them, and the ToolCallCard
 * surface already shows the rebase prompt for them.
 */
export function PendingProposalsBanner({
  proposals,
  staleProposalIds,
  proposalActionPendingIds,
  onAccept,
  onReject,
}: PendingProposalsBannerProps) {
  const [rejectConfirmId, setRejectConfirmId] = useState<string | null>(null);
  const actionable = actionableProposals(proposals, staleProposalIds);
  if (actionable.length === 0) {
    return null;
  }
  const rejectTarget =
    rejectConfirmId !== null
      ? actionable.find((p) => p.id === rejectConfirmId)
      : undefined;

  return (
    <section
      className="pending-proposals-banner"
      aria-label={`Pending changes (${actionable.length})`}
    >
      <header className="pending-proposals-banner-header">
        <strong>
          Pending change{actionable.length === 1 ? "" : "s"} (
          {actionable.length})
        </strong>
        <span className="pending-proposals-banner-help">
          The composer prepared {actionable.length === 1 ? "a change" : "changes"}{" "}
          but needs your approval before applying.
        </span>
      </header>
      <ul className="pending-proposals-banner-list">
        {actionable.map((proposal) => {
          const isBusy = proposalActionPendingIds.includes(proposal.id);
          return (
            <li
              key={proposal.id}
              className="pending-proposals-banner-item"
            >
              <div className="pending-proposals-banner-item-body">
                <p className="pending-proposals-banner-summary">
                  {proposal.summary}
                </p>
                {proposal.affects.length > 0 && (
                  <p className="pending-proposals-banner-affects">
                    Affects: {proposal.affects.join(", ")}
                  </p>
                )}
              </div>
              <div className="pending-proposals-banner-actions">
                <Button
                  variant="primary"
                  onClick={() => onAccept(proposal.id)}
                  aria-label={`Accept proposal: ${proposal.summary}`}
                  disabled={isBusy}
                >
                  Accept
                </Button>
                <Button
                  variant="danger"
                  onClick={() => setRejectConfirmId(proposal.id)}
                  aria-label={`Reject proposal: ${proposal.summary}`}
                  disabled={isBusy}
                >
                  Reject
                </Button>
              </div>
            </li>
          );
        })}
      </ul>
      {rejectTarget && (
        <ConfirmDialog
          title="Reject proposal"
          message="The composer's proposed change will be discarded. You can ask the composer to revise the proposal afterwards."
          confirmLabel="Reject proposal"
          cancelLabel="Keep open"
          variant="danger"
          onConfirm={() => {
            onReject(rejectTarget.id);
            setRejectConfirmId(null);
          }}
          onCancel={() => setRejectConfirmId(null)}
        />
      )}
    </section>
  );
}

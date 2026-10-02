import type { CompositionProposal, CompositionState } from "@/types/api";

/** Shared eligibility rule for pending proposal projection and actions. */
export function actionableProposals(
  proposals: readonly CompositionProposal[],
): CompositionProposal[] {
  // Obsolete proposals still need an explicit Reject action.
  return proposals.filter((proposal) => proposal.status === "pending");
}

/** Canonical pipelines use their own draft/base contract at settlement. */
export function proposalBaseChanged(
  proposal: CompositionProposal,
  currentState: CompositionState | null,
): boolean {
  return proposal.status === "pending" && proposal.pipeline_metadata == null &&
    proposal.base_state_id !== null && currentState !== null &&
    proposal.base_state_id !== currentState.id;
}

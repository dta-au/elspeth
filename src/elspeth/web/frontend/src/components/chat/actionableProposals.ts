import type { CompositionProposal } from "@/types/api";

/** Shared eligibility rule for pending proposal projection and actions. */
export function actionableProposals(
  proposals: readonly CompositionProposal[],
  staleProposalIds: readonly string[],
): CompositionProposal[] {
  return proposals.filter(
    (proposal) => proposal.status === "pending" && !staleProposalIds.includes(proposal.id),
  );
}

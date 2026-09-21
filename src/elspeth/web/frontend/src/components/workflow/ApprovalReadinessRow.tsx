import { type JSX, useEffect, useState } from "react";
import { Button } from "@/components/ui";
import { approvalForState, useMailboxStore } from "@/stores/mailboxStore";
import { WorkflowRequestDialog } from "./WorkflowRequestDialog";

/** Request controls sit beside the state they affect; execution remains server-authoritative. */
export function ApprovalReadinessRow({ sessionId, stateId, pendingApproval }: {
  sessionId: string;
  stateId: string;
  pendingApproval?: string | null;
}): JSX.Element | null {
  const summary = useMailboxStore((state) => state.summary);
  const approvals = useMailboxStore((state) => state.sent?.approvals ?? null);
  const loadSent = useMailboxStore((state) => state.loadSent);
  const [requestKind, setRequestKind] = useState<"approval" | "review" | null>(null);
  useEffect(() => {
    if (summary?.governance === "on") void loadSent();
  }, [summary?.governance, summary?.decisions_unseen, loadSent]);
  if (summary?.governance !== "on") return null;
  const latest = approvalForState(approvals, sessionId, stateId);
  const status = pendingApproval ?? (latest === null ? "No approval requested for this state."
    : latest.decision === null ? "Approval requested; awaiting a decision."
      : latest.decision === "approved" ? "Latest request approved. Execution checks the compiled binding again."
        : latest.decision === "rejected" ? "Latest request rejected. A new approval is required."
          : latest.decision === "superseded" ? "The request was superseded by a newer state."
            : "Approval withdrawn.");
  return <section className="workflow-approval-readiness" aria-label="Workflow approval readiness">
    <p>{status}</p>
    <div className="workflow-actions">
      <Button onClick={() => setRequestKind("approval")}>Request approval</Button>
      <Button onClick={() => setRequestKind("review")}>Request review</Button>
    </div>
    {requestKind !== null && <WorkflowRequestDialog kind={requestKind} sessionId={sessionId} stateId={stateId} onClose={() => setRequestKind(null)} />}
  </section>;
}

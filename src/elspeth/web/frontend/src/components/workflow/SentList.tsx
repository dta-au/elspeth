import { Button } from "@/components/ui";
import type { ApprovalView, MailboxSent } from "@/types/workflow";

export function isUnreadDecision(row: ApprovalView, identityId: string): boolean {
  if (row.requested_by_identity_id !== identityId || row.decision === null || row.decision === "superseded" || row.decision_seen_at !== null) return false;
  if (row.decision === "revoked" && row.revocation_actor_kind === "identity" && row.revoked_by_identity_id === identityId) return false;
  return true;
}

function approvalState(row: ApprovalView): string {
  if (row.decision === "revoked") return row.revocation_actor_kind === "identity" && row.revoked_by_identity_id === row.requested_by_identity_id
    ? "Withdrawn" : "Revoked";
  if (row.decision === "superseded") return "Superseded";
  if (row.decision === "approved") return "Approved";
  if (row.decision === "rejected") return "Rejected";
  return "Waiting";
}

export function SentList({ sent, identityId, onOpen }: { sent: MailboxSent; identityId: string; onOpen: (approvalId: string) => void }): JSX.Element {
  if (sent.approvals.length === 0 && sent.reviews.length === 0) return <p>You have not sent a request.</p>;
  const approvals = [...sent.approvals].sort((a, b) => Date.parse(b.requested_at) - Date.parse(a.requested_at) || b.approval_id.localeCompare(a.approval_id));
  const reviews = [...sent.reviews].sort((a, b) => Date.parse(b.request.requested_at) - Date.parse(a.request.requested_at) || b.request.request_id.localeCompare(a.request.request_id));
  return <div className="workflow-mailbox-sent">
    <ul className="workflow-mailbox-list">
      {approvals.map((row) => <li key={row.approval_id}>
        <Button variant="bare" className="workflow-mailbox-item" onClick={() => onOpen(row.approval_id)} aria-label={`Approval from ${row.approver_identity_id}`}>
          <strong>{approvalState(row)}{row.decided_by_identity_id ? ` by ${row.decided_by_identity_id}` : ` · to ${row.approver_identity_id}`}</strong>
          {isUnreadDecision(row, identityId) && <span className="workflow-new">New</span>}
          <span>{row.decision_note ?? row.request_note ?? "No note"}</span>
          <small>{row.session_id} · {row.state_id}{row.decided_at ? ` · ${row.decided_at}` : ""}</small>
        </Button>
      </li>)}
    </ul>
    {reviews.map(({ request, attestations }) => <section key={request.request_id} className="workflow-sent-review" data-testid="mailbox-sent-review">
      <h3>Review request to {request.reviewer_identity_id ?? "any reviewer"}: {request.open ? "Waiting" : "Reviewed"}</h3>
      <p>{request.request_note ?? "No request note"}</p>
      <small>{request.session_id} · {request.state_id}</small>
      {attestations.map((row) => <p key={row.attestation_id}>{row.verdict === "changes_requested" ? "Changes requested" : row.verdict === "withdrawn" ? "Withdrawn" : "Signed off"} by {row.reviewer_identity_id} · {row.attested_at}{row.note ? ` · ${row.note}` : ""}</p>)}
    </section>)}
  </div>;
}

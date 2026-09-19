import { Button } from "@/components/ui";
import type { ApprovalView, MailboxInbox, ReviewRequestView } from "@/types/workflow";

export type InboxItem = { kind: "approval"; approval: ApprovalView } | { kind: "review"; review: ReviewRequestView };

/** The server decides eligibility. Sorting only puts the addressed request first. */
export function inboxItems(inbox: MailboxInbox, identityId: string): InboxItem[] {
  const approvals = inbox.approvals.map((approval): InboxItem => ({ kind: "approval", approval }));
  approvals.sort((a, b) => {
    if (a.kind !== "approval" || b.kind !== "approval") return 0;
    const addressedA = a.approval.approver_identity_id === identityId ? 1 : 0;
    const addressedB = b.approval.approver_identity_id === identityId ? 1 : 0;
    return addressedB - addressedA || Date.parse(b.approval.requested_at) - Date.parse(a.approval.requested_at)
      || b.approval.approval_id.localeCompare(a.approval.approval_id);
  });
  const reviews = inbox.reviews.map((review): InboxItem => ({ kind: "review", review }));
  reviews.sort((a, b) => {
    if (a.kind !== "review" || b.kind !== "review") return 0;
    const addressedA = a.review.reviewer_identity_id === identityId ? 1 : 0;
    const addressedB = b.review.reviewer_identity_id === identityId ? 1 : 0;
    return addressedB - addressedA || Date.parse(b.review.requested_at) - Date.parse(a.review.requested_at)
      || b.review.request_id.localeCompare(a.review.request_id);
  });
  return [...approvals, ...reviews];
}

export function InboxList({ inbox, identityId, onSelect }: { inbox: MailboxInbox; identityId: string; onSelect: (item: InboxItem) => void }): JSX.Element {
  const items = inboxItems(inbox, identityId);
  if (items.length === 0) return <p>No requests need your decision.</p>;
  return <ul className="workflow-mailbox-list">
    {items.map((item) => {
      const row = item.kind === "approval" ? item.approval : item.review;
      const id = item.kind === "approval" ? item.approval.approval_id : item.review.request_id;
      return <li key={`${item.kind}:${id}`}>
        <Button variant="bare" className="workflow-mailbox-item" onClick={() => onSelect(item)}>
          <strong>{item.kind === "approval" ? "Approval" : "Review"} requested by {row.requested_by_identity_id}</strong>
          <span>{row.request_note ?? "No request note"}</span>
          <small>{row.session_id} · {row.state_id}</small>
        </Button>
      </li>;
    })}
  </ul>;
}

import { describe, expect, it } from "vitest";
import { inboxItems } from "./InboxList";
import type { ApprovalView, MailboxInbox } from "@/types/workflow";

function approval(id: string, addressedTo: string): ApprovalView {
  return { approval_id: id, session_id: "s", state_id: "t", binding: {}, requested_by_identity_id: "alice", approver_identity_id: addressedTo, requested_at: "2026-09-19T00:00:00Z", decided_at: null, decision: null, request_note: null, decision_seen_at: null, decided_by_identity_id: null, decision_note: null, revoked_by_identity_id: null, revocation_actor_kind: null, revocation_event_id: null };
}

describe("inbox order", () => {
  it("shows addressed approvals first without filtering other eligible approvals", () => {
    const inbox: MailboxInbox = { approvals: [approval("other", "carol"), approval("mine", "bob")], reviews: [] };
    const rows = inboxItems(inbox, "bob");
    expect(rows.map((row) => row.kind === "approval" ? row.approval.approval_id : "review")).toEqual(["mine", "other"]);
  });
});

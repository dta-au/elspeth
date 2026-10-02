import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as workflow from "@/api/workflow";
import { useMailboxStore } from "@/stores/mailboxStore";
import type { ApprovalView } from "@/types/workflow";
import { ApprovalReadinessRow } from "./ApprovalReadinessRow";

vi.mock("@/api/workflow", () => ({ fetchMailboxSent: vi.fn() }));

function approval(id: string, decision: ApprovalView["decision"]): ApprovalView {
  return { approval_id: id, session_id: "s", state_id: "t", binding: {}, requested_by_identity_id: "alice", approver_identity_id: "bob", requested_at: "2026-09-19T00:00:00Z", decided_at: decision === null ? null : "2026-09-19T01:00:00Z", decision, request_note: null, decision_seen_at: null, decided_by_identity_id: decision === null ? null : "bob", decision_note: null, revoked_by_identity_id: null, revocation_actor_kind: null, revocation_event_id: null };
}

describe("ApprovalReadinessRow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMailboxStore.getState().reset();
  });

  it("shows the later rejection even when an older request was approved", async () => {
    vi.mocked(workflow.fetchMailboxSent).mockResolvedValue({ approvals: [approval("a", "approved"), approval("b", "rejected")], reviews: [] });
    useMailboxStore.setState({ summary: { governance: "on", roles: [], approvals_to_decide: 0, reviews_to_attest: 0, decisions_unseen: 1 } });
    render(<ApprovalReadinessRow sessionId="s" stateId="t" />);
    expect(await screen.findByText("Latest request rejected. A new approval is required.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Request approval" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Request review" })).toBeInTheDocument();
  });

  it("shows execute-time refusal over the request summary", async () => {
    vi.mocked(workflow.fetchMailboxSent).mockResolvedValue({ approvals: [], reviews: [] });
    useMailboxStore.setState({ summary: { governance: "on", roles: [], approvals_to_decide: 0, reviews_to_attest: 0, decisions_unseen: 0 } });
    render(<ApprovalReadinessRow sessionId="s" stateId="t" pendingApproval="Approval is required before this pipeline can run." />);
    expect(screen.getByText("Approval is required before this pipeline can run.")).toBeInTheDocument();
  });
});

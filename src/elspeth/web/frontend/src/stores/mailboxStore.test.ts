import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as workflow from "@/api/workflow";
import { useAuthStore } from "./authStore";
import { approvalForState, badgeCount, MAILBOX_POLL_INTERVAL_MS, useMailboxStore } from "./mailboxStore";
import type { ApprovalView, MailboxSummary } from "@/types/workflow";

vi.mock("@/api/workflow", () => ({ fetchMailboxSummary: vi.fn(), fetchMailboxInbox: vi.fn(), fetchMailboxSent: vi.fn(), markApprovalSeen: vi.fn(), decideApproval: vi.fn(), attestReview: vi.fn() }));

const summary: MailboxSummary = { governance: "on", roles: ["approver"], approvals_to_decide: 2, reviews_to_attest: 1, decisions_unseen: 1 };
function approval(id: string, requestedAt: string): ApprovalView {
  return { approval_id: id, session_id: "s", state_id: "t", binding: {}, requested_by_identity_id: "alice", approver_identity_id: "bob", requested_at: requestedAt, decided_at: null, decision: null, request_note: null, decision_seen_at: null, decided_by_identity_id: null, decision_note: null, revoked_by_identity_id: null, revocation_actor_kind: null, revocation_event_id: null };
}

describe("mailbox store", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMailboxStore.getState().reset();
    vi.mocked(workflow.fetchMailboxSummary).mockResolvedValue(summary);
  });
  afterEach(() => {
    useMailboxStore.getState().reset();
    useAuthStore.setState({ token: null, user: null });
    vi.useRealTimers();
  });

  it("counts attention only with governance on", () => {
    expect(badgeCount(summary)).toBe(4);
    expect(badgeCount({ ...summary, governance: "off" })).toBe(0);
  });

  it("chooses the newest exact-state request through timezone, microseconds and ID ties", () => {
    const rows = [approval("z", "2026-09-19T09:00:00Z"), approval("a", "2026-09-19T09:00:00.000002Z"), approval("b", "2026-09-19T09:00:00.000002Z"), { ...approval("later-other-state", "2026-09-20T09:00:00Z"), state_id: "other" }];
    expect(approvalForState(rows, "s", "t")?.approval_id).toBe("b");
    expect(approvalForState(rows, "s", "missing")).toBeNull();
  });

  it("starts only one timer, and logout clears state and stops later polls", async () => {
    vi.useFakeTimers();
    useAuthStore.setState({ token: "test-token" });
    useMailboxStore.getState().startPolling();
    useMailboxStore.getState().startPolling();
    await vi.waitFor(() => expect(workflow.fetchMailboxSummary).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(MAILBOX_POLL_INTERVAL_MS);
    expect(workflow.fetchMailboxSummary).toHaveBeenCalledTimes(2);
    useAuthStore.setState({ token: null });
    expect(useMailboxStore.getState().summary).toBeNull();
    await vi.advanceTimersByTimeAsync(MAILBOX_POLL_INTERVAL_MS);
    expect(workflow.fetchMailboxSummary).toHaveBeenCalledTimes(2);
  });

  it("does not restore a late summary response after logout", async () => {
    let resolve!: (value: MailboxSummary) => void;
    vi.mocked(workflow.fetchMailboxSummary).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    useAuthStore.setState({ token: "test-token" });
    const pending = useMailboxStore.getState().refreshSummary();
    useAuthStore.setState({ token: null });
    resolve(summary);
    await pending;
    expect(useMailboxStore.getState().summary).toBeNull();
  });

  it("removes a concurrently decided request even when the inbox refresh fails", async () => {
    useMailboxStore.setState({ inbox: { approvals: [approval("a1", "2026-09-19T00:00:00Z")], reviews: [] } });
    vi.mocked(workflow.decideApproval).mockRejectedValue({
      status: 409,
      error_type: "approval_already_decided",
      current_state: "rejected",
      detail: "Already decided",
    });
    vi.mocked(workflow.fetchMailboxInbox).mockRejectedValue(new Error("temporary read failure"));

    expect(await useMailboxStore.getState().decide("a1", "approved", null)).toBe("already_decided");
    expect(useMailboxStore.getState().inbox?.approvals).toEqual([]);
    expect(useMailboxStore.getState().error).toBe("This request was already rejected.");
    expect(workflow.fetchMailboxInbox).toHaveBeenCalledTimes(1);
    expect(workflow.fetchMailboxSummary).toHaveBeenCalledTimes(1);
  });
});

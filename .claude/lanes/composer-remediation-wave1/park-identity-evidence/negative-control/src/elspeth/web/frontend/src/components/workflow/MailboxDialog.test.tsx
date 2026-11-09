import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as workflow from "@/api/workflow";
import { useAuthStore } from "@/stores/authStore";
import { useMailboxStore } from "@/stores/mailboxStore";
import type { ApprovalView, MailboxInbox, ReviewRequestView } from "@/types/workflow";
import { MailboxDialog } from "./MailboxDialog";

vi.mock("@/api/workflow", () => ({ fetchMailboxInbox: vi.fn(), fetchMailboxSent: vi.fn(), fetchMailboxSummary: vi.fn(), fetchWorkflowInspect: vi.fn(), fetchWorkflowAuditView: vi.fn(), decideApproval: vi.fn(), attestReview: vi.fn(), markApprovalSeen: vi.fn() }));

const approval: ApprovalView = { approval_id: "a1", session_id: "s1", state_id: "t1", binding: {}, requested_by_identity_id: "alice", approver_identity_id: "carol", requested_at: "2026-09-19T00:00:00Z", decided_at: null, decision: null, request_note: "Please check the inputs", decision_seen_at: null, decided_by_identity_id: null, decision_note: null, revoked_by_identity_id: null, revocation_actor_kind: null, revocation_event_id: null };
const review: ReviewRequestView = { request_id: "r1", session_id: "s2", state_id: "t2", requested_by_identity_id: "alice", reviewer_identity_id: null, requested_at: "2026-09-19T01:00:00Z", cancelled_at: null, request_note: "Check the joins", open: true };

describe("MailboxDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMailboxStore.getState().reset();
    useAuthStore.setState({ user: { user_id: "bob", username: "bob", display_name: null, email: null, groups: [], dev_admin: false } });
    vi.mocked(workflow.fetchMailboxInbox).mockResolvedValue({ approvals: [approval], reviews: [review] });
    vi.mocked(workflow.fetchMailboxSent).mockResolvedValue({ approvals: [], reviews: [] });
    vi.mocked(workflow.fetchMailboxSummary).mockResolvedValue({ governance: "on", roles: ["approver", "reviewer"], approvals_to_decide: 1, reviews_to_attest: 1, decisions_unseen: 0 });
    vi.mocked(workflow.fetchWorkflowInspect).mockImplementation(async (sessionId, stateId) => ({ session_id: sessionId, state_id: stateId, access_log_id: "audit", composition_snapshot: {}, yaml: "source: csv\n", attestations: [] }));
    vi.mocked(workflow.fetchWorkflowAuditView).mockResolvedValue({ identity_ids: ["alice"], truncated: false, runs: [], approvals: [], attestations: [], auth_events: [] });
  });

  it("lets an eligible non-addressed approver inspect the exact state and decide", async () => {
    vi.mocked(workflow.decideApproval).mockResolvedValue({ ...approval, decision: "approved" });
    render(<MailboxDialog onClose={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: /Approval requested by alice/ }));
    expect(await screen.findByTestId("mailbox-inspect-yaml")).toHaveTextContent("source: csv");
    expect(workflow.fetchWorkflowInspect).toHaveBeenCalledWith("s1", "t1");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(workflow.decideApproval).toHaveBeenCalledWith("a1", "approved", ""));
  });

  it("shows the server's winning decision after a concurrent loss", async () => {
    let resolveInbox!: (value: MailboxInbox) => void;
    const refreshedInbox = new Promise<MailboxInbox>((resolve) => { resolveInbox = resolve; });
    vi.mocked(workflow.fetchMailboxInbox)
      .mockResolvedValueOnce({ approvals: [approval], reviews: [review] })
      .mockReturnValueOnce(refreshedInbox);
    vi.mocked(workflow.decideApproval).mockRejectedValue({ status: 409, error_type: "approval_already_decided", current_state: "rejected", detail: "Already decided" });
    render(<MailboxDialog onClose={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: /Approval requested by alice/ }));
    await screen.findByTestId("mailbox-inspect-yaml");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This request was already rejected.");
    expect(workflow.fetchMailboxInbox).toHaveBeenCalledTimes(2);
    expect(workflow.fetchMailboxSummary).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("tabpanel", { name: "Inbox" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approval requested by alice/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Review requested by alice/ })).toBeInTheDocument();
    resolveInbox({ approvals: [], reviews: [review] });
    await waitFor(() => expect(useMailboxStore.getState().inbox?.approvals).toEqual([]));
  });

  it("lists sent decisions and stamps them only when opened", async () => {
    const decided = { ...approval, decision: "rejected" as const, decided_by_identity_id: "carol", decision_note: "Change source", decided_at: "2026-09-19T02:00:00Z" };
    vi.mocked(workflow.fetchMailboxSent).mockResolvedValue({ approvals: [decided], reviews: [] });
    vi.mocked(workflow.markApprovalSeen).mockResolvedValue({ ...decided, decision_seen_at: "2026-09-19T03:00:00Z" });
    render(<MailboxDialog onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("tab", { name: "Sent" }));
    expect(await screen.findByText("Change source")).toBeInTheDocument();
    expect(workflow.markApprovalSeen).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Approval from carol" }));
    await waitFor(() => expect(workflow.markApprovalSeen).toHaveBeenCalledWith("a1"));
  });

  it("does not fetch the audit view until the Audit folder is opened", async () => {
    render(<MailboxDialog onClose={vi.fn()} />);
    await screen.findByRole("button", { name: /Approval requested by alice/ });
    expect(workflow.fetchWorkflowAuditView).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("tab", { name: "Audit" }));
    await waitFor(() => expect(workflow.fetchWorkflowAuditView).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("tabpanel", { name: "Audit" })).toBeInTheDocument();
  });

  it("surfaces a refusal as a message rather than an empty audit table", async () => {
    vi.mocked(workflow.fetchWorkflowAuditView).mockRejectedValue(new Error("Not found"));
    render(<MailboxDialog onClose={vi.fn()} />);
    await screen.findByRole("button", { name: /Approval requested by alice/ });
    await userEvent.click(screen.getByRole("tab", { name: "Audit" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /^Runs/ })).not.toBeInTheDocument();
  });

  it("cycles the three folders with the arrow keys", async () => {
    render(<MailboxDialog onClose={vi.fn()} />);
    const inboxTab = await screen.findByRole("tab", { name: "Inbox" });
    inboxTab.focus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Audit" })).toHaveAttribute("aria-selected", "true");
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Inbox" })).toHaveAttribute("aria-selected", "true");
  });
});

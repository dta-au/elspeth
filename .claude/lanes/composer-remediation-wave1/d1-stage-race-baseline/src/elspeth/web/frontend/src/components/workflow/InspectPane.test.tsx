import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as workflow from "@/api/workflow";
import { useMailboxStore } from "@/stores/mailboxStore";
import type { ApprovalView, WorkflowInspect } from "@/types/workflow";
import { InspectPane } from "./InspectPane";
import type { InboxItem } from "./InboxList";

vi.mock("@/api/workflow", () => ({ fetchWorkflowInspect: vi.fn(), decideApproval: vi.fn(), attestReview: vi.fn(), fetchMailboxInbox: vi.fn(), fetchMailboxSummary: vi.fn() }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function approval(id: string, stateId = "t1"): InboxItem {
  const row: ApprovalView = { approval_id: id, session_id: "s1", state_id: stateId, binding: {}, requested_by_identity_id: "alice", approver_identity_id: "bob", requested_at: "2026-09-19T00:00:00Z", decided_at: null, decision: null, request_note: "<b>plain</b>", decision_seen_at: null, decided_by_identity_id: null, decision_note: null, revoked_by_identity_id: null, revocation_actor_kind: null, revocation_event_id: null };
  return { kind: "approval", approval: row };
}

function inspect(stateId: string): WorkflowInspect {
  return { session_id: "s1", state_id: stateId, access_log_id: "audit-1", composition_snapshot: {}, yaml: "source: csv\n", attestations: [] };
}

describe("InspectPane", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMailboxStore.getState().reset();
  });

  it("enables a decision only after the matching frozen inspection loads and renders notes as text", async () => {
    const pending = deferred<WorkflowInspect>();
    vi.mocked(workflow.fetchWorkflowInspect).mockReturnValue(pending.promise);
    render(<InspectPane item={approval("a1")} onBack={vi.fn()} onDone={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(screen.getByText(/Request note:/)).toHaveTextContent("<b>plain</b>");
    expect(document.querySelector("b")).toBeNull();
    await act(async () => pending.resolve(inspect("t1")));
    expect(screen.getByTestId("mailbox-inspect-yaml")).toHaveTextContent("source: csv");
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Note"), "Missing an owner");
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
  });

  it("does not use a late inspection for another request, even in the same session", async () => {
    const first = deferred<WorkflowInspect>();
    const second = deferred<WorkflowInspect>();
    vi.mocked(workflow.fetchWorkflowInspect).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { rerender } = render(<InspectPane item={approval("a1", "t1")} onBack={vi.fn()} onDone={vi.fn()} />);
    rerender(<InspectPane item={approval("a2", "t2")} onBack={vi.fn()} onDone={vi.fn()} />);
    await act(async () => first.resolve(inspect("t1")));
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    await act(async () => second.resolve(inspect("t2")));
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(workflow.fetchWorkflowInspect).toHaveBeenLastCalledWith("s1", "t2");
  });

  it("refuses an echoed state mismatch", async () => {
    vi.mocked(workflow.fetchWorkflowInspect).mockResolvedValue(inspect("wrong"));
    render(<InspectPane item={approval("a1")} onBack={vi.fn()} onDone={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("not the version this request names");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
  });
});

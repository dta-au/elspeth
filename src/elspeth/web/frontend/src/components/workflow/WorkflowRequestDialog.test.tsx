import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as workflow from "@/api/workflow";
import { useMailboxStore } from "@/stores/mailboxStore";
import { WorkflowRequestDialog } from "./WorkflowRequestDialog";

vi.mock("@/api/workflow", () => ({ fetchApproverDirectory: vi.fn(), requestApproval: vi.fn(), requestReview: vi.fn(), fetchMailboxSent: vi.fn(), fetchMailboxSummary: vi.fn() }));

describe("WorkflowRequestDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMailboxStore.getState().reset();
    vi.mocked(workflow.fetchApproverDirectory).mockResolvedValue({ approvers: [{ identity_id: "bob", username: "Bob" }, { identity_id: "carol", username: "Carol" }], suggested_identity_ids: ["carol"] });
    vi.mocked(workflow.fetchMailboxSent).mockResolvedValue({ approvals: [], reviews: [] });
    vi.mocked(workflow.fetchMailboxSummary).mockResolvedValue({ governance: "on", roles: [], approvals_to_decide: 0, reviews_to_attest: 0, decisions_unseen: 0 });
  });

  it("suggests the active lead while allowing another eligible approver", async () => {
    const onClose = vi.fn();
    vi.mocked(workflow.requestApproval).mockResolvedValue({} as Awaited<ReturnType<typeof workflow.requestApproval>>);
    render(<WorkflowRequestDialog kind="approval" sessionId="s1" stateId="t1" onClose={onClose} />);
    expect(await screen.findByRole("option", { name: "Carol (carol)" })).toBeInTheDocument();
    expect(screen.getByLabelText("Approver")).toHaveValue("carol");
    await userEvent.selectOptions(screen.getByLabelText("Approver"), "bob");
    await userEvent.type(screen.getByLabelText("Note"), "Please check");
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));
    await waitFor(() => expect(workflow.requestApproval).toHaveBeenCalledWith("s1", { state_id: "t1", approver_identity_id: "bob", note: "Please check" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  });

  it("requests an open reviewer slot for the exact state", async () => {
    vi.mocked(workflow.requestReview).mockResolvedValue({} as Awaited<ReturnType<typeof workflow.requestReview>>);
    render(<WorkflowRequestDialog kind="review" sessionId="s2" stateId="t2" onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));
    await waitFor(() => expect(workflow.requestReview).toHaveBeenCalledWith("s2", { state_id: "t2", note: "" }));
  });
});

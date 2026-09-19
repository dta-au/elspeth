import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SentList } from "./SentList";
import type { ApprovalView } from "@/types/workflow";

function approval(overrides: Partial<ApprovalView>): ApprovalView {
  return { approval_id: "a", session_id: "s", state_id: "t", binding: {}, requested_by_identity_id: "alice", approver_identity_id: "bob", requested_at: "2026-09-19T00:00:00Z", decided_at: "2026-09-19T01:00:00Z", decision: "rejected", request_note: null, decision_seen_at: null, decided_by_identity_id: "bob", decision_note: "<script>plain text</script>", revoked_by_identity_id: null, revocation_actor_kind: null, revocation_event_id: null, ...overrides };
}

describe("sent folder", () => {
  it("shows the decider's note as text, and does not mark own withdrawal unread", () => {
    render(<SentList sent={{ approvals: [approval({}), approval({ approval_id: "withdrawn", decision: "revoked", revoked_by_identity_id: "alice", revocation_actor_kind: "identity", decided_by_identity_id: null })], reviews: [] }} identityId="alice" onOpen={vi.fn()} />);
    expect(screen.getAllByText("<script>plain text</script>")).toHaveLength(2);
    expect(document.querySelector("script")).toBeNull();
    const rows = screen.getAllByRole("button", { name: "Approval from bob" });
    expect(rows.some((row) => row.textContent?.includes("Rejected by bob"))).toBe(true);
    expect(screen.getByText("New")).toBeInTheDocument();
    expect(rows.find((row) => row.textContent?.includes("Withdrawn"))).not.toHaveTextContent("New");
  });

  it("labels lifecycle revocation accurately and keeps its outcome unread", () => {
    render(<SentList sent={{ approvals: [approval({
      approval_id: "lifecycle",
      decision: "revoked",
      revoked_by_identity_id: "alice",
      revocation_actor_kind: "system",
      decided_by_identity_id: null,
    })], reviews: [] }} identityId="alice" onOpen={vi.fn()} />);
    const row = screen.getByRole("button", { name: "Approval from bob" });
    expect(row).toHaveTextContent("Revoked");
    expect(row).not.toHaveTextContent("Withdrawn");
    expect(row).toHaveTextContent("New");
  });
});

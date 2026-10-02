import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { WorkflowAuditView } from "@/types/workflow";
import { AuditViewList } from "./AuditViewList";

const empty: WorkflowAuditView = { identity_ids: [], truncated: false, runs: [], approvals: [], attestations: [], auth_events: [] };

const populated: WorkflowAuditView = {
  identity_ids: ["alice", "bob"],
  truncated: false,
  runs: [
    { run_id: "run-old", initiated_by_identity_id: "alice", recorded_at: "2026-09-01T00:00:00Z", status: "completed", started_at: "2026-09-01T00:00:00Z", completed_at: "2026-09-01T00:05:00Z" },
    { run_id: "run-new", initiated_by_identity_id: "bob", recorded_at: "2026-09-20T00:00:00Z", status: "running", started_at: "2026-09-20T00:00:00Z", completed_at: null },
  ],
  approvals: [
    { approval_id: "ap1", session_id: "s1", state_id: "t1", requested_by_identity_id: "alice", approver_identity_id: "carol", requested_at: "2026-09-19T00:00:00Z", decided_at: null, decision: null },
  ],
  attestations: [
    { attestation_id: "at1", session_id: "s2", state_id: "t2", payload_digest: "sha256:abc", reviewer_identity_id: "dan", author_identity_id: "alice", attested_at: "2026-09-18T00:00:00Z", verdict: "signed_off" },
  ],
  auth_events: [
    { event_id: "e1", occurred_at: "2026-09-17T00:00:00Z", event_type: "login", outcome: "success", identity_id: "alice", metadata_json: "{}" },
  ],
};

function section(title: string): HTMLElement {
  return screen.getByRole("heading", { name: new RegExp(`^${title}`) }).parentElement as HTMLElement;
}

describe("AuditViewList", () => {
  it("reports the scope as a count of identities rather than implying the reader sees everything", () => {
    render(<AuditViewList view={populated} />);
    expect(screen.getByText(/Scope: 2 identities\./)).toBeInTheDocument();
  });

  it("distinguishes an empty scope from a scope with no activity", () => {
    const { unmount } = render(<AuditViewList view={empty} />);
    expect(screen.getByText(/No identities are in your audit scope\./)).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    unmount();

    render(<AuditViewList view={{ ...empty, identity_ids: ["alice"] }} />);
    expect(screen.getByRole("status")).toHaveTextContent("No recorded activity for the identities in your scope.");
  });

  it("renders every record kind with its count", () => {
    render(<AuditViewList view={populated} />);
    expect(screen.getByRole("heading", { name: /^Runs/ })).toHaveTextContent("(2)");
    expect(screen.getByRole("heading", { name: /^Approvals/ })).toHaveTextContent("(1)");
    expect(screen.getByRole("heading", { name: /^Review attestations/ })).toHaveTextContent("(1)");
    expect(screen.getByRole("heading", { name: /^Authentication events/ })).toHaveTextContent("(1)");
  });

  it("orders runs newest first, whatever order the server returned", () => {
    render(<AuditViewList view={populated} />);
    const rows = within(section("Runs")).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("run-new");
    expect(rows[1]).toHaveTextContent("run-old");
  });

  it("says a run is unfinished rather than showing a blank completion time", () => {
    render(<AuditViewList view={populated} />);
    expect(within(section("Runs")).getByText(/run-new/)).toHaveTextContent("not finished");
  });

  it("names an undecided approval instead of leaving the decision empty", () => {
    render(<AuditViewList view={populated} />);
    expect(within(section("Approvals")).getByText("Awaiting a decision")).toBeInTheDocument();
  });

  it("warns when the server truncated the view, so absence is not read as evidence", () => {
    const { unmount } = render(<AuditViewList view={populated} />);
    expect(screen.queryByText(/this view is capped/)).not.toBeInTheDocument();
    unmount();

    render(<AuditViewList view={{ ...populated, truncated: true }} />);
    expect(screen.getByText(/Older rows are not shown — this view is capped\./)).toBeInTheDocument();
  });

  it("offers no control that would imply a decision can be made here", () => {
    render(<AuditViewList view={populated} />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("shows an empty section as None in scope rather than omitting it", () => {
    render(<AuditViewList view={{ ...populated, attestations: [] }} />);
    expect(within(section("Review attestations")).getByText("None in scope.")).toBeInTheDocument();
  });
});

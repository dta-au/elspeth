import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { CompositionProposal } from "@/types/api";
import {
  actionableProposals,
  PendingProposalsLiveRegion,
  pendingProposalsAnnounceText,
} from "./PendingProposalsBanner";

function makeProposal(
  id: string,
  status: CompositionProposal["status"] = "pending",
): CompositionProposal {
  return {
    id,
    session_id: "session-1",
    tool_call_id: `call-${id}`,
    tool_name: "set_pipeline",
    status,
    summary: "Replace the pipeline.",
    rationale: "Requested by the current composer turn.",
    affects: ["graph"],
    arguments_redacted_json: {},
    base_state_id: null,
    committed_state_id: null,
    audit_event_id: `event-${id}`,
    created_at: "2026-05-14T00:00:00Z",
    updated_at: "2026-05-14T00:00:00Z",
  };
}

describe("actionableProposals", () => {
  it("keeps pending non-stale proposals and drops stale or resolved ones", () => {
    const pending = makeProposal("p-1");
    const stale = makeProposal("p-2");
    const committed = makeProposal("p-3", "committed");

    const result = actionableProposals([pending, stale, committed], ["p-2"]);

    expect(result.map((p) => p.id)).toEqual(["p-1"]);
  });
});

describe("pendingProposalsAnnounceText", () => {
  it("is empty at zero so the region clears without announcing", () => {
    expect(pendingProposalsAnnounceText(0)).toBe("");
  });

  it("announces singular and plural counts", () => {
    expect(pendingProposalsAnnounceText(1)).toBe(
      "1 pending change needs your approval",
    );
    expect(pendingProposalsAnnounceText(3)).toBe(
      "3 pending changes need your approval",
    );
  });
});

describe("PendingProposalsLiveRegion", () => {
  it("stays mounted and empty when nothing is actionable", () => {
    render(
      <PendingProposalsLiveRegion proposals={[]} staleProposalIds={[]} />,
    );

    const region = screen.getByTestId("pending-proposals-live-region");
    expect(region).toHaveAttribute("role", "status");
    expect(region).toHaveTextContent("");
  });

  it("announces only the actionable count", () => {
    render(
      <PendingProposalsLiveRegion
        proposals={[
          makeProposal("p-1"),
          makeProposal("p-2"),
          makeProposal("p-3", "rejected"),
        ]}
        staleProposalIds={["p-2"]}
      />,
    );

    expect(
      screen.getByTestId("pending-proposals-live-region"),
    ).toHaveTextContent("1 pending change needs your approval");
  });
});

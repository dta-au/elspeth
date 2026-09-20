import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { GraphApprovals } from "@/components/inspector/GraphApprovals";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useSessionStore } from "@/stores/sessionStore";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
import { resetStore } from "@/test/store-helpers";
import type { CompositionState } from "@/types";
import type { InterpretationEvent } from "@/types/interpretation";
import { ApprovalsView } from "./ApprovalsView";

const event: InterpretationEvent = {
  id: "approval", session_id: "session-1", composition_state_id: "state",
  affected_node_id: "summarize_page", tool_call_id: "tool",
  kind: "pipeline_decision", user_term: "prompt_injection_shield_recommendation",
  llm_draft: "Review untrusted page content.", accepted_value: "Review untrusted\npage content.",
  choice: "accepted_as_drafted", created_at: "2026-09-20T07:00:00Z",
  resolved_at: "2026-09-20T07:01:00Z", actor: "user:owner:1",
  interpretation_source: "user_approved", model_identifier: "planner",
  model_version: "1", provider: "provider", composer_skill_hash: "hash",
  arguments_hash: "hash", hash_domain_version: "v2",
  runtime_model_identifier_at_resolve: null, runtime_model_version_at_resolve: null,
  approved_prompt_artifact_hash: null,
};

const state: CompositionState = {
  id: "state", ...compositionStateAuthorityFields, version: 1,
  sources: {}, outputs: [], edges: [], metadata: { name: null, description: null },
  nodes: [{
    id: "summarize_page", node_type: "transform", plugin: "llm",
    input: "pages", on_success: "results", on_error: "discard",
    options: { profile: "sonnet" },
  }],
};

function seed(events: InterpretationEvent[]): void {
  useSessionStore.setState({ activeSessionId: "session-1", compositionState: state } as never);
  useInterpretationEventsStore.setState({ resolvedBySession: { "session-1": events } } as never);
}

describe("ApprovalsView", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(useInterpretationEventsStore);
  });

  it("shows the Workflow tab's approvals table: Name, Approved value, Approved at", () => {
    seed([event]);
    render(<ApprovalsView />);

    const region = screen.getByRole("region", { name: "Approvals" });
    expect(within(region).getByRole("heading", { level: 2, name: "Approvals" })).toBeInTheDocument();
    expect(within(region).getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "Name", "Approved value", "Approved at",
    ]);
    const row = within(region).getByRole("row", { name: /Prompt injection protection for summarize_page/ });
    expect(row).toHaveTextContent("Now: profile sonnet");
    expect(within(row).getByText(/Review untrusted\s+page content\./)).toHaveClass("graph-approvals-value");
    expect(row.querySelector("time")).toHaveAttribute("datetime", "2026-09-20T07:01:00Z");
    // The whole panel is available here, so no 12rem scroller caps the table.
    expect(region.querySelector(".graph-detail-table-scroll")).toBeNull();
  });

  it("is a copy of the Workflow tab's table: identical cells from the one component", () => {
    seed([event]);
    const cells = (): (string | null)[] =>
      screen.getAllByRole("row").flatMap((r) => Array.from(r.children).map((c) => c.textContent));
    const { unmount } = render(<GraphApprovals events={[event]} state={state} />);
    const workflowCells = cells();
    unmount();
    render(<ApprovalsView />);
    expect(workflowCells.length).toBeGreaterThan(3);
    expect(cells()).toEqual(workflowCells);
  });

  it("leaves out rejected interpretations and says so when nothing is approved", () => {
    seed([{ ...event, choice: "opted_out" }]);
    render(<ApprovalsView />);
    expect(screen.getByText(/Nothing has been approved for this pipeline yet/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});

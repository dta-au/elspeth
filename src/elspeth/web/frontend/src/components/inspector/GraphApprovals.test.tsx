import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { CompositionState } from "@/types";
import type { InterpretationEvent } from "@/types/interpretation";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
import { GraphApprovals } from "./GraphApprovals";

const event: InterpretationEvent = {
  id: "approval", session_id: "session", composition_state_id: "state",
  affected_node_id: "summarize_page", tool_call_id: "tool",
  kind: "pipeline_decision", user_term: "prompt_injection_shield_recommendation",
  llm_draft: "Review untrusted page content.", accepted_value: "Review untrusted page content.",
  choice: "accepted_as_drafted", created_at: "2026-09-20T07:00:00Z",
  resolved_at: "2026-09-20T07:01:00Z", actor: "user:owner:1",
  interpretation_source: "user_approved", model_identifier: "planner",
  model_version: "1", provider: "provider", composer_skill_hash: "hash",
  arguments_hash: "hash", hash_domain_version: "v2",
  runtime_model_identifier_at_resolve: null, runtime_model_version_at_resolve: null,
  approved_prompt_artifact_hash: null,
};

function stateWithTitle(eventId: string): CompositionState {
  return {
    id: "state", ...compositionStateAuthorityFields, version: 1,
    sources: {}, outputs: [], edges: [], metadata: { name: null, description: null },
    nodes: [{
      id: "summarize_page", node_type: "transform", plugin: "llm",
      input: "pages", on_success: "results", on_error: "discard",
      options: {
        profile: "sonnet",
        interpretation_requirements: [{
          id: "decision", event_id: eventId, kind: event.kind, user_term: event.user_term,
          status: "resolved", display_title: "Protect the summary from page instructions",
        }],
      },
    }],
  };
}

describe("GraphApprovals", () => {
  it("shows the saved LLM title with styled node and profile details", async () => {
    render(<GraphApprovals events={[event]} state={stateWithTitle(event.id)} />);
    await userEvent.setup().click(screen.getByText("Approvals (1)"));
    const name = screen.getByRole("rowheader");
    expect(name).toHaveTextContent("Protect the summary from page instructions for summarize_page");
    expect(within(name).getByText("summarize_page").tagName).toBe("CODE");
    expect(within(name).getByText("llm")).toHaveClass("graph-output-detail");
    expect(within(name).getByText("profile sonnet")).toHaveClass("graph-output-detail");
    expect(screen.getByText(event.accepted_value!)).toBeInTheDocument();
  });

  it("uses a readable fallback for historical approvals instead of another event's title", async () => {
    render(<GraphApprovals events={[event]} state={stateWithTitle("newer-approval")} />);
    await userEvent.setup().click(screen.getByText("Approvals (1)"));
    expect(screen.getByRole("rowheader")).toHaveTextContent("Prompt injection protection for summarize_page");
    expect(screen.queryByText(/Protect the summary from page instructions/)).not.toBeInTheDocument();
  });

  it("keeps system and user prompt roles explicit when a saved custom title exists", async () => {
    const promptEvent: InterpretationEvent = {
      ...event, kind: "llm_prompt_template", user_term: "llm_prompt_template:summarize_page",
      accepted_value: "System prompt:\nSummarize faithfully.\n\nPrompt template:\nSummarize {{ row.text }}.",
    };
    const state = stateWithTitle(event.id);
    state.nodes[0].options.interpretation_requirements = [{
      event_id: event.id, kind: promptEvent.kind, user_term: promptEvent.user_term,
      display_title: "Summarization instructions",
    }];
    render(<GraphApprovals events={[promptEvent]} state={state} />);
    await userEvent.setup().click(screen.getByText("Approvals (1)"));
    expect(screen.getByRole("rowheader")).toHaveTextContent("Summarization instructions (system and user prompts)");
  });
});

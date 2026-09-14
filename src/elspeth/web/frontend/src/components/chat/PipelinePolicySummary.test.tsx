import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { CompositionState, NodeSpec } from "@/types";
import { PipelinePolicySummary } from "./PipelinePolicySummary";

function node(id: string, options: Record<string, unknown> = {}): NodeSpec {
  return { id, node_type: "transform", plugin: "llm", input: "rows", on_success: "results", on_error: "discard", options };
}

function composition(overrides: Partial<CompositionState> = {}): CompositionState {
  return {
    id: "state-1", session_id: "session-1", version: 1,
    sources: {}, nodes: [], edges: [], outputs: [],
    metadata: { name: null, description: null }, is_valid: true,
    validation_errors: null, validation_warnings: null, validation_suggestions: null,
    derived_from_state_id: null, created_at: "2026-09-15T00:00:00Z",
    composer_meta: null, plugin_policy_findings: [], ...overrides,
  };
}

describe("PipelinePolicySummary", () => {
  it("discloses saved persona-fork bindings, recorded failed merges, and absent CSV writes", () => {
    render(<PipelinePolicySummary state={composition({
      nodes: [
        node("ask_decorator", { profile: "sonnet" }),
        node("ask_assistant", { profile: "sonnet" }),
        { ...node("join_answers"), node_type: "coalesce", plugin: null, policy: "require_all", on_error: null },
      ],
      outputs: [{ name: "ab_results", plugin: "csv", options: {}, on_write_failure: "discard" }],
    })} />);
    expect(screen.getByText(/ask_decorator: profile sonnet; ask_assistant: profile sonnet/)).toBeVisible();
    expect(screen.getByText(/ask_decorator, ask_assistant: row errors discard/)).toBeVisible();
    expect(screen.getByText(/join_answers: every branch is required.*recorded failed merge.*no combined row/)).toBeVisible();
    expect(screen.getByText(/ab_results \(csv\): failed writes are discarded.*absent from the output.*failure remains recorded/)).toBeVisible();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("updates the disclosure when saved routing and model selection change", () => {
    const { rerender } = render(<PipelinePolicySummary state={composition({ nodes: [node("answer", { profile: "sonnet" })] })} />);
    rerender(<PipelinePolicySummary state={composition({
      nodes: [{ ...node("answer", { model: "claude-sonnet-4-6" }), on_error: "quarantine" }],
      outputs: [{ name: "results", plugin: "csv", options: {}, on_write_failure: "failed_writes" }],
    })} />);
    expect(screen.getByText(/answer: model claude-sonnet-4-6/)).toBeVisible();
    expect(screen.queryByText(/profile sonnet|discard|every branch/)).not.toBeInTheDocument();
  });

  it("covers LLM sources and explicit validation discard", () => {
    render(<PipelinePolicySummary state={composition({ sources: {
      colours: { plugin: "llm", options: { profile: "sonnet" }, on_validation_failure: "discard" },
    } })} />);
    expect(screen.getByText(/colours: profile sonnet/)).toBeVisible();
    expect(screen.getByText(/colours: invalid source rows are discarded.*audit outcome remains recorded/)).toBeVisible();
  });

  it("does not invent model or discard disclosures for unrelated configuration", () => {
    const state = composition({
      sources: { rows: { plugin: "csv", options: {} } },
      nodes: [
        { ...node("map_fields", { model: "not-an-llm-binding" }), plugin: "field_mapper", on_error: "quarantine" },
        { ...node("join_available"), node_type: "coalesce", plugin: null, policy: "best_effort", on_error: null },
      ],
      outputs: [{ name: "results", plugin: "csv", options: {}, on_write_failure: "quarantine" }],
    });
    const { container, rerender } = render(<PipelinePolicySummary state={state} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<PipelinePolicySummary state={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it.each([
    { profile: "sonnet", model: "private-model", api_key: "secret-value", endpoint: "private-endpoint" },
    { profile: { secret_ref: "private-ref" }, model: "private-model" },
    { profile_alias: "private-alias", model: "private-model" },
    { resolved_model: "private-model", model: "private-model" },
    { profile: "private/invalid", model: "private-model" },
    { model: "private model with credentials" },
  ])("never exposes private profile resolution or arbitrary options: %j", (options) => {
    const { container } = render(<PipelinePolicySummary state={composition({ nodes: [node("answer", options)] })} />);
    expect(container.textContent).not.toMatch(/private|secret-value/);
  });
});

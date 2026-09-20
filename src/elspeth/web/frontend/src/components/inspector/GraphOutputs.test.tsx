import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { CompositionState, NodeSpec } from "@/types";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
import { GraphOutputs } from "./GraphOutputs";

describe("GraphOutputs", () => {
  it("shows named gate routes and implicit success connections without counting routes as nodes", () => {
    const common: NodeSpec = {
      id: "filter", node_type: "gate", plugin: null, input: "source_out",
      on_success: null, on_error: null, options: {},
    };
    const state: CompositionState = {
      id: "session", ...compositionStateAuthorityFields, version: 1,
      metadata: { name: null, description: null }, sources: {}, outputs: [], edges: [],
      nodes: [
        {
          ...common, routes: { accepted: "fork", rejected: "discard" },
          fork_to: ["left", "right"],
        },
        {
          ...common, id: "combined", node_type: "coalesce", policy: "require_all",
          branches: { a: "left", b: "right" },
        },
        { ...common, id: "unfinished", node_type: "transform", input: "combined" },
      ],
    };

    render(<GraphOutputs state={state} />);

    expect(screen.getByText("Routing (3)")).toBeInTheDocument();
    const gate = screen.getByRole("row", { name: /Gate: filter/ });
    expect(gate).toHaveTextContent("accepted: Fork to combined, combined");
    expect(gate).toHaveTextContent("rejected: Discard row (audit recorded)");
    const combined = screen.getByRole("row", { name: /Coalesce: combined/ });
    expect(combined).toHaveTextContent("Send to unfinished");
    expect(combined).toHaveTextContent("Required branch missingNo combined row (failure recorded)");
    const unfinished = screen.getByRole("row", { name: /Transform: unfinished/ });
    expect(within(unfinished).getByText("No success route set")).toBeInTheDocument();
    expect(unfinished).toHaveTextContent("No failure route set");
  });

  it("resolves success and failure connections to node names and routes queues through their consumers", () => {
    const state: CompositionState = {
      id: "session", ...compositionStateAuthorityFields, version: 1,
      metadata: { name: null, description: null }, edges: [],
      sources: { source: { plugin: "csv", options: {}, on_success: "rows", on_validation_failure: "invalid" } },
      nodes: [
        {
          id: "classify", node_type: "transform", plugin: "llm", input: "rows",
          on_success: "buffer", on_error: "failed_rows", options: {},
        },
        {
          id: "recover", node_type: "transform", plugin: "field_mapper", input: "failed_rows",
          on_success: "results", on_error: "discard", options: {},
        },
        { id: "buffer", node_type: "queue", plugin: null, input: "buffer", on_success: null, on_error: null, options: {} },
        {
          id: "summarize", node_type: "transform", plugin: "llm", input: "buffer",
          on_success: "unbound", on_error: "discard", options: {},
        },
      ],
      outputs: [
        { name: "results", plugin: "csv", options: {}, on_write_failure: "invalid" },
        { name: "invalid", plugin: "json", options: {}, on_write_failure: "discard" },
      ],
    };
    render(<GraphOutputs state={state} />);
    const source = screen.getByRole("row", { name: /Source: source/ });
    expect(source).toHaveTextContent("Send to classify");
    expect(within(source).getByText("classify").tagName).toBe("CODE");
    expect(within(source).getByText("source").tagName).toBe("CODE");
    expect(source).toHaveTextContent("Send to invalid");
    const classify = screen.getByRole("row", { name: /Transform: classify/ });
    expect(classify).toHaveTextContent("Send to buffer");
    expect(classify).toHaveTextContent("Send to recover");
    expect(within(classify).getByText("buffer").tagName).toBe("CODE");
    expect(within(classify).getByText("recover").tagName).toBe("CODE");
    const queue = screen.getByRole("row", { name: /Queue: buffer/ });
    expect(queue).toHaveTextContent("Send to summarize");
    const summarize = screen.getByRole("row", { name: /Transform: summarize/ });
    expect(summarize).toHaveTextContent("Send to unbound (not connected)");
    const output = screen.getByRole("row", { name: /Output: results/ });
    expect(within(output).getByText("Row written")).toBeInTheDocument();
    expect(output).toHaveTextContent("Send to invalid");
  });
});

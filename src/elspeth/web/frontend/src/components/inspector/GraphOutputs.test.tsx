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
        { ...common, id: "combined", node_type: "coalesce", policy: "require_all" },
        { ...common, id: "unfinished", node_type: "transform" },
      ],
    };

    render(<GraphOutputs state={state} />);

    expect(screen.getByText("Outputs (3)")).toBeInTheDocument();
    const gate = screen.getByRole("row", { name: /Gate: filter/ });
    expect(within(gate).getByText("accepted: Fork to left, right")).toBeInTheDocument();
    expect(within(gate).getByText("rejected: Discard row (audit recorded)")).toBeInTheDocument();
    const combined = screen.getByRole("row", { name: /Coalesce: combined/ });
    expect(within(combined).getByText("Send to combined")).toBeInTheDocument();
    expect(combined).toHaveTextContent("Required branch missingNo combined row (failure recorded)");
    const unfinished = screen.getByRole("row", { name: /Transform: unfinished/ });
    expect(within(unfinished).getByText("No success route set")).toBeInTheDocument();
    expect(unfinished).toHaveTextContent("No failure route set");
  });
});

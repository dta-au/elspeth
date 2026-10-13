import { describe, expect, it } from "vitest";
import { visibleGraphRoutes } from "./graphRouteVisibility";

describe("graph route visibility", () => {
  it("filters semantic failure/discard routes, preserving business routes, shared sinks and ordinary orphans", () => {
    const nodes = ["source", "step", "gate", "shared", "quarantine", "business", "unused", "orphan"].map((id) => ({ id }));
    const edge = (id: string, source: string, target: string, flowType: "success" | "error", label = "success") => ({
      id, source, target, data: { flowType }, label, sourceHandle: `handle-${id}`, style: { stroke: "red" },
    });
    const edges = [
      edge("entry", "source", "step", "success"),
      edge("success", "step", "shared", "success"),
      edge("shared-failure", "step", "shared", "error"),
      edge("validation", "source", "quarantine", "error"),
      edge("false", "gate", "business", "success", "false"),
      edge("discard", "gate", "discard", "success", "rejected"),
    ];
    const before = JSON.stringify({ nodes, edges });
    const sinks = new Set(["shared", "quarantine", "business", "unused"]);
    const hidden = visibleGraphRoutes(nodes, edges, sinks, false);
    expect(hidden.edges.map((item) => item.id)).toEqual(["entry", "success", "false"]);
    expect(hidden.nodes.map((item) => item.id)).toEqual(["source", "step", "gate", "shared", "business", "unused", "orphan"]);
    expect(hidden.edges[1]).toBe(edges[1]);
    expect(JSON.stringify({ nodes, edges })).toBe(before);
    const complete = visibleGraphRoutes(nodes, edges, sinks, true);
    expect(complete.nodes).toBe(nodes);
    expect(complete.edges).toBe(edges);
  });
});

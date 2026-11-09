import { describe, expect, it } from "vitest";
import { withFanInArrivals } from "./GraphView";

describe("visual fan-in arrivals", () => {
  it.each([1, 3, 15])("keeps every logical route and stable arrival for %i inputs", (count) => {
    const nodes = [
      ...Array.from({ length: count }, (_, index) => ({ id: `source_${index}`, position: { x: index * 200, y: 0 }, data: {} })),
      { id: "shared_sink", position: { x: 0, y: 300 }, data: {} },
    ];
    const edges = Array.from({ length: count }, (_, index) => ({
      id: `route_${index}`, source: `source_${index}`, target: "shared_sink", label: index % 2 ? "false" : "success",
      sourceHandle: `original_source_${index}`, targetHandle: null,
      data: { flowType: index % 3 ? "success" as const : "error" as const },
    }));
    const rendered = withFanInArrivals(nodes, edges);
    expect(rendered.map(({ id, source, target, sourceHandle, targetHandle, label }) => ({ id, source, target, sourceHandle, targetHandle, label })))
      .toEqual(edges.map(({ id, source, target, sourceHandle, targetHandle, label }) => ({ id, source, target, sourceHandle, targetHandle, label })));
    expect(rendered.map((edge) => edge.data.flowType)).toEqual(edges.map((edge) => edge.data.flowType));
    expect(withFanInArrivals(nodes, [...edges].reverse()).reverse()).toEqual(rendered);
    if (count === 1) expect(rendered).toEqual(edges);
    else expect(new Set(rendered.map((edge) => edge.data.arrivalOffset)).size).toBe(count);
  });
});

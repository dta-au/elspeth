import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ReadOnlyPipelineGraph } from "./ReadOnlyPipelineGraph";

describe("ReadOnlyPipelineGraph", () => {
  it("renders parallel identity fork branches in distinct lanes with visible labels", () => {
    const { container } = render(
      <ReadOnlyPipelineGraph
        ariaLabel="Identity fork into row union"
        nodes={[
          {
            id: "experiment-gate",
            label: "experiment gate",
            kind: "gate",
            subtitle: null,
          },
          {
            id: "variant-union",
            label: "variant union",
            kind: "row_union",
            subtitle: null,
          },
        ]}
        edges={[
          {
            id: "control-edge",
            source: "experiment-gate",
            target: "variant-union",
            label: "control",
            isError: false,
          },
          {
            id: "treatment-edge",
            source: "experiment-gate",
            target: "variant-union",
            label: "treatment",
            isError: false,
          },
        ]}
      />,
    );

    const controlPath = container.querySelector(
      'path[data-edge-id="control-edge"]',
    );
    const treatmentPath = container.querySelector(
      'path[data-edge-id="treatment-edge"]',
    );
    expect(controlPath).not.toBeNull();
    expect(treatmentPath).not.toBeNull();
    expect(controlPath?.getAttribute("d")).not.toBe(
      treatmentPath?.getAttribute("d"),
    );
    expect(
      container.querySelector('text[data-edge-id="control-edge"]'),
    ).toHaveTextContent("control");
    expect(
      container.querySelector('text[data-edge-id="treatment-edge"]'),
    ).toHaveTextContent("treatment");
  });

  it("includes every admitted parallel branch lane in the SVG viewBox", () => {
    const edges = Array.from({ length: 64 }, (_, index) => ({
      id: `branch-${index + 1}`,
      source: "experiment-gate",
      target: "variant-union",
      label: `branch-${index + 1}`,
      isError: false,
    }));
    const { container } = render(
      <ReadOnlyPipelineGraph
        ariaLabel="64 identity fork branches into row union"
        nodes={[
          {
            id: "experiment-gate",
            label: "experiment gate",
            kind: "gate",
            subtitle: null,
          },
          {
            id: "variant-union",
            label: "variant union",
            kind: "row_union",
            subtitle: null,
          },
        ]}
        edges={edges}
      />,
    );

    const viewBox = container.querySelector("svg")?.getAttribute("viewBox");
    expect(viewBox).not.toBeNull();
    const [, minY, , height] = viewBox!.split(/\s+/).map(Number);
    const maxY = minY + height;
    const labelYs = Array.from(
      container.querySelectorAll("text[data-edge-id]"),
      (label) => Number(label.getAttribute("y")),
    );

    expect(labelYs).toHaveLength(64);
    expect(minY).toBeLessThan(Math.min(...labelYs));
    expect(maxY).toBeGreaterThan(Math.max(...labelYs));
  });

  it("anchors large-fanout labels at the midpoint of their cubic lanes", () => {
    const edges = Array.from({ length: 64 }, (_, index) => ({
      id: `branch-${index + 1}`,
      source: "experiment-gate",
      target: "variant-union",
      label: `branch-${index + 1}`,
      isError: false,
    }));
    const { container } = render(
      <ReadOnlyPipelineGraph
        ariaLabel="64 identity fork branches into row union"
        nodes={[
          {
            id: "experiment-gate",
            label: "experiment gate",
            kind: "gate",
            subtitle: null,
          },
          {
            id: "variant-union",
            label: "variant union",
            kind: "row_union",
            subtitle: null,
          },
        ]}
        edges={edges}
      />,
    );

    for (const edgeId of ["branch-1", "branch-64"]) {
      const path = container.querySelector(`path[data-edge-id="${edgeId}"]`);
      const label = container.querySelector(`text[data-edge-id="${edgeId}"]`);
      expect(path).not.toBeNull();
      expect(label).not.toBeNull();
      const coordinates = path!.getAttribute("d")!.match(/-?\d+(?:\.\d+)?/g)!.map(Number);
      expect(coordinates).toHaveLength(8);
      const [, startY, , controlOneY, , controlTwoY, , endY] = coordinates;
      const cubicMidpointY =
        (startY + 3 * controlOneY + 3 * controlTwoY + endY) / 8;
      expect(Number(label!.getAttribute("y"))).toBeCloseTo(cubicMidpointY);
    }
  });
});

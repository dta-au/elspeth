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
});

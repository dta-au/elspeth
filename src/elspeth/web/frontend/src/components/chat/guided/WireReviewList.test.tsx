import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { WireReviewList, type WireReviewItem } from "./WireReviewList";

// The shared route list is used by BOTH the wire card (with per-route
// contract status) and the proposal turn (without it), so the status vocabulary
// and the "chip must also be in the accessible name" contract are pinned here
// rather than only through one caller.

function item(overrides: Partial<WireReviewItem> = {}): WireReviewItem {
  return {
    id: "edge-1",
    from: "source-1",
    to: "output-1",
    summary: "Source success",
    ...overrides,
  };
}

describe("WireReviewList", () => {
  it("labels every contract status in the wire card's own register", () => {
    // "no static check" states what the payload knows; the retired
    // "not yet checked" promised a check that never arrives
    // (elspeth-e4c2ebb697). "discard route" is the one absent-contract case
    // whose reason the payload carries — the destination itself.
    render(
      <WireReviewList
        ariaLabel="Wiring routes"
        items={[
          item({ id: "edge-connected", status: "connected" }),
          item({ id: "edge-warning", status: "warning" }),
          item({ id: "edge-unchecked", status: "unchecked" }),
          item({ id: "edge-discard", status: "discard", to: "Discard" }),
        ]}
      />,
    );

    expect(screen.getByText("connected")).toHaveClass("wire-review-status--connected");
    expect(screen.getByText("not connected correctly")).toHaveClass("wire-review-status--warning");
    expect(screen.getByText("no static check")).toHaveClass("wire-review-status--unchecked");
    expect(screen.getByText("discard route")).toHaveClass("wire-review-status--discard");
    expect(screen.queryByText("not yet checked")).not.toBeInTheDocument();
  });

  it("renders no chip for a caller that carries no contract status", () => {
    // ProposePipelineTurn's routes: the proposal has no validated contracts
    // yet, so a status chip there would be an invented verdict.
    const { container } = render(
      <WireReviewList ariaLabel="Proposed pipeline routes" items={[item()]} />,
    );
    expect(container.querySelector(".wire-review-status")).toBeNull();
    expect(screen.getByRole("listitem")).toHaveAttribute("data-edge-id", "edge-1");
  });

  it("uses the caller's aria-label as the row's accessible name", () => {
    // The li aria-label OVERRIDES its text content, so a status chip outside
    // the label would be invisible to screen readers — hence the contract that
    // callers passing `status` fold the wording into `ariaLabel` too.
    render(
      <WireReviewList
        ariaLabel="Wiring routes"
        items={[
          item({
            status: "unchecked",
            ariaLabel: "source-1 to output-1 — Source success — no static check",
          }),
        ]}
      />,
    );
    expect(
      screen.getByRole("listitem", {
        name: "source-1 to output-1 — Source success — no static check",
      }),
    ).toBeInTheDocument();
  });

  it("renders a route detail beside the status when the caller supplies one", () => {
    render(
      <WireReviewList
        ariaLabel="Wiring routes"
        items={[item({ status: "warning", detail: "Missing fields: body" })]}
      />,
    );
    expect(screen.getByText("Missing fields: body")).toHaveClass("wire-review-detail");
  });
});

// ============================================================================
// InlineSourceDisambiguationTurn.test.tsx — Phase 5a Task 4
//
// Widget tests for the pre-success interactive disambiguation turn. Mirrors
// the test shape of InlineSourceCreatedTurn.test.tsx — both widgets live
// inside the chat message stream and share the role="region" surface, but
// the disambiguation widget is invoked BEFORE the inline_blob is committed
// (the post-success widget is what the user sees once the proposal lands).
//
// The accessible names asserted below are load-bearing: ChatPanel.tsx's
// disambiguation wiring routes proposals into this widget by region role +
// aria-label, and the F-10 / F-11 store mutations key on the per-button
// click handlers. Changing the literal "Yes — N rows" / "No — treat as 1
// row" / "Edit the rows" / "This isn't source data" labels is a spec
// amendment, not a refactor.
// ============================================================================

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { InlineSourceDisambiguationTurn } from "./InlineSourceDisambiguationTurn";

describe("InlineSourceDisambiguationTurn", () => {
  function makeProps() {
    return {
      userInput: "check these URLs: a.com, b.com, c.com",
      proposedRows: ["a.com", "b.com", "c.com"],
      proposalId: "p1",
      messageId: "msg-user-1",
      onConfirmMultiRow: vi.fn(),
      onTreatAsOneRow: vi.fn(),
      onEditRows: vi.fn(),
      onNotSourceData: vi.fn(), // F-10: escape action
    };
  }

  it("renders the user's original input verbatim", () => {
    render(<InlineSourceDisambiguationTurn {...makeProps()} />);
    expect(
      screen.getByText(/check these URLs: a\.com, b\.com, c\.com/),
    ).toBeInTheDocument();
  });

  it("shows the LLM's row breakdown with one row per item", () => {
    render(<InlineSourceDisambiguationTurn {...makeProps()} />);
    expect(screen.getByText("a.com")).toBeInTheDocument();
    expect(screen.getByText("b.com")).toBeInTheDocument();
    expect(screen.getByText("c.com")).toBeInTheDocument();
  });

  it("calls onConfirmMultiRow when the confirm button is clicked", () => {
    const props = makeProps();
    render(<InlineSourceDisambiguationTurn {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /yes.*3 rows/i }));
    expect(props.onConfirmMultiRow).toHaveBeenCalledWith("p1");
  });

  it("calls onTreatAsOneRow when the single-row button is clicked", () => {
    const props = makeProps();
    render(<InlineSourceDisambiguationTurn {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /treat as 1 row/i }));
    expect(props.onTreatAsOneRow).toHaveBeenCalledWith("p1");
  });

  it("calls onEditRows when the edit button is clicked", () => {
    const props = makeProps();
    render(<InlineSourceDisambiguationTurn {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /edit the rows/i }));
    expect(props.onEditRows).toHaveBeenCalledWith("p1");
  });

  it("calls onNotSourceData when the escape action is clicked (F-10)", () => {
    const props = makeProps();
    render(<InlineSourceDisambiguationTurn {...props} />);
    fireEvent.click(
      screen.getByRole("button", { name: /this isn.t source data/i }),
    );
    expect(props.onNotSourceData).toHaveBeenCalledWith("msg-user-1");
  });

  it("announces itself via role=region with an aria-label", () => {
    render(<InlineSourceDisambiguationTurn {...makeProps()} />);
    expect(
      screen.getByRole("region", { name: /row count/i }),
    ).toBeInTheDocument();
  });

  // n=1 is the only count that can expose a hardcoded plural: n=0 and n=2+
  // read correctly whether or not the noun is gated. The region's aria-label
  // and the confirm button used to spell the rule twice in this one file —
  // the label hardcoded "rows" while the button branched — so both are
  // asserted here against the same single-row props.
  it("says '1 row', not '1 rows', in BOTH the region label and the confirm button", () => {
    render(
      <InlineSourceDisambiguationTurn
        {...makeProps()}
        userInput="add this row: item-42"
        proposedRows={["item-42"]}
      />,
    );
    const region = screen.getByRole("region", { name: /row count/i });
    expect(region.getAttribute("aria-label")).toContain("1 row)");
    expect(region.getAttribute("aria-label")).not.toContain("1 rows");
    expect(
      screen.getByRole("button", { name: /yes\s*—\s*1 row$/i }),
    ).toBeInTheDocument();
  });

  it("moves focus to the primary action button on mount (F-19)", () => {
    render(<InlineSourceDisambiguationTurn {...makeProps()} />);
    expect(
      screen.getByRole("button", { name: /yes.*3 rows/i }),
    ).toHaveFocus();
  });
});

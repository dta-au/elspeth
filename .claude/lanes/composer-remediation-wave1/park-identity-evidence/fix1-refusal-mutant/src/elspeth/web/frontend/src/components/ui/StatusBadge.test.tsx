import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusBadge } from "./StatusBadge";

describe("StatusBadge", () => {
  it("gives completed_with_failures its own warning-family class and the ⚠ glyph (elspeth-cd885f4c4d)", () => {
    // The badge must agree with the progress bar / toast / Run-tab dot, which
    // all render this status as a warning — aliasing it to the green
    // "completed" class reported a partial failure as an unqualified success.
    render(<StatusBadge status="completed_with_failures" data-testid="sb" />);
    const badge = screen.getByTestId("sb");
    expect(badge).toHaveClass(
      "status-badge",
      "status-badge-completed_with_failures",
    );
    expect(badge).not.toHaveClass("status-badge-completed");
    expect(badge).toHaveTextContent("⚠");
  });
  it("renders empty with its own colour and the ∅ glyph", () => {
    render(<StatusBadge status="empty" data-testid="sb" />);
    const badge = screen.getByTestId("sb");
    expect(badge).toHaveClass("status-badge", "status-badge-empty");
    expect(badge).toHaveTextContent("∅");
  });
  it("maps cancelling to the cancelled colour", () => {
    render(<StatusBadge status="cancelling" data-testid="sb" />);
    const badge = screen.getByTestId("sb");
    expect(badge).toHaveClass("status-badge", "status-badge-cancelled");
  });
  it("renders running with no glyph", () => {
    render(<StatusBadge status="running" data-testid="sb" />);
    const badge = screen.getByTestId("sb");
    expect(badge).toHaveClass("status-badge", "status-badge-running");
    expect(badge).toHaveTextContent("running");
    expect(badge).not.toHaveTextContent("⚠");
    expect(badge).not.toHaveTextContent("∅");
  });
  it("defaults to pending and lets children override the label", () => {
    render(<StatusBadge data-testid="sb">Queued</StatusBadge>);
    const badge = screen.getByTestId("sb");
    expect(badge).toHaveClass("status-badge", "status-badge-pending");
    expect(badge).toHaveTextContent("Queued");
  });
  it("forwards className", () => {
    render(<StatusBadge status="failed" className="x" data-testid="sb" />);
    const badge = screen.getByTestId("sb");
    expect(badge).toHaveClass("status-badge", "status-badge-failed", "x");
  });
});

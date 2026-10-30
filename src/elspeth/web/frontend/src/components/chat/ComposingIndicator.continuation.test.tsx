import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ComposingIndicator, LIVE_UPDATES_INTERRUPTED, SUCCESSOR_NOTICE, SUCCESSOR_NOTICE_MS, continuationView } from "./ComposingIndicator";
import type { ComposerOperationStatusV2 } from "@/types/composerOperations";

const id = "22222222-2222-4222-8222-222222222222";
function status(extra: Partial<ComposerOperationStatusV2> = {}): ComposerOperationStatusV2 {
  const now = new Date().toISOString();
  return { protocol_version: 2, operation_id: id, kind: "compose_message", status: "running", phase: "executing", status_revision: 1, created_at: now, observed_at: now, poll_after_ms: 1000, cancel_requested: false, segment: { sequence: 1, generation: 1, state: "running", deadline_at: now }, ...extra };
}
afterEach(() => vi.useRealTimers());

describe("Continuation copy (design §4.2, F-12)", () => {
  it.each([
    ["queued", "Queued", "Waiting for a composer worker to start this request."],
    ["continuation_queued", "Continuing", "The composer reached a checkpoint. The next step of this request is queued."],
    ["retry_waiting", "Waiting to retry", "A service the composer depends on did not respond. The composer will retry this request automatically."],
    ["reconciling", "Recovering", "The server running this request stopped. Another server is resuming it."],
  ] as const)("phase %s reads %s", (phase, label, headline) => {
    expect(continuationView(status({ phase, status: phase === "queued" ? "queued" : "running" }), false)).toEqual({ label, headline });
  });
  it("keeps the ordinary progress view while executing and for v1", () => {
    expect(continuationView(status(), false)).toBeNull();
    expect(continuationView(null, false)).toBeNull();
  });
  it("puts Stop ahead of phase: requested, then confirmed", () => {
    expect(continuationView(status({ phase: "continuation_queued" }), true)?.label).toBe("Stopping");
    expect(continuationView(status({ cancel_requested: true }), false)).toEqual({
      label: "Stopping", headline: "Stop confirmed. The composer is finishing its current step and will not start another.",
    });
    expect(continuationView(null, true)?.headline).toBe("Stop requested. Waiting for the server to confirm.");
  });
  it("never says saved for a checkpoint and uses no first person", () => {
    for (const phase of ["queued", "continuation_queued", "retry_waiting", "reconciling"] as const) {
      const view = continuationView(status({ phase, status: phase === "queued" ? "queued" : "running" }), false)!;
      expect(`${view.label} ${view.headline}`).not.toMatch(/\bsaved\b|\bI\b|\bI'm\b|\bwe\b/i);
    }
    expect(SUCCESSOR_NOTICE).not.toMatch(/\bI\b|\bI'm\b/);
  });
  it("shows the successor notice once when a new segment starts, then clears it after 5 s", () => {
    vi.useFakeTimers();
    const { rerender } = render(<ComposingIndicator operationStatus={status()} />);
    expect(screen.queryByText(SUCCESSOR_NOTICE)).toBeNull();
    rerender(<ComposingIndicator operationStatus={status({ status_revision: 2, phase: "continuation_queued", segment: { sequence: 2, generation: 2, state: "queued", deadline_at: null } })} />);
    rerender(<ComposingIndicator operationStatus={status({ status_revision: 3, segment: { sequence: 3, generation: 3, state: "running", deadline_at: new Date().toISOString() } })} />);
    expect(screen.getByText(SUCCESSOR_NOTICE)).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(SUCCESSOR_NOTICE_MS + 10); });
    expect(screen.queryByText(SUCCESSOR_NOTICE)).toBeNull();
  });
  it("R10: announces the same successor's queued -> running transition", () => {
    const { rerender } = render(<ComposingIndicator operationStatus={status()} />);
    rerender(<ComposingIndicator operationStatus={status({ status_revision: 2, phase: "continuation_queued", segment: { sequence: 2, generation: 2, state: "queued", deadline_at: null } })} />);
    expect(screen.queryByText(SUCCESSOR_NOTICE)).toBeNull();
    rerender(<ComposingIndicator operationStatus={status({ status_revision: 3, segment: { sequence: 2, generation: 2, state: "running", deadline_at: new Date().toISOString() } })} />);
    expect(screen.getByText(SUCCESSOR_NOTICE)).toBeInTheDocument();
  });
  it("announces a successor first seen queued when it starts (attach mid-continuation)", () => {
    const { rerender } = render(<ComposingIndicator operationStatus={status({ status_revision: 2, phase: "continuation_queued", segment: { sequence: 2, generation: 2, state: "queued", deadline_at: null } })} />);
    expect(screen.queryByText(SUCCESSOR_NOTICE)).toBeNull();
    rerender(<ComposingIndicator operationStatus={status({ status_revision: 3, segment: { sequence: 2, generation: 2, state: "running", deadline_at: new Date().toISOString() } })} />);
    expect(screen.getByText(SUCCESSOR_NOTICE)).toBeInTheDocument();
  });
  it("does not show the notice on first attach to a request already past segment 1", () => {
    render(<ComposingIndicator operationStatus={status({ segment: { sequence: 4, generation: 4, state: "running", deadline_at: new Date().toISOString() } })} />);
    expect(screen.queryByText(SUCCESSOR_NOTICE)).toBeNull();
  });
  it("keeps all of this inside the single status region", () => {
    render(<ComposingIndicator operationStatus={status({ phase: "reconciling" })} liveUpdatesInterrupted />);
    const region = screen.getByRole("status");
    expect(region).toHaveTextContent("Recovering");
    expect(region).toHaveTextContent(LIVE_UPDATES_INTERRUPTED);
  });
});

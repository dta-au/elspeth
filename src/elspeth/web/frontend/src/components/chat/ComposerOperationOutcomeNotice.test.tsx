import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ComposerOperationFailureNotice } from "@/types/composerOperations";
import { ComposerOperationOutcomeNotice } from "./ComposerOperationOutcomeNotice";

const outcome: ComposerOperationFailureNotice = {
  session_id: "11111111-1111-4111-8111-111111111111", operation_id: "22222222-2222-4222-8222-222222222222",
  kind: "compose_message", user_message_id: null, settled_at: null, failure_code: "operation_failed", error_type: "operation_failed",
  diagnostic_id: "33333333-3333-4333-8333-333333333333", request_id: "request-reference",
  saved_revisions: { attribution: "unavailable", count: null, latest_state_id: null, latest_version: null },
};
afterEach(cleanup);
describe("operation outcome notice", () => {
  it("shows uncertainty and the canonical support diagnostic without inventing settlement time", () => {
    const { container } = render(<ComposerOperationOutcomeNotice outcome={outcome} />);
    expect(screen.getByRole("note")).toHaveTextContent("Saved progress has not been confirmed");
    expect(screen.getByRole("note")).toHaveTextContent(outcome.diagnostic_id!);
    expect(container.querySelector("time")).toBeNull();
    expect(screen.getByRole("note")).not.toHaveTextContent("No draft revisions");
  });
  it("shows actual operation revision facts and timestamp without claiming request satisfaction", () => {
    const settled_at = "2026-10-14T08:00:00Z";
    render(<ComposerOperationOutcomeNotice outcome={{ ...outcome, settled_at, saved_revisions: { attribution: "fenced", count: 2, latest_state_id: outcome.operation_id, latest_version: 7 } }} />);
    expect(screen.getByRole("note")).toHaveTextContent("saved 2 draft revisions, last saved version 7. Later changes may be newer");
    expect(screen.getByRole("time")).toHaveAttribute("datetime", settled_at);
  });
  it("falls back to the request reference and renders it as text", () => {
    render(<ComposerOperationOutcomeNotice outcome={{ ...outcome, failure_code: "worker_lost", diagnostic_id: null, request_id: "<script>synthetic</script>" }} />);
    expect(screen.getByRole("note")).toHaveTextContent("<script>synthetic</script>");
    expect(document.querySelector("script")).toBeNull();
  });
  it.each([
    { count: 1, noun: "revision", latest_version: 4 },
    { count: 3, noun: "revisions", latest_version: 9 },
  ])("confirms saved Stop changes from the operation's $count attributed revisions", ({ count, noun, latest_version }) => {
    render(<ComposerOperationOutcomeNotice outcome={{ ...outcome, failure_code: "request_cancelled", saved_revisions: { attribution: "fenced", count, latest_state_id: outcome.operation_id, latest_version } }} />);
    expect(screen.getByRole("note")).toHaveTextContent("Composition stopped");
    expect(screen.getByRole("note")).toHaveTextContent(`Saved draft changes remain available. This operation saved ${count} draft ${noun}, last saved version ${latest_version}. Later changes may be newer.`);
  });
  it("reports zero attributed Stop revisions without claiming new saved changes", () => {
    render(<ComposerOperationOutcomeNotice outcome={{ ...outcome, failure_code: "request_cancelled", saved_revisions: { attribution: "fenced", count: 0, latest_state_id: null, latest_version: null } }} />);
    expect(screen.getByRole("note")).toHaveTextContent("No draft revisions were saved by this operation. Earlier draft changes remain available.");
    expect(screen.getByRole("note")).not.toHaveTextContent("Saved draft changes remain available.");
    expect(screen.getByRole("note")).not.toHaveTextContent("last saved version");
  });
  it("keeps Stop saved changes uncertain until actual attribution arrives", () => {
    render(<ComposerOperationOutcomeNotice outcome={{ ...outcome, failure_code: "request_cancelled" }} />);
    expect(screen.getByRole("note")).toHaveTextContent("Saved progress has not been confirmed");
    expect(screen.getByRole("note")).not.toHaveTextContent("Saved draft changes remain available.");
    expect(screen.getByRole("note")).not.toHaveTextContent("last saved version");
  });
  it("does not attribute saved changes to a Stop before the operation started", () => {
    render(<ComposerOperationOutcomeNotice outcome={{ ...outcome, failure_code: "request_cancelled", saved_revisions: { attribution: "unstarted", count: 0, latest_state_id: null, latest_version: null } }} />);
    expect(screen.getByRole("note")).toHaveTextContent("This request stopped before composing began. No draft revisions were saved by this operation.");
    expect(screen.getByRole("note")).not.toHaveTextContent("Saved draft changes remain available.");
  });
});

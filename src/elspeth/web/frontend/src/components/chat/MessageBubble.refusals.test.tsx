import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MessageBubble } from "./MessageBubble";
import type { ChatMessage } from "@/types/index";
afterEach(cleanup);
function row(code: string): ChatMessage { return { id: "message", session_id: "session", role: "user", content: "hello", tool_calls: null, created_at: "2026-10-06T00:00:00Z", local_status: "failed", local_failure_code: code, local_error: "refused" }; }
it("permanent operation conflict has no inert Retry", () => {
  render(<MessageBubble message={row("composer_operation_conflict")} isComposing={false} onRetry={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
});
it("unknown outcome retains Refresh action", () => {
  render(<MessageBubble message={row("compose_outcome_unconfirmed")} isComposing={false} onRetry={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
});
it("paid overflow keeps the failed notice without an automatic Retry", () => {
  render(<MessageBubble message={{ ...row("paid_result_overflow"), local_error: "The call may have incurred a charge. Review the saved session." }} isComposing={false} onRetry={vi.fn()} />);
  expect(screen.getByText("The call may have incurred a charge. Review the saved session.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
});

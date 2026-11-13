import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "@/api/client";
import { detachComposerObservers } from "@/api/composerOperationObserver";
import { authenticateComposerCustody, purgeComposerCustody } from "@/stores/composerOperationCustody";
import { useSessionStore } from "@/stores/sessionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useExecutionStore } from "@/stores/executionStore";
import { resetStore } from "@/test/store-helpers";
import { makeComposition } from "@/test/composerFixtures";
import type { ChatMessage } from "@/types/api";
import { TutorialFreeformShell } from "./TutorialFreeformShell";

vi.mock("@/api/client");
const brief: ChatMessage = { id: "22222222-2222-4222-8222-222222222222", session_id: "11111111-1111-4111-8111-111111111111", role: "user", content: "Old stored brief, never the source for a new turn", tool_calls: null, created_at: "2026-11-13T12:00:00Z" };
const reply: ChatMessage = { ...brief, id: "reply-1", role: "assistant", content: "I can help with that." };

beforeEach(() => {
  vi.resetAllMocks();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  Element.prototype.scrollIntoView = vi.fn();
  resetStore(useSessionStore);
  resetStore(useInterpretationEventsStore);
  resetStore(useExecutionStore);
  purgeComposerCustody();
  sessionStorage.clear();
  authenticateComposerCustody({ principalId: "principal", authProvider: "local" });
  useSessionStore.setState({ activeSessionId: "11111111-1111-4111-8111-111111111111", compositionStateLoaded: true, messages: [brief] });
  vi.mocked(api.getTutorialSample).mockResolvedValue({ sample_urls: ["https://example.com/brief"] });
  vi.mocked(api.listInterpretationEvents).mockResolvedValue([]);
  vi.mocked(api.listBlobs).mockResolvedValue([]);
  // Hold the real operation in flight so double-click and cancellation exercise ownership.
  vi.mocked(api.submitComposerOperation).mockImplementation(() => new Promise(() => {}));
});
afterEach(() => { detachComposerObservers(); useSessionStore.getState().reset(); purgeComposerCustody(); });

async function resend() {
  render(<TutorialFreeformShell sessionId="11111111-1111-4111-8111-111111111111" onCompleted={vi.fn()} />);
  await screen.findByRole("button", { name: "Continue to Run" });
  expect(screen.queryByRole("textbox")).toBeNull();
  return screen.getByRole("button", { name: "Resend tutorial brief" });
}

it.each([
  { name: "failed compose", messages: [{ ...brief, local_status: "failed" as const, local_error: "Request failed" }] },
  { name: "reload before reply", messages: [{ ...brief, id: "33333333-3333-4333-8333-333333333333" }] },
  { name: "interrupted tool narration", messages: [brief, { ...reply, tool_calls: [{ id: "tool-1", type: "function" as const, function: { name: "list_plugins", arguments: "{}" } }] }] },
  { name: "latest unanswered turn in an older session", messages: [brief, reply, { ...brief, id: "44444444-4444-4444-8444-444444444444" }] },
])("retries by id after $name", async ({ messages }) => {
  useSessionStore.setState({ messages });
  const button = await resend();
  fireEvent.click(button);
  fireEvent.click(button);
  await waitFor(() => expect(api.submitComposerOperation).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.submitComposerOperation).mock.calls[0][0]).toMatchObject({
    kind: "compose_recompose", body: expect.objectContaining({ expected_user_message_id: [...messages].reverse().find((row) => row.role === "user")!.id }),
  });
});

it.each([
  { name: "text-only reply", response: reply },
  { name: "withheld reply", response: { ...reply, content: "The draft reply was withheld because required reviews remain incomplete." } },
  { name: "rejected proposal", response: { ...reply, content: "Here is the proposed pipeline for review." } },
])("sends the initial scripted brief as a new turn after $name", async ({ name, response }) => {
  // Capture the actual initial-button payload, so a divergent brief source fails.
  useSessionStore.setState({ messages: [] });
  const initial = render(<TutorialFreeformShell sessionId="11111111-1111-4111-8111-111111111111" onCompleted={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Send tutorial brief" }));
  await waitFor(() => expect(api.submitComposerOperation).toHaveBeenCalledTimes(1));
  const initialBody = vi.mocked(api.submitComposerOperation).mock.calls[0][0].body;
  initial.unmount();
  useSessionStore.getState().reset();
  purgeComposerCustody();
  authenticateComposerCustody({ principalId: "principal", authProvider: "local" });
  vi.mocked(api.submitComposerOperation).mockClear();
  useSessionStore.setState({ activeSessionId: "11111111-1111-4111-8111-111111111111", compositionStateLoaded: true, messages: [brief, response] });
  if (name === "rejected proposal") {
    useSessionStore.setState({ compositionProposals: [{ id: "rejected-1", session_id: "11111111-1111-4111-8111-111111111111", status: "rejected" } as import("@/types/api").CompositionProposal] });
  }
  const button = await resend();
  expect(button.closest(".tutorial-step-header-actions")).not.toBeNull();
  fireEvent.click(button);
  fireEvent.click(button);
  await waitFor(() => expect(api.submitComposerOperation).toHaveBeenCalledTimes(1));
  const descriptor = vi.mocked(api.submitComposerOperation).mock.calls[0][0];
  expect(descriptor.kind).toBe("compose_message");
  expect(descriptor.body).toEqual({ ...initialBody, operation_id: descriptor.operationId });
});

it("keeps Cancel during composition and prevents resend", async () => {
  const button = await resend();
  fireEvent.click(button);
  await waitFor(() => expect(api.submitComposerOperation).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("button", { name: "Resend tutorial brief" })).toBeNull();
  expect(screen.getByRole("button", { name: "Stop composing" })).toBeEnabled();
  fireEvent.click(button);
  expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
});

it("retains interrupted-request retry through the stored brief id", async () => {
  useSessionStore.setState({ composerProgress: {
    session_id: "11111111-1111-4111-8111-111111111111", request_id: brief.id, phase: "cancelled", reason: "client_cancelled",
    headline: "Cancelled", evidence: [], likely_next: "Retry when ready.",
    updated_at: "2026-11-13T12:00:00Z", inflight_requests: 0,
  } });
  await resend();
  fireEvent.click(screen.getByRole("button", { name: "Retry interrupted request" }));
  await waitFor(() => expect(api.submitComposerOperation).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.submitComposerOperation).mock.calls[0][0]).toMatchObject({ kind: "compose_recompose", body: { expected_user_message_id: brief.id } });
});

it("shows suggestions as text without Apply, Ask, repair, upload or an input", async () => {
  useSessionStore.setState({ compositionState: makeComposition(1, { validation_suggestions: [{ component: "pipeline", message: "Keep failed rows for review.", severity: "low" }] }) });
  useExecutionStore.setState({ validationResult: {
    is_valid: false, checks: [], errors: [], warnings: [],
    readiness: { authoring_valid: false, execution_ready: false, completion_ready: false, blockers: [{ code: "graph_structure", component_id: "pipeline", component_type: "pipeline", detail: "Graph needs repair", suggestion: null, note: null }] },
  } });
  render(<TutorialFreeformShell sessionId="11111111-1111-4111-8111-111111111111" onCompleted={vi.fn()} />);
  expect(await screen.findByText(/Keep failed rows for review/)).toBeInTheDocument();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.queryByRole("button", { name: /Apply|Ask.*composer|Edit the list|Use as input|Upload/i })).toBeNull();
  expect(screen.getByText("Continue to Run is blocked.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Resend tutorial brief" })).toBeNull();
  await act(async () => {});
});

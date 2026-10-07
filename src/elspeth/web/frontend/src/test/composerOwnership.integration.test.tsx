import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPanel } from "@/components/chat/ChatPanel";
import { SideRailValidationBanner } from "@/components/sidebar/SideRailValidationBanner";
import { useSessionStore } from "@/stores/sessionStore";
import { useExecutionStore } from "@/stores/executionStore";
import { useBlobStore } from "@/stores/blobStore";
import { resetStore } from "@/test/store-helpers";
import { makeComposition } from "@/test/composerFixtures";
import * as api from "@/api/client";
import { useComposer } from "@/hooks/useComposer";
import { detachComposerObservers } from "@/api/composerOperationObserver";
import { authenticateComposerCustody, findComposerOperationCustody, purgeComposerCustody } from "@/stores/composerOperationCustody";
import type { ComposerOperationSnapshot, SubmittedCustody } from "@/types/composerOperations";
import type { CompositionState } from "@/types/api";

vi.mock("@/api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/api/client")>(),
  submitComposerOperation: vi.fn(), fetchComposerOperation: vi.fn(),
  fetchComposerOperationStream: vi.fn(), cancelComposerOperation: vi.fn(),
  fetchCurrentUser: vi.fn(), fetchAuthConfig: vi.fn(),
  sendMessage: vi.fn(), recompose: vi.fn(), fetchMessages: vi.fn(),
  fetchComposerProgress: vi.fn(), fetchCompositionState: vi.fn(),
  fetchCompositionProposals: vi.fn(), fetchSessions: vi.fn(), listBlobs: vi.fn(),
  listInterpretationEvents: vi.fn(), fetchSystemStatus: vi.fn(),
  createSession: vi.fn(), fetchComposerPreferences: vi.fn(),
}));
const sessionA = "11111111-1111-4111-8111-111111111111";
const sessionB = "22222222-2222-4222-8222-222222222222";
const userId = "33333333-3333-4333-8333-333333333333";
const scope = { principalId: "test-user", authProvider: "local" };
interface HeldOperation { descriptor: SubmittedCustody; result: Promise<ComposerOperationSnapshot>; settle: (snapshot: ComposerOperationSnapshot) => void }
const held = new Map<string, HeldOperation>();
function composition(sessionId: string, version = 1): CompositionState {
  return { ...makeComposition(version), id: `55555555-5555-4555-8555-${String(version).padStart(12, "0")}`, session_id: sessionId };
}
function snapshot(descriptor: SubmittedCustody, status: "running" | "completed" | "failed"): ComposerOperationSnapshot {
  return {
    operation_id: descriptor.operationId, kind: descriptor.kind, status, poll_after_ms: 100,
    cancel_requested: status === "failed", deadline_at: "2030-01-01T00:00:00Z", deadline_remaining_ms: status === "running" ? 60000 : 0,
    result: status === "completed" ? { message: { id: "44444444-4444-4444-8444-444444444444", session_id: descriptor.sessionId, role: "assistant", content: `${descriptor.sessionId} completed`, tool_calls: null, created_at: "2026-10-06T00:00:00Z" }, state: composition(descriptor.sessionId, 2), proposals: [] } : null,
    error: status === "failed" ? { http_status: 409, failure_code: "request_cancelled", error_type: "request_cancelled", diagnostic_id: null, body: { error_type: "request_cancelled", detail: "Composer request stopped." } } : null,
  };
}
function operation(index = 0): HeldOperation { const descriptor = vi.mocked(api.submitComposerOperation).mock.calls[index][0]; return held.get(descriptor.operationId)!; }
async function settle(index: number, status: "completed" | "failed") { const job = operation(index); await act(async () => { job.settle(snapshot(job.descriptor, status)); }); }
async function admitted(count = 1) { await waitFor(() => expect(api.submitComposerOperation).toHaveBeenCalledTimes(count)); await waitFor(() => expect(api.fetchComposerOperation).toHaveBeenCalled()); }
function installUser() { useSessionStore.setState({ messages: [{ id: userId, session_id: sessionA, role: "user", content: "Retry this", tool_calls: null, created_at: "2026-10-06T00:00:00Z" }] }); }

describe("shared composer ownership across authoring surfaces", () => {
  beforeEach(() => {
    vi.resetAllMocks(); held.clear(); useSessionStore.getState().reset();
    resetStore(useSessionStore); resetStore(useBlobStore); useExecutionStore.getState().reset();
    sessionStorage.clear(); purgeComposerCustody(); authenticateComposerCustody(scope);
    Element.prototype.scrollTo = vi.fn(); Element.prototype.scrollIntoView = vi.fn();
    vi.mocked(api.submitComposerOperation).mockImplementation(async (descriptor) => {
      let resolve!: (value: ComposerOperationSnapshot) => void;
      const result = new Promise<ComposerOperationSnapshot>((settlement) => { resolve = settlement; });
      held.set(descriptor.operationId, { descriptor, result, settle: resolve });
      return { operation_id: descriptor.operationId, kind: descriptor.kind, status: "running", poll_after_ms: 100 };
    });
    vi.mocked(api.fetchComposerOperation).mockImplementation(async (_session, id) => held.get(id)!.result);
    vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(new Response(null, { status: 503 }));
    vi.mocked(api.cancelComposerOperation).mockImplementation(async (_session, id) => ({ ...snapshot(held.get(id)!.descriptor, "running"), cancel_requested: true }));
    vi.mocked(api.fetchCurrentUser).mockResolvedValue({ user_id: scope.principalId, username: "test-user", display_name: null, email: null, groups: [], dev_admin: false });
    vi.mocked(api.fetchAuthConfig).mockResolvedValue({ provider: "local", registration_mode: "closed", sso_start_url: null });
    vi.mocked(api.fetchMessages).mockResolvedValue([]); vi.mocked(api.fetchCompositionState).mockImplementation(async (sid) => composition(sid));
    vi.mocked(api.fetchCompositionProposals).mockResolvedValue([]); vi.mocked(api.fetchSessions).mockResolvedValue([]);
    vi.mocked(api.listBlobs).mockResolvedValue([]); vi.mocked(api.listInterpretationEvents).mockResolvedValue([]);
    vi.mocked(api.fetchComposerPreferences).mockImplementation(async (sid) => ({ session_id: sid, trust_mode: "auto_commit", density_default: "high", interpretation_review_disabled: false, updated_at: "2026-10-06T00:00:00Z" }));
    vi.mocked(api.createSession).mockResolvedValue({ id: sessionB, title: "New session", created_at: "2026-10-06T00:00:00Z", updated_at: "2026-10-06T00:00:00Z" });
    useSessionStore.setState({ activeSessionId: sessionA, compositionStateLoaded: true, compositionState: { ...composition(sessionA), validation_suggestions: [{ component: "source", message: "Use the normalized CSV field", severity: "info" }] } });
  });
  afterEach(() => { detachComposerObservers(); useSessionStore.getState().reset(); purgeComposerCustody(); });

  it.each(["checks", "chat", "checks_unmounted"])("Chat Stop requests cancellation of the durable action started from %s", async (surface) => {
    const view = render(<><SideRailValidationBanner /><ChatPanel /></>);
    if (surface !== "chat") fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    else { fireEvent.change(screen.getByRole("textbox", { name: "Message input" }), { target: { value: "Build a pipeline" } }); fireEvent.click(screen.getByRole("button", { name: "Send message" })); }
    await admitted();
    if (surface === "checks_unmounted") view.rerender(<ChatPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Stop composing" }));
    await waitFor(() => expect(api.cancelComposerOperation).toHaveBeenCalledWith(sessionA, operation().descriptor.operationId));
    expect(useSessionStore.getState().isComposing).toBe(true);
    expect(findComposerOperationCustody(scope, sessionA).foreground).not.toBeNull();
    await settle(0, "failed");
    await waitFor(() => expect(useSessionStore.getState().isComposing).toBe(false));
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
  });

  it.each(["success", "failure"])("old session %s cannot clear the new session's request owner", async (outcome) => {
    const hooks = renderHook(() => ({ first: useComposer(), second: useComposer() }));
    let oldRequest!: Promise<void>; act(() => { oldRequest = hooks.result.current.first.sendMessage("old request"); }); await admitted();
    await act(async () => { await useSessionStore.getState().createSession(); });
    let newRequest!: Promise<void>; act(() => { newRequest = hooks.result.current.second.sendMessage("new request"); }); await admitted(2);
    await settle(0, outcome === "success" ? "completed" : "failed"); await act(async () => { await oldRequest; });
    expect(useSessionStore.getState().isComposing).toBe(true); expect(useSessionStore.getState().activeSessionId).toBe(sessionB);
    expect(useSessionStore.getState().messages.some((row) => row.role === "assistant")).toBe(false);
    act(() => { hooks.result.current.first.cancelComposition(); });
    await waitFor(() => expect(api.cancelComposerOperation).toHaveBeenCalledWith(sessionB, operation(1).descriptor.operationId));
    await settle(1, "failed"); await act(async () => { await newRequest; });
    expect(useSessionStore.getState().isComposing).toBe(false);
  });

  it("retains cancellation ownership while the durable action settles across selection", async () => {
    const hooks = renderHook(() => useComposer()); let pending!: Promise<void>;
    act(() => { pending = hooks.result.current.sendMessage("old request"); }); await admitted();
    act(() => { hooks.result.current.cancelComposition(); });
    await waitFor(() => expect(api.cancelComposerOperation).toHaveBeenCalledTimes(1));
    await act(async () => { await useSessionStore.getState().selectSession(sessionB); }); expect(useSessionStore.getState().isComposing).toBe(false);
    await act(async () => { await useSessionStore.getState().selectSession(sessionA); });
    await waitFor(() => expect(useSessionStore.getState().isComposing).toBe(true));
    await settle(0, "failed"); await act(async () => { await pending; });
    await waitFor(() => expect(useSessionStore.getState().isComposing).toBe(false)); expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
  });

  it("another hook can stop a canonical retry after the originating hook unmounts", async () => {
    installUser(); const origin = renderHook(() => useComposer()); const chat = renderHook(() => useComposer()); let retry!: Promise<void>;
    act(() => { retry = origin.result.current.retryMessage(userId); }); await admitted(); origin.unmount();
    expect(operation().descriptor.body).toEqual({ operation_id: operation().descriptor.operationId, expected_user_message_id: userId, state_id: composition(sessionA).id });
    act(() => { chat.result.current.cancelComposition(); }); await waitFor(() => expect(api.cancelComposerOperation).toHaveBeenCalledTimes(1));
    expect(useSessionStore.getState().isComposing).toBe(true); await settle(0, "failed"); await act(async () => { await retry; });
    expect(api.sendMessage).not.toHaveBeenCalled(); expect(api.recompose).not.toHaveBeenCalled(); expect(useSessionStore.getState().isComposing).toBe(false);
  });

  it("another hook cannot start a canonical retry before the selected session head is loaded", async () => {
    installUser(); useSessionStore.setState({ compositionStateLoaded: false }); const origin = renderHook(() => useComposer()); const chat = renderHook(() => useComposer());
    await act(async () => { await origin.result.current.retryMessage(userId); }); origin.unmount();
    act(() => { chat.result.current.cancelComposition(); });
    expect(api.submitComposerOperation).not.toHaveBeenCalled(); expect(api.cancelComposerOperation).not.toHaveBeenCalled();
    expect(useSessionStore.getState().isComposing).toBe(false);
  });

  it("returning to A preserves Stop and reconciles A after B has composed", async () => {
    const hooks = renderHook(() => useComposer()); let requestA!: Promise<void>;
    act(() => { requestA = hooks.result.current.sendMessage("A request"); }); await admitted();
    await act(async () => { await useSessionStore.getState().createSession(); }); let requestB!: Promise<void>;
    act(() => { requestB = hooks.result.current.sendMessage("B request"); }); await admitted(2); await settle(1, "completed"); await act(async () => { await requestB; });
    await act(async () => { await useSessionStore.getState().selectSession(sessionA); }); await waitFor(() => expect(useSessionStore.getState().isComposing).toBe(true));
    const saved = composition(sessionA, 3); vi.mocked(api.fetchCompositionState).mockResolvedValue(saved);
    act(() => { hooks.result.current.cancelComposition(); }); await waitFor(() => expect(api.cancelComposerOperation).toHaveBeenCalledWith(sessionA, operation().descriptor.operationId));
    await settle(0, "failed"); await act(async () => { await requestA; }); await waitFor(() => expect(useSessionStore.getState().isComposing).toBe(false));
    expect(useSessionStore.getState().compositionState).toEqual(saved); expect(api.submitComposerOperation).toHaveBeenCalledTimes(2);
  });

  it("keeps admission visibly busy while a disconnected observer's server action settles", async () => {
    const hooks = renderHook(() => useComposer()); let pending!: Promise<void>;
    act(() => { pending = hooks.result.current.sendMessage("Assess data"); }); await admitted();
    expect(api.fetchComposerOperationStream).toHaveBeenCalled(); expect(hooks.result.current.isComposing).toBe(true);
    act(() => { hooks.result.current.cancelComposition(); }); await waitFor(() => expect(api.cancelComposerOperation).toHaveBeenCalledTimes(1));
    expect(hooks.result.current.isComposing).toBe(true); expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
    await settle(0, "failed"); await act(async () => { await pending; }); expect(useSessionStore.getState().isComposing).toBe(false);
  });
});

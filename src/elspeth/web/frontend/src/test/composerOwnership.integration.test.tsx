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
import type { MessageWithStateResponse } from "@/types/api";

vi.mock("@/api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/api/client")>(),
  sendMessage: vi.fn(), fetchMessages: vi.fn().mockResolvedValue([]),
  fetchComposerProgress: vi.fn().mockResolvedValue({
    session_id: "session-1", request_id: null, phase: "cancelled", inflight_requests: 0,
    headline: "Composition stopped", evidence: [], likely_next: "Retry when ready.",
    reason: "client_cancelled", updated_at: "2026-10-02T00:00:00Z",
  }),
  fetchCompositionState: vi.fn().mockResolvedValue(null),
  fetchCompositionProposals: vi.fn().mockResolvedValue([]),
  fetchSessions: vi.fn().mockResolvedValue([]), listBlobs: vi.fn().mockResolvedValue([]),
  listInterpretationEvents: vi.fn().mockResolvedValue([]), fetchSystemStatus: vi.fn(),
  createSession: vi.fn().mockResolvedValue({ id: "session-2", title: "New session", created_at: "2026-10-02T00:00:00Z", updated_at: "2026-10-02T00:00:00Z" }),
  fetchComposerPreferences: vi.fn().mockResolvedValue(null), recompose: vi.fn(),
}));

describe("shared composer ownership across authoring surfaces", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    useExecutionStore.getState().reset();
    Element.prototype.scrollTo = vi.fn();
    Element.prototype.scrollIntoView = vi.fn();
    useSessionStore.setState({
      activeSessionId: "session-1", composeTimeoutReady: true,
      compositionState: makeComposition(1, {
        validation_suggestions: [{ component: "source", message: "Use the normalized CSV field", severity: "info" }],
      }),
    });
  });

  afterEach(() => {
    useSessionStore.getState().stopComposerProgressPolling();
    useSessionStore.getState().stopInflightMessagesPolling();
  });

  it.each(["checks", "chat", "checks_unmounted"])("Chat Stop cancels a request started from %s", async (surface) => {
    let signal: AbortSignal | undefined;
    let rejectPost: (reason: unknown) => void = () => undefined;
    vi.mocked(api.sendMessage).mockImplementationOnce((_session, _content, _request, _state, requestSignal) => {
      signal = requestSignal;
      return new Promise((_resolve, reject) => {
        rejectPost = reject;
        requestSignal?.addEventListener("abort", () => reject(requestSignal.reason));
      });
    });
    const view = render(<><SideRailValidationBanner /><ChatPanel /></>);
    if (surface !== "chat") {
      fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    } else {
      fireEvent.change(screen.getByRole("textbox", { name: "Message input" }), { target: { value: "Build a pipeline" } });
      fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    }
    if (surface === "checks_unmounted") view.rerender(<ChatPanel />);
    expect(useSessionStore.getState().isComposing).toBe(true);
    try {
      fireEvent.click(screen.getByRole("button", { name: "Stop composing" }));
      expect(signal?.aborted).toBe(true);
      expect(signal?.reason).toBe("compose_user_cancel");
    } finally {
      await act(async () => { rejectPost("compose_user_cancel"); });
      await waitFor(() => expect(useSessionStore.getState().isComposing).toBe(false));
    }
  });

  it.each(["success", "failure"])("old session %s cannot clear the new session's request owner", async (outcome) => {
    let settleOld: (result: MessageWithStateResponse) => void = () => undefined;
    let rejectOld: (reason: unknown) => void = () => undefined;
    let oldSignal: AbortSignal | undefined;
    let newSignal: AbortSignal | undefined;
    vi.mocked(api.sendMessage)
      .mockImplementationOnce((_session, _content, _request, _state, signal) => {
        oldSignal = signal;
        return new Promise((resolve, reject) => { settleOld = resolve; rejectOld = reject; });
      })
      .mockImplementationOnce((_session, _content, _request, _state, signal) => {
        newSignal = signal;
        return new Promise((_resolve, reject) => signal?.addEventListener("abort", () => reject(signal.reason)));
      });
    const hooks = renderHook(() => ({ first: useComposer(), second: useComposer() }));
    let oldRequest: Promise<void> = Promise.resolve();
    let newRequest: Promise<void> = Promise.resolve();
    act(() => { oldRequest = hooks.result.current.first.sendMessage("old request"); });
    await act(async () => { await useSessionStore.getState().createSession(); });
    expect(useSessionStore.getState().isComposing).toBe(false);
    act(() => { newRequest = hooks.result.current.second.sendMessage("new request"); });
    await act(async () => {
      if (outcome === "success") settleOld({
        message: { id: "old-reply", session_id: "session-1", role: "assistant", content: "Old reply", tool_calls: null, created_at: "2026-10-02T00:00:01Z" },
        state: null, proposals: [],
      });
      else rejectOld({ status: 502, detail: "Old failure" });
      await oldRequest;
    });
    expect(oldSignal?.aborted).toBe(false);
    expect(newSignal?.aborted).toBe(false);
    expect(useSessionStore.getState().isComposing).toBe(true);
    expect(useSessionStore.getState().messages.map((message) => message.id)).not.toContain("old-reply");
    expect(useSessionStore.getState().error).toBeNull();
    await act(async () => { hooks.result.current.first.cancelComposition(); await newRequest; });
    expect(newSignal?.aborted).toBe(true);
    expect(useSessionStore.getState().isComposing).toBe(false);
  });

  it("retains cancellation ownership when an aborted request is still settling across selection", async () => {
    let signal: AbortSignal | undefined;
    let settleCancellation: (reason: unknown) => void = () => undefined;
    vi.mocked(api.sendMessage).mockImplementationOnce((_session, _content, _request, _state, requestSignal) => {
      signal = requestSignal;
      return new Promise((_resolve, reject) => { settleCancellation = reject; });
    });
    const hooks = renderHook(() => useComposer());
    let pending: Promise<void> = Promise.resolve();
    act(() => { pending = hooks.result.current.sendMessage("old request"); });
    act(() => { hooks.result.current.cancelComposition(); });
    expect(signal?.aborted).toBe(true);
    await act(async () => { await useSessionStore.getState().selectSession("session-2"); });
    expect(useSessionStore.getState().isComposing).toBe(false);
    await act(async () => { await useSessionStore.getState().selectSession("session-1"); });
    expect(useSessionStore.getState().isComposing).toBe(true);
    await act(async () => { settleCancellation(signal?.reason); await pending; });
    expect(useSessionStore.getState().isComposing).toBe(false);
  });

  it("another hook can stop a canonical retry after the originating hook unmounts", async () => {
    let signal: AbortSignal | undefined;
    vi.mocked(api.recompose).mockImplementationOnce((_session, _message, requestSignal) => {
      signal = requestSignal;
      return new Promise((_resolve, reject) => requestSignal?.addEventListener("abort", () => reject(requestSignal.reason)));
    });
    useSessionStore.setState({ messages: [{ id: "user-1", session_id: "session-1", role: "user", content: "Retry this", tool_calls: null, created_at: "2026-10-02T00:00:00Z" }] });
    const origin = renderHook(() => useComposer());
    const chat = renderHook(() => useComposer());
    let retry: Promise<void> = Promise.resolve();
    act(() => { retry = origin.result.current.retryMessage("user-1"); });
    origin.unmount();
    await act(async () => { chat.result.current.cancelComposition(); await retry; });
    expect(signal?.aborted).toBe(true);
    expect(api.sendMessage).not.toHaveBeenCalled();
    expect(useSessionStore.getState().isComposing).toBe(false);
  });

  it("returning to A preserves Stop and reconciles A after B has composed", async () => {
    let signal: AbortSignal | undefined;
    vi.mocked(api.sendMessage)
      .mockImplementationOnce((_session, _content, _request, _state, requestSignal) => {
        signal = requestSignal;
        return new Promise((_resolve, reject) => requestSignal?.addEventListener("abort", () => reject(requestSignal.reason)));
      })
      .mockResolvedValueOnce({
        message: { id: "b-reply", session_id: "session-2", role: "assistant", content: "B completed", tool_calls: null, created_at: "2026-10-02T00:00:01Z" },
        state: null, proposals: [],
      });
    const hooks = renderHook(() => useComposer());
    let requestA: Promise<void> = Promise.resolve();
    act(() => { requestA = hooks.result.current.sendMessage("A request"); });
    await act(async () => { await useSessionStore.getState().createSession(); });
    await act(async () => { await hooks.result.current.sendMessage("B request"); });
    expect(signal?.aborted).toBe(false);
    vi.mocked(api.fetchCompositionState).mockResolvedValue(makeComposition(1));
    await act(async () => { await useSessionStore.getState().selectSession("session-1"); });
    expect(useSessionStore.getState().isComposing).toBe(true);
    const savedDraft = makeComposition(2);
    vi.mocked(api.fetchCompositionState).mockResolvedValue(savedDraft);
    await act(async () => { hooks.result.current.cancelComposition(); await requestA; });
    expect(signal?.aborted).toBe(true);
    expect(useSessionStore.getState().compositionState).toEqual(savedDraft);
    expect(useSessionStore.getState().composerProgress?.phase).toBe("cancelled");
    expect(useSessionStore.getState().isComposing).toBe(false);
  });

  it("keeps compose admission visibly busy while a disconnected server request settles", async () => {
    let inflight = 1;
    vi.mocked(api.fetchComposerProgress).mockImplementation(async () => ({
      session_id: "session-1", request_id: null, phase: inflight === 0 ? "cancelled" : "calling_model",
      headline: "Settling request", evidence: [], likely_next: null,
      reason: inflight === 0 ? "client_cancelled" : null,
      updated_at: "2026-10-02T00:00:00Z", inflight_requests: inflight,
    }));
    vi.mocked(api.sendMessage).mockImplementationOnce((_session, _content, _request, _state, signal) =>
      new Promise((_resolve, reject) => signal?.addEventListener("abort", () => reject(signal.reason))),
    );
    const hooks = renderHook(() => useComposer());
    let pending: Promise<void> = Promise.resolve();
    act(() => { pending = hooks.result.current.sendMessage("Assess data"); });
    act(() => { hooks.result.current.cancelComposition(); });
    try {
      await waitFor(() => expect(api.fetchComposerProgress).toHaveBeenCalledTimes(2));
      expect(hooks.result.current.isComposing).toBe(true);
      expect(useSessionStore.getState().isComposing).toBe(true);
    } finally {
      inflight = 0;
      await act(async () => { await pending; });
    }
    expect(useSessionStore.getState().isComposing).toBe(false);
  });
});

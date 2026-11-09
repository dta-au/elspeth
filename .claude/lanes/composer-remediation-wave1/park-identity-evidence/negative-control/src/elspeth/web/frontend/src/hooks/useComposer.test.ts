import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { renderHook } from "@testing-library/react";

import { COMPOSE_CLIENT_GRACE_MS, COMPOSE_TIMEOUT_ABORT_REASON, COMPOSE_USER_CANCEL_ABORT_REASON } from "@/config/composer";
import { useComposer } from "@/hooks/useComposer";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";

describe("shared durable authoring hook", () => {
  beforeEach(() => {
    useSessionStore.setState({ activeSessionId: "session-1", compositionStateLoaded: true });
  });
  afterEach(() => {
    // The ceiling is module-level state; the readiness gate lives in the
    // store. Restore both so tests stay independent.

    resetStore(useSessionStore);
    vi.useRealTimers();
  });

  it("gates initial authoring on the selected session head", async () => { const send = vi.fn(); useSessionStore.setState({ compositionStateLoaded: false, sendMessage: send }); const { result } = renderHook(() => useComposer()); await result.current.sendMessage("hello"); expect(send).not.toHaveBeenCalled(); });

  it("admits a known-empty loaded session without a health-derived timeout", async () => { const send = vi.fn().mockResolvedValue(undefined); useSessionStore.setState({ compositionStateLoaded: true, compositionState: null, sendMessage: send }); const { result } = renderHook(() => useComposer()); await result.current.sendMessage("hello"); expect(send).toHaveBeenCalledWith("hello", expect.any(AbortSignal)); });

  it("does not create a mutable timeout ceiling from informational health fields", async () => { expect(COMPOSE_CLIENT_GRACE_MS).toBe(25000); const retry = vi.fn(); useSessionStore.setState({ compositionStateLoaded: false, retryMessage: retry }); const { result } = renderHook(() => useComposer()); await result.current.retryMessage("msg-1"); expect(retry).not.toHaveBeenCalled(); });

  it("keeps a durable turn pending when the informational timeout elapses", async () => {
    vi.useFakeTimers();

    let captured: AbortSignal | undefined;
    let finish = () => {};
    useSessionStore.setState({
      sendMessage: vi.fn(async (_content: string, signal?: AbortSignal) => {
        captured = signal;
        useSessionStore.setState({ isComposing: true });
        await new Promise<void>((resolve) => { finish = resolve; });
        useSessionStore.setState({ isComposing: false });
      }),
    });
    const { result } = renderHook(() => useComposer());
    const sendPromise = result.current.sendMessage("hello");
    await vi.advanceTimersByTimeAsync(300_000 + COMPOSE_CLIENT_GRACE_MS + 1);
    expect(captured?.aborted).toBe(false);
    expect(useSessionStore.getState().isComposing).toBe(true);
    finish();
    await sendPromise;
    expect(useSessionStore.getState().isComposing).toBe(false);
  });

  it("uses distinct abort reasons for timeout and user cancel paths", () => {
    expect(COMPOSE_TIMEOUT_ABORT_REASON).not.toBe(COMPOSE_USER_CANCEL_ABORT_REASON);
  });

  it("does not start a send before authoritative composition state is loaded", async () => {
    // The gate is the client-outlives-server invariant during boot: before
    // GET /api/system/status supplies the wall clock, no request may start —
    // it would schedule an abort from the stale default ceiling.
    const storeSend = vi.fn();
    useSessionStore.setState({ compositionStateLoaded: false, sendMessage: storeSend });

    const { result } = renderHook(() => useComposer());
    await result.current.sendMessage("hello");

    expect(storeSend).not.toHaveBeenCalled();
  });

  it("does not start a retry before authoritative composition state is loaded", async () => {
    const storeRetry = vi.fn();
    useSessionStore.setState({ compositionStateLoaded: false, retryMessage: storeRetry });

    const { result } = renderHook(() => useComposer());
    await result.current.retryMessage("msg-1");

    expect(storeRetry).not.toHaveBeenCalled();
  });

  it("starts send and retry once the readiness gate opens", async () => {

    const storeSend = vi.fn().mockResolvedValue(undefined);
    const storeRetry = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({
      sendMessage: storeSend,
      retryMessage: storeRetry,
    });

    const { result } = renderHook(() => useComposer());
    await result.current.sendMessage("hello");
    await result.current.retryMessage("msg-1");

    expect(storeSend).toHaveBeenCalledWith("hello", expect.any(AbortSignal));
    expect(storeRetry).toHaveBeenCalledWith("msg-1", expect.any(AbortSignal));
  });

  it("keeps Stop bound to the active compose when a raced entry is refused (elspeth-3f38ebb1b5)", async () => {

    let firstSignal: AbortSignal | undefined;
    // Mirror the real store contract: the admission gate refuses when a
    // compose is in flight, and isComposing is set synchronously before any
    // await.
    const storeSend = vi.fn(async (_content: string, signal?: AbortSignal) => {
      if (useSessionStore.getState().isComposing) return;
      useSessionStore.setState({ isComposing: true });
      firstSignal = signal;
      await new Promise<void>((resolve) => {
        signal?.addEventListener("abort", () => resolve());
      });
      useSessionStore.setState({ isComposing: false });
    });
    useSessionStore.setState({ sendMessage: storeSend });

    const { result } = renderHook(() => useComposer());
    const firstPromise = result.current.sendMessage("first");
    await Promise.resolve();

    // A raced second entry (Retry, Use-as-input) is refused — and the
    // refusal must NOT displace the first compose's AbortController:
    // runComposeWithTimeout would otherwise install (then clear) a fresh
    // controller, leaving Stop a no-op against the running compose.
    await result.current.sendMessage("second");

    result.current.cancelComposition();
    expect(firstSignal?.aborted).toBe(true);
    expect(firstSignal?.reason).toBe(COMPOSE_USER_CANCEL_ABORT_REASON);
    await firstPromise;
  });
});

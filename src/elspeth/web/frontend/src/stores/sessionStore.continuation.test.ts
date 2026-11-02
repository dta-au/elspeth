import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { COMPOSER_STOP_UNAVAILABLE_COPY, useSessionStore } from "./sessionStore";
import { adoptComposerObserver, authenticateComposerCustody, composerStopRequested, findComposerOperationCustody, purgeComposerCustody, requestComposerStop } from "./composerOperationCustody";
import { detachComposerObservers } from "@/api/composerOperationObserver";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
import type { CompositionState } from "@/types/index";
import { completedSnapshot, liveSnapshot, streamUnavailable } from "@/test/composerOperationWire";
import type { ComposerOperationSnapshot, ComposerOperationStatus } from "@/types/composerOperations";
import { sseFrame } from "@/test/composerOperationWire";
vi.mock("@/api/client", () => ({ submitComposerOperation: vi.fn(), fetchComposerOperation: vi.fn(), fetchComposerOperationFailures: vi.fn(), cancelComposerOperation: vi.fn(), fetchComposerOperationStream: vi.fn(), apiErrorFromBody: vi.fn(), composerActiveOperation: vi.fn(), isDefinitiveComposerRefusal: vi.fn(), fetchMessages: vi.fn(), fetchCompositionState: vi.fn(), fetchCompositionProposals: vi.fn(), fetchComposerPreferences: vi.fn(), revertToVersion: vi.fn(), fetchStateVersions: vi.fn(), fetchSessions: vi.fn(), fetchCurrentUser: vi.fn(), fetchAuthConfig: vi.fn(), listBlobs: vi.fn(), listInterpretationEvents: vi.fn(), fetchActiveComposerOperation: vi.fn() }));
vi.mock("./executionStore", () => ({ useExecutionStore: { getState: () => ({ clearValidation: vi.fn() }) } }));
import * as api from "@/api/client";

const sid = "11111111-1111-4111-8111-111111111111", stateId = "44444444-4444-4444-8444-444444444444";
const scope = { principalId: "principal", authProvider: "local" };
const operationId = "55555555-5555-4555-8555-555555555555";
function composition(): CompositionState { return { ...compositionStateAuthorityFields, id: stateId, session_id: sid, version: 1, sources: {}, nodes: [], edges: [], outputs: [], metadata: { name: null, description: null } }; }
function continuationStatus(): ComposerOperationStatus {
  return { operation_id: operationId, kind: "compose_message", status: "running", phase: "executing", status_revision: 4, created_at: new Date().toISOString(), observed_at: new Date().toISOString(), poll_after_ms: 1000, cancel_requested: false, segment: { sequence: 1, generation: 1, state: "running", deadline_at: new Date(Date.now() + 60_000).toISOString() } };
}
function completedContinuation(): ComposerOperationSnapshot {
  return { ...continuationStatus(), status: "completed", phase: "settled", status_revision: 5, segment: null, result: { message: { id: stateId, session_id: sid, role: "assistant", content: "done", tool_calls: null, created_at: new Date().toISOString() }, state: composition(), proposals: [] }, error: null };
}
beforeEach(() => {
  vi.resetAllMocks(); useSessionStore.getState().reset(); purgeComposerCustody(); sessionStorage.clear(); authenticateComposerCustody(scope);
  useSessionStore.setState({ activeSessionId: sid, compositionStateLoaded: true, compositionState: composition() });
  vi.mocked(api.fetchComposerOperationFailures).mockImplementation(async (sessionId) => ({ session_id: sessionId, items: [], next_cursor: null }));
  vi.mocked(api.composerActiveOperation).mockReturnValue(null);
  vi.mocked(api.isDefinitiveComposerRefusal).mockImplementation((error) => typeof error === "object" && error !== null && "status" in error && error.status === 409);
  vi.mocked(api.fetchMessages).mockResolvedValue([]); vi.mocked(api.fetchCompositionState).mockResolvedValue(composition()); vi.mocked(api.fetchCompositionProposals).mockResolvedValue([]); vi.mocked(api.listInterpretationEvents).mockResolvedValue([]);
  vi.mocked(api.fetchCurrentUser).mockResolvedValue({ user_id: scope.principalId, username: "principal", display_name: null, email: null, groups: [], dev_admin: false });
  vi.mocked(api.fetchAuthConfig).mockResolvedValue({ provider: "local", registration_mode: "closed", sso_start_url: null });
});
afterEach(() => { detachComposerObservers(); vi.useRealTimers(); purgeComposerCustody(); });

describe("accepted operation status owns visible progress", () => {
  function status(revision: number, sequence: number, generation = 1): ComposerOperationStatus {
    return { ...continuationStatus(), status_revision: revision, segment: { sequence, generation, state: "running", deadline_at: new Date(Date.now() + 60_000).toISOString() } };
  }
  function progress(sequence: number, generation = 1, headline = `Working on segment ${sequence}`) {
    return { request_id: `request-${sequence}-${generation}`, phase: "calling_model", headline, evidence: [], likely_next: null, reason: null, updated_at: new Date().toISOString(), segment_sequence: sequence, segment_generation: generation };
  }
  function controlledStream(signal: AbortSignal) {
    let enqueue!: (event: string, sequence: number, payload?: unknown, revision?: number) => void;
    const response = new Response(new ReadableStream<Uint8Array>({ start(controller) {
      enqueue = (event, sequence, payload, revision) => controller.enqueue(new TextEncoder().encode(sseFrame(sid, operationId, event, sequence, payload, revision)));
      signal.addEventListener("abort", () => controller.error(new Error("observer detached")), { once: true });
    } }), { headers: { "Content-Type": "text/event-stream" } });
    return { response, enqueue };
  }
  beforeEach(() => {
    vi.useFakeTimers();
    adoptComposerObserver(scope, sid, { operation_id: operationId, kind: "compose_message" });
  });

  it.each(["successor", "new generation", "reconnect", "durable GET"])("retires predecessor progress on %s before any new progress exists", async (transition) => {
    const streams: ReturnType<typeof controlledStream>[] = [];
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (_session, _operation, signal) => {
      const stream = controlledStream(signal); streams.push(stream); return stream.response;
    });
    await useSessionStore.getState().resumeComposerOperation(sid);
    await vi.advanceTimersByTimeAsync(0);
    const old = status(4, 1), next = status(5, transition === "new generation" ? 1 : 2, transition === "new generation" ? 2 : 1);
    streams[0].enqueue("status", 0, old, 4);
    streams[0].enqueue("progress", 1, progress(1));
    await vi.advanceTimersByTimeAsync(0);
    expect(useSessionStore.getState().composerProgress?.headline).toBe("Working on segment 1");
    if (transition === "durable GET") {
      vi.mocked(api.fetchComposerOperation).mockResolvedValue({ ...next, result: null, error: null });
      streams[0].enqueue("terminal", 2, { status: "completed", phase: "settled", status_revision: 5 }, 5);
    } else if (transition === "reconnect") {
      streams[0].enqueue("reconnect", 2);
      await vi.advanceTimersByTimeAsync(0);
      expect(streams).toHaveLength(2);
      streams[1].enqueue("status", 0, next, 5);
    } else streams[0].enqueue("status", 2, next, 5);
    await vi.advanceTimersByTimeAsync(0);
    expect(useSessionStore.getState().composerOperationStatus?.segment).toEqual(next.segment);
    expect(useSessionStore.getState().composerProgress).toBeNull();
    expect(useSessionStore.getState().isComposing).toBe(true);
    expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(operationId);
    detachComposerObservers(); await vi.advanceTimersByTimeAsync(0);
  });

  it("drops unbound and mismatched progress while preserving the accepted generation across rejected statuses", async () => {
    let stream!: ReturnType<typeof controlledStream>;
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (_session, _operation, signal) => {
      stream = controlledStream(signal); return stream.response;
    });
    await useSessionStore.getState().resumeComposerOperation(sid);
    await vi.advanceTimersByTimeAsync(0);
    stream.enqueue("progress", 0, progress(1));
    await vi.advanceTimersByTimeAsync(0);
    expect(useSessionStore.getState().composerProgress).toBeNull();
    const accepted = status(8, 2, 3);
    stream.enqueue("status", 1, accepted, 8);
    stream.enqueue("progress", 2, progress(1, 3, "Wrong segment"));
    stream.enqueue("progress", 3, progress(2, 2, "Wrong generation"));
    await vi.advanceTimersByTimeAsync(0);
    expect(useSessionStore.getState().composerProgress).toBeNull();
    stream.enqueue("progress", 4, progress(2, 3, "Current generation"));
    await vi.advanceTimersByTimeAsync(0);
    const currentProgress = useSessionStore.getState().composerProgress;
    expect(currentProgress?.headline).toBe("Current generation");
    stream.enqueue("status", 5, status(7, 1), 7);
    stream.enqueue("status", 6, accepted, 8);
    stream.enqueue("status", 7, { ...accepted, status_revision: 9, phase: "reconciling" }, 9);
    stream.enqueue("progress", 8, progress(2, 2, "Old generation returned"));
    await vi.advanceTimersByTimeAsync(0);
    expect(useSessionStore.getState().composerOperationStatus?.status_revision).toBe(9);
    expect(useSessionStore.getState().composerProgress).toBe(currentProgress);
    detachComposerObservers(); await vi.advanceTimersByTimeAsync(0);
  });
});

describe("adopting a live operation without custody (D-8)", () => {
  const activeId = "55555555-5555-4555-8555-555555555555";
  beforeEach(() => {
    vi.mocked(api.fetchCurrentUser).mockResolvedValue({ user_id: scope.principalId, username: "principal", display_name: null, email: null, groups: [], dev_admin: false });
    vi.mocked(api.fetchAuthConfig).mockResolvedValue({ provider: "local", registration_mode: "closed", sso_start_url: null });
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async () => streamUnavailable());
  });
  it("observes the session's live operation reported by the server", async () => {
    vi.mocked(api.fetchActiveComposerOperation).mockResolvedValue({ operation_id: activeId, kind: "compose_message", status: "running" });
    vi.mocked(api.fetchComposerOperation).mockResolvedValue(liveSnapshot(activeId, { poll_after_ms: 1000 }));
    await useSessionStore.getState().resumeComposerOperation(sid);
    await vi.waitFor(() => expect(api.fetchComposerOperation).toHaveBeenCalledWith(sid, activeId));
    expect(useSessionStore.getState().isComposing).toBe(true);
  });
  it("does nothing when the session is idle", async () => {
    vi.mocked(api.fetchActiveComposerOperation).mockResolvedValue(null);
    await useSessionStore.getState().resumeComposerOperation(sid);
    expect(useSessionStore.getState().isComposing).toBe(false);
    expect(api.fetchComposerOperation).not.toHaveBeenCalled();
  });
});

describe("Stop state is scoped to its operation (R9)", () => {
  it("shows an explicit Stop refusal while retaining observation until the natural terminal", async () => {
    const descriptor = adoptComposerObserver(scope, sid, { operation_id: operationId, kind: "compose_message" })!;
    requestComposerStop(descriptor);
    vi.mocked(api.cancelComposerOperation).mockRejectedValue({ status: 409, error_type: "composer_continuation_stop_unavailable", detail: "Stopping is unavailable." });
    const frame = { session_id: sid, operation_id: operationId, sequence: 0, event: "terminal", payload: { status: "completed", phase: "settled", status_revision: 5 } };
    vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(new Response(`id: 5\nevent: terminal\ndata: ${JSON.stringify(frame)}\n\n`, { headers: { "Content-Type": "text/event-stream" } }));
    let release!: (read: ComposerOperationSnapshot) => void;
    vi.mocked(api.fetchComposerOperation).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await useSessionStore.getState().resumeComposerOperation(sid);
    try {
      await vi.waitFor(() => expect(api.fetchComposerOperation).toHaveBeenCalledTimes(1));
      expect(useSessionStore.getState().error).toBe(COMPOSER_STOP_UNAVAILABLE_COPY);
      expect(useSessionStore.getState().composerStopPendingOperationId).toBeNull();
      expect(useSessionStore.getState().isComposing).toBe(true);
      expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(operationId);
      expect(composerStopRequested(descriptor)).toBe(false);
      release(completedContinuation());
      await vi.waitFor(() => expect(useSessionStore.getState().isComposing).toBe(false));
      expect(useSessionStore.getState().messages.some((message) => message.content === "done")).toBe(true);
      expect(api.cancelComposerOperation).toHaveBeenCalledTimes(1);
      expect(api.submitComposerOperation).not.toHaveBeenCalled();
    } finally {
      release(completedContinuation());
      detachComposerObservers();
    }
  });
  it("clears prior-session display before delayed or failed hydration and restores only the selected pending Stop", async () => {
    vi.useFakeTimers();
    const descriptor = adoptComposerObserver(scope, sid, { operation_id: operationId, kind: "compose_message" })!;
    requestComposerStop(descriptor);
    let releaseOldCancel!: (snapshot: ComposerOperationSnapshot) => void;
    vi.mocked(api.cancelComposerOperation).mockReturnValueOnce(new Promise((resolve) => { releaseOldCancel = resolve; }));
    await useSessionStore.getState().resumeComposerOperation(sid);
    await vi.advanceTimersByTimeAsync(0);
    expect(api.cancelComposerOperation).toHaveBeenCalledTimes(1);
    useSessionStore.setState({ composerOperationStatus: { ...continuationStatus(), cancel_requested: true }, composerStopPendingOperationId: operationId, composerLiveUpdatesInterrupted: true });
    const other = "77777777-7777-4777-8777-777777777777";
    adoptComposerObserver(scope, other, { operation_id: stateId, kind: "compose_message" });
    let failHydration!: (error: Error) => void;
    vi.mocked(api.fetchMessages).mockReturnValueOnce(new Promise((_resolve, reject) => { failHydration = reject; }));
    const switching = useSessionStore.getState().selectSession(other);
    expect(useSessionStore.getState().isComposing).toBe(true);
    expect(useSessionStore.getState().composerOperationStatus).toBeNull();
    expect(useSessionStore.getState().composerStopPendingOperationId).toBeNull();
    expect(useSessionStore.getState().composerLiveUpdatesInterrupted).toBe(false);
    failHydration(new Error("session read failed")); await switching;
    releaseOldCancel({ ...continuationStatus(), cancel_requested: true, result: null, error: null });
    await vi.advanceTimersByTimeAsync(0);
    expect(useSessionStore.getState().activeSessionId).toBe(other);
    expect(useSessionStore.getState().composerOperationStatus).toBeNull();
    expect(useSessionStore.getState().composerStopPendingOperationId).toBeNull();
    expect(useSessionStore.getState().composerLiveUpdatesInterrupted).toBe(false);
    expect(composerStopRequested(descriptor)).toBe(true);
    vi.mocked(api.cancelComposerOperation).mockRejectedValue(new Error("offline"));
    await useSessionStore.getState().selectSession(sid);
    await vi.advanceTimersByTimeAsync(0);
    expect(useSessionStore.getState().composerStopPendingOperationId).toBe(operationId);
    expect(composerStopRequested(descriptor)).toBe(true);
    expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(operationId);
    expect(api.cancelComposerOperation).toHaveBeenCalledTimes(2);
    detachComposerObservers(); await vi.advanceTimersByTimeAsync(0);
  });
  it("a stopped request does not leave a later request showing Stopping", async () => {
    vi.mocked(api.submitComposerOperation).mockImplementation(async (descriptor) => liveSnapshot(descriptor.operationId, { kind: descriptor.kind, status: "queued", phase: "queued" }));
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async () => streamUnavailable());
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const settled = async (_s: string, operationId: string) => {
      await gate;
      return completedSnapshot(operationId, { message: { id: crypto.randomUUID(), session_id: sid, role: "assistant", content: "ok", tool_calls: null, created_at: new Date().toISOString() }, state: composition(), proposals: [] } as unknown as NonNullable<ComposerOperationSnapshot["result"]>);
    };
    vi.mocked(api.fetchComposerOperation).mockImplementation(settled);
    vi.mocked(api.cancelComposerOperation).mockImplementation(settled);
    const first = useSessionStore.getState().sendMessage("first");
    await vi.waitFor(() => expect(useSessionStore.getState().isComposing).toBe(true));
    useSessionStore.getState().cancelComposition();
    expect(useSessionStore.getState().composerStopPendingOperationId).not.toBeNull();
    release(); await first;
    expect(useSessionStore.getState().composerStopPendingOperationId).toBeNull();
  });
});

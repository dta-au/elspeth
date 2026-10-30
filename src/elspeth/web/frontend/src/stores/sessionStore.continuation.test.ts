import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { COMPOSER_CONTINUATION_DISABLED_COPY, COMPOSER_STOP_UNAVAILABLE_COPY, useSessionStore } from "./sessionStore";
import { adoptComposerObserver, authenticateComposerCustody, composerStopRequested, findComposerOperationCustody, purgeComposerCustody, requestComposerStop } from "./composerOperationCustody";
import { detachComposerObservers } from "@/api/composerOperationObserver";
import { composerAdmissionVersion, setComposerAdmissionVersion } from "@/config/composerProtocol";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
import type { CompositionState } from "@/types/index";
import type { ComposerOperationSnapshotV2, ComposerOperationStatusV2 } from "@/types/composerOperations";
vi.mock("@/api/client", () => ({ submitComposerOperation: vi.fn(), fetchComposerOperation: vi.fn(), fetchComposerOperationFailures: vi.fn(), cancelComposerOperation: vi.fn(), fetchComposerOperationStream: vi.fn(), cancelComposerOperationV2: vi.fn(), fetchComposerOperationStreamV2: vi.fn(), fetchComposerOperationAnyVersion: vi.fn(), apiErrorFromBody: vi.fn(), composerActiveOperation: vi.fn(), isDefinitiveComposerRefusal: vi.fn(), fetchMessages: vi.fn(), fetchCompositionState: vi.fn(), fetchCompositionProposals: vi.fn(), fetchComposerPreferences: vi.fn(), revertToVersion: vi.fn(), fetchStateVersions: vi.fn(), fetchSessions: vi.fn(), fetchCurrentUser: vi.fn(), fetchAuthConfig: vi.fn(), listBlobs: vi.fn(), listInterpretationEvents: vi.fn(), fetchActiveComposerOperation: vi.fn() }));
vi.mock("./executionStore", () => ({ useExecutionStore: { getState: () => ({ clearValidation: vi.fn() }) } }));
import * as api from "@/api/client";

const sid = "11111111-1111-4111-8111-111111111111", stateId = "44444444-4444-4444-8444-444444444444";
const scope = { principalId: "principal", authProvider: "local" };
const operationId = "55555555-5555-4555-8555-555555555555";
function composition(): CompositionState { return { ...compositionStateAuthorityFields, id: stateId, session_id: sid, version: 1, sources: {}, nodes: [], edges: [], outputs: [], metadata: { name: null, description: null } }; }
function continuationStatus(): ComposerOperationStatusV2 {
  return { protocol_version: 2, operation_id: operationId, kind: "compose_message", status: "running", phase: "executing", status_revision: 4, created_at: new Date().toISOString(), observed_at: new Date().toISOString(), poll_after_ms: 1000, cancel_requested: false, segment: { sequence: 1, generation: 1, state: "running", deadline_at: new Date(Date.now() + 60_000).toISOString() } };
}
function completedContinuation(): ComposerOperationSnapshotV2 {
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
afterEach(() => { detachComposerObservers(); vi.useRealTimers(); setComposerAdmissionVersion(1); purgeComposerCustody(); });

describe("continuation-disabled refusal (F-11)", () => {
  it("keeps the request as a retryable failed message, falls back to version 1 and does not resend", async () => {
    setComposerAdmissionVersion(2);
    vi.mocked(api.submitComposerOperation).mockRejectedValue({ status: 409, error_type: "composer_continuation_disabled", detail: "New continuation requests are disabled." });
    await useSessionStore.getState().sendMessage("Please build my pipeline");
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.submitComposerOperation).mock.calls[0][0].protocolVersion).toBe(2);
    expect(composerAdmissionVersion()).toBe(1);
    const row = useSessionStore.getState().messages.find((message) => message.role === "user");
    expect(row?.content).toBe("Please build my pipeline");
    expect(row?.local_status).toBe("failed");
    expect(row?.local_error).toBe(COMPOSER_CONTINUATION_DISABLED_COPY);
  });
});

describe("adopting a live operation without custody (D-8)", () => {
  const activeId = "55555555-5555-4555-8555-555555555555";
  beforeEach(() => {
    vi.mocked(api.fetchCurrentUser).mockResolvedValue({ user_id: scope.principalId, username: "principal", display_name: null, email: null, groups: [], dev_admin: false });
    vi.mocked(api.fetchAuthConfig).mockResolvedValue({ provider: "local", registration_mode: "closed", sso_start_url: null });
    vi.mocked(api.fetchComposerOperationStream).mockRejectedValue(new Error("stream unavailable"));
  });
  it("observes the session's live operation reported by the server", async () => {
    vi.mocked(api.fetchActiveComposerOperation).mockResolvedValue({ operation_id: activeId, kind: "compose_message", lifecycle_version: 1, status: "running" });
    vi.mocked(api.fetchComposerOperation).mockResolvedValue({ operation_id: activeId, kind: "compose_message", status: "running", cancel_requested: false, poll_after_ms: 1000, deadline_at: new Date(Date.now() + 60_000).toISOString(), deadline_remaining_ms: 60_000, result: null, error: null });
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
    const descriptor = adoptComposerObserver(scope, sid, { operation_id: operationId, kind: "compose_message", lifecycle_version: 2 })!;
    requestComposerStop(descriptor);
    vi.mocked(api.cancelComposerOperationV2).mockRejectedValue({ status: 409, error_type: "composer_continuation_stop_unavailable", detail: "Stopping is unavailable." });
    const frame = { schema_version: "composer-operation-stream.v2", session_id: sid, operation_id: operationId, sequence: 0, event: "terminal", payload: { status: "completed", phase: "settled", status_revision: 5 } };
    vi.mocked(api.fetchComposerOperationStreamV2).mockResolvedValue(new Response(`id: 5\nevent: terminal\ndata: ${JSON.stringify(frame)}\n\n`, { headers: { "Content-Type": "text/event-stream" } }));
    let release!: (read: { protocolVersion: 2; snapshot: ComposerOperationSnapshotV2 }) => void;
    vi.mocked(api.fetchComposerOperationAnyVersion).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await useSessionStore.getState().resumeComposerOperation(sid);
    try {
      await vi.waitFor(() => expect(api.fetchComposerOperationAnyVersion).toHaveBeenCalledTimes(1));
      expect(useSessionStore.getState().error).toBe(COMPOSER_STOP_UNAVAILABLE_COPY);
      expect(useSessionStore.getState().composerStopPendingOperationId).toBeNull();
      expect(useSessionStore.getState().isComposing).toBe(true);
      expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(operationId);
      expect(composerStopRequested(descriptor)).toBe(false);
      release({ protocolVersion: 2, snapshot: completedContinuation() });
      await vi.waitFor(() => expect(useSessionStore.getState().isComposing).toBe(false));
      expect(useSessionStore.getState().messages.some((message) => message.content === "done")).toBe(true);
      expect(api.cancelComposerOperationV2).toHaveBeenCalledTimes(1);
      expect(api.submitComposerOperation).not.toHaveBeenCalled();
    } finally {
      release({ protocolVersion: 2, snapshot: completedContinuation() });
      detachComposerObservers();
    }
  });
  it("clears prior-session display before delayed or failed hydration and restores only the selected pending Stop", async () => {
    vi.useFakeTimers();
    const descriptor = adoptComposerObserver(scope, sid, { operation_id: operationId, kind: "compose_message", lifecycle_version: 2 })!;
    requestComposerStop(descriptor);
    let releaseOldCancel!: (snapshot: ComposerOperationSnapshotV2) => void;
    vi.mocked(api.cancelComposerOperationV2).mockReturnValueOnce(new Promise((resolve) => { releaseOldCancel = resolve; }));
    await useSessionStore.getState().resumeComposerOperation(sid);
    await vi.advanceTimersByTimeAsync(0);
    expect(api.cancelComposerOperationV2).toHaveBeenCalledTimes(1);
    useSessionStore.setState({ composerOperationStatusV2: { ...continuationStatus(), cancel_requested: true }, composerStopPendingOperationId: operationId, composerLiveUpdatesInterrupted: true });
    const other = "77777777-7777-4777-8777-777777777777";
    adoptComposerObserver(scope, other, { operation_id: stateId, kind: "compose_message", lifecycle_version: 2 });
    let failHydration!: (error: Error) => void;
    vi.mocked(api.fetchMessages).mockReturnValueOnce(new Promise((_resolve, reject) => { failHydration = reject; }));
    const switching = useSessionStore.getState().selectSession(other);
    expect(useSessionStore.getState().isComposing).toBe(true);
    expect(useSessionStore.getState().composerOperationStatusV2).toBeNull();
    expect(useSessionStore.getState().composerStopPendingOperationId).toBeNull();
    expect(useSessionStore.getState().composerLiveUpdatesInterrupted).toBe(false);
    failHydration(new Error("session read failed")); await switching;
    releaseOldCancel({ ...continuationStatus(), cancel_requested: true, result: null, error: null });
    await vi.advanceTimersByTimeAsync(0);
    expect(useSessionStore.getState().activeSessionId).toBe(other);
    expect(useSessionStore.getState().composerOperationStatusV2).toBeNull();
    expect(useSessionStore.getState().composerStopPendingOperationId).toBeNull();
    expect(useSessionStore.getState().composerLiveUpdatesInterrupted).toBe(false);
    expect(composerStopRequested(descriptor)).toBe(true);
    vi.mocked(api.cancelComposerOperationV2).mockRejectedValue(new Error("offline"));
    await useSessionStore.getState().selectSession(sid);
    await vi.advanceTimersByTimeAsync(0);
    expect(useSessionStore.getState().composerStopPendingOperationId).toBe(operationId);
    expect(composerStopRequested(descriptor)).toBe(true);
    expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(operationId);
    expect(api.cancelComposerOperationV2).toHaveBeenCalledTimes(2);
    detachComposerObservers(); await vi.advanceTimersByTimeAsync(0);
  });
  it("a stopped v1 request does not leave a later request showing Stopping", async () => {
    vi.mocked(api.submitComposerOperation).mockImplementation(async (descriptor) => ({ operation_id: descriptor.operationId, kind: descriptor.kind, status: "queued", poll_after_ms: 1000 }));
    vi.mocked(api.fetchComposerOperationStream).mockRejectedValue(new Error("stream unavailable"));
    vi.mocked(api.cancelComposerOperation).mockResolvedValue({ kind: "operation_missing" });
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    vi.mocked(api.fetchComposerOperation).mockImplementation(async (_s, operationId) => {
      await gate;
      return { operation_id: operationId, kind: "compose_message", status: "completed", cancel_requested: false, poll_after_ms: 1000, deadline_at: new Date().toISOString(), deadline_remaining_ms: 0, result: { message: { id: crypto.randomUUID(), session_id: sid, role: "assistant", content: "ok", tool_calls: null, created_at: new Date().toISOString() }, state: composition(), proposals: [] }, error: null };
    });
    const first = useSessionStore.getState().sendMessage("first");
    await vi.waitFor(() => expect(useSessionStore.getState().isComposing).toBe(true));
    useSessionStore.getState().cancelComposition();
    expect(useSessionStore.getState().composerStopPendingOperationId).not.toBeNull();
    release(); await first;
    expect(useSessionStore.getState().composerStopPendingOperationId).toBeNull();
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSessionStore } from "./sessionStore";
import { COMPOSER_STOP_KEY, acquireComposerOperationCustody, authenticateComposerCustody, composerCustodyRecoveryNotice, composerStopRequested, findComposerOperationCustody, purgeComposerCustody } from "./composerOperationCustody";
import { detachComposerObservers } from "@/api/composerOperationObserver";
import { decodeComposerOperationAck, decodeComposerOperationSnapshot } from "@/api/composerOperationDecoder";
import { decodeComposerOperationSnapshotV2 } from "@/api/composerOperationDecoderV2";
import { advanceAuthGeneration } from "@/api/authSession";
import { setComposerAdmissionVersion } from "@/config/composerProtocol";
import type { ComposerOperationFailureNotice, ComposerOperationSnapshot, OperationKind } from "@/types/composerOperations";

vi.mock("@/api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/api/client")>(),
  submitComposerOperation: vi.fn(), fetchComposerOperation: vi.fn(), fetchComposerOperationFailures: vi.fn(), cancelComposerOperation: vi.fn(), fetchComposerOperationStream: vi.fn(),
  fetchComposerOperationAnyVersion: vi.fn(), fetchComposerOperationStreamV2: vi.fn(), cancelComposerOperationV2: vi.fn(),
  fetchMessages: vi.fn(), fetchCompositionState: vi.fn(), fetchCompositionProposals: vi.fn(), fetchComposerPreferences: vi.fn(), fetchSessions: vi.fn(),
  fetchCurrentUser: vi.fn(), fetchAuthConfig: vi.fn(), listBlobs: vi.fn(), listInterpretationEvents: vi.fn(),
}));
import * as api from "@/api/client";

const sid = "11111111-1111-4111-8111-111111111111", id = "22222222-2222-4222-8222-222222222222", inactive = "33333333-3333-4333-8333-333333333333", other = "44444444-4444-4444-8444-444444444444";
const scope = { principalId: "principal", authProvider: "local" };
const iso = "2026-10-31T00:00:00Z";
const notice = "Composer updates stopped before the outcome could be confirmed. Reload this session to check the same action, or use Stop to request cancellation. Your request is still pending.";
const history: ComposerOperationFailureNotice[] = [{ session_id: sid, operation_id: other, kind: "compose_message", user_message_id: null, settled_at: null, failure_code: "worker_lost", error_type: null, diagnostic_id: null, request_id: null, saved_revisions: { attribution: "unavailable", count: null, latest_state_id: null, latest_version: null } }];
function running(sessionId: string, operationId: string, kind: OperationKind = "compose_message") {
  return decodeComposerOperationSnapshot({ operation_id: operationId, kind, status: "running", cancel_requested: false, poll_after_ms: 1000, deadline_at: iso, deadline_remaining_ms: 100000, result: null, error: null }, sessionId, operationId);
}
function opposite(sessionId: string, operationId: string) { return running(sessionId, operationId, "compose_recompose"); }
function holdInactive() {
  return acquireComposerOperationCustody({ mode: "submitted", scope, sessionId: inactive, operationId: id, kind: "compose_message", createdAt: Date.now(), body: { operation_id: id, content: "held request", state_id: null } });
}
function resetSessionGeneration() {
  useSessionStore.getState().reset();
  useSessionStore.setState({ activeSessionId: sid, compositionStateLoaded: true, composerFailureOutcomes: history });
}
function progressResponse(sessionId: string, operationId: string, version: 1 | 2) {
  const payload = { request_id: null, phase: "calling_model", headline: "Working", evidence: [], likely_next: null, reason: null, updated_at: iso };
  const frame = version === 1
    ? { schema_version: "composer-operation-stream.v1", session_id: sessionId, operation_id: operationId, sequence: 0, event: "progress", payload: { ...payload, session_operation_id: operationId, session_operation_epoch: 1, request_token: "token" } }
    : { schema_version: "composer-operation-stream.v2", session_id: sessionId, operation_id: operationId, sequence: 0, event: "progress", payload: { ...payload, segment_sequence: 1, segment_generation: 1 } };
  return new Response(new ReadableStream<Uint8Array>({ start(controller) {
    controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(frame)}\n\n`));
  } }), { headers: { "Content-Type": "text/event-stream" } });
}

beforeEach(() => {
  vi.resetAllMocks(); vi.useFakeTimers(); vi.setSystemTime(new Date(iso)); useSessionStore.getState().reset(); purgeComposerCustody(); sessionStorage.clear(); authenticateComposerCustody(scope); setComposerAdmissionVersion(1);
  useSessionStore.setState({ activeSessionId: sid, compositionStateLoaded: true, composerFailureOutcomes: history });
  vi.mocked(api.submitComposerOperation).mockImplementation(async (descriptor) => ({ ...decodeComposerOperationAck({ operation_id: descriptor.operationId, kind: descriptor.kind, status: "queued", poll_after_ms: 1000 }), ...(descriptor.protocolVersion === 2 ? { protocolVersion: 2 } : {}) }));
  vi.mocked(api.fetchComposerOperationStream).mockRejectedValue(new TypeError("offline"));
  vi.mocked(api.fetchComposerOperationStreamV2).mockRejectedValue(new TypeError("offline"));
  vi.mocked(api.fetchComposerOperation).mockImplementation(async (sessionId, operationId) => running(sessionId, operationId));
  vi.mocked(api.fetchComposerOperationAnyVersion).mockImplementation(async (sessionId, operationId) => ({ protocolVersion: 2, snapshot: decodeComposerOperationSnapshotV2({ protocol_version: 2, operation_id: operationId, kind: "compose_message", status: "running", phase: "executing", status_revision: 4, created_at: iso, observed_at: iso, poll_after_ms: 1000, cancel_requested: false, segment: { sequence: 1, generation: 1, state: "queued", deadline_at: null }, result: null, error: null }, sessionId, operationId) }));
  vi.mocked(api.fetchCurrentUser).mockResolvedValue({ user_id: scope.principalId, username: "principal", display_name: null, email: null, groups: [], dev_admin: false });
  vi.mocked(api.fetchAuthConfig).mockResolvedValue({ provider: "local", registration_mode: "closed", sso_start_url: null });
  vi.mocked(api.fetchMessages).mockResolvedValue([]); vi.mocked(api.fetchCompositionState).mockResolvedValue(null); vi.mocked(api.fetchCompositionProposals).mockResolvedValue([]);
  vi.mocked(api.fetchComposerOperationFailures).mockImplementation(async (sessionId) => ({ session_id: sessionId, items: [], next_cursor: null }));
  vi.mocked(api.fetchComposerPreferences).mockImplementation(async (sessionId) => ({ session_id: sessionId, trust_mode: "explicit_approve", density_default: "medium", interpretation_review_disabled: false, updated_at: iso }));
  vi.mocked(api.fetchSessions).mockResolvedValue([]); vi.mocked(api.listBlobs).mockResolvedValue([]); vi.mocked(api.listInterpretationEvents).mockResolvedValue([]);
});
afterEach(async () => { detachComposerObservers(); await vi.advanceTimersByTimeAsync(0); vi.useRealTimers(); purgeComposerCustody(); setComposerAdmissionVersion(1); vi.restoreAllMocks(); });

describe("R real observer-to-store disposition", () => {
  it.each([[1, false], [1, true], [2, false], [2, true]] as const)("keeps the actual V%s callback fault pending (custody notice %s)", async (version, withRecoveryNotice) => {
    setComposerAdmissionVersion(version);
    const diagnostics = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const privateCause = new Error("PRIVATE_CALLBACK_CAUSE_SENTINEL");
    let recoveryNotice: string | null = null, callbackRan = false;
    const unsubscribe = useSessionStore.subscribe((state) => {
      if (callbackRan || state.composerProgress === null) return;
      callbackRan = true;
      if (withRecoveryNotice) {
        sessionStorage.setItem(COMPOSER_STOP_KEY, JSON.stringify([{ unknown: true }]));
        authenticateComposerCustody(scope); recoveryNotice = composerCustodyRecoveryNotice();
      }
      throw privateCause;
    });
    if (version === 1) vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (sessionId, operationId) => progressResponse(sessionId, operationId, 1));
    else vi.mocked(api.fetchComposerOperationStreamV2).mockImplementation(async (sessionId, operationId) => progressResponse(sessionId, operationId, 2));
    const before = useSessionStore.getState();
    const sending = useSessionStore.getState().sendMessage("hello");
    const held = findComposerOperationCustody(scope, sid).foreground;
    try {
      expect(held).not.toBeNull();
      await vi.advanceTimersByTimeAsync(0);
      expect(callbackRan).toBe(true);
      if (withRecoveryNotice) expect(recoveryNotice).not.toBeNull();
      expect(useSessionStore.getState().error).toBe(withRecoveryNotice ? `${recoveryNotice} ${notice}` : notice);
      expect(useSessionStore.getState().errorDetails).toBeNull(); expect(useSessionStore.getState().composerProgress).toBeNull();
      expect(useSessionStore.getState().isComposing).toBe(true);
      expect(useSessionStore.getState().messages).toEqual([expect.objectContaining({ role: "user", content: "hello", local_status: "pending", operation_id: held!.operationId, local_error: undefined, local_failure_code: undefined })]);
      expect(useSessionStore.getState().composerFailureOutcomes).toBe(history);
      expect(useSessionStore.getState().compositionState).toBe(before.compositionState);
      expect(useSessionStore.getState().compositionProposals).toBe(before.compositionProposals);
      expect(useSessionStore.getState().composerStopPendingOperationId).toBeNull(); expect(composerStopRequested(held!)).toBe(false);
      expect(findComposerOperationCustody(scope, sid).foreground).toBe(held);
      expect(diagnostics.mock.calls).toEqual([["progress_callback_failed"]]);
      expect(JSON.stringify({ error: useSessionStore.getState().error, details: useSessionStore.getState().errorDetails, messages: useSessionStore.getState().messages, diagnostics: diagnostics.mock.calls })).not.toContain("PRIVATE_CALLBACK_CAUSE_SENTINEL");
      await vi.advanceTimersByTimeAsync(180000);
      expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
      expect(api.fetchComposerOperationStream).toHaveBeenCalledTimes(version === 1 ? 1 : 0); expect(api.fetchComposerOperationStreamV2).toHaveBeenCalledTimes(version === 2 ? 1 : 0);
      expect(api.fetchComposerOperation).not.toHaveBeenCalled(); expect(api.fetchComposerOperationAnyVersion).not.toHaveBeenCalled();
      expect(api.cancelComposerOperation).not.toHaveBeenCalled(); expect(api.cancelComposerOperationV2).not.toHaveBeenCalled();
      expect(api.fetchMessages).not.toHaveBeenCalled(); expect(api.fetchComposerOperationFailures).not.toHaveBeenCalled(); expect(vi.getTimerCount()).toBe(0);
    } finally { unsubscribe(); detachComposerObservers(); await vi.advanceTimersByTimeAsync(0); await sending; }
  });

  it("routes the requested-ID valid opposite-kind poll to one pending notice", async () => {
    const diagnostics = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.mocked(api.fetchComposerOperation).mockImplementation(async (sessionId, operationId) => {
      const decoded = opposite(sessionId, operationId); expect(decoded).toMatchObject({ operation_id: operationId, kind: "compose_recompose", status: "running" }); return decoded;
    });
    const sending = useSessionStore.getState().sendMessage("hello"), held = findComposerOperationCustody(scope, sid).foreground;
    try {
      await vi.advanceTimersByTimeAsync(0);
      expect(useSessionStore.getState().error).toBe(notice); expect(useSessionStore.getState().composerProgress).toBeNull();
      expect(useSessionStore.getState().messages).toEqual([expect.objectContaining({ role: "user", content: "hello", local_status: "pending", local_error: undefined })]);
      expect(useSessionStore.getState().composerFailureOutcomes).toBe(history); expect(findComposerOperationCustody(scope, sid).foreground).toBe(held);
      expect(useSessionStore.getState().composerStopPendingOperationId).toBeNull(); expect(composerStopRequested(held!)).toBe(false);
      expect(diagnostics.mock.calls).toEqual([["snapshot_kind_mismatch"]]);
      await vi.advanceTimersByTimeAsync(180000);
      expect(api.fetchComposerOperation).toHaveBeenCalledExactlyOnceWith(sid, held!.operationId); expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
      expect(api.cancelComposerOperation).not.toHaveBeenCalled(); expect(api.fetchMessages).not.toHaveBeenCalled(); expect(api.fetchComposerOperationFailures).not.toHaveBeenCalled();
      expect(vi.getTimerCount()).toBe(0);
    } finally { detachComposerObservers(); await vi.advanceTimersByTimeAsync(0); await sending; }
  });

  it("does not grant a structural fault impostor the nominal pending disposition", async () => {
    const diagnostics = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const impostor = Object.assign(new Error("Safe definitive refusal."), { name: "ComposerObservationFault", code: "progress_callback_failed", cause: "PRIVATE_IMPOSTOR_CAUSE", status: 403, error_type: "forbidden", detail: "Safe definitive refusal." });
    expect(api.isDefinitiveComposerRefusal(impostor)).toBe(true);
    vi.mocked(api.submitComposerOperation).mockRejectedValue(impostor);
    await useSessionStore.getState().sendMessage("hello");
    await vi.advanceTimersByTimeAsync(0);
    expect(diagnostics).not.toHaveBeenCalled();
    expect(useSessionStore.getState().error).toBe("Safe definitive refusal.");
    expect(useSessionStore.getState().messages[0]).toMatchObject({ role: "user", content: "hello", local_status: "failed", local_error: "Safe definitive refusal." });
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1); expect(api.fetchComposerOperation).not.toHaveBeenCalled(); expect(api.cancelComposerOperation).not.toHaveBeenCalled();
    expect(JSON.stringify({ error: useSessionStore.getState().error, details: useSessionStore.getState().errorDetails, messages: useSessionStore.getState().messages })).not.toContain("PRIVATE_IMPOSTOR_CAUSE");
  });
});

describe("R inactive-session diagnostic owner", () => {
  it("disposes once without active-session publication, replay, Stop or replacement observation", async () => {
    const diagnostics = vi.spyOn(console, "warn").mockImplementation(() => undefined), held = holdInactive();
    const before = useSessionStore.getState(), decoded = opposite(inactive, id);
    expect(decoded).toMatchObject({ operation_id: id, kind: "compose_recompose", status: "running" });
    vi.mocked(api.fetchComposerOperation).mockResolvedValue(decoded);
    try {
      await useSessionStore.getState().reconcileInactiveComposerCustody(); await vi.advanceTimersByTimeAsync(0);
      expect(diagnostics.mock.calls).toEqual([["snapshot_kind_mismatch"]]);
      expect(findComposerOperationCustody(scope, inactive).foreground).toBe(held); expect(composerStopRequested(held)).toBe(false);
      expect(useSessionStore.getState()).toBe(before);
      await vi.advanceTimersByTimeAsync(180000);
      expect(api.fetchComposerOperation).toHaveBeenCalledExactlyOnceWith(inactive, id);
      expect(api.submitComposerOperation).not.toHaveBeenCalled(); expect(api.cancelComposerOperation).not.toHaveBeenCalled(); expect(vi.getTimerCount()).toBe(0);
      await useSessionStore.getState().reconcileInactiveComposerCustody(); await vi.advanceTimersByTimeAsync(0);
      expect(diagnostics.mock.calls).toEqual([["snapshot_kind_mismatch"], ["snapshot_kind_mismatch"]]);
      expect(api.fetchComposerOperation).toHaveBeenCalledTimes(2); expect(api.submitComposerOperation).not.toHaveBeenCalled();
    } finally { detachComposerObservers(); await vi.advanceTimersByTimeAsync(0); }
  });
});

describe("P stale fault disposition", () => {
  it("publishes no inactive diagnostic after observer rejection but before the store catch", async () => {
    const diagnostics = vi.spyOn(console, "warn").mockImplementation(() => undefined), held = holdInactive();
    let afterNavigation = useSessionStore.getState(), navigated = false;
    vi.mocked(api.fetchComposerOperation).mockImplementation(async () => {
      // Navigate after the real observer's check/rejection, so its own abort
      // cannot substitute for the store's session-generation guard.
      void Promise.resolve().then(() => { void Promise.resolve().then(() => {
        resetSessionGeneration(); afterNavigation = useSessionStore.getState(); navigated = true;
      }); });
      return opposite(inactive, id);
    });
    await useSessionStore.getState().reconcileInactiveComposerCustody(); await vi.advanceTimersByTimeAsync(0);
    expect(navigated).toBe(true); expect(diagnostics).not.toHaveBeenCalled(); expect(useSessionStore.getState()).toBe(afterNavigation);
    expect(findComposerOperationCustody(scope, inactive).foreground).toBe(held); expect(api.submitComposerOperation).not.toHaveBeenCalled();
  });

  it.each([
    ["foreground", "auth"], ["foreground", "principal"], ["foreground", "session"],
    ["inactive", "auth"], ["inactive", "principal"], ["inactive", "session"],
  ] as const)("ignores a stale %s fault after %s changes", async (mode, change) => {
    const diagnostics = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    let release!: (value: ComposerOperationSnapshot) => void;
    vi.mocked(api.fetchComposerOperation).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const held = mode === "inactive" ? holdInactive() : null;
    const sending = mode === "foreground" ? useSessionStore.getState().sendMessage("hello") : null;
    if (mode === "inactive") await useSessionStore.getState().reconcileInactiveComposerCustody();
    await vi.advanceTimersByTimeAsync(0);
    const observed = mode === "inactive" ? held! : findComposerOperationCustody(scope, sid).foreground!;
    expect(api.fetchComposerOperation).toHaveBeenLastCalledWith(observed.sessionId, observed.operationId);
    if (change === "auth") advanceAuthGeneration();
    if (change === "principal") authenticateComposerCustody({ ...scope, principalId: "other-principal" });
    if (change === "session") resetSessionGeneration();
    const before = useSessionStore.getState(), custodyBefore = findComposerOperationCustody(scope, observed.sessionId);
    release(opposite(observed.sessionId, observed.operationId)); await vi.advanceTimersByTimeAsync(180000); if (sending !== null) await sending;
    expect(diagnostics).not.toHaveBeenCalled(); expect(useSessionStore.getState()).toBe(before);
    expect(findComposerOperationCustody(scope, observed.sessionId)).toEqual(custodyBefore);
    expect(api.cancelComposerOperation).not.toHaveBeenCalled();
    expect(api.cancelComposerOperationV2).not.toHaveBeenCalled(); expect(api.fetchComposerOperationAnyVersion).not.toHaveBeenCalled();
    expect(api.fetchComposerOperation).toHaveBeenCalledExactlyOnceWith(observed.sessionId, observed.operationId);
    expect(api.fetchMessages).not.toHaveBeenCalled(); expect(api.fetchComposerOperationFailures).not.toHaveBeenCalled();
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(mode === "foreground" ? 1 : 0);
    expect(vi.getTimerCount()).toBe(0);
  });
});

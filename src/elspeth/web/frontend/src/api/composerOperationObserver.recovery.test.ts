import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cancelObservedComposerOperation, ComposerActiveAttachment, detachComposerObservers, observeComposerOperation, submitAndObserveComposerOperation } from "./composerOperationObserver";
import { acquireComposerOperationCustody, authenticateComposerCustody, composerStopPersistenceLimited, COMPOSER_AGGREGATE_LIMIT, COMPOSER_STOP_KEY, requestComposerStop, COMPOSER_CUSTODY_KEY, COMPOSER_CUSTODY_LIFETIME_MS, composerStopRequested, findComposerOperationCustody, purgeComposerCustody } from "@/stores/composerOperationCustody";
import type { ComposerOperationSnapshot, SubmittedCustody } from "@/types/composerOperations";
import { attachComposerObserver, settleComposerCustody } from "@/stores/composerOperationCustody";
vi.mock("./client", () => ({ submitComposerOperation: vi.fn(), fetchComposerOperation: vi.fn(), cancelComposerOperation: vi.fn(), fetchComposerOperationStream: vi.fn(), apiErrorFromBody: vi.fn(), composerActiveOperation: vi.fn(), isDefinitiveComposerRefusal: vi.fn() }));
import * as api from "./client";
const sessionId = "11111111-1111-4111-8111-111111111111", operationId = "22222222-2222-4222-8222-222222222222", other = "33333333-3333-4333-8333-333333333333";
const scope = { principalId: "principal", authProvider: "local" };
function descriptor(): SubmittedCustody { return { mode: "submitted", scope, sessionId, operationId, kind: "compose_message", createdAt: Date.now(), body: { operation_id: operationId, content: "hello", state_id: null } }; }
function snapshot(status: "running" | "completed" = "completed", id = operationId): ComposerOperationSnapshot { return { operation_id: id, kind: "compose_message", status, cancel_requested: false, poll_after_ms: 1000, deadline_at: "2026-10-06T00:00:00Z", deadline_remaining_ms: status === "running" ? 100000 : 0, result: status === "running" ? null : { message: { id: other, session_id: sessionId, role: "assistant", content: "done", tool_calls: null, created_at: "2026-10-06T00:00:00Z" }, state: null, proposals: [] }, error: null }; }
beforeEach(() => { vi.resetAllMocks(); vi.useFakeTimers(); purgeComposerCustody(); sessionStorage.clear(); authenticateComposerCustody(scope); vi.mocked(api.fetchComposerOperationStream).mockRejectedValue(new Error("offline")); vi.mocked(api.composerActiveOperation).mockReturnValue(null); vi.mocked(api.isDefinitiveComposerRefusal).mockReturnValue(false); vi.mocked(api.cancelComposerOperation).mockResolvedValue({ kind: "operation_missing" }); });
afterEach(() => { detachComposerObservers(); vi.useRealTimers(); purgeComposerCustody(); });
it("Stop persists and cancels each exact unresolved action after the winner settles", async () => {
  const first = acquireComposerOperationCustody(descriptor());
  const winner = attachComposerObserver(first, other, "compose_message", true);
  settleComposerCustody(winner);
  expect(findComposerOperationCustody(scope, sessionId).foreground).toBeNull();
  vi.mocked(api.fetchComposerOperation).mockResolvedValue(snapshot("running"));
  const observing = observeComposerOperation(first, { reconciliationOnly: true }).catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(0);
  expect(cancelObservedComposerOperation(sessionId)).toBe(true);
  expect(composerStopRequested(first)).toBe(true);
  expect(vi.mocked(api.cancelComposerOperation).mock.calls.map(([, id]) => id)).toEqual([operationId]);
  await vi.advanceTimersByTimeAsync(1000);
  expect(api.cancelComposerOperation).toHaveBeenCalledTimes(2);
  expect(api.submitComposerOperation).not.toHaveBeenCalled();
  expect(vi.mocked(api.cancelComposerOperation).mock.calls.every(([, id]) => id !== other)).toBe(true);
  detachComposerObservers(); await vi.advanceTimersByTimeAsync(0); await observing;
});
it("ownerless Stop records every unresolved identity without canceling the settled winner", () => {
  const first = acquireComposerOperationCustody(descriptor());
  settleComposerCustody(attachComposerObserver(first, "55555555-5555-4555-8555-555555555555", "compose_message", true));
  const second = acquireComposerOperationCustody({ ...descriptor(), kind: "compose_message", operationId: other, body: { operation_id: other, content: "second", state_id: null } });
  const winnerId = "44444444-4444-4444-8444-444444444444";
  const winner = attachComposerObserver(second, winnerId, "compose_message", true);
  settleComposerCustody(winner);
  expect(cancelObservedComposerOperation(sessionId)).toBe(false);
  expect(composerStopRequested(first)).toBe(true);
  expect(composerStopRequested(second)).toBe(true);
  expect(vi.mocked(api.cancelComposerOperation).mock.calls.map(([, id]) => id)).toEqual([operationId, other]);
  expect(api.submitComposerOperation).not.toHaveBeenCalled();
});
it("ownerless Stop survives detach and retries its exact target on restored late admission", async () => {
  const pending = acquireComposerOperationCustody(descriptor());
  expect(cancelObservedComposerOperation(sessionId)).toBe(false); expect(composerStopRequested(pending)).toBe(true);
  vi.mocked(api.fetchComposerOperation).mockResolvedValueOnce(snapshot("running")).mockResolvedValue(snapshot());
  const observing = observeComposerOperation(pending);
  await vi.advanceTimersByTimeAsync(1000); await observing;
  expect(api.cancelComposerOperation).toHaveBeenCalledTimes(3);
  expect(vi.mocked(api.cancelComposerOperation).mock.calls.every(([, id]) => id === operationId)).toBe(true);
  expect(api.submitComposerOperation).not.toHaveBeenCalled();
});
it("Stop of held submitted A never drifts to active-refusal winner B", async () => {
  let reject!: (error: unknown) => void;
  vi.mocked(api.submitComposerOperation).mockReturnValue(new Promise((_resolve, rejectPromise) => { reject = rejectPromise; }));
  const refusal = { status: 409, error_type: "composer_operation_active" };
  vi.mocked(api.composerActiveOperation).mockImplementation((error) => error === refusal ? { operation_id: other, kind: "compose_message" } : null);
  vi.mocked(api.fetchComposerOperation).mockResolvedValue(snapshot("completed", other));
  const observing = submitAndObserveComposerOperation(descriptor()).catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(0); cancelObservedComposerOperation(sessionId); reject(refusal);
  await vi.advanceTimersByTimeAsync(0); expect(await observing).toBeInstanceOf(ComposerActiveAttachment);
  expect(vi.mocked(api.cancelComposerOperation).mock.calls.map(([, id]) => id)).toEqual([operationId]);
});
it("mixed expired and fresh custody exposes readonly descriptors while preserving exact raw storage", async () => {
  purgeComposerCustody();
  const expired = { ...descriptor(), createdAt: Date.now() - COMPOSER_CUSTODY_LIFETIME_MS - 1 };
  const fresh = { ...descriptor(), sessionId: other };
  const raw = JSON.stringify({ schema: "composer-operations.v1", entries: [expired, fresh].map((foreground) => ({ foreground, unresolvedSubmissions: [] })) });
  sessionStorage.setItem(COMPOSER_CUSTODY_KEY, raw); authenticateComposerCustody(scope);
  expect(findComposerOperationCustody(scope, sessionId).foreground).toEqual(expired);
  expect(findComposerOperationCustody(scope, other).foreground).toEqual(fresh);
  expect(sessionStorage.getItem(COMPOSER_CUSTODY_KEY)).toBe(raw);
  expect(() => acquireComposerOperationCustody({ ...descriptor(), sessionId: "44444444-4444-4444-8444-444444444444" })).toThrow(/reconcile/);
  vi.mocked(api.fetchComposerOperation).mockResolvedValue(snapshot()); await observeComposerOperation(expired);
  expect(api.submitComposerOperation).not.toHaveBeenCalled(); expect(sessionStorage.getItem(COMPOSER_CUSTODY_KEY)).toBe(raw);
});

it("persisted Stop control restores on a fresh custody module without changing request body", async () => {
  const held = acquireComposerOperationCustody(descriptor()); cancelObservedComposerOperation(sessionId);
  const originalBody = JSON.stringify(held.body);
  vi.resetModules();
  const fresh = await import("@/stores/composerOperationCustody"); fresh.authenticateComposerCustody(scope);
  const restored = fresh.findComposerOperationCustody(scope, sessionId).foreground!;
  expect(fresh.composerStopRequested(restored)).toBe(true);
  expect(restored.mode === "submitted" && JSON.stringify(restored.body)).toBe(originalBody);
  fresh.purgeComposerCustody();
});
it("unreadable nested failed terminal never settles exact submitted custody", async () => {
  vi.mocked(api.submitComposerOperation).mockResolvedValue({ operation_id: operationId, kind: "compose_message", status: "queued", poll_after_ms: 1000 });
  vi.mocked(api.fetchComposerOperation).mockResolvedValue({ ...snapshot(), status: "failed", result: null, error: { http_status: 500, error_type: "composer_plugin_crash", failure_code: "http_error", diagnostic_id: null, body: { detail: { failed_turn: { assistant_message_id: 22, tool_calls_attempted: {}, tool_responses_persisted: -3, transcript_url: 5, extra: true } } } } });
  const observing = submitAndObserveComposerOperation(descriptor()).catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(0);
  expect(findComposerOperationCustody(scope, sessionId).foreground?.operationId).toBe(operationId);
  expect(api.apiErrorFromBody).not.toHaveBeenCalled();
  detachComposerObservers(); await vi.advanceTimersByTimeAsync(0); await observing;
  expect(findComposerOperationCustody(scope, sessionId).foreground?.operationId).toBe(operationId);
});

it("quota admission reserves every held action's later persisted Stop control", () => {
  const admitted: SubmittedCustody[] = [];
  for (let index = 1; index <= 200; index += 1) {
    const id = `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
    const value: SubmittedCustody = { ...descriptor(), kind: "compose_message", sessionId: id, operationId: id, body: { operation_id: id, content: "x".repeat(7000), state_id: null } };
    try { admitted.push(acquireComposerOperationCustody(value)); } catch { break; }
  }
  expect(admitted.length).toBeGreaterThan(20); expect(admitted.length).toBeLessThan(200);
  const held = sessionStorage.getItem(COMPOSER_CUSTODY_KEY)!;
  for (const value of admitted) requestComposerStop(value);
  const controls = sessionStorage.getItem(COMPOSER_STOP_KEY)!;
  expect(JSON.parse(controls)).toHaveLength(admitted.length);
  expect(new TextEncoder().encode(held).byteLength + new TextEncoder().encode(controls).byteLength).toBeLessThanOrEqual(COMPOSER_AGGREGATE_LIMIT);
  expect(sessionStorage.getItem(COMPOSER_CUSTODY_KEY)).toBe(held);
});

it("malformed sibling remains quarantined while a valid identity stays reachable for readonly recovery", async () => {
  purgeComposerCustody();
  const valid = descriptor();
  const raw = JSON.stringify({ schema: "composer-operations.v1", entries: [{ foreground: valid, unresolvedSubmissions: [] }, { foreground: { ...descriptor(), sessionId: other, mode: "unknown" }, unresolvedSubmissions: [] }] });
  sessionStorage.setItem(COMPOSER_CUSTODY_KEY, raw); authenticateComposerCustody(scope); authenticateComposerCustody(scope);
  expect(findComposerOperationCustody(scope, sessionId).foreground?.operationId).toBe(operationId);
  expect(findComposerOperationCustody(scope, other).foreground).toBeNull();
  vi.mocked(api.fetchComposerOperation).mockResolvedValue(snapshot()); await observeComposerOperation(valid);
  expect(api.submitComposerOperation).not.toHaveBeenCalled(); expect(sessionStorage.getItem(COMPOSER_CUSTODY_KEY)).toBe(raw);
  expect(() => acquireComposerOperationCustody({ ...descriptor(), sessionId: other })).toThrow(/reconcile/);
});
it("live-tab expiry removes automatic replay eligibility without deleting its held body", async () => {
  vi.mocked(api.submitComposerOperation).mockRejectedValueOnce(new TypeError("lost admission"));
  vi.mocked(api.fetchComposerOperation).mockResolvedValueOnce({ kind: "operation_missing" }).mockResolvedValue(snapshot());
  const held = descriptor(); const observing = submitAndObserveComposerOperation(held);
  await vi.advanceTimersByTimeAsync(0); vi.setSystemTime(Date.now() + COMPOSER_CUSTODY_LIFETIME_MS + 1);
  await vi.advanceTimersByTimeAsync(1000); await observing;
  expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
  expect(vi.mocked(api.submitComposerOperation).mock.calls[0][0].body).toEqual(held.body);
});

it("malformed Stop controls retain raw unknown data while restoring the valid exact target", () => {
  const held = acquireComposerOperationCustody(descriptor());
  const identity = JSON.stringify([scope.principalId, scope.authProvider, sessionId, operationId]);
  const raw = JSON.stringify([identity, { unknown: true }]);
  sessionStorage.setItem(COMPOSER_STOP_KEY, raw); authenticateComposerCustody(scope);
  expect(composerStopRequested(held)).toBe(true);
  requestComposerStop(held);
  expect(sessionStorage.getItem(COMPOSER_STOP_KEY)).toBe(raw);
  expect(composerStopPersistenceLimited()).toBe(true);
  expect(() => acquireComposerOperationCustody({ ...descriptor(), sessionId: other })).toThrow(/reconcile/);
});

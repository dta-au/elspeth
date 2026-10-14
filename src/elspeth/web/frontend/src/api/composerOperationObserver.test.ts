import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { advanceAuthGeneration } from "./authSession";
import { cancelObservedComposerOperation, ComposerActiveAttachment, ComposerObservationDetached, detachComposerObservers, observeComposerOperation, submitAndObserveComposerOperation } from "./composerOperationObserver";
import { acquireComposerOperationCustody, authenticateComposerCustody, findComposerOperationCustody, purgeComposerCustody } from "@/stores/composerOperationCustody";
import type { ComposerOperationSnapshot, SubmittedCustody } from "@/types/composerOperations";
vi.mock("./client", () => ({ submitComposerOperation: vi.fn(), fetchComposerOperation: vi.fn(), cancelComposerOperation: vi.fn(), fetchComposerOperationStream: vi.fn(), apiErrorFromBody: vi.fn(), composerActiveOperation: vi.fn(), isDefinitiveComposerRefusal: vi.fn() }));
import * as api from "./client";
const sid = "11111111-1111-4111-8111-111111111111", id = "22222222-2222-4222-8222-222222222222", other = "33333333-3333-4333-8333-333333333333";
const scope = { principalId: "principal", authProvider: "local" };
function descriptor(): SubmittedCustody { return { mode: "submitted", scope, sessionId: sid, operationId: id, kind: "compose_message", createdAt: Date.now(), body: { operation_id: id, content: "hello", state_id: null } }; }
function snapshot(status: "running" | "completed" = "completed", operationId = id): ComposerOperationSnapshot {
  return { operation_id: operationId, kind: "compose_message", status, cancel_requested: false, deadline_at: new Date().toISOString(), deadline_remaining_ms: status === "running" ? 1000 : 0, poll_after_ms: 1000, error: null, result: status === "running" ? null : { message: { id: other, session_id: sid, role: "assistant", content: "done", tool_calls: null, created_at: new Date().toISOString() }, state: null, proposals: [] } };
}
beforeEach(() => {
  vi.resetAllMocks(); purgeComposerCustody(); sessionStorage.clear(); authenticateComposerCustody(scope);
  vi.mocked(api.submitComposerOperation).mockResolvedValue({ operation_id: id, kind: "compose_message", status: "queued", poll_after_ms: 1000 });
  vi.mocked(api.fetchComposerOperationStream).mockRejectedValue(new Error("stream unavailable"));
  vi.mocked(api.fetchComposerOperation).mockResolvedValue(snapshot());
  vi.mocked(api.cancelComposerOperation).mockResolvedValue(snapshot("running"));
  vi.mocked(api.composerActiveOperation).mockReturnValue(null);
  vi.mocked(api.isDefinitiveComposerRefusal).mockImplementation((error) => typeof error === "object" && error !== null && "status" in error && typeof error.status === "number" && error.status >= 400 && error.status < 500);
});
afterEach(() => { detachComposerObservers(); vi.useRealTimers(); purgeComposerCustody(); });
describe("one durable operation observer", () => {
  it.each([
    ["http_error", "admission_refused", "admission_refused"],
    ["http_error", undefined, undefined],
    ["operation_failed", "admission_refused", "operation_failed"],
    ["worker_lost", "admission_refused", "worker_lost"],
    ["request_cancelled", "policy_blocked", "request_cancelled"],
    ["deadline_expired", "policy_blocked", "deadline_expired"],
  ] as const)("keeps durable %s separate from body code %s", async (category, semanticCode, expectedCode) => {
    vi.mocked(api.apiErrorFromBody).mockReturnValue({ status: 422, detail: "Synthetic refusal", failure_code: semanticCode });
    const diagnosticId = category === "operation_failed" ? other : null;
    vi.mocked(api.fetchComposerOperation).mockResolvedValue({
      ...snapshot(), status: "failed", result: null,
      error: { http_status: 422, body: { detail: "Synthetic refusal", ...(semanticCode === undefined ? {} : { failure_code: semanticCode }) }, error_type: null, failure_code: category, diagnostic_id: diagnosticId },
    });
    const failure = await submitAndObserveComposerOperation(descriptor()).catch((error: unknown) => error);
    expect(failure).toMatchObject({
      failure_code: expectedCode,
      composer_operation: { operation_id: id, kind: "compose_message", failure_code: category },
      diagnostic_id: diagnosticId ?? undefined,
    });
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
    expect(api.fetchComposerOperation).toHaveBeenCalledTimes(1);
    expect(api.cancelComposerOperation).not.toHaveBeenCalled();
  });

  it("falls back after stream EOF and applies only the final authenticated GET", async () => {
    vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(new Response(new ReadableStream({ start(controller) { controller.close(); } }), { headers: { "Content-Type": "text/event-stream" } }));
    const result = await submitAndObserveComposerOperation(descriptor());
    expect(result.message.content).toBe("done"); expect(api.submitComposerOperation).toHaveBeenCalledTimes(1); expect(api.fetchComposerOperation).toHaveBeenCalledWith(sid, id);
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
  });
  it("does not replay on transient GET failures and keeps the exact operation", async () => {
    vi.useFakeTimers();
    vi.mocked(api.fetchComposerOperation).mockRejectedValueOnce(new TypeError("network")).mockRejectedValueOnce({ status: 503 }).mockResolvedValue(snapshot());
    const observing = submitAndObserveComposerOperation(descriptor()); await vi.advanceTimersByTimeAsync(3000); await observing;
    expect(api.fetchComposerOperation).toHaveBeenCalledTimes(3); expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
  });
  it("same-ID/body replay after ambiguous admission races a missing snapshot", async () => {
    vi.useFakeTimers(); vi.mocked(api.submitComposerOperation).mockRejectedValueOnce(new TypeError("POST response lost"));
    vi.mocked(api.fetchComposerOperation).mockResolvedValueOnce({ kind: "operation_missing" }).mockResolvedValue(snapshot());
    const observing = submitAndObserveComposerOperation(descriptor()); await vi.advanceTimersByTimeAsync(1000); await observing;
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(2);
    const bodies = vi.mocked(api.submitComposerOperation).mock.calls.map(([submission]) => submission.body); expect(bodies[1]).toEqual(bodies[0]);
  });
  it("Stop requests cancel and still waits for the durable terminal", async () => {
    vi.useFakeTimers(); vi.mocked(api.fetchComposerOperation).mockResolvedValueOnce(snapshot("running")).mockResolvedValue(snapshot());
    const observing = submitAndObserveComposerOperation(descriptor()); await vi.advanceTimersByTimeAsync(0); cancelObservedComposerOperation(sid);
    expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(id);
    await vi.advanceTimersByTimeAsync(1000); const result = await observing;
    expect(api.cancelComposerOperation).toHaveBeenCalledWith(sid, id); expect(result.message.content).toBe("done");
  });
  it("an observation deadline detaches and preserves submitted custody without cancel", async () => {
    const controller = new AbortController();
    vi.mocked(api.fetchComposerOperation).mockImplementation(async () => { controller.abort("compose_timeout"); return snapshot("running"); });
    await expect(submitAndObserveComposerOperation(descriptor(), { signal: controller.signal })).rejects.toBeInstanceOf(ComposerObservationDetached);
    expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(id); expect(api.cancelComposerOperation).not.toHaveBeenCalled();
  });
  it("Stop before POST does not dispatch or leave a pending submitted action", async () => {
    const controller = new AbortController(); controller.abort("compose_user_cancel");
    await expect(submitAndObserveComposerOperation(descriptor(), { signal: controller.signal })).rejects.toBeInstanceOf(ComposerObservationDetached);
    expect(api.submitComposerOperation).not.toHaveBeenCalled(); expect(api.cancelComposerOperation).not.toHaveBeenCalled(); expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
  });
  it("rejects a stale generation terminal without settling its custody", async () => {
    vi.mocked(api.fetchComposerOperation).mockImplementation(async () => { advanceAuthGeneration(); return snapshot(); });
    await expect(submitAndObserveComposerOperation(descriptor())).rejects.toBeInstanceOf(ComposerObservationDetached);
    expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(id);
  });
  it.each(["completed", "failed"] as const)("active refusal carries the %s winner terminal and preserves an earlier ambiguous submission", async (status) => {
    vi.useFakeTimers();
    const refusal = { status: 409, error_type: "composer_operation_active", operation_id: other, kind: "compose_message" };
    vi.mocked(api.submitComposerOperation).mockRejectedValueOnce(new TypeError("late admission")).mockRejectedValueOnce(refusal);
    vi.mocked(api.composerActiveOperation).mockImplementation((error) => error === refusal ? { operation_id: other, kind: "compose_message" } : null);
    const winner = snapshot("completed", other);
    vi.mocked(api.apiErrorFromBody).mockReturnValue({ status: 503, detail: "Server stopped" });
    vi.mocked(api.fetchComposerOperation).mockResolvedValueOnce({ kind: "operation_missing" }).mockResolvedValue(status === "completed" ? winner : { ...winner, status: "failed", result: null, error: { http_status: 503, failure_code: "worker_lost", error_type: "composer_operation_worker_lost", diagnostic_id: null, body: {} } });
    const observing = submitAndObserveComposerOperation(descriptor()); const caught = observing.catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(1000); expect(await caught).toBeInstanceOf(ComposerActiveAttachment);
    expect((await caught as ComposerActiveAttachment).terminal).toMatchObject(status === "completed" ? { status, result: winner.result } : { status, error: { composer_operation: { operation_id: other, kind: "compose_message" }, failure_code: "worker_lost" } });
    const record = findComposerOperationCustody(scope, sid); expect(record.unresolvedSubmissions.get(id)?.body.operation_id).toBe(id);
    expect(api.fetchComposerOperation).toHaveBeenLastCalledWith(sid, other);
    expect(vi.mocked(api.submitComposerOperation).mock.calls.every(([submission]) => submission.operationId === id)).toBe(true);
  });
});

describe("monotonic observation budget", () => {
  it.each([-600000, 0, 600000])("tightens only despite wall-clock skew %s", async (skew) => {
    vi.useFakeTimers(); vi.setSystemTime(Date.now() + skew);
    let reads = 0;
    vi.mocked(api.fetchComposerOperation).mockImplementation(async () => ({ ...snapshot("running"), deadline_remaining_ms: reads++ === 0 ? 1000 : 100000 }));
    const start = performance.now();
    const observing = submitAndObserveComposerOperation(descriptor()).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(25999);
    expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(id);
    await vi.advanceTimersByTimeAsync(1);
    expect(await observing).toBeInstanceOf(ComposerObservationDetached);
    expect(performance.now() - start).toBeGreaterThanOrEqual(26000);
    expect(api.cancelComposerOperation).not.toHaveBeenCalled();
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
    expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(id);
  });
});

it("never replays an ambiguous submitted body after Stop while both cancel and GET report missing", async () => {
  vi.useFakeTimers();
  vi.mocked(api.submitComposerOperation).mockRejectedValueOnce(new TypeError("POST response lost"));
  vi.mocked(api.fetchComposerOperation).mockResolvedValue({ kind: "operation_missing" });
  vi.mocked(api.cancelComposerOperation).mockResolvedValue({ kind: "operation_missing" });
  const observing = submitAndObserveComposerOperation(descriptor()).catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(0);
  expect(api.fetchComposerOperation).toHaveBeenCalledTimes(1);
  expect(cancelObservedComposerOperation(sid)).toBe(true);
  try {
    await vi.advanceTimersByTimeAsync(8000);
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
    expect(api.cancelComposerOperation).toHaveBeenCalledWith(sid, id);
    expect(findComposerOperationCustody(scope, sid).foreground?.mode).toBe("submitted");
    expect(findComposerOperationCustody(scope, sid).foreground).toMatchObject({ operationId: id, body: descriptor().body });
  } finally {
    detachComposerObservers();
    await vi.advanceTimersByTimeAsync(0);
    expect(await observing).toBeInstanceOf(ComposerObservationDetached);
  }
});

it("restored submitted custody reads missing snapshots without POST or loss to another action's terminal", async () => {
  vi.useFakeTimers();
  const original = acquireComposerOperationCustody(descriptor());
  const refusal = { status: 409, error_type: "composer_operation_active", operation_id: other, kind: "compose_message" };
  vi.mocked(api.submitComposerOperation).mockRejectedValue(refusal);
  vi.mocked(api.composerActiveOperation).mockImplementation((error) => error === refusal ? { operation_id: other, kind: "compose_message" } : null);
  vi.mocked(api.fetchComposerOperation).mockImplementation(async (_session, operationId) => operationId === id ? { kind: "operation_missing" } : snapshot("completed", other));
  const observing = observeComposerOperation(original).catch((error: unknown) => error);
  try {
    await vi.advanceTimersByTimeAsync(8000);
    expect(api.submitComposerOperation).not.toHaveBeenCalled();
    expect(api.fetchComposerOperation).toHaveBeenCalledWith(sid, id);
    expect(vi.mocked(api.fetchComposerOperation).mock.calls.every(([, operationId]) => operationId === id)).toBe(true);
    expect(findComposerOperationCustody(scope, sid).foreground).toEqual(original);
  } finally {
    detachComposerObservers(); await vi.advanceTimersByTimeAsync(0);
    expect(await observing).toBeInstanceOf(ComposerObservationDetached);
  }
  expect(findComposerOperationCustody(scope, sid).foreground).toMatchObject({ operationId: id, body: descriptor().body });
});

it.each(["ack", "running_snapshot"] as const)("does not recreate known admitted work after a missing GET proved by %s", async (proof) => {
  vi.useFakeTimers();
  if (proof === "running_snapshot") {
    vi.mocked(api.submitComposerOperation).mockRejectedValueOnce(new TypeError("Acknowledgement lost"));
    vi.mocked(api.fetchComposerOperation).mockResolvedValueOnce(snapshot("running"));
  }
  vi.mocked(api.fetchComposerOperation).mockResolvedValue({ kind: "operation_missing" });
  const observing = submitAndObserveComposerOperation(descriptor()).catch((error: unknown) => error);
  try {
    await vi.advanceTimersByTimeAsync(8000);
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
    expect(findComposerOperationCustody(scope, sid).foreground).toMatchObject({ operationId: id, body: descriptor().body });
  } finally {
    detachComposerObservers(); await vi.advanceTimersByTimeAsync(0);
    expect(await observing).toBeInstanceOf(ComposerObservationDetached);
  }
});

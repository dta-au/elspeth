// Regression cases for the U-42 Astra review (R1-R8, R12, R13).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("./client", () => ({
  fetchComposerOperationStreamV2: vi.fn(), fetchComposerOperationAnyVersion: vi.fn(), cancelComposerOperationV2: vi.fn(),
  apiErrorFromBody: vi.fn(), isDefinitiveComposerRefusal: vi.fn(() => false),
  submitComposerOperation: vi.fn(), fetchComposerOperation: vi.fn(), cancelComposerOperation: vi.fn(), fetchComposerOperationStream: vi.fn(), composerActiveOperation: vi.fn(() => null),
}));
import * as api from "./client";
import { observeComposerOperationV2 } from "./composerOperationObserverV2";
import { ComposerObservationDetached, ComposerSessionMissing, submitAndObserveComposerOperation } from "./composerOperationObserver";
import { COMPOSER_STOP_KEY, acquireComposerOperationCustody, attachComposerObserver, authenticateComposerCustody, composerProtocolVersion, composerStopRequested, findComposerOperationCustody, purgeComposerCustody, requestComposerStop } from "@/stores/composerOperationCustody";
import type { ComposerOperationSnapshotV2, ComposerOperationStatusV2, OperationCustody, SubmittedCustody } from "@/types/composerOperations";

const sid = "11111111-1111-4111-8111-111111111111", id = "22222222-2222-4222-8222-222222222222", mine = "55555555-5555-4555-8555-555555555555";
const scope = { principalId: "principal", authProvider: "local" };
const iso = () => new Date().toISOString();
function status(revision: number, extra: Partial<ComposerOperationStatusV2> = {}): ComposerOperationStatusV2 {
  return { protocol_version: 2, operation_id: id, kind: "compose_message", status: "running", phase: "executing", status_revision: revision, created_at: iso(), observed_at: iso(), poll_after_ms: 1000, cancel_requested: false, segment: { sequence: 1, generation: 1, state: "queued", deadline_at: null }, ...extra };
}
const live = (revision = 4): ComposerOperationSnapshotV2 => ({ ...status(revision), result: null, error: null });
const done: ComposerOperationSnapshotV2 = { ...status(9, { status: "completed", phase: "settled", segment: null }), result: { message: { id: "44444444-4444-4444-8444-444444444444", session_id: sid, role: "assistant", content: "done", tool_calls: null, created_at: iso() }, state: null, proposals: [] } as unknown as ComposerOperationSnapshotV2["result"], error: null };
function frame(event: string, sequence: number, payload?: unknown, eventId?: number): string {
  const data = { schema_version: "composer-operation-stream.v2", session_id: sid, operation_id: id, sequence, event, ...(payload === undefined ? {} : { payload }) };
  return `${eventId === undefined ? "" : `id: ${eventId}\n`}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}
function sse(...frames: string[]): Response {
  return new Response(new ReadableStream({ start(controller) { for (const item of frames) controller.enqueue(new TextEncoder().encode(item)); controller.close(); } }), { headers: { "Content-Type": "text/event-stream" } });
}
function openSse(signal: AbortSignal, ...frames: string[]): Response {
  return new Response(new ReadableStream({ start(controller) { for (const item of frames) controller.enqueue(new TextEncoder().encode(item)); signal.addEventListener("abort", () => controller.error(new Error("aborted"))); } }), { headers: { "Content-Type": "text/event-stream" } });
}
function observer(): OperationCustody {
  const submitted: SubmittedCustody = { mode: "submitted", scope, sessionId: sid, operationId: mine, kind: "compose_message", createdAt: Date.now(), body: { operation_id: mine, content: "x", state_id: null } };
  return attachComposerObserver(acquireComposerOperationCustody(submitted), id, "compose_message", false, 2);
}
beforeEach(() => {
  vi.resetAllMocks(); purgeComposerCustody(); sessionStorage.clear(); authenticateComposerCustody(scope);
  vi.mocked(api.isDefinitiveComposerRefusal).mockImplementation((error) => typeof error === "object" && error !== null && "status" in error && typeof error.status === "number" && error.status >= 400 && error.status < 500);
  vi.mocked(api.composerActiveOperation).mockReturnValue(null);
});
afterEach(() => { vi.useRealTimers(); purgeComposerCustody(); });

describe("U-42 review regressions", () => {
  it("R1: an expired segment deadline is consumed once, not a GET loop", async () => {
    vi.useFakeTimers();
    const expired = status(4, { segment: { sequence: 1, generation: 1, state: "running", deadline_at: new Date(Date.now() - 1000).toISOString() } });
    vi.mocked(api.fetchComposerOperationStreamV2).mockImplementation(async (_s, _o, signal) => openSse(signal, frame("status", 0, expired, 4)));
    vi.mocked(api.fetchComposerOperationAnyVersion).mockResolvedValue({ protocolVersion: 2, snapshot: { ...expired, result: null, error: null } });
    const controller = new AbortController();
    const observing = observeComposerOperationV2(observer(), { signal: controller.signal }).catch(() => undefined);
    await vi.advanceTimersByTimeAsync(3_000);
    expect(vi.mocked(api.fetchComposerOperationAnyVersion).mock.calls.length).toBeLessThanOrEqual(1);
    expect(vi.mocked(api.fetchComposerOperationStreamV2).mock.calls.length).toBeLessThanOrEqual(2);
    controller.abort(); await vi.advanceTimersByTimeAsync(1_000); await observing;
  });
  it("R2: a lifecycle-1 answer hands back to the v1 observer with custody recorded as 1", async () => {
    vi.mocked(api.fetchComposerOperationStreamV2).mockResolvedValue(new Response("", { status: 409 }));
    vi.mocked(api.fetchComposerOperationAnyVersion).mockResolvedValue({ protocolVersion: 1, snapshot: {} as never });
    const handBack = vi.fn(async (_descriptor: OperationCustody) => done.result!);
    vi.useFakeTimers();
    const observing = observeComposerOperationV2(observer(), { handBackToV1: handBack });
    await vi.advanceTimersByTimeAsync(4_000);
    await expect(observing).resolves.toBe(done.result);
    expect(handBack).toHaveBeenCalledTimes(1);
    expect(composerProtocolVersion(handBack.mock.calls[0][0])).toBe(1);
  });
  it("R3: a sealed v2 failure after admission is published once, not treated as an ambiguous admission", async () => {
    vi.mocked(api.submitComposerOperation).mockResolvedValue({ operation_id: id, kind: "compose_message", status: "queued", poll_after_ms: 1000, protocolVersion: 2 });
    const failed: ComposerOperationSnapshotV2 = { ...status(9, { status: "failed", phase: "settled", segment: null }), result: null, error: { http_status: 500, body: { detail: "Failed" }, error_type: null, failure_code: "operation_failed", diagnostic_id: "66666666-6666-4666-8666-666666666666" } };
    vi.mocked(api.fetchComposerOperationStreamV2).mockResolvedValue(sse(frame("terminal", 0, { status: "failed", phase: "settled", status_revision: 9 }, 9)));
    vi.mocked(api.fetchComposerOperationAnyVersion).mockResolvedValue({ protocolVersion: 2, snapshot: failed });
    vi.mocked(api.apiErrorFromBody).mockReturnValue({ status: 500, detail: "Failed" });
    const descriptor: SubmittedCustody = { mode: "submitted", scope, sessionId: sid, operationId: id, kind: "compose_message", createdAt: Date.now(), body: { operation_id: id, content: "x", state_id: null }, protocolVersion: 2 };
    await expect(submitAndObserveComposerOperation(descriptor)).rejects.toMatchObject({ composer_operation: { failure_code: "operation_failed" } });
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
  });
  it("R4: a second observer of the same operation replaces the first", async () => {
    vi.mocked(api.fetchComposerOperationStreamV2).mockImplementation(async (_s, _o, signal) => openSse(signal, frame("status", 0, status(4), 4)));
    const descriptor = observer();
    const first = observeComposerOperationV2(descriptor);
    await new Promise((resolve) => setTimeout(resolve, 20));
    const second = observeComposerOperationV2(descriptor).catch((error: unknown) => error);
    await expect(first).rejects.toBeInstanceOf(ComposerObservationDetached);
    const { detachComposerObservers } = await import("./composerOperationObserver");
    detachComposerObservers(sid);
    expect(await second).toBeInstanceOf(ComposerObservationDetached);
  });
  it.each([new Error("offline"), { status: 503, detail: "Service unavailable" }])("R5: an unconfirmed Stop retries without opening a stream (%#)", async (failure) => {
    vi.useFakeTimers();
    const descriptor = observer(); requestComposerStop(descriptor);
    vi.mocked(api.cancelComposerOperationV2).mockRejectedValueOnce(failure).mockResolvedValue({ ...live(5), cancel_requested: true });
    vi.mocked(api.fetchComposerOperationStreamV2).mockResolvedValue(sse(frame("terminal", 0, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    vi.mocked(api.fetchComposerOperationAnyVersion).mockResolvedValue({ protocolVersion: 2, snapshot: done });
    const unconfirmed: boolean[] = [];
    const observing = observeComposerOperationV2(descriptor, { stopUnconfirmed: (value) => unconfirmed.push(value) });
    await vi.advanceTimersByTimeAsync(10);
    expect(api.fetchComposerOperationStreamV2).not.toHaveBeenCalled();
    expect(unconfirmed).toEqual([true]);
    expect(composerStopRequested(descriptor)).toBe(true);
    expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(id);
    await vi.advanceTimersByTimeAsync(1_100);
    await expect(observing).resolves.toBe(done.result);
    expect(api.cancelComposerOperationV2).toHaveBeenCalledTimes(2);
    expect(unconfirmed[unconfirmed.length - 1]).toBe(false);
  });
  it("an explicit Stop-unavailable refusal preserves custody and observes the natural terminal", async () => {
    vi.useFakeTimers();
    const descriptor = observer(); requestComposerStop(descriptor);
    vi.mocked(api.cancelComposerOperationV2).mockRejectedValue({ status: 409, error_type: "composer_continuation_stop_unavailable", detail: "Stopping is unavailable." });
    vi.mocked(api.fetchComposerOperationStreamV2).mockResolvedValue(sse(frame("terminal", 0, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    let release!: (snapshot: { protocolVersion: 2; snapshot: ComposerOperationSnapshotV2 }) => void;
    vi.mocked(api.fetchComposerOperationAnyVersion).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const unavailable = vi.fn(), unconfirmed = vi.fn();
    const controller = new AbortController();
    const observing = observeComposerOperationV2(descriptor, { signal: controller.signal, stopUnavailable: unavailable, stopUnconfirmed: unconfirmed }).catch((error: unknown) => error);
    try {
      await vi.advanceTimersByTimeAsync(1_100);
      expect(unavailable).toHaveBeenCalledTimes(1);
      expect(unconfirmed).not.toHaveBeenCalledWith(true);
      expect(api.cancelComposerOperationV2).toHaveBeenCalledTimes(1);
      expect(api.fetchComposerOperationStreamV2).toHaveBeenCalledTimes(1);
      expect(api.fetchComposerOperationAnyVersion).toHaveBeenCalledTimes(1);
      expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(id);
      expect(composerStopRequested(descriptor)).toBe(false);
      expect(JSON.parse(sessionStorage.getItem(COMPOSER_STOP_KEY)!)).toEqual([]);
      release({ protocolVersion: 2, snapshot: done });
      await vi.advanceTimersByTimeAsync(0);
      expect(await observing).toBe(done.result);
      expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
      expect(api.submitComposerOperation).not.toHaveBeenCalled();
    } finally {
      release({ protocolVersion: 2, snapshot: done });
      controller.abort(); await vi.advanceTimersByTimeAsync(0); await observing;
    }
  });
  it("R6: repeated status-then-EOF connections escalate to the durable GET and report interruption", async () => {
    vi.useFakeTimers();
    vi.mocked(api.fetchComposerOperationStreamV2).mockImplementation(async () => sse(frame("status", 0, status(4), 4)));
    vi.mocked(api.fetchComposerOperationAnyVersion).mockResolvedValue({ protocolVersion: 2, snapshot: done });
    const interrupted: boolean[] = [];
    const observing = observeComposerOperationV2(observer(), { interrupted: (value) => interrupted.push(value) });
    await vi.advanceTimersByTimeAsync(1_100);
    await expect(observing).resolves.toBe(done.result);
    expect(interrupted).toContain(true);
  });
  it("R7: a refused read ends observation but keeps custody (it proves nothing finished)", async () => {
    vi.useFakeTimers();
    const descriptor = observer();
    vi.mocked(api.fetchComposerOperationStreamV2).mockResolvedValue(new Response("", { status: 403 }));
    vi.mocked(api.fetchComposerOperationAnyVersion).mockRejectedValue({ status: 403, detail: "Forbidden" });
    const observing = observeComposerOperationV2(descriptor).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(4_000);
    expect(await observing).toMatchObject({ status: 403 });
    expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(id);
  });
  it("R8: a lost session is the shared ComposerSessionMissing the store recognises", async () => {
    vi.mocked(api.fetchComposerOperationStreamV2).mockResolvedValue(sse(frame("terminal", 0, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    vi.mocked(api.fetchComposerOperationAnyVersion).mockResolvedValue({ kind: "session_missing" });
    await expect(observeComposerOperationV2(observer())).rejects.toBeInstanceOf(ComposerSessionMissing);
  });
  it("R12: a stream-capacity refusal polls the GET at poll_after_ms until Retry-After passes", async () => {
    vi.useFakeTimers();
    vi.mocked(api.fetchComposerOperationStreamV2).mockResolvedValueOnce(new Response("busy", { status: 503, headers: { "Retry-After": "3" } }))
      .mockResolvedValue(sse(frame("terminal", 0, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    vi.mocked(api.fetchComposerOperationAnyVersion).mockResolvedValue({ protocolVersion: 2, snapshot: live() });
    const controller = new AbortController();
    const observing = observeComposerOperationV2(observer(), { signal: controller.signal }).catch(() => undefined);
    await vi.advanceTimersByTimeAsync(2_500);
    expect(vi.mocked(api.fetchComposerOperationAnyVersion).mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(api.fetchComposerOperationStreamV2).toHaveBeenCalledTimes(1);
    controller.abort(); await vi.advanceTimersByTimeAsync(2_000); await observing;
  });
  it("R13: an equal-revision conflict stops streaming and falls back to the GET only", async () => {
    vi.useFakeTimers();
    vi.mocked(api.fetchComposerOperationStreamV2).mockResolvedValue(sse(frame("status", 0, status(4), 4), frame("status", 1, status(4, { phase: "reconciling" }), 4)));
    vi.mocked(api.fetchComposerOperationAnyVersion).mockResolvedValueOnce({ protocolVersion: 2, snapshot: live(4) }).mockResolvedValue({ protocolVersion: 2, snapshot: done });
    const observing = observeComposerOperationV2(observer());
    await vi.advanceTimersByTimeAsync(5_000);
    await expect(observing).resolves.toBe(done.result);
    expect(api.fetchComposerOperationStreamV2).toHaveBeenCalledTimes(1);
  });
});

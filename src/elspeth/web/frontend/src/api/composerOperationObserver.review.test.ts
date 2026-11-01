// Regression cases for the U-42 Astra review (R1, R3-R8, R12, R13; R2 was the retired lifecycle hand-back).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("./client", () => ({
  apiErrorFromBody: vi.fn(), isDefinitiveComposerRefusal: vi.fn(() => false),
  submitComposerOperation: vi.fn(), fetchComposerOperation: vi.fn(), cancelComposerOperation: vi.fn(), fetchComposerOperationStream: vi.fn(), composerActiveOperation: vi.fn(() => null),
}));
import * as api from "./client";
import { ComposerObservationDetached, ComposerSessionMissing, observeComposerOperation, submitAndObserveComposerOperation } from "./composerOperationObserver";
import { COMPOSER_STOP_KEY, acquireComposerOperationCustody, attachComposerObserver, authenticateComposerCustody, composerStopRequested, findComposerOperationCustody, purgeComposerCustody, requestComposerStop } from "@/stores/composerOperationCustody";
import type { ComposerOperationSnapshot, ComposerOperationStatus, OperationCustody, SubmittedCustody } from "@/types/composerOperations";

const sid = "11111111-1111-4111-8111-111111111111", id = "22222222-2222-4222-8222-222222222222", mine = "55555555-5555-4555-8555-555555555555";
const scope = { principalId: "principal", authProvider: "local" };
const iso = () => new Date().toISOString();
function status(revision: number, extra: Partial<ComposerOperationStatus> = {}): ComposerOperationStatus {
  return { operation_id: id, kind: "compose_message", status: "running", phase: "executing", status_revision: revision, created_at: iso(), observed_at: iso(), poll_after_ms: 1000, cancel_requested: false, segment: { sequence: 1, generation: 1, state: "queued", deadline_at: null }, ...extra };
}
const live = (revision = 4): ComposerOperationSnapshot => ({ ...status(revision), result: null, error: null });
const done: ComposerOperationSnapshot = { ...status(9, { status: "completed", phase: "settled", segment: null }), result: { message: { id: "44444444-4444-4444-8444-444444444444", session_id: sid, role: "assistant", content: "done", tool_calls: null, created_at: iso() }, state: null, proposals: [] } as unknown as ComposerOperationSnapshot["result"], error: null };
function frame(event: string, sequence: number, payload?: unknown, eventId?: number): string {
  const data = { session_id: sid, operation_id: id, sequence, event, ...(payload === undefined ? {} : { payload }) };
  return `${eventId === undefined ? "" : `id: ${eventId}\n`}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}
function sse(...frames: string[]): Response {
  return new Response(new ReadableStream({ start(controller) { for (const item of frames) controller.enqueue(new TextEncoder().encode(item)); controller.close(); } }), { headers: { "Content-Type": "text/event-stream" } });
}
function openSse(signal: AbortSignal, ...frames: string[]): Response {
  return new Response(new ReadableStream({ start(controller) { for (const item of frames) controller.enqueue(new TextEncoder().encode(item)); signal.addEventListener("abort", () => controller.error(new Error("aborted"))); } }), { headers: { "Content-Type": "text/event-stream" } });
}
function controlledSse(signal: AbortSignal, ...frames: string[]) {
  let enqueue!: (item: string) => void;
  const response = new Response(new ReadableStream<Uint8Array>({ start(controller) {
    enqueue = (item) => controller.enqueue(new TextEncoder().encode(item));
    for (const item of frames) enqueue(item);
    signal.addEventListener("abort", () => controller.error(new Error("aborted")));
  } }), { headers: { "Content-Type": "text/event-stream" } });
  return { response, enqueue: (item: string) => enqueue(item) };
}
function observer(): OperationCustody {
  const submitted: SubmittedCustody = { mode: "submitted", scope, sessionId: sid, operationId: mine, kind: "compose_message", createdAt: Date.now(), body: { operation_id: mine, content: "x", state_id: null } };
  return attachComposerObserver(acquireComposerOperationCustody(submitted), id, "compose_message", false);
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
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (_s, _o, signal) => openSse(signal, frame("status", 0, expired, 4)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue({ ...expired, result: null, error: null });
    const controller = new AbortController();
    const observing = observeComposerOperation(observer(), { signal: controller.signal }).catch(() => undefined);
    await vi.advanceTimersByTimeAsync(3_000);
    expect(vi.mocked(api.fetchComposerOperation).mock.calls.length).toBeLessThanOrEqual(1);
    expect(vi.mocked(api.fetchComposerOperationStream).mock.calls.length).toBeLessThanOrEqual(2);
    controller.abort(); await vi.advanceTimersByTimeAsync(1_000); await observing;
  });
  it("R3: a sealed failure after admission is published once, not treated as an ambiguous admission", async () => {
    vi.mocked(api.submitComposerOperation).mockResolvedValue({ ...status(1, { status: "queued", phase: "queued" }), result: null, error: null });
    const failed: ComposerOperationSnapshot = { ...status(9, { status: "failed", phase: "settled", segment: null }), result: null, error: { http_status: 500, body: { detail: "Failed" }, error_type: null, failure_code: "operation_failed", diagnostic_id: "66666666-6666-4666-8666-666666666666" } };
    vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(sse(frame("terminal", 0, { status: "failed", phase: "settled", status_revision: 9 }, 9)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue(failed);
    vi.mocked(api.apiErrorFromBody).mockReturnValue({ status: 500, detail: "Failed" });
    const descriptor: SubmittedCustody = { mode: "submitted", scope, sessionId: sid, operationId: id, kind: "compose_message", createdAt: Date.now(), body: { operation_id: id, content: "x", state_id: null } };
    await expect(submitAndObserveComposerOperation(descriptor)).rejects.toMatchObject({ composer_operation: { failure_code: "operation_failed" } });
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
  });
  it("R4: a second observer of the same operation replaces the first", async () => {
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (_s, _o, signal) => openSse(signal, frame("status", 0, status(4), 4)));
    const descriptor = observer();
    const first = observeComposerOperation(descriptor);
    await new Promise((resolve) => setTimeout(resolve, 20));
    const second = observeComposerOperation(descriptor).catch((error: unknown) => error);
    await expect(first).rejects.toBeInstanceOf(ComposerObservationDetached);
    const { detachComposerObservers } = await import("./composerOperationObserver");
    detachComposerObservers(sid);
    expect(await second).toBeInstanceOf(ComposerObservationDetached);
  });
  it.each([new Error("offline"), { status: 503, detail: "Service unavailable" }])("R5: an unconfirmed Stop retries without opening a stream (%#)", async (failure) => {
    vi.useFakeTimers();
    const descriptor = observer(); requestComposerStop(descriptor);
    vi.mocked(api.cancelComposerOperation).mockRejectedValueOnce(failure).mockResolvedValue({ ...live(5), cancel_requested: true });
    vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(sse(frame("terminal", 0, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue(done);
    const unconfirmed: boolean[] = [];
    const observing = observeComposerOperation(descriptor, { stopUnconfirmed: (value) => unconfirmed.push(value) });
    await vi.advanceTimersByTimeAsync(10);
    expect(api.fetchComposerOperationStream).not.toHaveBeenCalled();
    expect(unconfirmed).toEqual([true]);
    expect(composerStopRequested(descriptor)).toBe(true);
    expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(id);
    await vi.advanceTimersByTimeAsync(1_100);
    await expect(observing).resolves.toBe(done.result);
    expect(api.cancelComposerOperation).toHaveBeenCalledTimes(2);
    expect(unconfirmed[unconfirmed.length - 1]).toBe(false);
  });
  it("an explicit Stop-unavailable refusal preserves custody and observes the natural terminal", async () => {
    vi.useFakeTimers();
    const descriptor = observer(); requestComposerStop(descriptor);
    vi.mocked(api.cancelComposerOperation).mockRejectedValue({ status: 409, error_type: "composer_continuation_stop_unavailable", detail: "Stopping is unavailable." });
    vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(sse(frame("terminal", 0, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    let release!: (snapshot: ComposerOperationSnapshot) => void;
    vi.mocked(api.fetchComposerOperation).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const unavailable = vi.fn(), unconfirmed = vi.fn();
    const controller = new AbortController();
    const observing = observeComposerOperation(descriptor, { signal: controller.signal, stopUnavailable: unavailable, stopUnconfirmed: unconfirmed }).catch((error: unknown) => error);
    try {
      await vi.advanceTimersByTimeAsync(1_100);
      expect(unavailable).toHaveBeenCalledTimes(1);
      expect(unconfirmed).not.toHaveBeenCalledWith(true);
      expect(api.cancelComposerOperation).toHaveBeenCalledTimes(1);
      expect(api.fetchComposerOperationStream).toHaveBeenCalledTimes(1);
      expect(api.fetchComposerOperation).toHaveBeenCalledTimes(1);
      expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(id);
      expect(composerStopRequested(descriptor)).toBe(false);
      expect(JSON.parse(sessionStorage.getItem(COMPOSER_STOP_KEY)!)).toEqual([]);
      release(done);
      await vi.advanceTimersByTimeAsync(0);
      expect(await observing).toBe(done.result);
      expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
      expect(api.submitComposerOperation).not.toHaveBeenCalled();
    } finally {
      release(done);
      controller.abort(); await vi.advanceTimersByTimeAsync(0); await observing;
    }
  });
  it("R6: repeated status-then-EOF connections escalate to the durable GET and report interruption", async () => {
    vi.useFakeTimers();
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async () => sse(frame("status", 0, status(4), 4)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue(done);
    const interrupted: boolean[] = [];
    const observing = observeComposerOperation(observer(), { interrupted: (value) => interrupted.push(value) });
    await vi.advanceTimersByTimeAsync(1_100);
    await expect(observing).resolves.toBe(done.result);
    expect(interrupted).toContain(true);
  });
  it("R7: a refused read ends observation but keeps custody (it proves nothing finished)", async () => {
    vi.useFakeTimers();
    const descriptor = observer();
    vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(new Response("", { status: 403 }));
    vi.mocked(api.fetchComposerOperation).mockRejectedValue({ status: 403, detail: "Forbidden" });
    const observing = observeComposerOperation(descriptor).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(4_000);
    expect(await observing).toMatchObject({ status: 403 });
    expect(findComposerOperationCustody(scope, sid).foreground?.operationId).toBe(id);
  });
  it("R8: a lost session is the shared ComposerSessionMissing the store recognises", async () => {
    vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(sse(frame("terminal", 0, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue({ kind: "session_missing" });
    await expect(observeComposerOperation(observer())).rejects.toBeInstanceOf(ComposerSessionMissing);
  });
  it("R12: a stream-capacity refusal polls the GET at poll_after_ms until Retry-After passes", async () => {
    vi.useFakeTimers();
    vi.mocked(api.fetchComposerOperationStream).mockResolvedValueOnce(new Response("busy", { status: 503, headers: { "Retry-After": "3" } }))
      .mockResolvedValue(sse(frame("terminal", 0, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue(live());
    const controller = new AbortController();
    const observing = observeComposerOperation(observer(), { signal: controller.signal }).catch(() => undefined);
    await vi.advanceTimersByTimeAsync(2_500);
    expect(vi.mocked(api.fetchComposerOperation).mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(api.fetchComposerOperationStream).toHaveBeenCalledTimes(1);
    controller.abort(); await vi.advanceTimersByTimeAsync(2_000); await observing;
  });
  it("R13: an equal-revision conflict stops streaming and falls back to the GET only", async () => {
    vi.useFakeTimers();
    vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(sse(frame("status", 0, status(4), 4), frame("status", 1, status(4, { phase: "reconciling" }), 4)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValueOnce(live(4)).mockResolvedValue(done);
    const observing = observeComposerOperation(observer());
    await vi.advanceTimersByTimeAsync(5_000);
    await expect(observing).resolves.toBe(done.result);
    expect(api.fetchComposerOperationStream).toHaveBeenCalledTimes(1);
  });
});

describe("G6 accepted-state decisions", () => {
  it.each(["completed", "failed"] as const)("ignores a lower-revision %s status without closing the stream or dropping progress", async (terminalStatus) => {
    vi.useFakeTimers();
    const descriptor = observer(), progress = vi.fn(), interrupted = vi.fn(), accepted = vi.fn();
    const stale = status(9, { status: terminalStatus, phase: "settled", segment: null });
    const progressPayload = { request_id: null, phase: "calling_model", headline: "Still working", evidence: [], likely_next: null, reason: null, updated_at: iso(), segment_sequence: 1, segment_generation: 1 };
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (_s, _o, signal) => openSse(signal,
      frame("status", 0, status(10), 10), frame("status", 1, stale, 9), frame("progress", 2, progressPayload)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue({ ...done, status_revision: 11 });
    const controller = new AbortController();
    const observing = observeComposerOperation(descriptor, { signal: controller.signal, progress, interrupted, status: accepted }).catch((error: unknown) => error);
    try {
      await vi.advanceTimersByTimeAsync(0);
      expect(accepted.mock.calls.map(([value]) => value.status_revision)).toEqual([10]);
      expect(progress).toHaveBeenCalledOnce();
      expect(progress).toHaveBeenCalledWith(expect.objectContaining({ headline: "Still working" }));
      expect(api.fetchComposerOperation).not.toHaveBeenCalled();
      expect(interrupted).not.toHaveBeenCalled();
      expect(findComposerOperationCustody(scope, sid).foreground).toBe(descriptor);
    } finally { controller.abort(); await vi.advanceTimersByTimeAsync(0); await observing; }
  });

  it.each([
    ["lower", "completed"], ["lower", "failed"], ["equal-conflicting", "completed"], ["equal-conflicting", "failed"],
  ] as const)("does not seal a %s %s GET", async (revisionKind, terminalStatus) => {
    vi.useFakeTimers();
    const descriptor = observer(), accepted = vi.fn();
    const revision = revisionKind === "lower" ? 9 : 10;
    const rejected: ComposerOperationSnapshot = terminalStatus === "completed" ? { ...done, status_revision: revision } : {
      ...status(revision, { status: "failed", phase: "settled", segment: null }), result: null,
      error: { http_status: 500, body: { detail: "Rejected old failure" }, error_type: null, failure_code: "operation_failed", diagnostic_id: null },
    };
    const terminal = { ...done, status_revision: 12 };
    vi.mocked(api.apiErrorFromBody).mockReturnValue({ status: 500, detail: "Rejected old failure" });
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async () => sse(
      frame("status", 0, status(10), 10), frame("terminal", 1, { status: "completed", phase: "settled", status_revision: 11 }, 11)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValueOnce(rejected).mockResolvedValue(terminal);
    let settled = false;
    const controller = new AbortController();
    const observing = observeComposerOperation(descriptor, { signal: controller.signal, status: accepted }).catch((error: unknown) => error)
      .then((value: unknown) => { settled = true; return value; });
    try {
      await vi.advanceTimersByTimeAsync(0);
      expect(settled, "a rejected GET is not a sealed terminal").toBe(false);
      expect(findComposerOperationCustody(scope, sid).foreground).toBe(descriptor);
      expect(api.apiErrorFromBody).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(1000);
      expect(await observing).toBe(terminal.result);
      expect(accepted.mock.calls.map(([value]) => value.status_revision)).toEqual([10, 12]);
      expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
    } finally { controller.abort(); await vi.advanceTimersByTimeAsync(0); await observing; }
  });

  it("seals an equal-identical terminal GET after its status was already accepted from SSE", async () => {
    const descriptor = observer(), accepted = vi.fn();
    vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(sse(frame("status", 0, status(9, { status: "completed", phase: "settled", segment: null }), 9)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue(done);
    expect(await observeComposerOperation(descriptor, { status: accepted })).toBe(done.result);
    expect(accepted).toHaveBeenCalledOnce();
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
  });

  it.each(["lower", "equal-conflicting"] as const)("keeps retrying Stop after a %s apparent acknowledgement", async (revisionKind) => {
    vi.useFakeTimers();
    const descriptor = observer(), accepted = vi.fn((value: ComposerOperationStatus) => {
      if (value.status_revision === 10) requestComposerStop(descriptor);
    });
    vi.mocked(api.cancelComposerOperation).mockResolvedValueOnce({ ...live(revisionKind === "lower" ? 9 : 10), cancel_requested: true })
      .mockResolvedValue({ ...live(11), cancel_requested: true });
    vi.mocked(api.fetchComposerOperationStream).mockImplementationOnce(async () => sse(frame("status", 0, status(10), 10), frame("reconnect", 1)))
      .mockImplementation(async (_s, _o, signal) => openSse(signal));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue(live(10));
    const controller = new AbortController();
    const observing = observeComposerOperation(descriptor, { signal: controller.signal, status: accepted }).catch((error: unknown) => error);
    try {
      await vi.advanceTimersByTimeAsync(0);
      expect(api.cancelComposerOperation).toHaveBeenCalledOnce();
      expect(composerStopRequested(descriptor)).toBe(true);
      await vi.advanceTimersByTimeAsync(1000);
      expect(api.cancelComposerOperation).toHaveBeenCalledTimes(2);
      expect(accepted.mock.calls.map(([value]) => value.status_revision)).toEqual([10, 11]);
      expect(findComposerOperationCustody(scope, sid).foreground).toBe(descriptor);
    } finally { controller.abort(); await vi.advanceTimersByTimeAsync(0); await observing; }
  });

  it("ignores an older standalone terminal hint without claiming stream health", async () => {
    vi.useFakeTimers();
    const descriptor = observer(), interrupted = vi.fn();
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (_s, _o, signal) => openSse(signal,
      frame("status", 0, status(10), 10), frame("terminal", 1, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue(done);
    const controller = new AbortController();
    const observing = observeComposerOperation(descriptor, { signal: controller.signal, interrupted }).catch((error: unknown) => error);
    try {
      await vi.advanceTimersByTimeAsync(0);
      expect(api.fetchComposerOperation).not.toHaveBeenCalled();
      expect(interrupted).not.toHaveBeenCalled();
      expect(findComposerOperationCustody(scope, sid).foreground).toBe(descriptor);
    } finally { controller.abort(); await vi.advanceTimersByTimeAsync(0); await observing; }
  });

  it("uses GET recovery for an equal-conflicting terminal hint without reporting terminal stream health", async () => {
    vi.useFakeTimers();
    const descriptor = observer(), interrupted = vi.fn();
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (_s, _o, signal) => openSse(signal,
      frame("status", 0, status(10), 10), frame("terminal", 1, { status: "completed", phase: "settled", status_revision: 10 }, 10)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue({ ...done, status_revision: 11 });
    const controller = new AbortController();
    const observing = observeComposerOperation(descriptor, { signal: controller.signal, interrupted }).catch((error: unknown) => error);
    try {
      await vi.advanceTimersByTimeAsync(0);
      expect(interrupted).not.toHaveBeenCalledWith(false);
      expect(findComposerOperationCustody(scope, sid).foreground).toBe(descriptor);
      await vi.advanceTimersByTimeAsync(1000);
      expect(await observing).toBe(done.result);
      expect(api.fetchComposerOperationStream).toHaveBeenCalledOnce();
      expect(api.fetchComposerOperation).toHaveBeenCalledOnce();
    } finally { controller.abort(); await vi.advanceTimersByTimeAsync(0); await observing; }
  });
});

describe("G6 accepted deadline ownership", () => {
  it("ignores a rejected expired deadline while retaining the accepted deadline", async () => {
    vi.useFakeTimers();
    const accepted = status(10, { segment: { sequence: 1, generation: 1, state: "running", deadline_at: new Date(Date.now() + 10000).toISOString() } });
    const stale = status(9, { segment: { sequence: 2, generation: 1, state: "running", deadline_at: new Date(Date.now() - 1000).toISOString() } });
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (_s, _o, signal) => openSse(signal, frame("status", 0, accepted, 10), frame("status", 1, stale, 9)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue({ ...accepted, result: null, error: null });
    const controller = new AbortController(), observing = observeComposerOperation(observer(), { signal: controller.signal }).catch((error: unknown) => error);
    try {
      await vi.advanceTimersByTimeAsync(5000);
      expect(api.fetchComposerOperation).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(5000);
      expect(api.fetchComposerOperation).toHaveBeenCalledOnce();
    } finally { controller.abort(); await vi.advanceTimersByTimeAsync(0); await observing; }
  });

  it("does not postpone the accepted deadline using a duplicate observation timestamp", async () => {
    vi.useFakeTimers();
    const accepted = status(10, { segment: { sequence: 1, generation: 1, state: "running", deadline_at: new Date(Date.now() + 10000).toISOString() } });
    let connection!: ReturnType<typeof controlledSse>;
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (_s, _o, signal) => {
      connection = controlledSse(signal, frame("status", 0, accepted, 10)); return connection.response;
    });
    vi.mocked(api.fetchComposerOperation).mockResolvedValue({ ...accepted, result: null, error: null });
    const controller = new AbortController(), observing = observeComposerOperation(observer(), { signal: controller.signal }).catch((error: unknown) => error);
    try {
      await vi.advanceTimersByTimeAsync(6000);
      connection.enqueue(frame("status", 1, { ...accepted, observed_at: new Date(Date.now() - 7000).toISOString() }, 10));
      await vi.advanceTimersByTimeAsync(4000);
      expect(api.fetchComposerOperation).toHaveBeenCalledOnce();
    } finally { controller.abort(); await vi.advanceTimersByTimeAsync(0); await observing; }
  });

  it("cancels an earlier deadline when a fresh accepted state has no deadline", async () => {
    vi.useFakeTimers();
    const old = status(10, { segment: { sequence: 1, generation: 1, state: "running", deadline_at: new Date(Date.now() + 5000).toISOString() } });
    const next = status(11);
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (_s, _o, signal) => openSse(signal, frame("status", 0, old, 10), frame("status", 1, next, 11)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue({ ...next, result: null, error: null });
    const controller = new AbortController(), observing = observeComposerOperation(observer(), { signal: controller.signal }).catch((error: unknown) => error);
    try {
      await vi.advanceTimersByTimeAsync(6000);
      expect(api.fetchComposerOperation).not.toHaveBeenCalled();
    } finally { controller.abort(); await vi.advanceTimersByTimeAsync(0); await observing; }
  });

  it("replaces an earlier deadline with the fresh accepted segment deadline", async () => {
    vi.useFakeTimers();
    const old = status(10, { segment: { sequence: 1, generation: 1, state: "running", deadline_at: new Date(Date.now() + 5000).toISOString() } });
    const next = status(11, { segment: { sequence: 2, generation: 1, state: "running", deadline_at: new Date(Date.now() + 10000).toISOString() } });
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (_s, _o, signal) => openSse(signal, frame("status", 0, old, 10), frame("status", 1, next, 11)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue({ ...next, result: null, error: null });
    const controller = new AbortController(), observing = observeComposerOperation(observer(), { signal: controller.signal }).catch((error: unknown) => error);
    try {
      await vi.advanceTimersByTimeAsync(6000);
      expect(api.fetchComposerOperation).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(4000);
      expect(api.fetchComposerOperation).toHaveBeenCalledOnce();
    } finally { controller.abort(); await vi.advanceTimersByTimeAsync(0); await observing; }
  });

  it("carries an unconsumed accepted deadline across reconnect without restarting its duration", async () => {
    vi.useFakeTimers();
    const accepted = status(10, { segment: { sequence: 1, generation: 1, state: "running", deadline_at: new Date(Date.now() + 10000).toISOString() } });
    let first!: ReturnType<typeof controlledSse>;
    vi.mocked(api.fetchComposerOperationStream).mockImplementationOnce(async (_s, _o, signal) => {
      first = controlledSse(signal, frame("status", 0, accepted, 10)); return first.response;
    }).mockImplementation(async (_s, _o, signal) => openSse(signal, frame("status", 0, accepted, 10)));
    vi.mocked(api.fetchComposerOperation).mockResolvedValue({ ...accepted, result: null, error: null });
    const controller = new AbortController(), observing = observeComposerOperation(observer(), { signal: controller.signal }).catch((error: unknown) => error);
    try {
      await vi.advanceTimersByTimeAsync(6000);
      first.enqueue(frame("reconnect", 1));
      await vi.advanceTimersByTimeAsync(0);
      expect(api.fetchComposerOperationStream).toHaveBeenCalledTimes(2);
      expect(vi.mocked(api.fetchComposerOperationStream).mock.calls[1][3]).toBe(10);
      await vi.advanceTimersByTimeAsync(4000);
      expect(api.fetchComposerOperation).toHaveBeenCalledOnce();
    } finally { controller.abort(); await vi.advanceTimersByTimeAsync(0); await observing; }
  });
});

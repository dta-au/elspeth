import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as observer from "./composerOperationObserver";
import { observeComposerOperationV2 } from "./composerOperationObserverV2";
import { ComposerOperationDecodeError, decodeComposerOperationAck, decodeComposerOperationFrame, decodeComposerOperationSnapshot } from "./composerOperationDecoder";
import { decodeComposerOperationFrameV2, decodeComposerOperationSnapshotV2, decodeComposerOperationStatusV2 } from "./composerOperationDecoderV2";
import { acquireComposerOperationCustody, authenticateComposerCustody, composerStopRequested, findComposerOperationCustody, purgeComposerCustody, requestComposerStop } from "@/stores/composerOperationCustody";
import { setComposerAdmissionVersion } from "@/config/composerProtocol";
import type { OperationKind, OperationStatus, SubmittedCustody } from "@/types/composerOperations";

vi.mock("./client", async (importOriginal) => ({
  ...await importOriginal<typeof import("./client")>(),
  submitComposerOperation: vi.fn(), fetchComposerOperation: vi.fn(), cancelComposerOperation: vi.fn(), fetchComposerOperationStream: vi.fn(),
  fetchComposerOperationAnyVersion: vi.fn(), fetchComposerOperationStreamV2: vi.fn(), cancelComposerOperationV2: vi.fn(),
}));
import * as api from "./client";

const sid = "11111111-1111-4111-8111-111111111111", id = "22222222-2222-4222-8222-222222222222", other = "33333333-3333-4333-8333-333333333333";
const scope = { principalId: "principal", authProvider: "local" };
const iso = "2026-10-31T00:00:00Z";
const faultMessage = "Composer observation stopped before the outcome could be confirmed";
function descriptor(version: 1 | 2 = 1): SubmittedCustody {
  return { mode: "submitted", scope, sessionId: sid, operationId: id, kind: "compose_message", createdAt: Date.now(), ...(version === 2 ? { protocolVersion: 2 } : {}), body: { operation_id: id, content: "hello", state_id: null } };
}
function result(operationId = id) {
  return { message: { id: other, operation_id: operationId, session_id: sid, role: "assistant", content: "done", raw_content: null, segments: [], tool_calls: null, created_at: iso, composition_state_id: null, tool_call_id: null, parent_assistant_id: null, sequence_no: 2, rejection: null }, state: null, proposals: [] };
}
function snapshot(status: OperationStatus = "completed", kind: OperationKind = "compose_message", operationId = id) {
  return decodeComposerOperationSnapshot({ operation_id: operationId, kind, status, cancel_requested: false, deadline_at: iso, deadline_remaining_ms: status === "running" ? 100000 : 0, poll_after_ms: 1000,
    result: status === "completed" ? result(operationId) : null,
    error: status === "failed" ? { http_status: 503, body: {}, error_type: "composer_operation_worker_lost", failure_code: "worker_lost", diagnostic_id: null } : null,
  }, sid, operationId);
}
function statusV2(revision = 4, phase: "executing" | "reconciling" = "executing") {
  return decodeComposerOperationStatusV2({ protocol_version: 2, operation_id: id, kind: "compose_message", status: "running", phase, status_revision: revision, created_at: iso, observed_at: iso, poll_after_ms: 1000, cancel_requested: false, segment: { sequence: 1, generation: 1, state: "queued", deadline_at: null } }, id);
}
function snapshotV2(completed = true) {
  return decodeComposerOperationSnapshotV2({ ...statusV2(completed ? 9 : 4), ...(completed ? { status: "completed", phase: "settled", segment: null } : {}), result: completed ? result() : null, error: null }, sid, id);
}
function stream(text: string, close = false) {
  const cancel = vi.fn();
  const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new TextEncoder().encode(text)); if (close) controller.close(); }, cancel });
  return { body, cancel, response: new Response(body, { headers: { "Content-Type": "text/event-stream" } }) };
}
const progressV1 = { schema_version: "composer-operation-stream.v1", session_id: sid, operation_id: id, sequence: 1, event: "progress", payload: { session_operation_id: id, session_operation_epoch: 1, request_token: "token", request_id: null, phase: "calling_model", headline: "Working", evidence: [], likely_next: null, reason: null, updated_at: iso } };
const progressV2 = { schema_version: "composer-operation-stream.v2", session_id: sid, operation_id: id, sequence: 1, event: "progress", payload: { request_id: null, phase: "calling_model", headline: "Working", evidence: [], likely_next: null, reason: null, updated_at: iso, segment_sequence: 1, segment_generation: 1 } };
function v2StatusFrame(revision = 4, phase: "executing" | "reconciling" = "executing", sequence = 0) {
  return { schema_version: "composer-operation-stream.v2", session_id: sid, operation_id: id, sequence, event: "status", payload: statusV2(revision, phase) };
}
function sse(value: unknown, revision?: number) { return `${revision === undefined ? "" : `id: ${revision}\n`}data: ${JSON.stringify(value)}\n\n`; }
function ownedFault(value: unknown, code: string): PropertyDescriptor {
  // Lookup happens only after the real old consumer ran. A future export must
  // not turn this regression into an import/collection failure on the base.
  const constructor: unknown = Reflect.get(observer, "ComposerObservationFault");
  expect(constructor).toBeTypeOf("function");
  if (typeof constructor !== "function") throw new Error("Missing nominal observation-fault constructor after reaching the consumer");
  expect(value).toBeInstanceOf(constructor);
  expect(value).toHaveProperty("code", code);
  expect(value).toHaveProperty("message", faultMessage);
  const privateCause = Object.getOwnPropertyDescriptor(value, "cause");
  expect(privateCause).toBeDefined(); expect(privateCause?.enumerable).toBe(false);
  if (privateCause === undefined) throw new Error("Missing private observation-fault cause");
  expect(JSON.stringify(value)).not.toContain("PRIVATE_CALLBACK_SENTINEL");
  return privateCause;
}
const callbackValues = [
  ["Error", new Error("PRIVATE_CALLBACK_SENTINEL")],
  ["TypeError", new TypeError("PRIVATE_CALLBACK_SENTINEL")],
  ["decoder sentinel", new ComposerOperationDecodeError("PRIVATE_CALLBACK_SENTINEL")],
  ["non-Error object", { private: "PRIVATE_CALLBACK_SENTINEL" }],
  ["undefined", undefined],
] as const;

beforeEach(() => {
  vi.resetAllMocks(); vi.useFakeTimers(); vi.setSystemTime(new Date(iso)); purgeComposerCustody(); sessionStorage.clear(); authenticateComposerCustody(scope); setComposerAdmissionVersion(1);
  vi.mocked(api.submitComposerOperation).mockResolvedValue(decodeComposerOperationAck({ operation_id: id, kind: "compose_message", status: "queued", poll_after_ms: 1000 }));
  vi.mocked(api.fetchComposerOperationStream).mockRejectedValue(new TypeError("offline"));
  vi.mocked(api.fetchComposerOperation).mockImplementation(async () => snapshot());
  vi.mocked(api.cancelComposerOperation).mockResolvedValue({ kind: "operation_missing" });
  vi.mocked(api.fetchComposerOperationStreamV2).mockImplementation(async () => new Response("capacity", { status: 503 }));
  vi.mocked(api.fetchComposerOperationAnyVersion).mockImplementation(async () => ({ protocolVersion: 2, snapshot: snapshotV2() }));
});
afterEach(async () => { observer.detachComposerObservers(); await vi.advanceTimersByTimeAsync(0); vi.useRealTimers(); purgeComposerCustody(); setComposerAdmissionVersion(1); vi.restoreAllMocks(); });

describe("R F2 callback fault ownership", () => {
  describe.each(["v1 progress", "v2 stream progress", "v2 stream status", "v2 GET status"] as const)("%s", (route) => {
    it.each(callbackValues)("retains exact callback-thrown %s without fallback or settlement", async (_label, thrownValue) => {
      const held = acquireComposerOperationCustody(descriptor(route === "v1 progress" ? 1 : 2));
      const signal = new AbortController(), removeListener = vi.spyOn(signal.signal, "removeEventListener");
      const callback = vi.fn(() => { throw thrownValue; });
      const wire = route === "v1 progress" ? progressV1 : route === "v2 stream progress" ? progressV2 : v2StatusFrame();
      if (route === "v1 progress") expect(decodeComposerOperationFrame(wire, sid, id).event).toBe("progress");
      else expect(decodeComposerOperationFrameV2({ id: route === "v2 stream progress" ? null : "4", data: wire }, sid, id).event).toBe(route === "v2 stream progress" ? "progress" : "status");
      const live = stream(sse(wire, route === "v2 stream status" ? 4 : undefined), route === "v2 GET status");
      if (route === "v1 progress") vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(live.response);
      else if (route === "v2 GET status") vi.mocked(api.fetchComposerOperationAnyVersion).mockResolvedValue({ protocolVersion: 2, snapshot: snapshotV2(false) });
      else vi.mocked(api.fetchComposerOperationStreamV2).mockResolvedValue(live.response);
      let settled = false;
      const options = { signal: signal.signal, ...(route.endsWith("status") ? { status: callback } : { progress: callback }) };
      const observing = (route === "v1 progress" ? observer.observeComposerOperation(held, { ...options, submit: true }) : observeComposerOperationV2(held, options))
        .catch((error: unknown) => error).then((value: unknown) => { settled = true; return value; });
      try {
        await vi.advanceTimersByTimeAsync(0);
        expect(callback).toHaveBeenCalledTimes(1);
        expect(settled, "callback failure must reject before transport backoff").toBe(true);
        // The base must first fail at its real fallback/settlement behavior,
        // rather than merely at the not-yet-added nominal export.
        expect(api.fetchComposerOperation).not.toHaveBeenCalled();
        expect(api.fetchComposerOperationAnyVersion).toHaveBeenCalledTimes(route === "v2 GET status" ? 1 : 0);
        expect(findComposerOperationCustody(scope, sid).foreground).toBe(held);
        expect(ownedFault(await observing, "progress_callback_failed").value).toBe(thrownValue);
        expect(composerStopRequested(held)).toBe(false);
        expect(removeListener).toHaveBeenCalledWith("abort", expect.any(Function));
        if (route !== "v2 GET status") {
          expect(live.cancel).toHaveBeenCalledTimes(1); expect(live.body.locked).toBe(false);
          const transport = route === "v1 progress" ? api.fetchComposerOperationStream : api.fetchComposerOperationStreamV2;
          expect(vi.mocked(transport).mock.calls[0][2].aborted).toBe(true);
        }
        await vi.advanceTimersByTimeAsync(180000);
        expect(api.fetchComposerOperation).not.toHaveBeenCalled();
        expect(api.fetchComposerOperationAnyVersion).toHaveBeenCalledTimes(route === "v2 GET status" ? 1 : 0);
        expect(api.submitComposerOperation).toHaveBeenCalledTimes(route === "v1 progress" ? 1 : 0); expect(api.cancelComposerOperation).not.toHaveBeenCalled(); expect(api.cancelComposerOperationV2).not.toHaveBeenCalled();
        expect(vi.getTimerCount()).toBe(0);
      } finally { observer.detachComposerObservers(); await vi.advanceTimersByTimeAsync(0); await observing; }
    });
  });
});

describe("R F2 V2 Stop status callback ownership", () => {
  it.each(callbackValues)("retains exact Stop-status callback-thrown %s without cancellation recovery", async (_label, thrownValue) => {
    const held = acquireComposerOperationCustody(descriptor(2));
    requestComposerStop(held);
    const stopped = snapshotV2(false);
    expect(stopped).toMatchObject({ operation_id: id, kind: "compose_message", status: "running", cancel_requested: false, result: null, error: null });
    vi.mocked(api.cancelComposerOperationV2).mockResolvedValue(stopped);
    const callback = vi.fn(() => { throw thrownValue; });
    let settled = false;
    const observing = observeComposerOperationV2(held, { status: callback })
      .catch((error: unknown) => error).then((value: unknown) => { settled = true; return value; });
    try {
      await vi.advanceTimersByTimeAsync(0);
      expect(callback).toHaveBeenCalledExactlyOnceWith(stopped);
      expect(settled, "Stop status callback failure must reject before cancellation backoff").toBe(true);
      expect(ownedFault(await observing, "progress_callback_failed").value).toBe(thrownValue);
      expect(findComposerOperationCustody(scope, sid).foreground).toBe(held);
      expect(composerStopRequested(held)).toBe(true);
      await vi.advanceTimersByTimeAsync(180000);
      expect(api.cancelComposerOperationV2).toHaveBeenCalledExactlyOnceWith(sid, id);
      expect(api.cancelComposerOperation).not.toHaveBeenCalled(); expect(api.submitComposerOperation).not.toHaveBeenCalled();
      expect(api.fetchComposerOperationStreamV2).not.toHaveBeenCalled(); expect(api.fetchComposerOperationStream).not.toHaveBeenCalled();
      expect(api.fetchComposerOperationAnyVersion).not.toHaveBeenCalled(); expect(api.fetchComposerOperation).not.toHaveBeenCalled();
      expect(findComposerOperationCustody(scope, sid).foreground).toBe(held); expect(composerStopRequested(held)).toBe(true);
      expect(vi.getTimerCount()).toBe(0);
    } finally { observer.detachComposerObservers(); await vi.advanceTimersByTimeAsync(0); await observing; }
  });
});

describe("R F3 bound relationship ownership", () => {
  it.each([
    ["ack", "queued", "ack_identity_mismatch", "Composer acknowledgement identity mismatch"],
    ["poll", "running", "snapshot_kind_mismatch", "Operation kind mismatch"],
    ["poll", "completed", "snapshot_kind_mismatch", "Operation kind mismatch"],
    ["poll", "failed", "snapshot_kind_mismatch", "Operation kind mismatch"],
    ["cancel", "running", "cancel_kind_mismatch", "Cancellation kind mismatch"],
  ] as const)("rejects requested-ID opposite-kind %s %s before retry", async (boundary, status, code, causeText) => {
    const held = descriptor();
    if (boundary === "ack") {
      const ack = decodeComposerOperationAck({ operation_id: id, kind: "compose_recompose", status, poll_after_ms: 1000 });
      expect(ack).toMatchObject({ operation_id: id, kind: "compose_recompose", status: "queued" });
      vi.mocked(api.submitComposerOperation).mockResolvedValue(ack);
    } else {
      const opposite = snapshot(status, "compose_recompose");
      expect(opposite).toMatchObject({ operation_id: id, kind: "compose_recompose", status });
      if (boundary === "poll") vi.mocked(api.fetchComposerOperation).mockResolvedValue(opposite);
      else { acquireComposerOperationCustody(held); requestComposerStop(held); vi.mocked(api.cancelComposerOperation).mockResolvedValue(opposite); }
    }
    let settled = false;
    const observing = (boundary === "cancel" ? observer.observeComposerOperation(held) : observer.submitAndObserveComposerOperation(held))
      .catch((error: unknown) => error).then((value: unknown) => { settled = true; return value; });
    try {
      await vi.advanceTimersByTimeAsync(0);
      expect(settled, "relationship fault must reject before backoff").toBe(true);
      expect(api.fetchComposerOperation).toHaveBeenCalledTimes(boundary === "poll" ? 1 : 0);
      expect(findComposerOperationCustody(scope, sid).foreground).toEqual(held);
      // The native guard has no externally supplied identity witness. Check
      // its retained origin without replacing Error or the real consumer.
      const nativeCause: unknown = ownedFault(await observing, code).value;
      expect(nativeCause).toBeInstanceOf(Error);
      expect(nativeCause).toHaveProperty("message", causeText);
      expect(nativeCause).toHaveProperty("stack", expect.stringMatching(/at .*observeComposerOperation.*composerOperationObserver\.ts/));
      expect(composerStopRequested(held)).toBe(boundary === "cancel");
      await vi.advanceTimersByTimeAsync(180000);
      expect(api.submitComposerOperation).toHaveBeenCalledTimes(boundary === "cancel" ? 0 : 1);
      expect(api.fetchComposerOperation).toHaveBeenCalledTimes(boundary === "poll" ? 1 : 0);
      expect(api.cancelComposerOperation).toHaveBeenCalledTimes(boundary === "cancel" ? 1 : 0);
      if (boundary === "cancel") expect(api.cancelComposerOperation).toHaveBeenCalledExactlyOnceWith(sid, id);
      expect(vi.getTimerCount()).toBe(0);
    } finally { observer.detachComposerObservers(); await vi.advanceTimersByTimeAsync(0); await observing; }
  });
});

describe("P transport and decoding recovery", () => {
  it.each(["malformed_sse", "malformed_frame", "read_failure", "eof", "network_get", "503", "malformed_get", "wrong_id", "fault_impostor"])("recovers %s through the real decoder with one submission", async (failure) => {
    if (failure === "malformed_sse") vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(stream("data: {broken\n\n").response);
    if (failure === "malformed_frame") vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(stream(sse({ ...progressV1, payload: { ...progressV1.payload, phase: "unknown" } })).response);
    if (failure === "read_failure") vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(new Response(new ReadableStream({ start(controller) { controller.error(new TypeError("read failed")); } }), { headers: { "Content-Type": "text/event-stream" } }));
    if (failure === "eof") vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(stream("", true).response);
    if (failure === "network_get") vi.mocked(api.fetchComposerOperation).mockRejectedValueOnce(new TypeError("network unavailable"));
    if (failure === "503") vi.mocked(api.fetchComposerOperation).mockRejectedValueOnce({ status: 503 });
    if (failure === "fault_impostor") vi.mocked(api.fetchComposerOperation).mockRejectedValueOnce(Object.assign(new Error(faultMessage), { name: "ComposerObservationFault", code: "progress_callback_failed", cause: "PRIVATE_CALLBACK_SENTINEL" }));
    if (failure === "malformed_get" || failure === "wrong_id") {
      const malformed = failure === "wrong_id" ? snapshot("completed", "compose_message", other) : { ...snapshot(), poll_after_ms: "bad" };
      expect(() => decodeComposerOperationSnapshot(malformed, sid, id)).toThrow(ComposerOperationDecodeError);
      vi.mocked(api.fetchComposerOperation).mockImplementationOnce(async () => decodeComposerOperationSnapshot(malformed, sid, id));
    }
    const observing = observer.submitAndObserveComposerOperation(descriptor());
    await vi.advanceTimersByTimeAsync(1000);
    expect((await observing).message.content).toBe("done");
    expect(api.fetchComposerOperation).toHaveBeenCalledTimes(["malformed_sse", "malformed_frame", "read_failure", "eof"].includes(failure) ? 1 : 2);
    expect(vi.mocked(api.fetchComposerOperation).mock.calls.every(([session, operation]) => session === sid && operation === id)).toBe(true);
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1); expect(api.cancelComposerOperation).not.toHaveBeenCalled();
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
  });

  it("keeps V2 equal-revision conflicts on authenticated GET recovery", async () => {
    const held = acquireComposerOperationCustody(descriptor(2));
    const first = v2StatusFrame(), conflict = v2StatusFrame(4, "reconciling", 1);
    expect(decodeComposerOperationFrameV2({ id: "4", data: first }, sid, id).event).toBe("status");
    expect(decodeComposerOperationFrameV2({ id: "4", data: conflict }, sid, id).event).toBe("status");
    vi.mocked(api.fetchComposerOperationStreamV2).mockResolvedValue(stream(sse(first, 4) + sse(conflict, 4)).response);
    vi.mocked(api.fetchComposerOperationAnyVersion).mockResolvedValueOnce({ protocolVersion: 2, snapshot: snapshotV2(false) }).mockResolvedValue({ protocolVersion: 2, snapshot: snapshotV2() });
    let settled = false;
    const status = vi.fn(), observing = observeComposerOperationV2(held, { status }).then((value) => { settled = true; return value; });
    await vi.advanceTimersByTimeAsync(0);
    expect(api.fetchComposerOperationStreamV2).toHaveBeenCalledTimes(1); expect(api.fetchComposerOperationAnyVersion).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1000);
    expect(settled).toBe(false); expect(api.fetchComposerOperationAnyVersion).toHaveBeenCalledTimes(1);
    expect(status.mock.calls.map(([value]) => value.status_revision)).toEqual([4]);
    await vi.advanceTimersByTimeAsync(1000);
    expect((await observing).message.content).toBe("done");
    expect(status.mock.calls.map(([value]) => value.status_revision)).toEqual([4, 9]);
    expect(api.fetchComposerOperationStreamV2).toHaveBeenCalledTimes(1); expect(api.fetchComposerOperationAnyVersion).toHaveBeenCalledTimes(2);
    expect(api.submitComposerOperation).not.toHaveBeenCalled(); expect(api.cancelComposerOperationV2).not.toHaveBeenCalled();
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
  });

});

describe("R fault recovery custody and Stop", () => {
  it("restores custody after a fault and preserves its exact Stop target without POST", async () => {
    const held = acquireComposerOperationCustody(descriptor());
    vi.mocked(api.fetchComposerOperation).mockResolvedValueOnce(snapshot("running", "compose_recompose"));
    let settled = false;
    const first = observer.observeComposerOperation(held).catch((error: unknown) => error).then((value: unknown) => { settled = true; return value; });
    try {
      await vi.advanceTimersByTimeAsync(0); expect(settled).toBe(true);
      expect(await first).toHaveProperty("code", "snapshot_kind_mismatch");
      expect(observer.cancelObservedComposerOperation(sid)).toBe(false);
      expect(composerStopRequested(held)).toBe(true);
      expect(api.cancelComposerOperation).toHaveBeenCalledExactlyOnceWith(sid, id);
      vi.mocked(api.cancelComposerOperation).mockClear();
      vi.mocked(api.cancelComposerOperation).mockResolvedValue(snapshot("running"));
      vi.mocked(api.fetchComposerOperation).mockResolvedValueOnce({ kind: "operation_missing" }).mockResolvedValue(snapshot());
      const restored = observer.observeComposerOperation(findComposerOperationCustody(scope, sid).foreground!);
      await vi.advanceTimersByTimeAsync(1000); expect((await restored).message.content).toBe("done");
      expect(api.submitComposerOperation).not.toHaveBeenCalled(); expect(api.cancelComposerOperation).toHaveBeenCalled();
      expect(vi.mocked(api.cancelComposerOperation).mock.calls.every(([session, operation]) => session === sid && operation === id)).toBe(true);
      expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
    } finally { observer.detachComposerObservers(); await vi.advanceTimersByTimeAsync(0); await first; }
  });
});

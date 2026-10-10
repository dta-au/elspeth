import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ComposerObservationDetached, detachComposerObservers, submitAndObserveComposerOperation } from "./composerOperationObserver";
import { authenticateComposerCustody, findComposerOperationCustody, purgeComposerCustody } from "@/stores/composerOperationCustody";
import type { SubmittedCustody } from "@/types/composerOperations";
vi.mock("./client", () => ({ submitComposerOperation: vi.fn(), fetchComposerOperation: vi.fn(), cancelComposerOperation: vi.fn(), fetchComposerOperationStream: vi.fn(), apiErrorFromBody: vi.fn(), composerActiveOperation: vi.fn(), isDefinitiveComposerRefusal: vi.fn() }));
import * as api from "./client";
const sessionId = "11111111-1111-4111-8111-111111111111", operationId = "22222222-2222-4222-8222-222222222222";
const scope = { principalId: "principal", authProvider: "local" };
function descriptor(): SubmittedCustody { return { mode: "submitted", scope, sessionId, operationId, kind: "compose_message", createdAt: Date.now(), body: { operation_id: operationId, content: "hello", state_id: null } }; }
beforeEach(() => {
  vi.resetAllMocks(); vi.useFakeTimers(); sessionStorage.clear(); purgeComposerCustody(); authenticateComposerCustody(scope);
  vi.mocked(api.submitComposerOperation).mockResolvedValue({ operation_id: operationId, kind: "compose_message", status: "queued", poll_after_ms: 1000 });
  vi.mocked(api.composerActiveOperation).mockReturnValue(null); vi.mocked(api.isDefinitiveComposerRefusal).mockReturnValue(false);
  vi.mocked(api.fetchComposerOperation).mockResolvedValue({ operation_id: operationId, kind: "compose_message", status: "completed", cancel_requested: false, poll_after_ms: 1000, deadline_at: "2026-10-06T00:00:00Z", deadline_remaining_ms: 0, result: { message: { id: operationId, session_id: sessionId, role: "assistant", content: "done", tool_calls: null, created_at: "2026-10-06T00:00:00Z" }, state: null, proposals: [] }, error: null });
});
afterEach(() => { detachComposerObservers(); vi.useRealTimers(); purgeComposerCustody(); });
function liveStream(remaining: number): void {
  vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (_sid, _id, signal) => {
    let heartbeat: ReturnType<typeof setInterval>;
    let sequence = 0;
    const body = new ReadableStream<Uint8Array>({ start(controller) {
      const emit = (event: "status" | "heartbeat", payload?: unknown) => controller.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify({ schema_version: "composer-operation-stream.v1", session_id: sessionId, operation_id: operationId, sequence: sequence++, event, ...(payload === undefined ? {} : { payload }) })}\n\n`));
      emit("status", { status: "running", cancel_requested: false, deadline_remaining_ms: remaining });
      heartbeat = setInterval(() => emit("heartbeat"), 5000);
      signal.addEventListener("abort", () => { clearInterval(heartbeat); controller.error(new DOMException("aborted", "AbortError")); }, { once: true });
    }, cancel() { clearInterval(heartbeat); } });
    return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
  });
}
it("stream status zero expires observation after grace despite valid heartbeats", async () => {
  liveStream(0);
  const observing = submitAndObserveComposerOperation(descriptor()).catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(24999); expect(api.fetchComposerOperation).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1); expect(await observing).toBeInstanceOf(ComposerObservationDetached);
  expect(findComposerOperationCustody(scope, sessionId).foreground?.operationId).toBe(operationId);
  expect(api.cancelComposerOperation).not.toHaveBeenCalled(); expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
});
it("a throwing timeout callback still detaches and closes the physical stream", async () => {
  liveStream(0);
  const callbackFailure = new Error("timeout subscriber failed");
  const observing = submitAndObserveComposerOperation(descriptor(), { progress: () => undefined, timeout: () => { throw callbackFailure; } }).catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(0);
  const streamSignal = vi.mocked(api.fetchComposerOperationStream).mock.calls[0][2];
  await expect(vi.advanceTimersByTimeAsync(25000)).rejects.toBe(callbackFailure);
  expect(streamSignal.aborted).toBe(true);
  await vi.advanceTimersByTimeAsync(0);
  expect(await observing).toBeInstanceOf(ComposerObservationDetached);
  expect(findComposerOperationCustody(scope, sessionId).foreground?.operationId).toBe(operationId);
  expect(api.fetchComposerOperation).not.toHaveBeenCalled();
  expect(api.cancelComposerOperation).not.toHaveBeenCalled(); expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
});
it("absolute subscription lifetime falls back to durable GET despite continual heartbeats", async () => {
  liveStream(300000);
  const observing = submitAndObserveComposerOperation(descriptor());
  await vi.advanceTimersByTimeAsync(119999); expect(api.fetchComposerOperation).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1); expect((await observing).message.content).toBe("done");
  expect(api.fetchComposerOperation).toHaveBeenCalledTimes(1); expect(api.cancelComposerOperation).not.toHaveBeenCalled(); expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
});
it("header wait is bounded and falls back to GET without cancel or replay", async () => {
  vi.mocked(api.fetchComposerOperationStream).mockImplementation((_sid, _id, signal) => new Promise((_resolve, reject) => { signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true }); }));
  const observing = submitAndObserveComposerOperation(descriptor());
  await vi.advanceTimersByTimeAsync(14999); expect(api.fetchComposerOperation).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1); expect((await observing).message.content).toBe("done");
  expect(api.cancelComposerOperation).not.toHaveBeenCalled(); expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
});

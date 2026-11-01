import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("./client", () => ({
  fetchComposerOperationStream: vi.fn(), fetchComposerOperation: vi.fn(), cancelComposerOperation: vi.fn(),
  apiErrorFromBody: vi.fn(), isDefinitiveComposerRefusal: vi.fn(() => false),
}));
import * as api from "./client";
import { RevisionGate, observeComposerOperation } from "./composerOperationObserver";
import { ComposerSseDecoder, decodeComposerOperationFrame } from "./composerOperationDecoder";
import { authenticateComposerCustody, findComposerOperationCustody, purgeComposerCustody, requestComposerStop, attachComposerObserver, acquireComposerOperationCustody } from "@/stores/composerOperationCustody";
import type { ComposerOperationStatus, ComposerOperationSnapshot, OperationCustody, SubmittedCustody } from "@/types/composerOperations";

const sid = "11111111-1111-4111-8111-111111111111", id = "22222222-2222-4222-8222-222222222222";
const scope = { principalId: "principal", authProvider: "local" };
const now = () => new Date().toISOString();
function status(revision: number, extra: Partial<ComposerOperationStatus> = {}): ComposerOperationStatus {
  return { operation_id: id, kind: "compose_message", status: "running", phase: "executing", status_revision: revision, created_at: now(), observed_at: now(), poll_after_ms: 1000, cancel_requested: false, segment: { sequence: 1, generation: 1, state: "queued", deadline_at: null }, ...extra };
}
const completed: ComposerOperationSnapshot = {
  ...status(9, { status: "completed", phase: "settled", segment: null }),
  result: { message: { id: "44444444-4444-4444-8444-444444444444", session_id: sid, role: "assistant", content: "done", tool_calls: null, created_at: now() }, state: null, proposals: [] } as unknown as ComposerOperationSnapshot["result"],
  error: null,
};
function frame(event: string, sequence: number, payload?: unknown, eventId?: number): string {
  const data = { session_id: sid, operation_id: id, sequence, event, ...(payload === undefined ? {} : { payload }) };
  return `${eventId === undefined ? "" : `id: ${eventId}\n`}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}
function sse(...frames: string[]): Response {
  return new Response(new ReadableStream({ start(controller) { for (const item of frames) controller.enqueue(new TextEncoder().encode(item)); controller.close(); } }), { headers: { "Content-Type": "text/event-stream" } });
}
function observer(): OperationCustody {
  const submitted: SubmittedCustody = { mode: "submitted", scope, sessionId: sid, operationId: "55555555-5555-4555-8555-555555555555", kind: "compose_message", createdAt: Date.now(), body: { operation_id: "55555555-5555-4555-8555-555555555555", content: "x", state_id: null } };
  return attachComposerObserver(acquireComposerOperationCustody(submitted), id, "compose_message", false);
}
beforeEach(() => {
  vi.resetAllMocks(); purgeComposerCustody(); sessionStorage.clear(); authenticateComposerCustody(scope);
  vi.mocked(api.isDefinitiveComposerRefusal).mockReturnValue(false);
  vi.mocked(api.fetchComposerOperation).mockResolvedValue(completed);
});
afterEach(() => { vi.useRealTimers(); purgeComposerCustody(); });

describe("revision rules (F-3)", () => {
  it("accepts a greater revision, ignores equal and smaller, and refuses an equal-revision conflict", () => {
    const gate = new RevisionGate();
    expect(gate.accept(status(3))).toBe(true);
    expect(gate.accept(status(3))).toBe(false);
    expect(gate.accept(status(2))).toBe(false);
    expect(() => gate.accept(status(3, { phase: "reconciling" }))).toThrow("protocol error");
    expect(gate.accept(status(4))).toBe(true);
  });
});

describe("stream decoders (F-1, F-2)", () => {
  it("admits id: and binds it to the status revision", () => {
    const [raw] = new ComposerSseDecoder().push(new TextEncoder().encode(frame("status", 0, status(5), 5)));
    expect(decodeComposerOperationFrame(raw, sid, id)).toMatchObject({ event: "status", id: "5" });
    const [wrong] = new ComposerSseDecoder().push(new TextEncoder().encode(frame("status", 0, status(5), 6)));
    expect(() => decodeComposerOperationFrame(wrong, sid, id)).toThrow("revision");
    const [heartbeatWithId] = new ComposerSseDecoder().push(new TextEncoder().encode(frame("heartbeat", 1, undefined, 5)));
    expect(() => decodeComposerOperationFrame(heartbeatWithId, sid, id)).toThrow();
  });
});

describe("observation (F-4..F-8)", () => {
  it("reconnects at once after a reconnect frame and resumes from the last accepted revision", async () => {
    vi.mocked(api.fetchComposerOperationStream)
      .mockResolvedValueOnce(sse(frame("status", 0, status(4), 4), frame("reconnect", 1)))
      .mockResolvedValueOnce(sse(frame("status", 0, status(4), 4), frame("terminal", 1, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    const seen: number[] = [];
    const result = await observeComposerOperation(observer(), { status: (value) => seen.push(value.status_revision) });
    expect(result.message.content).toBe("done");
    const calls = vi.mocked(api.fetchComposerOperationStream).mock.calls;
    expect(calls.map((call) => call[3])).toEqual([null, 4]);
    expect(seen).toEqual([4, 9]);
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
  });
  it("falls back to the durable GET on stream capacity and does not detach", async () => {
    vi.useFakeTimers();
    vi.mocked(api.fetchComposerOperationStream).mockResolvedValueOnce(new Response("busy", { status: 503 }));
    vi.mocked(api.fetchComposerOperation).mockResolvedValueOnce({ ...status(4), result: null, error: null }).mockResolvedValue(completed);
    vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(sse(frame("terminal", 0, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    const observing = observeComposerOperation(observer());
    await vi.advanceTimersByTimeAsync(5_100);
    await expect(observing).resolves.toMatchObject({ message: { content: "done" } });
    expect(api.fetchComposerOperation).toHaveBeenCalledTimes(2);
  });
  it("closes an idle stream after 25 s, not 15 s", async () => {
    vi.useFakeTimers();
    let aborted = 0;
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (_s, _o, signal) => {
      signal.addEventListener("abort", () => { aborted += 1; });
      return new Response(new ReadableStream({ start(controller) { signal.addEventListener("abort", () => controller.error(new Error("aborted"))); } }), { headers: { "Content-Type": "text/event-stream" } });
    });
    const controller = new AbortController();
    const observing = observeComposerOperation(observer(), { signal: controller.signal }).catch(() => undefined);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(aborted).toBe(0);
    await vi.advanceTimersByTimeAsync(5_100);
    expect(aborted).toBe(1);
    controller.abort(); await vi.advanceTimersByTimeAsync(10_000); await observing;
  });
  it("sends a pending Stop through cancel before reading the stream", async () => {
    const descriptor = observer();
    requestComposerStop(descriptor);
    vi.mocked(api.cancelComposerOperation).mockResolvedValue({ ...status(5, { cancel_requested: true }), result: null, error: null });
    vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(sse(frame("terminal", 0, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    await observeComposerOperation(descriptor);
    expect(vi.mocked(api.cancelComposerOperation).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(api.fetchComposerOperationStream).mock.invocationCallOrder[0]);
  });
  it("turns a passed segment deadline into a GET, never a detach or resubmission", async () => {
    vi.useFakeTimers();
    const past = new Date(Date.now() + 1_000).toISOString();
    const running = status(4, { observed_at: new Date().toISOString(), segment: { sequence: 1, generation: 1, state: "running", deadline_at: past } });
    vi.mocked(api.fetchComposerOperationStream).mockImplementationOnce(async (_s, _o, signal) => new Response(new ReadableStream({ start(controller) {
      controller.enqueue(new TextEncoder().encode(frame("status", 0, running, 4)));
      signal.addEventListener("abort", () => controller.error(new Error("aborted")));
    } }), { headers: { "Content-Type": "text/event-stream" } }));
    const observing = observeComposerOperation(observer());
    await vi.advanceTimersByTimeAsync(1_200);
    await expect(observing).resolves.toMatchObject({ message: { content: "done" } });
    expect(api.fetchComposerOperation).toHaveBeenCalledTimes(1);
    expect(api.fetchComposerOperationStream).toHaveBeenCalledTimes(1);
  });
});

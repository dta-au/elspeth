import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("./client", () => ({
  fetchComposerOperationStreamV2: vi.fn(), fetchComposerOperationAnyVersion: vi.fn(), cancelComposerOperationV2: vi.fn(),
  apiErrorFromBody: vi.fn(), isDefinitiveComposerRefusal: vi.fn(() => false),
}));
import * as api from "./client";
import { RevisionGate, observeComposerOperationV2 } from "./composerOperationObserverV2";
import { ComposerSseDecoder } from "./composerOperationDecoder";
import { ComposerSseDecoderV2, decodeComposerOperationFrameV2 } from "./composerOperationDecoderV2";
import { authenticateComposerCustody, findComposerOperationCustody, purgeComposerCustody, requestComposerStop, attachComposerObserver, acquireComposerOperationCustody } from "@/stores/composerOperationCustody";
import type { ComposerOperationStatusV2, ComposerOperationSnapshotV2, OperationCustody, SubmittedCustody } from "@/types/composerOperations";

const sid = "11111111-1111-4111-8111-111111111111", id = "22222222-2222-4222-8222-222222222222";
const scope = { principalId: "principal", authProvider: "local" };
const now = () => new Date().toISOString();
function status(revision: number, extra: Partial<ComposerOperationStatusV2> = {}): ComposerOperationStatusV2 {
  return { protocol_version: 2, operation_id: id, kind: "compose_message", status: "running", phase: "executing", status_revision: revision, created_at: now(), observed_at: now(), poll_after_ms: 1000, cancel_requested: false, segment: { sequence: 1, generation: 1, state: "queued", deadline_at: null }, ...extra };
}
const completed: ComposerOperationSnapshotV2 = {
  ...status(9, { status: "completed", phase: "settled", segment: null }),
  result: { message: { id: "44444444-4444-4444-8444-444444444444", session_id: sid, role: "assistant", content: "done", tool_calls: null, created_at: now() }, state: null, proposals: [] } as unknown as ComposerOperationSnapshotV2["result"],
  error: null,
};
function frame(event: string, sequence: number, payload?: unknown, eventId?: number): string {
  const data = { schema_version: "composer-operation-stream.v2", session_id: sid, operation_id: id, sequence, event, ...(payload === undefined ? {} : { payload }) };
  return `${eventId === undefined ? "" : `id: ${eventId}\n`}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}
function sse(...frames: string[]): Response {
  return new Response(new ReadableStream({ start(controller) { for (const item of frames) controller.enqueue(new TextEncoder().encode(item)); controller.close(); } }), { headers: { "Content-Type": "text/event-stream" } });
}
function observer(): OperationCustody {
  const submitted: SubmittedCustody = { mode: "submitted", scope, sessionId: sid, operationId: "55555555-5555-4555-8555-555555555555", kind: "compose_message", createdAt: Date.now(), body: { operation_id: "55555555-5555-4555-8555-555555555555", content: "x", state_id: null } };
  return attachComposerObserver(acquireComposerOperationCustody(submitted), id, "compose_message", false, 2);
}
beforeEach(() => {
  vi.resetAllMocks(); purgeComposerCustody(); sessionStorage.clear(); authenticateComposerCustody(scope);
  vi.mocked(api.isDefinitiveComposerRefusal).mockReturnValue(false);
  vi.mocked(api.fetchComposerOperationAnyVersion).mockResolvedValue({ protocolVersion: 2, snapshot: completed });
});
afterEach(() => { vi.useRealTimers(); purgeComposerCustody(); });

describe("v2 revision rules (F-3)", () => {
  it("accepts a greater revision, ignores equal and smaller, and refuses an equal-revision conflict", () => {
    const gate = new RevisionGate();
    expect(gate.accept(status(3))).toBe(true);
    expect(gate.accept(status(3))).toBe(false);
    expect(gate.accept(status(2))).toBe(false);
    expect(() => gate.accept(status(3, { phase: "reconciling" }))).toThrow("protocol error");
    expect(gate.accept(status(4))).toBe(true);
  });
});

describe("v2 decoders (F-1, F-2)", () => {
  it("admits id: in v2 and binds it to the status revision", () => {
    const [raw] = new ComposerSseDecoderV2().push(new TextEncoder().encode(frame("status", 0, status(5), 5)));
    expect(decodeComposerOperationFrameV2(raw, sid, id)).toMatchObject({ event: "status", id: "5" });
    const [wrong] = new ComposerSseDecoderV2().push(new TextEncoder().encode(frame("status", 0, status(5), 6)));
    expect(() => decodeComposerOperationFrameV2(wrong, sid, id)).toThrow("revision");
    const [heartbeatWithId] = new ComposerSseDecoderV2().push(new TextEncoder().encode(frame("heartbeat", 1, undefined, 5)));
    expect(() => decodeComposerOperationFrameV2(heartbeatWithId, sid, id)).toThrow();
  });
  it("keeps the v1 decoder refusing id: (a v1 reader must never receive one)", () => {
    expect(() => new ComposerSseDecoder().push(new TextEncoder().encode(frame("status", 0, status(5), 5)))).toThrow("Unknown or duplicate SSE field");
  });
});

describe("v2 observation (F-4..F-8)", () => {
  it("reconnects at once after a reconnect frame and resumes from the last accepted revision", async () => {
    vi.mocked(api.fetchComposerOperationStreamV2)
      .mockResolvedValueOnce(sse(frame("status", 0, status(4), 4), frame("reconnect", 1)))
      .mockResolvedValueOnce(sse(frame("status", 0, status(4), 4), frame("terminal", 1, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    const seen: number[] = [];
    const result = await observeComposerOperationV2(observer(), { status: (value) => seen.push(value.status_revision) });
    expect(result.message.content).toBe("done");
    const calls = vi.mocked(api.fetchComposerOperationStreamV2).mock.calls;
    expect(calls.map((call) => call[3])).toEqual([null, 4]);
    expect(seen).toEqual([4, 9]);
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
  });
  it("falls back to the durable GET on stream capacity and does not detach", async () => {
    vi.useFakeTimers();
    vi.mocked(api.fetchComposerOperationStreamV2).mockResolvedValueOnce(new Response("busy", { status: 503 }));
    vi.mocked(api.fetchComposerOperationAnyVersion).mockResolvedValueOnce({ protocolVersion: 2, snapshot: { ...status(4), result: null, error: null } }).mockResolvedValue({ protocolVersion: 2, snapshot: completed });
    vi.mocked(api.fetchComposerOperationStreamV2).mockResolvedValue(sse(frame("terminal", 0, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    const observing = observeComposerOperationV2(observer());
    await vi.advanceTimersByTimeAsync(5_100);
    await expect(observing).resolves.toMatchObject({ message: { content: "done" } });
    expect(api.fetchComposerOperationAnyVersion).toHaveBeenCalledTimes(2);
  });
  it("closes an idle v2 stream after 25 s, not 15 s", async () => {
    vi.useFakeTimers();
    let aborted = 0;
    vi.mocked(api.fetchComposerOperationStreamV2).mockImplementation(async (_s, _o, signal) => {
      signal.addEventListener("abort", () => { aborted += 1; });
      return new Response(new ReadableStream({ start(controller) { signal.addEventListener("abort", () => controller.error(new Error("aborted"))); } }), { headers: { "Content-Type": "text/event-stream" } });
    });
    const controller = new AbortController();
    const observing = observeComposerOperationV2(observer(), { signal: controller.signal }).catch(() => undefined);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(aborted).toBe(0);
    await vi.advanceTimersByTimeAsync(5_100);
    expect(aborted).toBe(1);
    controller.abort(); await vi.advanceTimersByTimeAsync(10_000); await observing;
  });
  it("sends a pending Stop through the v2 cancel before reading the stream", async () => {
    const descriptor = observer();
    requestComposerStop(descriptor);
    vi.mocked(api.cancelComposerOperationV2).mockResolvedValue({ ...status(5, { cancel_requested: true }), result: null, error: null });
    vi.mocked(api.fetchComposerOperationStreamV2).mockResolvedValue(sse(frame("terminal", 0, { status: "completed", phase: "settled", status_revision: 9 }, 9)));
    await observeComposerOperationV2(descriptor);
    expect(vi.mocked(api.cancelComposerOperationV2).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(api.fetchComposerOperationStreamV2).mock.invocationCallOrder[0]);
  });
  it("turns a passed segment deadline into a GET, never a detach or resubmission", async () => {
    vi.useFakeTimers();
    const past = new Date(Date.now() + 1_000).toISOString();
    const running = status(4, { observed_at: new Date().toISOString(), segment: { sequence: 1, generation: 1, state: "running", deadline_at: past } });
    vi.mocked(api.fetchComposerOperationStreamV2).mockImplementationOnce(async (_s, _o, signal) => new Response(new ReadableStream({ start(controller) {
      controller.enqueue(new TextEncoder().encode(frame("status", 0, running, 4)));
      signal.addEventListener("abort", () => controller.error(new Error("aborted")));
    } }), { headers: { "Content-Type": "text/event-stream" } }));
    const observing = observeComposerOperationV2(observer());
    await vi.advanceTimersByTimeAsync(1_200);
    await expect(observing).resolves.toMatchObject({ message: { content: "done" } });
    expect(api.fetchComposerOperationAnyVersion).toHaveBeenCalledTimes(1);
    expect(api.fetchComposerOperationStreamV2).toHaveBeenCalledTimes(1);
  });
});

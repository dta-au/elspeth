import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchActiveComposerOperation, fetchComposerOperationAnyVersion, fetchComposerOperationStreamV2, submitComposerOperation } from "./client";
import { setComposerAdmissionVersion } from "@/config/composerProtocol";
import type { SubmittedCustody } from "@/types/composerOperations";

const sid = "11111111-1111-4111-8111-111111111111", id = "22222222-2222-4222-8222-222222222222";
const scope = { principalId: "principal", authProvider: "local" };
function descriptor(extra: Partial<SubmittedCustody> = {}): SubmittedCustody {
  return { mode: "submitted", scope, sessionId: sid, operationId: id, kind: "compose_message", createdAt: Date.now(), body: { operation_id: id, content: "hello", state_id: null }, ...extra } as SubmittedCustody;
}
const now = new Date().toISOString();
const snapshotV2 = { protocol_version: 2, operation_id: id, kind: "compose_message", status: "queued", phase: "queued", status_revision: 0, created_at: now, observed_at: now, poll_after_ms: 1000, cancel_requested: false, segment: { sequence: 1, generation: 1, state: "queued", deadline_at: null }, result: null, error: null };
const ackV1 = { operation_id: id, kind: "compose_message", status: "queued", poll_after_ms: 1000 };
beforeEach(() => { localStorage.setItem("auth_token", "operation-test-token"); });
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); setComposerAdmissionVersion(1); });

describe("Composer-Operation-Version on the wire (F-10)", () => {
  it("sends no version header for a v1 submission and records echo 1", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(ackV1), { status: 202, headers: { "Composer-Operation-Version": "1" } }));
    vi.stubGlobal("fetch", fetch);
    const ack = await submitComposerOperation(descriptor());
    expect(new Headers((fetch.mock.calls[0][1] as RequestInit).headers).get("Composer-Operation-Version")).toBeNull();
    expect(ack.protocolVersion).toBe(1);
  });
  it("sends version 2 when custody requests it and decodes the v2 acknowledgement by its echo", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(snapshotV2), { status: 202, headers: { "Composer-Operation-Version": "2" } }));
    vi.stubGlobal("fetch", fetch);
    const ack = await submitComposerOperation(descriptor({ protocolVersion: 2 }));
    expect(new Headers((fetch.mock.calls[0][1] as RequestInit).headers).get("Composer-Operation-Version")).toBe("2");
    expect(ack).toEqual({ operation_id: id, kind: "compose_message", status: "queued", poll_after_ms: 1000, protocolVersion: 2 });
  });
  it("records version 1 when an older server ignores the request and sends no echo", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(ackV1), { status: 202 })));
    expect((await submitComposerOperation(descriptor({ protocolVersion: 2 }))).protocolVersion).toBe(1);
  });
  it("reads either lifecycle with one v2-capable GET, dispatching on the echo", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...snapshotV2 }), { headers: { "Composer-Operation-Version": "2" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...ackV1, cancel_requested: false, deadline_at: now, deadline_remaining_ms: 5, result: null, error: null }), { headers: { "Composer-Operation-Version": "1" } }));
    vi.stubGlobal("fetch", fetch);
    const v2 = await fetchComposerOperationAnyVersion(sid, id);
    const v1 = await fetchComposerOperationAnyVersion(sid, id);
    expect("protocolVersion" in v2 && v2.protocolVersion).toBe(2);
    expect("protocolVersion" in v1 && v1.protocolVersion).toBe(1);
    for (const [, init] of fetch.mock.calls as [string, RequestInit][]) expect(new Headers(init.headers).get("Composer-Operation-Version")).toBe("2");
  });
  it("resumes a v2 stream with Last-Event-ID and the reader version", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("", { headers: { "Content-Type": "text/event-stream" } }));
    vi.stubGlobal("fetch", fetch);
    await fetchComposerOperationStreamV2(sid, id, new AbortController().signal, 7);
    const headers = new Headers((fetch.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get("Last-Event-ID")).toBe("7");
    expect(headers.get("Composer-Operation-Version")).toBe("2");
    expect(headers.get("Authorization")).toBe("Bearer operation-test-token");
  });
  it("finds the live operation or none", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ operation_id: id, kind: "compose_message", lifecycle_version: 2, status: "running" }))));
    expect(await fetchActiveComposerOperation(sid)).toBeNull();
    expect(await fetchActiveComposerOperation(sid)).toEqual({ operation_id: id, kind: "compose_message", lifecycle_version: 2, status: "running" });
  });
});

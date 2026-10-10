import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cancelComposerOperation, fetchComposerOperation, fetchComposerOperationStream, submitComposerOperation } from "./client";
import type { SubmittedCustody } from "@/types/composerOperations";
const sid = "11111111-1111-4111-8111-111111111111", id = "22222222-2222-4222-8222-222222222222";
const descriptor: SubmittedCustody = { mode: "submitted", scope: { principalId: "principal", authProvider: "local" }, sessionId: sid, operationId: id, kind: "compose_message", createdAt: Date.now(), body: { operation_id: id, content: "hello", state_id: null } };
beforeEach(() => { localStorage.setItem("auth_token", "operation-test-token"); });
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });
describe("authenticated composer operation wire", () => {
  it("sends bearer only in headers and preserves the exact admission body", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ operation_id: id, kind: "compose_message", status: "queued", poll_after_ms: 1000 }), { status: 202 })); vi.stubGlobal("fetch", fetch);
    await submitComposerOperation(descriptor);
    const [url, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/sessions/${sid}/messages`); expect(url).not.toContain("operation-test-token");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer operation-test-token"); expect(JSON.parse(init.body as string)).toEqual(descriptor.body); expect(init.signal).toBeInstanceOf(AbortSignal);
  });
  it("rejects a synchronous 200 as admission instead of trusting an answer", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 200 })));
    await expect(submitComposerOperation(descriptor)).rejects.toThrow("202");
  });
  it("opens the stream with authenticated fetch, no query credentials or replay cursor", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("", { headers: { "Content-Type": "text/event-stream" } })); vi.stubGlobal("fetch", fetch);
    const signal = new AbortController().signal; await fetchComposerOperationStream(sid, id, signal);
    const [url, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/sessions/${sid}/operations/${id}/stream`); expect(new Headers(init.headers).get("Authorization")).toBe("Bearer operation-test-token"); expect(new Headers(init.headers).get("Last-Event-ID")).toBeNull(); expect(init.signal).toBe(signal);
  });
  it("decodes exact session versus operation404 bodies and rejects other404shapes", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Session not found" }), { status: 404 })).mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Operation not found" }), { status: 404 })).mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Other not found" }), { status: 404 })); vi.stubGlobal("fetch", fetch);
    await expect(fetchComposerOperation(sid, id)).resolves.toEqual({ kind: "session_missing" }); await expect(fetchComposerOperation(sid, id)).resolves.toEqual({ kind: "operation_missing" }); await expect(fetchComposerOperation(sid, id)).rejects.toMatchObject({ status: 404 });
  });
});

it("cancel distinguishes exact ownership404s without treating missing as settlement", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Session not found" }), { status: 404 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Operation not found" }), { status: 404 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Session not found", extra: true }), { status: 404 })));
  await expect(cancelComposerOperation(sid, id)).resolves.toEqual({ kind: "session_missing" });
  await expect(cancelComposerOperation(sid, id)).resolves.toEqual({ kind: "operation_missing" });
  await expect(cancelComposerOperation(sid, id)).rejects.toMatchObject({ status: 404 });
});

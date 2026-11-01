import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiErrorFromBody, cancelComposerOperation, fetchActiveComposerOperation, fetchComposerOperation, fetchComposerOperationFailures, fetchComposerOperationStream, submitComposerOperation } from "./client";
import { liveSnapshot } from "@/test/composerOperationWire";
import type { SubmittedCustody } from "@/types/composerOperations";
const sid = "11111111-1111-4111-8111-111111111111", id = "22222222-2222-4222-8222-222222222222";
const descriptor: SubmittedCustody = { mode: "submitted", scope: { principalId: "principal", authProvider: "local" }, sessionId: sid, operationId: id, kind: "compose_message", createdAt: Date.now(), body: { operation_id: id, content: "hello", state_id: null } };
beforeEach(() => { localStorage.setItem("auth_token", "operation-test-token"); });
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });
describe("authenticated composer operation wire", () => {
  it("authenticates and bounds every retained page and exact operation read", async () => {
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ session_id: sid, items: [], next_cursor: null })));
    vi.stubGlobal("fetch", fetch);
    await fetchComposerOperationFailures(sid, { cursor: "opaque+/=" });
    const signal = new AbortController().signal;
    await fetchComposerOperationFailures(sid, { operationId: id, signal });
    for (const [url, init] of fetch.mock.calls as [string, RequestInit][]) {
      expect(url).not.toContain("operation-test-token");
      expect(new Headers(init.headers).get("Authorization")).toBe("Bearer operation-test-token");
      expect(init.cache).toBe("no-store");
      expect(init.signal).toBeInstanceOf(AbortSignal);
    }
    expect(fetch.mock.calls[0][0]).toBe(`/api/sessions/${sid}/operations?cursor=opaque%2B%2F%3D`);
    expect(fetch.mock.calls[1][0]).toBe(`/api/sessions/${sid}/operations?operation_id=${id}`);
    expect(fetch.mock.calls[1][1].signal).toBe(signal);
  });
  it("extracts the canonical diagnostic from both public error envelope shapes", async () => {
    for (const body of [{ diagnostic_id: id, request_id: "request-reference", detail: "Failed" }, { detail: { diagnostic_id: id, request_id: "request-reference", detail: "Failed" } }]) {
      expect(await apiErrorFromBody(500, body)).toMatchObject({ diagnostic_id: id, request_id: "request-reference" });
    }
    expect((await apiErrorFromBody(500, { diagnostic_id: "private malformed value", detail: "Failed" })).diagnostic_id).toBeUndefined();
  });
  it("sends bearer only in headers and preserves the exact admission body", async () => {
    const admitted = liveSnapshot(id, { status: "queued", phase: "queued" });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(admitted), { status: 202 })); vi.stubGlobal("fetch", fetch);
    expect(await submitComposerOperation(descriptor)).toEqual(admitted);
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
    const signal = new AbortController().signal; await fetchComposerOperationStream(sid, id, signal, null);
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

describe("one composer lifecycle on the wire", () => {
  it("resumes a stream with Last-Event-ID", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("", { headers: { "Content-Type": "text/event-stream" } }));
    vi.stubGlobal("fetch", fetch);
    await fetchComposerOperationStream(sid, id, new AbortController().signal, 7);
    const headers = new Headers((fetch.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get("Last-Event-ID")).toBe("7");
    expect(headers.get("Accept")).toBe("text/event-stream");
    expect(headers.get("Authorization")).toBe("Bearer operation-test-token");
  });
  it("sends no Composer-Operation-Version header on any composer route", async () => {
    const fetch = vi.fn().mockImplementation(async (url: string) => {
      if (url.endsWith("/messages")) return new Response(JSON.stringify(liveSnapshot(id)), { status: 202 });
      if (url.endsWith("/stream")) return new Response("", { headers: { "Content-Type": "text/event-stream" } });
      if (url.endsWith("/active")) return new Response(null, { status: 204 });
      return new Response(JSON.stringify(liveSnapshot(id)));
    });
    vi.stubGlobal("fetch", fetch);
    await submitComposerOperation(descriptor);
    await fetchComposerOperation(sid, id);
    await cancelComposerOperation(sid, id);
    await fetchComposerOperationStream(sid, id, new AbortController().signal, null);
    await fetchActiveComposerOperation(sid);
    expect(fetch).toHaveBeenCalledTimes(5);
    for (const [, init] of fetch.mock.calls as [string, RequestInit][]) {
      expect(new Headers(init.headers).get("Authorization")).toBe("Bearer operation-test-token");
      expect(new Headers(init.headers).has("Composer-Operation-Version")).toBe(false);
    }
  });
  it("finds the live operation or none", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ operation_id: id, kind: "compose_message", status: "running" }))));
    expect(await fetchActiveComposerOperation(sid)).toBeNull();
    expect(await fetchActiveComposerOperation(sid)).toEqual({ operation_id: id, kind: "compose_message", status: "running" });
  });
  it("refuses a retired lifecycle_version on the active operation", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ operation_id: id, kind: "compose_message", lifecycle_version: 2, status: "running" }))));
    await expect(fetchActiveComposerOperation(sid)).rejects.toThrow("Unexpected or missing field");
  });
});

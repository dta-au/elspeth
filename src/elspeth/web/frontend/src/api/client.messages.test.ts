import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MockInstance } from "vitest";
import type { ChatMessage } from "@/types/index";
import { fetchMessages } from "./client";

function message(sequence: number): ChatMessage {
  return {
    id: `message-${sequence}`,
    session_id: "session-1",
    role: sequence % 2 === 0 ? "assistant" : "user",
    content: `Conversation row ${sequence}`,
    tool_calls: null,
    sequence_no: sequence,
    created_at: "2026-10-05T00:00:00Z",
  };
}

function history(count: number): ChatMessage[] {
  return Array.from({ length: count }, (_, index) => message(index + 1));
}

function response(rows: ChatMessage[]): Response {
  return new Response(JSON.stringify(rows), { status: 200 });
}

describe("complete conversational history retrieval", () => {
  let fetchSpy: MockInstance<typeof globalThis.fetch>;

  beforeEach(() => {
    localStorage.clear();
    fetchSpy = vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  it.each([0, 101, 501, 1001])("retrieves all %i rows using read-only pages", async (count) => {
    const rows = history(count);
    fetchSpy.mockImplementation(async (input) => {
      const url = new URL(String(input), "http://localhost");
      const offset = Number(url.searchParams.get("offset"));
      const limit = Number(url.searchParams.get("limit"));
      expect(url.pathname).toBe("/api/sessions/session-1/messages");
      expect(limit).toBe(500);
      expect(url.searchParams.has("include_tool_rows")).toBe(false);
      return response(rows.slice(offset, offset + limit));
    });

    expect(await fetchMessages("session-1")).toEqual(rows);
    expect(fetchSpy).toHaveBeenCalledTimes(Math.floor(count / 500) + 1);
    for (const [, init] of fetchSpy.mock.calls) {
      expect(init?.method ?? "GET").toBe("GET");
      expect(init?.body).toBeUndefined();
    }
  });

  it("fetches an empty terminal page for an exact page boundary", async () => {
    const rows = history(500);
    fetchSpy.mockResolvedValueOnce(response(rows)).mockResolvedValueOnce(response([]));

    expect(await fetchMessages("session-1")).toEqual(rows);
    expect(fetchSpy.mock.calls.map(([url]) => url)).toEqual([
      "/api/sessions/session-1/messages?limit=500&offset=0",
      "/api/sessions/session-1/messages?limit=500&offset=500",
    ]);
  });

  it("includes a final answer appended between page reads in server sequence order", async () => {
    const rows = history(500);
    const finalAnswer = { ...message(501), role: "assistant" as const, content: "Saved final answer" };
    fetchSpy.mockResolvedValueOnce(response(rows)).mockResolvedValueOnce(response([finalAnswer]));

    expect(await fetchMessages("session-1")).toEqual([...rows, finalAnswer]);
  });

  it("deduplicates repeated IDs without moving their server order", async () => {
    const rows = history(500);
    const refreshed = { ...rows[499], content: "Updated tool outcome projection" };
    const finalAnswer = message(501);
    fetchSpy.mockResolvedValueOnce(response(rows)).mockResolvedValueOnce(response([refreshed, finalAnswer]));

    expect(await fetchMessages("session-1")).toEqual([...rows.slice(0, 499), refreshed, finalAnswer]);
  });

  it("authenticates every page", async () => {
    localStorage.setItem("auth_token", "history-test-token");
    fetchSpy.mockResolvedValueOnce(response(history(500))).mockResolvedValueOnce(response([message(501)]));

    await fetchMessages("session-1");

    for (const [, init] of fetchSpy.mock.calls) {
      expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer history-test-token");
    }
  });

  it("rejects a repeated full page instead of looping indefinitely", async () => {
    const rows = history(500);
    fetchSpy.mockImplementation(async () => response(rows));

    await expect(fetchMessages("session-1")).rejects.toThrow("pagination did not advance");
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it.each([0, 1])("rejects HTTP failure on page %i without returning partial history", async (failedPage) => {
    if (failedPage === 1) fetchSpy.mockResolvedValueOnce(response(history(500)));
    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({ detail: "History unavailable" }), {
      status: 504,
      statusText: "Gateway Timeout",
    }));

    await expect(fetchMessages("session-1")).rejects.toMatchObject({
      status: 504,
      detail: "History unavailable",
    });
    expect(fetchSpy).toHaveBeenCalledTimes(failedPage + 1);
  });

  it("rejects a later network failure without resubmitting composer work", async () => {
    fetchSpy.mockResolvedValueOnce(response(history(500))).mockRejectedValueOnce(new TypeError("offline"));

    await expect(fetchMessages("session-1")).rejects.toThrow("offline");
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    for (const [url, init] of fetchSpy.mock.calls) {
      expect(String(url)).toContain("/messages?");
      expect(init?.method ?? "GET").toBe("GET");
    }
  });

  it("passes an optional cancellation signal to every page", async () => {
    const controller = new AbortController();
    fetchSpy.mockResolvedValueOnce(response(history(500))).mockResolvedValueOnce(response([message(501)]));

    await fetchMessages("session-1", controller.signal);

    for (const [, init] of fetchSpy.mock.calls) expect(init?.signal).toBe(controller.signal);
  });
});

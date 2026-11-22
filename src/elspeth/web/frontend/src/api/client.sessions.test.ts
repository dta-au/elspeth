import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchSessions } from "./client";
import type { Session } from "@/types/index";

// Pages the way GET /api/sessions does (web/sessions/routes/sessions.py
// `list_sessions`): newest first, `limit` defaulting to 50 and capped at
// 200, `offset` defaulting to 0. A caller that sends no paging parameters
// gets the 50 newest sessions and nothing else, exactly as in production.
const ROUTE_DEFAULT_LIMIT = 50;
const ROUTE_MAX_LIMIT = 200;

function makeSessions(count: number): Session[] {
  const start = Date.parse("2026-11-01T00:00:00Z");
  return Array.from({ length: count }, (_, index) => ({
    id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    title: `Session ${index}`,
    created_at: new Date(start + index * 1000).toISOString(),
    updated_at: new Date(start + index * 1000).toISOString(),
  }) as Session);
}

function sessionListRoute(sessions: readonly Session[]) {
  const newestFirst = [...sessions].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  const requests: string[] = [];
  return {
    requests,
    fetch: async (input: RequestInfo | URL): Promise<Response> => {
      const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      requests.push(raw);
      const url = new URL(raw, "http://localhost");
      if (url.pathname !== "/api/sessions") throw new Error(`unexpected request ${raw}`);
      const limit = Number(url.searchParams.get("limit") ?? ROUTE_DEFAULT_LIMIT);
      const offset = Number(url.searchParams.get("offset") ?? 0);
      if (!Number.isInteger(limit) || limit < 1 || limit > ROUTE_MAX_LIMIT || !Number.isInteger(offset) || offset < 0) {
        return { ok: false, status: 422, statusText: "Unprocessable Entity", headers: { get: () => null }, json: async () => ({ detail: "bad paging" }) } as unknown as Response;
      }
      const page = newestFirst.slice(offset, offset + limit);
      return { ok: true, status: 200, headers: { get: () => null }, json: async () => page } as unknown as Response;
    },
  };
}

describe("api/client fetchSessions", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  it("returns every session of an account that holds more than the route's default page", async () => {
    const sessions = makeSessions(ROUTE_DEFAULT_LIMIT + 1);
    fetchSpy.mockImplementation(sessionListRoute(sessions).fetch);

    const listed = await fetchSessions();

    expect(listed).toHaveLength(sessions.length);
    expect(listed[0].id).toBe(sessions[sessions.length - 1].id);
    expect(listed[listed.length - 1].id).toBe(sessions[0].id);
  });

  it("pages past the route's maximum page size without repeating or dropping a session", async () => {
    const sessions = makeSessions(ROUTE_MAX_LIMIT * 2 + 7);
    const route = sessionListRoute(sessions);
    fetchSpy.mockImplementation(route.fetch);

    const listed = await fetchSessions();

    expect(new Set(listed.map((session) => session.id)).size).toBe(sessions.length);
    expect(listed).toHaveLength(sessions.length);
    expect(route.requests).toEqual([
      "/api/sessions?include_archived=true&limit=200&offset=0",
      "/api/sessions?include_archived=true&limit=200&offset=200",
      "/api/sessions?include_archived=true&limit=200&offset=400",
    ]);
  });

  it("asks once for an account that fits in one page, and omits include_archived when asked to", async () => {
    const route = sessionListRoute(makeSessions(3));
    fetchSpy.mockImplementation(route.fetch);

    expect(await fetchSessions(false)).toHaveLength(3);
    expect(route.requests).toEqual(["/api/sessions?limit=200&offset=0"]);
  });

  it("keeps one copy of a session that a concurrent update shifts into the next page", async () => {
    const before = sessionListRoute(makeSessions(ROUTE_MAX_LIMIT + 1));
    const after = sessionListRoute(makeSessions(ROUTE_MAX_LIMIT + 2));
    fetchSpy.mockImplementationOnce(before.fetch).mockImplementation(after.fetch);

    const listed = await fetchSessions();

    expect(new Set(listed.map((session) => session.id)).size).toBe(listed.length);
  });
});

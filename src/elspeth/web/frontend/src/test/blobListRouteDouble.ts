// A fetch double for `GET /api/sessions/{id}/blobs` that pages the way the
// real route does (src/elspeth/web/blobs/routes.py `list_blobs`): newest
// first, `limit` defaulting to 50 and capped at 500, `offset` defaulting to 0.
// A caller that sends no query string gets the 50 newest rows and nothing
// else, exactly as in production.
import type { BlobMetadata } from "@/types/api";

export const ROUTE_DEFAULT_LIMIT = 50;
export const ROUTE_MAX_LIMIT = 500;

/** `count` ready text files; index 0 is the OLDEST, so the route returns it last. */
export function makeSessionBlobs(sessionId: string, count: number): BlobMetadata[] {
  const start = Date.parse("2026-11-01T00:00:00Z");
  return Array.from({ length: count }, (_, index): BlobMetadata => ({
    id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    session_id: sessionId,
    filename: `prompt-${index}.md`,
    mime_type: "text/plain",
    size_bytes: 12,
    content_hash: "a".repeat(64),
    created_at: new Date(start + index * 1000).toISOString(),
    created_by: "assistant",
    source_description: null,
    status: "ready",
    creation_modality: "verbatim",
    created_from_message_id: null,
    creating_model_identifier: null,
    creating_model_version: null,
    creating_provider: null,
    creating_composer_skill_hash: null,
    creating_arguments_hash: null,
  }));
}

export interface BlobListRouteDouble {
  fetch: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
  /** The request URLs received, in order. */
  requests: string[];
}

export function blobListRouteDouble(sessionId: string, blobs: readonly BlobMetadata[]): BlobListRouteDouble {
  const newestFirst = [...blobs].sort((a, b) => b.created_at.localeCompare(a.created_at));
  const requests: string[] = [];
  return {
    requests,
    fetch: async (input) => {
      const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      requests.push(raw);
      const url = new URL(raw, "http://localhost");
      if (url.pathname !== `/api/sessions/${sessionId}/blobs`) {
        throw new Error(`blobListRouteDouble: unexpected request ${raw}`);
      }
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

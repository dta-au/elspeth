import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { listBlobs, previewBlobContentSnippet, savePromptFile } from "./client";
import { blobListRouteDouble, makeSessionBlobs, ROUTE_DEFAULT_LIMIT, ROUTE_MAX_LIMIT } from "@/test/blobListRouteDouble";

describe("api/client blob preview", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  it("fetches the bounded preview endpoint instead of full blob content", async () => {
    fetchSpy.mockResolvedValue({
      ok: true,
      headers: {
        get: (name: string) => {
          if (name.toLowerCase() === "x-preview-truncated") return "true";
          if (name.toLowerCase() === "x-preview-limit") return "5000";
          return null;
        },
      },
      text: async () => "preview text",
    } as Response);

    const preview = await previewBlobContentSnippet("session-1", "blob-1", 5000);

    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/sessions/session-1/blobs/blob-1/preview?limit=5000",
      expect.objectContaining({
        headers: expect.any(Object),
      }),
    );
    expect(preview).toEqual({
      text: "preview text",
      truncated: true,
      limit: 5000,
    });
  });

  it("sends the editor's observed hash as a concurrency precondition", async () => {
    fetchSpy.mockResolvedValue({
      ok: true,
      json: async () => ({ changed: true, content_sha256: "b".repeat(64) }),
    } as Response);

    await savePromptFile("session-1", "blob-1", "line one\n  line two\n", "a".repeat(64), "first", "prompt_template");

    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/sessions/session-1/blobs/blob-1/prompt-text",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ content: "line one\n  line two\n", expected_sha256: "a".repeat(64), node_id: "first", field_path: "prompt_template" }),
      }),
    );
  });

  it("surfaces the server's prompt-save refusal code and message from the nested detail object", async () => {
    fetchSpy.mockResolvedValue({
      ok: false,
      status: 409,
      statusText: "Conflict",
      headers: { get: () => null },
      json: async () => ({
        detail: {
          error_code: "prompt_file_shared",
          message: "This file is also used by another prompt. Ask the assistant to give this step its own copy.",
        },
      }),
    } as unknown as Response);

    await expect(
      savePromptFile("session-1", "blob-1", "text", "a".repeat(64), "first", "prompt_template"),
    ).rejects.toMatchObject({
      status: 409,
      error_type: "prompt_file_shared",
      detail: "This file is also used by another prompt. Ask the assistant to give this step its own copy.",
    });
  });
});

describe("api/client listBlobs", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  it("returns every file of a session that holds more than the route's default page", async () => {
    const blobs = makeSessionBlobs("session-1", ROUTE_DEFAULT_LIMIT + 1);
    fetchSpy.mockImplementation(blobListRouteDouble("session-1", blobs).fetch);

    const listed = await listBlobs("session-1");

    expect(listed).toHaveLength(blobs.length);
    expect(listed[listed.length - 1].id).toBe(blobs[0].id);
    expect(listed[0].id).toBe(blobs[blobs.length - 1].id);
  });

  it("pages past the route's maximum page size without repeating or dropping a file", async () => {
    const blobs = makeSessionBlobs("session-1", ROUTE_MAX_LIMIT * 2 + 7);
    const route = blobListRouteDouble("session-1", blobs);
    fetchSpy.mockImplementation(route.fetch);

    const listed = await listBlobs("session-1");

    expect(new Set(listed.map((blob) => blob.id)).size).toBe(blobs.length);
    expect(listed).toHaveLength(blobs.length);
    expect(route.requests).toEqual([
      "/api/sessions/session-1/blobs?limit=500&offset=0",
      "/api/sessions/session-1/blobs?limit=500&offset=500",
      "/api/sessions/session-1/blobs?limit=500&offset=1000",
    ]);
  });

  it("asks once for a session that fits in one page", async () => {
    const blobs = makeSessionBlobs("session-1", 3);
    const route = blobListRouteDouble("session-1", blobs);
    fetchSpy.mockImplementation(route.fetch);

    expect(await listBlobs("session-1")).toHaveLength(3);
    expect(route.requests).toHaveLength(1);
  });

  it("keeps one copy of a file that a concurrent upload shifts into the next page", async () => {
    const blobs = makeSessionBlobs("session-1", ROUTE_MAX_LIMIT + 1);
    const before = blobListRouteDouble("session-1", blobs);
    const uploaded = makeSessionBlobs("session-1", ROUTE_MAX_LIMIT + 2);
    const after = blobListRouteDouble("session-1", uploaded);
    // First page is served before the upload, the second after it: every row
    // has moved down one place, so the second page repeats a row.
    fetchSpy.mockImplementationOnce(before.fetch).mockImplementation(after.fetch);

    const listed = await listBlobs("session-1");

    expect(new Set(listed.map((blob) => blob.id)).size).toBe(listed.length);
    expect(listed).toHaveLength(blobs.length);
  });

  it("stops when a full page adds no new file", async () => {
    const page = makeSessionBlobs("session-1", ROUTE_MAX_LIMIT);
    // A server that ignores `offset` returns the same full page forever.
    fetchSpy.mockResolvedValue({ ok: true, status: 200, headers: { get: () => null }, json: async () => page } as unknown as Response);

    expect(await listBlobs("session-1")).toHaveLength(ROUTE_MAX_LIMIT);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it("returns exactly the ceiling without reporting truncation", async () => {
    const blobs = makeSessionBlobs("session-1", 10_000);
    fetchSpy.mockImplementation(blobListRouteDouble("session-1", blobs).fetch);

    expect(await listBlobs("session-1")).toHaveLength(10_000);
  });

  it("rejects with the newest files attached when a session exceeds the ceiling", async () => {
    const blobs = makeSessionBlobs("session-1", 10_001);
    fetchSpy.mockImplementation(blobListRouteDouble("session-1", blobs).fetch);

    const failure = await listBlobs("session-1").then(
      () => null,
      (error: unknown) => error as { name: string; message: string; blobs: { id: string }[] },
    );

    expect(failure).not.toBeNull();
    expect(failure?.name).toBe("BlobListTruncatedError");
    expect(failure?.message).toMatch(/older files are not shown/);
    expect(failure?.blobs).toHaveLength(10_000);
    expect(failure?.blobs.map((blob) => blob.id)).not.toContain(blobs[0].id);
  });

  it("propagates a failed page instead of returning a partial list", async () => {
    const blobs = makeSessionBlobs("session-1", ROUTE_MAX_LIMIT + 1);
    const route = blobListRouteDouble("session-1", blobs);
    fetchSpy.mockImplementationOnce(route.fetch).mockResolvedValue({
      ok: false, status: 503, statusText: "Service Unavailable", headers: { get: () => null }, json: async () => ({ detail: "down" }),
    } as unknown as Response);

    await expect(listBlobs("session-1")).rejects.toMatchObject({ status: 503 });
  });
});

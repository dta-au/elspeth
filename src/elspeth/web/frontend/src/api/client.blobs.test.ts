import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { previewBlobContentSnippet, savePromptFile } from "./client";

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

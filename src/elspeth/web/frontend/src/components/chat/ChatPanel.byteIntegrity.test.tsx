import { createHash } from "node:crypto";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPanel } from "./ChatPanel";
import { useSessionStore } from "@/stores/sessionStore";
import { useInlineSourceStore } from "@/stores/inlineSourceStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { makeComposition } from "@/test/composerFixtures";
import { resetStore } from "@/test/store-helpers";
import type { BlobMetadata } from "@/types/api";

vi.mock("@/hooks/useComposer", () => ({
  useComposer: () => ({ sendMessage: vi.fn(), retryMessage: vi.fn(), isComposing: false, error: null }),
}));

const sessionId = "byte-integrity-session";
const blobId = "byte-integrity-blob";
const metadataUrl = `/api/sessions/${sessionId}/blobs/${blobId}`;
const encoder = new TextEncoder();
let fetchSpy: ReturnType<typeof vi.spyOn>;
let errorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  resetStore(useSessionStore);
  resetStore(useInlineSourceStore);
  resetStore(useInterpretationEventsStore);
  useSessionStore.setState({
    activeSessionId: sessionId,
    compositionStateLoaded: true,
    compositionState: makeComposition(1, {
      sources: { source: { plugin: "csv", options: { blob_ref: blobId } } },
    }),
  });
  // Only fetch is doubled: the HTTP client, real Response decoding, digest,
  // projection, retry state and rendered card all participate.
  fetchSpy = vi.spyOn(globalThis, "fetch");
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  useSessionStore.getState().reset();
  vi.restoreAllMocks();
});

function serveContent(originalText: string, deliveredText = () => originalText) {
  const originalBytes = encoder.encode(originalText);
  const metadata: BlobMetadata = {
    id: blobId,
    session_id: sessionId,
    filename: "neutral.csv",
    mime_type: "text/csv",
    size_bytes: originalBytes.byteLength,
    content_hash: createHash("sha256").update(originalBytes).digest("hex"),
    created_at: "2026-10-17T00:00:00Z",
    created_by: "assistant",
    source_description: null,
    status: "ready",
    creation_modality: "verbatim",
    created_from_message_id: "neutral-message",
    creating_model_identifier: null,
    creating_model_version: null,
    creating_provider: null,
    creating_composer_skill_hash: null,
    creating_arguments_hash: null,
  };
  fetchSpy.mockImplementation(async (input: RequestInfo | URL) => {
    if (input === metadataUrl) return Response.json(metadata);
    if (input === `${metadataUrl}/content`) {
      return new Response(encoder.encode(deliveredText()), {
        headers: { "Content-Type": "text/csv; charset=utf-8" },
      });
    }
    throw new Error(`Unexpected mocked request: ${String(input)}`);
  });
  return metadata;
}

describe("inline-source raw-byte integrity through the HTTP client", () => {
  it.each([
    ["UTF-8 BOM", "\uFEFFid,value\n1,ok\n", "id,value\n1,ok\n"],
    ["plain UTF-8", "id,value\n1,ok\n", "id,value\n1,ok\n"],
    ["non-ASCII UTF-8", "id,value\n1,café 東京\n", "id,value\n1,café 東京\n"],
  ])("verifies original %s bytes before decoding the preview", async (_kind, original, preview) => {
    const metadata = serveContent(original);
    render(<ChatPanel />);

    const card = await screen.findByRole("region", { name: "Source created from your message" });
    expect(card).toHaveTextContent("neutral.csv");
    expect(card).toHaveTextContent("1 row");
    const summary = useInlineSourceStore.getState().getSummaries(sessionId)[0];
    expect(summary.contentHash).toBe(metadata.content_hash);
    expect(summary.contentPreview).toBe(preview);
    expect(summary.rowCount).toBe(1);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(fetchSpy).toHaveBeenCalledWith(metadataUrl, expect.any(Object));
    expect(fetchSpy).toHaveBeenCalledWith(`${metadataUrl}/content`, expect.any(Object));
    expect(errorSpy).not.toHaveBeenCalled();
    expect(screen.queryByText("Could not load some source details.")).not.toBeInTheDocument();
  });

  it("refuses altered bytes even when metadata and content both return HTTP200", async () => {
    serveContent("id,value\n1,ok\n", () => "id,value\n1,changed\n");
    render(<ChatPanel />);

    await screen.findByRole("button", { name: "Retry source details" });
    expect(errorSpy).toHaveBeenCalledWith(
      "[inline-source] projection failed:",
      expect.objectContaining({ message: expect.stringContaining("content_hash mismatch") }),
    );
    expect(useInlineSourceStore.getState().getSummaries(sessionId)).toEqual([]);
    expect(screen.queryByRole("region", { name: "Source created from your message" })).not.toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it("refuses invalid UTF-8 instead of rendering replacement characters as verified text", async () => {
    const bytes = new Uint8Array([...encoder.encode("id,value\n1,"), 0xff, 0x0a]);
    const metadata = serveContent("");
    metadata.content_hash = createHash("sha256").update(bytes).digest("hex");
    metadata.size_bytes = bytes.byteLength;
    fetchSpy.mockImplementation(async (input: RequestInfo | URL) => {
      if (input === metadataUrl) return Response.json(metadata);
      if (input === `${metadataUrl}/content`) return new Response(bytes);
      throw new Error(`Unexpected mocked request: ${String(input)}`);
    });
    render(<ChatPanel />);

    await screen.findByRole("button", { name: "Retry source details" });
    expect(errorSpy).toHaveBeenCalledWith(
      "[inline-source] projection failed:",
      expect.objectContaining({ name: "TypeError" }),
    );
    expect(useInlineSourceStore.getState().getSummaries(sessionId)).toEqual([]);
    expect(screen.queryByRole("region", { name: "Source created from your message" })).not.toBeInTheDocument();
  });

  it("clears a failed projection after an ordinary retry returns intact BOM-prefixed bytes", async () => {
    const original = "\uFEFFid,value\n1,ok\n";
    let damaged = true;
    serveContent(original, () => damaged ? "id,value\n1,changed\n" : original);
    render(<ChatPanel />);

    const retry = await screen.findByRole("button", { name: "Retry source details" });
    expect(useInlineSourceStore.getState().getSummaries(sessionId)).toEqual([]);
    expect(errorSpy).toHaveBeenCalledWith(
      "[inline-source] projection failed:",
      expect.objectContaining({ message: expect.stringContaining("content_hash mismatch") }),
    );
    errorSpy.mockClear();
    damaged = false;
    fireEvent.click(retry);

    await screen.findByRole("region", { name: "Source created from your message" });
    expect(screen.queryByText("Could not load some source details.")).not.toBeInTheDocument();
    expect(useInlineSourceStore.getState().getSummaries(sessionId)[0].contentPreview).toBe("id,value\n1,ok\n");
    expect(errorSpy).not.toHaveBeenCalled();
    expect(fetchSpy).toHaveBeenCalledTimes(4);
  });
});

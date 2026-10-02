import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ChatPanel } from "./ChatPanel";
import { useSessionStore } from "@/stores/sessionStore";
import { useInlineSourceStore } from "@/stores/inlineSourceStore";
import { makeComposition } from "@/test/composerFixtures";
import { resetStore } from "@/test/store-helpers";
import * as api from "@/api/client";
import type { BlobMetadata } from "@/types/api";
import { createHash } from "node:crypto";

const sendMessage = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useComposer", () => ({
  useComposer: () => ({ sendMessage, retryMessage: vi.fn(), isComposing: false, error: null }),
}));
vi.mock("@/api/client", async (original) => ({
  ...await original<typeof api>(),
  getBlobMetadata: vi.fn(), previewBlobContent: vi.fn(), fetchSystemStatus: vi.fn(),
}));

const content = (id: string) => `url\nhttps://example.com/${id}`;
function metadata(id: string, uploaded = false): BlobMetadata {
  return {
    id, session_id: "s", filename: `${id}.csv`, mime_type: "text/csv",
    size_bytes: content(id).length,
    content_hash: createHash("sha256").update(content(id)).digest("hex"),
    created_at: "2026-09-22T00:00:00Z", created_by: uploaded ? "user" : "assistant",
    source_description: null, status: "ready", creation_modality: "llm_generated",
    created_from_message_id: uploaded ? null : "m", creating_model_identifier: null,
    creating_model_version: null, creating_provider: null,
    creating_composer_skill_hash: null, creating_arguments_hash: null,
  };
}
function composition(...ids: string[]) {
  return makeComposition(1, { sources: Object.fromEntries(ids.map((id) => [id, {
    plugin: "csv_file", options: { blob_ref: id },
  }])) });
}
beforeEach(() => {
  vi.resetAllMocks();
  Element.prototype.scrollIntoView = vi.fn();
  resetStore(useSessionStore);
  resetStore(useInlineSourceStore);
  useSessionStore.setState({
    activeSessionId: "s", messages: [],
    sessions: [{ id: "s", title: "Test", created_at: "2026-09-22T00:00:00Z", updated_at: "2026-09-22T00:00:00Z" }],
  });
  vi.mocked(api.getBlobMetadata).mockImplementation(async (_session, id) => metadata(id, id === "a_upload"));
  vi.mocked(api.previewBlobContent).mockImplementation(async (_session, id) => content(id));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("removes a replaced source immediately and recovers its details through a GET retry", async () => {
  useSessionStore.setState({ compositionState: composition("old") });
  render(<ChatPanel />);
  await screen.findByText("old.csv");
  vi.mocked(api.getBlobMetadata).mockRejectedValueOnce(new Error("Service unavailable"));
  vi.spyOn(console, "error").mockImplementation(() => {});
  act(() => { useSessionStore.setState({ compositionState: composition("replacement") }); });
  expect(screen.queryByText("old.csv")).not.toBeInTheDocument();
  const retry = await screen.findByRole("button", { name: "Retry source details" });
  expect(screen.queryByRole("button", { name: "Edit the list" })).not.toBeInTheDocument();
  fireEvent.click(retry);
  await screen.findByText("replacement.csv");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(useInlineSourceStore.getState().getSummaries("s").map((item) => item.blobId)).toEqual(["replacement"]);
});

it("projects all generated sources even when an uploaded source sorts first", async () => {
  vi.mocked(api.getBlobMetadata).mockImplementation(async (_session, id) => ({
    ...metadata(id, id === "a_upload"), filename: "shared.csv",
  }));
  useSessionStore.setState({ compositionState: composition("a_upload", "b_generated", "c_generated") });
  render(<ChatPanel />);
  await screen.findByText(/example.com\/b_generated/);
  await screen.findByText(/example.com\/c_generated/);
  expect(screen.queryByText("a_upload.csv")).not.toBeInTheDocument();
  expect(api.previewBlobContent).not.toHaveBeenCalledWith("s", "a_upload");
  expect(screen.getAllByRole("region", { name: /source created/i })).toHaveLength(2);
  fireEvent.click(screen.getAllByRole("button", { name: "Edit the list" })[1]);
  expect(sendMessage).toHaveBeenCalledWith(expect.stringContaining('"shared.csv"'));
  expect(sendMessage).toHaveBeenCalledWith(expect.stringContaining(content("c_generated")));
  expect(sendMessage).toHaveBeenCalledWith(expect.stringContaining("Pipeline source names: c_generated. Blob ID: c_generated."));
  expect(sendMessage).not.toHaveBeenCalledWith(expect.stringContaining(content("b_generated")));
});

it("rejects delayed source details from a previous session", async () => {
  let finish: (value: BlobMetadata) => void = () => {};
  vi.mocked(api.getBlobMetadata).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  useSessionStore.setState({ compositionState: composition("old") });
  render(<ChatPanel />);
  act(() => { useSessionStore.setState({ activeSessionId: "other", compositionState: composition("current") }); });
  await screen.findByText("current.csv");
  await act(async () => { finish(metadata("old")); });
  expect(screen.queryByText("old.csv")).not.toBeInTheDocument();
  expect(useInlineSourceStore.getState().getSummaries("s")).toEqual([]);
  expect(useInlineSourceStore.getState().getSummaries("other").map((item) => item.blobId)).toEqual(["current"]);
});

it("does not let a delayed old blob response republish a removed card", async () => {
  let finish: (value: BlobMetadata) => void = () => {};
  vi.mocked(api.getBlobMetadata).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  useSessionStore.setState({ compositionState: composition("old") });
  render(<ChatPanel />);
  act(() => { useSessionStore.setState({ compositionState: composition("replacement") }); });
  await screen.findByText("replacement.csv");
  await act(async () => { finish(metadata("old")); });
  expect(screen.queryByText("old.csv")).not.toBeInTheDocument();
  expect(useInlineSourceStore.getState().getSummaries("s").map((item) => item.blobId)).toEqual(["replacement"]);
});

it("keeps successful cards actionable during partial failure and orders retried cards by source name", async () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.mocked(api.getBlobMetadata).mockImplementation(async (_session, id) => {
    if (id === "a_generated") throw new Error("Service unavailable");
    return metadata(id);
  });
  useSessionStore.setState({ compositionState: composition("a_generated", "b_generated") });
  render(<ChatPanel />);
  await screen.findByText("b_generated.csv");
  expect(screen.getByRole("button", { name: "Edit the list" })).toBeEnabled();
  vi.mocked(api.getBlobMetadata).mockImplementation(async (_session, id) => metadata(id));
  fireEvent.click(await screen.findByRole("button", { name: "Retry source details" }));
  expect(screen.getByText("b_generated.csv")).toBeInTheDocument();
  await screen.findByText("a_generated.csv");
  const cards = screen.getAllByRole("region", { name: /source created/i });
  expect(cards[0]).toHaveTextContent("a_generated.csv");
  expect(cards[1]).toHaveTextContent("b_generated.csv");
});

it("clears source cards when all sources are removed", async () => {
  useSessionStore.setState({ compositionState: composition("old") });
  render(<ChatPanel />);
  await screen.findByText("old.csv");
  act(() => { useSessionStore.setState({ compositionState: composition() }); });
  await waitFor(() => expect(screen.queryByRole("region", { name: /source created/i })).not.toBeInTheDocument());
  expect(useInlineSourceStore.getState().getSummaries("s")).toEqual([]);
});

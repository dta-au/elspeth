import { act, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, onTestFinished, vi } from "vitest";
import * as api from "@/api/client";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import type { BlobMetadata } from "@/types/api";
import { BlobFileName } from "./BlobFileName";

beforeEach(() => {
  vi.clearAllMocks();
  useBlobStore.getState().reset();
  useSessionStore.setState({ activeSessionId: "session-1" });
});

it("shows only the filename and keeps identifiers in attributes", () => {
  const id = "f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0";
  useBlobStore.setState({ activeSessionId: "session-1", blobs: [{ id, filename: "prompt.md", content_hash: "a".repeat(64) } as BlobMetadata] });
  render(<BlobFileName blobId={id} sha256={"a".repeat(64)} />);
  const name = screen.getByText(/prompt\.md/);
  expect(name).toHaveAttribute("data-blob-id", id);
  expect(name).toHaveAttribute("title", expect.stringContaining(id));
  expect(name.textContent).not.toMatch(/f976fd8b|inline_content|a{64}/);
  expect(screen.queryByText(/changed since attached/)).not.toBeInTheDocument();
});

it("distinguishes loading, missing, and another session", () => {
  useBlobStore.setState({ activeSessionId: "session-1", isLoading: true });
  const { rerender } = render(<BlobFileName blobId="missing" />);
  expect(screen.getByText("Loading file name…")).toBeInTheDocument();
  act(() => useBlobStore.setState({ isLoading: false }));
  expect(screen.getByText("File not in this session")).toBeInTheDocument();
  act(() => useBlobStore.setState({ activeSessionId: "session-2", blobs: [{ id: "missing", filename: "wrong.md" } as BlobMetadata] }));
  rerender(<BlobFileName blobId="missing" />);
  expect(screen.getByText("Loading file name…")).toBeInTheDocument();
  expect(screen.queryByText("wrong.md")).not.toBeInTheDocument();
});

it("uses the unavailable name as the default fallback after a failed list read", async () => {
  const list = vi.spyOn(api, "listBlobs").mockRejectedValueOnce(new Error("Network error"));
  onTestFinished(() => { list.mockRestore(); });
  await useBlobStore.getState().loadBlobs("session-1");

  render(<BlobFileName blobId="missing" />);

  expect(screen.getByText("File name unavailable")).toBeInTheDocument();
  expect(screen.queryByText("File not in this session")).not.toBeInTheDocument();
});

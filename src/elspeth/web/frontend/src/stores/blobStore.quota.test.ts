import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/client";
import { useBlobStore } from "./blobStore";

vi.mock("@/api/client", () => ({ uploadBlob: vi.fn() }));

describe("upload refusal copy", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useBlobStore.getState().reset();
    useBlobStore.getState().activateSession("session");
  });

  it("shows measured identity usage and the tighter storage limit", async () => {
    vi.mocked(api.uploadBlob).mockRejectedValue({ status: 413, storage_quota: { cap: 2048, ceiling: 4096, usage: 1536 } });
    await expect(useBlobStore.getState().uploadBlob("session", new File(["x"], "data.csv"))).rejects.toBeDefined();
    expect(useBlobStore.getState().error).toBe("Storage quota reached: 1.5 KB used of 2.0 KB. Delete files you no longer need, then try again.");
  });

  it("keeps a plain file-size 413 distinct", async () => {
    vi.mocked(api.uploadBlob).mockRejectedValue({ status: 413 });
    await expect(useBlobStore.getState().uploadBlob("session", new File(["x"], "data.csv"))).rejects.toBeDefined();
    expect(useBlobStore.getState().error).toBe("File exceeds the maximum upload size.");
  });
});

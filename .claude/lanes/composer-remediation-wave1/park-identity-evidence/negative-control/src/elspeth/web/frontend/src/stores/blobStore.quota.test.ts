import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/client";
import { useBlobStore } from "./blobStore";
import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { BlobManager } from "@/components/blobs/BlobManager";
import { useSessionStore } from "./sessionStore";

vi.mock("@/api/client", () => ({ uploadBlob: vi.fn() }));
vi.mock("@/api/workflow", () => ({ fetchMyQuota: vi.fn().mockReturnValue(new Promise(() => {})) }));

describe("upload refusal copy", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useBlobStore.getState().reset();
    useBlobStore.getState().activateSession("session");
  });

  it("shows measured identity usage against the binding personal limit", async () => {
    vi.mocked(api.uploadBlob).mockRejectedValue({ status: 413, storage_quota: { scope: "identity", limit: "2048", cap: "2048", ceiling: "4096", usage: "1536" } });
    await expect(useBlobStore.getState().uploadBlob("session", new File(["x"], "data.csv"))).rejects.toBeDefined();
    expect(useBlobStore.getState().error).toBe("Personal storage quota reached: 1.5 KB used of 2.0 KB. Delete files you no longer need or ask an administrator to increase your limit, then try again.");
  });

  it("renders shared exhaustion against the shared limit and offers shared recovery", async () => {
    vi.mocked(api.uploadBlob).mockRejectedValue({ status: 413, storage_quota: {
      scope: "container", limit: "1000", cap: "100", ceiling: "1000", usage: "995",
    } });
    await expect(useBlobStore.getState().uploadBlob("session", new File(["1234567890"], "data.csv"))).rejects.toBeDefined();
    useBlobStore.setState({ loadBlobs: vi.fn().mockResolvedValue(undefined) });
    useSessionStore.setState({ activeSessionId: "session" });
    render(createElement(BlobManager, { onUseAsInput: vi.fn() }));
    expect(screen.getByText("Shared storage quota reached: 995 B used of 1000 B across this deployment. Free shared storage or ask an administrator to increase the shared limit, then try again.")).toBeInTheDocument();
    expect(screen.queryByText(/995 B used of 100 B/)).not.toBeInTheDocument();
  });

  it("keeps legacy unscoped storage refusal generic rather than presenting a guessed cap", async () => {
    vi.mocked(api.uploadBlob).mockRejectedValue({ status: 413, error_type: "storage_quota_exceeded" });
    await expect(useBlobStore.getState().uploadBlob("session", new File(["x"], "data.csv"))).rejects.toBeDefined();
    expect(useBlobStore.getState().error).toContain("administrator to review the applicable limit");
    expect(useBlobStore.getState().error).not.toContain("maximum upload size");
  });

  it("keeps a plain file-size 413 distinct", async () => {
    vi.mocked(api.uploadBlob).mockRejectedValue({ status: 413 });
    await expect(useBlobStore.getState().uploadBlob("session", new File(["x"], "data.csv"))).rejects.toBeDefined();
    expect(useBlobStore.getState().error).toBe("File exceeds the maximum upload size.");
  });
});

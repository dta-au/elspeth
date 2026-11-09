import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import * as workflow from "@/api/workflow";
import { IdentityStorageTotal } from "./IdentityStorageTotal";
import type { IdentityQuota } from "@/types/workflow";

vi.mock("@/api/workflow", () => ({ fetchMyQuota: vi.fn() }));

const quota: IdentityQuota = {
  identity_id: "member", tokens_per_day: "500", storage_bytes: "4096",
  container_tokens_per_day: null, container_storage_bytes: "2048",
  tokens_used_today: "0", storage_bytes_used: "1024",
  policy_state: "active", can_edit: true,
  container_tokens_used_today: "0", container_storage_bytes_used: "1800",
};

describe("IdentityStorageTotal", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows personal and aggregate storage against their respective ceilings", async () => {
    vi.mocked(workflow.fetchMyQuota).mockResolvedValue(quota);
    render(<IdentityStorageTotal refreshKey="first" />);
    expect(await screen.findByText("Your storage uses 1.0 KB of 4.0 KB; shared storage 1.8 KB of 2.0 KB")).toBeInTheDocument();
    expect(screen.getByTitle("Includes uploads, retained run artifacts, and active output reservations. Archiving does not reclaim storage.")).toBeInTheDocument();
  });

  it("shows usage alone when neither cap applies", async () => {
    vi.mocked(workflow.fetchMyQuota).mockResolvedValue({ ...quota, storage_bytes: null, container_storage_bytes: null });
    render(<IdentityStorageTotal refreshKey="first" />);
    expect(await screen.findByText("Your storage uses 1.0 KB")).toBeInTheDocument();
  });

  it("refreshes with the file list and hides stale data after a read failure", async () => {
    vi.mocked(workflow.fetchMyQuota).mockResolvedValueOnce(quota).mockRejectedValueOnce(new Error("unavailable"));
    const { rerender, container } = render(<IdentityStorageTotal refreshKey="one" />);
    await screen.findByText("Your storage uses 1.0 KB of 4.0 KB; shared storage 1.8 KB of 2.0 KB");
    rerender(<IdentityStorageTotal refreshKey="two" />);
    await waitFor(() => expect(workflow.fetchMyQuota).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as workflow from "@/api/workflow";
import type { IdentityQuota } from "@/types/workflow";
import { QuotaEditor } from "./QuotaEditor";

vi.mock("@/api/workflow", () => ({ fetchIdentityQuota: vi.fn(), setIdentityQuota: vi.fn() }));

const quota: IdentityQuota = {
  identity_id: "member",
  tokens_per_day: 500,
  storage_bytes: 2048,
  container_tokens_per_day: 9000,
  container_storage_bytes: null,
  tokens_used_today: 160,
  storage_bytes_used: 1024,
};

describe("QuotaEditor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(workflow.fetchIdentityQuota).mockResolvedValue(quota);
  });

  it("shows identity caps, usage and container ceilings", async () => {
    render(<QuotaEditor identityId="member" />);
    expect(await screen.findByText("Tokens per day: 500 (160 used today; container ceiling 9000)")).toBeInTheDocument();
    expect(screen.getByText("Storage: 2.0 KB (1.0 KB used; no container ceiling)")).toBeInTheDocument();
  });

  it("edits exactly one selected dimension and shows the refreshed result", async () => {
    vi.mocked(workflow.setIdentityQuota).mockResolvedValue({ ...quota, storage_bytes: 3000 });
    render(<QuotaEditor identityId="member" />);
    await screen.findByText(/Tokens per day: 500/);
    await userEvent.selectOptions(screen.getByLabelText("Dimension"), "storage");
    await userEvent.type(screen.getByLabelText("New cap"), "3000");
    await userEvent.click(screen.getByRole("button", { name: "Save cap" }));
    await waitFor(() => expect(workflow.setIdentityQuota).toHaveBeenCalledWith("member", "storage", 3000));
    expect(await screen.findByText(/Storage: 2.9 KB/)).toBeInTheDocument();
  });

  it("rejects noninteger and out-of-range values before calling the API", async () => {
    render(<QuotaEditor identityId="member" />);
    await screen.findByText(/Tokens per day: 500/);
    const value = screen.getByLabelText("New cap");
    const save = screen.getByRole("button", { name: "Save cap" });
    for (const invalid of ["0", "2.5", "2147483648", "-1"]) {
      await userEvent.clear(value);
      await userEvent.type(value, invalid);
      expect(save).toBeDisabled();
    }
    expect(workflow.setIdentityQuota).not.toHaveBeenCalled();
  });

  it("shows a backend refusal and preserves the entered value", async () => {
    vi.mocked(workflow.setIdentityQuota).mockRejectedValue({ error_type: "quota_default_missing", detail: "No container default for storage" });
    render(<QuotaEditor identityId="member" />);
    await screen.findByText(/Tokens per day: 500/);
    await userEvent.type(screen.getByLabelText("New cap"), "42");
    await userEvent.click(screen.getByRole("button", { name: "Save cap" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No container default for storage");
    expect(screen.getByLabelText("New cap")).toHaveValue("42");
  });
});

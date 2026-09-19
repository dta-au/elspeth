import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as admin from "@/api/identityAdmin";
import * as workflow from "@/api/workflow";
import type { IdentityView } from "@/types/identityAdmin";
import { AdminDialog } from "./AdminDialog";

vi.mock("@/api/identityAdmin", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/identityAdmin")>();
  return { ...actual, listIdentities: vi.fn() };
});
vi.mock("@/api/workflow", () => ({ fetchIdentityQuota: vi.fn(), setIdentityQuota: vi.fn() }));

const active: IdentityView = {
  identity_id: "member", provider: "local", kind: "human", subject: "member", username: "member",
  display_name: null, email: null, organisation_id: null, access_state: "active",
  first_seen_at: "2026-09-19T00:00:00Z", last_login_at: null, pre_provisioned_at: null,
  activated_at: "2026-09-19T00:00:00Z", activated_by_identity_id: null,
  disabled_at: null, disabled_by_identity_id: null, disable_reason: null,
};

describe("admin quota entry", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(admin.listIdentities).mockImplementation(async (state) => ({
      identities: state === "active" ? [active] : [], access_state: state, limit: 50, offset: 0, active_human_admin_count: 2,
    }));
    vi.mocked(workflow.fetchIdentityQuota).mockResolvedValue({
      identity_id: "member", tokens_per_day: 500, storage_bytes: 2048, container_tokens_per_day: 9000,
      container_storage_bytes: null, tokens_used_today: 160, storage_bytes_used: 1024,
    });
  });

  it("opens the quota editor from an active identity row", async () => {
    render(<AdminDialog onClose={vi.fn()} currentIdentityId="admin" />);
    await userEvent.selectOptions(screen.getByLabelText("Access state"), "active");
    await screen.findByText("member");
    await userEvent.click(screen.getByRole("button", { name: "Quota" }));
    expect(await screen.findByRole("heading", { name: "Quota for member" })).toBeInTheDocument();
    expect(workflow.fetchIdentityQuota).toHaveBeenCalledWith("member");
    expect(await screen.findByText(/Storage: 2.0 KB/)).toBeInTheDocument();
  });
});

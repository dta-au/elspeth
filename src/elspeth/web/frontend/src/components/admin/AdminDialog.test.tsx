import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as admin from "@/api/identityAdmin";
import type { IdentityView } from "@/types/identityAdmin";
import { AdminDialog } from "./AdminDialog";

vi.mock("@/api/identityAdmin", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/identityAdmin")>();
  return { ...actual, listIdentities: vi.fn(), preProvisionIdentity: vi.fn(), activateIdentity: vi.fn(), disableIdentity: vi.fn(), enableIdentity: vi.fn(), listRoles: vi.fn(), grantRole: vi.fn(), revokeRole: vi.fn(), listRelationships: vi.fn(), assertRelationship: vi.fn(), revokeRelationship: vi.fn() };
});

const pending: IdentityView = {
  identity_id: "pending-id", provider: "oidc", kind: "human", subject: "subject-opaque", organisation_id: "org-a", access_state: "pending",
  username: "Sensitive Username", display_name: "Sensitive Name", email: "private@example.test", first_seen_at: "2026-09-19T00:00:00Z",
  last_login_at: null, pre_provisioned_at: null, activated_at: null, activated_by_identity_id: null,
  disabled_at: null, disabled_by_identity_id: null, disable_reason: null,
};

describe("AdminDialog", () => {
  const onClose = vi.fn();
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(admin.listIdentities).mockResolvedValue({ identities: [pending], access_state: "pending", limit: 50, offset: 0, active_human_admin_count: 1 });
    vi.mocked(admin.listRoles).mockResolvedValue({ roles: [], limit: 50, offset: 0 });
    vi.mocked(admin.listRelationships).mockResolvedValue({ relationships: [], limit: 50, offset: 0 });
  });

  it("redacts a never-admitted row and advises on a single active human admin", async () => {
    render(<AdminDialog onClose={onClose} currentIdentityId="root" />);
    expect(await screen.findByText("subject-opaque")).toBeInTheDocument();
    expect(screen.getByText("org-a")).toBeInTheDocument();
    expect(screen.queryByText("Sensitive Username")).not.toBeInTheDocument();
    expect(screen.queryByText("Sensitive Name")).not.toBeInTheDocument();
    expect(screen.queryByText("private@example.test")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("one active human administrator");
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-modal", "true");
  });

  it("does not offer operator-managed service identities in human pre-provisioning", async () => {
    render(<AdminDialog onClose={onClose} currentIdentityId="root" />);
    await screen.findByText("subject-opaque");
    const provider = screen.getByLabelText("Provider");
    expect(within(provider).queryByRole("option", { name: "service" })).not.toBeInTheDocument();
    expect(within(provider).getByRole("option", { name: "oidc" })).toBeInTheDocument();
  });

  it("activates with a required note, then reloads the selected page", async () => {
    vi.mocked(admin.activateIdentity).mockResolvedValue({ identity: { ...pending, access_state: "active" }, role: null, quota_written: true });
    render(<AdminDialog onClose={onClose} currentIdentityId="root" />);
    await screen.findByText("subject-opaque");
    await userEvent.click(screen.getByRole("button", { name: "Activate" }));
    const confirm = screen.getByRole("button", { name: "Confirm activate" });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Note"), "Admit for work");
    await userEvent.click(confirm);
    await waitFor(() => expect(admin.activateIdentity).toHaveBeenCalledWith("pending-id", "user", "Admit for work"));
    expect(admin.listIdentities).toHaveBeenCalledTimes(2);
    const completion = await screen.findByText("Identity change completed.");
    expect(completion).toHaveFocus();
  });

  it("returns focus to an action status after cancelling", async () => {
    render(<AdminDialog onClose={onClose} currentIdentityId="root" />);
    await screen.findByText("subject-opaque");
    await userEvent.click(screen.getByRole("button", { name: "Activate" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("button", { name: "Confirm activate" })).not.toBeInTheDocument();
    expect(screen.getByText("Identity action cancelled.")).toHaveFocus();
  });

  it("clears an identity action when paging away from its row", async () => {
    const firstPage = Array.from({ length: admin.ADMIN_PAGE_SIZE }, (_, index) => ({ ...pending, identity_id: `pending-${index}`, subject: `subject-${index}` }));
    vi.mocked(admin.listIdentities).mockImplementation(async (_state, offset = 0) => ({ identities: offset === 0 ? firstPage : [], access_state: "pending", limit: 50, offset, active_human_admin_count: 1 }));
    render(<AdminDialog onClose={onClose} currentIdentityId="root" />);
    await screen.findByText("subject-0");
    await userEvent.click(screen.getAllByRole("button", { name: "Activate" })[0]);
    expect(screen.getByRole("button", { name: "Confirm activate" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.queryByRole("button", { name: "Confirm activate" })).not.toBeInTheDocument();
    await waitFor(() => expect(admin.listIdentities).toHaveBeenCalledWith("pending", 50));
  });

  it("keeps the identity state filter fixed during a pending mutation", async () => {
    let complete!: (value: Awaited<ReturnType<typeof admin.activateIdentity>>) => void;
    vi.mocked(admin.activateIdentity).mockImplementation(() => new Promise((resolve) => { complete = resolve; }));
    render(<AdminDialog onClose={onClose} currentIdentityId="root" />);
    await screen.findByText("subject-opaque");
    await userEvent.click(screen.getByRole("button", { name: "Activate" }));
    await userEvent.type(screen.getByLabelText("Note"), "Admit");
    await userEvent.click(screen.getByRole("button", { name: "Confirm activate" }));
    expect(screen.getByLabelText("Access state")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    complete({ identity: { ...pending, access_state: "active" }, role: null, quota_written: true });
    await screen.findByText("Identity change completed.");
  });

  it("requires confirmation for a role revoke", async () => {
    vi.mocked(admin.listRoles).mockResolvedValue({ roles: [{ role_id: "role-a", identity_id: "member", role: "reviewer", scope: null, expires_at: null, note: null, granted_by_identity_id: "root", granted_at: "2026-09-19T00:00:00Z", revoked_at: null }], limit: 50, offset: 0 });
    vi.mocked(admin.revokeRole).mockResolvedValue({ role_id: "role-a", identity_id: "member", role: "reviewer", scope: null, expires_at: null, note: null, granted_by_identity_id: "root", granted_at: "2026-09-19T00:00:00Z", revoked_at: "2026-09-19T00:10:00Z" });
    render(<AdminDialog onClose={onClose} currentIdentityId="root" />);
    await userEvent.click(screen.getByRole("tab", { name: "Roles" }));
    await screen.findByText("member");
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    expect(admin.revokeRole).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Confirm revoke" }));
    await waitFor(() => expect(admin.revokeRole).toHaveBeenCalledWith("role-a"));
  });

  it("grants a deployment-wide role and holds the filter during the write", async () => {
    let complete!: (value: Awaited<ReturnType<typeof admin.grantRole>>) => void;
    vi.mocked(admin.grantRole).mockImplementation(() => new Promise((resolve) => { complete = resolve; }));
    render(<AdminDialog onClose={onClose} currentIdentityId="root" />);
    await userEvent.click(screen.getByRole("tab", { name: "Roles" }));
    await screen.findByText("No active roles on this page.");
    expect(screen.getByRole("heading", { name: "Grant deployment-wide role" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Scope (optional)")).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Identity ID"), "member");
    await userEvent.click(screen.getByRole("button", { name: "Grant role" }));
    expect(admin.grantRole).toHaveBeenCalledWith({ identity_id: "member", role: "user" });
    expect(screen.getByLabelText("Filter by identity ID")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Apply filter" })).toBeDisabled();
    complete({ role_id: "role-new", identity_id: "member", role: "user", scope: null, expires_at: null, note: null, granted_by_identity_id: "root", granted_at: "2026-09-19T00:00:00Z", revoked_at: null });
    await waitFor(() => expect(screen.getByLabelText("Filter by identity ID")).toBeEnabled());
  });

  it("supports keyboard tabs and confirms a relationship revoke", async () => {
    vi.mocked(admin.listRelationships).mockResolvedValue({ relationships: [{ relationship_id: "edge-a", from_identity_id: "lead", to_identity_id: "member", relationship_type: "approver", asserted_by_identity_id: "root", asserted_at: "2026-09-19T00:00:00Z", effective_from: null, effective_until: null, note: null, revoked_at: null, revoked_by_identity_id: null }], limit: 50, offset: 0 });
    vi.mocked(admin.revokeRelationship).mockResolvedValue({ relationship_id: "edge-a", from_identity_id: "lead", to_identity_id: "member", relationship_type: "approver", asserted_by_identity_id: "root", asserted_at: "2026-09-19T00:00:00Z", effective_from: null, effective_until: null, note: null, revoked_at: "2026-09-19T00:10:00Z", revoked_by_identity_id: "root" });
    render(<AdminDialog onClose={onClose} currentIdentityId="root" />);
    const identityTab = screen.getByRole("tab", { name: "Identities" });
    identityTab.focus();
    await userEvent.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Relationships" })).toHaveFocus();
    await screen.findByText("lead");
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    expect(admin.revokeRelationship).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Confirm revoke" }));
    await waitFor(() => expect(admin.revokeRelationship).toHaveBeenCalledWith("edge-a"));
  });

  it("holds the relationship filter during an assertion", async () => {
    let complete!: (value: Awaited<ReturnType<typeof admin.assertRelationship>>) => void;
    vi.mocked(admin.assertRelationship).mockImplementation(() => new Promise((resolve) => { complete = resolve; }));
    render(<AdminDialog onClose={onClose} currentIdentityId="root" />);
    await userEvent.click(screen.getByRole("tab", { name: "Relationships" }));
    await screen.findByText("No active relationships on this page.");
    await userEvent.type(screen.getByLabelText("Approver identity ID"), "lead");
    await userEvent.type(screen.getByLabelText("Member identity ID"), "member");
    await userEvent.click(screen.getByRole("button", { name: "Assert relationship" }));
    expect(screen.getByLabelText("Filter by identity ID")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Apply filter" })).toBeDisabled();
    complete({ relationship_id: "edge-new", from_identity_id: "lead", to_identity_id: "member", relationship_type: "approver", asserted_by_identity_id: "root", asserted_at: "2026-09-19T00:00:00Z", effective_from: null, effective_until: null, note: null, revoked_at: null, revoked_by_identity_id: null });
    await waitFor(() => expect(screen.getByLabelText("Filter by identity ID")).toBeEnabled());
  });

  it("closes on Escape and returns focus to its opener", async () => {
    const opener = document.createElement("button");
    opener.textContent = "Open identity admin";
    document.body.append(opener);
    opener.focus();
    const { unmount } = render(<AdminDialog onClose={onClose} currentIdentityId="root" />);
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
    unmount();
    expect(opener).toHaveFocus();
    opener.remove();
  });
});

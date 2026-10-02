import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as client from "@/api/client";
import * as admin from "@/api/identityAdmin";
import * as people from "@/api/people";
import * as workflow from "@/api/workflow";
import type { PersonRecord, PersonResponse } from "@/types/people";
import { PeopleAccessDialog } from "./PeopleAccessDialog";
import { BOTH, IDENTITY_ONLY, LOCAL_ONLY, identityPerson, identityView, localPerson, page, refusal, roleView } from "./peopleTestFixtures";

vi.mock("@/api/people", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/people")>()),
  fetchPeopleCapabilities: vi.fn(), listPeople: vi.fn(), fetchPerson: vi.fn(), fetchPersonLabels: vi.fn(),
}));
vi.mock("@/api/identityAdmin", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/identityAdmin")>()),
  preProvisionIdentity: vi.fn(), activateIdentity: vi.fn(), disableIdentity: vi.fn(), enableIdentity: vi.fn(),
  listRoles: vi.fn(), grantRole: vi.fn(), revokeRole: vi.fn(), listRelationships: vi.fn(), assertRelationship: vi.fn(), revokeRelationship: vi.fn(),
}));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  createAdminUser: vi.fn(), resetAdminUserPassword: vi.fn(), deleteAdminUser: vi.fn(),
}));
vi.mock("@/api/workflow", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/workflow")>()),
  fetchIdentityQuota: vi.fn(), setIdentityQuota: vi.fn(),
}));

const jane = identityPerson();
const sam = identityPerson({ identity_id: "sam-id", subject: "sam.lee", username: "sam.lee", display_name: "Sam Lee", provider: "oidc" });

/** The directory and the direct read both answer from this table, as the server's two routes do. */
let roster: PersonRecord[] = [];
function serve(records: PersonRecord[], capabilities = BOTH): void {
  roster = records;
  vi.mocked(people.fetchPeopleCapabilities).mockResolvedValue(capabilities);
  vi.mocked(people.listPeople).mockImplementation(() => Promise.resolve(page(roster, capabilities)));
  vi.mocked(people.fetchPerson).mockImplementation((key): Promise<PersonResponse> => {
    const found = roster.find((person) => person.key === key);
    return found === undefined ? Promise.reject(refusal(404, "Not found")) : Promise.resolve({ person: found, capabilities });
  });
}

async function openPerson(name: RegExp): Promise<void> {
  render(<PeopleAccessDialog onClose={vi.fn()} onUnavailable={vi.fn()} />);
  await userEvent.click(await within(await screen.findByRole("list", { name: "People results" })).findByRole("button", { name }));
}

describe("People & access journeys", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    serve([jane, sam]);
    vi.mocked(people.fetchPersonLabels).mockResolvedValue([]);
    vi.mocked(admin.listRoles).mockResolvedValue({ roles: [roleView()], limit: 50, offset: 0 });
    vi.mocked(admin.listRelationships).mockResolvedValue({ relationships: [], limit: 50, offset: 0 });
  });

  // ── Add a local person ──────────────────────────────────────────────────

  it("creates a local account, says access is not set up, then sets it up and moves to the identity key", async () => {
    serve([jane]);
    vi.mocked(client.createAdminUser).mockImplementation((body) => {
      roster = [...roster, localPerson(body.username, body.display_name, "not_set_up")];
      return Promise.resolve({ user_id: body.username, password: "gen-Pass-1" });
    });
    vi.mocked(admin.preProvisionIdentity).mockImplementation((body) => {
      const created = identityPerson({ identity_id: "alex-id", subject: body.subject, username: body.subject, display_name: "Alex Kim" }, { local_account: { username: body.subject, display_name: "Alex Kim", email: null, email_verified: false } });
      roster = roster.filter((person) => person.key !== `local:${body.subject}`).concat(created);
      return Promise.resolve({ identity: created.identity, role: null, quota_written: true });
    });
    render(<PeopleAccessDialog onClose={vi.fn()} onUnavailable={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Add person" }));
    expect(screen.getByText(/Creating the account does not grant access/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Username"), "alex");
    await userEvent.type(screen.getByLabelText("Display name"), "Alex Kim");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByRole("heading", { name: "Account created for alex" })).toHaveFocus();
    expect(client.createAdminUser).toHaveBeenCalledWith({ username: "alex", display_name: "Alex Kim" });
    expect(await screen.findByText("Access not set up", { selector: ".people-access-status *" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Set up access" }));
    await userEvent.type(screen.getByLabelText("Note (required)"), "new starter");
    await userEvent.click(screen.getByRole("button", { name: "Set up access" }));
    // Exact correlation: the LOCAL provider, and the subject IS the username.
    await waitFor(() => expect(admin.preProvisionIdentity).toHaveBeenCalledWith({ provider: "local", subject: "alex", username: "alex", role: "user", note: "new starter" }));
    await waitFor(() => expect(people.fetchPerson).toHaveBeenLastCalledWith("identity:alex-id", expect.anything()));
    expect(await screen.findByRole("button", { name: "Disable access" })).toBeInTheDocument();
    // The password from the first step is still there: the second step did not discard it.
    expect(screen.getByTestId("generated-password")).toHaveTextContent("gen-Pass-1");
  });

  it("keeps the account and retries only the access step when that step fails", async () => {
    serve([localPerson("alex", "Alex Kim", "not_set_up")]);
    vi.mocked(admin.preProvisionIdentity).mockRejectedValueOnce(refusal(409, "identity already exists"));
    await openPerson(/Alex Kim/);
    await userEvent.click(await screen.findByRole("button", { name: "Set up access" }));
    await userEvent.type(screen.getByLabelText("Note (required)"), "new starter");
    await userEvent.click(screen.getByRole("button", { name: "Set up access" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("identity already exists");
    expect(screen.getByText(/already exists and is kept whatever happens here/)).toBeInTheDocument();
    expect(screen.getByLabelText("Note (required)")).toHaveValue("new starter");
    expect(screen.getByRole("button", { name: "Retry access setup" })).toBeEnabled();
    expect(client.createAdminUser).not.toHaveBeenCalled();
    expect(client.deleteAdminUser).not.toHaveBeenCalled();
  });

  it("tells a local-only administrator that someone else sets access up", async () => {
    serve([localPerson("alex", "Alex Kim", "not_visible")], LOCAL_ONLY);
    await openPerson(/Alex Kim/);
    expect(await screen.findByText(/Access is managed by an access administrator/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Set up access" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reset password for Alex Kim" })).toBeInTheDocument();
  });

  it("prepares external sign-in access without offering service accounts or claiming an invitation", async () => {
    serve([jane], { ...IDENTITY_ONLY, auth_provider: "entra" });
    vi.mocked(admin.preProvisionIdentity).mockResolvedValue({ identity: identityView({ identity_id: "new-id", provider: "entra" }), role: null, quota_written: true });
    render(<PeopleAccessDialog onClose={vi.fn()} onUnavailable={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Add person" }));
    // Identity-only: there is no local-account option to choose between.
    expect(screen.queryByLabelText("Username")).not.toBeInTheDocument();
    expect(screen.getByText(/does not create an account with the sign-in provider and does not send an invitation/)).toBeInTheDocument();
    const provider = screen.getByLabelText("Sign-in provider");
    expect(provider).toHaveValue("entra");
    expect(within(provider).queryByRole("option", { name: /service/ })).not.toBeInTheDocument();
    expect(within(provider).queryByRole("option", { name: /^local/ })).not.toBeInTheDocument();
    expect(screen.getByText(/often not their email address/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Subject identifier"), "00u-abc");
    expect(screen.getByRole("button", { name: "Prepare access" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Note (required)"), "contractor");
    await userEvent.click(screen.getByRole("button", { name: "Prepare access" }));
    await waitFor(() => expect(admin.preProvisionIdentity).toHaveBeenCalledWith({ provider: "entra", subject: "00u-abc", role: "user", note: "contractor" }));
  });

  // ── Approve pending access ──────────────────────────────────────────────

  it("approves a pending person and keeps them on screen after they leave the pending list", async () => {
    const pending = identityPerson({ identity_id: "p-1", provider: "oidc", subject: "subject-opaque", username: null, display_name: null, email: null, access_state: "pending", activated_at: null, last_login_at: null });
    serve([pending]);
    vi.mocked(admin.activateIdentity).mockImplementation(() => {
      roster = [identityPerson({ ...pending.identity, access_state: "active", activated_at: "2026-09-20T00:00:00Z", username: "sam", display_name: "Sam Lee" })];
      return Promise.resolve({ identity: roster[0].record_type === "identity" ? roster[0].identity : pending.identity, role: null, quota_written: true });
    });
    render(<PeopleAccessDialog onClose={vi.fn()} onUnavailable={vi.fn()} />);
    await userEvent.selectOptions(await screen.findByLabelText("Status"), "pending");
    await userEvent.click(await within(await screen.findByRole("list", { name: "People results" })).findByRole("button", { name: /subject-opaque/ }));
    expect(screen.getByRole("button", { name: "Back to pending access" })).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Approve access" }));
    const confirm = screen.getByRole("button", { name: "Approve access" });
    expect(confirm).toBeDisabled();
    expect(within(screen.getByLabelText("Initial role")).getByRole("option", { name: "Add no new role" })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Note (required)"), "Admit for work");
    await userEvent.click(confirm);
    await waitFor(() => expect(admin.activateIdentity).toHaveBeenCalledWith("p-1", "user", "Admit for work"));
    expect(await screen.findByRole("heading", { name: "Sam Lee" })).toBeInTheDocument();
    expect(screen.getByText("Active", { selector: ".people-access-status *" })).toBeInTheDocument();
    // The approval granted the initial role server-side, so the roles list is
    // re-read: showing "holds no roles" beside "Approved" invites a duplicate grant.
    await waitFor(() => expect(vi.mocked(admin.listRoles).mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("shows a returning person's retained roles before the approval is confirmed", async () => {
    const returning = identityPerson({ access_state: "pending", disable_reason: "dormant" });
    serve([returning]);
    vi.mocked(admin.listRoles).mockResolvedValue({ roles: [roleView({ role: "admin" })], limit: 50, offset: 0 });
    await openPerson(/Jane Doe/);
    await userEvent.click(await screen.findByRole("button", { name: "Approve access" }));
    const form = screen.getByRole("heading", { name: "Approve access for Jane Doe?" }).closest("form") as HTMLElement;
    expect(await within(form).findByText(/Approving restores the roles Jane Doe already holds/)).toHaveTextContent("Administrator");
    expect(within(form).getByLabelText("Role to add")).toBeInTheDocument();
  });

  // ── Roles ───────────────────────────────────────────────────────────────

  it("grants an expiring role to the selected person without any identity ID, as a UTC instant", async () => {
    vi.mocked(admin.grantRole).mockResolvedValue(roleView({ role: "reviewer" }));
    await openPerson(/Jane Doe/);
    await userEvent.click(await screen.findByRole("button", { name: "Add role" }));
    // The ID is copyable under Advanced details, but nothing asks the administrator to TYPE one.
    expect(screen.queryByRole("textbox", { name: /identity id/i })).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Role"), "reviewer");
    expect(screen.getByText(/Reviews pipelines and attests to them/)).toBeInTheDocument();
    expect(screen.getByText(/Expiry is entered in your time zone/)).toBeInTheDocument();
    const future = new Date(Date.now() + 7 * 24 * 3600 * 1000);
    const local = new Date(future.getTime() - future.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    await userEvent.type(screen.getByLabelText("Expires (optional)"), local);
    await userEvent.click(screen.getByRole("button", { name: "Grant role" }));
    await waitFor(() => expect(admin.grantRole).toHaveBeenCalledOnce());
    const sent = vi.mocked(admin.grantRole).mock.calls[0][0];
    expect(sent.identity_id).toBe("jane-id");
    expect(sent.expires_at).toMatch(/Z$/);
    expect(new Date(sent.expires_at as string).getTime()).toBe(new Date(local).getTime());
  });

  it("warns about the sole administrator on that person only, and stops once a second administrator exists", async () => {
    const soleJane = identityPerson({}, { sole_active_admin: true });
    serve([soleJane, sam]);
    vi.mocked(admin.grantRole).mockImplementation(() => {
      // The server's next read of Jane says she is no longer the only one.
      roster = [identityPerson(), sam];
      return Promise.resolve(roleView({ role: "admin" }));
    });
    await openPerson(/Jane Doe/);
    expect(await screen.findByText(/Jane Doe is the only active administrator, so disabling them is refused/)).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Add role" }));
    await userEvent.selectOptions(screen.getByLabelText("Role"), "admin");
    await userEvent.click(screen.getByRole("button", { name: "Grant role" }));
    await waitFor(() => expect(screen.queryByText(/is the only active administrator/)).not.toBeInTheDocument());
  });

  it("does not warn about the sole administrator on anyone else", async () => {
    serve([identityPerson({}, { sole_active_admin: true }), sam]);
    await openPerson(/Sam Lee/);
    await screen.findByRole("button", { name: "Disable access" });
    expect(screen.queryByText(/is the only active administrator/)).toBeNull();
  });

  it("explains a combination the server will refuse, and restricts a service account's roles", async () => {
    vi.mocked(admin.listRoles).mockResolvedValue({ roles: [roleView({ role: "admin" })], limit: 50, offset: 0 });
    await openPerson(/Jane Doe/);
    await userEvent.click(await screen.findByRole("button", { name: "Add role" }));
    await userEvent.selectOptions(screen.getByLabelText("Role"), "approver");
    expect(screen.getByText(/is an Administrator, which cannot be combined with this role/)).toBeInTheDocument();
  });

  it("revokes one specific grant only after a named confirmation", async () => {
    vi.mocked(admin.revokeRole).mockResolvedValue(roleView({ revoked_at: "2026-09-20T00:00:00Z" }));
    await openPerson(/Jane Doe/);
    await userEvent.click(await screen.findByRole("button", { name: "Revoke User from Jane Doe" }));
    expect(admin.revokeRole).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "Revoke User from Jane Doe?" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Revoke role" }));
    await waitFor(() => expect(admin.revokeRole).toHaveBeenCalledWith("role-1", undefined));
  });

  // ── Approvers ───────────────────────────────────────────────────────────

  it("assigns an approver by name, in a stated direction, with the sentence shown before sending", async () => {
    vi.mocked(admin.assertRelationship).mockResolvedValue({} as never);
    await openPerson(/Jane Doe/);
    await userEvent.click(await screen.findByRole("tab", { name: "Approvers" }));
    await userEvent.click(await screen.findByRole("button", { name: "Assign an approver" }));
    // The ID is copyable under Advanced details, but nothing asks the administrator to TYPE one.
    expect(screen.queryByRole("textbox", { name: /identity id/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Assign approver" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Approver"), "sam");
    const result = await screen.findByRole("button", { name: /Sam Lee sam\.lee · OpenID Connect/ });
    // The picker searched the server, for active people, not the loaded page.
    expect(vi.mocked(people.listPeople).mock.lastCall?.[0]).toMatchObject({ q: "sam", status: "active", type: "people" });
    await userEvent.click(result);
    expect(screen.getByText("Assign Sam Lee to approve for Jane Doe.")).toBeInTheDocument();

    await userEvent.click(screen.getByLabelText("Jane Doe approves for someone"));
    expect(screen.getByText("Assign Jane Doe to approve for Sam Lee.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Assign approver" }));
    await waitFor(() => expect(admin.assertRelationship).toHaveBeenCalledWith({ from_identity_id: "jane-id", to_identity_id: "sam-id", relationship_type: "approver" }));
  });

  it("names both directions of existing links, including people who are not on the page", async () => {
    vi.mocked(admin.listRelationships).mockResolvedValue({ limit: 50, offset: 0, relationships: [
      { relationship_id: "r1", from_identity_id: "boss-id", to_identity_id: "jane-id", relationship_type: "approver", asserted_by_identity_id: "root-id", asserted_at: "2026-09-02T00:00:00Z", effective_from: "2026-09-03T00:00:00Z", effective_until: null, note: null, revoked_at: null, revoked_by_identity_id: null },
      { relationship_id: "r2", from_identity_id: "jane-id", to_identity_id: "sam-id", relationship_type: "approver", asserted_by_identity_id: "root-id", asserted_at: "2026-09-02T00:00:00Z", effective_from: null, effective_until: null, note: null, revoked_at: null, revoked_by_identity_id: null },
    ] });
    vi.mocked(people.fetchPersonLabels).mockResolvedValue([
      { identity_id: "boss-id", label: "Pat Boss", detail: "pboss", provider: "oidc", kind: "human", access_state: "active", retired: false },
      { identity_id: "sam-id", label: "Sam Lee", detail: "sam.lee", provider: "oidc", kind: "human", access_state: "active", retired: false },
    ]);
    await openPerson(/Jane Doe/);
    await userEvent.click(await screen.findByRole("tab", { name: "Approvers" }));
    expect(await screen.findByRole("heading", { name: "Approvers for Jane Doe" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "People Jane Doe approves for" })).toBeInTheDocument();
    expect(people.fetchPersonLabels).toHaveBeenCalledWith(["boss-id", "sam-id"]);
    expect(screen.getByText(/Effective from/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove Pat Boss as an approver for Jane Doe" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop Jane Doe approving for Sam Lee" })).toBeInTheDocument();

    // Removing a link names the direction again and needs a second, deliberate step.
    vi.mocked(admin.revokeRelationship).mockResolvedValue({} as never);
    await userEvent.click(screen.getByRole("button", { name: "Remove Pat Boss as an approver for Jane Doe" }));
    expect(admin.revokeRelationship).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "Remove Pat Boss as an approver for Jane Doe?" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Remove approver link" }));
    await waitFor(() => expect(admin.revokeRelationship).toHaveBeenCalledWith("r1", undefined));
  });

  // ── Disable is not delete ───────────────────────────────────────────────

  it("keeps Disable access and Delete local account as two differently explained actions", async () => {
    const linked = identityPerson({}, { local_account: { username: "jane.doe", display_name: "Jane Doe", email: null, email_verified: false }, actions: { manage_access: true, manage_credentials: true, set_up_access: false, is_self: false } });
    serve([linked]);
    vi.mocked(admin.disableIdentity).mockResolvedValue({ identity: identityView({ access_state: "disabled" }), revoked_relationship_ids: [] });
    await openPerson(/Jane Doe/);
    await userEvent.click(await screen.findByRole("button", { name: "Disable access" }));
    expect(screen.getByRole("heading", { name: "Disable access for Jane Doe?" }).closest("form")).toHaveTextContent("are not restored if you enable access again");
    expect(screen.getByRole("button", { name: "Disable access" })).toBeDisabled();
    await userEvent.keyboard("{Escape}");

    await userEvent.click(screen.getByRole("button", { name: "Delete local account for Jane Doe" }));
    const form = screen.getByRole("heading", { name: "Delete the local account for Jane Doe?" }).parentElement as HTMLElement;
    expect(form).toHaveTextContent("This is not the same as disabling access");
    expect(form).toHaveTextContent("The identity is retired. Its history is kept");
    expect(form).toHaveTextContent("does not receive this person's roles, limits or history");
    expect(client.deleteAdminUser).not.toHaveBeenCalled();
    // Deleting costs what disabling costs: a stated reason (ruling D3).
    const confirmDelete = screen.getByRole("button", { name: "Delete local account" });
    expect(confirmDelete).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reason (required)"), "   ");
    expect(confirmDelete).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reason (required)"), "left the team");
    vi.mocked(client.deleteAdminUser).mockResolvedValue(undefined);
    await userEvent.click(confirmDelete);
    expect(client.deleteAdminUser).toHaveBeenCalledExactlyOnceWith("jane.doe", "left the team");
  });

  it("keeps the two fixed sections together above the tabs, with confirmations one heading level down", async () => {
    const linked = identityPerson({}, { local_account: { username: "jane.doe", display_name: "Jane Doe", email: null, email_verified: false }, actions: { manage_access: true, manage_credentials: true, set_up_access: false, is_self: false } });
    serve([linked]);
    await openPerson(/Jane Doe/);
    const signIn = await screen.findByRole("heading", { level: 4, name: "Sign-in" });
    const tablist = screen.getByRole("tablist");
    expect(screen.getByRole("heading", { level: 4, name: "Access" }).compareDocumentPosition(signIn) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(signIn.compareDocumentPosition(tablist) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Disable access" }));
    expect(screen.getByRole("heading", { level: 5, name: "Disable access for Jane Doe?" })).toBeInTheDocument();
  });

  it("names sign-in methods in words everywhere, and leaves service accounts to the Type filter", async () => {
    await openPerson(/Sam Lee/);
    const method = screen.getByLabelText("Sign-in method");
    expect(within(method).getAllByRole("option").map((option) => option.textContent)).toEqual(["All", "Local account", "OpenID Connect", "Microsoft Entra ID", "VANguard", "Google"]);
    expect(await screen.findByText(/Sam Lee signs in through OpenID Connect\./)).toBeInTheDocument();
    expect(screen.queryByText(/signs in through oidc/)).toBeNull();
  });

  it("shows the server's last-administrator refusal for a deletion and changes nothing", async () => {
    const linked = identityPerson({}, { local_account: { username: "jane.doe", display_name: "Jane Doe", email: null, email_verified: false }, actions: { manage_access: true, manage_credentials: true, set_up_access: false, is_self: false } });
    serve([linked]);
    vi.mocked(client.deleteAdminUser).mockRejectedValue(refusal(409, "the last active human administrator cannot be removed"));
    await openPerson(/Jane Doe/);
    await userEvent.click(await screen.findByRole("button", { name: "Delete local account for Jane Doe" }));
    await userEvent.type(screen.getByLabelText("Reason (required)"), "left the team");
    await userEvent.click(screen.getByRole("button", { name: "Delete local account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("last active human administrator");
    expect(screen.getByRole("heading", { name: "Jane Doe" })).toBeInTheDocument();
  });

  it("explains why your own access and account cannot be removed by you", async () => {
    const me = identityPerson({}, { local_account: { username: "jane.doe", display_name: "Jane Doe", email: null, email_verified: false }, actions: { manage_access: true, manage_credentials: true, set_up_access: false, is_self: true } });
    serve([me]);
    await openPerson(/Jane Doe/);
    expect(await screen.findByRole("button", { name: "Disable access" })).toBeDisabled();
    expect(screen.getByText(/You cannot disable your own access/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete local account/ })).not.toBeInTheDocument();
    expect(screen.getByText(/cannot delete the account you are signed in with/)).toBeInTheDocument();
  });

  it("shows a retired account as history with no access or role controls", async () => {
    serve([identityPerson({ subject: "jane.doe#retired-jane-id", access_state: "disabled", disable_reason: "local credential deleted" }, { retired: true })]);
    await openPerson(/Jane Doe/);
    expect(await screen.findByText(/history of a deleted local account/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Enable access" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Roles" })).not.toBeInTheDocument();
  });

  // ── The three write failures are three different facts ──────────────────

  it("says Saved when the write landed and only the refresh failed", async () => {
    vi.mocked(admin.grantRole).mockResolvedValue(roleView({ role: "reviewer" }));
    await openPerson(/Jane Doe/);
    await userEvent.click(await screen.findByRole("button", { name: "Add role" }));
    vi.mocked(admin.listRoles).mockRejectedValueOnce(refusal(503, "unavailable"));
    await userEvent.click(screen.getByRole("button", { name: "Grant role" }));
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("Saved. Could not refresh details.");
    await userEvent.click(within(notice).getByRole("button", { name: "Retry refresh" }));
    expect(await screen.findByText("Details are up to date.")).toBeInTheDocument();
    expect(admin.grantRole).toHaveBeenCalledOnce();
  });

  it("withholds another write until an uncertain outcome has been checked", async () => {
    vi.mocked(admin.grantRole).mockRejectedValue(new TypeError("Failed to fetch"));
    await openPerson(/Jane Doe/);
    await userEvent.click(await screen.findByRole("button", { name: "Add role" }));
    await userEvent.type(screen.getByLabelText("Note (optional)"), "cover");
    await userEvent.click(screen.getByRole("button", { name: "Grant role" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("may or may not have been saved");
    expect(screen.getByRole("button", { name: "Grant role" })).toBeDisabled();
    expect(screen.getByLabelText("Note (optional)")).toHaveValue("cover");
    await userEvent.click(within(alert).getByRole("button", { name: "Check current details" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Grant role" })).toBeEnabled());
  });

  it("keeps the typed draft after a refusal", async () => {
    vi.mocked(admin.grantRole).mockRejectedValue(refusal(409, "role already held"));
    await openPerson(/Jane Doe/);
    await userEvent.click(await screen.findByRole("button", { name: "Add role" }));
    await userEvent.type(screen.getByLabelText("Note (optional)"), "cover for leave");
    await userEvent.click(screen.getByRole("button", { name: "Grant role" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("role already held");
    expect(screen.getByLabelText("Note (optional)")).toHaveValue("cover for leave");
    expect(screen.getByRole("button", { name: "Grant role" })).toBeEnabled();
  });

  // ── Usage & limits ──────────────────────────────────────────────────────

  it("shows usage, a personal cap and the shared ceiling for one person, and unknown as unknown", async () => {
    vi.mocked(workflow.fetchIdentityQuota).mockResolvedValue({ identity_id: "jane-id", tokens_per_day: null, tokens_used_today: null, container_tokens_per_day: 5000, storage_bytes: null, storage_bytes_used: 0, container_storage_bytes: null } as never);
    await openPerson(/Jane Doe/);
    // Opening a person costs no quota read: limits load when their section is opened.
    expect(workflow.fetchIdentityQuota).not.toHaveBeenCalled();
    await userEvent.click(await screen.findByRole("tab", { name: "Usage & limits" }));
    const region = await screen.findByRole("region", { name: "Usage and limits for Jane Doe" });
    expect(await within(region).findByText(/unknown used today; container ceiling 5000/)).toBeInTheDocument();
    expect(region).toHaveTextContent("A container ceiling is shared by everyone");
    expect(workflow.fetchIdentityQuota).toHaveBeenCalledWith("jane-id");
  });

  it("offers Retry when limits cannot be read instead of showing a zero", async () => {
    vi.mocked(workflow.fetchIdentityQuota).mockRejectedValueOnce(refusal(503, "quota unavailable"));
    await openPerson(/Jane Doe/);
    await userEvent.click(await screen.findByRole("tab", { name: "Usage & limits" }));
    const region = await screen.findByRole("region", { name: "Usage and limits for Jane Doe" });
    expect(await within(region).findByRole("alert")).toBeInTheDocument();
    vi.mocked(workflow.fetchIdentityQuota).mockResolvedValue({ identity_id: "jane-id", tokens_per_day: 100, tokens_used_today: 0, container_tokens_per_day: null, storage_bytes: null, storage_bytes_used: 0, container_storage_bytes: null } as never);
    await userEvent.click(within(region).getByRole("button", { name: "Retry" }));
    expect(await within(region).findByText(/Tokens per day: 100 \(0 used today/)).toBeInTheDocument();
  });

  it("keeps service accounts visible under their own filter, without approvers", async () => {
    serve([identityPerson({ identity_id: "svc", kind: "service", provider: "service", subject: "console", username: "console", display_name: null })]);
    render(<PeopleAccessDialog onClose={vi.fn()} onUnavailable={vi.fn()} />);
    await userEvent.selectOptions(await screen.findByLabelText("Type"), "service");
    await waitFor(() => expect(vi.mocked(people.listPeople).mock.lastCall?.[0]).toMatchObject({ type: "service" }));
    await userEvent.click(await within(await screen.findByRole("list", { name: "People results" })).findByRole("button", { name: /console/ }));
    expect(await screen.findByText(/Service account \(operator-managed\)/)).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Approvers" })).not.toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Add role" }));
    await userEvent.selectOptions(screen.getByLabelText("Role"), "user");
    expect(screen.getByText(/A service account can hold only Administrator or Oversight/)).toBeInTheDocument();
  });
});

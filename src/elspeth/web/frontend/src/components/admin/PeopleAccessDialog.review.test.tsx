/**
 * Regressions from the 2026-09-20 implementation review
 * (docs/plans/2026-09-20-people-access-implementation-review.md). Each case
 * is the review's reproduction, asserting the behaviour it asked for.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as client from "@/api/client";
import * as admin from "@/api/identityAdmin";
import * as people from "@/api/people";
import type { RelationshipView } from "@/types/identityAdmin";
import type { PeopleCapabilities, PersonRecord, PersonResponse } from "@/types/people";
import { PeopleAccessDialog } from "./PeopleAccessDialog";
import { BOTH, IDENTITY_ONLY, identityPerson, localPerson, page, refusal, roleView } from "./peopleTestFixtures";

vi.mock("@/api/people", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/people")>()),
  fetchPeopleCapabilities: vi.fn(), listPeople: vi.fn(), fetchPerson: vi.fn(), fetchPersonLabels: vi.fn(),
}));
vi.mock("@/api/identityAdmin", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/identityAdmin")>()),
  preProvisionIdentity: vi.fn(), listRoles: vi.fn(), grantRole: vi.fn(), revokeRole: vi.fn(), listRelationships: vi.fn(), assertRelationship: vi.fn(), revokeRelationship: vi.fn(),
}));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  resetAdminUserPassword: vi.fn(), deleteAdminUser: vi.fn(),
}));

const JANE_ACCOUNT = { username: "jane.doe", display_name: "Jane Doe", email: null, email_verified: false };
const MANAGED = { manage_access: true, manage_credentials: true, set_up_access: false, is_self: false };
const jane = identityPerson({}, { local_account: JANE_ACCOUNT, actions: MANAGED });
const sam = identityPerson({ identity_id: "sam-id", subject: "sam.lee", username: "sam.lee", display_name: "Sam Lee", provider: "oidc" });

let roster: PersonRecord[] = [];
let capabilities: PeopleCapabilities = BOTH;
function serve(records: PersonRecord[]): void {
  roster = records;
  capabilities = BOTH;
  vi.mocked(people.fetchPeopleCapabilities).mockImplementation(() => Promise.resolve(capabilities));
  vi.mocked(people.listPeople).mockImplementation(() => Promise.resolve(page(roster, capabilities)));
  vi.mocked(people.fetchPerson).mockImplementation((key): Promise<PersonResponse> => {
    const found = roster.find((person) => person.key === key);
    return found === undefined ? Promise.reject(refusal(404, "Not found")) : Promise.resolve({ person: found, capabilities });
  });
}

async function open(name: RegExp): Promise<void> {
  render(<PeopleAccessDialog onClose={vi.fn()} onUnavailable={vi.fn()} />);
  await userEvent.click(await within(await screen.findByRole("list", { name: "People results" })).findByRole("button", { name }));
  await screen.findByRole("button", { name: "Add role" });
}

/** A refused role write is how the panel learns its capabilities moved. */
async function loseLocalAccounts(): Promise<void> {
  capabilities = IDENTITY_ONLY;
  roster = [identityPerson(), sam];
  vi.mocked(admin.grantRole).mockRejectedValue(refusal(403, "Forbidden"));
  await userEvent.click(screen.getByRole("button", { name: "Add role" }));
  await userEvent.click(screen.getByRole("button", { name: "Grant role" }));
  await screen.findByText(/Your permissions changed/);
}

function edge(index: number, from: string, to: string): RelationshipView {
  return { relationship_id: `edge-${index}`, from_identity_id: from, to_identity_id: to, relationship_type: "approver", effective_from: null, effective_until: null, asserted_at: "2026-09-01T00:00:00Z", asserted_by_identity_id: "root-id", revoked_at: null, revoked_by_identity_id: null, note: null };
}

describe("People & access: implementation review regressions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    serve([jane, sam]);
    vi.mocked(people.fetchPersonLabels).mockResolvedValue([]);
    vi.mocked(admin.listRoles).mockResolvedValue({ roles: [roleView()], limit: 200, offset: 0 });
    vi.mocked(admin.listRelationships).mockResolvedValue({ relationships: [], limit: 200, offset: 0 });
  });

  // ── 1. Generated passwords and the capability that produced them ────────

  it("takes a generated password off the screen when the local-accounts capability is lost", async () => {
    vi.mocked(client.resetAdminUserPassword).mockResolvedValue({ user_id: "jane.doe", password: "gen-Pass-9" });
    await open(/Jane Doe/);
    await userEvent.click(screen.getByRole("button", { name: "Reset password for Jane Doe" }));
    await userEvent.click(screen.getByRole("button", { name: "Reset password" }));
    expect(await screen.findByTestId("generated-password")).toHaveTextContent("gen-Pass-9");

    await loseLocalAccounts();

    expect(screen.queryByTestId("generated-password")).not.toBeInTheDocument();
    expect(screen.queryByText("gen-Pass-9")).not.toBeInTheDocument();
  });

  it("does not show a password whose reset answers after the capability was lost", async () => {
    let answer!: () => void;
    vi.mocked(client.resetAdminUserPassword).mockReturnValue(new Promise((resolve) => { answer = () => resolve({ user_id: "jane.doe", password: "late-Pass-1" }); }));
    await open(/Jane Doe/);
    await userEvent.click(screen.getByRole("button", { name: "Reset password for Jane Doe" }));
    await userEvent.click(screen.getByRole("button", { name: "Reset password" }));
    // The reset is in flight; the capability goes before it answers.
    capabilities = IDENTITY_ONLY;
    roster = [identityPerson(), sam];
    await userEvent.click(within(screen.getByRole("list", { name: "People results" })).getByRole("button", { name: /Sam Lee/ }));
    vi.mocked(admin.grantRole).mockRejectedValue(refusal(403, "Forbidden"));
    await userEvent.click(await screen.findByRole("button", { name: "Add role" }));
    await userEvent.click(screen.getByRole("button", { name: "Grant role" }));
    await screen.findByText(/Your permissions changed/);

    await act(async () => { answer(); await Promise.resolve(); });

    expect(screen.queryByTestId("generated-password")).not.toBeInTheDocument();
    expect(screen.queryByText("late-Pass-1")).not.toBeInTheDocument();
  });

  // ── 2. Lists that answer questions of absence read every page ───────────

  it("finds an approver who lies beyond the first page of links", async () => {
    const all = [...Array.from({ length: 200 }, (_, index) => edge(index, "jane-id", `member-${index}`)), edge(200, "sam-id", "jane-id")];
    vi.mocked(admin.listRelationships).mockImplementation((_id, offset = 0, limit = 50) => Promise.resolve({ relationships: all.slice(offset, offset + limit), limit, offset }));
    vi.mocked(people.fetchPersonLabels).mockResolvedValue([{ identity_id: "sam-id", label: "Sam Lee", detail: "sam.lee", provider: "oidc", kind: "human", access_state: "active", retired: false }]);
    await open(/Jane Doe/);
    await userEvent.click(screen.getByRole("tab", { name: "Approvers" }));

    expect(await screen.findByRole("button", { name: "Remove Sam Lee as an approver for Jane Doe" })).toBeInTheDocument();
    expect(screen.queryByText("Nobody is assigned to approve for Jane Doe.")).not.toBeInTheDocument();
    expect(vi.mocked(admin.listRelationships).mock.calls.map((call) => call[1])).toEqual([0, 200]);
  });

  it("reads role grants past the first page, so none is left unrevokable", async () => {
    const grants = [...Array.from({ length: 200 }, (_, index) => roleView({ role_id: `scoped-${index}`, scope: `team-${index}` })), roleView({ role_id: "late-admin", role: "admin" })];
    vi.mocked(admin.listRoles).mockImplementation((_id, offset = 0, limit = 50) => Promise.resolve({ roles: grants.slice(offset, offset + limit), limit, offset }));
    await open(/Jane Doe/);

    expect(await screen.findByRole("button", { name: "Revoke Administrator from Jane Doe" })).toBeInTheDocument();
  });

  // ── 3. A late answer for one person does not move the selection ─────────

  it("keeps the person the administrator moved to when an earlier person's write finishes", async () => {
    let finish!: () => void;
    vi.mocked(admin.grantRole).mockReturnValue(new Promise((resolve) => { finish = () => resolve(roleView()); }));
    await open(/Jane Doe/);
    await userEvent.click(screen.getByRole("button", { name: "Add role" }));
    await userEvent.click(screen.getByRole("button", { name: "Grant role" }));
    await userEvent.click(within(screen.getByRole("list", { name: "People results" })).getByRole("button", { name: /Sam Lee/ }));
    expect(await screen.findByRole("heading", { name: "Sam Lee" })).toBeInTheDocument();
    const listReads = vi.mocked(people.listPeople).mock.calls.length;

    await act(async () => { finish(); await Promise.resolve(); });

    // The directory still refreshes for Jane's change; the selection does not move.
    await waitFor(() => expect(vi.mocked(people.listPeople).mock.calls.length).toBeGreaterThan(listReads));
    expect(screen.getByRole("heading", { name: "Sam Lee" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Jane Doe" })).not.toBeInTheDocument();
  });

  it("does not jump to a person whose access setup finishes after the administrator moved on", async () => {
    const alex = localPerson("alex", "Alex Kim", "not_set_up");
    serve([alex, sam]);
    let finish!: () => void;
    const created = identityPerson({ identity_id: "alex-id", subject: "alex", username: "alex", display_name: "Alex Kim" });
    vi.mocked(admin.preProvisionIdentity).mockReturnValue(new Promise((resolve) => { finish = () => resolve({ identity: created.identity, role: null, quota_written: true }); }));
    render(<PeopleAccessDialog onClose={vi.fn()} onUnavailable={vi.fn()} />);
    const list = await screen.findByRole("list", { name: "People results" });
    await userEvent.click(await within(list).findByRole("button", { name: /Alex Kim/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Set up access" }));
    await userEvent.type(screen.getByLabelText("Note (required)"), "new starter");
    await userEvent.click(screen.getByRole("button", { name: "Set up access" }));
    await userEvent.click(within(list).getByRole("button", { name: /Sam Lee/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Discard changes" }));
    expect(await screen.findByRole("heading", { name: "Sam Lee" })).toBeInTheDocument();

    roster = [created, sam];
    const listReads = vi.mocked(people.listPeople).mock.calls.length;
    await act(async () => { finish(); await Promise.resolve(); });

    await waitFor(() => expect(vi.mocked(people.listPeople).mock.calls.length).toBeGreaterThan(listReads));
    expect(screen.getByRole("heading", { name: "Sam Lee" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Alex Kim" })).not.toBeInTheDocument();
  });

  // ── 6. A failed check keeps its retry ───────────────────────────────────

  it("keeps the check control, and the block, when the check itself fails", async () => {
    vi.mocked(admin.grantRole).mockRejectedValue(new TypeError("Failed to fetch"));
    await open(/Jane Doe/);
    await userEvent.click(screen.getByRole("button", { name: "Add role" }));
    await userEvent.click(screen.getByRole("button", { name: "Grant role" }));
    vi.mocked(admin.listRoles).mockRejectedValueOnce(refusal(503, "Store unavailable"));
    await userEvent.click(await screen.findByRole("button", { name: "Check current details" }));

    expect(await screen.findByText(/still not known whether the change was saved/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Grant role" })).toBeDisabled();
    // The second check succeeds and gives the form back.
    await userEvent.click(screen.getByRole("button", { name: "Check current details" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Grant role" })).toBeEnabled());
  });

  it("treats a server failure as unknown, not as a refusal that changed nothing", async () => {
    vi.mocked(admin.grantRole).mockRejectedValue(refusal(500, "Internal Server Error"));
    await open(/Jane Doe/);
    await userEvent.click(screen.getByRole("button", { name: "Add role" }));
    await userEvent.click(screen.getByRole("button", { name: "Grant role" }));

    expect(await screen.findByText(/The server failed while handling this/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check current details" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Grant role" })).toBeDisabled();
  });

  // ── 7. Checking an unanswered deletion looks at the server ──────────────

  it("re-reads the person when checking an unanswered deletion, and leaves when the account is gone", async () => {
    const alex = localPerson("alex", "Alex Kim", "not_set_up");
    serve([alex, sam]);
    vi.mocked(client.deleteAdminUser).mockImplementation(() => { roster = [sam]; return Promise.reject(new TypeError("Failed to fetch")); });
    render(<PeopleAccessDialog onClose={vi.fn()} onUnavailable={vi.fn()} />);
    await userEvent.click(await within(await screen.findByRole("list", { name: "People results" })).findByRole("button", { name: /Alex Kim/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete local account for Alex Kim" }));
    await userEvent.type(screen.getByLabelText("Reason (required)"), "left the team");
    await userEvent.click(screen.getByRole("button", { name: "Delete local account" }));
    const reads = vi.mocked(people.fetchPerson).mock.calls.length;
    await userEvent.click(await screen.findByRole("button", { name: "Check current details" }));

    expect(await screen.findByText("The local account for Alex Kim is deleted.")).toBeInTheDocument();
    expect(vi.mocked(people.fetchPerson).mock.calls.length).toBe(reads + 1);
    expect(screen.queryByRole("heading", { name: "Alex Kim" })).not.toBeInTheDocument();
  });

  it("keeps deletion blocked when the check finds the account still there only after it has looked", async () => {
    const alex = localPerson("alex", "Alex Kim", "not_set_up");
    serve([alex, sam]);
    vi.mocked(client.deleteAdminUser).mockRejectedValue(new TypeError("Failed to fetch"));
    render(<PeopleAccessDialog onClose={vi.fn()} onUnavailable={vi.fn()} />);
    await userEvent.click(await within(await screen.findByRole("list", { name: "People results" })).findByRole("button", { name: /Alex Kim/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete local account for Alex Kim" }));
    await userEvent.type(screen.getByLabelText("Reason (required)"), "left the team");
    await userEvent.click(screen.getByRole("button", { name: "Delete local account" }));
    expect(screen.getByRole("button", { name: "Delete local account" })).toBeDisabled();
    const reads = vi.mocked(people.fetchPerson).mock.calls.length;
    await userEvent.click(await screen.findByRole("button", { name: "Check current details" }));

    // Still there: the deletion did not land, and saying so took a read.
    await waitFor(() => expect(screen.getByRole("button", { name: "Delete local account" })).toBeEnabled());
    expect(vi.mocked(people.fetchPerson).mock.calls.length).toBe(reads + 1);
  });

  it("offers to finish a deletion whose account went and whose retirement did not", async () => {
    vi.mocked(client.deleteAdminUser).mockImplementationOnce(() => {
      roster = [identityPerson(), sam];
      return Promise.reject(refusal(500, "Internal Server Error"));
    });
    await open(/Jane Doe/);
    await userEvent.click(screen.getByRole("button", { name: "Delete local account for Jane Doe" }));
    await userEvent.type(screen.getByLabelText("Reason (required)"), "left the team");
    await userEvent.click(screen.getByRole("button", { name: "Delete local account" }));
    await userEvent.click(await screen.findByRole("button", { name: "Check current details" }));

    expect(await screen.findByText(/retiring Jane Doe did not finish/)).toBeInTheDocument();
    vi.mocked(client.deleteAdminUser).mockImplementationOnce(() => { roster = [identityPerson({}, { retired: true }), sam]; return Promise.resolve(undefined); });
    await userEvent.click(screen.getByRole("button", { name: "Finish removing Jane Doe" }));

    await waitFor(() => expect(screen.queryByText(/retiring Jane Doe did not finish/)).not.toBeInTheDocument());
    // The retry writes the deletion's only audit row, so it carries the reason
    // already typed: not asked again, and never sent without one.
    expect(client.deleteAdminUser).toHaveBeenLastCalledWith("jane.doe", "left the team");
  });

  // ── 8. Leaving a section asks before it discards ────────────────────────

  it("asks before a tab change discards a typed draft, by pointer and by keyboard", async () => {
    await open(/Jane Doe/);
    await userEvent.click(screen.getByRole("button", { name: "Add role" }));
    await userEvent.type(screen.getByLabelText("Note (optional)"), "keep this note");

    await userEvent.click(screen.getByRole("tab", { name: "Approvers" }));
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("You have unsaved changes");
    await userEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Note (optional)")).toHaveValue("keep this note");
    expect(screen.getByRole("tab", { name: "Roles" })).toHaveAttribute("aria-selected", "true");

    screen.getByRole("tab", { name: "Roles" }).focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(await screen.findByRole("alertdialog")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Roles" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(screen.getByRole("tab", { name: "Approvers" })).toHaveAttribute("aria-selected", "true");
  });
});

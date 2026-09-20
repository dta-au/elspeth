import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as client from "@/api/client";
import * as admin from "@/api/identityAdmin";
import * as people from "@/api/people";
import * as workflow from "@/api/workflow";
import type { PersonResponse } from "@/types/people";
import { PeopleAccessDialog } from "./PeopleAccessDialog";
import { BOTH, IDENTITY_ONLY, LOCAL_ONLY, NEITHER, identityPerson, localPerson, page, refusal, roleView } from "./peopleTestFixtures";

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

function respond(person: PersonResponse["person"], capabilities = BOTH): PersonResponse {
  return { person, capabilities };
}

describe("PeopleAccessDialog", () => {
  const onClose = vi.fn();
  const onUnavailable = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(people.fetchPeopleCapabilities).mockResolvedValue(BOTH);
    vi.mocked(people.listPeople).mockResolvedValue(page([jane, sam], BOTH));
    vi.mocked(people.fetchPerson).mockImplementation((key) => Promise.resolve(respond(key === sam.key ? sam : jane)));
    vi.mocked(people.fetchPersonLabels).mockResolvedValue([]);
    vi.mocked(admin.listRoles).mockResolvedValue({ roles: [roleView()], limit: 50, offset: 0 });
    vi.mocked(admin.listRelationships).mockResolvedValue({ relationships: [], limit: 50, offset: 0 });
    vi.mocked(workflow.fetchIdentityQuota).mockResolvedValue({ identity_id: "jane-id", tokens_per_day: 1000, tokens_used_today: 10, container_tokens_per_day: 5000, storage_bytes: null, storage_bytes_used: 0, container_storage_bytes: null } as never);
  });

  function open(): ReturnType<typeof render> {
    return render(<PeopleAccessDialog onClose={onClose} onUnavailable={onUnavailable} />);
  }

  // ── The four callers ────────────────────────────────────────────────────

  it("hands back to the app when the caller holds neither capability", async () => {
    vi.mocked(people.fetchPeopleCapabilities).mockResolvedValue(NEITHER);
    open();
    await waitFor(() => expect(onUnavailable).toHaveBeenCalledOnce());
    expect(people.listPeople).not.toHaveBeenCalled();
  });

  it("tells a local-only administrator their scope and offers no access filters", async () => {
    vi.mocked(people.fetchPeopleCapabilities).mockResolvedValue(LOCAL_ONLY);
    vi.mocked(people.listPeople).mockResolvedValue(page([localPerson("alex", "Alex Kim", "not_visible")], LOCAL_ONLY));
    open();
    expect(await screen.findByText(/Showing local sign-in accounts only/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Status")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Type")).not.toBeInTheDocument();
    // A label that claimed "not set up" would assert something this caller cannot know.
    const row = await screen.findByRole("button", { name: /Alex Kim/ });
    expect(row).toHaveTextContent("Local account");
    expect(row).not.toHaveTextContent("not set up");
  });

  it("shows an identity-only administrator no credential controls", async () => {
    vi.mocked(people.fetchPeopleCapabilities).mockResolvedValue(IDENTITY_ONLY);
    vi.mocked(people.listPeople).mockResolvedValue(page([jane], IDENTITY_ONLY));
    open();
    await userEvent.click(await screen.findByRole("button", { name: /Jane Doe/ }));
    const signIn = await screen.findByRole("region", { name: "Sign-in for Jane Doe" });
    expect(signIn).toHaveTextContent("managed by the local account administrator");
    expect(within(signIn).queryByRole("button")).not.toBeInTheDocument();
    expect(within(screen.getByLabelText("Status")).queryByRole("option", { name: "Access not set up" })).not.toBeInTheDocument();
  });

  it("gives a caller with both capabilities access and credential controls for one person", async () => {
    const linked = identityPerson({}, { local_account: { username: "jane.doe", display_name: "Jane Doe", email: null, email_verified: false }, actions: { manage_access: true, manage_credentials: true, set_up_access: false, is_self: false } });
    vi.mocked(people.listPeople).mockResolvedValue(page([linked], BOTH));
    vi.mocked(people.fetchPerson).mockResolvedValue(respond(linked));
    open();
    await userEvent.click(await screen.findByRole("button", { name: /Jane Doe/ }));
    expect(await screen.findByRole("button", { name: "Disable access" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reset password for Jane Doe" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete local account for Jane Doe" })).toBeInTheDocument();
  });

  it("drops the identity half at once when that capability is lost while open", async () => {
    open();
    await userEvent.click(await screen.findByRole("button", { name: /Jane Doe/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Add role" }));
    vi.mocked(admin.grantRole).mockRejectedValue(refusal(404, "Not found"));
    vi.mocked(people.fetchPeopleCapabilities).mockResolvedValue(LOCAL_ONLY);
    vi.mocked(people.listPeople).mockResolvedValue(page([localPerson("jane.doe", "Jane Doe", "not_visible")], LOCAL_ONLY));
    await userEvent.click(screen.getByRole("button", { name: "Grant role" }));
    expect(await screen.findByText(/Your permissions changed/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disable access" })).not.toBeInTheDocument();
    expect(await screen.findByText(/Showing local sign-in accounts only/)).toBeInTheDocument();
  });

  // ── Directory states ────────────────────────────────────────────────────

  it("keeps loading, failure, empty and no-match as four different states", async () => {
    let settle: (value: Awaited<ReturnType<typeof people.listPeople>>) => void = () => undefined;
    vi.mocked(people.listPeople).mockReturnValueOnce(new Promise((resolve) => { settle = resolve; }));
    open();
    expect(await screen.findByText("Loading people…")).toBeInTheDocument();
    settle(page([], BOTH));
    expect(await screen.findByText(/There are no people here yet/)).toBeInTheDocument();

    vi.mocked(people.listPeople).mockResolvedValueOnce(page([], BOTH));
    await userEvent.type(screen.getByLabelText("Search people"), "nobody{Enter}");
    expect(await screen.findByText("No people match this search and these filters.")).toBeInTheDocument();

    vi.mocked(people.listPeople).mockRejectedValueOnce(refusal(503, "The identities store could not be read."));
    await userEvent.selectOptions(screen.getByLabelText("Status"), "pending");
    const alert = await screen.findByRole("alert");
    // A failed read must not keep saying "Loading", and must not pose as an empty roster.
    expect(alert).toHaveTextContent("could not be read");
    expect(alert).toHaveTextContent("nothing is shown");
    expect(screen.queryByText("Loading people…")).not.toBeInTheDocument();
    vi.mocked(people.listPeople).mockResolvedValueOnce(page([sam], BOTH));
    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: /Sam Lee/ })).toBeInTheDocument();
  });

  it("searches and pages on the server, never within the loaded page", async () => {
    vi.mocked(people.listPeople).mockResolvedValue(page([jane], BOTH, { has_more: true }));
    open();
    await screen.findByRole("button", { name: /Jane Doe/ });
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(vi.mocked(people.listPeople).mock.lastCall?.[0]).toMatchObject({ offset: 25 }));
    await userEvent.type(screen.getByLabelText("Search people"), "zed{Enter}");
    // A new search starts from the first page of the WHOLE directory.
    await waitFor(() => expect(vi.mocked(people.listPeople).mock.lastCall?.[0]).toMatchObject({ q: "zed", offset: 0 }));
    expect(screen.queryByText(/of \d+/)).not.toBeInTheDocument();
  });

  it("tells two people with one name apart without merging them", async () => {
    const other = identityPerson({ identity_id: "jane-2", provider: "oidc", subject: "00u-9931", username: "jdoe@partner.example" });
    vi.mocked(people.listPeople).mockResolvedValue(page([jane, other], BOTH));
    open();
    const rows = await screen.findAllByRole("button", { name: /Jane Doe/ });
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("jane.doe · local");
    expect(rows[1]).toHaveTextContent("jdoe@partner.example · oidc");
  });

  it("names a person prepared ahead of first sign-in from their linked account, but never a withheld one", async () => {
    const account = { username: "alex.kim", display_name: "Alex Kim", email: null, email_verified: false };
    const prepared = identityPerson({ identity_id: "alex-id", subject: "alex.kim", username: "alex.kim", display_name: null }, { local_account: account });
    const withheld = identityPerson({ identity_id: "w-id", subject: "w.subject", username: null, display_name: null, access_state: "pending", activated_at: null }, { local_account: { ...account, display_name: "Withheld Name" } });
    vi.mocked(people.listPeople).mockResolvedValue(page([prepared, withheld], BOTH));
    open();
    expect(await screen.findByRole("button", { name: /Alex Kim/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /w\.subject/ })).toBeInTheDocument();
    expect(screen.queryByText(/Withheld Name/)).not.toBeInTheDocument();
  });

  it("shows a never-admitted person by subject only", async () => {
    const pending = identityPerson({ identity_id: "p-1", provider: "oidc", subject: "subject-opaque", username: null, display_name: null, email: null, access_state: "pending", activated_at: null, last_login_at: null });
    vi.mocked(people.listPeople).mockResolvedValue(page([pending], BOTH, { active_human_admin_count: 1 }));
    open();
    const row = await screen.findByRole("button", { name: /subject-opaque/ });
    expect(row).toHaveTextContent("Pending access");
    expect(screen.getByText(/one active human administrator/)).toBeInTheDocument();
  });

  // ── Selection integrity ─────────────────────────────────────────────────

  it("never paints a slow answer for one person under another person's heading", async () => {
    let settleJane: (value: PersonResponse) => void = () => undefined;
    vi.mocked(people.fetchPerson).mockImplementation((key) => key === jane.key
      ? new Promise((resolve) => { settleJane = resolve; })
      : Promise.resolve(respond(sam)));
    open();
    await userEvent.click(await screen.findByRole("button", { name: /Jane Doe/ }));
    await userEvent.click(screen.getByRole("button", { name: /Sam Lee/ }));
    expect(await screen.findByRole("heading", { name: "Sam Lee" })).toBeInTheDocument();
    settleJane(respond(identityPerson({ display_name: "LATE JANE" })));
    await waitFor(() => expect(admin.listRoles).toHaveBeenLastCalledWith("sam-id", 0, 200));
    expect(screen.queryByText("LATE JANE")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Sam Lee" })).toBeInTheDocument();
  });

  it("warns before unsaved input is thrown away by choosing someone else", async () => {
    open();
    await userEvent.click(await screen.findByRole("button", { name: /Jane Doe/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Add role" }));
    await userEvent.type(screen.getByLabelText("Note (optional)"), "half-typed");
    await userEvent.click(screen.getByRole("button", { name: /Sam Lee/ }));
    const prompt = await screen.findByRole("alertdialog");
    expect(prompt).toHaveTextContent("unsaved changes");
    expect(prompt).toHaveFocus();
    await userEvent.click(within(prompt).getByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Note (optional)")).toHaveValue("half-typed");
    expect(screen.getByRole("heading", { name: "Jane Doe" })).toBeInTheDocument();
  });

  // ── Keyboard and focus ──────────────────────────────────────────────────

  it("unwinds Escape innermost first: the open form, then the panel", async () => {
    open();
    await userEvent.click(await screen.findByRole("button", { name: /Jane Doe/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Add role" }));
    expect(screen.getByRole("button", { name: "Grant role" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("button", { name: "Grant role" })).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("hands focus back to the control that opened a form when it is cancelled", async () => {
    open();
    await userEvent.click(await screen.findByRole("button", { name: /Jane Doe/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Add role" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    // Not <body>: a cancelled form unmounts, and focus must not be left on a removed node.
    await waitFor(() => expect(screen.getByRole("button", { name: "Add role" })).toHaveFocus());
    await userEvent.click(screen.getByRole("button", { name: "Disable access" }));
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByRole("button", { name: "Disable access" })).toHaveFocus());
  });

  it("is one modal and returns focus to its opener on close", async () => {
    const opener = document.createElement("button");
    document.body.append(opener);
    opener.focus();
    const { unmount } = open();
    const dialog = await screen.findByRole("dialog", { name: "People & access" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    await screen.findByRole("button", { name: /Jane Doe/ });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    unmount();
    expect(opener).toHaveFocus();
    opener.remove();
  });

  it("returns to the list with the query kept and focus on the row it left", async () => {
    open();
    await userEvent.type(await screen.findByLabelText("Search people"), "jane{Enter}");
    await userEvent.click(await screen.findByRole("button", { name: /Jane Doe/ }));
    await screen.findByRole("heading", { name: "Jane Doe" });
    await userEvent.click(screen.getByRole("button", { name: "Back to people" }));
    expect(screen.getByLabelText("Search people")).toHaveValue("jane");
    // jsdom applies no stylesheet, so both panes are in the tree: ask the results list for its row.
    await waitFor(() => expect(within(screen.getByRole("list", { name: "People results" })).getByRole("button", { name: /Jane Doe/ })).toHaveFocus());
  });

  it("walks the person sections with arrow, Home and End keys", async () => {
    open();
    await userEvent.click(await screen.findByRole("button", { name: /Jane Doe/ }));
    const roles = await screen.findByRole("tab", { name: "Roles" });
    roles.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Approvers" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Approvers" })).toHaveAttribute("aria-selected", "true");
    await userEvent.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Usage & limits" })).toHaveFocus();
    expect(await screen.findByText(/container ceiling is shared by everyone/)).toBeInTheDocument();
    await userEvent.keyboard("{Home}");
    expect(roles).toHaveFocus();
  });

  // ── One-time passwords ──────────────────────────────────────────────────

  describe("generated passwords", () => {
    const linked = identityPerson({}, { local_account: { username: "jane.doe", display_name: "Jane Doe", email: null, email_verified: false }, actions: { manage_access: true, manage_credentials: true, set_up_access: false, is_self: false } });

    beforeEach(() => {
      vi.mocked(people.listPeople).mockResolvedValue(page([linked], BOTH));
      vi.mocked(people.fetchPerson).mockResolvedValue(respond(linked));
      vi.mocked(client.resetAdminUserPassword).mockResolvedValue({ user_id: "jane.doe", password: "s3cret-Pass" });
    });

    async function resetPassword(): Promise<void> {
      open();
      await userEvent.click(await screen.findByRole("button", { name: /Jane Doe/ }));
      await userEvent.click(await screen.findByRole("button", { name: "Reset password for Jane Doe" }));
      // The confirmation must not promise that a reset signs anyone out.
      expect(screen.getByText(/already signed in stay signed in/)).toBeInTheDocument();
      await userEvent.click(screen.getByRole("button", { name: "Reset password" }));
    }

    it("moves focus to a heading that names the person, and keeps the password out of live regions", async () => {
      await resetPassword();
      const heading = await screen.findByRole("heading", { name: "Password reset for jane.doe" });
      expect(heading).toHaveFocus();
      const value = screen.getByTestId("generated-password");
      expect(value).toHaveTextContent("s3cret-Pass");
      expect(value.closest("[role=status], [role=alert], [aria-live]")).toBeNull();
    });

    it("leaves manual selection available when the clipboard refuses", async () => {
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
      await resetPassword();
      await userEvent.click(await screen.findByRole("button", { name: "Copy password" }));
      expect(await screen.findByText(/Select the password and copy it manually/)).toBeInTheDocument();
      expect(screen.getByTestId("generated-password")).toHaveTextContent("s3cret-Pass");
    });

    it("survives an unrelated role edit and is cleared only on dismissal", async () => {
      await resetPassword();
      await screen.findByTestId("generated-password");
      vi.mocked(admin.grantRole).mockResolvedValue(roleView({ role: "reviewer" }));
      await userEvent.click(screen.getByRole("button", { name: "Add role" }));
      await userEvent.click(screen.getByRole("button", { name: "Grant role" }));
      await screen.findByText("Granted User to Jane Doe.");
      expect(screen.getByTestId("generated-password")).toHaveTextContent("s3cret-Pass");
      await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
      expect(screen.queryByTestId("generated-password")).not.toBeInTheDocument();
    });

    it("keeps an earlier person's password on screen when a second one is generated", async () => {
      const sam = identityPerson({ identity_id: "sam-id", subject: "sam.lee", username: "sam.lee", display_name: "Sam Lee" }, { local_account: { username: "sam.lee", display_name: "Sam Lee", email: null, email_verified: false }, actions: { manage_access: true, manage_credentials: true, set_up_access: false, is_self: false } });
      vi.mocked(people.listPeople).mockResolvedValue(page([linked, sam], BOTH));
      await resetPassword();
      await screen.findByTestId("generated-password");
      vi.mocked(people.fetchPerson).mockResolvedValue(respond(sam));
      vi.mocked(client.resetAdminUserPassword).mockResolvedValue({ user_id: "sam.lee", password: "0ther-Pass" });
      await userEvent.click(screen.getByRole("button", { name: /Sam Lee/ }));
      await userEvent.click(await screen.findByRole("button", { name: "Reset password for Sam Lee" }));
      await userEvent.click(screen.getByRole("button", { name: "Reset password" }));
      await screen.findByRole("heading", { name: "Password reset for sam.lee" });
      // Neither can be shown again, so neither may replace the other.
      expect(screen.getAllByTestId("generated-password").map((node) => node.textContent)).toEqual(["s3cret-Pass", "0ther-Pass"]);
    });

    it("warns before closing while the only copy of a password is on screen", async () => {
      await resetPassword();
      await screen.findByTestId("generated-password");
      await userEvent.click(screen.getByRole("button", { name: "Close People & access" }));
      const prompt = await screen.findByRole("alertdialog");
      expect(prompt).toHaveTextContent("cannot be shown again");
      expect(onClose).not.toHaveBeenCalled();
      await userEvent.click(within(prompt).getByRole("button", { name: "Close without the password" }));
      expect(onClose).toHaveBeenCalledOnce();
    });

    it("says a lost reset answer needs a new reset, not retrieval", async () => {
      vi.mocked(client.resetAdminUserPassword).mockRejectedValue(new TypeError("Failed to fetch"));
      await resetPassword();
      expect(await screen.findByText(/cannot be shown again. Reset it once more/)).toBeInTheDocument();
      expect(screen.queryByTestId("generated-password")).not.toBeInTheDocument();
    });
  });
});

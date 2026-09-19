import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as admin from "@/api/identityAdmin";
import * as workflow from "@/api/workflow";
import type { IdentityAccessState, IdentityListResponse, IdentityView } from "@/types/identityAdmin";
import type { IdentityQuota } from "@/types/workflow";
import { IdentitiesTable } from "./IdentitiesTable";

vi.mock("@/api/identityAdmin", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/identityAdmin")>();
  return { ...actual, listIdentities: vi.fn() };
});
vi.mock("@/api/workflow", () => ({ fetchIdentityQuota: vi.fn(), setIdentityQuota: vi.fn() }));

const list = vi.mocked(admin.listIdentities);
const fetchQuota = vi.mocked(workflow.fetchIdentityQuota);
const setQuota = vi.mocked(workflow.setIdentityQuota);

function identity(overrides: Partial<IdentityView> = {}): IdentityView {
  return {
    identity_id: "id-alice", provider: "oidc", kind: "human", subject: "subject-alice",
    organisation_id: "org-a", access_state: "active", username: "alice",
    display_name: "Private display name", email: "private@example.test",
    first_seen_at: "2026-09-19T00:00:00Z", last_login_at: null,
    pre_provisioned_at: null, activated_at: "2026-09-19T00:00:00Z",
    activated_by_identity_id: "root", disabled_at: null, disabled_by_identity_id: null,
    disable_reason: null, ...overrides,
  };
}

function quota(overrides: Partial<IdentityQuota> = {}): IdentityQuota {
  return {
    identity_id: "id-alice", tokens_per_day: 500, storage_bytes: 2048,
    container_tokens_per_day: null, container_storage_bytes: null,
    tokens_used_today: 160, storage_bytes_used: 1024, ...overrides,
  };
}

function page(accessState: IdentityAccessState, identities: IdentityView[], offset = 0): IdentityListResponse {
  return { identities, access_state: accessState, limit: admin.ADMIN_PAGE_SIZE, offset, active_human_admin_count: 2 };
}

describe("IdentitiesTable quota rows", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    list.mockImplementation(async (accessState, offset) => page(accessState, accessState === "active" ? [identity()] : [], offset));
    fetchQuota.mockResolvedValue(quota());
    setQuota.mockResolvedValue(quota({ tokens_per_day: 750 }));
  });

  async function activeRows() {
    render(<IdentitiesTable currentIdentityId="root" />);
    await userEvent.selectOptions(screen.getByLabelText("Access state"), "active");
    return screen.findByRole("row", { name: /alice/ });
  }

  it("shows both current usages and identity limits inside each active row", async () => {
    const row = await activeRows();
    expect(await within(row).findByText("Tokens today: 160 of 500")).toBeInTheDocument();
    expect(within(row).getByText("Storage: 1.0 KB of 2.0 KB")).toBeInTheDocument();
    expect(screen.queryByText("Private display name")).toBeNull();
    expect(screen.queryByText("private@example.test")).toBeNull();
    expect(fetchQuota).toHaveBeenCalledExactlyOnceWith("id-alice");
  });

  it("does not request quota for pending or disabled rows", async () => {
    list.mockImplementation(async (accessState, offset) => page(accessState, [identity({ access_state: accessState, username: null })], offset));
    render(<IdentitiesTable currentIdentityId="root" />);
    expect(await screen.findByRole("row", { name: /subject-alice/ })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Access state"), "disabled");
    expect(await screen.findByRole("row", { name: /subject-alice/ })).toBeInTheDocument();
    expect(fetchQuota).not.toHaveBeenCalled();
  });

  it("distinguishes unknown usage, measured zero and unavailable quota", async () => {
    fetchQuota.mockResolvedValueOnce(quota({ tokens_used_today: null, storage_bytes_used: 0 }));
    const row = await activeRows();
    expect(await within(row).findByText("Tokens today: unknown of 500")).toBeInTheDocument();
    expect(within(row).getByText("Storage: 0 B of 2.0 KB")).toBeInTheDocument();
  });

  it("shows container ceilings when an identity has no individual cap", async () => {
    fetchQuota.mockResolvedValue(quota({
      tokens_per_day: null, container_tokens_per_day: 1000,
      storage_bytes: null, container_storage_bytes: 4096,
    }));
    const row = await activeRows();
    expect(await within(row).findByText("Tokens today: 160 of no identity cap (container ceiling 1000)")).toBeInTheDocument();
    expect(within(row).getByText("Storage: 1.0 KB of no identity cap (container ceiling 4.0 KB)")).toBeInTheDocument();
  });

  it("shows an unavailable state rather than a fabricated zero on failed read", async () => {
    fetchQuota.mockRejectedValue(new Error("quota route failed"));
    const row = await activeRows();
    expect(await within(row).findByText("Quota and usage unavailable")).toBeInTheDocument();
    expect(within(row).queryByText(/Tokens today:/)).toBeNull();
  });

  it("ignores a late first-page quota response after moving to another page", async () => {
    let resolveOld!: (value: IdentityQuota) => void;
    const old = new Promise<IdentityQuota>((resolve) => { resolveOld = resolve; });
    const firstPage = Array.from({ length: admin.ADMIN_PAGE_SIZE }, (_, index) => identity({
      identity_id: index === 0 ? "id-alice" : `id-${index}`,
      username: index === 0 ? "alice" : `user-${index}`,
    }));
    list.mockImplementation(async (accessState, offset) => page(accessState, accessState === "active" ? offset === 0 ? firstPage : [identity()] : [], offset));
    let aliceRequests = 0;
    fetchQuota.mockImplementation(async (id) => {
      if (id !== "id-alice") return quota({ identity_id: id });
      aliceRequests += 1;
      return aliceRequests === 1 ? old : quota({ tokens_per_day: 750 });
    });
    render(<IdentitiesTable currentIdentityId="root" />);
    await userEvent.selectOptions(screen.getByLabelText("Access state"), "active");
    await userEvent.click(await screen.findByRole("button", { name: "Next" }));
    const nextRow = await screen.findByRole("row", { name: /alice/ });
    expect(await within(nextRow).findByText("Tokens today: 160 of 750")).toBeInTheDocument();
    await act(async () => { resolveOld(quota({ tokens_per_day: 100 })); await old; });
    expect(within(nextRow).getByText("Tokens today: 160 of 750")).toBeInTheDocument();
    expect(within(nextRow).queryByText("Tokens today: 160 of 100")).toBeNull();
  });

  it("never displays a previous-page quota for an identity repeated on the next page", async () => {
    let resolveNext!: (value: IdentityQuota) => void;
    const nextQuota = new Promise<IdentityQuota>((resolve) => { resolveNext = resolve; });
    const firstPage = Array.from({ length: admin.ADMIN_PAGE_SIZE }, (_, index) => identity({
      identity_id: index === 0 ? "id-alice" : `id-${index}`,
      username: index === 0 ? "alice" : `user-${index}`,
    }));
    list.mockImplementation(async (accessState, offset) => page(accessState, accessState === "active" ? offset === 0 ? firstPage : [identity()] : [], offset));
    let aliceRequests = 0;
    fetchQuota.mockImplementation(async (id) => {
      if (id !== "id-alice") return quota({ identity_id: id });
      aliceRequests += 1;
      return aliceRequests === 1 ? quota({ tokens_per_day: 100 }) : nextQuota;
    });
    render(<IdentitiesTable currentIdentityId="root" />);
    await userEvent.selectOptions(screen.getByLabelText("Access state"), "active");
    expect(await screen.findByText("Tokens today: 160 of 100")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    const nextRow = await screen.findByRole("row", { name: /alice/ });
    expect(within(nextRow).getByText("Loading quota…")).toBeInTheDocument();
    expect(within(nextRow).queryByText("Tokens today: 160 of 100")).toBeNull();
    await act(async () => { resolveNext(quota({ tokens_per_day: 750 })); await nextQuota; });
    expect(within(nextRow).getByText("Tokens today: 160 of 750")).toBeInTheDocument();
  });

  it("updates the row immediately from the editor save and ignores its older fetch", async () => {
    let resolveOld!: (value: IdentityQuota) => void;
    const old = new Promise<IdentityQuota>((resolve) => { resolveOld = resolve; });
    let reads = 0;
    fetchQuota.mockImplementation(async () => { reads += 1; return reads === 1 ? old : quota(); });
    const row = await activeRows();
    expect(within(row).getByText("Loading quota…")).toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: "Quota" }));
    await screen.findByText("Quota for id-alice");
    await userEvent.type(screen.getByLabelText("New cap"), "750");
    await userEvent.click(screen.getByRole("button", { name: "Save cap" }));
    expect(setQuota).toHaveBeenCalledWith("id-alice", "tokens", 750);
    expect(await within(row).findByText("Tokens today: 160 of 750")).toBeInTheDocument();
    await act(async () => { resolveOld(quota({ tokens_per_day: 100 })); await old; });
    expect(within(row).getByText("Tokens today: 160 of 750")).toBeInTheDocument();
  });

  it("drops a prior unmounted editor's delayed save after a newer save for the same identity", async () => {
    const bob = identity({ identity_id: "id-bob", username: "bob", subject: "subject-bob" });
    list.mockImplementation(async (accessState, offset) => page(accessState, accessState === "active" ? [identity(), bob] : [], offset));
    fetchQuota.mockImplementation(async (id) => quota({ identity_id: id }));
    let resolveOldSave!: (value: IdentityQuota) => void;
    const oldSave = new Promise<IdentityQuota>((resolve) => { resolveOldSave = resolve; });
    let saves = 0;
    setQuota.mockImplementation(async () => { saves += 1; return saves === 1 ? oldSave : quota({ tokens_per_day: 750 }); });

    const aliceRow = await activeRows();
    expect(await within(aliceRow).findByText("Tokens today: 160 of 500")).toBeInTheDocument();
    await userEvent.click(within(aliceRow).getByRole("button", { name: "Quota" }));
    await screen.findByText("Quota for id-alice");
    await userEvent.type(screen.getByLabelText("New cap"), "100");
    await userEvent.click(screen.getByRole("button", { name: "Save cap" }));

    await userEvent.click(within(screen.getByRole("row", { name: /bob/ })).getByRole("button", { name: "Quota" }));
    await screen.findByText("Quota for id-bob");
    await userEvent.click(within(aliceRow).getByRole("button", { name: "Quota" }));
    await screen.findByText("Quota for id-alice");
    await userEvent.type(screen.getByLabelText("New cap"), "750");
    await userEvent.click(screen.getByRole("button", { name: "Save cap" }));
    expect(await within(aliceRow).findByText("Tokens today: 160 of 750")).toBeInTheDocument();

    await act(async () => { resolveOldSave(quota({ tokens_per_day: 100 })); await oldSave; });
    expect(within(aliceRow).getByText("Tokens today: 160 of 750")).toBeInTheDocument();
    expect(within(aliceRow).queryByText("Tokens today: 160 of 100")).toBeNull();
  });
});

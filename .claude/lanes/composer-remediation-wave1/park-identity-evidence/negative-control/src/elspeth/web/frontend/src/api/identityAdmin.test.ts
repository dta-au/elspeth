import { beforeEach, describe, expect, it, vi } from "vitest";
import { activateIdentity, adminErrorMessage, assertRelationship, listIdentities, listRoles, preProvisionIdentity, revokeRole } from "./identityAdmin";

const fetchMock = vi.fn();
const ok = (body: unknown): Response => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });

describe("identity admin API", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    globalThis.fetch = fetchMock;
    localStorage.setItem("auth_token", "admin-token");
  });

  it("passes the bounded page and state to the live admin route", async () => {
    fetchMock.mockResolvedValue(ok({ identities: [], access_state: "pending", limit: 50, offset: 50, active_human_admin_count: 1 }));
    await listIdentities("pending", 50);
    expect(fetchMock).toHaveBeenCalledWith("/api/auth/admin/identities?limit=50&offset=50&access_state=pending", {
      headers: { Authorization: "Bearer admin-token" }, cache: "no-store",
    });
    expect(() => listIdentities("active", 0, 201)).toThrow("Invalid admin page");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("sends exact activation and pre-provision bodies", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(ok({})));
    await activateIdentity("person/1", "none", "Admitted for operations");
    await preProvisionIdentity({ provider: "entra", subject: "opaque-subject", organisation_id: "org-a", role: "approver", note: "Known cohort" });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/auth/admin/identities/person%2F1/activate");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).toEqual({ role: "none", note: "Admitted for operations" });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body as string)).toEqual({ provider: "entra", subject: "opaque-subject", organisation_id: "org-a", role: "approver", note: "Known cohort" });
  });

  it("sends relationship and role changes to their specific routes", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(ok({})));
    await listRoles("person/1", 0);
    await assertRelationship({ from_identity_id: "a", to_identity_id: "b", relationship_type: "approver" });
    await revokeRole("role/1");
    expect(fetchMock.mock.calls[0][0]).toContain("identity_id=person%2F1");
    expect(JSON.parse(fetchMock.mock.calls[1][1].body as string)).toEqual({ from_identity_id: "a", to_identity_id: "b", relationship_type: "approver" });
    expect(fetchMock.mock.calls[2][0]).toBe("/api/auth/admin/roles/role%2F1/revoke");
  });

  it("shows a structured server refusal without treating it as a JavaScript Error", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: { refusal: "last_active_admin_protected", detail: "Last active admin is protected" } }), { status: 409, headers: { "content-type": "application/json" } }));
    const error = await activateIdentity("a", "user", "note").catch((failure: unknown) => failure);
    expect(adminErrorMessage(error, "Fallback")).toBe("Last active admin is protected");
    expect(error).toMatchObject({ status: 409, error_type: "last_active_admin_protected" });
  });
});

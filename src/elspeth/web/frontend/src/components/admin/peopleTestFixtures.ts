/** Records shaped exactly as the people read facade emits them, for the panel's tests. */
import type { IdentityView, RoleView } from "@/types/identityAdmin";
import type { IdentityPerson, LocalAccountPerson, PeopleCapabilities, PeopleListResponse, PersonRecord } from "@/types/people";

export const BOTH: PeopleCapabilities = { identity_admin: true, local_accounts: true, auth_provider: "local", self_identity_id: "root-id", self_username: "root" };
export const IDENTITY_ONLY: PeopleCapabilities = { ...BOTH, local_accounts: false };
export const LOCAL_ONLY: PeopleCapabilities = { ...BOTH, identity_admin: false };
export const NEITHER: PeopleCapabilities = { ...BOTH, identity_admin: false, local_accounts: false };

export function identityView(overrides: Partial<IdentityView> = {}): IdentityView {
  return {
    identity_id: "jane-id", provider: "local", kind: "human", subject: "jane.doe", organisation_id: null, access_state: "active",
    username: "jane.doe", display_name: "Jane Doe", email: "jane@corp.example", first_seen_at: "2026-09-01T00:00:00Z",
    last_login_at: "2026-09-19T00:00:00Z", pre_provisioned_at: null, activated_at: "2026-09-02T00:00:00Z", activated_by_identity_id: "root-id",
    disabled_at: null, disabled_by_identity_id: null, disable_reason: null,
    ...overrides,
  };
}

export function identityPerson(overrides: Partial<IdentityView> = {}, extra: Partial<Omit<IdentityPerson, "identity">> = {}): IdentityPerson {
  const identity = identityView(overrides);
  return {
    record_type: "identity", key: `identity:${identity.identity_id}`, identity, retired: false, local_account: null,
    actions: { manage_access: true, manage_credentials: false, set_up_access: false, is_self: false },
    ...extra,
  };
}

export function localPerson(username: string, displayName: string, access: LocalAccountPerson["access"], setUp = access === "not_set_up"): LocalAccountPerson {
  return {
    record_type: "local_account", key: `local:${username}`, access,
    local_account: { username, display_name: displayName, email: null, email_verified: false },
    actions: { manage_access: false, manage_credentials: true, set_up_access: setUp, is_self: false },
  };
}

export function page(people: PersonRecord[], capabilities: PeopleCapabilities, extra: Partial<PeopleListResponse> = {}): PeopleListResponse {
  return { people, limit: 25, offset: 0, has_more: false, capabilities, active_human_admin_count: capabilities.identity_admin ? 2 : null, ...extra };
}

export function roleView(overrides: Partial<RoleView> = {}): RoleView {
  return { role_id: "role-1", identity_id: "jane-id", role: "user", scope: null, expires_at: null, note: null, granted_by_identity_id: "root-id", granted_at: "2026-09-02T00:00:00Z", revoked_at: null, ...overrides };
}

/** What parseResponse rejects with for a server refusal. */
export function refusal(status: number, detail: string): { status: number; detail: string } {
  return { status, detail };
}

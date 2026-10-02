/** Wire contracts for the people directory read facade (/api/auth/admin/people). */
import type { IdentityAccessState, IdentityProvider, IdentityView } from "./identityAdmin";

/** What the signed-in caller may administer. Advice for the shell: every
 *  route re-checks its own guard, so a stale `true` here is refused there. */
export interface PeopleCapabilities {
  identity_admin: boolean;
  local_accounts: boolean;
  auth_provider: string;
  /** The deployment runs the quota system; off, no personal cap can be stored. */
  quotas_enabled: boolean;
  self_identity_id: string;
  self_username: string;
}

export interface LocalAccountView {
  username: string;
  display_name: string;
  email: string | null;
  email_verified: boolean;
}

export interface PersonActions {
  manage_access: boolean;
  manage_credentials: boolean;
  set_up_access: boolean;
  is_self: boolean;
}

/** A person the container holds an identity row for. */
export interface IdentityPerson {
  record_type: "identity";
  key: string;
  identity: IdentityView;
  /** History left behind by a deleted local account; not someone who can sign in. */
  retired: boolean;
  /** The only active human administrator: the server refuses to disable them. Advisory. */
  sole_active_admin: boolean;
  local_account: LocalAccountView | null;
  actions: PersonActions;
}

/** A local account shown without an identity. `not_set_up` is a fact about
 *  both stores; `not_visible` claims nothing because the caller may not read
 *  identities. The two must never be rendered as the same thing. */
export interface LocalAccountPerson {
  record_type: "local_account";
  key: string;
  local_account: LocalAccountView;
  access: "not_set_up" | "not_visible";
  actions: PersonActions;
}

export type PersonRecord = IdentityPerson | LocalAccountPerson;

export type PeopleStatusFilter = "all" | IdentityAccessState | "not_set_up";
export type PeopleTypeFilter = "people" | "service" | "all";

export interface PeopleQuery {
  q: string;
  status: PeopleStatusFilter;
  provider: IdentityProvider | "all";
  type: PeopleTypeFilter;
  offset: number;
}

export interface PeopleListResponse {
  people: PersonRecord[];
  limit: number;
  offset: number;
  has_more: boolean;
  capabilities: PeopleCapabilities;
  active_human_admin_count: number | null;
}

export interface PersonResponse {
  person: PersonRecord;
  capabilities: PeopleCapabilities;
}

export interface PersonLabel {
  identity_id: string;
  label: string;
  /** The username or subject that tells namesakes apart; the provider is beside it as data. */
  detail: string;
  provider: IdentityProvider;
  kind: "human" | "service";
  access_state: IdentityAccessState;
  retired: boolean;
}

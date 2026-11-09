/** Wire contracts for the role-guarded /api/auth/admin surface. */
export type IdentityAccessState = "pending" | "active" | "disabled";
export type IdentityProvider = "local" | "oidc" | "entra" | "vanguard" | "google" | "service";
/** Human onboarding through this dialog excludes operator-managed service identities. */
export type HumanProvisionProvider = Exclude<IdentityProvider, "service">;
export type IdentityRole = "admin" | "approver" | "reviewer" | "user" | "curator" | "auditor" | "oversight";
export type ActivationRole = "user" | "approver" | "reviewer" | "none";

export interface IdentityView {
  identity_id: string;
  provider: IdentityProvider;
  kind: "human" | "service";
  subject: string;
  organisation_id: string | null;
  access_state: IdentityAccessState;
  username: string | null;
  display_name: string | null;
  email: string | null;
  first_seen_at: string;
  last_login_at: string | null;
  pre_provisioned_at: string | null;
  activated_at: string | null;
  activated_by_identity_id: string | null;
  disabled_at: string | null;
  disabled_by_identity_id: string | null;
  disable_reason: string | null;
}

export interface IdentityListResponse {
  identities: IdentityView[];
  access_state: IdentityAccessState;
  limit: number;
  offset: number;
  active_human_admin_count: number;
}

export interface RoleView {
  role_id: string;
  identity_id: string;
  role: IdentityRole;
  scope: string | null;
  expires_at: string | null;
  note: string | null;
  granted_by_identity_id: string | null;
  granted_at: string;
  revoked_at: string | null;
}

export interface RoleListResponse {
  roles: RoleView[];
  limit: number;
  offset: number;
}

export interface RelationshipView {
  relationship_id: string;
  from_identity_id: string;
  to_identity_id: string;
  relationship_type: "approver";
  asserted_by_identity_id: string;
  asserted_at: string;
  effective_from: string | null;
  effective_until: string | null;
  note: string | null;
  revoked_at: string | null;
  revoked_by_identity_id: string | null;
}

export interface RelationshipListResponse {
  relationships: RelationshipView[];
  limit: number;
  offset: number;
}

export interface ActivationResponse {
  identity: IdentityView;
  role: RoleView | null;
  quota_written: boolean;
}

export interface DisableResponse {
  identity: IdentityView;
  revoked_relationship_ids: string[];
}

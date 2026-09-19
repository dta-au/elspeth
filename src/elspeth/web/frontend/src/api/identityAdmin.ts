/** Requests to the live-role-guarded identity administration API. */
import { authHeaders, parseResponse } from "./client";
import type {
  ActivationResponse,
  ActivationRole,
  DisableResponse,
  IdentityAccessState,
  IdentityListResponse,
  HumanProvisionProvider,
  IdentityRole,
  IdentityView,
  RelationshipListResponse,
  RelationshipView,
  RoleListResponse,
  RoleView,
} from "@/types/identityAdmin";

export const ADMIN_PAGE_SIZE = 50;
const BASE = "/api/auth/admin";

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`, { headers: authHeaders(), cache: "no-store" });
  return parseResponse<T>(response);
}

/** parseResponse throws an ApiError record; show its bounded server detail. */
export function adminErrorMessage(error: unknown, fallback: string): string {
  if (typeof error === "object" && error !== null && "detail" in error && typeof error.detail === "string") {
    return error.detail;
  }
  return error instanceof Error ? error.message : fallback;
}

async function post<T>(path: string, body: object): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: authHeaders("application/json"),
    body: JSON.stringify(body),
  });
  return parseResponse<T>(response);
}

function pageQuery(limit: number, offset: number): URLSearchParams {
  if (!Number.isInteger(limit) || limit < 1 || limit > 200 || !Number.isInteger(offset) || offset < 0) {
    throw new Error("Invalid admin page");
  }
  return new URLSearchParams({ limit: String(limit), offset: String(offset) });
}

export function listIdentities(accessState: IdentityAccessState, offset = 0, limit = ADMIN_PAGE_SIZE): Promise<IdentityListResponse> {
  const query = pageQuery(limit, offset);
  query.set("access_state", accessState);
  return get(`/identities?${query}`);
}

export function preProvisionIdentity(body: {
  provider: HumanProvisionProvider;
  subject: string;
  organisation_id?: string;
  role: ActivationRole;
  note: string;
}): Promise<ActivationResponse> {
  return post("/identities", body);
}

export function activateIdentity(identityId: string, role: ActivationRole, note: string): Promise<ActivationResponse> {
  return post(`/identities/${encodeURIComponent(identityId)}/activate`, { role, note });
}

export function enableIdentity(identityId: string, note: string): Promise<IdentityView> {
  return post(`/identities/${encodeURIComponent(identityId)}/enable`, { note });
}

export function disableIdentity(identityId: string, reason: string): Promise<DisableResponse> {
  return post(`/identities/${encodeURIComponent(identityId)}/disable`, { reason });
}

export function listRoles(identityId: string | null, offset = 0, limit = ADMIN_PAGE_SIZE): Promise<RoleListResponse> {
  const query = pageQuery(limit, offset);
  if (identityId !== null && identityId !== "") query.set("identity_id", identityId);
  return get(`/roles?${query}`);
}

export function grantRole(body: {
  identity_id: string;
  role: IdentityRole;
  expires_at?: string;
  note?: string;
}): Promise<RoleView> {
  return post("/roles", body);
}

export function revokeRole(roleId: string, note?: string): Promise<RoleView> {
  return post(`/roles/${encodeURIComponent(roleId)}/revoke`, note ? { note } : {});
}

export function listRelationships(identityId: string | null, offset = 0, limit = ADMIN_PAGE_SIZE): Promise<RelationshipListResponse> {
  const query = pageQuery(limit, offset);
  if (identityId !== null && identityId !== "") query.set("identity_id", identityId);
  return get(`/relationships?${query}`);
}

export function assertRelationship(body: {
  from_identity_id: string;
  to_identity_id: string;
  relationship_type: "approver";
  effective_from?: string;
  effective_until?: string;
  note?: string;
}): Promise<RelationshipView> {
  return post("/relationships", body);
}

export function revokeRelationship(relationshipId: string, note?: string): Promise<RelationshipView> {
  return post(`/relationships/${encodeURIComponent(relationshipId)}/revoke`, note ? { note } : {});
}

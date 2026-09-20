import type { IdentityRole } from "@/types/identityAdmin";

/** The viewer's zone, named, so a typed expiry is never silently UTC or silently local. */
export function viewerTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
}

export function formatInstant(iso: string): string {
  const instant = new Date(iso);
  if (Number.isNaN(instant.getTime())) return iso;
  // Explicit fields, not dateStyle/timeStyle: Intl refuses to combine those
  // with timeZoneName, and the zone is the part that must not be dropped.
  return instant.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short" });
}

/** A `datetime-local` value is wall-clock time in the viewer's zone; the wire wants a UTC instant. */
export function localInputToUtcIso(value: string): string | null {
  const instant = new Date(value);
  return Number.isNaN(instant.getTime()) ? null : instant.toISOString();
}

export const ROLE_LABEL: Record<IdentityRole, string> = {
  admin: "Administrator",
  user: "User",
  approver: "Approver",
  reviewer: "Reviewer",
  curator: "Curator",
  auditor: "Auditor",
  oversight: "Oversight",
};

export const ROLE_PURPOSE: Record<IdentityRole, string> = {
  admin: "Manages people, roles, approvers and limits. Cannot be combined with a role that builds, approves, reviews or publishes work.",
  user: "Builds and runs pipelines.",
  approver: "Decides approval requests from the people assigned to them.",
  reviewer: "Reviews pipelines and attests to them.",
  curator: "Decides what is published to the shared library.",
  auditor: "Reads the audit records. Cannot change anything.",
  oversight: "Reads usage across people and sets limits. Cannot approve access, grant roles or disable anyone.",
};

const WORKLOAD_ROLES: ReadonlySet<IdentityRole> = new Set(["user", "approver", "reviewer", "curator"]);
const SERVICE_ROLES: ReadonlySet<IdentityRole> = new Set(["admin", "oversight"]);

/**
 * Why a grant is likely to be refused, said before the administrator submits.
 * ADVICE ONLY: the server owns these rules and its refusal is what counts.
 */
export function roleConflictAdvice(role: IdentityRole, held: readonly IdentityRole[], kind: "human" | "service"): string | null {
  if (kind === "service" && !SERVICE_ROLES.has(role)) return "A service account can hold only Administrator or Oversight.";
  if (held.includes(role)) return "This person already holds this role deployment-wide.";
  if (role === "admin" && held.some((value) => WORKLOAD_ROLES.has(value))) {
    return "Administrator cannot be combined with User, Approver, Reviewer or Curator. Revoke those roles first.";
  }
  if (WORKLOAD_ROLES.has(role) && held.includes("admin")) {
    return "This person is an Administrator, which cannot be combined with this role. Revoke Administrator first.";
  }
  return null;
}

import type { AuditReadinessSnapshot } from "@/types/api";

/**
 * Admit a cached audit snapshot only for the exact active composition.
 *
 * The store is keyed by session, but the payload remains authoritative: both
 * identities must agree before any workspace surface may project readiness.
 */
export function matchingAuditReadinessSnapshot(
  snapshot: AuditReadinessSnapshot | undefined,
  activeSessionId: string | null,
  compositionVersion: number | null,
): AuditReadinessSnapshot | undefined {
  if (
    snapshot === undefined ||
    activeSessionId === null ||
    compositionVersion === null ||
    snapshot.session_id !== activeSessionId ||
    snapshot.composition_version !== compositionVersion
  ) {
    return undefined;
  }
  return snapshot;
}

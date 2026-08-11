import { matchingAuditReadinessSnapshot } from "@/lib/auditReadinessFreshness";
import type {
  AuditReadinessSnapshot,
  ValidationResult,
} from "@/types/api";

export type WorkspaceStatusTone =
  | "neutral"
  | "busy"
  | "success"
  | "warning"
  | "error";

export interface WorkspaceStatus {
  text: string;
  tone: WorkspaceStatusTone;
  accessibleLabel: string;
}

function validationStatus(
  text: string,
  tone: WorkspaceStatusTone,
): WorkspaceStatus {
  return { text, tone, accessibleLabel: `Validation: ${text}` };
}

export function projectValidationWorkspaceStatus(
  validationResult: ValidationResult | null,
): WorkspaceStatus {
  if (validationResult === null) {
    return validationStatus("Not checked", "neutral");
  }
  const errorCount = validationResult.errors.length;
  if (errorCount > 0) {
    return validationStatus(`${errorCount} errors`, "error");
  }
  const warningCount = validationResult.warnings?.length ?? 0;
  if (warningCount > 0) {
    return validationStatus(`${warningCount} warnings`, "warning");
  }
  return validationStatus("Passed", "success");
}

interface AuditWorkspaceStatusInputs {
  activeSessionId: string | null;
  compositionVersion: number | null;
  snapshotsBySession: Record<string, AuditReadinessSnapshot>;
  errorBySession: Record<string, string | null>;
}

function auditStatus(
  text: string,
  tone: WorkspaceStatusTone,
): WorkspaceStatus {
  return { text, tone, accessibleLabel: `Audit: ${text}` };
}

export function projectAuditWorkspaceStatus({
  activeSessionId,
  compositionVersion,
  snapshotsBySession,
  errorBySession,
}: AuditWorkspaceStatusInputs): WorkspaceStatus {
  const error =
    activeSessionId === null
      ? null
      : errorBySession[activeSessionId] ?? null;
  if (error !== null) return auditStatus("Checking", "busy");

  const cached =
    activeSessionId === null
      ? undefined
      : snapshotsBySession[activeSessionId];
  const snapshot = matchingAuditReadinessSnapshot(
    cached,
    activeSessionId,
    compositionVersion,
  );
  if (snapshot === undefined) return auditStatus("Checking", "busy");

  const issueRows = snapshot.rows.filter(
    (row) => row.status === "warning" || row.status === "error",
  );
  if (issueRows.length === 0) return auditStatus("Ready", "success");
  const tone = issueRows.some((row) => row.status === "error")
    ? "error"
    : "warning";
  return auditStatus(`${issueRows.length} issues`, tone);
}

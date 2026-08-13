import { matchingAuditReadinessSnapshot } from "@/lib/auditReadinessFreshness";
import type {
  AuditReadinessSnapshot,
  ValidationResult,
} from "@/types/api";
import { plural } from "@/utils/plural";

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
  isValidating = false,
  validationError: string | null = null,
): WorkspaceStatus {
  if (isValidating) return validationStatus("Checking", "busy");
  if (validationError !== null) {
    return validationStatus("Check failed", "error");
  }
  if (validationResult === null) {
    return validationStatus("Not checked", "neutral");
  }
  const errorCount = validationResult.errors.length;
  if (errorCount > 0) {
    return validationStatus(plural(errorCount, "error"), "error");
  }
  if (!validationResult.is_valid) return validationStatus("Failed", "error");
  const warningCount = validationResult.warnings?.length ?? 0;
  if (warningCount > 0) {
    return validationStatus(plural(warningCount, "warning"), "warning");
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
  if (error !== null) return auditStatus("Error", "error");

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

  const expectedIds = new Set([
    "validation", "plugin_trust", "provenance", "retention",
    "llm_interpretations", "secrets",
  ]);
  const rowIds = snapshot.rows.map((row) => row.id);
  const validationRow = snapshot.rows.find((row) => row.id === "validation");
  if (
    rowIds.length !== expectedIds.size ||
    new Set(rowIds).size !== expectedIds.size ||
    rowIds.some((id) => !expectedIds.has(id)) ||
    validationRow === undefined ||
    (!snapshot.validation_result.is_valid && validationRow.status !== "error") ||
    (snapshot.validation_result.is_valid && validationRow.status === "error")
  ) {
    return auditStatus("Error", "error");
  }

  const issueRows = snapshot.rows.filter(
    (row) => row.status === "warning" || row.status === "error",
  );
  if (issueRows.length === 0) return auditStatus("Ready", "success");
  const tone = issueRows.some((row) => row.status === "error")
    ? "error"
    : "warning";
  return auditStatus(plural(issueRows.length, "issue"), tone);
}
